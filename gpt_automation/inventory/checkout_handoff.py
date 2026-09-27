"""Cross-browser checkout handoff gate.

An account may start a new checkout session under any browser backend only
when its inventory state, credentials and side-effect journal prove the
handoff is safe.  This module performs no page actions and duplicates no
business phases; it only decides whether a new checkout may begin.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from gpt_automation.browser.models import BrowserBackend
from gpt_automation.security.credential_store import CredentialStore
from gpt_automation.state.store import StateStore


class CheckoutHandoffStatus(str, Enum):
    """Stable verdicts; never a bare True/False across layers."""

    ELIGIBLE = "ELIGIBLE"
    ACCOUNT_NOT_FOUND = "ACCOUNT_NOT_FOUND"
    ACCOUNT_NOT_READY = "ACCOUNT_NOT_READY"
    CREDENTIAL_INCOMPLETE = "CREDENTIAL_INCOMPLETE"
    AUTH_METHOD_UNSUPPORTED = "AUTH_METHOD_UNSUPPORTED"
    PROVISION_NOT_FINALIZED = "PROVISION_NOT_FINALIZED"
    ACTIVE_SESSION_EXISTS = "ACTIVE_SESSION_EXISTS"
    UNSETTLED_SIDE_EFFECT = "UNSETTLED_SIDE_EFFECT"
    TARGET_BACKEND_UNAVAILABLE = "TARGET_BACKEND_UNAVAILABLE"


@dataclass(frozen=True)
class CheckoutHandoffResult:
    status: CheckoutHandoffStatus
    account_id: str
    reason: str = ""

    @property
    def eligible(self) -> bool:
        return self.status is CheckoutHandoffStatus.ELIGIBLE


def _has_unsettled_side_effect(
    state_store: StateStore,
    session_id: Optional[str],
) -> bool:
    """True when a prior checkout session left an unresolved external action."""
    if session_id is None:
        return False
    unsettled = {"STARTED", "UNKNOWN"}
    for action, prefix in (
        ("CARD_PRESENTATION", "CARD_PRESENTATION"),
        ("PAYMENT_SUBMIT", "PAYMENT_SUBMIT"),
    ):
        record = state_store.get_action(session_id, action)
        if record is not None:
            state = str(getattr(record.state, "value", record.state))
            if state in unsettled:
                return True
        # 编号重试动作（PAYMENT_SUBMIT#2 / #3 …）同样代表未结算的外部动作。
        for numbered in state_store.list_actions_by_prefix(session_id, prefix):
            state = str(getattr(numbered.state, "value", numbered.state))
            if state in unsettled:
                return True
    return False


def evaluate_checkout_handoff(
    *,
    state_store: StateStore,
    credential_store: CredentialStore,
    account_id: str,
    target_backend: str | BrowserBackend,
    target_backend_ready: bool = True,
    active_session_lock: bool = False,
) -> CheckoutHandoffResult:
    """Decide whether a new checkout may start under ``target_backend``."""
    backend = (
        target_backend.value
        if isinstance(target_backend, BrowserBackend)
        else str(target_backend).strip().lower()
    )

    def reject(status: CheckoutHandoffStatus, reason: str) -> CheckoutHandoffResult:
        return CheckoutHandoffResult(status=status, account_id=account_id, reason=reason)

    record = state_store.get_registered_account(account_id)
    if record is None:
        return reject(CheckoutHandoffStatus.ACCOUNT_NOT_FOUND, "account not found")
    if record.status != "READY":
        return reject(
            CheckoutHandoffStatus.ACCOUNT_NOT_READY,
            f"account status is {record.status}",
        )
    if record.source == "provisioned" and not record.provision_session_id:
        return reject(
            CheckoutHandoffStatus.PROVISION_NOT_FINALIZED,
            "provision session was never finalized",
        )
    if record.has_oauth and not record.has_chatgpt_password:
        return reject(
            CheckoutHandoffStatus.AUTH_METHOD_UNSUPPORTED,
            "oauth-only accounts cannot log in across browser backends",
        )
    try:
        credential = credential_store.load(account_id)
    except Exception:  # noqa: BLE001 - unreadable credentials are incomplete
        credential = None
    if credential is None or not credential.complete or credential.consumed:
        return reject(
            CheckoutHandoffStatus.CREDENTIAL_INCOMPLETE,
            "recoverable email+password+TOTP credentials are missing",
        )
    if active_session_lock:
        return reject(
            CheckoutHandoffStatus.ACTIVE_SESSION_EXISTS,
            "an active session lock holds the account",
        )
    if _has_unsettled_side_effect(state_store, record.last_checkout_session_id):
        return reject(
            CheckoutHandoffStatus.UNSETTLED_SIDE_EFFECT,
            "a prior checkout left a payment/card action unsettled",
        )
    if not target_backend_ready:
        return reject(
            CheckoutHandoffStatus.TARGET_BACKEND_UNAVAILABLE,
            f"browser backend {backend} is not ready",
        )
    return CheckoutHandoffResult(
        status=CheckoutHandoffStatus.ELIGIBLE,
        account_id=account_id,
    )
