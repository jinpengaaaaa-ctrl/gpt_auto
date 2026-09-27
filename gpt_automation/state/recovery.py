"""Session discovery helpers for interactive recovery and profile inspection."""

from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable, Optional

from gpt_automation.state.execution_lock import SessionExecutionLock
from gpt_automation.state.store import StateStore


class ProfileState(str, Enum):
    AVAILABLE = "AVAILABLE"
    LEGACY = "LEGACY"
    MISSING = "MISSING"
    LOCKED = "LOCKED"
    ACTIVE = "ACTIVE"


class RecoveryCapability(str, Enum):
    RESUME = "RESUME"
    INSPECT = "INSPECT"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class SessionCandidate:
    session_id: str
    email: str
    stage: str
    status: str
    profile_state: ProfileState
    capability: RecoveryCapability
    profile_id: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class UnmappedProfile:
    profile_id: str
    state: ProfileState
    updated_at: str


_RESUMABLE_STATUSES = {
    "PENDING",
    "RUNNING",
    "NEEDS_OPERATOR",
    "UNKNOWN_SIDE_EFFECT",
}

_PRECHECKOUT_STAGES = {
    "CREATED",
    "NETWORK_PENDING",
    "NETWORK_READY",
    "ACCOUNT_VERIFIED",
    "PASSWORD_PENDING",
    "PASSWORD_VERIFIED",
    "MFA_PENDING",
    "MFA_VERIFIED",
    "OFFER_VERIFIED",
}


def inspect_profile_state(profile_path: str | Path) -> ProfileState:
    path = Path(profile_path)
    if not path.is_dir():
        return ProfileState.MISSING
    if any((path / lock_name).exists() for lock_name in (
        "SingletonLock",
        "SingletonCookie",
        "SingletonSocket",
    )):
        return ProfileState.LOCKED
    if path.name.startswith("profile_"):
        profile_id = path.name.removeprefix("profile_")
        if len(profile_id) == 12 and all(
            character in "0123456789abcdef" for character in profile_id.lower()
        ):
            return ProfileState.AVAILABLE
    return ProfileState.LEGACY


def resolve_email_for_fingerprint(
    lines: Iterable[str],
    fingerprint: str,
) -> Optional[str]:
    """Resolve an original input email using both historical hash formats."""
    target = fingerprint.strip().lower()
    for line in lines:
        email = line.split("----", 1)[0].strip()
        if not email:
            continue
        exact_hash = hashlib.sha256(email.encode("utf-8")).hexdigest()
        normalized_hash = hashlib.sha256(email.lower().encode("utf-8")).hexdigest()
        candidates = {
            exact_hash,
            exact_hash[:12],
            normalized_hash,
            normalized_hash[:12],
        }
        if target in candidates:
            return email
    return None


def render_session_candidate(candidate: SessionCandidate, index: int) -> str:
    """Render an interactive listing without masking the resolved email."""
    return (
        f"{index}) {candidate.email or '(email unavailable)'}  "
        f"session={candidate.session_id}  "
        f"stage={candidate.stage}  status={candidate.status}  "
        f"profile={candidate.profile_state.value}  "
        f"action={candidate.capability.value}"
    )


class SessionCatalog:
    """Build recovery choices without exposing plaintext resources to storage."""

    def __init__(
        self,
        state_store: StateStore,
        *,
        input_dir: str | Path,
        profile_dir: str | Path,
        lock_dir: str | Path | None = None,
    ) -> None:
        self.state_store = state_store
        self.input_dir = Path(input_dir)
        self.profile_dir = Path(profile_dir)
        self.lock_dir = Path(lock_dir) if lock_dir else self.profile_dir.parent / "session_locks"

    def _input_lines(self, filename: str) -> list[str]:
        path = self.input_dir / filename
        if not path.is_file():
            return []
        return path.read_text(encoding="utf-8").splitlines()

    @staticmethod
    def _matching_card_value(
        lines: Iterable[str],
        fingerprint: Optional[str],
    ) -> Optional[str]:
        if not fingerprint:
            return None
        for row in csv.reader(lines):
            if len(row) < 3:
                continue
            offset = 1 if len(row) >= 4 else 0
            card = row[offset].strip()
            normalized = "".join(character for character in card if character.isdigit())
            if not normalized:
                continue
            if hashlib.sha256(normalized.encode("utf-8")).hexdigest() == fingerprint:
                return normalized
        return None

    def list_candidates(self) -> list[SessionCandidate]:
        email_lines = self._input_lines("email.txt")
        card_lines = self._input_lines("cards.csv")
        candidates: list[SessionCandidate] = []
        for session in self.state_store.list_sessions():
            email_fingerprint = self.state_store.get_session_resource_fingerprint(
                session.session_id,
                "email",
            ) or session.email_fingerprint
            email = resolve_email_for_fingerprint(email_lines, email_fingerprint) or ""
            profile_id = (
                hashlib.sha256(email.encode("utf-8")).hexdigest()[:12]
                if email
                else ""
            )
            profile_path = self.profile_dir / f"profile_{profile_id}" if profile_id else self.profile_dir / "__missing__"
            profile_state = inspect_profile_state(profile_path)
            if SessionExecutionLock.is_locked(self.lock_dir / f"{session.session_id}.lock"):
                profile_state = ProfileState.ACTIVE
            email_owned = bool(email) and self.state_store.session_owns_resource(
                session.session_id,
                "email",
                email,
            )
            card_value = self._matching_card_value(
                card_lines,
                self.state_store.get_session_resource_fingerprint(
                    session.session_id,
                    "card",
                ),
            )
            card_owned = card_value is not None and self.state_store.session_owns_resource(
                session.session_id,
                "card",
                card_value,
            )
            resources_available = email_owned and (
                card_owned or session.stage in _PRECHECKOUT_STAGES
            )
            if profile_state in {ProfileState.LOCKED, ProfileState.ACTIVE}:
                capability = RecoveryCapability.UNAVAILABLE
            elif session.status in _RESUMABLE_STATUSES and resources_available:
                capability = RecoveryCapability.RESUME
            elif email and profile_state in {ProfileState.AVAILABLE, ProfileState.LEGACY}:
                capability = RecoveryCapability.INSPECT
            else:
                capability = RecoveryCapability.UNAVAILABLE
            candidates.append(
                SessionCandidate(
                    session_id=session.session_id,
                    email=email,
                    stage=session.stage,
                    status=session.status,
                    profile_state=profile_state,
                    capability=capability,
                    profile_id=profile_id,
                    updated_at=session.updated_at,
                )
            )
        return candidates

    def list_unmapped_profiles(self) -> list[UnmappedProfile]:
        managed_ids = {
            candidate.profile_id
            for candidate in self.list_candidates()
            if candidate.profile_id
        }
        if not self.profile_dir.is_dir():
            return []
        profiles: list[UnmappedProfile] = []
        for path in sorted(self.profile_dir.iterdir(), key=lambda item: item.name):
            if not path.is_dir() or not path.name.startswith("profile_"):
                continue
            profile_id = path.name.removeprefix("profile_")
            if profile_id in managed_ids:
                continue
            profiles.append(
                UnmappedProfile(
                    profile_id=profile_id,
                    state=inspect_profile_state(path),
                    updated_at=str(path.stat().st_mtime),
                )
            )
        return profiles
