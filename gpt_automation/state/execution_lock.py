"""Process lifetime locks for one workflow session."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


class SessionExecutionLock:
    """Hold an OS-level lock so a session cannot run in two processes."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._handle: Optional[object] = None

    def acquire(self) -> bool:
        if self._handle is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            handle.seek(0)
            if self.path.stat().st_size == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError):
            handle.close()
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except (OSError, IOError):
            pass
        finally:
            handle.close()

    @classmethod
    def is_locked(cls, path: str | Path) -> bool:
        lock_path = Path(path)
        if not lock_path.exists():
            return False
        probe = cls(lock_path)
        if probe.acquire():
            probe.release()
            return False
        return True

    def __enter__(self) -> "SessionExecutionLock":
        if not self.acquire():
            raise RuntimeError(f"session execution lock is already held: {self.path}")
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.release()
