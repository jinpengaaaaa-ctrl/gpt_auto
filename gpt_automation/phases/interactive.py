"""
Interactive failure handling for Phase 1-4 — guarded execution with beep + menu.

Provides:
    - ElementNotFoundError: raised by safe_click / safe_fill when element not found.
    - SkipEmailException:   raised when operator chooses to skip current email.
    - RestartBrowserException: raised when operator chooses to restart browser context.
    - guarded_execution():  wraps any phase step with auto-retry, beep alert,
                            screenshot, and interactive menu on failure.
    - circuit_breaker:      warns after 3 consecutive failures on same step.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Mapping, Optional

from gpt_automation.utils.beep import play_captcha_beep
from gpt_automation.utils.csv_writer import evidence_path
from gpt_automation.utils.exceptions import (
    FATAL_EXCEPTIONS,
    SkipEmailException, QuitBatchException, RestartBrowserException,
    NeedsOperatorException, OperatorDeferredException,
)
from gpt_automation.utils.playwright_errors import (
    SessionLostError,
    is_session_lost,
)
from gpt_automation.workflow.interaction_policy import (
    FailureContext,
    InteractionChoice,
    InteractionDecision,
    get_interaction_policy,
)
from gpt_automation.utils.redaction import mask_email

logger = logging.getLogger(__name__)


def _reraise_session_loss(exc: BaseException) -> None:
    """Re-raise *exc* as SessionLostError when the browser target is gone.

    A dead browser must never be folded into the interactive menu: there is
    no page left to screenshot, verify or retry.  The workflow's session-loss
    branch owns the disposition (restart vs UNKNOWN_SIDE_EFFECT).
    """
    if isinstance(exc, SessionLostError):
        raise exc
    if is_session_lost(exc):
        raise SessionLostError("target_closed") from exc

# ── Circuit breaker tracking ─────────────────────────────────
_CIRCUIT_STATE: dict[str, int] = {}
CIRCUIT_WARN_THRESHOLD: int = 3


def _circuit_count(step: str) -> int:
    count = _CIRCUIT_STATE.get(step, 0) + 1
    _CIRCUIT_STATE[step] = count
    if count >= CIRCUIT_WARN_THRESHOLD:
        logger.warning(
            "circuit-breaker: '%s' failed %d consecutive times — "
            "check page changes or selectors.", step, count,
        )
    return count


def _circuit_clear(step: str) -> None:
    if step in _CIRCUIT_STATE:
        del _CIRCUIT_STATE[step]


def _circuit_expose(step: str) -> None:
    if step not in _CIRCUIT_STATE:
        _CIRCUIT_STATE[step] = 0


def circuit_reset_all() -> None:
    """Clear all circuit breaker state. Call at the start of each new account."""
    _CIRCUIT_STATE.clear()


async def _interactive_menu(
    page,
    email: str,
    step: str,
    exc: BaseException,
    description: str,
    verification_available: bool = False,
    retry_allowed: bool | Callable[[], bool] = True,
    advance_available: bool = False,
    next_step_description: Optional[str] = None,
    target_options: tuple[tuple[str, str], ...] = (),
) -> InteractionChoice:
    """Display the interactive failure menu and handle the choice.

    Returns the selected interaction-decision value. Enter or M verifies manual
    work only when a verifier exists; otherwise it preserves the account.
    """
    # Screenshot
    try:
        shot_path = evidence_path(email, f"fail_{step}")
        if page is not None:
            from gpt_automation.phases.helpers import safe_screenshot
            await safe_screenshot(page, shot_path)
            logger.info("Screenshot saved: %s", shot_path)
    except FATAL_EXCEPTIONS:
        raise
    except Exception:
        # 截图是辅助证据采集，失败不得影响业务决策（页面可能已关闭）。
        pass

    # Beep
    play_captcha_beep()

    # Menu
    print(f"\n{'=' * 60}")
    print(f"  ⚠️  步骤失败: {description}")
    print(f"      邮箱: {mask_email(email)}")
    print(f"      错误: {type(exc).__name__}: {exc}")
    print()
    print("  [R]       重试当前步骤")
    print("  [Enter]   人工操作已完成，重新检测")
    print("  [C]       关闭并重开浏览器，继续当前邮箱")
    if step in (
        "stripe_card",
        "stripe_confirm",
        "stripe_payment",
        "stripe_manual_submit",
        "stripe_addr",
    ):
        print("  [1]       保留当前已有填写（不重填，点击支付）")
        print("  [2a]      更换信用卡（当前卡标记弃用/隔离 QUARANTINED，使用新卡）")
        print("  [2b]      更换信用卡（当前卡保留可用 RELEASED，使用新卡）")
        print("  [3]       更换账单地址（重新生成合规地址重填）")
    if advance_available:
        print("  [N]       人工已完成当前步骤，验证后进入下一步")
        print(f"            下一步：{next_step_description}")
    if target_options:
        print("  [T:<目标ID>] 仅在后置步骤验证通过后跳转：")
        for target_id, label in target_options:
            print(f"            {target_id} = {label}")
    print("  [S]       跳过当前账户")
    print("  [Q]       终止整个批次")
    if not verification_available:
        print("  [D]       保存现场并关闭浏览器")
    print(f"{'=' * 60}")

    decision = await get_interaction_policy().decide(
        FailureContext(
            page=page,
            email=email,
            step=step,
            exception=exc,
            description=description,
            verification_available=verification_available,
            retry_allowed=retry_allowed,
            advance_available=advance_available,
            next_step_description=next_step_description,
            target_options=target_options,
        )
    )
    if isinstance(decision, InteractionChoice):
        return decision
    if isinstance(decision, InteractionDecision):
        return InteractionChoice(decision)
    return InteractionChoice(InteractionDecision(str(decision)))


def _choice(value: InteractionChoice | InteractionDecision | str) -> InteractionChoice:
    if isinstance(value, InteractionChoice):
        return value
    if isinstance(value, InteractionDecision):
        return InteractionChoice(value)
    return InteractionChoice(InteractionDecision(value))


async def hold_for_operator(
    page,
    email: str,
    step: str,
    description: str,
    exception: BaseException,
) -> InteractionDecision:
    """Keep an assisted browser open until the operator explicitly releases it."""
    while True:
        choice = await _interactive_menu(
            page,
            email,
            step,
            exception,
            description,
            verification_available=False,
            retry_allowed=False,
        )
        decision = _choice(choice).decision
        if decision is InteractionDecision.KEEP_OPEN:
            continue
        if decision is InteractionDecision.RESTART_BROWSER:
            raise RestartBrowserException(f"restart browser requested: {email} at {step}")
        if decision is InteractionDecision.ABORT_BATCH:
            raise QuitBatchException(f"operator quit: {email} at {step}")
        if decision is InteractionDecision.SKIP_ACCOUNT:
            raise SkipEmailException(f"skipped: {email} at {step}")
        if decision is InteractionDecision.DEFER_ACCOUNT:
            return InteractionDecision.DEFER_ACCOUNT


async def request_manual_intervention(
    page,
    email: str,
    step: str,
    description: str,
    verification_fn: Callable[[], Awaitable[bool]],
    next_step_description: Optional[str] = None,
    next_step_verification_fn: Optional[Callable[[], Awaitable[bool]]] = None,
    target_verifiers: Optional[Mapping[str, Callable[[], Awaitable[bool]]]] = None,
    target_labels: Optional[Mapping[str, str]] = None,
) -> bool:
    """Pause for an operator and resume only after the verifier succeeds."""
    last_error: BaseException = RuntimeError(description)
    while True:
        target_options = tuple(
            (target_id, (target_labels or {}).get(target_id, target_id))
            for target_id in (target_verifiers or {})
        )
        choice = await _interactive_menu(
            page,
            email,
            step,
            last_error,
            description,
            verification_available=True,
            retry_allowed=False,
            advance_available=(
                next_step_description is not None
                and next_step_verification_fn is not None
            ),
            next_step_description=next_step_description,
            target_options=target_options,
        )
        selected = _choice(choice)
        if selected.decision is InteractionDecision.RESTART_BROWSER:
            raise RestartBrowserException(f"restart browser requested: {email} at {step}")
        if selected.decision is InteractionDecision.ABORT_BATCH:
            raise QuitBatchException(f"operator quit: {email} at {step}")
        if selected.decision is InteractionDecision.SKIP_ACCOUNT:
            raise SkipEmailException(f"skipped: {email} at {step}")
        if selected.decision is InteractionDecision.DEFER_ACCOUNT:
            raise OperatorDeferredException(f"operator required: {email} at {step}")
        verification_to_run = verification_fn
        if selected.decision is InteractionDecision.SELECT_TARGET:
            verification_to_run = (target_verifiers or {}).get(selected.target_id)
            if verification_to_run is None:
                raise NeedsOperatorException(
                    f"selected target is not available: {email} at {step}"
                )
        if selected.decision is InteractionDecision.ADVANCE_AFTER_MANUAL:
            if next_step_verification_fn is None:
                raise NeedsOperatorException(
                    f"next-step verification is not available: {email} at {step}"
                )
            verification_to_run = next_step_verification_fn
        try:
            if await verification_to_run():
                if (
                    selected.decision is InteractionDecision.ADVANCE_AFTER_MANUAL
                    and next_step_description
                ):
                    print(f"  当前步骤已确认，下一步：{next_step_description}")
                return True
        except FATAL_EXCEPTIONS:
            raise
        except Exception as exc:
            last_error = exc
            continue
        last_error = RuntimeError(
            f"manual action did not reach the expected state: {step}"
        )


async def guarded_execution(
    page,
    action_fn: Callable[[], Awaitable[Any]],
    email: str,
    step: str,
    description: str,
    max_auto_retry: int = 2,
    timeout: Optional[float] = None,
    menu_active_callback: Optional[Callable[[], None]] = None,
    verification_fn: Optional[Callable[[], Awaitable[bool]]] = None,
    manual_success_value: Any = True,
    retry_allowed: bool = True,
    next_step_description: Optional[str] = None,
    next_step_verification_fn: Optional[Callable[[], Awaitable[bool]]] = None,
    target_verifiers: Optional[Mapping[str, Callable[[], Awaitable[bool]]]] = None,
    target_labels: Optional[Mapping[str, str]] = None,
    refill_none_fn: Optional[Callable[[], Awaitable[Any]]] = None,
    switch_card_quarantine_fn: Optional[Callable[[], Awaitable[Any]]] = None,
    switch_card_release_fn: Optional[Callable[[], Awaitable[Any]]] = None,
    switch_address_fn: Optional[Callable[[], Awaitable[Any]]] = None,
    menu_on_result: object = None,
    on_menu_retry: Optional[Callable[[], Awaitable[Any]]] = None,
) -> Any:
    """Execute *action_fn* with auto-retry and interactive fallback.

    When *action_fn* raises an exception:
      1. Auto-retry up to *max_auto_retry* times (with optional *timeout*).
      2. If all retries are exhausted (or timeout) → beep, screenshot, interactive menu.
      3. Operator chooses: verify manual work, retry, restart browser, skip, or quit.
    """
    _circuit_expose(step)
    menu_trigger_values = (
        tuple(menu_on_result)
        if isinstance(menu_on_result, (tuple, list, set, frozenset))
        else (menu_on_result,)
    )
    async def retry_action() -> Any:
        if on_menu_retry is not None:
            return await on_menu_retry()
        return await action_fn()

    advance_available = (
        next_step_description is not None
        and next_step_verification_fn is not None
    )
    target_options = tuple(
        (target_id, (target_labels or {}).get(target_id, target_id))
        for target_id in (target_verifiers or {})
    )

    def is_retry_allowed() -> bool:
        if not callable(retry_allowed):
            return retry_allowed
        try:
            return bool(retry_allowed())
        except Exception:
            logger.warning("guarded/%s: retry permission check failed", step)
            return False

    # ── Helper: enter interactive menu loop (no timeout) ──
    async def _enter_menu(last_exc: BaseException) -> Any:
        """Show interactive menu and loop until operator resolves."""
        if menu_active_callback is not None:
            menu_active_callback()

        while True:
            choice = await _interactive_menu(
                page,
                email,
                step,
                last_exc,
                description,
                verification_available=(verification_fn is not None or bool(target_verifiers)),
                retry_allowed=is_retry_allowed(),
                advance_available=advance_available,
                next_step_description=next_step_description,
                target_options=target_options,
            )

            selected = _choice(choice)
            if selected.decision is InteractionDecision.RESTART_BROWSER:
                raise RestartBrowserException(f"restart browser requested: {email} at {step}")

            if selected.decision is InteractionDecision.ABORT_BATCH:
                raise QuitBatchException(f"operator quit: {email} at {step}")

            if selected.decision is InteractionDecision.SKIP_ACCOUNT:
                raise SkipEmailException(f"skipped: {email} at {step}")

            if selected.decision is InteractionDecision.DEFER_ACCOUNT:
                raise OperatorDeferredException(f"operator required: {email} at {step}")

            if selected.decision is InteractionDecision.SELECT_TARGET:
                target_verifier = (target_verifiers or {}).get(selected.target_id)
                if target_verifier is None:
                    raise NeedsOperatorException(
                        f"selected target is not available: {email} at {step}"
                    )
                try:
                    if await target_verifier():
                        _circuit_clear(step)
                        return manual_success_value
                except FATAL_EXCEPTIONS:
                    raise
                except Exception as verify_exc:
                    _reraise_session_loss(verify_exc)
                    last_exc = verify_exc
                    continue
                last_exc = RuntimeError(
                    f"selected target did not reach the expected state: {selected.target_id}"
                )
                continue

            if selected.decision is InteractionDecision.ADVANCE_AFTER_MANUAL:
                if not advance_available or next_step_verification_fn is None:
                    raise NeedsOperatorException(
                        f"next-step verification is not available: {email} at {step}"
                    )
                try:
                    if await next_step_verification_fn():
                        if next_step_description:
                            print(f"  当前步骤已确认，下一步：{next_step_description}")
                        _circuit_clear(step)
                        return manual_success_value
                except FATAL_EXCEPTIONS:
                    raise
                except Exception as verify_exc:
                    _reraise_session_loss(verify_exc)
                    last_exc = verify_exc
                    continue
                last_exc = RuntimeError(
                    f"next step did not reach the expected state: {step}"
                )
                continue

            if selected.decision is InteractionDecision.VERIFY_MANUAL:
                if verification_fn is None:
                    raise NeedsOperatorException(
                        f"manual verification is not available: {email} at {step}"
                    )
                try:
                    if await verification_fn():
                        _circuit_clear(step)
                        return manual_success_value
                except FATAL_EXCEPTIONS:
                    raise
                except Exception as verify_exc:
                    _reraise_session_loss(verify_exc)
                    last_exc = verify_exc
                    continue
                last_exc = RuntimeError(
                    f"manual action did not reach the expected state: {step}"
                )
                continue

            if selected.decision is InteractionDecision.STRIPE_REFILL_NONE:
                if refill_none_fn is not None:
                    try:
                        result = await refill_none_fn()
                        _circuit_clear(step)
                        return result
                    except Exception as retry_exc:
                        _reraise_session_loss(retry_exc)
                        last_exc = retry_exc
                        continue
            if selected.decision is InteractionDecision.STRIPE_SWITCH_CARD_QUARANTINE:
                if switch_card_quarantine_fn is not None:
                    try:
                        result = await switch_card_quarantine_fn()
                        if result == "SWITCH_FAILED":
                            raise RuntimeError("card switch failed; keep old card")
                        _circuit_clear(step)
                        return result
                    except Exception as retry_exc:
                        _reraise_session_loss(retry_exc)
                        last_exc = retry_exc
                        continue
            if selected.decision is InteractionDecision.STRIPE_SWITCH_CARD_RELEASE:
                if switch_card_release_fn is not None:
                    try:
                        result = await switch_card_release_fn()
                        if result == "SWITCH_FAILED":
                            raise RuntimeError("card switch failed; keep old card")
                        _circuit_clear(step)
                        return result
                    except Exception as retry_exc:
                        _reraise_session_loss(retry_exc)
                        last_exc = retry_exc
                        continue
            if selected.decision is InteractionDecision.STRIPE_SWITCH_ADDRESS:
                if switch_address_fn is not None:
                    try:
                        result = await switch_address_fn(selected.target_id or "")
                        _circuit_clear(step)
                        return result
                    except Exception as retry_exc:
                        _reraise_session_loss(retry_exc)
                        last_exc = retry_exc
                        continue

            # RETRY_AUTO: re-execute the failed action.
            if not is_retry_allowed() and selected.decision is InteractionDecision.RETRY_AUTO:
                raise NeedsOperatorException(
                    f"automatic retry is not permitted for this step: {step}"
                )
            try:
                result = await retry_action()
                _circuit_clear(step)
                return result
            except Exception as retry_exc:
                if isinstance(retry_exc, (KeyboardInterrupt, SystemExit,
                                           asyncio.CancelledError,
                                           MemoryError, RecursionError)):
                    raise
                _reraise_session_loss(retry_exc)
                logger.warning("guarded/%s: retry also failed: %s", step, retry_exc)
                # Loop back to menu
                continue

    # ── Auto-retry phase ──
    try:
        if timeout is not None:
            result = await asyncio.wait_for(retry_action(), timeout=timeout)
        else:
            result = await retry_action()
        if menu_on_result is not None and result in menu_trigger_values:
            _circuit_count(step)
            return await _enter_menu(
                RuntimeError(
                    f"step result {menu_on_result!r} requires an operator decision"
                )
            )
        _circuit_clear(step)
        return result

    except asyncio.TimeoutError:
        logger.warning(
            "guarded/%s: auto-retry timed out after %.0fs — entering interactive menu",
            step, timeout,
        )
        _circuit_count(step)
        timeout_exc = TimeoutError(
            f"自动重试超时 ({timeout}s)，所有尝试均失败"
        )
        return await _enter_menu(timeout_exc)

    except (SkipEmailException, QuitBatchException, RestartBrowserException):
        raise
    except Exception as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit,
                            asyncio.CancelledError, MemoryError, RecursionError)):
            raise
        _reraise_session_loss(exc)

        # First attempt failed — continue auto-retry
        for attempt in range(2, max_auto_retry + 1):
            logger.warning(
                "guarded/%s: attempt %d/%d failed: %s",
                step, attempt - 1, max_auto_retry, exc,
            )
            await asyncio.sleep(1.5)
            try:
                if timeout is not None:
                    result = await asyncio.wait_for(retry_action(), timeout=timeout)
                else:
                    result = await retry_action()
                if menu_on_result is not None and result in menu_trigger_values:
                    _circuit_count(step)
                    return await _enter_menu(
                        RuntimeError(
                            f"step result {menu_on_result!r} requires an operator decision"
                        )
                    )
                _circuit_clear(step)
                return result
            except asyncio.TimeoutError:
                logger.warning(
                    "guarded/%s: auto-retry timed out after %.0fs",
                    step, timeout,
                )
                break
            except Exception as retry_exc:
                if isinstance(retry_exc, (KeyboardInterrupt, SystemExit,
                                          asyncio.CancelledError,
                                          MemoryError, RecursionError)):
                    raise
                _reraise_session_loss(retry_exc)
                exc = retry_exc
                continue

        # All auto-retries exhausted → interactive menu (no timeout)
        _circuit_count(step)
        return await _enter_menu(exc)

    # Should not reach here
    raise SkipEmailException(f"exhausted: {email} at {step}")
