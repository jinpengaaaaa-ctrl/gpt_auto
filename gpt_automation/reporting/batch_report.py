"""批次诊断报告 — 终端摘要与 `runtime/reports/batch_errors_*.txt`.
报告只做「投影」：它读取批次调度已经结算的 `AccountRunResult` 与资源状态，
不参与账户状态判定、资源释放或重试决策。写入失败不得改变账户状态，也不得
改变批次退出码语义。
安全边界：允许保存邮箱、Session ID、阶段、稳定错误代码、脱敏原因与证据摘要；
禁止保存密码、验证码、TOTP Secret、OAuth Token、卡片、地址、原始输入行或
完整堆栈。
"""
from __future__ import annotations
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional
from gpt_automation.workflow.models import AccountRunResult, AccountStatus
logger = logging.getLogger(__name__)
class BatchOutcomeCategory(str, Enum):
    SUCCESS = "SUCCESS"
    AUTO_SKIPPED = "AUTO_SKIPPED"
    BLOCKED = "BLOCKED"
    BUSINESS_REJECTED = "BUSINESS_REJECTED"
class RetryDisposition(str, Enum):
    NONE = "NONE"                    # 成功或无需再次执行
    USER_DECIDES = "USER_DECIDES"    # 已移入 skipped，用户决定是否放回
    RETRYABLE = "RETRYABLE"          # 无副作用业务拒绝，可再次执行
    MANUAL_REVIEW = "MANUAL_REVIEW"  # 需要人工检查，禁止自动重试
@dataclass(frozen=True)
class BatchOutcomeEntry:
    email: str
    session_id: str
    category: BatchOutcomeCategory
    status: str
    stage: str
    stage_label: str
    reason_text: str
    retry_disposition: RetryDisposition
    finished_at: str
    last_error: str = ""
    resource_state: str = ""
    in_input: bool = False
    in_skipped: bool = False
    held_archived: bool = False
    held_disposition: str = ""
    evidence_path: str = ""
    page_url: str = ""
@dataclass
class BatchRunReport:
    batch_id: str
    run_scope: str
    interaction_mode: str
    rotation_mode: str
    started_at: str
    build: str = ""
    ended_at: str = ""
    batch_end_reason: str = ""
    report_status: str = "incomplete"
    browser_backend: str = ""
    binary_tier: str = ""
    license_configured: Optional[bool] = None
    entries: list[BatchOutcomeEntry] = field(default_factory=list)
    batch_issues: list[str] = field(default_factory=list)
    @property
    def success_count(self) -> int:
        return sum(
            entry.category is BatchOutcomeCategory.SUCCESS
            for entry in self.entries
        )
    @property
    def skipped_count(self) -> int:
        return sum(
            entry.category is BatchOutcomeCategory.AUTO_SKIPPED
            for entry in self.entries
        )
    @property
    def blocked_count(self) -> int:
        return sum(
            entry.category is BatchOutcomeCategory.BLOCKED
            for entry in self.entries
        )
    @property
    def rejected_count(self) -> int:
        return sum(
            entry.category is BatchOutcomeCategory.BUSINESS_REJECTED
            for entry in self.entries
        )
    @property
    def failed_entries(self) -> list[BatchOutcomeEntry]:
        return [
            entry
            for entry in self.entries
            if entry.category is not BatchOutcomeCategory.SUCCESS
        ]
    def add_entry(self, entry: BatchOutcomeEntry) -> None:
        self.entries.append(entry)
    def update_entry(self, email: str, **changes: Any) -> None:
        """Replace one entry (e.g. after a deferred archive finally succeeds)."""
        from dataclasses import replace
        for index, entry in enumerate(self.entries):
            if entry.email == email and entry.status == "SKIP_ARCHIVE_FAILED":
                self.entries[index] = replace(entry, **changes)
                return
    def add_batch_issue(self, message: str) -> None:
        self.batch_issues.append(message)
    def finalize(self, batch_end_reason: str) -> None:
        self.ended_at = _now()
        self.batch_end_reason = batch_end_reason
        self.report_status = "completed"
# ── 分类与处置（只做投影，不做业务判定） ──────────────────────────────
def classify_result(result: AccountRunResult, run_scope: str) -> BatchOutcomeCategory:
    """Map an already-settled result to a report category."""
    if result.status in {
        AccountStatus.SUCCESS,
        AccountStatus.ACCOUNT_READY,
        AccountStatus.VERIFICATION_COMPLETE,
    }:
        return BatchOutcomeCategory.SUCCESS
    if result.raw_status == "SKIPPED":
        return BatchOutcomeCategory.AUTO_SKIPPED
    if result.status is AccountStatus.BUSINESS_REJECTED:
        return BatchOutcomeCategory.BUSINESS_REJECTED
    if result.status in {
        AccountStatus.NEEDS_OPERATOR,
        AccountStatus.UNKNOWN_SIDE_EFFECT,
        AccountStatus.FATAL,
        AccountStatus.RETRYABLE_FAILURE,
    }:
        return BatchOutcomeCategory.BLOCKED
    return BatchOutcomeCategory.BLOCKED
