"""开通会员无免费会员验收批次报告（HTML，三类分区）。

报告只做「投影」：消费批次已经结算的 AccountRunResult，不参与账户状态
判定、不改写库存、不改变资源处置。写入失败不影响账户状态与批次退出码。

安全边界：允许邮箱、结果、URL、标记、失败摘要、截图与路径；
禁止密码、TOTP、OAuth Token、卡号、完整堆栈。
"""

from __future__ import annotations

import html
import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional, Sequence

import config
from gpt_automation.utils.redaction import account_fingerprint
from gpt_automation.workflow.models import AccountRunResult, AccountStatus

logger = logging.getLogger(__name__)

REPORT_PREFIX = "checkout_offer"
_SAFE_BATCH_ID = re.compile(r"^[A-Za-z0-9_-]+$")

_NO_FREE_PAID = {"NON_ZERO_AMOUNT", "CHECKOUT_NON_ZERO_AMOUNT"}
_NO_FREE_REVIEW = {"NO_FREE_OFFER"}
_SUCCESS = {
    AccountStatus.SUCCESS,
    AccountStatus.ACCOUNT_READY,
    AccountStatus.VERIFICATION_COMPLETE,
}

RESULT_LABELS = {
    "no_free_paid": ("没有免费会员", "none"),
    "no_free_review": ("无入口待复核", "warn"),
    "failed": ("执行失败", "fail"),
}

_SHOT_STATUS_TEXT = {
    "browser_not_started": "浏览器未成功启动，无法截图",
    "page_closed": "页面在截图前已关闭",
    "capture_timeout": "截图超时，账户处理已继续",
    "write_failed": "截图文件写入失败",
    "missing": "截图不可用",
}


@dataclass(frozen=True)
class CheckoutOfferItem:
    """One account entry inside the checkout offer report."""

    email: str
    result: str  # no_free_paid | no_free_review | failed
    ip: str = ""
    page_url: str = ""
    stage_or_reason: str = ""
    screenshot_relpath: Optional[str] = None
    screenshot_status: str = "missing"
    finished_at: str = ""
    offer_mark: str = ""  # PAID_ONLY | REVIEW_REQUIRED | ""


@dataclass
class CheckoutOfferReport:
    """Batch-level report data consumed by the HTML renderer."""

    generated_at: str
    batch_id: str
    interaction_mode: str = ""
    items: list[CheckoutOfferItem] = field(default_factory=list)

    @property
    def no_free_paid_count(self) -> int:
        return sum(item.result == "no_free_paid" for item in self.items)

    @property
    def no_free_review_count(self) -> int:
        return sum(item.result == "no_free_review" for item in self.items)

    @property
    def failed_count(self) -> int:
        return sum(item.result == "failed" for item in self.items)

    def section(self, result: str) -> list[CheckoutOfferItem]:
        return [item for item in self.items if item.result == result]


def checkout_offer_item_from_result(
    email: str,
    result: AccountRunResult,
) -> Optional[CheckoutOfferItem]:
    """Map one settled account result onto a report item (checkout_fulfillment only)."""
    if result.status in _SUCCESS:
        return None
    raw = result.raw_status or ""
    if raw in _NO_FREE_PAID:
        mapped = "no_free_paid"
        offer_mark = "PAID_ONLY"
    elif raw in _NO_FREE_REVIEW:
        mapped = "no_free_review"
        offer_mark = "REVIEW_REQUIRED"
    else:
        mapped = "failed"
        offer_mark = ""
    evidence = getattr(result, "evidence_paths", ()) or ()
    screenshot_status = getattr(result, "screenshot_status", "missing") or "missing"
    if evidence:
        screenshot_status = "captured"
    return CheckoutOfferItem(
        email=email,
        result=mapped,
        ip=getattr(result, "ip", "") or "",
        page_url=getattr(result, "page_url", "") or "",
        stage_or_reason=result.reason or raw,
        screenshot_relpath=evidence[0] if evidence else None,
        screenshot_status=screenshot_status,
        finished_at=datetime.now().isoformat(timespec="seconds"),
        offer_mark=offer_mark,
    )


def _esc(value: object) -> str:
    return html.escape(str(value or ""), quote=True)


