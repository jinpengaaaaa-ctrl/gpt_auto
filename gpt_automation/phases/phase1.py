"""
Phase 1: Registration — navigate, email verify, form fill, ready confirm, survey.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import config
from gpt_automation.utils.exceptions import SAFE_EXCEPTIONS
from gpt_automation.utils.timing import (
    human_delay, tiny_pause, DELAY_MEDIUM,
)
from gpt_automation.phases.helpers import safe_click, human_pause
from gpt_automation.phases.human_input import (
    human_clear,
    human_submit,
    human_type_and_verify,
    normalize_email,
)
from gpt_automation.utils.exceptions import ElementNotFoundError

logger = logging.getLogger(__name__)


CHATGPT_AUTH_LOGIN_URL = "https://chatgpt.com/auth/login"
SIGNUP_PASSWORD_LINK_SELECTOR = "a[href='/create-account/password']"
SIGNUP_PASSWORD_INPUT_SELECTOR = "input[autocomplete='new-password']"
SIGNUP_PASSWORD_ROUTE = "/create-account/password"

_AUTH_OVERLAY_CLOSE_SELECTORS = (
    "button[aria-label='Close']",
    "button[aria-label='閉じる']",
    "button[aria-label*='close' i]",
    "[aria-label='Close']",
    "[data-testid*='close']",
)


async def _dismiss_auth_overlay(page) -> None:
    """Best-effort close of the Google login hint popover on the auth page.

    Firefox private sessions start with no cookies, so ChatGPT always shows
    the Google login hint overlay on the first auth visit; the overlay sits
    above the 続行 button and blocks Playwright's pointer click.  Esc and the
    overlay's own close button are tried; failures are tolerated.
    """
    try:
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.4)
    except SAFE_EXCEPTIONS:
        pass
    for selector in _AUTH_OVERLAY_CLOSE_SELECTORS:
        try:
            locator = page.locator(selector).first
            if await locator.is_visible(timeout=1000):
                await locator.click(timeout=3000)
                await asyncio.sleep(0.4)
                return
        except SAFE_EXCEPTIONS:
            continue


async def password_continuation_available(
    page,
    *,
    timeout_s: float = 8.0,
) -> bool:
    """Probe the "use password" continuation link without clicking.

    The email-verification page may or may not offer a password
    continuation link.  This read-only probe decides the signup route:
    visible link means the account will get a password; a missing link
    means direct email verification with a passwordless account.
    """
    deadline = time.monotonic() + timeout_s
    link = page.locator(SIGNUP_PASSWORD_LINK_SELECTOR).first
    while time.monotonic() < deadline:
        try:
            if await link.is_visible(timeout=500):
                return True
        except SAFE_EXCEPTIONS:
            pass
        await asyncio.sleep(0.2)
    return False


async def phase1_choose_password_signup(page) -> None:
    """Choose the locale-independent password continuation for a new account.

    The email-verification page exposes this stable route before any localized
    text is rendered.  Waiting for the exact link keeps the action responsive
    when it appears quickly while allowing slower auth pages enough time to
    settle.
    """
    if SIGNUP_PASSWORD_ROUTE not in (page.url or ""):
        link = page.locator(SIGNUP_PASSWORD_LINK_SELECTOR).first
        await link.wait_for(state="visible", timeout=45_000)
        await link.click(timeout=15_000)

    await page.wait_for_url(
        "**/create-account/password**",
        timeout=30_000,
        wait_until="domcontentloaded",
    )


async def phase1_create_password(
    page,
    chatgpt_password: str,
    *,
    submit_callback: Optional[Callable[[], None]] = None,
) -> None:
    """Create the ChatGPT password on the new-account password route.

    This page has one ``autocomplete='new-password'`` field.  The callback is
    deliberately invoked immediately before the non-idempotent submit click so
    the workflow can journal the action before the browser changes state.
    """
    if SIGNUP_PASSWORD_ROUTE not in (page.url or ""):
        raise ElementNotFoundError("new-account password route is not active")
    if not chatgpt_password or len(chatgpt_password) < 12:
        raise ElementNotFoundError("new-account password is missing or too short")

    password_input = page.locator(SIGNUP_PASSWORD_INPUT_SELECTOR).first
    await password_input.wait_for(state="visible", timeout=45_000)
    await human_type_and_verify(
        page,
        password_input,
        chatgpt_password,
        field_name="signup_password",
    )

    await human_delay((0.3, 0.6))
    if submit_callback is not None:
        submit_callback()
    await human_submit(
        page,
        ["button[type='submit']"],
        timeout_ms=30000,
    )
    logger.info("phase1: submitted new-account password")
    await human_delay((2.5, 4.0))


async def phase1_open_login(page) -> None:
    """Open the canonical ChatGPT auth entry point.

    ChatGPT decides whether the existing browser session is valid: a logged-in
    profile is redirected to the main page, while a logged-out profile remains
    on the auth page and exposes the email field.
    """
    await human_delay(DELAY_MEDIUM)
    await page.goto(CHATGPT_AUTH_LOGIN_URL, wait_until="domcontentloaded")


async def phase1_login_password(page, chatgpt_password: str) -> None:
    """已有账户登录页：逐键输入 ChatGPT 密码并提交（普通交互）.

    Only the encrypted ChatGPT password may be used here; the email password
    must never reach this page.
    """
    pwd_selector = config.SELECTORS.get("reg_password_input", "input[name='password']")
    password_input = page.locator(pwd_selector).first
    await password_input.wait_for(state="visible", timeout=30_000)
    await human_type_and_verify(
        page,
        password_input,
        chatgpt_password,
        field_name="login_password",
    )
    await human_delay((0.3, 0.6))
    try:
        await human_submit(
            page,
            [
                "button:text-is('続行')",
                "button:text-is('Continue')",
                "button[type='submit']",
            ],
            timeout_ms=15000,
        )
    except ElementNotFoundError:
        await password_input.press("Enter")
        await asyncio.sleep(2.0)
        logger.info("phase1: 登录密码表单已通过 Enter 提交")


async def phase1_navigate(page, email: str) -> None:
    if "/auth/login" not in (page.url or ""):
        await phase1_open_login(page)
    else:
        await human_delay(DELAY_MEDIUM)
    await page.wait_for_selector(config.SELECTORS["reg_email_input"], timeout=30000)
    # Close the Google hint overlay BEFORE filling so its close button can
    # never interact with the email field.
    await _dismiss_auth_overlay(page)
    email_input = page.locator(config.SELECTORS["reg_email_input"]).first
    # 共享逐键输入：等待可编辑 → 逐键输入 → human_delay → 读回校验，
    # 不一致时以同样方式重输一次。绝不使用 fill() 或 JS 写入。
    await human_type_and_verify(
        page,
        email_input,
        email,
        field_name="email",
        normalize=normalize_email,
    )
    try:
        await _submit_email_form(page, email_input)
    except ElementNotFoundError:
        logger.warning("普通点击与安全点击均失败，尝试 Enter 键提交表单...")
        try:
            await email_input.press("Enter")
            await asyncio.sleep(2.0)
            logger.info("Enter 键已发送，等待表单提交...")
        except SAFE_EXCEPTIONS:
            raise ElementNotFoundError("邮箱表单提交失败（普通点击与 Enter 均失败）") from None


async def _submit_email_form(page, email_input) -> None:
    """以普通交互提交邮箱表单.

    优先使用表单内 submit 按钮（排除 Google/Apple 社交按钮），普通点击失败时
    重新定位再试一次；整个过程不使用 force、dispatch 或 JS click。
    """
    form_selector = (
        config.SELECTORS["reg_email_input"]
        + " >> xpath=ancestor::form >> button[type='submit']"
    )
    try:
        await human_submit(
            page,
            [
                form_selector,
                "button:text-is('続行')",
                "button:text-is('Continue')",
                "button[type='submit']",
            ],
            timeout_ms=15000,
        )
    except SAFE_EXCEPTIONS:
        # safe_click 兼容既有页面变体（含按钮文本包裹 span 的情况），
        # 但仍为普通鼠标点击。
        await safe_click(
            page,
            [
                "button:text-is('続行')",
                "button:has-text('続行'):not(:has-text('Google')):not(:has-text('Apple')):not(:has-text('で続行'))",
                "button:text-is('Continue')",
                "button:has-text('Continue'):not(:has-text('with Google')):not(:has-text('with Apple'))",
                "button[type='submit']",
            ],
            timeout_ms=15000,
        )


async def phase1_email_verify_code(
    page,
    code_reader,
    *,
    submit_callback: Optional[Callable[[], None]] = None,
) -> None:
    """Handle the signup email verification page via the unified module."""
    from gpt_automation.phases.email_verification import (
        DEFAULT_SUBMIT_SELECTORS,
        EmailCodeError,
        handle_email_code_with_resend,
    )

    logger.info("phase1: waiting for email verification code page...")
    try:
        cfg = config.EMAIL_CODE_CONFIG
        await handle_email_code_with_resend(
            page,
            code_reader,
            step="signup",
            submit_selectors=list(DEFAULT_SUBMIT_SELECTORS),
            submit_callback=submit_callback,
            round_timeouts=(
                cfg["round1_timeout"],
                cfg["round2_timeout"],
                cfg["round3_timeout"],
            ),
            max_resend=cfg["max_resend"] if cfg["enable_resend"] else 0,
            poll_interval=cfg["poll_interval"],
        )
        await human_delay((1.0, 1.5))
        logger.info("phase1: email verification code auto-filled")
    except EmailCodeError as exc:
        logger.error("phase1: email verification handling failed: %s", exc.code)
        raise

async def phase1_captcha_human(page) -> None:
    await human_pause("请在浏览器窗口中完成验证（CAPTCHA / 邮件链接 / 一次性验证码）。")


_BIRTHDATE_SINGLE_SELECTORS = (
    "input[name='birthday']",
    "input[name='birthdate']",
    "input[placeholder*='生年月日']",
    "input[aria-label*='生年月日']",
)

_AGE_INPUT_SELECTORS = (
    config.SELECTORS.get("age_input", "input[name='age']"),
    "input[placeholder='年齢']",
    "input[aria-label*='年齢']",
)

# 提交后验收：这些注册表单字段仍可见 = 表单未离开。
_FORM_PRESENT_SELECTORS = (
    "input[name='name']",
    "input[placeholder*='名前']",
    "input[placeholder*='Name' i]",
    "input[name='age']",
    "input[placeholder*='年齢']",
    "input[aria-label*='年齢']",
    "input[name='birthday']",
    "input[name='birthdate']",
    "input[placeholder*='生年月日']",
    "[role='spinbutton'][data-type='year']",
)

# 提交后验收：出现这些校验失败文案 = 表单拒绝了填写值。
_FORM_VALIDATION_ERROR_SELECTORS = (
    "text=続けるには有効な年齢を入力してください",
    "text=有効な年齢",
    "text=valid age",
    "text=Please enter a valid age",
    "text=有効な生年月日",
    "text=正しい生年月日",
)

# 表单离开轮询超时（秒）：提交后等表单消失或校验错误出现。
_FORM_DEPART_TIMEOUT = 15.0

_NATIVE_SETTER_JS = """(el, value) => {
    const proto = HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
    setter.call(el, value);
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
    return el.value;
}"""

_ACTIVE_ELEMENT_JS = """() => {
    const el = document.activeElement;
    if (!el) return null;
    return {name: el.name || '', placeholder: el.placeholder || ''};
}"""


def _normalize_date_digits(value: str) -> str:
    """YYYY/MM/DD → digits only (YYYYMMDD) for read-back comparison."""
    return "".join(ch for ch in (value or "") if ch.isdigit())


def _age_to_birthdate(age: str) -> str:
    """Derive a deterministic YYYY/MM/DD birthdate from the age value.

    Deterministic on purpose: retries of the same account must keep the
    same birthdate instead of changing month/day each attempt.
    """
    try:
        age_int = int(age)
    except (ValueError, TypeError):
        age_int = 30
    birth_year = datetime.now().year - age_int
    rng = random.Random(f"birthdate:{age_int}")
    birth_month = rng.randint(1, 12)
    max_day = 28 if birth_month == 2 else 30 if birth_month in (4, 6, 9, 11) else 31
    birth_day = rng.randint(1, max_day)
    return f"{birth_year}/{birth_month:02d}/{birth_day:02d}"


async def _find_visible_input(page, selectors):
    """Return (selector, locator) for the first VISIBLE, non-hidden input.

    count() 只表示 DOM 存在；hidden 字段（如年龄页上的
    ``input[name='birthday']``）存在但不可见、type=hidden，必须排除。
    """
    for selector in selectors:
        try:
            loc = page.locator(selector)
            if await loc.count() == 0:
                continue
            first = loc.first
            if not await first.is_visible():
                continue
            input_type = await first.get_attribute("type")
            if (input_type or "").lower() == "hidden":
                continue
            return selector, first
        except Exception:  # noqa: BLE001 - probe each candidate independently
            continue
    return None


async def _find_age_input(page):
    """Locate a VISIBLE age input (年齢), or None."""
    return await _find_visible_input(page, _AGE_INPUT_SELECTORS)


async def _find_birthdate_single(page):
    """Locate a VISIBLE single birthdate input (``生年月日``), or None.

    年龄页上 hidden ``input[name='birthday']`` 存在但不可见，必须被排除，
    否则会把它当作单框生年月日并“假成功”。
    """
    return await _find_visible_input(page, _BIRTHDATE_SINGLE_SELECTORS)


async def _detect_age_or_birthdate(page):
    """Return (variant, selector, locator) — variant: age|birthdate|segmented.

    None 表示未知页面变体（调用方必须明确失败，不假装成功）。
    """
    age_found = await _find_age_input(page)
    if age_found is not None:
        return ("age", *age_found)
    birthdate_found = await _find_birthdate_single(page)
    if birthdate_found is not None:
        return ("birthdate", *birthdate_found)
    try:
        year_segment = page.locator("[role='spinbutton'][data-type='year']").first
        if await year_segment.count() > 0 and await year_segment.is_visible():
            return ("segmented", None, None)
    except Exception:  # noqa: BLE001 - probe failure means no segmented widget
        pass
    return (None, None, None)


async def _fill_birthdate_single(page, selector: str, locator, target: str) -> None:
    """Clear the default value first, then fill YYYY/MM/DD and verify."""
    # 1) Keyboard: clear the pre-filled default, then type the date.
    try:
        await human_clear(page, locator)
        await tiny_pause()
        await human_type_and_verify(
            page,
            locator,
            target,
            field_name="birthdate",
            normalize=_normalize_date_digits,
        )
        logger.info("phase1: birthdate single input filled via keyboard")
        return
    except Exception as exc:  # noqa: BLE001 - fall through to JS setter
        logger.warning("phase1: birthdate single keyboard fill failed: %s", exc)
    # 2) JS native setter fallback (controlled exception for this widget).
    try:
        written = await page.eval_on_selector(selector, _NATIVE_SETTER_JS, target)
        if _normalize_date_digits(written) == _normalize_date_digits(target):
            try:
                await locator.blur()
            except Exception:  # noqa: BLE001 - blur 失败不阻塞读回
                pass
            await tiny_pause()
            logger.info("phase1: birthdate single input filled via native setter")
            return
        logger.warning("phase1: birthdate single native setter read-back mismatch")
    except Exception as exc:  # noqa: BLE001 - keep the original error below
        logger.warning("phase1: birthdate single native setter failed: %s", exc)
    raise ElementNotFoundError("birthdate single input could not be filled")


async def _fill_birthdate_segments(page, age_value: str) -> bool:
    """Fill the segmented year/month/day widget; True when all segments filled."""
    try:
        age_int = int(age_value)
    except (ValueError, TypeError):
        age_int = 30
    birth_year = datetime.now().year - age_int
    birth_month = random.randint(1, 12)
    max_day = 28 if birth_month == 2 else 30 if birth_month in (4, 6, 9, 11) else 31
    birth_day = random.randint(1, max_day)
    values = {
        "year": str(birth_year),
        "month": f"{birth_month:02d}",
        "day": f"{birth_day:02d}",
    }
    filled = 0
    for data_type, value in values.items():
        selector = f"[role='spinbutton'][data-type='{data_type}']"
        segment = page.locator(selector).first
        try:
            await segment.click(timeout=1000)
            await segment.press("Control+a", timeout=1000)
            await segment.type(value, delay=45, timeout=2000)
            filled += 1
        except Exception as exc:
            logger.warning("phase1: birthdate %s segment fill failed: %s", data_type, exc)
    return filled == len(values)


async def _dump_age_dom(page) -> None:
    """Best-effort DOM dump for operator diagnostics; never masks the error."""
    try:
        candidate_detail = []
        for selector in _AGE_INPUT_SELECTORS:
            entry = {"selector": selector}
            loc = page.locator(selector)
            try:
                entry["count"] = await loc.count()
            except Exception as exc:
                entry["count_error"] = f"{type(exc).__name__}: {exc}"
            try:
                entry["is_editable"] = await loc.first.is_editable(timeout=3000)
            except Exception as exc:
                entry["is_editable_error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
            candidate_detail.append(entry)
        dump = await page.eval_on_selector_all(
            "input, [contenteditable='true'], select, textarea",
            """els => els.map(e => ({
                tag: e.tagName.toLowerCase(),
                name: e.name, id: e.id, placeholder: e.placeholder,
                type: e.type, role: e.getAttribute('role'),
                ariaLabel: e.getAttribute('aria-label'),
                ariaLabelledby: e.getAttribute('aria-labelledby'),
                disabled: e.disabled,
                readOnly: e.readOnly,
                ariaDisabled: e.getAttribute('aria-disabled'),
                maxLength: e.maxLength,
                contenteditable: e.getAttribute('contenteditable'),
                dataType: e.getAttribute('data-type'),
                outer: e.outerHTML.slice(0, 420)
            }))""",
        )
        body = await page.evaluate("document.body.innerText.slice(0, 500)")
        dump_path = Path(__file__).resolve().parents[2] / "diagnostics" / "age_dom_dump.json"
        dump_path.write_text(
            json.dumps(
                {"candidates": candidate_detail, "inputs": dump, "body": body},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        logger.warning("phase1: age DOM dump written to %s", dump_path)
    except Exception as dump_exc:
        logger.warning("phase1: age DOM dump failed: %s", dump_exc)


async def _fill_age_field(page, selector: str, locator, age_value: str, *, name_input=None) -> None:
    """填写可见年龄框：Tab → 逐键 → fill → 受控 JS setter（最后手段）。

    Only the age/birthdate widgets allow the JS setter fallback; email,
    password and code fields keep the shared per-keystroke contract.
    """
    # 0) Tab 从姓名框跳到年龄框（最接近真人操作），随后逐键输入。
    if name_input is not None:
        try:
            await name_input.press("Tab")
            await tiny_pause()
            active = await page.evaluate(_ACTIVE_ELEMENT_JS)
            focused_name = (active or {}).get("name") or ""
            focused_placeholder = (active or {}).get("placeholder") or ""
            if focused_name == "age" or "年齢" in focused_placeholder:
                await human_type_and_verify(page, locator, age_value, field_name="age")
                logger.info("phase1: age filled via Tab + keyboard (%s)", selector)
                return
        except Exception as exc:
            logger.warning("phase1: age Tab path failed: %s", exc)

    # 1) 点击年龄框 + 逐键输入（与邮箱/密码共用逐键契约）。
    try:
        await locator.click(timeout=3000)
        await tiny_pause()
        await human_type_and_verify(page, locator, age_value, field_name="age")
        logger.info("phase1: age filled via click + keyboard (%s)", selector)
        return
    except Exception as exc:
        logger.warning("phase1: age click+keyboard path failed: %s", exc)

    # 2) Playwright fill（自带 actionability 等待；遮罩页面可能失败）。
    try:
        await locator.fill(age_value, timeout=10000)
        if (await locator.input_value(timeout=2000)).strip() == age_value:
            logger.info("phase1: age filled via fill (%s)", selector)
            return
        logger.warning("phase1: age candidate %s read-back mismatch after fill", selector)
    except Exception as exc:
        logger.warning("phase1: age candidate %s fill failed: %s", selector, exc)

    # 3) 受控 JS native setter + blur + 读回（最后手段，仅此控件）。
    try:
        written = await page.eval_on_selector(selector, _NATIVE_SETTER_JS, age_value)
        if written == age_value:
            try:
                await locator.blur()
            except Exception:  # noqa: BLE001 - blur 失败不阻塞读回
                pass
            await tiny_pause()
            if (await locator.input_value(timeout=2000)).strip() == age_value:
                logger.info("phase1: age filled via native setter (%s)", selector)
                return
        logger.warning("phase1: age candidate %s native setter read-back mismatch", selector)
    except Exception as exc:
        logger.warning("phase1: age candidate %s native setter failed: %s", selector, exc)

    raise ElementNotFoundError(f"age input could not be filled: {selector}")


async def _attempt_age_fill(page, age_value: str, *, name_input=None) -> bool:
    """生年月日失败后的年龄兜底：可见年龄框优先，其次直接候选框。

    可见性探测可能漏报（页面刚渲染完成），因此候选框只要 count>0 就
    尝试填写链（setter 可写，即使探测不到可见性）。
    """
    age_found = await _find_age_input(page)
    if age_found is not None:
        selector, locator = age_found
        await _fill_age_field(page, selector, locator, age_value, name_input=name_input)
        return True
    for selector in _AGE_INPUT_SELECTORS:
        try:
            loc = page.locator(selector).first
            if await loc.count() > 0:
                await _fill_age_field(page, selector, loc, age_value, name_input=name_input)
                return True
        except Exception:  # noqa: BLE001 - 尝试下一个候选
            continue
    return False


async def _fill_age_or_birthdate(page, age: str, *, name_input=None) -> None:
    """Fill the VISIBLE age input, or the VISIBLE single birthdate field,
    or the segmented birthdate widget.

    Priority (real-page evidence 2026-08):
      1. Visible age input (年齢): Tab → click+keyboard → fill → JS setter last.
      2. Visible single birthdate input (生年月日): clear default, fill date.
      3. Segmented year/month/day.
      4. Unknown page → DOM dump + raise (never a silent fake success).

    The hidden ``input[name='birthday']`` on the age page must never be
    treated as the single birthdate field.
    """
    age_value = str(age)
    variant, selector, locator = await _detect_age_or_birthdate(page)

    if variant == "age":
        try:
            await _fill_age_field(page, selector, locator, age_value, name_input=name_input)
            return
        except ElementNotFoundError:
            await _dump_age_dom(page)
            raise

    if variant == "birthdate":
        try:
            await _fill_birthdate_single(
                page, selector, locator, _age_to_birthdate(age_value)
            )
            return
        except ElementNotFoundError:
            logger.warning(
                "phase1: single birthdate fill failed, "
                "falling back to age/segmented paths"
            )
        if await _attempt_age_fill(page, age_value, name_input=name_input):
            return

    if variant == "segmented" and await _fill_birthdate_segments(page, age_value):
        return

    await _dump_age_dom(page)
    raise ElementNotFoundError("age and birthdate inputs were not editable")


async def _assert_form_ready_before_submit(page, age: str) -> None:
    """提交前读回可见年龄/生年月日字段；值不一致则不提交。

    JS setter 可能写入 DOM value 但 React 未接受；提交前再读一次，
    不一致时抛错（此时还没有提交副作用，可安全重填重试）。
    """
    age_value = str(age)
    age_found = await _find_age_input(page)
    if age_found is not None:
        _, locator = age_found
        try:
            value = (await locator.input_value(timeout=2000)).strip()
        except Exception:  # noqa: BLE001 - 读回失败按不一致处理
            value = ""
        if value != age_value:
            raise ElementNotFoundError("age input read-back mismatch before submit")
        return
    birthdate_found = await _find_birthdate_single(page)
    if birthdate_found is not None:
        _, locator = birthdate_found
        try:
            value = await locator.input_value(timeout=2000)
        except Exception:  # noqa: BLE001 - 读回失败按不一致处理
            value = ""
        expected = _age_to_birthdate(age_value)
        if _normalize_date_digits(value) != _normalize_date_digits(expected):
            raise ElementNotFoundError("birthdate input read-back mismatch before submit")
        return
    # 分段/未知变体：提交后验收兜底。


async def _form_still_visible(page) -> bool:
    """True 表示注册表单字段仍可见（提交被页面拒绝）。"""
    for selector in _FORM_PRESENT_SELECTORS:
        try:
            loc = page.locator(selector).first
            if await loc.count() > 0 and await loc.is_visible():
                return True
        except Exception:  # noqa: BLE001 - 单 selector 探测失败继续
            continue
    return False


async def _validation_error_visible(page) -> bool:
    """True 表示页面出现年龄/生年月日校验失败文案。"""
    for selector in _FORM_VALIDATION_ERROR_SELECTORS:
        try:
            loc = page.locator(selector).first
            if await loc.count() > 0 and await loc.is_visible():
                return True
        except Exception:  # noqa: BLE001 - 单 selector 探测失败继续
            continue
    return False


async def _wait_form_departed_after_submit(page, *, timeout: float) -> bool:
    """轮询直到表单离开或校验错误出现；超时仍未离开返回 False。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await _validation_error_visible(page):
            return False
        if not await _form_still_visible(page):
            return True
        await asyncio.sleep(0.5)
    return False


