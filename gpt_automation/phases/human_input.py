"""
共享人工输入 — CloakBrowser 与 Firefox 隐私模式共用同一套逐键输入契约.

Why this exists:
    邮箱、密码、姓名和验证码字段必须使用「等待可见 → 逐键输入 → human_delay
    → 读回校验」的人工作业模式。CloakBrowser 的 humanize=True 会在底层把
    locator.type() 进一步人工化（鼠标移动、随机按键间隔、纠错），因此这里
    只提供浏览器无关的调用顺序，绝不按 browser_backend 分叉。

安全约定:
    expected 值只用于读回比较，绝不写入日志、异常消息或截图文件名。
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from gpt_automation.utils.exceptions import ElementNotFoundError
from gpt_automation.utils.timing import (
    SUBMIT_PRE_PAUSE,
    TYPE_KEY_DELAY_MS_RANGE,
    TYPE_POST_PAUSE,
    human_delay,
    tiny_pause,
)

logger = logging.getLogger(__name__)


def typing_delay_ms() -> float:
    """Randomized per-keystroke delay used by the shared typing path."""
    lo, hi = TYPE_KEY_DELAY_MS_RANGE
    import random

    return random.uniform(lo, hi)


def normalize_email(value: str) -> str:
    """邮箱比较归一化：去除首尾空白并转为小写."""
    return (value or "").strip().lower()


def normalize_exact(value: str) -> str:
    """严格比较：密码、验证码等敏感字段原样比较."""
    return value or ""


async def human_clear(page, locator) -> None:
    """用键盘清空字段当前值（Ctrl+A + Backspace），并在前后保留人工停顿.

    CloakBrowser 的 humanize 同样会接管 press() 的键盘事件，因此该清空
    路径在两个后端保持一致的人工节奏。
    """
    await locator.press("Control+A")
    await tiny_pause()
    await locator.press("Backspace")
    await tiny_pause()


async def human_clear_in_frame(frame, css: str) -> None:
    """用 frame 级键盘清空 iframe 内字段（Ctrl+A + Backspace）.

    CloakBrowser humanize 下 locator 级 press 对 FrameLocator 派生 locator
    会路由到 isolated-world 解析器并被拒绝，frame 级 press 走真实几何
    解析 + 页面级人工键盘，两个后端行为一致。
    """
    await frame.press(css, "Control+A")
    await tiny_pause()
    await frame.press(css, "Backspace")
    await tiny_pause()


async def human_type_in_frame(
    frame,
    css: str,
    expected: str,
    *,
    field_name: str,
    delay_ms: Optional[float] = None,
    normalize: Callable[[str], str] = normalize_exact,
) -> None:
    """iframe 内逐键输入并读回校验，最多自动重输一次.

    与 human_type_and_verify 同一契约，但全部使用真实 Frame 的 frame 级
    API（frame.type / frame.press / locator 只读方法）。CloakBrowser
    humanize=True 会把 FrameLocator 派生 locator 的 press_sequentially
    路由到 isolated-world 解析器，对 enter-frame 链式选择器直接抛
    UnsupportedHumanizeSelectorError，因此 iframe 内不得使用 locator 级
    动作方法；frame 级 API 走真实几何解析 + 页面级人工键盘，Cloak 与
    Firefox 原生路径均安全。

    Args:
        frame: Stripe iframe 的真实 Frame 对象（page.frames 成员）。
        css: iframe 内目标字段的 CSS 选择器。
        expected: 期望的输入值；绝不进入日志或异常。
        field_name: 安全标签，如 "card_number" / "card_expiry"。
        delay_ms: 逐键间隔（毫秒）；省略时使用 timing.py 统一人类节奏。
        normalize: 读回比较的归一化函数；卡号/有效期用 normalize_digits。

    Raises:
        ElementNotFoundError: 重输后仍不一致；消息只含 field_name，不含值。
    """
    for attempt in range(2):
        current = normalize(await frame.locator(css).first.input_value())
        expected_norm = normalize(expected)
        if current == expected_norm:
            logger.info("human_input: %s 已就绪", field_name)
            return
        if attempt > 0:
            await human_clear_in_frame(frame, css)
        per_key = delay_ms if delay_ms is not None else typing_delay_ms()
        await frame.type(css, expected, delay=per_key)
        await human_delay(TYPE_POST_PAUSE)
        if normalize(await frame.locator(css).first.input_value()) == expected_norm:
            logger.info("human_input: %s 逐键输入并校验通过", field_name)
            return
        logger.warning(
            "human_input: %s 读回不一致，尝试第 %d 次重输",
            field_name,
            attempt + 2,
        )
    raise ElementNotFoundError(
        f"{field_name} 输入读回校验失败（重输后仍不一致）；已保留现场"
    )


async def human_type_and_verify(
    page,
    locator,
    expected: str,
    *,
    field_name: str,
    delay_ms: Optional[float] = None,
    normalize: Callable[[str], str] = normalize_exact,
) -> None:
    """逐键输入并读回校验，最多自动重输一次.

    Args:
        page: Playwright Page（用于 page.keyboard 兜底路径）。
        locator: 已解析的目标字段 locator（.first）。
        expected: 期望的输入值；绝不进入日志或异常。
        field_name: 安全标签，如 "email" / "signup_password" / "code"。
        delay_ms: 逐键间隔（毫秒）；省略时使用 timing.py 中统一的人类节奏
            区间随机值；Cloak 会忽略并用自己的节奏。
        normalize: 读回比较的归一化函数；邮箱用 normalize_email。

    Raises:
        ElementNotFoundError: 重输后仍不一致；消息只含 field_name，不含值。
    """
    for attempt in range(2):
        current = normalize(await locator.input_value())
        expected_norm = normalize(expected)
        if current == expected_norm:
            logger.info("human_input: %s 已就绪", field_name)
            return
        if attempt > 0:
            await human_clear(page, locator)
        per_key = delay_ms if delay_ms is not None else typing_delay_ms()
        if hasattr(locator, "press_sequentially"):
            await locator.press_sequentially(expected, delay=per_key)
        else:
            await locator.type(expected, delay=per_key)
        await human_delay(TYPE_POST_PAUSE)
        if normalize(await locator.input_value()) == expected_norm:
            logger.info("human_input: %s 逐键输入并校验通过", field_name)
            return
        logger.warning(
            "human_input: %s 读回不一致，尝试第 %d 次重输",
            field_name,
            attempt + 2,
        )
    raise ElementNotFoundError(
        f"{field_name} 输入读回校验失败（重输后仍不一致）；已保留现场"
    )


async def human_submit(page, selectors: list[str], *, timeout_ms: int = 15000) -> None:
    """以普通鼠标点击提交按钮，最多重试一次；不接受 force/dispatch/JS click.

    Firefox + React 重绘会导致普通点击的可操作性检查超时，因此失败时重新
    定位按钮后以普通方式再点击一次；两次都失败则抛出 ElementNotFoundError，
    由调用方决定 Enter 兜底或保留现场。
    """
    # 第一次定位：逐个 selector 等待可见，找到第一个可用的按钮。
    locator = None
    matched_selector = None
    for selector in selectors:
        try:
            candidate = page.locator(selector).first
            await candidate.wait_for(state="visible", timeout=timeout_ms)
            locator = candidate
            matched_selector = selector
            break
        except Exception:  # noqa: BLE001 - 当前 selector 不可见，尝试下一个
            continue
    if locator is None:
        raise ElementNotFoundError("提交按钮未找到（所有 selector 均不可见）")

    last_error: Optional[BaseException] = None
    for _attempt in range(2):
        try:
            await human_delay(SUBMIT_PRE_PAUSE)
            await locator.click(timeout=timeout_ms)
            logger.info("human_input: 已点击提交按钮（%s）", matched_selector)
            return
        except ElementNotFoundError:
            raise
        except Exception as exc:  # noqa: BLE001 - 重绘导致的 actionability 异常
            last_error = exc
            await human_delay()
            # 页面重绘可能使 locator 失效：重新定位后普通点击重试一次。
            try:
                locator = page.locator(matched_selector).first
                await locator.wait_for(state="visible", timeout=timeout_ms)
            except Exception as exc2:  # noqa: BLE001
                raise ElementNotFoundError(
                    f"提交按钮普通点击失败：{last_error}"
                ) from exc2
    raise ElementNotFoundError(f"提交按钮普通点击失败：{last_error}")
