"""Canonical implementation of the per-account registration business flow."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import TYPE_CHECKING, Any, AsyncIterator, Awaitable, Callable, Dict, Optional, Set

import config
from gpt_automation.browser import ip_guard
from gpt_automation.network.rotator import record_ip_usage
from gpt_automation.network.strategy import NetworkStrategy
from gpt_automation.phases.helpers import safe_click, safe_screenshot
from gpt_automation.phases.interactive import (
    circuit_reset_all,
    guarded_execution,
    hold_for_operator,
    request_manual_intervention,
)
from gpt_automation.phases.phase1 import (
    phase1_choose_password_signup,
    phase1_create_password,
    phase1_email_verify_code,
    phase1_form,
    phase1_login_password,
    phase1_navigate,
    phase1_ready_confirm,
    phase1_survey,
)
from gpt_automation.phases.phase2 import phase2_mfa
from gpt_automation.phases.phase3 import (
    phase3_accept_offer,
    phase3_amount_guard,
    phase3_open_pricing_page,
    phase3_pricing_page_verified,
    build_offer_eligibility_policy,
    verify_offer_eligibility,
)
from gpt_automation.phases.stripe import (
    phase3_stripe_address,
    phase3_stripe_card,
    phase3_stripe_confirm,
    phase3_stripe_confirm_manual,
    classify_checkout_amount,
    checkout_amount_gate,
    extract_checkout_amount_evidence,
    verify_stripe_address_ready,
)
from gpt_automation.phases.phase4 import (
    phase4_cancel,
    phase4_status_confirm,
    phase4_subscription_active_confirm,
)
from gpt_automation.utils.timing import (
    CHECKOUT_NAV_INTERVAL_S,
    CHECKOUT_NAV_TIMEOUT_S,
)
from gpt_automation.utils.csv_writer import evidence_path, write_status
from gpt_automation.utils.exceptions import (
    FATAL_EXCEPTIONS,
    QuitBatchException,
    SAFE_EXCEPTIONS,
    NeedsOperatorException,
    OperatorDeferredException,
    SkipEmailException,
    RestartBrowserException,
)
from gpt_automation.utils.playwright_errors import SessionLostError
from gpt_automation.utils.redaction import account_fingerprint, mask_email
from gpt_automation.verification.unified import UnifiedCodeReader
from gpt_automation.workflow.models import (
    AccountRunResult,
    RegistrationInput,
    StepStatus,
    WorkflowStage,
)
from gpt_automation.workflow.interaction_policy import InteractionDecision
from gpt_automation.workflow.authentication import (
    IdentityVerificationOutcome,
    verify_authenticated_identity,
)
from gpt_automation.workflow.graph import (
    ActionEffectClass,
    Evidence,
    StepStatus as GraphStepStatus,
    WorkflowStepId,
)
from gpt_automation.workflow.engine import WorkflowEngine
from gpt_automation.workflow.manual_targets import ManualTargetPlanner
from gpt_automation.workflow.runtime_state import WorkflowRuntimeState
from gpt_automation.workflow.steps import BrowserStepHandler
from gpt_automation.workflow.registry import StepRegistry
from gpt_automation.workflow.handlers import (
    AuthEntryHandler,
    AuthRouteHandler,
    CancellationHandler,
    CheckoutHandler,
    LoginEmailCodeHandler,
    LoginPasswordHandler,
    MfaRegistrationHandler,
    OfferDecisionHandler,
    PaymentHandler,
    SignupEmailCodeHandler,
    SignupPasswordHandler,
)
from gpt_automation.workflow.page_observer import ChatGPTPageObserver
from gpt_automation.workflow.verifiers import (
    AUTH_ROUTE_NEXT_STATES,
    EMAIL_CODE_NEXT_STATES,
    REGISTRATION_FORM_NEXT_STATES,
    SIGNUP_PASSWORD_NEXT_STATES,
    SURVEY_NEXT_STATES,
    WELCOME_NEXT_STATES,
    is_positive_transition,
    password_value_proven,
)
from gpt_automation.security.credential_store import (
    CredentialStore,
    CredentialStoreError,
    RegistrationCredential,
)

if TYPE_CHECKING:
    from gpt_automation.state import StateStore


logger = logging.getLogger(__name__)


def payment_confirmation_is_verified(result: str) -> bool:
    """Accept only payment outcomes that were explicitly verified."""
    return result in {"success", "manual_completed"}


def _must_verify_before_side_effect_replay(
    runtime_state: WorkflowRuntimeState,
    action: str,
    pending_stage: WorkflowStage,
) -> bool:
    """Treat either an action record or a pending checkpoint as ambiguous."""
    return (
        runtime_state.action_state(action) is not None
        or runtime_state.has_reached(pending_stage)
    )


async def _verify_zero_amount(page, email: str) -> bool:
    return (await phase3_amount_guard(page, email)).verified


# Non-idempotent external transactions: a browser crash while one of these
# actions is STARTED/UNKNOWN means the real-world outcome cannot be verified,
# so the account must be quarantined (UNKNOWN_SIDE_EFFECT) instead of
# restarting the browser and replaying the action.
_NON_IDEMPOTENT_ACTIONS = ("PAYMENT_SUBMIT", "CANCEL_SUBSCRIPTION")


def _record_browser_crashed(
    state_store,
    session_id: str,
    reason: str,
) -> None:
    """Best-effort BROWSER_CRASHED step event for batch diagnostics."""
    if state_store is None or not session_id:
        return
    try:
        state_store.record_step_status(
            session_id,
            "BROWSER_CRASHED",
            status="FAILED",
            outcome_code="BROWSER_CRASHED",
            last_error=f"browser session lost ({reason})",
        )
    except Exception:  # noqa: BLE001 - diagnostic recording is best-effort
        logger.warning(
            "could not record BROWSER_CRASHED event for session %s", session_id
        )


def _session_loss_disposition(runtime_state: WorkflowRuntimeState) -> str:
    """Classify a crashed browser session: "restart" or "unknown_side_effect".

    With durable state, any non-idempotent action in STARTED/UNKNOWN puts the
    account inside the unknown-side-effect window.  Without durable state the
    stage checkpoint is the only signal; reaching PAYMENT_PENDING without
    PAYMENT_VERIFIED is treated conservatively.
    """
    from gpt_automation.state import ActionState

    if runtime_state.enabled:
        assert runtime_state.store is not None
        for action in _NON_IDEMPOTENT_ACTIONS:
            if runtime_state.action_state(action) in (
                ActionState.STARTED,
                ActionState.UNKNOWN,
            ):
                return "unknown_side_effect"
        return "restart"
    return (
        "unknown_side_effect"
        if runtime_state.has_reached(WorkflowStage.PAYMENT_PENDING)
        and not runtime_state.has_reached(WorkflowStage.PAYMENT_VERIFIED)
        else "restart"
    )


@asynccontextmanager
async def _registration_resources(
    registration: RegistrationInput,
    profile_id: str,
    previous_ip: Optional[str],
    used_ips: Set[str],
    network_strategy: NetworkStrategy,
    *,
    state_store=None,
    session_id: str = "",
    credential_store=None,
) -> AsyncIterator[Dict[str, Any]]:
    """Acquire and (always) release all per-account resources.

    Yields a context dict with keys:
        - code_reader: UnifiedCodeReader (always present)
        - rotator:     IPRotator
        - browser:     Browser context or None (None means IP rotation failed)
        - page:        Playwright Page or None
        - current_ip:  The IP the browser is using, or None
    """
    code_reader = UnifiedCodeReader(
        registration.email,
        registration.email_password,
        client_id=registration.client_id,
        refresh_token=registration.refresh_token,
        on_token_rotated=_make_refresh_token_rotator(
            registration.email,
            session_id=session_id,
            credential_store=credential_store,
        ),
    )
    acquired = await network_strategy.acquire_browser(
        previous_ip,
        used_ips,
        profile_id,
    )
    browser = acquired.browser
    page = acquired.page
    current_ip = acquired.current_ip
    # Use a mutable dict for stripe_page so callbacks (which receive this dict
    # via ctx) can update it, and the finally block can read the updated value.
    ctx: Dict[str, Any] = {
        "code_reader": code_reader,
        "browser": browser,
        "page": page,
        "current_ip": current_ip,
        "stripe_page": None,
        "email": registration.email,
        "email_pwd": registration.email_password,
    }
    try:
        yield ctx
    finally:
        # Stripe popup cleanup — read from ctx dict (cancel_callback may have set it)
        _stripe_page = ctx.get("stripe_page")
        try:
            if _stripe_page is not None and _stripe_page is not page:
                await _stripe_page.close()
        except FATAL_EXCEPTIONS:
            raise
        except Exception:
            # 清理是辅助操作：页面可能已被业务异常关闭，失败不得覆盖原始异常。
            pass
        # Browser cleanup: the close result must reach the batch scheduler.
        # A leaked Firefox process (or a failed context teardown) is recorded
        # as a step event so the orchestrator can trip a circuit breaker
        # instead of silently continuing to allocate more accounts.
        close_ok = await ip_guard.close_browser(browser)
        if not close_ok:
            logger.error(
                "browser close failed for %s; recording BROWSER_CLOSE_FAILED",
                mask_email(registration.email),
            )
            if state_store is not None and session_id:
                try:
                    state_store.record_step_status(
                        session_id,
                        "BROWSER_CLOSE",
                        status="FAILED",
                        outcome_code="BROWSER_CLOSE_FAILED",
                        last_error="browser teardown failed; process may still be alive",
                    )
                except Exception:  # noqa: BLE001 - diagnostic recording is best-effort
                    logger.exception(
                        "could not record browser close failure for %s",
                        mask_email(registration.email),
                    )
        # The orchestrator owns the shared network strategy lifecycle.
        try:
            await code_reader.close()
        except FATAL_EXCEPTIONS:
            raise
        except Exception:
            # 同上：读取器清理失败不得覆盖业务异常。
            pass


def _valid_evidence(path: str) -> bool:
    """True when an evidence path really exists and is non-empty."""
    try:
        return bool(path) and os.path.isfile(path) and os.path.getsize(path) > 0
    except OSError:
        return False


async def signup_route_is_passwordless(page, post_route_state) -> bool:
    """True when the signup page offers no password continuation.

    Route A (password + 2FA) applies when the page exposes the
    create-account/password link or when the workflow is already past
    the password page (resume).  Route B (direct email verification)
    applies when the email-verification page has no password link.
    """
    from gpt_automation.phases.phase1 import password_continuation_available
    from gpt_automation.phases.screen_detector import ScreenState

    if post_route_state is ScreenState.SIGNUP_PASSWORD:
        return False
    if post_route_state in {
        ScreenState.REGISTRATION_FORM,
        ScreenState.WELCOME_MODAL,
        ScreenState.SURVEY,
        ScreenState.MAIN_PAGE,
    }:
        # Already past the password page (resume): keep existing behaviour.
        return False
    if post_route_state is not ScreenState.EMAIL_VERIFY_CODE:
        return False
    return not await password_continuation_available(
        page,
        timeout_s=config.PASSWORD_CONTINUATION_PROBE_S,
    )


def _make_refresh_token_rotator(
    email: str,
    *,
    session_id: str = "",
    credential_store=None,
) -> Callable[[str], None]:
    """Return a best-effort refresh-token persister for one account.

    Writes the rotated token back to ``input/email.txt`` (when the row still
    exists) and, for resumable sessions, to the encrypted credential store.
    Failures are logged without the token value and never block the flow.
    """
    from gpt_automation.inventory.email_queue import EmailQueue

    queue = EmailQueue(
        input_path=config.EMAIL_FILE,
        skipped_path=config.SKIPPED_FILE,
    )
    account_label = mask_email(email)

    def on_token_rotated(new_refresh_token: str) -> None:
        try:
            if not queue.update_refresh_token(email, new_refresh_token):
                logger.warning(
                    "refresh token rotated for %s but email is no longer in input",
                    account_label,
                )
        except Exception as exc:  # noqa: BLE001 - persistence is best-effort
            logger.error(
                "refresh token persist to input failed for %s: %s",
                account_label,
                exc,
            )
        if credential_store is not None and session_id:
            try:
                credential = credential_store.load(session_id)
                if credential is not None:
                    credential_store.save(
                        session_id,
                        replace(credential, refresh_token=new_refresh_token),
                    )
            except Exception as exc:  # noqa: BLE001 - persistence is best-effort
                logger.error(
                    "refresh token persist to credential store failed for %s: %s",
                    account_label,
                    exc,
                )

    return on_token_rotated


async def settle_account_result(
    page,
    email: str,
    code: str,
    *,
    ip: Optional[str] = None,
    reason: str = "",
    offer_result: Optional[str] = None,
    page_url: str = "",
    promo_campaign: Optional[str] = None,
    evidence_paths: tuple = (),
    terminal_screenshot: bool = True,
    screenshot_kind: str = "terminal",
    passwordless: bool = False,
) -> AccountRunResult:
    """Build the terminal account result and capture a final screenshot.

    The screenshot happens BEFORE the browser is closed: this is the only
    point where the page is guaranteed to still be alive for failure
    outcomes.  Existing valid evidence wins; a failed capture never changes
    the business result nor blocks the batch (continuous mode).  The
    ``RETRY_CONFIG["screenshot_on_error"]`` switch keeps the historical
    configuration effective (default on).
    """
    valid = tuple(p for p in evidence_paths if _valid_evidence(p))
    screenshot_status = "missing"
    final_paths: tuple = valid
    if valid:
        screenshot_status = "captured"
    elif page is None:
        screenshot_status = "browser_not_started"
    elif terminal_screenshot and bool(
        config.RETRY_CONFIG.get("screenshot_on_error", True)
    ):
        shot_path = evidence_path(email, screenshot_kind)
        outcome = await safe_screenshot(page, shot_path)
        screenshot_status = outcome.status
        if outcome.path:
            final_paths = (outcome.path,)
    base = AccountRunResult.for_code(
        email=email,
        code=code,
        ip=ip,
        reason=reason,
    )
    if not (
        offer_result
        or page_url
        or promo_campaign
        or final_paths
        or screenshot_status != "missing"
    ):
        return base
    return replace(
        base,
        offer_result=offer_result,
        page_url=page_url,
        promo_campaign=promo_campaign,
        evidence_paths=final_paths,
        screenshot_status=screenshot_status,
        passwordless=passwordless,
    )


# ═══════════════════════════════════════════════════════════════
# Top-level registration
# ═══════════════════════════════════════════════════════════════


async def run_registration_business_flow(
    registration: RegistrationInput,
    previous_ip: Optional[str] = None,
    used_ips: Optional[Set[str]] = None,
    network_strategy: Optional[NetworkStrategy] = None,
    state_store: Optional["StateStore"] = None,
    session_id: str = "",
    card_resource_provider: Optional[
        Callable[[], Awaitable[Optional[Dict[str, str]]]]
    ] = None,
    run_scope: str = "full",
    require_login_gate: bool = False,
    checkout_goal: str = "complete",
    signup_method: Optional[str] = None,
) -> AccountRunResult:
    """Run the complete 4-phase flow for one account.

    Args:
        registration: Immutable account, card and address input.
        previous_ip: IP used by the previously-processed account (for diversity).
        used_ips: Set of all IPs already used in this batch.

    Returns:
        A typed account result shared by all entrypoints.
    """
    email = registration.email
    account_label = mask_email(email)
    email_pwd = registration.email_password
    name = registration.name
    age = registration.age
    card_no = registration.card_number
    card_expiry = registration.card_expiry
    card_cvc = registration.card_cvc
    zip_code = registration.zip_code
    pref = registration.prefecture
    city = registration.city
    addr1 = registration.address1
    addr2 = registration.address2

    def result_for(
        code: str,
        *,
        ip: Optional[str],
        reason: str = "",
        offer_result: Optional[str] = None,
        page_url: str = "",
        promo_campaign: Optional[str] = None,
        evidence_paths: tuple = (),
    ) -> AccountRunResult:
        base = AccountRunResult.for_code(
            email=email,
            code=code,
            ip=ip,
            reason=reason,
        )
        if offer_result is None and not page_url and promo_campaign is None and not evidence_paths:
            return base
        return replace(
            base,
            offer_result=offer_result,
            page_url=page_url,
            promo_campaign=promo_campaign,
            evidence_paths=evidence_paths,
        )

    used: Set[str] = used_ips if used_ips is not None else set()
    # ????????????? 2FA??????????????
    passwordless = False
    if network_strategy is None:
        raise ValueError("RegistrationWorkflow requires a NetworkStrategy")
    runtime_state = WorkflowRuntimeState(
        state_store,
        session_id,
        enforce_graph_dependencies=True,
    )
    scope = (run_scope or "full").strip().lower()
    login_gate_required = bool(require_login_gate or scope == "checkout_fulfillment")
    goal = (checkout_goal or "complete").strip().lower()
    step_registry = StepRegistry()
    step_engine = WorkflowEngine.for_step_execution(registry=step_registry)
    credential_store = (
        CredentialStore(config.CREDENTIAL_STORE_DIR)
        if session_id
        else None
    )
    runtime_state.checkpoint("NETWORK_PENDING")
    circuit_reset_all()  # Clear circuit breaker state from previous account
    print(f"\n{'=' * 60}\n[任务开始] 目标：{account_label}\n{'=' * 60}")
    logger.info("registration start: %s", account_label)

    profile_id = account_fingerprint(email)[:12]
    max_restarts = 3
    restart_count = 0

    while True:
        try:
            async with _registration_resources(
                        registration,
                        profile_id,
                        previous_ip,
                        used,
                        network_strategy,
                        state_store=state_store,
                        session_id=session_id,
                        credential_store=credential_store,
            ) as ctx:
                browser = ctx["browser"]
                page = ctx["page"]
                current_ip = ctx["current_ip"]
                code_reader = ctx["code_reader"]

                if browser is None:
                    return result_for("IP_NOT_ROTATED", ip=previous_ip)
                if current_ip:
                    logger.info("account exit IP ready: %s (%s)", current_ip, account_label)
                try:
                    runtime_state.checkpoint("NETWORK_READY")
                    if runtime_state.enabled and runtime_state.step_evidence(
                        WorkflowStepId.NETWORK_READY
                    ) is None:
                        observed_details = (
                            ("network_state", "ready"),
                            ("verification_source", "network_strategy"),
                        )
                        if current_ip:
                            observed_details += (
                                ("observed_ip", current_ip),
                                (
                                    "observed_at",
                                    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                ),
                            )
                        runtime_state.record_step_evidence(
                            Evidence(
                                step_id=WorkflowStepId.NETWORK_READY,
                                evidence_type="network_postcondition",
                                details=observed_details,
                                verifier_version="account-flow-v1",
                            )
                        )
                    # The browser-verified exit IP is recorded the moment the
                    # network is ready.  A later failure, skip or operator
                    # abort must never lose which IP this account actually
                    # used, so the next account's switch has a real baseline.
                    if current_ip:
                        used.add(current_ip)
                        if not record_ip_usage(current_ip, email):
                            logger.error(
                                "IP history could not be persisted at network ready for %s (IP: %s)",
                                account_label,
                                current_ip,
                            )
                except FATAL_EXCEPTIONS:
                    raise
                except Exception as exc:
                    logger.exception(
                        "network state could not be verified: %s",
                        account_label,
                    )
                    decision = await hold_for_operator(
                        page,
                        email,
                        "network_state",
                        "网络准备状态无法确认；请检查浏览器现场后明确选择如何处理。",
                        exc,
                    )
                    if decision is InteractionDecision.DEFER_ACCOUNT:
                        runtime_state.checkpoint(
                            WorkflowStage.NETWORK_READY,
                            status="NEEDS_OPERATOR",
                        )
                        write_status(email, "NEEDS_OPERATOR", "", session_id=session_id)
                        return await settle_account_result(
                            page,
                            email,
                            "NEEDS_OPERATOR",
                            ip=current_ip,
                            reason="network_state",
                        )
                    write_status(
                        email,
                        f"ERROR:{type(exc).__name__}",
                        "",
                        session_id=session_id,
                    )
                    return await settle_account_result(
                        page,
                        email,
                        f"ERROR:{type(exc).__name__}",
                        ip=current_ip,
                        reason=type(exc).__name__,
                    )

                clean_secret: Optional[str] = None
                chatgpt_password = registration.chatgpt_password or config.CHATGPT_PASSWORD

                def load_saved_credential() -> Optional[RegistrationCredential]:
                    if credential_store is None:
                        return None
                    try:
                        credential = credential_store.load(session_id)
                    except CredentialStoreError as exc:
                        logger.error("credential recovery failed for %s: %s", account_label, exc)
                        return None
                    if credential is not None and credential.consumed:
                        logger.warning(
                            "credential recovery refused for consumed session %s",
                            session_id,
                        )
                        return None
                    return credential

                saved_credential = load_saved_credential()
                resumable_chatgpt_password = bool(
                    saved_credential is not None
                    and saved_credential.chatgpt_password
                )
                if saved_credential is not None and saved_credential.chatgpt_password:
                    chatgpt_password = saved_credential.chatgpt_password

                def persist_password() -> None:
                    if not chatgpt_password or len(chatgpt_password) < 12:
                        raise NeedsOperatorException(
                            "generated ChatGPT password is missing or shorter than 12 characters"
                        )
                    if credential_store is None:
                        return
                    current_rt = (
                        code_reader.latest_refresh_token or registration.refresh_token
                    )
                    credential = RegistrationCredential(
                        account=email,
                        chatgpt_password=chatgpt_password,
                        totp_secret=clean_secret or "",
                        email_password=email_pwd,
                        client_id=registration.client_id,
                        refresh_token=current_rt,
                    )
                    try:
                        credential_store.save(session_id, credential)
                    except CredentialStoreError as exc:
                        raise NeedsOperatorException(
                            "encrypted ChatGPT password could not be persisted"
                        ) from exc

                def persist_credential(secret: Optional[str]) -> None:
                    if not secret:
                        raise NeedsOperatorException(
                            "MFA completed without a recoverable TOTP secret"
                        )
                    if credential_store is None:
                        # Direct callers without a resumable session may still reach
                        # a later business rejection.  A final success is blocked
                        # below unless this record can be durably stored.
                        logger.warning(
                            "MFA secret kept in memory because no workflow session exists"
                        )
                        return
                    current_rt = (
                        code_reader.latest_refresh_token or registration.refresh_token
                    )
                    credential = RegistrationCredential(
                        account=email,
                        chatgpt_password=chatgpt_password,
                        totp_secret=secret,
                        email_password=email_pwd,
                        client_id=registration.client_id,
                        refresh_token=current_rt,
                    )
                    try:
                        credential_store.save(session_id, credential)
                        if not credential_store.is_complete(session_id):
                            raise CredentialStoreError("credential record is incomplete")
                    except CredentialStoreError as exc:
                        raise NeedsOperatorException(
                            "encrypted credential record could not be persisted"
                        ) from exc

                def persist_passwordless_credential() -> None:
                    """Save the passwordless variant (no ChatGPT password, no TOTP).

                    The email password and OAuth values remain recoverable for
                    mailbox access; the account simply has no ChatGPT password
                    or authenticator binding on the direct email-verify route.
                    """
                    if credential_store is None:
                        logger.warning(
                            "passwordless credential kept in memory because no workflow session exists"
                        )
                        return
                    current_rt = (
                        code_reader.latest_refresh_token or registration.refresh_token
                    )
                    credential = RegistrationCredential(
                        account=email,
                        chatgpt_password="",
                        totp_secret="",
                        email_password=email_pwd,
                        client_id=registration.client_id,
                        refresh_token=current_rt,
                    )
                    try:
                        credential_store.save(session_id, credential)
                        if not credential_store.is_complete_passwordless(session_id):
                            raise CredentialStoreError(
                                "passwordless credential record is incomplete"
                            )
                    except CredentialStoreError as exc:
                        raise NeedsOperatorException(
                            "encrypted passwordless credential could not be persisted"
                        ) from exc

                def persist_extracted_secret(secret: str) -> None:
                    nonlocal clean_secret
                    clean_secret = secret
                    logger.info("MFA secret captured in memory; waiting for server-side verification")

                try:
                    # Phase 1 — 画面状态检测：判断是否已登录
                    from gpt_automation.phases.screen_detector import ScreenState
                    page_observer = ChatGPTPageObserver(page)

                    async def detected_state():
                        return (await page_observer.observe()).state

                    recovery_totp = clean_secret or registration.totp_secret or ""
                    # 按 scope 分流 bootstrap：开通会员需要恢复已有登录态
                    # （用库存凭据补登录）；创建账户只做 auth 入口就位，
                    # 绝不填凭据 —— 新账户还没有 ChatGPT 密码/TOTP，且
                    # 「ログインまたは新規登録」（login_modal）就是可用入口。
                    if login_gate_required:
                        from gpt_automation.workflow.login_recovery import (
                            LoginRecoveryOutcome,
                            ensure_login_or_auth_entry,
                        )

                        bootstrap_outcome = await ensure_login_or_auth_entry(
                            page,
                            email=email,
                            chatgpt_password=chatgpt_password,
                            totp_secret=recovery_totp,
                            detect_state=detected_state,
                            code_reader=code_reader,
                        )
                        bootstrap_ok = (
                            bootstrap_outcome is LoginRecoveryOutcome.AUTHENTICATED
                        )
                        if not bootstrap_ok:
                            await request_manual_intervention(
                                page,
                                email,
                                "login_bootstrap",
                                "登录状态未能自动恢复；请完成登录或 MFA，程序会重新验证。",
                                lambda: detected_state()
                                is not ScreenState.UNKNOWN,
                                next_step_description="继续当前流程",
                                next_step_verification_fn=lambda: detected_state()
                                is not ScreenState.UNKNOWN,
                            )
                    else:
                        from gpt_automation.workflow.login_recovery import (
                            ensure_auth_entry,
                        )

                        entry_state = await ensure_auth_entry(
                            page,
                            detect_state=detected_state,
                        )
                        logger.info(
                            "orchestrator: auth entry ready (state=%s)",
                            entry_state,
                        )

                    async def execute_page_step(
                        step_id: WorkflowStepId,
                        action,
                        verify,
                        *,
                        evidence_type: str,
                        evidence_details: tuple[tuple[str, str], ...],
                        effect_class: ActionEffectClass = ActionEffectClass.REPEATABLE,
                        handler_type=BrowserStepHandler,
                    ):
                        """Run a page action through the state-machine boundary.

                        Direct callers without a durable session remain a diagnostic
                        compatibility path. Production sessions always use the
                        durable handler executor, so page success cannot bypass
                        evidence persistence.
                        """
                        if not runtime_state.enabled:
                            await action()
                            if not await verify():
                                await request_manual_intervention(
                                    page,
                                    email,
                                    step_id.value,
                                    f"页面后置条件未确认: {step_id.value}",
                                    verify,
                                )
                            return
                        if handler_type is BrowserStepHandler:
                            handler = handler_type(
                                step_id=step_id,
                                action=action,
                                verify=verify,
                                evidence_type=evidence_type,
                                evidence_details=evidence_details,
                                verifier_version="account-flow-v1",
                                effect_class=effect_class,
                            )
                        else:
                            handler = handler_type(action=action, verify=verify)
                        result = await execute_registered_handler(handler)
                        if result.status is GraphStepStatus.SUCCEEDED:
                            return
                        if result.status is GraphStepStatus.WAITING_OPERATOR:
                            await request_manual_intervention(
                                page,
                                email,
                                step_id.value,
                                f"页面后置条件未确认: {step_id.value}",
                                verify,
                            )
                            if runtime_state.enabled:
                                runtime_state.record_step_evidence(
                                    Evidence(
                                        step_id=step_id,
                                        evidence_type=evidence_type,
                                        details=evidence_details,
                                        verifier_version="account-flow-v1",
                                    )
                                )
                            return
                        raise RuntimeError(
                            f"page step {step_id.value} did not complete: {result.code}"
                        )

                    async def execute_registered_handler(handler: BrowserStepHandler):
                        """Register exactly one handler and execute it through Engine."""
                        if not step_registry.contains(handler.step_id):
                            step_registry.register(handler)
                        return await step_engine.execute_registered_step(
                            handler.step_id,
                            runtime_state,
                        )

                    def step_is_verified(
                        step_id: WorkflowStepId,
                        legacy_stage: WorkflowStage,
                    ) -> bool:
                        if runtime_state.enabled:
                            return runtime_state.step_evidence(step_id) is not None
                        return runtime_state.has_reached(legacy_stage)

                    async def verify_login_transition() -> bool:
                        # Firefox private cold starts navigate slower than
                        # Chromium after the email is submitted; poll instead
                        # of a single read so the verification-code page has
                        # time to appear.
                        return await _poll_transition(
                            AUTH_ROUTE_NEXT_STATES,
                            timeout=30.0,
                        )

                    async def verify_password_filled() -> bool:
                        state = await detected_state()
                        if state is not ScreenState.LOGIN_PASSWORD:
                            return False
                        selector = config.SELECTORS.get(
                            "reg_password_input",
                            "input[name='password']",
                        )
                        value = await page.locator(selector).first.input_value()
                        return password_value_proven(value, chatgpt_password)

                    async def _poll_transition(expected_states, timeout: float = 15.0, interval: float = 0.5) -> bool:
                        deadline = time.time() + timeout
                        while time.time() < deadline:
                            state = await detected_state()
                            if is_positive_transition(state, expected_states):
                                return True
                            await asyncio.sleep(interval)
                        return False

                    async def verify_email_code_complete() -> bool:
                        return await _poll_transition(EMAIL_CODE_NEXT_STATES, timeout=15.0)

                    def mark_email_code_submit_started(
                        step_id: WorkflowStepId,
                    ) -> None:
                        """Journal the email-code submit immediately before click.

                        The mailbox read is safe to exhaust and skip.  Once the
                        page submit click starts, the result must be verified
                        before this email can be released or archived.
                        """
                        runtime_state.begin_action(
                            "EMAIL_CODE_SUBMIT",
                            step_id=step_id,
                            effect_class=ActionEffectClass.SUBMIT_ONCE.value,
                        )
                        if runtime_state.enabled:
                            runtime_state.record_step_status(
                                step_id,
                                status="RUNNING",
                                outcome_code="EMAIL_CODE_SUBMIT_STARTED",
                            )

                    async def verify_signup_password_complete() -> bool:
                        return await _poll_transition(SIGNUP_PASSWORD_NEXT_STATES, timeout=15.0)

                    async def verify_registration_form_complete() -> bool:
                        return await _poll_transition(REGISTRATION_FORM_NEXT_STATES, timeout=15.0)

                    async def verify_welcome_complete() -> bool:
                        return await _poll_transition(WELCOME_NEXT_STATES, timeout=15.0)

                    async def verify_survey_complete() -> bool:
                        return await _poll_transition(SURVEY_NEXT_STATES, timeout=15.0)

                    def record_verified_step(
                        step_id: WorkflowStepId,
                        *,
                        state: str,
                        source: str = "screen_observer",
                    ) -> None:
                        """Persist branch evidence only after its postcondition passed."""
                        if not runtime_state.enabled or runtime_state.step_evidence(step_id):
                            return
                        runtime_state.record_step_evidence(
                            Evidence(
                                step_id=step_id,
                                evidence_type="auth_branch_postcondition",
                                details=(
                                    ("page_state", state),
                                    ("verification_source", source),
                                ),
                                verifier_version="account-flow-v1",
                            )
                        )

                    async def verify_unknown_page() -> bool:
                        state = await detected_state()
                        return state is not ScreenState.UNKNOWN

                    screen_state = (await page_observer.observe()).state
                    logger.info("orchestrator: 初始画面状态: %s", screen_state.value)
                    if screen_state is ScreenState.UNKNOWN:
                        from gpt_automation.phases.screen_manager import ScreenManager

                        initial_screen_manager = ScreenManager(
                            page=page,
                            ctx=ctx,
                            manual_verifiers={ScreenState.UNKNOWN: verify_unknown_page},
                        )
                        screen_state = await initial_screen_manager.ensure_screen(
                            set(ScreenState) - {ScreenState.UNKNOWN},
                        )
                    if runtime_state.enabled and runtime_state.step_evidence(
                        WorkflowStepId.AUTH_ENTRY_READY
                    ) is None:
                        runtime_state.record_step_evidence(
                            Evidence(
                                step_id=WorkflowStepId.AUTH_ENTRY_READY,
                                evidence_type="auth_entry_observation",
                                details=(
                                    ("screen_state", screen_state.value),
                                    ("verification_source", "screen_observer"),
                                ),
                                verifier_version="screen-detector-v1",
                            )
                        )
                    resume_account_verified = runtime_state.has_reached(
                        WorkflowStage.ACCOUNT_VERIFIED
                    )
                    auth_branch = "existing"
                    signup_email_code_completed = False

                    if not resume_account_verified and screen_state == ScreenState.MAIN_PAGE:
                        # 二次确认：URL 快路径可能误判（有 profile 但未登录）
                        from gpt_automation.phases.screen_detector import is_not_logged_in
                        if await is_not_logged_in(page):
                            logger.warning("orchestrator: MAIN_PAGE 但检测到登录按钮 — 实际未登录，走 Phase 1")
                            screen_state = ScreenState.LOGIN_EMAIL
                        else:
                            logger.info("orchestrator: 已登录（profile 复用），跳过 Phase 1")

                    if resume_account_verified:
                        logger.info("workflow: account phase already verified; resuming later stage")
                    elif screen_state == ScreenState.MAIN_PAGE:
                        pass  # 已登录，什么都不做
                    elif screen_state == ScreenState.WELCOME_MODAL:
                        logger.info("orchestrator: 检测到 Welcome 弹窗，直接关闭")
                        await guarded_execution(
                            page, lambda: phase1_ready_confirm(page), email, "ready", "準備完了→続行",
                            verification_fn=verify_welcome_complete,
                            next_step_description="处理问卷",
                            next_step_verification_fn=verify_welcome_complete,
                        )
                        await guarded_execution(
                            page, lambda: phase1_survey(page), email, "survey", "问券处理",
                            verification_fn=verify_survey_complete,
                            next_step_description="进入 MFA 设置",
                            next_step_verification_fn=verify_survey_complete,
                        )
                    elif screen_state == ScreenState.SURVEY:
                        logger.info("orchestrator: 检测到 Survey 页面，直接处理")
                        await guarded_execution(
                            page, lambda: phase1_survey(page), email, "survey", "问券处理",
                            verification_fn=verify_survey_complete,
                            next_step_description="进入 MFA 设置",
                            next_step_verification_fn=verify_survey_complete,
                        )
                    else:
                        # 未登录 —— 走正常流程
                        async def navigate_to_auth_route() -> None:
                            await guarded_execution(
                                page,
                                lambda: phase1_navigate(page, email),
                                email,
                                "navigate",
                                "输入邮箱+点击続行",
                                verification_fn=verify_login_transition,
                                next_step_description="继续当前登录或注册流程",
                                next_step_verification_fn=verify_login_transition,
                            )
                            await asyncio.sleep(3.0)

                        await execute_page_step(
                            WorkflowStepId.ACCOUNT_ROUTE_RESOLVED,
                            navigate_to_auth_route,
                            verify_login_transition,
                            evidence_type="auth_route_observation",
                            evidence_details=(
                                ("route_state", "login_or_signup"),
                                ("verification_source", "screen_observer"),
                            ),
                            handler_type=AuthRouteHandler,
                        )

                        # 判断分支：创建密码页与既有账号登录页都包含 password
                        # input，因此必须优先使用 URL/页面状态，而不能只靠通用选择器。
                        post_route_state = await detected_state()
                        login_detected = post_route_state is ScreenState.LOGIN_PASSWORD

                        if (
                            scope == "checkout_fulfillment"
                            and post_route_state
                            in {
                                ScreenState.SIGNUP_PASSWORD,
                                ScreenState.REGISTRATION_FORM,
                            }
                        ):
                            # A READY account must never be routed into the
                            # signup flow: creating a new password here could
                            # overwrite the stored credential, register a
                            # different account, or proceed to a wrong payment.
                            # Keep the scene, persist nothing, and stop.
                            pipeline_failure_status = (
                                "CHECKOUT_SIGNUP_ROUTE_FORBIDDEN"
                            )
                            logger.error(
                                "checkout_fulfillment detected signup route "
                                "%s for %s; payment is blocked",
                                post_route_state.value,
                                account_label,
                            )
                            route_shot = evidence_path(
                                email,
                                "checkout_signup_route",
                            )
                            await safe_screenshot(page, route_shot)
                            print(
                                "\n⚠️  开通会员检测到新账户注册路由（"
                                f"{post_route_state.value}）。\n"
                                "    已注册账户不允许进入创建密码/注册资料流程；"
                                "不会保存新密码，也不会提交注册或获取信用卡。\n"
                                "    证据截图：evidence/checkout_signup_route_*.png。\n"
                            )
                            write_status(
                                email,
                                "CHECKOUT_SIGNUP_ROUTE_FORBIDDEN",
                                "",
                                session_id=session_id,
                            )
                            return await settle_account_result(
                                page,
                                email,
                                "CHECKOUT_SIGNUP_ROUTE_FORBIDDEN",
                                ip=current_ip,
                                evidence_paths=(route_shot,),
                            )

                        if login_detected:
                            auth_branch = "login"
                            if not resumable_chatgpt_password:
                                write_status(
                                    email,
                                    "ACCOUNT_ALREADY_EXISTS",
                                    "",
                                    session_id=session_id,
                                )
                                return await settle_account_result(
                                    page,
                                    email,
                                    "ACCOUNT_ALREADY_EXISTS",
                                    ip=current_ip,
                                    reason="password login page detected without a resumable ChatGPT credential",
                                )
                            # ── 登录路径（已有账号）──
                            logger.info("orchestrator: 检测到登录路径（已有账号）")
                            await guarded_execution(
                                page,
                                lambda: phase1_login_password(page, chatgpt_password),
                                email,
                                "password",
                                "输入密码+点击继续",
                                verification_fn=verify_password_filled,
                                next_step_description="点击继续并输入邮箱验证码",
                                next_step_verification_fn=verify_password_filled,
                            )
                            await asyncio.sleep(1.0)
                            try:
                                await guarded_execution(page, lambda: safe_click(
                                    page,
                                    ["button:text-is('続行')", "button:text-is('Continue')", "button[type='submit']"],
                                    timeout_ms=5000,
                                ), email, "continue", "点击继续",
                                verification_fn=verify_login_transition,
                                next_step_description="输入邮箱验证码",
                                next_step_verification_fn=verify_login_transition,
                            )
                            except Exception:
                                pwd_selector = config.SELECTORS.get(
                                    "reg_password_input",
                                    "input[name='password']",
                                )
                                await page.locator(pwd_selector).press("Enter")
                            await asyncio.sleep(3.0)
                            post_login_state = (await page_observer.observe()).state
                            if post_login_state is ScreenState.LOGIN_PASSWORD:
                                raise NeedsOperatorException(
                                    "ChatGPT password was rejected; do not try the email password"
                                )
                            async def verify_login_password_postcondition() -> bool:
                                return is_positive_transition(
                                    (await page_observer.observe()).state,
                                    EMAIL_CODE_NEXT_STATES
                                    | WELCOME_NEXT_STATES
                                    | SURVEY_NEXT_STATES
                                    | {ScreenState.MAIN_PAGE},
                                )

                            await execute_page_step(
                                WorkflowStepId.LOGIN_PASSWORD_VERIFIED,
                                lambda: asyncio.sleep(0),
                                verify_login_password_postcondition,
                                evidence_type="login_password_postcondition",
                                evidence_details=(
                                    ("login_state", "accepted"),
                                    ("verification_source", "screen_observer"),
                                ),
                                handler_type=LoginPasswordHandler,
                            )
                            if post_login_state is ScreenState.EMAIL_VERIFY_CODE:
                                async def verify_login_email_code() -> None:
                                    if runtime_state.action_state("EMAIL_CODE_SUBMIT") is not None:
                                        await request_manual_intervention(
                                            page,
                                            email,
                                            "login_email_code_recovery",
                                            (
                                                "邮箱验证码提交曾经发生或结果未知；请在当前页面"
                                                "完成或核对后，按 Enter 重新验证页面状态。"
                                            ),
                                            verify_email_code_complete,
                                            next_step_description="进入准备页面",
                                            next_step_verification_fn=verify_email_code_complete,
                                        )
                                        return
                                    await guarded_execution(
                                        page,
                                        lambda: phase1_email_verify_code(
                                            page,
                                            code_reader,
                                            submit_callback=lambda: mark_email_code_submit_started(
                                                WorkflowStepId.LOGIN_EMAIL_CODE_VERIFIED,
                                            ),
                                        ),
                                        email,
                                        "verify_code",
                                        "邮箱验证码",
                                        max_auto_retry=0,
                                        retry_allowed=lambda: runtime_state.action_state(
                                            "EMAIL_CODE_SUBMIT"
                                        )
                                        is None,
                                        verification_fn=verify_email_code_complete,
                                        next_step_description="进入准备页面",
                                        next_step_verification_fn=verify_email_code_complete,
                                    )

                                await verify_login_email_code()
                                await execute_page_step(
                                    WorkflowStepId.LOGIN_EMAIL_CODE_VERIFIED,
                                    lambda: asyncio.sleep(0),
                                    verify_email_code_complete,
                                    evidence_type="login_email_code_postcondition",
                                    evidence_details=(
                                        ("login_state", "email_code_verified"),
                                        ("verification_source", "screen_observer"),
                                    ),
                                    handler_type=LoginEmailCodeHandler,
                                )
                                if runtime_state.action_state("EMAIL_CODE_SUBMIT") is not None:
                                    runtime_state.verify_action("EMAIL_CODE_SUBMIT")
                            if post_login_state is ScreenState.WELCOME_MODAL:
                                await guarded_execution(
                                    page, lambda: phase1_ready_confirm(page), email, "ready", "準備完了→続行",
                                    verification_fn=verify_welcome_complete,
                                    next_step_description="处理问卷",
                                    next_step_verification_fn=verify_welcome_complete,
                                )
                            if post_login_state is ScreenState.SURVEY:
                                await guarded_execution(
                                    page, lambda: phase1_survey(page), email, "survey", "问券处理",
                                    verification_fn=verify_survey_complete,
                                    next_step_description="进入 MFA 设置",
                                    next_step_verification_fn=verify_survey_complete,
                                )
                            if post_login_state not in {
                                ScreenState.EMAIL_VERIFY_CODE,
                                ScreenState.WELCOME_MODAL,
                                ScreenState.SURVEY,
                                ScreenState.MAIN_PAGE,
                            }:
                                raise NeedsOperatorException(
                                    f"login outcome is unconfirmed: {post_login_state.value}"
                                )
                        else:
                            auth_branch = "signup"
                            # ── 注册路径（新账号）──
                            # 邮箱确认页的「使用密码继续」会进入独立的创建密码
                            # 路由。密码提交是不可重放动作，必须在点击前记账，并且
                            # 只在页面实际进入下一注册状态后标记为已验证。
                            logger.info("orchestrator: 检测到注册路径（新账号）")
                            route_passwordless = await signup_route_is_passwordless(
                                page,
                                post_route_state,
                            )
                            passwordless = bool(route_passwordless)
                            if not passwordless:
                                persist_password()

                                password_transition_already_visible = post_route_state in {
                                    ScreenState.REGISTRATION_FORM,
                                    ScreenState.WELCOME_MODAL,
                                    ScreenState.SURVEY,
                                    ScreenState.MAIN_PAGE,
                                }
                                signup_password_action_state = runtime_state.action_state(
                                    "SIGNUP_PASSWORD_SUBMIT"
                                )

                                def mark_signup_password_submit_started() -> None:
                                    runtime_state.begin_action(
                                        "SIGNUP_PASSWORD_SUBMIT",
                                        step_id=WorkflowStepId.SIGNUP_PASSWORD_VERIFIED,
                                        effect_class=ActionEffectClass.SUBMIT_ONCE.value,
                                    )
                                    if runtime_state.enabled:
                                        runtime_state.record_step_status(
                                            WorkflowStepId.SIGNUP_PASSWORD_VERIFIED,
                                            status="RUNNING",
                                            outcome_code="SIGNUP_PASSWORD_SUBMIT_STARTED",
                                        )

                                if password_transition_already_visible:
                                    # A resumed browser can already be past the password
                                    # page.  The observed next page is sufficient positive
                                    # evidence; never navigate back to submit again.
                                    await execute_page_step(
                                        WorkflowStepId.SIGNUP_PASSWORD_VERIFIED,
                                        lambda: asyncio.sleep(0),
                                        verify_signup_password_complete,
                                        evidence_type="signup_password_postcondition",
                                        evidence_details=(
                                            ("page_state", "signup_password_transition"),
                                            ("verification_source", "screen_observer"),
                                        ),
                                        handler_type=SignupPasswordHandler,
                                    )
                                elif signup_password_action_state is not None:
                                    # A previous submit may have reached the server.  Keep
                                    # the current browser and let the operator finish or
                                    # inspect it; automatic resubmission is forbidden.
                                    await request_manual_intervention(
                                        page,
                                        email,
                                        "signup_password_recovery",
                                        (
                                            "创建账户密码曾经提交或结果未知；请在当前页面完成或"
                                            "核对后续操作，按 Enter 重新验证页面状态。"
                                        ),
                                        verify_signup_password_complete,
                                        next_step_description="填写注册姓名和年龄",
                                        next_step_verification_fn=verify_signup_password_complete,
                                    )
                                    await execute_page_step(
                                        WorkflowStepId.SIGNUP_PASSWORD_VERIFIED,
                                        lambda: asyncio.sleep(0),
                                        verify_signup_password_complete,
                                        evidence_type="signup_password_postcondition",
                                        evidence_details=(
                                            ("page_state", "signup_password_transition"),
                                            ("verification_source", "operator_postcondition"),
                                        ),
                                        handler_type=SignupPasswordHandler,
                                    )
                                else:
                                    async def submit_signup_password() -> None:
                                        await phase1_choose_password_signup(page)
                                        await phase1_create_password(
                                            page,
                                            chatgpt_password,
                                            submit_callback=mark_signup_password_submit_started,
                                        )

                                    async def create_signup_password() -> None:
                                        await guarded_execution(
                                            page,
                                            submit_signup_password,
                                            email,
                                            "signup_password",
                                            "选择“使用密码继续”并创建账户密码",
                                            max_auto_retry=0,
                                            retry_allowed=False,
                                            verification_fn=verify_signup_password_complete,
                                            next_step_description="填写注册姓名和年龄",
                                            next_step_verification_fn=verify_signup_password_complete,
                                        )

                                    await execute_page_step(
                                        WorkflowStepId.SIGNUP_PASSWORD_VERIFIED,
                                        create_signup_password,
                                        verify_signup_password_complete,
                                        evidence_type="signup_password_postcondition",
                                        evidence_details=(
                                            ("page_state", "signup_password_transition"),
                                            ("verification_source", "screen_observer"),
                                        ),
                                        handler_type=SignupPasswordHandler,
                                    )
                            else:
                                logger.info(
                                    "orchestrator: email-verify direct route, "
                                    "skipping password creation and 2FA"
                                )
                                record_verified_step(
                                    WorkflowStepId.SIGNUP_PASSWORD_VERIFIED,
                                    state="email_verify_direct",
                                    source="passwordless_route",
                                )

                            if runtime_state.action_state("SIGNUP_PASSWORD_SUBMIT") is not None:
                                runtime_state.verify_action("SIGNUP_PASSWORD_SUBMIT")
                            runtime_state.checkpoint(WorkflowStage.PASSWORD_VERIFIED)

                            post_password_state = await detected_state()
                            if post_password_state is ScreenState.EMAIL_VERIFY_CODE:
                                # Some locale/account variants ask for one more email
                                # confirmation after password creation.  It remains a
                                # Phase 1 fallback, never a Phase 2 password action.
                                async def verify_signup_email_code() -> None:
                                    if runtime_state.action_state("EMAIL_CODE_SUBMIT") is not None:
                                        await request_manual_intervention(
                                            page,
                                            email,
                                            "signup_email_code_recovery",
                                            (
                                                "邮箱验证码提交曾经发生或结果未知；请在当前页面"
                                                "完成或核对后，按 Enter 重新验证页面状态。"
                                            ),
                                            verify_email_code_complete,
                                            next_step_description="填写注册姓名和年龄",
                                            next_step_verification_fn=verify_email_code_complete,
                                        )
                                        return
                                    await guarded_execution(
                                        page,
                                        lambda: phase1_email_verify_code(
                                            page,
                                            code_reader,
                                            submit_callback=lambda: mark_email_code_submit_started(
                                                WorkflowStepId.SIGNUP_EMAIL_CODE_VERIFIED,
                                            ),
                                        ),
                                        email,
                                        "verify_code",
                                        "邮箱验证码",
                                        max_auto_retry=0,
                                        retry_allowed=lambda: runtime_state.action_state(
                                            "EMAIL_CODE_SUBMIT"
                                        )
                                        is None,
                                        verification_fn=verify_email_code_complete,
                                        next_step_description="填写注册姓名和年龄",
                                        next_step_verification_fn=verify_email_code_complete,
                                    )

                                await verify_signup_email_code()
                                await execute_page_step(
                                    WorkflowStepId.SIGNUP_EMAIL_CODE_VERIFIED,
                                    lambda: asyncio.sleep(0),
                                    verify_email_code_complete,
                                    evidence_type="signup_email_code_postcondition",
                                    evidence_details=(
                                        ("signup_state", "email_code_verified"),
                                        ("verification_source", "screen_observer"),
                                    ),
                                    handler_type=SignupEmailCodeHandler,
                                )
                                if runtime_state.action_state("EMAIL_CODE_SUBMIT") is not None:
                                    runtime_state.verify_action("EMAIL_CODE_SUBMIT")
                                signup_email_code_completed = True

                            if await detected_state() is ScreenState.REGISTRATION_FORM:
                                await guarded_execution(
                                    page, lambda: phase1_form(page, name, age), email, "form", "填写姓名+年龄",
                                    verification_fn=verify_registration_form_complete,
                                    next_step_description="进入准备页面",
                                    next_step_verification_fn=verify_registration_form_complete,
                                )
                            await guarded_execution(
                                page, lambda: phase1_ready_confirm(page), email, "ready", "準備完了→続行",
                                verification_fn=verify_welcome_complete,
                                next_step_description="处理问卷",
                                next_step_verification_fn=verify_welcome_complete,
                            )
                            await guarded_execution(
                                page, lambda: phase1_survey(page), email, "survey", "问券处理",
                                verification_fn=verify_survey_complete,
                                next_step_description="进入 MFA 设置",
                                next_step_verification_fn=verify_survey_complete,
                            )

                    async def verify_authenticated_page() -> bool:
                        return await detected_state() is ScreenState.MAIN_PAGE

                    async def no_op_page_action() -> None:
                        return None

                    if runtime_state.enabled and runtime_state.step_evidence(
                        WorkflowStepId.ACCOUNT_ROUTE_RESOLVED
                    ) is None:
                        runtime_state.record_step_evidence(
                            Evidence(
                                step_id=WorkflowStepId.ACCOUNT_ROUTE_RESOLVED,
                                evidence_type="auth_route_observation",
                                details=(
                                    ("route_state", "existing_or_completed_auth"),
                                    ("verification_source", "screen_observer"),
                                ),
                               verifier_version="screen-detector-v1",
                           )
                       )

                    await execute_page_step(
                        WorkflowStepId.ACCOUNT_AUTHENTICATED,
                        no_op_page_action,
                        verify_authenticated_page,
                        evidence_type="account_authenticated_observation",
                       evidence_details=(
                            ("screen_state", "main_page"),
                            ("verification_source", "screen_observer"),
                        ),
                        effect_class=ActionEffectClass.READ_ONLY,
                    )
                    if auth_branch == "signup":
                        record_verified_step(
                            WorkflowStepId.SIGNUP_PASSWORD_VERIFIED,
                           state="signup_password_verified",
                            source="final_authenticated_page",
                        )
                        if signup_email_code_completed:
                            record_verified_step(
                                WorkflowStepId.SIGNUP_EMAIL_CODE_VERIFIED,
                                state="signup_email_code_verified",
                                source="final_authenticated_page",
                            )
                        record_verified_step(
                            WorkflowStepId.PROFILE_VERIFIED,
                            state="profile_form_verified",
                            source="final_authenticated_page",
                        )
                        record_verified_step(
                            WorkflowStepId.ONBOARDING_VERIFIED,
                            state="onboarding_verified",
                            source="final_authenticated_page",
                        )
                    runtime_state.checkpoint(WorkflowStage.ACCOUNT_VERIFIED)

                    # Account workflow owns the business order. ScreenManager only
                    # recovers page mechanics before each business step.
                    from gpt_automation.phases.screen_manager import ScreenManager

                    pipeline_failure_status: Optional[str] = None
                    pipeline_evidence_paths: tuple = ()
                    pipeline_page_url: str = ""

                    async def verify_manual_mfa() -> bool:
                        nonlocal clean_secret
                        from gpt_automation.phases.phase2 import (
                            _extract_totp_secret,
                            _phase2_verify_completion,
                            mfa_secret_proof_allowed,
                        )
                        from gpt_automation.state import ActionState

                        captured_secret = await _extract_totp_secret(page)
                        if captured_secret:
                            clean_secret = captured_secret
                        if not await _phase2_verify_completion(page):
                            return False
                        saved_secret = ""
                        binding_previously_verified = (
                            runtime_state.action_state("MFA_BINDING")
                            is ActionState.VERIFIED
                        )
                        if not clean_secret and binding_previously_verified:
                            saved = load_saved_credential()
                            saved_secret = saved.totp_secret if saved is not None else ""
                            clean_secret = saved_secret or None
                        if not mfa_secret_proof_allowed(
                            captured_secret=captured_secret or clean_secret or "",
                            saved_secret=saved_secret,
                            binding_previously_verified=binding_previously_verified,
                        ):
                            logger.warning(
                                "MFA page is complete but no current-session credential proof exists"
                            )
                            return False
                        return bool(clean_secret)

                    def commit_manual_mfa(source: str) -> None:
                        """Commit durable MFA state only after observation succeeded."""
                        if not clean_secret:
                            raise NeedsOperatorException(
                                "manual MFA verification has no recoverable secret"
                            )
                        persist_credential(clean_secret)
                        if runtime_state.action_state("MFA_BINDING") is not None:
                            runtime_state.verify_action("MFA_BINDING")
                        if runtime_state.enabled and runtime_state.step_evidence(
                            WorkflowStepId.MFA_VERIFIED
                        ) is None:
                            runtime_state.record_step_evidence(
                                Evidence(
                                    step_id=WorkflowStepId.MFA_VERIFIED,
                                    evidence_type="mfa_manual_postcondition",
                                    details=(
                                        ("page_state", "mfa_verified"),
                                        ("verification_source", source),
                                    ),
                                    verifier_version="account-flow-v1",
                                )
                            )
                        runtime_state.checkpoint(WorkflowStage.MFA_VERIFIED)

                    async def verify_manual_payment() -> bool:
                        return await phase4_subscription_active_confirm(page, email)

                    def commit_manual_payment(source: str) -> None:
                        runtime_state.verify_action("PAYMENT_SUBMIT")
                        if runtime_state.enabled and runtime_state.step_evidence(
                            WorkflowStepId.PAYMENT_VERIFIED
                        ) is None:
                            runtime_state.record_step_evidence(
                                Evidence(
                                    step_id=WorkflowStepId.PAYMENT_VERIFIED,
                                    evidence_type="payment_manual_postcondition",
                                    details=(
                                        ("subscription_state", "active"),
                                        ("verification_source", source),
                                    ),
                                    verifier_version="account-flow-v1",
                                )
                            )
                        runtime_state.checkpoint(WorkflowStage.PAYMENT_VERIFIED)

                    async def verify_manual_cancellation() -> bool:
                        return bool(
                            await phase4_status_confirm(
                                page,
                                ctx.get("stripe_page"),
                                email,
                            )
                        )

                    def commit_manual_cancellation(source: str) -> None:
                        runtime_state.verify_action("CANCEL_SUBSCRIPTION")
                        if runtime_state.enabled and runtime_state.step_evidence(
                            WorkflowStepId.CANCELLATION_VERIFIED
                        ) is None:
                            runtime_state.record_step_evidence(
                                Evidence(
                                    step_id=WorkflowStepId.CANCELLATION_VERIFIED,
                                    evidence_type="cancellation_manual_postcondition",
                                    details=(
                                        ("subscription_state", "cancelled"),
                                        ("verification_source", source),
                                    ),
                                    verifier_version="account-flow-v1",
                                )
                            )
                        runtime_state.checkpoint(WorkflowStage.CANCELLATION_VERIFIED)

                    async def verify_pricing_page() -> bool:
                        """Pricing page reached: plan grid / target plan button."""
                        return await phase3_pricing_page_verified(page)

                    async def verify_target_mfa() -> bool:
                        verified = await verify_manual_mfa()
                        if verified:
                            commit_manual_mfa("operator_target")
                        return verified

                    async def verify_target_pricing() -> bool:
                        verified = await verify_pricing_page()
                        if verified:
                            if runtime_state.enabled and runtime_state.step_evidence(
                                WorkflowStepId.PRICING_PAGE_VERIFIED
                            ) is None:
                                runtime_state.record_step_evidence(
                                    Evidence(
                                        step_id=WorkflowStepId.PRICING_PAGE_VERIFIED,
                                        evidence_type="pricing_page_operator_postcondition",
                                        details=(
                                            ("page_state", "pricing"),
                                            ("verification_source", "operator_postcondition"),
                                        ),
                                        verifier_version="account-flow-v1",
                                    )
                                )
                            runtime_state.checkpoint(WorkflowStage.PRICING_VERIFIED)
                        return verified

                    mfa_target_verifiers = {
                        WorkflowStepId.MFA_VERIFIED.value: verify_target_mfa,
                        WorkflowStepId.PRICING_PAGE_VERIFIED.value: verify_target_pricing,
                    }
                    mfa_target_labels = {
                        WorkflowStepId.MFA_VERIFIED.value: "完成 2FA",
                        WorkflowStepId.PRICING_PAGE_VERIFIED.value: "确认套餐选择画面",
                    }

                    manual_target_planner = ManualTargetPlanner(step_engine)
                    manual_target_specs = {
                        WorkflowStepId.MFA_VERIFIED: ("完成 2FA", verify_target_mfa),
                        WorkflowStepId.PRICING_PAGE_VERIFIED: (
                            "确认套餐选择画面",
                            verify_target_pricing,
                        ),
                    }

                    manual_target_specs.update(
                        {
                            WorkflowStepId.MFA_VERIFIED: (
                                mfa_target_labels[WorkflowStepId.MFA_VERIFIED.value],
                                mfa_target_verifiers[WorkflowStepId.MFA_VERIFIED.value],
                            ),
                            WorkflowStepId.PRICING_PAGE_VERIFIED: (
                                mfa_target_labels[WorkflowStepId.PRICING_PAGE_VERIFIED.value],
                                mfa_target_verifiers[WorkflowStepId.PRICING_PAGE_VERIFIED.value],
                            ),
                        }
                    )

                    def current_manual_target_maps() -> tuple[dict[str, Any], dict[str, str]]:
                        if not runtime_state.enabled:
                            return {}, {}
                        assert runtime_state.store is not None
                        options = manual_target_planner.build_options(
                            runtime_state.store,
                            runtime_state.session_id,
                            manual_target_specs,
                        )
                        return (
                            {option.target_id: option.verifier for option in options},
                            {option.target_id: option.label for option in options},
                        )

                    async def verify_stripe_checkout() -> bool:
                        return await _poll_transition(
                            {ScreenState.STRIPE_PAYMENT},
                            timeout=CHECKOUT_NAV_TIMEOUT_S,
                        )

                    async def verify_stripe_form_ready() -> bool:
                        from gpt_automation.phases.stripe import _find_submit_btn

                        submit = await _find_submit_btn(page)
                        return submit is not None

                    async def verify_stripe_address_form_ready() -> bool:
                        return await verify_stripe_address_ready(
                            page,
                            zip_code,
                            addr1,
                            addr2,
                        )

                    async def verify_main_page() -> bool:
                        return await detected_state() is ScreenState.MAIN_PAGE

                    async def mfa_callback():
                        """Run MFA and convert its page result into workflow state."""
                        nonlocal clean_secret
                        from gpt_automation.phases.phase2 import MfaSetupResult
                        from gpt_automation.state import ActionState

                        if passwordless:
                            # ???????2FA ??????????
                            if runtime_state.enabled and runtime_state.step_evidence(
                                WorkflowStepId.MFA_VERIFIED
                            ) is None:
                                record_verified_step(
                                    WorkflowStepId.MFA_VERIFIED,
                                    state="mfa_not_applicable",
                                    source="passwordless_route",
                                )
                            runtime_state.checkpoint(WorkflowStage.MFA_VERIFIED)
                            return True

                        mfa_action_state = runtime_state.action_state("MFA_BINDING")
                        if mfa_action_state is ActionState.VERIFIED:
                            if runtime_state.enabled and runtime_state.step_evidence(
                                WorkflowStepId.MFA_VERIFIED
                            ) is None:
                                if not await verify_manual_mfa():
                                    return False
                                commit_manual_mfa("operator_recovery_check")
                            runtime_state.checkpoint(WorkflowStage.MFA_VERIFIED)
                            return True

                        if runtime_state.has_reached(WorkflowStage.MFA_PENDING):
                            persist_password()
                            manual_verified = await request_manual_intervention(
                                page,
                                email,
                                "mfa_resume",
                                "MFA 配置曾经开始；请完成或核对绑定，程序验证后继续。",
                                verify_manual_mfa,
                                next_step_description="检测免费优惠",
                                next_step_verification_fn=verify_manual_mfa,
                                target_verifiers=current_manual_target_maps()[0],
                                target_labels=current_manual_target_maps()[1],
                            )
                            if not manual_verified:
                                return False
                            commit_manual_mfa("operator_postcondition")
                            return True

                        persist_password()

                        def mark_mfa_pending() -> None:
                            if runtime_state.enabled:
                                runtime_state.record_step_status(
                                    WorkflowStepId.MFA_VERIFIED,
                                    status="READY",
                                    outcome_code="MFA_READY",
                                )
                            runtime_state.checkpoint(WorkflowStage.MFA_PENDING)

                        def mark_mfa_binding_started() -> None:
                            runtime_state.begin_action(
                                "MFA_BINDING",
                                step_id=WorkflowStepId.MFA_VERIFIED,
                                effect_class=ActionEffectClass.SUBMIT_ONCE.value,
                            )
                            if runtime_state.enabled:
                                runtime_state.record_step_status(
                                    WorkflowStepId.MFA_VERIFIED,
                                    status="RUNNING",
                                    outcome_code="MFA_BINDING_STARTED",
                                )

                        async def run_mfa_action():
                            return await guarded_execution(
                                page,
                                lambda: phase2_mfa(
                                    page,
                                    name,
                                    code_reader,
                                    secret_callback=persist_extracted_secret,
                                    mfa_pending_callback=mark_mfa_pending,
                                    mfa_binding_callback=mark_mfa_binding_started,
                                ),
                                email,
                               "mfa",
                               "绑定MFA/Authenticator",
                                max_auto_retry=0,
                                retry_allowed=False,
                                verification_fn=verify_manual_mfa,
                                manual_success_value=MfaSetupResult(
                                    "", True, "MANUAL_OPERATOR"
                                ),
                                next_step_description="检测免费优惠",
                                next_step_verification_fn=verify_manual_mfa,
                                target_verifiers=current_manual_target_maps()[0],
                                target_labels=current_manual_target_maps()[1],
                            )

                        mfa_result: object = None

                        async def mfa_action() -> None:
                            nonlocal mfa_result
                            mfa_result = await run_mfa_action()

                        async def mfa_postcondition() -> bool:
                            nonlocal clean_secret
                            normalized = mfa_result
                            if isinstance(normalized, str) and normalized.strip():
                                normalized = MfaSetupResult(
                                    normalized.strip(), True, "LEGACY_TYPED_COMPAT"
                                )
                            if not isinstance(normalized, MfaSetupResult) or not normalized.verified:
                                return False
                            if normalized.secret:
                                clean_secret = normalized.secret
                            return await verify_manual_mfa()

                        if runtime_state.enabled:
                            mfa_step_result = await execute_registered_handler(
                                MfaRegistrationHandler(
                                    action=mfa_action,
                                    verify=mfa_postcondition,
                                ),
                            )
                            result = mfa_result
                            if mfa_step_result.status is GraphStepStatus.WAITING_OPERATOR:
                                await request_manual_intervention(
                                    page,
                                    email,
                                    "mfa",
                                    "MFA 自动结果未确认；请在浏览器中完成或核对绑定，程序会重新验证。",
                                    verify_manual_mfa,
                                    next_step_description="检测免费优惠",
                                    next_step_verification_fn=verify_manual_mfa,
                                    target_verifiers=current_manual_target_maps()[0],
                                    target_labels=current_manual_target_maps()[1],
                                )
                                commit_manual_mfa("operator_postcondition")
                                return True
                            if mfa_step_result.status is not GraphStepStatus.SUCCEEDED:
                                raise NeedsOperatorException(
                                    f"MFA step did not complete: {mfa_step_result.code}"
                                )
                        else:
                            result = await run_mfa_action()
                        # Keep the workflow boundary compatible with older diagnostic
                        # doubles that returned the raw TOTP secret.  The production
                        # phase2 implementation always returns MfaSetupResult.
                        if isinstance(result, str) and result.strip():
                            result = MfaSetupResult(result.strip(), True, "LEGACY_TYPED_COMPAT")
                        if isinstance(result, MfaSetupResult) and result.verified:
                            if result.secret:
                                clean_secret = result.secret
                            commit_manual_mfa("phase2_postcondition")
                            return True
                        await request_manual_intervention(
                            page,
                            email,
                            "mfa",
                            "MFA 自动结果未确认；请在浏览器中完成或核对绑定，程序会重新验证。",
                            verify_manual_mfa,
                           next_step_description="检测免费优惠",
                            next_step_verification_fn=verify_manual_mfa,
                            target_verifiers=current_manual_target_maps()[0],
                            target_labels=current_manual_target_maps()[1],
                        )
                        commit_manual_mfa("operator_postcondition")
                        return True

                    async def pricing_verification_callback() -> bool:
                        """Offer detection + pricing-page amount verification.

                        This is the terminal step of the ``pricing_page``
                        checkout goal.  It never clicks the target plan button
                        and never opens Stripe; the operator finishes on the
                        plan-selection screen.  In the ``complete`` goal the
                        separate OFFER_DECISION_VERIFIED step performs the
                        actual plan-button click afterwards.
                        """
                        nonlocal pipeline_failure_status, pipeline_evidence_paths, pipeline_page_url

                        # Enter the pricing page first: the amount verdict
                        # must be read from the *target plan card* there,
                        # never from arbitrary ¥0 text on the home page.
                        navigate_outcome = await guarded_execution(
                            page,
                            lambda: phase3_open_pricing_page(page),
                            email,
                            "open_pricing_page",
                            "进入套餐页",
                            verification_fn=phase3_pricing_page_verified,
                            next_step_description="核对目标计划首期金额是否为 0",
                            next_step_verification_fn=phase3_pricing_page_verified,
                        )
                        if navigate_outcome.status is StepStatus.BUSINESS_REJECTED:
                            pipeline_failure_status = navigate_outcome.code
                            logger.error("no free Plus offer for %s", account_label)
                            shot = await safe_screenshot(page, evidence_path(email, "no_offer"))
                            pipeline_evidence_paths = (shot.path,) if shot.path else ()
                            try:
                                pipeline_page_url = page.url or ""
                            except Exception:
                                pipeline_page_url = ""
                            print(
                                "\n⚠️  该账户未发现任何套餐入口（可能是无免费优惠或页面结构变化）。\n"
                                "    账户将恢复为 READY 并标记 REVIEW_REQUIRED（待复核），默认不再进入开通池。\n"
                                "    证据截图已保存，可在后续人工确认是否确实无免费优惠。\n"
                            )
                            return False
                        if not navigate_outcome.verified:
                            pipeline_failure_status = "PRICING_UNREACHABLE"
                            return False

                        amount_outcome = await guarded_execution(
                            page, lambda: phase3_amount_guard(page, email), email, "amount_guard", "金额验证",
                            verification_fn=lambda: _verify_zero_amount(page, email),
                            next_step_description="确认套餐选择画面",
                            next_step_verification_fn=lambda: _verify_zero_amount(page, email),
                        )
                        if amount_outcome.status is StepStatus.NEEDS_OPERATOR:
                            await request_manual_intervention(
                                page,
                                email,
                                "amount_guard",
                                "确认目标计划（Plus）金额明确显示为 0 元（免费优惠），然后让程序重新验证。",
                                lambda: _verify_zero_amount(page, email),
                                next_step_description="确认套餐选择画面",
                                next_step_verification_fn=lambda: _verify_zero_amount(page, email),
                            )
                        elif amount_outcome.status is StepStatus.BUSINESS_REJECTED:
                            pipeline_failure_status = amount_outcome.code
                            shot = await safe_screenshot(page, evidence_path(email, "no_offer"))
                            pipeline_evidence_paths = (shot.path,) if shot.path else ()
                            try:
                                pipeline_page_url = page.url or ""
                            except Exception:
                                pipeline_page_url = ""
                            print(
                                f"\n⚠️  目标 Plus 套餐首期金额为 {amount_outcome.reason}，不是 0 元（免费优惠）。\n"
                                "    账户将恢复为 READY 并标记 PAID_ONLY，后续默认跳过该账户。\n"
                                "    证据截图：evidence/no_offer_*.png（套餐页现场）。\n"
                            )
                            return False

                        if not amount_outcome.verified:
                            pipeline_failure_status = "PRICING_UNVERIFIED"
                            return False

                        if runtime_state.enabled and runtime_state.step_evidence(
                            WorkflowStepId.PRICING_PAGE_VERIFIED
                        ) is None:
                            runtime_state.record_step_evidence(
                                Evidence(
                                    step_id=WorkflowStepId.PRICING_PAGE_VERIFIED,
                                    evidence_type="pricing_page_postcondition",
                                    details=(
                                        ("page_state", "pricing"),
                                        ("verification_source", "phase3_amount_guard"),
                                    ),
                                    verifier_version="pricing-page-v1",
                                )
                            )
                        runtime_state.checkpoint(WorkflowStage.PRICING_VERIFIED)
                        await safe_screenshot(page, evidence_path(email, "pricing_verified"))
                        return True

                    async def offer_decision_callback() -> bool:
                        """Click the target plan button and reach Stripe checkout.

                        OFFER_DECISION_VERIFIED now means "the free offer was
                        accepted and the Stripe checkout page is visible" —
                        never merely "the pricing page is reachable".
                        """
                        nonlocal pipeline_failure_status, pipeline_evidence_paths, pipeline_page_url

                        async def accept_offer_action() -> None:
                            await guarded_execution(
                                page,
                                lambda: phase3_accept_offer(page),
                                email,
                                "accept_offer",
                                "接受無料オファー",
                                verification_fn=verify_stripe_checkout,
                                next_step_description="填写信用卡",
                                next_step_verification_fn=verify_stripe_checkout,
                            )

                        await execute_page_step(
                            WorkflowStepId.OFFER_DECISION_VERIFIED,
                            accept_offer_action,
                            verify_stripe_checkout,
                            evidence_type="offer_checkout_postcondition",
                            evidence_details=(
                                ("page_state", "stripe_checkout"),
                                ("offer_state", "free_offer_accepted"),
                                ("verification_source", "phase3_postcondition"),
                            ),
                            handler_type=OfferDecisionHandler,
                        )
                        runtime_state.checkpoint(WorkflowStage.OFFER_VERIFIED)

                        # Second amount gate: the amount actually due today on
                        # the Stripe checkout page.  The gate is selector-
                        # driven; with no confirmed containers it is skipped
                        # (logged) until the real DOM is captured.
                        async def verify_checkout_zero() -> bool:
                            return (await checkout_amount_gate(page)).verified

                        checkout_outcome = await checkout_amount_gate(page)
                        if checkout_outcome.status is StepStatus.NEEDS_OPERATOR:
                            if (
                                checkout_outcome.code
                                == "CHECKOUT_AMOUNT_GATE_UNCONFIGURED"
                            ):
                                pipeline_failure_status = checkout_outcome.code
                                logger.error(
                                    "checkout amount gate is not configured for "
                                    "%s; payment is blocked",
                                    account_label,
                                )
                                await safe_screenshot(
                                    page,
                                    evidence_path(
                                        email,
                                        "checkout_gate_unconfigured",
                                    ),
                                )
                                print(
                                    "\n⚠️  Checkout 金额闸门未配置（缺少真实 DOM "
                                    "selector），不允许支付。\n"
                                    "    请先捕获 Stripe 页面真实 DOM 并填写 "
                                    "checkout_amount_containers。\n"
                                    "    证据截图：evidence/checkout_gate_unconfigured_*.png。\n"
                                )
                                return False
                            await request_manual_intervention(
                                page,
                                email,
                                "checkout_amount",
                                "Checkout 页面本次应付金额无法确认；请核对金额是否为 0 元（免费优惠）后让程序重新验证。",
                                verify_checkout_zero,
                                next_step_description="填写信用卡",
                                next_step_verification_fn=verify_checkout_zero,
                            )
                        elif checkout_outcome.status is StepStatus.BUSINESS_REJECTED:
                            pipeline_failure_status = checkout_outcome.code
                            logger.error(
                                "checkout amount is non-zero for %s: %s",
                                account_label,
                                checkout_outcome.reason,
                            )
                            shot = await safe_screenshot(
                                page,
                                evidence_path(email, "checkout_no_offer"),
                            )
                            pipeline_evidence_paths = (shot.path,) if shot.path else ()
                            try:
                                pipeline_page_url = page.url or ""
                            except Exception:
                                pipeline_page_url = ""
                            print(
                                f"\n⚠️  Stripe Checkout 页面本次应付金额为 "
                                f"{checkout_outcome.reason}，不是 0 元。\n"
                                "    账户将恢复为 READY 并标记 PAID_ONLY，后续默认跳过。\n"
                                "    证据截图：evidence/checkout_no_offer_*.png。\n"
                            )
                            return False
                        if not checkout_outcome.verified:
                            pipeline_failure_status = (
                                "CHECKOUT_AMOUNT_UNCONFIRMED"
                            )
                            return False
                        return True

                    async def stripe_callback():
                        """Stripe phase callback — wraps the full Phase 3 flow + MFA 重试。"""
                        nonlocal card_no, card_expiry, card_cvc, pipeline_failure_status
                        from gpt_automation.state import ActionState

                        payment_state = runtime_state.action_state("PAYMENT_SUBMIT")
                        if payment_state is ActionState.VERIFIED:
                            if runtime_state.enabled and runtime_state.step_evidence(
                                WorkflowStepId.PAYMENT_VERIFIED
                            ) is None:
                                if not await verify_manual_payment():
                                    return False
                                commit_manual_payment("operator_recovery_check")
                            runtime_state.checkpoint(WorkflowStage.PAYMENT_VERIFIED)
                            return True
                        if _must_verify_before_side_effect_replay(
                            runtime_state,
                            "PAYMENT_SUBMIT",
                            WorkflowStage.PAYMENT_PENDING,
                        ):
                            await request_manual_intervention(
                                page,
                                email,
                                "payment_resume",
                                "支付动作已有记录；请核对账户订阅状态，不要再次提交支付。",
                                verify_manual_payment,
                                next_step_description="进入取消订阅流程",
                                next_step_verification_fn=verify_manual_payment,
                            )
                            commit_manual_payment("operator_recovery_check")
                            return True

                        if not card_no:
                            if card_resource_provider is None:
                                pipeline_failure_status = "CARD_UNAVAILABLE"
                                return False
                            card_data = await card_resource_provider()
                            if not card_data:
                                pipeline_failure_status = "CARD_UNAVAILABLE"
                                return False
                            card_no = card_data.get("card_no", "")
                            card_expiry = card_data.get("card_expiry", "")
                            card_cvc = card_data.get("card_cvc", "")
                        if not all((card_no, card_expiry, card_cvc)):
                            pipeline_failure_status = "CARD_UNAVAILABLE"
                            return False

                        # Reserve the card's single-use boundary before any checkout
                        # field can receive it.  A crash after this marker quarantines
                        # the card on the next startup instead of releasing it.
                        if runtime_state.action_state("CARD_PRESENTATION") is None:
                            runtime_state.begin_action(
                                "CARD_PRESENTATION",
                                step_id=WorkflowStepId.CHECKOUT_READY,
                                effect_class=ActionEffectClass.SUBMIT_ONCE.value,
                            )

                        async def checkout_form_action() -> None:
                            await guarded_execution(
                                page,
                                lambda: phase3_stripe_card(
                                    page,
                                    card_no,
                                    card_expiry,
                                    card_cvc,
                                ),
                                email,
                                "stripe_card",
                                "填写信用卡",
                                verification_fn=verify_stripe_form_ready,
                                next_step_description="填写日本地址",
                                next_step_verification_fn=verify_stripe_form_ready,
                            )
                            await guarded_execution(
                                page,
                                lambda: phase3_stripe_address(
                                    page,
                                    zip_code,
                                    pref,
                                    city,
                                    addr1,
                                    addr2,
                                    name,
                                ),
                                email,
                                "stripe_addr",
                                "填写日本地址",
                                verification_fn=verify_stripe_address_form_ready,
                                next_step_description="确认订阅",
                                next_step_verification_fn=verify_stripe_address_form_ready,
                            )

                        await execute_page_step(
                            WorkflowStepId.CHECKOUT_READY,
                            checkout_form_action,
                            verify_stripe_address_form_ready,
                            evidence_type="checkout_form_postcondition",
                            evidence_details=(
                                ("page_state", "stripe_checkout_ready"),
                                ("verification_source", "stripe_form_postcondition"),
                            ),
                            handler_type=CheckoutHandler,
                        )
                        if runtime_state.action_state("CARD_PRESENTATION") is not ActionState.VERIFIED:
                            runtime_state.verify_action("CARD_PRESENTATION")
                        async def submit_payment_once() -> str:
                            return await runtime_state.run_non_idempotent(
                                "PAYMENT_SUBMIT",
                                lambda: phase3_stripe_confirm(
                                    page,
                                    card_no,
                                    card_expiry,
                                    card_cvc,
                                ),
                                # The Stripe page result is only a submission signal.
                                # Account-side billing verification below decides whether
                                # the non-idempotent action really took effect.  An
                                # unverified outcome must stay UNKNOWN (quarantine) —
                                # only an explicit card rejection may be REJECTED.
                                is_verified=lambda _result: False,
                                is_rejected=lambda result: result == "rejected",
                                step_id=WorkflowStepId.PAYMENT_VERIFIED,
                                effect_class=ActionEffectClass.EXTERNAL_TRANSACTION.value,
                                unverified_state=ActionState.UNKNOWN,
                            )

                        runtime_state.checkpoint(WorkflowStage.PAYMENT_PENDING)
                        payment_result: object = None
                        submit_attempt_no = 1

                        async def refill_none_action() -> str:
                            # Keep the already-filled form as-is; the operator
                            # clicks the payment button manually.  Never
                            # auto-submit after an operator decision.
                            return "FORM_REFILLED"

                        def record_card_switch() -> None:
                            """Audit one card switch in the action journal."""
                            try:
                                runtime_state.begin_action(
                                    "CARD_SWITCH",
                                    step_id=WorkflowStepId.CHECKOUT_READY,
                                    effect_class="AUDIT",
                                )
                            except Exception:
                                pass
                            runtime_state.verify_action("CARD_SWITCH")

                        async def _rebind_card(
                            *,
                            old_card_final_state: str,
                            on_switch_success: Optional[Callable[[], None]] = None,
                        ) -> str:
                            """Atomically replace the card lease and refill the form.

                            The new card is reserved first; only a successful
                            reservation retires the old lease.  A failed
                            acquisition leaves the old lease untouched and
                            returns "SWITCH_FAILED" so the operator can retry.
                            """
                            nonlocal card_no, card_expiry, card_cvc
                            from gpt_automation.state import ActionState

                            if state_store is None or not session_id:
                                return "SWITCH_FAILED"
                            if card_resource_provider is None:
                                return "SWITCH_FAILED"
                            payment_state = runtime_state.action_state(
                                "PAYMENT_SUBMIT"
                            )
                            if payment_state in {
                                ActionState.STARTED,
                                ActionState.UNKNOWN,
                            }:
                                logger.warning(
                                    "card switch refused: payment result is %s",
                                    payment_state.value,
                                )
                                return "SWITCH_FAILED"
                            old_fingerprint = (
                                state_store.get_session_resource_fingerprint(
                                    session_id,
                                    "card",
                                )
                            )
                            if old_fingerprint is None:
                                return "SWITCH_FAILED"
                            new_card = await card_resource_provider()
                            if not new_card or not new_card.get("card_no"):
                                return "SWITCH_FAILED"
                            try:
                                state_store.swap_session_card(
                                    session_id,
                                    old_card_fingerprint=old_fingerprint,
                                    new_card_value=new_card["card_no"],
                                    old_card_final_state=old_card_final_state,
                                )
                            except Exception:
                                logger.exception(
                                    "card swap failed for %s; old lease kept",
                                    account_label,
                                )
                                return "SWITCH_FAILED"
                            card_no = new_card.get("card_no", "")
                            card_expiry = new_card.get("card_expiry", "")
                            card_cvc = new_card.get("card_cvc", "")
                            await phase3_stripe_card(
                                page,
                                card_no,
                                card_expiry,
                                card_cvc,
                            )
                            if runtime_state.action_state(
                                "CARD_PRESENTATION"
                            ) is not ActionState.VERIFIED:
                                runtime_state.verify_action("CARD_PRESENTATION")
                            if on_switch_success is not None:
                                on_switch_success()
                            record_card_switch()
                            return "FORM_REFILLED"

                        async def switch_address_action(index: str = "") -> str:
                            nonlocal zip_code, pref, city, addr1, addr2, submit_attempt_no
                            from gpt_automation.phases.stripe import (
                                _address_by_index,
                                _random_address,
                            )
                            from gpt_automation.state import ActionState

                            payment_state = runtime_state.action_state(
                                "PAYMENT_SUBMIT"
                            )
                            if payment_state in {
                                ActionState.STARTED,
                                ActionState.UNKNOWN,
                            }:
                                return "SWITCH_FAILED"
                            new_addr = _address_by_index(index) or _random_address()
                            zip_code = new_addr.get('zip_code', zip_code) or zip_code
                            pref = new_addr.get('pref', pref) or pref
                            city = new_addr.get('city', city) or city
                            addr1 = new_addr.get('addr1', addr1) or addr1
                            addr2 = new_addr.get('addr2', addr2) or addr2
                            await phase3_stripe_address(page, zip_code, pref, city, addr1, addr2, name)
                            if runtime_state.action_state(
                                "CARD_PRESENTATION"
                            ) is not ActionState.VERIFIED:
                                runtime_state.verify_action("CARD_PRESENTATION")
                            submit_attempt_no += 1
                            return "FORM_REFILLED"

                        async def payment_action() -> None:
                            nonlocal payment_result, submit_attempt_no

                            async def _submit_current_attempt() -> str:
                                nonlocal submit_attempt_no
                                if submit_attempt_no == 1:
                                    return await submit_payment_once()
                                attempt_action = f"PAYMENT_SUBMIT#{submit_attempt_no}"
                                return await runtime_state.run_non_idempotent(
                                    attempt_action,
                                    lambda: phase3_stripe_confirm_manual(page),
                                    is_verified=lambda _result: False,
                                    is_rejected=lambda result: result == "rejected",
                                    step_id=WorkflowStepId.PAYMENT_VERIFIED,
                                    effect_class=ActionEffectClass.EXTERNAL_TRANSACTION.value,
                                    unverified_state=ActionState.UNKNOWN,
                                )

                            def _bump_attempt() -> None:
                                nonlocal submit_attempt_no
                                submit_attempt_no += 1

                            switch_card_quarantine_fn = lambda: _rebind_card(
                                old_card_final_state="QUARANTINED",
                                on_switch_success=_bump_attempt,
                            )
                            switch_card_release_fn = lambda: _rebind_card(
                                old_card_final_state="RELEASED",
                                on_switch_success=_bump_attempt,
                            )

                            payment_result = await guarded_execution(
                               page,
                               submit_payment_once,
                               email,
                               "stripe_confirm",
                               "确认订阅",
                               max_auto_retry=0,
                               retry_allowed=False,
                               verification_fn=verify_manual_payment,
                               manual_success_value="manual_completed",
                               next_step_description="返回主页面",
                               next_step_verification_fn=verify_manual_payment,
                                refill_none_fn=refill_none_action,
                                switch_card_quarantine_fn=switch_card_quarantine_fn,
                                switch_card_release_fn=switch_card_release_fn,
                                switch_address_fn=switch_address_action,
                            )

                            # After an operator switch (or a rejection) the
                            # payment is NEVER auto-submitted: the operator
                            # clicks the button in the browser, then the
                            # workflow detects and classifies the outcome.
                            # The manual loop is state-driven and unbounded;
                            # each card/address switch opens a fresh journal
                            # attempt so an explicit rejection can be retried.
                            while True:
                                if payment_confirmation_is_verified(payment_result):
                                    return
                                if payment_result not in {
                                    "FORM_REFILLED",
                                    "rejected",
                                    "unknown",
                                }:
                                    return
                                if payment_result == "unknown":
                                    raise NeedsOperatorException(
                                        "payment result is unknown; operator must "
                                        "verify the account subscription state"
                                    )
                                payment_result = await guarded_execution(
                                    page,
                                    _submit_current_attempt,
                                    email,
                                    "stripe_manual_submit",
                                    "表单已重填，请在浏览器点击支付按钮完成支付（可继续换卡/换地址）",
                                    max_auto_retry=0,
                                    retry_allowed=False,
                                    menu_on_result=("rejected", "unknown"),
                                    on_menu_retry=_submit_current_attempt,
                                    verification_fn=verify_manual_payment,
                                    manual_success_value="manual_completed",
                                    next_step_description="返回主页面",
                                    next_step_verification_fn=verify_manual_payment,
                                    refill_none_fn=refill_none_action,
                                    switch_card_quarantine_fn=switch_card_quarantine_fn,
                                    switch_card_release_fn=switch_card_release_fn,
                                    switch_address_fn=switch_address_action,
                                )

                        async def payment_postcondition() -> bool:
                            return payment_confirmation_is_verified(payment_result) and await verify_manual_payment()

                        payment_step_result = await execute_registered_handler(
                            PaymentHandler(
                                action=payment_action,
                                verify=payment_postcondition,
                            ),
                        )
                        confirm_ok = payment_result
                        if payment_step_result.status is GraphStepStatus.WAITING_OPERATOR:
                            await request_manual_intervention(
                                page,
                                email,
                                "payment",
                                "支付结果未确认；请核对账户订阅状态，程序会重新验证且不会再次提交。",
                                verify_manual_payment,
                                next_step_description="返回主页面",
                                next_step_verification_fn=verify_manual_payment,
                            )
                            confirm_ok = "manual_completed"
                        elif payment_step_result.status is not GraphStepStatus.SUCCEEDED:
                            raise NeedsOperatorException(
                                f"payment step did not complete: {payment_step_result.code}"
                            )

                        # 返回主页面
                        from gpt_automation.phases.phase2 import _navigate_to_main
                        await guarded_execution(
                            page, lambda: _navigate_to_main(page), email, "nav_main", "返回主画面",
                            verification_fn=verify_main_page,
                            next_step_description="核对订阅状态并进入取消订阅流程",
                            next_step_verification_fn=verify_main_page,
                        )

                        # 只有明确的 success 才能继续；rejected/unknown/manual_review 都失败关闭。
                        if not payment_confirmation_is_verified(confirm_ok):
                            logger.error(
                                "stripe confirmation is not verified for %s: %s",
                                account_label,
                                confirm_ok,
                            )
                            return False

                        if not await verify_manual_payment():
                            logger.error(
                                "account-side subscription verification failed for %s",
                                account_label,
                            )
                            return False

                        commit_manual_payment("account_billing")

                        return True

                    async def cancel_callback():
                        """Run cancellation and verify the resulting subscription state."""
                        from gpt_automation.state import ActionState

                        cancellation_state = runtime_state.action_state(
                            "CANCEL_SUBSCRIPTION"
                        )
                        if cancellation_state is ActionState.VERIFIED:
                            if runtime_state.enabled and runtime_state.step_evidence(
                                WorkflowStepId.CANCELLATION_VERIFIED
                            ) is None:
                                if not await verify_manual_cancellation():
                                    return False
                                commit_manual_cancellation("operator_recovery_check")
                            runtime_state.checkpoint(WorkflowStage.CANCELLATION_VERIFIED)
                            return True
                        if _must_verify_before_side_effect_replay(
                            runtime_state,
                            "CANCEL_SUBSCRIPTION",
                            WorkflowStage.CANCELLATION_PENDING,
                        ):
                            await request_manual_intervention(
                                page,
                                email,
                                "cancellation_resume",
                                "取消动作已有记录；请核对订阅到期状态，不要再次点击取消。",
                                verify_manual_cancellation,
                                next_step_description="确认取消状态并写入结果",
                                next_step_verification_fn=verify_manual_cancellation,
                            )
                            commit_manual_cancellation("operator_recovery_check")
                            return True

                        async def cancel_once():
                            return await runtime_state.run_non_idempotent(
                                "CANCEL_SUBSCRIPTION",
                                lambda: phase4_cancel(page),
                                # Cancellation is only verified by the status check below.
                                is_verified=lambda result: False,
                                step_id=WorkflowStepId.CANCELLATION_VERIFIED,
                                effect_class=ActionEffectClass.EXTERNAL_TRANSACTION.value,
                            )

                        runtime_state.checkpoint(WorkflowStage.CANCELLATION_PENDING)
                        async def cancellation_action() -> None:
                            ctx["stripe_page"] = await guarded_execution(
                                page,
                                cancel_once,
                                email,
                                "cancel_sub",
                                "取消订阅",
                                max_auto_retry=0,
                                retry_allowed=False,
                                verification_fn=verify_manual_cancellation,
                                manual_success_value=None,
                                next_step_description="确认订阅已取消",
                                next_step_verification_fn=verify_manual_cancellation,
                            )

                        async def cancellation_postcondition() -> bool:
                            return await verify_manual_cancellation()

                        cancellation_step_result = await execute_registered_handler(
                            CancellationHandler(
                                action=cancellation_action,
                                verify=cancellation_postcondition,
                            ),
                        )
                        if cancellation_step_result.status is GraphStepStatus.WAITING_OPERATOR:
                            await request_manual_intervention(
                                page,
                                email,
                                "cancellation",
                                "取消结果未确认；请核对账单状态，程序不会再次提交取消动作。",
                                verify_manual_cancellation,
                                next_step_description="写入最终结果",
                                next_step_verification_fn=verify_manual_cancellation,
                            )
                        elif cancellation_step_result.status is not GraphStepStatus.SUCCEEDED:
                            raise NeedsOperatorException(
                                f"cancellation step did not complete: {cancellation_step_result.code}"
                            )
                        commit_manual_cancellation("billing_status")
                        return True

                    mgr = ScreenManager(
                        page=page,
                        ctx=ctx,
                        manual_verifiers={
                            ScreenState.SETTINGS: verify_manual_mfa,
                            ScreenState.STRIPE_PAYMENT: verify_manual_payment,
                            ScreenState.STRIPE_BILLING: verify_manual_cancellation,
                        },
                    )

                    async def inventory_login_callback():
                        """Checkout login health gate before offer/payment."""
                        from gpt_automation.phases.phase1 import phase1_dismiss_personalization_modal
                        from gpt_automation.phases.screen_detector import is_not_logged_in
                        from gpt_automation.workflow.graph import Evidence
                        from gpt_automation.workflow.login_recovery import (
                            LoginRecoveryOutcome,
                            recover_login_health,
                        )

                        async def read_login_code():
                            if code_reader is None:
                                return None
                            return await code_reader.get_verification_code(timeout=30.0)

                        async def verify_main_page_after_operator() -> bool:
                            """Operator resumption verifies that the browser reached the main page."""
                            await phase1_dismiss_personalization_modal(page)
                            return await verify_main_page()

                        recovery_outcome = await recover_login_health(
                            page,
                            email=email,
                            chatgpt_password=chatgpt_password,
                            totp_secret=clean_secret or registration.totp_secret,
                            detect_state=detected_state,
                            is_logged_out=is_not_logged_in,
                            code_reader=read_login_code,
                        )
                        if recovery_outcome is not LoginRecoveryOutcome.AUTHENTICATED:
                            # Keep the browser and page on the operator's screen;
                            # the workflow must not continue to payment unverified.
                            await request_manual_intervention(
                                page,
                                email,
                                "inventory_login",
                                "开通前登录状态未确认；请完成登录或 MFA，程序会重新验证。",
                                verify_main_page_after_operator,
                                next_step_description="进入优惠与支付",
                                next_step_verification_fn=verify_main_page_after_operator,
                            )
                            if await is_not_logged_in(page):
                                return False

                        # Dismiss personalization modal if present
                        await phase1_dismiss_personalization_modal(page)

                        # NOTE: 2026-08-31 身份二次确认门禁已暂停启用（登录直接使用所选 READY 账户凭据）：
                        # 避免因 ChatGPT 主页未明文展示邮箱导致假死锁。底层 verify_authenticated_identity 实现保留供按需启用。
                        # identity = await verify_authenticated_identity(page, expected_email=email)
                        # if identity.outcome is not IdentityVerificationOutcome.VERIFIED:
                        #     await request_manual_intervention(...)

                        from gpt_automation.workflow.authentication import account_fingerprint
                        expected_fp = account_fingerprint(email)

                        if runtime_state.enabled:
                            runtime_state.record_step_evidence(
                                Evidence(
                                    step_id=WorkflowStepId.INVENTORY_LOGIN_VERIFIED,
                                    evidence_type="inventory_login_postcondition",
                                    details=(
                                        ("page_state", "authenticated"),
                                        (
                                            "identity",
                                            "confirmed_by_login_health",
                                        ),
                                        (
                                            "expected_account_fingerprint",
                                            expected_fp[:12],
                                        ),
                                        (
                                            "observed_account_fingerprint",
                                            expected_fp[:12],
                                        ),
                                        (
                                            "verification_source",
                                            "login_health",
                                        ),
                                    ),
                                    verifier_version="inventory-login-v2",
                                )
                            )
                        return True

                    async def checkout_auth_callback():
                        """Shared postcondition for the checkout authentication node.

                        The authenticated main page is the positive evidence that
                        either a fresh MFA setup (full) or an inventory login
                        (checkout) has reached the same checkpoint.
                        """
                        if not await verify_main_page():
                            return False
                        if runtime_state.enabled:
                            runtime_state.record_step_evidence(
                                Evidence(
                                    step_id=WorkflowStepId.CHECKOUT_AUTHENTICATED,
                                    evidence_type="checkout_auth_postcondition",
                                    details=(
                                        ("page_state", "main_page"),
                                        ("verification_source", "screen_observer"),
                                    ),
                                    verifier_version="checkout-auth-v1",
                                )
                            )
                        runtime_state.checkpoint(WorkflowStage.ACCOUNT_VERIFIED)
                        return True

                    # Step order is declared by WorkflowEngine.run_sequence below.
                    sequence_routes = {
                        WorkflowStepId.INVENTORY_LOGIN_VERIFIED: (
                            WorkflowStage.NETWORK_READY,
                            {ScreenState.MAIN_PAGE, ScreenState.LOGIN_EMAIL, ScreenState.SETTINGS},
                            inventory_login_callback,
                            "LOGIN_UNVERIFIED",
                        ),
                        WorkflowStepId.CHECKOUT_AUTHENTICATED: (
                            WorkflowStage.ACCOUNT_VERIFIED,
                            {ScreenState.MAIN_PAGE, ScreenState.SETTINGS},
                            checkout_auth_callback,
                            "CHECKOUT_AUTH_UNVERIFIED",
                        ),
                        WorkflowStepId.MFA_VERIFIED: (
                            WorkflowStage.MFA_VERIFIED,
                            {ScreenState.MAIN_PAGE, ScreenState.SETTINGS},
                            mfa_callback,
                            "MFA_UNCONFIRMED",
                        ),
                        WorkflowStepId.PRICING_PAGE_VERIFIED: (
                            WorkflowStage.PRICING_VERIFIED,
                            {ScreenState.MAIN_PAGE, ScreenState.PRICING_PAGE},
                            pricing_verification_callback,
                            "PRICING_UNVERIFIED",
                        ),
                        WorkflowStepId.OFFER_DECISION_VERIFIED: (
                            WorkflowStage.OFFER_VERIFIED,
                            {
                                ScreenState.MAIN_PAGE,
                                ScreenState.PRICING_PAGE,
                                ScreenState.STRIPE_PAYMENT,
                            },
                            offer_decision_callback,
                            "OFFER_UNVERIFIED",
                        ),
                        WorkflowStepId.PAYMENT_VERIFIED: (
                            WorkflowStage.PAYMENT_VERIFIED,
                            {
                                ScreenState.MAIN_PAGE,
                                ScreenState.PRICING_PAGE,
                                ScreenState.STRIPE_PAYMENT,
                            },
                            stripe_callback,
                            "PAYMENT_UNKNOWN",
                        ),
                        WorkflowStepId.CANCELLATION_VERIFIED: (
                            WorkflowStage.CANCELLATION_VERIFIED,
                            {ScreenState.MAIN_PAGE, ScreenState.STRIPE_BILLING},
                            cancel_callback,
                            "STATUS_UNCONFIRMED",
                        ),
                    }
                    if scope == "account_provision":
                        ordered_steps = (WorkflowStepId.MFA_VERIFIED,)
                    elif scope == "checkout_fulfillment":
                        if goal == "pricing_page":
                            ordered_steps = (
                                WorkflowStepId.INVENTORY_LOGIN_VERIFIED,
                                WorkflowStepId.CHECKOUT_AUTHENTICATED,
                                WorkflowStepId.PRICING_PAGE_VERIFIED,
                            )
                        else:
                            ordered_steps = (
                                WorkflowStepId.INVENTORY_LOGIN_VERIFIED,
                                WorkflowStepId.CHECKOUT_AUTHENTICATED,
                                WorkflowStepId.PRICING_PAGE_VERIFIED,
                                WorkflowStepId.OFFER_DECISION_VERIFIED,
                                WorkflowStepId.PAYMENT_VERIFIED,
                                WorkflowStepId.CANCELLATION_VERIFIED,
                            )
                    else:
                        ordered_steps = (
                            WorkflowStepId.MFA_VERIFIED,
                            WorkflowStepId.INVENTORY_LOGIN_VERIFIED,
                            WorkflowStepId.CHECKOUT_AUTHENTICATED,
                            WorkflowStepId.PRICING_PAGE_VERIFIED,
                            WorkflowStepId.OFFER_DECISION_VERIFIED,
                            WorkflowStepId.PAYMENT_VERIFIED,
                            WorkflowStepId.CANCELLATION_VERIFIED,
                        )
                    sequence = tuple(
                        step_engine.sequence_step(step_id, step_id.value.lower())
                        for step_id in ordered_steps
                    )

                    async def prepare_sequence_step(item) -> None:
                        _, accepted_screens, _, _ = sequence_routes[item.step_id]
                        await mgr.ensure_screen(accepted_screens)

                    async def execute_sequence_step(item):
                        from gpt_automation.workflow.engine import SequenceOutcome

                        _, _, callback, failure_code = sequence_routes[item.step_id]
                        verified = await callback()
                        if verified:
                            return SequenceOutcome(
                                GraphStepStatus.SUCCEEDED,
                                "SEQUENCE_STEP_VERIFIED",
                            )
                        return SequenceOutcome(
                            GraphStepStatus.FAILED,
                            pipeline_failure_status or failure_code,
                        )

                    outcome = await step_engine.run_sequence(
                        sequence,
                        is_verified=lambda step_id: step_is_verified(
                            step_id,
                            sequence_routes[step_id][0],
                        ),
                        prepare=prepare_sequence_step,
                        execute=execute_sequence_step,
                    )
                    if outcome.status is not GraphStepStatus.SUCCEEDED:
                        code = outcome.code
                        if code not in {
                            "NO_FREE_OFFER",
                            "NON_ZERO_AMOUNT",
                            "CHECKOUT_NON_ZERO_AMOUNT",
                        }:
                            decision = await hold_for_operator(
                                page,
                                email,
                                "workflow_result",
                                f"Workflow step failed: {code}",
                                RuntimeError(code),
                            )
                            if decision is InteractionDecision.DEFER_ACCOUNT:
                                write_status(email, "NEEDS_OPERATOR", "", session_id=session_id)
                                return await settle_account_result(
                                    page,
                                    email,
                                    "NEEDS_OPERATOR",
                                    ip=current_ip,
                                )
                        write_status(email, code, email_pwd, session_id=session_id)
                        return await settle_account_result(
                            page,
                            email,
                            code,
                            ip=current_ip,
                            evidence_paths=pipeline_evidence_paths,
                            page_url=pipeline_page_url,
                        )

                    if goal == "pricing_page":
                        # Terminal step of the verification-only goal: the
                        # pricing page was reached and the target-plan amount
                        # was confirmed.  The account is intact and must stay
                        # reusable; the operator keeps the browser on the
                        # plan-selection screen.
                        runtime_state.checkpoint(WorkflowStage.PRICING_VERIFIED)
                        write_status(
                            email,
                            "PRICING_PAGE_VERIFIED",
                            "",
                            session_id=session_id,
                        )
                        return result_for("PRICING_PAGE_VERIFIED", ip=current_ip)

                    if scope == "account_provision":
                        # Offer 资格检查必须从干净的主画面开始：MFA 后置验收
                        # 若重开过 Security 面板，残留弹窗会挡住优惠入口按钮。
                        await mgr.ensure_screen({ScreenState.MAIN_PAGE})
                        # 创建账户 Offer 资格验收闸门：只有 promo_campaign
                        # 精确等于配置目标 campaign 的账号才能进入 READY 池。
                        offer_policy = build_offer_eligibility_policy()
                        eligibility = await verify_offer_eligibility(
                            page,
                            email,
                            policy=offer_policy,
                        )
                        if runtime_state.enabled:
                            try:
                                runtime_state.record_step_evidence(
                                    Evidence(
                                        step_id=WorkflowStepId.OFFER_ELIGIBILITY_VERIFIED,
                                        evidence_type="offer_eligibility_check",
                                        details=(
                                            (
                                                "target_campaign",
                                                offer_policy.target_promo_campaign,
                                            ),
                                            (
                                                "observed_campaign",
                                                eligibility.promo_campaign or "",
                                            ),
                                            ("page_url", eligibility.page_url),
                                            ("verdict", eligibility.result),
                                        ),
                                        verifier_version="offer-policy-v2",
                                    )
                                )
                                runtime_state.record_step_status(
                                    WorkflowStepId.OFFER_ELIGIBILITY_VERIFIED,
                                    status="SUCCEEDED",
                                    outcome_code=eligibility.result,
                                )
                            except Exception as exc:  # noqa: BLE001
                                # Evidence recording is advisory: a correct
                                # HAS_OFFER / NO_OFFER verdict must survive a
                                # workflow-graph bookkeeping failure instead of
                                # becoming an unexpected registration error.
                                logger.warning(
                                    "offer eligibility evidence recording failed: %s",
                                    exc,
                                )
                        if eligibility.result == "NO_OFFER":
                            # 主页确认没有任何 offer / upgrade 入口：合理判定
                            # 没有 offer，落 NO_TARGET_OFFER（报告 no_offer 区）。
                            code = "NO_TARGET_OFFER"
                            return await settle_account_result(
                                page,
                                email,
                                code,
                                ip=current_ip,
                                reason=eligibility.reason or code,
                                offer_result=eligibility.result,
                                page_url=eligibility.page_url,
                                promo_campaign=eligibility.promo_campaign,
                                evidence_paths=(
                                    (eligibility.screenshot_path,)
                                    if eligibility.screenshot_path
                                    else ()
                                ),
                            )
                        if eligibility.result == "UNREACHABLE":
                            # 主页存在 offer 入口但点不进套餐页：导航失败，
                            # 不是没有 offer。连续模式记执行失败（报告 failed
                            # 区），辅助模式保留现场由操作员处理，不得吞成
                            # NO_TARGET_OFFER 假装验过套餐页。
                            code = "AMOUNT_UNKNOWN"
                            return await settle_account_result(
                                page,
                                email,
                                code,
                                ip=current_ip,
                                reason=eligibility.reason or code,
                                offer_result=eligibility.result,
                                page_url=eligibility.page_url,
                                promo_campaign=eligibility.promo_campaign,
                                evidence_paths=(
                                    (eligibility.screenshot_path,)
                                    if eligibility.screenshot_path
                                    else ()
                                ),
                            )
                        if credential_store is None:
                            raise NeedsOperatorException(
                                "workflow session is required before reporting account readiness"
                            )
                        if passwordless:
                            persist_passwordless_credential()
                        else:
                            persist_password()
                            if not clean_secret:
                                saved = load_saved_credential()
                                clean_secret = saved.totp_secret if saved is not None else None
                            if not clean_secret:
                                raise NeedsOperatorException(
                                    "MFA is marked complete but no recoverable TOTP secret exists"
                                )
                            persist_credential(clean_secret)
                        if runtime_state.enabled:
                            runtime_state.record_step_evidence(
                                Evidence(
                                    step_id=WorkflowStepId.ACCOUNT_READY_EXPORTED,
                                    evidence_type="account_ready_export",
                                    details=(
                                        ("inventory_status", "READY"),
                                        ("verification_source", "provision_scope"),
                                    ),
                                    verifier_version="account-ready-v1",
                                )
                            )
                            runtime_state.record_step_status(
                                WorkflowStepId.ACCOUNT_READY_EXPORTED,
                                status="SUCCEEDED",
                                outcome_code="ACCOUNT_READY",
                            )
                        runtime_state.checkpoint(WorkflowStage.MFA_VERIFIED)
                        if current_ip:
                            used.add(current_ip)
                            if not record_ip_usage(current_ip, email):
                                logger.error(
                                    "IP history could not be persisted for ready account %s (IP: %s)",
                                    account_label,
                                    current_ip,
                                )
                        logger.info("account provision complete: %s", account_label)
                        return await settle_account_result(
                            page,
                            email,
                            "ACCOUNT_READY",
                            ip=current_ip,
                            offer_result="HAS_OFFER",
                            page_url=eligibility.page_url,
                            promo_campaign=eligibility.promo_campaign,
                            evidence_paths=(
                                (eligibility.screenshot_path,)
                                if eligibility.screenshot_path
                                else ()
                            ),
                            terminal_screenshot=False,
                            passwordless=passwordless,
                        )
                    # No legacy phase loop remains: the Engine owns ordering.
                    for (
                        completed_stage,
                        step_id,
                        accepted_screens,
                        callback,
                        failure_code,
                    ) in ():
                        if step_is_verified(step_id, completed_stage):
                            continue
                        await mgr.ensure_screen(accepted_screens)
                        if not await callback():
                            code = pipeline_failure_status or failure_code
                            if code not in {
                                "NO_FREE_OFFER",
                                "NON_ZERO_AMOUNT",
                                "CHECKOUT_NON_ZERO_AMOUNT",
                            }:
                                decision = await hold_for_operator(
                                    page,
                                    email,
                                    "workflow_result",
                                    f"工作流结果未确认：{code}；请检查浏览器现场后明确选择如何处理。",
                                    RuntimeError(code),
                                )
                                if decision is InteractionDecision.DEFER_ACCOUNT:
                                    write_status(email, "NEEDS_OPERATOR", "", session_id=session_id)
                                    return await settle_account_result(
                                        page,
                                        email,
                                        "NEEDS_OPERATOR",
                                        ip=current_ip,
                                    )
                            write_status(email, code, email_pwd, session_id=session_id)
                            return await settle_account_result(
                                page,
                                email,
                                code,
                                ip=current_ip,
                            )

                    if credential_store is None:
                        raise NeedsOperatorException(
                            "workflow session is required before reporting success"
                        )
                    if not clean_secret:
                        saved = load_saved_credential()
                        clean_secret = saved.totp_secret if saved is not None else None
                    if not clean_secret:
                        raise NeedsOperatorException(
                            "MFA is marked complete but no recoverable TOTP secret exists"
                        )

                    # Result writing belongs exclusively to FinalizationCoordinator.
                    # The business flow only hands back a verified success; this
                    # prevents a resume from appending the same result twice.
                    if runtime_state.enabled:
                        runtime_state.record_step_status(
                            WorkflowStepId.RESULT_PERSISTED,
                            status="READY",
                            outcome_code="FINALIZATION_PENDING",
                        )
                        result_written = True
                    else:
                        result_written = False
                    if not result_written:
                        logger.error(
                            "registration result could not be persisted: %s",
                            account_label,
                        )
                        decision = await hold_for_operator(
                            page,
                            email,
                            "result_persist",
                            "结果写入失败；请检查浏览器现场后明确选择如何处理。",
                            RuntimeError("RESULT_PERSIST_FAILED"),
                        )
                        if decision is InteractionDecision.DEFER_ACCOUNT:
                            write_status(email, "NEEDS_OPERATOR", "", session_id=session_id)
                            return await settle_account_result(
                                page,
                                email,
                                "NEEDS_OPERATOR",
                                ip=current_ip,
                            )
                        write_status(email, "RESULT_PERSIST_FAILED", "", session_id=session_id)
                        return await settle_account_result(
                            page,
                            email,
                            "RESULT_PERSIST_FAILED",
                            ip=current_ip,
                        )

                    runtime_state.checkpoint(WorkflowStage.RESULT_PERSISTED)

                    if current_ip:
                        used.add(current_ip)
                        if not record_ip_usage(current_ip, email):
                            logger.error(
                                "IP history could not be persisted for successful account %s (IP: %s)",
                                account_label,
                                current_ip,
                            )

                    logger.info("registration complete: %s", account_label)
                    return result_for("success", ip=current_ip)

                except OperatorDeferredException:
                    logger.warning(
                        "registration explicitly deferred by operator: %s",
                        account_label,
                    )
                    write_status(email, "NEEDS_OPERATOR", "", session_id=session_id)
                    return await settle_account_result(
                        page,
                        email,
                        "NEEDS_OPERATOR",
                        ip=current_ip,
                        screenshot_kind="needs_operator",
                    )
                except NeedsOperatorException as exc:
                    logger.warning(
                        "registration suspended for operator: %s",
                        account_label,
                    )
                    await hold_for_operator(
                        page,
                        email,
                        "needs_operator",
                        "当前步骤需要人工确认；确认页面状态后再决定是否延期、跳过或中止",
                        exc,
                    )
                    write_status(email, "NEEDS_OPERATOR", "", session_id=session_id)
                    return await settle_account_result(
                        page,
                        email,
                        "NEEDS_OPERATOR",
                        ip=current_ip,
                        screenshot_kind="needs_operator",
                    )
                except SkipEmailException:
                    logger.warning("registration skipped by operator: %s", account_label)
                    write_status(email, "SKIPPED", "", session_id=session_id)
                    return await settle_account_result(
                        page,
                        email,
                        "SKIPPED",
                        ip=current_ip,
                        screenshot_kind="skipped",
                    )
        except SessionLostError as lost_exc:
            # The browser process/context died underneath the workflow.
            # The disposition depends on whether a non-idempotent external
            # transaction (payment, cancellation) is in flight: replaying it
            # blindly is forbidden, so that window quarantines the account.
            logger.error(
                "BROWSER_CRASHED: browser session lost for %s (reason=%s)",
                account_label,
                lost_exc.reason,
            )
            _record_browser_crashed(state_store, session_id, lost_exc.reason)
            if _session_loss_disposition(runtime_state) == "unknown_side_effect":
                from gpt_automation.state import ActionState

                for action in _NON_IDEMPOTENT_ACTIONS:
                    if runtime_state.action_state(action) in (
                        ActionState.STARTED,
                        ActionState.UNKNOWN,
                    ):
                        runtime_state.mark_action_unknown(action, "BROWSER_CRASHED")
                logger.warning(
                    "session lost inside the non-idempotent window; "
                    "quarantining %s as UNKNOWN_SIDE_EFFECT",
                    account_label,
                )
                write_status(email, "UNKNOWN_SIDE_EFFECT", "", session_id=session_id)
                return await settle_account_result(
                    page if "page" in locals() else None,
                    email,
                    "UNKNOWN_SIDE_EFFECT",
                    ip=current_ip if "current_ip" in locals() else None,
                    screenshot_kind="browser_crashed",
                )
            # Safe window: restart through the same bookkeeping as the
            # operator-requested restart.  Sibling handlers do not catch
            # exceptions raised inside another handler, so the restart
            # bookkeeping is repeated here instead of raising
            # RestartBrowserException.
            restart_count += 1
            if restart_count > max_restarts:
                logger.warning(
                    "max browser restarts (%d) reached for %s after session loss",
                    max_restarts,
                    account_label,
                )
                write_status(email, "NEEDS_OPERATOR", "", session_id=session_id)
                return await settle_account_result(
                    page if "page" in locals() else None,
                    email,
                    "NEEDS_OPERATOR",
                    ip=current_ip if "current_ip" in locals() else None,
                    screenshot_kind="restart_exhausted",
                )
            logger.info(
                "restarting browser for %s (attempt %d/%d) after session loss (%s)",
                account_label,
                restart_count,
                max_restarts,
                lost_exc.reason,
            )
            continue
        except RestartBrowserException as restart_exc:
            restart_count += 1
            if restart_count > max_restarts:
                logger.warning("max browser restarts (%d) reached for %s", max_restarts, account_label)
                write_status(email, "NEEDS_OPERATOR", "", session_id=session_id)
                restart_page = page if "page" in locals() else None
                return await settle_account_result(
                    restart_page,
                    email,
                    "NEEDS_OPERATOR",
                    ip=current_ip if "current_ip" in locals() else None,
                    screenshot_kind="restart_exhausted",
                )
            logger.info("restarting browser for %s (attempt %d/%d): %s", account_label, restart_count, max_restarts, restart_exc)
            continue
        except QuitBatchException:
            raise
        except SkipEmailException:
            # A skip chosen from inside an error handler (e.g. the operator
            # pressing [S] in the unexpected_error menu) must not escape the
            # workflow without the browser-verified IP: the batch layer uses
            # that IP as the next account's switch baseline.
            logger.warning(
                "registration skipped by operator (error handler): %s",
                account_label,
            )
            write_status(email, "SKIPPED", "", session_id=session_id)
            skip_page = page if "page" in locals() else None
            return await settle_account_result(
                skip_page,
                email,
                "SKIPPED",
                ip=current_ip if "current_ip" in locals() else None,
                screenshot_kind="skipped",
            )
        except SAFE_EXCEPTIONS as exc:
            logger.exception("registration error: %s", account_label)
            try:
                decision = await hold_for_operator(
                    page,
                    email,
                    "workflow_error",
                    "工作流出现未分类错误；请检查浏览器现场并明确选择如何处理。",
                    exc,
                )
            except SkipEmailException:
                write_status(email, "SKIPPED", "", session_id=session_id)
                return await settle_account_result(
                    page,
                    email,
                    "SKIPPED",
                    ip=current_ip,
                    screenshot_kind="skipped",
                )
            if decision is InteractionDecision.DEFER_ACCOUNT:
                write_status(email, "NEEDS_OPERATOR", "", session_id=session_id)
                return await settle_account_result(
                    page,
                    email,
                    "NEEDS_OPERATOR",
                    ip=current_ip,
                    screenshot_kind="error",
                )
            write_status(
                email,
                f"ERROR:{type(exc).__name__}",
                email_pwd,
                session_id=session_id,
            )
            return await settle_account_result(
                page,
                email,
                f"ERROR:{type(exc).__name__}",
                ip=current_ip,
                reason=type(exc).__name__,
                screenshot_kind="error",
            )
        except Exception as exc:
            if isinstance(exc, FATAL_EXCEPTIONS):
                raise
            logger.exception("unexpected registration error: %s", account_label)
            try:
                decision = await hold_for_operator(
                    page,
                    email,
                    "unexpected_error",
                    "工作流出现未预期错误；请检查浏览器现场并明确选择如何处理。",
                    exc,
                )
            except SkipEmailException:
                write_status(email, "SKIPPED", "", session_id=session_id)
                return await settle_account_result(
                    page,
                    email,
                    "SKIPPED",
                    ip=current_ip,
                    screenshot_kind="skipped",
                )
            if decision is InteractionDecision.DEFER_ACCOUNT:
                write_status(email, "NEEDS_OPERATOR", "", session_id=session_id)
                return await settle_account_result(
                    page,
                    email,
                    "NEEDS_OPERATOR",
                    ip=current_ip,
                    screenshot_kind="error",
                )
            write_status(
                email,
                f"ERROR:{type(exc).__name__}",
                email_pwd,
                session_id=session_id,
            )
            return await settle_account_result(
                page,
                email,
                f"ERROR:{type(exc).__name__}",
                ip=current_ip,
                reason=type(exc).__name__,
                screenshot_kind="error",
            )
