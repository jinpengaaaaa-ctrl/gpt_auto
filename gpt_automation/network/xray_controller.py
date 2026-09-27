"""
Xray 进程控制器 — 直接管理 Xray 核心进程的启动、停止与重启.

替代原有的 v2rayN HTTP API 间接控制方式，改为通过 subprocess 直接管理
Xray 进程生命周期，配合 Windows 系统代理实现完整的代理链路控制。

节点配置从 v2rayN 的 guiNDB.db SQLite 数据库直接读取，不需要手动导出 JSON。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import subprocess
import tempfile
from typing import Any, Dict, Optional

import config
from gpt_automation.utils.exceptions import SAFE_EXCEPTIONS

logger = logging.getLogger(__name__)


class XrayController:
    """直接管理 Xray 核心进程的启动、停止与重启.

    节点配置从 v2rayN 的 guiNDB.db 直接读取，自动组装为完整的 Xray JSON。
    """

    # VLESS reality 协议中 UUID 不影响认证，使用固定占位值
    _DEFAULT_UUID = "00000000-0000-0000-0000-000000000000"

    def __init__(
        self,
        executable_path: Optional[str] = None,
        config_dir: Optional[str] = None,
        config_name: Optional[str] = None,
        startup_delay: Optional[float] = None,
    ) -> None:
        cfg = config.XRAY_CONFIG
        self.executable_path: str = executable_path or cfg["executable"]
        self.config_dir: str = config_dir or cfg["config_dir"]
        self.db_path: str = cfg.get("db_path", "")
        self.config_name: str = config_name or config.ADSL_XRAY_MAP.get(
            config.IKUAI_CONFIG["target_wan_name"], ""
        )
        self.startup_delay: float = (
            startup_delay if startup_delay is not None else cfg["startup_delay"]
        )
        self._process: Optional[asyncio.subprocess.Process] = None
        self._temp_config: Optional[str] = None

    # ------------------------------------------------------------------
    # Process lifecycle
    # ------------------------------------------------------------------

    async def kill_existing(self) -> bool:
        """通过 taskkill 杀掉所有已存在的 xray.exe 进程.

        Returns:
            True 表示操作成功（目标不存在或已成功终止）。
        """
        try:
            await asyncio.to_thread(
                subprocess.run,
                ["taskkill", "/F", "/IM", "xray.exe"],
                capture_output=True,
                check=False,
            )
            logger.info("xray: kill_existing OK")
            return True
        except SAFE_EXCEPTIONS as exc:
            logger.warning("xray: kill_existing failed: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Config generation (from v2rayN guiNDB.db)
    # ------------------------------------------------------------------

    def load_config(self) -> Dict[str, Any]:
        """从 v2rayN guiNDB.db 读取节点配置并组装为 Xray JSON.

        Returns:
            完整的 Xray JSON 配置字典。若 DB 不存在或节点未找到则返回空字典。
        """
        if not self.db_path or not os.path.isfile(self.db_path):
            logger.warning(
                "xray: guiNDB.db not found at %s, falling back to JSON file",
                self.db_path,
            )
            return self._load_config_from_file()

        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                """SELECT Address, Port, Network, StreamSecurity, Sni, Fingerprint,
                          PublicKey, ShortId, ProtoExtra, TransportExtra, HeaderType, Path,
                          Password, SpiderX
                   FROM ProfileItem WHERE Remarks = ?""",
                (self.config_name,),
            )
            row = cursor.fetchone()
            conn.close()
        except sqlite3.Error as exc:
            logger.warning(
                "xray: failed to read guiNDB.db: %s, falling back to JSON", exc
            )
            return self._load_config_from_file()

        if row is None:
            logger.error("xray: node '%s' not found in guiNDB.db", self.config_name)
            return {}

        (
            address,
            port,
            network,
            security,
            sni,
            fingerprint,
            public_key,
            short_id,
            proto_extra_str,
            transport_extra_str,
            header_type,
            path,
            password,
            spider_x,
        ) = row

        # Split Address:Port
        host = address or ""
        if ":" in host and port is None:
            parts = host.rsplit(":", 1)
            host = parts[0]
            try:
                port = int(parts[1])
            except ValueError:
                pass

        # Parse JSON extra fields
        proto_extra = self._parse_json(proto_extra_str)
        transport_extra = self._parse_json(transport_extra_str)

        # Build Xray config
        return self._build_xray_config(
            host=host,
            port=port or 443,
            network=network or "",
            security=security or "",
            sni=sni or "",
            fingerprint=fingerprint or "",
            public_key=public_key or "",
            short_id=short_id or "",
            proto_extra=proto_extra,
            transport_extra=transport_extra,
            header_type=header_type or "",
            path=path or "/",
            uuid=password or XrayController._DEFAULT_UUID,
            spider_x=spider_x or "",
        )

    def _load_config_from_file(self) -> Dict[str, Any]:
        """Fallback: 从 JSON 文件读取配置."""
        config_path = f"{self.config_dir}/{self.config_name}.json"
        try:
            with open(config_path, "r", encoding="utf-8") as fh:
                data: Dict[str, Any] = json.load(fh)
            logger.info("xray: config loaded from %s", config_path)
            return data
        except FileNotFoundError:
            logger.error("xray: config file not found: %s", config_path)
            return {}
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("xray: failed to load config: %s", exc)
            return {}

    @staticmethod
    def _parse_json(s: Optional[str]) -> Dict[str, Any]:
        if not s:
            return {}
        try:
            return json.loads(s)
        except (json.JSONDecodeError, TypeError):
            return {}

    @staticmethod
    def _build_xray_config(
        host: str,
        port: int,
        network: str,
        security: str,
        sni: str,
        fingerprint: str,
        public_key: str,
        short_id: str,
        proto_extra: Dict[str, Any],
        transport_extra: Dict[str, Any],
        header_type: str,
        path: str,
        uuid: str = _DEFAULT_UUID,
        spider_x: str = "",
    ) -> Dict[str, Any]:
        """组装完整的 Xray JSON 配置（inbound + outbound + routing）."""
        # UUID 来自 DB 的 Password 字段（v2rayN 的 VLESS 密钥存储位置）
        _uuid = uuid

        # Outbound settings
        stream_settings: Dict[str, Any] = {
            "network": network or "tcp",
        }

        if security == "reality":
            stream_settings["security"] = "reality"
            stream_settings["realitySettings"] = {
                "serverName": sni,
                "fingerprint": fingerprint,
                "publicKey": public_key,
                "shortId": short_id,
            }
        elif security:
            stream_settings["security"] = security

        # Network-specific settings
        if network == "xhttp":
            mode = transport_extra.get("XhttpMode", "auto")
            xhttp_host = transport_extra.get("Host", host)
            xhttp_path = path or transport_extra.get("Path", "/")
            stream_settings["xhttpSettings"] = {
                "mode": mode,
                "path": xhttp_path,
                "host": xhttp_host,
            }
            if spider_x:
                stream_settings["xhttpSettings"]["spiderX"] = spider_x

        return {
            "inbounds": [
                {
                    "port": config.PROXY_CONFIG.get("port", 10808),
                    "listen": config.PROXY_CONFIG.get("host", "127.0.0.1"),
                    "protocol": "socks",
                    "settings": {"udp": True},
                },
                {
                    "port": config.PROXY_CONFIG.get("port", 10808) + 1,
                    "listen": config.PROXY_CONFIG.get("host", "127.0.0.1"),
                    "protocol": "http",
                },
            ],
            "outbounds": [
                {
                    "protocol": "vless",
                    "settings": {
                        "vnext": [
                            {
                                "address": host,
                                "port": port,
                                "users": [
                                    {
                                        "id": _uuid,
                                        "flow": proto_extra.get("Flow", ""),
                                        "encryption": proto_extra.get(
                                            "VlessEncryption", "none"
                                        ),
                                    }
                                ],
                            }
                        ],
                    },
                    "streamSettings": stream_settings,
                    "tag": "proxy",
                }
            ],
            "routing": {
                "domainStrategy": "IPIfNonMatch",
                "rules": [
                    {
                        "type": "field",
                        "inboundTag": ["inbound"],
                        "outboundTag": "proxy",
                    }
                ],
            },
        }

    async def start(self) -> bool:
        """从 v2rayN DB 读取节点配置，写入临时文件，启动 Xray.

        Returns:
            True 表示进程已成功启动。
        """
        xray_config = self.load_config()
        if not xray_config:
            logger.error("xray: no config loaded for %s", self.config_name)
            return False

        # Write config to temp file
        try:
            fd, self._temp_config = tempfile.mkstemp(
                suffix=".json", prefix="xray_config_"
            )
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(xray_config, f, indent=2)
        except OSError as exc:
            logger.error("xray: failed to write temp config: %s", exc)
            return False

        logger.info(
            "xray: config generated from DB for %s → %s",
            self.config_name,
            self._temp_config,
        )
        try:
            self._process = await asyncio.create_subprocess_exec(
                self.executable_path,
                "run",
                f"-config={self._temp_config}",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.sleep(self.startup_delay)
            if self._process.returncode is not None:
                logger.error(
                    "xray: process exited immediately, returncode=%d",
                    self._process.returncode,
                )
                self._process = None
                return False
            logger.info(
                "xray: started (pid=%d) config=%s", self._process.pid, self._temp_config
            )
            return True
        except SAFE_EXCEPTIONS as exc:
            logger.error("xray: start failed: %s", exc)
            self._process = None
            return False

    async def stop(self) -> bool:
        """终止 Xray 进程（terminate → 5s 超时 → kill 兜底）并清理临时配置.

        始终通过 taskkill /F /IM xray.exe 兜底，确保不会有遗留 xray.exe
        占用端口（即使 self._process 为 None 或引用丢失）。

        Returns:
            True 表示进程已终止或不存在。
        """
        # 1. 如果有已跟踪的进程引用，先通过 terminate/kill 停止它
        if self._process is not None:
            try:
                self._process.terminate()
                try:
                    await asyncio.wait_for(self._process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    logger.warning("xray: terminate timed out, sending kill")
                    self._process.kill()
                    await self._process.wait()
                logger.info("xray: stopped (tracked process)")
            except SAFE_EXCEPTIONS as exc:
                logger.warning("xray: stop error (tracked process): %s", exc)
            finally:
                self._process = None
                self._cleanup_temp_config()

        # 2. 兜底：taskkill 杀掉所有 xray.exe（防止遗留进程占用端口）
        #    即使 self._process 为 None（Ctrl+C 后引用丢失等场景）也能清理干净
        await self.kill_existing()

        # 3. 端口诊断：检查 10808 是否已释放（不阻塞，仅日志）
        await self._log_port_status()

        return True

    async def _log_port_status(self) -> None:
        """诊断端口 10808 状态，帮助排查代理未清除的问题。仅日志，不抛异常。"""
        import config as _cfg

        port = _cfg.PROXY_CONFIG.get("port", 10808)
        try:
            proc = await asyncio.create_subprocess_exec(
                "netstat",
                "-ano",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3.0)
            lines = stdout.decode("utf-8", errors="replace").splitlines()
            matches = [ln for ln in lines if f":{port}" in ln and "LISTENING" in ln]
            if matches:
                logger.warning(
                    "xray: port %d still in LISTENING state after stop: %s",
                    port,
                    matches[:3],
                )
            else:
                logger.info("xray: port %d is free (no LISTENING entries)", port)
        except Exception:
            logger.debug("xray: port diagnostic skipped (netstat unavailable)")

    def is_running(self) -> bool:
        """检查 Xray 进程是否仍在运行.

        Returns:
            True 表示进程存在且未退出。
        """
        return self._process is not None and self._process.returncode is None

    async def restart(self) -> bool:
        """重启 Xray：stop → start.

        Returns:
            True 表示重启成功。
        """
        await self.stop()
        return await self.start()

    async def close(self) -> None:
        """关闭资源：终止进程并清理临时配置文件。

        stop() 内部已通过 kill_existing() 兜底处理 _process 为 None 的情况，
        所以无条件调用 stop() 确保所有 xray.exe 都被杀掉。
        """
        await self.stop()
        self._cleanup_temp_config()

    def _cleanup_temp_config(self) -> None:
        """删除临时配置文件。"""
        if self._temp_config and os.path.isfile(self._temp_config):
            try:
                os.remove(self._temp_config)
                logger.debug("xray: cleaned up temp config %s", self._temp_config)
            except OSError:
                pass
            self._temp_config = None
