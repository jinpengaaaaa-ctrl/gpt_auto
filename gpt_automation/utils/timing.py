"""
统一延迟管理 — 把所有 phase 中散落的 random.uniform + asyncio.sleep 调用集中起来.

Why this exists:
    之前每个阶段都自己写 `await asyncio.sleep(random.uniform(0.5, 2.0))`，
    这些魔法数字散落各处，没法统一调整人机交互速度。
    现在所有阶段都通过 `human_delay()` 调用，调一处全调整。
"""

from __future__ import annotations

import asyncio
import random
from typing import Tuple, Union

# 常用延迟区间（秒）— 集中定义便于调整
DELAY_TINY = (0.2, 0.5)         # 两次输入之间
DELAY_SHORT = (0.5, 1.5)        # 正常点击
DELAY_MEDIUM = (1.0, 2.5)       # 页面跳转
DELAY_LONG = (2.0, 4.0)         # 重要操作后（提交等）
DELAY_AFTER_SUBMIT = (5.0, 8.0) # Stripe 提交后等待

# 逐键输入的按键间隔（毫秒）。CloakBrowser 的 humanize 引擎会忽略该值并
# 使用自身随机节奏；Firefox 隐私模式使用该值作为每个字符的按键间隔。
HUMAN_TYPE_DELAY_MS = 45
HUMAN_TYPE_DELAY_MS_SLOW = 60

# Firefox（普通 Playwright）逐键输入的按键间隔区间（毫秒）。
# CloakBrowser 的 humanize 引擎忽略该值并用自己的节奏；两个后端共享同一
# 段代码时，此区间就是 Firefox 一侧的人类输入节奏。
TYPE_KEY_DELAY_MS_RANGE = (80, 140)

# 逐键输入完成后的停顿区间（秒），模拟人输入完一栏后短暂确认。
TYPE_POST_PAUSE = (0.3, 0.8)

# 提交按钮点击前的停顿区间（秒），模拟人点击前的短暂停顿。
SUBMIT_PRE_PAUSE = (0.5, 1.2)

Number = Union[int, float]
Range = Union[Number, Tuple[Number, Number]]


def _to_range(seconds: Range) -> Tuple[float, float]:
    """将单值或元组统一为 (lo, hi) 元组."""
    if isinstance(seconds, (int, float)):
        v = float(seconds)
        return (v, v)
    return (float(seconds[0]), float(seconds[1]))


async def human_delay(seconds: Range = DELAY_SHORT) -> float:
    """Wait a randomized, human-like delay.

    Args:
        seconds: 单值 / (lo, hi) 元组，默认短延迟

    Returns:
        实际等待的秒数
    """
    lo, hi = _to_range(seconds)
    wait = random.uniform(lo, hi)
    await asyncio.sleep(wait)
    return wait


async def tiny_pause() -> float:
    return await human_delay(DELAY_TINY)


async def short_pause() -> float:
    return await human_delay(DELAY_SHORT)


async def medium_pause() -> float:
    return await human_delay(DELAY_MEDIUM)


async def long_pause() -> float:
    return await human_delay(DELAY_LONG)


async def after_submit_pause() -> float:
    return await human_delay(DELAY_AFTER_SUBMIT)


# 开通会员 / 订阅导航与就绪轮询参数
CHECKOUT_NAV_TIMEOUT_S = 60.0
CHECKOUT_NAV_INTERVAL_S = 0.5

