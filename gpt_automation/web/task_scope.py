"""Map console task types to the single RegistrationWorkflow run scopes."""

from __future__ import annotations

from typing import Iterable

from gpt_automation.workflow.models import RunScope

_TASK_TYPE_TO_SCOPE = {
    "provision": RunScope.ACCOUNT_PROVISION.value,
    "checkout": RunScope.CHECKOUT_FULFILLMENT.value,
    "full": RunScope.FULL.value,
    # Accept already-normalized values from advanced callers.
    RunScope.ACCOUNT_PROVISION.value: RunScope.ACCOUNT_PROVISION.value,
    RunScope.CHECKOUT_FULFILLMENT.value: RunScope.CHECKOUT_FULFILLMENT.value,
    RunScope.FULL.value: RunScope.FULL.value,
}

_SCOPE_TO_TASK_TYPE = {
    RunScope.ACCOUNT_PROVISION.value: "provision",
    RunScope.CHECKOUT_FULFILLMENT.value: "checkout",
    RunScope.FULL.value: "full",
}


class TaskScopeError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def resolve_run_scope(task_type: str) -> str:
    """Convert a console task type into a durable RunScope value."""
    key = (task_type or "").strip().lower()
    try:
        return _TASK_TYPE_TO_SCOPE[key]
    except KeyError as exc:
        raise TaskScopeError(
            "INVALID_TASK_TYPE",
            "task_type must be provision, checkout, or full",
        ) from exc


def task_type_for_scope(run_scope: str) -> str:
    """Convert a durable RunScope value back to the console task type."""
    try:
        return _SCOPE_TO_TASK_TYPE[run_scope]
    except KeyError as exc:
        raise TaskScopeError(
            "INVALID_RUN_SCOPE",
            f"unknown run_scope: {run_scope}",
        ) from exc


def parse_account_ids(raw: str | Iterable[str] | None) -> tuple[str, ...]:
    """Normalize multi-select account ids from form or JSON payloads."""
    if raw is None:
        return ()
    if isinstance(raw, str):
        parts = [part.strip() for part in raw.replace(";", ",").split(",")]
        return tuple(part for part in parts if part)
    return tuple(str(item).strip() for item in raw if str(item).strip())


def normalize_run_parameters(parameters: dict[str, str]) -> dict[str, str]:
    """Whitelist and normalize console run parameters for both engines."""
    allowed_keys = {
        "execution_mode",
        "mode",
        "interaction",
        "network",
        "target_count",
        "resume_session_id",
        "task_type",
        "account_ids",
        "run_scope",
        "checkout_goal",
        "browser_backend",
    }
    unknown = set(parameters) - allowed_keys
    if unknown:
        raise TaskScopeError(
            "UNKNOWN_PARAMETER",
            f"unknown parameter(s): {', '.join(sorted(unknown))}",
        )
    mode = parameters.get("mode", "new")
    interaction = parameters.get("interaction", "assisted")
    network = parameters.get("network", "manual")
    target = parameters.get("target_count", "1")
    execution_mode = parameters.get("execution_mode", "production")
    task_type = parameters.get("task_type", "provision")
    resume_session_id = parameters.get("resume_session_id", "").strip()
    account_ids = parse_account_ids(parameters.get("account_ids", ""))
    checkout_goal = parameters.get("checkout_goal", "complete")
    browser_backend = parameters.get("browser_backend", "cloak")

    if mode not in {"new", "resume"}:
        raise TaskScopeError("INVALID_PARAMETER", "mode must be new or resume")
    if interaction not in {"assisted", "unattended"}:
        raise TaskScopeError(
            "INVALID_PARAMETER",
            "interaction must be assisted or unattended",
        )
    if network not in {"manual", "auto"}:
        raise TaskScopeError("INVALID_PARAMETER", "network must be manual or auto")
    if execution_mode not in {"demo", "production"}:
        raise TaskScopeError(
            "INVALID_PARAMETER",
            "execution_mode must be demo or production",
        )
    if not target.isdigit() or not 1 <= int(target) <= 100:
        raise TaskScopeError("INVALID_PARAMETER", "target_count must be 1-100")
    if mode == "resume" and not resume_session_id:
        raise TaskScopeError(
            "INVALID_PARAMETER",
            "resume requires resume_session_id",
        )

    run_scope = resolve_run_scope(task_type)
    if run_scope == RunScope.CHECKOUT_FULFILLMENT.value and not account_ids and mode == "new":
        raise TaskScopeError(
            "INVALID_PARAMETER",
            "checkout requires one or more READY account_ids",
        )
    if run_scope != RunScope.CHECKOUT_FULFILLMENT.value:
        account_ids = ()
    if run_scope != RunScope.CHECKOUT_FULFILLMENT.value:
        checkout_goal = "complete"
    if checkout_goal not in {"complete", "pricing_page"}:
        raise TaskScopeError("INVALID_PARAMETER", "checkout_goal must be complete or pricing_page")
    if browser_backend not in {"cloak", "firefox_private"}:
        raise TaskScopeError(
            "INVALID_PARAMETER",
            "browser_backend must be cloak or firefox_private",
        )

    return {
        "execution_mode": execution_mode,
        "mode": mode,
        "interaction": interaction,
        "network": network,
        "target_count": target,
        "resume_session_id": resume_session_id,
        "task_type": task_type_for_scope(run_scope),
        "run_scope": run_scope,
        "account_ids": ",".join(account_ids),
        "checkout_goal": checkout_goal,
        "browser_backend": browser_backend,
    }
