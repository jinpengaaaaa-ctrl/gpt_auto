"""
OutlookCodeReader — direct Outlook IMAP OAuth method.

Uses OutlookMailReader for direct IMAP OAuth access.
No Docker, no Flask API needed.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from gpt_automation.verification.outlook_mail_reader import OutlookMailReader
from gpt_automation.utils.redaction import mask_email

logger = logging.getLogger(__name__)


class OutlookCodeReader:
    """Async wrapper for Outlook email verification code reading.

    Uses OutlookMailReader with client_id + refresh_token from email.csv
    to read verification emails through IMAP OAuth.
    """

    def __init__(
        self,
        email_addr: str,
        client_id: str = "",
        refresh_token: str = "",
        *,
        on_token_rotated: Optional[Callable[[str], None]] = None,
    ) -> None:
        """Initialize the OutlookCodeReader.

        Args:
            email_addr: Email address to read from.
            client_id: OAuth2 client ID (from email.csv field[2]).
            refresh_token: OAuth2 refresh token (from email.csv field[3]).
        """
        self.email_addr = email_addr
        self.account_label = mask_email(email_addr)
        self.client_id = client_id
        self.refresh_token = refresh_token
        self._on_token_rotated = on_token_rotated
        self._reader = OutlookMailReader(
            email_addr=email_addr,
            client_id=client_id,
            refresh_token=refresh_token,
            on_token_rotated=on_token_rotated,
        )

    @property
    def latest_refresh_token(self) -> str:
        """Return the reader's current token after any rotation."""
        if self._reader is not None:
            return self._reader.refresh_token or self.refresh_token
        return self.refresh_token

    @property
    def last_failure_reason(self) -> Optional[str]:
        """Expose the underlying reader's triage reason, if any."""
        if self._reader is not None:
            return getattr(self._reader, "last_failure_reason", None)
        return None

    async def close(self) -> None:
        """Close the underlying mail reader (no-op for current implementation)."""
        self._reader = None
        logger.debug("OutlookCodeReader closed for %s", self.account_label)

    async def get_verification_code(
        self,
        timeout: Optional[float] = None,
        poll_interval: Optional[float] = None,
        min_received_at: Optional[float] = None,
    ) -> Optional[str]:
        """Retrieve the ChatGPT verification code from Outlook inbox.

        Args:
            timeout: Maximum time to wait for code (seconds). Defaults to 120.
            poll_interval: Time between poll attempts (seconds). Defaults to 5.

        Returns:
            6-digit verification code if found, None otherwise.
        """
        if not self.client_id or not self.refresh_token:
            logger.warning(
                "OutlookCodeReader: client_id or refresh_token not provided for %s",
                self.account_label,
            )
            return None

        timeout_val = timeout if timeout is not None else 120.0
        poll_val = poll_interval if poll_interval is not None else 5.0

        return await self._reader.get_verification_code(
            timeout=timeout_val,
            poll_interval=poll_val,
            min_received_at=min_received_at,
        )
