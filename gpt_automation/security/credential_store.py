"""Per-user encrypted credential storage for recoverable workflows."""

from __future__ import annotations

import ctypes
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Protocol


class CredentialStoreError(RuntimeError):
    """Raised when credentials cannot be protected or recovered."""


@dataclass(frozen=True)
class RegistrationCredential:
    account: str
    chatgpt_password: str
    totp_secret: str
    email_password: str
    client_id: str = ""
    refresh_token: str = ""
    state: str = "ACTIVE"

    @property
    def complete(self) -> bool:
        return all(
            bool(value.strip())
            for value in (
                self.account,
                self.chatgpt_password,
                self.totp_secret,
                self.email_password,
            )
        ) and self.state == "ACTIVE"

    @property
    def complete_passwordless(self) -> bool:
        """Passwordless variant: email + email password are enough.

        Used by the direct email-verification signup route where the
        account has no ChatGPT password and no TOTP secret.
        """
        return (
            bool(self.account.strip())
            and bool(self.email_password.strip())
            and self.state == "ACTIVE"
        )

    @property
    def consumed(self) -> bool:
        return self.state == "CONSUMED"


class SecretCodec(Protocol):
    def protect(self, plaintext: bytes) -> bytes: ...

    def unprotect(self, ciphertext: bytes) -> bytes: ...


class WindowsDpapiCodec:
    """Use the current Windows user's DPAPI key without adding a dependency."""

    class _DataBlob(ctypes.Structure):
        _fields_ = [
            ("cbData", ctypes.c_ulong),
            ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
        ]

    def __init__(self) -> None:
        if os.name != "nt":
            raise CredentialStoreError("Windows DPAPI is required for credential storage")
        self._crypt32 = ctypes.windll.crypt32
        self._kernel32 = ctypes.windll.kernel32

    def protect(self, plaintext: bytes) -> bytes:
        return self._transform("CryptProtectData", plaintext)

    def unprotect(self, ciphertext: bytes) -> bytes:
        return self._transform("CryptUnprotectData", ciphertext)

    def _transform(self, function_name: str, data: bytes) -> bytes:
        input_buffer = ctypes.create_string_buffer(data)
        input_blob = self._DataBlob(
            len(data),
            ctypes.cast(input_buffer, ctypes.POINTER(ctypes.c_ubyte)),
        )
        output_blob = self._DataBlob()
        function = getattr(self._crypt32, function_name)
        if not function(
            ctypes.byref(input_blob),
            None,
            None,
            None,
            None,
            0,
            ctypes.byref(output_blob),
        ):
            raise CredentialStoreError(f"{function_name} failed")
        try:
            return ctypes.string_at(output_blob.pbData, output_blob.cbData)
        finally:
            self._kernel32.LocalFree(output_blob.pbData)


_SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]+$")


class CredentialStore:
    """Atomically persist encrypted credentials keyed by workflow session."""

    def __init__(self, directory: str | Path, codec: SecretCodec | None = None) -> None:
        self.directory = Path(directory)
        self._codec = codec or WindowsDpapiCodec()

    def save(self, session_id: str, credential: RegistrationCredential) -> None:
        path = self._path_for(session_id)
        self.directory.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            asdict(credential),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        protected = self._codec.protect(payload)
        temporary_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=self.directory,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_path = handle.name
                handle.write(protected)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, path)
        finally:
            if temporary_path and os.path.exists(temporary_path):
                os.unlink(temporary_path)

    def load(self, session_id: str) -> RegistrationCredential | None:
        path = self._path_for(session_id)
        if not path.exists():
            return None
        try:
            payload = self._codec.unprotect(path.read_bytes())
            values = json.loads(payload.decode("utf-8"))
            credential = RegistrationCredential(**values)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise CredentialStoreError(
                f"credential record cannot be read for session {session_id}"
            ) from exc
        return credential

    def is_complete(self, session_id: str) -> bool:
        credential = self.load(session_id)
        return credential is not None and credential.complete

    def is_complete_passwordless(self, session_id: str) -> bool:
        credential = self.load(session_id)
        return credential is not None and credential.complete_passwordless


    def consume(self, session_id: str) -> bool:
        """Mark credentials unusable for resume while retaining an audit-safe tombstone."""
        credential = self.load(session_id)
        if credential is None or credential.consumed:
            return True
        self.save(session_id, replace(credential, state="CONSUMED"))
        return True

    def _path_for(self, session_id: str) -> Path:
        if not _SAFE_SESSION_ID.fullmatch(session_id):
            raise CredentialStoreError("invalid workflow session id")
        return self.directory / f"{session_id}.bin"
