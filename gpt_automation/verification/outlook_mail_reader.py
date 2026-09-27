"""
outlook_mail_reader.py — Outlook email reader via IMAP XOAUTH2 only.

Uses IMAP XOAUTH2 authentication to read Outlook/Hotmail inboxes.
This works with tokens that have only IMAP.AccessAsUser.All scope,
which is the most common scope granted to Outlook OAuth tokens.

Email credential format (from email.csv):
    email,email_pwd,client_id,refresh_token
    field[2] = client_id (uuid)
    field[3] = refresh_token
"""

from __future__ import annotations

import asyncio
import email
import imaplib
import logging
import re
import time
from email.header import decode_header
from email.utils import parsedate_to_datetime
from typing import Callable, Optional

from gpt_automation.utils.exceptions import SAFE_EXCEPTIONS
from gpt_automation.utils.redaction import mask_email

logger = logging.getLogger(__name__)

# Regular expression to extract 6-digit verification code
CODE_RE = re.compile(r"\b(\d{6})\b")

# ChatGPT verification email subjects
VERIFICATION_SUBJECTS = ["chatgpt", "verification", "verify", "confirm", "openai"]

# IMAP servers to try, paired with their token refresh endpoint style
IMAP_SERVERS = [
    ("outlook.office365.com", "old"),
    ("outlook.live.com", "new"),
]


def _parse_date_to_ts(date_str: str) -> Optional[float]:
    """Parse an RFC 2822 Date header into an epoch timestamp (UTC)."""
    if not date_str:
        return None
    try:
        parsed = parsedate_to_datetime(date_str)
        if parsed is not None:
            return parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        pass
    return None


