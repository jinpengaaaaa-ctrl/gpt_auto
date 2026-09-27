"""Browser page observation boundary for migrated workflow steps."""

from __future__ import annotations

from dataclasses import dataclass

from gpt_automation.phases import screen_detector
from gpt_automation.phases.screen_detector import ScreenState


async def ensure_screen_detected(page, *, initial_timeout_ms: int = 15000):
    """Resolve the detector at call time so test and diagnostic overrides work."""
    return await screen_detector.ensure_screen_detected(
        page,
        initial_timeout_ms=initial_timeout_ms,
    )


@dataclass(frozen=True)
class PageObservation:
    """Redacted page state suitable for workflow evidence."""

    state: ScreenState
    details: tuple[tuple[str, str], ...]


class ChatGPTPageObserver:
    """Observe the current screen without performing browser actions."""

    def __init__(self, page, *, timeout_ms: int = 15000) -> None:
        self.page = page
        self.timeout_ms = timeout_ms

    async def observe(self) -> PageObservation:
        state = await ensure_screen_detected(
            self.page,
            initial_timeout_ms=self.timeout_ms,
        )
        return PageObservation(
            state=state,
            details=(
                ("screen_state", state.value),
                ("verification_source", "screen_detector"),
            ),
        )

    async def is_state(self, *expected: ScreenState) -> bool:
        observation = await self.observe()
        return observation.state in set(expected)


__all__ = ["ChatGPTPageObserver", "PageObservation"]
