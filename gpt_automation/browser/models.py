"""Typed browser-session contracts shared by every browser backend.

The business layers (``workflow/``, ``phases/``, ``inventory/``) must never
import concrete providers; they consume only the value types defined here
plus the ``BrowserSession`` returned by a provider.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, List, Optional


class BrowserBackend(str, Enum):
    """Stable identifiers for supported browser execution backends."""

    CLOAK = "cloak"
    FIREFOX_PRIVATE = "firefox_private"


class BackendAvailability(str, Enum):
    """Preflight verdict for one browser backend."""

    AVAILABLE = "AVAILABLE"
    MISSING_BINARY = "MISSING_BINARY"
    UNSUPPORTED = "UNSUPPORTED"


@dataclass(frozen=True)
class BackendReport:
    """Read-only preflight outcome; never contains secrets."""

    backend: BrowserBackend
    availability: BackendAvailability
    detail: str = ""


@dataclass(frozen=True)
class BrowserLaunchRequest:
    """One isolated browser session request for exactly one account."""

    account_id: str
    profile_id: str
    proxy: Optional[dict] = None
    locale: str = "ja-JP"
    timezone_id: str = "Asia/Tokyo"
    headed: bool = True


@dataclass
class BrowserSession:
    """Uniform handle over a browser session for one account.

    ``context`` and ``page`` are Playwright objects (or None when the session
    was never fully opened).  ``close()`` is idempotent and never raises; it
    first runs backend-specific teardown callbacks and then best-effort closes
    the Playwright context as a safety net.
    """

    backend: BrowserBackend
    context: Any
    page: Any
    private_mode: bool = False
    persistent_auth_state: bool = True
    closed_ok: bool = True
    close_error: str = ""
    session_lost: bool = False
    lost_reason: str = ""
    _closed: bool = False
    _close_callbacks: List[Callable[[], Awaitable[None]]] = field(
        default_factory=list
    )

    def attach_close(self, callback: Callable[[], Awaitable[None]]) -> None:
        """Register a backend-specific teardown callback (e.g. browser stop)."""
        self._close_callbacks.append(callback)

    def mark_session_lost(self, reason: str) -> None:
        """Flag the environment as gone (crash / unexpected close).

        Once flagged, :meth:`close` treats teardown as already complete: the
        browser no longer exists, so a failing ``context.close()`` is a no-op
        outcome, not a process leak.  ``reason`` is a stable code such as
        ``crash_event`` or ``browser_closed``.
        """
        if self._closed or self.session_lost:
            return
        self.session_lost = True
        self.lost_reason = reason

    async def close(self) -> None:
        """Close the session exactly once; later calls are no-ops."""
        if self._closed:
            return
        self._closed = True
        close_failures: List[str] = []
        for callback in self._close_callbacks:
            try:
                result = await callback()
                if result is False and not self.session_lost:
                    close_failures.append("backend_teardown")
            except Exception:  # noqa: BLE001 - cleanup is best-effort
                if not self.session_lost:
                    close_failures.append("backend_teardown")
        context = self.context
        if context is not None:
            try:
                await context.close()
            except Exception as exc:  # noqa: BLE001 - cleanup is best-effort
                # A session whose environment already vanished is clean by
                # definition; only a live session failing to tear down leaks.
                if not self.session_lost:
                    close_failures.append(f"context:{type(exc).__name__}")
        self.closed_ok = not close_failures
        if close_failures:
            self.close_error = ",".join(close_failures)


@dataclass(frozen=True)
class AcquiredBrowser:
    """A launched session plus the exit IP observed through it."""

    session: BrowserSession
    current_ip: Optional[str] = None
