"""邮箱活动队列 — `input/email.txt` 移除与 `accounts/accounts_skipped.txt` 归档.

职责：
    * `input/email.txt` 只保存等待执行的邮箱；成功创建或连续模式跳过（SKIPPED）
      的邮箱都必须退出该队列。
    * 跳过邮箱的原始行（四列 ``----`` 分隔）先原子写入 `accounts_skipped.txt`，
      再从 input 移除；skipped 不是黑名单，用户复制回 input 即重新授权。
    * 所有文件写入均为临时文件 + ``os.replace`` 原子替换，并在独立文件锁内执行。

安全约定：
    * 原始行只在本模块内部流转，不进入日志、checkpoint、错误报告或异常消息。
    * 同一批次排除集合只保存邮箱 fingerprint，仅存在于进程内存。
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

logger = logging.getLogger(__name__)


class EmailQueueError(RuntimeError):
    """Raised when an email queue file transition cannot be completed."""


_WRITE_RETRY_COUNT = 3
_WRITE_RETRY_DELAYS = (0.3, 0.7, 1.5)


def _is_retryable_write_error(exc: BaseException) -> bool:
    """True for transient file locks (permission / Windows sharing violation)."""
    if isinstance(exc, PermissionError):
        return True
    if isinstance(exc, OSError) and getattr(exc, "winerror", None) in (32, 33, 36):
        return True
    return False


def email_fingerprint(email: str) -> str:
    """Stable fingerprint for in-batch exclusion; never reveals the address."""
    return hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()


def _atomic_write(path: Path, lines: list[str]) -> None:
    """原子写，遇瞬时文件占用（PermissionError / 共享冲突）短退避重试。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(_WRITE_RETRY_COUNT):
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=str(path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write("\n".join(lines))
                if lines:
                    fh.write("\n")
            os.replace(tmp_name, path)
            return
        except BaseException as exc:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            if (
                _is_retryable_write_error(exc)
                and attempt < _WRITE_RETRY_COUNT - 1
            ):
                time.sleep(_WRITE_RETRY_DELAYS[attempt])
                continue
            raise


def _read_lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    return path.read_text(encoding="utf-8").splitlines()


def _normalized_email(line: str) -> str:
    """Extract the email from a raw input/skipped line without logging it."""
    return line.split("----", 1)[0].strip().lower()


class EmailQueue:
    """Primitives for moving emails between the active queue and skipped."""

    def __init__(
        self,
        input_path: str | Path,
        skipped_path: str | Path,
        lock_path: str | Path | None = None,
    ) -> None:
        self.input_path = Path(input_path)
        self.skipped_path = Path(skipped_path)
        self.lock_path = (
            Path(lock_path)
            if lock_path is not None
            else self.input_path.parent.parent / "runtime" / "locks" / "email_queue.lock"
        )

    # ── reads ──

    def find_raw_line(self, email: str) -> Optional[str]:
        """Return the first raw input line whose email matches (case-insensitive)."""
        target = email.strip().lower()
        for line in _read_lines(self.input_path):
            if _normalized_email(line) == target:
                return line
        return None

    def has_in_input(self, email: str) -> bool:
        return self.find_raw_line(email) is not None

    def has_in_skipped(self, email: str) -> bool:
        target = email.strip().lower()
        return any(
            _normalized_email(line) == target
            for line in _read_lines(self.skipped_path)
        )

    # ── mutations (all under the file lock) ──

    def archive_skipped(self, raw_line: str) -> None:
        """Append-or-update the skipped file with the exact raw line."""
        with self._lock():
            lines = _read_lines(self.skipped_path)
            target = _normalized_email(raw_line)
            remaining = [
                line for line in lines if _normalized_email(line) != target
            ]
            remaining.append(raw_line.rstrip("\r\n"))
            _atomic_write(self.skipped_path, remaining)

    def remove_from_input(self, email: str) -> None:
        """Remove every input row belonging to the email (dedupe-safe)."""
        with self._lock():
            lines = _read_lines(self.input_path)
            target = email.strip().lower()
            remaining = [
                line for line in lines if _normalized_email(line) != target
            ]
            if len(remaining) == len(lines):
                return  # already absent; idempotent
            _atomic_write(self.input_path, remaining)

    def remove_from_skipped(self, email: str) -> None:
        """Remove the email from the skipped file after a later success."""
        with self._lock():
            lines = _read_lines(self.skipped_path)
            target = email.strip().lower()
            remaining = [
                line for line in lines if _normalized_email(line) != target
            ]
            if len(remaining) == len(lines):
                return
            _atomic_write(self.skipped_path, remaining)

    def update_refresh_token(self, email: str, new_token: str) -> bool:
        """Replace the 4th column (refresh_token) of every matching input row.

        Returns True when at least one row was updated.  Rows with fewer than
        four ``----`` fields are left untouched.  The write is atomic and
        serialized under the queue file lock.
        """
        with self._lock():
            lines = _read_lines(self.input_path)
            target = email.strip().lower()
            updated_lines: list[str] = []
            updated = False
            for line in lines:
                if _normalized_email(line) == target:
                    parts = line.split("----")
                    if len(parts) >= 4:
                        parts[3] = new_token
                        line = "----".join(parts)
                        updated = True
                updated_lines.append(line)
            if updated:
                _atomic_write(self.input_path, updated_lines)
            return updated

    @contextmanager
    def _lock(self) -> Iterator[None]:
        """Serialize queue mutations across processes."""
        from gpt_automation.state.execution_lock import SessionExecutionLock

        lock = SessionExecutionLock(self.lock_path)
        if not lock.acquire():
            raise EmailQueueError("email queue is locked by another process")
        try:
            yield
        finally:
            lock.release()


class BatchEmailExclusion:
    """In-batch exclusion: an email is processed at most once per batch."""

    def __init__(self) -> None:
        self._fingerprints: set[str] = set()

    def mark(self, email: str) -> None:
        self._fingerprints.add(email_fingerprint(email))

    def contains(self, email: str) -> bool:
        return email_fingerprint(email) in self._fingerprints

    def __len__(self) -> int:
        return len(self._fingerprints)