def disposition_for(result: AccountRunResult) -> RetryDisposition:
    if result.status in {
        AccountStatus.SUCCESS,
        AccountStatus.ACCOUNT_READY,
        AccountStatus.VERIFICATION_COMPLETE,
    }:
        return RetryDisposition.NONE
    if result.raw_status == "SKIPPED":
        return RetryDisposition.USER_DECIDES
    if result.raw_status in {
        "ACCOUNT_ALREADY_EXISTS",
        "UNKNOWN_SIDE_EFFECT",
        "STATUS_UNCONFIRMED",
        "PAYMENT_UNKNOWN",
        "SKIP_ARCHIVE_FAILED",
    }:
        return RetryDisposition.MANUAL_REVIEW
    if result.status is AccountStatus.NEEDS_OPERATOR:
        return RetryDisposition.MANUAL_REVIEW
    if result.status is AccountStatus.BUSINESS_REJECTED:
        return RetryDisposition.RETRYABLE
    if result.status is AccountStatus.RETRYABLE_FAILURE:
        return RetryDisposition.RETRYABLE
    return RetryDisposition.MANUAL_REVIEW
# ── 中文原因映射 ──────────────────────────────────────────────────────
_STAGE_LABELS = {
    "CREATED": "会话创建",
    "NETWORK_PENDING": "网络准备",
    "NETWORK_READY": "网络就绪",
    "ACCOUNT_VERIFIED": "登录验证",
    "PASSWORD_PENDING": "密码设置",
    "PASSWORD_VERIFIED": "密码确认",
    "MFA_PENDING": "MFA 设置",
    "MFA_VERIFIED": "MFA 确认",
    "PRICING_VERIFIED": "套餐选择",
    "OFFER_VERIFIED": "优惠确认",
    "PAYMENT_PENDING": "支付提交",
    "PAYMENT_VERIFIED": "支付确认",
    "CANCELLATION_PENDING": "取消订阅",
    "CANCELLATION_VERIFIED": "取消确认",
    "RESULT_PERSISTED": "结果保存",
    "COMPLETED": "完成",
}
_REASON_TABLE = {
    "PREFLIGHT_FAILED": ("浏览器或运行环境预检失败", "修复环境后重新执行"),
    "IP_NOT_ROTATED": ("IP 未能成功轮换（未变化 / 检测失败 / 浏览器启动失败）", "检查手机热点或路由器后重试"),
    "IP_HISTORY_PERSIST_FAILED": ("IP 历史记录保存失败", "检查磁盘权限后重试"),
    "EMAIL_CODE_READER_FAILED": ("邮箱验证码读取失败", "检查邮箱取码通道后重试"),
    "EMAIL_CODE_NOT_SIX_DIGITS": ("取得的邮箱验证码不是 6 位数字", "确认邮箱邮件后重试"),
    "EMAIL_CODE_INPUT_MISMATCH": ("验证码输入框与实际验证码不一致", "人工核对后重试"),
    "EMAIL_CODE_SUBMIT_UNCONFIRMED": ("验证码提交结果无法确认", "人工检查页面后继续"),
    "EMAIL_CODE_POSTCONDITION_FAILED": ("验证码提交后页面未进入预期状态", "人工检查页面"),
    "EMAIL_CODE_RESEND_BLOCKED": ("验证码邮件再送信不可用或被限流", "人工检查页面后重试"),
    "EMAIL_CODE_GIVE_UP": ("邮箱验证码多次取码仍未成功", "人工确认邮件后决定跳过或重试"),
    "EMAIL_VERIFY_FAILED": ("邮箱验证失败", "人工检查邮箱后重试"),
    "MFA_UNCONFIRMED": ("MFA 状态未确认", "人工核对 MFA 设置"),
    "ACCOUNT_ALREADY_EXISTS": ("该邮箱可能已经创建过账户", "人工核对，禁止重复注册"),
    "NO_FREE_OFFER": (
        "登录已成功，主页未发现免费优惠入口。不是认证失败，而是没有免费会员",
        "账户已回 READY 并标记 REVIEW_REQUIRED，查看套餐页截图后决定是否重试",
    ),
    "NO_TARGET_OFFER": ("账号已创建但未命中配置目标优惠", "账号保留在库存，不进入默认开通池"),
    "NON_ZERO_AMOUNT": (
        "登录已成功，套餐页 Plus 为首期付费价，没有免费会员。不是认证失败",
        "账户已回 READY 并标记 PAID_ONLY，默认不再进入开通池",
    ),
    "CHECKOUT_NON_ZERO_AMOUNT": (
        "登录已成功，支付页不是 0 元免费优惠。不是认证失败，而是没有免费会员",
        "账户已回 READY 并标记 PAID_ONLY，默认不再进入开通池",
    ),
    "SKIPPED": ("账户步骤遇阻后被自动跳过", "查看错误报告后决定是否重新投入"),
    "SKIP_ARCHIVE_FAILED": ("跳过归档失败，邮箱未移入 skipped", "人工核对文件状态"),
    "UNKNOWN_SIDE_EFFECT": ("操作可能已发生，但结果无法确认", "隔离并人工检查"),
    "STATUS_UNCONFIRMED": ("账户状态无法确认", "人工检查"),
    "PAYMENT_UNKNOWN": ("支付结果未知", "人工核对交易后处理"),
    "FINALIZATION_FAILED": ("结果保存失败", "人工检查后处理"),
    "CARD_UNAVAILABLE": ("没有可用信用卡", "补充信用卡后重试"),
    "STRIPE_FAILED": ("Stripe 支付失败", "人工检查支付页面"),
    "NEEDS_OPERATOR": ("需要人工介入", "查看浏览器现场后处理"),
    "BROWSER_CRASHED": ("浏览器进程异常退出（CloakBrowser 崩溃或被系统终止）", "重试；反复出现时检查 CloakBrowser 二进制或配置 license"),
}
def stage_label(stage: str) -> str:
    return _STAGE_LABELS.get(stage or "", stage or "未知阶段")
