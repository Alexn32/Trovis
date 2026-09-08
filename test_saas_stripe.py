"""SaaS Work-event spine + Stripe adapter.

Hard locks this file guards:
  - No metadata → no-op (no invented loop).
  - No matching open loop → ignore.
  - /billing/webhook is isolated (plan gate only).
  - Mapped Stripe events update wait / clear / stuck on an existing loop.
  - Brand flip is covered in frontend/test/brandMarks.test.mjs (after E2E).

Run:
  TROVIS_DISABLE_PRICING_SYNC=1 python3 test_saas_stripe.py
"""
from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import os
import tempfile
import time

os.environ.update({
    "OVERSEE_DISABLE_PRICING_SYNC": "1",
    "TROVIS_DISABLE_PRICING_SYNC": "1",
    "TROVIS_DISABLE_ALERTS": "1",
    "TROVIS_DISABLE_LOOP_SWEEP": "1",
    "TROVIS_LOOP_TITLES": "off",
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


def sign(payload: bytes, secret: str, ts: int | None = None) -> str:
    ts = int(ts if ts is not None else time.time())
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def event(etype, obj, *, eid="evt_1", account="acct_saas", created=None):
    return {
        "id": eid,
        "type": etype,
        "account": account,
        "created": created or int(time.time()),
        "data": {"object": obj},
    }


def post_saas(c, ev, secret="whsec_saas_test"):
    body = json.dumps(ev).encode()
    return c.post(
        "/saas/stripe/webhook",
        content=body,
        headers={
            "Stripe-Signature": sign(body, secret),
            "Content-Type": "application/json",
        },
    )


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


print("-- extract_link_key --")
check("preferred underscore key wins",
      saas.extract_link_key({
          "trovis_loop_external_id": "L1",
          "trovis.run.id": "r1",
      }) == "L1")
check("dotted loop key accepted",
      saas.extract_link_key({"trovis.loop.external_id": "L2"}) == "L2")
check("run_id underscore accepted",
      saas.extract_link_key({"trovis_run_id": "run-9"}) == "run-9")
check("dotted run.id accepted",
      saas.extract_link_key({"trovis.run.id": "run-8"}) == "run-8")
check("empty / missing is None",
      saas.extract_link_key({}) is None
      and saas.extract_link_key(None) is None
      and saas.extract_link_key({"foo": "bar"}) is None)
check("whitespace-only is None",
      saas.extract_link_key({"trovis_loop_external_id": "  "}) is None)

print("-- map_event --")
m = saas_stripe.map_event(event("payment_intent.processing", {
    "id": "pi_1", "metadata": {"trovis_loop_external_id": "n1"},
}))
check("processing → wait + payment processing",
      m and m["effect"] == "wait" and m["waiting_on"] == "payment processing")
m = saas_stripe.map_event(event("payment_intent.succeeded", {
    "id": "pi_1", "metadata": {"trovis_loop_external_id": "n1"},
}))
check("succeeded → clear", m and m["effect"] == "clear")
m = saas_stripe.map_event(event("payment_intent.payment_failed", {
    "id": "pi_1",
    "metadata": {"trovis_loop_external_id": "n1"},
    "last_payment_error": {"message": "Your card was declined."},
}))
check("payment_failed → stuck with reason",
      m and m["effect"] == "stuck" and "declined" in (m.get("reason") or ""))
m = saas_stripe.map_event(event("invoice.paid", {
    "id": "in_1", "metadata": {"trovis.loop.external_id": "n1"},
}))
check("invoice.paid → clear", m and m["effect"] == "clear")
m = saas_stripe.map_event(event("invoice.payment_failed", {
    "id": "in_1", "metadata": {"trovis_run_id": "n1"},
}))
check("invoice.payment_failed → stuck", m and m["effect"] == "stuck")
m = saas_stripe.map_event(event("charge.dispute.created", {
    "id": "dp_1", "metadata": {"trovis.run.id": "n1"}, "status": "needs_response",
}))
check("dispute.created → stuck", m and m["effect"] == "stuck" and m["waiting_on"] == "dispute")
m = saas_stripe.map_event(event("charge.dispute.closed", {
    "id": "dp_1", "metadata": {"trovis_loop_external_id": "n1"}, "status": "won",
}))
check("dispute.closed won → clear", m and m["effect"] == "clear")
m = saas_stripe.map_event(event("charge.dispute.closed", {
    "id": "dp_1", "metadata": {"trovis_loop_external_id": "n1"}, "status": "lost",
}))
check("dispute.closed lost → stuck", m and m["effect"] == "stuck")
check("checkout.session.completed is unmapped (billing-only)",
      saas_stripe.map_event(event("checkout.session.completed", {"id": "cs_1"})) is None)
check("customer.subscription.updated is unmapped",
      saas_stripe.map_event(event("customer.subscription.updated", {"id": "sub_1"})) is None)

print("-- webhook secret isolation --")
check("SaaS secret is STRIPE_SAAS_WEBHOOK_SECRET",
      saas_stripe.webhook_secret() == "whsec_saas_test")
check("billing secret is STRIPE_WEBHOOK_SECRET",
      billing.webhook_secret() == "whsec_billing_test")
check("SaaS never falls back to billing secret",
      saas_stripe.webhook_secret() != billing.webhook_secret())
src = inspect.getsource(main.billing_webhook)
check("billing_webhook source does not call saas",
      "saas_stripe" not in src and "saas.apply" not in src)
check("billing_webhook still uses billing.parse_webhook_event",
      "billing.parse_webhook_event" in src)
src_saas = inspect.getsource(main.saas_stripe_webhook)
check("SaaS webhook uses saas_stripe.handle_webhook",
      "saas_stripe.handle_webhook" in src_saas)
check("SaaS webhook does not call billing.parse",
      "billing.parse_webhook_event" not in src_saas)

with TestClient(main.app) as c:
    r = c.post("/auth/signup", json={
        "email": "saas@t.com", "password": "supersecret123",
        "name": "Alex", "account_type": "business", "org_name": "Pay Co",
    }).json()
    K, T = r["api_key"], r["token"]
    H = {"Authorization": f"Bearer {T}"}
    aid = database.resolve_session(T)["account_id"]

    def post_traces(svc, spans):
        return c.post("/v1/traces", json={"resourceSpans": [{
            "resource": {"attributes": kv({"service.name": svc})},
            "scopeSpans": [{"spans": spans}],
        }]}, headers={"X-Trovis-Api-Key": K})

    # Named open loop the Stripe events will attach to.
    post_traces("bill-agent", [
        sp("message_received", 400, {
            "trovis.loop.title": "Collect invoice",
            "trovis.loop.external_id": "loop-pay",
        }),
        sp("tool_call", 200, {
            "trovis.loop.external_id": "loop-pay",
            "trovis.tool.name": "create_payment_intent",
        }),
    ])
    # A second tenant's loop with the same external_id must never be hit.
    r2 = c.post("/auth/signup", json={
        "email": "other@t.com", "password": "supersecret123",
        "name": "Other", "account_type": "individual", "org_name": "Other",
    }).json()
    c.post("/v1/traces", json={"resourceSpans": [{
        "resource": {"attributes": kv({"service.name": "other-agent"})},
        "scopeSpans": [{"spans": [sp("message_received", 100, {
            "trovis.loop.title": "Other tenant job",
            "trovis.loop.external_id": "loop-pay",
        })]}],
    }]}, headers={"X-Trovis-Api-Key": r2["api_key"]})

    database.upsert_saas_connection(
        aid, "stripe", provider_account_id="acct_saas",
        access_token="tok_test", status="connected",
    )

    print("\n--- signature + isolation ---")
    ev = event("payment_intent.processing", {
        "id": "pi_sig", "metadata": {"trovis_loop_external_id": "loop-pay"},
    }, eid="evt_sig")
    bad = c.post("/saas/stripe/webhook", content=json.dumps(ev).encode(),
                 headers={"Stripe-Signature": sign(json.dumps(ev).encode(), "whsec_wrong")})
    check("bad SaaS signature → 400", bad.status_code == 400)
    missing = c.post("/saas/stripe/webhook", content=json.dumps(ev).encode())
    check("missing signature → 400", missing.status_code == 400)
    # Billing secret must not verify the SaaS route.
    billed = c.post(
        "/saas/stripe/webhook",
        content=json.dumps(ev).encode(),
        headers={"Stripe-Signature": sign(json.dumps(ev).encode(), "whsec_billing_test")},
    )
    check("billing secret rejected on SaaS route", billed.status_code == 400)

    print("\n--- metadata miss is a no-op ---")
    loops_before = database.find_open_loop_by_external_id(aid, "invented-from-saas")
    resp = post_saas(c, event("payment_intent.processing", {
        "id": "pi_nometa", "metadata": {},
    }, eid="evt_nometa"))
    check("no metadata still 200 (ack)", resp.status_code == 200)
    check("no metadata status is ignored_no_metadata",
          resp.json().get("status") == "ignored_no_metadata")
    check("SaaS alone did not invent a loop",
          database.find_open_loop_by_external_id(aid, "invented-from-saas") is None)
    check("named loop unchanged after metadata miss",
          work_item(c, H, "Collect invoice")["status"] == "moving")

    print("\n--- no matching open loop ---")
    resp = post_saas(c, event("payment_intent.processing", {
        "id": "pi_ghost",
        "metadata": {"trovis_loop_external_id": "no-such-loop"},
    }, eid="evt_ghost"))
    check("unknown key → ignored_no_loop",
          resp.status_code == 200 and resp.json().get("status") == "ignored_no_loop")
    check("still no invented loop",
          database.find_open_loop_by_external_id(aid, "no-such-loop") is None)

    print("\n--- wait (payment_intent.processing) ---")
    resp = post_saas(c, event("payment_intent.processing", {
        "id": "pi_1",
        "metadata": {"trovis_loop_external_id": "loop-pay"},
    }, eid="evt_wait"))
    check("processing applied",
          resp.status_code == 200 and resp.json().get("status") == "applied")
    item = work_item(c, H, "Collect invoice")
    check("wait: holder is Stripe (tool)",
          item and item["holder"]["kind"] == "tool"
          and item["holder"]["name"] == "Stripe")
    check("wait: waiting_on / whats_next is payment processing",
          item and "payment processing" in (item.get("whats_next") or "").lower())
    check("wait: Work status is stuck (awaiting_system)",
          item and item["status"] == "stuck")
    evs = loop_events("loop-pay")
    hi = [e for e in evs if e["type"] == "handoff_initiated"]
    check("wait wrote to_system handoff toward Stripe",
          hi and hi[-1]["payload"].get("direction") == "to_system"
          and hi[-1]["payload"].get("target_id") == "Stripe"
          and hi[-1]["actor_type"] == "system")
    other_item = c.get("/work/items", headers={"Authorization": f"Bearer {r2['token']}"}).json()
    other = [i for i in other_item.get("items") or [] if i["title"] == "Other tenant job"]
    check("other tenant's same-key loop was not touched",
          other and other[0]["status"] == "moving")

    print("\n--- clear (payment_intent.succeeded) ---")
    resp = post_saas(c, event("payment_intent.succeeded", {
        "id": "pi_1",
        "metadata": {"trovis_loop_external_id": "loop-pay"},
    }, eid="evt_clear"))
    check("succeeded applied",
          resp.status_code == 200 and resp.json().get("status") == "applied")
    item = work_item(c, H, "Collect invoice")
    check("clear: work is moving again",
          item and item["status"] == "moving")
    evs = loop_events("loop-pay")
    check("clear wrote handoff_completed",
          any(e["type"] == "handoff_completed" for e in evs))

    print("\n--- stuck (payment_intent.payment_failed) ---")
    resp = post_saas(c, event("payment_intent.payment_failed", {
        "id": "pi_2",
        "metadata": {"trovis_loop_external_id": "loop-pay"},
        "last_payment_error": {"message": "Your card was declined."},
    }, eid="evt_fail"))
    check("failed applied",
          resp.status_code == 200 and resp.json().get("status") == "applied")
    item = work_item(c, H, "Collect invoice")
    check("stuck: status stuck + failure reason surfaced",
          item and item["status"] == "stuck"
          and "declined" in (item.get("whats_next") or "").lower())

    print("\n--- invoice.paid clears the payment wait ---")
    resp = post_saas(c, event("invoice.paid", {
        "id": "in_1",
        "metadata": {"trovis.loop.external_id": "loop-pay"},
    }, eid="evt_in_paid"))
    check("invoice.paid applied",
          resp.status_code == 200 and resp.json().get("status") == "applied")
    item = work_item(c, H, "Collect invoice")
    check("invoice.paid cleared the wait",
          item and item["status"] == "moving")

    print("\n--- invoice.payment_failed → stuck ---")
    resp = post_saas(c, event("invoice.payment_failed", {
        "id": "in_2",
        "metadata": {"trovis_run_id": "loop-pay"},
    }, eid="evt_in_fail"))
    check("invoice.payment_failed stuck",
          resp.status_code == 200 and resp.json().get("status") == "applied"
          and work_item(c, H, "Collect invoice")["status"] == "stuck")

    # Clear so the dispute cases start from a known wait.
    post_saas(c, event("invoice.paid", {
        "id": "in_3", "metadata": {"trovis_loop_external_id": "loop-pay"},
    }, eid="evt_in_clear2"))

    print("\n--- dispute created / closed ---")
    resp = post_saas(c, event("charge.dispute.created", {
        "id": "dp_1",
        "metadata": {"trovis.run.id": "loop-pay"},
        "status": "needs_response",
    }, eid="evt_dp_open"))
    item = work_item(c, H, "Collect invoice")
    check("dispute.created → stuck (dispute)",
          resp.json().get("status") == "applied"
          and item["status"] == "stuck"
          and "dispute" in (item.get("whats_next") or "").lower())

    resp = post_saas(c, event("charge.dispute.closed", {
        "id": "dp_1",
        "metadata": {"trovis_loop_external_id": "loop-pay"},
        "status": "lost",
    }, eid="evt_dp_lost"))
    item = work_item(c, H, "Collect invoice")
    check("dispute.closed lost stays stuck",
          resp.json().get("status") == "applied" and item["status"] == "stuck")

    resp = post_saas(c, event("charge.dispute.closed", {
        "id": "dp_1",
        "metadata": {"trovis_loop_external_id": "loop-pay"},
        "status": "won",
    }, eid="evt_dp_won"))
    item = work_item(c, H, "Collect invoice")
    check("dispute.closed won → clear",
          resp.json().get("status") == "applied" and item["status"] == "moving")

    print("\n--- unmapped + closed loop + disconnected ---")
    resp = post_saas(c, event("checkout.session.completed", {
        "id": "cs_1", "metadata": {"trovis_loop_external_id": "loop-pay"},
        "payment_status": "paid",
    }, eid="evt_unmapped"))
    check("unmapped type is ignored (not applied to Work)",
          resp.status_code == 200 and resp.json().get("status") == "ignored_unmapped")

    # Close the loop, then a Stripe event must ignore it.
    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute("SELECT id FROM loops WHERE external_id = ? AND account_id = ?",
                    ("loop-pay", aid))
        lid = cur.fetchone()["id"]
    with database._connect() as conn, database._cursor(conn) as cur:
        database.append_loop_event(
            cur, lid, "loop_closed", "agent", "bill-agent:main",
            payload={"reason": "completed_by_agent"}, account_id=aid,
        )
        cur.execute("UPDATE loops SET closed_at = CURRENT_TIMESTAMP, cached_state = 'done' "
                    "WHERE id = ?", (lid,))
    resp = post_saas(c, event("payment_intent.processing", {
        "id": "pi_closed",
        "metadata": {"trovis_loop_external_id": "loop-pay"},
    }, eid="evt_closed"))
    check("closed loop is ignored (no reopen)",
          resp.json().get("status") == "ignored_no_loop")

    database.disconnect_saas_connection(aid, "stripe")
    resp = post_saas(c, event("payment_intent.processing", {
        "id": "pi_dc",
        "metadata": {"trovis_loop_external_id": "loop-pay"},
    }, eid="evt_dc"))
    check("disconnected connection ignores events",
          resp.json().get("status") == "ignored_no_connection")

    print("\n--- OAuth token storage ---")
    database.upsert_saas_connection(
        aid, "stripe", provider_account_id="acct_saas",
        access_token="tok_test", status="connected",
    )
    start = c.get("/saas/stripe/oauth/start", headers=H)
    check("oauth start returns authorize_url",
          start.status_code == 200 and "connect.stripe.com/oauth/authorize" in start.json().get("authorize_url", ""))
    check("oauth start is account-authed (401 without)",
          c.get("/saas/stripe/oauth/start").status_code == 401)

    def fake_exchange(code):
        return {
            "access_token": "tok_from_oauth",
            "refresh_token": "rt_from_oauth",
            "stripe_user_id": "acct_oauth",
            "livemode": False,
            "scope": "read_write",
            "token_type": "bearer",
        }

    saas_stripe._oauth_token_exchange = fake_exchange
    state = database.create_saas_oauth_state(aid, "stripe")
    cb = c.get(f"/saas/stripe/oauth/callback?code=ac_test&state={state}", follow_redirects=False)
    check("oauth callback redirects to app",
          cb.status_code in (302, 307) and "saas=stripe_connected" in (cb.headers.get("location") or ""))
    listed = c.get("/saas/connections", headers=H).json()
    stripe_row = next((x for x in listed["connections"] if x["provider"] == "stripe"), None)
    check("connection stored with stripe_user_id",
          stripe_row and stripe_row["status"] == "connected"
          and stripe_row["provider_account_id"] == "acct_oauth")
    check("tokens never appear on the connections API",
          stripe_row and "access_token" not in stripe_row)
    # Raw token is in the DB but the public helper stripped it.
    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute("SELECT access_token FROM saas_connections WHERE account_id = ? AND provider = ?",
                    (aid, "stripe"))
        tok = cur.fetchone()["access_token"]
    check("access_token persisted server-side", tok == "tok_from_oauth")

    replay = c.get(f"/saas/stripe/oauth/callback?code=ac_test&state={state}", follow_redirects=False)
    check("oauth state is single-use",
          "stripe_error" in (replay.headers.get("location") or ""))

    print("\n--- billing webhook isolation ---")
    # A Work-shaped PI event hitting /billing/webhook must NOT attach to loops.
    # Re-open a fresh named loop for this assertion.
    c.post("/v1/traces", json={"resourceSpans": [{
        "resource": {"attributes": kv({"service.name": "bill-agent"})},
        "scopeSpans": [{"spans": [sp("message_received", 50, {
            "trovis.loop.title": "Billing isolation",
            "trovis.loop.external_id": "loop-bill-iso",
        })]}],
    }]}, headers={"X-Trovis-Api-Key": K})
    ev = event("payment_intent.succeeded", {
        "id": "pi_billing",
        "metadata": {"trovis_loop_external_id": "loop-bill-iso", "account_id": str(aid), "plan": "pro"},
        "payment_status": "paid",
    }, eid="evt_billing_iso")
    body = json.dumps(ev).encode()
    # Need stripe SDK for billing.parse_webhook_event.
    try:
        import stripe as stripe_sdk  # noqa: F401
        have_stripe = True
    except ImportError:
        have_stripe = False
    if have_stripe:
        br = c.post(
            "/billing/webhook",
            content=body,
            headers={"Stripe-Signature": sign(body, "whsec_billing_test")},
        )
        check("billing webhook accepts its own signature", br.status_code == 200)
        iso_item = work_item(c, H, "Billing isolation")
        check("billing webhook did not attach a SaaS wait to the loop",
              iso_item and iso_item["status"] == "moving")
        check("billing webhook did not raise the plan from a PI event",
              database.get_account(aid)["plan"] == "free")
    else:
        check("stripe SDK present for billing isolation (skipped if missing)", False)

    # SaaS route + checkout.session.completed must not set a paid plan.
    database.upsert_saas_connection(
        aid, "stripe", provider_account_id="acct_saas",
        access_token="tok_test", status="connected",
    )
    resp = post_saas(c, event("checkout.session.completed", {
        "id": "cs_plan",
        "metadata": {"account_id": str(aid), "plan": "pro", "trovis_loop_external_id": "loop-bill-iso"},
        "payment_status": "paid",
        "client_reference_id": str(aid),
    }, eid="evt_saas_plan"))
    check("SaaS webhook ignores checkout.session.completed",
          resp.json().get("status") == "ignored_unmapped")
    check("SaaS webhook never writes a paid plan",
          database.get_account(aid)["plan"] == "free")

    print("\n--- duplicate event is a no-op ---")
    post_saas(c, event("payment_intent.processing", {
        "id": "pi_dup",
        "metadata": {"trovis_loop_external_id": "loop-bill-iso"},
    }, eid="evt_dup"))
    again = post_saas(c, event("payment_intent.processing", {
        "id": "pi_dup",
        "metadata": {"trovis_loop_external_id": "loop-bill-iso"},
    }, eid="evt_dup"))
    check("replayed Stripe event_id is ignored_duplicate",
          again.json().get("status") == "ignored_duplicate")


print()
if failures:
    print(f"{len(failures)} FAIL: {failures}")
    raise SystemExit(1)
print("All SaaS Stripe checks passed.")
