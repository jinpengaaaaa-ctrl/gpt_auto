"""Conservative business-state verification for subscription actions."""

from __future__ import annotations

from gpt_automation.workflow.models import StepOutcome, StepStatus


def classify_subscription_status(text: str) -> StepOutcome:
    """Require explicit active-Plus evidence before accepting payment."""
    normalized = (text or "").casefold()
    has_plus = "chatgpt plus" in normalized
    active_markers = (
        "status: active",
        "active plan",
        "next billing date",
        "renews",
        "利用中",
        "次回請求",
    )
    if has_plus and any(marker in normalized for marker in active_markers):
        return StepOutcome(StepStatus.VERIFIED, "SUBSCRIPTION_ACTIVE")
    return StepOutcome(StepStatus.NEEDS_OPERATOR, "SUBSCRIPTION_UNCONFIRMED")


def classify_cancellation_status(
    text: str,
    *,
    cancel_button_visible: bool,
) -> StepOutcome:
    """Require explicit cancellation or expiry evidence.

    A missing cancel button is only a diagnostic signal. It is never enough
    to mark a cancellation successful on its own.
    """
    normalized = (text or "").casefold()
    cancellation_markers = (
        "キャンセル済み",
        "解約済み",
        "キャンセルされます",
        "キャンセルされました",
        "will not renew",
        "will be cancelled",
        "will be canceled",
        "cancelled on",
        "canceled on",
    )
    if any(marker.casefold() in normalized for marker in cancellation_markers):
        return StepOutcome(StepStatus.VERIFIED, "CANCELLATION_CONFIRMED")
    if cancel_button_visible:
        return StepOutcome(StepStatus.NEEDS_OPERATOR, "CANCELLATION_UNCONFIRMED")
    return StepOutcome(StepStatus.NEEDS_OPERATOR, "CANCELLATION_UNCONFIRMED")
