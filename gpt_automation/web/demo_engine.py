"""Demo run engine for the local dashboard.

This engine is explicitly a demonstration control plane. It does not open a
browser, does not reserve resources and never calls RegistrationWorkflow.
Production wiring to the real workflow is intentionally left to a later
iteration; the UI marks every demo run as such.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from .contracts import (
    DemoRunState,
    ParamField,
    ParamGroup,
    PhaseView,
    RunEvent,
    RunParametersSchema,
    StepView,
)

PROVISION_PHASES = (
    PhaseView(
        phase_id="provision",
        title="创建账户",
        description="网络就绪、密码注册、资料与 MFA 验证后导出 READY 账户。",
        steps=(
            StepView("NETWORK_READY", "网络准备", "确认当前 IP 与网络链路就绪。"),
            StepView("AUTH_ENTRY_READY", "进入注册入口", "打开 ChatGPT 登录/注册页面。"),
            StepView("ACCOUNT_ROUTE_RESOLVED", "账户路径判定", "根据页面状态判定注册或登录路径。"),
            StepView("SIGNUP_PASSWORD_VERIFIED", "创建账户密码", "选择“使用密码继续”并创建 ChatGPT 密码。"),
            StepView("PROFILE_VERIFIED", "资料设置", "填写姓名、年龄等注册资料。"),
            StepView("ACCOUNT_AUTHENTICATED", "账户认证完成", "确认账户已登录并可继续。"),
            StepView("MFA_VERIFIED", "MFA 绑定验证", "绑定 TOTP 并验证验证码，需要人工确认。"),
            StepView("ACCOUNT_READY_EXPORTED", "账户就绪", "导出 READY 账户并保留浏览器 profile。"),
        ),
    ),
)

CHECKOUT_PHASES = (
    PhaseView(
        phase_id="checkout",
        title="开通会员",
        description="选择 READY 账户、确认登录、优惠支付、取消订阅并落库。",
        steps=(
            StepView("INVENTORY_LOGIN_VERIFIED", "登录健康检查", "复用 profile 或密码 + TOTP 恢复登录态。"),
            StepView("PRICING_PAGE_VERIFIED", "套餐金额确认", "确认目标 Plus 套餐首期金额为 0（免费优惠）。"),
            StepView("OFFER_DECISION_VERIFIED", "接受优惠进入支付", "点击免费优惠并进入 Stripe Checkout。"),
            StepView("CHECKOUT_READY", "Checkout 填写", "填写信用卡与账单地址。"),
            StepView("PAYMENT_VERIFIED", "支付确认", "确认支付结果，需要人工核验。"),
            StepView("CANCELLATION_VERIFIED", "取消订阅", "确认订阅状态并执行取消。"),
            StepView("RESULT_PERSISTED", "结果落库", "写入结果、处置资源并完成会话。"),
        ),
    ),
)

FULL_PHASES = (
    PhaseView(
        phase_id="phase1",
        title="Phase 1 · 账户创建",
        description="网络就绪、进入注册入口、选择密码注册并完成账户资料。",
        steps=(
            StepView("NETWORK_READY", "网络准备", "确认当前 IP 与网络链路就绪。"),
            StepView("AUTH_ENTRY_READY", "进入注册入口", "打开 ChatGPT 登录/注册页面。"),
            StepView("ACCOUNT_ROUTE_RESOLVED", "账户路径判定", "根据页面状态判定注册或登录路径。"),
            StepView("SIGNUP_PASSWORD_VERIFIED", "创建账户密码", "选择“使用密码继续”并创建 ChatGPT 密码。"),
            StepView("PROFILE_VERIFIED", "资料设置", "填写姓名、年龄等注册资料。"),
            StepView("ACCOUNT_AUTHENTICATED", "账户认证完成", "确认账户已登录并可继续。"),
        ),
    ),
    PhaseView(
        phase_id="phase2",
        title="Phase 2 · MFA 验证",
        description="绑定 TOTP 并验证 MFA；此阶段支持人工接管。",
        steps=(
            StepView("MFA_VERIFIED", "MFA 绑定验证", "绑定 TOTP 并验证验证码，需要人工确认。"),
        ),
    ),
    PhaseView(
        phase_id="phase3",
        title="Phase 3 · 优惠与支付",
        description="核对免费优惠金额、填写卡片并确认支付结果；支付不可自动重试。",
        steps=(
            StepView("PRICING_PAGE_VERIFIED", "套餐金额确认", "确认目标 Plus 套餐首期金额为 0（免费优惠）。"),
            StepView("OFFER_DECISION_VERIFIED", "接受优惠进入支付", "点击免费优惠并进入 Stripe Checkout。"),
            StepView("CHECKOUT_READY", "Checkout 填写", "填写信用卡与账单地址。"),
            StepView("PAYMENT_VERIFIED", "支付确认", "确认支付结果，需要人工核验。"),
        ),
    ),
    PhaseView(
        phase_id="phase4",
        title="Phase 4 · 取消与落库",
        description="确认订阅状态、执行取消并写入最终结果。",
        steps=(
            StepView("CANCELLATION_VERIFIED", "取消订阅", "确认订阅状态并执行取消。"),
            StepView("RESULT_PERSISTED", "结果落库", "写入结果、处置资源并完成会话。"),
        ),
    ),
)

# Default console projection is two-stage; full remains advanced/compat only.
PHASES = PROVISION_PHASES + CHECKOUT_PHASES


def phases_for_task_type(task_type: str | None) -> tuple[PhaseView, ...]:
    """Return the user-facing phase track for a console task type."""
    key = (task_type or "provision").strip().lower()
    if key in {"checkout", "checkout_fulfillment"}:
        return CHECKOUT_PHASES
    if key == "full":
        return FULL_PHASES
    return PROVISION_PHASES


PARAMETERS_SCHEMA = RunParametersSchema(
    groups=(
        ParamGroup(
            key="run",
            label="运行方式",
            fields=(
                ParamField(
                    "mode", "模式", "select", "new", True, ["new", "resume"]
                ),
                ParamField("resume_session_id", "恢复 Session ID", "text", ""),
            ),
        ),
        ParamGroup(
            key="policy",
            label="策略",
            fields=(
                ParamField(
                    "interaction",
                    "交互策略",
                    "select",
                    "assisted",
                    True,
                    ["assisted", "unattended"],
                ),
                ParamField(
                    "network",
                    "网络策略",
                    "select",
                    "manual",
                    True,
                    ["manual", "auto"],
                ),
            ),
        ),
        ParamGroup(
            key="task",
            label="业务任务",
            fields=(
                ParamField(
                    "task_type",
                    "任务类型",
                    "select",
                    "provision",
                    True,
                    ["provision", "checkout", "full"],
                ),
                ParamField(
                    "account_ids",
                    "READY 账户",
                    "text",
                    "",
                    False,
                    placeholder="checkout 时填写，多个 account_id 用逗号分隔",
                ),
            ),
        ),
        ParamGroup(
            key="batch",
            label="批次参数",
            fields=(
                ParamField(
                    "target_count",
                    "目标数量",
                    "number",
                    "1",
                    True,
                    placeholder="1-100",
                ),
            ),
        ),
        ParamGroup(
            key="browser",
            label="浏览器执行后端",
            fields=(
                ParamField(
                    "browser_backend",
                    "浏览器执行后端",
                    "select",
                    "cloak",
                    True,
                    ["cloak", "firefox_private"],
                ),
            ),
        ),
    )
)

_OPERATOR_WAIT_STEPS = {
    "MFA_VERIFIED": (
        "请在浏览器现场完成 MFA 绑定并输入验证码，程序将重新检测页面后置条件。"
    ),
    "INVENTORY_LOGIN_VERIFIED": (
        "请确认账户已登录成功；profile 失效时可用密码 + TOTP 恢复，程序将重新检测页面。"
    ),
    "PAYMENT_VERIFIED": "请确认支付结果页面；结果未知时禁止自动重试。",
    "CANCELLATION_VERIFIED": "请确认取消订阅后的状态页面。",
}


class DemoEngineError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class _Run:
    def __init__(self, parameters: Dict[str, str]) -> None:
        self.session_id = "demo-" + uuid.uuid4().hex[:12]
        self.mode = "demo"
        self.parameters = dict(parameters)
        self.phase_index = 0
        self.step_index = 0
        self.status = "RUNNING"
        self.waiting_reason = ""
        self.events: List[RunEvent] = []
        self.started_at = self._now()
        self.updated_at = self.started_at
        self._stage_seconds = 0.0
        self._append_event("INFO", "演示运行已启动", "RUN_STARTED")
        self._append_event("INFO", "演示引擎不执行真实浏览器操作", "")

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def _phases(self) -> tuple[PhaseView, ...]:
        return phases_for_task_type(self.parameters.get("task_type", "provision"))

    def _append_event(self, level: str, message: str, step_id: str = "") -> None:
        self.events.append(
            RunEvent(at=self._now(), level=level, message=message, step_id=step_id)
        )

    def advance(self, seconds: float) -> None:
        """Advance the demo state machine by ``seconds`` of simulated work."""
        if self.status != "RUNNING":
            return
        self._stage_seconds += seconds
        phase = self._phases()[self.phase_index]
        step = phase.steps[self.step_index]
        if step.step_id in _OPERATOR_WAIT_STEPS:
            self.waiting_reason = _OPERATOR_WAIT_STEPS[step.step_id]
            self.status = "NEEDS_OPERATOR"
            self._append_event("WARN", f"等待人工确认：{step.title}", step.step_id)
            self.updated_at = self._now()
            return
        if self._stage_seconds >= 0.5:
            self._stage_seconds = 0.0
            self._append_event("INFO", f"步骤完成：{step.title}", step.step_id)
            self._next_step()

    def _next_step(self) -> None:
        phases = self._phases()
        phase = phases[self.phase_index]
        if self.step_index + 1 < len(phase.steps):
            self.step_index += 1
            self.updated_at = self._now()
            return
        if self.phase_index + 1 < len(phases):
            self.phase_index += 1
            self.step_index = 0
            self._append_event(
                "INFO", f"进入 {phases[self.phase_index].title}", ""
            )
            self.updated_at = self._now()
            return
        self.status = "COMPLETED"
        self.waiting_reason = ""
        self._append_event("INFO", "演示运行完成", "RUN_COMPLETED")
        self.updated_at = self._now()

    def decide(self, decision: str) -> None:
        if self.status != "NEEDS_OPERATOR":
            raise DemoEngineError(
                "DECISION_NOT_ALLOWED", "run is not waiting for operator"
            )
        if decision == "verify":
            self.status = "RUNNING"
            self.waiting_reason = ""
            self._append_event(
                "INFO",
                "已提交人工验证，等待后端重新检测页面后置条件",
                "",
            )
            # Operator confirmed the current wait step; advance past it so the
            # demo keeps progressing into the next step/phase.
            self._next_step()
            self.updated_at = self._now()
            return
        if decision == "defer":
            self.status = "NEEDS_OPERATOR"
            self._append_event("WARN", "已延期：继续保留当前现场", "")
            self.updated_at = self._now()
            return
        if decision == "terminate":
            self.status = "TERMINATED"
            self.waiting_reason = ""
            self._append_event("WARN", "已按操作员决定终止演示运行", "")
            self.updated_at = self._now()
            return
        raise DemoEngineError(
            "DECISION_NOT_ALLOWED", f"unknown decision: {decision}"
        )


class DemoRunEngine:
    """Single-demo-run in-memory engine with JSON snapshot persistence."""

    def __init__(self, snapshot_path: str | Path) -> None:
        self.snapshot_path = Path(snapshot_path)
        self._lock = threading.RLock()
        self._run: Optional[_Run] = None
        self._load()

    def _load(self) -> None:
        if not self.snapshot_path.exists():
            return
        try:
            data = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
            run = _Run(data.get("parameters", {}))
            run.session_id = data.get("session_id", run.session_id)
            run.phase_index = int(data.get("phase_index", 0))
            run.step_index = int(data.get("step_index", 0))
            run.status = data.get("status", "RUNNING")
            run.waiting_reason = data.get("waiting_reason", "")
            run.started_at = data.get("started_at", run.started_at)
            run.updated_at = data.get("updated_at", run.updated_at)
            run.events = [
                RunEvent(**event) for event in data.get("events", [])
            ]
            self._run = run
        except (OSError, ValueError, TypeError, KeyError):
            self._run = None

    def start(self, parameters: Dict[str, str]) -> DemoRunState:
        validated = self._validate_parameters(parameters)
        with self._lock:
            self._run = _Run(validated)
            self._persist()
            return self.state()

    def tick(self, seconds: float = 1.0) -> DemoRunState:
        with self._lock:
            if self._run is not None:
                self._run.advance(seconds)
                self._persist()
            return self.state()

    def decide(self, decision: str) -> DemoRunState:
        with self._lock:
            if self._run is None:
                raise DemoEngineError("NO_ACTIVE_RUN", "no active demo run")
            self._run.decide(decision)
            self._persist()
            return self.state()

    def state(self) -> Optional[DemoRunState]:
        with self._lock:
            if self._run is None:
                return None
            run = self._run
            phases = run._phases()
            phase = phases[min(run.phase_index, len(phases) - 1)]
            step = phase.steps[min(run.step_index, len(phase.steps) - 1)]
            return DemoRunState(
                session_id=run.session_id,
                mode="demo",
                parameters=dict(run.parameters),
                phase_index=run.phase_index,
                step_index=run.step_index,
                status=run.status,
                current_step_id=step.step_id,
                waiting_reason=run.waiting_reason,
                events=list(run.events),
                started_at=run.started_at,
                updated_at=run.updated_at,
            )

    def _persist(self) -> None:
        run = self._run
        if run is None:
            return
        payload = {
            "session_id": run.session_id,
            "parameters": run.parameters,
            "phase_index": run.phase_index,
            "step_index": run.step_index,
            "status": run.status,
            "waiting_reason": run.waiting_reason,
            "started_at": run.started_at,
            "updated_at": run.updated_at,
            "events": [vars(event) for event in run.events],
        }
        self.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{self.snapshot_path.name}.",
            suffix=".tmp",
            dir=str(self.snapshot_path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
            # os.replace can hit sandbox permission issues on Windows when the
            # destination exists; remove first, then move.
            if self.snapshot_path.exists():
                try:
                    self.snapshot_path.unlink()
                except OSError:
                    pass
            os.replace(tmp_name, self.snapshot_path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    @staticmethod
    def _validate_parameters(parameters: Dict[str, str]) -> Dict[str, str]:
        from .task_scope import TaskScopeError, normalize_run_parameters

        payload = dict(parameters)
        payload.pop("execution_mode", None)
        try:
            return normalize_run_parameters(
                {
                    **payload,
                    "execution_mode": "demo",
                }
            )
        except TaskScopeError as exc:
            raise DemoEngineError(exc.code, exc.message) from exc

