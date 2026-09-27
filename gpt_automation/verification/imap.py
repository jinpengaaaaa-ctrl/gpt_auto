"""
IMAP Email Verification Code Auto-Reader.

Supports 15+ email providers with automatic IMAP server detection,
async polling for verification codes, and multi-byte subject decoding.
"""

import imaplib
import email
import re
import asyncio
import time
from email.header import decode_header
from typing import Optional, Dict

VERIFICATION_SUBJECTS = [
    "verify your email", "verify email", "email verification",
    "\u30e1\u30fc\u30eb\u30a2\u30c9\u30ec\u30b9\u3092\u78ba\u8a8d",
    "\u78ba\u8a8d\u30b3\u30fc\u30c9", "verification code", "security code",
    "\u30bb\u30ad\u30e5\u30ea\u30c6\u30a3\u30b3\u30fc\u30c9", "one-time code",
    "\u30ef\u30f3\u30bf\u30a4\u30e0\u30b3\u30fc\u30c9", "login code",
    "\u30ed\u30b0\u30a4\u30f3\u30b3\u30fc\u30c9", "authentication code",
    "\u8a8d\u8a3c\u30b3\u30fc\u30c9", "your code is",
    "\u30b3\u30fc\u30c9\u306f", "sign-in code", "two-factor", "2fa",
    "2\u6bb5\u968e\u8a8d\u8a3c",
]

PROVIDER_MAP: Dict[str, Dict[str, object]] = {
    "gmail.com":       {"host": "imap.gmail.com",           "port": 993, "note": "Requires App Password"},
    "googlemail.com":  {"host": "imap.gmail.com",           "port": 993, "note": "Requires App Password"},
    "outlook.com":     {"host": "outlook.office365.com",    "port": 993, "note": "Regular password"},
    "outlook.jp":      {"host": "outlook.office365.com",    "port": 993, "note": "Regular password"},
    "hotmail.com":     {"host": "outlook.office365.com",    "port": 993, "note": "Regular password"},
    "hotmail.co.jp":   {"host": "outlook.office365.com",    "port": 993, "note": "Regular password"},
    "live.com":        {"host": "outlook.office365.com",    "port": 993, "note": "Regular password"},
    "live.jp":         {"host": "outlook.office365.com",    "port": 993, "note": "Regular password"},
    "yahoo.co.jp":     {"host": "imap.mail.yahoo.co.jp",    "port": 993, "note": "Regular password"},
    "ybb.ne.jp":       {"host": "imap.mail.yahoo.co.jp",    "port": 993, "note": "Regular password"},
    "ymobile.ne.jp":   {"host": "imap.mail.yahoo.co.jp",    "port": 993, "note": "Regular password"},
    "icloud.com":      {"host": "imap.mail.me.com",         "port": 993, "note": "App-specific password"},
    "me.com":          {"host": "imap.mail.me.com",         "port": 993, "note": "App-specific password"},
    "mac.com":         {"host": "imap.mail.me.com",         "port": 993, "note": "App-specific password"},
    "qq.com":          {"host": "imap.qq.com",              "port": 993, "note": "Authorization code"},
    "foxmail.com":     {"host": "imap.qq.com",              "port": 993, "note": "Authorization code"},
    "163.com":         {"host": "imap.163.com",             "port": 993, "note": "Authorization code"},
    "126.com":         {"host": "imap.126.com",             "port": 993, "note": "Authorization code"},
    "yeah.net":        {"host": "imap.yeah.net",            "port": 993, "note": "Authorization code"},
    "protonmail.com":  {"host": "127.0.0.1",                "port": 1143, "note": "Requires ProtonMail Bridge"},
    "proton.me":       {"host": "127.0.0.1",                "port": 1143, "note": "Requires ProtonMail Bridge"},
    "pm.me":           {"host": "127.0.0.1",                "port": 1143, "note": "Requires ProtonMail Bridge"},
    "zoho.com":        {"host": "imap.zoho.com",            "port": 993, "note": "App password"},
    "zoho.jp":         {"host": "imap.zoho.jp",             "port": 993, "note": "App password"},
    "mail.ru":         {"host": "imap.mail.ru",             "port": 993, "note": "App password"},
    "bk.ru":           {"host": "imap.mail.ru",             "port": 993, "note": "App password"},
    "list.ru":         {"host": "imap.mail.ru",             "port": 993, "note": "App password"},
    "inbox.ru":        {"host": "imap.mail.ru",             "port": 993, "note": "App password"},
}


def detect_provider(email_addr: str) -> Dict[str, object]:
    domain = email_addr.split("@")[-1].lower().strip()
    if domain in PROVIDER_MAP:
        return dict(PROVIDER_MAP[domain])
    parts = domain.split(".")
    for i in range(len(parts) - 2, -1, -1):
        candidate = ".".join(parts[i:])
        if candidate in PROVIDER_MAP:
            return dict(PROVIDER_MAP[candidate])
    return {"host": f"imap.{domain}", "port": 993, "note": "Auto-detected (may not work)"}


