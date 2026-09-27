"""
重试 / 退避工具 (Feature #18).

Provides a small async-friendly decorator that wraps any awaitable with:
    * up to N attempts (config.RETRY_CONFIG["max_attempts"])
    * exponential backoff with jitter
    * structured logging on each failure
    * exception classification (transient vs. permanent)
"""

from __future__ import annotations

import asyncio
import functools
import logging
import random
from typing import Any, Awaitable, Callable, Optional, Tuple, Type, TypeVar

import config

logger = logging.getLogger(__name__)

T = TypeVar("T")

# ── Exception classification ───────────────────────────────────────

TRANSIENT_EXCEPTIONS: Tuple[Type[BaseException], ...] = (
    asyncio.TimeoutError,
    ConnectionError,
    OSError,
)

FATAL_EXCEPTIONS: Tuple[Type[BaseException], ...] = (
    asyncio.CancelledError,
    KeyboardInterrupt,
    SystemExit,
    MemoryError,
    RecursionError,
)


def is_transient(exc: BaseException) -> bool:
    """Return True if *exc* looks like a recoverable transient error."""
    if isinstance(exc, FATAL_EXCEPTIONS):
        return False
    if isinstance(exc, TRANSIENT_EXCEPTIONS):
        return True
    name: str = type(exc).__name__
    transient_names: Tuple[str, ...] = (
        "TimeoutError",
        "PlaywrightTimeoutError",
        "Error",
        "NetworkError",
        "HTTPError",
    )
    return name in transient_names


# ── Decorator ───────────────────────────────────────────────────────


def with_retry(
    step_name: str,
    max_attempts: Optional[int] = None,
    base_delay: Optional[float] = None,
    max_delay: Optional[float] = None,
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[Optional[T]]]]:
    """Decorator: retry the wrapped coroutine with exponential backoff."""
    cfg = config.RETRY_CONFIG
    max_a: int = max_attempts if max_attempts is not None else int(cfg["max_attempts"])
    base: float = base_delay if base_delay is not None else float(cfg["base_delay"])
    cap: float = max_delay if max_delay is not None else float(cfg["max_delay"])
    expo: float = float(cfg["exponential_base"])
    jitter: float = float(cfg["jitter"])

    def decorator(
        fn: Callable[..., Awaitable[T]],
    ) -> Callable[..., Awaitable[Optional[T]]]:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Optional[T]:
            attempt: int = 0
            last_exc: Optional[BaseException] = None
            while attempt < max_a:
                attempt += 1
                try:
                    result: T = await fn(*args, **kwargs)
                    if attempt > 1:
                        logger.info(
                            "retry: %s succeeded on attempt %d/%d",
                            step_name, attempt, max_a,
                        )
                    return result
                except Exception as exc:
                    last_exc = exc
                    transient: bool = is_transient(exc)
                    if not transient:
                        logger.error(
                            "retry: %s non-transient %s on attempt %d/%d: %s",
                            step_name, type(exc).__name__, attempt, max_a, exc,
                        )
                        raise  # re-raise for upper-level guarded execution
                    if attempt >= max_a:
                        logger.error(
                            "retry: %s exhausted %d attempts: %s",
                            step_name, max_a, exc,
                        )
                        raise  # exhausted transient retries
                    sleep_for: float = min(cap, base * (expo ** (attempt - 1)))
                    sleep_for *= 1.0 + random.uniform(-jitter, jitter)
                    sleep_for = max(0.0, sleep_for)
                    logger.warning(
                        "retry: %s transient %s on attempt %d/%d (sleep %.2fs): %s",
                        step_name, type(exc).__name__, attempt, max_a, sleep_for, exc,
                    )
                    await asyncio.sleep(sleep_for)
            if last_exc is not None:
                raise last_exc  # should not reach here; re-raise

            return None  # unreachable; satisfy type checker

        return wrapper

    return decorator