def _card(item: CheckoutOfferItem) -> str:
    label, tone = RESULT_LABELS.get(item.result, (item.result, "none"))
    rel = item.screenshot_relpath
    if rel:
        img = (
            f'<button type="button" class="shot-open" '
            f'data-src="{_esc(rel)}" '
            f'data-alt="{_esc(item.email)} 的{label}截图" '
            f'aria-label="点击放大 {_esc(item.email)} 的{label}截图">'
            f'<img src="{_esc(rel)}" '
            f'alt="{_esc(item.email)} 的{label}截图" loading="lazy">'
            f"</button>"
        )
    else:
        reason = _SHOT_STATUS_TEXT.get(item.screenshot_status, "截图不可用")
        img = f'<div class="shot-missing">{_esc(reason)}</div>'

    mark_badge = (
        f'<span class="badge mark">{_esc(item.offer_mark)}</span>'
        if item.offer_mark
        else ""
    )

    conclusion_line = (
        '<p class="conclusion"><strong>结论：登录已成功。不是认证失败，而是没有免费会员。</strong></p>'
        if item.result in {"no_free_paid", "no_free_review"}
        else ""
    )

    url_line = (
        f'<p class="url">页面 URL：<span title="{_esc(item.page_url)}">'
        f"{_esc(item.page_url)}</span></p>"
        if item.page_url
        else ""
    )
    reason_line = (
        f'<p class="reason">原因：{_esc(item.stage_or_reason)}</p>'
        if item.stage_or_reason
        else ""
    )
    ip_line = (
        f'<p class="ip">使用 IP：{_esc(item.ip)}</p>'
        if item.ip
        else ""
    )
    return (
        f'<article class="card {tone}">'
        f'<header class="card-head">'
        f'<span class="email">{_esc(item.email)}</span>'
        f'<span class="badge {tone}">{label}</span>'
        f'{mark_badge}'
        f"</header>"
        f'<div class="shot">{img}</div>'
        f'{conclusion_line}{url_line}{reason_line}{ip_line}'
        f"</article>"
    )


def _section(title: str, items: list[CheckoutOfferItem], *, open_by_default: bool) -> str:
    open_attr = " open" if open_by_default else ""
    body = "".join(_card(item) for item in items)
    if not body:
        body = '<p class="empty">本批次没有此类账号</p>'
    return (
        f'<details class="section"{open_attr}>'
        f"<summary>{_esc(title)}（{len(items)}）</summary>"
        f'<div class="grid">{body}</div>'
        f"</details>"
    )


