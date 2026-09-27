"""
Stripe payment processing — iframe detection, card/address fill, confirm & rejection retry.

Extracted from phase3.py to reduce module size and isolate Stripe-specific logic.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import List, Optional

import config
from gpt_automation.phases.human_input import (
    human_clear_in_frame,
    human_type_in_frame,
)
from gpt_automation.utils.exceptions import SAFE_EXCEPTIONS, ElementNotFoundError, SkipEmailException
from gpt_automation.utils.timing import (
    CHECKOUT_NAV_INTERVAL_S,
    CHECKOUT_NAV_TIMEOUT_S,
    human_delay,
)
from gpt_automation.phases.helpers import (
    safe_click, _first_visible, human_pause,
)
from gpt_automation.workflow.models import StepOutcome, StepStatus

logger = logging.getLogger(__name__)


def normalize_digits(value: str) -> str:
    """Strip non-digit characters for Stripe-formatted input comparison."""
    return re.sub(r"\D", "", value or "")


# ------------------------------------------------------------------
# Checkout amount second gate (阶段三)
# ------------------------------------------------------------------


@dataclass(frozen=True)
class CheckoutAmountEvidence:
    """Amount actually due today on the Stripe checkout page."""

    initial_amount: Optional[Decimal]
    currency: str
    source: str


_CHECKOUT_AMOUNT_TOKEN_RE = re.compile(
    r"(?:¥|￥)\s*([0-9][0-9,]*(?:\.[0-9]+)?)"
    r"|([0-9][0-9,]*(?:\.[0-9]+)?)\s*円"
)


def _parse_checkout_amount(text: str) -> Optional[Decimal]:
    match = _CHECKOUT_AMOUNT_TOKEN_RE.search(text or "")
    if not match:
        return None
    try:
        return Decimal((match.group(1) or match.group(2)).replace(",", ""))
    except InvalidOperation:
        return None


def _selector_candidates(raw: str) -> tuple[str, ...]:
    value = (raw or "").strip()
    if not value:
        return ()
    parts = tuple(item.strip() for item in value.split(",") if item.strip())
    return tuple(dict.fromkeys((value, *parts)))


async def extract_checkout_amount_evidence(page) -> Optional[CheckoutAmountEvidence]:
    """Read the amount due today from the Stripe checkout page.

    Dual-evidence model:
      1. Verified due-today label line / parent hierarchy (e.g. 「本日のお支払額 ¥0」).
      2. Specific container selectors (config.SELECTORS["checkout_amount_containers"]) if configured.
    """
    label_raw = config.SELECTORS.get("checkout_amount_label", "")
    containers_raw = config.SELECTORS.get("checkout_amount_containers") or ""
    containers = [
        selector.strip()
        for selector in containers_raw.split(",")
        if selector.strip()
    ]

    amount_text = ""
    try:
        for label_selector in _selector_candidates(label_raw):
            try:
                label = page.locator(label_selector).first
                if await label.is_visible(timeout=2500):
                    amount_text = (await label.inner_text(timeout=2000) or "").strip()
                    if _parse_checkout_amount(amount_text) is not None:
                        break
                    try:
                        parent = label.locator("..").first
                        parent_text = (await parent.inner_text(timeout=1500) or "").strip()
                        if _parse_checkout_amount(parent_text) is not None:
                            amount_text = parent_text
                            break
                    except SAFE_EXCEPTIONS:
                        pass
            except SAFE_EXCEPTIONS:
                continue

        if not amount_text and containers:
            for selector in containers:
                try:
                    node = page.locator(selector).first
                    if await node.is_visible(timeout=2000):
                        amount_text = (await node.inner_text(timeout=2000) or "").strip()
                        if _parse_checkout_amount(amount_text) is not None:
                            break
                except SAFE_EXCEPTIONS:
                    continue
    except SAFE_EXCEPTIONS:
        return None

    amount = _parse_checkout_amount(amount_text)
    if amount is None:
        return None
    currency = "JPY" if ("円" in amount_text or "¥" in amount_text or "￥" in amount_text) else ""
    return CheckoutAmountEvidence(
        initial_amount=amount,
        currency=currency,
        source="checkout",
    )


async def checkout_amount_gate(
    page,
    *,
    wait_timeout_s: float = CHECKOUT_NAV_TIMEOUT_S,
) -> StepOutcome:
    """Fail-closed verdict for the amount actually due on the Stripe page.

    The card must never be requested unless the evidence is explicitly 0 JPY.
    Polls up to wait_timeout_s for late render.
    """
    deadline = time.time() + max(wait_timeout_s, 0.0)
    while True:
        evidence = await extract_checkout_amount_evidence(page)
        if evidence is not None and evidence.initial_amount is not None:
            conflicting = await _checkout_amounts_conflict(page)
            if conflicting:
                return StepOutcome(
                    StepStatus.NEEDS_OPERATOR,
                    "CHECKOUT_AMOUNT_UNKNOWN",
                    "multiple visible checkout amounts disagree",
                )
            return classify_checkout_amount(evidence)
        if time.time() >= deadline:
            return StepOutcome(
                StepStatus.NEEDS_OPERATOR,
                "CHECKOUT_AMOUNT_UNKNOWN",
                "checkout amount could not be verified",
            )
        await asyncio.sleep(CHECKOUT_NAV_INTERVAL_S)


async def _checkout_amounts_conflict(page) -> bool:
    """True when two visible amount sources disagree on the amount."""
    seen: List[Decimal] = []
    try:
        label_raw = config.SELECTORS.get("checkout_amount_label", "")
        for label_selector in _selector_candidates(label_raw):
            try:
                label = page.locator(label_selector).first
                if await label.is_visible(timeout=2500):
                    text = (await label.inner_text(timeout=2000) or "").strip()
                    amount = _parse_checkout_amount(text)
                    if amount is not None:
                        seen.append(amount)
                    else:
                        try:
                            parent = label.locator("..").first
                            p_text = (await parent.inner_text(timeout=1500) or "").strip()
                            p_amount = _parse_checkout_amount(p_text)
                            if p_amount is not None:
                                seen.append(p_amount)
                        except SAFE_EXCEPTIONS:
                            pass
            except SAFE_EXCEPTIONS:
                continue

        containers_raw = config.SELECTORS.get("checkout_amount_containers") or ""
        for selector in [
            s.strip() for s in containers_raw.split(",") if s.strip()
        ]:
            try:
                node = page.locator(selector).first
                if await node.is_visible(timeout=2000):
                    amount = _parse_checkout_amount(
                        (await node.inner_text(timeout=2000) or "").strip()
                    )
                    if amount is not None:
                        seen.append(amount)
            except SAFE_EXCEPTIONS:
                continue
    except SAFE_EXCEPTIONS:
        return True
    return len(set(seen)) > 1


def classify_checkout_amount(
    evidence: Optional[CheckoutAmountEvidence],
) -> StepOutcome:
    """Verdict for the amount actually due today on the checkout page."""
    if evidence is None or evidence.initial_amount is None:
        return StepOutcome(
            StepStatus.NEEDS_OPERATOR,
            "CHECKOUT_AMOUNT_UNKNOWN",
            "checkout amount could not be verified",
        )
    if evidence.initial_amount != Decimal("0"):
        return StepOutcome(
            StepStatus.BUSINESS_REJECTED,
            "CHECKOUT_NON_ZERO_AMOUNT",
            f"checkout amount is {evidence.initial_amount}",
        )
    return StepOutcome(StepStatus.VERIFIED, "CHECKOUT_AMOUNT_VERIFIED")


# ------------------------------------------------------------------
# Stripe iframe detection
# ------------------------------------------------------------------


async def _get_stripe_frame(page):
    """Return the REAL Stripe Payment Element Frame containing card inputs.

    CloakBrowser humanize=True rejects locator-level action methods on
    FrameLocator-derived locators (UnsupportedHumanizeSelectorError on
    chained enter-frame selectors), so iframe input must go through the
    frame-level API of a real Frame object.  This resolver iterates
    page.frames (skipping the main frame) and probes each sub-frame for
    input[name='number']; the express-checkout iframe has no such input,
    so content probing alone distinguishes the payment element.

    Returns:
        Real playwright Frame (a member of page.frames).

    Raises:
        ElementNotFoundError: if no card-input Stripe iframe is found.
    """
    for frame in getattr(page, "frames", []):
        try:
            if frame is getattr(page, "main_frame", None):
                continue
            await asyncio.sleep(0.5)  # allow sub-document attach
            count = await frame.locator("input[name='number']").count()
            if count > 0:
                logger.info("phase3: stripe payment frame found (input[name=number])")
                return frame
        except SAFE_EXCEPTIONS:
            continue
    raise ElementNotFoundError("no visible Stripe iframe found on page")


async def wait_for_card_number_ready(
    page,
    *,
    timeout_s: float = CHECKOUT_NAV_TIMEOUT_S,
):
    """Poll until the Payment-Element card input iframe is fillable."""
    deadline = time.time() + max(timeout_s, 0.0)
    while True:
        try:
            return await _get_stripe_frame(page)
        except ElementNotFoundError:
            pass
        if time.time() >= deadline:
            raise ElementNotFoundError(
                "card number input did not become ready within checkout wait window"
            )
        await asyncio.sleep(CHECKOUT_NAV_INTERVAL_S)


# ------------------------------------------------------------------
# Stripe input helpers
# ------------------------------------------------------------------






async def _stripe_type_fill(
    frame, selectors: List[str], value: str, timeout_ms: int = 5000,
    *, label: str = "",
) -> None:
    """Fill a Stripe Payment Element input with frame-level per-key typing.

    1. Locates the element via parallel selector polling on the real Frame.
    2. Types every character through human_type_in_frame (frame.type with
       a randomized human key delay — no instant fill fast path).
    3. Verifies the value actually stuck by reading input_value().
    4. On mismatch, clears and retypes once with the same typing method.

    Args:
        frame: Real playwright Frame inside the Stripe iframe.
        selectors: CSS selectors to try (first visible wins).
        value: The value to enter (e.g. card number, expiry, CVC).
        timeout_ms: Timeout for finding the element.
        label: Optional context label for error messages.

    Raises:
        ElementNotFoundError: if the element can't be found or the frame-level
            typing helper gives up after its single retype.
    """
    _loc, matched_sel = await _first_visible(frame, selectors, timeout_ms=timeout_ms)
    logger.info("phase3: stripe element found via '%s'", matched_sel)

    # frame 级逐键输入：CloakBrowser humanize 对 FrameLocator 派生 locator
    # 的动作方法会抛 UnsupportedHumanizeSelectorError，必须走真实 Frame 的
    # frame.type / frame.press（真实几何解析 + 页面级人工键盘）。
    ctx = f" [{label}]" if label else ""
    try:
        await human_type_in_frame(
            frame,
            matched_sel,
            value,
            field_name=f"stripe_{label or 'field'}",
            normalize=normalize_digits,
        )
    except ElementNotFoundError as exc:
        raise ElementNotFoundError(f"stripe typing failed{ctx}: {exc}") from exc


# ------------------------------------------------------------------
# Stripe card fill
# ------------------------------------------------------------------


async def phase3_stripe_card(page, card_no: str, card_expiry: str, card_cvc: str) -> None:
    """Fill Stripe Payment Element fields: card number, expiry, CVC.

    Stripe Payment Element in ChatGPT renders all three card inputs inside
    a <div class="__PrivateStripeElement"> → <iframe> hierarchy.
    We locate the iframe first, then fill each field within it using
    _stripe_type_fill (fill() + verify + type() fallback).
    """
    # Normalize expiry: "06/2031" → "0631" (Stripe expects MMYY, not MMYYYY)
    raw_expiry = card_expiry
    card_expiry = _normalize_expiry(card_expiry)
    if raw_expiry != card_expiry:
        logger.info("phase3: normalized card expiry format")

    # ── Log which card is being used (full number for traceability) ──
        logger.info("phase3: filling payment details")

    # Context label for error messages
    card_ctx = "payment-card"

    # Step 1: Find the Stripe iframe (already delayed 1.5-2.5s from accept_offer)
    # Extra 0.3-0.8s safety margin for any remaining iframe sub-doc init
    await asyncio.sleep(0.5)
    frame = await _get_stripe_frame(page)
    logger.info("phase3: filling card fields inside Stripe iframe")

    # Step 2: Fill each field inside the iframe
    # ── Card number ──
    await _stripe_type_fill(frame, [
        "input[name='number']",
        "#payment-numberInput",
        "input[autocomplete='cc-number']",
        "input[placeholder*='1234']",
        ".p-CardNumberInput-input",
    ], card_no, timeout_ms=5000, label=card_ctx)
    await human_delay((0.5, 1.0))

    # ── Expiry ──
    await _stripe_type_fill(frame, [
        "input[name='expiry']",
        "#payment-expiryInput",
        "input[autocomplete='cc-exp']",
        "input[placeholder*='月']",
        ".p-Input-input--numeric[name='expiry']",
    ], card_expiry, timeout_ms=5000, label=card_ctx)
    await human_delay((0.5, 1.0))

    # ── CVC ──
    await _stripe_type_fill(frame, [
        "input[name='cvc']",
        "#payment-cvcInput",
        "input[autocomplete='cc-csc']",
        "input[placeholder='CVC']",
        ".p-CardCvcInput input",
    ], card_cvc, timeout_ms=5000, label=card_ctx)
    await human_delay((0.5, 1.0))


# ------------------------------------------------------------------
# Stripe address fill
# ------------------------------------------------------------------


async def _get_stripe_address_frame(page):

    """Return the REAL Stripe Address Element Frame containing the postal input.

    Mirrors _get_stripe_frame: content probing over page.frames so all
    subsequent input uses frame-level APIs (CloakBrowser humanize safe).

    Raises:
        ElementNotFoundError: if no address iframe is found.
    """
    for frame in getattr(page, "frames", []):
        try:
            if frame is getattr(page, "main_frame", None):
                continue
            await asyncio.sleep(0.5)
            count = await frame.locator("#billingAddress-postalCodeInput").count()
            if count > 0:
                logger.info("phase3: address iframe found (postalCode input)")
                return frame
        except SAFE_EXCEPTIONS:
            continue
    raise ElementNotFoundError("no visible Stripe address iframe found on page")


async def wait_for_address_frame_ready(
    page,
    *,
    timeout_s: float = CHECKOUT_NAV_TIMEOUT_S,
    max_attempts: Optional[int] = None,
):
    """Poll until the billing-address iframe + postal input are present."""
    deadline = time.time() + max(timeout_s, 0.0)
    attempt = 0
    while True:
        attempt += 1
        try:
            return await _get_stripe_address_frame(page)
        except ElementNotFoundError:
            pass
        time_exhausted = time.time() >= deadline
        attempts_exhausted = max_attempts is not None and attempt >= max_attempts
        if time_exhausted and (max_attempts is None or attempts_exhausted):
            raise ElementNotFoundError(
                "address iframe did not become ready within checkout wait window"
            )
        await asyncio.sleep(CHECKOUT_NAV_INTERVAL_S)


async def _safe_address_fill(frame, css: str, expected: str, label: str) -> bool:
    """Fill an input only if it doesn't already contain the expected value.

    Retry-safe: on re-execution, skips fields already correctly filled,
    clears and retypes with frame-level per-key typing if wrong text.

    Returns:
        True if the field was filled (or was already correct).
    """
    try:
        await human_type_in_frame(
            frame,
            css,
            expected,
            field_name=f"stripe_address_{label}",
        )
        logger.info("phase3: address %s filled", label)
        return True
    except SAFE_EXCEPTIONS as exc:
        logger.warning("phase3: address %s fill failed: %s", label, exc)
        return False


async def _select_postal_suggestion(addr_frame, zip_css: str, zip_code: str) -> bool:
    """Select the first postal-code suggestion and verify that it closes."""
    option = addr_frame.locator("[role='option']").first
    try:
        await option.wait_for(state="visible", timeout=5000)
    except SAFE_EXCEPTIONS:
        return False

    await asyncio.sleep(0.3)
    try:
        await option.click(timeout=3000)
        logger.info("phase3: clicked first Google Places postal suggestion")
    except SAFE_EXCEPTIONS as exc:
        logger.warning(
            "phase3: postal suggestion click failed, falling back to keyboard: %s",
            exc,
        )
        try:
            # frame 级键盘：locator 级 press 在 iframe 内不安全（Cloak humanize）
            await addr_frame.press(zip_css, "ArrowDown")
            await addr_frame.press(zip_css, "Enter")
        except SAFE_EXCEPTIONS as keyboard_exc:
            logger.warning(
                "phase3: keyboard postal suggestion selection failed: %s",
                keyboard_exc,
            )
            return False

    try:
        await option.wait_for(state="hidden", timeout=5000)
    except SAFE_EXCEPTIONS:
        logger.warning("phase3: postal suggestion remained visible after selection")
        return False
    logger.info("phase3: postal suggestion closed after selection for %s", zip_code)
    return True


async def _enter_address_autocomplete(addr_frame, zip_css: str) -> bool:
    """Press Enter in the postal-code field and verify Stripe's auto-fill.

    Operator-confirmed behavior (2026-08-08): after typing the Japanese
    postal code, pressing Enter makes Stripe auto-fill prefecture, city and
    address line 1.  Returns True only when all three became non-empty; the
    caller then skips the dropdown/manual fallbacks.
    """
    try:
        # frame 级键盘：locator 级 press 在 iframe 内不安全（Cloak humanize）
        await addr_frame.press(zip_css, "Enter")
    except SAFE_EXCEPTIONS as exc:
        return False
    await asyncio.sleep(1.5)
    city = await _address_field_value(
        addr_frame,
        ["#billingAddress-localityInput", "input[name='locality']"],
    )
    pref = await _address_field_value(
        addr_frame,
        [
            "#billingAddress-administrativeAreaInput",
            "select[name='administrativeArea']",
        ],
    )
    line1 = await _address_field_value(
        addr_frame,
        ["#billingAddress-addressLine1Input", "input[name='addressLine1']"],
    )
    if city and pref and line1:
        logger.info("phase3: postal Enter autocomplete filled pref/city/line1")
        return True
    logger.warning(
        "phase3: postal Enter autocomplete incomplete "
        "(pref=%r city=%r line1=%r)",
        pref,
        city,
        line1,
    )
    return False


async def _wait_for_address_field(
    addr_frame, selectors: list[str], timeout: int = 5000, *, return_selector: bool = False,
):
    """Return the first visible address field (locator or selector), or ``None``."""
    for selector in selectors:
        try:
            loc = addr_frame.locator(selector).first
            await loc.wait_for(state="visible", timeout=timeout)
            if return_selector:
                return selector
            return loc
        except SAFE_EXCEPTIONS:
            continue
    return None


async def _address_field_value(addr_frame, selectors: list[str]) -> str:
    """Read the first available address input without changing page state."""
    for selector in selectors:
        try:
            loc = addr_frame.locator(selector).first
            return (await loc.input_value() or "").strip()
        except SAFE_EXCEPTIONS:
            continue
    return ""


async def _verify_stripe_address_fields(
    addr_frame,
    zip_code: str,
    addr1: str,
    addr2: str,
) -> bool:
    """Verify the completed Stripe address form.

    The postal code is verified strictly.  Address line 1 only needs to be
    non-empty: the postal autocomplete fills a canonical address that may
    differ from the address-pool copy (e.g. 「荒川1丁目1番1号」 vs 「1-1-1」)
    and is preserved as-is.  Line 2, when expected, is verified strictly.
    """
    zip_clean = re.sub(r"\D", "", zip_code)
    expected_zip = (
        f"{zip_clean[:3]}-{zip_clean[3:]}"
        if len(zip_clean) == 7
        else zip_code
    )
    postal = await _address_field_value(
        addr_frame,
        ["#billingAddress-postalCodeInput", "input[name='postalCode']"],
    )
    line1 = await _address_field_value(
        addr_frame,
        ["#billingAddress-addressLine1Input", "input[name='addressLine1']"],
    )
    if postal != expected_zip or not line1.strip():
        return False

    if addr2.strip():
        line2 = await _address_field_value(
            addr_frame,
            ["#billingAddress-addressLine2Input", "input[name='addressLine2']"],
        )
        if line2 != addr2.strip():
            return False
    return True


async def verify_stripe_address_ready(
    page,
    zip_code: str,
    addr1: str,
    addr2: str,
) -> bool:
    """Verify the completed Stripe address form for guarded manual resume."""
    try:
        addr_frame = await _get_stripe_address_frame(page)
        return await _verify_stripe_address_fields(addr_frame, zip_code, addr1, addr2)
    except SAFE_EXCEPTIONS:
        return False


async def phase3_stripe_address(
    page, zip_code: str, pref: str, city: str, addr1: str, addr2: str, name: str,
) -> None:
    """Fill Stripe billing address inside the address iframe."""
    addr_frame = await wait_for_address_frame_ready(page)
    logger.info("phase3: filling address fields inside Stripe address iframe")

    # Normalize zip: Stripe Japan expects "xxx-xxxx" format (with hyphen).
    # Input may be "270-1367" (already correct) or "2701367" (7 digits, needs hyphen).
    zip_clean = re.sub(r"\D", "", zip_code)
    if len(zip_clean) == 7:
        zip_formatted = f"{zip_clean[:3]}-{zip_clean[3:]}"
    else:
        zip_formatted = zip_code  # Keep original if unexpected format
    if zip_formatted != zip_code:
        logger.info("phase3: normalized postal code format")
        zip_code = zip_formatted

    # Step 2: Fill name
    name_filled = False
    for name_sel in ["#billingAddress-nameInput", "input[name='name']"]:
        try:
            loc = addr_frame.locator(name_sel).first
            if await loc.is_visible(timeout=2000):
                name_filled = await _safe_address_fill(addr_frame, name_sel, name, "name")
                break
        except SAFE_EXCEPTIONS:
            continue
    if not name_filled:
        logger.warning("phase3: address name input not found")
    await asyncio.sleep(0.3)

    # Step 3: Type postal code (shared per-key typing also triggers Google
    # Places autocomplete because real keyboard events are dispatched).
    zip_filled = False
    zip_sel_matched = None
    for zip_sel in ["#billingAddress-postalCodeInput", "input[name='postalCode']"]:
        try:
            loc = addr_frame.locator(zip_sel).first
            if await loc.is_visible(timeout=2000):
                await human_type_in_frame(
                    addr_frame,
                    zip_sel,
                    zip_code,
                    field_name="stripe_address_zip",
                )
                await asyncio.sleep(0.5)
                zip_filled = True
                zip_sel_matched = zip_sel
                logger.info("phase3: address zip typed via '%s'", zip_sel)
                break
        except SAFE_EXCEPTIONS:
            continue
    if not zip_filled:
        logger.warning("phase3: address zip input not found")
    await asyncio.sleep(0.5)

    # Step 4: Press Enter in the postal-code field — Stripe auto-fills
    # prefecture / city / address line 1 (operator-confirmed behavior).
    # The Google Places dropdown click is only a fallback.
    dropdown_prefilled = False
    if zip_filled and zip_sel_matched is not None and await _enter_address_autocomplete(
        addr_frame, zip_sel_matched
    ):
        dropdown_prefilled = True
    elif zip_filled and zip_sel_matched is not None and await _select_postal_suggestion(
        addr_frame, zip_sel_matched, zip_code
    ):
        await asyncio.sleep(1.5)  # Allow Stripe to async-fill pref+city
        city_val = await _address_field_value(
            addr_frame,
            ["#billingAddress-localityInput", "input[name='locality']"],
        )
        pref_val = await _address_field_value(
            addr_frame,
            [
                "#billingAddress-administrativeAreaInput",
                "select[name='administrativeArea']",
            ],
        )
        if city_val and pref_val:
            dropdown_prefilled = True
            logger.info("phase3: postal suggestion selected; pref+city verified")
        else:
            logger.warning(
                "phase3: postal suggestion selected but pref/city remained empty; "
                "falling back to manual"
            )
    else:
        logger.info(
            "phase3: no Enter autocomplete or dropdown; "
            "postal input unavailable or address unknown"
        )

    # Step 5: If autocomplete did not fill pref+city → manual select + fill
    if not dropdown_prefilled:
        logger.info("phase3: falling back to manual address fill (pref+city)")

        # Prefecture: select from <select> (retry-safe: check selected option)
        pref_ok = False
        for sel in ["#billingAddress-administrativeAreaInput", "select[name='administrativeArea']"]:
            try:
                loc = addr_frame.locator(sel).first
                if await loc.is_visible(timeout=1500):
                    # Wait for options to finish loading (expect > 5 options for a prefecture dropdown)
                    try:
                        await loc.evaluate(
                            """el => new Promise((resolve) => {
                                let attempts = 0;
                                const check = () => {
                                    if (el.options.length > 5 || attempts >= 20) {
                                        resolve(el.options.length);
                                    } else {
                                        attempts++;
                                        setTimeout(check, 250);
                                    }
                                };
                                check();
                            })"""
                        )
                    except SAFE_EXCEPTIONS:
                        logger.warning("phase3: pref options wait timed out, proceeding anyway")

                    # Check if already selected
                    try:
                        selected = await loc.evaluate("el => el.options[el.selectedIndex]?.text")
                        if selected and selected == pref:
                            logger.info("phase3: address pref already selected, skip")
                            pref_ok = True
                            break
                    except SAFE_EXCEPTIONS:
                        pass

                    # Try Playwright select_option by label (e.g. "千葉県")
                    try:
                        await loc.select_option(label=pref)
                        pref_ok = True
                        logger.info("phase3: address pref selected via '%s'", sel)
                        break
                    except SAFE_EXCEPTIONS as exc:
                        logger.warning(
                            "phase3: select_option failed: %s; trying JS fallback",
                            exc,
                        )

                    # JS fallback: iterate options, find text that includes the prefecture name,
                    # set value, and dispatch a change event so Stripe's form picks it up
                    try:
                        matched = await loc.evaluate(
                            """(el, prefName) => {
                                for (let i = 0; i < el.options.length; i++) {
                                    if (el.options[i].text.includes(prefName) || el.options[i].value === prefName) {
                                        el.value = el.options[i].value;
                                        el.dispatchEvent(new Event('change', { bubbles: true }));
                                        return el.options[i].text;
                                    }
                                }
                                return null;
                            }""",
                            pref,
                        )
                        if matched:
                            pref_ok = True
                            logger.info("phase3: address pref selected via JS fallback '%s'", sel)
                            break
                        else:
                            logger.warning("phase3: JS fallback found no matching pref option")
                    except SAFE_EXCEPTIONS as exc:
                        logger.warning("phase3: JS fallback also failed: %s", exc)
            except SAFE_EXCEPTIONS:
                continue
        if not pref_ok:
            logger.warning("phase3: address pref select not found or no match, skipping")
        await asyncio.sleep(0.3)

        # City: fill text input (retry-safe)
        city_ok = False
        city_sel_matched = None
        for sel in ["#billingAddress-localityInput", "input[name='locality']"]:
            try:
                loc = addr_frame.locator(sel).first
                if await loc.is_visible(timeout=1500):
                    city_sel_matched = sel
                    city_ok = await _safe_address_fill(addr_frame, sel, city, "city")
                    break
            except SAFE_EXCEPTIONS:
                continue
        if not city_ok:
            logger.warning("phase3: address city input not found, skipping")
        await asyncio.sleep(0.3)

    # Step 6: Address line 1 — the postal autocomplete normally fills it, and
    # the autofilled canonical value is preserved instead of being rewritten
    # with the address-pool copy.  Only an empty line 1 is filled manually.
    addr1_sel = await _wait_for_address_field(
        addr_frame,
        ["#billingAddress-addressLine1Input", "input[name='addressLine1']"],
        timeout=5000,
        return_selector=True,
    )
    if addr1_sel is None:
        raise ElementNotFoundError(
            "Stripe address line1 did not appear or could not be filled"
        )
    current_line1 = await _address_field_value(
        addr_frame,
        ["#billingAddress-addressLine1Input", "input[name='addressLine1']"],
    )
    if current_line1.strip():
        logger.info("phase3: addr1 already auto-filled; preserving value")
    elif not await _safe_address_fill(addr_frame, addr1_sel, addr1, "addr1"):
        raise ElementNotFoundError(
            "Stripe address line1 did not appear or could not be filled"
        )
    await asyncio.sleep(0.3)

    # Step 7: Address line 2 (optional, retry-safe).  A stale value from a
    # previous address is cleared when the new address has no line 2.
    addr2_sel = await _wait_for_address_field(
        addr_frame,
        ["#billingAddress-addressLine2Input", "input[name='addressLine2']"],
        timeout=5000,
        return_selector=True,
    )
    if addr2_sel is None and addr2.strip():
        raise ElementNotFoundError(
            "Stripe address line2 did not appear or could not be filled"
        )
    if addr2_sel is not None:
        current_line2 = await _address_field_value(
            addr_frame,
            ["#billingAddress-addressLine2Input", "input[name='addressLine2']"],
        )
        if addr2.strip():
            if current_line2 != addr2.strip():
                if not await _safe_address_fill(addr_frame, addr2_sel, addr2, "addr2"):
                    raise ElementNotFoundError(
                        "Stripe address line2 did not appear or could not be filled"
                    )
        elif current_line2:
            logger.info("phase3: addr2 clearing stale value")
            await human_clear_in_frame(addr_frame, addr2_sel)

    if not await _verify_stripe_address_fields(addr_frame, zip_code, addr1, addr2):
        raise ElementNotFoundError("Stripe address fields failed post-fill verification")

    logger.info("phase3: address fields filled")


# ------------------------------------------------------------------
# CAPTCHA / human verification detection
# ------------------------------------------------------------------

# Selectors for CAPTCHA, puzzle, 3DS, and human-verification widgets.
# Stripe may inject various challenge UIs after clicking submit.
_CAPTCHA_INDICATORS = [
    # Generic CAPTCHA containers
    "div[style*='captcha']",
    "iframe[title*='captcha']",
    "iframe[title*='CAPTCHA']",
    "iframe[src*='recaptcha']",
    "iframe[src*='hcaptcha']",
    "div[role='presentation'] iframe[src*='challenges']",
    # hCaptcha / puzzle widgets
    "div[data-testid='challenge']",
    "div.h-captcha",
    "div.g-recaptcha",
    # Stripe 3DS / identity verification
    "text=人間であることを確認",
    "text=Verify you are human",
    "text=ロボット",
    "text=I'm not a robot",
    # Cloudflare Turnstile
    "iframe[src*='turnstile']",
    "iframe[src*='challenges.cloudflare']",
]


async def _detect_captcha(page) -> bool:
    """Check if any CAPTCHA / human-verification widget is visible.

    Returns True if at least one indicator is visible; False otherwise.
    Never raises — swallows all SAFE_EXCEPTIONS.
    """
    for indicator in _CAPTCHA_INDICATORS:
        try:
            loc = page.locator(indicator).first
            if await loc.is_visible(timeout=500):
                logger.info("phase3: CAPTCHA indicator detected — '%s'", indicator)
                return True
        except SAFE_EXCEPTIONS:
            continue
    return False


# ------------------------------------------------------------------
# Stripe confirm & rejection handling
# ------------------------------------------------------------------


async def _wait_for_stripe_result(page) -> str:
    """Post-submit: scan for CAPTCHA, then poll for completion/rejection.

    Consolidates the progress-screen check, CAPTCHA freeze loop, and
    completion polling into one reusable function.

    Returns:
        "success"  — subscription confirmed (iframes gone + navigated, or success text)
        "rejected" — payment auth failure / rejection pattern detected
        "unknown"  — no clear signal after polling
    """
    # ── Progress screen ("完了まで" / "ステップ"): wait and continue ──
    try:
        progress = page.locator("text=完了まで").first
        if await progress.is_visible(timeout=3000):
            logger.info("phase3: progress screen detected, waiting for completion...")
            await asyncio.sleep(3.0)
            try:
                await safe_click(page, [
                    "button:has-text('続行')",
                    "button:has-text('Continue')",
                ], timeout_ms=3000)
            except ElementNotFoundError:
                pass
            await asyncio.sleep(2.0)
    except SAFE_EXCEPTIONS:
        pass

    # ── CAPTCHA / human-verification: FREEZE MODE ──
    logger.info("phase3: scanning for CAPTCHA / human verification...")
    captcha_was_detected = False
    captcha_scan_deadline = time.monotonic() + 15.0
    while time.monotonic() < captcha_scan_deadline:
        if await _detect_captcha(page):
            captcha_was_detected = True
            logger.warning("phase3: CAPTCHA detected — entering FREEZE mode")
            logger.warning("phase3: 严禁任何页面操作，等待人类手动完成验证")

            # ── Freeze loop: block until human completes ──
            while True:
                reply = await human_pause(
                    "🚨 CAPTCHA / 人类验证已触发！请在浏览器中手动完成验证。\n"
                    "完成后按 Enter 继续 — 脚本将自动验证订阅是否完成，如未完成将自动重试。"
                )
                if reply == "S":
                    logger.warning("phase3: operator chose to SKIP this account during CAPTCHA")
                    raise SkipEmailException("skipped during CAPTCHA freeze")
                await asyncio.sleep(1.0)
                if not await _detect_captcha(page):
                    logger.info("phase3: CAPTCHA confirmed gone by human")
                    break
                logger.warning("phase3: CAPTCHA still visible — please complete it first")
            break  # exit CAPTCHA scan loop → proceed to completion polling

        # Break early if page navigated away from checkout (payment completed)
        try:
            current_url = page.url or ""
            if "/checkout/" not in current_url and current_url.startswith("http"):
                logger.info("phase3: page navigated from checkout (no CAPTCHA) — likely success")
                break
        except SAFE_EXCEPTIONS:
            pass

        await asyncio.sleep(1.0)

    if not captcha_was_detected:
        logger.info("phase3: no CAPTCHA detected during scan window")

    # ── Completion polling: wait for reliable success / rejection signals ──
    logger.info("phase3: waiting for Stripe submit completion signal...")
    completion_ok = False
    rejection_detected = False
    deadline = time.monotonic() + 40.0
    while time.monotonic() < deadline:
        any_signal = False
        # Signal A: Stripe modal iframes gone
        for sel in [
            "iframe[title*='セキュア']",
            "iframe[src*='stripe.com']",
            "div.__PrivateStripeElement",
        ]:
            try:
                if await page.locator(sel).first.count() > 0 and await page.locator(sel).first.is_visible():
                    any_signal = True
                    break
            except SAFE_EXCEPTIONS:
                continue
        if not any_signal:
            current_url = page.url or ""
            if "/checkout/" not in current_url:
                logger.info("phase3: Stripe iframes gone + navigated from checkout — subscription confirmed")
                completion_ok = True
                break
            else:
                logger.debug("phase3: iframes gone but still on checkout URL — Stripe still processing")

        # Signal B: success text
        for text_sel in [
            "text=ありがとう",
            "text=ご登録ありがとう",
            "text=Thank you",
            "text=登録が完了",
            "text=subscription",
        ]:
            try:
                if await page.locator(text_sel).first.is_visible(timeout=500):
                    logger.info("phase3: success text detected — subscription confirmed")
                    completion_ok = True
                    break
            except SAFE_EXCEPTIONS:
                continue
        if completion_ok:
            break

        # Signal C: main ChatGPT sidebar visible
        current_url_c = page.url or ""
        if "/checkout/" not in current_url_c:
            for logo_sel in [
                "span.header-wordmark",
                "[data-testid='accounts-profile-button']",
                "nav.sidebar",
            ]:
                try:
                    if await page.locator(logo_sel).first.is_visible(timeout=500):
                        logger.info("phase3: main ChatGPT page detected — subscription confirmed")
                        completion_ok = True
                        break
                except SAFE_EXCEPTIONS:
                    continue
            if completion_ok:
                break

        # Signal R: payment rejection / authentication failure
        rej_info = await _extract_rejection_info(page)
        if rej_info is not None:
            logger.warning("phase3: rejection detected during wait loop [%s] — '%s'", rej_info.kind, rej_info.text)
            rejection_detected = True
            break
        if rejection_detected:
            break

        await asyncio.sleep(0.5)

    if completion_ok:
        return "success"
    if rejection_detected:
        return "rejected"
    return "unknown"


async def _click_submit(page) -> None:
    """Locate the Stripe submit button, wait until enabled, and click it."""
    btn_selectors = [
        "button[aria-label='サブスクリプションを登録する']",
        "button[type='submit']",
        "button:has-text('サブスクリプションを登録する')",
        "button.btn-primary",
    ]
    btn = None
    for sel in btn_selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=3000):
                btn = loc
                break
        except SAFE_EXCEPTIONS:
            continue

    if btn is None:
        raise ElementNotFoundError("stripe submit button not found on main page")

    # Wait for disabled attribute to be removed (Stripe disables while processing)
    try:
        for _ in range(10):  # up to 5s
            if not await btn.is_disabled():
                break
            await asyncio.sleep(0.5)
        else:
            raise ElementNotFoundError(
                "stripe submit button stayed disabled; payment was not submitted"
            )
    except SAFE_EXCEPTIONS:
        raise

    await btn.click()
    await human_delay((4.0, 5.0))



@dataclass(frozen=True)
class StripeRejectionInfo:
    kind: str  # generic_declined | auth_failed | try_another_card | declined_unspecified
    label: str  # 卡片被拒绝 | 支付认证失败 | 请更换卡片 | 卡片被拒绝（未分类）
    text: str  # 页面实际提取文本


_last_rejection_info: Optional[StripeRejectionInfo] = None


def get_last_rejection_info() -> Optional[StripeRejectionInfo]:
    global _last_rejection_info
    return _last_rejection_info


def set_last_rejection_info(info: Optional[StripeRejectionInfo]) -> None:
    global _last_rejection_info
    _last_rejection_info = info


def classify_rejection_text(text: str) -> StripeRejectionInfo:
    text_clean = (text or "").strip()
    if "カードが拒否されました" in text_clean or "Card declined" in text_clean or "card was declined" in text_clean.lower() or "payment method was declined" in text_clean.lower():
        return StripeRejectionInfo("generic_declined", "卡片被拒绝", text_clean or "カードが拒否されました。")
    if "認証できません" in text_clean or "could not be verified" in text_clean.lower():
        return StripeRejectionInfo("auth_failed", "支付认证失败", text_clean or "決済手段を認証できませんでした")
    if "別のカード" in text_clean or "別の決済手段" in text_clean or "支払いが拒否されました" in text_clean or "お支払いが拒否されました" in text_clean:
        return StripeRejectionInfo("try_another_card", "请更换卡片", text_clean or "支払いが拒否されました。別のカードをお試しください。")
    return StripeRejectionInfo("declined_unspecified", "卡片被拒绝", text_clean or "カードが拒否されました。")


async def _extract_rejection_info(page) -> Optional[StripeRejectionInfo]:
    """Scan visible rejection selectors and extract the actual banner text."""
    for pat in config.STRIPE_REJECTION_PATTERNS:
        try:
            loc = page.locator(pat).first
            if await loc.is_visible(timeout=300):
                txt = ""
                try:
                    txt = (await loc.inner_text(timeout=500) or "").strip()
                except Exception:
                    pass
                if not txt:
                    txt = pat.replace("text=", "").strip()
                info = classify_rejection_text(txt)
                set_last_rejection_info(info)
                return info
        except SAFE_EXCEPTIONS:
            continue
    return None


async def _scan_early_rejection(page) -> bool:
    """True when a Stripe rejection pattern is visible right after submit."""
    info = await _extract_rejection_info(page)
    if info is not None:
        logger.warning("phase3: Stripe card REJECTED [%s] — '%s'", info.kind, info.text)
        return True
    return False


async def _detect_result(page) -> str:
    """Classify the payment outcome after a submit happened.

    Returns:
        "success"  — payment confirmed
        "rejected" — card declined
        "unknown"  — unclear result after human_pause
    """
    early_rejection = await _scan_early_rejection(page)

    if early_rejection:
        return "rejected"

    # ── Post-submit: CAPTCHA scan + completion polling ──
    result = await _wait_for_stripe_result(page)

    if result == "success":
        logger.info("phase3: Stripe submit confirmed")
        return "success"

    if result == "rejected":
        logger.warning("phase3: rejection detected during wait; no automatic retry")
        return "rejected"

    # result == "unknown" → ask human, then re-detect page state
    logger.warning("phase3: Stripe submit completion unclear after polling — asking human")
    await human_pause("Stripe 支付状态不明确，请确认是否已完成。完成或关闭 Stripe 页面后按 Enter 继续。")

    result = await _detect_page_state_after_manual(page)
    logger.info("phase3: post-manual page state → %s", result)

    if result == "rejected":
        return "rejected"
    elif result == "success":
        return "success"
    else:
        return "unknown"


async def phase3_stripe_confirm(page, card_no: str = "", card_expiry: str = "", card_cvc: str = "") -> str:
    """Submit the Stripe subscription (automatic mode) and classify the result.

    This function never submits payment a second time.
    """
    del card_no, card_expiry, card_cvc
    await _click_submit(page)
    return await _detect_result(page)


async def phase3_stripe_confirm_manual(page, timeout: float = 300.0) -> str:
    """Wait for the operator to click the Stripe submit button, then classify.

    Used after a card/address switch: the form is refilled but the payment
    button is deliberately NOT clicked by the automation.  This function
    polls until the submit button disappears (operator clicked it) or a
    result signal appears, then runs the shared result classification.

    Raises TimeoutError when the operator does not submit in time, so the
    caller can return to the interactive intervention menu.
    """
    deadline = time.monotonic() + timeout
    while True:
        if await _scan_early_rejection(page):
            return "rejected"
        btn = await _find_submit_btn(page)
        if btn is None:
            # The button is gone: either the operator clicked it, or the
            # page moved on — classify the current state.
            return await _detect_result(page)
        if time.monotonic() >= deadline:
            raise TimeoutError("operator did not submit the Stripe payment in time")
        await asyncio.sleep(min(2.0, max(0.1, deadline - time.monotonic())))


# ── Post-manual page state detection ────────────────────────────


async def _detect_page_state_after_manual(page) -> str:
    """Detect current page state after human interaction with the Stripe modal.

    Called AFTER user presses Enter on human_pause.  Determines whether
    the subscription succeeded, was rejected, or remains unclear.

    Returns:
        "success"  — subscription completed (thank-you / main page / iframes gone)
        "rejected" — payment auth failure error visible on page
        "unknown"  — cannot determine (still on checkout with no clear signal)
    """
    await asyncio.sleep(0.5)

    # Check 1: rejection / auth failure
    info = await _extract_rejection_info(page)
    if info is not None:
        return "rejected"

    # Check 2: success text
    for ts in config.STRIPE_SUCCESS_TEXTS:
        try:
            if await page.locator(ts).first.is_visible(timeout=500):
                return "success"
        except SAFE_EXCEPTIONS:
            continue

    # Check 3: Stripe iframes gone → modal closed → success
    try:
        stripe_visible = False
        for sel in [
            "iframe[title*='セキュア']",
            "div.__PrivateStripeElement",
        ]:
            try:
                if await page.locator(sel).first.count() > 0 and await page.locator(sel).first.is_visible():
                    stripe_visible = True
                    break
            except SAFE_EXCEPTIONS:
                continue
        if not stripe_visible:
            logger.info("phase3: post-manual — Stripe iframes disappeared")
            return "success"
    except SAFE_EXCEPTIONS:
        pass

    # Check 4: main ChatGPT page visible
    for logo_sel in [
        "span.header-wordmark",
        "[data-testid='accounts-profile-button']",
    ]:
        try:
            if await page.locator(logo_sel).first.is_visible(timeout=500):
                logger.info("phase3: post-manual — main ChatGPT page detected")
                return "success"
        except SAFE_EXCEPTIONS:
            continue

    logger.info("phase3: post-manual — still on checkout, no clear signal")
    return "unknown"


async def _find_submit_button(page):
    return await _find_submit_btn(page)


async def _find_submit_btn(page):
    """Find Stripe submit button — returns a visible Locator, or None if none is visible."""
    for sel in [
        "button[aria-label='サブスクリプションを登録する']",
        "button[type='submit']",
        "button:has-text('サブスクリプションを登録する')",
        "button:has-text('Subscribe')",
    ]:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=1500):
                return btn
        except Exception:
            continue
    return None


# ------------------------------------------------------------------
# Address data helper
# ------------------------------------------------------------------


def _load_address_rows() -> list:
    """Load address rows from CSV once and cache at module level."""
    import csv as _csv
    import os as _os
    addr_file = _os.path.join("input", "address_jp.csv")
    path = addr_file if _os.path.exists(addr_file) else _os.path.join(_os.path.dirname(__file__), "..", "..", addr_file)
    if not _os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8-sig") as f:
        return list(_csv.DictReader(f))


_ADDRESS_CACHE: list | None = None


def list_switch_addresses() -> list:
    """Standard-format billing addresses usable for manual switching.

    Government-format rows (no postal code) are skipped because Stripe Japan
    requires a postal code; the returned dicts use the
    ``zip_code/pref/city/addr1/addr2`` contract of :func:`_random_address`.
    """
    addresses = []
    for row in _load_address_rows():
        if "都道府県名" in row:
            continue
        if not row.get("zip_code") or not row.get("pref"):
            continue
        addresses.append(
            {
                "zip_code": row.get("zip_code", ""),
                "pref": row.get("pref", ""),
                "city": row.get("city", ""),
                "addr1": row.get("addr1", ""),
                "addr2": row.get("addr2", ""),
            }
        )
    return addresses


def _address_by_index(index: str) -> Optional[dict]:
    """Pick one billing address from the pool by 0-based index string."""
    try:
        idx = int(index)
    except (TypeError, ValueError):
        return None
    addresses = list_switch_addresses()
    if 0 <= idx < len(addresses):
        return addresses[idx]
    return None


def _random_address() -> dict:
    """Load a random address from input/address_jp.csv (cached after first read)."""
    global _ADDRESS_CACHE
    default = {"zip_code": "100-0001", "pref": "東京都", "city": "千代田区", "addr1": "千代田1-1-1", "addr2": ""}
    if _ADDRESS_CACHE is None:
        _ADDRESS_CACHE = _load_address_rows()
    if not _ADDRESS_CACHE:
        return default
    row = random.choice(_ADDRESS_CACHE)
    return {"zip_code": row.get("zip_code", ""), "pref": row.get("pref", ""),
            "city": row.get("city", ""), "addr1": row.get("addr1", ""), "addr2": row.get("addr2", "")}


# ------------------------------------------------------------------
# Utility: expiry normalization (used by stripe card fill)
# ------------------------------------------------------------------


def _normalize_expiry(raw: str) -> str:
    """Normalize card expiry to 4-digit format Stripe expects.

    Handles various input formats:
      "06/2031"  → "0631"  (6 digits with full year)
      "06/31"    → "0631"  (4 digits as-is)
      "06-2031"  → "0631"  (dash separator)
      "062031"   → "0631"  (raw 6 digits)
      "0631"     → "0631"  (already correct)
      "06/2025"  → "0625"  (first 2 + last 2)
    """
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 6:
        # "MMYYYY" → "MMYY"
        digits = digits[:2] + digits[4:]
    return digits[:4]
