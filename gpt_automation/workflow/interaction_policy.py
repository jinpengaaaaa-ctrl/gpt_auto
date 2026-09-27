"""Interaction policies shared by every registration mode."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Iterator, Optional, Protocol


class InteractionDecision(str, Enum):
    VERIFY_MANUAL = "verify_manual"
    ADVANCE_AFTER_MANUAL = "advance_after_manual"
    RETRY_AUTO = "retry_auto"
    RESTART_BROWSER = "restart_browser"
    SKIP_ACCOUNT = "skip_account"
    ABORT_BATCH = "abort_batch"
    DEFER_ACCOUNT = "defer_account"
    KEEP_OPEN = "keep_open"
    SELECT_TARGET = "select_target"
    STRIPE_REFILL_NONE = "stripe_refill_none"
    STRIPE_SWITCH_CARD_QUARANTINE = "stripe_switch_card_quarantine"
    STRIPE_SWITCH_CARD_RELEASE = "stripe_switch_card_release"
    STRIPE_SWITCH_ADDRESS = "stripe_switch_address"


_STRIPE_INTERVENTION_STEPS = (
    "stripe_card",
    "stripe_confirm",
    "stripe_payment",
    "stripe_manual_submit",
    "stripe_addr",
)


@dataclass(frozen=True)
class InteractionChoice:
    """A decision plus an allow-listed later workflow target."""

    decision: InteractionDecision
    target_id: str = ""


@dataclass(frozen=True)
class FailureContext:
    page: object
    email: str
    step: str
    exception: BaseException
    description: str
    verification_available: bool
    retry_allowed: bool = True
    advance_available: bool = False
    next_step_description: Optional[str] = None
    target_options: tuple[tuple[str, str], ...] = ()
    stripe_retry_available: bool = False


class InteractionPolicy(Protocol):
    async def decide(self, context: FailureContext) -> InteractionDecision | InteractionChoice:
        """Choose how the workflow handles a blocked step."""


class AssistedPolicy:
    """Wait for an operator and never interpret Enter as skipping a step."""

    def __init__(self, input_fn: Optional[Callable[[str], str]] = None) -> None:
        self._input_fn = input_fn

    async def decide(self, context: FailureContext) -> InteractionDecision | InteractionChoice:
        prompt_fn = self._input_fn or input
        retry_text = "R=重试 / " if context.retry_allowed else ""
        advance_text = " / N=人工完成下一步" if context.advance_available else ""
        target_text = " / T:<目标ID>=选择后置目标" if context.target_options else ""
        stripe_text = (
            " / 1=不重填 / 2a=换卡(弃用) / 2b=换卡(保留) / 3=换地址"
            if (
                context.stripe_retry_available
                or context.step in _STRIPE_INTERVENTION_STEPS
            )
            else ""
        )
        prompt = f"  选择 [{retry_text}Enter=人工完成 / C=重开浏览器{advance_text}{target_text}{stripe_text} / S=跳过账户 / Q=终止批次]: "
        raw_choice = (
            await asyncio.get_running_loop().run_in_executor(
                None,
                prompt_fn,
                prompt,
            )
        ).strip()
        choice = raw_choice.upper()
        if choice == "1":
            return InteractionDecision.STRIPE_REFILL_NONE
        if choice in ("2A", "21"):
            return InteractionDecision.STRIPE_SWITCH_CARD_QUARANTINE
        if choice in ("2B", "22"):
            return InteractionDecision.STRIPE_SWITCH_CARD_RELEASE
        if choice == "3":
            return InteractionDecision.STRIPE_SWITCH_ADDRESS
        if choice.startswith("T:"):
            target_id = raw_choice.split(":", 1)[1].strip()
            allowed_targets = {target for target, _ in context.target_options}
            if target_id in allowed_targets:
                return InteractionChoice(InteractionDecision.SELECT_TARGET, target_id)
            return InteractionDecision.DEFER_ACCOUNT
        if choice == "N":
            return (
                InteractionDecision.ADVANCE_AFTER_MANUAL
                if context.advance_available
                else InteractionDecision.DEFER_ACCOUNT
            )
        if choice == "R":
            return (
                InteractionDecision.RETRY_AUTO
                if context.retry_allowed
                else InteractionDecision.VERIFY_MANUAL
                if context.verification_available
                else InteractionDecision.DEFER_ACCOUNT
            )
        if choice == "C":
            return InteractionDecision.RESTART_BROWSER
        if choice in ("M", "VERIFY"):
            return (
                InteractionDecision.VERIFY_MANUAL
                if context.verification_available
                else InteractionDecision.DEFER_ACCOUNT
            )
        if choice == "S":
            return InteractionDecision.SKIP_ACCOUNT
        if choice == "Q":
            return InteractionDecision.ABORT_BATCH
        if choice == "D":
            return InteractionDecision.DEFER_ACCOUNT
        if not context.verification_available and not choice:
            return InteractionDecision.KEEP_OPEN
        return (
            InteractionDecision.VERIFY_MANUAL
            if context.verification_available
            else InteractionDecision.DEFER_ACCOUNT
        )


class UnattendedPolicy:
    """Suspend blocked accounts instead of skipping business steps."""

    async def decide(self, context: FailureContext) -> InteractionDecision:
        return InteractionDecision.DEFER_ACCOUNT


class ContinueOnBlockedPolicy:
    """连续执行：账户步骤遇阻后自动跳过当前账户，继续下一个账户。

    允许由批次调度层用于 ``account_provision`` 与 ``checkout_fulfillment`` 范围；
    未知副作用步骤由调度层重分类为 UNKNOWN_SIDE_EFFECT 并隔离，不自动重试。
    """

    async def decide(self, context: FailureContext) -> InteractionDecision:
        return InteractionDecision.SKIP_ACCOUNT


_DEFAULT_POLICY = AssistedPolicy()
_CURRENT_POLICY: ContextVar[InteractionPolicy] = ContextVar(
    "registration_interaction_policy",
    default=_DEFAULT_POLICY,
)


def get_interaction_policy() -> InteractionPolicy:
    return _CURRENT_POLICY.get()


@contextmanager
def use_interaction_policy(policy: InteractionPolicy) -> Iterator[None]:
    token = _CURRENT_POLICY.set(policy)
    try:
        yield
    finally:
        _CURRENT_POLICY.reset(token)