def reason_text_for(result: AccountRunResult) -> tuple[str, str]:
    """Return (reason, suggestion) for a settled result."""
    raw = result.raw_status or ""
    if raw in _REASON_TABLE:
        if (
            raw == "NEEDS_OPERATOR"
            and result.reason
            and result.reason != "NEEDS_OPERATOR"
        ):
            # 调度层把异常类型保存在 reason（脱敏），报告用它显示真实根因。
            return ("程序异常：" + result.reason, "查看运行日志定位原因")
        return _REASON_TABLE[raw]
    if raw.startswith("ERROR:"):
        return ("程序异常：" + raw.removeprefix("ERROR:"), "查看运行日志定位原因")
    if raw.startswith("UNEXPECTED:"):
        return ("程序异常：" + raw.removeprefix("UNEXPECTED:"), "查看运行日志定位原因")
    if raw.startswith("PREFLIGHT:"):
        return ("预检失败：" + raw.removeprefix("PREFLIGHT:"), "修复环境后重试")
    return ("原因未识别，请查看运行日志", "查看运行日志或浏览器现场")
def build_version() -> str:
    """Package ``VERSION`` 文件（打包时写入），回退到模块版本号。"""
    version_file = Path(__file__).resolve().parents[2] / "VERSION"
    if version_file.is_file():
        try:
            text = version_file.read_text(encoding="utf-8-sig").strip()
            if text:
                return text
        except OSError:
            pass
    try:
        from gpt_automation import __version__
        return __version__
    except Exception:
        return "unknown"
# ── 渲染与写入 ─────────────────────────────────────────────────────────
def render_terminal_summary(
    report: BatchRunReport,
    *,
    pending_file: str,
    skipped_file: str,
    results_file: str | None = None,
    held_file: str | None = None,
    run_scope: str | None = None,
) -> str:
    scope = (run_scope or report.run_scope or "account_provision").strip().lower()
    lines = [
        f"  批次结束：{report.batch_end_reason}",
        "  " + "═" * 30,
        f"  程序版本: {report.build or build_version()}",
        f"  本次总共执行: {len(report.entries)} 个账户",
        f"  成功创建账户: {report.success_count} 个",
        f"  自动跳过账户: {report.skipped_count} 个",
        f"  业务拒绝账户: {report.rejected_count} 个",
        f"  待人工账户:   {report.blocked_count} 个",
    ]
    failed = report.failed_entries
    if failed:
        lines.append("")
        lines.append("  失败／跳过账户明细：")
        for index, entry in enumerate(failed, start=1):
            lines.append(f"    {index}) {entry.email}")
            lines.append(
                f"       {entry.stage_label}｜{entry.reason_text}｜{entry.retry_disposition.value}"
            )
    if report.batch_issues:
        lines.append("")
        lines.append("  批次级问题：")
        for issue in report.batch_issues:
            lines.append(f"    - {issue}")
    lines.append("")
    def _abs(p: str | None) -> str:
        return os.path.abspath(p) if p else ""

    if scope == "checkout_fulfillment":
        lines.append(f"  成功账户文件: {_abs(results_file or pending_file)}")
        if held_file:
            lines.append(f"  待处理账户文件: {_abs(held_file)}")
    else:
        lines.append(f"  成功账户文件: {_abs(pending_file)}")
        lines.append(f"  跳过邮箱文件: {_abs(skipped_file)}")
    return "\n".join(lines)

