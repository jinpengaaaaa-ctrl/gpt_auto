"""Browser provider protocol and factory.

Only this module and ``ip_guard`` may select concrete providers.  Business
layers receive the already-acquired ``BrowserSession`` and never branch on
the browser backend.
"""

from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

from gpt_automation.browser.models import (
    AcquiredBrowser,
    BackendAvailability,
    BackendReport,
    BrowserBackend,
    BrowserLaunchRequest,
)


@runtime_checkable
class BrowserProvider(Protocol):
    """Launch contract implemented by each browser backend."""

    backend: BrowserBackend

    def preflight(self) -> BackendReport: ...

    async def launch(self, request: BrowserLaunchRequest) -> AcquiredBrowser: ...


def normalize_backend(backend: str | BrowserBackend | None) -> BrowserBackend:
    """Coerce a backend value into a stable enum; None means CloakBrowser."""
    if backend is None:
        return BrowserBackend.CLOAK
    if isinstance(backend, BrowserBackend):
        return backend
    key = str(backend).strip().lower()
    try:
        return BrowserBackend(key)
    except ValueError as exc:
        raise ValueError(
            f"unknown browser_backend: {backend!r} "
            f"(supported: {[item.value for item in BrowserBackend]})"
        ) from exc


def create_browser_provider(
    backend: str | BrowserBackend | None,
) -> BrowserProvider:
    """Build the provider selected by ``backend`` (default CloakBrowser)."""
    selected = normalize_backend(backend)
    if selected is BrowserBackend.CLOAK:
        from gpt_automation.browser.engine import CloakBrowserProvider

        return CloakBrowserProvider()
    if selected is BrowserBackend.FIREFOX_PRIVATE:
        from gpt_automation.browser.firefox_private import (
            FirefoxPrivateProvider,
        )

        return FirefoxPrivateProvider()
    raise ValueError(f"unsupported browser backend: {selected.value}")


def unavailable_report(
    backend: BrowserBackend,
    detail: str,
) -> BackendReport:
    """Build a consistent MISSING_BINARY report for preflight failures."""
    return BackendReport(
        backend=backend,
        availability=BackendAvailability.MISSING_BINARY,
        detail=detail,
    )
