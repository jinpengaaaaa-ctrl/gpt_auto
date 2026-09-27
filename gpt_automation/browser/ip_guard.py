"""IP rotation guard shared by every browser backend (Feature #11).

Acquires a browser session whose exit IP satisfies the only admission rule:
the IP must differ from the previous account's IP, unless the operator has
authorized a one-shot reuse of that exact IP (assisted mode only).  The
7-day IP history and the batch ``used_ips`` set are no longer admission
rules.  The browser backend is injected; this module never selects a
backend itself.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Optional, Set

import config
from gpt_automation.browser.models import BrowserLaunchRequest
from gpt_automation.browser.provider import BrowserProvider, create_browser_provider

logger = logging.getLogger(__name__)


# Typed outcomes of a browser-IP acquisition attempt.
BROWSER_READY = "READY"
BROWSER_IP_UNCHANGED = "IP_UNCHANGED"
BROWSER_IP_DETECTION_FAILED = "IP_DETECTION_FAILED"
BROWSER_LAUNCH_FAILED = "BROWSER_LAUNCH_FAILED"
BROWSER_MANUAL_SWITCH_FAILED = "MANUAL_SWITCH_FAILED"


@dataclass(frozen=True)
class BrowserAcquireResult:
    """Outcome of a browser/IP acquisition attempt.

    ``READY``: browser is running and its exit IP satisfies the active rule.
    ``IP_UNCHANGED``: manual-hotspot only - the exit IP still equals the
        previous account's IP; no account work has started, retry after the
        next poll.
    ``IP_DETECTION_FAILED``: manual-hotspot only - no public IP could be
        detected for the launched session; retryable.
    ``BROWSER_LAUNCH_FAILED``: the browser backend could not be launched
        (environment error); do not loop forever, surface to the batch.
    ``MANUAL_SWITCH_FAILED``: the manual IP switch interaction ended without
        a usable IP (cancelled / input unavailable / retries exhausted).
    """

    status: str
    browser: Any = None
    page: Any = None
    current_ip: Optional[str] = None

    @property
    def ready(self) -> bool:
        return self.status == BROWSER_READY


async def ensure_ip_rotated(
    rotator: Any,
    last_ip: Optional[str],
    used_ips: Set[str],
    profile_id: str,
    manual_hotspot: bool,
    allowed_reuse_ip: Optional[str] = None,
    provider: Optional[BrowserProvider] = None,
) -> BrowserAcquireResult:
    """Spin up a browser session whose IP satisfies the adjacent-IP rule.

    Args:
        rotator:   IPRotator instance used to trigger iKuai redial + Xray
                   restart when the detected IP has not changed (automatic
                   mode only; manual-hotspot skips the force change).
        last_ip:   IP used by the previous account in batch.
        used_ips:  Set of all IPs already used in this batch.  Retained for
                   batch-level statistics; no longer an admission rule.
        profile_id: Identifier for the browser profile directory.
        manual_hotspot: True when the caller selected the manual-hotspot
            network strategy.  The guard never re-reads the global
            ``rotation_mode`` config - the strategy is the only source of
            truth for the rotation mode.
        allowed_reuse_ip: One-shot operator-authorized IP reuse for the next
            account (assisted mode only): the browser may use the same IP as
            the previous account if and only if it equals this value.
        provider:  BrowserProvider used to launch the session.  Defaults to
            CloakBrowserProvider so existing callers keep their behavior.

    Returns:
        A ``BrowserAcquireResult``.  ``browser`` is a BrowserSession (or a
        legacy Playwright context); both expose ``close()``.  Non-READY
        results carry ``browser=None``.
    """
    browser_provider = provider or create_browser_provider(None)
    cfg = config.IP_ROTATION_CONFIG
    need_force_change = False

    is_manual = manual_hotspot
    max_retries = 2 if is_manual else int(cfg["max_retries"])
    # Remember the last failure reason so an exhausted retry loop reports the
    # real problem (launch failure / unchanged IP / undetectable IP) instead
    # of always claiming the browser failed to launch.
    last_failure: Optional[str] = None
    last_failure_ip: Optional[str] = None

    for attempt in range(1, max_retries + 1):
        if need_force_change:
            if is_manual:
                # The operator switches the hotspot manually; the batch just
                # waits for the next prompt, so no programmatic change here.
                logger.info("manual-hotspot: skipping force_ip_change (attempt %d)", attempt)
            else:
                logger.info("triggering iKuai redial + Xray restart (attempt %d)", attempt)
                try:
                    await rotator.force_ip_change()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("force_ip_change failed: %s", exc)
            need_force_change = False

        try:
            acquired = await browser_provider.launch(
                BrowserLaunchRequest(
                    account_id=profile_id,
                    profile_id=profile_id,
                )
            )
            browser = acquired.session
            page = browser.page
            current_ip = acquired.current_ip
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - launch is broad on purpose
            logger.warning("browser launch failed (attempt %d): %s", attempt, exc)
            last_failure = BROWSER_LAUNCH_FAILED
            last_failure_ip = None
            await asyncio.sleep(float(cfg["retry_delay"]))
            continue

        if not current_ip:
            await close_browser(browser)
            if is_manual:
                logger.info(
                    "manual: IP detection failed (attempt %d), will retry",
                    attempt,
                )
                return BrowserAcquireResult(BROWSER_IP_DETECTION_FAILED)
            logger.warning("IP detection failed (attempt %d)", attempt)
            last_failure = BROWSER_IP_DETECTION_FAILED
            last_failure_ip = None
            continue

        reuse_authorized = bool(allowed_reuse_ip and current_ip == allowed_reuse_ip)

        if last_ip and current_ip == last_ip and not reuse_authorized:
            if is_manual:
                logger.info(
                    "manual: IP %s unchanged from last account, waiting",
                    current_ip,
                )
                await close_browser(browser)
                return BrowserAcquireResult(
                    BROWSER_IP_UNCHANGED,
                    current_ip=current_ip,
                )
            logger.info("IP %s == last, retrying (%d/%d)",
                        current_ip, attempt, cfg["max_retries"])
            await close_browser(browser)
            last_failure = BROWSER_IP_UNCHANGED
            last_failure_ip = current_ip
            need_force_change = True
            continue

        if reuse_authorized:
            logger.warning(
                "operator-authorized one-time IP reuse: %s (attempt %d)",
                current_ip,
                attempt,
            )
        else:
            logger.info("✅ IP rotated: %s → %s (attempt %d)",
                        last_ip, current_ip, attempt)
        return BrowserAcquireResult(BROWSER_READY, browser, page, current_ip)

    return BrowserAcquireResult(
        last_failure or BROWSER_LAUNCH_FAILED,
        current_ip=last_failure_ip,
    )


async def close_browser(browser: Any) -> bool:
    """Close the browser session/context if it's still alive. Never raises.

    Returns whether the shutdown was clean.  A modern ``BrowserSession``
    carries ``closed_ok``; legacy contexts without that attribute are treated
    as clean only when ``close()`` did not raise.  A session flagged
    ``session_lost`` (browser process already gone) is always clean: there is
    nothing left to leak, so it must not pollute the BROWSER_CLOSE_FAILED
    circuit breaker.  The result must reach the batch scheduler so repeated
    Firefox process leaks trigger a circuit breaker instead of being silently
    ignored.
    """
    if browser is None:
        return True
    session_lost = bool(getattr(browser, "session_lost", False))
    try:
        await browser.close()
    except Exception:  # noqa: BLE001 - cleanup is best-effort
        return session_lost
    closed_ok = getattr(browser, "closed_ok", True)
    if closed_ok is False:
        return session_lost
    return True
