"""Read-only history queries for the local dashboard.

All values come from the existing SQLite workflow database. The service only
returns fingerprints and digests; it never reconstructs secrets.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Dict, List, Optional

from .contracts import HistoryDetail, HistoryItem


class HistoryQueryError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class HistoryQueryService:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    def _connect(self) -> sqlite3.Connection:
        if not self.db_path.exists():
            raise HistoryQueryError(
                "HISTORY_UNAVAILABLE", "workflow database does not exist"
            )
        db = sqlite3.connect(self.db_path)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _session_columns(db: sqlite3.Connection) -> set[str]:
        return {
            str(row[1])
            for row in db.execute("PRAGMA table_info(workflow_sessions)")
        }

    def list_sessions(
        self,
        status: Optional[str] = None,
        session_id: Optional[str] = None,
        limit: int = 200,
    ) -> List[HistoryItem]:
        with self._connect() as db:
            columns = self._session_columns(db)
            has_scope = "run_scope" in columns and "account_id" in columns
            has_backend = "browser_backend" in columns
            select = (
                "session_id, email_fingerprint, stage, status, "
                "last_error, created_at, updated_at"
            )
            if has_scope:
                select += ", COALESCE(run_scope, 'full') AS run_scope, account_id"
            if has_backend:
                select += ", browser_backend"
            sql = f"SELECT {select} FROM workflow_sessions "
            clauses: List[str] = []
            params: List[str] = []
            if status:
                clauses.append("status = ?")
                params.append(status)
            if session_id:
                clauses.append("session_id LIKE ?")
                params.append(f"%{session_id}%")
            if clauses:
                sql += "WHERE " + " AND ".join(clauses) + " "
            sql += "ORDER BY created_at DESC LIMIT ?"
            params.append(str(int(limit)))
            rows = db.execute(sql, params).fetchall()
        items: List[HistoryItem] = []
        for row in rows:
            data = dict(row)
            items.append(
                HistoryItem(
                    session_id=str(data["session_id"]),
                    email_fingerprint=str(data["email_fingerprint"]),
                    stage=str(data["stage"]),
                    status=str(data["status"]),
                    last_error=str(data["last_error"] or ""),
                    created_at=str(data["created_at"]),
                    updated_at=str(data["updated_at"]),
                    run_scope=str(data.get("run_scope") or "full")
                    if has_scope
                    else "full",
                    account_id=(
                        str(data["account_id"])
                        if has_scope and data.get("account_id")
                        else None
                    ),
                    browser_backend=(
                        str(data["browser_backend"])
                        if has_backend and data.get("browser_backend")
                        else "cloak"
                    ),
                )
            )
        return items

    def get_detail(self, session_id: str) -> Optional[HistoryDetail]:
        with self._connect() as db:
            columns = self._session_columns(db)
            has_scope = "run_scope" in columns and "account_id" in columns
            has_backend = "browser_backend" in columns
            select = (
                "session_id, email_fingerprint, stage, status, "
                "last_error, created_at, updated_at"
            )
            if has_scope:
                select += ", COALESCE(run_scope, 'full') AS run_scope, account_id"
            if has_backend:
                select += ", browser_backend"
            session_row = db.execute(
                f"SELECT {select} FROM workflow_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if session_row is None:
                return None
            session_data = dict(session_row)
            session = HistoryItem(
                session_id=str(session_data["session_id"]),
                email_fingerprint=str(session_data["email_fingerprint"]),
                stage=str(session_data["stage"]),
                status=str(session_data["status"]),
                last_error=str(session_data["last_error"] or ""),
                created_at=str(session_data["created_at"]),
                updated_at=str(session_data["updated_at"]),
                run_scope=str(session_data.get("run_scope") or "full")
                if has_scope
                else "full",
                account_id=(
                    str(session_data["account_id"])
                    if has_scope and session_data.get("account_id")
                    else None
                ),
                browser_backend=(
                    str(session_data["browser_backend"])
                    if has_backend and session_data.get("browser_backend")
                    else "cloak"
                ),
            )
            step_rows = db.execute(
                "SELECT step_id, status, outcome_code, attempt_no, "
                "evidence_digest, last_error, updated_at "
                "FROM workflow_step_runs WHERE session_id = ? "
                "ORDER BY created_at",
                (session_id,),
            ).fetchall()
            evidence_rows = db.execute(
                "SELECT step_id, evidence_type, evidence_digest, "
                "verifier_version, created_at FROM workflow_evidence "
                "WHERE session_id = ? ORDER BY created_at",
                (session_id,),
            ).fetchall()
            action_rows = db.execute(
                "SELECT action, step_id, effect_class, state, details, "
                "updated_at FROM action_journal WHERE session_id = ? "
                "ORDER BY created_at",
                (session_id,),
            ).fetchall()
            event_rows = db.execute(
                "SELECT step_id, attempt_no, status, outcome_code, "
                "evidence_digest, last_error, created_at "
                "FROM workflow_step_events WHERE session_id = ? "
                "ORDER BY event_id",
                (session_id,),
            ).fetchall()
            lease_rows = db.execute(
                "SELECT resource_type, resource_fingerprint, state "
                "FROM resource_leases WHERE session_id = ? "
                "ORDER BY resource_type, updated_at",
                (session_id,),
            ).fetchall()
        return HistoryDetail(
            session=session,
            step_runs=[dict(r) for r in step_rows],
            evidence=[dict(r) for r in evidence_rows],
            actions=[dict(r) for r in action_rows],
            events=[dict(r) for r in event_rows],
            resource_fingerprints={
                str(r["resource_type"]): str(r["resource_fingerprint"])
                for r in lease_rows
            },
        )
