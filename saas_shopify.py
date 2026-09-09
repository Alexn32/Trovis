"""Shopify SaaS adapter — OAuth, webhook HMAC, order/fulfillment → Work map.

Reuses ``saas.apply_work_effect`` and ``saas_connections`` from Stripe PR A
(#143) / HubSpot PR B (#144). Never invents a loop. No catalog / product /
customer / inventory sync. Isolated from ``/billing/webhook`` and
Stripe / HubSpot secrets.

Connect mapping contract (V1, folded here — surviving Shopify PR C):

  Link keys (require one, first present wins) on the order, fulfillment,
  refund, or transaction — note attributes, metafields, or attributes:
    trovis_loop_external_id | trovis.loop.external_id |
    trovis_run_id | trovis.run.id
    → open loop on this Trovis account; else no-op. Never invent a loop.
    No catalog / product / customer sync.

  Events (minimal official Admin webhooks):
    orders/create                         → wait
      honest wait: "Waiting on payment" when financial_status is
      pending / authorized / partially_paid; else "Waiting on fulfillment"
    orders/paid                           → clear
    orders/fulfilled                      → clear
    fulfillments/create (success)         → clear
    orders/cancelled                      → stuck
    order_transactions/create (failure)   → stuck
    refunds/create                        → stuck (open work only)
    fulfillments/update (error/failure)   → stuck

  Locks:
    reuse #143/#144 SaaS spine (saas.py / apply_work_effect / saas_connections);
    Shopify brand coming→live only after E2E;
    no Intercom / Slack / GitHub adapters;
    never touch /billing/webhook or Stripe/HubSpot secrets;
    scopes read_orders + read_fulfillments only (no write).

Env (read live, never cached):
  SHOPIFY_SAAS_CLIENT_ID        — app API key
  SHOPIFY_SAAS_CLIENT_SECRET    — app API secret (OAuth + webhook HMAC)
  SHOPIFY_SAAS_REDIRECT_URI     — optional override of the OAuth callback
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any

import database
import saas

logger = logging.getLogger("trovis.saas.shopify")

PROVIDER = "shopify"
ADMIN_API_VERSION = "2024-10"
# Offline token (no grant_options[]=per-user). Read-only — no write, no
# catalog / product / customer scopes.
OAUTH_SCOPES = "read_orders,read_fulfillments"
_SHOP_RE = re.compile(r"^[a-z0-9][a-z0-9\-]*\.myshopify\.com$")
_SIG_TOLERANCE_S = 300
_PAYMENT_PENDING = frozenset({"pending", "authorized", "partially_paid"})
_TX_FAIL = frozenset({"failure", "error"})
_FULFILL_OK = frozenset({"success", "success_fulfilled", "fulfilled"})
_FULFILL_FAIL = frozenset({"error", "failure", "failed"})

# GraphQL-style topic names some Partner-dashboard payloads still send.
_TOPIC_ALIASES = {
    "orders_create": "orders/create",
    "orders_paid": "orders/paid",
    "orders_fulfilled": "orders/fulfilled",
    "orders_cancelled": "orders/cancelled",
    "refunds_create": "refunds/create",
    "fulfillments_create": "fulfillments/create",
    "fulfillments_update": "fulfillments/update",
    "order_transactions_create": "order_transactions/create",
}

# V1 mapped topics only. Everything else is acknowledged and ignored
# (products, inventory, customers, GDPR mandatory topics, …).
MAPPED_TOPICS = frozenset(_TOPIC_ALIASES.values())


class ShopifySaaSError(RuntimeError):
    """OAuth or webhook verify failed."""


class ShopifySaaSNotConfigured(ShopifySaaSError):
    """SaaS Shopify env is missing. Fail closed for verify / OAuth start."""


def client_id() -> str | None:
    return os.getenv("SHOPIFY_SAAS_CLIENT_ID") or None


def client_secret() -> str | None:
    """App secret. Never falls back to Stripe or HubSpot secrets."""
    return os.getenv("SHOPIFY_SAAS_CLIENT_SECRET") or None


def oauth_configured() -> bool:
    return bool(client_id() and client_secret())


def webhook_configured() -> bool:
    return bool(client_secret())


def normalize_shop(raw: str | None) -> str | None:
    """Accept ``store``, ``store.myshopify.com``, or a URL. Else None."""
    s = (raw or "").strip().lower()
    if not s:
        return None
    s = re.sub(r"^https?://", "", s)
    s = s.split("/")[0].split("?")[0].strip()
    if not s:
        return None
    if "." not in s:
        s = f"{s}.myshopify.com"
    if not _SHOP_RE.match(s):
        return None
    return s


def authorize_url(*, shop: str, redirect_uri: str, state: str) -> str:
    if not oauth_configured():
        raise ShopifySaaSNotConfigured("Shopify SaaS OAuth is not configured")
    host = normalize_shop(shop)
    if not host:
        raise ShopifySaaSError("invalid Shopify shop domain")
    qs = urllib.parse.urlencode(
        {
            "client_id": client_id(),
            "scope": OAUTH_SCOPES,
            "redirect_uri": redirect_uri,
            "state": state,
        }
    )
    return f"https://{host}/admin/oauth/authorize?{qs}"


def _oauth_token_exchange(code: str, shop: str) -> dict[str, Any]:
    """POST the authorization code to the shop. Tests patch this."""
    if not oauth_configured():
        raise ShopifySaaSNotConfigured("Shopify SaaS OAuth is not configured")
    host = normalize_shop(shop)
    if not host:
        raise ShopifySaaSError("invalid Shopify shop domain")
    body = json.dumps(
        {
            "client_id": client_id(),
            "client_secret": client_secret(),
            "code": code,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"https://{host}/admin/oauth/access_token",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:300]
        raise ShopifySaaSError(f"Shopify OAuth token exchange failed: {detail}") from e
    except urllib.error.URLError as e:
        raise ShopifySaaSError(f"Shopify OAuth token exchange failed: {e}") from e
    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as e:
        raise ShopifySaaSError("Shopify OAuth returned non-JSON") from e
    if not isinstance(data, dict) or not data.get("access_token"):
        raise ShopifySaaSError("Shopify OAuth returned no access_token")
    return data


def exchange_code(code: str, *, shop: str) -> dict[str, Any]:
    """Exchange an OAuth code. Returns the token payload (no DB write)."""
    code = (code or "").strip()
    if not code:
        raise ShopifySaaSError("missing OAuth code")
    tokens = _oauth_token_exchange(code, shop)
    tokens["shop"] = normalize_shop(shop)
    return tokens


def verify_oauth_query(params: dict[str, str], *, timestamp: str | None = None) -> None:
    """Verify the Shopify OAuth callback ``hmac`` query param. Fail closed."""
    secret = client_secret()
    if not secret:
        raise ShopifySaaSNotConfigured("Shopify SaaS client secret is not configured")
    given = (params.get("hmac") or "").strip()
    if not given:
        raise ShopifySaaSError("missing Shopify OAuth hmac")
    pairs = []
    for key in sorted(params):
        if key in ("hmac", "signature"):
            continue
        val = params[key]
        if val is None:
            continue
        pairs.append(f"{key}={val}")
    message = "&".join(pairs)
    expected = hmac.new(
        secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, given):
        raise ShopifySaaSError("invalid Shopify OAuth hmac")
    ts = (timestamp if timestamp is not None else params.get("timestamp") or "").strip()
    if ts:
        try:
            ts_i = int(ts)
        except (TypeError, ValueError) as e:
            raise ShopifySaaSError("invalid Shopify OAuth timestamp") from e
        if abs(int(time.time()) - ts_i) > _SIG_TOLERANCE_S:
            raise ShopifySaaSError("Shopify OAuth timestamp is too old")


def _sign_webhook(payload: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), bytes(payload), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def parse_webhook_object(payload: bytes, hmac_header: str | None) -> dict[str, Any]:
    """Verify X-Shopify-Hmac-SHA256 over the RAW body. Fail closed."""
    secret = client_secret()
    if not secret:
        raise ShopifySaaSNotConfigured("Shopify SaaS client secret is not configured")
    if not hmac_header:
        raise ShopifySaaSError("missing X-Shopify-Hmac-SHA256 header")
    if not isinstance(payload, (bytes, bytearray)):
        raise ShopifySaaSError("webhook payload must be raw bytes")
    expected = _sign_webhook(bytes(payload), secret)
    given = str(hmac_header).strip()
    if not hmac.compare_digest(expected, given):
        raise ShopifySaaSError("invalid Shopify webhook signature")
    try:
        parsed = json.loads(bytes(payload).decode("utf-8"))
    except (TypeError, ValueError) as e:
        raise ShopifySaaSError(f"invalid Shopify webhook payload: {e}") from e
    if not isinstance(parsed, dict):
        raise ShopifySaaSError("Shopify webhook payload must be an object")
    return parsed


def normalize_topic(topic: str | None) -> str:
    t = str(topic or "").strip().lower().replace("\\", "/")
    if t in MAPPED_TOPICS:
        return t
    compact = t.replace("/", "_")
    return _TOPIC_ALIASES.get(compact, t)


def _as_dict(obj: Any) -> dict[str, Any]:
    return obj if isinstance(obj, dict) else {}


def _flatten_named(items: Any, into: dict[str, Any]) -> None:
    if isinstance(items, dict):
        for k, v in items.items():
            if k not in (None, ""):
                into[str(k)] = v
        return
    if not isinstance(items, list):
        return
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if name in (None, ""):
            name = item.get("key")
        if name in (None, ""):
            continue
        into[str(name)] = item.get("value")


def _flatten_metafields(items: Any, into: dict[str, Any]) -> None:
    if not isinstance(items, list):
        return
    for item in items:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        ns = str(item.get("namespace") or "").strip()
        val = item.get("value")
        if not key:
            continue
        into[key] = val
        if ns:
            into[f"{ns}.{key}"] = val
            into[f"{ns}_{key}"] = val


def shopify_link_metadata(obj: Any) -> dict[str, Any]:
    """Flatten note attributes / metafields / attributes into a link dict.

    The spine only reads V1 keys. Nothing here invents a loop id.
    """
    data = _as_dict(obj)
    out: dict[str, Any] = {}
    meta = data.get("metadata")
    if isinstance(meta, dict):
        out.update(meta)
    _flatten_named(data.get("note_attributes"), out)
    _flatten_named(data.get("attributes"), out)
    _flatten_metafields(data.get("metafields"), out)
    nested = data.get("order")
    if isinstance(nested, dict):
        nested_meta = shopify_link_metadata(nested)
        for k, v in nested_meta.items():
            out.setdefault(k, v)
    for key in saas.LINK_KEYS:
        if key in data and data[key] not in (None, ""):
            out.setdefault(key, data[key])
    return out


def _object_id(obj: dict[str, Any]) -> str | None:
    for key in ("admin_graphql_api_id", "id"):
        raw = obj.get(key)
        if raw not in (None, ""):
            val = str(raw).strip()
            if val:
                return val
    return None


def _order_id(obj: dict[str, Any]) -> str | None:
    raw = obj.get("order_id")
    if raw not in (None, ""):
        val = str(raw).strip()
        if val:
            return val
    order = obj.get("order")
    if isinstance(order, dict):
        return _object_id(order)
    return None


def _iso_to_unix_ns(raw: Any) -> int | None:
    if raw in (None, ""):
        return None
    if isinstance(raw, (int, float)):
        n = int(raw)
        return n * 1_000_000_000 if n < 10_000_000_000 else n
    text = str(raw).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return int(dt.timestamp() * 1_000_000_000)


def _wait_copy(obj: dict[str, Any]) -> tuple[str, str]:
    """Honest wait for orders/create.

    Pending / authorized / partially_paid → Waiting on payment.
    Otherwise the order exists and fulfillment is the remaining wait.
    """
    fin = str(obj.get("financial_status") or "").strip().lower()
    if fin in _PAYMENT_PENDING:
        return "payment", "Waiting on payment"
    return "fulfillment", "Waiting on fulfillment"


def _tx_failed(obj: dict[str, Any]) -> bool:
    status = str(obj.get("status") or "").strip().lower()
    return status in _TX_FAIL


def _fulfillment_effect(obj: dict[str, Any], *, create: bool) -> str | None:
    status = str(obj.get("status") or "").strip().lower()
    if create:
        if status in _FULFILL_OK or status in ("", "success"):
            return saas.EFFECT_CLEAR
        return None
    if status in _FULFILL_FAIL:
        return saas.EFFECT_STUCK
    return None


def _stuck_reason(topic: str, obj: dict[str, Any]) -> str:
    if topic == "orders/cancelled":
        cancel = obj.get("cancel_reason")
        return f"order cancelled ({cancel})" if cancel else "order cancelled"
    if topic == "refunds/create":
        return "refund"
    if topic.startswith("fulfillments/"):
        return "fulfillment failed"
    for key in ("message", "error_code", "gateway"):
        val = obj.get(key)
        if val:
            return str(val)
    return "payment failed"


def map_event(topic: str | None, obj: Any) -> dict[str, Any] | None:
    """Map a verified Shopify topic + object to a spine effect, or None."""
    etype = normalize_topic(topic)
    data = _as_dict(obj)
    effect: str | None
    if etype == "orders/create":
        effect = saas.EFFECT_WAIT
    elif etype in ("orders/paid", "orders/fulfilled"):
        effect = saas.EFFECT_CLEAR
    elif etype == "orders/cancelled":
        effect = saas.EFFECT_STUCK
    elif etype == "refunds/create":
        effect = saas.EFFECT_STUCK
    elif etype == "fulfillments/create":
        effect = _fulfillment_effect(data, create=True)
    elif etype == "fulfillments/update":
        effect = _fulfillment_effect(data, create=False)
    elif etype == "order_transactions/create":
        effect = saas.EFFECT_STUCK if _tx_failed(data) else None
    else:
        return None
    if effect is None:
        return None

    waiting_on = None
    reason = None
    if effect == saas.EFFECT_WAIT:
        waiting_on, reason = _wait_copy(data)
    elif effect == saas.EFFECT_STUCK:
        reason = _stuck_reason(etype, data)
        waiting_on = reason
    else:
        reason = "cleared"

    object_id = _order_id(data) or _object_id(data)
    return {
        "effect": effect,
        "event_type": etype,
        "object_id": object_id,
        "order_id": _order_id(data) or (
            _object_id(data) if etype.startswith("orders/") else None
        ),
        "metadata": shopify_link_metadata(data),
        "waiting_on": waiting_on,
        "reason": reason,
        "event_time_unix": _iso_to_unix_ns(
            data.get("updated_at") or data.get("processed_at") or data.get("created_at")
        ),
    }


def _http_json(url: str, access_token: str) -> tuple[int, Any]:
    req = urllib.request.Request(
        url,
        method="GET",
        headers={
            "X-Shopify-Access-Token": access_token,
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
            status = getattr(resp, "status", 200)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")[:300]
    except urllib.error.URLError:
        return 599, None
    try:
        return status, json.loads(raw)
    except (TypeError, ValueError):
        return status, None


def _fetch_order_link_metadata(
    access_token: str, shop: str, order_id: str,
) -> dict[str, Any]:
    """GET order note attributes + metafields. Tests patch this.

    Read-only. Empty dict on failure — caller then no-ops. Never invents a key.
    """
    host = normalize_shop(shop)
    oid = str(order_id or "").strip()
    token = (access_token or "").strip()
    if not host or not oid or not token:
        return {}
    base = f"https://{host}/admin/api/{ADMIN_API_VERSION}/orders/{urllib.parse.quote(oid)}"
    out: dict[str, Any] = {}
    status, data = _http_json(f"{base}.json?fields=id,note_attributes", token)
    if status == 200 and isinstance(data, dict):
        out.update(shopify_link_metadata(data.get("order") if isinstance(data.get("order"), dict) else data))
    elif status != 200:
        logger.info(
            "[saas.shopify] fetch order %s/%s failed status=%s — no note attributes",
            host, oid, status,
        )
    status, data = _http_json(f"{base}/metafields.json", token)
    if status == 200 and isinstance(data, dict):
        _flatten_metafields(data.get("metafields"), out)
    return out


def apply_verified_event(
    topic: str | None,
    obj: dict[str, Any],
    *,
    account_id: int,
    event_id: str | None = None,
    secrets: dict[str, Any] | None = None,
    shop: str | None = None,
) -> dict[str, Any]:
    """Map + apply. Fetch the parent order when the payload has no link key."""
    mapped = map_event(topic, obj)
    if mapped is None:
        logger.info(
            "[saas.shopify] ignoring unmapped topic=%s event=%s",
            topic, event_id,
        )
        return {"status": "ignored_unmapped", "event_type": topic}

    metadata = dict(mapped.get("metadata") or {})
    if saas.extract_link_key(metadata) is None:
        order_id = mapped.get("order_id")
        token = (secrets or {}).get("access_token") if secrets else None
        host = shop or (secrets or {}).get("provider_account_id")
        if token and order_id and host:
            fetched = _fetch_order_link_metadata(str(token), str(host), str(order_id))
            for k, v in fetched.items():
                metadata.setdefault(k, v)
            if not fetched:
                logger.info(
                    "[saas.shopify] no link key on payload and order fetch empty "
                    "topic=%s order=%s event=%s — no-op",
                    mapped.get("event_type"), order_id, event_id,
                )

    return saas.apply_work_effect(
        account_id,
        provider=PROVIDER,
        effect=mapped["effect"],
        object_id=mapped.get("object_id"),
        waiting_on=mapped.get("waiting_on"),
        reason=mapped.get("reason"),
        event_id=event_id,
        event_type=mapped.get("event_type"),
        event_time_unix=mapped.get("event_time_unix"),
        metadata=metadata,
    )


def handle_webhook(
    payload: bytes,
    *,
    hmac_header: str | None,
    shop: str | None,
    topic: str | None,
    webhook_id: str | None = None,
) -> dict[str, Any]:
    """Verify, resolve the Trovis account via shop domain, apply.

    Account resolution: ``X-Shopify-Shop-Domain`` → saas_connections.
    No matching **connected** row → ignore (log). Never guess a tenant.
    Missing loop metadata on the order/fulfillment/refund → no-op (log).
    Never invent a loop. Never sync catalog / products / customers.
    """
    obj = parse_webhook_object(payload, hmac_header)
    host = normalize_shop(shop)
    if not host:
        logger.info(
            "[saas.shopify] webhook topic=%s has no shop domain — ignore",
            topic,
        )
        return {"status": "ignored_no_account", "event_id": webhook_id}

    secrets = database.get_saas_connection_secrets_by_provider_account(
        PROVIDER, host,
    )
    if secrets is None or secrets.get("status") != "connected":
        logger.info(
            "[saas.shopify] no connected Trovis account for shop=%s topic=%s — ignore",
            host, topic,
        )
        return {
            "status": "ignored_no_connection",
            "event_id": webhook_id,
            "shop": host,
        }

    event_id = (webhook_id or "").strip() or None
    if not event_id:
        oid = obj.get("id")
        event_id = f"{host}:{normalize_topic(topic)}:{oid}" if oid is not None else None

    return apply_verified_event(
        topic,
        obj,
        account_id=int(secrets["account_id"]),
        event_id=event_id,
        secrets=secrets,
        shop=host,
    )
