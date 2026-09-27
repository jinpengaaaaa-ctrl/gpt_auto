"""network/ — Xray + iKuai + IP 历史管理."""

from gpt_automation.network.ikuai import IKuaiClient
from gpt_automation.network.xray_controller import XrayController
from gpt_automation.network.proxy_manager import ProxyManager
from gpt_automation.network.ip_history import IPHistory, IPUsageRecord
from gpt_automation.network.rotator import IPRotator, record_ip_usage
from gpt_automation.network.ip_detector import get_ip_from_page, get_exit_ip_http
from gpt_automation.network.strategy import (
    AutomaticNetworkStrategy,
    ManualHotspotNetworkStrategy,
    NetworkStrategy,
    create_network_strategy,
)

__all__ = [
    "IKuaiClient",
    "XrayController",
    "ProxyManager",
    "IPHistory", "IPUsageRecord",
    "IPRotator", "record_ip_usage",
    "get_ip_from_page", "get_exit_ip_http",
    "AutomaticNetworkStrategy", "ManualHotspotNetworkStrategy",
    "NetworkStrategy", "create_network_strategy",
]
