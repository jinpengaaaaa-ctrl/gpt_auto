"""Runtime bridge between the canonical workflow and persistent state."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Optional, TypeVar

from gpt_automation.state import (
    ActionState,
    EvidenceRecord,
    StateStore,
    StepRecord,
)
from gpt_automation.workflow.graph import (
    Evidence,
    WorkflowGraphError,
    WorkflowStepId,
)
from gpt_automation.workflow.models import WorkflowStage


T = TypeVar("T")

@dataclass
class WorkflowRuntimeState:
    """No-op without a store; durable checkpoints when one is configured."""

    store: Optional[StateStore] = None
    session_id: str = ""
    enforce_graph_dependencies: bool = False

    @property
    def enabled(self) -> bool:
        return self.store is not None and bool(self.session_id)

    def checkpoint(
        self,
        stage: WorkflowStage | str,
        status: str = "RUNNING",
    ) -> None:
        if self.enabled:
            assert self.store is not None
            target = stage if isinstance(stage, WorkflowStage) else WorkflowStage(stage)
            current = self.store.get_session(self.session_id)
            if (
                current is not None
                and target.rank < WorkflowStage(current.stage).rank
            ):
                return
            self.store.update_session(
                self.session_id,
                stage=target.value,
                status=status,
            )

    def has_reached(self, stage: WorkflowStage) -> bool:
        if not self.enabled:
            return False
        assert self.store is not None
        current = self.store.get_session(self.session_id)
        return current is not None and WorkflowStage(current.stage).rank >= stage.rank

    def verify_action(self, action: str) -> None:
        if self.enabled:
            assert self.store is not None
            self.store.mark_action_verified(self.session_id, action)

    def begin_action(
        self,
        action: str,
        *,
        step_id: WorkflowStepId | str = "",
        effect_class: str = "UNKNOWN",
    ) -> None:
        """Record intent before a non-idempotent browser action.

        A second start is rejected by ``StateStore`` so a resume cannot
        silently replay a password submit, MFA binding, payment, or cancel.
        """
        if self.enabled:
            assert self.store is not None
            normalized_step = (
                step_id.value if isinstance(step_id, WorkflowStepId) else step_id
            )
            self.store.begin_action(
                self.session_id,
                action,
                step_id=normalized_step,
                effect_class=effect_class,
            )

    def mark_action_unknown(self, action: str, details: str = "") -> None:
        if self.enabled:
            assert self.store is not None
            self.store.mark_action_unknown(self.session_id, action, details)

    def action_state(self, action: str) -> Optional[ActionState]:
        if not self.enabled:
            return None
        assert self.store is not None
        record = self.store.get_action(self.session_id, action)
        return None if record is None else record.state

    def record_step_status(
        self,
        step_id: WorkflowStepId | str,
        *,
        status: str,
        outcome_code: str = "",
        last_error: str = "",
    ) -> None:
        """Persist a step state without treating it as verified."""
        if not self.enabled:
            raise RuntimeError("workflow step state requires durable workflow state")
        assert self.store is not None
        normalized = step_id.value if isinstance(step_id, WorkflowStepId) else step_id
        self.store.record_step_status(
            self.session_id,
            normalized,
            status=status,
            outcome_code=outcome_code,
            last_error=last_error,
        )

    def record_step_evidence(self, evidence: Evidence) -> EvidenceRecord:
        """Persist positive evidence and atomically mark its step succeeded."""
        if not self.enabled:
            raise RuntimeError("workflow step evidence requires durable workflow state")
        assert self.store is not None
        if self.enforce_graph_dependencies:
            from gpt_automation.workflow.engine import WorkflowEngine

            plan = WorkflowEngine.for_step_execution().plan_to_state(
                evidence.step_id,
                self.store,
                self.session_id,
            )
            missing_prerequisites = tuple(
                step for step in plan.missing if step != evidence.step_id
            )
            if missing_prerequisites:
                missing = ", ".join(step.value for step in missing_prerequisites)
                raise WorkflowGraphError(
                    f"cannot verify {evidence.step_id.value}; missing: {missing}"
                )
        return self.store.record_step_evidence(self.session_id, evidence)

    def step_record(self, step_id: WorkflowStepId | str) -> Optional[StepRecord]:
        if not self.enabled:
            return None
        assert self.store is not None
        normalized = step_id.value if isinstance(step_id, WorkflowStepId) else step_id
        return self.store.get_step_record(self.session_id, normalized)

    def step_evidence(
        self,
        step_id: WorkflowStepId | str,
    ) -> Optional[EvidenceRecord]:
        if not self.enabled:
            return None
        assert self.store is not None
        normalized = step_id.value if isinstance(step_id, WorkflowStepId) else step_id
        return self.store.get_step_evidence(self.session_id, normalized)

    async def run_non_idempotent(
        self,
        action: str,
        operation: Callable[[], Awaitable[T]],
        *,
        is_verified: Callable[[T], bool],
        step_id: WorkflowStepId | str = "",
        effect_class: str = "UNKNOWN",
        unverified_state: ActionState = ActionState.UNKNOWN,
        is_rejected: Optional[Callable[[T], bool]] = None,
    ) -> T:
        """Run once and persist whether its external effect was verified.

        A result that is not verified is recorded as ``unverified_state``
        (default ``UNKNOWN``).  Only an *explicit* rejection (card declined,
        payment refused) may be recorded as ``REJECTED`` via ``is_rejected``;
        an unverified outcome must never be treated as rejected, because
        ``REJECTED`` permits a new checkout while ``UNKNOWN`` quarantines the
        account and blocks cross-backend retries.
        """
        if not self.enabled:
            raise RuntimeError(
                "non-idempotent actions require durable workflow state"
            )

        assert self.store is not None
        normalized_step = (
            step_id.value if isinstance(step_id, WorkflowStepId) else step_id
        )
        self.store.begin_action(
            self.session_id,
            action,
            step_id=normalized_step,
            effect_class=effect_class,
        )
        try:
            result = await operation()
        except BaseException as exc:
            self.store.mark_action_unknown(
                self.session_id,
                action,
                type(exc).__name__,
            )
            raise

        if is_verified(result):
            self.store.mark_action_verified(self.session_id, action)
        elif is_rejected is not None and is_rejected(result):
            rej_details = "REJECTED"
            try:
                from gpt_automation.phases.stripe import get_last_rejection_info
                info = get_last_rejection_info()
                if info is not None:
                    import json
                    rej_details = json.dumps({
                        "kind": info.kind,
                        "label": info.label,
                        "text": info.text,
                    }, ensure_ascii=False)
            except Exception:
                pass
            self.store.mark_action_rejected(self.session_id, action, rej_details)
        elif unverified_state is ActionState.REJECTED:
            rej_details = "REJECTED"
            try:
                from gpt_automation.phases.stripe import get_last_rejection_info
                info = get_last_rejection_info()
                if info is not None:
                    import json
                    rej_details = json.dumps({
                        "kind": info.kind,
                        "label": info.label,
                        "text": info.text,
                    }, ensure_ascii=False)
            except Exception:
                pass
            self.store.mark_action_rejected(self.session_id, action, rej_details)
        else:
            self.store.mark_action_unknown(
                self.session_id,
                action,
                "UNVERIFIED_RESULT",
            )
        return result