def render_checkout_offer_report_html(report: CheckoutOfferReport) -> str:
    """Render the full standalone HTML report (inline CSS, UTF-8)."""
    paid_items = report.section("no_free_paid")
    review_items = report.section("no_free_review")
    failed_items = report.section("failed")
    css = """
        :root {
          --bg: #F7F6F3; --surface: #FFFFFF; --text: #1F1D1A;
          --muted: #6F6A63; --border: #E6E1DA;
          --none: #6B7280; --none-bg: #F3F4F6;
          --warn: #926C15; --warn-bg: #FEF7DA;
          --fail: #A15C3E; --fail-bg: #F7EEE9;
          --shadow: 0 1px 2px rgba(31,29,26,0.06);
          --radius: 12px;
        }
        * { box-sizing: border-box; }
        body {
          margin: 0; background: var(--bg); color: var(--text);
          font: 14px/1.6 "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
        }
        .wrap { max-width: 1180px; margin: 0 auto; padding: 28px 24px 48px; }
        header.top { border-bottom: 1px solid var(--border); padding-bottom: 18px; }
        h1 { font-size: 22px; margin: 0 0 4px; letter-spacing: -0.2px; }
        .meta { color: var(--muted); font-size: 13px; margin: 4px 0; }
        .counts { display: flex; gap: 10px; flex-wrap: wrap; margin: 16px 0 4px; }
        .pill {
          padding: 6px 12px; border: 1px solid var(--border);
          border-radius: 999px; background: var(--surface); font-size: 13px;
        }
        .pill.none { color: var(--none); border-color: var(--border); background: var(--none-bg); }
        .pill.warn { color: var(--warn); border-color: var(--warn); background: var(--warn-bg); }
        .pill.fail { color: var(--fail); border-color: var(--fail); background: var(--fail-bg); }
        details.section {
          margin-top: 16px; border: 1px solid var(--border);
          border-radius: var(--radius); background: var(--surface);
          box-shadow: var(--shadow); overflow: hidden;
        }
        summary {
          cursor: pointer; padding: 14px 18px; font-size: 16px; font-weight: 600;
          user-select: none; transition: background 0.18s ease;
        }
        summary:hover { background: #FAF9F6; }
        summary:focus-visible { outline: 2px solid var(--text); outline-offset: -2px; }
        .grid {
          display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
          gap: 16px; padding: 4px 18px 18px;
        }
        .card {
          border: 1px solid var(--border); border-radius: 10px;
          padding: 12px; background: var(--surface); overflow: hidden;
        }
        .card-head { display: flex; justify-content: space-between; gap: 8px; align-items: center; margin-bottom: 10px; }
        .email { font-weight: 600; overflow-wrap: anywhere; }
        .badge {
          flex: none; font-size: 12px; padding: 3px 9px; border-radius: 999px;
        }
        .badge.none { color: var(--none); background: var(--none-bg); }
        .badge.warn { color: var(--warn); background: var(--warn-bg); }
        .badge.fail { color: var(--fail); background: var(--fail-bg); }
        .badge.mark { border: 1px solid var(--border); background: #FAF9F6; color: var(--text); font-family: monospace; }
        .shot {
          border: 1px solid var(--border); border-radius: 8px;
          background: #FBFBFA; overflow: hidden; aspect-ratio: 16 / 9;
          display: flex; align-items: center; justify-content: center;
        }
        .shot-open {
          width: 100%; height: 100%; border: 0; padding: 0;
          background: none; display: block; cursor: zoom-in;
        }
        .shot-open:focus-visible { outline: 2px solid var(--text); outline-offset: -2px; }
        .shot img { width: 100%; height: 100%; object-fit: contain; display: block; }
        .shot-missing { color: var(--muted); font-size: 13px; }
        .conclusion {
          margin: 8px 0 0; font-size: 13px; color: #2F6F4E;
          background: #E8F3EC; padding: 6px 10px; border-radius: 6px;
        }
        .url, .reason {
          margin: 8px 0 0; font-size: 12px; color: var(--muted);
          overflow-wrap: anywhere;
        }
        .ip {
          margin: 8px 0 0; font-size: 12px; color: var(--muted);
          font-variant-numeric: tabular-nums;
        }
        .empty { color: var(--muted); padding: 6px 18px 16px; margin: 0; }
        .lightbox {
          position: fixed; inset: 0; z-index: 1000;
          background: rgba(24, 21, 18, 0.88);
          display: flex; align-items: center; justify-content: center;
          padding: 24px;
        }
        .lightbox[hidden] { display: none; }
        .lightbox-figure {
          margin: 0; max-width: 94vw; max-height: 92vh;
          display: flex; flex-direction: column; align-items: center; gap: 10px;
        }
        .lightbox img {
          max-width: 94vw; max-height: 82vh; object-fit: contain;
          background: #FFFFFF; border-radius: 6px;
          box-shadow: 0 10px 48px rgba(0, 0, 0, 0.45);
        }
        .lightbox figcaption {
          color: #EDEAE5; font-size: 13px; overflow-wrap: anywhere;
        }
        .lightbox-close {
          position: fixed; top: 14px; right: 18px;
          font-size: 28px; line-height: 1; color: #EDEAE5;
          background: none; border: 0; cursor: pointer; padding: 6px 10px;
        }
        .lightbox-close:hover { color: #FFFFFF; }
        body.lb-open { overflow: hidden; }
        footer { margin-top: 22px; color: var(--muted); font-size: 12px; }
        @media (prefers-reduced-motion: reduce) {
          summary { transition: none; }
        }
        @media (max-width: 720px) {
          .grid { grid-template-columns: 1fr; }
        }
    """
    baseline_ip = next(
        (item.ip for item in reversed(report.items) if item.ip),
        "",
    )
    sections = (
        _section("没有免费会员（付费套餐）", paid_items, open_by_default=True)
        + _section("无入口待复核", review_items, open_by_default=False)
        + _section("执行失败", failed_items, open_by_default=False)
    )
    return (
        "<!DOCTYPE html>\n"
        '<html lang="zh-CN">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "<title>开通会员优惠验收报告</title>\n"
        f"<style>{css}</style>\n"
        "</head>\n<body>\n<div class=\"wrap\">\n"
        "<header class=\"top\">\n"
        "<h1>开通会员优惠验收报告</h1>\n"
        f'<p class="meta">生成时间：{_esc(report.generated_at)}　'
        f"批次：{_esc(report.batch_id)}　"
        f"执行模式：{_esc(report.interaction_mode or 'assisted')}</p>\n"
        '<div class="counts">\n'
        f'<span class="pill">本次未开通 {len(report.items)}</span>\n'
        f'<span class="pill none">没有免费会员 {report.no_free_paid_count}</span>\n'
        f'<span class="pill warn">无入口待复核 {report.no_free_review_count}</span>\n'
        f'<span class="pill fail">执行失败 {report.failed_count}</span>\n'
        "</div>\n"
        "</header>\n"
        f"{sections}\n"
        "<footer>仅供开通会员优惠验收使用，不包含账户敏感凭据。"
        + (
            f" 基准 IP：{_esc(baseline_ip)}"
            if baseline_ip
            else ""
        )
        + "</footer>\n"
        "</div>\n"
        '<div id="lightbox" class="lightbox" hidden>\n'
        '<button type="button" class="lightbox-close" '
        'aria-label="关闭大图">\u00d7</button>\n'
        '<figure class="lightbox-figure">\n'
        '<img id="lightbox-img" src="" alt="">\n'
        '<figcaption id="lightbox-cap"></figcaption>\n'
        "</figure>\n"
        "</div>\n"
        "<script>\n"
        "(function () {\n"
        "  'use strict';\n"
        "  var lb = document.getElementById('lightbox');\n"
        "  var img = document.getElementById('lightbox-img');\n"
        "  var cap = document.getElementById('lightbox-cap');\n"
        "  function closeLb() {\n"
        "    lb.hidden = true;\n"
        "    img.removeAttribute('src');\n"
        "    document.body.classList.remove('lb-open');\n"
        "  }\n"
        "  Array.prototype.forEach.call(\n"
        "    document.querySelectorAll('.shot-open'),\n"
        "    function (btn) {\n"
        "      btn.addEventListener('click', function () {\n"
        "        img.src = btn.getAttribute('data-src');\n"
        "        img.alt = btn.getAttribute('data-alt') || '';\n"
        "        cap.textContent = img.alt;\n"
        "        lb.hidden = false;\n"
        "        document.body.classList.add('lb-open');\n"
        "      });\n"
        "    },\n"
        "  );\n"
        "  lb.addEventListener('click', function (e) {\n"
        "    if (e.target === lb || e.target.classList.contains('lightbox-close')) {\n"
        "      closeLb();\n"
        "    }\n"
        "  });\n"
        "  document.addEventListener('keydown', function (e) {\n"
        "    if (e.key === 'Escape' && !lb.hidden) { closeLb(); }\n"
        "  });\n"
        "})();\n"
        "</script>\n"
        "</body>\n</html>\n"
    )


