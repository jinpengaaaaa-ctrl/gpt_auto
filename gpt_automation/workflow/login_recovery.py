"""Checkout login-health recovery shared by the single RegistrationWorkflow.

The recovery keeps the browser on its current profile and only performs
idempotent form fills plus one TOTP submission.  Email verification codes are
only entered when a ``code_reader`` is provided; otherwise the operator keeps
the page.
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

from gpt_automation.utils.timing import human_delay

if TYPE_CHECKING:
    from playwright.async_api import Page

try:  # Playwright's TimeoutError is NOT a subclass of the builtin one
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError
except Exception:  # noqa: BLE001 — keep the module importable without Playwright
    PlaywrightTimeoutError = TimeoutError


class LoginRecoveryOutcome(str, Enum):
    AUTHENTICATED = "AUTHENTICATED"
    NEEDS_OPERATOR = "NEEDS_OPERATOR"


class LoginEntryUnavailable(RuntimeError):
    """Raised when the auth entry cannot be reached and the page state is unknown."""


def _auth_entry_states() -> frozenset[str]:
    """States accepted as a usable auth entry for account provision.

    ``login_modal`` is the canonical JP ChatGPT ``/auth/login`` render
    (「ログインまたは新規登録」) and MUST count as a ready entry; otherwise a
    freshly opened login page is misjudged as a failed navigation.
    """
    return frozenset(
        {
            "login_email",
            "login_password",
            "email_verify",
            "email_verify_code",
            "email_verification",
            "totp",
            "mfa",
            "login_totp",
            "authenticator",
            "signup_password",
            "reg_form",
            "login_modal",
        }
    )


def _login_recovery_states() -> frozenset[str]:
    """States the checkout bootstrap may recover with stored credentials.

    ``login_modal`` is kept conservative: its layout is not proven for
    credential fills, so recovery hands the browser to the operator instead of
    raising a hard entry failure.
    """
    return _auth_entry_states()


CodeReader = Callable[[], Awaitable[Optional[str]]]
DetectState = Callable[[], Awaitable[str]]
LoggedOutCheck = Callable[["Page"], Awaitable[bool]]
CodeSource = object


def _normalize_state(state: object) -> str:
    """Accept plain strings and str-Enum page states from the screen detector."""
    value = getattr(state, "value", state)
    return str(value).strip().lower()


def _as_code_reader(code_reader: Optional[object]) -> Optional[CodeReader]:
    """Accept both callables and ``UnifiedCodeReader``-style objects."""
    if code_reader is None:
        return None
    if callable(code_reader):
        return code_reader
    getter = getattr(code_reader, "get_verification_code", None)
    if callable(getter):
        return lambda: getter(timeout=30.0)
    return None


async def recover_login_health(
    page: "Page",
    *,
    email: str,
    chatgpt_password: str,
    totp_secret: str,
    detect_state: DetectState,
    is_logged_out: LoggedOutCheck,
    code_reader: Optional[CodeReader] = None,
) -> LoginRecoveryOutcome:
    """Return ``AUTHENTICATED`` when the profile is usable or can be recovered.

    The function treats only a clear ``main_page`` as authenticated.  On the
    login screens it fills the known fields; when a TOTP code is available it
    submits it once.  Anything requiring an email verification code or an
    unknown page returns ``NEEDS_OPERATOR`` so the operator keeps the browser.
    """
    state = _normalize_state(await detect_state())
    if state == "main_page" and not await is_logged_out(page):
        return LoginRecoveryOutcome.AUTHENTICATED
    submitted_totp = False

    if state == "login_email":
        selector = "input[name='email']"
        from gpt_automation.phases.human_input import (
            human_type_and_verify,
            normalize_email,
        )

        await human_type_and_verify(
            page,
            page.locator(selector).first,
            email,
            field_name="login_email",
            normalize=normalize_email,
        )
        await human_delay((0.3, 0.8))
        await page.locator(selector).first.press("Enter")
        state = _normalize_state(await detect_state())

    if state == "login_password":
        if not chatgpt_password:
            return LoginRecoveryOutcome.NEEDS_OPERATOR
        selector = "input[name='password']"
        from gpt_automation.phases.human_input import human_type_and_verify

        await human_type_and_verify(
            page,
            page.locator(selector).first,
            chatgpt_password,
            field_name="login_password",
        )
        await human_delay((0.3, 0.8))
        await page.locator(selector).first.press("Enter")
        state = _normalize_state(await detect_state())

    if state in {"email_verify", "email_verify_code", "email_verification"}:
        if code_reader is None:
            return LoginRecoveryOutcome.NEEDS_OPERATOR
        code = await code_reader()
        from gpt_automation.phases.email_verification import (
            fill_code_with_verification,
            is_six_digit_code,
        )

        if code is None or not is_six_digit_code(str(code)):
            return LoginRecoveryOutcome.NEEDS_OPERATOR
        selector = "input[inputmode='numeric'], input[name='code'], input[name='verification_code']"
        await fill_code_with_verification(
            page,
            page.locator(selector).first,
            str(code),
            field_name="login_email_code",
        )
        await page.locator(selector).first.press("Enter")
        state = _normalize_state(await detect_state())

    if state in {"totp", "mfa", "login_totp", "authenticator"}:
        if not totp_secret:
            return LoginRecoveryOutcome.NEEDS_OPERATOR
        try:
            import pyotp

            code = pyotp.TOTP(totp_secret).now()
        except Exception:
            return LoginRecoveryOutcome.NEEDS_OPERATOR
        selector = "#totp_otp, input[name='totp_otp'], input[maxLength='6'][inputmode='numeric']"
        from gpt_automation.phases.human_input import human_type_and_verify

        await human_type_and_verify(
            page,
            page.locator(selector).first,
            code,
            field_name="login_totp_code",
        )
        await human_delay((0.3, 0.8))
        await page.locator(selector).first.press("Enter")
        submitted_totp = True
        state = _normalize_state(await detect_state())

    if submitted_totp and state == "main_page":
        # A successful TOTP submission is itself proof of the login session;
        # the header-based logged-out probe is redundant after this point.
        return LoginRecoveryOutcome.AUTHENTICATED
    if state == "main_page" and not await is_logged_out(page):
        return LoginRecoveryOutcome.AUTHENTICATED
    return LoginRecoveryOutcome.NEEDS_OPERATOR


async def _navigate_to_auth_entry(
    page: "Page",
    auth_login_url: str = "https://chatgpt.com/auth/login",
) -> None:
    """Open the canonical auth entry; a logged-in profile redirects to main."""
    await page.goto(auth_login_url, wait_until="domcontentloaded")


async def ensure_auth_entry(
    page: "Page",
    *,
    detect_state: DetectState,
    is_logged_out: Optional[LoggedOutCheck] = None,
    navigate_fn: Optional[Callable[["Page"], Awaitable[None]]] = None,
) -> str:
    """Ensure the page sits on a recognizable auth-usable screen.

    This is the navigation-only entry used by the provision/full scope:
    it never fills credentials.  A live main page, an open login screen,
    or any known auth screen is preserved; an unknown location is
    navigated to the canonical auth entry and then re-detected.

    Returns the normalized page state (``main_page`` or one of the login
    states) when usable, otherwise raises :class:`LoginEntryUnavailable`.
    """
    from gpt_automation.phases.screen_detector import is_not_logged_in

    logged_out_check = is_logged_out or is_not_logged_in
    state = _normalize_state(await detect_state())
    if state == "main_page" and not await logged_out_check(page):
        return state
    if state in _auth_entry_states():
        return state

    navigate = navigate_fn or _navigate_to_auth_entry
    navigation_error: Optional[BaseException] = None
    try:
        await navigate(page)
    except (TimeoutError, PlaywrightTimeoutError):
        pass
    except Exception as exc:  # noqa: BLE001 — recoverable until the DOM verdict
        navigation_error = exc

    state = _normalize_state(await detect_state())
    if state == "main_page" and not await logged_out_check(page):
        return state
    if state in _auth_entry_states():
        return state
    raise LoginEntryUnavailable(
        "auth entry navigation failed and the page state is unknown"
    ) from navigation_error


async def ensure_login_or_auth_entry(
    page: "Page",
    *,
    email: str,
    chatgpt_password: str,
    totp_secret: str,
    detect_state: DetectState,
    is_logged_out: Optional[LoggedOutCheck] = None,
    code_reader: Optional[object] = None,
    navigate_fn: Optional[Callable[["Page"], Awaitable[None]]] = None,
) -> LoginRecoveryOutcome:
    """Recover the profile session without navigating when the page is usable.

    An authenticated main page or an already-open login screen is preserved
    as-is (no ``goto``).  Only an unknown location triggers the canonical
    auth-entry navigation, and a navigation timeout is tolerated when the
    page actually landed on a recognizable screen.
    """
    from gpt_automation.phases.screen_detector import is_not_logged_in

    logged_out_check = is_logged_out or is_not_logged_in
    reader = _as_code_reader(code_reader)
    state = _normalize_state(await detect_state())
    if state == "main_page" and not await logged_out_check(page):
        return LoginRecoveryOutcome.AUTHENTICATED
    if state in _login_recovery_states():
        return await recover_login_health(
            page,
            email=email,
            chatgpt_password=chatgpt_password,
            totp_secret=totp_secret,
            detect_state=detect_state,
            is_logged_out=logged_out_check,
            code_reader=reader,
        )

    navigate = navigate_fn or _navigate_to_auth_entry
    navigation_error: Optional[BaseException] = None
    try:
        await navigate(page)
    except (TimeoutError, PlaywrightTimeoutError):
        # A long navigation may still land on a usable page; judge by DOM.
        pass
    except Exception as exc:  # noqa: BLE001 — recoverable until the DOM verdict
        navigation_error = exc

    state = _normalize_state(await detect_state())
    if state == "main_page" and not await logged_out_check(page):
        return LoginRecoveryOutcome.AUTHENTICATED
    if state in _login_recovery_states():
        return await recover_login_health(
            page,
            email=email,
            chatgpt_password=chatgpt_password,
            totp_secret=totp_secret,
            detect_state=detect_state,
            is_logged_out=logged_out_check,
            code_reader=reader,
        )
    raise LoginEntryUnavailable(
        "auth entry navigation failed and the page state is unknown"
    ) from navigation_error
