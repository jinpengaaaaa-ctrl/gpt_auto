"""Contracts shared by incrementally migrated workflow step handlers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol, TYPE_CHECKING

from gpt_automation.workflow.graph import (
    ActionEffectClass,
    StepResult,
    StepStatus,
    WorkflowStepId,
)

if TYPE_CHECKING:
    from gpt_automation.workflow.runtime_state import WorkflowRuntimeState


@dataclass(frozen=True)
class StepContext:
    """Minimal context a migrated handler may use to inspect durable state."""

    runtime_state: "WorkflowRuntimeState"
    step_id: WorkflowStepId


class StepHandler(Protocol):
    """Browser-independent protocol implemented by one business step."""

    step_id: WorkflowStepId
    effect_class: ActionEffectClass

    async def execute(self, context: StepContext) -> StepResult:
        ...


class PageObserver(Protocol):
    """Read-only browser observation contract for a migrated step."""

    async def observe(self):
        ...

    async def is_state(self, *expected):
        ...


@dataclass
class BrowserStepHandler:
    """Adapter for one existing page action and its postcondition verifier.

    The adapter deliberately does not know about page selectors.  Existing
    phase functions own browser mechanics; this class only makes their action,
    verification, evidence, and replay policy explicit to the workflow engine.
    """

    step_id: WorkflowStepId
    action: Callable[[], Awaitable[object]]
    verify: Callable[[], Awaitable[bool]]
    evidence_type: str
    evidence_details: tuple[tuple[str, str], ...]
    verifier_version: str
    effect_class: ActionEffectClass = ActionEffectClass.REPEATABLE

    async def execute(self, context: StepContext) -> StepResult:
        await self.action()
        if not await self.verify():
            return StepResult(
                status=StepStatus.WAITING_OPERATOR,
                code="POSTCONDITION_UNCONFIRMED",
            )
        from gpt_automation.workflow.graph import Evidence

        return StepResult.success(
            Evidence(
                step_id=self.step_id,
                evidence_type=self.evidence_type,
                details=self.evidence_details,
                verifier_version=self.verifier_version,
            )
        )


__all__ = ["BrowserStepHandler", "PageObserver", "StepContext", "StepHandler"]
