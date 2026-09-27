"""
共享工具函数 — 所有阶段共用的浏览器交互工具 + 邮箱验证码处理.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, List, Optional, Tuple

import config
from gpt_automation.utils.beep import play_captcha_beep
from gpt_automation.utils.exceptions import (
    FATAL_EXCEPTIONS,
    SAFE_EXCEPTIONS,
    ElementNotFoundError,
    NeedsOperatorException,
    QuitBatchException,
    SkipEmailException,
)
from gpt_automation.verification.unified import UnifiedCodeReader
from gpt_automation.phases.email_verification import (
    EmailCodeError,
    fill_code_with_verification,
    is_six_digit_code,
    read_six_digit_code,
)
from gpt_automation.workflow.interaction_policy import (
    FailureContext,
    InteractionDecision,
    get_interaction_policy,
)
from gpt_automation.phases.human_input import human_type_and_verify
from gpt_automation.phases.probe import probe_visible

if TYPE_CHECKING:
    from playwright.async_api import Locator

logger = logging.getLogger(__name__)


def is_windows() -> bool:
    """True when running on a Windows host."""
    return platform.system().lower().startswith("win")


async def human_pause(message: str, prompt: str = "") -> str:
    """Pause for manual operator action (replaces silent input() where possible).

    Args:
        message: Description shown to the operator.
        prompt: Custom input prompt. Defaults to standard Enter prompt.

    Returns:
        The user's input string (stripped, uppercased). Empty string on EOF/interrupt.
    """
    logger.info("手动步骤：%s", message)
    print(f"\n[手动] {message}")
    play_captcha_beep()
    decision = await get_interaction_policy().decide(
        FailureContext(
            page=None,
            email="",
            step="manual_pause",
            exception=RuntimeError(message),
            description=message,
            verification_available=True,
        )
    )
    if decision is InteractionDecision.DEFER_ACCOUNT:
        raise NeedsOperatorException(message)
    if decision is InteractionDecision.SKIP_ACCOUNT:
        raise SkipEmailException(message)
    if decision is InteractionDecision.ABORT_BATCH:
        raise QuitBatchException(message)
    if decision is InteractionDecision.RETRY_AUTO:
        return "R"
    return ""


@dataclass(frozen=True)
class ScreenshotOutcome:
    """Best-effort screenshot result: real path when captured, else status."""

    path: Optional[str] = None
    status: str = "missing"  # captured | browser_not_started | page_closed | capture_timeout | write_failed


async def safe_screenshot(
    page,
    path: str,
    timeout: float = 8.0,
) -> ScreenshotOutcome:
    """Best-effort screenshot — never raises and never masks a business error.

    返回 ``ScreenshotOutcome``：仅当文件真实存在且非空时为 ``captured``；
    页面为 None 时为 ``browser_not_started``；页面已关闭时为 ``page_closed``；
    超过 *timeout* 为 ``capture_timeout``（批次必须继续推进）；其余为
    ``write_failed``。截图属于辅助证据采集，失败绝不允许改变业务决策，
    只有确认落盘后才打印保存成功。
    """
    if page is None:
        return ScreenshotOutcome(status="browser_not_started")
    try:
        await asyncio.wait_for(
            page.screenshot(path=path, full_page=True),
            timeout=timeout,
        )
    except FATAL_EXCEPTIONS:
        raise
    except asyncio.TimeoutError:
        logger.debug("screenshot timed out after %.1fs for %s", timeout, path)
        return ScreenshotOutcome(status="capture_timeout")
    except Exception as exc:
        if "closed" in type(exc).__name__.lower() or "closed" in str(exc).lower():
            logger.debug("screenshot skipped, page closed for %s", path)
            return ScreenshotOutcome(status="page_closed")
        logger.debug("screenshot failed for %s: %s", path, type(exc).__name__)
        return ScreenshotOutcome(status="write_failed")
    try:
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            logger.info("Screenshot saved: %s", path)
            return ScreenshotOutcome(path=path, status="captured")
    except OSError:
        pass
    return ScreenshotOutcome(status="write_failed")


async def _first_visible(page, selectors: List[str], timeout_ms: int = 3000) -> "tuple[Locator, str]":
    """Return (locator, matched_selector) for the first visible match.

    Uses parallel polling: checks ALL selectors every ~150ms until one
    becomes visible or timeout expires. Much faster than checking them
    sequentially.

    Returns:
        (Locator, str): The visible element locator and the CSS selector
            that matched it (useful for debugging/logging).

    Raises:
        ElementNotFoundError: if no selector matches a visible element.
    """
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if await probe_visible(loc):
                    return loc, sel
            except SAFE_EXCEPTIONS:
                continue
        await asyncio.sleep(0.15)
    raise ElementNotFoundError(
        f"none of {len(selectors)} selectors matched a visible element "
        f"(timeout {timeout_ms}ms): {selectors[:3]}..."
    )


async def wait_until_gone(page, selectors: List[str], timeout_ms: int = 8000) -> bool:
    """Wait until NONE of the given selectors are visible.

    Returns True once all are gone, False on timeout.
    """
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        any_visible = False
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if await probe_visible(loc):
                    any_visible = True
                    break
            except SAFE_EXCEPTIONS:
                continue
        if not any_visible:
            return True
        await asyncio.sleep(0.2)
    return False


async def wait_until_appears(page, selectors: List[str], timeout_ms: int = 8000) -> bool:
    """Wait until AT LEAST ONE of the given selectors becomes visible.

    Returns True once an element appears, False on timeout.
    """
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if await probe_visible(loc):
                    return True
            except SAFE_EXCEPTIONS:
                continue
        await asyncio.sleep(0.2)
    return False


async def safe_click(page, selectors: List[str], timeout_ms: int = 3000) -> None:
    """Click the first visible matching element.

    Raises:
        ElementNotFoundError: if no matching element is visible or click fails.
    """
    loc, _ = await _first_visible(page, selectors, timeout_ms=timeout_ms)
    try:
        await loc.click()
    except SAFE_EXCEPTIONS as exc:
        raise ElementNotFoundError(
            f"click failed on '{selectors[0]}': {exc}"
        ) from exc


async def safe_fill(page, selectors: List[str], value: str, timeout_ms: int = 3000) -> None:
    """Fill the first visible matching input.

    Raises:
        ElementNotFoundError: if no matching element is visible or fill fails.
    """
    loc, _ = await _first_visible(page, selectors, timeout_ms=timeout_ms)
    try:
        await loc.fill(value)
    except SAFE_EXCEPTIONS as exc:
        raise ElementNotFoundError(
            f"fill failed on '{selectors[0]}': {exc}"
        ) from exc


def split_destructured(row: List[str]) -> Tuple[str, str, str, str, str, str, str, str, str, str, str, str]:
    """Split a 12-column row into named positional variables.

    Returns:
        (email, email_pwd, name, age, card_no, card_expiry, card_cvc,
         zip_code, pref, city, addr1, addr2)
    """
    return (row[0], row[1], row[2], row[3], row[4], row[5], row[6],
            row[7], row[8], row[9], row[10], row[11])


async def phase_handle_email_code(
    page,
    code_reader: UnifiedCodeReader,
    step: str,
    *,
    force_manual_code: bool = False,
) -> Optional[str]:
    """If a 6-digit input is visible, pull a code and fill it.

    Note: code_reader is now required (no Optional) — caller is responsible
    for ensuring a reader is available before invoking this helper.

    Args:
        page: Playwright Page object.
        code_reader: Email code reader (required, never None).
        step: Label for logging (e.g. "password", "mfa", "mfa_confirm").
        force_manual_code: If True, skip IMAP auto-read entirely;
            let the operator type the code directly in the browser
            and press Enter when done.
    """
    try:
        code_input = page.locator(config.SELECTORS["mfa_email_input"]).first
        if not await probe_visible(code_input):
            return None
    except SAFE_EXCEPTIONS:
        return None

    if force_manual_code:
        # Manual mode: operator types code directly in browser
        await human_pause(
            f"检测到需要邮箱验证码（{step}）。请在浏览器窗口中手动输入验证码，输入完成后按 Enter 继续。"
        )
        # Only click when the DOM actually holds a six-digit code.
        for _attempt in range(3):
            try:
                dom_value = (await code_input.input_value() or "").strip()
            except SAFE_EXCEPTIONS:
                dom_value = ""
            if is_six_digit_code(dom_value):
                await safe_click(
                    page,
                    [config.SELECTORS["mfa_email_verify_btn"],
                     "button:has-text('Verify')",
                     "button:has-text('\u78ba\u8a8d')"],
                    timeout_ms=3000,
                )
                logger.info("helpers/%s: manual code entry confirmed", step)
                return None
            await human_pause(
                f"输入框内容不是 6 位数字（当前 {len(dom_value)} 位），"
                "请修正后按 Enter 继续。"
            )
        return None

    try:
        code = await read_six_digit_code(
            lambda **kwargs: code_reader.get_verification_code(**kwargs),
            timeout=config.OUTLOOK_CONFIG["timeout"],
            poll_interval=config.OUTLOOK_CONFIG["poll_interval"],
        )
    except EmailCodeError:
        code = ""
        if config.IMAP_CONFIG.get("fallback_to_manual", True):
            await human_pause(f"请手动输入浏览器中显示的 {step} 验证码。")
            try:
                code = await asyncio.get_event_loop().run_in_executor(
                    None, input, f"请输入 {step} 验证码："
                )
                code = (code or "").strip()
            except (EOFError, KeyboardInterrupt):
                code = ""
    if not is_six_digit_code(code):
        return None
    try:
        await fill_code_with_verification(
            page,
            code_input,
            code,
            field_name="email_code",
        )
    except EmailCodeError:
        logger.warning("helpers/%s: email code fill failed, not submitting", step)
        return None
    await safe_click(
        page,
        [config.SELECTORS["mfa_email_verify_btn"],
         "button:has-text('Verify')",
         "button:has-text('\u78ba\u8a8d')"],
        timeout_ms=3000,
    )
    logger.info("helpers/%s: email code auto-filled", step)
    return code
