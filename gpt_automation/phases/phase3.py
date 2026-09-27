"""
Phase 3: Plus activation — free offer detection + Stripe payment delegation.

Offer detection (amount guard, click, accept) lives here.
Stripe-specific logic (iframe detection, card/address fill, confirm, rejection retry)
is delegated to gpt_automation.phases.stripe.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import List, Optional
from urllib.parse import parse_qs, urlparse

import config
from gpt_automation.utils.csv_writer import evidence_path
from gpt_automation.utils.exceptions import FATAL_EXCEPTIONS, SAFE_EXCEPTIONS
from gpt_automation.workflow.models import StepOutcome, StepStatus
from gpt_automation.utils.redaction import mask_email
from gpt_automation.utils.timing import (
    CHECKOUT_NAV_INTERVAL_S,
    CHECKOUT_NAV_TIMEOUT_S,
    human_delay,
)
from gpt_automation.phases.probe import probe_visible
from gpt_automation.phases.helpers import (
    safe_click, safe_screenshot,
    wait_until_gone, wait_until_appears,
)

logger = logging.getLogger(__name__)

PROMO_CAMPAIGN_FREE = "promo_campaign=plus-1-month-free"
# 兼容旧导入：目标值现在由 config.OFFER_ELIGIBILITY_CONFIG 决定。
TARGET_PROMO_CAMPAIGN = config.OFFER_ELIGIBILITY_CONFIG["target_promo_campaign"]
_PROMO_CAMPAIGN_ALLOWED = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-_.~")
FREE_OFFER_KEYWORDS = (
    "無料オファーを受け取る",
    "無料で始める",
    "無料で試す",
    "receive free offer",
    "start free",
)
PAID_UPGRADE_KEYWORDS = (
    "アップグレードする",
    "upgrade",
    "subscribe",
    "登録する",
)
PRICING_PAGE_INDICATORS = [
    config.SELECTORS["pricing_page_indicator"],
    config.SELECTORS["target_plan_btn"],
]
_PRICE_TOKEN_RE = re.compile(
    r"(?:¥|￥)\s*([0-9][0-9,]*(?:\.[0-9]+)?)"
    r"|([0-9][0-9,]*(?:\.[0-9]+)?)\s*円"
)
_CURRENCY_TOKEN_RE = re.compile(r"JPY|円|￥|¥|USD|\$", re.IGNORECASE)
_PURE_NUMBER_RE = re.compile(r"^[0-9][0-9,]*$")
_DISCLAIMER_MARKERS = (
    "プロモーション規約",
    "promotion terms",
    "promo terms",
    "terms apply",
    "以降は、月額",
    "その後",
)
_LIMITED_TIME_BADGE = "期間限定"


async def phase3_check_free_offer(page) -> bool:
    """Check for free Plus offer button on the current page.

    Assumes we are already on the main ChatGPT page (Phase 2 closed settings).
    Does NOT navigate — caller is responsible for ensuring we're on the right page.
    """
    try:
        loc = page.locator(config.SELECTORS["free_offer_btn"]).first
        return bool(await loc.is_visible(timeout=10000))
    except SAFE_EXCEPTIONS:
        return False


async def phase3_find_upgrade_entry(page) -> bool:
    """Check for a generic upgrade entry on the home page.

    Used when the dedicated free-offer button is absent: the account may
    still carry a free promotion that is only visible on the pricing page,
    so we still enter the pricing page and verify the target plan price.
    """
    try:
        loc = page.locator(config.SELECTORS["upgrade_entry_btn"]).first
        return bool(await loc.is_visible(timeout=8000))
    except SAFE_EXCEPTIONS:
        return False


async def phase3_pricing_page_verified(page) -> bool:
    """True when the pricing page (plan grid / target plan button) is visible.

    URL evidence is checked first: when the account already sits on a
    chatgpt.com/#pricing URL, the page is the pricing page even if the
    plan-grid testids are missing (2026-08 real site: the promo pricing
    page renders without pricing-modal-plan-grid).  DOM indicators
    remain the fallback for hash-less layouts.
    """
    try:
        if is_pricing_url(page.url or ""):
            return True
    except SAFE_EXCEPTIONS:
        pass
    try:
        for selector in PRICING_PAGE_INDICATORS:
            loc = page.locator(selector).first
            if await probe_visible(loc):
                return True
    except SAFE_EXCEPTIONS:
        pass
    return False


async def _close_overlay_before_offer(page) -> bool:
    """Close any leftover modal dialog or settings overlay that blocks the offer entry.

    Handles:
    1. Memory / personalization survey modal ('より関連性が高く、パーソナライズされた回答' -> click OK).
    2. Lingering welcome dialog ('準備が完了しました' -> 続行).
    3. Leftover settings panel or dialog with close button ('閉じる' / 'Close' / '×').

    Returns True if an overlay was dismissed, False if clean.
    """
    from gpt_automation.phases.phase1 import (
        phase1_dismiss_personalization_modal,
        phase1_ready_confirm,
    )

    dismissed = False

    # 1. Personalization / memory survey dialog (primary action: OK button)
    try:
        if await phase1_dismiss_personalization_modal(page):
            logger.info("phase3: dismissed personalization modal")
            dismissed = True
            await human_delay((0.5, 1.0))
    except SAFE_EXCEPTIONS:
        pass

    # 2. Lingering welcome modal
    try:
        welcome_loc = page.locator("text=準備が完了しました").or_(
            page.locator("[role='dialog']:has-text('ようこそ')")
        ).first
        res = welcome_loc.is_visible(timeout=500)
        if asyncio.iscoroutine(res) or hasattr(res, "__await__"):
            res = await res
        if res:
            await phase1_ready_confirm(page)
            dismissed = True
            await human_delay((0.5, 1.0))
    except (*SAFE_EXCEPTIONS, TypeError):
        pass

    # 3. Settings panel or modal with a close button
    try:
        eval_res = page.evaluate("""
            () => {
                const selectors = [
                    'button[aria-label="閉じる"]',
                    'button[aria-label="Close"]',
                    'button[aria-label="×"]',
                ];
                for (const sel of selectors) {
                    const btn = document.querySelector(sel);
                    if (btn && btn.offsetParent !== null) {
                        btn.click();
                        return true;
                    }
                }
                const btns = document.querySelectorAll('[role="dialog"] button, [data-testid="settings-dialog"] button');
                for (const btn of btns) {
                    const t = btn.textContent.trim();
                    if ((t === '閉じる' || t === 'Close' || t === '×') && btn.offsetParent !== null) {
                        btn.click();
                        return true;
                    }
                }
                return false;
            }
        """)
        if asyncio.iscoroutine(eval_res) or hasattr(eval_res, "__await__"):
            close_btn_clicked = await eval_res
        else:
            close_btn_clicked = bool(eval_res)
        if close_btn_clicked:
            await human_delay((0.5, 1.0))
            logger.info("phase3: closed settings/dialog via close button")
            dismissed = True
    except (*SAFE_EXCEPTIONS, TypeError):
        pass

    return dismissed


def _home_pricing_entries() -> tuple[tuple[str, str], ...]:
    return (
        (config.SELECTORS["free_offer_btn_exact"], "click_free_offer_entry"),
        (config.SELECTORS["free_offer_banner_exact"], "click_free_offer_banner"),
        (config.SELECTORS["upgrade_entry_btn_exact"], "click_upgrade_entry"),
    )


async def _probe_home_pricing_entry(page) -> Optional[str]:
    """Return the first currently visible home-entry selector, else None."""
    for selector, _name in _home_pricing_entries():
        try:
            loc = page.locator(selector).first
            if await probe_visible(loc):
                return selector
        except SAFE_EXCEPTIONS:
            continue
    return None


async def _wait_for_pricing_page(page, *, wait_timeout_s: float) -> bool:
    """Poll phase3_pricing_page_verified until True or timeout."""
    deadline = time.monotonic() + max(wait_timeout_s, 0.0)
    while True:
        if await phase3_pricing_page_verified(page):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(CHECKOUT_NAV_INTERVAL_S, remaining))


async def phase3_open_pricing_page(
    page,
    *,
    wait_timeout_s: float = CHECKOUT_NAV_TIMEOUT_S,
) -> StepOutcome:
    """Unified pricing-page navigation: discover entry -> click -> verify.

    URL evidence is checked first: if the current URL already points at the
    pricing page (chatgpt.com/#pricing), the account is already on it and no
    entry is clicked (a leftover claim button on the pricing page must never
    be treated as a home-page entry).  On the home page the free-offer
    entries (left-bottom receive-offer / top-right free-offer banners) are
    *entry buttons*, not claim buttons: the account must click into the
    pricing page before any URL-based offer verdict is possible.  The old
    upgrade entry remains a fallback for accounts without a free-offer
    banner.  Entry selectors are single-text (never comma-joined with
    multiple :has-text conditions) so CloakBrowser human click resolves
    them correctly.  Returns NO_FREE_OFFER only when no entry exists at
    all; entries that fail to open the pricing page are AMOUNT_UNKNOWN
    (operator must look at the live browser).
    """
    if await phase3_pricing_page_verified(page):
        return StepOutcome(StepStatus.VERIFIED, "PRICING_PAGE_REACHED")

    # Dismiss any blocking overlays on the home page before scanning for entries
    try:
        await _close_overlay_before_offer(page)
    except FATAL_EXCEPTIONS:
        raise
    except Exception:  # noqa: BLE001 - cleanup is best-effort
        pass

    deadline = time.monotonic() + max(wait_timeout_s, 0.0)
    tried: set[str] = set()
    found_entry = False
    last_reason = ""
    entries = _home_pricing_entries()
    first_tick = True

    while True:
        if not first_tick and await phase3_pricing_page_verified(page):
            return StepOutcome(StepStatus.VERIFIED, "PRICING_PAGE_REACHED")
        first_tick = False

        clicked = False
        for selector, step_name in entries:
            if selector in tried:
                continue
            try:
                loc = page.locator(selector).first
                if not await probe_visible(loc):
                    continue
            except SAFE_EXCEPTIONS:
                continue

            found_entry = True
            tried.add(selector)
            try:
                await safe_click(page, [selector], timeout_ms=10000)
            except FATAL_EXCEPTIONS:
                raise
            except Exception as exc:  # noqa: BLE001 - overlay/redraw blocks the click
                # 覆盖层（如 MFA 验收后残留的设置弹窗）挡住入口按钮：先关闭覆盖
                # 层，再以普通方式重试一次；仍失败则记录原因并尝试下一个入口。
                logger.warning(
                    "%s: entry click blocked (%s); closing overlay and retrying once",
                    step_name,
                    type(exc).__name__,
                )
                try:
                    await _close_overlay_before_offer(page)
                except FATAL_EXCEPTIONS:
                    raise
                except Exception:  # noqa: BLE001 - cleanup is best-effort
                    pass
                await human_delay((1.0, 2.0))
                try:
                    await safe_click(page, [selector], timeout_ms=10000)
                except FATAL_EXCEPTIONS:
                    raise
                except Exception as retry_exc:  # noqa: BLE001 - keep the scene
                    # 重试仍失败：再核对一次 URL。点击可能其实已经打开了套餐页
                    # （覆盖层只是挡住了 pointer-events 检查），URL 证据优先。
                    if await phase3_pricing_page_verified(page):
                        return StepOutcome(
                            StepStatus.VERIFIED,
                            "PRICING_PAGE_REACHED",
                        )
                    last_reason = (
                        f"{step_name}: entry click still blocked after overlay cleanup "
                        f"({type(retry_exc).__name__})"
                    )
                    continue

            await human_delay((2.0, 3.5))
            remaining_entries = any(
                other != selector and other not in tried for other, _ in entries
            )
            remaining_budget = max(deadline - time.monotonic(), 0.0)
            post_click_wait = (
                min(remaining_budget, 8.0) if remaining_entries else remaining_budget
            )
            if await _wait_for_pricing_page(page, wait_timeout_s=post_click_wait):
                return StepOutcome(StepStatus.VERIFIED, "PRICING_PAGE_REACHED")
            last_reason = f"{step_name}: entry clicked but pricing page did not appear"

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if not clicked:
            await asyncio.sleep(min(CHECKOUT_NAV_INTERVAL_S, remaining))

    if not found_entry:
        return StepOutcome(
            StepStatus.BUSINESS_REJECTED,
            "NO_FREE_OFFER",
            "no offer or upgrade entry found on home page",
        )
    return StepOutcome(
        StepStatus.NEEDS_OPERATOR,
        "AMOUNT_UNKNOWN",
        last_reason or "pricing page could not be reached",
    )


async def phase3_click_upgrade_entry(page) -> None:
    """Click the generic upgrade entry and wait for the pricing page."""
    await safe_click(
        page,
        [config.SELECTORS["upgrade_entry_btn"]],
        timeout_ms=10000,
    )
    await human_delay((2.0, 3.5))
    await wait_until_appears(
        page,
        [
            config.SELECTORS["target_plan_btn"],
            "[data-testid='pricing-modal-plan-grid']",
        ],
        timeout_ms=8000,
    )


def parse_offer_amount(amount_text: str) -> Optional[Decimal]:
    """Parse a displayed offer amount; return None when it is not trustworthy."""
    text = " ".join((amount_text or "").split()).strip()
    if not text:
        return None
    lowered = text.lower()
    if "無料" in text or re.search(r"\bfree\b", lowered):
        return Decimal("0")
    match = re.search(r"(?:¥|￥|\$|usd|jpy)?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:円|yen|jpy|usd)?", text, re.IGNORECASE)
    if not match:
        return None
    try:
        return Decimal(match.group(1).replace(",", ""))
    except InvalidOperation:
        return None


def classify_offer_amount(amount_text: str) -> StepOutcome:
    """Separate an unreadable amount from a verified non-zero amount."""
    parsed_amount = parse_offer_amount(amount_text)
    if parsed_amount is None:
        return StepOutcome(
            StepStatus.NEEDS_OPERATOR,
            "AMOUNT_UNKNOWN",
            "offer amount could not be verified",
        )
    if parsed_amount != Decimal("0"):
        return StepOutcome(
            StepStatus.BUSINESS_REJECTED,
            "NON_ZERO_AMOUNT",
            f"offer amount is {parsed_amount}",
        )
    return StepOutcome(StepStatus.VERIFIED, "AMOUNT_VERIFIED")


def read_plan_card_price_from_text(card_text: str) -> Optional[Decimal]:
    """Parse the trustworthy main price out of a target plan card.

    Promo cards show ¥0 for the first month together with the regular price
    afterwards; the regular (largest) price is the reliable signal.  Only
    currency-prefixed amounts are accepted so plain numerals inside prose
    (e.g. "最初の1か月間") are ignored.
    """
    amounts = []
    for match in _PRICE_TOKEN_RE.finditer(card_text or ""):
        raw = match.group(1) or match.group(2)
        try:
            amounts.append(Decimal(raw.replace(",", "")))
        except InvalidOperation:
            continue
    if not amounts:
        return None
    return max(amounts)


@dataclass(frozen=True)
class PlanAmountEvidence:
    """Structured amounts read from the target plan card on the pricing page.

    ``initial_amount`` is what the account actually pays for the first
    period; ``renewal_amount`` is the regular price afterwards.  The
    free-offer verdict must be based on ``initial_amount`` alone.
    """

    initial_amount: Optional[Decimal]
    renewal_amount: Optional[Decimal]
    currency: str
    source: str


def _plan_amount_nodes(card_text: str) -> List[Decimal]:
    """Ordered, de-duplicated amounts found in card text (regular first)."""
    amounts: List[Decimal] = []
    seen = set()
    for match in _PRICE_TOKEN_RE.finditer(card_text or ""):
        raw = match.group(1) or match.group(2)
        try:
            amount = Decimal(raw.replace(",", ""))
        except InvalidOperation:
            continue
        if amount not in seen:
            seen.add(amount)
            amounts.append(amount)
    return amounts


def _node_amounts(nodes: List[dict]) -> List[Decimal]:
    """Ordered, de-duplicated amounts read from the collected price nodes."""
    amounts: List[Decimal] = []
    seen = set()
    for node in nodes:
        node_amounts = []
        for match in _PRICE_TOKEN_RE.finditer(node.get("text") or ""):
            raw = match.group(1) or match.group(2)
            try:
                amount = Decimal(raw.replace(",", ""))
            except InvalidOperation:
                continue
            node_amounts.append(amount)
        if not node_amounts:
            own = (node.get("ownText") or "").strip().replace(",", "")
            if (
                own
                and node.get("inPriceBlock")
                and _PURE_NUMBER_RE.match(own)
            ):
                try:
                    node_amounts.append(Decimal(own))
                except InvalidOperation:
                    pass
        if node_amounts:
            amounts.append(node_amounts[0])
        for amount in node_amounts:
            if amount not in seen:
                seen.add(amount)
    return amounts


def _is_disclaimer_text(text: str) -> bool:
    lowered = text.lower()
    return any(marker.lower() in lowered for marker in _DISCLAIMER_MARKERS)


def _price_candidates(nodes: List[dict]) -> List[dict]:
    """Visible leaf price nodes, screen-reader text and containers excluded.

    ``sr-only`` / ``aria-hidden`` nodes repeat the price for screen readers
    (e.g. 「元の価格：¥3000」「割引価格：¥0」) and must never become verdict
    evidence.  Parent containers repeat their children's text, so only leaves
    (nodes carrying their own text) are trustworthy.  Buttons, feature lists
    and disclaimer footers are excluded so their incidental amounts cannot
    become a verdict.
    """
    candidates: List[dict] = []
    for node in nodes or []:
        if node.get("sr"):
            continue
        text = (node.get("text") or "").strip()
        if not text:
            continue
        own = (node.get("ownText") or "").strip()
        if not own:
            continue
        testid = (node.get("testid") or "").strip().lower()
        if _is_disclaimer_text(text):
            continue
        if "button" in testid or "feature" in testid or "headline" in testid:
            continue
        pure_number_in_block = bool(node.get("inPriceBlock")) and bool(
            _PURE_NUMBER_RE.match(own)
        )
        if not any(k in testid for k in ("price", "amount", "cost")) and not (
            _PRICE_TOKEN_RE.search(text)
            or "無料" in text
            or "free" in text.lower()
            or pure_number_in_block
        ):
            continue
        candidates.append(node)
    return candidates


def _plan_amounts_from_nodes(
    nodes: List[dict],
    *,
    free_marker_in_card: bool = False,
) -> Optional[tuple[Optional[Decimal], Optional[Decimal]]]:
    """Resolve (initial, renewal) from candidate price nodes.

    The amount that is struck through (self or any ancestor) is the regular
    price; the remaining non-struck amount is the first-period price.  Two or
    more unattributed non-struck amounts are ambiguous and return ``None``.
    """
    amounts = _node_amounts(nodes)
    if not amounts:
        return None
    plain: List[Decimal] = []
    struck: List[Decimal] = []
    for node, amount in zip(nodes, _node_amounts(nodes)):
        if amount is None:
            continue
        if node.get("strike"):
            struck.append(amount)
        else:
            plain.append(amount)
    plain = list(dict.fromkeys(plain))
    struck = list(dict.fromkeys(struck))

    initial: Optional[Decimal] = None
    renewal: Optional[Decimal] = None
    if struck:
        if len(plain) == 1:
            initial = plain[0]
            renewal = struck[0] if len(struck) == 1 else None
        elif len(plain) == 2 and plain[0] == struck[0]:
            initial, renewal = plain[0], plain[1]
        else:
            return None
    elif len(plain) == 1:
        initial = plain[0]
        # A single non-struck price is both initial and renewal on a plain
        # (non-promo) card; a card whose only price reads "free" has no
        # renewal amount to report.
        free_card = free_marker_in_card or any(
            "無料" in (node.get("text") or "")
            or re.search(r"\bfree\b", (node.get("text") or "").lower())
            for node in nodes
        )
        renewal = None if free_card else plain[0]
    else:
        return None
    return initial, renewal


async def extract_plan_amount_evidence(page) -> Optional[PlanAmountEvidence]:
    """Extract initial/renewal amounts from the target plan card.

    Struck-through nodes are renewal (regular) prices; the remaining amount
    node is the initial price.  Without strike marks (plain pricing layout)
    the single amount is both initial and renewal.  Returns ``None`` when no
    trustworthy amount pair can be read.
    """
    card = page.locator(config.SELECTORS["plan_card_container"]).first
    try:
        if not await card.is_visible(timeout=3000):
            return None
        nodes = await card.evaluate(
            """el => {
                const out = [];
                const priceNodes = el.querySelectorAll(
                    "[data-testid*='price'], [class*='price'], [class*='amount'], " +
                    "span, p, div"
                );
                for (const pn of priceNodes) {
                    const t = (pn.textContent || "").replace(/\\s+/g, " ").trim();
                    if (!t || !/[¥￥$]|円|無料|月/.test(t) || t.length > 60) {
                        continue;
                    }
                    const style = getComputedStyle(pn);
                    const own = (pn.childNodes.length === 1 && pn.firstChild
                                 && pn.firstChild.nodeType === 3)
                        ? pn.textContent.replace(/\\s+/g, " ").trim()
                        : "";
                    const cls = String(pn.className || "");
                    const sr = cls.includes("sr-only")
                        || (pn.getAttribute && pn.getAttribute("aria-hidden") === "true");
                    const inPriceBlock = !!(
                        pn.closest && pn.closest("[data-testid*='cost']")
                    );
                    let strike = style.textDecorationLine.includes("line-through");
                    if (!strike) {
                        let cur = pn.parentElement;
                        for (let depth = 0; cur && depth < 4; depth++) {
                            const cs = getComputedStyle(cur);
                            if (cs.textDecorationLine.includes("line-through")) {
                                strike = true;
                                break;
                            }
                            cur = cur.parentElement;
                        }
                    }
                    out.push({
                        text: t,
                        ownText: own,
                        strike: strike,
                        sr: sr,
                        inPriceBlock: inPriceBlock,
                        testid: pn.getAttribute && pn.getAttribute("data-testid") || "",
                    });
                }
                return out;
            }"""
        )
    except SAFE_EXCEPTIONS:
        return None

    currency = ""
    currency_code = ""
    seen_nodes: List[dict] = []
    seen_text = set()
    for node in nodes or []:
        text = (node.get("text") or "").strip()
        if not text or text in seen_text:
            continue
        seen_text.add(text)
        if _is_disclaimer_text(text):
            continue
        seen_nodes.append(node)
        currency_match = _CURRENCY_TOKEN_RE.search(text)
        if currency_match:
            token = currency_match.group(0).upper()
            if token in {"JPY", "USD"} and not currency_code:
                currency_code = token
            elif not currency_code and not currency:
                currency = token
    if currency_code:
        currency = currency_code

    card_text = ""
    try:
        card_text = await card.inner_text(timeout=2000)
    except SAFE_EXCEPTIONS:
        pass
    if not currency and card_text:
        currency_match = _CURRENCY_TOKEN_RE.search(card_text)
        if currency_match:
            currency = currency_match.group(0).upper()
    candidates = _price_candidates(seen_nodes)
    amounts = _node_amounts(candidates)
    if not amounts:
        amounts = _plan_amount_nodes(card_text)
    if not amounts:
        return None

    if candidates:
        resolved = _plan_amounts_from_nodes(
            candidates,
            free_marker_in_card="無料" in card_text,
        )
    else:
        resolved = None
    if resolved is None and amounts and len(amounts) == 1:
        free_card = "無料" in card_text or "free" in card_text.lower()
        resolved = (amounts[0], None if free_card else amounts[0])
    if resolved is None:
        return None
    initial, renewal = resolved

    if initial is None:
        return None
    return PlanAmountEvidence(
        initial_amount=initial,
        renewal_amount=renewal,
        currency=currency,
        source="card",
    )


def classify_plan_amounts(evidence: PlanAmountEvidence) -> StepOutcome:
    """Strict first-payment verdict from structured plan-card evidence."""
    if evidence.initial_amount is None:
        return StepOutcome(
            StepStatus.NEEDS_OPERATOR,
            "AMOUNT_UNKNOWN",
            "initial plan amount could not be verified",
        )
    if evidence.initial_amount != Decimal("0"):
        return StepOutcome(
            StepStatus.BUSINESS_REJECTED,
            "NON_ZERO_AMOUNT",
            f"initial plan amount is {evidence.initial_amount}",
        )
    return StepOutcome(StepStatus.VERIFIED, "AMOUNT_VERIFIED")


async def read_plan_card_price(page) -> Optional[Decimal]:
    """Read the target plan price, retrying until the pricing modal settles.

    The pricing modal renders asynchronously after navigation; a single
    visibility probe can fire too early and produce a false AMOUNT_UNKNOWN.
    Prefer the dedicated ``plus-pricing-column-cost`` element (bound to the
    target plan, excludes the free-plan row), then fall back to the card text.
    """
    selectors = [
        config.SELECTORS["target_plan_price"],
        config.SELECTORS["plan_card_container"],
    ]
    for attempt in range(3):
        for selector in selectors:
            try:
                node = page.locator(selector).first
                if not await node.is_visible(timeout=2500):
                    continue
                text = await node.inner_text(timeout=2000)
                price = read_plan_card_price_from_text(text)
                if price is not None:
                    return price
            except SAFE_EXCEPTIONS:
                continue
        if attempt < 2:
            await asyncio.sleep(1.5)
    return None


async def phase3_amount_guard(
    page,
    email: str,
    *,
    wait_timeout_s: float = CHECKOUT_NAV_TIMEOUT_S,
) -> StepOutcome:
    """Verify the *initial* amount on the target plan card (strict).

    The URL promo marker and the target-button copy are navigation clues only;
    the verdict comes from the initial amount actually shown on the plan card.
    A limited-time badge (期間限定) on the target card is the operator-
    confirmed free-offer signal and verifies immediately.  Free button copy
    contradicted by a non-zero card is AMOUNT_UNKNOWN.
    """
    deadline = time.monotonic() + max(wait_timeout_s, 0.0)
    last_unreadable: StepOutcome | None = None
    while True:
        badges = await _target_plan_badge_texts(page)
        if any(_LIMITED_TIME_BADGE in badge for badge in badges):
            logger.info(
                "phase3: limited-time badge detected for %s (free promotion)",
                mask_email(email),
            )
            return StepOutcome(StepStatus.VERIFIED, "AMOUNT_VERIFIED")

        evidence = await extract_plan_amount_evidence(page)
        if evidence is not None:
            outcome = classify_plan_amounts(evidence)
            btn_text = (await _target_btn_text(page)).lower()
            button_claims_free = any(
                keyword in btn_text for keyword in FREE_OFFER_KEYWORDS
            )
            if outcome.status is StepStatus.BUSINESS_REJECTED and button_claims_free:
                # A "free" button contradicted by a non-zero initial amount is
                # ambiguous and must go to the operator instead of silently rejecting.
                logger.error(
                    "phase3: free button contradicts non-zero initial amount for %s",
                    mask_email(email),
                )
                return StepOutcome(
                    StepStatus.NEEDS_OPERATOR,
                    "AMOUNT_UNKNOWN",
                    "free button copy conflicts with non-zero initial amount",
                )
            if outcome.status is StepStatus.BUSINESS_REJECTED:
                await safe_screenshot(page, evidence_path(email, "no_offer"))
                logger.error(
                    "phase3: initial plan amount is %s for %s",
                    evidence.initial_amount,
                    mask_email(email),
                )
            return outcome

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return await _amount_unreadable(page, email, screenshot=True)
        last_unreadable = await _amount_unreadable(page, email, screenshot=False)
        await asyncio.sleep(min(CHECKOUT_NAV_INTERVAL_S, remaining))


async def _target_btn_text(page) -> str:
    try:
        btn = page.locator(config.SELECTORS["target_plan_btn"]).first
        if await btn.is_visible(timeout=2000):
            return (await btn.inner_text(timeout=1000) or "").strip()
    except SAFE_EXCEPTIONS:
        pass
    return ""


async def _target_plan_badge_texts(page) -> List[str]:
    """Read the plan-card badge texts (e.g. 「期間限定」 / 「おすすめ」).

    A limited-time badge (期間限定) is the operator-confirmed signal that the
    target plan currently carries a free first-month promotion; the paid-only
    layout shows 「おすすめ」 instead.
    """
    texts: List[str] = []
    try:
        nodes = page.locator(config.SELECTORS["plan_badge"])
        count = int(await nodes.count())
        for index in range(min(count, 6)):
            node = nodes.nth(index)
            if await node.is_visible(timeout=1500):
                text = (await node.inner_text(timeout=1000) or "").strip()
                if text:
                    texts.append(text[:60])
    except Exception:
        # The badge is an acceleration signal only; any read failure must
        # never break the amount-based verdict path.
        logger.debug("phase3: plan badge could not be read", exc_info=True)
    return texts


async def _amount_unreadable(page, email: str, *, screenshot: bool = True) -> StepOutcome:
    logger.error(
        "phase3: amount verification stopped for %s: AMOUNT_UNKNOWN",
        mask_email(email),
    )
    if screenshot:
        try:
            await safe_screenshot(page, evidence_path(email, "amount_unverified"))
        except SAFE_EXCEPTIONS:
            pass
    return StepOutcome(
        StepStatus.NEEDS_OPERATOR,
        "AMOUNT_UNKNOWN",
        "offer amount could not be verified",
    )


async def phase3_click_offer(page) -> None:
    await safe_click(page, [config.SELECTORS["free_offer_btn"]], timeout_ms=10000)
    await human_delay((2.0, 3.5))
    # Confirm offer page loaded (offer button should be gone)
    await wait_until_gone(page, [config.SELECTORS["free_offer_btn"]], timeout_ms=5000)


async def phase3_accept_offer(page) -> None:
    # Target plan button uses the confirmed stable data-testid; text-based
    # selectors with multiple :has-text conditions break CloakBrowser's
    # humanized click resolution.
    await safe_click(page, [config.SELECTORS["target_plan_btn"]], timeout_ms=10000)
    await human_delay((3.0, 5.0))
    # Confirm Stripe page loaded (iframe appears with card inputs inside)
    await wait_until_appears(page, [
        "div.__PrivateStripeElement iframe",
        "iframe[title*='セキュア']",
        "iframe[title*='secure']",
        "iframe[src*='stripe.com']",
        "iframe[name*='__privateStripeFrame']",
    ], timeout_ms=8000)
    # CRITICAL: iframe DOM existing does NOT mean the sub-document has
    # loaded its internal Stripe JS + rendered the card inputs.
    # Wait 1.5-2.5s for the child document to fully initialize.
    await human_delay((1.5, 2.5))


# ═══════════════════════════════════════════════════════════════════
# 创建账户 Offer 资格验收（account_provision 附加闸门）
#
# 判据（主人确认，见 docs/account-provision-offer-check-and-report-plan.md）：
#   只有 promo_campaign 精确等于配置目标才算有 Offer。
#   go-2-months-50-pct-off 等非目标活动一律视为没有 Offer。
# ═══════════════════════════════════════════════════════════════════


class _AmbiguousCampaign(str):
    """Sentinel for a promo_campaign query that cannot be trusted."""


AMBIGUOUS_PROMO_CAMPAIGN = _AmbiguousCampaign("")


@dataclass(frozen=True)
class OfferEligibilityPolicy:
    """Immutable per-batch offer target; snapshot at batch start.

    The target campaign comes from ``config.OFFER_ELIGIBILITY_CONFIG`` and is
    frozen once per batch/session so mid-run config edits cannot change the
    verdict of accounts already being checked.
    """

    target_promo_campaign: str


def build_offer_eligibility_policy(
    target_promo_campaign: Optional[str] = None,
) -> OfferEligibilityPolicy:
    """Build and validate the offer policy (defaults to config value)."""
    campaign = target_promo_campaign
    if campaign is None:
        campaign = config.OFFER_ELIGIBILITY_CONFIG["target_promo_campaign"]
    if not isinstance(campaign, str):
        raise ValueError(
            "offer target_promo_campaign must be a string, "
            f"got {type(campaign).__name__}"
        )
    stripped = campaign.strip()
    if not stripped:
        raise ValueError("offer target_promo_campaign must not be empty")
    if stripped != campaign:
        raise ValueError(
            "offer target_promo_campaign must not contain leading/trailing whitespace"
        )
    if any(ch not in _PROMO_CAMPAIGN_ALLOWED for ch in campaign.lower()):
        raise ValueError(
            "offer target_promo_campaign contains invalid characters: "
            f"{campaign!r}"
        )
    return OfferEligibilityPolicy(target_promo_campaign=stripped.lower())


def parse_promo_campaign(url: str) -> Optional[str]:
    """Extract one trustworthy ``promo_campaign`` value (case-insensitive).

    Returns ``AMBIGUOUS_PROMO_CAMPAIGN`` when the parameter is duplicated,
    None when it is absent or empty.
    """
    try:
        values = parse_qs(urlparse(url or "").query).get("promo_campaign")
    except ValueError:
        return None
    if not values or not values[0]:
        return None
    if len(values) > 1:
        return AMBIGUOUS_PROMO_CAMPAIGN
    return values[0].strip().lower() or None


def is_pricing_url(url: str) -> bool:
    """True when the URL is on the real chatgpt.com pricing page."""
    try:
        parsed = urlparse(url or "")
    except ValueError:
        return False
    hostname = (parsed.hostname or "").lower()
    if hostname not in {"chatgpt.com", "www.chatgpt.com"}:
        return False
    fragment = parsed.fragment or ""
    path = parsed.path or ""
    return fragment == "pricing" or path.rstrip("/") == "/pricing"


def classify_offer_by_pricing_url(
    url: str,
    *,
    policy: Optional[OfferEligibilityPolicy] = None,
) -> str:
    """Return ``HAS_OFFER`` / ``NO_OFFER`` for a reached URL.

    The gate is deliberately binary for account_provision: only the exact
    target campaign on the real chatgpt.com pricing page is ``HAS_OFFER``.
    Every other outcome - wrong domain, missing pricing marker, duplicated
    campaign parameter, malformed URL, or any other campaign - is a settled
    ``NO_OFFER``.  A ``FAILED`` verdict must not escape here: it later turns
    a correct no-offer result into SKIPPED in continuous mode.
    """
    effective = policy or build_offer_eligibility_policy()
    campaign = parse_promo_campaign(url)
    if not is_pricing_url(url) or campaign is AMBIGUOUS_PROMO_CAMPAIGN:
        return "NO_OFFER"
    if campaign == effective.target_promo_campaign:
        return "HAS_OFFER"
    return "NO_OFFER"


@dataclass
class OfferEligibilityResult:
    """Typed verdict of the account_provision offer gate."""

    # HAS_OFFER | NO_OFFER | UNREACHABLE
    # - HAS_OFFER: 真实套餐页 + 精确命中目标 campaign
    # - NO_OFFER:   主页无任何入口（合理判定没有 offer）
    # - UNREACHABLE: 入口存在但点不进套餐页（导航失败，不是没有 offer）
    result: str
    page_url: str = ""
    promo_campaign: Optional[str] = None
    screenshot_path: Optional[str] = None
    reason: str = ""


async def verify_offer_eligibility(
    page,
    email: str,
    *,
    policy: Optional[OfferEligibilityPolicy] = None,
) -> OfferEligibilityResult:
    """Open the pricing page and classify the account's target offer.

    Used only by ``account_provision``.  Never clicks accept-offer, never
    opens Stripe, never claims a card.
    """
    effective = policy or build_offer_eligibility_policy()
    screenshot_path: Optional[str] = None
    page_url = ""
    try:
        page_url = page.url
    except Exception:  # noqa: BLE001 — page may already be gone
        pass
    try:
        navigate_outcome = await phase3_open_pricing_page(page)
        if not navigate_outcome.verified:
            try:
                screenshot_path = evidence_path(email, "offer_no")
                await safe_screenshot(page, screenshot_path)
            except SAFE_EXCEPTIONS:
                screenshot_path = None
            if (
                navigate_outcome.status is StepStatus.BUSINESS_REJECTED
                and navigate_outcome.code == "NO_FREE_OFFER"
            ):
                return OfferEligibilityResult(
                    result="NO_OFFER",
                    page_url=page_url,
                    screenshot_path=screenshot_path,
                    reason="no offer or upgrade entry found on home page",
                )
            # 主页存在入口但无法进入套餐页：导航失败，不是没有 offer。
            # 连续模式应记执行失败（报告 failed 区），不得吞成 NO_OFFER。
            return OfferEligibilityResult(
                result="UNREACHABLE",
                page_url=page_url,
                screenshot_path=screenshot_path,
                reason=f"pricing page unreachable: {navigate_outcome.code}",
            )
        page_url = page.url
        campaign = parse_promo_campaign(page_url)
        verdict = classify_offer_by_pricing_url(page_url, policy=effective)
        try:
            kind = "offer_has" if verdict == "HAS_OFFER" else "offer_no"
            screenshot_path = evidence_path(email, kind)
            await safe_screenshot(page, screenshot_path)
        except SAFE_EXCEPTIONS:
            screenshot_path = None
        return OfferEligibilityResult(
            result=verdict,
            page_url=page_url,
            promo_campaign=campaign,
            screenshot_path=screenshot_path,
            reason="" if verdict == "HAS_OFFER" else f"promo_campaign={campaign!r}",
        )
    except SAFE_EXCEPTIONS:
        try:
            screenshot_path = evidence_path(email, "offer_no")
            await safe_screenshot(page, screenshot_path)
        except SAFE_EXCEPTIONS:
            screenshot_path = None
        return OfferEligibilityResult(
            result="UNREACHABLE",
            page_url=page_url,
            screenshot_path=screenshot_path,
            reason="offer eligibility check interrupted (navigation unknown)",
        )
