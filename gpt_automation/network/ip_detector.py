"""
出口 IP 检测模块 — 从 CloakBrowser 页面获取真实公网出口 IP.
"""

import json
from typing import Optional
import httpx
import config


async def get_exit_ip_http(timeout: float = 8.0) -> Optional[str]:
    """独立的 HTTP IP 检测（无需浏览器）.

    通过 Xray HTTP 入站代理请求 api.ipify.org 获取当前公网出口 IP。
    用于 Step 3 流水线中快速验证代理链路的出口 IP。

    Args:
        timeout: HTTP 请求超时秒数（默认 8.0）。

    Returns:
        公网 IP 字符串，失败时返回 None。
    """
    try:
        http_proxy_port = config.PROXY_CONFIG["port"] + 1
        proxy_url = f"http://{config.PROXY_CONFIG['host']}:{http_proxy_port}"
        async with httpx.AsyncClient(timeout=timeout, proxy=proxy_url) as client:
            response = await client.get("https://api.ipify.org?format=json")
            if response.status_code == 200:
                data = response.json()
                return data.get("ip")
    except Exception:
        pass
    return None


async def get_ip_from_page(page) -> Optional[str]:
    """
    从 CloakBrowser 页面获取该会话的真实公网出口 IP。

    三层兜底：
      1. context.request.get (最快，不污染历史与 DOM)
      2. page.evaluate → 浏览器内 fetch (绕过部分代理层限制)
      3. page.goto 跳转 (终极兜底)
    """
    # 1. context.request (绕过 CSP 与页面状态，最快)
    try:
        if hasattr(page, "context") and hasattr(page.context, "request"):
            response = await page.context.request.get(
                "https://api.ipify.org?format=json", timeout=5000
            )
            if response.ok:
                data = await response.json()
                return data.get("ip")
    except Exception:
        pass

    # 2. 页面内 fetch
    try:
        ip = await page.evaluate("""async () => {
            try {
                const controller = new AbortController();
                const timeoutId = setTimeout(() => controller.abort(), 5000);
                const response = await fetch('https://api.ipify.org?format=json', { signal: controller.signal });
                clearTimeout(timeoutId);
                if (!response.ok) return null;
                const data = await response.json();
                return data.ip;
            } catch (e) { return null; }
        }""")
        if ip:
            return ip
    except Exception:
        pass

    # 3. 终极兜底：页面跳转
    try:
        await page.goto("https://api.ipify.org?format=json", timeout=8000)
        body_raw = await page.text_content("body")
        data = json.loads(body_raw.strip())
        return data.get("ip")
    except Exception as e:
        print(f"\u26a0\ufe0f [IP Detector] \u7ec8\u6781\u515c\u5e95\u5931\u8d25: {e}")
        return None
