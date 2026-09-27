"""Scope and checkout-goal policy for browser backends."""

from __future__ import annotations


BROWSER_BACKEND_NOT_ALLOWED_FOR_SCOPE = "BROWSER_BACKEND_NOT_ALLOWED_FOR_SCOPE"
CHECKOUT_GOAL_BACKEND_LOCKED = "CHECKOUT_GOAL_BACKEND_LOCKED"


def validate_scope_backend(run_scope: str, browser_backend: str) -> None:
    """Reject illegal scope/backend combinations before any side effect.

    Only ``firefox_private`` on ``checkout_fulfillment`` is allowed outside
    Cloak; ``account_provision`` and ``full`` always require Cloak.
    """
    scope = (run_scope or "").strip().lower()
    backend = (browser_backend or "").strip().lower()
    if backend in ("", "cloak"):
        return
    if backend == "firefox_private" and scope == "checkout_fulfillment":
        return
    raise ValueError(
        f"{BROWSER_BACKEND_NOT_ALLOWED_FOR_SCOPE}: backend {backend!r} "
        f"is not allowed for scope {scope!r}"
    )


def validate_checkout_goal_backend(
    run_scope: str,
    checkout_goal: str,
    browser_backend: str,
) -> None:
    """Lock Firefox complete checkout behind acceptance; pricing page only.

    ``complete`` on Firefox stays blocked until a real payment-controlled
    acceptance run passes; ``pricing_page`` diagnostics remain allowed.
    """
    scope = (run_scope or "").strip().lower()
    goal = (checkout_goal or "complete").strip().lower()
    backend = (browser_backend or "").strip().lower()
    if backend != "firefox_private" or scope != "checkout_fulfillment":
        return
    if goal == "complete":
        raise ValueError(
            f"{CHECKOUT_GOAL_BACKEND_LOCKED}: firefox_private complete checkout "
            "is release-locked; use cloak or pricing_page diagnostics"
        )
