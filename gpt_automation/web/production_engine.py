"""Production control-plane adapter for the local dashboard.

The adapter owns process lifecycle, operator request brokering and a redacted
projection of the existing workflow state.  It never implements registration
steps itself; the worker calls ``orchestrator.main`` which remains the only
batch entrypoint to ``RegistrationWorkflow``.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from gpt_automation import orchestrator
from gpt_automation.orchestrator import RuntimeOptions
from gpt_automation.state import StateStore
from gpt_automation.utils.redaction import mask_email
from gpt_automation.workflow.interaction_policy import (
    FailureContext,
    InteractionChoice,
    InteractionDecision,
    InteractionPolicy,
)

from .contracts import (
    OperatorRequestView,
    RunEvent,
    RunState,
)
from .demo_engine import FULL_PHASES, PHASES, phases_for_task_type

_SENSITIVE_DIGITS = re.compile(r"\d{12,}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _redact_text(value: str) -> str:
    return _SENSITIVE_DIGITS.sub("[REDACTED]", value)


_STEP_TITLES = {
    "manual_pause": "请人工完成页面操作",
    "mfa_setup": "MFA 绑定需要人工确认",
    "mfa_verify": "MFA 验证需要人工确认",
    "captcha": "验证码需要人工处理",
    "unknown_page": "遇到未知页面，需要人工处理",
    "stripe_card": "支付卡片需要人工处理",
    "stripe_confirm": "支付确认需要人工处理",
    "stripe_payment": "支付结果需要人工确认",
    "stripe_addr": "账单地址需要人工处理",
    "payment_verified": "支付结果需要人工确认",
    "subscription": "订阅状态需要人工确认",
    "cancellation": "取消订阅需要人工确认",
    "login_recovery": "登录恢复需要人工确认",
}

_STRIPE_STEPS = {
    "stripe_card",
    "stripe_confirm",
    "stripe_payment",
    "stripe_manual_submit",
    "stripe_addr",
}

_DANGER_STEPS = _STRIPE_STEPS | {
    "payment_verified",
    "subscription",
    "cancellation",
    "cancel_subscription",
}


def _severity_for_step(step: str) -> str:
    if step in _DANGER_STEPS:
        return "danger"
    if step == "NETWORK_READY":
        return "warning"
    return "info"


class ProductionEngineError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class WebInteractionBroker:
    """Thread-safe bridge between a workflow event loop and HTTP requests."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._requests: Dict[str, Dict[str, Any]] = {}
        self._decisions: Dict[str, str] = {}

    def publish(self, request: Dict[str, Any]) -> Dict[str, Any]:
        with self._condition:
            self._requests[request["request_id"]] = dict(request)
            self._condition.notify_all()
        return request

    def wait_for_request(self, timeout: Optional[float] = None) -> Optional[Dict[str, Any]]:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while not self._requests:
                if deadline is None:
                    self._condition.wait()
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return None
                    self._condition.wait(remaining)
            request_id = next(iter(self._requests))
            return dict(self._requests[request_id])

    def current_request(self) -> Optional[Dict[str, Any]]:
        with self._condition:
            if not self._requests:
                return None
            request_id = next(iter(self._requests))
            return dict(self._requests[request_id])

    def resolve(self, request_id: str, decision: str) -> None:
        with self._condition:
            if request_id not in self._requests:
                raise ProductionEngineError(
                    "OPERATOR_REQUEST_NOT_FOUND",
                    "operator request is no longer waiting",
                )
            allowed = set(self._requests[request_id]["allowed_decisions"])
            normalized = decision.split(":", 1)[0]
            if normalized not in allowed:
                raise ProductionEngineError(
                    "DECISION_NOT_ALLOWED",
                    "decision is not allowed for the current request",
                )
            self._decisions[request_id] = decision
            self._condition.notify_all()

    def wait_for_decision(self, request_id: str) -> str:
        with self._condition:
            while request_id not in self._decisions:
                self._condition.wait()
            decision = self._decisions.pop(request_id)
            self._requests.pop(request_id, None)
            self._condition.notify_all()
            return decision