async def phase1_form(page, name: str, age: str) -> None:
    """Fill name + age/birthdate, submit, and verify the form actually left.

    提交前读回 + 提交后离开表单验收：表单仍在或出现校验错误 = 明确失败，
    绝不进入 welcome 等待（防止“假填写成功”后卡在年龄校验页）。
    """
    t0 = time.monotonic()
    logger.info("phase1_form: start (t=0.0s)")

    # ── Name: 共享逐键输入（已填正确值时跳过，避免覆盖重试现场）──
    name_input = page.locator(config.SELECTORS["name_input"]).first
    await name_input.wait_for(state="visible", timeout=5000)
    await human_type_and_verify(
        page,
        name_input,
        name,
        field_name="name",
    )
    logger.info("phase1_form: name ready (t=%.1fs)", time.monotonic() - t0)

    # ── Age or birthdate ──
    logger.info("phase1_form: checking age input (t=%.1fs)", time.monotonic() - t0)
    await _fill_age_or_birthdate(page, age, name_input=name_input)
    logger.info("phase1_form: age/birthdate filled (t=%.1fs)", time.monotonic() - t0)

    # 提交前读回：setter 写入后 React 可能未接受，值不一致则不提交。
    await _assert_form_ready_before_submit(page, age)

    await human_delay((0.8, 1.5))
    logger.info("phase1_form: pre-submit (t=%.1fs), clicking submit...", time.monotonic() - t0)
    await safe_click(page, [
        "button:has-text('\u30a2\u30ab\u30a6\u30f3\u30c8\u306e\u4f5c\u6210\u3092\u5b8c\u4e86\u3059\u308b')",
        "button:has-text('Complete account creation')",
    ], timeout_ms=5000)
    logger.info("phase1_form: submit clicked (t=%.1fs)", time.monotonic() - t0)
    await human_delay((2.0, 4.0))

    # 提交后验收：表单必须离开；仍在表单或出现校验错误 = 明确失败。
    if not await _wait_form_departed_after_submit(page, timeout=_FORM_DEPART_TIMEOUT):
        raise ElementNotFoundError(
            "registration form still visible after submit; "
            "age/birthdate validation rejected the values"
        )
    logger.info("phase1_form: post-submit wait done (t=%.1fs)", time.monotonic() - t0)


