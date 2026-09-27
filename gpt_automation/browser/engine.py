"""
CloakBrowser 引擎 — 为每个账号在本地创建物理级隔离的持久化指纹环境。
"""

import asyncio
import importlib
import logging
import os
import shutil  # noqa: F401 - re-exported below for backward compatibility
import subprocess
import sys
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Optional
import config
from gpt_automation.browser.models import (
    AcquiredBrowser,
    BackendAvailability,
    BackendReport,
    BrowserBackend,
    BrowserLaunchRequest,
    BrowserSession,
)
from gpt_automation.browser.cleanup import cleanup_profile  # noqa: F401
from gpt_automation.browser.session_monitor import attach_session_loss_monitor
from gpt_automation.network.ip_detector import get_ip_from_page
from gpt_automation.utils.playwright_errors import (
    SessionLostError,
    is_session_lost,
)

logger = logging.getLogger(__name__)
CLOAKBROWSER_PACKAGE = "cloakbrowser"
CLOAKBROWSER_UPDATE_TIMEOUT_SECONDS = 180

# Load the third-party module lazily, after the per-run wrapper update.  These
# references remain module attributes so tests can replace the binary API.
binary_info = None
ensure_binary = None
check_for_update = None
_launch_persistent_context_async = None
_cloakbrowser_module = None


@dataclass(frozen=True)
class CloakBrowserRuntimeReport:
    """Read-only runtime versions used for startup diagnostics."""

    wrapper_version: str
    binary_version: Optional[str]
    binary_path: Optional[str]
    binary_tier: Optional[str]
    license_configured: bool


def _load_cloakbrowser_api(*, reload: bool = False) -> None:
    """Load the installed CloakBrowser API only after wrapper management."""
    global _cloakbrowser_module
    global binary_info, ensure_binary, check_for_update
    global _launch_persistent_context_async

    loaded = _cloakbrowser_module is None
    if loaded:
        _cloakbrowser_module = importlib.import_module(CLOAKBROWSER_PACKAGE)
    elif reload:
        _cloakbrowser_module = importlib.reload(_cloakbrowser_module)

    if loaded or reload:
        binary_info = getattr(_cloakbrowser_module, "binary_info", None)
        ensure_binary = getattr(_cloakbrowser_module, "ensure_binary", None)
        check_for_update = getattr(_cloakbrowser_module, "check_for_update", None)
        _launch_persistent_context_async = getattr(
            _cloakbrowser_module,
            "launch_persistent_context_async",
            None,
        )


