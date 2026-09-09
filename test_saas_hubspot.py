"""SaaS Work-event spine + HubSpot adapter.

Connect mapping contract (HubSpot PR B):
  Link (require one): trovis_loop_external_id | trovis.loop.external_id |
    trovis_run_id | trovis.run.id → open loop this account; else no-op.
  Deals (dealstage):
    contractsent / pending / waiting / payment-style → wait
    closedwon / completed → clear
    closedlost → stuck
  Tickets (hs_pipeline_stage):
    waiting on contact / us / agent → wait
    solved / closed → clear
    escalated / on-hold failure → stuck
  Locks: explicit metadata only; never invent a loop; no CRM sync;
    never touch /billing/webhook; reuse saas.py + saas_connections;
    HubSpot brand coming→live only after this suite; no Intercom/Slack/GitHub/Shopify.

Run:
  TROVIS_DISABLE_PRICING_SYNC=1 python3 test_saas_hubspot.py
"""
from __future__ import annotations

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
    "HUBSPOT_SAAS_CLIENT_ID": "hubspot-client-test",
    "HUBSPOT_SAAS_CLIENT_SECRET": "hubspot-secret-test",
    "HUBSPOT_SAAS_WEBHOOK_URI": "https://app.trovis.test/saas/hubspot/webhook",
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
WEBHOOK_URI = os.environ["HUBSPOT_SAAS_WEBHOOK_URI"]


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


def hs_event(
    *,
    subscription="deal.propertyChange",
    property_name="dealstage",
    property_value="contractsent",
    object_id=101,
    event_id=1,
    portal_id=999001,
    occurred=None,
    object_type_id=None,
):
    ev = {
        "eventId": event_id,
        "subscriptionType": subscription,
        "propertyName": property_name,
        "propertyValue": property_value,
        "objectId": object_id,
        "portalId": portal_id,
        "occurredAt": occurred or int(time.time() * 1000),
        "attemptNumber": 0,
    }
    if object_type_id:
        ev["objectTypeId"] = object_type_id
    return ev


def sign_v3(payload: bytes, ts: str | None = None) -> tuple[str, str]:
    ts = ts if ts is not None else str(int(time.time() * 1000))
    sig = saas_hubspot._sign_v3("POST", WEBHOOK_URI, payload, ts, "hubspot-secret-test")
    return sig, ts


