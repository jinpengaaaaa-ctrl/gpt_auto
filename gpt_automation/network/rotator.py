"""
IP 轮换编排器 — Xray 直控 + Windows 代理 + 8 步注册流水线 (Feature #11A refactored).

替代原有的 v2rayN + iKuai 组合方式，直接管理 Xray 进程 + Windows 系统代理，
实现 Step 1-7 的注册前后网络编排流水线。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

import config
from gpt_automation.network.ikuai import IKuaiClient
from gpt_automation.network.ip_history import IPHistory
from gpt_automation.network.xray_controller import XrayController
from gpt_automation.utils.redaction import mask_email
from gpt_automation.network.proxy_manager import ProxyManager
from gpt_automation.network.ip_detector import get_exit_ip_http
from gpt_automation.utils.exceptions import SAFE_EXCEPTIONS

logger = logging.getLogger(__name__)


class IPRotator:
    """组合 Xray 直控 + Windows 代理 + iKuai ADSL 重拨.

    实现 8 步注册流水线（Step 1-7 在本类中，Step 4 由外层 orchestrator 执行，
    Step 8 由主循环隐式驱动）。
    """

    def __init__(
        self,
        history: Optional[IPHistory] = None,
        xray: Optional[XrayController] = None,
        proxy: Optional[ProxyManager] = None,
        ikuai: Optional[IKuaiClient] = None,
        mode: Optional[str] = None,
    ) -> None:
        mode = mode or config.IP_ROTATION_CONFIG.get("rotation_mode", "ikuai-single")

        self.history: IPHistory = history or IPHistory()

        if mode == "manual-hotspot":
            # 手动模式：Xray/代理/iKuai 都不需要，用占位对象
            self.xray: XrayController = xray or XrayController()
            self.proxy: ProxyManager = proxy or ProxyManager()
            self.ikuai: IKuaiClient = ikuai or IKuaiClient()
            self._mode: str = "manual-hotspot"
            logger.info("IPRotator: initialized in manual-hotspot mode (no Xray/proxy)")
        else:
            self.xray: XrayController = xray or XrayController()
            self.proxy: ProxyManager = proxy or ProxyManager()
            self.ikuai: IKuaiClient = ikuai or IKuaiClient()
            self._mode: str = mode

    # ------------------------------------------------------------------
    # 8-step pipeline
    # ------------------------------------------------------------------

    async def run_pipeline(self, email: str) -> Dict[str, Any]:
        """执行 Step 1-3 流水线：启动 Xray → 启用代理 → 验证 IP.

        Step 4（注册）由外层 orchestrator 调用 process_single_registration() 完成。
        Step 5-7（关闭代理、杀 Xray、iKuai 重拨）统一由外层 orchestrator 调用 cleanup() 执行。

        Args:
            email: 目标邮箱（用于日志记录）。

        Returns:
            {"status": str, "ip": Optional[str]}
            status: "ready" | "XRAY_START_FAILED" | "PROXY_ENABLE_FAILED" | "IP_VERIFY_FAILED"
        """
        # 手动模式：跳过 Xray/代理流水线
        if getattr(self, "_mode", None) == "manual-hotspot":
            logger.info("rotator: manual-hotspot mode — skipping pipeline steps 1-3")
            return {"status": "ready", "ip": None}

        # Step 1: Kill old Xray + start new one
        if not await self._step1_start_xray():
            logger.error("rotator: step 1 failed — xray start")
            return {"status": "XRAY_START_FAILED", "ip": None}

        # Step 2: Enable system proxy
        if not self._step2_enable_proxy():
            logger.error("rotator: step 2 failed — proxy enable")
            return {"status": "PROXY_ENABLE_FAILED", "ip": None}

        # Step 3: Verify IP with retries
        ip = await self._step3_verify_ip()
        if ip is None:
            logger.error("rotator: step 3 failed — IP verify exhausted")
            return {"status": "IP_VERIFY_FAILED", "ip": None}

        logger.info("rotator: pipeline ready for %s, ip=%s", mask_email(email), ip)
        return {"status": "ready", "ip": ip}

    # ------------------------------------------------------------------
    # Private step methods
    # ------------------------------------------------------------------

    async def _step1_start_xray(self) -> bool:
        """Step 1: Kill existing Xray, load config, start new Xray.

        Returns:
            True if Xray started successfully.
        """
        await self.xray.kill_existing()
        config_data = self.xray.load_config()
        if not config_data:
            logger.warning("rotator: xray config is empty, continuing anyway")
        return await self.xray.start()

    def _step2_enable_proxy(self) -> bool:
        """Step 2: Enable Windows system proxy.

        Returns:
            True if proxy enabled successfully.
        """
        return self.proxy.enable()

    async def _step3_verify_ip(self) -> Optional[str]:
        """Step 3: Get the exit IP via HTTP. Retry up to max_retries.

        On each failed attempt, restarts Xray before retrying.

        Returns:
            An IP string, or None if all retries exhausted.
        """
        timeout_val = config.IP_CHECK_CONFIG["timeout"]
        max_retries_val = config.IP_CHECK_CONFIG["max_retries"]

        for attempt in range(1, max_retries_val + 1):
            ip = await get_exit_ip_http(timeout=timeout_val)
            if ip:
                logger.info("rotator: exit IP verified: %s (attempt %d)", ip, attempt)
                return ip
            logger.warning("rotator: IP detection failed (attempt %d/%d)",
                           attempt, max_retries_val)
            # Restart Xray before next attempt
            if attempt < max_retries_val:
                await self.xray.restart()

        logger.error("rotator: IP verify exhausted after %d retries", max_retries_val)
        return None

    async def _step5_disable_proxy(self) -> bool:
        """Step 5: Disable Windows system proxy.

        Returns:
            True if proxy disabled successfully.
        """
        return self.proxy.disable()

    async def _step6_kill_xray(self) -> bool:
        """Step 6: Kill Xray process.

        Returns:
            True if Xray stopped successfully.
        """
        return await self.xray.stop()

    async def _step7_ikuai_redial(self) -> bool:
        """Step 7: Redial target ADSL via iKuai.

        Returns:
            True if redial succeeded.
        """
        try:
            result = await self.ikuai.redial_target_wan()
            logger.info("rotator: ikuai redial done")
            return result
        except SAFE_EXCEPTIONS as exc:
            logger.warning("rotator: ikuai redial failed: %s", exc)
            return False

    def record_usage(self, ip: Optional[str], email: str) -> bool:
        """Record IP usage into history and report persistence success."""
        return self.history.record_usage(ip or "", email)

    # ------------------------------------------------------------------
    # Public cleanup interface
    # ------------------------------------------------------------------

    async def cleanup(self) -> None:
        """执行 Steps 5-7 的清理流程：关闭代理 + 终止 Xray + iKuai 重拨。

        外部调用方统一使用此方法，禁止直接访问私有方法。
        每一步独立 try/except，确保某步失败不阻塞后续清理。
        手动模式（manual-hotspot）跳过所有清理步骤。
        """
        if getattr(self, "_mode", None) == "manual-hotspot":
            logger.info("rotator: manual-hotspot mode — skipping cleanup steps 5-7")
            return

        # Step 5: 关闭 Windows 系统代理
        try:
            if not await self._step5_disable_proxy():
                logger.warning("cleanup: step 5 (disable proxy) returned False")
        except Exception as exc:
            logger.warning("cleanup: step 5 (disable proxy) failed: %s", exc)

        # Step 6: 终止 Xray 进程
        try:
            if not await self._step6_kill_xray():
                logger.warning("cleanup: step 6 (kill xray) returned False")
        except Exception as exc:
            logger.warning("cleanup: step 6 (kill xray) failed: %s", exc)

        # Step 7: iKuai ADSL 重拨
        try:
            if not await self._step7_ikuai_redial():
                logger.warning("cleanup: step 7 (ikuai redial) returned False")
        except Exception as exc:
            logger.warning("cleanup: step 7 (ikuai redial) failed: %s", exc)

    async def force_ip_change(self) -> bool:
        """强制触发 IP 变更：iKuai 重拨 + Xray 重启.

        供 ip_guard 在检测到 IP 未变化时调用，主动触发换 IP。
        与 cleanup() 的区别：不关闭系统代理（代理由 cleanup 统一管理）。

        Returns:
            True if both redial and restart succeeded.
        """
        if getattr(self, "_mode", None) == "manual-hotspot":
            logger.info("rotator: manual-hotspot mode — skipping force_ip_change")
            return False

        success = True

        # iKuai ADSL 重拨
        try:
            if not await self._step7_ikuai_redial():
                logger.warning("force_ip_change: iKuai redial failed")
                success = False
        except Exception as exc:
            logger.warning("force_ip_change: iKuai redial exception: %s", exc)
            success = False

        # 等待 ADSL 线路稳定
        await asyncio.sleep(2.0)

        # 重启 Xray（重连上游节点，获取新出口 IP）
        try:
            if not await self.xray.restart():
                logger.warning("force_ip_change: Xray restart failed")
                success = False
        except Exception as exc:
            logger.warning("force_ip_change: Xray restart exception: %s", exc)
            success = False

        # 等待 Xray 启动完成
        await asyncio.sleep(float(config.IP_ROTATION_CONFIG.get("startup_delay", 3.0)))

        return success

    # ------------------------------------------------------------------
    # Resource cleanup
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Release all resources: Xray process + iKuai session."""
        if getattr(self, "_mode", None) == "manual-hotspot":
            return  # Nothing to close in manual mode
        await self.xray.close()
        await self.ikuai.close()


# ═══════════════════════════════════════════════════════════════
# Module-level convenience
# ═══════════════════════════════════════════════════════════════


def record_ip_usage(ip: Optional[str], email: str) -> bool:
    """Module-level convenience for the registration loop."""
    return IPHistory().record_usage(ip or "", email)
