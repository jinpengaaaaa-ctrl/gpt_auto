"""Named, independently dispatchable handlers for the registration graph.

The browser phases still own selectors and page mechanics.  These adapters
own only the business-step identity, replay policy, and evidence contract so
the engine can dispatch each step without knowing phase implementation details.
"""

from __future__ import annotations

from typing import Awaitable, Callable

from gpt_automation.workflow.graph import ActionEffectClass, WorkflowStepId
from gpt_automation.workflow.steps import BrowserStepHandler


Action = Callable[[], Awaitable[object]]
Verifier = Callable[[], Awaitable[bool]]


class _BusinessHandler(BrowserStepHandler):
    def __init__(
        self,
        *,
        step_id: WorkflowStepId,
        action: Action,
        verify: Verifier,
        effect_class: ActionEffectClass,
        evidence_type: str,
        evidence_details: tuple[tuple[str, str], ...],
        verifier_version: str,
    ) -> None:
        super().__init__(
            step_id=step_id,
            action=action,
            verify=verify,
            evidence_type=evidence_type,
            evidence_details=evidence_details,
            verifier_version=verifier_version,
            effect_class=effect_class,
        )


class PasswordSetupHandler(_BusinessHandler):
    def __init__(self, *, action: Action, verify: Verifier) -> None:
        super().__init__(
            step_id=WorkflowStepId.PASSWORD_VERIFIED,
            action=action,
            verify=verify,
            effect_class=ActionEffectClass.SUBMIT_ONCE,
            evidence_type="password_postcondition",
            evidence_details=(
                ("page_state", "password_verified"),
                ("verification_source", "phase2_postcondition"),
            ),
            verifier_version="password-handler-v1",
        )


class MfaRegistrationHandler(_BusinessHandler):
    def __init__(self, *, action: Action, verify: Verifier) -> None:
        super().__init__(
            step_id=WorkflowStepId.MFA_VERIFIED,
            action=action,
            verify=verify,
            effect_class=ActionEffectClass.SUBMIT_ONCE,
            evidence_type="mfa_postcondition",
            evidence_details=(
                ("page_state", "mfa_verified"),
                ("verification_source", "phase2_postcondition"),
            ),
            verifier_version="mfa-handler-v1",
        )


class OfferDecisionHandler(_BusinessHandler):
    def __init__(self, *, action: Action, verify: Verifier) -> None:
        super().__init__(
            step_id=WorkflowStepId.OFFER_DECISION_VERIFIED,
            action=action,
            verify=verify,
            effect_class=ActionEffectClass.REPEATABLE,
            evidence_type="offer_checkout_postcondition",
            evidence_details=(
                ("page_state", "stripe_checkout"),
                ("offer_state", "free_offer_accepted"),
                ("verification_source", "phase3_postcondition"),
            ),
            verifier_version="offer-handler-v1",
        )


class CheckoutHandler(_BusinessHandler):
    def __init__(self, *, action: Action, verify: Verifier) -> None:
        super().__init__(
            step_id=WorkflowStepId.CHECKOUT_READY,
            action=action,
            verify=verify,
            effect_class=ActionEffectClass.REPEATABLE,
            evidence_type="checkout_form_postcondition",
            evidence_details=(
                ("page_state", "stripe_checkout_ready"),
                ("verification_source", "stripe_form_postcondition"),
            ),
            verifier_version="checkout-handler-v1",
        )


class PaymentHandler(_BusinessHandler):
    def __init__(self, *, action: Action, verify: Verifier) -> None:
        super().__init__(
            step_id=WorkflowStepId.PAYMENT_VERIFIED,
            action=action,
            verify=verify,
            effect_class=ActionEffectClass.EXTERNAL_TRANSACTION,
            evidence_type="payment_subscription_postcondition",
            evidence_details=(
                ("subscription_state", "active"),
                ("payment_state", "verified"),
                ("verification_source", "account_billing"),
            ),
            verifier_version="payment-handler-v1",
        )


