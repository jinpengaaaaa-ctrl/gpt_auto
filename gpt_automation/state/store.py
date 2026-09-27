"""SQLite-backed state for safe local workflow recovery.

Only fingerprints and workflow metadata are persisted.  Email passwords,
tokens, card numbers, and other input secrets must never be written here.
"""

from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Dict, Iterator, Optional, Tuple


SCHEMA_VERSION = 7


class ResourceState(str, Enum):
    RESERVED = "RESERVED"
    CONSUMED = "CONSUMED"
    RELEASED = "RELEASED"
    QUARANTINED = "QUARANTINED"


class ActionState(str, Enum):
    STARTED = "STARTED"
    VERIFIED = "VERIFIED"
    UNKNOWN = "UNKNOWN"
    REJECTED = "REJECTED"


class FinalizationStatus(str, Enum):
    PREPARING = "PREPARING"
    RESULT_PERSISTED = "RESULT_PERSISTED"
    RESOURCES_CONSUMED = "RESOURCES_CONSUMED"
    SESSION_COMPLETED = "SESSION_COMPLETED"
    CLEANUP_PENDING = "CLEANUP_PENDING"
    FINALIZED = "FINALIZED"
    FAILED = "FAILED"


class ResourceConflictError(RuntimeError):
    """Raised when another session already owns an input resource."""


class UnsafeActionReplayError(RuntimeError):
    """Raised when a non-idempotent action has already been attempted."""


class StateTransitionError(RuntimeError):
    """Raised when persisted state does not match the required transition."""


@dataclass(frozen=True)
class SessionRecord:
    session_id: str
    email_fingerprint: str
    stage: str
    status: str
    last_error: str
    browser_backend: str = "cloak"


@dataclass(frozen=True)
class SessionOverview:
    session_id: str
    email_fingerprint: str
    stage: str
    status: str
    last_error: str
    created_at: str
    updated_at: str
    browser_backend: str = "cloak"


@dataclass(frozen=True)
class ActionRecord:
    session_id: str
    action: str
    state: ActionState
    details: str
    step_id: str = ""
    effect_class: str = "UNKNOWN"


@dataclass(frozen=True)
class StepRecord:
    session_id: str
    step_id: str
    status: str
    outcome_code: str
    attempt_no: int
    evidence_digest: str
    last_error: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class EvidenceRecord:
    session_id: str
    step_id: str
    evidence_type: str
    evidence_digest: str
    details: Tuple[Tuple[str, str], ...]
    verifier_version: str
    created_at: str


@dataclass(frozen=True)
class StepEventRecord:
    event_id: int
    session_id: str
    step_id: str
    attempt_no: int
    status: str
    outcome_code: str
    evidence_digest: str
    last_error: str
    created_at: str


@dataclass(frozen=True)
class FinalizationRecord:
    session_id: str
    status: FinalizationStatus
    result_token: str
    last_error: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class RegisteredAccountRecord:
    account_id: str
    email_fingerprint: str
    source: str
    status: str
    provision_session_id: Optional[str]
    last_checkout_session_id: Optional[str]
    credential_ref: str
    has_chatgpt_password: bool
    has_totp_secret: bool
    has_email_password: bool
    has_oauth: bool
    ready_verified_at: Optional[str]
    last_login_verified_at: Optional[str]
    notes: str
    created_at: str
    updated_at: str
    offer_status: str = "UNKNOWN"
    offer_marked_at: Optional[str] = None
    offer_marked_reason: str = ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalize_resource(resource_type: str, value: str) -> str:
    value = value.strip()
    if resource_type == "email":
        return value.lower()
    if resource_type == "card":
        return "".join(character for character in value if character.isdigit())
    return value