def update_cloakbrowser_wrapper() -> str:
    """Upgrade the wrapper in the interpreter that will run registration."""
    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--upgrade",
        "--no-input",
        "--disable-pip-version-check",
        CLOAKBROWSER_PACKAGE,
    ]
    try:
        subprocess.run(
            command,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=CLOAKBROWSER_UPDATE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(
            "CloakBrowser Python wrapper update failed; "
            "registration is blocked until the runtime can be updated"
        ) from exc

    importlib.invalidate_caches()
    installed_version = version(CLOAKBROWSER_PACKAGE)
    _load_cloakbrowser_api(reload=True)
    return installed_version


def inspect_cloakbrowser_runtime() -> CloakBrowserRuntimeReport:
    """Inspect installed wrapper and cached binary without downloading or upgrading."""
    _load_cloakbrowser_api()
    try:
        installed_version = version(CLOAKBROWSER_PACKAGE)
    except PackageNotFoundError as exc:
        raise RuntimeError(
            "cloakbrowser is not installed; run the per-run wrapper updater"
        ) from exc

    info = {}
    if binary_info is not None:
        try:
            info = binary_info() or {}
        except Exception as exc:
            logger.warning("CloakBrowser binary inspection failed: %s", exc)
    return CloakBrowserRuntimeReport(
        wrapper_version=installed_version,
        binary_version=info.get("version"),
        binary_path=info.get("path"),
        binary_tier=info.get("tier"),
        license_configured=bool(os.environ.get("CLOAKBROWSER_LICENSE_KEY")),
    )


def ensure_cloakbrowser_latest(*, update_wrapper: bool = True) -> str:
    """Update and report the wrapper before any browser context is created."""
    if update_wrapper:
        update_cloakbrowser_wrapper()
    else:
        _load_cloakbrowser_api()
    if _launch_persistent_context_async is None:
        raise RuntimeError(
            "CloakBrowser wrapper is incompatible: "
            "launch_persistent_context_async is unavailable"
        )
    report = inspect_cloakbrowser_runtime()
    print(f"✅ [CloakBrowser] Python wrapper: {report.wrapper_version}")
    if report.binary_version:
        tier = report.binary_tier or "unknown"
        print(f"   Chromium binary: {report.binary_version} ({tier})")
    else:
        print("   Chromium binary: 未缓存，将在浏览器预检阶段按授权策略准备")
    if not report.license_configured:
        print("   授权状态: 未配置 CloakBrowser license，使用免费二进制策略")
    return report.wrapper_version


def prepare_cloakbrowser_binary(
    *,
    allow_update: bool = False,
) -> CloakBrowserRuntimeReport:
    """Ensure the authorized binary exists, optionally applying a binary update."""
    if binary_info is None and ensure_binary is None and check_for_update is None:
        _load_cloakbrowser_api()
    if allow_update and check_for_update is not None:
        updated_version = check_for_update()
        if updated_version:
            print(f"   Chromium binary 已更新：{updated_version}")
    if ensure_binary is None:
        return inspect_cloakbrowser_runtime()
    binary_path = ensure_binary()
    report = inspect_cloakbrowser_runtime()
    if report.binary_path is None:
        print(f"   Chromium binary path: {binary_path}")
    return report


async def warmup_cloak_session(page) -> None:
    """Heartbeat-probe a freshly launched session for ``duration_s`` seconds.

    The free CloakBrowser binary crashes most often shortly after launch
    (observed ~7s after NETWORK_READY).  A heartbeat that dies inside the
    warmup window surfaces as :class:`SessionLostError` so the caller treats
    it as a launch failure instead of entering the business flow.
    """
    warmup_cfg = getattr(config, "CLOAKBROWSER_WARMUP", {})
    if not warmup_cfg.get("enabled", True):
        return
    duration = float(warmup_cfg.get("duration_s", 30.0))
    interval = float(warmup_cfg.get("interval_s", 5.0))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + duration
    logger.info(
        "CloakBrowser warmup: probing for %.0fs (interval %.0fs)",
        duration,
        interval,
    )
    while True:
        try:
            await page.evaluate("1")
        except Exception as exc:  # noqa: BLE001 - classified, not swallowed
            if is_session_lost(exc):
                raise SessionLostError("target_closed") from exc
            raise
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        await asyncio.sleep(min(interval, remaining))


async def create_isolated_cloak_context(profile_id: str, proxy: Optional[dict] = None):
    """
    基于 CloakBrowser 为每个账号在本地创建物理级隔离的持久化指纹环境。

    Args:
        profile_id: 标识符（如 email 前缀），用于构建隔离的 profile 目录。

    Returns:
        (context, page, current_ip)
    """
    user_profile_path = os.path.abspath(
        f"{config.PROFILE_DIR}/profile_{profile_id}"
    )

    if not os.path.exists(config.EVIDENCE_DIR):
        os.makedirs(config.EVIDENCE_DIR)

    print("\U0001f512 [环境隔离] 正在为账户启动 CloakBrowser C++ 原生指纹隔离沙盒...")
    print(f"   持久化目录: {user_profile_path}")

    # 清理残留的 Chromium 进程锁，避免启动互斥或崩溃误判
    for lock_name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        lock_path = os.path.join(user_profile_path, lock_name)
        if os.path.exists(lock_path):
            try:
                os.remove(lock_path)
            except Exception:
                pass

    launch_kwargs = {
        "user_data_dir": user_profile_path,
        "headless": False,
        "locale": "ja-JP",
        "timezone": "Asia/Tokyo",
        "humanize": True,
        "args": [
            "--disable-infobars",
            "--lang=ja-JP",
            "--disable-session-crashed-bubble",
            "--hide-crash-restore-bubble",
        ],
    }

    # Manual hotspot mode must not inherit a stale system/environment proxy.
    if config.IP_ROTATION_CONFIG.get("rotation_mode") == "manual-hotspot":
        launch_kwargs["args"].append("--no-proxy-server")

    p_cfg = getattr(config, "PROXY_CONFIG", {})
    if proxy is not None:
        # Unified proxy boundary: the caller (e.g. NetworkStrategy through
        # BrowserLaunchRequest) resolved the proxy for this run.  Keep the
        # Cloak launch arguments otherwise identical.
        proxy_opts = dict(proxy)
        launch_kwargs["proxy"] = proxy_opts
        print(
            f"\U0001f310 [代理整合] 已为 CloakBrowser 挂载统一注入的代理: "
            f"{proxy_opts.get('server')}"
        )
    elif p_cfg.get("enabled", False):
        if p_cfg.get("dynamic_api_url"):
            try:
                print("\U0001f310 [代理整合] 正在请求代理商 API 刷新动态住宅 IP...")
                import httpx
                resp = httpx.get(p_cfg["dynamic_api_url"], timeout=5.0)
                print(f"   API 刷 IP 结果: {resp.text.strip()[:100]}")
            except Exception as e:
                print(f"\u26a0\ufe0f [代理整合] 请求代理 IP 刷新接口异常: {e}")

        proxy_opts = {"server": p_cfg["server"]}
        if p_cfg.get("username"):
            proxy_opts["username"] = p_cfg["username"]
        if p_cfg.get("password"):
            proxy_opts["password"] = p_cfg["password"]
        launch_kwargs["proxy"] = proxy_opts
        print(f"\U0001f310 [代理整合] 已为 CloakBrowser 挂载代理: {p_cfg['server']}")

    _load_cloakbrowser_api()
    if _launch_persistent_context_async is None:
        raise RuntimeError("CloakBrowser launch_persistent_context_async is unavailable")
    context = await _launch_persistent_context_async(**launch_kwargs)
    existing_pages = getattr(context, "pages", None)
    if existing_pages:
        page = existing_pages[0]
    else:
        page = await context.new_page()

    current_ip: Optional[str] = None
    try:
        current_ip = await get_ip_from_page(page)
        if current_ip:
            print(f"\U0001f310 [网络整合] 当前 CloakBrowser 代理出口 IP: {current_ip}")
        else:
            print("\u26a0\ufe0f [网络警告] 出口 IP 获取失败（返回空值）")
    except Exception:
        print("\u26a0\ufe0f [网络警告] 出口 IP 检测超时")

    # Warmup probe: the free-tier binary crashes most often right after
    # launch.  A death inside the warmup window is a launch failure — clean
    # up the half-open session and surface SessionLostError to the guard.
    try:
        await warmup_cloak_session(page)
    except BaseException:
        try:
            await context.close()
        except Exception:  # noqa: BLE001 - cleanup is best-effort
            pass
        raise

    return context, page, current_ip


class CloakBrowserProvider:
    """CloakBrowser backend: patched Chromium with a persistent per-account profile."""

    backend = BrowserBackend.CLOAK

    def preflight(self) -> BackendReport:
        """Read-only runtime check; never installs or upgrades anything."""
        from gpt_automation.utils.dependency_check import (
            check_runtime_dependencies,
        )

        problems = check_runtime_dependencies()
        if problems:
            return BackendReport(
                backend=self.backend,
                availability=BackendAvailability.MISSING_BINARY,
                detail="; ".join(problems),
            )
        try:
            _load_cloakbrowser_api()
            ensure_cloakbrowser_latest(update_wrapper=False)
            prepare_cloakbrowser_binary(allow_update=False)
        except Exception as exc:  # noqa: BLE001 - report, never raise
            return BackendReport(
                backend=self.backend,
                availability=BackendAvailability.MISSING_BINARY,
                detail=str(exc),
            )
        return BackendReport(
            backend=self.backend,
            availability=BackendAvailability.AVAILABLE,
            detail="cloakbrowser ready",
        )

    async def launch(self, request: BrowserLaunchRequest) -> AcquiredBrowser:
        """Launch one persistent-profile CloakBrowser session."""
        context, page, current_ip = await create_isolated_cloak_context(
            request.profile_id,
            proxy=request.proxy,
        )
        session = BrowserSession(
            backend=self.backend,
            context=context,
            page=page,
            private_mode=False,
            persistent_auth_state=True,
        )
        attach_session_loss_monitor(session)
        return AcquiredBrowser(session=session, current_ip=current_ip)
