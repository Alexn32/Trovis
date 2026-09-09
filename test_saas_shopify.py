"""SaaS Work-event spine + Shopify adapter.

Connect mapping contract (folded into Shopify PR C):
  Link keys (require one): trovis_loop_external_id |
    trovis.loop.external_id | trovis_run_id | trovis.run.id
    → open loop this account; else no-op. Never invent loops.
    No catalog / product / customer sync.
  Events:
    orders/create → wait (payment if pending/authorized/partially_paid,
      else fulfillment)
    orders/paid / orders/fulfilled / fulfillments/create (success) → clear
    orders/cancelled / payment failure / refunds/create → stuck
    fulfillments/update (error/failure) → stuck
  Locks: reuse #143/#144 SaaS spine; Shopify brand coming→live only after
    E2E; no Intercom/Slack/GitHub adapters; don't touch Stripe billing
    webhook or Stripe/HubSpot secrets.

Run:
  TROVIS_DISABLE_PRICING_SYNC=1 python3 test_saas_shopify.py
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import inspect
import json
import os
import tempfile
import time
import urllib.parse

os.environ.update({
    "OVERSEE_DISABLE_PRICING_SYNC": "1",
    "TROVIS_DISABLE_PRICING_SYNC": "1",
    "TROVIS_DISABLE_ALERTS": "1",
    "TROVIS_DISABLE_LOOP_SWEEP": "1",
    "TROVIS_LOOP_TITLES": "off",
    "SHOPIFY_SAAS_CLIENT_ID": "shopify-client-test",
    "SHOPIFY_SAAS_CLIENT_SECRET": "shopify-secret-test",
    "HUBSPOT_SAAS_CLIENT_ID": "hubspot-client-test",
    "HUBSPOT_SAAS_CLIENT_SECRET": "hubspot-secret-test",
    "STRIPE_SAAS_WEBHOOK_SECRET": "whsec_saas_test",
    "STRIPE_SAAS_CLIENT_ID": "ca_test_xxx",
    "STRIPE_SAAS_CLIENT_SECRET": "sk_test_saas",
    "STRIPE_WEBHOOK_SECRET": "whsec_billing_test",
    "STRIPE_SECRET_KEY": "sk_test_billing",
})
os.environ.pop("DATABASE_URL", None)
os.environ.pop("ANTHROPIC_API_KEY", None)

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()

import database
database.SQLITE_PATH = _tmp.name

import billing
import saas
import saas_hubspot
import saas_shopify
import saas_stripe
import main
from fastapi.testclient import TestClient

main._auto_describe = lambda *a, **k: False

failures = []


def check(label, cond):
    print(("  PASS " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


NS = 10**9
NOW = time.time_ns()
SHOP = "demo-store.myshopify.com"
SECRET = "shopify-secret-test"


def kv(d):
    return [{"key": k, "value": {"stringValue": str(v)}} for k, v in d.items()]


_n = [0]


def sp(name, off, attrs):
    _n[0] += 1
    return {
        "traceId": f"{_n[0]:032d}",
        "spanId": f"{_n[0]:016d}",
        "name": name,
        "kind": 1,
        "startTimeUnixNano": str(NOW - off * NS),
        "endTimeUnixNano": str(NOW - off * NS + 10**6),
        "status": {"code": 1},
        "attributes": kv(attrs),
    }


def sign_body(payload: bytes, secret: str = SECRET) -> str:
    digest = hmac.new(secret.encode(), payload, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def order(
    *,
    oid=1001,
    financial_status="pending",
    fulfillment_status=None,
    note_attributes=None,
    metafields=None,
    extra=None,
):
    obj = {
        "id": oid,
        "financial_status": financial_status,
        "fulfillment_status": fulfillment_status,
        "created_at": "2026-01-15T12:00:00-05:00",
        "updated_at": "2026-01-15T12:00:00-05:00",
    }
    if note_attributes is not None:
        obj["note_attributes"] = note_attributes
    if metafields is not None:
        obj["metafields"] = metafields
    if extra:
        obj.update(extra)
    return obj


def post_shop(
    c,
    obj,
    *,
    topic="orders/create",
    shop=SHOP,
    webhook_id="wh_1",
    secret=SECRET,
):
    body = json.dumps(obj).encode()
    return c.post(
        "/saas/shopify/webhook",
        content=body,
        headers={
            "X-Shopify-Hmac-SHA256": sign_body(body, secret),
            "X-Shopify-Shop-Domain": shop,
            "X-Shopify-Topic": topic,
            "X-Shopify-Webhook-Id": webhook_id,
            "Content-Type": "application/json",
        },
    )


def oauth_hmac(params: dict, secret: str = SECRET) -> str:
    pairs = []
    for key in sorted(params):
        if key in ("hmac", "signature"):
            continue
        pairs.append(f"{key}={params[key]}")
    return hmac.new(secret.encode(), "&".join(pairs).encode(), hashlib.sha256).hexdigest()


def work_item(c, headers, title):
    page = c.get("/work/items", headers=headers).json()
    for it in page.get("items") or []:
        if it.get("title") == title:
            return it
    return None


def loop_events(external_id, account_id=1):
    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute(
            "SELECT id FROM loops WHERE external_id = ? AND account_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (external_id, account_id),
        )
        row = cur.fetchone()
        if not row:
            return []
        cur.execute(
            "SELECT type, actor_type, actor, payload FROM loop_events "
            "WHERE loop_id = ? ORDER BY event_time_unix, id",
            (row["id"],),
        )
        out = []
        for r in cur.fetchall():
            p = r["payload"]
            if isinstance(p, str):
                try:
                    p = json.loads(p)
                except (TypeError, ValueError):
                    p = {}
            out.append({
                "type": r["type"],
                "actor_type": r["actor_type"],
                "actor": r["actor"],
                "payload": p or {},
            })
        return out


# In-memory Admin API stand-in. Webhooks never invent a loop key.
ORDERS = {}


def fake_fetch(access_token, shop, order_id):
    return dict(ORDERS.get((str(shop), str(order_id)), {}))


saas_shopify._fetch_order_link_metadata = fake_fetch


print("-- extract_link_key + shopify flatten --")
check("Shopify is a labeled provider", saas.PROVIDER_LABELS.get("shopify") == "Shopify")
check("preferred underscore key wins",
      saas.extract_link_key({
          "trovis_loop_external_id": "L1",
          "trovis.run.id": "r1",
      }) == "L1")
meta = saas_shopify.shopify_link_metadata({
    "note_attributes": [{"name": "trovis_loop_external_id", "value": "from-note"}],
})
check("note_attributes flatten to link key",
      saas.extract_link_key(meta) == "from-note")
meta = saas_shopify.shopify_link_metadata({
    "metafields": [{
        "namespace": "trovis", "key": "loop.external_id", "value": "from-mf",
    }],
})
check("metafield namespace.key is trovis.loop.external_id",
      saas.extract_link_key(meta) == "from-mf")
meta = saas_shopify.shopify_link_metadata({
    "metafields": [{"key": "trovis_run_id", "value": "run-9"}],
})
check("bare metafield key accepted",
      saas.extract_link_key(meta) == "run-9")
check("empty Shopify object is no key",
      saas.extract_link_key(saas_shopify.shopify_link_metadata({})) is None)
check("normalize shop accepts bare handle",
      saas_shopify.normalize_shop("Demo-Store") == SHOP)
check("normalize shop rejects junk",
      saas_shopify.normalize_shop("https://evil.example") is None)

print("-- map_event --")
m = saas_shopify.map_event("orders/create", order(
    financial_status="pending",
    note_attributes=[{"name": "trovis_loop_external_id", "value": "n1"}],
))
check("orders/create pending → wait on payment",
      m and m["effect"] == "wait" and m["waiting_on"] == "payment"
      and m["reason"] == "Waiting on payment")
m = saas_shopify.map_event("orders/create", order(financial_status="authorized"))
check("orders/create authorized → wait on payment",
      m and m["effect"] == "wait" and m["waiting_on"] == "payment")
m = saas_shopify.map_event("orders/create", order(financial_status="paid"))
check("orders/create paid → wait on fulfillment (honest wait)",
      m and m["effect"] == "wait" and m["waiting_on"] == "fulfillment"
      and m["reason"] == "Waiting on fulfillment")
m = saas_shopify.map_event("orders/paid", order(financial_status="paid"))
check("orders/paid → clear", m and m["effect"] == "clear")
m = saas_shopify.map_event("orders/fulfilled", order(fulfillment_status="fulfilled"))
check("orders/fulfilled → clear", m and m["effect"] == "clear")
m = saas_shopify.map_event("fulfillments/create", {
    "id": 9, "order_id": 1001, "status": "success",
})
check("fulfillments/create success → clear", m and m["effect"] == "clear")
check("fulfillments/create pending is unmapped (not yet fulfilled)",
      saas_shopify.map_event("fulfillments/create", {
          "id": 9, "order_id": 1001, "status": "pending",
      }) is None)
m = saas_shopify.map_event("orders/cancelled", order(extra={"cancel_reason": "customer"}))
check("orders/cancelled → stuck",
      m and m["effect"] == "stuck" and "cancelled" in (m.get("reason") or ""))
m = saas_shopify.map_event("order_transactions/create", {
    "id": 7, "order_id": 1001, "status": "failure", "message": "Card declined",
})
check("failed transaction → stuck with reason",
      m and m["effect"] == "stuck" and "declined" in (m.get("reason") or "").lower())
check("successful transaction is unmapped",
      saas_shopify.map_event("order_transactions/create", {
          "id": 8, "order_id": 1001, "status": "success",
      }) is None)
m = saas_shopify.map_event("refunds/create", {"id": 3, "order_id": 1001})
check("refunds/create → stuck", m and m["effect"] == "stuck" and m["reason"] == "refund")
m = saas_shopify.map_event("fulfillments/update", {
    "id": 9, "order_id": 1001, "status": "failure",
})
check("fulfillments/update failure → stuck",
      m and m["effect"] == "stuck")
check("fulfillments/update success is unmapped",
      saas_shopify.map_event("fulfillments/update", {
          "id": 9, "order_id": 1001, "status": "success",
      }) is None)
check("products/create is unmapped (no catalog sync)",
      saas_shopify.map_event("products/create", {"id": 1, "title": "Hat"}) is None)
check("customers/create is unmapped",
      saas_shopify.map_event("customers/create", {"id": 1}) is None)
check("inventory_levels/update is unmapped",
      saas_shopify.map_event("inventory_levels/update", {"inventory_item_id": 1}) is None)
check("ORDERS_CREATE alias maps",
      saas_shopify.map_event("ORDERS_CREATE", order(financial_status="pending"))
      and saas_shopify.map_event("ORDERS_CREATE", order())["event_type"] == "orders/create")

print("-- webhook secret isolation --")
check("Shopify signs with SHOPIFY_SAAS_CLIENT_SECRET",
      saas_shopify.client_secret() == "shopify-secret-test")
check("Shopify never reads STRIPE_WEBHOOK_SECRET",
      saas_shopify.client_secret() != billing.webhook_secret())
check("Shopify never reads STRIPE_SAAS_WEBHOOK_SECRET",
      saas_shopify.client_secret() != saas_stripe.webhook_secret())
check("Shopify never reads HUBSPOT_SAAS_CLIENT_SECRET",
      saas_shopify.client_secret() != saas_hubspot.client_secret())
src = inspect.getsource(main.billing_webhook)
check("billing_webhook source does not call shopify",
      "saas_shopify" not in src and "saas.apply" not in src)
check("billing_webhook still uses billing.parse_webhook_event",
      "billing.parse_webhook_event" in src)
src_stripe = inspect.getsource(main.saas_stripe_webhook)
check("Stripe SaaS webhook untouched (no shopify handle)",
      "saas_shopify" not in src_stripe and "saas_stripe.handle_webhook" in src_stripe)
src_hs = inspect.getsource(main.saas_hubspot_webhook)
check("HubSpot webhook untouched (no shopify handle)",
      "saas_shopify" not in src_hs and "saas_hubspot.handle_webhook" in src_hs)
src_sh = inspect.getsource(main.saas_shopify_webhook)
check("Shopify webhook uses saas_shopify.handle_webhook",
      "saas_shopify.handle_webhook" in src_sh)
check("Shopify webhook does not call billing.parse",
      "billing.parse_webhook_event" not in src_sh)
check("Shopify webhook does not call saas_stripe",
      "saas_stripe" not in src_sh)
check("Shopify webhook does not call saas_hubspot",
      "saas_hubspot" not in src_sh)
check("OAuth scopes are read-only orders+fulfillments",
      saas_shopify.OAUTH_SCOPES == "read_orders,read_fulfillments")
check("OAuth scopes have no write / catalog / customers",
      "write_" not in saas_shopify.OAUTH_SCOPES
      and "product" not in saas_shopify.OAUTH_SCOPES
      and "customer" not in saas_shopify.OAUTH_SCOPES
      and "inventory" not in saas_shopify.OAUTH_SCOPES)

with TestClient(main.app) as c:
    r = c.post("/auth/signup", json={
        "email": "sh@t.com", "password": "supersecret123",
        "name": "Alex", "account_type": "business", "org_name": "Shop Co",
    }).json()
    K, T = r["api_key"], r["token"]
    H = {"Authorization": f"Bearer {T}"}
    aid = database.resolve_session(T)["account_id"]

    def post_traces(svc, spans):
        return c.post("/v1/traces", json={"resourceSpans": [{
            "resource": {"attributes": kv({"service.name": svc})},
            "scopeSpans": [{"spans": spans}],
        }]}, headers={"X-Trovis-Api-Key": K})

    post_traces("shop-agent", [
        sp("message_received", 400, {
            "trovis.loop.title": "Ship the hammock",
            "trovis.loop.external_id": "loop-order",
        }),
        sp("tool_call", 200, {
            "trovis.loop.external_id": "loop-order",
            "trovis.tool.name": "create_shopify_order",
        }),
    ])
    r2 = c.post("/auth/signup", json={
        "email": "other-sh@t.com", "password": "supersecret123",
        "name": "Other", "account_type": "individual", "org_name": "Other",
    }).json()
    c.post("/v1/traces", json={"resourceSpans": [{
        "resource": {"attributes": kv({"service.name": "other-agent"})},
        "scopeSpans": [{"spans": [sp("message_received", 100, {
            "trovis.loop.title": "Other tenant job",
            "trovis.loop.external_id": "loop-order",
        })]}],
    }]}, headers={"X-Trovis-Api-Key": r2["api_key"]})

    database.upsert_saas_connection(
        aid, "shopify", provider_account_id=SHOP,
        access_token="tok_sh", status="connected",
    )
    listed = c.get("/saas/connections", headers=H).json()
    check("connections lists shopify_oauth_configured",
          listed.get("shopify_oauth_configured") is True)
    sh_row = next((x for x in listed["connections"] if x["provider"] == "shopify"), None)
    check("tokens never appear on the connections API",
          sh_row and "access_token" not in sh_row and "refresh_token" not in sh_row)

    print("\n--- signature + isolation ---")
    ev = order(note_attributes=[{"name": "trovis_loop_external_id", "value": "loop-order"}])
    bad = post_shop(c, ev, webhook_id="wh_sig_bad", secret="wrong-secret")
    check("bad Shopify signature → 400", bad.status_code == 400)
    missing = c.post("/saas/shopify/webhook", content=json.dumps(ev).encode())
    check("missing signature → 400", missing.status_code == 400)
    billed = post_shop(c, ev, webhook_id="wh_sig_bill", secret="whsec_billing_test")
    check("billing secret rejected on Shopify route", billed.status_code == 400)
    stripe_secret = post_shop(c, ev, webhook_id="wh_sig_stripe", secret="whsec_saas_test")
    check("Stripe SaaS secret rejected on Shopify route", stripe_secret.status_code == 400)
    hs_secret = post_shop(c, ev, webhook_id="wh_sig_hs", secret="hubspot-secret-test")
    check("HubSpot secret rejected on Shopify route", hs_secret.status_code == 400)

    print("\n--- metadata miss is a no-op ---")
    resp = post_shop(c, order(oid=2001, financial_status="pending"), webhook_id="wh_nometa")
    check("no metadata still 200 (ack)", resp.status_code == 200)
    check("no metadata status is ignored_no_metadata",
          resp.json().get("status") == "ignored_no_metadata")
    check("Shopify alone did not invent a loop",
          database.find_open_loop_by_external_id(aid, "invented-from-shopify") is None)
    check("named loop unchanged after metadata miss",
          work_item(c, H, "Ship the hammock")["status"] == "moving")

    print("\n--- fetch miss (empty order properties) is a no-op ---")
    ORDERS.pop((SHOP, "2002"), None)
    resp = post_shop(c, {"id": 88, "order_id": 2002, "status": "success"},
                     topic="fulfillments/create", webhook_id="wh_fetchmiss")
    check("missing order fetch → ignored_no_metadata",
          resp.status_code == 200 and resp.json().get("status") == "ignored_no_metadata")
    check("still no invented loop after fetch miss",
          database.find_open_loop_by_external_id(aid, "2002") is None)

    print("\n--- no matching open loop ---")
    resp = post_shop(c, order(
        oid=2003,
        note_attributes=[{"name": "trovis_loop_external_id", "value": "no-such-loop"}],
    ), webhook_id="wh_ghost")
    check("unknown key → ignored_no_loop",
          resp.status_code == 200 and resp.json().get("status") == "ignored_no_loop")
    check("still no invented loop",
          database.find_open_loop_by_external_id(aid, "no-such-loop") is None)

    print("\n--- wait (orders/create pending payment) ---")
    resp = post_shop(c, order(
        financial_status="pending",
        note_attributes=[{"name": "trovis_loop_external_id", "value": "loop-order"}],
    ), webhook_id="wh_wait")
    check("orders/create applied",
          resp.status_code == 200 and resp.json().get("status") == "applied")
    item = work_item(c, H, "Ship the hammock")
    check("wait: holder is Shopify (tool)",
          item and item["holder"]["kind"] == "tool"
          and item["holder"]["name"] == "Shopify")
    check("wait: waiting_on is payment",
          item and "payment" in (item.get("whats_next") or "").lower())
    check("wait: Work status is stuck (awaiting_system)",
          item and item["status"] == "stuck")
    evs = loop_events("loop-order")
    hi = [e for e in evs if e["type"] == "handoff_initiated"]
    check("wait wrote to_system handoff toward Shopify",
          hi and hi[-1]["payload"].get("direction") == "to_system"
          and hi[-1]["payload"].get("target_id") == "Shopify"
          and hi[-1]["actor_type"] == "system")
    other_item = c.get("/work/items", headers={"Authorization": f"Bearer {r2['token']}"}).json()
    other = [i for i in other_item.get("items") or [] if i["title"] == "Other tenant job"]
    check("other tenant's same-key loop was not touched",
          other and other[0]["status"] == "moving")

    print("\n--- clear (orders/paid) ---")
    resp = post_shop(c, order(
        financial_status="paid",
        note_attributes=[{"name": "trovis_loop_external_id", "value": "loop-order"}],
    ), topic="orders/paid", webhook_id="wh_paid")
    check("orders/paid applied",
          resp.status_code == 200 and resp.json().get("status") == "applied")
    item = work_item(c, H, "Ship the hammock")
    check("clear: work is moving again",
          item and item["status"] == "moving")
    evs = loop_events("loop-order")
    check("clear wrote handoff_completed",
          any(e["type"] == "handoff_completed" for e in evs))

    print("\n--- dotted + run_id link keys ---")
    resp = post_shop(c, order(
        financial_status="pending",
        note_attributes=[{"name": "trovis.loop.external_id", "value": "loop-order"}],
    ), webhook_id="wh_dotted")
    check("dotted trovis.loop.external_id waits",
          resp.json().get("status") == "applied"
          and work_item(c, H, "Ship the hammock")["status"] == "stuck")
    resp = post_shop(c, order(
        fulfillment_status="fulfilled",
        metafields=[{"key": "trovis_run_id", "value": "loop-order"}],
    ), topic="orders/fulfilled", webhook_id="wh_runid")
    check("trovis_run_id on metafield clears",
          resp.json().get("status") in ("applied", "noop_already")
          and work_item(c, H, "Ship the hammock")["status"] == "moving")

    print("\n--- fulfillments/create via parent-order fetch ---")
    post_traces("shop-agent", [
        sp("message_received", 40, {
            "trovis.loop.title": "Fulfill order 88",
            "trovis.loop.external_id": "loop-ful",
        }),
    ])
    ORDERS[(SHOP, "3001")] = {"trovis_loop_external_id": "loop-ful"}
    resp = post_shop(c, order(
        oid=3001, financial_status="paid",
        note_attributes=[{"name": "trovis_loop_external_id", "value": "loop-ful"}],
    ), webhook_id="wh_ful_wait")
    check("orders/create on fulfillment loop waits",
          resp.json().get("status") == "applied"
          and work_item(c, H, "Fulfill order 88")["status"] == "stuck")
    resp = post_shop(c, {"id": 77, "order_id": 3001, "status": "success"},
                     topic="fulfillments/create", webhook_id="wh_ful")
    check("fulfillment create fetched order key and cleared",
          resp.json().get("status") == "applied"
          and work_item(c, H, "Fulfill order 88")["status"] == "moving")
    check("fulfillment clear did not invent a loop",
          database.find_open_loop_by_external_id(aid, "loop-ful") is not None)

    print("\n--- stuck: cancelled / payment failure / refund ---")
    resp = post_shop(c, order(
        extra={"cancel_reason": "fraud"},
        note_attributes=[{"name": "trovis_loop_external_id", "value": "loop-order"}],
    ), topic="orders/cancelled", webhook_id="wh_cancel")
    item = work_item(c, H, "Ship the hammock")
    check("orders/cancelled → stuck",
          resp.json().get("status") == "applied"
          and item["status"] == "stuck"
          and "cancel" in (item.get("whats_next") or "").lower())

    resp = post_shop(c, {
        "id": 55, "order_id": 1001, "status": "error", "message": "Insufficient funds",
        "note_attributes": [{"name": "trovis.run.id", "value": "loop-order"}],
    }, topic="order_transactions/create", webhook_id="wh_txfail")
    item = work_item(c, H, "Ship the hammock")
    check("payment failure → stuck",
          resp.json().get("status") in ("applied", "noop_already")
          and item["status"] == "stuck")

    resp = post_shop(c, {
        "id": 44, "order_id": 1001,
        "note_attributes": [{"name": "trovis_loop_external_id", "value": "loop-order"}],
    }, topic="refunds/create", webhook_id="wh_refund")
    item = work_item(c, H, "Ship the hammock")
    check("refunds/create → stuck (open work)",
          resp.json().get("status") in ("applied", "noop_already")
          and item["status"] == "stuck"
          and "refund" in (item.get("whats_next") or "").lower())

    resp = post_shop(c, {
        "id": 66, "order_id": 1001, "status": "error",
        "note_attributes": [{"name": "trovis_loop_external_id", "value": "loop-order"}],
    }, topic="fulfillments/update", webhook_id="wh_fulfail")
    item = work_item(c, H, "Ship the hammock")
    check("fulfillments/update failure → stuck",
          resp.json().get("status") in ("applied", "noop_already")
          and item["status"] == "stuck")

    print("\n--- unmapped + closed loop + disconnected ---")
    resp = post_shop(c, {"id": 1, "title": "Hammock"},
                     topic="products/create", webhook_id="wh_product")
    check("products/create ignored (no catalog sync)",
          resp.status_code == 200 and resp.json().get("status") == "ignored_unmapped")
    resp = post_shop(c, {"id": 2, "email": "a@b.com"},
                     topic="customers/create", webhook_id="wh_cust")
    check("customers/create ignored",
          resp.json().get("status") == "ignored_unmapped")

    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute("SELECT id FROM loops WHERE external_id = ? AND account_id = ?",
                    ("loop-order", aid))
        lid = cur.fetchone()["id"]
    with database._connect() as conn, database._cursor(conn) as cur:
        database.append_loop_event(
            cur, lid, "loop_closed", "agent", "shop-agent:main",
            payload={"reason": "completed_by_agent"}, account_id=aid,
        )
        cur.execute("UPDATE loops SET closed_at = CURRENT_TIMESTAMP, cached_state = 'done' "
                    "WHERE id = ?", (lid,))
    resp = post_shop(c, order(
        note_attributes=[{"name": "trovis_loop_external_id", "value": "loop-order"}],
    ), webhook_id="wh_closed")
    check("closed loop is ignored (no reopen)",
          resp.json().get("status") == "ignored_no_loop")
    resp = post_shop(c, {
        "id": 45, "order_id": 1001,
        "note_attributes": [{"name": "trovis_loop_external_id", "value": "loop-order"}],
    }, topic="refunds/create", webhook_id="wh_refund_closed")
    check("refund on closed work is no-op (stuck only if still open)",
          resp.json().get("status") == "ignored_no_loop")

    database.disconnect_saas_connection(aid, "shopify")
    resp = post_shop(c, order(
        note_attributes=[{"name": "trovis_loop_external_id", "value": "loop-order"}],
    ), webhook_id="wh_dc")
    check("disconnected connection ignores events",
          resp.json().get("status") == "ignored_no_connection")

    print("\n--- OAuth token storage ---")
    database.upsert_saas_connection(
        aid, "shopify", provider_account_id=SHOP,
        access_token="tok_sh", status="connected",
    )
    start = c.get(f"/saas/shopify/oauth/start?shop={SHOP}", headers=H)
    check("oauth start returns authorize_url",
          start.status_code == 200
          and f"{SHOP}/admin/oauth/authorize" in start.json().get("authorize_url", ""))
    start_url = start.json().get("authorize_url", "")
    check("oauth scopes are read_orders,read_fulfillments",
          "read_orders" in start_url and "read_fulfillments" in start_url)
    check("oauth scopes exclude write and catalog",
          "write_" not in urllib.parse.unquote(start_url)
          and "read_products" not in start_url
          and "read_customers" not in start_url)
    check("oauth start is account-authed (401 without)",
          c.get(f"/saas/shopify/oauth/start?shop={SHOP}").status_code == 401)
    check("oauth start without shop is 400",
          c.get("/saas/shopify/oauth/start", headers=H).status_code == 400)
    check("oauth start with junk shop is 400",
          c.get("/saas/shopify/oauth/start?shop=evil.example", headers=H).status_code == 400)

    def fake_exchange(code, shop):
        return {"access_token": "tok_from_sh_oauth", "scope": "read_orders,read_fulfillments"}

    saas_shopify._oauth_token_exchange = fake_exchange
    state = database.create_saas_oauth_state(aid, "shopify", payload=SHOP)
    ts = str(int(time.time()))
    q = {
        "code": "sh_test",
        "shop": SHOP,
        "state": state,
        "timestamp": ts,
    }
    q["hmac"] = oauth_hmac(q)
    qs = urllib.parse.urlencode(q)
    cb = c.get(f"/saas/shopify/oauth/callback?{qs}", follow_redirects=False)
    check("oauth callback redirects to app",
          cb.status_code in (302, 307) and "saas=shopify_connected" in (cb.headers.get("location") or ""))
    listed = c.get("/saas/connections", headers=H).json()
    sh_row = next((x for x in listed["connections"] if x["provider"] == "shopify"), None)
    check("connection stored with shop domain",
          sh_row and sh_row["status"] == "connected"
          and sh_row["provider_account_id"] == SHOP)
    check("tokens never appear on the connections API after oauth",
          sh_row and "access_token" not in sh_row)
    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute(
            "SELECT access_token, scope FROM saas_connections "
            "WHERE account_id = ? AND provider = ?",
            (aid, "shopify"),
        )
        tok = cur.fetchone()
    check("access_token persisted server-side", tok["access_token"] == "tok_from_sh_oauth")
    check("read-only scope persisted", "read_orders" in (tok["scope"] or ""))
    secrets = database.get_saas_connection_secrets(aid, "shopify")
    check("secrets helper is server-only (has token)",
          secrets and secrets.get("access_token") == "tok_from_sh_oauth")

    replay = c.get(f"/saas/shopify/oauth/callback?{qs}", follow_redirects=False)
    check("oauth state is single-use",
          "shopify_error" in (replay.headers.get("location") or ""))

    # Shop-swap: state was minted for SHOP, callback claims another shop.
    state2 = database.create_saas_oauth_state(aid, "shopify", payload=SHOP)
    swap = {
        "code": "sh_test2",
        "shop": "other-store.myshopify.com",
        "state": state2,
        "timestamp": str(int(time.time())),
    }
    swap["hmac"] = oauth_hmac(swap)
    swapped = c.get(
        f"/saas/shopify/oauth/callback?{urllib.parse.urlencode(swap)}",
        follow_redirects=False,
    )
    check("oauth shop-swap is rejected",
          "shopify_error" in (swapped.headers.get("location") or ""))

    print("\n--- Stripe / HubSpot routes reject Shopify HMAC ---")
    body = json.dumps(order()).encode()
    sig = sign_body(body)
    stripe_as_sh = c.post(
        "/saas/stripe/webhook",
        content=body,
        headers={
            "X-Shopify-Hmac-SHA256": sig,
            "X-Shopify-Shop-Domain": SHOP,
            "X-Shopify-Topic": "orders/create",
            "Content-Type": "application/json",
        },
    )
    check("Shopify HMAC is not accepted on Stripe SaaS route",
          stripe_as_sh.status_code == 400)
    hs_as_sh = c.post(
        "/saas/hubspot/webhook",
        content=body,
        headers={
            "X-Shopify-Hmac-SHA256": sig,
            "X-Shopify-Shop-Domain": SHOP,
            "X-Shopify-Topic": "orders/create",
            "Content-Type": "application/json",
        },
    )
    check("Shopify HMAC is not accepted on HubSpot route",
          hs_as_sh.status_code == 400)

    print("\n--- billing webhook isolation ---")
    c.post("/v1/traces", json={"resourceSpans": [{
        "resource": {"attributes": kv({"service.name": "shop-agent"})},
        "scopeSpans": [{"spans": [sp("message_received", 50, {
            "trovis.loop.title": "Billing isolation SH",
            "trovis.loop.external_id": "loop-bill-sh",
        })]}],
    }]}, headers={"X-Trovis-Api-Key": K})
    ev = {
        "id": "evt_billing_sh",
        "type": "payment_intent.succeeded",
        "account": "acct_saas",
        "created": int(time.time()),
        "data": {"object": {
            "id": "pi_sh",
            "metadata": {
                "account_id": str(aid),
                "plan": "pro",
                "trovis_loop_external_id": "loop-bill-sh",
            },
            "payment_status": "paid",
        }},
    }
    body = json.dumps(ev).encode()
    try:
        import stripe as stripe_sdk  # noqa: F401
        have_stripe = True
    except ImportError:
        have_stripe = False
    if have_stripe:
        ts_s = int(time.time())
        mac = hmac.new(b"whsec_billing_test", f"{ts_s}.".encode() + body, hashlib.sha256).hexdigest()
        br = c.post(
            "/billing/webhook",
            content=body,
            headers={"Stripe-Signature": f"t={ts_s},v1={mac}"},
        )
        check("billing webhook still accepts its own signature", br.status_code == 200)
        iso_item = work_item(c, H, "Billing isolation SH")
        check("billing webhook did not attach a Shopify wait",
              iso_item and iso_item["status"] == "moving")
        check("billing webhook did not raise the plan from this event",
              database.get_account(aid)["plan"] == "free")
    else:
        check("stripe SDK present for billing isolation (skipped if missing)", False)

    check("Shopify webhook never writes a paid plan",
          database.get_account(aid)["plan"] == "free")

    print("\n--- duplicate event is a no-op ---")
    database.upsert_saas_connection(
        aid, "shopify", provider_account_id=SHOP,
        access_token="tok_sh", status="connected",
    )
    post_shop(c, order(
        financial_status="pending",
        note_attributes=[{"name": "trovis_loop_external_id", "value": "loop-bill-sh"}],
    ), webhook_id="wh_dup")
    again = post_shop(c, order(
        financial_status="pending",
        note_attributes=[{"name": "trovis_loop_external_id", "value": "loop-bill-sh"}],
    ), webhook_id="wh_dup")
    check("replayed Shopify webhook id is ignored_duplicate",
          again.json().get("status") == "ignored_duplicate")


print()
if failures:
    print(f"{len(failures)} FAIL: {failures}")
    raise SystemExit(1)
print("All SaaS Shopify checks passed.")
