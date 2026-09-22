"""Connection instances — the durable record that a connection was set up.

Store recorded ACTIONS, derive state. A telemetry connector used to have no
fact but "a span showed up"; now setup started / completed / disconnected
are rows in connection_instances, each with a key the wire stamps as
`trovis.connection.id`, and connection health reads them:

  started + nothing observed     → setup_started
  completed + nothing observed   → waiting_for_data (connector-level too)
  a span carries the key         → connected  (connector-level as before)
  disconnected                   → disconnected; a past observation stays true

The key is not a credential: a foreign account's key attributes nothing.
Unstamped telemetry attributes at connector level, exactly as before.

Run:
  TROVIS_DISABLE_PRICING_SYNC=1 python3 test_connect_connections.py
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
import connectors
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


def span(name, start_ns=None):
    start_ns = start_ns or int(time.time() * 1e9)
    return {
        "traceId": uuid.uuid4().hex, "spanId": uuid.uuid4().hex[:16], "name": name, "kind": 1,
        "startTimeUnixNano": str(start_ns), "endTimeUnixNano": str(start_ns + 50_000_000),
        "attributes": [], "status": {"code": 1},
    }


with TestClient(main.app) as c:
    a = c.post("/auth/signup", json={
        "email": "ci-a@t.com", "password": "supersecret123",
        "name": "Ada", "account_type": "business", "org_name": "A Co",
    }).json()
    b = c.post("/auth/signup", json={
        "email": "ci-b@t.com", "password": "supersecret123",
        "name": "Bob", "account_type": "business", "org_name": "B Co",
    }).json()
    KA, HA = a["api_key"], {"Authorization": f"Bearer {a['token']}"}
    KB, HB = b["api_key"], {"Authorization": f"Bearer {b['token']}"}
    aid_a = database.resolve_session(a["token"])["account_id"]

    def health(h):
        r = c.get("/connect/health", headers=h)
        assert r.status_code == 200, r.text
        return {row["connector_id"]: row for row in r.json()["connectors"]}

    def post_traces(key, svc, resource_extra=None):
        res = {"service.name": svc}
        res.update(resource_extra or {})
        return c.post("/v1/traces", json={"resourceSpans": [{
            "resource": {"attributes": kv(res)},
            "scopeSpans": [{"spans": [span("step")]}],
        }]}, headers={"X-Trovis-Api-Key": key})

    print("1. before anything: every row carries instances: [] and configured stays None")
    h = health(HA)
    check("instances present and empty on every row", all(r["instances"] == [] for r in h.values()))
    check("telemetry configured is None — nothing recorded, not False",
          all(h[cid]["configured"] is None for cid in connect_health.TELEMETRY_CONNECTOR_IDS))
    check("instance state enum is separate from the connector enum",
          set(connect_health.INSTANCE_STATES) == {"setup_started", "waiting_for_data", "connected", "disconnected"}
          and connect_health.STATES == ("not_connected", "waiting_for_data", "connected"))

    print("\n2. creating an instance records setup_started; the connector does not become connected")
    check("unauthenticated is 401",
          c.post("/connect/connections", json={"connector_id": "claude"}).status_code == 401)
    check("unknown connector is 400",
          c.post("/connect/connections", json={"connector_id": "zendesk"}, headers=HA).status_code == 400)
    check("coming-soon connector is 400",
          c.post("/connect/connections", json={"connector_id": "slack"}, headers=HA).status_code == 400)
    check("an OAuth connector is set up by authorizing, not by hand — 400",
          c.post("/connect/connections", json={"connector_id": "stripe"}, headers=HA).status_code == 400)
    r = c.post("/connect/connections", json={
        "connector_id": "claude", "label": "Refund helper", "setup_source": "guide",
    }, headers=HA)
    check("created → 201", r.status_code == 201)
    inst = r.json()
    check("the instance is setup_started with a cn_ key and its stamp",
          inst["state"] == "setup_started" and inst["setup_status"] == "started"
          and inst["connection_key"].startswith("cn_")
          and inst["stamp"] == {"trovis.connection.id": inst["connection_key"]}
          and inst["setup_type"] == "sdk" and inst["setup_source"] == "guide"
          and inst["label"] == "Refund helper" and inst["setup_started_at"])
    h = health(HA)
    check("the connector row lists it and is configured=True, still not_connected",
          [i["id"] for i in h["claude"]["instances"]] == [inst["id"]]
          and h["claude"]["configured"] is True and h["claude"]["state"] == "not_connected"
          and h["claude"]["observed"] is False)

    print("\n3. completing records the person's word; the connector reads waiting_for_data — a first for telemetry")
    r = c.post(f"/connect/connections/{inst['id']}/complete", headers=HA)
    check("complete → 200 waiting_for_data",
          r.status_code == 200 and r.json()["state"] == "waiting_for_data"
          and r.json()["setup_status"] == "completed" and r.json()["setup_completed_at"])
    h = health(HA)
    check("connector-level state is waiting_for_data while nothing is observed",
          h["claude"]["state"] == "waiting_for_data" and h["claude"]["observed"] is False)
    check("HEALTH_STATES enum unchanged — waiting_for_data is one of the three",
          h["claude"]["state"] in connect_health.STATES)

    print("\n4. unstamped telemetry attributes at connector level only — the instance is untouched")
    post_traces(KA, "refund-helper", {"trovis.sdk.platform": "anthropic"})
    h = health(HA)
    check("connector connected from the SDK stamp", h["claude"]["state"] == "connected" and h["claude"]["observed"])
    i = h["claude"]["instances"][0]
    check("the instance still waits — no span carried its key",
          i["state"] == "waiting_for_data" and i["last_observed_at"] is None and i["source_count"] == 0)

    print("\n5. a span stamped with the key attributes to the instance")
    t0 = int(time.time() * 1e9)
    c.post("/v1/traces", json={"resourceSpans": [{
        "resource": {"attributes": kv({
            "service.name": "refund-helper", "trovis.sdk.platform": "anthropic",
            "trovis.connection.id": inst["connection_key"],
        })},
        "scopeSpans": [{"spans": [span("step", t0)]}],
    }]}, headers={"X-Trovis-Api-Key": KA})
    h = health(HA)
    i = h["claude"]["instances"][0]
    check("instance connected, with its own last_observed_at and source",
          i["state"] == "connected" and i["last_observed_at"] == database._ns_to_iso(t0)
          and i["source_count"] == 1 and i["observed_connector_ids"] == [])
    r = c.get("/connect/connections?connector_id=claude", headers=HA)
    check("GET /connect/connections lists it with the derived state",
          r.status_code == 200 and [x["id"] for x in r.json()["connections"]] == [inst["id"]]
          and r.json()["connections"][0]["state"] == "connected")
    check("and filters by connector", c.get("/connect/connections?connector_id=grok", headers=HA).json()["connections"] == [])

    print("\n6. a mismatched stamp: the instance wins, the mismatch is surfaced, the connector row still counts the stamped connector")
    post_traces(KA, "mystery-agent", {"trovis.sdk.platform": "openai", "trovis.connection.id": inst["connection_key"]})
    h = health(HA)
    i = h["claude"]["instances"][0]
    check("instance now lists openai-agents among observed connectors and two sources",
          i["observed_connector_ids"] == ["openai-agents"] and i["source_count"] == 2)
    check("openai-agents connector row is connected from that same span",
          h["openai-agents"]["state"] == "connected" and h["openai-agents"]["instances"] == [])

    print("\n7. a foreign account's key attributes nothing")
    post_traces(KB, "impostor", {"trovis.sdk.platform": "anthropic", "trovis.connection.id": inst["connection_key"]})
    hb = health(HB)
    check("account B: claude connected at connector level (its own span), no instances",
          hb["claude"]["state"] == "connected" and hb["claude"]["instances"] == [] and hb["claude"]["configured"] is None)
    h = health(HA)
    check("account A's instance did not gain B's source",
          h["claude"]["instances"][0]["source_count"] == 2)
    check("cross-account instance ids are 404, never 403",
          c.get("/connect/connections", headers=HB).json()["connections"] == []
          and c.post(f"/connect/connections/{inst['id']}/complete", headers=HB).status_code == 404
          and c.post(f"/connect/connections/{inst['id']}/disconnect", headers=HB).status_code == 404)

    print("\n8. disconnect is a record; the past observation stays true")
    r = c.post(f"/connect/connections/{inst['id']}/disconnect", headers=HA)
    check("disconnect → 200 disconnected with a timestamp",
          r.status_code == 200 and r.json()["state"] == "disconnected" and r.json()["disconnected_at"])
    h = health(HA)
    check("connector configured=False once only disconnected rows remain; observed stays True",
          h["claude"]["configured"] is False and h["claude"]["observed"] is True
          # the newer mismatched span (step 6) moved it past t0; it is not un-happened
          and h["claude"]["instances"][0]["last_observed_at"] >= database._ns_to_iso(t0))
    check("completing a disconnected instance is 409",
          c.post(f"/connect/connections/{inst['id']}/complete", headers=HA).status_code == 409)
    r2 = c.post("/connect/connections", json={"connector_id": "claude", "setup_source": "manual"}, headers=HA).json()
    h = health(HA)
    check("a second instance of the same connector: two rows, newest first, configured=True again",
          [i["id"] for i in h["claude"]["instances"]] == [r2["id"], inst["id"]] and h["claude"]["configured"] is True)

    print("\n9. an OAuth door records its own instance when the provider authorizes")
    database.upsert_saas_connection(aid_a, "stripe", provider_account_id="acct_ci", access_token="t")
    h = health(HA)
    si = h["stripe"]["instances"]
    check("one oauth instance, completed, waiting_for_data, labelled with the account, pointing at the credential row",
          len(si) == 1 and si[0]["setup_type"] == "oauth" and si[0]["setup_status"] == "completed"
          and si[0]["state"] == "waiting_for_data" and si[0]["label"] == "acct_ci"
          and si[0]["saas_connection_id"] is not None)
    check("re-authorizing keeps one instance", (database.upsert_saas_connection(
        aid_a, "stripe", provider_account_id="acct_ci", access_token="t2"), len(health(HA)["stripe"]["instances"]))[1] == 1)
    database.claim_saas_event(aid_a, "stripe", "evt_ci_1", "payment_intent.succeeded")
    h = health(HA)
    check("a verified event makes the oauth instance connected",
          h["stripe"]["instances"][0]["state"] == "connected" and h["stripe"]["state"] == "connected")
    r = c.post(f"/connect/connections/{h['stripe']['instances'][0]['id']}/disconnect", headers=HA)
    check("disconnecting the oauth instance revokes the provider connection too",
          r.status_code == 200 and r.json()["state"] == "disconnected"
          and database.get_saas_connection(aid_a, "stripe")["status"] == "disconnected"
          and health(HA)["stripe"]["state"] == "not_connected")
    check("and DELETE /saas/stripe-style disconnect marks the instance too (no second live row)",
          all(i["setup_status"] == "disconnected" for i in health(HA)["stripe"]["instances"]))

    print("\n10. the registry validates the connector id and the key never attributes without its account")
    check("get_connection_instance_by_key needs the owning account",
          database.get_connection_instance_by_key(aid_a, r2["connection_key"]) is not None
          and database.get_connection_instance_by_key(aid_a + 999, r2["connection_key"]) is None
          and database.get_connection_instance_by_key(aid_a, "cn_nope") is None)
    check("every telemetry connector can be created; every oauth one cannot",
          all(c.post("/connect/connections", json={"connector_id": cid}, headers=HA).status_code == 201
              for cid in connectors.telemetry_ids())
          and all(c.post("/connect/connections", json={"connector_id": cid}, headers=HA).status_code == 400
                  for cid in connectors.saas_ids()))

os.unlink(_tmp.name)
if failures:
    print(f"\n{len(failures)} check(s) failed:")
    for f in failures:
        print("  -", f)
    raise SystemExit(1)
print("\nall connection-instance checks passed")
