"""Dependency-aware options for operator-selected workflow targets."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Mapping

from gpt_automation.state import StateStore
from gpt_automation.workflow.engine import WorkflowEngine
from gpt_automation.workflow.graph import TargetPlan, WorkflowStepId


Verifier = Callable[[], Awaitable[bool]]


@dataclass(frozen=True)
class ManualTargetOption:
    """A target whose graph prerequisites are already positively verified."""

    target_id: str
    label: str
    plan: TargetPlan
    verifier: Verifier


class ManualTargetPlanner:
    """Expose only safe next targets; never turn a jump into a skip."""

    def __init__(self, engine: WorkflowEngine) -> None:
        self.engine = engine

    def build_options(
        self,
        state_store: StateStore,
        session_id: str,
        targets: Mapping[
            WorkflowStepId | str,
            tuple[str, Verifier],
        ],
    ) -> tuple[ManualTargetOption, ...]:
        options: list[ManualTargetOption] = []
        for raw_target, (label, verifier) in targets.items():
            target = (
                raw_target
                if isinstance(raw_target, WorkflowStepId)
                else WorkflowStepId(raw_target)
            )
            plan = self.engine.plan_to_state(target, state_store, session_id)
            # A target is selectable only when all of its prerequisites are
            # already durable proof.  The selected verifier must prove this
            # one target; it cannot silently perform a multi-step skip.
            if plan.missing != (target,):
                continue
            options.append(
                ManualTargetOption(
                    target_id=target.value,
                    label=label,
                    plan=plan,
                    verifier=verifier,
                )
            )
        return tuple(options)


__all__ = ["ManualTargetOption", "ManualTargetPlanner", "Verifier"]