async def phase1_ready_confirm(page) -> None:
    """Dismiss the '準備が完了しました' / welcome modal.

    Uses Playwright OR locator chain to match any welcome text.
    """
    logger.info("phase1: waiting for welcome modal (timeout=30s)...")
    try:
        # Build OR chain: match any welcome text
        welcome_loc = page.locator("text=準備が完了しました")
        for alt in ("text=準備", "text=ようこそ", "text=Welcome to ChatGPT", "text=Welcome"):
            welcome_loc = welcome_loc.or_(page.locator(alt))
        await welcome_loc.first.wait_for(timeout=30000)

        logger.info("phase1: welcome text detected, dismissing modal...")
        await safe_click(page, [
            "button:text-is('\u7d9a\u884c')",
            "button:has-text('\u7d9a\u884c')",
            "button:has-text('Continue')",
            "button:has-text('Get started')",
            "button:has-text('\u59cb\u3081\u308b')",
            "button:has-text('Next')",
            "button:has-text('\u6b21\u3078')",
        ], timeout_ms=5000)
        await human_delay((2.0, 3.0))
        logger.info("phase1: welcome modal dismissed")
    except ElementNotFoundError:
        logger.info("phase1: no welcome modal detected (already on home/survey)")
    except SAFE_EXCEPTIONS:
       logger.info("phase1: welcome modal handling failed, continuing...")


