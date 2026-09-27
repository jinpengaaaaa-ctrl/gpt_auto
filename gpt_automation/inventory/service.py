"""Lifecycle for accounts that are ready for checkout fulfillment.

The inventory table stores only fingerprints and flags.  Plaintext email,
password, TOTP secret, and OAuth values live in the encrypted
``CredentialStore`` and are only materialized for the loopback console or an
explicit operator export.
"""

from __future__ import annotations

import os
import re
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from gpt_automation.security.credential_store import (
    CredentialStore,
    CredentialStoreError,
    RegistrationCredential,
)
from gpt_automation.state import (
    RegisteredAccountRecord,
    ResourceState,
    StateStore,
    StateTransitionError,
)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_EXPORT_HEADER = (
    "email----chatgpt_password----totp_secret----email_password----"
    "client_id----refresh_token----source----account_id----status----exported_at"
)


@dataclass(frozen=True)
class RegisteredAccountView:
    account_id: str
    email: str
    status: str
    source: str
    has_chatgpt_password: bool
    has_totp_secret: bool
    has_email_password: bool
    has_oauth: bool
    provision_session_id: Optional[str]
    last_checkout_session_id: Optional[str]
    ready_verified_at: Optional[str]
    last_login_verified_at: Optional[str]
    updated_at: str
    offer_status: str = "UNKNOWN"
    offer_marked_at: Optional[str] = None
    offer_marked_reason: str = ""
    chatgpt_password: str = ""
    totp_secret: str = ""
    email_password: str = ""
    client_id: str = ""
    refresh_token: str = ""
    provision_backend: str = ""
    last_checkout_backend: str = ""


@dataclass(frozen=True)
class ImportRowResult:
    row_no: int
    result: str
    reason: str = ""


@dataclass(frozen=True)
class ImportSummary:
    import_id: str
    source_name: str
    accepted_rows: int
    rejected_rows: int
    rows: tuple[ImportRowResult, ...]


