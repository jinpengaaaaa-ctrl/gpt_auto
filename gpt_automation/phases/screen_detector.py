"""
Screen state detector — identifies the current ChatGPT page by scanning selectors.

Used by the state-based router to decide which phase handler to invoke.
Detection is fast (≤3s) and non-destructive: it never clicks, fills, or navigates.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from enum import Enum
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from gpt_automation.utils.exceptions import SAFE_EXCEPTIONS

if TYPE_CHECKING:
    from playwright.async_api import Page

logger = logging.getLogger(__name__)


class ScreenState(str, Enum):
    """Distinct ChatGPT screens the automation can encounter."""

    LOGIN_EMAIL = "login_email"
    LOGIN_PASSWORD = "login_password"
    EMAIL_VERIFY_CODE = "email_verify"
    SIGNUP_PASSWORD = "signup_password"
    REGISTRATION_FORM = "reg_form"
    LOGIN_MODAL = "login_modal"
    WELCOME_MODAL = "welcome"
    SURVEY = "survey"
    MAIN_PAGE = "main_page"
    PRICING_PAGE = "pricing_page"
    SETTINGS = "settings"
    STRIPE_PAYMENT = "stripe_payment"
    STRIPE_BILLING = "stripe_billing"
    UNKNOWN = "unknown"


# ── Screen signatures: (state, selectors) ──
# Order matters: more specific states first, broader ones later.
# Each entry: (ScreenState, [selectors to probe])
# A screen matches when ANY of its selectors is visible.

_SCREEN_SIGNATURES: list[tuple[ScreenState, list[str]]] = [
    # Stripe iframes take priority — they overlay the main page
    (ScreenState.STRIPE_PAYMENT, [
        "div.__PrivateStripeElement iframe",
        "iframe[title*='セキュア']",
        "iframe[title*='secure']",
        "iframe[src*='stripe.com']",
    ]),

    # Pricing modal overlays the main page; it must win over MAIN_PAGE.
    (ScreenState.PRICING_PAGE, [
        "[data-testid='pricing-modal-plan-grid']",
        "[data-testid='plus-pricing-modal-column']",
        "[data-testid='select-plan-button-plus-upgrade']",
    ]),

    # Email verification code (auth flow)
    (ScreenState.EMAIL_VERIFY_CODE, [
        "input[maxLength='6']",
        "input[name='code']",
        "input[autocomplete='one-time-code']",
        "input[aria-label*='code' i]",
        "input[aria-label*='コード' i]",
    ]),

    # Registration form (name/age)
    (ScreenState.REGISTRATION_FORM, [
        "input[name='name']",
        "[role='spinbutton'][data-type='year']",
        "input[name='birthday']",
        "input[placeholder*='名前']",
        "input[placeholder*='Name' i]",
        "[role='heading']:has-text('Tell us about you')",
        "[role='heading']:has-text('アカウント情報')",
        "text=Tell us about you",
        "text=お名前と生年月日",
    ]),

    # Welcome modal
    (ScreenState.WELCOME_MODAL, [
        "text=準備が完了しました",
        "[role='dialog']:has-text('ようこそ')",
        "[role='dialog']:has-text('Welcome to ChatGPT')",
    ]),

    # Survey popup
    (ScreenState.SURVEY, [
        "text=アンケート",
        "text=Survey",
        "text=Questionnaire",
        "text=お客様アンケート",
    ]),

    # Login/registration modal overlay (session lost during Phase 2-4)
    # Appears as a modal dialog over the main page, distinct from full login page.
    # Also matches the standalone /auth/login page heading (h1) which Playwright's
    # text= selector misses on some ChatGPT UI versions.
    (ScreenState.LOGIN_MODAL, [
        "h1:has-text('ログインまたは新規登録')",
        "h1:has-text('Log in or sign up')",
        "h2:has-text('ログインまたは新規登録')",
        "[role='heading']:has-text('ログインまたは新規登録')",
        "text=ログインまたは新規登録",
        "text=Log in or sign up",
    ]),

    # Login page — password field (must come before email, since both may be on page)
    (ScreenState.LOGIN_PASSWORD, [
        "input[name='password']",
    ]),

    # Login page — email input. The [name='email'] input exists in DOM on /auth/login
    # (verified via check_env.py), but also add the placeholder-based selector as backup
    # for UI variants that use a different input attribute.
    (ScreenState.LOGIN_EMAIL, [
        "input[name='email']",
        "input[placeholder*='メール']",
        "input[placeholder*='email' i]",
        "button[data-testid='login-button']",
        "button[data-testid='signup-button']",
        "button:has-text('ログイン')",
        "button:has-text('無料でサインアップ')",
        "button:has-text('Log in')",
        "button:has-text('Sign up')",
    ]),

    # Settings panel (sidebar open)
    (ScreenState.SETTINGS, [
        "text=セキュリティとログイン",
        "text=Security & Login",
        "a:has-text('セキュリティ')",
    ]),

    # Main chat page — only a positive account/profile marker is accepted.
    # The URL fast-path above disambiguates the profile button from logged-out
    # pages that expose the same header element. Wordmarks and generic headings
    # are deliberately not enough to claim that a session is authenticated.
    (ScreenState.MAIN_PAGE, [
        "[data-testid='accounts-profile-button']",
    ]),
]


_LOGIN_BUTTON_SELECTORS: tuple[str, ...] = (
    "button[data-testid='login-button']",
    "button[data-testid='signup-button']",
    "button:has-text('ログイン')",
    "button:has-text('無料でサインアップ')",
    "button:has-text('Log in')",
    "button:has-text('Sign up')",
)

_PRICING_PAGE_SELECTORS: tuple[str, ...] = (
    "[data-testid='pricing-modal-plan-grid']",
    "[data-testid='plus-pricing-modal-column']",
    "[data-testid='select-plan-button-plus-upgrade']",
)


async def _pricing_modal_visible(page: "Page") -> bool:
    """True when the pricing modal (plan grid / target plan button) is visible."""
    for selector in _PRICING_PAGE_SELECTORS:
        try:
            if await _is_visible_now(page.locator(selector).first):
                return True
        except SAFE_EXCEPTIONS:
            continue
    return False


def _screen_poll_interval(timeout_ms: int) -> float:
    """Return a bounded polling interval for one detection budget.

    Short transitions need a responsive 250ms cadence.  Longer budgets use
    progressively less frequent probes so a page that never settles does not
    consume unnecessary browser/CPU work while still returning immediately on
    the first positive match.
    """
    if timeout_ms < 3000:
        return 0.25
    if timeout_ms <= 15000:
        return 0.5
    return 1.0


async def _is_visible_now(locator) -> bool:
    """Probe visibility without delegating the overall wait budget to Playwright.

    Delegates to the shared probe so a dead browser target raises
    :class:`SessionLostError` immediately instead of being retried until the
    probe budget expires.
    """
    from gpt_automation.phases.probe import probe_visible

    return await probe_visible(locator)


async def _detect_url_fast_path(page: "Page") -> ScreenState | None:
    """Return a URL-derived state, or ``None`` when selector probing is needed."""
    try:
        url = page.url or ""
        parsed = urlparse(url)
        if not url or url == "about:blank" or parsed.scheme == "about":
            logger.debug("detect_screen: blank or empty page -> UNKNOWN")
            return ScreenState.UNKNOWN
        if (
            parsed.hostname == "auth.openai.com"
            and parsed.path.rstrip("/") == "/create-account/password"
        ):
            logger.debug("detect_screen: matched new-account password route")
            return ScreenState.SIGNUP_PASSWORD
        if (
            parsed.hostname == "auth.openai.com"
            and "email-verification" in parsed.path
        ):
            logger.debug("detect_screen: matched email-verification route")
            return ScreenState.EMAIL_VERIFY_CODE
        if "billing.stripe.com" in url:
            return ScreenState.STRIPE_BILLING
        if "chatgpt.com" not in url:
            return None

        if "settings" in parsed.fragment:
            logger.debug("detect_screen: matched settings via URL")
            return ScreenState.SETTINGS
        if parsed.path not in ("", "/"):
            return None

        if await _pricing_modal_visible(page):
            logger.debug("detect_screen: matched pricing_page via modal overlay")
            return ScreenState.PRICING_PAGE

        avatar = page.locator("[data-testid='accounts-profile-button']").first
        if not await _is_visible_now(avatar):
            return None

        for selector in _LOGIN_BUTTON_SELECTORS:
            try:
                if await _is_visible_now(page.locator(selector).first):
                    logger.debug(
                        "detect_screen: URL root + avatar + login control -> LOGIN_EMAIL"
                    )
                    return ScreenState.LOGIN_EMAIL
            except SAFE_EXCEPTIONS:
                continue

        logger.debug("detect_screen: matched main_page via URL + avatar")
        return ScreenState.MAIN_PAGE
    except SAFE_EXCEPTIONS:
        logger.debug("detect_screen: URL fast-path probe failed", exc_info=True)
        return None


async def detect_screen(page: "Page", timeout_ms: int = 10000) -> ScreenState:
    """Detect the current screen state by probing selectors in priority order.

    Args:
        page: Playwright Page object.
        timeout_ms: Total time budget for all probes. Default 10s.

    Returns:
        The detected ScreenState, or UNKNOWN if nothing matches.
    """
    timeout_ms = max(int(timeout_ms), 0)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_ms / 1000.0
    poll_interval = _screen_poll_interval(timeout_ms)

    while True:
        url_state = await _detect_url_fast_path(page)
        if url_state is not None:
            return url_state

        for state, selectors in _SCREEN_SIGNATURES:
            for sel in selectors:
                try:
                    loc = page.locator(sel).first
                    if await _is_visible_now(loc):
                        logger.debug("detect_screen: matched %s via '%s'", state.value, sel)
                        return state
                except SAFE_EXCEPTIONS:
                    continue

        remaining = deadline - loop.time()
        if remaining <= 0:
            logger.debug("detect_screen: timeout after probing all states")
            return ScreenState.UNKNOWN

        await asyncio.sleep(min(poll_interval, remaining))


async def is_logged_in(page: "Page", timeout_ms: int = 5000) -> bool:
    """Quick check: is the user logged in on the ChatGPT main page?

    Returns True if the user avatar/name is visible in the sidebar.
    """
    state = await detect_screen(page, timeout_ms=timeout_ms)
    return state == ScreenState.MAIN_PAGE


async def is_not_logged_in(page: "Page", timeout_ms: int = 3000) -> bool:
    """Explicit check: is the user NOT logged in?

    Detects the login/signup buttons (ログイン / 無料でサインアップ) in the
    page header.  Returns True when the user is clearly logged out.

    Use this as a secondary confirmation after ``detect_screen`` returns
    ``MAIN_PAGE`` via the URL fast-path, to avoid false positives when the
    profile exists but the session has expired.
    """
    timeout_ms = max(int(timeout_ms), 0)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_ms / 1000.0
    poll_interval = _screen_poll_interval(timeout_ms)

    while True:
        # A stable root URL can be decided immediately.  If the page is still
        # transitioning, fall through to the bounded selector polling below.
        url_state = await _detect_url_fast_path(page)
        if url_state is ScreenState.LOGIN_EMAIL:
            return True
        if url_state is ScreenState.MAIN_PAGE:
            return False

        for selector in _LOGIN_BUTTON_SELECTORS:
            try:
                if await _is_visible_now(page.locator(selector).first):
                    logger.debug("is_not_logged_in: matched '%s'", selector)
                    return True
            except SAFE_EXCEPTIONS:
                continue

        remaining = deadline - loop.time()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(poll_interval, remaining))


async def _log_page_diagnostics(page: "Page") -> None:
    """Log non-sensitive diagnostics when screen detection fails."""
    try:
        host = urlparse(page.url or "").hostname or "(empty)"
        logger.warning("screen_detector DIAG: host=%s", host)
    except Exception as exc:  # noqa: BLE001
        logger.warning("screen_detector DIAG: could not read URL host: %s", exc)
        return

    try:
        tmp_dir = tempfile.gettempdir()
        screenshot_path = os.path.join(tmp_dir, "screen_detector_unknown.png")
        await page.screenshot(path=screenshot_path, full_page=True)
        logger.warning("screen_detector DIAG: screenshot saved to %s", screenshot_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("screen_detector DIAG: screenshot failed: %s", exc)


async def ensure_screen_detected(
    page: "Page",
    *,
    initial_timeout_ms: int = 15000,
    retry_timeout_ms: int = 30000,
    max_retries: int = 3,
) -> ScreenState:
    """Detect the current screen without changing the browser location.

    Unknown pages are business-relevant operator work. The detector therefore
    never navigates, reloads, or retries by changing the current page. Callers
    can offer the unchanged page to an operator and invoke this function again
    after the operator has completed a manual action.

    Args:
        page: Playwright Page object.
        initial_timeout_ms: Timeout for the first detection attempt (default 15s).
        retry_timeout_ms: Retained for API compatibility; it is not used.
        max_retries: Retained for API compatibility; it is not used.

    Returns:
        The detected ScreenState (may still be UNKNOWN if all attempts fail).
    """
    del retry_timeout_ms, max_retries
    state = await detect_screen(page, timeout_ms=initial_timeout_ms)
    if state is ScreenState.UNKNOWN:
        logger.warning(
            "ensure_screen_detected: current page is UNKNOWN; preserving it for operator"
        )
        await _log_page_diagnostics(page)
    return state
