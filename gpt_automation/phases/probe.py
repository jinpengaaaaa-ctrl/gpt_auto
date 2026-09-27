"""Shared locator visibility probe with session-loss fast-fail.

All screen/page probing funnels through :func:`probe_visible`.  A probe that
hits a dead browser target (``TargetClosedError`` / "has been closed") is
re-raised as :class:`SessionLostError` so polling loops abort immediately
instead of burning their whole budget against a page that no longer exists.
Transient probe failures keep propagating unchanged so existing
``SAFE_EXCEPTIONS`` semantics stay intact.
"""

from __future__ import annotations

from gpt_automation.utils.playwright_errors import (
    SessionLostError,
    is_session_lost,
)


async def probe_visible(locator) -> bool:
    """Return whether *locator* is visible right now; fast-fail on dead targets.

    Keeps the legacy ``locator.is_visible(timeout=0)`` fallback for small
    test/diagnostic locators that still require the ``timeout`` keyword.
    """
    try:
        try:
            return await locator.is_visible()
        except TypeError:
            return await locator.is_visible(timeout=0)
    except Exception as exc:  # noqa: BLE001 - classified, not swallowed
        if is_session_lost(exc):
            raise SessionLostError("target_closed") from exc
        raise
