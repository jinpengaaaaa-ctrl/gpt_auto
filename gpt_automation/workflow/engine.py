"""Single entrypoint for the per-account business workflow."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, Optional, Set

from gpt_automation.workflow.interaction_policy import (
    AssistedPolicy,
    InteractionPolicy,
    use_interaction_policy,
)
from gpt_automation.workflow.models import (
    AccountRunResult,
    AccountStatus,
    RegistrationInput,
)
from gpt_automation.workflow.graph import StepStatus, WorkflowStepId

if TYPE_CHECKING:
    from gpt_automation.network.strategy import NetworkStrategy
    from gpt_automation.state import StateStore
    from gpt_automation.workflow.finalization import FinalizationCoordinator


BusinessRunner = Callable[..., Awaitable[AccountRunResult]]


@dataclass(frozen=True)
class SequenceOutcome:
    """Typed result for Engine-owned ordering of already split steps."""

    status: StepStatus
    code: str


@dataclass(frozen=True)
class SequenceStep:
    """One unit in an Engine-owned sequence; no browser behavior is stored."""

    step_id: WorkflowStepId
    name: str

    def success(self) -> SequenceOutcome:
        from gpt_automation.workflow.graph import StepStatus

        return SequenceOutcome(StepStatus.SUCCEEDED, "SEQUENCE_STEP_VERIFIED")


class WorkflowEngine:
    """State-machine boundary used during the incremental migration.

    The first migration slice deliberately delegates execution to the current
    business runner.  The graph is already available to future step handlers,
    while the compatibility runner preserves today's browser behavior until a
    concrete step is migrated.
    """

    def __init__(
        self,
        runner: Optional[BusinessRunner] = None,
        graph=None,
        registry=None,
    ) -> None:
        from gpt_automation.workflow.graph import REGISTRATION_GRAPH

        self._runner = runner
        self.graph = graph or REGISTRATION_GRAPH
        self._registry = registry

    @classmethod
    def for_step_execution(cls, graph=None, registry=None) -> "WorkflowEngine":
        """Create an engine for migrated steps without a legacy runner."""
        return cls(runner=None, graph=graph, registry=registry)

    @property
    def registry(self):
        """The explicit handler registry used by migrated graph steps."""
        return self._registry

    def handler_for(self, step_id):
        """Resolve a migrated handler or fail explicitly at the boundary."""
        if self._registry is None:
            raise RuntimeError("no step registry configured")
        return self._registry.get(step_id)

    def sequence_step(self, step_id, name: str) -> SequenceStep:
        return SequenceStep(step_id=step_id, name=name)

    async def run_sequence(
        self,
        steps,
        *,
        is_verified,
        prepare,
        execute,
    ) -> SequenceOutcome:
        """Let the Engine own ordering while adapters own step mechanics.

        ``prepare`` may recover the page for one step.  ``execute`` must
        return ``SequenceOutcome``; a raw boolean is rejected so a callback
        cannot blur business failure and success at this boundary.
        """
        from gpt_automation.workflow.graph import StepStatus

        for item in steps:
            if is_verified(item.step_id):
                continue
            await prepare(item)
            outcome = await execute(item)
            if not isinstance(outcome, SequenceOutcome):
                raise TypeError("sequence executor must return SequenceOutcome")
            if outcome.status is not StepStatus.SUCCEEDED:
                return outcome
        return SequenceOutcome(StepStatus.SUCCEEDED, "SEQUENCE_COMPLETED")

    async def execute_registered_step(self, step_id, runtime_state):
        """Execute the handler registered for ``step_id`` through one path."""
        return await self.execute_step(
            step_id,
            self.handler_for(step_id),
            runtime_state,
        )

    async def run(self, registration: RegistrationInput, **kwargs: Any) -> AccountRunResult:
        """Execute the current compatibility step through one engine boundary."""
        if self._runner is None:
            raise RuntimeError("step-only workflow engine cannot run a registration")
        return await self._runner(registration, **kwargs)

    def plan_to(self, target, *, completed=()):
        """Return a dependency-aware plan for a future operator target."""
        return self.graph.plan_to(target, completed=completed)

    def completed_steps(self, state_store, session_id: str) -> frozenset:
        """Read only positively verified steps from durable evidence."""
        from gpt_automation.workflow.graph import WorkflowStepId, StepStatus

        completed = set()
        for record in state_store.list_step_records(session_id):
            if (
                record.status != StepStatus.SUCCEEDED.value
                or not record.evidence_digest
                or state_store.get_step_evidence(
                    session_id,
                    record.step_id,
                    evidence_digest=record.evidence_digest,
                ) is None
            ):
                continue
            try:
                completed.add(WorkflowStepId(record.step_id))
            except ValueError:
                # Unknown future/legacy step IDs must not become proof for the
                # current graph.  They remain visible in the state database.
                continue
        return frozenset(completed)

    def plan_to_state(self, target, state_store, session_id: str):
        """Plan a target from the verified-step evidence of one session."""
        return self.plan_to(
            target,
            completed=self.completed_steps(state_store, session_id),
        )

    async def execute_step(self, step_id, handler, runtime_state):
        """Execute one migrated step with durable status and evidence rules.

        The compatibility runner remains responsible for unmigrated browser
        flow.  A migrated handler enters through this method so a successful
        result cannot be persisted without matching positive evidence, and an
        exception from a non-repeatable action is preserved as an ambiguous
        side effect rather than being retried automatically.
        """
        from gpt_automation.workflow.graph import (
            ActionEffectClass,
            Evidence,
            StepResult,
            StepStatus,
            WorkflowStepId,
            WorkflowGraphError,
        )
        from gpt_automation.workflow.steps import StepContext

        if not isinstance(step_id, WorkflowStepId):
            raise WorkflowGraphError(f"unknown workflow step: {step_id}")
        if getattr(handler, "step_id", None) != step_id:
            raise ValueError("handler step does not match requested step")
        if not runtime_state.enabled:
            raise RuntimeError("migrated workflow steps require durable workflow state")

        state_store = runtime_state.store
        assert state_store is not None
        plan = self.plan_to_state(step_id, state_store, runtime_state.session_id)
        missing_prerequisites = tuple(
            dependency for dependency in plan.missing if dependency != step_id
        )
        if missing_prerequisites:
            runtime_state.record_step_status(
                step_id,
                status=StepStatus.READY.value,
                outcome_code="PREREQUISITES_MISSING",
            )
            return StepResult(StepStatus.READY, "PREREQUISITES_MISSING")

        current_record = runtime_state.step_record(step_id)
        existing = runtime_state.step_evidence(step_id)
        if (
            current_record is not None
            and current_record.status == StepStatus.SUCCEEDED.value
            and current_record.evidence_digest
            and existing is not None
        ):
            evidence = Evidence(
                step_id=step_id,
                evidence_type=existing.evidence_type,
                details=existing.details,
                verifier_version=existing.verifier_version,
            )
            return StepResult.success(evidence)

        runtime_state.record_step_status(
            step_id,
            status=StepStatus.RUNNING.value,
            outcome_code="STARTED",
        )
        try:
            result = await handler.execute(StepContext(runtime_state, step_id))
        except BaseException as exc:
            effect_class = getattr(
                handler,
                "effect_class",
                ActionEffectClass.UNKNOWN,
            )
            if isinstance(effect_class, str):
                try:
                    effect_class = ActionEffectClass(effect_class)
                except ValueError:
                    effect_class = ActionEffectClass.UNKNOWN
            status = (
                StepStatus.UNKNOWN_SIDE_EFFECT
                if effect_class
                in {
                    ActionEffectClass.SUBMIT_ONCE,
                    ActionEffectClass.EXTERNAL_TRANSACTION,
                    ActionEffectClass.UNKNOWN,
                }
                else StepStatus.FAILED
            )
            runtime_state.record_step_status(
                step_id,
                status=status.value,
                outcome_code=type(exc).__name__,
                last_error=type(exc).__name__,
            )
            raise

        if result.status is StepStatus.SUCCEEDED:
            if (
                result.evidence is None
                or result.evidence.step_id is not step_id
                or not result.verified
            ):
                failed = StepResult(StepStatus.FAILED, "MISSING_EVIDENCE")
                runtime_state.record_step_status(
                    step_id,
                    status=failed.status.value,
                    outcome_code=failed.code,
                )
                return failed
            runtime_state.record_step_evidence(result.evidence)
            return result

        runtime_state.record_step_status(
            step_id,
            status=result.status.value,
            outcome_code=result.code,
        )
        return result


class RegistrationWorkflow:
    """Run exactly one canonical account business pipeline."""

    def __init__(
        self,
        runner: Optional[BusinessRunner] = None,
        interaction_policy: Optional[InteractionPolicy] = None,
        engine: Optional[WorkflowEngine] = None,
        finalization_coordinator: Optional["FinalizationCoordinator"] = None,
    ) -> None:
        if runner is None:
            from gpt_automation.workflow.account_flow import (
                run_registration_business_flow,
            )

            runner = run_registration_business_flow
        self._runner = runner
        self._engine = engine or WorkflowEngine(runner)
        self._interaction_policy = interaction_policy or AssistedPolicy()
        self._finalization_coordinator = finalization_coordinator

    def set_finalization_coordinator(
        self,
        coordinator: "FinalizationCoordinator",
    ) -> None:
        """Attach the durable success finalizer used by production adapters."""
        self._finalization_coordinator = coordinator

    async def run(
        self,
        registration: RegistrationInput,
        network_strategy: Optional["NetworkStrategy"] = None,
        previous_ip: Optional[str] = None,
        used_ips: Optional[Set[str]] = None,
        state_store: Optional["StateStore"] = None,
        session_id: str = "",
        card_resource_provider=None,
        run_scope: str = "full",
        require_login_gate: bool = False,
        checkout_goal: str = "complete",
        signup_method: Optional[str] = None,
    ) -> AccountRunResult:
        runner_state_args: Dict[str, Any] = {
            "run_scope": run_scope or "full",
            "require_login_gate": bool(require_login_gate),
            "checkout_goal": checkout_goal or "complete",
            "signup_method": signup_method,
        }
        if card_resource_provider is not None:
            runner_state_args["card_resource_provider"] = card_resource_provider
        if state_store is not None and session_id:
            session = state_store.get_session(session_id)
            if session is None:
                raise ValueError(f"unknown workflow session: {session_id}")
            state_store.update_session(
                session_id,
                stage=session.stage,
                status="RUNNING",
            )
            runner_state_args.update(
                {
                    "state_store": state_store,
                    "session_id": session_id,
                }
            )

        with use_interaction_policy(self._interaction_policy):
            result = await self._engine.run(
                registration,
                previous_ip=previous_ip,
                used_ips=used_ips,
                network_strategy=network_strategy,
                **runner_state_args,
            )

        finalization_record = None
        if (
            state_store is not None
            and session_id
            and result.status is AccountStatus.SUCCESS
            and self._finalization_coordinator is not None
        ):
            from gpt_automation.utils.redaction import account_fingerprint

            try:
                finalization_record = self._finalization_coordinator.finalize(
                    session_id,
                    account=registration.email,
                    result=result,
                    profile_id=account_fingerprint(registration.email)[:12],
                )
            except Exception as exc:
                # A successful browser flow is not a successful account run
                # until its durable result/resource finalization is known.
                result = AccountRunResult.for_code(
                    email=registration.email,
                    code="FINALIZATION_FAILED",
                    ip=result.ip,
                    reason=type(exc).__name__,
                )
            else:
                finalization_status = getattr(finalization_record, "status", None)
                status_value = getattr(finalization_status, "value", finalization_status)
                if status_value == "FAILED":
                    result = AccountRunResult.for_code(
                        email=registration.email,
                        code="FINALIZATION_FAILED",
                        ip=result.ip,
                        reason=(
                            getattr(finalization_record, "last_error", "")
                            or "FINALIZATION_FAILED"
                        ),
                    )
                elif status_value in {"PREPARING", "RESULT_PERSISTED"}:
                    result = AccountRunResult.for_code(
                        email=registration.email,
                        code="FINALIZATION_PENDING",
                        ip=result.ip,
                        reason=(
                            getattr(finalization_record, "last_error", "")
                            or "FINALIZATION_PENDING"
                        ),
                    )

        if state_store is not None and session_id:
            session = state_store.get_session(session_id)
            assert session is not None
            final_stage = (
                "COMPLETED"
                if result.status is AccountStatus.SUCCESS
                else (
                    "MFA_VERIFIED"
                    if result.status is AccountStatus.ACCOUNT_READY
                    else session.stage
                )
            )
            state_store.update_session(
                session_id,
                stage=final_stage,
                status=result.status.value,
                last_error=result.reason,
            )
            result = replace(
                result,
                stage=final_stage,
                resume_id=session_id,
            )

        return result


__all__ = ["RegistrationWorkflow", "WorkflowEngine"]
