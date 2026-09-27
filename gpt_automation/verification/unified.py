"""
UnifiedCodeReader — Outlook email reader via OAuth/XOAUTH2.

Behaviour:
    Uses OutlookCodeReader via Outlook OAuth/XOAUTH2 directly to fetch email verification codes.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from gpt_automation.verification.outlook import OutlookCodeReader
from gpt_automation.utils.redaction import mask_email

logger = logging.getLogger(__name__)


class UnifiedCodeReader:
    """Email verification reader using Outlook OAuth/XOAUTH2 directly."""

    def __init__(
        self,
        email_addr: str,
        email_pwd: str = "",
        client_id: str = "",
        refresh_token: str = "",
        *,
        on_token_rotated: Optional[Callable[[str], None]] = None,
    ) -> None:
        """Initialize the UnifiedCodeReader.

        Args:
            email_addr: Email address for reading verification codes.
            email_pwd: Email password (retained for signature compatibility).
            client_id: OAuth2 client ID (for Outlook method).
            refresh_token: OAuth2 refresh token (for Outlook method).
        """
        self.email_addr = email_addr
        self.account_label = mask_email(email_addr)
        self.email_pwd = email_pwd
        self.client_id = client_id
        self.refresh_token = refresh_token
        self._on_token_rotated = on_token_rotated
        self.last_failure_reason: Optional[str] = None

        # Initialize Outlook reader if credentials are provided
        if self.client_id and self.refresh_token:
            self.outlook = OutlookCodeReader(
                email_addr,
                client_id=client_id,
                refresh_token=refresh_token,
                on_token_rotated=on_token_rotated,
            )
            self._outlook_enabled = True
        else:
            self.outlook = None
            self._outlook_enabled = False

    @property
    def latest_refresh_token(self) -> str:
        """Return the current refresh token after any OAuth rotation."""
        if self.outlook is not None:
            return self.outlook.latest_refresh_token or self.refresh_token
        return self.refresh_token

    async def close(self) -> None:
        """Close reader and release resources."""
        if self.outlook is not None:
            try:
                await self.outlook.close()
            except Exception:
                pass

    async def get_verification_code(
        self,
        timeout: Optional[float] = None,
        poll_interval: Optional[float] = None,
        min_received_at: Optional[float] = None,
    ) -> Optional[str]:
        """Retrieve verification code via Outlook OAuth/XOAUTH2.

        Args:
            timeout: Maximum time to wait for code (seconds).
            poll_interval: Time between poll attempts (seconds).

        Returns:
            6-digit verification code if found, None otherwise.
        """
        if self._outlook_enabled and self.outlook is not None:
            logger.info("unified: trying Outlook OAuth for %s", self.account_label)
            try:
                code = await self.outlook.get_verification_code(
                    timeout=timeout,
                    poll_interval=poll_interval,
                    min_received_at=min_received_at,
                )
                if code:
                    self.last_failure_reason = None
                    logger.info("unified: Outlook OAuth succeeded for %s", self.account_label)
                    return code
                self.last_failure_reason = getattr(
                    self.outlook,
                    "last_failure_reason",
                    None,
                )
            except Exception as exc:
                self.last_failure_reason = getattr(
                    self.outlook,
                    "last_failure_reason",
                    None,
                )
                logger.warning("unified: Outlook OAuth error (%s): %s", self.account_label, exc)

        logger.warning("unified: Outlook verification failed or not configured for %s", self.account_label)
        return None
