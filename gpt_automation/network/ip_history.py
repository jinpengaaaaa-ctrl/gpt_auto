"""
IP 使用历史 — 诊断账本 (Feature #11A).

File-backed ledger of which IPs were used, when, and by account fingerprint.
This ledger is a diagnostic record only: it never participates in admission
decisions (the only IP rule is "the next account's IP differs from the
previous one").  The file is bounded by ``ip_history_max`` only; there is no
time-based safety window anymore.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from typing import Optional

import config
from gpt_automation.utils.redaction import account_fingerprint

logger = logging.getLogger(__name__)


@dataclass
class IPUsageRecord:
    """One row in the IP history ledger."""

    ip: str
    used_at: str            # ISO-8601 UTC
    email: str                 # Historical field name; value is a fingerprint.

class IPHistory:
    """File-backed ledger of which IPs were used, and when."""

    def __init__(self, path: Optional[str] = None) -> None:
        cfg = config.IP_ROTATION_CONFIG
        self.path: str = path or config.IP_HISTORY_FILE
        self.max_records: int = int(cfg.get("ip_history_max", 1000))

    # ── IO ───────────────────────────────────────────────────────

    def load(self) -> list[IPUsageRecord]:
        """Read & prune the file, returning the in-window records."""
        if not os.path.exists(self.path):
            return []
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("ip_history: failed to read %s: %s", self.path, exc)
            return []
        if not isinstance(raw, list):
            return []

        records: list[IPUsageRecord] = []
        sanitized_legacy_account = False
        for item in raw:
            if not isinstance(item, dict):
                continue
            item = dict(item)
            stored_account = str(item.get("email", ""))
            if "@" in stored_account:
                item["email"] = account_fingerprint(stored_account)
                sanitized_legacy_account = True
            try:
                records.append(IPUsageRecord(**item))
            except TypeError:
                continue

        if len(records) > self.max_records:
            records = records[-self.max_records :]
            self._save(records)
        elif sanitized_legacy_account:
            self._save(records)
        return records

    def save(self, records: list[IPUsageRecord]) -> None:
        """Persist the records to disk."""
        self._save(records)

    def _save(self, records: list[IPUsageRecord]) -> None:
        try:
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump([asdict(r) for r in records], fh, ensure_ascii=False, indent=2)
        except OSError as exc:
            logger.warning("ip_history: failed to write %s: %s", self.path, exc)

    # ── Convenience ──────────────────────────────────────────────

    def record_usage(self, ip: str, email: str) -> bool:
        """Persist one usage row and report whether the ledger was updated.

        The ``ip + email`` pair is idempotent within the current safety window,
        preventing duplicate rows when a guarded account path is retried.
        """
        if not ip:
            return False
        account_reference = account_fingerprint(email)
        records = self.load()
        if any(
            record.ip == ip and record.email == account_reference
            for record in records
        ):
            return True
        records.append(
            IPUsageRecord(
                ip=ip,
                used_at=datetime.now(tz=timezone.utc).isoformat(),
                email=account_reference,
            )
        )
        records = records[-self.max_records :]
        try:
            self._save(records)
            with open(self.path, "r", encoding="utf-8") as fh:
                persisted = json.load(fh)
            return any(
                isinstance(item, dict)
                and item.get("ip") == ip
                and item.get("email") == account_reference
                for item in persisted
            )
        except (OSError, json.JSONDecodeError):
            return False
