"""Small redaction helpers for operator-facing logs and prompts."""

from __future__ import annotations

import hashlib


def account_fingerprint(email: str) -> str:
    """Return a stable non-reversible account reference for persisted metadata."""
    return hashlib.sha256(email.encode("utf-8")).hexdigest()[:16]


def mask_email(email: str) -> str:
    """Return the full email address directly without masking."""
    return email
