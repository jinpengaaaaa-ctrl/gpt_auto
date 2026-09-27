"""Typed contracts shared by the local web dashboard.

Resource DTOs may include the values explicitly authorized for display in the
loopback-only resource console. They must never be copied into logs, audit
records, checkpoints, ordinary results or browser persistent storage.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class StepView:
    step_id: str
    title: str
    description: str
    status: str = "NOT_STARTED"


@dataclass(frozen=True)
class PhaseView:
    phase_id: str
    title: str
    description: str
    steps: List[StepView]
    status: str = "NOT_STARTED"


@dataclass(frozen=True)
class WorkflowDefinition:
    phases: List[PhaseView]


@dataclass(frozen=True)
class ParamField:
    key: str
    label: str
    type: str
    default: str
    required: bool = False
    options: List[str] = field(default_factory=list)
    placeholder: str = ""


@dataclass(frozen=True)
class ParamGroup:
    key: str
    label: str
    fields: List[ParamField]


@dataclass(frozen=True)
class RunParametersSchema:
    groups: List[ParamGroup]


@dataclass(frozen=True)
class ResourceView:
    resource_type: str
    resource_id: str
    display: str
    state: str
    session_id: Optional[str] = None
    updated_at: Optional[str] = None
    editable: bool = False
    detail: Dict[str, str] = field(default_factory=dict)
    state_actions: List[Dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class ResourceSummary:
    resource_type: str
    total: int
    available: int
    reserved: int
    consumed: int
    quarantined: int


@dataclass(frozen=True)
class RunEvent:
    at: str
    level: str
    message: str
    step_id: str = ""


@dataclass(frozen=True)
class OperatorRequestView:
    request_id: str
    session_id: str
    email: str
    step_id: str
    description: str
    verification_available: bool
    retry_allowed: bool
    advance_available: bool
    allowed_decisions: List[str] = field(default_factory=list)
    created_at: str = ""
    kind: str = "intervention"
    title: str = ""
    instruction: str = ""
    after_action: str = ""
    verification_hint: str = ""
    severity_level: str = "info"
    error_summary: str = ""
    browser_started: bool = False
    account_index: int = 0
    account_total: int = 0
    target_options: List[Dict[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class RunState:
    session_id: str
    mode: str
    parameters: Dict[str, str]
    phase_index: int
    step_index: int
    status: str
    current_step_id: str
    waiting_reason: str
    events: List[RunEvent]
    started_at: str
    updated_at: str
    operator_request: Optional[OperatorRequestView] = None
    allowed_decisions: List[str] = field(default_factory=list)
    account_index: int = 0
    current_action: str = ""
    browser_state: str = ""
    last_error: str = ""


# Kept as an import-compatible alias for the existing demo engine and tests.
DemoRunState = RunState


@dataclass(frozen=True)
class HistoryItem:
    session_id: str
    email_fingerprint: str
    stage: str
    status: str
    last_error: str
    created_at: str
    updated_at: str
    run_scope: str = "full"
    account_id: Optional[str] = None
    browser_backend: str = "cloak"


@dataclass(frozen=True)
class HistoryDetail:
    session: HistoryItem
    step_runs: List[Dict[str, str]]
    evidence: List[Dict[str, str]]
    actions: List[Dict[str, str]]
    events: List[Dict[str, str]]
    resource_fingerprints: Dict[str, str]