def write_checkout_offer_report(
    items: Iterable[CheckoutOfferItem],
    report_dir: str | Path,
    batch_id: str,
    *,
    interaction_mode: str = "",
) -> Optional[Path]:
    """Write timestamped HTML report + asset copies; returns the HTML path."""
    item_list = list(items)
    if not _SAFE_BATCH_ID.fullmatch(batch_id or ""):
        return None
    directory = Path(report_dir)
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"{REPORT_PREFIX}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{batch_id}"
    batch_dir = directory / stem
    batch_dir.mkdir(parents=True, exist_ok=True)
    html_path = batch_dir / "index.html"

    normalized: list[CheckoutOfferItem] = []
    for item in item_list:
        rel = None
        if item.screenshot_relpath:
            source = Path(item.screenshot_relpath)
            if source.is_file():
                prefix = {
                    "no_free_paid": "no_offer",
                    "no_free_review": "no_offer_review",
                    "failed": "failed",
                }.get(item.result, "account")
                fp = account_fingerprint(item.email)[:12]
                target = batch_dir / f"{prefix}_{fp}_{source.stem[:24]}{source.suffix}"
                try:
                    shutil.copy2(source, target)
                    rel = target.name
                except OSError as exc:
                    logger.warning("checkout offer report: screenshot copy failed: %s", exc)
        normalized.append(
            CheckoutOfferItem(
                email=item.email,
                result=item.result,
                ip=item.ip,
                page_url=item.page_url,
                stage_or_reason=item.stage_or_reason,
                screenshot_relpath=rel,
                screenshot_status=item.screenshot_status if not rel else "captured",
                finished_at=item.finished_at,
                offer_mark=item.offer_mark,
            )
        )

    report = CheckoutOfferReport(
        generated_at=datetime.now().isoformat(timespec="seconds"),
        batch_id=batch_id,
        interaction_mode=interaction_mode,
        items=normalized,
    )
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{html_path.name}.",
        suffix=".tmp",
        dir=str(batch_dir),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8-sig", newline="\n") as fh:
            fh.write(render_checkout_offer_report_html(report))
        os.replace(tmp_name, html_path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    logger.info("checkout offer report written: %s", html_path)
    return html_path
