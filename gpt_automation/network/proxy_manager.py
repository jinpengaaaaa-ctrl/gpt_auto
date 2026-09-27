"""
Windows 系统代理管理器 — 通过注册表操作启用/禁用系统代理.

使用 winreg 直接修改 HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings
下的 ProxyEnable 和 ProxyServer 值，控制 Windows 系统级代理状态。
"""

from __future__ import annotations

import ctypes
import logging
import os
import winreg
from typing import Optional

import config

logger = logging.getLogger(__name__)


class ProxyManager:
    """通过 Windows 注册表管理系统代理的启用/禁用.

    Attributes:
        host: 代理服务器地址（默认 127.0.0.1）。
        port: 代理服务器端口（默认 10808）。
    """

    _REG_PATH: str = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"

    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
    ) -> None:
        self.host: str = host or config.PROXY_CONFIG.get("host", "127.0.0.1")
        self.port: int = port or config.PROXY_CONFIG.get("port", 10808)

    # ------------------------------------------------------------------
    # Registry helpers
    # ------------------------------------------------------------------

    def _set_dword(self, name: str, value: int) -> bool:
        """Set a REG_DWORD value in the Internet Settings key."""
        try:
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, self._REG_PATH,
                0, winreg.KEY_SET_VALUE,
            )
        except OSError as exc:
            logger.warning("proxy: OpenKey(KEY_SET_VALUE) failed: %s", exc)
            return False
        try:
            winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, value)
            return True
        except OSError as exc:
            logger.warning("proxy: SetValueEx(%s) failed: %s", name, exc)
            return False
        finally:
            winreg.CloseKey(key)

    def _set_string(self, name: str, value: str) -> bool:
        """Set a REG_SZ value in the Internet Settings key."""
        try:
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, self._REG_PATH,
                0, winreg.KEY_SET_VALUE,
            )
        except OSError as exc:
            logger.warning("proxy: OpenKey(KEY_SET_VALUE) failed: %s", exc)
            return False
        try:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
            return True
        except OSError as exc:
            logger.warning("proxy: SetValueEx(%s) failed: %s", name, exc)
            return False
        finally:
            winreg.CloseKey(key)

    def _read_value(self, name: str) -> Optional[str]:
        """Read a value from the Internet Settings key. Returns None on failure."""
        try:
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, self._REG_PATH,
                0, winreg.KEY_READ,
            )
        except OSError as exc:
            logger.debug("proxy: OpenKey(KEY_READ) failed: %s", exc)
            return None
        try:
            value, _ = winreg.QueryValueEx(key, name)
            return str(value)
        except FileNotFoundError:
            return None
        except OSError as exc:
            logger.debug("proxy: QueryValueEx(%s) failed: %s", name, exc)
            return None
        finally:
            winreg.CloseKey(key)

    def _notify_change(self) -> None:
        """Broadcast WM_SETTINGCHANGE + refresh WinINet settings.

        After modifying the registry, Windows applications (browsers, etc.)
        need to be notified to re-read proxy settings. This calls:
        1. InternetSetOption(INTERNET_OPTION_SETTINGS_CHANGED) — tells WinINet
           to reload from registry.
        2. InternetSetOption(INTERNET_OPTION_REFRESH) — forces refresh.
        3. SendMessageTimeoutW(HWND_BROADCAST, WM_SETTINGCHANGE) — broadcasts
           to all top-level windows.

        This is equivalent to what v2rayN does when toggling system proxy.
        """
        try:
            INTERNET_OPTION_SETTINGS_CHANGED = 39
            INTERNET_OPTION_REFRESH = 37
            wininet = ctypes.windll.wininet
            wininet.InternetSetOptionW(0, INTERNET_OPTION_SETTINGS_CHANGED, None, 0)
            wininet.InternetSetOptionW(0, INTERNET_OPTION_REFRESH, None, 0)
        except Exception as exc:
            logger.debug("proxy: InternetSetOption failed: %s", exc)

        try:
            HWND_BROADCAST = 0xFFFF
            WM_SETTINGCHANGE = 0x001A
            SMTO_ABORTIFHUNG = 0x0002
            user32 = ctypes.windll.user32
            # Set argtypes for 64-bit safety (prevents pointer truncation)
            user32.SendMessageTimeoutW.argtypes = [
                ctypes.c_void_p,    # HWND
                ctypes.c_uint,      # UINT  (msg)
                ctypes.c_void_p,    # WPARAM
                ctypes.c_wchar_p,   # LPCWSTR  (lParam: "WinINet")
                ctypes.c_uint,      # UINT  (fuFlags)
                ctypes.c_uint,      # UINT  (uTimeout)
                ctypes.c_void_p,    # PDWORD_PTR  (lpdwResult, ignored)
            ]
            user32.SendMessageTimeoutW(
                HWND_BROADCAST, WM_SETTINGCHANGE, 0, "WinINet",
                SMTO_ABORTIFHUNG, 1000, None,
            )
        except Exception as exc:
            logger.debug("proxy: SendMessageTimeout failed: %s", exc)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def enable(self) -> bool:
        """启用 Windows 系统代理.

        设置 ProxyEnable=1, ProxyServer={host}:{port}，并广播设置变更通知。

        Returns:
            True 表示操作成功。
        """
        proxy_server = f"{self.host}:{self.port}"
        ok1 = self._set_dword("ProxyEnable", 1)
        ok2 = self._set_string("ProxyServer", proxy_server)
        self._notify_change()
        if ok1 and ok2:
            logger.info("proxy: enabled → %s", proxy_server)
            return True
        logger.warning("proxy: enable partially failed (enable=%s, server=%s)", ok1, ok2)
        return False

    def disable(self) -> bool:
        """禁用 Windows 系统代理（完整清除 = v2rayN「システムプロキシをクリア」同等）.

        1. 设置 ProxyEnable=0，清空 ProxyServer、ProxyOverride（注册表）
        2. 清除 os.environ 中的 http_proxy/https_proxy 等环境变量
           （v2rayN 开启代理时会设这些变量，仅清注册表不足以让已启动的进程恢复）
        3. 广播 WM_SETTINGCHANGE + InternetSetOption 通知所有应用刷新代理设置
        4. 写入后验证 ProxyEnable 以确保注册表更新已生效

        Returns:
            True 表示 ProxyEnable 已成功设为 0。
        """
        ok1 = self._set_dword("ProxyEnable", 0)
        ok2 = self._set_string("ProxyServer", "")
        ok3 = self._set_string("ProxyOverride", "")
        self._notify_change()

        # Clear process environment variables (v2rayN sets these)
        self._clear_env_proxy_vars()

        # Self-verify: confirm ProxyEnable actually reads 0
        actual = self._read_value("ProxyEnable")
        if actual is not None and actual != "0":
            logger.warning(
                "proxy: disable verified — ProxyEnable still %s (expected 0), "
                "registry write may have been silently rejected", actual,
            )

        if ok1 and ok2 and ok3:
            logger.info("proxy: fully disabled (registry + env + notification)")
            return True
        logger.warning("proxy: disable partially failed (enable=%s, server=%s, override=%s)", ok1, ok2, ok3)
        return ok1

    def _clear_env_proxy_vars(self) -> None:
        """Clear all proxy-related os.environ variables that v2rayN sets.

        Without this, child processes (Node.js, curl, etc.) inherit
        http_proxy=127.0.0.1:10808 even after the registry is cleared,
        causing ECONNREFUSED on 10808 when xray is stopped.
        """
        for key in (
            "http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
            "all_proxy", "ALL_PROXY", "NO_PROXY", "no_proxy",
        ):
            if key in os.environ:
                del os.environ[key]
                logger.debug("proxy: cleared env var %s", key)

    def is_enabled(self) -> bool:
        """读取 ProxyEnable 值，判断系统代理是否已启用.

        Returns:
            True 表示代理当前已启用。
        """
        val = self._read_value("ProxyEnable")
        if val is None:
            return False
        try:
            return int(val) != 0
        except (ValueError, TypeError):
            return False

    def get_current_proxy(self) -> str:
        """读取 ProxyServer 值，返回当前代理地址.

        Returns:
            代理地址字符串（如 "127.0.0.1:10808"），未设置时返回空字符串。
        """
        val = self._read_value("ProxyServer")
        return val or ""
