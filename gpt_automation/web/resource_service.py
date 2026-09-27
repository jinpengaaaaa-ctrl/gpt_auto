"""Resource administration for the local dashboard.

The service reads ``input/email.txt`` and ``input/cards.csv`` and consults
the existing SQLite lease table for resource state. Values are returned to
the loopback-only console because the operator explicitly authorized local
display; writes remain atomic and audit records remain value-free.
"""
from __future__ import annotations

import csv
import hashlib
import os
import re
import sqlite3
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .contracts import ResourceSummary, ResourceView

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_CARD_RE = re.compile(r"^\d{12,19}$")
_EXPIRY_RE = re.compile(r"^(0[1-9]|1[0-2])(\d{2}|\d{4})$")
_CVC_RE = re.compile(r"^\d{3,4}$")
_CARDS_HEADER = (
    "UPSIDER帳号,卡号,有效期,CVC,持卡人,限额金额,グループ名,カード名,是否解绑"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fingerprint(resource_type: str, value: str) -> str:
    if resource_type == "email":
        normalized = value.strip().lower()
    elif resource_type == "card":
        normalized = "".join(ch for ch in value if ch.isdigit())
    else:
        normalized = value.strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class ResourceAdminError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ResourceAdminService:
    """Thread-safe resource listing and local-console administration."""

    def __init__(self, input_dir: str | Path, db_path: str | Path) -> None:
        self.input_dir = Path(input_dir)
        self.db_path = Path(db_path)
        self.input_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def list_resources(self, resource_type: str) -> List[ResourceView]:
        with self._lock:
            if resource_type == "email":
                return self._list_emails()
            if resource_type == "card":
                return self._list_cards()
            raise ResourceAdminError(
                "UNKNOWN_RESOURCE_TYPE", f"unknown resource type: {resource_type}"
            )

    def summary(self, resource_type: str) -> ResourceSummary:
        views = self.list_resources(resource_type)
        counts = {"available": 0, "reserved": 0, "consumed": 0, "quarantined": 0}
        for view in views:
            if view.state in counts:
                counts[view.state] += 1
        return ResourceSummary(
            resource_type=resource_type,
            total=len(views),
            available=counts["available"],
            reserved=counts["reserved"],
            consumed=counts["consumed"],
            quarantined=counts["quarantined"],
        )

    # ------------------------------------------------------------------
    # Writes (atomic)
    # ------------------------------------------------------------------

    def add_email(
        self,
        email: str,
        password: str,
        client_id: str,
        refresh_token: str,
    ) -> ResourceView:
        email = email.strip().lower()
        password = password.strip()
        client_id = client_id.strip()
        refresh_token = refresh_token.strip()
        self._validate_email_fields(email, password, client_id, refresh_token)
        with self._lock:
            rows = self._read_email_rows()
            identities = [self._email_identity(row) for row in rows]
            if email in identities:
                raise ResourceAdminError(
                    "RESOURCE_DUPLICATE", "email already exists"
                )
            line = "----".join(
                [email, password, client_id, refresh_token.rstrip("$")]
            )
            self._atomic_append_lines(self.input_dir / "email.txt", [line])
            return self._email_view(email, rows=rows + [line])

    def edit_email(
        self,
        resource_id: str,
        password: Optional[str] = None,
        client_id: Optional[str] = None,
        refresh_token: Optional[str] = None,
    ) -> ResourceView:
        resource_id = resource_id.strip().lower()
        with self._lock:
            rows = self._read_email_rows()
            index = self._find_email_index(rows, resource_id)
            if index is None:
                raise ResourceAdminError(
                    "RESOURCE_NOT_FOUND", "email not found"
                )
            self._ensure_editable("email", resource_id)
            current = self._parse_email_row(rows[index])
            new_password = (
                current["password"] if password is None else password.strip()
            )
            new_client_id = (
                current["client_id"] if client_id is None else client_id.strip()
            )
            new_token = (
                current["refresh_token"]
                if refresh_token is None
                else refresh_token.strip()
            )
            self._validate_email_fields(
                resource_id, new_password, new_client_id, new_token
            )
            rows[index] = "----".join(
                [resource_id, new_password, new_client_id, new_token.rstrip("$")]
            )
            self._atomic_write_lines(self.input_dir / "email.txt", rows)
            return self._email_view(resource_id, rows=rows)

    def add_card(self, card_no: str, expiry: str, cvc: str) -> ResourceView:
        card_no = "".join(ch for ch in card_no if ch.isdigit())
        expiry = self._normalize_expiry(expiry)
        cvc = cvc.strip()
        self._validate_card_fields(card_no, expiry, cvc)
        with self._lock:
            rows = self._read_card_rows()
            identities = [self._card_identity(row) for row in rows]
            if card_no in identities:
                raise ResourceAdminError(
                    "RESOURCE_DUPLICATE", "card already exists"
                )
            # Column layout mirrors input/cards.csv:
            # 帳号,卡号,有效期,CVC,持卡人,限额金额,グループ名,カード名,是否解绑
            row = ["", card_no, expiry, cvc, "", "", "", "", ""]
            line = self._render_csv_row(row)
            path = self.input_dir / "cards.csv"
            if not path.exists() or path.read_text(encoding="utf-8-sig").strip() == "":
                self._atomic_write(path, _CARDS_HEADER + "\n" + line + "\n")
            else:
                self._atomic_append_lines(path, [line])
            return self._card_view(card_no, rows=rows + [line])

    def edit_card(
        self,
        resource_id: str,
        expiry: Optional[str] = None,
        cvc: Optional[str] = None,
    ) -> ResourceView:
        with self._lock:
            rows = self._read_card_rows()
            card_no = self._resolve_card_id(rows, resource_id)
            index = self._find_card_index(rows, card_no)
            if index is None:
                raise ResourceAdminError(
                    "RESOURCE_NOT_FOUND", "card not found"
                )
            self._ensure_editable("card", card_no)
            current = self._parse_card_row(rows[index])
            new_expiry = (
                current["expiry"]
                if expiry is None
                else self._normalize_expiry(expiry)
            )
            new_cvc = current["cvc"] if cvc is None else cvc.strip()
            self._validate_card_fields(card_no, new_expiry, new_cvc)
            rows[index] = self._rebuild_card_row(rows[index], new_expiry, new_cvc)
            self._atomic_write_lines(
                self.input_dir / "cards.csv", rows, header=_CARDS_HEADER
            )
            return self._card_view(card_no, rows=rows)

    # ------------------------------------------------------------------
    # Internals: reading
    # ------------------------------------------------------------------

    def _read_email_rows(self) -> List[str]:
        path = self.input_dir / "email.txt"
        if not path.exists():
            return []
        return [
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def _read_card_rows(self) -> List[str]:
        path = self.input_dir / "cards.csv"
        if not path.exists():
            return []
        text = path.read_text(encoding="utf-8-sig")
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            return []
        return lines[1:]  # header is skipped

    @staticmethod
    def _parse_email_row(line: str) -> Dict[str, str]:
        parts = line.split("----")
        return {
            "email": parts[0].strip(),
            "password": parts[1].strip() if len(parts) > 1 else "",
            "client_id": parts[2].strip() if len(parts) > 2 else "",
            "refresh_token": parts[3].strip().rstrip("$")
            if len(parts) > 3
            else "",
        }

    @staticmethod
    def _parse_card_row(line: str) -> Dict[str, str]:
        parts = ResourceAdminService._split_csv_row(line)
        if len(parts) >= 9:
            return {
                "account": parts[0].strip(),
                "card_no": parts[1].strip(),
                "expiry": parts[2].strip(),
                "cvc": parts[3].strip(),
                "holder": parts[4].strip(),
                "limit_amount": parts[5].strip(),
                "group_name": parts[6].strip(),
                "card_name": parts[7].strip(),
                "unbind": parts[8].strip(),
            }
        offset = 1 if len(parts) >= 4 else 0
        return {
            "account": "",
            "card_no": parts[offset].strip() if len(parts) > offset else "",
            "expiry": parts[offset + 1].strip() if len(parts) > offset + 1 else "",
            "cvc": parts[offset + 2].strip() if len(parts) > offset + 2 else "",
            "holder": "",
            "limit_amount": "",
            "group_name": "",
            "card_name": "",
            "unbind": "",
        }

    @staticmethod
    def _split_csv_row(line: str) -> List[str]:
        return next(csv.reader([line]))

    @staticmethod
    def _rebuild_card_row(line: str, expiry: str, cvc: str) -> str:
        """Rewrite one card CSV row, preserving unrelated columns."""
        parts = ResourceAdminService._split_csv_row(line)
        if len(parts) >= 4:
            parts[2] = expiry
            parts[3] = cvc
        elif len(parts) == 3:
            parts = [parts[0], parts[1], expiry, cvc]
        else:
            raise ResourceAdminError(
                "RESOURCE_MALFORMED", "card row has too few columns"
            )
        return ResourceAdminService._render_csv_row(parts)

    @staticmethod
    def _render_csv_row(parts: List[str]) -> str:
        import io

        buffer = io.StringIO()
        csv.writer(buffer, lineterminator="\n").writerow(parts)
        return buffer.getvalue().rstrip("\r\n")

    @staticmethod
    def _email_identity(line: str) -> str:
        return line.split("----", 1)[0].strip().lower()

    @staticmethod
    def _card_identity(line: str) -> str:
        parts = ResourceAdminService._split_csv_row(line)
        if len(parts) < 3:
            return ""
        offset = 1 if len(parts) >= 4 else 0
        return parts[offset].strip()

    @staticmethod
    def _find_email_index(rows: List[str], email: str) -> Optional[int]:
        for index, row in enumerate(rows):
            if ResourceAdminService._email_identity(row) == email:
                return index
        return None

    @staticmethod
    def _find_card_index(rows: List[str], card_no: str) -> Optional[int]:
        for index, row in enumerate(rows):
            identity = ResourceAdminService._card_identity(row)
            if identity and identity.isdigit() and identity == card_no:
                return index
        return None

    @staticmethod
    def _resolve_card_id(rows: List[str], resource_id: str) -> str:
        """Resolve an opaque card ID (fingerprint) back to the card number."""
        resource_id = resource_id.strip()
        for row in rows:
            card_no = ResourceAdminService._card_identity(row)
            if not card_no or not card_no.isdigit():
                continue
            if (
                _fingerprint("card", card_no) == resource_id
                or "".join(ch for ch in resource_id if ch.isdigit()) == card_no
            ):
                return card_no
        raise ResourceAdminError("RESOURCE_NOT_FOUND", "card not found")

    def _ensure_editable(self, resource_type: str, resource_id: str) -> None:
        state = self._resource_state(resource_type, resource_id)
        if state != "available":
            raise ResourceAdminError(
                "RESOURCE_NOT_EDITABLE",
                f"{resource_type} resource is {state}; only available "
                "resources can be edited",
            )

    def _resource_state(self, resource_type: str, resource_id: str) -> str:
        fingerprint = _fingerprint(resource_type, resource_id)
        if not self.db_path.exists():
            return "available"
        try:
            with sqlite3.connect(self.db_path) as db:
                row = db.execute(
                    "SELECT state FROM resource_leases "
                    "WHERE resource_type = ? AND resource_fingerprint = ?",
                    (resource_type, fingerprint),
                ).fetchone()
        except sqlite3.Error:
            return "available"
        if row is None or row[0] == "RELEASED":
            return "available"
        return {
            "RESERVED": "reserved",
            "CONSUMED": "consumed",
            "QUARANTINED": "quarantined",
        }.get(row[0], "available")

    # ------------------------------------------------------------------
    # Internals: views
    # ------------------------------------------------------------------

    def _list_emails(self) -> List[ResourceView]:
        rows = self._read_email_rows()
        return [
            self._email_view(self._email_identity(row), rows=rows) for row in rows
        ]

    def _email_view(self, email: str, rows: List[str]) -> ResourceView:
        state = self._resource_state("email", email)
        matching = [
            row for row in rows if self._email_identity(row) == email
        ]
        parsed = self._parse_email_row(matching[0])
        return ResourceView(
            resource_type="email",
            resource_id=email,
            display=parsed["email"],
            state=state,
            session_id=self._lease_session("email", email),
            updated_at=self._lease_updated_at("email", email),
            editable=state == "available",
            detail={
                "email": parsed["email"],
                "password": parsed["password"],
                "client_id": parsed["client_id"],
                "refresh_token": parsed["refresh_token"],
            },
        )

    def _list_cards(self) -> List[ResourceView]:
        rows = self._read_card_rows()
        return [
            self._card_view(self._card_identity(row), rows=rows) for row in rows
        ]

    def _card_view(self, card_no: str, rows: List[str]) -> ResourceView:
        state = self._resource_state("card", card_no)
        matching = [
            row for row in rows if self._card_identity(row) == card_no
        ]
        parsed = self._parse_card_row(matching[0])
        return ResourceView(
            resource_type="card",
            resource_id=_fingerprint("card", card_no),
            display=parsed["card_no"],
            state=state,
            session_id=self._lease_session("card", card_no),
            updated_at=self._lease_updated_at("card", card_no),
            editable=state == "available",
            detail=parsed,
        )

    def _lease_session(self, resource_type: str, resource_id: str) -> Optional[str]:
        fingerprint = _fingerprint(resource_type, resource_id)
        if not self.db_path.exists():
            return None
        try:
            with sqlite3.connect(self.db_path) as db:
                row = db.execute(
                    "SELECT session_id FROM resource_leases "
                    "WHERE resource_type = ? AND resource_fingerprint = ? "
                    "AND session_id IS NOT NULL ORDER BY updated_at DESC LIMIT 1",
                    (resource_type, fingerprint),
                ).fetchone()
            return None if row is None else str(row[0])
        except sqlite3.Error:
            return None

    def _lease_updated_at(self, resource_type: str, resource_id: str) -> Optional[str]:
        fingerprint = _fingerprint(resource_type, resource_id)
        if not self.db_path.exists():
            return None
        try:
            with sqlite3.connect(self.db_path) as db:
                row = db.execute(
                    "SELECT updated_at FROM resource_leases "
                    "WHERE resource_type = ? AND resource_fingerprint = ? "
                    "ORDER BY updated_at DESC LIMIT 1",
                    (resource_type, fingerprint),
                ).fetchone()
            return None if row is None else str(row[0])
        except sqlite3.Error:
            return None

    # ------------------------------------------------------------------
    # Internals: validation
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_expiry(expiry: str) -> str:
        clean = expiry.strip().replace("/", "").replace("-", "")
        if len(clean) == 4:
            month, year = clean[:2], clean[2:]
        elif len(clean) == 6:
            month, year = clean[:2], clean[2:4]
        else:
            raise ResourceAdminError(
                "INVALID_EXPIRY", "expiry must be MMYY or MMYYYY"
            )
        if not _EXPIRY_RE.match(f"{month}{year}"):
            raise ResourceAdminError(
                "INVALID_EXPIRY", "expiry must be MMYY or MMYYYY"
            )
        return f"{month}{year}"

    def _validate_email_fields(
        self, email: str, password: str, client_id: str, refresh_token: str
    ) -> None:
        if not _EMAIL_RE.match(email):
            raise ResourceAdminError("INVALID_EMAIL", "invalid email address")
        if not password:
            raise ResourceAdminError(
                "INVALID_EMAIL_PASSWORD", "email password is required"
            )
        if not client_id:
            raise ResourceAdminError(
                "INVALID_CLIENT_ID", "client id is required"
            )
        if not refresh_token:
            raise ResourceAdminError(
                "INVALID_REFRESH_TOKEN", "refresh token is required"
            )

    def _validate_card_fields(self, card_no: str, expiry: str, cvc: str) -> None:
        if not _CARD_RE.match(card_no):
            raise ResourceAdminError(
                "INVALID_CARD", "card number must contain 12-19 digits"
            )
        if not _EXPIRY_RE.match(expiry):
            raise ResourceAdminError(
                "INVALID_EXPIRY", "expiry must be MMYY or MMYYYY"
            )
        if not _CVC_RE.match(cvc):
            raise ResourceAdminError(
                "INVALID_CVC", "cvc must contain 3-4 digits"
            )

    # ------------------------------------------------------------------
    # Internals: atomic writes
    # ------------------------------------------------------------------

    def _atomic_append_lines(self, path: Path, rows: Iterable[str]) -> None:
        existing = path.read_text(encoding="utf-8-sig") if path.exists() else ""
        text = existing.rstrip("\r\n")
        if text:
            text += "\n"
        text += "\n".join(rows) + "\n"
        self._atomic_write(path, text)

    def _atomic_write_lines(
        self, path: Path, rows: Iterable[str], header: Optional[str] = None
    ) -> None:
        lines = [header] if header else []
        lines.extend(rows)
        text = "\n".join(lines) + "\n"
        self._atomic_write(path, text)

    def _atomic_write(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