def _fingerprint(resource_type: str, value: str) -> str:
    normalized = _normalize_resource(resource_type, value)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class StateStore:
    """Small process-safe state store using one transaction per mutation."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Yield a connection and always release its file handle."""
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            current_version = int(
                connection.execute("PRAGMA user_version").fetchone()[0]
            )
            if current_version > SCHEMA_VERSION:
                raise StateTransitionError(
                    "workflow database uses a newer schema version"
                )
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS workflow_sessions (
                    session_id TEXT PRIMARY KEY,
                    email_fingerprint TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    status TEXT NOT NULL,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS action_journal (
                    session_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    step_id TEXT NOT NULL DEFAULT '',
                    effect_class TEXT NOT NULL DEFAULT 'UNKNOWN',
                    state TEXT NOT NULL CHECK (
                        state IN ('STARTED', 'VERIFIED', 'UNKNOWN', 'REJECTED')
                    ),
                    details TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (session_id, action),
                    FOREIGN KEY (session_id)
                        REFERENCES workflow_sessions(session_id)
                );

                CREATE TABLE IF NOT EXISTS resource_leases (
                    resource_type TEXT NOT NULL,
                    resource_fingerprint TEXT NOT NULL,
                    session_id TEXT,
                    state TEXT NOT NULL CHECK (
                        state IN ('RESERVED', 'CONSUMED', 'RELEASED', 'QUARANTINED')
                    ),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (resource_type, resource_fingerprint),
                    FOREIGN KEY (session_id)
                        REFERENCES workflow_sessions(session_id)
                );

                CREATE TABLE IF NOT EXISTS legacy_imports (
                    source_key TEXT PRIMARY KEY,
                    imported_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS workflow_step_runs (
                    session_id TEXT NOT NULL,
                    step_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    outcome_code TEXT NOT NULL DEFAULT '',
                    attempt_no INTEGER NOT NULL DEFAULT 1,
                    evidence_digest TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (session_id, step_id),
                    FOREIGN KEY (session_id)
                        REFERENCES workflow_sessions(session_id)
                );

                CREATE TABLE IF NOT EXISTS workflow_evidence (
                    session_id TEXT NOT NULL,
                    step_id TEXT NOT NULL,
                    evidence_type TEXT NOT NULL,
                    evidence_digest TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    verifier_version TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (session_id, step_id, evidence_digest),
                    FOREIGN KEY (session_id)
                        REFERENCES workflow_sessions(session_id)
                );

                CREATE TABLE IF NOT EXISTS workflow_step_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    step_id TEXT NOT NULL,
                    attempt_no INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    outcome_code TEXT NOT NULL DEFAULT '',
                    evidence_digest TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (session_id)
                        REFERENCES workflow_sessions(session_id)
                );

                CREATE TABLE IF NOT EXISTS workflow_finalizations (
                    session_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL CHECK (
                        status IN (
                            'PREPARING', 'RESULT_PERSISTED',
                            'RESOURCES_CONSUMED', 'SESSION_COMPLETED',
                            'CLEANUP_PENDING', 'FINALIZED', 'FAILED'
                        )
                    ),
                    result_token TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (session_id)
                        REFERENCES workflow_sessions(session_id)
                );

                CREATE TABLE IF NOT EXISTS registered_accounts (
                    account_id TEXT PRIMARY KEY,
                    email_fingerprint TEXT NOT NULL UNIQUE,
                    source TEXT NOT NULL CHECK (
                        source IN ('provisioned', 'imported', 'manual')
                    ),
                    status TEXT NOT NULL CHECK (
                        status IN (
                            'DRAFT', 'READY', 'RESERVED', 'CHECKOUT_RUNNING',
                            'CHECKOUT_DONE', 'NEEDS_OPERATOR', 'QUARANTINED',
                            'INVALID'
                        )
                    ),
                    provision_session_id TEXT,
                    last_checkout_session_id TEXT,
                    credential_ref TEXT NOT NULL,
                    has_chatgpt_password INTEGER NOT NULL DEFAULT 0,
                    has_totp_secret INTEGER NOT NULL DEFAULT 0,
                    has_email_password INTEGER NOT NULL DEFAULT 0,
                    has_oauth INTEGER NOT NULL DEFAULT 0,
                    ready_verified_at TEXT,
                    last_login_verified_at TEXT,
                    notes TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (provision_session_id)
                        REFERENCES workflow_sessions(session_id),
                    FOREIGN KEY (last_checkout_session_id)
                        REFERENCES workflow_sessions(session_id)
                );

                CREATE TABLE IF NOT EXISTS registered_account_imports (
                    import_id TEXT PRIMARY KEY,
                    source_name TEXT NOT NULL,
                    total_rows INTEGER NOT NULL,
                    accepted_rows INTEGER NOT NULL,
                    rejected_rows INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    created_by TEXT NOT NULL DEFAULT 'local-operator'
                );

                CREATE TABLE IF NOT EXISTS registered_account_import_rows (
                    import_id TEXT NOT NULL,
                    row_no INTEGER NOT NULL,
                    email_fingerprint TEXT,
                    result TEXT NOT NULL CHECK (
                        result IN ('ACCEPTED', 'REJECTED', 'UPDATED')
                    ),
                    reason TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (import_id, row_no),
                    FOREIGN KEY (import_id)
                        REFERENCES registered_account_imports(import_id)
                );

                CREATE INDEX IF NOT EXISTS idx_sessions_status
                    ON workflow_sessions(status);
                CREATE INDEX IF NOT EXISTS idx_leases_session
                    ON resource_leases(session_id);
                CREATE INDEX IF NOT EXISTS idx_step_runs_session
                    ON workflow_step_runs(session_id);
                CREATE INDEX IF NOT EXISTS idx_evidence_session
                    ON workflow_evidence(session_id);
                CREATE INDEX IF NOT EXISTS idx_step_events_session
                    ON workflow_step_events(session_id, step_id, event_id);
                CREATE INDEX IF NOT EXISTS idx_finalizations_status
                    ON workflow_finalizations(status);
                CREATE INDEX IF NOT EXISTS idx_registered_accounts_status
                    ON registered_accounts(status);
                CREATE INDEX IF NOT EXISTS idx_registered_accounts_source
                    ON registered_accounts(source);
                """
            )
            action_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(action_journal)")
            }
            if "step_id" not in action_columns:
                connection.execute(
                    """
                    ALTER TABLE action_journal
                    ADD COLUMN step_id TEXT NOT NULL DEFAULT ''
                    """
                )
            if "effect_class" not in action_columns:
                connection.execute(
                    """
                    ALTER TABLE action_journal
                    ADD COLUMN effect_class TEXT NOT NULL DEFAULT 'UNKNOWN'
                    """
                )
            session_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(workflow_sessions)")
            }
            if "run_scope" not in session_columns:
                connection.execute(
                    """
                    ALTER TABLE workflow_sessions
                    ADD COLUMN run_scope TEXT NOT NULL DEFAULT 'full'
                    """
                )
            if "account_id" not in session_columns:
                connection.execute(
                    """
                    ALTER TABLE workflow_sessions
                    ADD COLUMN account_id TEXT
                    """
                )
            if "browser_backend" not in session_columns:
                connection.execute(
                    """
                    ALTER TABLE workflow_sessions
                    ADD COLUMN browser_backend TEXT NOT NULL DEFAULT 'cloak'
                    """
                )
            account_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(registered_accounts)")
            }
            if "offer_status" not in account_columns:
                connection.execute(
                    """
                    ALTER TABLE registered_accounts
                    ADD COLUMN offer_status TEXT NOT NULL DEFAULT 'UNKNOWN'
                    """
                )
            if "offer_marked_at" not in account_columns:
                connection.execute(
                    """
                    ALTER TABLE registered_accounts
                    ADD COLUMN offer_marked_at TEXT
                    """
                )
            if "offer_marked_reason" not in account_columns:
                connection.execute(
                    """
                    ALTER TABLE registered_accounts
                    ADD COLUMN offer_marked_reason TEXT NOT NULL DEFAULT ''
                    """
                )
            if current_version < SCHEMA_VERSION:
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            connection.commit()

    def create_session(self, email: str) -> str:
        return self.create_session_with_resources(email, {})

    def create_session_with_resources(
        self,
        email: str,
        resources: Dict[str, str],
        browser_backend: str = "cloak",
    ) -> str:
        """Create a workflow session and reserve its inputs atomically."""
        session_id = str(uuid.uuid4())
        timestamp = _now()
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO workflow_sessions (
                    session_id, email_fingerprint, stage, status,
                    browser_backend, created_at, updated_at
                ) VALUES (?, ?, 'CREATED', 'PENDING', ?, ?, ?)
                """,
                (
                    session_id,
                    _fingerprint("email", email),
                    browser_backend,
                    timestamp,
                    timestamp,
                ),
            )
            self._reserve_resources(connection, session_id, resources)
        return session_id

    def get_session_backend(self, session_id: str) -> str:
        """Return the browser backend recorded for a workflow session."""
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT browser_backend
                FROM workflow_sessions WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
        if row is None:
            raise StateTransitionError(f"unknown workflow session: {session_id}")
        return str(row["browser_backend"])

    def set_session_backend(self, session_id: str, backend: str) -> None:
        """Persist the browser backend; explicit values are immutable.

        The legacy placeholder ``cloak`` may be upgraded to the real backend
        once, but a session that already recorded a non-default backend can
        never be re-assigned, so a resumed session always keeps its original
        browser.
        """
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT browser_backend
                FROM workflow_sessions WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
            if row is None:
                raise StateTransitionError(f"unknown workflow session: {session_id}")
            current = str(row["browser_backend"])
            if current != "cloak" and current != backend:
                raise StateTransitionError(
                    f"session {session_id} browser backend is immutable: {current}"
                )
            if current != backend:
                connection.execute(
                    """
                    UPDATE workflow_sessions
                    SET browser_backend = ?, updated_at = ?
                    WHERE session_id = ?
                    """,
                    (backend, _now(), session_id),
                )

    def update_session(
        self,
        session_id: str,
        *,
        stage: str,
        status: str,
        last_error: str = "",
    ) -> None:
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE workflow_sessions
                SET stage = ?, status = ?, last_error = ?, updated_at = ?
                WHERE session_id = ?
                """,
                (stage, status, last_error, _now(), session_id),
            )
            if cursor.rowcount != 1:
                raise StateTransitionError(f"unknown workflow session: {session_id}")

    def update_session_scope(
        self,
        session_id: str,
        *,
        run_scope: str,
        account_id: Optional[str] = None,
    ) -> None:
        """Persist which pipeline created/owns a session and its inventory link."""
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE workflow_sessions
                SET run_scope = ?, account_id = ?, updated_at = ?
                WHERE session_id = ?
                """,
                (run_scope, account_id, _now(), session_id),
            )
            if cursor.rowcount != 1:
                raise StateTransitionError(f"unknown workflow session: {session_id}")

    def get_session_scope(
        self,
        session_id: str,
    ) -> Optional[tuple[str, Optional[str]]]:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT run_scope, account_id
                FROM workflow_sessions WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        return str(row["run_scope"]), row["account_id"]

    @staticmethod
    def email_fingerprint(email: str) -> str:
        """Public one-way fingerprint used by inventory and resource views."""
        return _fingerprint("email", email)

    def upsert_registered_account(
        self,
        *,
        account_id: str,
        email: str,
        source: str,
        status: str,
        credential_ref: str,
        has_chatgpt_password: bool,
        has_totp_secret: bool,
        has_email_password: bool,
        has_oauth: bool,
        provision_session_id: Optional[str] = None,
        last_checkout_session_id: Optional[str] = None,
        ready_verified_at: Optional[str] = None,
        notes: str = "",
        offer_status: str = "UNKNOWN",
    ) -> RegisteredAccountRecord:
        timestamp = _now()
        fingerprint = _fingerprint("email", email)
        with self._transaction() as connection:
            existing = connection.execute(
                """
                SELECT status FROM registered_accounts
                WHERE email_fingerprint = ?
                """,
                (fingerprint,),
            ).fetchone()
            if existing is not None:
                connection.execute(
                    """
                    UPDATE registered_accounts
                    SET source = ?, status = ?, credential_ref = ?,
                        has_chatgpt_password = ?, has_totp_secret = ?,
                        has_email_password = ?, has_oauth = ?,
                        provision_session_id = COALESCE(?, provision_session_id),
                        last_checkout_session_id = ?,
                        ready_verified_at = COALESCE(?, ready_verified_at),
                        notes = ?, updated_at = ?
                    WHERE email_fingerprint = ?
                    """,
                    (
                        source,
                        status,
                        credential_ref,
                        int(has_chatgpt_password),
                        int(has_totp_secret),
                        int(has_email_password),
                        int(has_oauth),
                        provision_session_id,
                        last_checkout_session_id,
                        ready_verified_at,
                        notes,
                        timestamp,
                        fingerprint,
                    ),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO registered_accounts (
                        account_id, email_fingerprint, source, status,
                        provision_session_id, last_checkout_session_id,
                        credential_ref, has_chatgpt_password, has_totp_secret,
                        has_email_password, has_oauth, ready_verified_at,
                        last_login_verified_at, notes, offer_status,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?)
                    """,
                    (
                        account_id,
                        fingerprint,
                        source,
                        status,
                        provision_session_id,
                        last_checkout_session_id,
                        credential_ref,
                        int(has_chatgpt_password),
                        int(has_totp_secret),
                        int(has_email_password),
                        int(has_oauth),
                        ready_verified_at,
                        notes,
                        offer_status,
                        timestamp,
                        timestamp,
                    ),
                )
        return self.get_registered_account(account_id)

    def get_registered_account(
        self,
        account_id: str,
    ) -> Optional[RegisteredAccountRecord]:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT account_id, email_fingerprint, source, status,
                       provision_session_id, last_checkout_session_id,
                       credential_ref, has_chatgpt_password, has_totp_secret,
                       has_email_password, has_oauth, ready_verified_at,
                       last_login_verified_at, notes, offer_status,
                       offer_marked_at, offer_marked_reason,
                       created_at, updated_at
                FROM registered_accounts WHERE account_id = ?
                """,
                (account_id,),
            ).fetchone()
        return None if row is None else self._registered_account_from_row(row)

    def get_registered_account_by_email(
        self,
        email: str,
    ) -> Optional[RegisteredAccountRecord]:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT account_id, email_fingerprint, source, status,
                       provision_session_id, last_checkout_session_id,
                       credential_ref, has_chatgpt_password, has_totp_secret,
                       has_email_password, has_oauth, ready_verified_at,
                       last_login_verified_at, notes, offer_status,
                       offer_marked_at, offer_marked_reason,
                       created_at, updated_at
                FROM registered_accounts WHERE email_fingerprint = ?
                """,
                (_fingerprint("email", email),),
            ).fetchone()
        return None if row is None else self._registered_account_from_row(row)

    def list_registered_accounts(
        self,
        *,
        statuses: Optional[tuple[str, ...]] = None,
        include_paid_only: bool = False,
    ) -> tuple[RegisteredAccountRecord, ...]:
        offer_filter = (
            ""
            if include_paid_only
            else (
                " AND offer_status NOT IN "
                "('PAID_ONLY', 'REVIEW_REQUIRED', 'NO_TARGET_OFFER')"
            )
        )
        with self._connection() as connection:
            if statuses is None:
                rows = connection.execute(
                    f"""
                    SELECT account_id, email_fingerprint, source, status,
                           provision_session_id, last_checkout_session_id,
                           credential_ref, has_chatgpt_password, has_totp_secret,
                           has_email_password, has_oauth, ready_verified_at,
                           last_login_verified_at, notes, offer_status,
                           offer_marked_at, offer_marked_reason,
                           created_at, updated_at
                    FROM registered_accounts
                    WHERE 1 = 1{offer_filter}
                    ORDER BY created_at
                    """
                ).fetchall()
            else:
                placeholders = ",".join("?" for _ in statuses)
                rows = connection.execute(
                    f"""
                    SELECT account_id, email_fingerprint, source, status,
                           provision_session_id, last_checkout_session_id,
                           credential_ref, has_chatgpt_password, has_totp_secret,
                           has_email_password, has_oauth, ready_verified_at,
                           last_login_verified_at, notes, offer_status,
                           offer_marked_at, offer_marked_reason,
                           created_at, updated_at
                    FROM registered_accounts
                    WHERE status IN ({placeholders}){offer_filter}
                    ORDER BY created_at
                    """,
                    statuses,
                ).fetchall()
        return tuple(self._registered_account_from_row(row) for row in rows)

    def update_registered_account_status(
        self,
        account_id: str,
        status: str,
        *,
        last_checkout_session_id: Optional[str] = None,
        last_login_verified_at: Optional[str] = None,
    ) -> RegisteredAccountRecord:
        timestamp = _now()
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE registered_accounts
                SET status = ?,
                    last_checkout_session_id = COALESCE(?, last_checkout_session_id),
                    last_login_verified_at = COALESCE(?, last_login_verified_at),
                    updated_at = ?
                WHERE account_id = ?
                """,
                (
                    status,
                    last_checkout_session_id,
                    last_login_verified_at,
                    timestamp,
                    account_id,
                ),
            )
            if cursor.rowcount != 1:
                raise StateTransitionError(f"unknown registered account: {account_id}")
        record = self.get_registered_account(account_id)
        if record is None:
            raise StateTransitionError(f"registered account disappeared: {account_id}")
        return record

    def registered_account_summary(self) -> dict[str, int]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM registered_accounts "
                "GROUP BY status"
            ).fetchall()
        key_map = {
            "READY": "ready",
            "RESERVED": "reserved",
            "CHECKOUT_RUNNING": "reserved",
            "CHECKOUT_DONE": "checkout_done",
            "NEEDS_OPERATOR": "needs_operator",
            "QUARANTINED": "quarantined",
            "INVALID": "invalid",
        }
        counts = {
            "total": sum(int(row["count"]) for row in rows),
            "ready": 0,
            "reserved": 0,
            "checkout_done": 0,
            "needs_operator": 0,
            "quarantined": 0,
            "invalid": 0,
        }
        for row in rows:
            key = key_map.get(row["status"])
            if key is not None:
                counts[key] += int(row["count"])
        return counts

    def begin_registered_account_import(
        self,
        *,
        source_name: str,
        total_rows: int,
        accepted_rows: int,
        rejected_rows: int,
    ) -> str:
        import_id = str(uuid.uuid4())
        timestamp = _now()
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO registered_account_imports (
                    import_id, source_name, total_rows, accepted_rows,
                    rejected_rows, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    import_id,
                    source_name,
                    total_rows,
                    accepted_rows,
                    rejected_rows,
                    timestamp,
                ),
            )
        return import_id

    def add_registered_account_import_row(
        self,
        *,
        import_id: str,
        row_no: int,
        email_fingerprint: Optional[str],
        result: str,
        reason: str = "",
    ) -> None:
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO registered_account_import_rows (
                    import_id, row_no, email_fingerprint, result, reason
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (import_id, row_no, email_fingerprint, result, reason),
            )

    @staticmethod
    def _registered_account_from_row(row: sqlite3.Row) -> RegisteredAccountRecord:
        return RegisteredAccountRecord(
            account_id=row["account_id"],
            email_fingerprint=row["email_fingerprint"],
            source=row["source"],
            status=row["status"],
            provision_session_id=row["provision_session_id"],
            last_checkout_session_id=row["last_checkout_session_id"],
            credential_ref=row["credential_ref"],
            has_chatgpt_password=bool(row["has_chatgpt_password"]),
            has_totp_secret=bool(row["has_totp_secret"]),
            has_email_password=bool(row["has_email_password"]),
            has_oauth=bool(row["has_oauth"]),
            ready_verified_at=row["ready_verified_at"],
            last_login_verified_at=row["last_login_verified_at"],
            offer_status=row["offer_status"],
            offer_marked_at=row["offer_marked_at"],
            offer_marked_reason=row["offer_marked_reason"],
            notes=row["notes"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def update_registered_account_offer(
        self,
        account_id: str,
        *,
        offer_status: str,
        reason: str = "",
    ) -> RegisteredAccountRecord:
        """Set the free-offer marker (PAID_ONLY / REVIEW_REQUIRED) or reset it to UNKNOWN."""
        timestamp = _now()
        marked_at = (
            timestamp if offer_status in {"PAID_ONLY", "REVIEW_REQUIRED"} else None
        )
        reason_value = reason if offer_status in {"PAID_ONLY", "REVIEW_REQUIRED"} else ""
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE registered_accounts
                SET offer_status = ?,
                    offer_marked_at = ?,
                    offer_marked_reason = ?,
                    updated_at = ?
                WHERE account_id = ?
                """,
                (
                    offer_status,
                    marked_at,
                    reason_value,
                    timestamp,
                    account_id,
                ),
            )
            if cursor.rowcount != 1:
                raise StateTransitionError(f"unknown registered account: {account_id}")
        record = self.get_registered_account(account_id)
        if record is None:
            raise StateTransitionError(f"registered account disappeared: {account_id}")
        return record

    def record_step_status(
        self,
        session_id: str,
        step_id: str,
        *,
        status: str,
        outcome_code: str = "",
        last_error: str = "",
    ) -> None:
        """Persist a step state without inventing success evidence."""
        timestamp = _now()
        with self._transaction() as connection:
            self._require_session(connection, session_id)
            current = connection.execute(
                """
                SELECT status, evidence_digest, attempt_no
                FROM workflow_step_runs
                WHERE session_id = ? AND step_id = ?
                """,
                (session_id, step_id),
            ).fetchone()
            if (
                current is not None
                and current["status"] == "SUCCEEDED"
                and current["evidence_digest"]
            ):
                # Positive evidence is the durable proof.  A later resume or
                # diagnostic observation may not overwrite it with RUNNING,
                # READY, or an unverified failure state.
                return
            current_attempt = 0 if current is None else int(current["attempt_no"])
            if current is None:
                attempt_no = 0 if status == "READY" else 1
            elif status == "RUNNING" and current["status"] != "RUNNING":
                attempt_no = current_attempt + 1
            else:
                attempt_no = current_attempt
            connection.execute(
                """
                INSERT INTO workflow_step_runs (
                    session_id, step_id, status, outcome_code, attempt_no,
                    evidence_digest, last_error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, '', ?, ?, ?)
                ON CONFLICT(session_id, step_id) DO UPDATE SET
                    status = excluded.status,
                    outcome_code = excluded.outcome_code,
                    last_error = excluded.last_error,
                    attempt_no = excluded.attempt_no,
                    updated_at = excluded.updated_at
                """,
                (
                    session_id,
                    step_id,
                    status,
                    outcome_code,
                    attempt_no,
                    last_error,
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                """
                INSERT INTO workflow_step_events (
                    session_id, step_id, attempt_no, status, outcome_code,
                    evidence_digest, last_error, created_at
                ) VALUES (?, ?, ?, ?, ?, '', ?, ?)
                """,
                (
                    session_id,
                    step_id,
                    attempt_no,
                    status,
                    outcome_code,
                    last_error,
                    timestamp,
                ),
            )

    def record_step_evidence(self, session_id: str, evidence) -> EvidenceRecord:
        """Atomically persist positive evidence and mark its step succeeded."""
        from gpt_automation.workflow.graph import Evidence

        if not isinstance(evidence, Evidence):
            raise TypeError("step evidence must be an Evidence instance")
        details_json = json.dumps(
            dict(evidence.details),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        canonical_details_json = json.dumps(
            dict(evidence.details),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(
            "|".join(
                (
                    evidence.step_id.value,
                    evidence.evidence_type,
                    evidence.verifier_version,
                    canonical_details_json,
                )
            ).encode("utf-8")
        ).hexdigest()
        timestamp = _now()
        with self._transaction() as connection:
            self._require_session(connection, session_id)
            current = connection.execute(
                """
                SELECT attempt_no FROM workflow_step_runs
                WHERE session_id = ? AND step_id = ?
                """,
                (session_id, evidence.step_id.value),
            ).fetchone()
            attempt_no = 1 if current is None else max(1, int(current["attempt_no"]))
            connection.execute(
                """
                INSERT OR IGNORE INTO workflow_evidence (
                    session_id, step_id, evidence_type, evidence_digest,
                    details_json, verifier_version, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    evidence.step_id.value,
                    evidence.evidence_type,
                    digest,
                    details_json,
                    evidence.verifier_version,
                    timestamp,
                ),
            )
            connection.execute(
                """
                INSERT INTO workflow_step_runs (
                    session_id, step_id, status, outcome_code, attempt_no,
                    evidence_digest, last_error, created_at, updated_at
                ) VALUES (?, ?, 'SUCCEEDED', 'VERIFIED', ?, ?, '', ?, ?)
                ON CONFLICT(session_id, step_id) DO UPDATE SET
                    status = 'SUCCEEDED',
                    outcome_code = 'VERIFIED',
                    attempt_no = excluded.attempt_no,
                    evidence_digest = excluded.evidence_digest,
                    last_error = '',
                    updated_at = excluded.updated_at
                """,
                (
                    session_id,
                    evidence.step_id.value,
                    attempt_no,
                    digest,
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                """
                INSERT INTO workflow_step_events (
                    session_id, step_id, attempt_no, status, outcome_code,
                    evidence_digest, last_error, created_at
                ) VALUES (?, ?, ?, 'SUCCEEDED', 'VERIFIED', ?, '', ?)
                """,
                (
                    session_id,
                    evidence.step_id.value,
                    attempt_no,
                    digest,
                    timestamp,
                ),
            )

        return EvidenceRecord(
            session_id=session_id,
            step_id=evidence.step_id.value,
            evidence_type=evidence.evidence_type,
            evidence_digest=digest,
            details=evidence.details,
            verifier_version=evidence.verifier_version,
            created_at=timestamp,
        )

    def get_step_record(self, session_id: str, step_id: str) -> Optional[StepRecord]:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT session_id, step_id, status, outcome_code, attempt_no,
                       evidence_digest, last_error, created_at, updated_at
                FROM workflow_step_runs
                WHERE session_id = ? AND step_id = ?
                """,
                (session_id, step_id),
            ).fetchone()
        if row is None:
            return None
        return StepRecord(**dict(row))

    def list_step_records(self, session_id: str) -> tuple[StepRecord, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT session_id, step_id, status, outcome_code, attempt_no,
                       evidence_digest, last_error, created_at, updated_at
                FROM workflow_step_runs
                WHERE session_id = ?
                ORDER BY updated_at, step_id
                """,
                (session_id,),
            ).fetchall()
        return tuple(StepRecord(**dict(row)) for row in rows)

    def list_step_events(
        self,
        session_id: str,
        step_id: Optional[str] = None,
    ) -> tuple[StepEventRecord, ...]:
        with self._connection() as connection:
            if step_id is None:
                rows = connection.execute(
                    """
                    SELECT event_id, session_id, step_id, attempt_no, status,
                           outcome_code, evidence_digest, last_error, created_at
                    FROM workflow_step_events
                    WHERE session_id = ?
                    ORDER BY event_id
                    """,
                    (session_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT event_id, session_id, step_id, attempt_no, status,
                           outcome_code, evidence_digest, last_error, created_at
                    FROM workflow_step_events
                    WHERE session_id = ? AND step_id = ?
                    ORDER BY event_id
                    """,
                    (session_id, step_id),
                ).fetchall()
        return tuple(StepEventRecord(**dict(row)) for row in rows)

    def get_step_evidence(
        self,
        session_id: str,
        step_id: str,
        *,
        evidence_digest: Optional[str] = None,
    ) -> Optional[EvidenceRecord]:
        with self._connection() as connection:
            if evidence_digest is None:
                row = connection.execute(
                    """
                    SELECT session_id, step_id, evidence_type, evidence_digest,
                           details_json, verifier_version, created_at
                    FROM workflow_evidence
                    WHERE session_id = ? AND step_id = ?
                    ORDER BY created_at DESC, evidence_digest DESC
                    LIMIT 1
                    """,
                    (session_id, step_id),
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT session_id, step_id, evidence_type, evidence_digest,
                           details_json, verifier_version, created_at
                    FROM workflow_evidence
                    WHERE session_id = ? AND step_id = ? AND evidence_digest = ?
                    LIMIT 1
                    """,
                    (session_id, step_id, evidence_digest),
                ).fetchone()
        if row is None:
            return None
        details = tuple(json.loads(row["details_json"]).items())
        return EvidenceRecord(
            session_id=row["session_id"],
            step_id=row["step_id"],
            evidence_type=row["evidence_type"],
            evidence_digest=row["evidence_digest"],
            details=details,
            verifier_version=row["verifier_version"],
            created_at=row["created_at"],
        )

    def get_latest_network_ip(self) -> Optional[str]:
        """Recover the most recent browser-verified IP across all sessions.

        Used at batch start so a crashed or aborted process can still enforce
        the adjacent-IP rule instead of forgetting which IP the last account
        actually used.  Only ``NETWORK_READY`` evidence carrying an
        ``observed_ip`` detail qualifies; returns ``None`` when nothing has
        been recorded yet.
        """
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT details_json
                FROM workflow_evidence
                WHERE step_id = 'NETWORK_READY'
                ORDER BY created_at DESC
                LIMIT 50
                """,
            ).fetchall()
        for row in rows:
            try:
                details = json.loads(row["details_json"])
            except (json.JSONDecodeError, TypeError):
                continue
            ip = details.get("observed_ip") if isinstance(details, dict) else None
            if isinstance(ip, str) and ip:
                return ip
        return None

    def begin_finalization(
        self,
        session_id: str,
        result_token: str = "",
    ) -> FinalizationRecord:
        timestamp = _now()
        with self._transaction() as connection:
            self._require_session(connection, session_id)
            connection.execute(
                """
                INSERT OR IGNORE INTO workflow_finalizations (
                    session_id, status, result_token, last_error,
                    created_at, updated_at
                ) VALUES (?, 'PREPARING', ?, '', ?, ?)
                """,
                (session_id, result_token, timestamp, timestamp),
            )
        record = self.get_finalization(session_id)
        if record is None:
            raise StateTransitionError(
                f"finalization record could not be created: {session_id}"
            )
        return record

    def mark_finalization(
        self,
        session_id: str,
        status: FinalizationStatus,
        *,
        last_error: str = "",
    ) -> FinalizationRecord:
        with self._transaction() as connection:
            self._require_session(connection, session_id)
            cursor = connection.execute(
                """
                UPDATE workflow_finalizations
                SET status = ?, last_error = ?, updated_at = ?
                WHERE session_id = ?
                """,
                (status.value, last_error, _now(), session_id),
            )
            if cursor.rowcount != 1:
                raise StateTransitionError(
                    f"unknown finalization record: {session_id}"
                )
        record = self.get_finalization(session_id)
        if record is None:
            raise StateTransitionError(
                f"finalization record disappeared: {session_id}"
            )
        return record

    def get_finalization(
        self,
        session_id: str,
    ) -> Optional[FinalizationRecord]:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT session_id, status, result_token, last_error,
                       created_at, updated_at
                FROM workflow_finalizations
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        return FinalizationRecord(
            session_id=row["session_id"],
            status=FinalizationStatus(row["status"]),
            result_token=row["result_token"],
            last_error=row["last_error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def list_pending_finalizations(self) -> list[FinalizationRecord]:
        """Return finalization records that still need reconciliation."""
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT session_id, status, result_token, last_error,
                       created_at, updated_at
                FROM workflow_finalizations
                WHERE status != 'FINALIZED'
                ORDER BY updated_at
                """
            ).fetchall()
        return [
            FinalizationRecord(
                session_id=row["session_id"],
                status=FinalizationStatus(row["status"]),
                result_token=row["result_token"],
                last_error=row["last_error"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

    @staticmethod
    def _require_session(connection: sqlite3.Connection, session_id: str) -> None:
        row = connection.execute(
            "SELECT 1 FROM workflow_sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            raise StateTransitionError(f"unknown workflow session: {session_id}")

    def get_session(self, session_id: str) -> Optional[SessionRecord]:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT session_id, email_fingerprint, stage, status,
                       last_error, browser_backend
                FROM workflow_sessions WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        return SessionRecord(**dict(row))

    def recover_interrupted_sessions(self, session_id: Optional[str] = None) -> int:
        """Convert sessions left RUNNING by a prior process into operator work."""
        with self._transaction() as connection:
            if session_id is None:
                cursor = connection.execute(
                    """
                    UPDATE workflow_sessions
                    SET status = 'NEEDS_OPERATOR',
                        last_error = 'PROCESS_INTERRUPTED',
                        updated_at = ?
                    WHERE status = 'RUNNING'
                    """,
                    (_now(),),
                )
            else:
                cursor = connection.execute(
                    """
                    UPDATE workflow_sessions
                    SET status = 'NEEDS_OPERATOR',
                        last_error = 'PROCESS_INTERRUPTED',
                        updated_at = ?
                    WHERE status = 'RUNNING' AND session_id = ?
                    """,
                    (_now(), session_id),
                )
            # 已被登记为 PROCESS_INTERRUPTED 的会话若仍挂着 CHECKOUT_RUNNING
            # 账号，则把账号同步转成 NEEDS_OPERATOR，避免下一次开通被
            # 「开通中」状态假死锁（支付中断遗留现场）。
            connection.execute(
                """
                UPDATE registered_accounts
                SET status = 'NEEDS_OPERATOR',
                    updated_at = ?
                WHERE status = 'CHECKOUT_RUNNING'
                  AND last_checkout_session_id IN (
                      SELECT session_id FROM workflow_sessions
                      WHERE last_error = 'PROCESS_INTERRUPTED'
                  )
                """,
                (_now(),),
            )
            return cursor.rowcount

    def list_resumable_sessions(self) -> list[SessionRecord]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT session_id, email_fingerprint, stage, status,
                       last_error, browser_backend
                FROM workflow_sessions
                WHERE status IN (
                    'PENDING', 'RUNNING', 'NEEDS_OPERATOR', 'UNKNOWN_SIDE_EFFECT'
                )
                ORDER BY created_at
                """
            ).fetchall()
        return [SessionRecord(**dict(row)) for row in rows]

    def list_sessions(self) -> list[SessionOverview]:
        """Return all sessions for the interactive recovery catalog."""
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT session_id, email_fingerprint, stage, status,
                       last_error, created_at, updated_at, browser_backend
                FROM workflow_sessions
                ORDER BY created_at
                """
            ).fetchall()
        return [SessionOverview(**dict(row)) for row in rows]

    def get_session_resource_fingerprint(
        self,
        session_id: str,
        resource_type: str,
    ) -> Optional[str]:
        """Return a resource fingerprint without exposing the resource value."""
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT resource_fingerprint
                FROM resource_leases
                WHERE session_id = ? AND resource_type = ?
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (session_id, resource_type),
            ).fetchone()
        return None if row is None else str(row[0])

    def session_owns_resource(
        self,
        session_id: str,
        resource_type: str,
        value: str,
    ) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM resource_leases
                WHERE session_id = ?
                    AND resource_type = ?
                    AND resource_fingerprint = ?
                    AND state IN ('RESERVED', 'QUARANTINED')
                """,
                (
                    session_id,
                    resource_type,
                    _fingerprint(resource_type, value),
                ),
            ).fetchone()
        return row is not None

    def reserve_resources(
        self,
        session_id: str,
        resources: Dict[str, str],
    ) -> None:
        """Reserve all resources atomically or reserve none of them."""
        with self._transaction() as connection:
            self._reserve_resources(connection, session_id, resources)

    @staticmethod
    def _reserve_resources(
        connection: sqlite3.Connection,
        session_id: str,
        resources: Dict[str, str],
    ) -> None:
        for resource_type, value in resources.items():
            fingerprint = _fingerprint(resource_type, value)
            row = connection.execute(
                """
                SELECT state FROM resource_leases
                WHERE resource_type = ? AND resource_fingerprint = ?
                """,
                (resource_type, fingerprint),
            ).fetchone()
            timestamp = _now()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO resource_leases (
                        resource_type, resource_fingerprint, session_id,
                        state, created_at, updated_at
                    ) VALUES (?, ?, ?, 'RESERVED', ?, ?)
                    """,
                    (
                        resource_type,
                        fingerprint,
                        session_id,
                        timestamp,
                        timestamp,
                    ),
                )
                continue
            if row["state"] == ResourceState.RELEASED.value:
                connection.execute(
                    """
                    UPDATE resource_leases
                    SET session_id = ?, state = 'RESERVED', updated_at = ?
                    WHERE resource_type = ? AND resource_fingerprint = ?
                    """,
                    (session_id, timestamp, resource_type, fingerprint),
                )
                continue
            raise ResourceConflictError(
                f"{resource_type} resource is already {row['state']}"
            )

    def get_resource_state(
        self,
        resource_type: str,
        value: str,
    ) -> Optional[ResourceState]:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT state FROM resource_leases
                WHERE resource_type = ? AND resource_fingerprint = ?
                """,
                (resource_type, _fingerprint(resource_type, value)),
            ).fetchone()
        return None if row is None else ResourceState(row["state"])

    def resource_is_available(self, resource_type: str, value: str) -> bool:
        state = self.get_resource_state(resource_type, value)
        return state is None or state is ResourceState.RELEASED

    def available_resource_values(
        self,
        resource_type: str,
        values: list[str],
    ) -> list[str]:
        """Filter candidate values against one consistent SQLite snapshot."""
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT resource_fingerprint
                FROM resource_leases
                WHERE resource_type = ? AND state != 'RELEASED'
                """,
                (resource_type,),
            ).fetchall()
        unavailable = {row["resource_fingerprint"] for row in rows}
        return [
            value
            for value in values
            if _fingerprint(resource_type, value) not in unavailable
        ]

    def transition_resource(
        self,
        resource_type: str,
        value: str,
        new_state: ResourceState,
        *,
        expected: ResourceState,
    ) -> None:
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE resource_leases
                SET state = ?, updated_at = ?
                WHERE resource_type = ? AND resource_fingerprint = ?
                    AND state = ?
                """,
                (
                    new_state.value,
                    _now(),
                    resource_type,
                    _fingerprint(resource_type, value),
                    expected.value,
                ),
            )
            if cursor.rowcount != 1:
                raise StateTransitionError(
                    f"{resource_type} resource is not {expected.value}"
                )

    def swap_session_card(
        self,
        session_id: str,
        *,
        old_card_fingerprint: str,
        new_card_value: str,
        old_card_final_state: ResourceState | str,
    ) -> None:
        """Atomically reserve the replacement card and retire the old one.

        The new card is reserved before the old card lease is moved to
        ``old_card_final_state`` (RELEASED to keep it reusable, QUARANTINED
        when the operator flagged the old card as bad).  A failed swap leaves
        both leases untouched so the page form and the inventory never
        diverge.
        """
        final_state = ResourceState(old_card_final_state)
        new_fingerprint = _fingerprint("card", new_card_value)
        timestamp = _now()
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT state FROM resource_leases
                WHERE resource_type = 'card' AND resource_fingerprint = ?
                """,
                (new_fingerprint,),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO resource_leases (
                        resource_type, resource_fingerprint, session_id,
                        state, created_at, updated_at
                    ) VALUES ('card', ?, ?, 'RESERVED', ?, ?)
                    """,
                    (new_fingerprint, session_id, timestamp, timestamp),
                )
            elif row["state"] == ResourceState.RELEASED.value:
                connection.execute(
                    """
                    UPDATE resource_leases
                    SET session_id = ?, state = 'RESERVED', updated_at = ?
                    WHERE resource_type = 'card' AND resource_fingerprint = ?
                    """,
                    (session_id, timestamp, new_fingerprint),
                )
            else:
                raise ResourceConflictError(
                    f"card resource is already {row['state']}"
                )
            cursor = connection.execute(
                """
                UPDATE resource_leases
                SET state = ?, updated_at = ?
                WHERE resource_type = 'card'
                    AND resource_fingerprint = ?
                    AND session_id = ?
                    AND state IN ('RESERVED', 'QUARANTINED')
                """,
                (
                    final_state.value,
                    timestamp,
                    old_card_fingerprint,
                    session_id,
                ),
            )
            if cursor.rowcount != 1:
                raise StateTransitionError(
                    "old card lease is not attached to this session"
                )

    def transition_session_resources(
        self,
        session_id: str,
        transitions: Dict[str, ResourceState],
        *,
        expected: ResourceState | tuple[ResourceState, ...] = ResourceState.RESERVED,
    ) -> None:
        """Transition all resources for a session in one transaction."""
        expected_states = (
            expected if isinstance(expected, tuple) else (expected,)
        )
        with self._transaction() as connection:
            for resource_type, new_state in transitions.items():
                current = connection.execute(
                    """
                    SELECT state FROM resource_leases
                    WHERE session_id = ? AND resource_type = ?
                    """,
                    (session_id, resource_type),
                ).fetchone()
                if current is not None and current["state"] == new_state.value:
                    continue
                if (
                    current is None
                    or current["state"]
                    not in {state.value for state in expected_states}
                ):
                    expected_names = ", ".join(
                        state.value for state in expected_states
                    )
                    raise StateTransitionError(
                        f"{resource_type} lease for {session_id} is not "
                        f"one of: {expected_names}"
                    )
                cursor = connection.execute(
                    """
                    UPDATE resource_leases
                    SET state = ?, updated_at = ?
                    WHERE session_id = ? AND resource_type = ? AND state = ?
                    """,
                    (
                        new_state.value,
                        _now(),
                        session_id,
                        resource_type,
                        current["state"],
                    ),
                )
                if cursor.rowcount != 1:
                    raise StateTransitionError("resource lease changed concurrently")

    def abandon_session(
        self,
        session_id: str,
        dispositions: Dict[str, ResourceState],
    ) -> None:
        """Mark a session abandoned and dispose each attached resource atomically.

        Missing resources are valid for sessions created before delayed card
        acquisition. Existing terminal resource states are left unchanged so
        the operation is safe to repeat after a partial operator retry.
        """
        with self._transaction() as connection:
            session = connection.execute(
                "SELECT status FROM workflow_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if session is None:
                raise StateTransitionError(f"unknown workflow session: {session_id}")
            if session["status"] == "ABANDONED":
                return
            if session["status"] not in {
                "PENDING",
                "RUNNING",
                "NEEDS_OPERATOR",
                "UNKNOWN_SIDE_EFFECT",
            }:
                raise StateTransitionError(
                    f"session {session_id} cannot be abandoned from {session['status']}"
                )

            timestamp = _now()
            for resource_type, new_state in dispositions.items():
                current = connection.execute(
                    """
                    SELECT state FROM resource_leases
                    WHERE session_id = ? AND resource_type = ?
                    """,
                    (session_id, resource_type),
                ).fetchone()
                if current is None or current["state"] == new_state.value:
                    continue
                if current["state"] not in {
                    ResourceState.RESERVED.value,
                    ResourceState.QUARANTINED.value,
                }:
                    continue
                connection.execute(
                    """
                    UPDATE resource_leases
                    SET state = ?, updated_at = ?
                    WHERE session_id = ? AND resource_type = ?
                    """,
                    (new_state.value, timestamp, session_id, resource_type),
                )
            connection.execute(
                """
                UPDATE workflow_sessions
                SET status = 'ABANDONED',
                    last_error = 'TASK_ABANDONED',
                    updated_at = ?
                WHERE session_id = ?
                """,
                (timestamp, session_id),
            )

    def begin_action(
        self,
        session_id: str,
        action: str,
        *,
        step_id: str = "",
        effect_class: str = "UNKNOWN",
    ) -> None:
        timestamp = _now()
        try:
            with self._transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO action_journal (
                        session_id, action, step_id, effect_class, state,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'STARTED', ?, ?)
                    """,
                    (
                        session_id,
                        action,
                        step_id,
                        effect_class,
                        timestamp,
                        timestamp,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            action_record = self.get_action(session_id, action)
            if action_record is not None:
                raise UnsafeActionReplayError(
                    f"{action} was already recorded as {action_record.state.value}"
                ) from exc
            raise

    def _finish_action(
        self,
        session_id: str,
        action: str,
        state: ActionState,
        details: str = "",
    ) -> None:
        with self._transaction() as connection:
            current = connection.execute(
                """
                SELECT state FROM action_journal
                WHERE session_id = ? AND action = ?
                """,
                (session_id, action),
            ).fetchone()
            if current is not None and current["state"] == state.value:
                return
            allowed_states = (
                (
                    ActionState.STARTED.value,
                    ActionState.UNKNOWN.value,
                    ActionState.REJECTED.value,
                )
                if state is ActionState.VERIFIED
                else (ActionState.STARTED.value,)
            )
            placeholders = ", ".join("?" for _ in allowed_states)
            cursor = connection.execute(
                f"""
                UPDATE action_journal
                SET state = ?, details = ?, updated_at = ?
                WHERE session_id = ? AND action = ?
                    AND state IN ({placeholders})
                """,
                (
                    state.value,
                    details,
                    _now(),
                    session_id,
                    action,
                    *allowed_states,
                ),
            )
            if cursor.rowcount != 1:
                raise StateTransitionError(
                    f"{action} cannot transition to {state.value}"
                )

    def mark_action_verified(self, session_id: str, action: str) -> None:
        self._finish_action(session_id, action, ActionState.VERIFIED)

    def mark_action_unknown(
        self,
        session_id: str,
        action: str,
        details: str = "",
    ) -> None:
        self._finish_action(session_id, action, ActionState.UNKNOWN, details)

    def mark_action_rejected(
        self,
        session_id: str,
        action: str,
        details: str = "",
    ) -> None:
        """Record an explicit payment rejection (safe to switch/retry)."""
        self._finish_action(session_id, action, ActionState.REJECTED, details)

    def get_action(self, session_id: str, action: str) -> Optional[ActionRecord]:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT session_id, action, state, details, step_id, effect_class
                FROM action_journal
                WHERE session_id = ? AND action = ?
                """,
                (session_id, action),
            ).fetchone()
        if row is None:
            return None
        return ActionRecord(
            session_id=row["session_id"],
            action=row["action"],
            state=ActionState(row["state"]),
            details=row["details"],
            step_id=row["step_id"],
                effect_class=row["effect_class"],
            )

    def list_actions_by_prefix(
        self,
        session_id: str,
        prefix: str,
    ) -> Tuple[ActionRecord, ...]:
        """Return every journal action whose name starts with ``prefix``.

        Used to catch numbered payment actions such as ``PAYMENT_SUBMIT#2``
        that exact-name lookups would miss.
        """
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT session_id, action, state, details, step_id, effect_class
                FROM action_journal
                WHERE session_id = ? AND action LIKE ?
                ORDER BY action
                """,
                (session_id, f"{prefix}%"),
            ).fetchall()
        return tuple(
            ActionRecord(
                session_id=row["session_id"],
                action=row["action"],
                state=ActionState(row["state"]),
                details=row["details"],
                step_id=row["step_id"],
                effect_class=row["effect_class"],
            )
            for row in rows
        )

    def has_unconfirmed_actions(self, session_id: str) -> bool:
        """True when any journal action is still ``STARTED`` or ``UNKNOWN``.

        连续执行模式用它区分「普通阻塞（可跳过）」与「外部动作结果不确定
        （必须隔离，禁止自动重试）」：已记录的非幂等动作一旦开始，就认为
        可能存在外部副作用。
        """
        return any(
            record.state in {ActionState.STARTED, ActionState.UNKNOWN}
            for record in self.list_actions_by_prefix(session_id, "")
        )

    def import_legacy_used_files(
        self,
        used_email_file: str | Path,
        used_card_file: str | Path,
    ) -> Tuple[int, int]:
        """Import old consumed resources once without changing source files."""
        email_count = self._import_legacy_file(Path(used_email_file), "email")
        card_count = self._import_legacy_file(Path(used_card_file), "card")
        return email_count, card_count

    def _import_legacy_file(self, path: Path, resource_type: str) -> int:
        if not path.exists():
            return 0
        source_key = f"{resource_type}:{path.resolve()}"
        with self._transaction() as connection:
            already_imported = connection.execute(
                "SELECT 1 FROM legacy_imports WHERE source_key = ?",
                (source_key,),
            ).fetchone()
            if already_imported:
                return 0

            imported = 0
            for raw_line in path.read_text(encoding="utf-8").splitlines():
                value = self._legacy_identity(resource_type, raw_line)
                if not value:
                    continue
                timestamp = _now()
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO resource_leases (
                        resource_type, resource_fingerprint, session_id,
                        state, created_at, updated_at
                    ) VALUES (?, ?, NULL, 'CONSUMED', ?, ?)
                    """,
                    (
                        resource_type,
                        _fingerprint(resource_type, value),
                        timestamp,
                        timestamp,
                    ),
                )
                imported += cursor.rowcount

            connection.execute(
                "INSERT INTO legacy_imports (source_key, imported_at) VALUES (?, ?)",
                (source_key, _now()),
            )
            return imported

    @staticmethod
    def _legacy_identity(resource_type: str, raw_line: str) -> str:
        raw_line = raw_line.strip()
        if not raw_line:
            return ""
        if resource_type == "email":
            return raw_line.split("----", 1)[0].strip()
        row = next(csv.reader([raw_line]), [])
        if len(row) < 3:
            return ""
        offset = 1 if len(row) >= 4 else 0
        return row[offset].strip()