def post_hs(c, events, *, secret=None):
    body = json.dumps(events if isinstance(events, list) else [events]).encode()
    if secret is not None:
        # Caller wants a wrong/other secret — still v3 shape.
        ts = str(int(time.time() * 1000))
        sig = saas_hubspot._sign_v3("POST", WEBHOOK_URI, body, ts, secret)
    else:
        sig, ts = sign_v3(body)
    return c.post(
        "/saas/hubspot/webhook",
        content=body,
        headers={
            "X-HubSpot-Signature-v3": sig,
            "X-HubSpot-Request-Timestamp": ts,
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


# In-memory CRM stand-in. Webhooks never invent a loop key — they read it here.
CRM = {}
STAGE_LABELS = {
    ("deals", "123456789"): "Awaiting Payment",
    ("tickets", "99"): "Escalated",
    ("tickets", "88"): "On-hold failure",
}


def fake_fetch(access_token, object_type, object_id, extra_properties=None):
    return dict(CRM.get((object_type, str(object_id)), {}))


def fake_label(access_token, object_type, stage_id):
    return STAGE_LABELS.get((object_type, str(stage_id)))


saas_hubspot._fetch_crm_properties = fake_fetch
saas_hubspot._fetch_stage_label = fake_label


print("-- extract_link_key (shared spine) --")
check("HubSpot is a labeled provider", saas.PROVIDER_LABELS.get("hubspot") == "HubSpot")
check("preferred underscore key wins",
      saas.extract_link_key({
          "trovis_loop_external_id": "L1",
          "trovis.run.id": "r1",
      }) == "L1")
check("empty HubSpot properties is None",
      saas.extract_link_key({}) is None)

print("-- map_stage deals --")
check("contractsent → wait",
      saas_hubspot.map_stage("deals", "contractsent") == "wait")
check("pending → wait",
      saas_hubspot.map_stage("deals", "pending") == "wait")
check("custom awaiting payment label → wait",
      saas_hubspot.map_stage("deals", "123456789", "Awaiting Payment") == "wait")
check("closedwon → clear",
      saas_hubspot.map_stage("deals", "closedwon") == "clear")
check("Closed Won label → clear",
      saas_hubspot.map_stage("deals", "custom1", "Closed Won") == "clear")
check("completed label → clear",
      saas_hubspot.map_stage("deals", "custom2", "Completed") == "clear")
check("closedlost → stuck",
      saas_hubspot.map_stage("deals", "closedlost") == "stuck")
check("Closed Lost label → stuck",
      saas_hubspot.map_stage("deals", "custom3", "Closed Lost") == "stuck")
check("appointmentscheduled unmapped",
      saas_hubspot.map_stage("deals", "appointmentscheduled") is None)
check("qualifiedtobuy unmapped",
      saas_hubspot.map_stage("deals", "qualifiedtobuy") is None)
check("presentationscheduled unmapped",
      saas_hubspot.map_stage("deals", "presentationscheduled") is None)

print("-- map_stage tickets --")
check("default id 2 → wait",
      saas_hubspot.map_stage("tickets", "2") == "wait")
check("default id 3 → wait",
      saas_hubspot.map_stage("tickets", "3") == "wait")
check("Waiting on contact → wait",
      saas_hubspot.map_stage("tickets", "2", "Waiting on contact") == "wait")
check("Waiting on us → wait",
      saas_hubspot.map_stage("tickets", "3", "Waiting on us") == "wait")
check("Waiting on agent → wait",
      saas_hubspot.map_stage("tickets", "x", "Waiting on agent") == "wait")
check("Waiting on customer → wait",
      saas_hubspot.map_stage("tickets", "x", "Waiting on customer") == "wait")
check("default id 4 → clear",
      saas_hubspot.map_stage("tickets", "4") == "clear")
check("Closed → clear",
      saas_hubspot.map_stage("tickets", "4", "Closed") == "clear")
check("Solved → clear",
      saas_hubspot.map_stage("tickets", "s", "Solved") == "clear")
check("Escalated → stuck",
      saas_hubspot.map_stage("tickets", "99", "Escalated") == "stuck")
check("On-hold failure → stuck",
      saas_hubspot.map_stage("tickets", "88", "On-hold failure") == "stuck")
check("New (id 1) unmapped",
      saas_hubspot.map_stage("tickets", "1", "New") is None)

print("-- map_event --")
m = saas_hubspot.map_event(hs_event(), properties={"trovis_loop_external_id": "n1"})
check("deal contractsent → wait",
      m and m["effect"] == "wait" and m["object_type"] == "deals")
m = saas_hubspot.map_event(hs_event(property_value="closedwon"),
                           properties={"trovis_loop_external_id": "n1"})
check("deal closedwon → clear", m and m["effect"] == "clear")
m = saas_hubspot.map_event(hs_event(property_value="closedlost"),
                           properties={"trovis_loop_external_id": "n1"})
check("deal closedlost → stuck", m and m["effect"] == "stuck")
m = saas_hubspot.map_event(hs_event(
    subscription="ticket.propertyChange",
    property_name="hs_pipeline_stage",
    property_value="2",
    object_id=201,
), properties={"trovis_run_id": "n1"}, stage_label="Waiting on contact")
check("ticket waiting on contact → wait", m and m["effect"] == "wait")
m = saas_hubspot.map_event(hs_event(
    subscription="ticket.propertyChange",
    property_name="hs_pipeline_stage",
    property_value="4",
), properties={"trovis.loop.external_id": "n1"}, stage_label="Closed")
check("ticket closed → clear", m and m["effect"] == "clear")
m = saas_hubspot.map_event(hs_event(
    subscription="ticket.propertyChange",
    property_name="hs_pipeline_stage",
    property_value="99",
), properties={"trovis.run.id": "n1"}, stage_label="Escalated")
check("ticket escalated → stuck", m and m["effect"] == "stuck")
check("contact.propertyChange is unmapped (no CRM sync)",
      saas_hubspot.map_event(hs_event(
          subscription="contact.propertyChange",
          property_name="email",
          property_value="a@b.com",
      ), properties={"trovis_loop_external_id": "n1"}) is None)
check("deal amount change is unmapped",
      saas_hubspot.map_event(hs_event(
          property_name="amount", property_value="100",
      ), properties={"trovis_loop_external_id": "n1"}) is None)
check("company.propertyChange is unmapped",
      saas_hubspot.map_event(hs_event(
          subscription="company.propertyChange",
          property_name="name",
      )) is None)

print("-- webhook secret isolation --")
check("HubSpot signs with HUBSPOT_SAAS_CLIENT_SECRET",
      saas_hubspot.client_secret() == "hubspot-secret-test")
check("HubSpot never reads STRIPE_WEBHOOK_SECRET",
      saas_hubspot.client_secret() != billing.webhook_secret())
check("HubSpot never reads STRIPE_SAAS_WEBHOOK_SECRET",
      saas_hubspot.client_secret() != saas_stripe.webhook_secret())
src = inspect.getsource(main.billing_webhook)
check("billing_webhook source does not call hubspot",
      "saas_hubspot" not in src and "saas.apply" not in src)
check("billing_webhook still uses billing.parse_webhook_event",
      "billing.parse_webhook_event" in src)
src_stripe = inspect.getsource(main.saas_stripe_webhook)
check("Stripe SaaS webhook untouched (no hubspot handle)",
      "saas_hubspot" not in src_stripe and "saas_stripe.handle_webhook" in src_stripe)
src_hs = inspect.getsource(main.saas_hubspot_webhook)
check("HubSpot webhook uses saas_hubspot.handle_webhook",
      "saas_hubspot.handle_webhook" in src_hs)
check("HubSpot webhook does not call billing.parse",
      "billing.parse_webhook_event" not in src_hs)
check("HubSpot webhook does not call saas_stripe",
      "saas_stripe" not in src_hs)

with TestClient(main.app) as c:
    r = c.post("/auth/signup", json={
        "email": "hs@t.com", "password": "supersecret123",
        "name": "Alex", "account_type": "business", "org_name": "Deal Co",
    }).json()
    K, T = r["api_key"], r["token"]
    H = {"Authorization": f"Bearer {T}"}
    aid = database.resolve_session(T)["account_id"]

    def post_traces(svc, spans):
        return c.post("/v1/traces", json={"resourceSpans": [{
            "resource": {"attributes": kv({"service.name": svc})},
            "scopeSpans": [{"spans": spans}],
        }]}, headers={"X-Trovis-Api-Key": K})

    post_traces("crm-agent", [
        sp("message_received", 400, {
            "trovis.loop.title": "Close the Acme deal",
            "trovis.loop.external_id": "loop-deal",
        }),
        sp("tool_call", 200, {
            "trovis.loop.external_id": "loop-deal",
            "trovis.tool.name": "update_deal",
        }),
    ])
    r2 = c.post("/auth/signup", json={
        "email": "other-hs@t.com", "password": "supersecret123",
        "name": "Other", "account_type": "individual", "org_name": "Other",
    }).json()
    c.post("/v1/traces", json={"resourceSpans": [{
        "resource": {"attributes": kv({"service.name": "other-agent"})},
        "scopeSpans": [{"spans": [sp("message_received", 100, {
            "trovis.loop.title": "Other tenant job",
            "trovis.loop.external_id": "loop-deal",
        })]}],
    }]}, headers={"X-Trovis-Api-Key": r2["api_key"]})

    database.upsert_saas_connection(
        aid, "hubspot", provider_account_id="999001",
        access_token="tok_hs", refresh_token="rt_hs", status="connected",
    )
    listed = c.get("/saas/connections", headers=H).json()
    check("connections lists hubspot_oauth_configured",
          listed.get("hubspot_oauth_configured") is True)
    hs_row = next((x for x in listed["connections"] if x["provider"] == "hubspot"), None)
    check("tokens never appear on the connections API",
          hs_row and "access_token" not in hs_row and "refresh_token" not in hs_row)

    print("\n--- signature + isolation ---")
    ev = hs_event(event_id=10)
    CRM[("deals", "101")] = {"trovis_loop_external_id": "loop-deal", "dealstage": "contractsent"}
    bad = post_hs(c, ev, secret="wrong-secret")
    check("bad HubSpot signature → 400", bad.status_code == 400)
    missing = c.post("/saas/hubspot/webhook", content=json.dumps([ev]).encode())
    check("missing signature → 400", missing.status_code == 400)
    billed = post_hs(c, ev, secret="whsec_billing_test")
    check("billing secret rejected on HubSpot route", billed.status_code == 400)
    stripe_secret = post_hs(c, ev, secret="whsec_saas_test")
    check("Stripe SaaS secret rejected on HubSpot route", stripe_secret.status_code == 400)

    print("\n--- metadata miss is a no-op ---")
    CRM[("deals", "102")] = {"dealstage": "contractsent"}  # no trovis_* key
    resp = post_hs(c, hs_event(object_id=102, event_id=20, property_value="contractsent"))
    check("no metadata still 200 (ack)", resp.status_code == 200)
    check("no metadata status is ignored_no_metadata",
          resp.json().get("status") == "ignored_no_metadata")
    check("HubSpot alone did not invent a loop",
          database.find_open_loop_by_external_id(aid, "invented-from-hubspot") is None)
    check("named loop unchanged after metadata miss",
          work_item(c, H, "Close the Acme deal")["status"] == "moving")

    print("\n--- fetch miss (empty properties) is a no-op ---")
    CRM.pop(("deals", "103"), None)
    resp = post_hs(c, hs_event(object_id=103, event_id=21, property_value="contractsent"))
    check("missing CRM properties → ignored_no_metadata",
          resp.status_code == 200 and resp.json().get("status") == "ignored_no_metadata")
    check("still no invented loop after fetch miss",
          database.find_open_loop_by_external_id(aid, "103") is None)

    print("\n--- no matching open loop ---")
    CRM[("deals", "104")] = {"trovis_loop_external_id": "no-such-loop"}
    resp = post_hs(c, hs_event(object_id=104, event_id=22, property_value="contractsent"))
    check("unknown key → ignored_no_loop",
          resp.status_code == 200 and resp.json().get("status") == "ignored_no_loop")
    check("still no invented loop",
          database.find_open_loop_by_external_id(aid, "no-such-loop") is None)

    print("\n--- wait (deal contractsent) ---")
    CRM[("deals", "101")] = {"trovis_loop_external_id": "loop-deal", "dealstage": "contractsent"}
    resp = post_hs(c, hs_event(object_id=101, event_id=30, property_value="contractsent"))
    check("contractsent applied",
          resp.status_code == 200 and resp.json().get("status") == "applied")
    item = work_item(c, H, "Close the Acme deal")
    check("wait: holder is HubSpot (tool)",
          item and item["holder"]["kind"] == "tool"
          and item["holder"]["name"] == "HubSpot")
    check("wait: waiting_on mentions contract/deal",
          item and (
              "contract" in (item.get("whats_next") or "").lower()
              or "deal" in (item.get("whats_next") or "").lower()
          ))
    check("wait: Work status is stuck (awaiting_system)",
          item and item["status"] == "stuck")
    evs = loop_events("loop-deal")
    hi = [e for e in evs if e["type"] == "handoff_initiated"]
    check("wait wrote to_system handoff toward HubSpot",
          hi and hi[-1]["payload"].get("direction") == "to_system"
          and hi[-1]["payload"].get("target_id") == "HubSpot"
          and hi[-1]["actor_type"] == "system")
    other_item = c.get("/work/items", headers={"Authorization": f"Bearer {r2['token']}"}).json()
    other = [i for i in other_item.get("items") or [] if i["title"] == "Other tenant job"]
    check("other tenant's same-key loop was not touched",
          other and other[0]["status"] == "moving")

    print("\n--- clear (deal closedwon) ---")
    CRM[("deals", "101")]["dealstage"] = "closedwon"
    resp = post_hs(c, hs_event(object_id=101, event_id=31, property_value="closedwon"))
    check("closedwon applied",
          resp.status_code == 200 and resp.json().get("status") == "applied")
    item = work_item(c, H, "Close the Acme deal")
    check("clear: work is moving again",
          item and item["status"] == "moving")
    evs = loop_events("loop-deal")
    check("clear wrote handoff_completed",
          any(e["type"] == "handoff_completed" for e in evs))

    print("\n--- stuck (deal closedlost) ---")
    CRM[("deals", "101")]["dealstage"] = "closedlost"
    resp = post_hs(c, hs_event(object_id=101, event_id=32, property_value="closedlost"))
    check("closedlost applied",
          resp.status_code == 200 and resp.json().get("status") == "applied")
    item = work_item(c, H, "Close the Acme deal")
    check("stuck: status stuck + lost reason surfaced",
          item and item["status"] == "stuck"
          and "lost" in (item.get("whats_next") or "").lower())

    print("\n--- dotted + run_id link keys ---")
    # Clear first so the wait below is a real apply.
    CRM[("deals", "101")]["trovis.loop.external_id"] = "loop-deal"
    CRM[("deals", "101")].pop("trovis_loop_external_id", None)
    CRM[("deals", "101")]["dealstage"] = "closedwon"
    resp = post_hs(c, hs_event(object_id=101, event_id=33, property_value="closedwon"))
    check("dotted trovis.loop.external_id clears",
          resp.json().get("status") in ("applied", "noop_already")
          and work_item(c, H, "Close the Acme deal")["status"] == "moving")

    print("\n--- tickets ---")
    post_traces("crm-agent", [
        sp("message_received", 40, {
            "trovis.loop.title": "Support ticket 88",
            "trovis.loop.external_id": "loop-tix",
        }),
    ])
    CRM[("tickets", "201")] = {
        "trovis_run_id": "loop-tix",
        "hs_pipeline_stage": "2",
    }
    resp = post_hs(c, hs_event(
        subscription="ticket.propertyChange",
        property_name="hs_pipeline_stage",
        property_value="2",
        object_id=201,
        event_id=40,
    ))
    item = work_item(c, H, "Support ticket 88")
    check("ticket waiting applied",
          resp.json().get("status") == "applied"
          and item and item["status"] == "stuck"
          and item["holder"]["name"] == "HubSpot")

    CRM[("tickets", "201")]["hs_pipeline_stage"] = "4"
    resp = post_hs(c, hs_event(
        subscription="ticket.propertyChange",
        property_name="hs_pipeline_stage",
        property_value="4",
        object_id=201,
        event_id=41,
    ))
    check("ticket closed clears",
          resp.json().get("status") == "applied"
          and work_item(c, H, "Support ticket 88")["status"] == "moving")

    CRM[("tickets", "201")]["hs_pipeline_stage"] = "99"
    resp = post_hs(c, hs_event(
        subscription="ticket.propertyChange",
        property_name="hs_pipeline_stage",
        property_value="99",
        object_id=201,
        event_id=42,
    ))
    item = work_item(c, H, "Support ticket 88")
    check("ticket escalated → stuck",
          resp.json().get("status") == "applied"
          and item["status"] == "stuck")

    print("\n--- unmapped + closed loop + disconnected ---")
    CRM[("deals", "101")]["trovis_loop_external_id"] = "loop-deal"
    resp = post_hs(c, hs_event(
        property_name="amount", property_value="500",
        object_id=101, event_id=50,
    ))
    check("unmapped property is ignored",
          resp.status_code == 200 and resp.json().get("status") == "ignored_unmapped")

    resp = post_hs(c, hs_event(
        subscription="contact.propertyChange",
        property_name="email",
        object_id=9, event_id=51,
    ))
    check("contact events ignored (no CRM sync)",
          resp.json().get("status") == "ignored_unmapped")

    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute("SELECT id FROM loops WHERE external_id = ? AND account_id = ?",
                    ("loop-deal", aid))
        lid = cur.fetchone()["id"]
    with database._connect() as conn, database._cursor(conn) as cur:
        database.append_loop_event(
            cur, lid, "loop_closed", "agent", "crm-agent:main",
            payload={"reason": "completed_by_agent"}, account_id=aid,
        )
        cur.execute("UPDATE loops SET closed_at = CURRENT_TIMESTAMP, cached_state = 'done' "
                    "WHERE id = ?", (lid,))
    CRM[("deals", "101")]["dealstage"] = "contractsent"
    resp = post_hs(c, hs_event(object_id=101, event_id=52, property_value="contractsent"))
    check("closed loop is ignored (no reopen)",
          resp.json().get("status") == "ignored_no_loop")

    database.disconnect_saas_connection(aid, "hubspot")
    resp = post_hs(c, hs_event(object_id=101, event_id=53, property_value="contractsent"))
    check("disconnected connection ignores events",
          resp.json().get("status") == "ignored_no_connection")

    print("\n--- OAuth token storage ---")
    database.upsert_saas_connection(
        aid, "hubspot", provider_account_id="999001",
        access_token="tok_hs", refresh_token="rt_hs", status="connected",
    )
    start = c.get("/saas/hubspot/oauth/start", headers=H)
    check("oauth start returns authorize_url",
          start.status_code == 200
          and "app.hubspot.com/oauth/authorize" in start.json().get("authorize_url", ""))
    check("oauth scopes are deals+tickets only (no contacts)",
          "crm.objects.deals.read" in start.json().get("authorize_url", "")
          and "crm.objects.tickets.read" in start.json().get("authorize_url", "")
          and "crm.objects.contacts" not in start.json().get("authorize_url", ""))
    check("oauth start is account-authed (401 without)",
          c.get("/saas/hubspot/oauth/start").status_code == 401)

    def fake_exchange(code, redirect_uri):
        return {
            "access_token": "tok_from_hs_oauth",
            "refresh_token": "rt_from_hs_oauth",
            "token_type": "bearer",
            "expires_in": 1800,
        }

    def fake_info(access_token):
        return {"hub_id": 424242, "hub_domain": "dealco.hubspot.com"}

    saas_hubspot._oauth_token_exchange = fake_exchange
    saas_hubspot._fetch_token_info = fake_info
    state = database.create_saas_oauth_state(aid, "hubspot")
    cb = c.get(f"/saas/hubspot/oauth/callback?code=hs_test&state={state}", follow_redirects=False)
    check("oauth callback redirects to app",
          cb.status_code in (302, 307) and "saas=hubspot_connected" in (cb.headers.get("location") or ""))
    listed = c.get("/saas/connections", headers=H).json()
    hs_row = next((x for x in listed["connections"] if x["provider"] == "hubspot"), None)
    check("connection stored with hub_id",
          hs_row and hs_row["status"] == "connected"
          and hs_row["provider_account_id"] == "424242")
    check("tokens never appear on the connections API after oauth",
          hs_row and "access_token" not in hs_row)
    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute(
            "SELECT access_token, refresh_token FROM saas_connections "
            "WHERE account_id = ? AND provider = ?",
            (aid, "hubspot"),
        )
        tok = cur.fetchone()
    check("access_token persisted server-side", tok["access_token"] == "tok_from_hs_oauth")
    check("refresh_token persisted server-side", tok["refresh_token"] == "rt_from_hs_oauth")
    secrets = database.get_saas_connection_secrets(aid, "hubspot")
    check("secrets helper is server-only (has token)",
          secrets and secrets.get("access_token") == "tok_from_hs_oauth")

    replay = c.get(f"/saas/hubspot/oauth/callback?code=hs_test&state={state}", follow_redirects=False)
    check("oauth state is single-use",
          "hubspot_error" in (replay.headers.get("location") or ""))

    print("\n--- Stripe path untouched ---")
    # A HubSpot-shaped body on the Stripe SaaS route must not verify.
    body = json.dumps([hs_event(event_id=90)]).encode()
    sig, ts = sign_v3(body)
    stripe_as_hs = c.post(
        "/saas/stripe/webhook",
        content=body,
        headers={
            "X-HubSpot-Signature-v3": sig,
            "X-HubSpot-Request-Timestamp": ts,
            "Content-Type": "application/json",
        },
    )
    check("HubSpot signature is not accepted on Stripe SaaS route",
          stripe_as_hs.status_code == 400)

    print("\n--- billing webhook isolation ---")
    c.post("/v1/traces", json={"resourceSpans": [{
        "resource": {"attributes": kv({"service.name": "crm-agent"})},
        "scopeSpans": [{"spans": [sp("message_received", 50, {
            "trovis.loop.title": "Billing isolation HS",
            "trovis.loop.external_id": "loop-bill-hs",
        })]}],
    }]}, headers={"X-Trovis-Api-Key": K})
    ev = {
        "id": "evt_billing_hs",
        "type": "payment_intent.succeeded",
        "account": "acct_saas",
        "created": int(time.time()),
        "data": {"object": {
            "id": "pi_hs",
            "metadata": {
                "account_id": str(aid),
                "plan": "pro",
                "trovis_loop_external_id": "loop-bill-hs",
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
        import hashlib
        import hmac as hm
        ts_s = int(time.time())
        mac = hm.new(b"whsec_billing_test", f"{ts_s}.".encode() + body, hashlib.sha256).hexdigest()
        br = c.post(
            "/billing/webhook",
            content=body,
            headers={"Stripe-Signature": f"t={ts_s},v1={mac}"},
        )
        check("billing webhook still accepts its own signature", br.status_code == 200)
        iso_item = work_item(c, H, "Billing isolation HS")
        check("billing webhook did not attach a HubSpot wait",
              iso_item and iso_item["status"] == "moving")
        check("billing webhook did not raise the plan from this event",
              database.get_account(aid)["plan"] == "free")
    else:
        check("stripe SDK present for billing isolation (skipped if missing)", False)

    check("HubSpot webhook never writes a paid plan",
          database.get_account(aid)["plan"] == "free")

    print("\n--- duplicate event is a no-op ---")
    database.upsert_saas_connection(
        aid, "hubspot", provider_account_id="999001",
        access_token="tok_hs", refresh_token="rt_hs", status="connected",
    )
    CRM[("tickets", "301")] = {
        "trovis_loop_external_id": "loop-bill-hs",
        "hs_pipeline_stage": "2",
    }
    post_hs(c, hs_event(
        subscription="ticket.propertyChange",
        property_name="hs_pipeline_stage",
        property_value="2",
        object_id=301,
        event_id=70,
    ))
    again = post_hs(c, hs_event(
        subscription="ticket.propertyChange",
        property_name="hs_pipeline_stage",
        property_value="2",
        object_id=301,
        event_id=70,
    ))
    check("replayed HubSpot event_id is ignored_duplicate",
          again.json().get("status") == "ignored_duplicate")

    print("\n--- v1 signature still verifies ---")
    ev = hs_event(
        subscription="ticket.propertyChange",
        property_name="hs_pipeline_stage",
        property_value="4",
        object_id=301,
        event_id=71,
    )
    CRM[("tickets", "301")]["hs_pipeline_stage"] = "4"
    body = json.dumps([ev]).encode()
    v1 = saas_hubspot._sign_v1(body, "hubspot-secret-test")
    r = c.post(
        "/saas/hubspot/webhook",
        content=body,
        headers={
            "X-HubSpot-Signature": v1,
            "Content-Type": "application/json",
        },
    )
    check("v1 signature accepted",
          r.status_code == 200 and r.json().get("status") in ("applied", "noop_already"))


print()
if failures:
    print(f"{len(failures)} FAIL: {failures}")
    raise SystemExit(1)
print("All SaaS HubSpot checks passed.")
