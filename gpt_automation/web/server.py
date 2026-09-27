"""Local HTTP server for the desktop dashboard.

Binds to 127.0.0.1 only. Serves the built frontend and a small JSON API.
No authentication is provided on purpose; the loopback bind and Origin check
are the security boundary. Demo and explicitly selected production runs share
the existing RegistrationWorkflow through the orchestrator adapter.
"""
from __future__ import annotations

import dataclasses
import json
import mimetypes
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

import config
from .contracts import (
    DemoRunState,
    HistoryDetail,
    HistoryItem,
    ResourceSummary,
    ResourceView,
    RunParametersSchema,
    WorkflowDefinition,
)
from .demo_engine import (
    PARAMETERS_SCHEMA,
    PHASES,
    DemoEngineError,
    DemoRunEngine,
)
from gpt_automation.inventory.service import RegisteredAccountError, RegisteredAccountService
from gpt_automation.security.credential_store import CredentialStore
from gpt_automation.state import StateStore
from .history_service import HistoryQueryError, HistoryQueryService
from .production_engine import ProductionEngineError, ProductionRunEngine
from .resource_service import ResourceAdminError, ResourceAdminService
from .resource_state_service import ResourceStateCorrectionService
from .result_file_service import ResultFileError, ResultFileService


def _json_default(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)
    return str(value)


