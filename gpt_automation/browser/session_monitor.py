"""Attach crash / unexpected-close monitors to a browser session.

Playwright never tells the business layers that the browser process died
until the next page operation hits ``TargetClosedError``.  These listeners
convert the death into an eager ``session_lost`` flag on the
:class:`BrowserSession`, so cleanup stays idempotent and the workflow can
react to a stable signal instead of racing page probes.
"""

from __future__ import annotations

import logging

from gpt_automation.browser.models import BrowserSession

logger = logging.getLogger(__name__)


def attach_session_loss_monitor(session: BrowserSession) -> None:
    """Register crash/close listeners that flag ``session.session_lost``.

    - ``page.on("crash")`` → ``lost_reason="crash_event"`` (renderer/page died).
    - ``context.on("close")`` → ``lost_reason="browser_closed"`` but only when
      the close was NOT requested by ``session.close()`` itself, so a normal
      teardown never mislabels the session as lost.
    """
    page = session.page
    if page is not None and hasattr(page, "on"):

        def _on_page_crash() -> None:
            logger.error(
                "BROWSER_CRASHED: page crashed "
                "(backend=%s, reason=crash_event)",
                session.backend.value,
            )
            session.mark_session_lost("crash_event")

        page.on("crash", _on_page_crash)
    context = session.context
    if context is not None and hasattr(context, "on"):

        def _on_context_close() -> None:
            if session._closed:
                return
            logger.error(
                "BROWSER_CRASHED: context closed unexpectedly "
                "(backend=%s, reason=browser_closed)",
                session.backend.value,
            )
            session.mark_session_lost("browser_closed")

        context.on("close", _on_context_close)
