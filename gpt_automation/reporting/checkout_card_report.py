"""开通会员卡片结算批次报告（HTML，三类分区）。

安全边界：允许邮箱、掩码卡号、结果、原因、拒绝种类、脱敏页面原文、导出文件、IP与截图；
禁止完整卡号、CVC、有效期、密码、TOTP、OAuth Token、完整堆栈。
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

from gpt_automation.utils.redaction import account_fingerprint

logger = logging.getLogger(__name__)

REPORT_PREFIX = "checkout_cards"
_SAFE_BATCH_ID = re.compile(r"^[A-Za-z0-9_-]+$")

RESULT_LABELS = {
    "declined": ("卡片被拒绝", "fail"),
    "passed": ("开通成功", "ok"),
    "unknown": ("支付未确认", "warn"),
}

DECLINE_KIND_LABELS = {
    "generic_declined": "卡片被拒绝",
    "auth_failed": "支付认证失败",
    "try_another_card": "请更换卡片",
    "declined_unspecified": "卡片被拒绝",
}

_SHOT_STATUS_TEXT = {
    "browser_not_started": "浏览器未成功启动，无法截图",
    "page_closed": "页面在截图前已关闭",
    "capture_timeout": "截图超时，账户处理已继续",
    "write_failed": "截图文件写入失败",
    "missing": "截图不可用",
}


@dataclass(frozen=True)
class CardReportItem:
    """One card settlement entry inside the checkout cards report."""

    email: str
    result: str  # declined | passed | unknown
    card_no_masked: str
    card_source_line: int = 0
    upsider_account: str = ""
    group_name: str = ""
    card_name: str = ""
    export_file: str = ""
    ip: str = ""
    stage_or_reason: str = ""
    decline_kind: str = ""
    decline_text: str = ""
    screenshot_relpath: Optional[str] = None
    screenshot_status: str = "missing"
    finished_at: str = ""


@dataclass
class CheckoutCardReport:
    """Batch-level card report data consumed by the HTML renderer."""

    generated_at: str
    batch_id: str
    interaction_mode: str = ""
    passed_file: str = ""
    declined_file: str = ""
    items: list[CardReportItem] = field(default_factory=list)

    @property
    def declined_count(self) -> int:
        return sum(item.result == "declined" for item in self.items)

    @property
    def passed_count(self) -> int:
        return sum(item.result == "passed" for item in self.items)

    @property
    def unknown_count(self) -> int:
        return sum(item.result == "unknown" for item in self.items)

    def section(self, result: str) -> list[CardReportItem]:
        return [item for item in self.items if item.result == result]


def _esc(value: object) -> str:
    return html.escape(str(value or ""), quote=True)


def _card(item: CardReportItem) -> str:
    label, tone = RESULT_LABELS.get(item.result, (item.result, "none"))
    rel = item.screenshot_relpath
    if rel:
        img = (
            f'<button type="button" class="shot-open" '
            f'data-src="{_esc(rel)}" '
            f'data-alt="{_esc(item.email)} 的结算截图" '
            f'aria-label="点击放大 {_esc(item.email)} 的结算截图">'
            f'<img src="{_esc(rel)}" '
            f'alt="{_esc(item.email)} 的结算截图" loading="lazy">'
            f'</button>'
        )
    else:
        reason = _SHOT_STATUS_TEXT.get(item.screenshot_status, "截图不可用")
        img = f'<div class="shot-missing">{_esc(reason)}</div>'

    decline_kind_line = ""
    if item.result == "declined" and item.decline_kind:
        kind_zh = DECLINE_KIND_LABELS.get(item.decline_kind, "卡片被拒绝")
        decline_kind_line = (
            f'<p class="kind-line"><strong>拒绝种类：</strong>{_esc(kind_zh)}　'
            f'<code class="badge mark">{_esc(item.decline_kind)}</code></p>'
        )

    decline_quote_line = ""
    if item.decline_text:
        decline_quote_line = (
            f'<p class="quote">页面原文：{_esc(item.decline_text)}</p>'
        )

    card_meta_parts = []
    if item.card_no_masked:
        card_meta_parts.append(f"卡号：{_esc(item.card_no_masked)}")
    if item.card_source_line > 0:
        card_meta_parts.append(f"来源行：{item.card_source_line}")
    if item.upsider_account:
        card_meta_parts.append(f"Upsider：{_esc(item.upsider_account)}")
    if item.card_name:
        card_meta_parts.append(f"卡名：{_esc(item.card_name)}")
    card_meta_line = (
        f'<p class="meta-line">{"　".join(card_meta_parts)}</p>'
        if card_meta_parts
        else ""
    )

    export_parts = []
    if item.export_file:
        export_parts.append(f"导出：{_esc(item.export_file)}")
    if item.ip:
        export_parts.append(f"IP：{_esc(item.ip)}")
    export_line = (
        f'<p class="meta-line">{"　".join(export_parts)}</p>'
        if export_parts
        else ""
    )

    reason_line = ""
    if item.stage_or_reason and item.stage_or_reason != item.decline_text:
        reason_line = f'<p class="reason">说明：{_esc(item.stage_or_reason)}</p>'

    return (
        f'<article class="card {tone}">'
        f'<header class="card-head">'
        f'<span class="email">{_esc(item.email)}</span>'
        f'<span class="badge {tone}">{label}</span>'
        f'</header>'
        f'<div class="shot">{img}</div>'
        f'{decline_kind_line}{decline_quote_line}{card_meta_line}{export_line}{reason_line}'
        f'</article>'
    )


def _section(title: str, items: list[CardReportItem], *, open_by_default: bool) -> str:
    open_attr = " open" if open_by_default else ""
    body = "".join(_card(item) for item in items)
    if not body:
        body = '<p class="empty">本批次没有此类卡片记录</p>'
    return (
        f'<details class="section"{open_attr}>'
        f'<summary>{_esc(title)}（{len(items)}）</summary>'
        f'<div class="grid">{body}</div>'
        f'</details>'
    )


def render_checkout_card_report_html(report: CheckoutCardReport) -> str:
    """Render the full standalone HTML report for checkout cards."""
    declined_items = report.section("declined")
    passed_items = report.section("passed")
    unknown_items = report.section("unknown")

    css = """
        :root {
          --bg: #F7F6F3; --surface: #FFFFFF; --text: #1F1D1A;
          --muted: #6F6A63; --border: #E6E1DA;
          --ok: #2F6F4E; --ok-bg: #E8F3EC;
          --fail: #A15C3E; --fail-bg: #F7EEE9;
          --warn: #926C15; --warn-bg: #FEF7DA;
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
        .pill.ok { color: var(--ok); background: var(--ok-bg); }
        .pill.fail { color: var(--fail); background: var(--fail-bg); border-color: var(--fail); }
        .pill.warn { color: var(--warn); background: var(--warn-bg); border-color: var(--warn); }
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
        .badge.ok { color: var(--ok); background: var(--ok-bg); }
        .badge.fail { color: var(--fail); background: var(--fail-bg); }
        .badge.warn { color: var(--warn); background: var(--warn-bg); }
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
        .kind-line {
          margin: 8px 0 0; font-size: 13px; color: #7A3A2A;
          background: #F7EEE9; padding: 6px 10px; border-radius: 6px;
        }
        .quote {
          margin: 8px 0 0; font-size: 12px; color: var(--text);
          background: #FAF9F6; border: 1px solid var(--border);
          padding: 6px 10px; border-radius: 6px;
          overflow-wrap: anywhere;
        }
        .meta-line, .reason {
          margin: 8px 0 0; font-size: 12px; color: var(--muted);
          overflow-wrap: anywhere;
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

    sections = (
        _section("卡片被拒绝", declined_items, open_by_default=True)
        + _section("开通成功", passed_items, open_by_default=False)
        + _section("支付未确认", unknown_items, open_by_default=False)
    )

    return (
        "<!DOCTYPE html>\n"
        '<html lang="zh-CN">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "<title>开通会员卡片结算报告</title>\n"
        f"<style>{css}</style>\n"
        "</head>\n<body>\n"
        '<div class="wrap">\n'
        '<header class="top">\n'
        '<h1>开通会员卡片结算报告</h1>\n'
        f'<p class="meta">生成时间：{_esc(report.generated_at)}　'
        f'批次：{_esc(report.batch_id)}　'
        f"执行模式：{_esc(report.interaction_mode or 'assisted')}</p>\n"
        '<div class="counts">\n'
        f'<span class="pill">本次结算 {len(report.items)}</span>\n'
        f'<span class="pill fail">卡片被拒绝 {report.declined_count}</span>\n'
        f'<span class="pill ok">开通成功 {report.passed_count}</span>\n'
        f'<span class="pill warn">支付未确认 {report.unknown_count}</span>\n'
        '</div>\n'
        '</header>\n'
        f"{sections}\n"
        '<footer>仅供开通会员卡片结算验收使用，HTML 仅展示掩码卡号与拒绝种类，不包含完整卡号与 CVC 等敏感凭据。</footer>\n'
        '</div>\n'
        '<div id="lightbox" class="lightbox" hidden>\n'
        '<button type="button" class="lightbox-close" aria-label="关闭大图">×</button>\n'
        '<figure class="lightbox-figure">\n'
        '<img id="lightbox-img" src="" alt="">\n'
        '<figcaption id="lightbox-cap"></figcaption>\n'
        '</figure>\n'
        '</div>\n'
        '<script>\n'
        '(function () {\n'
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
        '</script>\n'
        '</body>\n</html>\n'
    )


def write_checkout_card_report(
    items: Iterable[CardReportItem],
    output_dir: str | Path,
    batch_id: str,
    *,
    passed_file: str | Path = "",
    declined_file: str | Path = "",
    interaction_mode: str = "",
) -> Optional[str]:
    """Write timestamped HTML card report + copied screenshots; returns the HTML path."""
    item_list = list(items)
    if not _SAFE_BATCH_ID.fullmatch(batch_id or ""):
        return None
    out_root = Path(output_dir)
    out_root.mkdir(parents=True, exist_ok=True)
    stem = f"{REPORT_PREFIX}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{batch_id}"
    batch_dir = out_root / stem
    batch_dir.mkdir(parents=True, exist_ok=True)
    html_path = batch_dir / "index.html"

    normalized: list[CardReportItem] = []
    for item in item_list:
        rel = None
        if item.screenshot_relpath:
            source = Path(item.screenshot_relpath)
            if source.is_file():
                prefix = item.result
                fp = account_fingerprint(item.email)[:12]
                target = batch_dir / f"{prefix}_{fp}_{source.stem[:24]}{source.suffix}"
                try:
                    shutil.copy2(source, target)
                    rel = target.name
                except OSError as exc:
                    logger.warning("checkout card report: screenshot copy failed: %s", exc)
        normalized.append(
            CardReportItem(
                email=item.email,
                result=item.result,
                card_no_masked=item.card_no_masked,
                card_source_line=item.card_source_line,
                upsider_account=item.upsider_account,
                group_name=item.group_name,
                card_name=item.card_name,
                export_file=item.export_file,
                ip=item.ip,
                stage_or_reason=item.stage_or_reason,
                decline_kind=item.decline_kind,
                decline_text=item.decline_text,
                screenshot_relpath=rel,
                screenshot_status=item.screenshot_status if not rel else "captured",
                finished_at=item.finished_at,
            )
        )

    report = CheckoutCardReport(
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        batch_id=batch_id,
        interaction_mode=interaction_mode,
        passed_file=str(passed_file or ""),
        declined_file=str(declined_file or ""),
        items=normalized,
    )

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{html_path.name}.",
        suffix=".tmp",
        dir=str(batch_dir),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8-sig", newline="\n") as fh:
            fh.write(render_checkout_card_report_html(report))
        os.replace(tmp_name, html_path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    logger.info("checkout card report written: %s", html_path)
    return str(html_path)