def _to_dict(value: Any) -> Dict[str, Any]:
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)
    if isinstance(value, dict):
        return {key: _to_dict(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_dict(item) for item in value]
    return value


class DashboardServer:
    """Owns services and exposes them to the HTTP handler."""

    def __init__(
        self,
        *,
        project_root: str | Path,
        input_dir: str | Path,
        db_path: str | Path,
        snapshot_path: str | Path,
    ) -> None:
        self.project_root = Path(project_root)
        self.resources = ResourceAdminService(input_dir=input_dir, db_path=db_path)
        runtime_dir = Path(db_path).parent
        self.resource_states = ResourceStateCorrectionService(
            self.resources,
            db_path=db_path,
            lock_dir=runtime_dir / "session_locks",
            audit_path=runtime_dir / "resource_state_corrections.jsonl",
        )
        self.history = HistoryQueryService(db_path=db_path)
        self.accounts = RegisteredAccountService(
            state_store=StateStore(db_path),
            credential_store=CredentialStore(Path(db_path).parent / "credentials"),
            output_dir=Path(db_path).parent / "exports",
        )
        self.results = ResultFileService(self.project_root / config.OUTPUT_CSV)
        self.demo = DemoRunEngine(snapshot_path=snapshot_path)
        self.production = ProductionRunEngine(
            db_path=db_path,
            snapshot_path=runtime_dir / "production_runs.json",
            input_dir=input_dir,
            lock_dir=runtime_dir / "session_locks",
        )
        self._run_kind = "production" if self.production.state() else "demo"
        self._event_clients: set[Any] = set()
        self._event_lock = threading.Lock()
        self._run_lock = threading.Lock()
        self._ticker_started = False

    # ------------------------------------------------------------------
    # Demo ticking
    # ------------------------------------------------------------------

    def ensure_ticker(self) -> None:
        if self._ticker_started:
            return
        self._ticker_started = True

        def run_loop() -> None:
            while True:
                time.sleep(1.0)
                try:
                    with self._run_lock:
                        if self._run_kind == "production":
                            state = self.production.tick()
                        else:
                            state = self.demo.tick(seconds=1.0)
                    if state is not None:
                        self.broadcast_event("run", _to_dict(state))
                    self.broadcast_event("tick", {})
                except Exception:
                    pass

        thread = threading.Thread(target=run_loop, daemon=True, name="demo-ticker")
        thread.start()

    def broadcast_event(self, event: str, payload: Dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, default=_json_default)
        with self._event_lock:
            clients = list(self._event_clients)
        for client in clients:
            try:
                client.send_event(event, data)
            except Exception:
                with self._event_lock:
                    self._event_clients.discard(client)

    # ------------------------------------------------------------------
    # API operations
    # ------------------------------------------------------------------

    def get_workflow_definition(self, task_type: str | None = None) -> Dict[str, Any]:
        from .demo_engine import phases_for_task_type

        phases = phases_for_task_type(task_type) if task_type else PHASES
        return _to_dict(WorkflowDefinition(phases=list(phases)))

    def get_parameters_schema(self) -> Dict[str, Any]:
        return _to_dict(PARAMETERS_SCHEMA)

    def get_run_state(self) -> Dict[str, Any]:
        state = (
            self.production.state()
            if self._run_kind == "production"
            else self.demo.state()
        )
        return {} if state is None else _to_dict(state)

    def preflight_run(self, parameters: Dict[str, Any]) -> Dict[str, Any]:
        from .task_scope import TaskScopeError, normalize_run_parameters

        values = {str(key): str(value) for key, value in parameters.items()}
        try:
            normalized = normalize_run_parameters(values)
        except TaskScopeError as exc:
            raise DemoEngineError(exc.code, exc.message) from exc
        execution_mode = normalized.pop("execution_mode", "production")
        if execution_mode == "production":
            return self.production.preflight(normalized)
        if execution_mode == "demo":
            return {
                "ok": True,
                "checks": [
                    {
                        "key": "execution_mode",
                        "label": "执行类型",
                        "status": "ready",
                        "detail": "演示运行不会打开浏览器或修改资源。",
                    },
                    {
                        "key": "task_type",
                        "label": "任务类型",
                        "status": "ready",
                        "detail": f"将按 {normalized.get('task_type')} 两段式投影演示。",
                    },
                ],
            }
        raise DemoEngineError(
            "INVALID_PARAMETER",
            "execution_mode must be demo or production",
        )

    def start_run(self, parameters: Dict[str, Any]) -> Dict[str, Any]:
        from .task_scope import TaskScopeError, normalize_run_parameters

        run_parameters = {
            str(key): str(value) for key, value in parameters.items()
        }
        try:
            run_parameters = normalize_run_parameters(run_parameters)
        except TaskScopeError as exc:
            raise DemoEngineError(exc.code, exc.message) from exc
        execution_mode = run_parameters.pop("execution_mode", "production")
        with self._run_lock:
            if execution_mode == "production":
                state = self.production.start(run_parameters)
                self._run_kind = "production"
            elif execution_mode == "demo":
                state = self.demo.start(run_parameters)
                self._run_kind = "demo"
            else:
                raise DemoEngineError(
                    "INVALID_PARAMETER",
                    "execution_mode must be demo or production",
                )
        self.broadcast_event("run", _to_dict(state))
        return _to_dict(state)

    def submit_decision(self, decision: str) -> Dict[str, Any]:
        with self._run_lock:
            state = (
                self.production.decide(decision)
                if self._run_kind == "production"
                else self.demo.decide(decision)
            )
        self.broadcast_event("run", _to_dict(state))
        return _to_dict(state)

    def stop_run(self) -> Dict[str, Any]:
        with self._run_lock:
            if self._run_kind != "production":
                raise DemoEngineError(
                    "STOP_NOT_AVAILABLE",
                    "演示运行只能通过演示决策终止",
                )
            state = self.production.stop()
        self.broadcast_event("run", _to_dict(state))
        return _to_dict(state)

    def force_stop_run(self) -> Dict[str, Any]:
        with self._run_lock:
            if self._run_kind != "production":
                if self.demo.state() is None:
                    raise DemoEngineError("NO_ACTIVE_RUN", "没有真实运行")
                raise DemoEngineError(
                    "STOP_NOT_AVAILABLE",
                    "演示运行不支持强制结束",
                )
            state = self.production.force_stop()
        self.broadcast_event("run", _to_dict(state))
        return _to_dict(state)

    def list_resources(self, resource_type: str) -> Dict[str, Any]:
        views = self.resources.list_resources(resource_type)
        enriched = []
        for view in views:
            actions = self.resource_states.allowed_actions_for_view(view)
            enriched.append(dataclasses.replace(view, state_actions=actions))
        return {"items": _to_dict(enriched)}

    def get_resource_summary(self, resource_type: str) -> Dict[str, Any]:
        return _to_dict(self.resources.summary(resource_type))

    def resource_audit(self) -> Dict[str, Any]:
        return {"items": self.resource_states.list_audit()}

    def add_email(self, body: Dict[str, Any]) -> Dict[str, Any]:
        return _to_dict(
            self.resources.add_email(
                str(body.get("email", "")),
                str(body.get("password", "")),
                str(body.get("client_id", "")),
                str(body.get("refresh_token", "")),
            )
        )

    def edit_email(self, resource_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        return _to_dict(
            self.resources.edit_email(
                resource_id,
                password=(
                    str(body["password"]) if body.get("password") else None
                ),
                client_id=(
                    str(body["client_id"]) if body.get("client_id") else None
                ),
                refresh_token=(
                    str(body["refresh_token"])
                    if body.get("refresh_token")
                    else None
                ),
            )
        )

    def add_card(self, body: Dict[str, Any]) -> Dict[str, Any]:
        return _to_dict(
            self.resources.add_card(
                str(body.get("card_no", "")),
                str(body.get("expiry", "")),
                str(body.get("cvc", "")),
            )
        )

    def edit_card(self, resource_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        return _to_dict(
            self.resources.edit_card(
                resource_id,
                expiry=str(body["expiry"]) if body.get("expiry") else None,
                cvc=str(body["cvc"]) if body.get("cvc") else None,
            )
        )

    def resource_allowed_actions(
        self, resource_type: str, resource_id: str
    ) -> Dict[str, Any]:
        return {
            "resource_type": resource_type,
            "resource_id": resource_id,
            "actions": self.resource_states.allowed_actions(
                resource_type, resource_id
            ),
        }

    def correct_resource_state(
        self,
        resource_type: str,
        resource_id: str,
        body: Dict[str, Any],
    ) -> Dict[str, Any]:
        view = self.resource_states.correct_state(
            resource_type,
            resource_id,
            target=str(body.get("target", "")),
            reason=str(body.get("reason", "")),
            confirmed=bool(body.get("confirmed", False)),
        )
        return _to_dict(view)

    def list_registered_accounts(
        self,
        status: Optional[str] = None,
        include_paid_only: bool = False,
    ) -> Dict[str, Any]:
        statuses = (status,) if status else None
        items = self.accounts.list_accounts(
            statuses=statuses,
            include_paid_only=include_paid_only,
        )
        return {
            "items": [
                {
                    "account_id": item.account_id,
                    "email": item.email,
                    "status": item.status,
                    "source": item.source,
                    "has_chatgpt_password": item.has_chatgpt_password,
                    "has_totp_secret": item.has_totp_secret,
                    "has_email_password": item.has_email_password,
                    "has_oauth": item.has_oauth,
                    "provision_session_id": item.provision_session_id,
                    "last_checkout_session_id": item.last_checkout_session_id,
                    "ready_verified_at": item.ready_verified_at,
                    "last_login_verified_at": item.last_login_verified_at,
                    "offer_status": item.offer_status,
                    "offer_marked_at": item.offer_marked_at,
                    "offer_marked_reason": item.offer_marked_reason,
                    "updated_at": item.updated_at,
                    "chatgpt_password": item.chatgpt_password,
                    "totp_secret": item.totp_secret,
                    "email_password": item.email_password,
                    "client_id": item.client_id,
                    "refresh_token": item.refresh_token,
                }
                for item in items
            ]
        }

    def registered_account_summary(self) -> Dict[str, Any]:
        return self.accounts.summary()

    def list_billing_addresses(self) -> Dict[str, Any]:
        """Standard-format billing addresses for manual checkout switching."""
        from gpt_automation.phases.stripe import list_switch_addresses

        return {"items": list_switch_addresses()}

    def get_registered_account(self, account_id: str) -> Optional[Dict[str, Any]]:
        item = self.accounts.get_account(account_id)
        if item is None:
            return None
        return {
            "account_id": item.account_id,
            "email": item.email,
            "status": item.status,
            "source": item.source,
            "has_chatgpt_password": item.has_chatgpt_password,
            "has_totp_secret": item.has_totp_secret,
            "has_email_password": item.has_email_password,
            "has_oauth": item.has_oauth,
            "provision_session_id": item.provision_session_id,
            "last_checkout_session_id": item.last_checkout_session_id,
            "ready_verified_at": item.ready_verified_at,
            "last_login_verified_at": item.last_login_verified_at,
            "offer_status": item.offer_status,
            "offer_marked_at": item.offer_marked_at,
            "offer_marked_reason": item.offer_marked_reason,
            "updated_at": item.updated_at,
            "chatgpt_password": item.chatgpt_password,
            "totp_secret": item.totp_secret,
            "email_password": item.email_password,
            "client_id": item.client_id,
            "refresh_token": item.refresh_token,
        }

    def correct_registered_account(
        self, account_id: str, body: Dict[str, Any]
    ) -> Dict[str, Any]:
        target = str(body.get("target", "")).strip().upper()
        item = self.accounts.correct_status(account_id, target=target)
        return self.get_registered_account(item.account_id) or {}

    def reset_registered_account_offer(
        self,
        account_id: str,
    ) -> Dict[str, Any]:
        """Clear the PAID_ONLY marker so the account can be retried."""
        item = self.accounts.reset_offer_marking(account_id)
        return self.get_registered_account(item.account_id) or {}

    def export_registered_accounts(
        self, body: Dict[str, Any] | None = None
    ) -> Dict[str, Any]:
        payload = body or {}
        statuses_raw = str(payload.get("statuses", "READY"))
        statuses = tuple(
            part.strip().upper()
            for part in statuses_raw.replace(";", ",").split(",")
            if part.strip()
        ) or ("READY",)
        path = self.accounts.export_csv(statuses=statuses)
        return {"path": str(path), "statuses": list(statuses)}

    def import_registered_accounts(self, body: Dict[str, Any]) -> Dict[str, Any]:
        csv_text = str(body.get("csv_text", ""))
        source_name = str(body.get("source_name", "")).strip()
        if not source_name:
            source_name = "dashboard-import.csv"
        summary = self.accounts.import_csv(
            csv_text,
            source_name=source_name,
        )
        return _to_dict(summary)

    def list_history(
        self, status: Optional[str], session_id: Optional[str]
    ) -> Dict[str, Any]:
        items = self.history.list_sessions(
            status=status or None,
            session_id=session_id or None,
            limit=200,
        )
        return {"items": _to_dict(items)}

    def get_history_detail(self, session_id: str) -> Optional[Dict[str, Any]]:
        detail = self.history.get_detail(session_id)
        return None if detail is None else _to_dict(detail)

    def get_result_file(self) -> Dict[str, Any]:
        return _to_dict(self.results.read())

    def save_result_file(self, body: Dict[str, Any]) -> Dict[str, Any]:
        expected_version = body.get("expected_version")
        raw_text = body.get("raw_text")
        if not isinstance(expected_version, str) or not isinstance(raw_text, str):
            raise ResultFileError(
                "RESULT_FILE_INVALID",
                "结果文件保存请求缺少版本或文本内容。",
            )
        return _to_dict(self.results.save(expected_version, raw_text))

    # ------------------------------------------------------------------
    # SSE
    # ------------------------------------------------------------------

    def register_event_client(self, client: Any) -> None:
        with self._event_lock:
            self._event_clients.add(client)

    def unregister_event_client(self, client: Any) -> None:
        with self._event_lock:
            self._event_clients.discard(client)


class _SSEClient:
    def __init__(self, handler: BaseHTTPRequestHandler) -> None:
        self.handler = handler

    def send_event(self, event: str, data: str) -> None:
        self.handler.wfile.write(
            f"event: {event}\ndata: {data}\n\n".encode("utf-8")
        )
        self.handler.wfile.flush()


def _make_handler(server: DashboardServer, dist_dir: Path, allowed_origins: set):
    class Handler(BaseHTTPRequestHandler):
        server_version = "GPTAutomationDashboard/1.0"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _origin_allowed(self) -> bool:
            origin = self.headers.get("Origin")
            if not origin:
                return True
            return origin in allowed_origins

        def _send_json(self, status: int, payload: Any) -> None:
            body = json.dumps(
                payload, ensure_ascii=False, default=_json_default
            ).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_error_json(self, status: int, code: str, message: str) -> None:
            self._send_json(status, {"error": {"code": code, "message": message}})

        def _read_body(self) -> Dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return {}
            return data if isinstance(data, dict) else {}

        def _route(self, method: str) -> None:
            if not self._origin_allowed():
                self._send_error_json(
                    403, "ORIGIN_NOT_ALLOWED", "request origin is not allowed"
                )
                return
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)
            try:
                if path == "/api/health":
                    self._send_json(200, {"status": "ok"})
                    return
                if path == "/api/results/file" and method == "GET":
                    self._send_json(200, server.get_result_file())
                    return
                if path == "/api/results/file" and method == "PUT":
                    self._send_json(200, server.save_result_file(self._read_body()))
                    return
                if path == "/api/workflow/definition" and method == "GET":
                    task_type = query.get("task_type", [""])[0] or None
                    self._send_json(200, server.get_workflow_definition(task_type))
                    return
                if path == "/api/run-parameters/schema" and method == "GET":
                    self._send_json(200, server.get_parameters_schema())
                    return
                if path == "/api/runs/current" and method == "GET":
                    self._send_json(200, server.get_run_state())
                    return
                if path == "/api/runs/preflight" and method == "POST":
                    self._send_json(200, server.preflight_run(self._read_body()))
                    return
                if path == "/api/runs" and method == "POST":
                    self._send_json(201, server.start_run(self._read_body()))
                    return
                if path == "/api/runs/decisions" and method == "POST":
                    body = self._read_body()
                    self._send_json(
                        200,
                        server.submit_decision(str(body.get("decision", ""))),
                    )
                    return
                if path == "/api/runs/stop" and method == "POST":
                    self._send_json(200, server.stop_run())
                    return
                if path == "/api/runs/force-stop" and method == "POST":
                    self._send_json(200, server.force_stop_run())
                    return
                if path == "/api/runs/events" and method == "GET":
                    self._stream_events()
                    return
                if path == "/api/resources" and method == "GET":
                    resource_type = str(query.get("type", [""])[0] or "")
                    self._send_json(200, server.list_resources(resource_type))
                    return
                if path == "/api/resources/summary" and method == "GET":
                    resource_type = str(query.get("type", [""])[0] or "")
                    self._send_json(
                        200, server.get_resource_summary(resource_type)
                    )
                    return
                if path == "/api/resources/audit" and method == "GET":
                    self._send_json(200, server.resource_audit())
                    return
                if path == "/api/resources/emails" and method == "POST":
                    self._send_json(201, server.add_email(self._read_body()))
                    return
                if path.startswith("/api/resources/emails/") and method == "PATCH":
                    resource_id = path.rsplit("/", 1)[-1]
                    self._send_json(
                        200, server.edit_email(resource_id, self._read_body())
                    )
                    return
                if path == "/api/resources/cards" and method == "POST":
                    self._send_json(201, server.add_card(self._read_body()))
                    return
                if path.startswith("/api/resources/cards/") and method == "PATCH":
                    resource_id = path.rsplit("/", 1)[-1]
                    self._send_json(
                        200, server.edit_card(resource_id, self._read_body())
                    )
                    return
                resource_action = self._resource_action_path(path)
                if resource_action is not None:
                    resource_type, resource_id, action = resource_action
                    if action == "allowed-actions" and method == "GET":
                        self._send_json(
                            200,
                            server.resource_allowed_actions(
                                resource_type, resource_id
                            ),
                        )
                        return
                    if action == "state-corrections" and method == "POST":
                        self._send_json(
                            200,
                            server.correct_resource_state(
                                resource_type,
                                resource_id,
                                self._read_body(),
                            ),
                        )
                        return
                if path == "/api/registered-accounts" and method == "GET":
                    status = query.get("status", [""])[0] or None
                    include_paid_only = (
                        query.get("include_paid_only", ["false"])[0].lower()
                        in {"true", "1", "yes"}
                    )
                    self._send_json(
                        200,
                        server.list_registered_accounts(
                            status,
                            include_paid_only=include_paid_only,
                        ),
                    )
                    return
                if path == "/api/registered-accounts/summary" and method == "GET":
                    self._send_json(200, server.registered_account_summary())
                    return
                if path == "/api/billing-addresses" and method == "GET":
                    self._send_json(200, server.list_billing_addresses())
                    return
                if path == "/api/registered-accounts/import" and method == "POST":
                    self._send_json(
                        200, server.import_registered_accounts(self._read_body())
                    )
                    return
                if path == "/api/registered-accounts/export" and method == "POST":
                    self._send_json(
                        200, server.export_registered_accounts(self._read_body())
                    )
                    return
                if (
                    path.startswith("/api/registered-accounts/")
                    and path.endswith("/state-corrections")
                    and method == "POST"
                ):
                    account_id = path.split("/api/registered-accounts/", 1)[1].split(
                        "/", 1
                    )[0]
                    self._send_json(
                        200,
                        server.correct_registered_account(
                            account_id, self._read_body()
                        ),
                    )
                    return
                if (
                    path.startswith("/api/registered-accounts/")
                    and path.endswith("/offer-reset")
                    and method == "POST"
                ):
                    account_id = path.split("/api/registered-accounts/", 1)[1].split(
                        "/",
                        1,
                    )[0]
                    item = server.reset_registered_account_offer(account_id)
                    self._send_json(200, item)
                    return
                if path.startswith("/api/registered-accounts/") and method == "GET":
                    account_id = path.rsplit("/", 1)[-1]
                    if account_id in {"summary", "export"}:
                        self._send_error_json(
                            404, "NOT_FOUND", "api route not found"
                        )
                        return
                    detail = server.get_registered_account(account_id)
                    if detail is None:
                        self._send_error_json(
                            404, "ACCOUNT_NOT_FOUND", "account not found"
                        )
                    else:
                        self._send_json(200, detail)
                    return
                if path == "/api/history" and method == "GET":
                    self._send_json(
                        200,
                        server.list_history(
                            query.get("status", [""])[0] or None,
                            query.get("session_id", [""])[0] or None,
                        ),
                    )
                    return
                if path.startswith("/api/history/") and method == "GET":
                    session_id = path.rsplit("/", 1)[-1]
                    detail = server.get_history_detail(session_id)
                    if detail is None:
                        self._send_error_json(
                            404, "SESSION_NOT_FOUND", "session not found"
                        )
                    else:
                        self._send_json(200, detail)
                    return
                if path.startswith("/api/"):
                    self._send_error_json(
                        404, "NOT_FOUND", "api route not found"
                    )
                    return
                self._serve_static(path)
            except DemoEngineError as exc:
                self._send_error_json(400, exc.code, exc.message)
            except ProductionEngineError as exc:
                self._send_error_json(400, exc.code, exc.message)
            except RegisteredAccountError as exc:
                self._send_error_json(422, exc.code, exc.message)
            except ResourceAdminError as exc:
                self._send_error_json(422, exc.code, exc.message)
            except HistoryQueryError as exc:
                self._send_error_json(503, exc.code, exc.message)
            except ResultFileError as exc:
                status = {
                    "RESULT_FILE_CONFLICT": 409,
                    "RESULT_FILE_INVALID": 422,
                    "RESULT_FILE_ENCODING": 422,
                }.get(exc.code, 503)
                self._send_error_json(status, exc.code, exc.message)
            except Exception:
                self._send_error_json(
                    500, "INTERNAL_ERROR", "internal server error"
                )

        def _stream_events(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            client = _SSEClient(self)
            server.register_event_client(client)
            try:
                client.send_event("hello", json.dumps({"ok": True}))
                while True:
                    time.sleep(5)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                server.unregister_event_client(client)

        @staticmethod
        def _resource_action_path(path: str):
            parts = path.strip("/").split("/")
            if len(parts) != 5 or parts[:2] != ["api", "resources"]:
                return None
            if parts[2] not in {"emails", "cards"}:
                return None
            resource_type = "email" if parts[2] == "emails" else "card"
            return resource_type, parts[3], parts[4]

        def _serve_static(self, path: str) -> None:
            if path in ("", "/"):
                relative = "index.html"
            else:
                relative = path.lstrip("/")
            candidate = (dist_dir / relative).resolve()
            dist_resolved = dist_dir.resolve()
            if (
                dist_resolved not in candidate.parents
                and candidate != dist_resolved
            ):
                self._send_error_json(403, "FORBIDDEN", "path not allowed")
                return
            if not candidate.exists():
                candidate = dist_dir / "index.html"
            if not candidate.exists():
                self._send_error_json(
                    404, "DIST_MISSING", "frontend build is missing"
                )
                return
            body = candidate.read_bytes()
            content_type = (
                mimetypes.guess_type(candidate.name)[0]
                or "application/octet-stream"
            )
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            self._route("GET")

        def do_POST(self) -> None:
            self._route("POST")

        def do_PATCH(self) -> None:
            self._route("PATCH")

        def do_PUT(self) -> None:
            self._route("PUT")

        def do_OPTIONS(self) -> None:
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header(
                "Access-Control-Allow-Methods", "GET, POST, PATCH, PUT, OPTIONS"
            )
            self.send_header(
                "Access-Control-Allow-Headers", "Content-Type, Origin"
            )
            self.end_headers()

    return Handler


def serve(
    *,
    project_root: str | Path,
    input_dir: str | Path,
    db_path: str | Path,
    snapshot_path: str | Path,
    dist_dir: str | Path,
    host: str = "127.0.0.1",
    port: int = 8765,
    allowed_origins: Optional[set] = None,
) -> ThreadingHTTPServer:
    """Start the dashboard server and return the serving object."""
    server = DashboardServer(
        project_root=project_root,
        input_dir=input_dir,
        db_path=db_path,
        snapshot_path=snapshot_path,
    )
    server.ensure_ticker()
    origins = allowed_origins or {
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        f"http://127.0.0.1:{port}",
        f"http://localhost:{port}",
    }
    handler = _make_handler(server, Path(dist_dir), origins)
    httpd = ThreadingHTTPServer((host, port), handler)
    return httpd