class CancellationHandler(_BusinessHandler):
    def __init__(self, *, action: Action, verify: Verifier) -> None:
        super().__init__(
            step_id=WorkflowStepId.CANCELLATION_VERIFIED,
            action=action,
            verify=verify,
            effect_class=ActionEffectClass.EXTERNAL_TRANSACTION,
            evidence_type="cancellation_postcondition",
            evidence_details=(
                ("subscription_state", "cancelled"),
                ("verification_source", "billing_status"),
            ),
            verifier_version="cancellation-handler-v1",
        )


class AuthEntryHandler(_BusinessHandler):
    def __init__(self, *, action: Action, verify: Verifier) -> None:
        super().__init__(
            step_id=WorkflowStepId.AUTH_ENTRY_READY,
            action=action,
            verify=verify,
            effect_class=ActionEffectClass.READ_ONLY,
            evidence_type="auth_entry_postcondition",
            evidence_details=(("verification_source", "screen_observer"),),
            verifier_version="auth-handler-v1",
        )


class AuthRouteHandler(_BusinessHandler):
    def __init__(self, *, action: Action, verify: Verifier) -> None:
        super().__init__(
            step_id=WorkflowStepId.ACCOUNT_ROUTE_RESOLVED,
            action=action,
            verify=verify,
            effect_class=ActionEffectClass.REPEATABLE,
            evidence_type="auth_route_postcondition",
            evidence_details=(("verification_source", "screen_observer"),),
            verifier_version="auth-handler-v1",
        )


class LoginPasswordHandler(_BusinessHandler):
    def __init__(self, *, action: Action, verify: Verifier) -> None:
        super().__init__(
            step_id=WorkflowStepId.LOGIN_PASSWORD_VERIFIED,
            action=action,
            verify=verify,
            effect_class=ActionEffectClass.SUBMIT_ONCE,
            evidence_type="login_password_postcondition",
            evidence_details=(("verification_source", "screen_observer"),),
            verifier_version="auth-handler-v1",
        )


class LoginEmailCodeHandler(_BusinessHandler):
    def __init__(self, *, action: Action, verify: Verifier) -> None:
        super().__init__(
            step_id=WorkflowStepId.LOGIN_EMAIL_CODE_VERIFIED,
            action=action,
            verify=verify,
            effect_class=ActionEffectClass.SUBMIT_ONCE,
            evidence_type="login_email_code_postcondition",
            evidence_details=(("verification_source", "screen_observer"),),
            verifier_version="auth-handler-v1",
        )


class SignupEmailCodeHandler(_BusinessHandler):
    def __init__(self, *, action: Action, verify: Verifier) -> None:
        super().__init__(
            step_id=WorkflowStepId.SIGNUP_EMAIL_CODE_VERIFIED,
            action=action,
            verify=verify,
            effect_class=ActionEffectClass.SUBMIT_ONCE,
            evidence_type="signup_email_code_postcondition",
            evidence_details=(("verification_source", "screen_observer"),),
            verifier_version="auth-handler-v1",
        )


class SignupPasswordHandler(_BusinessHandler):
    def __init__(self, *, action: Action, verify: Verifier) -> None:
        super().__init__(
            step_id=WorkflowStepId.SIGNUP_PASSWORD_VERIFIED,
            action=action,
            verify=verify,
            effect_class=ActionEffectClass.SUBMIT_ONCE,
            evidence_type="signup_password_postcondition",
            evidence_details=(
                ("page_state", "signup_password_transition"),
                ("verification_source", "phase1_postcondition"),
            ),
            verifier_version="signup-password-handler-v1",
        )


__all__ = [
    "AuthEntryHandler",
    "AuthRouteHandler",
    "CancellationHandler",
    "CheckoutHandler",
    "LoginEmailCodeHandler",
    "LoginPasswordHandler",
    "MfaRegistrationHandler",
    "OfferDecisionHandler",
    "PasswordSetupHandler",
    "PaymentHandler",
    "SignupEmailCodeHandler",
    "SignupPasswordHandler",
]
