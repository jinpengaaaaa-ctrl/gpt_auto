"""Operator-controlled resource state corrections.

This service deliberately does not implement a card reuse policy.  A card is
marked consumed by the workflow, and an operator may later release it again
when they have confirmed it can be reused.  The service only protects active
leases and unknown payment side effects, then records an append-only local
audit entry without storing secrets.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from gpt_automation.state import ResourceState
from gpt_automation.state.execution_lock import SessionExecutionLock

from .resource_service import ResourceAdminError, ResourceAdminService, _fingerprint


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _redact_reason(reason: str) -> str:
    return re.sub(r"\d{12,}", "[REDACTED]", reason.strip())[:500]


class ResourceStateCorrectionError(ResourceAdminError):
    pass


class ResourceStateCorrectionService:
    """Validate and apply one operator state correction at a time."""

    _TARGET_STATES = {
        "available": ResourceState.RELEASED,
        "used": ResourceState.CONSUMED,
        "consumed": ResourceState.CONSUMED,
        "quarantined": ResourceState.QUARANTINED,
    }

    def __init__(
        self,
        resource_service: ResourceAdminService,
        *,
        db_path: str | Path,
        lock_dir: str | Path,
        audit_path: str | Path,
    ) -> None:
        self.resource_service = resource_service
        self.db_path = Path(db_path)
        self.lock_dir = Path(lock_dir)
        self.audit_path = Path(audit_path)
        self._lock = threading.RLock()

    def allowed_actions(
        self,
        resource_type: str,
        resource_id: str,
    ) -> List[Dict[str, Any]]:
        view = self._view(resource_type, resource_id)
        return self.allowed_actions_for_view(view)

    def allowed_actions_for_view(self, view) -> List[Dict[str, Any]]:
        """Return correction actions for an already-loaded resource view.

        Resource list endpoints already load the complete resource collection.
        Re-listing that collection for every row would make the page quadratic
        and can look like an infinite loading state with a large input file.
        """
        active, active_reason = self._active_session(view.session_id)
        unknown_payment = self._unknown_payment(view.session_id)
        actions: List[Dict[str, Any]] = []
        for action, label, target in (
            ("release", "纠正为可用", "available"),
            ("consume", "标记为已使用", "used"),
            ("quarantine", "转入隔离", "quarantined"),
        ):
            enabled = view.state != target
            reason = ""
            if active:
                enabled = False
                reason = active_reason
            elif unknown_payment and target == "available":
                enabled = False
                reason = "存在未知支付结果，先核对真实扣款状态。"
            elif view.state == target:
                enabled = False
                reason = "资源已经是该状态。"
            actions.append(
                {
                    "action": action,
                    "label": label,
                    "target_state": target,
                    "enabled": enabled,
                    "reason": reason,
                    "requires_reason": False,
                    "requires_confirmation": True,
                }
            )
        return actions

    def correct_state(
        self,
        resource_type: str,
        resource_id: str,
        *,
        target: str,
        reason: str,
        confirmed: bool,
    ):
        if target not in self._TARGET_STATES:
            raise ResourceStateCorrectionError(
                "INVALID_RESOURCE_STATE",
                "target must be available, used or quarantined",
            )
        if not confirmed:
            raise ResourceStateCorrectionError(
                "CORRECTION_CONFIRMATION_REQUIRED",
                "state correction requires explicit confirmation",
            )
        with self._lock:
            view = self._view(resource_type, resource_id)
            actions = self.allowed_actions_for_view(view)
            selected = next(
                (item for item in actions if item["target_state"] == target),
                None,
            )
            if selected is None or not selected["enabled"]:
                raise ResourceStateCorrectionError(
                    "RESOURCE_STATE_NOT_EDITABLE",
                    selected["reason"] if selected else "state correction is not allowed",
                )
            fingerprint = self._fingerprint_for_view(view)
            desired = self._TARGET_STATES[target]
            previous = self._transition(
                resource_type,
                fingerprint,
                desired,
                view.state,
            )
            self._append_audit(
                resource_type=resource_type,
                fingerprint=fingerprint,
                session_id=view.session_id,
                previous_state=previous,
                new_state=desired.value,
                reason=_redact_reason(reason),
            )
            return self._view(resource_type, resource_id)

    def list_audit(self, *, limit: int = 100) -> List[Dict[str, str]]:
        if not self.audit_path.exists():
            return []
        entries: List[Dict[str, str]] = []
        try:
            lines = self.audit_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        for line in lines[-max(1, min(limit, 500)) :]:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                entries.append({str(k): str(v) for k, v in value.items()})
        return list(reversed(entries))

    def _view(self, resource_type: str, resource_id: str):
        try:
            views = self.resource_service.list_resources(resource_type)
        except ResourceAdminError:
            raise
        for view in views:
            if view.resource_id == resource_id:
                return view
        raise ResourceStateCorrectionError(
            "RESOURCE_NOT_FOUND",
            "resource not found",
        )

    @staticmethod
    def _fingerprint_for_view(view) -> str:
        if view.resource_type == "card":
            return view.resource_id
        return _fingerprint(view.resource_type, view.resource_id)

    def _active_session(self, session_id: Optional[str]) -> tuple[bool, str]:
        if not session_id:
            return False, ""
        lock_path = self.lock_dir / f"{session_id}.lock"
        if SessionExecutionLock.is_locked(lock_path):
            return True, "关联执行仍在运行，先安全停止或等待执行结束。"
        try:
            with sqlite3.connect(self.db_path) as db:
                row = db.execute(
                    "SELECT status FROM workflow_sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
        except sqlite3.Error:
            return True, "无法确认关联执行状态，先在执行履历中核对。"
        if row is None:
            return True, "关联执行记录不存在，先核对后再修改占用资源。"
        if row[0] in {"PENDING", "RUNNING"}:
            return True, "关联执行仍在运行，先安全停止或等待执行结束。"
        return False, ""

    def _unknown_payment(self, session_id: Optional[str]) -> bool:
        if not session_id:
            return False
        try:
            with sqlite3.connect(self.db_path) as db:
                row = db.execute(
                    "SELECT state FROM action_journal "
                    "WHERE session_id = ? AND action = 'PAYMENT_SUBMIT'",
                    (session_id,),
                ).fetchone()
        except sqlite3.Error:
            return False
        return row is not None and row[0] in {"STARTED", "UNKNOWN"}

    def _transition(
        self,
        resource_type: str,
        fingerprint: str,
        desired: ResourceState,
        expected_view_state: str,
    ) -> str:
        timestamp = _now()
        with sqlite3.connect(self.db_path, timeout=5.0) as db:
            db.execute("PRAGMA busy_timeout = 5000")
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT state FROM resource_leases "
                "WHERE resource_type = ? AND resource_fingerprint = ?",
                (resource_type, fingerprint),
            ).fetchone()
            current_raw = None if row is None else str(row[0])
            current_view_state = {
                None: "available",
                "RELEASED": "available",
                "RESERVED": "reserved",
                "CONSUMED": "consumed",
                "QUARANTINED": "quarantined",
            }.get(current_raw, "available")
            if current_view_state != expected_view_state:
                raise ResourceStateCorrectionError(
                    "RESOURCE_STATE_CHANGED",
                    "resource state changed; refresh and try again",
                )
            if row is None:
                if desired is ResourceState.RELEASED:
                    db.commit()
                    return "AVAILABLE"
                db.execute(
                    "INSERT INTO resource_leases ("
                    "resource_type, resource_fingerprint, session_id, state,"
                    "created_at, updated_at) VALUES (?, ?, NULL, ?, ?, ?)",
                    (resource_type, fingerprint, desired.value, timestamp, timestamp),
                )
            else:
                db.execute(
                    "UPDATE resource_leases SET state = ?, updated_at = ? "
                    "WHERE resource_type = ? AND resource_fingerprint = ?",
                    (desired.value, timestamp, resource_type, fingerprint),
                )
            db.commit()
            return current_raw or "AVAILABLE"

    def _append_audit(
        self,
        *,
        resource_type: str,
        fingerprint: str,
        session_id: Optional[str],
        previous_state: str,
        new_state: str,
        reason: str,
    ) -> None:
        record = {
            "at": _now(),
            "operator": "local_operator",
            "resource_type": resource_type,
            "resource_fingerprint": fingerprint,
            "session_id": session_id or "",
            "previous_state": previous_state,
            "new_state": new_state,
            "reason": reason,
        }
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


__all__ = [
    "ResourceStateCorrectionError",
    "ResourceStateCorrectionService",
]
