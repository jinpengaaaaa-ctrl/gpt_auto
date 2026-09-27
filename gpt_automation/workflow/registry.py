"""Runtime registry for the independently executable workflow steps."""

from __future__ import annotations

from collections.abc import Iterable

from gpt_automation.workflow.graph import WorkflowStepId
from gpt_automation.workflow.steps import StepHandler


class StepRegistry:
    """Resolve one handler for each graph step.

    Registration is deliberately explicit.  The registry does not construct
    handlers or infer a step from a callback, which keeps orchestration out of
    phase adapters and makes missing migrations visible at startup.
    """

    def __init__(self, handlers: Iterable[StepHandler] = ()) -> None:
        self._handlers: dict[WorkflowStepId, StepHandler] = {}
        for handler in handlers:
            self.register(handler)

    @property
    def step_ids(self) -> tuple[WorkflowStepId, ...]:
        return tuple(self._handlers)

    def register(
        self,
        handler: StepHandler,
        *,
        declared_step: WorkflowStepId | None = None,
    ) -> None:
        step_id = getattr(handler, "step_id", None)
        if not isinstance(step_id, WorkflowStepId):
            raise TypeError("step handler must declare a WorkflowStepId step_id")
        if declared_step is not None and declared_step is not step_id:
            raise ValueError(
                "declared step_id does not match handler step_id"
            )
        if step_id in self._handlers:
            raise ValueError(f"duplicate step handler: {step_id.value}")
        self._handlers[step_id] = handler

    def get(self, step_id: WorkflowStepId) -> StepHandler:
        try:
            return self._handlers[step_id]
        except KeyError as exc:
            raise KeyError(f"no handler registered for {step_id.value}") from exc

    def contains(self, step_id: WorkflowStepId) -> bool:
        return step_id in self._handlers


__all__ = ["StepRegistry"]
