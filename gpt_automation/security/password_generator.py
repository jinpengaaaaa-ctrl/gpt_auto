"""Generate per-session passwords without persisting plaintext in source."""

from __future__ import annotations

import secrets
import string


# 账户密码策略：只允许大小写字母与数字，禁止特殊符号（部分页面/输入法
# 对特殊符号处理不稳定）；默认 18 位，由 CHATGPT_PASSWORD_LENGTH 覆盖。
_ALPHABET = string.ascii_letters + string.digits
_MIN_LENGTH = 12


def generate_chatgpt_password(length: int = 18) -> str:
    """Return a random password meeting the account password policy.

    The generated password contains only ASCII letters and digits, is at
    least ``_MIN_LENGTH`` characters long, and always includes at least one
    lowercase letter, one uppercase letter and one digit.
    """
    if length < _MIN_LENGTH:
        raise ValueError(f"password length must be at least {_MIN_LENGTH}")

    generator = secrets.SystemRandom()
    required = [
        generator.choice(string.ascii_lowercase),
        generator.choice(string.ascii_uppercase),
        generator.choice(string.digits),
    ]
    required.extend(generator.choice(_ALPHABET) for _ in range(length - len(required)))
    generator.shuffle(required)
    return "".join(required)
