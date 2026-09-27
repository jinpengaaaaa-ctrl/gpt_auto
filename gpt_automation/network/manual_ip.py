"""
Manual IP Switch Mode — Phone Hotspot + Airplane Mode Toggle (Feature #11B).

For scenarios where the operator uses a phone hotspot for internet access
and manually toggles airplane mode (off → on) to obtain a new IP after
each account registration.

The only admission rule: the next account's exit IP must differ from the
previous account's IP.  The 7-day IP history is no longer consulted as an
admission rule anywhere (the ledger kept by ``ip_history`` is only a
diagnostic record).

Interaction differences between modes:
* assisted (operator-in-the-loop): polls every ``poll_interval_s`` seconds;
  if the IP has not changed within ``poll_window_s`` (default 9 minutes) the
  operator is asked whether to reuse the previous IP once, keep waiting, or
  cancel the batch.
* unattended (continue_on_blocked): polls forever until the IP changes and
  never reads stdin.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import sys
from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable, Optional

import config
import httpx
from gpt_automation.utils.beep import play_ip_switch_prompt

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ManualIPResult:
    """Outcome of a manual switch interaction."""

    status: str
    baseline_ip: Optional[str] = None
    candidate_ip: Optional[str] = None
    reuse_authorized: bool = False


class DuplicateIPDecision(str, Enum):
    """Operator decision after the detected IP matches the previous account."""

    CONTINUE_REUSE = "continue_reuse"
    RETRY_SWITCH = "retry_switch"
    CANCEL = "cancel"
    INPUT_UNAVAILABLE = "input_unavailable"


async def detect_ip_direct(timeout: float = 15.0) -> Optional[str]:
    """Detect the current exit IP via direct HTTP (no proxy).

    Unlike get_exit_ip_http() which routes through the Xray HTTP proxy,
    this function connects directly to api.ipify.org — suitable for
    manual-hotspot mode where no proxy is running.

    Args:
        timeout: HTTP request timeout in seconds.

    Returns:
        Public IP string, or None on failure.
    """
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            response = await client.get("https://api.ipify.org?format=json")
            if response.status_code == 200:
                data = response.json()
                raw_ip = data.get("ip") if isinstance(data, dict) else None
                if raw_ip:
                    return str(ipaddress.ip_address(str(raw_ip).strip()))
    except (ValueError, TypeError) as exc:
        logger.warning("direct IP detection returned invalid data: %s", exc)
    except Exception as exc:  # noqa: BLE001 - detection must never raise
        logger.warning("direct IP detection failed: %s", exc)
    return None


async def _async_read_line(timeout: Optional[float] = None) -> Optional[str]:
    """Read one line from stdin without treating EOF as Enter.

    Returns ``None`` for EOF or an input error, ``""`` for a real blank line,
    and propagates cancellation so Ctrl+C reaches the batch owner.
    """
    loop = asyncio.get_event_loop()
    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(None, sys.stdin.readline),
            timeout=timeout,
        )
        return result if result != "" else None
    except asyncio.TimeoutError:
        return None
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - input channel closed
        return None


class ManualIPSwitcher:
    """Manual IP switch handler — operator-in-the-loop IP rotation.

    The first account may use the current IP directly.  Later accounts
    require the operator to toggle phone airplane mode; the exit IP is
    polled every ``poll_interval_s`` seconds until it differs from the
    previous account's IP.  ``unattended=True`` (continuous batches) never
    reads stdin and never reuses an IP; assisted mode pauses for a human
    decision after ``poll_window_s`` without a change.
    """

    def __init__(
        self,
        input_fn: Optional[Callable[[str, str], Awaitable[Optional[str]]]] = None,
        unattended: bool = False,
    ):
        self.cfg = config.MANUAL_MODE_CONFIG
        self.unattended = unattended
        # ``input_fn`` lets a control surface (e.g. the local dashboard) answer
        # prompts from the browser instead of blocking on stdin.  It receives
        # the prompt text and a ``keys`` hint describing the available CLI
        # choices (use/retry/continue/switch/reuse/cancel) and must return the
        # same value the CLI would read (``""``, ``"s"``, ``"c"``, ``"q"``).
        self._input_fn = input_fn

    async def _read_line(self, prompt: str = "", keys: str = "") -> Optional[str]:
        """Read one operator line, optionally routed to a web confirmer."""
        if self._input_fn is None:
            if prompt:
                print(prompt, end=" ", flush=True)
            return await _async_read_line(timeout=None)
        return await self._input_fn(prompt, keys)

    async def prepare_initial_switch(self) -> ManualIPResult:
        """Use the current IP by default, or explicitly start a switch."""
        if self.unattended:
            return await self._prepare_initial_unattended()
        timeout = float(self.cfg.get("ip_check_timeout", 8.0))
        poll_interval = float(self.cfg.get("poll_interval_s", 10.0))
        baseline: Optional[str] = None

        # 网络未恢复时自动等待并持续检测，不打扰操作员。
        while True:
            baseline = await detect_ip_direct(timeout=timeout)
            if baseline:
                break
            print(
                "  网络尚未恢复，正在自动检测"
                f"（每 {poll_interval:g} 秒一次，无需人工输入）..."
            )
            await asyncio.sleep(poll_interval)

        # IP 历史不参与准入：当前 IP 可直接使用（或按 S 主动切换）。
        print(f"\n  ✅ 当前检测 IP 可用：{baseline}")
        print("  按 Enter 直接使用当前 IP（默认），输入 S 切换 IP，输入 Q 取消。")
        while True:
            answer = await self._read_line(
                "当前检测 IP 可用，可直接使用当前 IP、切换 IP，或取消。",
                "use|switch|cancel",
            )
            if answer is None:
                return ManualIPResult("input_unavailable", candidate_ip=baseline)
            choice = answer.strip().lower()
            if choice in {"", "y", "yes"}:
                print(f"\n  ✅ 将直接使用当前 IP：{baseline}")
                return ManualIPResult("success", candidate_ip=baseline)
            if choice in {"s", "switch"}:
                return await self.wait_for_ip_switch(baseline)
            if choice in {"q", "quit"}:
                print("\n  已取消手动 IP 准备，批次将安全终止。")
                return ManualIPResult("cancelled", candidate_ip=baseline)
            print("  无效输入：按 Enter 直接使用，输入 S 切换 IP，输入 Q 取消。")

    async def _prepare_initial_unattended(self) -> ManualIPResult:
        """Unattended batch start: use any reachable public IP, never prompt.

        The first account has no predecessor, so any detected IP is accepted
        without any gate.
        """
        poll_interval = float(self.cfg.get("poll_interval_s", 10.0))
        timeout = float(self.cfg.get("ip_check_timeout", 8.0))
        while True:
            baseline = await detect_ip_direct(timeout=timeout)
            if baseline:
                print(f"\n  ✅ 连续模式自动使用当前检测 IP：{baseline}")
                return ManualIPResult("success", candidate_ip=baseline)
            logger.info(
                "manual_ip: unattended initial IP detection failed, retrying",
            )
            await asyncio.sleep(poll_interval)

    async def wait_for_ip_switch(
        self,
        previous_ip: Optional[str],
    ) -> ManualIPResult:
        """Auto-poll for a manually changed IP without operator keystrokes.

        The operator toggles airplane mode on the phone hotspot; this method
        automatically polls the exit IP every ``poll_interval_s`` seconds.
        An IP that differs from ``previous_ip`` is accepted immediately — the
        7-day history is never consulted.

        Assisted mode pauses for the operator when the IP has not changed
        within ``poll_window_s`` seconds (default 9 minutes): reuse the
        previous IP once, keep waiting, or cancel.  Unattended mode never
        pauses and never reuses an IP; it keeps polling until the IP changes.
        """
        baseline_ip = previous_ip
        poll_interval = float(self.cfg.get("poll_interval_s", 10.0))
        poll_window = float(self.cfg.get("poll_window_s", 540.0))
        timeout = float(self.cfg.get("ip_check_timeout", 8.0))

        play_ip_switch_prompt()
        self._print_banner(baseline_ip)

        loop = asyncio.get_event_loop()
        window_deadline = (
            loop.time() + poll_window if not self.unattended else None
        )
        tick = 0
        next_tick = loop.time() + poll_interval

        while True:
            tick += 1
            current_ip = await detect_ip_direct(timeout=timeout)

            if current_ip is None:
                logger.info(
                    "manual_ip: detection failed on poll tick %d, continuing",
                    tick,
                )
            elif current_ip != baseline_ip:
                # 相邻 IP 不同即通过 —— IP 历史不参与准入。
                print(
                    f"\n  ✅ IP 切换已自动确认："
                    f"{baseline_ip or '(无基准)'} → {current_ip}"
                )
                return ManualIPResult("success", baseline_ip, current_ip)

            # 同一 IP 或检测失败：继续等待。连续模式永不提示。
            if self.unattended:
                next_tick = await self._pace_tick(loop, next_tick, poll_interval)
                continue

            if loop.time() >= window_deadline:
                play_ip_switch_prompt()
                decision = await self._prompt_no_change_decision(
                    current_ip,
                    has_baseline=bool(previous_ip),
                )
                if decision is DuplicateIPDecision.CONTINUE_REUSE:
                    reuse_ip = current_ip or baseline_ip
                    print(
                        f"  ⚠️ 已授权下一个账户继续使用 IP {reuse_ip}，"
                        "该授权仅生效一次。"
                    )
                    return ManualIPResult(
                        "success",
                        baseline_ip,
                        reuse_ip,
                        reuse_authorized=True,
                    )
                if decision is DuplicateIPDecision.CANCEL:
                    print("\n  已取消手动 IP 切换，批次将安全终止。")
                    return ManualIPResult("cancelled", baseline_ip)
                if decision is DuplicateIPDecision.INPUT_UNAVAILABLE:
                    print("\n  ❌ 输入通道已关闭，无法确认 IP 处理方式。")
                    return ManualIPResult("input_unavailable", baseline_ip)
                # Operator confirmed to keep waiting — reset the window.
                window_deadline = loop.time() + poll_window
                continue

            next_tick = await self._pace_tick(loop, next_tick, poll_interval)

    async def _prompt_no_change_decision(
        self,
        current_ip: Optional[str],
        has_baseline: bool = True,
    ) -> DuplicateIPDecision:
        """Ask how to proceed after the polling window expired without a change.

        While offline (``current_ip`` is None) the exit IP cannot be verified,
        so reusing the previous IP is never offered.
        """
        offline = current_ip is None
        window_s = int(self.cfg.get("poll_window_s", 540.0))
        if offline:
            print(
                f"\n  ⏳ 已等待 {window_s} 秒，"
                "网络尚未恢复，无法确认出口 IP。"
            )
        else:
            print(
                f"\n  ⏳ 已等待 {window_s} 秒，"
                f"IP 未发生变化（当前：{current_ip}）。"
            )
            if has_baseline:
                print("  [C] 使用上一 IP 继续（仅授权下一个账户一次）")
        print(
            f"  [Enter/R] 继续等待（重新开始 "
            f"{window_s / 60:g} 分钟窗口）"
        )
        print("  [Q] 取消当前批次")
        while True:
            answer = await self._read_line(
                (
                    "网络尚未恢复：可继续等待，或取消。"
                    if offline
                    else "IP 长时间未变化：可继续使用、继续等待，或取消。"
                ),
                "continue|cancel" if offline else "continue|reuse|cancel",
            )
            if answer is None:
                return DuplicateIPDecision.INPUT_UNAVAILABLE
            choice = answer.strip().lower()
            if (
                not offline
                and has_baseline
                and choice in {"c", "continue", "reuse", "use"}
            ):
                return DuplicateIPDecision.CONTINUE_REUSE
            if choice in {"", "r", "retry", "wait", "y", "yes"}:
                return DuplicateIPDecision.RETRY_SWITCH
            if choice in {"q", "quit"}:
                return DuplicateIPDecision.CANCEL
            if offline:
                print("  无效输入：按 Enter/R 继续等待，输入 Q 取消。")
            else:
                print("  无效输入：输入 C 继续使用，按 Enter/R 继续等待，输入 Q 取消。")

    # ── Internal helpers ──

    async def _pace_tick(
        self,
        loop: asyncio.AbstractEventLoop,
        next_tick: float,
        interval: float,
    ) -> float:
        """Sleep to the fixed polling cadence; return the next tick deadline.

        Probes start on a steady beat even when a single detection took a long
        time (e.g. an HTTP timeout while the network is down), instead of
        sleeping a full interval after the request finished.
        """
        now = loop.time()
        if now < next_tick:
            await asyncio.sleep(next_tick - now)
        return next_tick + interval

    def _print_banner(self, previous_ip: Optional[str]) -> None:
        """Print the visual banner for the auto-polling IP switch."""
        ip_info = f"  切换前 IP: {previous_ip}" if previous_ip else ""
        poll_interval = float(self.cfg.get("poll_interval_s", 10.0))
        if self.unattended:
            print(f"""
╔══════════════════════════════════════════════════════════════╗
║  📱 请切换手机飞行模式（开→关），系统自动检测 IP...        ║
║     每 {poll_interval:g} 秒自动检测一次 IP 变化                       ║
║     只要与上一个账户 IP 不同，无需人工确认                         ║
║     IP 未变化时将一直等待，不需要人工输入                          ║
║  {ip_info:<58s} ║
╚══════════════════════════════════════════════════════════════╝
""")
            return
        poll_window = float(self.cfg.get("poll_window_s", 300.0))
        print(f"""
╔══════════════════════════════════════════════════╗
║  📱 请切换手机飞行模式（开→关），            ║
║     系统每 {poll_interval:g} 秒自动检测 IP 变化...   ║
║     最长等待 {poll_window / 60:g} 分钟，之后需人工确认  ║
║                                              ║
║  {ip_info:<36s} ║
╚══════════════════════════════════════════════════╝
""")
