"""Pure workflow graph primitives for the incremental state-machine migration.

This module deliberately has no browser, persistence, or policy dependencies.
It answers only two questions:

* which business steps exist and what do they depend on; and
* which prerequisites are still missing for a requested target.

The existing ``RegistrationWorkflow`` remains the runtime entrypoint while
these primitives are introduced.  Later migration slices can replace legacy
composite steps one at a time without changing this contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from typing import Iterable, Mapping, Sequence


class WorkflowGraphError(ValueError):
    """Raised when a workflow graph is structurally invalid."""


class WorkflowStepId(str, Enum):
    """Stable business-step identifiers used by the new workflow engine."""

    NETWORK_READY = "NETWORK_READY"
    AUTH_ENTRY_READY = "AUTH_ENTRY_READY"
    ACCOUNT_ROUTE_RESOLVED = "ACCOUNT_ROUTE_RESOLVED"
    LOGIN_PASSWORD_VERIFIED = "LOGIN_PASSWORD_VERIFIED"
    LOGIN_EMAIL_CODE_VERIFIED = "LOGIN_EMAIL_CODE_VERIFIED"
    SIGNUP_PASSWORD_VERIFIED = "SIGNUP_PASSWORD_VERIFIED"
    SIGNUP_EMAIL_CODE_VERIFIED = "SIGNUP_EMAIL_CODE_VERIFIED"
    PROFILE_VERIFIED = "PROFILE_VERIFIED"
    ONBOARDING_VERIFIED = "ONBOARDING_VERIFIED"
    ACCOUNT_AUTHENTICATED = "ACCOUNT_AUTHENTICATED"
    PASSWORD_VERIFIED = "PASSWORD_VERIFIED"
    MFA_VERIFIED = "MFA_VERIFIED"
    INVENTORY_LOGIN_VERIFIED = "INVENTORY_LOGIN_VERIFIED"
    CHECKOUT_AUTHENTICATED = "CHECKOUT_AUTHENTICATED"
    ACCOUNT_READY_EXPORTED = "ACCOUNT_READY_EXPORTED"
    PRICING_PAGE_VERIFIED = "PRICING_PAGE_VERIFIED"
    OFFER_ELIGIBILITY_VERIFIED = "OFFER_ELIGIBILITY_VERIFIED"
    OFFER_DECISION_VERIFIED = "OFFER_DECISION_VERIFIED"
    CHECKOUT_READY = "CHECKOUT_READY"
    PAYMENT_VERIFIED = "PAYMENT_VERIFIED"
    CANCELLATION_VERIFIED = "CANCELLATION_VERIFIED"
    RESULT_PERSISTED = "RESULT_PERSISTED"


class StepStatus(str, Enum):
    """Business outcome of one graph step."""

    NOT_STARTED = "NOT_STARTED"
    READY = "READY"
    RUNNING = "RUNNING"
    WAITING_OPERATOR = "WAITING_OPERATOR"
    SUCCEEDED = "SUCCEEDED"
    BUSINESS_REJECTED = "BUSINESS_REJECTED"
    FAILED = "FAILED"
    UNKNOWN_SIDE_EFFECT = "UNKNOWN_SIDE_EFFECT"


class ActionEffectClass(str, Enum):
    """Replay policy for one externally visible action."""

    READ_ONLY = "READ_ONLY"
    REPEATABLE = "REPEATABLE"
    SUBMIT_ONCE = "SUBMIT_ONCE"
    EXTERNAL_TRANSACTION = "EXTERNAL_TRANSACTION"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class StepDefinition:
    """One graph node and its required predecessor nodes."""

    step_id: WorkflowStepId
    dependencies: tuple[WorkflowStepId, ...] = ()


@dataclass(frozen=True)
class Evidence:
    """Redacted, positive evidence for a verified business step.

    Details are intentionally represented as a tuple rather than an arbitrary
    dictionary so callers must make the persisted evidence shape explicit.
    Sensitive keys are rejected before an evidence record can be created.
    """

    step_id: WorkflowStepId
    evidence_type: str
    details: tuple[tuple[str, str], ...]
    verifier_version: str

    def __post_init__(self) -> None:
        if not self.evidence_type.strip():
            raise ValueError("evidence type must not be empty")
        if not self.verifier_version.strip():
            raise ValueError("verifier version must not be empty")

        seen: set[str] = set()
        for key, value in self.details:
            normalized = key.strip().lower()
            if not normalized:
                raise ValueError("evidence detail keys must not be empty")
            if normalized in seen:
                raise ValueError(f"duplicate evidence detail key: {key}")
            seen.add(normalized)
            if _is_sensitive_key(normalized):
                raise ValueError(
                    f"sensitive evidence detail is not allowed: {key}"
                )
            if not isinstance(value, str):
                raise TypeError("evidence detail values must be strings")
            if _is_sensitive_value(value):
                raise ValueError(
                    f"sensitive evidence detail is not allowed: {key}"
                )


@dataclass(frozen=True)
class StepResult:
    """Typed result returned by a future Step Handler."""

    status: StepStatus
    code: str
    evidence: Evidence | None = None

    @property
    def verified(self) -> bool:
        return self.status is StepStatus.SUCCEEDED and self.evidence is not None

    @classmethod
    def success(cls, evidence: Evidence) -> "StepResult":
        return cls(StepStatus.SUCCEEDED, "VERIFIED", evidence)


@dataclass(frozen=True)
class TargetPlan:
    """Dependency-aware plan for an operator-requested target."""

    target: WorkflowStepId
    missing: tuple[WorkflowStepId, ...]
    satisfied: tuple[WorkflowStepId, ...]

    @property
    def ready(self) -> bool:
        return not self.missing


class WorkflowGraph:
    """Validated dependency graph for business steps."""

    def __init__(self, definitions: Iterable[StepDefinition]) -> None:
        definition_list = tuple(definitions)
        self._definitions: dict[WorkflowStepId, StepDefinition] = {}
        for definition in definition_list:
            if definition.step_id in self._definitions:
                raise WorkflowGraphError(
                    f"duplicate step definition: {definition.step_id.value}"
                )
            self._definitions[definition.step_id] = definition

        for definition in definition_list:
            for dependency in definition.dependencies:
                if dependency not in self._definitions:
                    raise WorkflowGraphError(
                        "unknown dependency "
                        f"{dependency.value} for {definition.step_id.value}"
                    )

        self._topological_order = self._build_topological_order()

    @property
    def steps(self) -> tuple[WorkflowStepId, ...]:
        return self._topological_order

    def definition(self, step_id: WorkflowStepId) -> StepDefinition:
        try:
            return self._definitions[step_id]
        except KeyError as exc:
            raise WorkflowGraphError(f"unknown workflow step: {step_id}") from exc

    def plan_to(
        self,
        target: WorkflowStepId,
        *,
        completed: Iterable[WorkflowStepId] = (),
    ) -> TargetPlan:
        """Return only the unmet dependency closure for ``target``.

        A target is never considered complete merely because an operator asked
        for it.  The caller must verify each item in ``missing`` and persist
        evidence before the graph can be advanced.
        """

        self.definition(target)
        completed_set = set(completed)
        unknown_completed = completed_set.difference(self._definitions)
        if unknown_completed:
            names = ", ".join(sorted(step.value for step in unknown_completed))
            raise WorkflowGraphError(f"unknown completed step(s): {names}")

        required: set[WorkflowStepId] = set()

        def visit(step_id: WorkflowStepId) -> None:
            if step_id in completed_set or step_id in required:
                return
            required.add(step_id)
            for dependency in self.definition(step_id).dependencies:
                visit(dependency)

        visit(target)
        missing = tuple(step for step in self._topological_order if step in required)
        satisfied = tuple(
            step for step in self._topological_order if step in completed_set
        )
        return TargetPlan(target=target, missing=missing, satisfied=satisfied)

    def _build_topological_order(self) -> tuple[WorkflowStepId, ...]:
        visiting: set[WorkflowStepId] = set()
        visited: set[WorkflowStepId] = set()
        ordered: list[WorkflowStepId] = []

        def visit(step_id: WorkflowStepId) -> None:
            if step_id in visiting:
                raise WorkflowGraphError(
                    f"dependency cycle detected at {step_id.value}"
                )
            if step_id in visited:
                return
            visiting.add(step_id)
            for dependency in self.definition(step_id).dependencies:
                visit(dependency)
            visiting.remove(step_id)
            visited.add(step_id)
            ordered.append(step_id)

        for step_id in self._definitions:
            visit(step_id)
        return tuple(ordered)


def _is_sensitive_key(key: str) -> bool:
    sensitive_markers = (
        "password",
        "secret",
        "token",
        "code",
        "card",
        "cvc",
        "cvv",
    )
    return any(marker in key for marker in sensitive_markers)


def _is_sensitive_value(value: str) -> bool:
    """Reject common secret shapes even when a caller uses a safe-looking key."""
    normalized = value.strip().lower()
    if any(
        marker in normalized
        for marker in ("password=", "secret=", "token=", "cvc=", "cvv=")
    ):
        return True
    if normalized.startswith("otpauth://"):
        return True
    if re.fullmatch(r"[0-9]{13,19}", normalized):
        return True
    return bool(re.fullmatch(r"[a-z2-7]{16,}", normalized))


REGISTRATION_GRAPH = WorkflowGraph(
    (
        StepDefinition(WorkflowStepId.NETWORK_READY),
        StepDefinition(
            WorkflowStepId.AUTH_ENTRY_READY,
            (WorkflowStepId.NETWORK_READY,),
        ),
        StepDefinition(
            WorkflowStepId.ACCOUNT_ROUTE_RESOLVED,
            (WorkflowStepId.AUTH_ENTRY_READY,),
        ),
        StepDefinition(
            WorkflowStepId.LOGIN_PASSWORD_VERIFIED,
            (WorkflowStepId.ACCOUNT_ROUTE_RESOLVED,),
        ),
        StepDefinition(
            WorkflowStepId.LOGIN_EMAIL_CODE_VERIFIED,
            (WorkflowStepId.LOGIN_PASSWORD_VERIFIED,),
        ),
        StepDefinition(
            WorkflowStepId.SIGNUP_PASSWORD_VERIFIED,
            (WorkflowStepId.ACCOUNT_ROUTE_RESOLVED,),
        ),
        StepDefinition(
            WorkflowStepId.SIGNUP_EMAIL_CODE_VERIFIED,
            (WorkflowStepId.ACCOUNT_ROUTE_RESOLVED,),
        ),
        StepDefinition(
            WorkflowStepId.PROFILE_VERIFIED,
            (WorkflowStepId.SIGNUP_PASSWORD_VERIFIED,),
        ),
        StepDefinition(
            WorkflowStepId.ONBOARDING_VERIFIED,
            (WorkflowStepId.PROFILE_VERIFIED,),
        ),
        StepDefinition(
            WorkflowStepId.ACCOUNT_AUTHENTICATED,
            (WorkflowStepId.ACCOUNT_ROUTE_RESOLVED,),
        ),
        StepDefinition(
            WorkflowStepId.PASSWORD_VERIFIED,
            (WorkflowStepId.ACCOUNT_AUTHENTICATED,),
        ),
        StepDefinition(
            WorkflowStepId.MFA_VERIFIED,
            (WorkflowStepId.ACCOUNT_AUTHENTICATED,),
        ),
        StepDefinition(
            WorkflowStepId.INVENTORY_LOGIN_VERIFIED,
            (WorkflowStepId.NETWORK_READY,),
        ),
        StepDefinition(
            WorkflowStepId.CHECKOUT_AUTHENTICATED,
            (WorkflowStepId.INVENTORY_LOGIN_VERIFIED,),
        ),
        StepDefinition(
            WorkflowStepId.ACCOUNT_READY_EXPORTED,
            (WorkflowStepId.OFFER_ELIGIBILITY_VERIFIED,),
        ),
        StepDefinition(
            WorkflowStepId.OFFER_ELIGIBILITY_VERIFIED,
            (WorkflowStepId.MFA_VERIFIED,),
        ),
        StepDefinition(
            WorkflowStepId.OFFER_DECISION_VERIFIED,
            (WorkflowStepId.PRICING_PAGE_VERIFIED,),
        ),
        StepDefinition(
            WorkflowStepId.PRICING_PAGE_VERIFIED,
            (WorkflowStepId.CHECKOUT_AUTHENTICATED,),
        ),
        StepDefinition(
            WorkflowStepId.CHECKOUT_READY,
            (WorkflowStepId.OFFER_DECISION_VERIFIED,),
        ),
        StepDefinition(
            WorkflowStepId.PAYMENT_VERIFIED,
            (WorkflowStepId.CHECKOUT_READY,),
        ),
        StepDefinition(
            WorkflowStepId.CANCELLATION_VERIFIED,
            (WorkflowStepId.PAYMENT_VERIFIED,),
        ),
        StepDefinition(
            WorkflowStepId.RESULT_PERSISTED,
            (WorkflowStepId.CANCELLATION_VERIFIED,),
        ),
    )
)


__all__ = [
    "Evidence",
    "ActionEffectClass",
    "REGISTRATION_GRAPH",
    "StepDefinition",
    "StepResult",
    "StepStatus",
    "TargetPlan",
    "WorkflowGraph",
    "WorkflowGraphError",
    "WorkflowStepId",
]