class RegisteredAccountError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class RegisteredAccountService:
    """Thread-safe inventory operations backed by StateStore + CredentialStore."""

    def __init__(
        self,
        *,
        state_store: StateStore,
        credential_store: CredentialStore,
        output_dir: str | Path,
    ) -> None:
        self.state_store = state_store
        self.credential_store = credential_store
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Import
    # ------------------------------------------------------------------

    def import_csv(
        self,
        csv_text: str,
        *,
        source_name: str,
    ) -> ImportSummary:
        lines = [
            line.strip()
            for line in csv_text.splitlines()
            if line.strip() and not line.strip().startswith("email----")
        ]
        import_id = self.state_store.begin_registered_account_import(
            source_name=source_name,
            total_rows=len(lines),
            accepted_rows=0,
            rejected_rows=0,
        )
        rows: list[ImportRowResult] = []
        accepted = 0
        rejected = 0

        for row_no, line in enumerate(lines, start=1):
            parsed = self._parse_row(line)
            if parsed is None:
                rejected += 1
                rows.append(
                    ImportRowResult(row_no, "REJECTED", "MALFORMED_ROW")
                )
                self.state_store.add_registered_account_import_row(
                    import_id=import_id,
                    row_no=row_no,
                    email_fingerprint=None,
                    result="REJECTED",
                    reason="MALFORMED_ROW",
                )
                continue

            email, chatgpt_password, totp_secret, email_password, client_id, refresh_token = parsed
            problems = self._validate(
                email, chatgpt_password, totp_secret, email_password
            )
            if problems:
                rejected += 1
                rows.append(
                    ImportRowResult(row_no, "REJECTED", problems)
                )
                self.state_store.add_registered_account_import_row(
                    import_id=import_id,
                    row_no=row_no,
                    email_fingerprint=self.state_store.email_fingerprint(email),
                    result="REJECTED",
                    reason=problems,
                )
                continue

            existing = self.state_store.get_registered_account_by_email(email)
            if existing is not None and existing.status in {
                "RESERVED",
                "CHECKOUT_RUNNING",
                "CHECKOUT_DONE",
            }:
                rejected += 1
                rows.append(
                    ImportRowResult(row_no, "REJECTED", "ACCOUNT_LOCKED")
                )
                self.state_store.add_registered_account_import_row(
                    import_id=import_id,
                    row_no=row_no,
                    email_fingerprint=self.state_store.email_fingerprint(email),
                    result="REJECTED",
                    reason="ACCOUNT_LOCKED",
                )
                continue

            account_id = existing.account_id if existing is not None else str(uuid.uuid4())
            credential = RegistrationCredential(
                account=email,
                chatgpt_password=chatgpt_password,
                totp_secret=totp_secret,
                email_password=email_password,
                client_id=client_id,
                refresh_token=refresh_token,
            )
            try:
                self.credential_store.save(account_id, credential)
            except CredentialStoreError as exc:
                rejected += 1
                rows.append(
                    ImportRowResult(row_no, "REJECTED", "CREDENTIAL_SAVE_FAILED")
                )
                self.state_store.add_registered_account_import_row(
                    import_id=import_id,
                    row_no=row_no,
                    email_fingerprint=self.state_store.email_fingerprint(email),
                    result="REJECTED",
                    reason="CREDENTIAL_SAVE_FAILED",
                )
                continue

            self.state_store.upsert_registered_account(
                account_id=account_id,
                email=email,
                source="imported",
                status="READY",
                credential_ref=account_id,
                has_chatgpt_password=bool(chatgpt_password),
                has_totp_secret=bool(totp_secret),
                has_email_password=bool(email_password),
                has_oauth=bool(client_id and refresh_token),
                ready_verified_at=self._now(),
            )
            accepted += 1
            rows.append(
                ImportRowResult(
                    row_no,
                    "UPDATED" if existing is not None else "ACCEPTED",
                )
            )
            self.state_store.add_registered_account_import_row(
                import_id=import_id,
                row_no=row_no,
                email_fingerprint=self.state_store.email_fingerprint(email),
                result="UPDATED" if existing is not None else "ACCEPTED",
            )

        return ImportSummary(
            import_id=import_id,
            source_name=source_name,
            accepted_rows=accepted,
            rejected_rows=rejected,
            rows=tuple(rows),
        )

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_account(self, account_id: str) -> Optional[RegisteredAccountView]:
        record = self.state_store.get_registered_account(account_id)
        return None if record is None else self._view(record)

    def get_account_by_email(self, email: str) -> Optional[RegisteredAccountView]:
        record = self.state_store.get_registered_account_by_email(email)
        return None if record is None else self._view(record)

    def list_accounts(
        self,
        *,
        statuses: Optional[tuple[str, ...]] = None,
        include_paid_only: bool = False,
    ) -> tuple[RegisteredAccountView, ...]:
        records = self.state_store.list_registered_accounts(
            statuses=statuses,
            include_paid_only=include_paid_only,
        )
        return tuple(self._view(record) for record in records)

    def summary(self) -> dict[str, int]:
        return self.state_store.registered_account_summary()

    # ------------------------------------------------------------------
    # Batch transitions
    # ------------------------------------------------------------------

    def promote_session_to_ready(
        self,
        *,
        session_id: str,
        email: str,
        offer_status: str = "TARGET_ELIGIBLE",
        allow_passwordless: bool = False,
    ) -> RegisteredAccountView:
        """Promote a provision-session credential into a READY inventory account.

        The encrypted credential is copied under the inventory account id so a
        later checkout run can load it without depending on the original
        provision session.  An incomplete credential is never promoted.

        ``offer_status`` records the account's free-offer verdict at creation:
        ``TARGET_ELIGIBLE`` (enters pending) or ``NO_TARGET_OFFER`` (account
        kept in inventory but excluded from the default checkout pool).
        """
        try:
            credential = self.credential_store.load(session_id)
        except CredentialStoreError as exc:
            raise RegisteredAccountError(
                "CREDENTIAL_INCOMPLETE",
                "provision session has no recoverable credential",
            ) from exc
        if credential is None:
            raise RegisteredAccountError(
                "CREDENTIAL_INCOMPLETE",
                "provision session credential is incomplete",
            )
        complete_ok = (
            credential.complete_passwordless
            if allow_passwordless
            else credential.complete
        )
        if not complete_ok:
            raise RegisteredAccountError(
                "CREDENTIAL_INCOMPLETE",
                "provision session credential is incomplete",
            )

        existing = self.state_store.get_registered_account_by_email(email)
        account_id = existing.account_id if existing is not None else str(uuid.uuid4())
        self.credential_store.save(
            account_id,
            RegistrationCredential(
                account=credential.account,
                chatgpt_password=credential.chatgpt_password,
                totp_secret=credential.totp_secret,
                email_password=credential.email_password,
                client_id=credential.client_id,
                refresh_token=credential.refresh_token,
                state="ACTIVE",
            ),
        )
        self.state_store.upsert_registered_account(
            account_id=account_id,
            email=email,
            source="provisioned",
            status="READY",
            credential_ref=account_id,
            has_chatgpt_password=bool(credential.chatgpt_password),
            has_totp_secret=bool(credential.totp_secret),
            has_email_password=bool(credential.email_password),
            has_oauth=bool(credential.client_id and credential.refresh_token),
            provision_session_id=session_id,
            ready_verified_at=self._now(),
            offer_status=offer_status,
        )
        return self._view(self.state_store.get_registered_account(account_id))

    def reserve_ready_account(
        self,
        *,
        session_id: str,
    ) -> Optional[RegisteredAccountView]:
        """Atomically claim one READY inventory account for a checkout session."""
        records = self.state_store.list_registered_accounts(statuses=("READY",))
        for record in records:
            try:
                reserved = self.reserve_accounts(
                    session_id=session_id,
                    account_ids=(record.account_id,),
                )
            except RegisteredAccountError:
                continue
            return reserved[0]
        return None

    def reserve_accounts(
        self,
        *,
        session_id: str | None = None,
        account_ids: tuple[str, ...],
    ) -> tuple[RegisteredAccountView, ...]:
        """Reserve explicit READY accounts for one checkout batch.

        When ``session_id`` is omitted, each account gets its own durable
        workflow session so later checkout can resume independently.
        """
        if not account_ids:
            raise RegisteredAccountError(
                "NO_ACCOUNTS",
                "checkout requires one or more READY accounts",
            )
        reserved: list[RegisteredAccountView] = []
        for account_id in account_ids:
            record = self.state_store.get_registered_account(account_id)
            if record is None:
                raise RegisteredAccountError(
                    "NOT_FOUND",
                    f"account not found: {account_id}",
                )
            if record.status != "READY":
                raise RegisteredAccountError(
                    "ACCOUNT_NOT_READY",
                    f"account is not READY: {account_id}",
                )
            # Reconstruct a stable placeholder email from the fingerprint-backed
            # inventory row. The real address lives in CredentialStore.
            email = self._view(record).email or f"{account_id}@inventory.local"
            try:
                if session_id:
                    existing = self.state_store.get_session(session_id)
                    if existing is None:
                        # Batch ids from orchestrator/console are not durable
                        # workflow sessions yet; bind each account to its own
                        # durable session so resource_leases FK stays valid.
                        bound_session_id = self.state_store.create_session_with_resources(
                            email,
                            {"registered_account": account_id},
                        )
                    else:
                        self.state_store.reserve_resources(
                            session_id,
                            {"registered_account": account_id},
                        )
                        bound_session_id = session_id
                else:
                    bound_session_id = self.state_store.create_session_with_resources(
                        email,
                        {"registered_account": account_id},
                    )
            except RegisteredAccountError:
                raise
            except Exception as exc:
                raise RegisteredAccountError(
                    "ACCOUNT_RESERVED",
                    f"account cannot be reserved: {account_id}",
                ) from exc
            updated = self.state_store.update_registered_account_status(
                account_id,
                "RESERVED",
                last_checkout_session_id=bound_session_id,
            )
            reserved.append(self._view(updated))
        return tuple(reserved)

    def mark_reserved(
        self,
        account_id: str,
        *,
        session_id: str,
    ) -> RegisteredAccountView:
        state = self.state_store.get_resource_state(
            "registered_account", account_id
        )
        if state is None or state is ResourceState.RELEASED:
            self.state_store.reserve_resources(
                session_id,
                {"registered_account": account_id},
            )
        record = self.state_store.update_registered_account_status(
            account_id,
            "RESERVED",
        )
        return self._view(record)

    def mark_checkout_running(
        self,
        account_id: str,
        *,
        session_id: str,
    ) -> RegisteredAccountView:
        record = self.state_store.update_registered_account_status(
            account_id,
            "CHECKOUT_RUNNING",
            last_checkout_session_id=session_id,
        )
        return self._view(record)

    def mark_checkout_done(
        self,
        account_id: str,
        *,
        session_id: str,
    ) -> RegisteredAccountView:
        record = self.state_store.update_registered_account_status(
            account_id,
            "CHECKOUT_DONE",
            last_checkout_session_id=session_id,
        )
        self.state_store.transition_session_resources(
            session_id,
            {"registered_account": ResourceState.CONSUMED},
            expected=(ResourceState.RESERVED, ResourceState.QUARANTINED),
        )
        return self._view(record)

    def release_to_ready(
        self,
        account_id: str,
        *,
        session_id: str,
        offer_mark_reason: str = "",
    ) -> RegisteredAccountView:
        """Return an intact account to READY after a side-effect-free failure.

        Used for business rejections (no free offer, non-zero amount, operator
        skip) that happen before any payment submission: the account itself is
        unharmed, so it must be selectable again in a later checkout batch.

        When ``offer_mark_reason`` is set (NO_FREE_OFFER / NON_ZERO_AMOUNT) the
        account is marked so later checkout batches skip it by default:
        NON_ZERO_AMOUNT (verified paid pricing) → PAID_ONLY;
        NO_FREE_OFFER (no entry found, weak evidence) → REVIEW_REQUIRED.
        An operator can reset the marker to retry.
        """
        record = self.state_store.update_registered_account_status(
            account_id,
            "READY",
            last_checkout_session_id=session_id,
        )
        if offer_mark_reason:
            offer_status = (
                "REVIEW_REQUIRED"
                if offer_mark_reason == "NO_FREE_OFFER"
                else "PAID_ONLY"
            )
            self.state_store.update_registered_account_offer(
                account_id,
                offer_status=offer_status,
                reason=offer_mark_reason,
            )
        try:
            self.state_store.transition_session_resources(
                session_id,
                {"registered_account": ResourceState.RELEASED},
                expected=(
                    ResourceState.RESERVED,
                    ResourceState.QUARANTINED,
                ),
            )
        except StateTransitionError:
            pass
        return self._view(self.state_store.get_registered_account(account_id))

    def reset_offer_marking(
        self,
        account_id: str,
    ) -> RegisteredAccountView:
        """Clear the PAID_ONLY marker so the account can be retried."""
        record = self.state_store.update_registered_account_offer(
            account_id,
            offer_status="UNKNOWN",
        )
        return self._view(record)

    def mark_login_verified(
        self,
        account_id: str,
        *,
        session_id: str,
    ) -> RegisteredAccountView:
        record = self.state_store.update_registered_account_status(
            account_id,
            "CHECKOUT_RUNNING",
            last_checkout_session_id=session_id,
            last_login_verified_at=self._now(),
        )
        return self._view(record)

    def mark_needs_operator(
        self,
        account_id: str,
        *,
        session_id: str,
    ) -> RegisteredAccountView:
        self._release_account_lease(account_id, session_id)
        record = self.state_store.update_registered_account_status(
            account_id,
            "NEEDS_OPERATOR",
            last_checkout_session_id=session_id,
        )
        return self._view(record)

    def _release_account_lease(
        self,
        account_id: str,
        session_id: str,
    ) -> None:
        """Release the registered-account lease so later correction can run."""
        try:
            self.state_store.transition_session_resources(
                session_id,
                {"registered_account": ResourceState.RELEASED},
                expected=(
                    ResourceState.RESERVED,
                    ResourceState.QUARANTINED,
                ),
            )
        except StateTransitionError:
            pass

    def mark_quarantined(
        self,
        account_id: str,
        *,
        session_id: str,
    ) -> RegisteredAccountView:
        record = self.state_store.update_registered_account_status(
            account_id,
            "QUARANTINED",
            last_checkout_session_id=session_id,
        )
        try:
            self.state_store.transition_session_resources(
                session_id,
                {"registered_account": ResourceState.QUARANTINED},
                expected=(
                    ResourceState.RESERVED,
                    ResourceState.QUARANTINED,
                ),
            )
        except StateTransitionError:
            pass
        return self._view(record)

    def correct_status(
        self,
        account_id: str,
        *,
        target: str,
    ) -> RegisteredAccountView:
        if target not in {"READY", "QUARANTINED", "INVALID", "NEEDS_OPERATOR"}:
            raise RegisteredAccountError(
                "INVALID_TARGET", f"unsupported target status: {target}"
            )
        record = self.state_store.get_registered_account(account_id)
        if record is None:
            raise RegisteredAccountError("NOT_FOUND", "account not found")
        if record.status in {"RESERVED", "CHECKOUT_RUNNING"}:
            raise RegisteredAccountError(
                "ACCOUNT_RESERVED",
                "reserved account cannot be corrected while a session is active",
            )
        if target == "READY":
            if record.status == "CHECKOUT_DONE":
                raise RegisteredAccountError(
                    "CHECKOUT_DONE",
                    "completed checkout accounts cannot return to READY",
                )
            payment = None
            if record.last_checkout_session_id:
                payment = self.state_store.get_action(
                    record.last_checkout_session_id,
                    "PAYMENT_SUBMIT",
                )
            if payment is not None and str(
                getattr(payment.state, "value", payment.state)
            ) in {"STARTED", "UNKNOWN"}:
                raise RegisteredAccountError(
                    "PAYMENT_UNSETTLED",
                    "payment result is unsettled; account cannot return to READY",
                )
            if record.last_checkout_session_id:
                self._release_account_lease(
                    account_id,
                    record.last_checkout_session_id,
                )
        self.state_store.update_registered_account_status(account_id, target)
        updated = self.state_store.get_registered_account(account_id)
        if updated is None:
            raise RegisteredAccountError("NOT_FOUND", "account not found")
        return self._view(updated)

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def export_csv(
        self,
        *,
        statuses: tuple[str, ...] = ("READY",),
        filename: str = "registered_accounts.csv",
    ) -> Path:
        lines = [_EXPORT_HEADER]
        exported_at = self._now()
        for view in self.list_accounts(statuses=statuses):
            fields = (
                view.email,
                view.chatgpt_password,
                view.totp_secret,
                view.email_password,
                view.client_id,
                view.refresh_token,
                view.source,
                view.account_id,
                view.status,
                exported_at,
            )
            lines.append(
                "----".join(
                    str(value).replace("\r", " ").replace("\n", " ")
                    for value in fields
                )
            )
        path = self.output_dir / filename
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=str(path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write("\n".join(lines) + "\n")
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        return path

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _view(self, record: RegisteredAccountRecord) -> RegisteredAccountView:
        email = ""
        chatgpt_password = ""
        totp_secret = ""
        email_password = ""
        client_id = ""
        refresh_token = ""
        try:
            credential = self.credential_store.load(record.credential_ref)
        except CredentialStoreError:
            credential = None
        if credential is not None:
            email = credential.account
            chatgpt_password = credential.chatgpt_password
            totp_secret = credential.totp_secret
            email_password = credential.email_password
            client_id = credential.client_id
            refresh_token = credential.refresh_token
        return RegisteredAccountView(
            account_id=record.account_id,
            email=email,
            status=record.status,
            source=record.source,
            has_chatgpt_password=record.has_chatgpt_password,
            has_totp_secret=record.has_totp_secret,
            has_email_password=record.has_email_password,
            has_oauth=record.has_oauth,
            provision_session_id=record.provision_session_id,
            last_checkout_session_id=record.last_checkout_session_id,
            ready_verified_at=record.ready_verified_at,
            last_login_verified_at=record.last_login_verified_at,
            updated_at=record.updated_at,
            offer_status=record.offer_status,
            offer_marked_at=record.offer_marked_at,
            offer_marked_reason=record.offer_marked_reason,
            chatgpt_password=chatgpt_password,
            totp_secret=totp_secret,
            email_password=email_password,
            client_id=client_id,
            refresh_token=refresh_token,
            provision_backend=self._session_backend(record.provision_session_id),
            last_checkout_backend=self._session_backend(
                record.last_checkout_session_id
            ),
        )

    def _session_backend(self, session_id: Optional[str]) -> str:
        """Best-effort browser backend recorded on a workflow session."""
        if not session_id:
            return ""
        try:
            return self.state_store.get_session_backend(session_id)
        except Exception:  # noqa: BLE001 - display-only, never raise
            return ""

    @staticmethod
    def _parse_row(line: str) -> Optional[tuple[str, str, str, str, str, str]]:
        parts = line.split("----")
        if len(parts) < 3:
            return None
        email = parts[0].strip()
        chatgpt_password = parts[1].strip()
        totp_secret = parts[2].strip()
        email_password = parts[3].strip() if len(parts) > 3 else ""
        client_id = parts[4].strip() if len(parts) > 4 else ""
        refresh_token = parts[5].strip() if len(parts) > 5 else ""
        return (
            email,
            chatgpt_password,
            totp_secret,
            email_password,
            client_id,
            refresh_token,
        )

    @staticmethod
    def _validate(
        email: str,
        chatgpt_password: str,
        totp_secret: str,
        email_password: str,
    ) -> str:
        if not _EMAIL_RE.match(email):
            return "INVALID_EMAIL"
        if len(chatgpt_password) < 12:
            return "MISSING_CHATGPT_PASSWORD"
        if not totp_secret:
            return "MISSING_TOTP"
        if not email_password:
            return "MISSING_EMAIL_PASSWORD"
        return ""

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")
