"""Stripe SaaS adapter — Connect OAuth, webhook verify, event → Work map.

Isolated from ``billing.py``. The Trovis *plan* webhook lives at
``POST /billing/webhook`` and uses ``STRIPE_WEBHOOK_SECRET``. This adapter
never reads that secret and never writes a plan.

Env (read live, never cached):
  STRIPE_SAAS_CLIENT_ID        ca_...  — Connect OAuth client
  STRIPE_SAAS_CLIENT_SECRET    sk_... / client secret
  STRIPE_SAAS_WEBHOOK_SECRET   whsec_...  — SaaS webhook only
  STRIPE_SAAS_REDIRECT_URI     optional override of the OAuth callback
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import database
import saas

logger = logging.getLogger("trovis.saas.stripe")

PROVIDER = "stripe"
AUTHORIZE_URL = "https://connect.stripe.com/oauth/authorize"
TOKEN_URL = "https://connect.stripe.com/oauth/token"
OAUTH_SCOPE = "read_write"
# Stripe's default tolerance is 5 minutes.
_SIG_TOLERANCE_S = 300

# V1 mapped types only. Everything else is acknowledged and ignored.
EVENT_MAP = {
    "payment_intent.processing": saas.EFFECT_WAIT,
    "payment_intent.succeeded": saas.EFFECT_CLEAR,
    "payment_intent.payment_failed": saas.EFFECT_STUCK,
    "invoice.paid": saas.EFFECT_CLEAR,
    "invoice.payment_failed": saas.EFFECT_STUCK,
    "charge.dispute.created": saas.EFFECT_STUCK,
    "charge.dispute.closed": None,  # status-based; see _dispute_closed_effect
}

# Dispute statuses that mean "closed cleanly" → clear the wait.
_DISPUTE_CLEAR_STATUSES = frozenset({"won", "warning_closed"})
# Lost (and cousins) stay stuck.
_DISPUTE_STUCK_STATUSES = frozenset({"lost", "charge_refunded"})


class StripeSaaSError(RuntimeError):
    """OAuth or webhook verify failed."""


class StripeSaaSNotConfigured(StripeSaaSError):
    """SaaS Stripe env is missing. Fail closed for verify / OAuth start."""


def client_id() -> str | None:
    return os.getenv("STRIPE_SAAS_CLIENT_ID") or None


def client_secret() -> str | None:
    return os.getenv("STRIPE_SAAS_CLIENT_SECRET") or None


def webhook_secret() -> str | None:
    """SaaS webhook secret only. Never falls back to STRIPE_WEBHOOK_SECRET."""
    return os.getenv("STRIPE_SAAS_WEBHOOK_SECRET") or None


def oauth_configured() -> bool:
    return bool(client_id() and client_secret())


def webhook_configured() -> bool:
    return bool(webhook_secret())


def authorize_url(*, redirect_uri: str, state: str) -> str:
    if not oauth_configured():
        raise StripeSaaSNotConfigured("Stripe SaaS OAuth is not configured")
    qs = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id(),
            "scope": OAUTH_SCOPE,
            "redirect_uri": redirect_uri,
            "state": state,
        }
    )
    return f"{AUTHORIZE_URL}?{qs}"


def _oauth_token_exchange(code: str) -> dict[str, Any]:
    """POST the authorization code to Stripe Connect. Tests patch this."""
    if not oauth_configured():
        raise StripeSaaSNotConfigured("Stripe SaaS OAuth is not configured")
    body = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "client_id": client_id(),
            "client_secret": client_secret(),
            "code": code,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        TOKEN_URL,
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:300]
        raise StripeSaaSError(f"Stripe OAuth token exchange failed: {detail}") from e
    except urllib.error.URLError as e:
        raise StripeSaaSError(f"Stripe OAuth token exchange failed: {e}") from e
    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as e:
        raise StripeSaaSError("Stripe OAuth returned non-JSON") from e
    if not isinstance(data, dict) or not data.get("access_token"):
        raise StripeSaaSError("Stripe OAuth returned no access_token")
    return data


def exchange_code(code: str) -> dict[str, Any]:
    """Exchange an OAuth code. Returns the token payload (no DB write)."""
    code = (code or "").strip()
    if not code:
        raise StripeSaaSError("missing OAuth code")
    return _oauth_token_exchange(code)


def parse_webhook_event(payload: bytes, sig_header: str | None) -> dict[str, Any]:
    """Verify Stripe-Signature over the RAW body with the SaaS secret.

    Independent of ``billing.parse_webhook_event``. Raises
    ``StripeSaaSNotConfigured`` when we cannot verify, ``StripeSaaSError``
    on a missing/bad signature. Returns a plain dict.
    """
    secret = webhook_secret()
    if not secret:
        raise StripeSaaSNotConfigured("Stripe SaaS webhook secret is not configured")
    if not sig_header:
        raise StripeSaaSError("missing Stripe-Signature header")
    if not isinstance(payload, (bytes, bytearray)):
        raise StripeSaaSError("webhook payload must be raw bytes")

    parts: dict[str, list[str]] = {}
    for item in str(sig_header).split(","):
        if "=" not in item:
            continue
        k, v = item.split("=", 1)
        parts.setdefault(k.strip(), []).append(v.strip())
    try:
        timestamp = int((parts.get("t") or [""])[0])
    except (TypeError, ValueError) as e:
        raise StripeSaaSError("invalid Stripe-Signature timestamp") from e
    v1s = parts.get("v1") or []
    if not v1s:
        raise StripeSaaSError("invalid Stripe-Signature header")

    if abs(int(time.time()) - timestamp) > _SIG_TOLERANCE_S:
        raise StripeSaaSError("Stripe-Signature timestamp is too old")

    signed = f"{timestamp}.".encode("utf-8") + bytes(payload)
    expected = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, cand) for cand in v1s):
        raise StripeSaaSError("invalid Stripe webhook signature")

    try:
        event = json.loads(bytes(payload).decode("utf-8"))
    except (TypeError, ValueError) as e:
        raise StripeSaaSError(f"invalid Stripe webhook payload: {e}") from e
    if not isinstance(event, dict) or not event.get("type"):
        raise StripeSaaSError("Stripe webhook event missing type")
    return event


def _as_dict(obj: Any) -> dict[str, Any]:
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "to_dict"):
        try:
            d = obj.to_dict()
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}
    return {}


def _object_id(obj: dict[str, Any]) -> str | None:
    raw = obj.get("id")
    if raw is None:
        return None
    val = str(raw).strip()
    return val or None


def _failure_reason(obj: dict[str, Any]) -> str | None:
    err = obj.get("last_payment_error")
    if isinstance(err, dict):
        for k in ("message", "decline_code", "code"):
            val = err.get(k)
            if val:
                return str(val)
    # Invoice / charge-level hint.
    for k in ("last_finalization_error",):
        nested = obj.get(k)
        if isinstance(nested, dict) and nested.get("message"):
            return str(nested["message"])
    return None


def _dispute_closed_effect(obj: dict[str, Any]) -> str | None:
    status = str(obj.get("status") or "").strip().lower()
    if status in _DISPUTE_CLEAR_STATUSES:
        return saas.EFFECT_CLEAR
    if status in _DISPUTE_STUCK_STATUSES:
        return saas.EFFECT_STUCK
    # Unknown closed status: keep stuck (don't clear a dispute we can't read).
    logger.info("[saas.stripe] dispute.closed status=%r — keep stuck", status)
    return saas.EFFECT_STUCK


def map_event(event: dict[str, Any]) -> dict[str, Any] | None:
    """Map a verified Stripe event to a spine effect, or None to ignore."""
    etype = str(event.get("type") or "")
    if etype not in EVENT_MAP:
        return None
    obj = _as_dict((event.get("data") or {}).get("object") if isinstance(event.get("data"), dict) else None)
    if etype == "charge.dispute.closed":
        effect = _dispute_closed_effect(obj)
    else:
        effect = EVENT_MAP[etype]
    if effect is None:
        return None

    waiting_on = None
    reason = None
    if effect == saas.EFFECT_WAIT:
        waiting_on = "payment processing"
        reason = "payment processing"
    elif effect == saas.EFFECT_STUCK:
        if etype.startswith("charge.dispute"):
            waiting_on = "dispute"
            reason = _failure_reason(obj) or f"dispute ({obj.get('status') or 'open'})"
        else:
            reason = _failure_reason(obj) or (
                "invoice payment failed" if etype.startswith("invoice.") else "payment failed"
            )
            waiting_on = reason
    elif effect == saas.EFFECT_CLEAR:
        reason = "cleared"
        if etype == "charge.dispute.closed":
            reason = f"dispute {obj.get('status') or 'closed'}"

    created = event.get("created")
    try:
        event_time_unix = int(created) * 1_000_000_000 if created is not None else None
    except (TypeError, ValueError):
        event_time_unix = None

    return {
        "effect": effect,
        "event_type": etype,
        "event_id": event.get("id"),
        "object_id": _object_id(obj),
        "metadata": saas.metadata_of(obj),
        "waiting_on": waiting_on,
        "reason": reason,
        "event_time_unix": event_time_unix,
        "stripe_account": event.get("account") or obj.get("on_behalf_of"),
    }


def apply_verified_event(event: dict[str, Any], *, account_id: int) -> dict[str, Any]:
    """Map + apply a verified event onto the caller's Trovis account."""
    mapped = map_event(event)
    if mapped is None:
        logger.info(
            "[saas.stripe] ignoring unmapped type=%s event=%s",
            event.get("type"), event.get("id"),
        )
        return {"status": "ignored_unmapped", "event_type": event.get("type")}
    return saas.apply_work_effect(
        account_id,
        provider=PROVIDER,
        effect=mapped["effect"],
        object_id=mapped.get("object_id"),
        waiting_on=mapped.get("waiting_on"),
        reason=mapped.get("reason"),
        event_id=mapped.get("event_id"),
        event_type=mapped.get("event_type"),
        event_time_unix=mapped.get("event_time_unix"),
        metadata=mapped.get("metadata"),
    )


