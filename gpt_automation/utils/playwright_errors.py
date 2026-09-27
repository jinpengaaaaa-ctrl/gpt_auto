"""Session-loss classification for Playwright browser crashes.

Distinguishes "the browser process / context / page is gone" (a hard
environment failure that must abort probing and surface as
:class:`SessionLostError`) from transient probe failures (selector races,
timeouts) which stay inside ``SAFE_EXCEPTIONS``.

Playwright is imported lazily so test environments without the package keep
working; when it is unavailable every exception is classified as
not-session-lost, preserving the pre-hardening behaviour.
"""

from __future__ import annotations

_CLOSED_MESSAGE_MARKERS = (
    "target page, context or browser has been closed",
    "browser has been closed",
    "target closed",
)


class SessionLostError(RuntimeError):
    """The browser session (process, context or page) has disappeared.

    ``reason`` is a stable, non-sensitive code: ``target_closed``,
    ``browser_closed`` or ``crash_event``.
    """

    def __init__(self, reason: str = "target_closed") -> None:
        super().__init__(reason)
        self.reason = reason


def _playwright_error_types() -> tuple:
    """Return (Error, TimeoutError) from Playwright, or () when unavailable."""
    try:
        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError
    except ImportError:  # pragma: no cover - environment without playwright
        return ()
    return PlaywrightError, PlaywrightTimeoutError


def is_session_lost(exc: BaseException) -> bool:
    """True when *exc* means the browser process/context/page is gone.

    Playwright's ``TimeoutError`` also subclasses ``Error`` but represents a
    transient probe failure, so it is explicitly excluded.  Non-Playwright
    exceptions are never classified as session loss even when their message
    looks like a closed error.
    """
    if isinstance(exc, SessionLostError):
        return True
    error_types = _playwright_error_types()
    if not error_types:
        return False
    PlaywrightError, PlaywrightTimeoutError = error_types
    if not isinstance(exc, PlaywrightError):
        return False
    if isinstance(exc, PlaywrightTimeoutError):
        return False
    message = str(exc).lower()
    return any(marker in message for marker in _CLOSED_MESSAGE_MARKERS)
