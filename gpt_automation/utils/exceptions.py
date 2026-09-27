"""
Safe exception whitelist + custom exceptions.

Provides:
    - SAFE_EXCEPTIONS   : transient errors safe to catch/silence.
    - FATAL_EXCEPTIONS  : errors that must never be swallowed.
    - ElementNotFoundError : raised when a required page element is not found.
    - SkipEmailException    : raised when operator chooses to skip current email.
    - RestartBrowserException: raised when operator chooses to restart browser.
"""

from __future__ import annotations

import asyncio

# 致命异常：绝不应该被吞掉
FATAL_EXCEPTIONS: tuple = (
    KeyboardInterrupt,
    SystemExit,
    MemoryError,
    RecursionError,
    asyncio.CancelledError,
)

# 安全异常：网络波动、UI 渲染延迟等可恢复错误
SAFE_EXCEPTIONS: tuple = (
    asyncio.TimeoutError,
    ConnectionError,
    OSError,
    TimeoutError,
)


class ElementNotFoundError(Exception):
    """Raised when a required page element is not found."""


class SkipEmailException(Exception):
    """Raised to skip the current email and move to the next one."""


class QuitBatchException(Exception):
    """Raised when operator chooses to quit the entire batch from interactive menu."""


class RestartBrowserException(Exception):
    """Raised when operator chooses to restart the browser for the current account."""


class NeedsOperatorException(Exception):
    """Raised when the current account must be suspended for manual handling."""


class OperatorDeferredException(NeedsOperatorException):
    """Raised when the operator explicitly defers the current account."""


def is_fatal(exc: BaseException) -> bool:
    """True if *exc* should NEVER be silently swallowed."""
    return isinstance(exc, FATAL_EXCEPTIONS)