class ImapCodeReader:
    """Async IMAP email verification code reader with polling support."""

    def __init__(self, email_addr: str, password: str):
        self.email_addr = email_addr
        self.password = password
        self._provider = detect_provider(email_addr)
        self._imap: Optional[imaplib.IMAP4_SSL] = None
        self._baseline_uid: int = 0
        self._connected: bool = False

    def connect(self) -> None:
        host = str(self._provider["host"])
        port = int(self._provider["port"])
        note = str(self._provider.get("note", ""))
        print(f"\U0001f4e7 [IMAP] 正在连接 {host}:{port}  ({note})")
        self._imap = imaplib.IMAP4_SSL(host, port, timeout=15)
        self._imap.login(self.email_addr, self.password)
        self._imap.select("INBOX")
        status, data = self._imap.uid("SEARCH", None, "ALL")
        if status == "OK" and data[0]:
            uids = [int(uid) for uid in data[0].split()]
            self._baseline_uid = max(uids) if uids else 0
        else:
            self._baseline_uid = 0
        self._connected = True
        print(f"\U0001f4e7 [IMAP] 已连接。Baseline UID = {self._baseline_uid}")

    def disconnect(self) -> None:
        if self._imap is not None and self._connected:
            try:
                self._imap.logout()
            except Exception:
                pass
            self._connected = False
            self._imap = None
            print("\U0001f4e7 [IMAP] 连接已断开。")

    async def get_verification_code(
        self, timeout: float = 60.0, poll_interval: float = 5.0
    ) -> Optional[str]:
        if not self._connected or self._imap is None:
            print("\u26a0\ufe0f [IMAP] 未连接。请先调用 connect()。")
            return None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                code = self._search_new_verification_email(search_since_uid=self._baseline_uid)
                if code is not None:
                    print("\U0001f4e7 [IMAP] 已找到验证码")
                    return code
            except Exception as e:
                print(f"\u26a0\ufe0f [IMAP] 轮询错误：{e}")
            remaining = deadline - time.monotonic()
            if remaining > 0:
                await asyncio.sleep(min(poll_interval, remaining))
        print(f"\u26a0\ufe0f [IMAP] 超时（{timeout}s）")
        return None

    def _search_new_verification_email(self, search_since_uid: int) -> Optional[str]:
        if self._imap is None:
            return None
        try:
            self._imap.noop()
            status, data = self._imap.uid("SEARCH", None, f"UID {search_since_uid + 1}:*")
            if status != "OK" or not data[0]:
                return None
            new_uids = [int(uid) for uid in data[0].split()]
            if not new_uids:
                return None
            for uid in reversed(new_uids):
                code = self._fetch_and_extract_code(uid)
                if code is not None:
                    self._baseline_uid = max(self._baseline_uid, uid)
                    return code
            self._baseline_uid = max(self._baseline_uid, max(new_uids))
        except Exception:
            pass
        return None

    def _fetch_and_extract_code(self, uid: int) -> Optional[str]:
        if self._imap is None:
            return None
        try:
            status, msg_data = self._imap.uid("FETCH", str(uid), "(RFC822)")
            if status != "OK" or msg_data[0] is None:
                return None
            raw_email = msg_data[0][1]
            msg = email.message_from_bytes(raw_email)
            subject = self._decode_email_subject(msg)
            subject_lower = subject.lower()
            is_verification = any(keyword.lower() in subject_lower for keyword in VERIFICATION_SUBJECTS)
            subject_code = self._extract_code_from_body(subject)
            body_texts = []
            self._collect_body_texts(msg, body_texts)
            combined_body = " ".join(body_texts)
            body_code = self._extract_code_from_body(combined_body)
            if is_verification or subject_code is not None:
                code = subject_code or body_code
                if code is not None:
                    print(f"\U0001f4e7 [IMAP] 来自邮件：subject='{subject[:60]}'")
                    return code
            if body_code is not None and len(combined_body) < 5000:
                print(f"\U0001f4e7 [IMAP] 启发式匹配：subject='{subject[:60]}'")
                return body_code
        except Exception:
            pass
        return None

    def _collect_body_texts(self, msg, results: list) -> None:
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    try:
                        payload = part.get_payload(decode=True)
                        if isinstance(payload, bytes):
                            charset = part.get_content_charset() or "utf-8"
                            results.append(payload.decode(charset, errors="replace"))
                        elif isinstance(payload, str):
                            results.append(payload)
                    except Exception:
                        pass
        else:
            if msg.get_content_type() == "text/plain":
                try:
                    payload = msg.get_payload(decode=True)
                    if isinstance(payload, bytes):
                        charset = msg.get_content_charset() or "utf-8"
                        results.append(payload.decode(charset, errors="replace"))
                    elif isinstance(payload, str):
                        results.append(payload)
                except Exception:
                    pass

    def _decode_email_subject(self, msg) -> str:
        decoded_parts = []
        for part, charset in decode_header(msg.get("Subject", "")):
            if isinstance(part, bytes):
                try:
                    decoded_parts.append(part.decode(charset or "utf-8", errors="replace"))
                except (LookupError, UnicodeDecodeError):
                    decoded_parts.append(part.decode("utf-8", errors="replace"))
            else:
                decoded_parts.append(str(part))
        return "".join(decoded_parts)

    def _extract_code_from_body(self, body: str) -> Optional[str]:
        if not body:
            return None
        matches = re.findall(r"\b(\d{6})\b", body)
        if not matches:
            return None
        valid_codes = [c for c in matches if c != "000000" and not all(d == c[0] for d in c)]
        return valid_codes[0] if valid_codes else None
