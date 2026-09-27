"""verification/ — Outlook IMAP OAuth 邮箱验证码获取。"""

from gpt_automation.verification.unified import UnifiedCodeReader
from gpt_automation.verification.outlook import OutlookCodeReader
from gpt_automation.verification.outlook_mail_reader import OutlookMailReader
from gpt_automation.verification.imap import ImapCodeReader, detect_provider, PROVIDER_MAP

__all__ = [
    "UnifiedCodeReader", "OutlookCodeReader", "OutlookMailReader",
    "ImapCodeReader", "detect_provider", "PROVIDER_MAP",
]
