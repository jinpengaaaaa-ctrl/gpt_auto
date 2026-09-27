"""运行时依赖健康检查（Windows）— greenlet / playwright / VC++ 运行库.

背景：Windows Server 2022 常缺 VC++ Redistributable，greenlet 的 C 扩展
``_greenlet.pyd`` 依赖 ``vcruntime140*.dll``／``msvcp140.dll``，缺失时浏览器
启动阶段报 ``DLL load failed while importing _greenlet``。本模块在安装脚本与
浏览器预检阶段提前检测并给出可读提示，避免批次启动后才失败。
"""

from __future__ import annotations

import ctypes
import platform
import sys

_VC_RUNTIME_DLLS = ("vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll")
_VC_RUNTIME_URL = "https://aka.ms/vs/17/release/vc_redist.x64.exe"


def _vc_runtime_missing() -> tuple[str, ...]:
    """Return the VC++ runtime DLLs that cannot be loaded (Windows only)."""
    if sys.platform != "win32":
        return ()
    missing: list[str] = []
    for dll in _VC_RUNTIME_DLLS:
        try:
            ctypes.WinDLL(dll)
        except OSError:
            missing.append(dll)
    return tuple(missing)


def _check_package_files_corrupt(pkg_name: str) -> bool:
    """True if installed package directory has empty/corrupt .py source files."""
    import importlib.util
    try:
        spec = importlib.util.find_spec(pkg_name)
        if spec is None or spec.origin is None:
            return False
        from pathlib import Path
        origin = Path(spec.origin)
        if origin.is_file() and origin.stat().st_size == 0:
            return True
        pkg_dir = origin.parent
        if pkg_dir.is_dir():
            init_file = pkg_dir / "__init__.py"
            if init_file.exists() and init_file.stat().st_size == 0:
                return True
    except Exception:
        pass
    return False


def check_runtime_dependencies() -> list[str]:
    """Return human-readable environment problems; an empty list means OK.

    检查项：
        * Python 必须为 64 位（greenlet/playwright 均发布 64 位 wheel）；
        * 包源码文件未损坏（防止 0 字节损坏包误报）；
        * greenlet 可导入（缺失或损坏时给出重装指引）；
        * playwright 可导入；
        * Windows 上 VC++ 运行库可加载（缺失时给出官方安装地址）。
    """
    problems: list[str] = []
    arch = platform.architecture()[0]
    if arch != "64bit":
        problems.append(
            f"Python 必须是 64 位（当前 {arch}）；请重新安装 64 位 Python 3.12+。"
        )
    if _check_package_files_corrupt("playwright"):
        problems.append(
            "playwright 源码文件损坏或为空；请在对应 Python 环境执行: "
            "python -m pip install --force-reinstall playwright==1.60.0"
        )
    if _check_package_files_corrupt("cloakbrowser"):
        problems.append(
            "cloakbrowser 源码文件损坏或为空；请在对应 Python 环境执行: "
            "python -m pip install --force-reinstall cloakbrowser==0.5.9"
        )
    try:
        import greenlet  # noqa: F401
    except Exception as exc:  # noqa: BLE001 - report the failure, never crash
        problems.append(
            "greenlet 导入失败（"
            f"{type(exc).__name__}: {exc}）；请执行 "
            "python -m pip install --only-binary=:all: --force-reinstall greenlet"
        )
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        if not any("playwright 源码文件损坏" in p for p in problems):
            problems.append(
                "playwright 导入失败（"
                f"{type(exc).__name__}: {exc}）；请执行: "
                "python -m pip install --force-reinstall playwright==1.60.0"
            )
    if sys.platform == "win32":
        missing = _vc_runtime_missing()
        if missing:
            problems.append(
                "缺少 VC++ 运行库（"
                f"{', '.join(missing)}）；请安装 VC++ Redistributable "
                f"2015-2022 x64：{_VC_RUNTIME_URL}"
            )
    return problems
