"""统一邮箱验证码处理 — 六码校验、清空重输、DOM 读回、提交与后置确认.

所有邮箱验证码路径（Phase 1 注册、Phase 2 密码/MFA 邮箱确认、登录恢复、
共享 helpers）都必须经由本模块完成「读取 → 六码校验 → 清空 → 逐键输入 →
DOM 读回 → 提交 → 后置确认」，禁止各调用方自行复制实现。

安全约定：
    * 验证码只允许通过本模块的 ``show_terminal`` 直接打印到终端；
      禁止写入 logger、错误报告、checkpoint、截图文件名或异常消息。
    * 输入框实际值不是严格六码数字时，绝不点击验证按钮。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable, Optional, Sequence

from gpt_automation.utils.exceptions import ElementNotFoundError
from gpt_automation.phases.human_input import human_clear, human_type_and_verify
from gpt_automation.utils.timing import human_delay

logger = logging.getLogger(__name__)


# 通道级失败：再送信无法解决 token / IMAP 问题，直接上报
NON_RESENDABLE_REASONS = frozenset(
    {
        "TOKEN_REFRESH_FAILED",
        "IMAP_AUTH_FAILED",
        "IMAP_RATE_LIMITED",
    }
)

# ChatGPT 验证码页的「メールを再送信する」入口
DEFAULT_RESEND_SELECTORS = (
    "a:has-text('メールを再送信する')",
    "button:has-text('メールを再送信する')",
    "text=メールを再送信する",
)

# ChatGPT 验证码页の「続行 / Continue」提交按钮（日文与英文；排除社交登录）
DEFAULT_SUBMIT_SELECTORS = (
    "button:text-is('\u7d9a\u884c')",
    "button:has-text('\u7d9a\u884c'):not(:has-text('Google')):not(:has-text('Apple')):not(:has-text('\u3067\u7d9a\u884c'))",
    "button:text-is('Continue')",
    "button:has-text('Continue'):not(:has-text('with Google')):not(:has-text('with Apple'))",
    "button[type='submit']",
)


class EmailCodeError(RuntimeError):
    """Structured email verification failure with a stable error code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def is_six_digit_code(value: str) -> bool:
    """Strictly match ``[0-9]{6}`` (no spaces, no dashes)."""
    return len(value) == 6 and value.isdigit()


async def read_six_digit_code(
    code_reader: Callable[..., Awaitable[Optional[str]]],
    *,
    timeout: float = 120.0,
    poll_interval: float = 5.0,
    retries: int = 3,
) -> str:
    """Read a verification code and require it to be exactly six digits.

    Raises:
        EmailCodeError: ``EMAIL_CODE_READER_FAILED`` when the reader itself
            raises, or ``EMAIL_CODE_NOT_SIX_DIGITS`` when every attempt
            returned a non-six-digit value.
    """
    last_error: Optional[BaseException] = None
    for attempt in range(1, retries + 1):
        try:
            code = await code_reader(timeout=timeout, poll_interval=poll_interval)
        except Exception as exc:  # noqa: BLE001 - reader failures are reported
            last_error = exc
            code = None
        if code is not None and is_six_digit_code(str(code).strip()):
            return str(code).strip()
        if attempt < retries:
            await asyncio.sleep(2.0)
    if last_error is not None:
        raise EmailCodeError(
            "EMAIL_CODE_READER_FAILED",
            "邮箱验证码读取失败，请检查邮箱取码通道后重试。",
        ) from last_error
    raise EmailCodeError(
        "EMAIL_CODE_NOT_SIX_DIGITS",
        "取得的邮箱验证码不是 6 位数字，已放弃自动输入。",
    )


async def ensure_input_empty(page, code_input) -> None:
    """Clear the code input and confirm it is actually empty."""
    await human_clear(page, code_input)
    try:
        current = await code_input.input_value()
    except Exception:  # noqa: BLE001 - treat an unreadable input as not empty
        current = "?"
    if current:
        raise EmailCodeError(
            "EMAIL_CODE_INPUT_MISMATCH",
            "邮箱验证码输入框无法清空，已保留现场。",
        )


async def fill_code_with_verification(
    page,
    code_input,
    code: str,
    *,
    field_name: str,
    delay_ms: Optional[float] = None,
    max_attempts: int = 2,
) -> None:
    """Clear, type and read back until the DOM value is the exact six-digit code.

    Keystroke timing is owned by ``timing.py``: when ``delay_ms`` is omitted
    the shared per-key human range is used, exactly like every other field.
    ``max_attempts`` defaults to 2 (first pass plus at most one re-type);
    this module must never stack a second fast-retry loop on top of the
    shared typing path.

    Raises:
        EmailCodeError: ``EMAIL_CODE_INPUT_MISMATCH`` when the input never
            matches after ``max_attempts``.  The submit button is never
            clicked by this function.
    """
    for attempt in range(1, max_attempts + 1):
        try:
            current = await code_input.input_value()
        except Exception:  # noqa: BLE001 - retry the fill on a transient read
            current = ""
        if current == code:
            return
        await ensure_input_empty(page, code_input)
        try:
            await human_type_and_verify(
                page,
                code_input,
                code,
                field_name=field_name,
                delay_ms=delay_ms,
            )
        except ElementNotFoundError:
            # Read-back mismatch: retry from a cleared input.
            pass
        try:
            if await code_input.input_value() == code:
                return
        except Exception:  # noqa: BLE001
            pass
    raise EmailCodeError(
        "EMAIL_CODE_INPUT_MISMATCH",
        "邮箱验证码输入框与实际取得的验证码不一致（重输后仍失败）。",
    )