class OutlookMailReader:
    """Read Outlook emails using IMAP XOAUTH2 only.

    Uses client_id + refresh_token to authenticate with Microsoft's OAuth2
    endpoints and read verification emails from ChatGPT/OpenAI.

    IMAP XOAUTH2 is used because Outlook OAuth tokens commonly provide
    the IMAP.AccessAsUser.All scope.
    """

    def __init__(
        self,
        email_addr: str,
        client_id: str,
        refresh_token: str,
        *,
        on_token_rotated: Optional[Callable[[str], None]] = None,
    ) -> None:
        """Initialize the OutlookMailReader.

        Args:
            email_addr: Email address to read from.
            client_id: OAuth2 client ID (from email.csv field[2]).
            refresh_token: OAuth2 refresh token (from email.csv field[3]).
        """
        self.email_addr = email_addr
        self.account_label = mask_email(email_addr)
        self.client_id = client_id
        self.refresh_token = refresh_token
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self._token_method: Optional[str] = None  # Track which method worked
        self._on_token_rotated = on_token_rotated
        self.last_failure_reason: Optional[str] = None

    def _notify_token_rotated(self, new_refresh_token: str) -> None:
        """Best-effort callback: persistence failure must never block polling."""
        if self._on_token_rotated is None:
            return
        try:
            self._on_token_rotated(new_refresh_token)
        except Exception as exc:  # noqa: BLE001 - persistence is best-effort
            logger.error(
                "refresh token rotation callback failed for %s: %s",
                self.account_label,
                exc,
            )

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------

    async def _ensure_token(self) -> str:
        """Refresh and return a valid access token.

        Tries token refresh methods in order:
        1. login.live.com (old IMAP endpoint, no scope param)
        2. consumers endpoint (new IMAP, with IMAP scope)

        Returns the access_token from the first successful attempt.
        Caches token for 1 hour (with 5 minute safety buffer).

        Returns:
            Valid access token string.

        Raises:
            Exception: If all token refresh methods fail.
        """
        # Check if current token is still valid (with 5 min buffer)
        if self._access_token and time.time() < (self._token_expires_at - 300):
            return self._access_token

        errors = []

        # Method 1: Old IMAP endpoint (login.live.com)
        try:
            token = await self._refresh_via_live()
            if token:
                self._cache_token(token, "old_imap")
                return token
        except SAFE_EXCEPTIONS as exc:
            errors.append(f"live.com: {exc}")
        except Exception as exc:
            errors.append(f"live.com: {exc}")
            logger.debug("login.live.com token refresh failed: %s", exc)

        # Method 2: New IMAP endpoint (consumers tenant)
        try:
            token = await self._refresh_via_consumers()
            if token:
                self._cache_token(token, "new_imap")
                return token
        except SAFE_EXCEPTIONS as exc:
            errors.append(f"consumers: {exc}")
        except Exception as exc:
            errors.append(f"consumers: {exc}")
            logger.debug("consumers token refresh failed: %s", exc)

        raise RuntimeError(
            f"All token refresh methods failed for {self.account_label}: "
            f"{'; '.join(errors)}"
        )

    async def _refresh_via_live(self) -> Optional[str]:
        """Refresh token via login.live.com (old IMAP endpoint).

        This endpoint does NOT require a scope parameter and is compatible
        with tokens that have IMAP.AccessAsUser.All scope.

        Returns:
            Access token if successful, None otherwise.
        """
        import httpx

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://login.live.com/oauth20_token.srf",
                data={
                    "client_id": self.client_id,
                    "grant_type": "refresh_token",
                    "refresh_token": self.refresh_token,
                },
            )

        if resp.status_code != 200:
            logger.debug("login.live.com returned %d", resp.status_code)
            return None

        result = resp.json()
        token = result.get("access_token")
        if token:
            # Update refresh token if a new one is provided
            new_rt = result.get("refresh_token")
            if new_rt:
                self.refresh_token = new_rt
                self._notify_token_rotated(new_rt)
        return token

    async def _refresh_via_consumers(self) -> Optional[str]:
        """Refresh token via consumers tenant (new IMAP endpoint).

        Uses the IMAP.AccessAsUser.All scope which is the correct scope
        for IMAP XOAUTH2 access.

        Returns:
            Access token if successful, None otherwise.
        """
        import httpx

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://login.microsoftonline.com/consumers/oauth2/v2.0/token",
                data={
                    "client_id": self.client_id,
                    "grant_type": "refresh_token",
                    "refresh_token": self.refresh_token,
                    "scope": "https://outlook.office.com/IMAP.AccessAsUser.All offline_access",
                },
            )

        if resp.status_code != 200:
            logger.debug("consumers endpoint returned %d", resp.status_code)
            return None

        result = resp.json()
        token = result.get("access_token")
        if token:
            new_rt = result.get("refresh_token")
            if new_rt:
                self.refresh_token = new_rt
                self._notify_token_rotated(new_rt)
        return token

    def _cache_token(self, token: str, method: str) -> None:
        """Cache the access token with 1-hour expiry.

        Args:
            token: The access token to cache.
            method: Which refresh method succeeded ("old_imap", "new_imap", "graph").
        """
        self._access_token = token
        self._token_expires_at = time.time() + 3600
        self._token_method = method

    # ------------------------------------------------------------------
    # Primary: IMAP XOAUTH2 fetch
    # ------------------------------------------------------------------

    async def _fetch_via_imap(self, access_token: str, top: int = 10) -> list:
        """Fetch latest messages via IMAP XOAUTH2.

        Tries IMAP servers in order: outlook.office365.com, outlook.live.com.

        Args:
            access_token: OAuth2 access token for XOAUTH2 authentication.
            top: Maximum number of recent messages to fetch.

        Returns:
            List of message dicts with keys: id, subject, bodyPreview,
            receivedDateTime, from. Empty list if all IMAP servers fail.
        """
        saw_rate_limit = False
        for server, _method in IMAP_SERVERS:
            try:
                messages = await asyncio.to_thread(
                    self._imap_fetch_sync, server, access_token, top
                )
                if messages is not None:
                    logger.debug("IMAP fetch succeeded via %s: %d messages", server, len(messages))
                    self.last_failure_reason = None
                    return messages
            except SAFE_EXCEPTIONS as exc:
                logger.warning("IMAP server %s failed: %s", server, exc)
            except Exception as exc:
                err_msg = str(exc).lower()
                logger.warning("IMAP server %s unexpected error: %s", server, exc)
                if "not connected" in err_msg or "command argument error" in err_msg:
                    saw_rate_limit = True
                    logger.warning(
                        "IMAP rate-limited for %s on %s, waiting 60s...",
                        self.account_label, server,
                    )
                    await asyncio.sleep(60)

        self.last_failure_reason = (
            "IMAP_RATE_LIMITED" if saw_rate_limit else "IMAP_AUTH_FAILED"
        )
        logger.warning(
            "All IMAP servers failed for %s",
            self.account_label,
        )
        return []

    def _imap_fetch_sync(self, server: str, access_token: str, top: int) -> list:
        """Synchronous IMAP fetch (runs in thread pool via asyncio.to_thread).

        Args:
            server: IMAP server hostname (e.g., outlook.office365.com).
            access_token: OAuth2 access token.
            top: Maximum number of recent messages to fetch.

        Returns:
            List of normalized message dicts.
        """
        conn = imaplib.IMAP4_SSL(server, 993, timeout=45)
        try:
            # XOAUTH2 authentication
            auth_string = f"user={self.email_addr}\x01auth=Bearer {access_token}\x01\x01"
            conn.authenticate("XOAUTH2", lambda x: auth_string.encode())

            # Select inbox
            status, _data = conn.select("INBOX")
            if status != "OK":
                logger.debug("IMAP SELECT INBOX failed on %s: %s", server, status)
                return []

            # Search for all messages
            status, messages = conn.search(None, "ALL")
            if status != "OK":
                logger.debug("IMAP SEARCH failed on %s: %s", server, status)
                return []

            ids = messages[0].split()
            if not ids:
                logger.debug("IMAP inbox empty on %s", server)
                return []

            # Get the most recent N messages (newest first)
            recent_ids = ids[-top:][::-1]

            results = []
            for mid in recent_ids:
                status, data = conn.fetch(mid, "(RFC822)")
                if status != "OK":
                    continue

                # data is a list of (header, content) tuples; extract the raw email
                raw_email = None
                for part in data:
                    if isinstance(part, tuple):
                        raw_email = part[1]
                        break

                if raw_email is None:
                    continue

                msg = email.message_from_bytes(raw_email)
                msg_dict = self._normalize_imap_message(mid.decode() if isinstance(mid, bytes) else mid, msg)
                if msg_dict:
                    results.append(msg_dict)

            return results

        finally:
            try:
                conn.logout()
            except Exception:
                pass

    def _normalize_imap_message(self, msg_id: str, msg: email.message.Message) -> dict:
        """Normalize an IMAP email.message.Message into the standard dict format.

        Args:
            msg_id: The IMAP message ID (string).
            msg: The parsed email.message.Message object.

        Returns:
            Dict with keys: id, subject, bodyPreview, receivedDateTime, from.
        """
        subject = _decode_email_subject(msg.get("Subject", ""))
        body = _extract_text_body(msg)
        date_str = msg.get("Date", "")
        sender = msg.get("From", "")

        # Extract sender email address from "Name <email>" format
        sender_email = _extract_email_address(sender)

        return {
            "id": msg_id,
            "subject": subject,
            "bodyPreview": body if body else "",
            "receivedDateTime": date_str,
            "received_ts": _parse_date_to_ts(date_str),
            "from": {"emailAddress": {"address": sender_email}},
        }

    # ------------------------------------------------------------------
    # Unified fetch
    # ------------------------------------------------------------------

    async def _fetch_messages(self) -> list:
        """Fetch messages through IMAP XOAUTH2 only.

        Returns:
            List of normalized message dicts, empty when IMAP has no matches.
        """
        try:
            access_token = await self._ensure_token()
        except Exception:  # noqa: BLE001 - triage reason for the caller
            self.last_failure_reason = "TOKEN_REFRESH_FAILED"
            raise
        return await self._fetch_via_imap(access_token)

    async def _get_latest_message_id(self) -> Optional[str]:
        """Get the ID of the latest message in inbox.

        Returns:
            Message ID if successful, None otherwise.
        """
        try:
            messages = await self._fetch_messages()
            if messages:
                return messages[0].get("id")
        except Exception as exc:
            logger.debug("Failed to get latest message ID: %s", exc)
        return None

    # ------------------------------------------------------------------
    # Polling / public API
    # ------------------------------------------------------------------

    async def get_verification_code(
        self,
        timeout: float = 120.0,
        poll_interval: float = 5.0,
        min_received_at: Optional[float] = None,
    ) -> Optional[str]:
        """Poll Outlook inbox for ChatGPT verification email.

        Uses IMAP XOAUTH2 as the only mailbox access method with adaptive polling
        (2s for the first 30s, then poll_interval thereafter).

        Args:
            timeout: Maximum time to wait for verification code (seconds).
            poll_interval: Time between polling attempts after initial 30s (seconds).

        Returns:
            6-digit verification code if found, None otherwise.
        """
        start_time = time.time()
        deadline = start_time + timeout
        baseline_message_id: Optional[str] = None
        saw_any_messages = False
        saw_verification_mail = False

        while time.time() < deadline:
            try:
                messages = await self._fetch_messages()
                if messages:
                    saw_any_messages = True
                    if baseline_message_id is None:
                        first_msg = messages[0]
                        if not self._is_verification_email(
                            first_msg.get("subject", ""),
                            first_msg.get("bodyPreview", ""),
                        ):
                            baseline_message_id = first_msg.get("id")

                    if any(
                        self._is_verification_email(
                            m.get("subject", ""),
                            m.get("bodyPreview", ""),
                        )
                        for m in messages
                    ):
                        saw_verification_mail = True

                    code = self._find_code_in_messages(
                        messages,
                        baseline_message_id,
                        min_received_at=min_received_at,
                    )
                    if code:
                        logger.info("Verification code found for %s", self.account_label)
                        self.last_failure_reason = None
                        return code

            except SAFE_EXCEPTIONS as exc:
                logger.warning("Poll attempt failed for %s: %s", self.account_label, exc)
            except Exception as exc:
                logger.warning(
                    "Poll attempt unexpected error for %s: %s",
                    self.account_label,
                    exc,
                )

            remaining = deadline - time.time()
            if remaining <= 0:
                break

            elapsed = time.time() - start_time
            current_interval = 2.0 if elapsed < 30.0 else poll_interval
            await asyncio.sleep(min(current_interval, remaining))

        if not saw_any_messages:
            self.last_failure_reason = "MAILBOX_EMPTY"
        elif saw_verification_mail:
            self.last_failure_reason = "CODE_EXTRACT_FAILED"
        else:
            self.last_failure_reason = "CODE_TIMEOUT"
        logger.warning(
            "Verification code timeout for %s after %.1fs (reason=%s)",
            self.account_label,
            timeout,
            self.last_failure_reason,
        )
        return None

    def _find_code_in_messages(
        self,
        messages: list,
        baseline_message_id: Optional[str],
        *,
        min_received_at: Optional[float] = None,
    ) -> Optional[str]:
        """Scan fetched messages for a verification code.

        Args:
            messages: List of message dicts.
            baseline_message_id: If set, skip message with this ID.

        Returns:
            Verification code if found, None otherwise.
        """
        for msg in messages:
            msg_id = msg.get("id", "")
            subject = msg.get("subject", "")
            body_preview = msg.get("bodyPreview", "")

            if baseline_message_id and msg_id == baseline_message_id:
                continue

            if min_received_at is not None:
                received_ts = msg.get("received_ts")
                if received_ts is None or received_ts < min_received_at:
                    continue

            if not self._is_verification_email(subject, body_preview):
                continue

            code = self._extract_code(subject, body_preview)
            if code:
                return code

        return None

    # ------------------------------------------------------------------
    # Verification helpers
    # ------------------------------------------------------------------

    def _is_verification_email(self, subject: str, body_preview: str) -> bool:
        """Check if email is a ChatGPT verification email.

        Args:
            subject: Email subject.
            body_preview: Email body preview.

        Returns:
            True if this is a verification email.
        """
        subject_lower = subject.lower()
        body_lower = body_preview.lower()

        # Check subject for verification keywords
        for keyword in VERIFICATION_SUBJECTS:
            if keyword in subject_lower:
                return True

        # Check body for verification keywords
        for keyword in VERIFICATION_SUBJECTS:
            if keyword in body_lower:
                return True

        return False

    def _extract_code(self, subject: str, body: str) -> Optional[str]:
        """Extract 6-digit verification code from email.

        ChatGPT verification emails contain multiple 6-digit numbers
        (tracking codes, style IDs, etc.). The actual code always sits
        inside a Menlo-styled paragraph right after the instruction text
        「この一時検証コードを入力して続行してください」.

        Extraction priority:
          1. Menlo-styled paragraph (font-family: Menlo...)
          2. After verification instruction text
          3. Subject line
          4. Fallback: last 6-digit number in body

        Args:
            subject: Email subject.
            body: Email body or body preview (HTML).

        Returns:
            6-digit code if found, None otherwise.
        """
        # ── 1. Menlo-styled paragraph ─────────────────────────
        m = re.search(
            r'font-family:\s*["\']?Menlo[^>]*>\s*(\d{6})\s*<',
            body, re.DOTALL | re.IGNORECASE,
        )
        if m and self._is_valid_code(m.group(1)):
            return m.group(1)

        # ── 2. After instruction text ─────────────────────────
        m = re.search(
            r'(?:一時検証コード|認証コード|verification code|enter the code).{0,200}?(\d{6})',
            body, re.DOTALL | re.IGNORECASE,
        )
        if m and self._is_valid_code(m.group(1)):
            return m.group(1)

        # ── 3. Subject line ──────────────────────────────────
        m = CODE_RE.search(subject)
        if m and self._is_valid_code(m.group(1)):
            return m.group(1)

        # ── 4. Last 6-digit in body (fallback) ────────────────
        codes = CODE_RE.findall(body)
        for code in reversed(codes):
            if self._is_valid_code(code):
                return code

        return None

    @staticmethod
    def _is_valid_code(code: str) -> bool:
        """Check if a 6-digit code is valid (not 000000, not all same digits).

        Args:
            code: 6-digit code string.

        Returns:
            True if code appears valid.
        """
        if code == "000000":
            return False
        if all(c == code[0] for c in code):
            return False
        return True


