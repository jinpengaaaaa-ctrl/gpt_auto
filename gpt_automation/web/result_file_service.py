"""Read and atomically update the local ``results.txt`` file.

The dashboard treats the file as a user-editable document.  This service keeps
the file boundary in the Python process so the browser never receives direct
filesystem access and stale drafts cannot silently overwrite newer results.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


class ResultFileError(Exception):
    """A safe, user-facing result file error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ResultLine:
    line_no: int
    raw: str
    fields: list[str]
    kind: str


@dataclass(frozen=True)
class ResultFileSnapshot:
    name: str
    version: str
    modified_at: str | None
    size: int
    newline: str
    raw_text: str
    lines: list[ResultLine]


class ResultFileService:
    """Manage one local result file with optimistic version checks."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    def read(self) -> ResultFileSnapshot:
        with self._lock:
            return self._read_unlocked()

    def save(self, expected_version: str, raw_text: str) -> ResultFileSnapshot:
        if not isinstance(raw_text, str):
            raise ResultFileError(
                "RESULT_FILE_INVALID", "结果文件内容必须是文本。"
            )
        if "\x00" in raw_text:
            raise ResultFileError(
                "RESULT_FILE_INVALID", "结果文件不能包含无效控制字符。"
            )

        with self._lock:
            current = self._read_unlocked()
            if expected_version != current.version:
                raise ResultFileError(
                    "RESULT_FILE_CONFLICT",
                    "结果文件已被其他流程修改，请重新读取后再保存。",
                )
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path: str | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    newline="",
                    delete=False,
                    dir=self.path.parent,
                    prefix=f".{self.path.name}.",
                    suffix=".tmp",
                ) as temporary:
                    temporary_path = temporary.name
                    temporary.write(raw_text)
                    temporary.flush()
                    os.fsync(temporary.fileno())
                os.replace(temporary_path, self.path)
                temporary_path = None
            except OSError as exc:
                raise ResultFileError(
                    "RESULT_FILE_WRITE_FAILED",
                    "结果文件保存失败，原文件未被替换。",
                ) from exc
            finally:
                if temporary_path:
                    try:
                        os.unlink(temporary_path)
                    except OSError:
                        pass
            return self._read_unlocked()

    def _read_unlocked(self) -> ResultFileSnapshot:
        try:
            raw_bytes = self.path.read_bytes() if self.path.exists() else b""
        except OSError as exc:
            raise ResultFileError(
                "RESULT_FILE_READ_FAILED", "结果文件读取失败。"
            ) from exc

        try:
            raw_text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ResultFileError(
                "RESULT_FILE_ENCODING", "结果文件不是有效的 UTF-8 文本。"
            ) from exc

        modified_at: str | None = None
        if self.path.exists():
            try:
                modified_at = datetime.fromtimestamp(
                    self.path.stat().st_mtime, tz=timezone.utc
                ).isoformat()
            except OSError as exc:
                raise ResultFileError(
                    "RESULT_FILE_READ_FAILED", "结果文件元数据读取失败。"
                ) from exc

        return ResultFileSnapshot(
            name=self.path.name,
            version=hashlib.sha256(raw_bytes).hexdigest(),
            modified_at=modified_at,
            size=len(raw_bytes),
            newline="\r\n" if "\r\n" in raw_text else "\n",
            raw_text=raw_text,
            lines=_parse_lines(raw_text),
        )


def _parse_lines(raw_text: str) -> list[ResultLine]:
    if not raw_text:
        return []
    lines: list[ResultLine] = []
    for line_no, raw in enumerate(raw_text.splitlines(), start=1):
        fields = raw.split("----") if "----" in raw else [raw]
        if not raw:
            kind = "empty"
        elif line_no == 1 and _looks_like_header(fields):
            kind = "header"
        elif len(fields) >= 4:
            kind = "credential"
        elif len(fields) >= 2:
            kind = "record"
        else:
            kind = "raw"
        lines.append(
            ResultLine(
                line_no=line_no,
                raw=raw,
                fields=fields,
                kind=kind,
            )
        )
    return lines


def _looks_like_header(fields: list[str]) -> bool:
    normalized = {field.strip().lower().replace(" ", "_") for field in fields}
    has_account = bool(normalized & {"account", "email", "email_address"})
    has_password = any("password" in field or field in {"pwd", "pass"} for field in normalized)
    return has_account and has_password
