"""Crash-resumable finalization of a successful account workflow."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import config
from gpt_automation.browser.cleanup import cleanup_profile
from gpt_automation.state import (
    FinalizationRecord,
    FinalizationStatus,
    ResourceState,
    StateStore,
    StateTransitionError,
)
from gpt_automation.workflow.models import AccountRunResult, AccountStatus


class FinalizationError(RuntimeError):
    """Raised when a successful workflow cannot be finalized safely."""


def _plaintext_result_writer(account: str, status_token: str, session_id: str) -> bool:
    """full 流程成功：从凭据库组装四列明文写入 results.txt。

    与 orchestrator 的 `_consume_credentials` 一致，按需构造 CredentialStore；
    凭据缺失或不可读时返回 False，由 finalization 标记失败而非猜测成功。
    """
    del account, status_token
    from gpt_automation.results.final_file import FinalResultsFile
    from gpt_automation.security.credential_store import CredentialStore, CredentialStoreError

    try:
        credential = CredentialStore(config.CREDENTIAL_STORE_DIR).load(session_id)
    except CredentialStoreError:
        return False
    if credential is None or not credential.account:
        return False
    return FinalResultsFile(config.OUTPUT_CSV).append(
        email=credential.account,
        chatgpt_password=credential.chatgpt_password,
        totp_secret=credential.totp_secret,
        email_password=credential.email_password,
    )


def _cleanup_profile_or_confirm_missing(profile_id: str) -> bool:
    """Treat an already-absent profile as an idempotent cleanup success."""
    profile_root = Path(config.PROFILE_DIR).resolve()
    profile_path = (profile_root / f"profile_{profile_id}").resolve()
    if profile_path.parent == profile_root and not profile_path.exists():
        return True
    return cleanup_profile(profile_id)


class FinalizationCoordinator:
    """Commit result, resources, session, and cleanup in resumable order."""

    def __init__(
        self,
        state_store: StateStore,
        *,
        result_writer: Callable[[str, str, str], bool] | None = None,
        credential_consumer: Callable[[str], bool] | None = None,
        profile_cleaner: Callable[[str], bool] | None = None,
    ) -> None:
        self.state_store = state_store
        self.result_writer = result_writer or _plaintext_result_writer
        self.credential_consumer = credential_consumer or (lambda _session_id: True)
        self.profile_cleaner = profile_cleaner or _cleanup_profile_or_confirm_missing

    def finalize(
        self,
        session_id: str,
        *,
        account: str,
        result: AccountRunResult,
        profile_id: str,
    ) -> FinalizationRecord:
        if result.status is not AccountStatus.SUCCESS:
            raise ValueError("only successful workflows can be finalized")

        record = self.state_store.get_finalization(session_id)
        if record is None:
            record = self.state_store.begin_finalization(
                session_id,
                result_token=AccountStatus.SUCCESS.value,
            )

        if record.status is FinalizationStatus.FINALIZED:
            return record

        if record.status in {
            FinalizationStatus.PREPARING,
            FinalizationStatus.FAILED,
        }:
            status_token = record.result_token or AccountStatus.SUCCESS.value
            from gpt_automation.workflow.graph import Evidence, WorkflowStepId

            result_evidence_exists = self.state_store.get_step_evidence(
                session_id,
                WorkflowStepId.RESULT_PERSISTED.value,
            ) is not None
            if not result_evidence_exists:
                if not self.result_writer(account, status_token, session_id):
                    return self.state_store.mark_finalization(
                        session_id,
                        FinalizationStatus.FAILED,
                        last_error="RESULT_WRITE_FAILED",
                    )
                self.state_store.record_step_evidence(
                    session_id,
                    Evidence(
                        step_id=WorkflowStepId.RESULT_PERSISTED,
                        evidence_type="finalization_result_write",
                        details=(
                            ("result_state", "success_persisted"),
                            ("verification_source", "finalization_coordinator"),
                        ),
                        verifier_version="finalization-v1",
                    ),
                )
            try:
                credentials_consumed = self.credential_consumer(session_id)
            except StateTransitionError:
                credentials_consumed = False
            if not credentials_consumed:
                return self.state_store.mark_finalization(
                    session_id,
                    FinalizationStatus.FAILED,
                    last_error="CREDENTIAL_CONSUME_FAILED",
                )
            record = self.state_store.mark_finalization(
                session_id,
                FinalizationStatus.RESULT_PERSISTED,
            )

        if record.status is FinalizationStatus.RESULT_PERSISTED:
            try:
                self.state_store.transition_session_resources(
                    session_id,
                    {
                        "email": ResourceState.CONSUMED,
                        "card": ResourceState.CONSUMED,
                    },
                    expected=(ResourceState.RESERVED, ResourceState.QUARANTINED),
                )
            except Exception:
                return self.state_store.mark_finalization(
                    session_id,
                    FinalizationStatus.FAILED,
                    last_error="RESOURCE_CONSUME_FAILED",
                )
            record = self.state_store.mark_finalization(
                session_id,
                FinalizationStatus.RESOURCES_CONSUMED,
            )

        if record.status is FinalizationStatus.RESOURCES_CONSUMED:
            self.state_store.update_session(
                session_id,
                stage="COMPLETED",
                status=AccountStatus.SUCCESS.value,
            )
            record = self.state_store.mark_finalization(
                session_id,
                FinalizationStatus.SESSION_COMPLETED,
            )

        if record.status in {
            FinalizationStatus.SESSION_COMPLETED,
            FinalizationStatus.CLEANUP_PENDING,
        }:
            if self.profile_cleaner(profile_id):
                record = self.state_store.mark_finalization(
                    session_id,
                    FinalizationStatus.FINALIZED,
                )
            else:
                record = self.state_store.mark_finalization(
                    session_id,
                    FinalizationStatus.CLEANUP_PENDING,
                    last_error="PROFILE_CLEANUP_PENDING",
                )

        return record


__all__ = [
    "FinalizationCoordinator",
    "FinalizationError",
    "FinalizationStatus",
]