async def phase1_dismiss_personalization_modal(page) -> bool:
    """Dismiss the 'より関連性が高く、パーソナライズされた回答' / memory personalization modal.

    Clicks 'OK' when the personalization dialog appears.
    Returns True if modal was detected and dismissed, False otherwise.
    """
    try:
        loc = page.locator("text=より関連性が高く、パーソナライズされた回答")
        for alt in (
            "text=より関連性が高く",
            "text=パーソナライズされた回答",
            "text=メモリをより最新の状態に保てるようになったため",
            "text=More relevant and personalized answers",
            "text=Personalized answers",
            "[role='dialog']:has-text('パーソナライズ')",
            "[role='dialog']:has-text('メモリ')",
        ):
            loc = loc.or_(page.locator(alt))
        if await loc.first.is_visible(timeout=1000):
            logger.info("phase1: personalization modal detected, clicking OK...")
            ok_clicked = False
            for ok_sel in (
                "[role='dialog'] button.btn-primary:has-text('OK')",
                "button.btn-primary:has-text('OK')",
                "[role='dialog'] button.btn-primary",
                "button:text-is('OK')",
                "[role='dialog'] button:has-text('OK')",
                "button:has-text('OK')",
            ):
                try:
                    btn = page.locator(ok_sel).first
                    if await btn.is_visible(timeout=1000):
                        await btn.click()
                        ok_clicked = True
                        await human_delay((1.0, 2.0))
                        logger.info("phase1: personalization modal dismissed via '%s'", ok_sel)
                        break
                except Exception:
                    continue
            return ok_clicked
    except Exception as exc:
        logger.debug("phase1: personalization modal check skipped/failed: %s", exc)
    return False


