"""Typed outcomes shared by every account registration entrypoint."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Optional, Sequence


class AccountStatus(str, Enum):
    SUCCESS = "SUCCESS"
    ACCOUNT_READY = "ACCOUNT_READY"
    VERIFICATION_COMPLETE = "VERIFICATION_COMPLETE"
    RETRYABLE_FAILURE = "RETRYABLE_FAILURE"
    NEEDS_OPERATOR = "NEEDS_OPERATOR"
    BUSINESS_REJECTED = "BUSINESS_REJECTED"
    UNKNOWN_SIDE_EFFECT = "UNKNOWN_SIDE_EFFECT"
    FATAL = "FATAL"


class RunScope(str, Enum):
    """Supported batch pipelines sharing one RegistrationWorkflow."""

    FULL = "full"
    ACCOUNT_PROVISION = "account_provision"
    CHECKOUT_FULFILLMENT = "checkout_fulfillment"


class CheckoutGoal(str, Enum):
    """White-listed execution targets for a checkout_fulfillment batch."""

    COMPLETE = "complete"
    PRICING_PAGE = "pricing_page"


class WorkflowStage(str, Enum):
    CREATED = "CREATED"
    NETWORK_PENDING = "NETWORK_PENDING"
    NETWORK_READY = "NETWORK_READY"
    ACCOUNT_VERIFIED = "ACCOUNT_VERIFIED"
    PASSWORD_PENDING = "PASSWORD_PENDING"
    PASSWORD_VERIFIED = "PASSWORD_VERIFIED"
    MFA_PENDING = "MFA_PENDING"
    MFA_VERIFIED = "MFA_VERIFIED"
    PRICING_VERIFIED = "PRICING_VERIFIED"
    OFFER_VERIFIED = "OFFER_VERIFIED"
    PAYMENT_PENDING = "PAYMENT_PENDING"
    PAYMENT_VERIFIED = "PAYMENT_VERIFIED"
    CANCELLATION_PENDING = "CANCELLATION_PENDING"
    CANCELLATION_VERIFIED = "CANCELLATION_VERIFIED"
    RESULT_PERSISTED = "RESULT_PERSISTED"
    COMPLETED = "COMPLETED"

    @property
    def rank(self) -> int:
        return _WORKFLOW_STAGE_ORDER.index(self)


_WORKFLOW_STAGE_ORDER = tuple(WorkflowStage)


class StepStatus(str, Enum):
    """Business meaning of one workflow step, independent of page mechanics."""

    VERIFIED = "VERIFIED"
    BUSINESS_REJECTED = "BUSINESS_REJECTED"
    NEEDS_OPERATOR = "NEEDS_OPERATOR"


@dataclass(frozen=True)
class StepOutcome:
    """Typed step result used instead of ambiguous booleans across layers."""

    status: StepStatus
    code: str
    reason: str = ""

    @property
    def verified(self) -> bool:
        return self.status is StepStatus.VERIFIED


_BUSINESS_REJECTED = {
    "NO_FREE_OFFER",
    "NO_TARGET_OFFER",
    "NON_ZERO_AMOUNT",
    "CHECKOUT_NON_ZERO_AMOUNT",
    "SKIPPED",
    "ACCOUNT_ALREADY_EXISTS",
}
_ACCOUNT_READY = {
    "ACCOUNT_READY",
}
_NEEDS_OPERATOR = {
    "NEEDS_OPERATOR",
    "AMOUNT_UNKNOWN",
    "CHECKOUT_AMOUNT_UNKNOWN",
    "CHECKOUT_SIGNUP_ROUTE_FORBIDDEN",
    "MFA_UNCONFIRMED",
    "CARD_UNAVAILABLE",
    "FINALIZATION_FAILED",
    "FINALIZATION_PENDING",
    "SKIP_ARCHIVE_FAILED",
}
_UNKNOWN_SIDE_EFFECT = {
    "PAYMENT_UNKNOWN",
    "STATUS_UNCONFIRMED",
    "UNKNOWN_SIDE_EFFECT",
}
_VERIFICATION_COMPLETE = {
    "PRICING_PAGE_VERIFIED",
}
_RETRYABLE_FAILURE = {
    "PREFLIGHT_FAILED",
    "IP_NOT_ROTATED",
    "IP_HISTORY_PERSIST_FAILED",
}


def _status_for_code(code: str) -> AccountStatus:
    """Map a raw result code onto its account status (single source of truth)."""
    if code == "success":
        return AccountStatus.SUCCESS
    if code in _ACCOUNT_READY:
        return AccountStatus.ACCOUNT_READY
    if code in _VERIFICATION_COMPLETE:
        return AccountStatus.VERIFICATION_COMPLETE
    if code in _NEEDS_OPERATOR:
        return AccountStatus.NEEDS_OPERATOR
    if code in _BUSINESS_REJECTED:
        return AccountStatus.BUSINESS_REJECTED
    if code in _UNKNOWN_SIDE_EFFECT:
        return AccountStatus.UNKNOWN_SIDE_EFFECT
    if code in _RETRYABLE_FAILURE:
        return AccountStatus.RETRYABLE_FAILURE
    return AccountStatus.FATAL


@dataclass(frozen=True)
class RegistrationInput:
    """Immutable account input shared by every registration entrypoint."""

    email: str
    email_password: str
    name: str
    age: str
    card_number: str
    card_expiry: str
    card_cvc: str
    zip_code: str
    prefecture: str
    city: str
    address1: str
    address2: str
    client_id: str = ""
    refresh_token: str = ""
    chatgpt_password: str = ""
    totp_secret: str = ""

    @classmethod
    def from_row(
        cls,
        row: Sequence[str],
        *,
        client_id: str = "",
        refresh_token: str = "",
        chatgpt_password: str = "",
        totp_secret: str = "",
    ) -> "RegistrationInput":
        if len(row) != 12:
            raise ValueError(f"registration input requires 12 fields, got {len(row)}")
        return cls(
            email=row[0],
            email_password=row[1],
            name=row[2],
            age=row[3],
            card_number=row[4],
            card_expiry=row[5],
            card_cvc=row[6],
            zip_code=row[7],
            prefecture=row[8],
            city=row[9],
            address1=row[10],
            address2=row[11],
            client_id=client_id,
            refresh_token=refresh_token,
            chatgpt_password=chatgpt_password,
            totp_secret=totp_secret,
        )

    def to_row(self) -> list[str]:
        return [
            self.email,
            self.email_password,
            self.name,
            self.age,
            self.card_number,
            self.card_expiry,
            self.card_cvc,
            self.zip_code,
            self.prefecture,
            self.city,
            self.address1,
            self.address2,
        ]


@dataclass(frozen=True)
class AccountRunResult:
    email: str
    status: AccountStatus
    raw_status: str
    ip: Optional[str] = None
    stage: str = ""
    reason: str = ""
    resume_id: str = ""
    # 创建账户 Offer 验收附加信息（仅 account_provision 填充；其余范围保持空）
    offer_result: Optional[str] = None   # HAS_OFFER / NO_OFFER / FAILED
    page_url: str = ""
    promo_campaign: Optional[str] = None
    evidence_paths: tuple = ()
    # 截图采集结果：captured / browser_not_started / page_closed /
    # capture_timeout / write_failed / missing
    screenshot_status: str = "missing"
    # ???? 2FA ????????????
    passwordless: bool = False

    @property
    def succeeded(self) -> bool:
        return self.status is AccountStatus.SUCCESS

    @classmethod
    def for_code(
        cls,
        *,
        email: str,
        code: str,
        ip: Optional[str] = None,
        stage: str = "",
        reason: str = "",
        resume_id: str = "",
    ) -> "AccountRunResult":
        """Create a result while keeping status-code classification in one place."""
        return cls(
            email=email,
            status=_status_for_code(code),
            raw_status=code,
            ip=ip,
            stage=stage,
            reason=reason or code,
            resume_id=resume_id,
        )

    def reclassify(self, code: str) -> "AccountRunResult":
        """Change only status/raw_status; keep every evidence field.

        Continuous-mode conversions (e.g. NEEDS_OPERATOR → SKIPPED) must not
        lose the captured screenshot, page URL, campaign or failure reason.
        """
        return replace(
            self,
            status=_status_for_code(code),
            raw_status=code,
        )
