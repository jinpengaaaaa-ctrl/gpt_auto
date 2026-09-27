"""Target-account identity verification for checkout login gates.

Logging in successfully only proves that *some* account is authenticated.
Before any checkout/payment step, the workflow must also prove that the
authenticated account is the READY account selected for this run.  This
module performs that second proof with fingerprint-only evidence.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Awaitable, Callable, Optional


class IdentityVerificationOutcome(str, Enum):
    """Stable verdict for the target-account identity gate."""

    VERIFIED = "VERIFIED"
    NEEDS_OPERATOR = "NEEDS_OPERATOR"


@dataclass(frozen=True)
class AuthenticationProof:
    """Fingerprint-only proof; plaintext emails must never be stored here."""

    expected_account_fingerprint: str
    observed_account_fingerprint: str
    verification_source: str
    login_method: str = "email_password_totp"
    verified_at: str = ""

    @property
    def identity_matches(self) -> bool:
        return bool(
            self.expected_account_fingerprint
            and self.observed_account_fingerprint
            and self.expected_account_fingerprint == self.observed_account_fingerprint
        )


@dataclass(frozen=True)
class IdentityVerificationResult:
    outcome: IdentityVerificationOutcome
    proof: AuthenticationProof


def account_fingerprint(email: str) -> str:
    """One-way fingerprint of the account email used in evidence records."""
    return hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()


async def verify_authenticated_identity(
    page,
    *,
    expected_email: str,
    read_account_email: Optional[
        Callable[[object], Awaitable[Optional[str]]]
    ] = None,
) -> IdentityVerificationResult:
    """Prove the logged-in account equals the selected READY account.

    The default reader performs a best-effort DOM read of the authenticated
    page and returns None when the current page cannot prove the account
    email; the gate then requires the operator.  The default is
    intentionally conservative and never guesses.
    """
    reader = read_account_email or _default_read_account_email
    observed: Optional[str] = None
    try:
        observed = await reader(page)
    except Exception:  # noqa: BLE001 - unreadable identity means operator
        observed = None
    expected_fp = account_fingerprint(expected_email)
    observed_fp = account_fingerprint(observed) if observed else ""
    proof = AuthenticationProof(
        expected_account_fingerprint=expected_fp,
        observed_account_fingerprint=observed_fp,
        verification_source=(
            "injected_reader" if read_account_email else "page_dom_reader"
        ),
        verified_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    outcome = (
        IdentityVerificationOutcome.VERIFIED
        if proof.identity_matches
        else IdentityVerificationOutcome.NEEDS_OPERATOR
    )
    return IdentityVerificationResult(outcome=outcome, proof=proof)


async def _default_read_account_email(page) -> Optional[str]:
    """Best-effort email read from the current authenticated page.

    Tries stable locators only; returns the first email-shaped value or None.
    """
    if page is None:
        return None
    candidates = (
        "input[name='email']",
        "input[type='email']",
        "[data-testid*='account-email']",
        "[data-testid*='email']",
        "[data-testid='account-menu-button']",
        "button[aria-label*='account']",
        "a[href*='/settings']",
    )
    for selector in candidates:
        try:
            locator = page.locator(selector).first
            if await locator.count() == 0:
                continue
            value = await locator.get_attribute("value", timeout=2000)
            if not value:
                value = (
                    await locator.text_content(timeout=2000) or ""
                ).strip()
            if value and "@" in value:
                return value
        except Exception:  # noqa: BLE001 - keep trying other selectors
            continue
    return None
