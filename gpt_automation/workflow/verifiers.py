"""Small, positive postcondition predicates shared by browser workflow steps."""

from __future__ import annotations

from collections.abc import Collection

from gpt_automation.phases.screen_detector import ScreenState


AUTH_ROUTE_NEXT_STATES = frozenset(
    {
        ScreenState.LOGIN_PASSWORD,
        ScreenState.EMAIL_VERIFY_CODE,
        ScreenState.SIGNUP_PASSWORD,
        ScreenState.REGISTRATION_FORM,
        ScreenState.WELCOME_MODAL,
        ScreenState.SURVEY,
        ScreenState.MAIN_PAGE,
    }
)

EMAIL_CODE_NEXT_STATES = frozenset(
    {
        ScreenState.LOGIN_PASSWORD,
        ScreenState.SIGNUP_PASSWORD,
        ScreenState.REGISTRATION_FORM,
        ScreenState.WELCOME_MODAL,
        ScreenState.SURVEY,
        ScreenState.MAIN_PAGE,
    }
)

SIGNUP_PASSWORD_NEXT_STATES = frozenset(
    {
        ScreenState.EMAIL_VERIFY_CODE,
        ScreenState.REGISTRATION_FORM,
        ScreenState.WELCOME_MODAL,
        ScreenState.SURVEY,
        ScreenState.MAIN_PAGE,
    }
)

REGISTRATION_FORM_NEXT_STATES = frozenset(
    {
        ScreenState.WELCOME_MODAL,
        ScreenState.SURVEY,
        ScreenState.MAIN_PAGE,
    }
)

WELCOME_NEXT_STATES = frozenset(
    {
        ScreenState.SURVEY,
        ScreenState.MAIN_PAGE,
    }
)

SURVEY_NEXT_STATES = frozenset(
    {
        ScreenState.MAIN_PAGE,
        ScreenState.SETTINGS,
    }
)


def is_positive_transition(
    state: ScreenState,
    expected_states: Collection[ScreenState],
) -> bool:
    """Return true only when the detector reports an explicitly allowed state."""
    return state in expected_states


def password_value_proven(value: str, expected: str) -> bool:
    """Prove a login password fill by exact read-back, without persisting it."""
    return bool(expected and value and value == expected)


__all__ = [
    "AUTH_ROUTE_NEXT_STATES",
    "EMAIL_CODE_NEXT_STATES",
    "REGISTRATION_FORM_NEXT_STATES",
    "SIGNUP_PASSWORD_NEXT_STATES",
    "SURVEY_NEXT_STATES",
    "WELCOME_NEXT_STATES",
    "is_positive_transition",
    "password_value_proven",
]
