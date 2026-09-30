"""Connector-aware verification — what arrived for THIS setup since it began.

Replaces the guided setup's old success heuristic ("a new service_name
appeared in /agents"), which counted any unrelated agent, missed an existing
service exporting again, could not say what kind of data came, and lived only
in client state. The read is scoped to the connector (and the instance, when
one exists), bounded by `since`, and names what the spans carried.

Run:
  TROVIS_DISABLE_PRICING_SYNC=1 python3 test_connect_status.py
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


def span(name, start_ns, attrs=None):
    return {
        "traceId": uuid.uuid4().hex, "spanId": uuid.uuid4().hex[:16], "name": name, "kind": 1,
        "startTimeUnixNano": str(start_ns), "endTimeUnixNano": str(start_ns + 50_000_000),
        "attributes": kv(attrs or {}), "status": {"code": 1},
    }


with TestClient(main.app) as c:
    a = c.post("/auth/signup", json={
        "email": "st-a@t.com", "password": "supersecret123",
        "name": "Ada", "account_type": "business", "org_name": "A Co",
    }).json()
    KA, HA = a["api_key"], {"Authorization": f"Bearer {a['token']}"}

    def post(svc, spans, resource_extra=None):
        res = {"service.name": svc}
        res.update(resource_extra or {})
        r = c.post("/v1/traces", json={"resourceSpans": [{
            "resource": {"attributes": kv(res)},
            "scopeSpans": [{"spans": spans}],
        }]}, headers={"X-Trovis-Api-Key": KA})
        assert r.status_code == 200, r.text

    now = int(time.time() * 1e9)
    old = now - 3_600 * 1_000_000_000  # an hour before setup began

    print("1. before setup: an old span for the same connector is not this setup's data")
    post("old-claude", [span("step", old)], {"trovis.sdk.platform": "anthropic"})
    inst = c.post("/connect/connections", json={"connector_id": "claude", "setup_source": "guide"}, headers=HA).json()
    r = c.get(f"/connect/connections/{inst['id']}/status", headers=HA)
    check("status → 200", r.status_code == 200)
    st = r.json()
    check("nothing since setup began: state setup_started, no attribution, nothing seen",
          st["state"] == "setup_started" and st["attribution"] is None and st["services"] == []
          and st["span_count"] == 0 and not any(st["sees"].values()) and st["since"] == inst["setup_started_at"])

    print("\n2. connector-level traffic without the key: attribution=connector, instance still waits")
    t1 = int(time.time() * 1e9)
    post("refund-helper", [
        span("step", t1),
        span("tool_call", t1 + 1, {"trovis.tool.name": "stripe.refunds.create"}),
    ], {"trovis.sdk.platform": "anthropic"})
    st = c.get(f"/connect/connections/{inst['id']}/status", headers=HA).json()
    check("attribution is connector; the instance itself is not connected",
          st["attribution"] == "connector" and st["state"] == "setup_started")
    check("services and counts name what arrived (the old span is excluded by since)",
          [s["service_name"] for s in st["services"]] == ["refund-helper"] and st["span_count"] == 2)
    check("sees: execution and tool activity, no model usage, no named work",
          st["sees"] == {"execution": True, "actions": True, "model_usage": False, "named_work": False})

    print("\n3. a span carrying the key: attribution=instance, the instance is connected")
    t2 = int(time.time() * 1e9)
    post("refund-helper", [
        span("llm", t2, {"gen_ai.usage.input_tokens": 12, "gen_ai.usage.output_tokens": 3,
                         "gen_ai.usage.total_tokens": 15, "gen_ai.request.model": "claude-sonnet-4-5"}),
        span("step", t2 + 1, {"trovis.loop.title": "Approve refund for order #4821"}),
    ], {"trovis.sdk.platform": "anthropic", "trovis.connection.id": inst["connection_key"]})
    st = c.get(f"/connect/connections/{inst['id']}/status", headers=HA).json()
    check("instance connected with attribution=instance",
          st["state"] == "connected" and st["attribution"] == "instance")
    check("only the keyed spans count once the key is seen",
          st["span_count"] == 2 and st["last_observed_at"] == database._ns_to_iso(t2 + 1))
    check("sees: execution, model usage, named work — no tool activity in the keyed spans",
          st["sees"] == {"execution": True, "actions": False, "model_usage": True, "named_work": True})

    print("\n4. `since` narrows the read; a bad since is 400")
    st_late = c.get(f"/connect/connections/{inst['id']}/status", params={"since": str(t2 + 5)}, headers=HA).json()
    check("since after everything → nothing, state falls back to the recorded setup fact",
          st_late["span_count"] == 0 and st_late["attribution"] is None)
    st_iso = c.get(f"/connect/connections/{inst['id']}/status",
                   params={"since": database._ns_to_iso(t1)}, headers=HA).json()
    check("ISO since works too", st_iso["span_count"] == 2)
    check("garbage since → 400",
          c.get(f"/connect/connections/{inst['id']}/status", params={"since": "yesterday"}, headers=HA).status_code == 400)

    print("\n5. the connector-level read for a setup with no instance yet (manual wizard before a row)")
    r = c.get("/connect/health/claude", params={"since": str(t1)}, headers=HA)
    check("→ 200 connected with everything for the connector since then",
          r.status_code == 200 and r.json()["state"] == "connected"
          and r.json()["connection_id"] is None and r.json()["span_count"] == 4)
    check("since is required here", c.get("/connect/health/claude", headers=HA).status_code == 422
          or c.get("/connect/health/claude", headers=HA).status_code == 400)
    check("an OAuth connector is not a telemetry status", c.get("/connect/health/stripe", params={"since": "1"}, headers=HA).status_code == 404)
    check("unknown connector → 404", c.get("/connect/health/zendesk", params={"since": "1"}, headers=HA).status_code == 404)
    check("unauthenticated → 401", c.get("/connect/health/claude", params={"since": "1"}).status_code == 401)

    print("\n6. an OAuth instance reads the provider's events, not spans")
    aid = database.resolve_session(a["token"])["account_id"]
    database.upsert_saas_connection(aid, "stripe", provider_account_id="acct_st", access_token="t")
    sid = [i for i in c.get("/connect/connections?connector_id=stripe", headers=HA).json()["connections"]][0]["id"]
    st = c.get(f"/connect/connections/{sid}/status", headers=HA).json()
    check("authorized, no events → waiting_for_data", st["state"] == "waiting_for_data" and st["attribution"] is None)
    database.claim_saas_event(aid, "stripe", "evt_st_1", "payment_intent.succeeded")
    st = c.get(f"/connect/connections/{sid}/status", headers=HA).json()
    check("a verified event → connected, counted", st["state"] == "connected" and st["span_count"] == 1)

    print("\n7. the read is bounded")
    obs = database.get_observations_since(aid, 0, limit=1)
    check("limit caps the groups and says so", len(obs["groups"]) == 1 and obs["bounded"] is True)
    check("the enum of things the read can see is fixed",
          connect_health.STATUS_SEES == ("execution", "actions", "model_usage", "named_work"))

os.unlink(_tmp.name)
if failures:
    print(f"\n{len(failures)} check(s) failed:")
    for f in failures:
        print("  -", f)
    raise SystemExit(1)
print("\nall connection-status checks passed")