def handle_webhook(payload: bytes, sig_header: str | None) -> dict[str, Any]:
    """Verify, resolve the Trovis account via the connected Stripe account,
    apply the spine effect. Always a business-level result dict after verify.

    Account resolution: ``event.account`` (Connect) → saas_connections.
    No matching **connected** row → ignore (log). Never guess a tenant.
    """
    event = parse_webhook_event(payload, sig_header)
    stripe_acct = event.get("account")
    if not stripe_acct:
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        obj = data.get("object") if isinstance(data.get("object"), dict) else {}
        stripe_acct = obj.get("on_behalf_of")
    if not stripe_acct:
        logger.info(
            "[saas.stripe] event %s type=%s has no connected account — ignore",
            event.get("id"), event.get("type"),
        )
        return {"status": "ignored_no_account", "event_id": event.get("id")}

    conn = database.get_saas_connection_by_provider_account(
        PROVIDER, str(stripe_acct),
    )
    if conn is None or conn.get("status") != "connected":
        logger.info(
            "[saas.stripe] no connected Trovis account for stripe=%s event=%s — ignore",
            stripe_acct, event.get("id"),
        )
        return {
            "status": "ignored_no_connection",
            "event_id": event.get("id"),
            "stripe_account": stripe_acct,
        }
    return apply_verified_event(event, account_id=int(conn["account_id"]))