async def phase1_survey(page) -> None:
    logger.info("phase1: checking for survey popup")
    for _page_idx in range(5):
        triggered = False
        for txt in ("\u30a2\u30f3\u30b1\u30fc\u30c8", "Survey", "Questionnaire", "\u304a\u5ba2\u69d8\u30a2\u30f3\u30b1\u30fc\u30c8"):
            try:
                loc = page.locator(f"text={txt}").first
                if await loc.is_visible(timeout=2000):
                    triggered = True
                    break
            except SAFE_EXCEPTIONS:
                continue
        if not triggered:
            return
        for selector in ("input[type='radio']", "input[type='checkbox']"):
            try:
                loc = page.locator(selector).first
                if await loc.is_visible(timeout=1500):
                    await loc.click()
                    await tiny_pause()
                    break
            except SAFE_EXCEPTIONS:
                continue
        try:
            await safe_click(page, [
                "button:has-text('\u6b21\u3078')", "button:has-text('Next')",
                "button:has-text('\u7d9a\u884c')", "button:has-text('Submit')",
                "button:has-text('\u9001\u4fe1')",
            ], timeout_ms=2000)
        except ElementNotFoundError:
            return
        await human_delay((1.5, 1.5))
    logger.info("phase1: survey handler gave up after 5 pages")
