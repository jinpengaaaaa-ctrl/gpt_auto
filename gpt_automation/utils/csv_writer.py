"""
结果实时写入与 evidence 目录管理 (Feature #18).

Wraps the boilerplate of:
  * creating the output file with the correct header on first run
  * appending rows in ---- separated format with explicit flush
  * writing error / evidence rows without losing previous successes
  * producing a timestamped screenshot path

Used by `orchestrator.py` so the in-flight progress is preserved even
if the script crashes mid-batch.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime

import config
from gpt_automation.utils.redaction import account_fingerprint

logger = logging.getLogger(__name__)

# Output format is an index only; credentials are stored separately.
SEP: str = "----"
# Persisted rows contain only a session reference, an irreversible account
# fingerprint, and a status token.
OUTPUT_HEADER = "session_id----account_fingerprint----status"


def ensure_output_csv() -> None:
    """Create the output file with header if it does not exist yet."""
    if os.path.exists(config.OUTPUT_CSV):
        return
    try:
        with open(config.OUTPUT_CSV, mode="w", encoding="utf-8") as fh:
            fh.write(OUTPUT_HEADER + "\n")
        logger.info("csv_writer: created output %s", config.OUTPUT_CSV)
    except OSError as exc:
        logger.warning("csv_writer: failed to create output file: %s", exc)


def ensure_dirs() -> None:
    """Make sure profile / evidence / log directories exist."""
    for directory in (
        config.PROFILE_DIR,
        config.EVIDENCE_DIR,
        os.path.dirname(os.path.abspath(config.OUTPUT_CSV)) or ".",
        os.path.dirname(os.path.abspath(config.IP_HISTORY_FILE)) or ".",
    ):
        if directory and not os.path.exists(directory):
            try:
                os.makedirs(directory, exist_ok=True)
            except OSError as exc:
                logger.warning("csv_writer: mkdir %s failed: %s", directory, exc)


def append_result(account: str, status_token: str, session_id: str = "") -> bool:
    """Append a redacted result row, treating identical writes as a no-op."""
    ensure_output_csv()
    safe_session = _safe_field(session_id)
    safe_status = _safe_field(status_token)
    line = SEP.join([safe_session, account_fingerprint(account), safe_status])
    try:
        try:
            with open(config.OUTPUT_CSV, mode="r", encoding="utf-8") as fh:
                if line in {row.strip() for row in fh if row.strip()}:
                    return True
        except OSError:
            pass
        with open(config.OUTPUT_CSV, mode="a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        return True
    except OSError as exc:
        logger.error(
            "csv_writer: failed to append account %s: %s",
            account_fingerprint(account)[:12],
            exc,
        )
        return False


def _safe_field(value: str) -> str:
    """Keep the line-oriented index safe from control characters."""
    return str(value or "").replace("\r", " ").replace("\n", " ").replace(SEP, "_")


def evidence_path(email: str, kind: str, ext: str = "png") -> str:
    """Return a unique evidence file path for a given account and kind.

    The account is represented by a short fingerprint so evidence can be
    shared for diagnosis without exposing the email address.
    """
    fingerprint = account_fingerprint(email)[:12]
    ts: str = datetime.now().strftime("%Y%m%dT%H%M%S")
    return os.path.join(
        config.EVIDENCE_DIR, f"{kind}_{fingerprint}_{ts}.{ext}",
    )


def write_status(
    email: str,
    status_token: str,
    email_pwd: str,
    session_id: str = "",
) -> None:
    """失败状态不再写入结果文件（两段式管理：结果文件只装已开通账户）。

    失败状态由 registered_accounts 库存与日志承载；保留签名与调用点
    以最小化 account_flow 改动。ACCOUNT_READY 的写入已由 pending 接管。
    """
    del email, status_token, email_pwd, session_id


# Failed account format: 帐号----状态----错误信息----失败阶段----卡号末四位
FAILED_OUTPUT_FILE: str = os.path.join(os.path.dirname(config.OUTPUT_CSV) or "output", "failed_accounts.csv")
FAILED_HEADER: str = "account_fingerprint----status----error----stage----card_last4"


def ensure_failed_csv() -> None:
    """Create failed_accounts.csv with header if missing."""
    if os.path.exists(FAILED_OUTPUT_FILE):
        return
    try:
        os.makedirs(os.path.dirname(FAILED_OUTPUT_FILE), exist_ok=True)
        with open(FAILED_OUTPUT_FILE, "w", encoding="utf-8") as fh:
            fh.write(FAILED_HEADER + "\n")
        logger.info("csv_writer: created %s", FAILED_OUTPUT_FILE)
    except OSError as exc:
        logger.warning("csv_writer: failed to create failed output: %s", exc)


def write_failed(
    account: str,
    gpt_password: str,
    two_fa_secret: str,
    email_password: str,
    card_no: str = "",
    card_expiry: str = "",
    card_cvc: str = "",
    error_msg: str = "",
    stage: str = "",
) -> bool:
    """Append a failed account record to failed_accounts.csv.

    Args:
        account: Email address
        gpt_password: Status token only; never a password
        two_fa_secret: Ignored for failure records
        email_password: Ignored for failure records
        card_no: Only the last four digits are retained
        card_expiry: Ignored for failure records
        card_cvc: Ignored for failure records
        error_msg: Non-sensitive error description
        stage: Phase/stage where failure occurred
    """
    ensure_failed_csv()
    try:
        safe_card_last4 = (card_no or "")[-4:] if card_no else ""
        line = SEP.join([
            account_fingerprint(account),
            _safe_field(gpt_password),
            _safe_field(error_msg),
            _safe_field(stage),
            safe_card_last4,
        ])
        with open(FAILED_OUTPUT_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        logger.info(
            "csv_writer: failed account recorded: %s (stage=%s)",
            account_fingerprint(account)[:12],
            stage,
        )
        return True
    except OSError as exc:
        logger.error(
            "csv_writer: failed to write failed account %s: %s",
            account_fingerprint(account)[:12],
            exc,
        )
        return False
