"""Card tracking and result file utilities for checkout fulfillment."""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class TrackedCardInfo:
    line_no: int
    card_no: str
    expiry: str
    cvc: str
    account: str = ""
    group_name: str = ""
    card_name: str = ""

    @property
    def masked_card_no(self) -> str:
        clean = self.card_no.replace(" ", "").replace("-", "")
        if len(clean) >= 8:
            return f"{clean[:4]} **** **** {clean[-4:]}"
        return "****"


class CardSourceTracker:
    """Tracks line numbers and metadata from input/cards.csv."""

    def __init__(self, file_path: str | Path) -> None:
        self.file_path = Path(file_path)
        self._cards: dict[str, TrackedCardInfo] = {}
        self._load()

    def _load(self) -> None:
        if not self.file_path.exists():
            return
        try:
            with open(self.file_path, "r", encoding="utf-8-sig") as f:
                reader = csv.reader(f)
                header = next(reader, None)
                for idx, row in enumerate(reader, start=2):
                    if not row:
                        continue
                    if len(row) >= 4 and len(row[1].strip()) >= 12:
                        account = row[0].strip()
                        card_no = row[1].strip()
                        expiry = row[2].strip()
                        cvc = row[3].strip()
                        group = row[4].strip() if len(row) > 4 else ""
                        name = row[5].strip() if len(row) > 5 else ""
                    elif len(row) >= 3:
                        account = ""
                        card_no = row[0].strip()
                        expiry = row[1].strip()
                        cvc = row[2].strip()
                        group = ""
                        name = ""
                    else:
                        continue
                    info = TrackedCardInfo(
                        line_no=idx,
                        card_no=card_no,
                        expiry=expiry,
                        cvc=cvc,
                        account=account,
                        group_name=group,
                        card_name=name,
                    )
                    self._cards[card_no] = info
                    clean_no = card_no.replace(" ", "").replace("-", "")
                    self._cards[clean_no] = info
        except Exception:
            pass

    def get_card_info(self, fingerprint_or_no: str) -> Optional[TrackedCardInfo]:
        return self._cards.get(fingerprint_or_no)

    def get_or_fallback_info(self, card_no: str, expiry: str = "", cvc: str = "") -> TrackedCardInfo:
        hit = self.get_card_info(card_no)
        if hit is not None:
            return hit
        return TrackedCardInfo(
            line_no=0,
            card_no=card_no,
            expiry=expiry,
            cvc=cvc,
        )


class BaseCardResultFile:
    def __init__(self, file_path: str | Path) -> None:
        self.file_path = Path(file_path)

    def append_card(
        self,
        card_info: TrackedCardInfo,
        *,
        email: str,
        pending_line_no: int = 0,
        batch_id: str = "",
        reason: str = "",
        decline_kind: str = "",
        decline_text: str = "",
    ) -> None:
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        write_header = not self.file_path.exists() or self.file_path.stat().st_size == 0
        with open(self.file_path, "a", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            if write_header:
                writer.writerow([
                    "email",
                    "card_no",
                    "expiry",
                    "cvc",
                    "status",
                    "reason",
                    "decline_kind",
                    "decline_text",
                    "batch_id",
                ])
            writer.writerow([
                email,
                card_info.card_no,
                card_info.expiry,
                card_info.cvc,
                getattr(self, "status_label", "PROCESSED"),
                reason,
                decline_kind,
                decline_text,
                batch_id,
            ])


class PassedCardsFile(BaseCardResultFile):
    status_label = "PASSED"


class DeclinedCardsFile(BaseCardResultFile):
    status_label = "DECLINED"
