"""Detect and recover browser screens without owning business order."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, Mapping, Optional, Set

from gpt_automation.phases.screen_detector import ScreenState, detect_screen
from gpt_automation.phases.interactive import request_manual_intervention
from gpt_automation.utils.exceptions import (
    FATAL_EXCEPTIONS,
    NeedsOperatorException,
)
from gpt_automation.workflow.manual_actions import MANUAL_ACTIONS

if TYPE_CHECKING:
    from playwright.async_api import Page

logger = logging.getLogger(__name__)


class ScreenManager:
    """Recover page mechanics before the workflow executes a business step."""

    def __init__(
        self,
        page: Page,
        ctx: Mapping[str, Any],
        manual_verifiers: Optional[
            Dict[ScreenState, Callable[[], Awaitable[bool]]]
        ] = None,
    ):
        self.page = page
        self.ctx = ctx
        self.current_state: ScreenState = ScreenState.UNKNOWN
        self.manual_verifiers = manual_verifiers or {}

        # 意外画面处理策略
        self._unexpected_handlers: Dict[
            ScreenState,
            Callable[[], Awaitable[str]],
        ] = {
            ScreenState.SETTINGS: self._handle_settings,
            ScreenState.WELCOME_MODAL: self._handle_welcome_modal,
            ScreenState.SURVEY: self._handle_survey,
            ScreenState.STRIPE_PAYMENT: self._handle_stripe_payment,
            ScreenState.STRIPE_BILLING: self._handle_stripe_billing,
            ScreenState.UNKNOWN: self._handle_unknown,
        }

    async def refresh(self) -> ScreenState:
        """刷新当前画面状态。"""
        self.current_state = await detect_screen(self.page, timeout_ms=5000)
        logger.info(f"[ScreenManager] 当前画面: {self.current_state.value}")
        return self.current_state

    async def handle_unexpected(self, state: ScreenState) -> str:
        """
        处理意外画面。

        Returns:
            "recovered" - 已恢复，可以继续执行
            "fatal" - 无法恢复，应该终止
        """
        handler = self._unexpected_handlers.get(state)

        if handler is None:
            # 没有对应的处理策略
            logger.error(f"[ScreenManager] 无处理策略的意外画面: {state.value}")
            return "fatal"

        try:
            result = await handler()
            logger.info(f"[ScreenManager] 意外画面处理结果: {result}")
            return result
        except FATAL_EXCEPTIONS:
            raise
        except Exception as e:
            logger.error(f"[ScreenManager] 意外画面处理失败: {e}")
            return "fatal"

    async def _request_operator(self, state: ScreenState) -> bool:
        spec = MANUAL_ACTIONS[state]
        email = str(self.ctx.get("email", "unknown"))

        verification_fn = self.manual_verifiers.get(state)
        if verification_fn is None:
            if state is ScreenState.UNKNOWN:
                async def verify_unknown_screen() -> bool:
                    detected = await detect_screen(self.page, timeout_ms=5000)
                    self.current_state = detected
                    return detected is not ScreenState.UNKNOWN

                verification_fn = verify_unknown_screen
            else:
                if spec.critical:
                    raise NeedsOperatorException(
                        f"business verifier is required for critical screen: {state.value}"
                    )

                async def verify_screen_changed() -> bool:
                    detected = await detect_screen(self.page, timeout_ms=5000)
                    self.current_state = detected
                    return detected is not ScreenState.UNKNOWN and detected is not state

                verification_fn = verify_screen_changed

        description = (
            f"{spec.title}：{spec.instruction} 验证条件：{spec.expected_result}"
        )
        return await request_manual_intervention(
            self.page,
            email,
            f"screen_{state.value}",
            description,
            verification_fn,
        )

    async def ensure_screen(
        self,
        accepted_states: Set[ScreenState],
        *,
        max_iterations: int = 20,
    ) -> ScreenState:
        """Recover page mechanics until the workflow's expected screen is ready."""
        for _ in range(max_iterations):
            state = await self.refresh()
            if state in accepted_states:
                return state

            recovery = await self.handle_unexpected(state)
            if recovery in {"fatal", "manual_required"}:
                await self._request_operator(state)

        raise NeedsOperatorException("screen recovery exceeded its iteration limit")

    # ========== 意外画面处理策略 ==========

    async def _handle_settings(self) -> str:
        """处理设置面板打开的情况。"""
        logger.info("[ScreenManager] 设置面板打开，尝试关闭")
        try:
            from gpt_automation.phases.phase2 import _phase2_close_settings

            await _phase2_close_settings(self.page)
            return "recovered"
        except FATAL_EXCEPTIONS:
            raise
        except Exception as e:
            logger.error(f"[ScreenManager] 关闭设置面板失败: {e}")
            return "fatal"

    async def _handle_welcome_modal(self) -> str:
        """处理 Welcome 弹窗残留。"""
        logger.info("[ScreenManager] Welcome 弹窗残留，尝试关闭")
        try:
            from gpt_automation.phases.phase1 import phase1_ready_confirm

            await phase1_ready_confirm(self.page)
            return "recovered"
        except FATAL_EXCEPTIONS:
            raise
        except Exception as e:
            logger.error(f"[ScreenManager] 关闭 Welcome 弹窗失败: {e}")
            return "fatal"

    async def _handle_survey(self) -> str:
        """处理问卷页残留。"""
        logger.info("[ScreenManager] 问卷页残留，尝试处理")
        try:
            from gpt_automation.phases.phase1 import phase1_survey

            await phase1_survey(self.page)
            return "recovered"
        except FATAL_EXCEPTIONS:
            raise
        except Exception as e:
            logger.error(f"[ScreenManager] 处理问卷失败: {e}")
            return "fatal"

    async def _handle_stripe_payment(self) -> str:
        """Preserve a payment page because its external effect may be unknown."""
        logger.warning("[ScreenManager] Stripe 支付页需要人工核对，保留现场")
        return "manual_required"

    async def _handle_stripe_billing(self) -> str:
        """Preserve a billing page until cancellation state is verified."""
        logger.warning("[ScreenManager] Stripe 账单页需要人工核对，保留现场")
        return "manual_required"

    async def _handle_unknown(self) -> str:
        """处理无法识别的画面。"""
        logger.warning("[ScreenManager] 无法识别的画面，保留现场并请求人工处理")
        return "manual_required"
