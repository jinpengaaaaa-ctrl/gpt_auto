"""
Development mode: intercept ChatGPT/Stripe requests and return local HTML fixtures.

Usage:
    Set config.DEV_MODE = True to enable this mode.
    Run scripts/fixture_collector.py first to generate the HTML fixture files.
"""

from pathlib import Path

FIXTURES_DIR = Path(__file__).resolve().parent.parent.parent / "fixtures"

ROUTE_MAP: dict[str, str] = {
    "chatgpt.com/auth/login": "01_login_page.html",
    "chatgpt.com/auth/signup": "03_signup_form.html",
    "chatgpt.com/onboarding/survey": "04_survey.html",
    "chatgpt.com/": "05_home_free_offer.html",
    "chatgpt.com/settings": "07_settings_security.html",
    "checkout.stripe.com": "09_stripe_payment.html",
    "billing.stripe.com": "11_subscription_manage.html",
}


async def setup_dev_mode(page) -> None:
    """Intercept all ChatGPT/Stripe requests and return local HTML fixtures.

    Call BEFORE any page.goto() navigation.
    """
    async def handle_route(route) -> None:
        url = route.request.url
        for pattern, fixture_file in ROUTE_MAP.items():
            if pattern in url:
                fixture_path = FIXTURES_DIR / fixture_file
                if fixture_path.exists():
                    html = fixture_path.read_text(encoding="utf-8")
                    await route.fulfill(
                        status=200,
                        content_type="text/html",
                        body=html,
                    )
                    print(f"  [DEV] \u8def\u7531\u62e6\u622a\uff1a{pattern} \u2192 {fixture_file}")
                    return
        await route.continue_()

    await page.route("**/*", handle_route)
    print("\U0001f9ea [DEV] \u8def\u7531\u62e6\u622a\u5df2\u542f\u7528\u3002")
