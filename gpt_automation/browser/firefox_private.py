"""Firefox private-mode provider built on Playwright's patched Firefox.

Every account gets its own Firefox process and a brand-new non-persistent
browser context.  Permanent private browsing is enabled through the
``browser.privatebrowsing.autostart`` preference, so cookies, localStorage
and site data live only for the lifetime of the process and are discarded
when the session closes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Optional

import config
from playwright.async_api import async_playwright
from gpt_automation.browser.models import (
    AcquiredBrowser,
    BackendAvailability,
    BackendReport,
    BrowserBackend,
    BrowserLaunchRequest,
    BrowserSession,
)
from gpt_automation.browser.session_monitor import attach_session_loss_monitor
from gpt_automation.network.ip_detector import get_ip_from_page

logger = logging.getLogger(__name__)

# Permanent private browsing; matches the Firefox "Always use private browsing
# mode" setting.  Playwright's patched Firefox honors these preferences.
_PRIVATE_PREFS = {
    "browser.privatebrowsing.autostart": True,
}


def firefox_binary_path() -> Optional[str]:
    """Resolve the patched Firefox executable without starting the driver.

    Playwright's sync driver cannot be started from inside a running asyncio
    loop, so preflight must never rely on ``sync_playwright``.  This resolver
    reads the driver's own ``browsers.json`` and checks the local cache
    directly; it works both inside and outside an event loop.
    """
    try:
        import playwright
    except Exception:
        return None
    try:
        browsers_json = (
            Path(playwright.__file__).resolve().parent
            / "driver"
            / "package"
            / "browsers.json"
        )
        data = json.loads(browsers_json.read_text(encoding="utf-8"))
    except Exception:
        return None
    revision = None
    for entry in data.get("browsers", []):
        if entry.get("name") == "firefox":
            revision = str(entry.get("revision", ""))
            break
    if not revision:
        return None
    browsers_root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or ""
    if not browsers_root or browsers_root == "0":
        browsers_root = str(Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright")
    executable = (
        Path(browsers_root) / f"firefox-{revision}" / "firefox" / "firefox.exe"
    )
    if executable.is_file():
        return str(executable)
    # Non-Windows layout fallback.
    unix_executable = (
        Path(browsers_root) / f"firefox-{revision}" / "firefox" / "firefox"
    )
    if unix_executable.is_file():
        return str(unix_executable)
    return None


def firefox_binary_available() -> bool:
    """True when Playwright's patched Firefox binary is installed."""
    return firefox_binary_path() is not None


def check_firefox_installed() -> tuple[bool, str]:
    """Return (available, human-readable detail) without launching Firefox."""
    if firefox_binary_available():
        return True, "playwright firefox is installed"
    return (
        False,
        "playwright firefox binary is missing; "
        "run `python -m playwright install firefox`",
    )


async def verify_firefox_runtime() -> tuple[bool, str]:
    """Launch a short-lived headless browser to verify the runtime works.

    ``check_firefox_installed`` only checks the cached executable.  The batch
    preflight also needs to catch missing DLLs or a broken Playwright driver
    before allocating network or workflow resources, so perform a local
    ``about:blank`` launch without making any network requests.
    """
    available, detail = check_firefox_installed()
    if not available:
        return False, detail

    browser = None
    try:
        async with async_playwright() as playwright:
            browser = await playwright.firefox.launch(headless=True)
            page = await browser.new_page()
            await page.goto("about:blank")
        return True, "playwright firefox launch passed"
    except Exception as exc:  # noqa: BLE001 - converted into preflight detail
        return False, f"playwright firefox launch failed: {type(exc).__name__}: {exc}"
    finally:
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                pass


def _proxy_options_from_config() -> Optional[dict]:
    """Mirror the CloakBrowser proxy selection rules for Firefox."""
    if config.IP_ROTATION_CONFIG.get("rotation_mode") == "manual-hotspot":
        return None
    p_cfg = getattr(config, "PROXY_CONFIG", {})
    if not p_cfg.get("enabled", False):
        return None
    proxy_opts = {"server": p_cfg["server"]}
    if p_cfg.get("username"):
        proxy_opts["username"] = p_cfg["username"]
    if p_cfg.get("password"):
        proxy_opts["password"] = p_cfg["password"]
    return proxy_opts


class FirefoxPrivateProvider:
    """One fresh private Firefox session per account; never a shared profile."""

    backend = BrowserBackend.FIREFOX_PRIVATE

    def preflight(self) -> BackendReport:
        available, detail = check_firefox_installed()
        return BackendReport(
            backend=self.backend,
            availability=(
                BackendAvailability.AVAILABLE
                if available
                else BackendAvailability.MISSING_BINARY
            ),
            detail=detail,
        )

    async def launch(self, request: BrowserLaunchRequest) -> AcquiredBrowser:
        """Start a brand-new Firefox process in permanent private mode."""
        playwright = await async_playwright().start()
        browser = None
        context = None
        page = None
        try:
            proxy = request.proxy
            if proxy is None:
                proxy = _proxy_options_from_config()
            browser = await playwright.firefox.launch(
                headless=not request.headed,
                firefox_user_prefs=_PRIVATE_PREFS,
                proxy=proxy,
            )
            context = await browser.new_context(
                locale=request.locale,
                timezone_id=request.timezone_id,
            )
            page = await context.new_page()
            current_ip = await get_ip_from_page(page)
        except asyncio.CancelledError:
            await _release_partial(browser, playwright)
            raise
        except Exception:
            await _release_partial(browser, playwright)
            raise

        session = BrowserSession(
            backend=self.backend,
            context=context,
            page=page,
            private_mode=True,
            persistent_auth_state=False,
        )

        async def teardown() -> None:
            browser_ok = True
            if browser is not None:
                try:
                    await browser.close()
                except Exception:  # noqa: BLE001 - cleanup is best-effort
                    browser_ok = False
            try:
                await playwright.stop()
            except Exception:  # noqa: BLE001 - cleanup is best-effort
                browser_ok = False
            return browser_ok

        session.attach_close(teardown)
        attach_session_loss_monitor(session)
        return AcquiredBrowser(session=session, current_ip=current_ip)


async def _release_partial(browser, playwright) -> None:
    """Best-effort teardown when launch fails before the session exists."""
    if browser is not None:
        try:
            await browser.close()
        except Exception:  # noqa: BLE001 - cleanup is best-effort
            pass
    try:
        await playwright.stop()
    except Exception:  # noqa: BLE001 - cleanup is best-effort
        pass
