"""Connection health — normalized semantics over facts Trovis records.

The read model (connect_health.py, GET /connect/health) must say only what
the data supports: a span proves a data path worked at that time; an OAuth
row proves authorization, not activity; a verified webhook proves provider
activity; a bare service name proves nothing about the vendor; silence is
not a fault.

Run:
  TROVIS_DISABLE_PRICING_SYNC=1 python3 test_connect_health.py
"""
from __future__ import annotations

import os
import tempfile
import time
import uuid

os.environ.update({
    "OVERSEE_DISABLE_PRICING_SYNC": "1",
    "TROVIS_DISABLE_PRICING_SYNC": "1",
    "TROVIS_DISABLE_ALERTS": "1",
    "TROVIS_DISABLE_LOOP_SWEEP": "1",
    "TROVIS_LOOP_TITLES": "off",
})
os.environ.pop("DATABASE_URL", None)
os.environ.pop("ANTHROPIC_API_KEY", None)

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()

import database
database.SQLITE_PATH = _tmp.name

import connect_health
import main
from fastapi.testclient import TestClient

main._auto_describe = lambda *a, **k: False

failures = []


def check(label, cond):
    print(("  PASS " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


def kv(d):
    return [{"key": k, "value": {"stringValue": str(v)}} for k, v in d.items()]


def span(name, start_ns=None, dur_ms=50):
    start_ns = start_ns or int(time.time() * 1e9)
    return {
        "traceId": uuid.uuid4().hex,
        "spanId": uuid.uuid4().hex[:16],
        "name": name,
        "kind": 1,
        "startTimeUnixNano": str(start_ns),
        "endTimeUnixNano": str(start_ns + dur_ms * 1_000_000),
        "attributes": [],
        "status": {"code": 1},
    }


# --- the resolver on its own -------------------------------------------------

print("identity resolution")
ident = connect_health.identify_connector
check("bare service.name is custom-otel, never a brand",
      ident({"service.name": "refund-worker"}) == ("custom-otel", "otel"))
check("a service NAMED claude is still custom-otel without a stamp",
      ident({"service.name": "claude-refund-helper"})[0] == "custom-otel")
check("SDK anthropic → claude", ident({"trovis.sdk.platform": "anthropic"}) == ("claude", "sdk"))
check("SDK claude-agent-sdk → claude", ident({"trovis.sdk.platform": "claude-agent-sdk"})[0] == "claude")
check("SDK openai → openai-agents", ident({"trovis.sdk.platform": "openai"}) == ("openai-agents", "sdk"))
check("SDK xai → grok", ident({"trovis.sdk.platform": "xai"}) == ("grok", "sdk"))
check("SDK generic modes name no vendor",
      ident({"trovis.sdk.platform": "all"})[0] == "custom-otel"
      and ident({"trovis.sdk.platform": "agent"})[0] == "custom-otel")
check("legacy trovis.platform cursor-grok-bot → grok-bot over mcp",
      ident({"trovis.platform": "cursor-grok-bot"}) == ("grok-bot", "mcp"))
check("legacy trovis.platform chatgpt → chatgpt, method unknown",
      ident({"trovis.platform": "chatgpt"}) == ("chatgpt", None))
check("OpenClaw gateway stamp → openclaw over plugin",
      ident({"openclaw.gateway.version": "2.1"}) == ("openclaw", "plugin"))
check("explicit trovis.connector.id wins over other stamps",
      ident({"trovis.connector.id": "cursor", "telemetry.sdk.language": "python"}) == ("cursor", "otel"))
check("an unknown explicit id is ignored, not trusted",
      ident({"trovis.connector.id": "zendesk"}) == ("custom-otel", "otel"))
check("Grok and Grok Bot are different connectors",
      ident({"trovis.sdk.platform": "xai"})[0] != ident({"trovis.platform": "grok-bot"})[0])
check("there is no degraded state in the enum", "degraded" not in connect_health.STATES)
check("malformed resource JSON is custom-otel, not an error",
      ident("{not json") == ("custom-otel", "otel") and ident(None) == ("custom-otel", "otel"))

# --- the endpoint ------------------------------------------------------------

with TestClient(main.app) as c:
    a = c.post("/auth/signup", json={
        "email": "health-a@t.com", "password": "supersecret123",
        "name": "Ada", "account_type": "business", "org_name": "A Co",
    }).json()
    b = c.post("/auth/signup", json={
        "email": "health-b@t.com", "password": "supersecret123",
        "name": "Bob", "account_type": "business", "org_name": "B Co",
    }).json()
    KA, TA = a["api_key"], a["token"]
    KB, TB = b["api_key"], b["token"]
    HA = {"Authorization": f"Bearer {TA}"}
    HB = {"Authorization": f"Bearer {TB}"}
    aid_a = database.resolve_session(TA)["account_id"]

    def health(h):
        r = c.get("/connect/health", headers=h)
        assert r.status_code == 200, r.text
        body = r.json()
        return {row["connector_id"]: row for row in body["connectors"]}

    def post_traces(key, svc, resource_extra=None, spans=None):
        res = {"service.name": svc}
        res.update(resource_extra or {})
        return c.post("/v1/traces", json={"resourceSpans": [{
            "resource": {"attributes": kv(res)},
            "scopeSpans": [{"spans": spans or [span("step")]}],
        }]}, headers={"X-Trovis-Api-Key": key})

    print("\n1. nothing configured or observed → not connected")
    h = health(HA)
    check("every telemetry connector is present", all(
        cid in h for cid in connect_health.TELEMETRY_CONNECTOR_IDS))
    check("every work system is present", all(cid in h for cid in ("stripe", "hubspot", "shopify")))
    check("all not_connected", all(r["state"] == "not_connected" for r in h.values()))
    check("nothing observed", not any(r["observed"] for r in h.values()))
    check("no last_observed_at anywhere", all(r["last_observed_at"] is None for r in h.values()))
    check("telemetry configured is None (not tracked), not False",
          all(h[cid]["configured"] is None for cid in connect_health.TELEMETRY_CONNECTOR_IDS))
    check("SaaS configured is False", all(h[cid]["configured"] is False for cid in ("stripe", "hubspot", "shopify")))

    print("\n4/5. attributable telemetry → observed, last_observed_at from the span")
    t0 = int(time.time() * 1e9) - 5 * 60 * 1_000_000_000  # five minutes ago
    r = post_traces(KA, "claude-worker", {"trovis.sdk.platform": "anthropic"}, [span("run", t0)])
    check("SDK-stamped traces accepted", r.status_code == 200)
    h = health(HA)
    check("claude is connected", h["claude"]["state"] == "connected" and h["claude"]["observed"])
    check("claude method is sdk", h["claude"]["connection_method"] == "sdk")
    check("claude last_observed_at is the span time",
          h["claude"]["last_observed_at"] == database._ns_to_iso(t0))
    check("one source rolled up", h["claude"]["source_count"] == 1)
    check("telemetry has no account label", h["claude"]["label"] is None)

    print("\n6. generic OTEL does not become a brand by guessing")
    r = post_traces(KA, "refund-worker")
    check("unstamped traces still accepted (backwards compatible)", r.status_code == 200)
    r = post_traces(KA, "claude-refund-helper")  # the NAME says Claude; the stamp says nothing
    check("a second unstamped service accepted", r.status_code == 200)
    h = health(HA)
    check("custom-otel is connected with both sources",
          h["custom-otel"]["state"] == "connected" and h["custom-otel"]["source_count"] == 2)
    check("claude did NOT absorb the service named claude", h["claude"]["source_count"] == 1)
    check("openai-agents / grok untouched",
          h["openai-agents"]["state"] == "not_connected" and h["grok"]["state"] == "not_connected")

    print("\n7. Grok and Grok Bot are distinct")
    post_traces(KA, "xai-agent", {"trovis.sdk.platform": "xai"})
    post_traces(KA, "Grok Bot", {"trovis.platform": "cursor-grok-bot", "trovis.connector.id": "grok-bot"})
    h = health(HA)
    check("grok connected over sdk", h["grok"]["state"] == "connected" and h["grok"]["connection_method"] == "sdk")
    check("grok-bot connected over mcp", h["grok-bot"]["state"] == "connected" and h["grok-bot"]["connection_method"] == "mcp")
    check("each has exactly one source", h["grok"]["source_count"] == 1 and h["grok-bot"]["source_count"] == 1)

    print("\n   explicit trovis.connector.id (Cursor recipe) and legacy stamps")
    post_traces(KA, "cursor-thing", {"trovis.connector.id": "cursor"})
    post_traces(KA, "gpt-thing", {"trovis.platform": "chatgpt"})  # old telemetry, no connector id
    post_traces(KA, "claw", {"openclaw.gateway.version": "2.0", "trovis.plugin.version": "1.0"})
    h = health(HA)
    check("cursor connected", h["cursor"]["state"] == "connected")
    check("legacy chatgpt stamp still resolves", h["chatgpt"]["state"] == "connected")
    check("chatgpt method not claimed (two doors share the stamp)", h["chatgpt"]["connection_method"] is None)
    check("openclaw connected over plugin", h["openclaw"]["connection_method"] == "plugin")

    print("\n10. silence is not degraded")
    old = int(time.time() * 1e9) - 40 * 24 * 3600 * 1_000_000_000
    post_traces(KA, "monthly-openai", {"trovis.sdk.platform": "openai"}, [span("monthly", old)])
    h = health(HA)
    check("a 40-day-old observation is connected, not degraded",
          h["openai-agents"]["state"] == "connected")
    check("its last_observed_at is honest about the age",
          h["openai-agents"]["last_observed_at"] == database._ns_to_iso(old))
    check("no row anywhere says degraded", not any(r["state"] == "degraded" for r in h.values()))

    print("\n2. SaaS OAuth alone → waiting_for_data")
    database.upsert_saas_connection(aid_a, "stripe", provider_account_id="acct_saasA",
                                    access_token="tok_test", status="connected")
    h = health(HA)
    s = h["stripe"]
    check("stripe configured", s["configured"] is True)
    check("stripe NOT observed from authorization alone", s["observed"] is False)
    check("stripe waiting_for_data", s["state"] == "waiting_for_data")
    check("stripe last_observed_at stays None (not the OAuth time)", s["last_observed_at"] is None)
    check("stripe label is the provider account", s["label"] == "acct_saasA")
    check("stripe method oauth", s["connection_method"] == "oauth")
    check("hubspot / shopify untouched",
          h["hubspot"]["state"] == "not_connected" and h["shopify"]["state"] == "not_connected")

    print("\n3. attributable SaaS activity → connected")
    check("event claimed", database.claim_saas_event(aid_a, "stripe", "evt_health_1", "payment_intent.succeeded"))
    h = health(HA)
    s = h["stripe"]
    check("stripe connected", s["state"] == "connected" and s["observed"] is True)
    check("stripe last_observed_at is the webhook time", isinstance(s["last_observed_at"], str) and s["last_observed_at"])
    check("a replayed event is not a new observation",
          database.claim_saas_event(aid_a, "stripe", "evt_health_1", "payment_intent.succeeded") is False)

    print("\n11. existing SaaS behaviour unchanged")
    r = c.get("/saas/connections", headers=HA).json()
    row = next((x for x in r["connections"] if x["provider"] == "stripe"), None)
    check("/saas/connections still reports the OAuth row", row and row["status"] == "connected")
    database.disconnect_saas_connection(aid_a, "stripe")
    h = health(HA)
    check("after disconnect: not_connected", h["stripe"]["state"] == "not_connected")
    check("after disconnect: configured False, label withheld",
          h["stripe"]["configured"] is False and h["stripe"]["label"] is None)
    check("after disconnect: the past observation is not un-happened", h["stripe"]["observed"] is True)

    print("\n8. account isolation")
    hb = health(HB)
    check("account B sees none of A's telemetry", all(
        hb[cid]["state"] == "not_connected" and not hb[cid]["observed"]
        for cid in connect_health.TELEMETRY_CONNECTOR_IDS))
    check("account B sees none of A's SaaS", hb["stripe"]["state"] == "not_connected"
          and hb["stripe"]["observed"] is False and hb["stripe"]["label"] is None)
    post_traces(KB, "b-only", {"trovis.sdk.platform": "openai"})
    check("B's telemetry does not leak into A's counts",
          health(HA)["openai-agents"]["source_count"] == 1)

    print("\n9. missing stays missing / auth")
    h = health(HA)
    check("unobserved rows carry no method", h["hubspot"]["connection_method"] is None)
    check("telemetry rows carry no label", all(h[cid]["label"] is None for cid in connect_health.TELEMETRY_CONNECTOR_IDS))
    r = c.get("/connect/health")
    check("no credential → rejected", r.status_code in (401, 403))
    r = c.get("/connect/health", headers={"X-Trovis-Api-Key": KA})
    check("API-key sessions can read it too", r.status_code == 200)

    # --- attribution belongs to the observation, not the service ----------
    cc = c.post("/auth/signup", json={
        "email": "health-c@t.com", "password": "supersecret123",
        "name": "Cy", "account_type": "business", "org_name": "C Co",
    }).json()
    KC, TC = cc["api_key"], cc["token"]
    HC = {"Authorization": f"Bearer {TC}"}
    now_ns = int(time.time() * 1e9)
    MIN = 60 * 1_000_000_000

    print("\nR1. same service: older Claude-stamped span, newer unstamped span")
    t_claude = now_ns - 60 * MIN
    t_plain = now_ns - 5 * MIN
    post_traces(KC, "refund-worker", {"trovis.sdk.platform": "anthropic"}, [span("run", t_claude)])
    post_traces(KC, "refund-worker", None, [span("run", t_plain)])
    h = health(HC)
    check("claude remains observed", h["claude"]["observed"] is True and h["claude"]["state"] == "connected")
    check("claude last_observed_at is the Claude-stamped observation, not the newer span",
          h["claude"]["last_observed_at"] == database._ns_to_iso(t_claude))
    check("custom-otel is also observed", h["custom-otel"]["observed"] is True)
    check("custom-otel last_observed_at is the newer generic observation",
          h["custom-otel"]["last_observed_at"] == database._ns_to_iso(t_plain))
    check("one source each", h["claude"]["source_count"] == 1 and h["custom-otel"]["source_count"] == 1)

    print("\nR2. same service: older generic span, newer Grok-stamped span")
    t_plain2 = now_ns - 50 * MIN
    t_grok = now_ns - 2 * MIN
    post_traces(KC, "pricing-agent", None, [span("run", t_plain2)])
    post_traces(KC, "pricing-agent", {"trovis.sdk.platform": "xai"}, [span("run", t_grok)])
    h = health(HC)
    check("grok observed from the newer stamped observation",
          h["grok"]["observed"] and h["grok"]["last_observed_at"] == database._ns_to_iso(t_grok))
    check("custom-otel still carries the older generic observation of pricing-agent (two sources now)",
          h["custom-otel"]["source_count"] == 2)
    check("custom-otel last_observed_at is not moved by the Grok span",
          h["custom-otel"]["last_observed_at"] == database._ns_to_iso(t_plain))
    check("grok did not absorb the generic history", h["grok"]["source_count"] == 1)

    print("\nR3. same service: Grok SDK observation and Grok Bot observation")
    t_sdk = now_ns - 30 * MIN
    t_bot = now_ns - 1 * MIN
    post_traces(KC, "grok-thing", {"trovis.sdk.platform": "xai"}, [span("run", t_sdk)])
    post_traces(KC, "grok-thing", {"trovis.platform": "cursor-grok-bot"}, [span("run", t_bot)])
    h = health(HC)
    check("grok and grok-bot both observed", h["grok"]["observed"] and h["grok-bot"]["observed"])
    check("grok-bot last_observed_at is its own observation",
          h["grok-bot"]["last_observed_at"] == database._ns_to_iso(t_bot))
    # The newest Grok SDK observation in this account is pricing-agent's
    # (R2, two minutes ago). grok-thing's Bot span is NEWER than that and
    # must not move Grok's time — that would be the Bot leaking into Grok.
    check("grok last_observed_at is the newest Grok SDK observation, not the Bot's newer span",
          h["grok"]["last_observed_at"] == database._ns_to_iso(t_grok)
          and h["grok"]["last_observed_at"] != database._ns_to_iso(t_bot))
    check("grok now has two sources, grok-bot one",
          h["grok"]["source_count"] == 2 and h["grok-bot"]["source_count"] == 1)
    check("methods stay deterministic per connector",
          h["grok"]["connection_method"] == "sdk" and h["grok-bot"]["connection_method"] == "mcp")

    print("\nR4. source_count is distinct attributable service names")
    for svc in ("claude-a", "claude-b"):
        post_traces(KC, svc, {"trovis.sdk.platform": "anthropic"})
        post_traces(KC, svc, {"trovis.sdk.platform": "claude-agent-sdk"})  # a second blob, same service
    h = health(HC)
    check("claude counts refund-worker + claude-a + claude-b = 3 distinct services, not 5 blobs",
          h["claude"]["source_count"] == 3)
    check("a service seen under two stamps counts once per connector, not twice",
          h["custom-otel"]["source_count"] == 2)

    print("\nR5. account isolation still holds under observation-level attribution")
    ha = health(HA)
    check("account A's claude unchanged by C's refund-worker", ha["claude"]["source_count"] == 1)
    hb = health(HB)
    check("account B still sees only its own source", hb["openai-agents"]["source_count"] == 1
          and hb["claude"]["state"] == "not_connected")

    print("\n12. agent-to-agent /connections is untouched")
    r = c.get("/connections", headers=HA)
    check("/connections still answers (Agent Flow)", r.status_code == 200 and isinstance(r.json(), list))

print()
if failures:
    print(f"{len(failures)} FAILED:")
    for f in failures:
        print("  -", f)
    raise SystemExit(1)
print("all connection-health checks passed")