async def submit_code(
    page,
    selectors: Sequence[str],
    *,
    timeout_ms: int = 5000,
    submit_callback: Optional[Callable[[], None]] = None,
) -> bool:
    """Click the verification submit button with a normal mouse click.

    When supplied, ``submit_callback`` records the submit intent immediately
    before the first real click.  A click exception after that marker is an
    unknown side effect, so this function must not try another selector.
    """
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            await locator.wait_for(state="visible", timeout=timeout_ms)
            await human_delay()
        except Exception:  # noqa: BLE001 - try the next selector
            continue
        if submit_callback is not None:
            submit_callback()
        # 普通点击可能因页面重绘/遮罩失败（actionability 检查未通过，点击未发生）：
        # 重新定位同一按钮后以普通方式再点一次；仍失败才按原有语义处理。
        last_click_error: Optional[BaseException] = None
        for _attempt in range(2):
            try:
                await locator.click(timeout=timeout_ms)
                return True
            except Exception as exc:  # noqa: BLE001 - retry once on redraw
                last_click_error = exc
                try:
                    locator = page.locator(selector).first
                    await locator.wait_for(state="visible", timeout=timeout_ms)
                    await human_delay()
                except Exception:  # noqa: BLE001 - button disappeared
                    break
        if submit_callback is not None:
            raise EmailCodeError(
                "EMAIL_CODE_SUBMIT_UNCONFIRMED",
                "邮箱验证码提交点击后结果无法确认。",
            ) from last_click_error
    return False


async def handle_email_code(
    page,
    code_reader: Callable[..., Awaitable[Optional[str]]],
    *,
    step: str,
    input_selector: str = "input[maxLength='6']",
    submit_selectors: Optional[Sequence[str]] = None,
    timeout: float = 120.0,
    poll_interval: float = 5.0,
    show_terminal: bool = True,
    submit_callback: Optional[Callable[[], None]] = None,
) -> str:
    """Orchestrate the complete email verification sequence.

    Returns the six-digit code on success.  When ``submit_selectors`` is
    provided the submit button is clicked and the code input must disappear
    afterwards; otherwise the caller owns the submit and postcondition.

    Raises:
        EmailCodeError: stable codes
            ``EMAIL_CODE_READER_FAILED`` / ``EMAIL_CODE_NOT_SIX_DIGITS`` /
            ``EMAIL_CODE_INPUT_MISMATCH`` / ``EMAIL_CODE_SUBMIT_UNCONFIRMED``.
    """
    try:
        code_input = page.locator(input_selector).first
        await code_input.wait_for(state="visible", timeout=30000)
    except Exception as exc:  # noqa: BLE001
        raise EmailCodeError(
            "EMAIL_CODE_INPUT_MISMATCH",
            "邮箱验证码输入框未出现，无法自动输入。",
        ) from exc

    code = await read_six_digit_code(
        code_reader,
        timeout=timeout,
        poll_interval=poll_interval,
    )
    if show_terminal:
        print(f"\n  [邮箱验证码/{step}] 本次取得的验证码：{code}")

    await ensure_input_empty(page, code_input)
    await fill_code_with_verification(
        page,
        code_input,
        code,
        field_name=f"email_code_{step}",
    )

    if submit_selectors:
        clicked = await submit_code(
            page,
            submit_selectors,
            submit_callback=submit_callback,
        )
        if not clicked:
            # 所有提交按钮选择器都未命中时，最后兜底：对验证码输入框按 Enter 提交（普通键盘提交）。
            try:
                await human_delay()
                await code_input.press("Enter")
            except Exception:  # noqa: BLE001 - Enter 失败后由下方轮询结果决定
                pass
        # Poll until the code input actually leaves the page; a fixed sleep
        # can misjudge a slow navigation as a submit failure.
        if not await _poll_input_gone(code_input, timeout_ms=8000):
            raise EmailCodeError(
                "EMAIL_CODE_SUBMIT_UNCONFIRMED",
                "提交后邮箱验证码输入框仍然可见，结果无法确认。",
            )
    return code


async def _poll_input_gone(code_input, *, timeout_ms: int) -> bool:
    """True when the code input disappears within the deadline."""
    deadline = time.monotonic() + (timeout_ms / 1000.0)
    while True:
        try:
            still_visible = await code_input.is_visible(timeout=500)
        except Exception:  # noqa: BLE001 - unreadable input is not "gone"
            still_visible = True
        if not still_visible:
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.5)


