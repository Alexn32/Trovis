"""Stripe billing — the payment gate in front of plan upgrades.

The cardinal rule of plan changes (see ``database.set_account_plan`` and
``database.get_locked_state``): raising a plan unlocks previously view-locked
agents instantly, and telemetry is NEVER gated — only the *view*. This module
adds the missing *payment* gate in front of that: a paid tier can only be
reached after Stripe confirms payment via a signed ``checkout.session.completed``
webhook. The client never sets a paid plan directly.

Two things are OPTIONAL at import time — the ``stripe`` package and the Stripe
credentials. If either is missing the module still imports cleanly and reports
``is_configured() == False``; callers then FAIL CLOSED (refuse the upgrade)
rather than falling back to a free self-upgrade. That fail-closed behavior is
the whole point: absent a working payment path, nobody can raise their own plan.

Config is read live (never cached) so a deploy/test can set it without a
restart, and so a half-configured deploy is caught per request.

Env vars (Stripe's own naming, so dashboard values paste straight in):
  STRIPE_SECRET_KEY        sk_live_... / sk_test_...  — server API key
  STRIPE_WEBHOOK_SECRET    whsec_...                  — webhook signing secret
  STRIPE_PRICE_STARTER     price_...                  — monthly Price for each
  STRIPE_PRICE_PRO         price_...                    paid tier
  STRIPE_PRICE_ENTERPRISE  price_...
  STRIPE_PRICE_STARTER_ANNUAL   price_...             — annual Price (20% off)
  STRIPE_PRICE_PRO_ANNUAL       price_...               for each paid tier;
  STRIPE_PRICE_ENTERPRISE_ANNUAL price_...              used when cycle='annual'
"""
from __future__ import annotations

import os

import database

# Paid tiers, derived from the single source of truth in database so adding a
# tier there can't silently bypass this gate. "free" is the unpaid floor.
PAID_PLANS = frozenset(
    plan for plan in database._AGENT_LIMIT_BY_PLAN if plan != "free"
)

# plan -> env var holding its Stripe Price ID. A paid tier with no configured
# price isn't checkout-able (``is_configured(plan)`` returns False for it).
# Annual prices live in separate env vars (the 20%-off yearly Price); the
# monthly/annual choice rides on the `cycle` argument throughout.
_PRICE_ENV = {
    "starter": "STRIPE_PRICE_STARTER",
    "pro": "STRIPE_PRICE_PRO",
    "enterprise": "STRIPE_PRICE_ENTERPRISE",
}
_PRICE_ENV_ANNUAL = {
    "starter": "STRIPE_PRICE_STARTER_ANNUAL",
    "pro": "STRIPE_PRICE_PRO_ANNUAL",
    "enterprise": "STRIPE_PRICE_ENTERPRISE_ANNUAL",
}
BILLING_CYCLES = frozenset({"monthly", "annual"})


class BillingError(RuntimeError):
    """A Stripe interaction failed (network/API error, bad signature)."""


class BillingNotConfigured(BillingError):
    """Stripe isn't wired up (missing package, key, or price). Fail closed."""


def _stripe():
    """Import the Stripe SDK lazily; return None if it isn't installed."""
    try:
        import stripe  # noqa: PLC0415
    except ImportError:
        return None
    return stripe


def secret_key() -> str | None:
    return os.getenv("STRIPE_SECRET_KEY") or None


def webhook_secret() -> str | None:
    return os.getenv("STRIPE_WEBHOOK_SECRET") or None


def price_id_for(plan: str | None, cycle: str = "monthly") -> str | None:
    table = _PRICE_ENV_ANNUAL if cycle == "annual" else _PRICE_ENV
    env_name = table.get((plan or "").strip().lower())
    return (os.getenv(env_name) if env_name else None) or None


def is_configured(plan: str | None = None, cycle: str = "monthly") -> bool:
    """True if checkout can run: SDK installed and a secret key set. When
    ``plan`` is given, also require that tier's Price ID for the given cycle
    ('monthly' | 'annual')."""
    if _stripe() is None or not secret_key():
        return False
    if plan is not None and not price_id_for(plan, cycle):
        return False
    return True


