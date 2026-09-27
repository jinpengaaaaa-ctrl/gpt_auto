"""Browser backend capability descriptions shared by UI, logs and tests."""

from __future__ import annotations

from gpt_automation.browser.models import BrowserBackend


def backend_capabilities(backend: str | BrowserBackend) -> dict[str, str]:
    """Human-readable capability map for one backend.

    The map is deliberately conservative: Firefox private mode guarantees a
    fresh non-persistent session per account but does *not* claim distinct
    browser fingerprints.  Cross-backend handoff always re-authenticates
    with saved credentials, never with cookies or profiles.
    """
    selected = (
        backend.value if isinstance(backend, BrowserBackend) else str(backend)
    )
    common = {
        "cross_backend_handoff": "credentials_relogin",
    }
    if selected == BrowserBackend.CLOAK.value:
        return {
            **common,
            "local_storage_isolation": "per_account_profile",
            "persistent_login": "yes",
            "profile_reuse_across_tasks": "yes",
            "fingerprint_isolation": "cloak_scope",
        }
    if selected == BrowserBackend.FIREFOX_PRIVATE.value:
        return {
            **common,
            "local_storage_isolation": "yes",
            "persistent_login": "no",
            "profile_reuse_across_tasks": "no",
            "fingerprint_isolation": "not_guaranteed",
        }
    raise ValueError(f"unknown browser backend: {selected}")
