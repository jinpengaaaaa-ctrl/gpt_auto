"""One network lifecycle shared by manual and automatic registration modes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Protocol, Set

import config
from gpt_automation.browser.ip_guard import (
    BROWSER_IP_DETECTION_FAILED,
    BROWSER_IP_UNCHANGED,
    BROWSER_MANUAL_SWITCH_FAILED,
    BROWSER_READY,
    BrowserAcquireResult,
)
from gpt_automation.browser.provider import BrowserProvider, create_browser_provider
from gpt_automation.network.manual_ip import (
    ManualIPResult,
    ManualIPSwitcher,
)
from gpt_automation.network.rotator import IPRotator


@dataclass(frozen=True)
class NetworkPreparation:
    status: str
    ip: Optional[str] = None

    @property
    def ready(self) -> bool:
        return self.status == "ready"


class NetworkStrategy(Protocol):
    async def prepare_batch(self) -> Optional[ManualIPResult]: ...
    async def prepare_account(self, email: str) -> NetworkPreparation: ...
    async def acquire_browser(
        self,
        previous_ip: Optional[str],
        used_ips: Set[str],
        profile_id: str,
    ) -> BrowserAcquireResult: ...
    async def finish_account(self) -> None: ...
    async def prepare_next(
        self,
        previous_ip: Optional[str],
    ) -> Optional[ManualIPResult]: ...
    async def close(self) -> None: ...


class AutomaticNetworkStrategy:
    """Automatic Xray/proxy/iKuai network lifecycle."""

    def __init__(
        self,
        rotator: Optional[IPRotator] = None,
        browser_provider: Optional[BrowserProvider] = None,
    ) -> None:
        self.rotator = rotator or IPRotator()
        self.browser_provider = browser_provider or create_browser_provider(None)

    async def prepare_batch(self) -> Optional[ManualIPResult]:
        return None

    async def prepare_account(self, email: str) -> NetworkPreparation:
        result = await self.rotator.run_pipeline(email)
        return NetworkPreparation(result["status"], result.get("ip"))

    async def acquire_browser(
        self,
        previous_ip: Optional[str],
        used_ips: Set[str],
        profile_id: str,
    ) -> BrowserAcquireResult:
        from gpt_automation.browser.ip_guard import ensure_ip_rotated

        return await ensure_ip_rotated(
            self.rotator,
            previous_ip,
            used_ips,
            profile_id,
            manual_hotspot=False,
            provider=self.browser_provider,
        )

    async def finish_account(self) -> None:
        await self.rotator.cleanup()

    async def prepare_next(
        self,
        previous_ip: Optional[str],
    ) -> Optional[ManualIPResult]:
        return None

    async def close(self) -> None:
        await self.rotator.close()


class ManualHotspotNetworkStrategy(AutomaticNetworkStrategy):
    """Manual hotspot prompts with the same browser/IP validation contract.

    ``unattended=True`` powers continuous (continue_on_blocked) batches: the
    switch poll never reads stdin, never reuses an IP and loops until the
    exit IP differs from the previous account's IP.  Assisted mode (False)
    keeps the operator-in-the-loop prompts and the one-shot "reuse the
    previous IP" authorization.
    """

    def __init__(
        self,
        rotator: Optional[IPRotator] = None,
        switcher: Optional[ManualIPSwitcher] = None,
        browser_provider: Optional[BrowserProvider] = None,
        unattended: bool = False,
    ) -> None:
        super().__init__(
            rotator=rotator or IPRotator(mode="manual-hotspot"),
            browser_provider=browser_provider,
        )
        self.unattended = unattended
        self.switcher = switcher or ManualIPSwitcher(unattended=self.unattended)
        self._pending_reuse_ip: Optional[str] = None

    async def prepare_batch(self) -> Optional[ManualIPResult]:
        result = await self.switcher.prepare_initial_switch()
        # The first account must inherit an operator-authorized one-shot IP
        # reuse exactly like accounts after prepare_next().  Without this the
        # guard rejects the very IP the operator just authorized and the
        # batch aborts with initial_ip_failed.
        self._pending_reuse_ip = (
            result.candidate_ip
            if result.status == "success" and result.reuse_authorized
            else None
        )
        return result

    async def prepare_account(self, email: str) -> NetworkPreparation:
        return NetworkPreparation("ready")

    async def finish_account(self) -> None:
        return None

    async def prepare_next(
        self,
        previous_ip: Optional[str],
    ) -> Optional[ManualIPResult]:
        result = await self.switcher.wait_for_ip_switch(previous_ip)
        self._pending_reuse_ip = (
            result.candidate_ip
            if result.status == "success" and result.reuse_authorized
            else None
        )
        return result

    async def acquire_browser(
        self,
        previous_ip: Optional[str],
        used_ips: Set[str],
        profile_id: str,
    ) -> BrowserAcquireResult:
        from gpt_automation.browser.ip_guard import ensure_ip_rotated

        # Unified loop: keep polling until the browser's real exit IP differs
        # from the previous account's IP (or the operator authorized a one-shot
        # reuse of that exact IP).  Never returns a failure for "IP did not
        # change yet"; only genuine environment errors (browser launch
        # failures) and a failed manual switch interaction surface to the
        # batch.
        while True:
            acquired = await ensure_ip_rotated(
                self.rotator,
                previous_ip,
                used_ips,
                profile_id,
                allowed_reuse_ip=self._pending_reuse_ip,
                manual_hotspot=True,
                provider=self.browser_provider,
            )
            if acquired.status == BROWSER_READY:
                # 一次性授权只覆盖下一个账户：成功后立即清除。
                self._pending_reuse_ip = None
                return acquired
            if acquired.status in (
                BROWSER_IP_UNCHANGED,
                BROWSER_IP_DETECTION_FAILED,
            ):
                # No account work has started; wait for a different IP and try
                # again.  Unattended mode never prompts; assisted mode may
                # authorize a one-shot reuse of the previous IP.
                switch_result = await self.switcher.wait_for_ip_switch(
                    previous_ip
                )
                if switch_result.status != "success":
                    self._pending_reuse_ip = None
                    return BrowserAcquireResult(
                        BROWSER_MANUAL_SWITCH_FAILED,
                        current_ip=switch_result.candidate_ip,
                    )
                if switch_result.reuse_authorized:
                    self._pending_reuse_ip = switch_result.candidate_ip
                continue
            # BROWSER_LAUNCH_FAILED —— 环境错误，上抛给批次。
            self._pending_reuse_ip = None
            return acquired


def create_network_strategy(
    mode: Optional[str] = None,
    browser_provider: Optional[BrowserProvider] = None,
    unattended: bool = False,
) -> NetworkStrategy:
    selected = mode or config.IP_ROTATION_CONFIG.get(
        "rotation_mode",
        "ikuai-single",
    )
    if selected == "manual-hotspot":
        return ManualHotspotNetworkStrategy(
            browser_provider=browser_provider,
            unattended=unattended,
        )
    return AutomaticNetworkStrategy(browser_provider=browser_provider)