def webhook_configured() -> bool:
    """True if we can verify inbound webhook signatures."""
    return _stripe() is not None and bool(webhook_secret())


def create_checkout_session(
    *, account_id: int, plan: str, success_url: str, cancel_url: str,
    cycle: str = "monthly",
) -> str:
    """Create a Stripe Checkout session for ``account_id`` to subscribe to
    ``plan`` on ``cycle`` ('monthly' | 'annual') and return the hosted checkout
    URL.

    The account, plan, and cycle ride along in ``client_reference_id`` and
    ``metadata`` so the webhook knows what to apply once payment completes. This
    NEVER changes the plan — that happens only in the verified webhook handler.
    """
    plan = (plan or "").strip().lower()
    cycle = cycle if cycle in BILLING_CYCLES else "monthly"
    if plan not in PAID_PLANS:
        raise BillingError(f"{plan!r} is not a paid tier; nothing to check out")
    stripe = _stripe()
    price = price_id_for(plan, cycle)
    if stripe is None or not secret_key() or not price:
        raise BillingNotConfigured(
            "Stripe is not configured for this tier; cannot start checkout"
        )
    stripe.api_key = secret_key()
    meta = {"account_id": str(account_id), "plan": plan, "cycle": cycle}
    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": price, "quantity": 1}],
            success_url=success_url,
            cancel_url=cancel_url,
            client_reference_id=str(account_id),
            metadata=meta,
            # Mirror onto the subscription so later subscription.* events
            # (renewals, cancellations) still carry the account+plan.
            subscription_data={"metadata": meta},
        )
    except Exception as e:  # stripe.error.StripeError and anything else
        raise BillingError(f"Stripe checkout failed: {e}") from e
    url = session.get("url") if hasattr(session, "get") else getattr(session, "url", None)
    if not url:
        raise BillingError("Stripe returned no checkout URL")
    return url


def plan_for_price(price_id: str | None) -> str | None:
    """Reverse of price_id_for: given a Stripe Price id, return the plan tier it
    maps to (across monthly + annual), or None. Used by subscription webhooks to
    reflect a portal-driven plan change back into Trovis."""
    if not price_id:
        return None
    for table in (_PRICE_ENV, _PRICE_ENV_ANNUAL):
        for plan, env_name in table.items():
            if os.getenv(env_name) == price_id:
                return plan
    return None


def create_portal_session(*, customer_id: str, return_url: str) -> str:
    """Open a Stripe Customer Portal session for an existing customer and return
    its URL. The portal is where customers upgrade/downgrade, update payment
    methods, see invoices, and cancel — Stripe hosts all of it. Raises
    BillingNotConfigured when Stripe isn't wired, BillingError on a missing
    customer or API failure."""
    stripe = _stripe()
    if stripe is None or not secret_key():
        raise BillingNotConfigured("Stripe is not configured; cannot open the billing portal")
    if not customer_id:
        raise BillingError("no Stripe customer on file for this account")
    stripe.api_key = secret_key()
    try:
        session = stripe.billing_portal.Session.create(
            customer=customer_id, return_url=return_url
        )
    except Exception as e:
        raise BillingError(f"Stripe portal session failed: {e}") from e
    url = session.get("url") if hasattr(session, "get") else getattr(session, "url", None)
    if not url:
        raise BillingError("Stripe returned no portal URL")
    return url


def parse_webhook_event(payload: bytes, sig_header: str | None):
    """Verify the Stripe signature over the RAW body and return the event
    (a dict-accessible Stripe object).

    Raises ``BillingNotConfigured`` when we can't verify (no SDK/secret) — we
    refuse to act on an unverifiable event — and ``BillingError`` on a missing
    header or a bad signature/payload.
    """
    stripe = _stripe()
    secret = webhook_secret()
    if stripe is None or not secret:
        raise BillingNotConfigured("Stripe webhook secret is not configured")
    if not sig_header:
        raise BillingError("missing Stripe-Signature header")
    try:
        return stripe.Webhook.construct_event(payload, sig_header, secret)
    except Exception as e:  # SignatureVerificationError / ValueError
        raise BillingError(f"invalid Stripe webhook signature: {e}") from e
