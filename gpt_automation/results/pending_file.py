"""中间产物文件：已创建、未开通的账户（明文四列，幂等，原子重写）。"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

from gpt_automation.utils.redaction import account_fingerprint

logger = logging.getLogger(__name__)

SEP = "----"
HEADER = "帐号----密码----2FA----邮箱密码"
TWOFA_PREFIX = "https://2fa.run/2fa/"
PASSWORDLESS_PLACEHOLDER = "-"


def twofa_link(totp_secret: str) -> str:
    """2FA 一键导入链接：前缀 + TOTP secret（用户确认规则）。"""
    return f"{TWOFA_PREFIX}{str(totp_secret or '').strip()}"


def _safe_field(value: str) -> str:
    return str(value or "").replace("\r", " ").replace("\n", " ").replace(SEP, "_")


class PendingCheckoutCandidate:
    """One complete, checkout-ready row from accounts_pending.txt."""

    __slots__ = ("email", "chatgpt_password", "totp_secret", "email_password", "line_no")

    def __init__(
        self,
        *,
        email: str,
        chatgpt_password: str,
        totp_secret: str,
        email_password: str,
        line_no: int,
    ) -> None:
        self.email = email
        self.chatgpt_password = chatgpt_password
        self.totp_secret = totp_secret
        self.email_password = email_password
        self.line_no = line_no


class PendingAccountsFile:
    """行式文件：表头 + 邮箱----密码----2FA----邮箱密码。"""

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
            logger.info("pending: %s already present; skipping", account_fingerprint(email))
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
        logger.info("pending: appended %s", account_fingerprint(email))
        return True

    def append_passwordless(self, *, email: str, email_password: str) -> bool:
        """Append a passwordless account (direct email-verification route).

        Password and 2FA columns use the literal ``-`` placeholder because
        the account has neither; the email password stays for mailbox
        recovery.  Idempotent and atomically rewritten like append().
        """
        lines = self._read_lines()
        if not lines:
            lines = [HEADER]
        safe_email = _safe_field(email)
        if any(ln.split(SEP, 1)[0] == safe_email for ln in lines[1:]):
            logger.info("pending: %s already present; skipping", account_fingerprint(email))
            return True
        lines.append(
            SEP.join(
                [
                    _safe_field(email),
                    PASSWORDLESS_PLACEHOLDER,
                    PASSWORDLESS_PLACEHOLDER,
                    _safe_field(email_password),
                ]
            )
        )
        self._write_lines(lines)
        logger.info("pending: appended passwordless %s", account_fingerprint(email))
        return True

    def remove(self, *, email: str) -> bool:
        lines = self._read_lines()
        safe_email = _safe_field(email)
        kept = [ln for ln in lines if ln == HEADER or ln.split(SEP, 1)[0] != safe_email]
        if len(kept) == len(lines):
            return False
        self._write_lines(kept if kept else [HEADER])
        logger.info("pending: removed %s", account_fingerprint(email))
        return True

    def list_emails(self) -> list[str]:
        return [ln.split(SEP, 1)[0] for ln in self._read_lines()[1:] if ln != HEADER]

    def list_candidates(self) -> list["PendingCheckoutCandidate"]:
        """Return valid checkout candidates from the pending file.

        Filters out malformed lines and empty passwords, deduplicates by
        email preserving the first seen line number.
        """
        candidates: list[PendingCheckoutCandidate] = []
        seen_emails: set[str] = set()
        for line_no, raw_line in enumerate(self._read_lines(), start=1):
            line = raw_line.strip()
            if not line or line == HEADER or SEP not in line:
                continue
            parts = line.split(SEP)
            if len(parts) < 4:
                continue
            email = parts[0].strip()
            chatgpt_password = parts[1].strip()
            totp_raw = parts[2].strip()
            email_password = parts[3].strip()
            if (
                not email
                or not chatgpt_password
                or chatgpt_password == PASSWORDLESS_PLACEHOLDER
                or not totp_raw
                or totp_raw == PASSWORDLESS_PLACEHOLDER
            ):
                continue
            totp_secret = totp_raw.removeprefix(TWOFA_PREFIX).strip()
            if not totp_secret:
                continue
            if email in seen_emails:
                continue
            seen_emails.add(email)
            candidates.append(
                PendingCheckoutCandidate(
                    email=email,
                    chatgpt_password=chatgpt_password,
                    totp_secret=totp_secret,
                    email_password=email_password,
                    line_no=line_no,
                )
            )
        return candidates

    def get_candidate(self, email: str) -> "PendingCheckoutCandidate | None":
        email_key = str(email or "").strip().lower()
        for cand in self.list_candidates():
            if cand.email.lower() == email_key:
                return cand
        return None