# ------------------------------------------------------------------
# Email parsing utilities (module-level, reusable)
# ------------------------------------------------------------------


def _decode_email_subject(raw_subject: str) -> str:
    """Decode an email subject from RFC 2047 encoded format.

    Args:
        raw_subject: Raw email subject string (possibly encoded).

    Returns:
        Decoded subject string.
    """
    if not raw_subject:
        return ""
    try:
        parts = decode_header(raw_subject)
    except Exception:
        return str(raw_subject)

    result = ""
    for part, charset in parts:
        if isinstance(part, bytes):
            result += part.decode(charset if charset else "utf-8", errors="replace")
        else:
            result += str(part)
    return result


def _extract_text_body(msg: email.message.Message) -> str:
    """Extract the text body from an email.message.Message.

    Prefers text/plain over text/html.

    Args:
        msg: Parsed email message.

    Returns:
        Extracted text body as a string, empty string if nothing found.
    """
    if msg.is_multipart():
        # First pass: look for text/plain
        for part in msg.walk():
            content_type = part.get_content_type()
            if content_type == "text/plain":
                charset = part.get_content_charset() or "utf-8"
                try:
                    payload = part.get_payload(decode=True)
                    if payload:
                        return payload.decode(charset, errors="replace")
                except Exception:
                    pass
        # Second pass: look for text/html
        for part in msg.walk():
            content_type = part.get_content_type()
            if content_type == "text/html":
                charset = part.get_content_charset() or "utf-8"
                try:
                    payload = part.get_payload(decode=True)
                    if payload:
                        return payload.decode(charset, errors="replace")
                except Exception:
                    pass
    else:
        charset = msg.get_content_charset() or "utf-8"
        try:
            payload = msg.get_payload(decode=True)
            if payload:
                return payload.decode(charset, errors="replace")
        except Exception:
            pass

    return ""


def _extract_email_address(from_header: str) -> str:
    """Extract the bare email address from a From header.

    Handles formats like:
        "Name <user@example.com>"
        "user@example.com"

    Args:
        from_header: The From header value.

    Returns:
        The bare email address, or the original string if parsing fails.
    """
    if not from_header:
        return ""

    # Simple extraction: find content between < and >
    if "<" in from_header and ">" in from_header:
        start = from_header.rfind("<") + 1
        end = from_header.rfind(">")
        if start < end:
            return from_header[start:end]

    # No angle brackets, assume it's already a bare email
    return from_header.strip()