async def _click_resend(page, selectors: Sequence[str]) -> None:
    """Click the resend link; raises RESEND_BLOCKED when unavailable."""
    last_exc: Optional[BaseException] = None
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            await locator.wait_for(state="visible", timeout=5000)
            await human_delay()
            await locator.click(timeout=5000)
            return
        except Exception as exc:  # noqa: BLE001 - try the next selector
            last_exc = exc
            continue
    raise EmailCodeError(
        "EMAIL_CODE_RESEND_BLOCKED",
        "邮件再送信按钮不可见或点击失败，无法重新发送验证码。",
    ) from last_exc


async def handle_email_code_with_resend(
    page,
    code_reader,
    *,
    step: str,
    input_selector: str = "input[maxLength='6']",
    submit_selectors: Optional[Sequence[str]] = None,
    resend_selectors: Sequence[str] = DEFAULT_RESEND_SELECTORS,
    round_timeouts: Sequence[float] = (60.0, 45.0, 45.0),
    max_resend: int = 2,
    poll_interval: float = 3.0,
    show_terminal: bool = True,
    submit_callback: Optional[Callable[[], None]] = None,
) -> str:
    """Retry email-code reading in up to ``max_resend + 1`` rounds.

    Round 1 waits for the first mail; later rounds click the page's
    resend link before polling again.  Channel-level failures (token /
    IMAP) are raised immediately without resend.  After all rounds the
    caller receives ``EMAIL_CODE_GIVE_UP`` so the interaction layer can
    branch (auto skip vs operator menu).
    """
    try:
        code_input = page.locator(input_selector).first
        await code_input.wait_for(state="visible", timeout=30000)
    except Exception as exc:  # noqa: BLE001
        raise EmailCodeError(
            "EMAIL_CODE_INPUT_MISMATCH",
            "邮箱验证码输入框未出现，无法自动输入。",
        ) from exc

    round_count = min(len(round_timeouts), max_resend + 1)
    if round_count < 1:
        raise EmailCodeError(
            "EMAIL_CODE_GIVE_UP",
            "邮箱验证码取码轮次配置无效。",
        )

    for round_idx in range(round_count):
        resend_clicked_at: Optional[float] = None
        if round_idx > 0:
            try:
                await _click_resend(page, resend_selectors)
            except EmailCodeError:
                raise
            except Exception as exc:  # noqa: BLE001 - surface as blocked
                raise EmailCodeError(
                    "EMAIL_CODE_RESEND_BLOCKED",
                    "邮件再送信点击失败，无法重新发送验证码。",
                ) from exc
            resend_clicked_at = time.time()

        min_received_at = (
            resend_clicked_at - 60.0 if resend_clicked_at is not None else None
        )
        try:
            code = await code_reader.get_verification_code(
                timeout=round_timeouts[round_idx],
                poll_interval=poll_interval,
                min_received_at=min_received_at,
            )
        except Exception:  # noqa: BLE001 - reader failure reported below
            reason = getattr(code_reader, "last_failure_reason", None)
            logger.warning(
                "email_code/%s: round %d reader error (reason=%s)",
                step,
                round_idx + 1,
                reason,
            )
            code = None

        if code is not None and is_six_digit_code(str(code).strip()):
            code = str(code).strip()
            if show_terminal:
                print(f"\n  [邮箱验证码/{step}] 本次取得的验证码：{code}")
            await ensure_input_empty(page, code_input)
            await fill_code_with_verification(
                page,
                code_input,
                code,
                field_name=f"email_code_{step}",
            )
            if submit_selectors:
                clicked = await submit_code(
                    page,
                    submit_selectors,
                    submit_callback=submit_callback,
                )
                if not clicked:
                    # 所有提交按钮选择器都未命中时，最后兜底：对验证码输入框按 Enter 提交。
                    try:
                        await human_delay()
                        await code_input.press("Enter")
                    except Exception:  # noqa: BLE001 - Enter 失败后由下方轮询结果决定
                        pass
                if not await _poll_input_gone(code_input, timeout_ms=8000):
                    raise EmailCodeError(
                        "EMAIL_CODE_SUBMIT_UNCONFIRMED",
                        "提交后邮箱验证码输入框仍然可见，结果无法确认。",
                    )
            return code

        reason = getattr(code_reader, "last_failure_reason", None)
        if reason in NON_RESENDABLE_REASONS:
            raise EmailCodeError(
                "EMAIL_CODE_READER_FAILED",
                "邮箱验证码读取通道失败，重发邮件无法解决。",
            )
        if round_idx + 1 >= round_count:
            break

    raise EmailCodeError(
        "EMAIL_CODE_GIVE_UP",
        "邮箱验证码多次取码仍失败，请人工确认邮件后处理。",
    )
