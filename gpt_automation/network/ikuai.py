"""
iKuai 路由器 HTTP API 封装 (Feature #11A / Mode A).

Router login flow: POST /Action/login with
{"username": ..., "passwd": md5(password)}.
WAN control endpoints under /Action/call with func_name field.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Any, Dict, List, Optional

import httpx

import config

logger = logging.getLogger(__name__)


class IKuaiClient:
    """Async client for the iKuai router's local HTTP API."""

    def __init__(
        self,
        api_url: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        timeout: float = 10.0,
    ) -> None:
        cfg = config.IKUAI_CONFIG
        self.api_url: str = (api_url or cfg["api_url"]).rstrip("/")
        self.username: str = username or cfg["username"]
        self.password: str = password or cfg["password"]
        self.timeout: float = timeout
        self._client: httpx.AsyncClient = httpx.AsyncClient(
            timeout=timeout, verify=False,
        )
        self._logged_in: bool = False

    async def close(self) -> None:
        if not self._client.is_closed:
            try:
                await self._client.aclose()
            except Exception:
                pass
        self._logged_in = False

    async def __aenter__(self) -> "IKuaiClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def login(self) -> bool:
        """Authenticate against the router. Returns True on success."""
        if not self.username or not self.password:
            logger.debug("ikuai: credentials not configured, skipping login")
            return False
        if self._logged_in:
            return True
        hashed_pw = hashlib.md5(self.password.encode("utf-8")).hexdigest()
        payload = {"username": self.username, "passwd": hashed_pw, "rememberme": 0}
        try:
            response = await self._client.post(
                f"{self.api_url}/Action/login", json=payload,
            )
        except httpx.HTTPError as exc:
            logger.warning("ikuai: login HTTP error: %s", exc)
            return False
        if response.status_code != 200:
            return False
        try:
            data = response.json()
        except json.JSONDecodeError:
            return False
        if data.get("Result") == 10000:
            self._logged_in = True
            logger.info("ikuai: login OK")
            return True
        logger.warning("ikuai: login failed: %s", data.get("ErrMsg"))
        return False

    async def list_wans(self) -> List[Dict[str, Any]]:
        """Return a list of WAN interface descriptors."""
        if not await self.login():
            return []
        try:
            response = await self._client.post(
                f"{self.api_url}/Action/call",
                json={"func_name": "wan", "action": "show", "param": {"TYPE": "wan_info,wan_list"}},
            )
        except httpx.HTTPError as exc:
            logger.warning("ikuai: list_wans HTTP error: %s", exc)
            return []
        if response.status_code != 200:
            return []
        try:
            data = response.json()
        except json.JSONDecodeError:
            return []
        return self._flatten_wans(data)

    async def get_wan_id_by_ip(self, target_ip: str) -> Optional[int]:
        for wan in await self.list_wans():
            if wan.get("ip_addr") == target_ip:
                wan_id = wan.get("id")
                if wan_id is not None:
                    try:
                        return int(wan_id)
                    except (TypeError, ValueError):
                        return None
        return None

    async def redial_wan(self, wan_id: int) -> bool:
        if not await self.login():
            return False
        try:
            response = await self._client.post(
                f"{self.api_url}/Action/call",
                json={"func_name": "wan", "action": "link_dhcp_reconnect", "param": {"id": int(wan_id)}},
            )
        except httpx.HTTPError as exc:
            logger.warning("ikuai: redial_wan HTTP error: %s", exc)
            return False
        if response.status_code == 200:
            try:
                result = response.json()
            except json.JSONDecodeError:
                return False
            if result.get("Result") == 10000:
                logger.info("ikuai: redial OK for WAN %d", wan_id)
                return True
        return False

    async def redial_random_wan(self) -> Optional[int]:
        wans = await self.list_wans()
        if not wans:
            logger.warning("ikuai: no WANs available to redial")
            return None
        import random
        chosen = random.choice(wans)
        wan_id = chosen.get("id")
        if wan_id is None:
            return None
        try:
            ok = await self.redial_wan(int(wan_id))
        except (TypeError, ValueError):
            return None
        return int(wan_id) if ok else None

    async def redial_target_wan(self, wan_id: str = None, name: str = None, delay: float = None) -> bool:
        """停用 → 等待 → 启用 目标线路（一次完整重拨）"""
        cfg = config.IKUAI_CONFIG
        wan_id = str(wan_id or cfg["target_wan_id"])
        name = name or cfg["target_wan_name"]
        delay = delay if delay is not None else cfg.get("redial_delay", 5.0)

        logger.info("ikuai: redial %s(id=%s) starting...", name, wan_id)
        if not await self.disable_wan(wan_id, name):
            return False
        logger.info("ikuai: redial %s(id=%s) waiting %.1fs...", name, wan_id, delay)
        await asyncio.sleep(delay)
        if not await self.enable_wan(wan_id, name):
            return False
        logger.info("ikuai: redial %s(id=%s) complete", name, wan_id)
        return True

    async def disable_wan(self, wan_id: str = None, name: str = None) -> bool:
        """停用目标 ADSL (vlan_down)"""
        cfg = config.IKUAI_CONFIG
        wan_id = str(wan_id or cfg["target_wan_id"])
        name = name or cfg["target_wan_name"]
        if not await self.login():
            return False
        response = await self._client.post(
            f"{self.api_url}/Action/call",
            json={
                "func_name": "wan",
                "action": "vlan_down",
                "param": {"id": wan_id}
            }
        )
        if response.status_code != 200:
            return False
        try:
            result = response.json()
        except Exception:
            return False
        ok = result.get("Result") == 30000
        logger.info("ikuai: disable %s(id=%s) → %s", name, wan_id, "OK" if ok else "FAIL")
        return ok

    async def enable_wan(self, wan_id: str = None, name: str = None) -> bool:
        """启用目标 ADSL (vlan_up)"""
        cfg = config.IKUAI_CONFIG
        wan_id = str(wan_id or cfg["target_wan_id"])
        name = name or cfg["target_wan_name"]
        if not await self.login():
            return False
        response = await self._client.post(
            f"{self.api_url}/Action/call",
            json={
                "func_name": "wan",
                "action": "vlan_up",
                "param": {"id": wan_id}
            }
        )
        if response.status_code != 200:
            return False
        try:
            result = response.json()
        except Exception:
            return False
        ok = result.get("Result") == 30000
        logger.info("ikuai: enable %s(id=%s) → %s", name, wan_id, "OK" if ok else "FAIL")
        return ok

    @staticmethod
    def _flatten_wans(data: Any) -> List[Dict[str, Any]]:
        out = []
        def _walk(node: Any) -> None:
            if isinstance(node, dict):
                if "id" in node and "ip_addr" in node:
                    out.append(node)
                for value in node.values():
                    _walk(value)
            elif isinstance(node, list):
                for item in node:
                    _walk(item)
        _walk(data)
        return out
