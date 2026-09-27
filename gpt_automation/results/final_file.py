"""最终结果文件：已开通账户（明文四列，幂等追加）。"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

from gpt_automation.results.pending_file import (
    HEADER,
    SEP,
    _safe_field,
    twofa_link,
)
from gpt_automation.utils.redaction import account_fingerprint

logger = logging.getLogger(__name__)


class FinalResultsFile:
    """行式文件：表头 + 邮箱----密码----2FA----邮箱密码，同一邮箱只出现一次。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _read_lines(self) -> list[str]:
        if not self.path.exists():
            return []
        return [ln for ln in self.path.read_text(encoding="utf-8").splitlines() if ln.strip()]

    def _write_lines(self, lines: list[str]) -> None:
        fd, tmp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write("\n".join(lines) + "\n")
            os.replace(tmp_name, self.path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def append(self, *, email: str, chatgpt_password: str, totp_secret: str, email_password: str) -> bool:
        lines = self._read_lines()
        if not lines:
            lines = [HEADER]
        safe_email = _safe_field(email)
        if any(ln.split(SEP, 1)[0] == safe_email for ln in lines[1:]):
            logger.info("final: %s already present; skipping", account_fingerprint(email))
            return True
        lines.append(
            SEP.join(
                [
                    _safe_field(email),
                    _safe_field(chatgpt_password),
                    _safe_field(twofa_link(totp_secret)),
                    _safe_field(email_password),
                ]
            )
        )
        self._write_lines(lines)
        logger.info("final: appended %s", account_fingerprint(email))
        return True

    def list_emails(self) -> list[str]:
        return [ln.split(SEP, 1)[0] for ln in self._read_lines()[1:] if ln != HEADER]