class WebInteractionPolicy:
    """InteractionPolicy implementation backed by the browser console."""

    def __init__(
        self,
        broker: WebInteractionBroker,
        *,
        session_id: str = "",
        on_request: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        self.broker = broker
        self.session_id = session_id
        self.on_request = on_request

    async def decide(
        self,
        context: FailureContext,
    ) -> InteractionDecision | InteractionChoice:
        allowed: list[str] = [
            InteractionDecision.DEFER_ACCOUNT.value,
            InteractionDecision.ABORT_BATCH.value,
            InteractionDecision.SKIP_ACCOUNT.value,
            InteractionDecision.KEEP_OPEN.value,
            InteractionDecision.RESTART_BROWSER.value,
        ]
        if context.verification_available:
            allowed.append(InteractionDecision.VERIFY_MANUAL.value)
        if context.advance_available:
            allowed.append(InteractionDecision.ADVANCE_AFTER_MANUAL.value)
        if context.retry_allowed:
            allowed.append(InteractionDecision.RETRY_AUTO.value)
        if context.target_options:
            allowed.append(InteractionDecision.SELECT_TARGET.value)
        stripe_available = getattr(context, "stripe_retry_available", False) or (
            context.step in _STRIPE_STEPS
        )
        if stripe_available:
            allowed.extend(
                [
                    InteractionDecision.STRIPE_REFILL_NONE.value,
                    InteractionDecision.STRIPE_SWITCH_CARD_QUARANTINE.value,
                    InteractionDecision.STRIPE_SWITCH_CARD_RELEASE.value,
                    InteractionDecision.STRIPE_SWITCH_ADDRESS.value,
                ]
            )

        step = _redact_text(context.step)
        title = _STEP_TITLES.get(context.step, f"需要人工处理：{step or '当前步骤'}")
        instruction = _redact_text(context.description)
        if context.verification_available:
            after_action = "系统将重新检测页面状态，确认已完成后再继续。"
        elif context.advance_available:
            after_action = (
                f"系统将验证下一步状态（{_redact_text(context.next_step_description or '下一步')}）后再继续。"
            )
        elif stripe_available:
            after_action = (
                "切换后系统不会自动提交支付；请在浏览器点击支付按钮，"
                "完成后回来提交确认，系统将核实支付结果。"
            )
        else:
            after_action = "系统将保留当前现场，等待你的处理结果。"
        error_summary = _redact_text(
            f"{type(context.exception).__name__}: {context.exception}"
        )[:300]

        request = {
            "request_id": "operator-" + uuid.uuid4().hex[:12],
            "session_id": self.session_id,
            "email": mask_email(context.email) if context.email else "-",
            "step_id": step,
            "description": instruction,
            "verification_available": context.verification_available,
            "retry_allowed": context.retry_allowed,
            "advance_available": context.advance_available,
            "allowed_decisions": allowed,
            "created_at": _now(),
            "kind": "intervention",
            "title": title,
            "instruction": instruction,
            "after_action": after_action,
            "verification_hint": (
                "确认浏览器页面已达到预期状态后再提交。"
                if context.verification_available
                else ""
            ),
            "severity_level": _severity_for_step(context.step),
            "error_summary": error_summary,
            "target_options": [
                {"id": target_id, "label": label}
                for target_id, label in context.target_options
            ],
        }
        self.broker.publish(request)
        if self.on_request is not None:
            self.on_request(request)
        selected = await asyncio.to_thread(
            self.broker.wait_for_decision,
            request["request_id"],
        )
        if selected.startswith(InteractionDecision.SELECT_TARGET.value + ":"):
            return InteractionChoice(
                InteractionDecision.SELECT_TARGET,
                selected.split(":", 1)[1],
            )
        if selected.startswith(InteractionDecision.STRIPE_SWITCH_ADDRESS.value + ":"):
            return InteractionChoice(
                InteractionDecision.STRIPE_SWITCH_ADDRESS,
                selected.split(":", 1)[1],
            )
        return InteractionDecision(selected)


class WebManualIPConfirmer:
    """Bridge manual-hotspot IP prompts into browser-console operator requests.

    The manual IP switcher normally waits on stdin; the dashboard process has
    no interactive terminal, so the same prompts are published through the
    operator broker and the operator answers from the execution page.  The
    returned value keeps the CLI character vocabulary (``""``/``"s"``/``"c"``/
    ``"q"``) so the network strategy logic stays unchanged.
    """

    _KEY_TO_DECISION = {
        "use": "confirm_network",
        "continue": "confirm_network",
        "retry": "retry_network",
        "switch": "switch_network",
        "reuse": "reuse_ip",
        "cancel": "abort_batch",
    }
    _DECISION_TO_CHAR = {
        "confirm_network": "",
        "retry_network": "",
        "switch_network": "s",
        "reuse_ip": "c",
        "abort_batch": "q",
    }

    def __init__(
        self,
        broker: WebInteractionBroker,
        *,
        session_id: str = "",
        on_request: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        self.broker = broker
        self.session_id = session_id
        self.on_request = on_request

    async def __call__(self, prompt: str, keys: str = "") -> str:
        """Make the confirmer usable as ``input_fn`` for ManualIPSwitcher."""
        return await self.read(prompt, keys)

    async def read(self, prompt: str, keys: str = "") -> str:
        allowed: list[str] = []
        for key in keys.split("|"):
            decision = self._KEY_TO_DECISION.get(key.strip())
            if decision and decision not in allowed:
                allowed.append(decision)
        if not allowed:
            allowed = ["confirm_network", "abort_batch"]
        request = {
            "request_id": "network-" + uuid.uuid4().hex[:12],
            "session_id": self.session_id,
            "email": "-",
            "step_id": "NETWORK_READY",
            "description": _redact_text(prompt),
            "verification_available": False,
            "retry_allowed": True,
            "advance_available": False,
            "allowed_decisions": allowed,
            "created_at": _now(),
            "kind": "network",
            "title": "网络确认",
            "instruction": _redact_text(prompt),
            "after_action": "系统将重新检测出口 IP，确认通过后启动浏览器。",
            "verification_hint": "",
            "severity_level": "warning",
            "error_summary": "",
            "target_options": [],
        }
        self.broker.publish(request)
        if self.on_request is not None:
            self.on_request(request)
        selected = await asyncio.to_thread(
            self.broker.wait_for_decision,
            request["request_id"],
        )
        base = selected.split(":", 1)[0]
        return self._DECISION_TO_CHAR.get(base, "q")


@dataclass
class _ProductionRun:
    run_id: str
    parameters: Dict[str, str]
    status: str
    current_step_id: str
    phase_index: int
    step_index: int
    waiting_reason: str
    events: list[RunEvent]
    started_at: str
    updated_at: str
    workflow_session_id: str = ""
    operator_request: Optional[OperatorRequestView] = None
    exit_code: Optional[int] = None
    account_index: int = 0
    current_action: str = ""
    browser_state: str = ""
    last_error: str = ""

    def state(self) -> RunState:
        return RunState(
            session_id=self.workflow_session_id or self.run_id,
            mode="production",
            parameters=dict(self.parameters),
            phase_index=self.phase_index,
            step_index=self.step_index,
            status=self.status,
            current_step_id=self.current_step_id,
            waiting_reason=self.waiting_reason,
            events=list(self.events),
            started_at=self.started_at,
            updated_at=self.updated_at,
            operator_request=self.operator_request,
            allowed_decisions=(
                list(self.operator_request.allowed_decisions)
                if self.operator_request is not None
                else []
            ),
            account_index=self.account_index,
            current_action=self.current_action,
            browser_state=self.browser_state,
            last_error=self.last_error,
        )


_STAGE_TO_STEP = {
    "CREATED": "NETWORK_READY",
    "NETWORK_PENDING": "NETWORK_READY",
    "NETWORK_READY": "NETWORK_READY",
    "ACCOUNT_VERIFIED": "ACCOUNT_AUTHENTICATED",
    "PASSWORD_PENDING": "SIGNUP_PASSWORD_VERIFIED",
    "PASSWORD_VERIFIED": "SIGNUP_PASSWORD_VERIFIED",
    "MFA_PENDING": "MFA_VERIFIED",
    "MFA_VERIFIED": "MFA_VERIFIED",
    "OFFER_VERIFIED": "OFFER_DECISION_VERIFIED",
    "PAYMENT_PENDING": "PAYMENT_VERIFIED",
    "PAYMENT_VERIFIED": "PAYMENT_VERIFIED",
    "CANCELLATION_PENDING": "CANCELLATION_VERIFIED",
    "CANCELLATION_VERIFIED": "CANCELLATION_VERIFIED",
    "RESULT_PERSISTED": "RESULT_PERSISTED",
    "COMPLETED": "RESULT_PERSISTED",
}


def _step_position(step_id: str, task_type: str | None = None) -> tuple[int, int]:
    phases = (
        phases_for_task_type(task_type)
        if task_type
        else FULL_PHASES
    )
    for phase_index, phase in enumerate(phases):
        for step_index, step in enumerate(phase.steps):
            if step.step_id == step_id:
                return phase_index, step_index
    # A step from another task must never leak into this track; only the
    # advanced ``full`` projection may fall back to the 4-phase layout.
    if not task_type or (task_type or "").strip().lower() == "full":
        for phase_index, phase in enumerate(FULL_PHASES):
            for step_index, step in enumerate(phase.steps):
                if step.step_id == step_id:
                    return phase_index, step_index
    return 0, 0


class ProductionRunEngine:
    """Run the existing orchestrator in one background worker."""

    def __init__(
        self,
        *,
        db_path: str | Path,
        snapshot_path: str | Path,
        input_dir: str | Path,
        lock_dir: str | Path,
    ) -> None:
        self.db_path = Path(db_path)
        self.snapshot_path = Path(snapshot_path)
        self.input_dir = Path(input_dir)
        self.lock_dir = Path(lock_dir)
        self._lock = threading.RLock()
        self._run: Optional[_ProductionRun] = None
        self._broker = WebInteractionBroker()
        self._stop_event = threading.Event()
        self._known_session_ids: set[str] = set()
        self._last_stage = ""
        self._orphan_after_restart = False
        self._load()

    def _load(self) -> None:
        if not self.snapshot_path.exists():
            return
        try:
            payload = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
            events = [RunEvent(**event) for event in payload.get("events", [])]
            request_payload = payload.get("operator_request")
            request = (
                OperatorRequestView(**request_payload)
                if request_payload
                else None
            )
            self._run = _ProductionRun(
                run_id=str(payload["run_id"]),
                parameters={
                    str(key): str(value)
                    for key, value in payload.get("parameters", {}).items()
                },
                status=str(payload.get("status", "NEEDS_OPERATOR")),
                current_step_id=str(payload.get("current_step_id", "NETWORK_READY")),
                phase_index=int(payload.get("phase_index", 0)),
                step_index=int(payload.get("step_index", 0)),
                waiting_reason=str(payload.get("waiting_reason", "")),
                events=events,
                started_at=str(payload.get("started_at", _now())),
                updated_at=str(payload.get("updated_at", _now())),
                workflow_session_id=str(payload.get("workflow_session_id", "")),
                operator_request=request,
                exit_code=payload.get("exit_code"),
                account_index=int(payload.get("account_index", 0) or 0),
                current_action=str(payload.get("current_action", "")),
                browser_state=str(payload.get("browser_state", "")),
                last_error=str(payload.get("last_error", "")),
            )
            try:
                store = StateStore(self.db_path)
                store.initialize()
                self._known_session_ids = {
                    session.session_id for session in store.list_sessions()
                }
            except Exception:
                self._known_session_ids = set()
            self._stop_event.clear()
            self._orphan_after_restart = False
            # 控制台重启后 Worker 已不存在：任何非干净终态的残留都必须先经
            # 「结束运行」确认；历史 DB 会话不得被回放进当前运行。
            orphaned = self._run.status in {
                "RUNNING",
                "STOPPING",
                "NEEDS_OPERATOR",
                "UNKNOWN_SIDE_EFFECT",
                "FAILED",
            }
            if orphaned:
                self._run.status = "NEEDS_OPERATOR"
                self._run.workflow_session_id = ""
                self._run.waiting_reason = (
                    "控制台重启，需重新确认当前浏览器现场；可结束本次控制台运行。"
                )
                self._orphan_after_restart = True
                if not any(
                    "控制台重启" in event.message for event in self._run.events
                ):
                    self._append_event("WARN", self._run.waiting_reason)
                self._persist()
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            self._run = None

    def start(self, parameters: Dict[str, str]) -> RunState:
        validated = self._validate_parameters(parameters)
        with self._lock:
            if self._run is not None and self._run.status in {
                "RUNNING",
                "STOPPING",
                "NEEDS_OPERATOR",
                "UNKNOWN_SIDE_EFFECT",
            }:
                raise ProductionEngineError(
                    "RUN_ALREADY_ACTIVE",
                    "已有真实运行需要先处理或结束",
                )
            try:
                store = StateStore(self.db_path)
                store.initialize()
                self._known_session_ids = {
                    session.session_id for session in store.list_sessions()
                }
            except Exception:
                self._known_session_ids = set()
            timestamp = _now()
            self._orphan_after_restart = False
            self._run = _ProductionRun(
                run_id="production-" + uuid.uuid4().hex[:12],
                parameters=validated,
                status="RUNNING",
                current_step_id="NETWORK_READY",
                phase_index=0,
                step_index=0,
                waiting_reason="",
                events=[
                    RunEvent(
                        timestamp,
                        "INFO",
                        "真实运行已启动，Worker 正在准备工作流。",
                        "RUN_STARTED",
                    )
                ],
                started_at=timestamp,
                updated_at=timestamp,
                account_index=0,
                current_action="正在预检浏览器运行环境",
                browser_state="preflight",
            )
            self._persist()
            worker = threading.Thread(
                target=self._worker,
                name="production-run-worker",
                daemon=True,
            )
            worker.start()
            return self._run.state()

    def preflight(self, parameters: Dict[str, str]) -> Dict[str, Any]:
        validated = self._validate_parameters(parameters)
        checks: list[Dict[str, str]] = [
            {
                "key": "parameters",
                "label": "运行参数",
                "status": "ready",
                "detail": "参数已通过白名单校验。",
            }
        ]
        if validated["interaction"] == "unattended" and validated["network"] == "manual":
            checks.append(
                {
                    "key": "policy_network",
                    "label": "交互与网络组合",
                    "status": "blocked",
                    "detail": "无人值守模式不能使用手动 IP 网络策略。",
                }
            )
        with self._lock:
            if self._run is not None and self._run.status in {
                "RUNNING",
                "STOPPING",
                "NEEDS_OPERATOR",
                "UNKNOWN_SIDE_EFFECT",
            }:
                checks.append(
                    {
                        "key": "run_lock",
                        "label": "运行锁",
                        "status": "blocked",
                        "detail": "已有真实运行需要先处理或结束。",
                    }
                )
            else:
                checks.append(
                    {
                        "key": "run_lock",
                        "label": "运行锁",
                        "status": "ready",
                        "detail": "当前没有冲突的真实运行。",
                    }
                )
        if validated["mode"] == "resume":
            try:
                session = StateStore(self.db_path).get_session(
                    validated["resume_session_id"]
                )
            except Exception:
                session = None
            checks.append(
                {
                    "key": "resume_session",
                    "label": "恢复会话",
                    "status": "ready" if session else "blocked",
                    "detail": "恢复会话存在。" if session else "恢复会话不存在。",
                }
            )
        try:
            store = StateStore(self.db_path)
            store.initialize()
            from gpt_automation.utils.input_manager import InputManager

            report = InputManager(
                input_dir=self.input_dir,
                state_store=store,
            ).get_resource_report()
            from gpt_automation.orchestrator import resource_report_ready_for_scope
            from gpt_automation.inventory.service import RegisteredAccountService
            from gpt_automation.security.credential_store import CredentialStore
            from .task_scope import parse_account_ids

            scope = validated.get("run_scope", "full")
            if scope == "checkout_fulfillment":
                account_ids = parse_account_ids(validated.get("account_ids", ""))
                checkout_goal = validated.get("checkout_goal", "complete")
                inventory = RegisteredAccountService(
                    state_store=store,
                    credential_store=CredentialStore(
                        self.db_path.parent / "credentials"
                    ),
                    output_dir=self.db_path.parent / "exports",
                )
                ready_ids = {
                    item.account_id
                    for item in inventory.list_accounts(statuses=("READY",))
                }
                missing = [item for item in account_ids if item not in ready_ids]
                cards_ready = report.card.available > 0
                if checkout_goal == "pricing_page":
                    resources_ready = not missing and bool(account_ids)
                    detail = (
                        f"仅验证套餐页目标不要求信用卡，已选 READY 账户 {len(account_ids)}"
                        + (f"，缺失 {len(missing)}" if missing else "")
                        + "。"
                    )
                else:
                    resources_ready = (
                        cards_ready and not missing and bool(account_ids)
                    )
                    detail = (
                        f"可用信用卡 {report.card.available}，已选 READY 账户 {len(account_ids)}"
                        + (f"，缺失 {len(missing)}" if missing else "")
                        + "。"
                    )
            elif scope == "account_provision":
                resources_ready = report.email.available > 0
                detail = (
                    f"可用邮箱 {report.email.available}（创建账户不需要信用卡）。"
                )
            else:
                resources_ready = resource_report_ready_for_scope(report, scope)
                detail = (
                    f"可用邮箱 {report.email.available}，可用信用卡 {report.card.available}。"
                )
        except Exception:
            resources_ready = False
            detail = "无法读取资源状态。"
        checks.append(
            {
                "key": "resources",
                "label": "资源可用性",
                "status": "ready" if resources_ready else "blocked",
                "detail": detail,
            }
        )
        return {"ok": all(check["status"] == "ready" for check in checks), "checks": checks}

    def state(self) -> Optional[RunState]:
        with self._lock:
            if self._run is None:
                return None
            self._sync_from_db()
            return self._run.state()

    def tick(self) -> Optional[RunState]:
        return self.state()

    def decide(self, decision: str) -> RunState:
        with self._lock:
            if self._run is None:
                raise ProductionEngineError("NO_ACTIVE_RUN", "没有真实运行")
            request = self._broker.current_request()
            normalized = {
                "verify": InteractionDecision.VERIFY_MANUAL.value,
                "defer": InteractionDecision.DEFER_ACCOUNT.value,
                "terminate": InteractionDecision.ABORT_BATCH.value,
            }.get(decision, decision)
            if request is None:
                raise ProductionEngineError(
                    "DECISION_NOT_ALLOWED",
                    "当前没有等待人工的请求",
                )
            self._broker.resolve(request["request_id"], normalized)
            if normalized == InteractionDecision.ABORT_BATCH.value:
                self._stop_event.set()
            self._run.operator_request = None
            self._run.waiting_reason = ""
            self._run.status = "RUNNING"
            self._run.updated_at = _now()
            self._append_event("INFO", "已提交人工决定，工作流将重新验证页面。")
            self._persist()
            return self._run.state()

    def stop(self) -> RunState:
        with self._lock:
            if self._run is None:
                raise ProductionEngineError("NO_ACTIVE_RUN", "没有真实运行")
            if self._run.status not in {"RUNNING", "NEEDS_OPERATOR"}:
                raise ProductionEngineError(
                    "STOP_NOT_AVAILABLE",
                    "当前真实运行不在可停止状态",
                )
            request = self._broker.current_request()
            if request is not None:
                self._broker.resolve(
                    request["request_id"],
                    InteractionDecision.ABORT_BATCH.value,
                )
            self._stop_event.set()
            self._run.status = "STOPPING"
            self._run.waiting_reason = "已请求安全停止，正在等待当前等待点退出…"
            self._run.current_action = "已请求安全停止，等待当前步骤在账户边界结束"
            self._run.updated_at = _now()
            self._append_event("WARN", self._run.waiting_reason)
            self._persist()
            return self._run.state()

    def force_stop(self) -> RunState:
        """End the control-plane run when safe stop cannot finish in time."""
        with self._lock:
            if self._run is None:
                raise ProductionEngineError("NO_ACTIVE_RUN", "没有真实运行")
            orphan = self._orphan_after_restart and self._run.status == "NEEDS_OPERATOR"
            dead_error = (
                self._run.status == "NEEDS_OPERATOR"
                and self._run.operator_request is None
                and bool(self._run.last_error)
            )
            if self._run.status != "STOPPING" and not orphan and not dead_error:
                raise ProductionEngineError(
                    "FORCE_STOP_NOT_AVAILABLE",
                    "只有停止中或控制台重启后残留的运行可以强制结束",
                )
            request = self._broker.current_request()
            if request is not None:
                try:
                    self._broker.resolve(
                        request["request_id"],
                        InteractionDecision.ABORT_BATCH.value,
                    )
                except ProductionEngineError:
                    pass
            self._stop_event.set()
            self._run.status = "TERMINATED"
            self._run.exit_code = 130
            self._run.current_action = "控制台运行已强制结束，浏览器现场需人工核对"
            self._run.waiting_reason = (
                "控制台运行已结束；浏览器现场与资源状态请人工确认。"
            )
            self._append_event(
                "WARN",
                "控制台运行已强制结束，浏览器现场与资源状态需人工确认。",
            )
            self._persist()
            self._orphan_after_restart = False
            return self._run.state()

    def _worker(self) -> None:
        with self._lock:
            assert self._run is not None
            parameters = dict(self._run.parameters)
            run_id = self._run.run_id
        policy = WebInteractionPolicy(
            self._broker,
            session_id=run_id,
            on_request=self._on_operator_request,
        )
        confirmer = (
            WebManualIPConfirmer(
                self._broker,
                session_id=run_id,
                on_request=self._on_operator_request,
            )
            if parameters.get("network") == "manual"
            else None
        )
        from .task_scope import parse_account_ids

        account_ids = parse_account_ids(parameters.get("account_ids", ""))

        def status_callback(kind: str, detail: Any = None) -> None:
            self._on_status(kind, detail)

        options = RuntimeOptions(
            auto_mode=parameters["interaction"] == "unattended",
            rotation_mode=(
                "manual-hotspot"
                if parameters["network"] == "manual"
                else "ikuai-single"
            ),
            max_accounts=int(parameters["target_count"]),
            state_db_path=str(self.db_path),
            input_dir=str(self.input_dir),
            execution_lock_dir=str(self.lock_dir),
            interaction_policy=(
                None if parameters["interaction"] == "unattended" else policy
            ),
            stop_event=self._stop_event,
            run_scope=parameters.get("run_scope"),
            account_ids=account_ids or None,
            checkout_goal=parameters.get("checkout_goal") or None,
            browser_backend=parameters.get("browser_backend") or None,
            network_confirmer=confirmer,
            status_callback=status_callback,
        )
        try:
            exit_code = asyncio.run(
                orchestrator.main(
                    resume_session_id=parameters.get("resume_session_id") or None,
                    runtime_options=options,
                )
            )
        except BaseException as exc:
            with self._lock:
                if self._run is None or self._run.status == "TERMINATED":
                    return
                self._run.exit_code = 1
                if self._run.status == "STOPPING":
                    self._run.status = "TERMINATED"
                    self._run.waiting_reason = ""
                    self._append_event("WARN", "真实运行已安全停止。")
                else:
                    redacted = _redact_text(
                        f"{type(exc).__name__}: {exc}"
                    )[:300]
                    stage = self._error_stage()
                    self._run.last_error = redacted
                    self._run.status = "NEEDS_OPERATOR"
                    self._run.waiting_reason = (
                        f"真实运行异常终止（{stage}），请检查浏览器现场和执行履历。"
                    )
                    self._run.current_action = (
                        f"运行异常终止（{stage}）：{redacted}"
                    )
                    self._append_event("ERROR", f"真实运行异常：{redacted}")
                self._persist()
            return
        with self._lock:
            if self._run is None:
                return
            self._run.exit_code = int(exit_code)
            self._sync_from_db()
            if self._run.status in {"RUNNING", "STOPPING"}:
                stopped = self._stop_event.is_set() or self._run.status == "STOPPING"
                self._run.status = (
                    "TERMINATED"
                    if stopped
                    else "COMPLETED"
                    if exit_code == 0
                    else "FAILED"
                )
                self._run.waiting_reason = ""
                self._append_event(
                    "WARN" if stopped else "INFO" if exit_code == 0 else "ERROR",
                    "真实运行已安全停止。"
                    if stopped
                    else "真实运行已完成。"
                    if exit_code == 0
                    else "真实运行结束，请检查履历。",
                    "RUN_TERMINATED"
                    if stopped
                    else "RUN_COMPLETED"
                    if exit_code == 0
                    else "RUN_FAILED",
                )
            self._run.updated_at = _now()
            self._persist()

    def _on_operator_request(self, request: Dict[str, Any]) -> None:
        with self._lock:
            if self._run is None or self._run.status == "TERMINATED":
                return
            payload = dict(request)
            payload["account_index"] = self._run.account_index
            payload["account_total"] = max(
                int(self._run.parameters.get("target_count", "1") or "1"), 1
            )
            payload["browser_started"] = self._run.browser_state in {
                "launching",
                "opened",
            }
            payload.pop("last_error", None)
            view = OperatorRequestView(**payload)
            self._run.operator_request = view
            self._run.status = "NEEDS_OPERATOR"
            self._run.waiting_reason = view.description
            self._run.updated_at = _now()
            self._append_event("WARN", f"等待人工处理：{view.description}", view.step_id)
            self._persist()

    def _error_stage(self) -> str:
        """Return a human-readable stage for a worker exception."""
        state = self._run.browser_state if self._run is not None else ""
        if state == "network_wait":
            return "网络确认阶段"
        if state in {"preflight", "preflight_failed", "ready"}:
            return "预检/准备阶段"
        if state in {"launching", "launch_failed"}:
            return "浏览器启动阶段"
        if state == "opened":
            return "页面步骤阶段"
        return "准备阶段"

    def _on_status(self, kind: str, detail: Any = None) -> None:
        """Apply a worker milestone emitted by the orchestrator."""
        with self._lock:
            if self._run is None or self._run.status == "TERMINATED":
                return
            if kind == "cloak_preflight":
                self._run.current_action = "正在预检浏览器运行环境"
                self._run.browser_state = "preflight"
            elif kind == "cloak_preflight_ok":
                self._run.current_action = "浏览器预检完成，准备网络与资源"
                self._run.browser_state = "ready"
                self._append_event("INFO", "浏览器预检完成。")
            elif kind == "cloak_preflight_failed":
                reason = _redact_text(str(detail or ""))[:300]
                self._run.current_action = "浏览器预检失败，本次运行将结束"
                self._run.browser_state = "preflight_failed"
                self._run.last_error = reason
                self._append_event(
                    "ERROR",
                    "浏览器预检失败，本次运行将结束。"
                    + (f" 原因：{reason}" if reason else ""),
                )
            elif kind == "network_waiting":
                self._run.current_action = "等待网络确认（尚未启动浏览器）"
                self._run.browser_state = "network_wait"
            elif kind == "resource_acquiring":
                self._run.current_action = "正在领取账户资源"
                self._run.browser_state = "resources"
            elif kind == "account_started":
                try:
                    self._run.account_index = int(detail or 0)
                except (TypeError, ValueError):
                    self._run.account_index = 0
                self._run.current_action = (
                    f"正在处理第 {self._run.account_index} 个账户"
                )
                self._append_event(
                    "INFO",
                    f"开始处理第 {self._run.account_index} 个账户。",
                )
            elif kind == "browser_launching":
                self._run.current_action = "正在启动浏览器"
                self._run.browser_state = "launching"
                self._append_event("INFO", "正在启动隔离浏览器…")
            elif kind == "browser_opened":
                self._run.current_action = "浏览器已打开，正在执行页面步骤"
                self._run.browser_state = "opened"
                self._append_event("INFO", "浏览器已打开，正在执行页面步骤。")
            elif kind == "browser_launch_failed":
                self._run.current_action = (
                    "浏览器启动失败，将按流程重试或移交人工"
                )
                self._run.browser_state = "launch_failed"
                self._append_event(
                    "ERROR",
                    "浏览器启动失败，将按流程重试或移交人工处理。",
                )
            elif kind == "account_completed":
                self._append_event("INFO", f"第 {detail} 个账户处理完成。")
            self._run.updated_at = _now()
            self._persist()

    def _sync_from_db(self) -> None:
        if self._run is None:
            return
        if self._orphan_after_restart or self._run.status in {
            "TERMINATED",
            "COMPLETED",
            "FAILED",
        }:
            return
        try:
            store = StateStore(self.db_path)
            store.initialize()
            sessions = store.list_sessions()
        except Exception:
            return
        resume_id = self._run.parameters.get("resume_session_id", "")
        candidate = None
        if resume_id:
            candidate = next(
                (session for session in sessions if session.session_id == resume_id),
                None,
            )
        if candidate is None:
            candidates = [
                session
                for session in sessions
                if session.session_id not in self._known_session_ids
            ]
            if candidates:
                candidate = sorted(candidates, key=lambda item: item.created_at)[-1]
        if candidate is None:
            return
        self._known_session_ids.add(candidate.session_id)
        self._run.workflow_session_id = candidate.session_id
        step_id = _STAGE_TO_STEP.get(candidate.stage, "NETWORK_READY")
        phase_index, step_index = _step_position(
            step_id,
            self._run.parameters.get("task_type"),
        )
        self._run.phase_index = phase_index
        self._run.step_index = step_index
        self._run.current_step_id = step_id
        mapped_status = {
            "PENDING": "RUNNING",
            "SUCCESS": "COMPLETED",
            "COMPLETED": "COMPLETED",
            "RETRYABLE_FAILURE": "FAILED",
            "BUSINESS_REJECTED": "FAILED",
            "FATAL": "FAILED",
        }.get(candidate.status, candidate.status)
        if (
            self._run.operator_request is None
            and not self._orphan_after_restart
            and self._run.status not in {"STOPPING", "TERMINATED"}
        ):
            self._run.status = mapped_status
        if mapped_status in {"NEEDS_OPERATOR", "UNKNOWN_SIDE_EFFECT"}:
            self._run.waiting_reason = candidate.last_error or (
                "需要人工核对真实结果。"
                if mapped_status == "UNKNOWN_SIDE_EFFECT"
                else "工作流等待人工处理。"
            )
        if candidate.stage != self._last_stage:
            self._last_stage = candidate.stage
            self._append_event("INFO", f"工作流阶段：{candidate.stage}", step_id)
        self._run.updated_at = candidate.updated_at or _now()
        self._persist()

    def _append_event(self, level: str, message: str, step_id: str = "") -> None:
        if self._run is None:
            return
        self._run.events.append(
            RunEvent(_now(), level, _redact_text(message), _redact_text(step_id))
        )
        self._run.events = self._run.events[-100:]

    def _persist(self) -> None:
        if self._run is None:
            return
        self.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        payload = asdict(self._run)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{self.snapshot_path.name}.",
            suffix=".tmp",
            dir=str(self.snapshot_path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            # Windows/sandbox can reject replacing an existing file; mirror
            # the demo engine's remove-then-replace with a small retry.
            if self.snapshot_path.exists():
                try:
                    self.snapshot_path.unlink()
                except OSError:
                    pass
            for attempt in range(3):
                try:
                    os.replace(tmp_name, self.snapshot_path)
                    break
                except PermissionError:
                    if attempt == 2:
                        raise
                    time.sleep(0.05)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def _validate_parameters(self, parameters: Dict[str, str]) -> Dict[str, str]:
        from .task_scope import TaskScopeError, normalize_run_parameters, parse_account_ids

        payload = dict(parameters)
        payload.pop("execution_mode", None)
        try:
            validated = normalize_run_parameters(
                {
                    **payload,
                    "execution_mode": "production",
                }
            )
        except TaskScopeError as exc:
            raise ProductionEngineError(exc.code, exc.message) from exc
        account_ids = parse_account_ids(validated.get("account_ids", ""))
        if validated.get("run_scope") == "checkout_fulfillment" and account_ids:
            validated["target_count"] = str(len(account_ids))
        return validated


__all__ = [
    "ProductionEngineError",
    "ProductionRunEngine",
    "RuntimeOptions",
    "WebInteractionBroker",
    "WebInteractionPolicy",
]
