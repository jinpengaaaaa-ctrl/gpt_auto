"""开通失败待处理账户文件：明文四列 + 原因 + 处置方式（幂等，原子重写）。"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

from gpt_automation.results.pending_file import (
    SEP as HELD_SEP,
    _safe_field,
    twofa_link,
)
from gpt_automation.utils.redaction import account_fingerprint

logger = logging.getLogger(__name__)

HELD_HEADER = "帐号----密码----2FA----邮箱密码----HOLD_REASON----DISPOSITION"


class HeldAccountsFile:
    """行式文件：表头 + 邮箱----密码----2FA----邮箱密码----原因----处置。"""

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

    def append(
        self,
        *,
        email: str,
        chatgpt_password: str,
        totp_secret: str,
        email_password: str,
        hold_reason: str = "NEEDS_OPERATOR",
        disposition: str = "REVIEW",
    ) -> bool:
        lines = self._read_lines()
        if not lines:
            lines = [HELD_HEADER]
        safe_email = _safe_field(email)
        clean_totp = twofa_link(totp_secret) if not str(totp_secret or "").startswith("https://2fa.run/2fa/") else str(totp_secret)
        new_row = HELD_SEP.join([
            safe_email,
            _safe_field(chatgpt_password),
            _safe_field(clean_totp),
            _safe_field(email_password),
            _safe_field(hold_reason or "NEEDS_OPERATOR"),
            _safe_field(disposition or "REVIEW"),
        ])
        updated = False
        for index in range(1, len(lines)):
            if lines[index].split(HELD_SEP, 1)[0] == safe_email:
                lines[index] = new_row
                updated = True
                break
        if not updated:
            lines.append(new_row)
        self._write_lines(lines)
        logger.info("held: recorded %s (reason=%s, disposition=%s)", account_fingerprint(email), hold_reason, disposition)
        return True

    def remove(self, *, email: str) -> bool:
        lines = self._read_lines()
        safe_email = _safe_field(email)
        kept = [ln for ln in lines if ln == HELD_HEADER or ln.split(HELD_SEP, 1)[0] != safe_email]
        if len(kept) == len(lines):
            return False
        self._write_lines(kept if kept else [HELD_HEADER])
        logger.info("held: removed %s", account_fingerprint(email))
        return True

    def contains(self, email: str) -> bool:
        target = str(email or "").strip().lower()
        return target in [e.lower() for e in self.list_emails()]

    def list_emails(self) -> list[str]:
        return [ln.split(HELD_SEP, 1)[0] for ln in self._read_lines()[1:] if ln != HELD_HEADER]
