"""结果文件对账：以 registered_accounts 库存为事实源，重建文件投影。

数据库状态（READY / CHECKOUT_DONE）是唯一事实源；本模块把当前库存
投影回两个结果文件，用于文件写入失败后的恢复（幂等，可重复运行）。
"""

from __future__ import annotations

import logging
from pathlib import Path

from gpt_automation.results.final_file import FinalResultsFile
from gpt_automation.results.pending_file import PendingAccountsFile

logger = logging.getLogger(__name__)


def reconcile(
    *,
    inventory,
    pending_path: str | Path,
    final_path: str | Path,
) -> dict:
    """按库存状态同步两个结果文件。

    - READY 账户：确保出现在 pending（幂等追加）。
    - CHECKOUT_DONE 账户：确保出现在 final，并从 pending 移出。

    Args:
        inventory: RegisteredAccountService 实例（提供 list_accounts）。

    Returns:
        {"pending_added": int, "final_added": int, "pending_removed": int}
    """
    pending = PendingAccountsFile(pending_path)
    final = FinalResultsFile(final_path)
    pending_added = 0
    final_added = 0
    removed = 0
    for view in inventory.list_accounts(statuses=("READY",)):
        if pending.append(
            email=view.email,
            chatgpt_password=view.chatgpt_password,
            totp_secret=view.totp_secret,
            email_password=view.email_password,
        ):
            pending_added += 1
    for view in inventory.list_accounts(statuses=("CHECKOUT_DONE",)):
        if final.append(
            email=view.email,
            chatgpt_password=view.chatgpt_password,
            totp_secret=view.totp_secret,
            email_password=view.email_password,
        ):
            final_added += 1
        if pending.remove(email=view.email):
            removed += 1
    logger.info(
        "reconcile: pending_added=%d final_added=%d pending_removed=%d",
        pending_added,
        final_added,
        removed,
    )
    return {
        "pending_added": pending_added,
        "final_added": final_added,
        "pending_removed": removed,
    }
