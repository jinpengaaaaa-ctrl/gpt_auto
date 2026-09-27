"""
Phase 2: Security — navigate to security settings, bind MFA via Authenticator app.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional, Sequence

import pyotp

import config
from gpt_automation.utils.csv_writer import evidence_path
from gpt_automation.utils.exceptions import SAFE_EXCEPTIONS
from gpt_automation.utils.timing import human_delay
from gpt_automation.phases.helpers import safe_click, safe_screenshot, wait_until_appears, wait_until_gone
from gpt_automation.phases.email_verification import (
    EmailCodeError,
    fill_code_with_verification,
    is_six_digit_code,
)
from gpt_automation.phases.human_input import (
    human_clear,
    human_type_and_verify,
)
from gpt_automation.utils.exceptions import ElementNotFoundError

logger = logging.getLogger(__name__)


class PasswordUiState(str, Enum):
    NOT_CONFIGURED = "NOT_CONFIGURED"
    SETUP_OPEN = "SETUP_OPEN"
    CONFIGURED = "CONFIGURED"
    UNKNOWN = "UNKNOWN"


class MfaUiState(str, Enum):
    OFF = "OFF"
    SETUP_OPEN = "SETUP_OPEN"
    ON = "ON"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class PasswordSetupResult:
    state: PasswordUiState
    credential_proven: bool
    reason: str = ""

    @property
    def verified(self) -> bool:
        return self.state is PasswordUiState.CONFIGURED and self.credential_proven


@dataclass(frozen=True)
class MfaSetupResult:
    secret: str
    verified: bool
    reason: str = ""


def classify_password_section_text(section_text: str) -> PasswordUiState:
    """Classify only the password row text, never unrelated page actions."""
    text = " ".join((section_text or "").split())
    if not text:
        return PasswordUiState.UNKNOWN
    if any(mask in text for mask in ("******", "＊＊＊＊＊＊")):
        return PasswordUiState.CONFIGURED
    if any(marker in text for marker in ("変更", "Change", "リセット", "Reset")):
        return PasswordUiState.CONFIGURED
    if any(marker in text for marker in ("追加する", "Add", "設定する", "Set")):
        return PasswordUiState.NOT_CONFIGURED
    return PasswordUiState.UNKNOWN


def classify_mfa_toggle_state(
    aria_checked: Optional[str],
    data_state: Optional[str] = None,
) -> MfaUiState:
    """Classify the switch only when the visible attributes agree.

    ``aria-checked`` and ``data-state`` must not contradict each other; a
    conflicting pair is an unknown variant, never a silent ON/OFF guess.
    """
    aria_bool = None
    if aria_checked in ("true", "false"):
        aria_bool = aria_checked == "true"
    data_bool = None
    if data_state in ("checked", "unchecked"):
        data_bool = data_state == "checked"
    if aria_bool is not None and data_bool is not None and aria_bool != data_bool:
        return MfaUiState.UNKNOWN
    if aria_bool is True or data_bool is True:
        return MfaUiState.ON
    if aria_bool is False or data_bool is False:
        return MfaUiState.OFF
    return MfaUiState.UNKNOWN


async def wait_for_password_row(
    page,
    timeout_ms: int = 30000,
) -> PasswordSetupResult:
    """Wait until the password row renders a classifiable action label.

    ``locator.is_visible(timeout=...)`` returns immediately because Playwright
    ignores that option, so this helper polls the row until its action text
    (``追加する`` / ``変更`` / ``******``) is readable.  A row that only shows
    the section title is still loading and is not treated as ready.
    """
    deadline = time.monotonic() + (timeout_ms / 1000.0)
    last_text = ""
    while True:
        try:
            row = page.locator("[data-testid='password-setting']").first
            if await row.count() > 0 and await row.is_visible():
                last_text = (await row.text_content() or "").strip()
                state = classify_password_section_text(last_text)
                if state is not PasswordUiState.UNKNOWN:
                    if not await row.is_enabled():
                        return PasswordSetupResult(
                            PasswordUiState.UNKNOWN,
                            False,
                            "PASSWORD_TRIGGER_NOT_ACTIONABLE",
                        )
                    return PasswordSetupResult(state, False, "")
        except SAFE_EXCEPTIONS:
            pass
        if time.monotonic() >= deadline:
            break
        await asyncio.sleep(0.5)
    logger.warning(
        "phase2: password row did not become classifiable (last text: '%s')",
        last_text[:50],
    )
    return PasswordSetupResult(PasswordUiState.UNKNOWN, False, "PASSWORD_ROW_TIMEOUT")


async def wait_for_mfa_toggle(
    page,
    timeout_ms: int = 30000,
) -> tuple[MfaUiState, str]:
    """Wait until the Authenticator switch exposes consistent state attributes.

    Returns ``(MfaUiState, reason)``; reason is empty when the switch is ready.
    A visible switch with conflicting attributes is reported immediately as
    ``MFA_TOGGLE_STATE_CONFLICT`` instead of being polled into a guess.
    """
    deadline = time.monotonic() + (timeout_ms / 1000.0)
    while True:
        try:
            toggle = page.locator("[data-testid='mfa-authenticator-toggle']").first
            if await toggle.count() > 0 and await toggle.is_visible():
                aria = await toggle.get_attribute("aria-checked")
                data_state = await toggle.get_attribute("data-state")
                state = classify_mfa_toggle_state(aria, data_state)
                if state is not MfaUiState.UNKNOWN:
                    if not await toggle.is_enabled():
                        return MfaUiState.UNKNOWN, "MFA_TOGGLE_NOT_ACTIONABLE"
                    return state, ""
                if aria in ("true", "false") and data_state in ("checked", "unchecked"):
                    return MfaUiState.UNKNOWN, "MFA_TOGGLE_STATE_CONFLICT"
                if aria is not None or data_state is not None:
                    return MfaUiState.UNKNOWN, "MFA_TOGGLE_UNKNOWN"
        except SAFE_EXCEPTIONS:
            pass
        if time.monotonic() >= deadline:
            break
        await asyncio.sleep(0.5)
    return MfaUiState.UNKNOWN, "MFA_TOGGLE_TIMEOUT"


def password_fields_match(values: Sequence[str], expected: str) -> bool:
    """Return true only for two non-empty fields matching the target exactly."""
    return bool(expected and len(values) == 2 and all(value == expected for value in values))


async def _visible_password_inputs(page):
    """Prefer the active dialog and never count hidden background inputs."""
    dialog_inputs = page.locator("[role='dialog'] input[type='password']:visible")
    if await dialog_inputs.count() > 0:
        return dialog_inputs
    return page.locator("input[type='password']:visible")


async def _password_submit_button(page):
    """Find a submit action inside the active password form only."""
    selectors = (
        "button:text-is('続行')",
        "button:has-text('続行')",
        "button:has-text('Continue')",
        "button:has-text('保存')",
        "button:has-text('Save')",
        "button[type='submit']",
    )
    roots = [page.locator("[role='dialog']").first, page.locator("form").last]
    for root in roots:
        try:
            if await root.count() == 0:
                continue
            for selector in selectors:
                button = root.locator(selector).first
                if await button.is_visible(timeout=1000):
                    return button, selector
        except SAFE_EXCEPTIONS:
            continue
    return None, ""


async def _email_reauth_visible(page) -> bool:
    """Distinguish email re-auth from the six-digit TOTP setup input."""
    url = page.url or ""
    if "auth.openai.com" in url or "email-verification" in url:
        return True
    for selector in (
        "text=メールアドレスを確認",
        "text=Email verification",
        "text=確認コード",
        "text=verification code",
    ):
        try:
            if await page.locator(selector).first.is_visible(timeout=500):
                return True
        except SAFE_EXCEPTIONS:
            continue
    return False


def mfa_completion_succeeded(
    *,
    otp_verified: bool,
    completion_ok: bool,
    clean_secret: str,
) -> bool:
    """Require page verification and a TOTP proof from this run."""
    return bool(otp_verified and completion_ok and clean_secret.strip())


def mfa_secret_proof_allowed(
    *,
    captured_secret: str,
    saved_secret: str,
    binding_previously_verified: bool,
) -> bool:
    """Allow a saved secret only when the same session already verified MFA.

    A credential file by itself is not proof that the current browser page has
    an active MFA binding.  A fresh secret extracted from the visible setup
    page remains sufficient for the current verification attempt.
    """
    if captured_secret.strip():
        return True
    return bool(binding_previously_verified and saved_secret.strip())


async def _phase2_open_security(page, name: str) -> None:
    """Navigate to Security & Login settings panel.

    New ChatGPT UI (2026-06) has no visible "設定" text button — the avatar
    opens a pricing modal instead.  The settings panel is only accessible via
    the URL hash ``#settings/Security``.

    Strategy (verified via UI probe):
      1. Navigate to chatgpt.com/ (establishes session, may show pricing modal).
      2. Close any pricing modal (Escape).
      3. Set ``window.location.hash = '#settings/Security'`` via JS — this
         reliably opens the security settings side panel without triggering
         the pricing modal again.
      4. Wait for the panel to render (detect "セキュリティとログイン" text).
    """
    # Step 1: Navigate to main page first (not directly to #settings,
    # because page.goto with hash + Escape clears the hash)
    await human_delay((0.5, 1.0))
    try:
        await page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=15000)
        await asyncio.sleep(2.0)
    except SAFE_EXCEPTIONS as exc:
        logger.warning("phase2: navigation to chatgpt.com failed: %s", exc)

    # Step 2: Close pricing modal if present
    if "#pricing" in (page.url or ""):
        logger.info("phase2: pricing modal detected, closing...")
        try:
            await page.keyboard.press("Escape")
            await asyncio.sleep(1.0)
        except SAFE_EXCEPTIONS:
            pass

    # Step 3: Open security settings via hash navigation
    logger.info("phase2: opening security settings via #settings/Security")
    await page.evaluate("window.location.hash = '#settings/Security'")
    await asyncio.sleep(2.0)

    # Step 4: Wait for security settings panel to appear
    try:
        panel = page.locator("text=セキュリティとログイン").first
        await panel.wait_for(state="visible", timeout=30_000)
        logger.info("phase2: navigated to Security & Login page")
    except SAFE_EXCEPTIONS:
        logger.warning("phase2: security settings panel may not have appeared")


async def _phase2_set_password(
    page,
    code_reader=None,
    chatgpt_password: Optional[str] = None,
    submit_callback: Optional[Callable[[], None]] = None,
) -> PasswordSetupResult:
    """Set the ChatGPT account password on the Security & Login page.

    Must be called BEFORE toggling MFA. Uses config.CHATGPT_PASSWORD
    (fixed ≥12 char password, overridable via env var).

    Flow (verified via debug_password.py 2026-06-30):
      1. Click [data-testid='password-setting'] button on Security page
      2. Page redirects to auth.openai.com/email-verification
      3. Use code_reader to get 6-digit email verification code
      4. Fill code → click 続行 → wait for redirect back to chatgpt.com
      5. Navigate back to #settings/Security if needed
      6. Password dialog 「パスワードを追加する」appears
      7. Fill both password fields (new + confirm) → click 続行

    Args:
        page: Playwright Page (already on #settings/Security).
        code_reader: Email code reader for verification codes.

    Returns:
        A typed result distinguishing a proven submission from an unknown page.
    """
    chatgpt_pwd = chatgpt_password or config.CHATGPT_PASSWORD
    if not chatgpt_pwd or len(chatgpt_pwd) < 12:
        logger.error("phase2: CHATGPT_PASSWORD must be ≥12 chars (got %d) — skipping",
                     len(chatgpt_pwd or ""))
        return PasswordSetupResult(PasswordUiState.UNKNOWN, False, "PASSWORD_INVALID")

    logger.info("phase2: attempting to set account password")

    # ── Pre-check with a real wait: the row may render after the panel title ──
    # A resumed workflow may already have set the password. Re-setting the same
    # password causes "パスワードは再利用できません".  The absence of an
    # Add label is not evidence of configuration because another UI variant or
    # an incomplete row can produce the same text.
    readiness = await wait_for_password_row(page, timeout_ms=30000)
    if readiness.reason:
        logger.warning("phase2: password row not ready: %s", readiness.reason)
        await safe_screenshot(page, evidence_path("unknown", "p2_pwd_row_not_ready"))
        return readiness
    if readiness.state is PasswordUiState.CONFIGURED:
        logger.info("phase2: password already set (shows ******), skipping")
        return PasswordSetupResult(
            PasswordUiState.CONFIGURED,
            False,
            "PASSWORD_ALREADY_CONFIGURED",
        )

    # ── Step 1: Click the password row trigger ──
    # The trigger may be retried only while no password submit has been
    # recorded; once PASSWORD_SUBMIT starts, replay is forbidden.
    change_clicked = False
    last_click_error = ""
    for attempt in range(1, 4):
        if attempt > 1:
            logger.info("phase2: password trigger retry %d (no submit recorded yet)", attempt)
            await _phase2_open_security(page, "")
            readiness = await wait_for_password_row(page, timeout_ms=20000)
            if readiness.reason:
                await safe_screenshot(page, evidence_path("unknown", "p2_pwd_row_not_ready"))
                return readiness
            if readiness.state is PasswordUiState.CONFIGURED:
                return PasswordSetupResult(
                    PasswordUiState.CONFIGURED,
                    False,
                    "PASSWORD_ALREADY_CONFIGURED",
                )
        try:
            pwd_btn = page.locator("[data-testid='password-setting']").first
            await pwd_btn.wait_for(state="visible", timeout=20000)
            if not await pwd_btn.is_enabled():
                return PasswordSetupResult(
                    PasswordUiState.UNKNOWN,
                    False,
                    "PASSWORD_TRIGGER_NOT_ACTIONABLE",
                )
            await pwd_btn.click(timeout=20000)
            change_clicked = True
            logger.info("phase2: password button clicked via [data-testid='password-setting']")
            break
        except SAFE_EXCEPTIONS as exc:
            last_click_error = str(exc)
            logger.warning(
                "phase2: password trigger click attempt %d failed: %s",
                attempt,
                exc,
            )

    if not change_clicked:
        logger.warning("phase2: password row trigger not found after retries")
        await safe_screenshot(page, evidence_path("unknown", "p2_no_pwd_button"))
        reason = (
            "PASSWORD_TRIGGER_NOT_ACTIONABLE"
            if "actionability" in last_click_error
            else "PASSWORD_BUTTON_NOT_FOUND"
        )
        return PasswordSetupResult(PasswordUiState.UNKNOWN, False, reason)

    # ── Step 2: Detect email verification redirect OR direct password dialog ──
    # Clicking the password button triggers email verification → redirects to
    # auth.openai.com/email-verification → after verify → redirects back.
    await asyncio.sleep(4.0)
    current_url = page.url or ""
    logger.info("phase2: password action completed; detecting next screen")

    is_email_verify = ("auth.openai.com" in current_url or "email-verification" in current_url)
    pwd_dialog_visible = False

    # Quick check: did the password dialog appear directly (no email verify)?
    try:
        pwd_input = (await _visible_password_inputs(page)).first
        if await pwd_input.is_visible(timeout=2000):
            pwd_dialog_visible = True
            logger.info("phase2: password dialog appeared directly (no email verify)")
    except SAFE_EXCEPTIONS:
        pass

    # ── Step 2a: Handle email verification ──
    if is_email_verify and not pwd_dialog_visible:
        logger.info("phase2: email verification required (redirected to auth.openai.com)")

        if code_reader is None:
            logger.error("phase2: email verification required but no code_reader provided")
            await safe_screenshot(page, evidence_path("unknown", "p2_no_code_reader"))
            return PasswordSetupResult(PasswordUiState.UNKNOWN, False, "EMAIL_VERIFY_FAILED")

        # Wait for email delivery (typically arrives in 3-5s)
        await asyncio.sleep(5.0)

        # Poll for verification code input
        verify_input_visible = False
        for poll_attempt in range(10):  # up to 30s
            try:
                verify_input = page.locator("input[maxLength='6']").first
                if await verify_input.is_visible(timeout=1000):
                    verify_input_visible = True
                    break
            except SAFE_EXCEPTIONS:
                pass
            await asyncio.sleep(3.0)

        if not verify_input_visible:
            logger.warning("phase2: email verification input not found")
            await safe_screenshot(page, evidence_path("unknown", "p2_no_verify_input"))
            return PasswordSetupResult(
                PasswordUiState.UNKNOWN,
                False,
                "EMAIL_VERIFY_INPUT_NOT_FOUND",
            )

        # Read verification code from email
        email_verified = False
        for verify_attempt in range(2):
            try:
                code = await code_reader.get_verification_code(timeout=120, poll_interval=5)
            except SAFE_EXCEPTIONS as exc:
                logger.warning("phase2: code reader error (attempt %d): %s", verify_attempt + 1, exc)
                code = None

            if code is not None and is_six_digit_code(str(code)):
                try:
                    verify_input = page.locator("input[maxLength='6']").first
                    if await verify_input.is_visible(timeout=5000):
                        await fill_code_with_verification(
                            page,
                            verify_input,
                            str(code),
                            field_name="verify_code",
                        )
                        await human_delay((0.5, 1.0))

                        # Click continue
                        await safe_click(page, [
                            "button:text-is('続行')", "button:has-text('続行')",
                            "button:has-text('Continue')", "button[type='submit']",
                        ], timeout_ms=5000)
                        logger.info("phase2: verification code submitted (attempt %d)", verify_attempt + 1)

                        # Wait for page to process after code submission
                        await asyncio.sleep(5.0)
                        logger.info("phase2: verification submitted; detecting next screen")

                        # Check if password dialog appeared directly on auth.openai.com
                        direct_pwd = False
                        try:
                            pwd_check = page.locator("input[type='password']")
                            if await pwd_check.count() >= 1 and await pwd_check.first.is_visible(timeout=2000):
                                direct_pwd = True
                                logger.info("phase2: password dialog appeared directly on auth.openai.com")
                        except SAFE_EXCEPTIONS:
                            pass

                        if not direct_pwd:
                            # Navigate to chatgpt.com and re-open Security settings
                            # (matches debug_password.py flow that successfully showed dialog)
                            post_url = page.url or ""
                            if "chatgpt.com" not in post_url:
                                logger.info("phase2: navigating to chatgpt.com after email verify")
                                try:
                                    await page.goto("https://chatgpt.com/",
                                                    wait_until="domcontentloaded", timeout=15000)
                                    await asyncio.sleep(3.0)
                                except SAFE_EXCEPTIONS:
                                    pass

                            # Re-open security settings (dialog appears automatically)
                            try:
                                await page.evaluate(
                                    "window.location.hash = '#settings/Security'")
                                await asyncio.sleep(3.0)
                            except SAFE_EXCEPTIONS:
                                pass

                            logger.info("phase2: security navigation completed")

                        email_verified = True
                        logger.info("phase2: email verification completed")
                        break
                except SAFE_EXCEPTIONS as exc:
                    logger.warning("phase2: fill/submit failed (attempt %d): %s",
                                   verify_attempt + 1, exc)
            else:
                logger.warning(
                    "phase2: invalid verification code (attempt %d)",
                    verify_attempt + 1,
                )

            if verify_attempt < 1:
                await asyncio.sleep(5.0)

        if not email_verified:
            logger.error("phase2: email verification failed after 2 attempts")
            await safe_screenshot(page, evidence_path("unknown", "p2_email_verify_fail"))
            return PasswordSetupResult(
                PasswordUiState.UNKNOWN,
                False,
                "EMAIL_VERIFY_FAILED",
            )

    # ── Step 3: Wait for password dialog to appear ──
    # After email verify + redirect, the password dialog should appear on
    # the Security page.  Poll for password input fields.
    pwd_dialog_ready = False
    for poll in range(10):  # up to 30s
        try:
            pwd_inputs = await _visible_password_inputs(page)
            count = await pwd_inputs.count()
            if count == 2:
                visible = await pwd_inputs.first.is_visible(timeout=1000)
                if visible:
                    pwd_dialog_ready = True
                    logger.info("phase2: password dialog detected (poll %d, %d inputs)", poll, count)
                    break
        except SAFE_EXCEPTIONS:
            pass
        await asyncio.sleep(3.0)

    if not pwd_dialog_ready:
        logger.warning("phase2: password dialog did not appear after email verification")
        await safe_screenshot(page, evidence_path("unknown", "p2_no_pwd_dialog"))
        return PasswordSetupResult(PasswordUiState.UNKNOWN, False, "PASSWORD_DIALOG_NOT_FOUND")

    # ── Step 4: 逐键输入两个密码字段 ──
    # UI has 2 password fields:
    #   - 「新しいパスワード」 (new password)
    #   - 「新しいパスワードを再入力してください」 (confirm password)
    filled = False
    try:
        pwd_inputs = await _visible_password_inputs(page)
        count = await pwd_inputs.count()
        logger.info("phase2: found %d password input(s)", count)

        if count == 2:
            # 共享逐键输入：第一个字段输入并读回校验后，重新查询 locator
            # （React 可能在首次输入后重绘），再输入第二个字段。
            first_input = pwd_inputs.nth(0)
            await human_type_and_verify(
                page,
                first_input,
                chatgpt_pwd,
                field_name="new_password",
            )
            logger.info("phase2: new password typed")
            await human_delay((0.5, 1.0))
            pwd_inputs = await _visible_password_inputs(page)
            second_input = pwd_inputs.nth(1)
            await human_type_and_verify(
                page,
                second_input,
                chatgpt_pwd,
                field_name="confirm_password",
            )
            logger.info("phase2: confirm password typed")
            filled = True
    except SAFE_EXCEPTIONS as exc:
        logger.warning("phase2: password fill failed: %s", exc)

    if not filled:
        logger.warning("phase2: could not fill password fields")
        await safe_screenshot(page, evidence_path("unknown", "p2_pwd_fill_fail"))
        return PasswordSetupResult(PasswordUiState.UNKNOWN, False, "PASSWORD_FIELDS_NOT_FILLED")

    try:
        values = [
            await (await _visible_password_inputs(page)).nth(index).input_value()
            for index in range(2)
        ]
    except SAFE_EXCEPTIONS as exc:
        logger.warning("phase2: password field read-back failed: %s", exc)
        return PasswordSetupResult(PasswordUiState.UNKNOWN, False, "PASSWORD_FIELDS_UNREADABLE")
    if not password_fields_match(values, chatgpt_pwd):
        logger.warning("phase2: password field read-back mismatch; refusing submit")
        return PasswordSetupResult(PasswordUiState.UNKNOWN, False, "PASSWORD_FIELDS_MISMATCH")

    # ── Step 5: Submit ──
    await asyncio.sleep(0.5)
    submit_clicked = False
    btn, sel = await _password_submit_button(page)
    if btn is not None:
        try:
            if submit_callback is not None:
                submit_callback()
            await btn.click()
            submit_clicked = True
            logger.info("phase2: password form submitted via scoped '%s'", sel)
        except SAFE_EXCEPTIONS as exc:
            logger.warning("phase2: scoped password submit failed: %s", exc)

    if not submit_clicked:
        logger.warning("phase2: submit button not found")
        return PasswordSetupResult(PasswordUiState.UNKNOWN, False, "PASSWORD_SUBMIT_NOT_FOUND")

    await asyncio.sleep(3.0)

    # ── Step 6: Verify success ──
    # Check for validation errors (12文字以上, パスワードの確認が必要です, etc.)
    error_detected = False
    try:
        alerts = page.locator("[role='alert']")
        alert_count = await alerts.count()
        if alert_count > 0:
            for i in range(alert_count):
                text = (await alerts.nth(i).text_content() or "").strip()
                if text:
                    logger.warning("phase2: password validation error: '%s'", text)
                    error_detected = True
    except SAFE_EXCEPTIONS:
        pass

    for err_text in ["12 文字以上", "12文字以上", "12 characters", "パスワードの確認",
                     "passwords do not match", "パスワードが一致しません"]:
        try:
            if await page.locator(f"text={err_text}").first.is_visible(timeout=1000):
                logger.warning("phase2: password error detected: '%s'", err_text)
                error_detected = True
                break
        except SAFE_EXCEPTIONS:
            continue

    if error_detected:
        logger.warning("phase2: password setting failed — validation errors")
        await safe_screenshot(page, evidence_path("unknown", "p2_pwd_validation_error"))
        return PasswordSetupResult(PasswordUiState.UNKNOWN, False, "PASSWORD_VALIDATION_ERROR")

    # A dismissed dialog is not sufficient. Re-read the scoped password row
    # with a real wait; the server may take seconds to flip the row to ******.
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        try:
            pwd_section = page.locator("[data-testid='password-setting']").first
            if await pwd_section.count() > 0 and await pwd_section.is_visible():
                state = classify_password_section_text(
                    (await pwd_section.text_content() or "").strip()
                )
                if state is PasswordUiState.CONFIGURED:
                    logger.info("phase2: password postcondition confirmed")
                    return PasswordSetupResult(PasswordUiState.CONFIGURED, True)
        except SAFE_EXCEPTIONS as exc:
            logger.warning("phase2: password postcondition check failed: %s", exc)
        await asyncio.sleep(1.0)

    logger.warning("phase2: password submission outcome is unknown")
    return PasswordSetupResult(PasswordUiState.UNKNOWN, False, "PASSWORD_POSTCONDITION_UNKNOWN")


async def _read_mfa_toggle_state(page) -> tuple[MfaUiState, str]:
    """Wait for the Authenticator toggle and classify its visible state.

    Returns ``(state, reason)``.  Any unreadable page state is UNKNOWN, never
    a silent OFF/ON guess.  Playwright locator errors are not subclasses of
    the SAFE_EXCEPTIONS whitelist, so every observation failure is treated as
    UNKNOWN; the caller must still require an explicit ON observation.
    """
    try:
        mfa_toggle = page.locator("[data-testid='mfa-authenticator-toggle']").first
        await mfa_toggle.wait_for(state="visible", timeout=15_000)
        checked = await mfa_toggle.get_attribute("aria-checked")
        data_state = await mfa_toggle.get_attribute("data-state")
        return classify_mfa_toggle_state(checked, data_state), ""
    except Exception as exc:  # noqa: BLE001 - verifier returns UNKNOWN, never raises
        return MfaUiState.UNKNOWN, f"{type(exc).__name__}: {exc}"


async def _phase2_verify_completion(page) -> bool:
    """Verify Phase 2 completion on #settings/Security page.

    Phase 1 owns the new-account password creation.  Phase 2 therefore
    verifies only that the Authenticator switch is ON with consistent state
    attributes; the obsolete Security/Passkey row is not password evidence.

    The settings panel may already be closed when this verifier runs (Phase 2
    closes it after binding and the postcondition re-checks afterwards), so a
    hidden toggle triggers a read-only re-open of the Security panel before
    giving up.  True is returned only when the switch is explicitly ON, and
    the settings panel is always closed again before returning so the next
    workflow step starts from a clean main page (a leftover panel covers the
    offer entry and its human click fails the pointer-events check).

    Returns:
        True only when MFA is explicitly confirmed; False otherwise.
    """
    logger.info("phase2: verifying completion (MFA only)...")

    mfa_state, reason = await _read_mfa_toggle_state(page)
    if mfa_state is MfaUiState.ON:
        logger.info("phase2: verify — MFA toggle confirmed ON")
        await _phase2_close_settings(page)
        return True
    if mfa_state is not MfaUiState.UNKNOWN:
        logger.warning(
            "phase2: verify — MFA toggle state is %s (not ON)",
            mfa_state.value,
        )
        await safe_screenshot(page, evidence_path("unknown", "p2_verify_incomplete"))
        return False

    # Toggle unreadable: the settings panel may have been closed after
    # binding.  Re-open the Security panel and re-check (read-only; success
    # still requires an explicit ON observation).
    logger.info(
        "phase2: verify — toggle not visible (%s); re-opening Security panel",
        reason,
    )
    try:
        await _phase2_open_security(page, "")
    except Exception as exc:  # noqa: BLE001 - re-open failure stays UNKNOWN
        logger.warning("phase2: verify — Security re-open failed: %s", exc)

    mfa_state, reason = await _read_mfa_toggle_state(page)
    if mfa_state is MfaUiState.ON:
        logger.info("phase2: verify — MFA toggle confirmed ON after re-open")
        await _phase2_close_settings(page)
        return True
    logger.warning(
        "phase2: verify — MFA toggle still not ON after re-open (%s / %s)",
        mfa_state.value,
        reason,
    )
    await safe_screenshot(page, evidence_path("unknown", "p2_verify_incomplete"))
    return False


async def _phase2_verify_password_configured(page) -> bool:
    """Verify only the scoped password row, without making an MFA claim."""
    try:
        pwd_section = page.locator("[data-testid='password-setting']").first
        await pwd_section.wait_for(state="visible", timeout=8000)
        section_text = (await pwd_section.text_content() or "").strip()
        password_state = classify_password_section_text(section_text)
        if password_state is PasswordUiState.CONFIGURED:
            logger.info("phase2: password row confirmed configured")
            return True
        logger.warning(
            "phase2: password row state is %s (text: '%s')",
            password_state.value,
            section_text[:50],
        )
    except SAFE_EXCEPTIONS as exc:
        logger.warning("phase2: password check failed: %s", exc)
    return False


async def _phase2_close_settings(page) -> None:
    """Close settings panel and return to the main ChatGPT page.

    Uses JS click (page.evaluate) instead of Playwright's click() to avoid
    the actionability check that blocks 30+s when the settings panel is
    re-rendering after MFA binding.

    Tries in order: close/X button → ChatGPT logo → Escape key.
    """
    # Try closing via a close/cancel/X button (JS click — instant, no actionability wait)
    js_clicked = await page.evaluate("""
        () => {
            const selectors = [
                'button[aria-label="閉じる"]',
                'button[aria-label="Close"]',
                'button[aria-label="×"]',
            ];
            for (const sel of selectors) {
                const btn = document.querySelector(sel);
                if (btn && btn.offsetParent !== null) {
                    btn.click();
                    return true;
                }
            }
            // Fallback: text-based match for 閉じる / ×
            const btns = document.querySelectorAll('button');
            for (const btn of btns) {
                const t = btn.textContent.trim();
                if ((t === '閉じる' || t === '×') && btn.offsetParent !== null) {
                    btn.click();
                    return true;
                }
            }
            return false;
        }
    """)
    if js_clicked:
        await human_delay((0.5, 1.0))
        logger.info("phase2: settings closed via close button (JS click)")
        return

    # Fallback: click ChatGPT logo / home link (JS click)
    js_logo_clicked = await page.evaluate("""
        () => {
            const selectors = [
                'span.header-wordmark',
                'a[href="/"]',
                '[data-testid="accounts-profile-button"]',
            ];
            for (const sel of selectors) {
                const el = document.querySelector(sel);
                if (el && el.offsetParent !== null) {
                    el.click();
                    return true;
                }
            }
            return false;
        }
    """)
    if js_logo_clicked:
        await human_delay((1.0, 2.0))
        logger.info("phase2: navigated to main via logo click (JS click)")
        return

    # Last resort: Escape key
    try:
        await page.keyboard.press("Escape")
        await human_delay((0.5, 1.0))
        logger.info("phase2: settings closed via Escape")
    except SAFE_EXCEPTIONS:
        logger.info("phase2: could not close settings, proceeding anyway")


async def _navigate_to_main(page) -> None:
    """Navigate to the main ChatGPT page from anywhere.

    Used after Phase 3 Stripe to return to the main page before Phase 4.
    """
    # Try ChatGPT logo / home link first (no full page reload)
    try:
        await safe_click(page, [
            "span.header-wordmark",
            "a[href='/']",
            "[data-testid='accounts-profile-button']",
            "text=ChatGPT",
            "svg[aria-label='ChatGPT']",
        ], timeout_ms=3000)
        await human_delay((1.0, 2.0))
        logger.info("navigate: back to main via logo click")
        return
    except ElementNotFoundError:
        logger.info("navigate: logo not found, using page.goto fallback")

    # Fallback: full navigation
    await page.goto("https://chatgpt.com", wait_until="domcontentloaded")
    await human_delay((2.0, 3.0))


async def _extract_totp_secret(page) -> Optional[str]:
    """Read and validate the current MFA setup secret without exposing it.

    The helper is shared by the automatic and manual-recovery paths.  A page
    can still be on the MFA setup panel after an operator resumes a paused
    run, so recovery must use the same extraction and validation rules as the
    normal flow instead of accepting a blank placeholder.
    """
    try:
        evidence = await page.evaluate("""
            () => {
                const roots = Array.from(document.querySelectorAll(
                    '[role="dialog"], [data-testid*="mfa"], [data-testid*="authenticator"]'
                )).filter((root) => {
                    const style = window.getComputedStyle(root);
                    return style.display !== 'none' && style.visibility !== 'hidden';
                });
                for (const root of roots) {
                    const link = root.querySelector('a[href^="otpauth://"]');
                    if (link) {
                        const url = new URL(link.href);
                        return {
                            secret: url.searchParams.get('secret') || null,
                            text: root.textContent || '',
                        };
                    }
                }
                const body = document.body?.innerText || '';
                if (/(authenticator|two-factor|2fa|totp|qr)/i.test(body)) {
                    return {secret: null, text: body};
                }
                return null;
            }
        """)
    except SAFE_EXCEPTIONS as exc:
        logger.warning("phase2: otpauth URL extraction failed: %s", exc)
        evidence = None

    secret = evidence.get("secret") if isinstance(evidence, dict) else None
    if secret:
        try:
            pyotp.TOTP(secret).now()
            logger.info("phase2: extracted TOTP secret")
            return secret
        except Exception as exc:
            logger.warning("phase2: otpauth secret invalid: %s", exc)

    try:
        setup_text = evidence.get("text", "") if isinstance(evidence, dict) else ""
        for candidate in re.findall(r"[A-Z2-7]{16,}", setup_text):
            try:
                pyotp.TOTP(candidate).now()
                logger.info("phase2: extracted TOTP secret from page text")
                return candidate
            except Exception:
                continue
    except SAFE_EXCEPTIONS as exc:
        logger.warning("phase2: body text scan failed: %s", exc)

    return None


async def phase2_mfa(
    page,
    name: str,
    code_reader=None,
    secret_callback: Optional[Callable[[str], None]] = None,
    mfa_pending_callback: Optional[Callable[[], None]] = None,
    mfa_binding_callback: Optional[Callable[[], None]] = None,
) -> MfaSetupResult:
    """Bind MFA via Authenticator app.

    Flow:
      1. Open Security & Login settings panel
      2. Toggle the Authenticator app switch
      3. Handle email verification (if triggered by toggle)
      4. Read and save the TOTP secret key from page
      5. Generate OTP with pyotp → enter code → click 検証
      6. Detect OTP errors (wrong code) → retry with fresh OTP
      7. Verify MFA success → close settings panel

    Args:
        page: Playwright Page object.
        name: Registered user's full name (from Phase 1).
        code_reader: Email code reader for verification codes (used by
                     MFA re-authentication when required by the toggle.
    Returns:
        A typed result; ``verified`` is true only after the final page check.
    """
    # Step 1: Navigate to Security & Login
    await _phase2_open_security(page, name)

    if mfa_pending_callback is not None:
        mfa_pending_callback()

    # Step 2: Toggle the exact Authenticator app switch.
    # The switch may render long after the panel title, so wait for its state
    # attributes instead of a single instant visibility check.
    current_state, toggle_reason = await wait_for_mfa_toggle(page, timeout_ms=30000)
    if toggle_reason:
        logger.warning("phase2: Authenticator switch not ready: %s", toggle_reason)
        return MfaSetupResult("", False, toggle_reason)
    if current_state is not MfaUiState.OFF:
        logger.warning(
            "phase2: Authenticator switch is %s; refusing to infer setup",
            current_state.value,
        )
        return MfaSetupResult("", False, f"MFA_SWITCH_{current_state.value}")
    if mfa_binding_callback is not None:
        mfa_binding_callback()
    try:
        mfa_toggle = page.locator("[data-testid='mfa-authenticator-toggle']").first
        await mfa_toggle.click(timeout=15000)
        logger.info("phase2: clicked exact Authenticator switch")
    except SAFE_EXCEPTIONS as exc:
        logger.warning("phase2: exact Authenticator switch click failed: %s", exc)
        return MfaSetupResult("", False, "MFA_TOGGLE_ACTION_FAILED")

    await human_delay((2.0, 3.0))

    # Step 2b: Handle email verification triggered by MFA toggle
    # OpenAI may require email re-verification when enabling security features.
    # Strengthened: longer wait, polling, retry, success verification.
    logger.info("phase2: checking for email verification after MFA toggle...")
    await asyncio.sleep(3.0)

    # Poll for 6-digit verification input (may take time for page to render)
    verify_input_visible = False
    if await _email_reauth_visible(page):
        for poll_attempt in range(10):  # up to 30s
            try:
                verify_input = page.locator("input[maxLength='6']").first
                if await verify_input.is_visible(timeout=1000):
                    verify_input_visible = True
                    logger.info("phase2: email verification input detected (poll %d)", poll_attempt)
                    break
            except SAFE_EXCEPTIONS:
                pass
            await asyncio.sleep(3.0)

    if verify_input_visible and code_reader is not None:
        logger.info("phase2: email verification required after MFA toggle")
        await asyncio.sleep(5.0)  # wait for email delivery

        email_verified = False
        for verify_attempt in range(2):  # up to 2 attempts
            try:
                code = await code_reader.get_verification_code(timeout=120, poll_interval=5)
            except SAFE_EXCEPTIONS as exc:
                logger.warning("phase2: code reader error (attempt %d): %s", verify_attempt + 1, exc)
                code = None

            if code is not None and is_six_digit_code(str(code)):
                # Re-check input is still visible
                try:
                    verify_input = page.locator("input[maxLength='6']").first
                    if await verify_input.is_visible(timeout=3000):
                        await fill_code_with_verification(
                            page,
                            verify_input,
                            str(code),
                            field_name="mfa_verify_code",
                        )
                        await human_delay((0.5, 1.0))
                        await safe_click(page, [
                            "button:text-is('続行')", "button:has-text('続行')",
                            "button:has-text('Continue')", "button[type='submit']",
                        ], timeout_ms=5000)
                        await human_delay((3.0, 5.0))
                        logger.info(
                            "phase2: email verification code submitted (attempt %d)",
                            verify_attempt + 1,
                        )

                        # Verify success: check if verification input disappeared
                        await asyncio.sleep(2.0)
                        try:
                            still_visible = await page.locator("input[maxLength='6']").first.is_visible(timeout=2000)
                            if not still_visible:
                                email_verified = True
                                logger.info("phase2: email verification succeeded")
                                break
                            else:
                                # Check for error message
                                error_visible = False
                                for err_sel in ["[role='alert']", "text=無効", "text=invalid",
                                                "text=エラー", "text=expired"]:
                                    try:
                                        if await page.locator(err_sel).first.is_visible(timeout=1000):
                                            error_visible = True
                                            break
                                    except SAFE_EXCEPTIONS:
                                        continue
                                if error_visible:
                                    logger.warning("phase2: email verification failed (attempt %d) — retrying",
                                                   verify_attempt + 1)
                                    continue
                                else:
                                    # An input that remains visible is ambiguous;
                                    # do not promote it to success merely because
                                    # no error text was found.
                                    logger.warning(
                                        "phase2: email verification input remains visible without a clear result"
                                    )
                                    continue
                        except SAFE_EXCEPTIONS:
                            logger.warning(
                                "phase2: email verification postcondition could not be read"
                            )
                            continue
                except SAFE_EXCEPTIONS as exc:
                    logger.warning("phase2: email verification postcondition failed: %s", exc)
                    continue
            else:
                logger.warning(
                    "phase2: failed to read verification code (attempt %d)",
                    verify_attempt + 1,
                )

            if verify_attempt < 1:
                logger.info("phase2: waiting 5s before retry...")
                await asyncio.sleep(5.0)

        if not email_verified:
            logger.error("phase2: email verification failed after 2 attempts")
            await safe_screenshot(page, evidence_path("unknown", "p2_email_verify_fail"))
            return MfaSetupResult("", False, "EMAIL_VERIFY_FAILED")
    elif verify_input_visible and code_reader is None:
        logger.error("phase2: email verification required but no code_reader provided")
        return MfaSetupResult("", False, "EMAIL_VERIFY_READER_MISSING")
    else:
        logger.info("phase2: no email verification page — proceeding normally")

    # Confirm the actual MFA setup screen appeared. The settings row text alone
    # is not enough because it also exists when the switch is still OFF.
    appeared = await wait_until_appears(page, [
        "svg[aria-label*='QR']",
        "a[href^='otpauth://']",
        "#totp_otp",
        "input[name='totp_otp']",
        "input[maxLength='6'][inputmode='numeric']",
    ], timeout_ms=30_000)
    if not appeared:
        logger.warning("phase2: auth setup screen did not appear")
        return MfaSetupResult("", False, "MFA_SETUP_NOT_OPEN")

    # Step 3: Extract and validate the TOTP secret.  The same helper is used
    # when an operator resumes the setup, so manual recovery cannot succeed
    # with a fabricated or missing secret.
    clean_secret = await _extract_totp_secret(page)

    if not clean_secret:
        logger.warning("phase2: could not extract TOTP secret from page")
        await safe_screenshot(page, evidence_path("unknown", "mfa_no_secret"))
        return MfaSetupResult("", False, "MFA_SECRET_NOT_FOUND")

    if secret_callback is not None:
        secret_callback(clean_secret)

    # Step 4: Simulate human interaction delay before generating OTP.
    # Real human flow: toggle MFA → QR code appears → take phone →
    # open authenticator app → scan QR → see code → type in browser.
    # This is NOT instant; 4-7 seconds is a realistic lower bound.
    await human_delay((4.0, 7.0))

    # Step 5: Find the OTP code input (multi-strategy selectors)
    totp = pyotp.TOTP(clean_secret)
    working_code_sel = None
    for code_sel in [
        "#totp_otp",
        "input[name='totp_otp']",
        "input[maxLength='6']",
        "input[maxlength='6']",
        "input[placeholder*='6桁']",
        "input[placeholder*='コード']",
        "input[placeholder='●●●●●●']",
        "input[placeholder='000000']",
        "input[inputmode='numeric'][maxlength='6']",
        ".p-Input-input--numeric[maxlength='6']",
        "input[type='text'][maxlength='6']",
        "input[inputmode='numeric']",
        "input[type='tel'][maxlength='6']",
    ]:
        try:
            code_input = page.locator(code_sel).first
            if await code_input.is_visible(timeout=3000):
                working_code_sel = code_sel
                logger.info("phase2: OTP input found via '%s'", code_sel)
                break
        except SAFE_EXCEPTIONS:
            continue

    if not working_code_sel:
        logger.error("phase2: no code input found — MFA cannot be confirmed")
        return MfaSetupResult("", False, "MFA_OTP_INPUT_NOT_FOUND")

    # Step 6: Fill OTP + click 検証 + error detection with retry
    # pyotp TOTP codes have a 30s window. If we fill near the boundary,
    # the server may reject with "invalid code". Detect the error and
    # retry with a fresh code in the next 30s window.
    otp_verified = False
    for otp_attempt in range(3):  # up to 3 OTP attempts
        if otp_attempt > 0:
            # Wait only until the next TOTP window, instead of a fixed 30s.
            remaining = totp.interval - (time.time() % totp.interval)
            wait_seconds = max(1.0, min(float(remaining) + 0.5, 30.0))
            logger.info(
                "phase2: waiting %.1fs for fresh OTP window (attempt %d)...",
                wait_seconds,
                otp_attempt + 1,
            )
            await asyncio.sleep(wait_seconds)

        # Generate fresh OTP code
        fresh_code = totp.now()
        logger.info("phase2: OTP attempt %d", otp_attempt + 1)

        # Clear existing input and fill fresh code using working selector
        try:
            code_input = page.locator(working_code_sel).first
            if await code_input.is_visible(timeout=3000):
                await human_clear(page, code_input)
                await human_type_and_verify(
                    page,
                    code_input,
                    fresh_code,
                    field_name="totp_code",
                )
                await human_delay((0.5, 1.0))
            else:
                logger.warning("phase2: OTP input disappeared on attempt %d", otp_attempt + 1)
                break
        except SAFE_EXCEPTIONS:
            logger.warning("phase2: OTP fill failed on attempt %d", otp_attempt + 1)
            break

        # Click 検証 button
        try:
            await safe_click(page, [
                "button:has-text('検証')",
                "button:has-text('\u691c\u8a3c')",
                "button:has-text('Verify')",
                "button:has-text('確認')",
                "button:has-text('続行')",
                "button:has-text('Next')",
            ], timeout_ms=5000)
        except ElementNotFoundError:
            logger.warning("phase2: verify button not found on attempt %d", otp_attempt + 1)
            break

        await human_delay((2.0, 3.0))

        # Check for error messages (wrong OTP)
        otp_error = False
        for err_sel in [
            "text=無効なコード",
            "text=Invalid code",
            "text=invalid",
            "text=無効",
            "text=エラー",
            "text=incorrect",
            "text=wrong",
            "text=期限切れ",
            "text=expired",
        ]:
            try:
                err_loc = page.locator(err_sel).first
                if await err_loc.is_visible(timeout=1500):
                    otp_error = True
                    logger.warning("phase2: OTP error detected (attempt %d) — '%s'",
                                   otp_attempt + 1, err_sel)
                    break
            except SAFE_EXCEPTIONS:
                continue

        if otp_error:
            if otp_attempt < 2:
                logger.info("phase2: will retry with fresh OTP in next window")
                continue
            else:
                logger.error("phase2: OTP failed 3 times — giving up")
                await safe_screenshot(page, evidence_path("unknown", "p2_otp_fail"))
                break

        # No error detected — check if auth screen dismissed (success signal)
        dismissed = await wait_until_gone(page, [
            "button:has-text('検証')",
            "input[maxLength='6']",
            "svg[aria-label*='QR']",
        ], timeout_ms=30_000)
        if dismissed:
            otp_verified = True
            logger.info("phase2: auth screen dismissed — MFA binding confirmed (attempt %d)",
                        otp_attempt + 1)
            break
        else:
            # The setup screen is still visible. Absence of an error is not a
            # success signal; keep the result unverified for operator handling.
            logger.warning(
                "phase2: OTP result is ambiguous because the setup screen remains visible"
            )
            if otp_attempt < 2:
                continue

    if otp_verified:
        logger.info("phase2: MFA binding confirmed")
    else:
        logger.warning("phase2: MFA binding could not be confirmed")
        return MfaSetupResult("", False, "MFA_OTP_UNCONFIRMED")

    # Step 6b: Verify Phase 2 completion (MFA toggle ON)
    completion_ok = await _phase2_verify_completion(page)
    if not mfa_completion_succeeded(
        otp_verified=otp_verified,
        completion_ok=completion_ok,
        clean_secret=clean_secret,
    ):
        logger.error("phase2: MFA binding verification failed")
        return MfaSetupResult("", False, "MFA_FINAL_PAGE_UNCONFIRMED")

    # Step 7: Close settings panel → return to main ChatGPT page
    await _phase2_close_settings(page)

    return MfaSetupResult(clean_secret, True)