def render_error_report(report: BatchRunReport) -> str:
    lines = [
        "批次错误报告",
        "",
        f"运行编号：{report.batch_id}",
        f"程序版本：{report.build or build_version()}",
        f"执行范围：{report.run_scope}",
        f"交互模式：{report.interaction_mode}",
        f"IP 模式：{report.rotation_mode}",
    ]
    if report.browser_backend:
        lines.append(f"浏览器后端：{report.browser_backend}")
    if report.binary_tier:
        lines.append(f"binary_tier: {report.binary_tier}")
    if report.license_configured is not None:
        lines.append(f"license_configured: {report.license_configured}")
        if not report.license_configured and report.binary_tier == "free":
            lines.append(
                "提示：当前使用免费版 CloakBrowser 二进制；崩溃反复出现时建议配置 CLOAKBROWSER_LICENSE_KEY。"
            )
    lines.extend([
        f"开始时间：{report.started_at}",
        f"结束时间：{report.ended_at or '（进程中断）'}",
        f"批次结果：{report.batch_end_reason or 'unknown'}",
        f"报告状态：{report.report_status}",
        (
            f"失败／跳过数量：{len(report.failed_entries)}"
            if report.report_status == "completed"
            else "失败／跳过数量：报告未完整生成"
        ),
    ])
    for entry in report.failed_entries:
        scope = (report.run_scope or "account_provision").strip().lower()
        entry_lines = [
            "─" * 50,
            f"账户：{entry.email}",
            f"处理结果：{entry.category.value}",
            f"停止阶段：{entry.stage or '（未知）'}",
            f"停止步骤：{entry.stage_label}",
            f"结果代码：{entry.status}",
            (
                f"错误代码：{entry.last_error}"
                if entry.last_error and entry.last_error != entry.status
                else "错误代码：（无额外记录）"
            ),
            f"失败原因：{entry.reason_text}",
        ]
        if entry.status in {
            "NO_FREE_OFFER",
            "NON_ZERO_AMOUNT",
            "CHECKOUT_NON_ZERO_AMOUNT",
        }:
            entry_lines.append("结论：不是认证失败，而是没有免费会员")
        if entry.page_url:
            entry_lines.append(f"页面 URL：{entry.page_url}")
        if entry.evidence_path:
            entry_lines.append(f"证据截图：{entry.evidence_path}")
        if scope == "checkout_fulfillment":
            entry_lines.extend([
                f"归档文件：{os.path.abspath('accounts/accounts_held.txt')}",
                f"归档结果：{'已写入 (' + entry.held_disposition + ')' if entry.held_archived else '未写入'}",
            ])
        else:
            entry_lines.extend([
                (
                    "邮箱队列：仍在 input（未移出）"
                    if entry.in_input
                    else (
                        "邮箱队列：已移出 input（已归档 skipped）"
                        if entry.in_skipped
                        else "邮箱队列：已移出 input"
                    )
                ),
                f"邮箱资源：{entry.resource_state or '（未记录）'}",
            ])
        entry_lines.extend([
            f"下次处理：{entry.retry_disposition.value}",
            f"Session ID：{entry.session_id}",
            f"完成时间：{entry.finished_at}",
        ])
        lines.extend(entry_lines)
    if report.batch_issues:
        lines.append("─" * 50)
        lines.append("批次级问题：")
        lines.extend(f"  - {issue}" for issue in report.batch_issues)
    lines.append("─" * 50)
    lines.append("安全说明：本报告不包含密码、验证码、TOTP、OAuth Token、卡片或地址。")
    return "\n".join(lines)
def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
_SAFE_BATCH_ID = re.compile(r"^[A-Za-z0-9_-]+$")
def write_error_report(
    report: BatchRunReport,
    report_dir: str | Path,
) -> Optional[Path]:
    """Atomically write the error report; returns the path or None."""
    if report.report_status != "completed" or not report.failed_entries:
        return None
    if not _SAFE_BATCH_ID.fullmatch(report.batch_id):
        logger.error("refusing to write report with unsafe batch id")
        return None
    directory = Path(report_dir)
    directory.mkdir(parents=True, exist_ok=True)
    filename = (
        f"batch_errors_"
        f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{report.batch_id}.txt"
    )
    path = directory / filename
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(directory),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(render_error_report(report) + "\n")
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return path
