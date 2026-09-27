"""
Phase 4: Unsubscribe & final status confirmation.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import config
from gpt_automation.utils.csv_writer import evidence_path
from gpt_automation.utils.exceptions import SAFE_EXCEPTIONS
from gpt_automation.utils.timing import human_delay
from gpt_automation.phases.helpers import safe_click, safe_screenshot

logger = logging.getLogger(__name__)


async def _handle_qualtrics_survey(page) -> None:
    """Handle Qualtrics multi-page survey that may appear after cancellation.

    ChatGPT sometimes redirects to a Qualtrics survey (4-8 pages) after
    cancelling a subscription. Auto-click through all pages by selecting
    default answers and clicking 'Next' until the survey completes.
    """
    for survey_page in range(10):  # max 10 pages
        await asyncio.sleep(3.0)

        # Check if we're on a survey page
        current_url = page.url or ""
        is_qualtrics = "qualtrics" in current_url.lower()

        if not is_qualtrics:
            # Only check body text for survey keywords if we've been redirected
            # AWAY from chatgpt.com (chatgpt.com billing page may contain words
            # like "フィードバック" which are NOT a post-cancel survey)
            if "chatgpt.com" in current_url:
                if survey_page == 0:
                    logger.info("phase4: no survey redirect detected, proceeding")
                break

            # Check for survey-like content on current page
            try:
                body_text = (await page.text_content("body")) or ""
                is_survey_content = any(kw in body_text for kw in [
                    "アンケート", "survey", "Qualtrics",
                    "ご意見をお聞かせ", "フィードバック",
                    "ChatGPTをキャンセルした理由",
                ])
                if not is_survey_content:
                    if survey_page == 0:
                        logger.info("phase4: no survey detected, proceeding")
                    break
            except SAFE_EXCEPTIONS:
                break

        logger.info("phase4: survey page %d detected", survey_page + 1)

        # Select default radio/checkbox answers if any are unselected
        try:
            # Select first available radio button per group (for unanswered questions)
            await page.evaluate("""
                () => {
                    const groups = {};
                    document.querySelectorAll('input[type="radio"]').forEach(r => {
                        if (!r.checked) {
                            if (!groups[r.name]) groups[r.name] = r;
                        }
                    });
                    Object.values(groups).forEach(r => {
                        // Pick the first option in each group
                        const all = document.querySelectorAll(`input[type="radio"][name="${r.name}"]`);
                        if (all.length > 0) all[0].click();
                    });
                }
            """)
        except SAFE_EXCEPTIONS:
            pass

        await asyncio.sleep(0.5)

        # Click next page button
        next_clicked = False
        for next_sel in [
            "button:has-text('次のページ')",
            "button:has-text('Next')",
            "button:has-text('次へ')",
            "input[value='次のページ']",
            "input[value='Next']",
            "button[type='submit']",
        ]:
            try:
                btn = page.locator(next_sel).first
                if await btn.is_visible(timeout=2000):
                    await btn.click()
                    next_clicked = True
                    logger.info("phase4: survey next clicked via '%s'", next_sel)
                    break
            except SAFE_EXCEPTIONS:
                continue

        if not next_clicked:
            # Check for final submit button
            for submit_sel in [
                "button:has-text('送信')",
                "button:has-text('Submit')",
                "button:has-text('完了')",
                "button:has-text('Done')",
                "button:has-text('Finish')",
            ]:
                try:
                    btn = page.locator(submit_sel).first
                    if await btn.is_visible(timeout=2000):
                        await btn.click()
                        logger.info("phase4: survey submit clicked via '%s'", submit_sel)
                        next_clicked = True
                        break
                except SAFE_EXCEPTIONS:
                    continue

            if not next_clicked:
                logger.info("phase4: survey — no next/submit button found, may have completed")
                break

    # After survey, navigate back to chatgpt.com if not already there
    await asyncio.sleep(2.0)
    if "chatgpt.com" not in (page.url or ""):
        try:
            await page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(2.0)
            logger.info("phase4: navigated back to chatgpt.com after survey")
        except SAFE_EXCEPTIONS:
            logger.warning("phase4: failed to navigate back after survey")


async def phase4_cancel(page) -> Any:
    """Cancel the ChatGPT Plus subscription via the Billing settings page.

    New ChatGPT UI (2026-06): The Billing page (#settings/Billing) has a
    "プランのキャンセル" section with a "キャンセルする" button directly
    on the page — no Stripe popup needed.

    Fallback: if the button is not found (older UI), try the legacy approach
    of opening a Stripe popup via "管理する" / "Manage subscription".

    Returns:
        The Page object for status confirmation (stripe_page or the main
        page), or ``None`` if the cancel could not be initiated.
    """
    # Step 1: Navigate to Billing settings
    await page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=20000)
    await asyncio.sleep(3.0)

    # Close pricing modal if it appeared
    if "#pricing" in (page.url or ""):
        await page.keyboard.press("Escape")
        await asyncio.sleep(1.0)

    # Navigate to Billing settings via hash
    await page.evaluate("window.location.hash = '#settings/Billing'")
    await asyncio.sleep(3.0)

    # Wait for billing panel to render
    billing_panel_found = False
    for text_selector in ["請求", "サブスクリプション", "Billing", "Manage subscription"]:
        panel = page.locator(f"text={text_selector}").first
        try:
            await panel.wait_for(state="visible", timeout=5000)
            billing_panel_found = True
            logger.info("phase4: billing panel appeared (matched '%s')", text_selector)
            break
        except SAFE_EXCEPTIONS:
            continue

    if not billing_panel_found:
        logger.warning("phase4: billing panel did not appear")
        return None

    # ── New UI path: click "キャンセルする" directly on Billing page ──
    # DOM structure (verified via probe 2026-06-30):
    #   <section>
    #     <h3>プランのキャンセル</h3>
    #     <p>キャンセルしても、請求期間の終了までは...</p>
    #     <button class="btn-danger-outline rounded-full">キャンセルする</button>
    #   </section>
    cancel_clicked = False
    for cancel_sel in [
        "button:has-text('キャンセルする')",
        "button:has-text('Cancel plan')",
        "button:has-text('Cancel subscription')",
        "button.btn-danger-outline",
    ]:
        try:
            cancel_btn = page.locator(cancel_sel).first
            if await cancel_btn.is_visible(timeout=5000):
                await cancel_btn.click()
                cancel_clicked = True
                logger.info("phase4: cancel button clicked via '%s'", cancel_sel)
                break
        except SAFE_EXCEPTIONS:
            continue

    if cancel_clicked:
        # Wait for dialog to render (Go downsell or final confirm)
        # Fixed 4s proven reliable in debug script (2026-06-30)
        await asyncio.sleep(4.0)

        # ── Step A: Handle Go downsell dialog (may or may not appear) ──
        # ChatGPT sometimes shows a retention offer: "¥1,400 で ChatGPT Go
        # に切り替えますか？" with buttons "利用しない" / "ChatGPT Go を試す".
        # We must dismiss it before the final cancellation confirmation appears.
        go_downsell_dismissed = False
        try:
            body_text = (await page.text_content("body")) or ""
            is_go_dialog = ("ChatGPT Go" in body_text and "利用しない" in body_text)
        except SAFE_EXCEPTIONS:
            is_go_dialog = False

        if is_go_dialog:
            logger.info("phase4: Go downsell dialog detected — dismissing")
            for dismiss_sel in [
                "button:has-text('利用しない')",
                "button:has-text('No thanks')",
                "button:has-text('Stay with Plus')",
            ]:
                try:
                    dismiss_btn = page.locator(dismiss_sel).first
                    if await dismiss_btn.is_visible(timeout=3000):
                        await dismiss_btn.click()
                        go_downsell_dismissed = True
                        logger.info("phase4: Go downsell dismissed via '%s'", dismiss_sel)
                        break
                except SAFE_EXCEPTIONS:
                    continue

            if go_downsell_dismissed:
                # Fixed 3s proven reliable in debug script (2026-06-30)
                await asyncio.sleep(3.0)

        # ── Step B: Final cancellation confirmation ──
        # Dialog: "Plus へのアクセスを終了しますか？"
        # Red button: "サブスクリプションをキャンセルする"
        # White button: "戻る"
        final_confirm_selectors = [
            "button:has-text('サブスクリプションをキャンセルする')",
            "button:has-text('Cancel subscription')",
            "button:has-text('Cancel plan')",
            "button:has-text('Confirm cancellation')",
            "button:has-text('Confirm')",
            "button.btn-danger",
        ]

        final_clicked = False
        for final_sel in final_confirm_selectors:
            try:
                final_btn = page.locator(final_sel).first
                if await final_btn.is_visible(timeout=5000):
                    await final_btn.click()
                    final_clicked = True
                    logger.info("phase4: final cancel confirmation clicked via '%s'", final_sel)
                    break
            except SAFE_EXCEPTIONS:
                continue

        if not final_clicked:
            logger.warning(
                "phase4: final confirmation dialog was not found; "
                "returning for status verification without another click"
            )

        if final_clicked:
            await asyncio.sleep(3.0)

        # ── Step C: Handle Qualtrics survey (may appear after cancel) ──
        await _handle_qualtrics_survey(page)

        # ── Step D: Verify cancellation success ──
        await asyncio.sleep(2.0)
        for success_text in ["キャンセル済み", "解約済み", "cancelled", "canceled",
                             "キャンセルされました", "キャンセルされます",
                             "will not renew", "will be cancelled", "will be canceled"]:
            try:
                if await page.locator(f"text={success_text}").first.is_visible(timeout=3000):
                    logger.info("phase4: cancellation confirmed on page — '%s'", success_text)
                    return page
            except SAFE_EXCEPTIONS:
                continue

        # Even if we can't find success text, return the page for status check
        logger.info("phase4: cancel flow completed (final_clicked=%s)", final_clicked)
        return page

    # ── Legacy fallback: Stripe popup via "管理する" ──
    logger.info("phase4: 'キャンセルする' not found, trying legacy Stripe popup approach")
    stripe_page = None
    try:
        async with page.expect_popup(timeout=8000) as popup_info:
            await safe_click(page, [config.SELECTORS["manage_sub_btn"]], timeout_ms=5000)
        stripe_page = await popup_info.value
        logger.info("phase4: manage subscription opened as popup (legacy)")
    except SAFE_EXCEPTIONS:
        logger.info("phase4: no popup detected, checking for in-page navigation...")
        try:
            await page.wait_for_url("**/billing.stripe.com/**", timeout=5000)
            stripe_page = page
            logger.info("phase4: manage subscription navigated main page to Stripe (legacy)")
        except SAFE_EXCEPTIONS:
            logger.error("phase4: all cancel approaches failed")
            return None

    if stripe_page is None:
        return None

    # Legacy Stripe cancel flow (3 confirmations)
    await stripe_page.wait_for_load_state("domcontentloaded")
    await human_delay((1.0, 2.0))
    await safe_click(stripe_page, [config.SELECTORS["cancel_sub_link"]], timeout_ms=5000)
    await human_delay((0.5, 1.0))
    await safe_click(stripe_page, [config.SELECTORS["dont_use_go_btn"]], timeout_ms=5000)
    await human_delay((0.5, 1.0))
    await safe_click(stripe_page, [config.SELECTORS["final_cancel_btn"]], timeout_ms=5000)
    await human_delay((2.0, 2.0))
    return stripe_page


async def _load_billing_state(page, email: str) -> tuple[str, bool] | None:
    """Load billing text and the visible cancellation-button state."""
    # Navigate to Billing settings
    try:
        await page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=15000)
        await asyncio.sleep(3.0)
        await page.evaluate("window.location.hash = '#settings/Billing'")
        await asyncio.sleep(3.0)
    except SAFE_EXCEPTIONS as exc:
        logger.warning("phase4: status navigation failed: %s", exc)
        return None

    # Read billing panel text
    try:
        billing_text = await page.evaluate("""
            () => {
                const panel = document.querySelector('[id*="content-Billing"]');
                return panel ? panel.textContent : '';
            }
        """)
    except SAFE_EXCEPTIONS:
        billing_text = ""

    # Fallback: read body text if billing panel not found
    if not billing_text:
        try:
            billing_text = (await page.text_content("body")) or ""
        except SAFE_EXCEPTIONS:
            billing_text = ""

    cancel_btn_gone = False
    try:
        cancel_btn_gone = await page.evaluate("""
            () => {
                const btns = document.querySelectorAll('button');
                for (const btn of btns) {
                    if (btn.textContent.trim() === 'キャンセルする' &&
                        btn.offsetParent !== null) return false;
                }
                return true;
            }
        """)
    except SAFE_EXCEPTIONS:
        pass

    await safe_screenshot(page, evidence_path(email, "final"))
    return str(billing_text or ""), bool(cancel_btn_gone)


async def phase4_subscription_active_confirm(page, email: str) -> bool:
    """Verify an explicitly active Plus plan after payment submission."""
    from gpt_automation.workflow.subscription_verification import (
        classify_subscription_status,
    )

    billing_state = await _load_billing_state(page, email)
    if billing_state is None:
        return False
    billing_text, _cancel_btn_gone = billing_state
    outcome = classify_subscription_status(billing_text)
    if outcome.verified:
        logger.info("phase4: active subscription status verified")
        return True
    logger.warning("phase4: active subscription status requires operator confirmation")
    return False


async def phase4_status_confirm(page, stripe_page, email: str) -> bool:
    """Verify cancellation status using explicit billing-page evidence."""
    from gpt_automation.workflow.subscription_verification import (
        classify_cancellation_status,
    )

    billing_state = await _load_billing_state(page, email)
    if billing_state is None:
        return False
    billing_text, cancel_btn_gone = billing_state

    outcome = classify_cancellation_status(
        billing_text,
        cancel_button_visible=not cancel_btn_gone,
    )
    if outcome.verified:
        logger.info(
            "phase4: cancellation status verified (button_gone=%s)",
            cancel_btn_gone,
        )
        return True
    logger.warning(
        "phase4: cancellation status requires operator confirmation (button_gone=%s)",
        cancel_btn_gone,
    )
    return False
