"""Workflow map tests: the station-position derivation (pure) and
GET /workflows/{id}/map (live positions + done-today).

v1 alignment is a greedy monotone walk: a loop is on_path when its
possession chain reads left-to-right along the declared stations
(holder_type match, holder-name match when the station names one).
Anything that can't align is off_path — list-only, never dotted.

Run:
  OVERSEE_DISABLE_PRICING_SYNC=1 python3 test_workflow_map.py
"""
import os
import tempfile
import time

os.environ["OVERSEE_DISABLE_PRICING_SYNC"] = "1"
os.environ["TROVIS_DISABLE_ALERTS"] = "1"
os.environ["TROVIS_DISABLE_LOOP_SWEEP"] = "1"
os.environ["TROVIS_LOOP_TITLES"] = "off"
os.environ.pop("DATABASE_URL", None)
os.environ.pop("ANTHROPIC_API_KEY", None)

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()

import database
database.SQLITE_PATH = _tmp.name

import loops
from loops import align_loop_to_stations

failures = []
def check(label, cond):
    print(("  PASS " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


def seg(holder_type, holder, waiting=False):
    return {"holder_type": holder_type, "holder": holder, "waiting": waiting}


ST = [
    {"holder_type": "agent", "holder": "triage-bot", "label": "scores it"},
    {"holder_type": "human", "holder": "Sarah", "label": "approves"},
    {"holder_type": "agent", "holder": "", "label": "ships"},  # unnamed agent
    {"holder_type": "system", "holder": "stripe"},
]

# --- Pure alignment ---
check("no stations -> no_stations",
      align_loop_to_stations([seg("agent", "x")], []) ==
      {"status": "no_stations", "station_index": None})
check("first station: agent composite matches by service part",
      align_loop_to_stations([seg("agent", "triage-bot:main")], ST) ==
      {"status": "on_path", "station_index": 0})
check("progression: agent -> Sarah -> agent lands on station 2",
      align_loop_to_stations(
          [seg("agent", "triage-bot:main"), seg("human", "Sarah", True),
           seg("agent", "ship-bot:main")], ST) ==
      {"status": "on_path", "station_index": 2})
check("full chain reaches the system station",
      align_loop_to_stations(
          [seg("agent", "triage-bot:main"), seg("human", "Sarah", True),
           seg("agent", "ship-bot:main"), seg("system", "Stripe", True)], ST)
      == {"status": "on_path", "station_index": 3})
check("wrong human name -> off_path",
      align_loop_to_stations(
          [seg("agent", "triage-bot:main"), seg("human", "Omar", True)], ST)["status"]
      == "off_path")
# Skipping forward is legal (a loop can enter the record mid-process);
# going BACKWARD is not: once past station 1 (Sarah), a return to her
# can't align monotonically.
check("skipping stations forward is on_path",
      align_loop_to_stations(
          [seg("human", "Sarah", True), seg("agent", "triage-bot:main")], ST)
      == {"status": "on_path", "station_index": 2})
check("backtracking (monotone violation) -> off_path",
      align_loop_to_stations(
          [seg("agent", "triage-bot:main"), seg("system", "stripe", True),
           seg("human", "Sarah", True)], ST)["status"]
      == "off_path")
check("name matching is case-insensitive",
      align_loop_to_stations([seg("agent", "Triage-Bot:main")], ST)["status"] == "on_path")

# --- Endpoint, seeded through the real ingest path ---
import main
from fastapi.testclient import TestClient
main._auto_describe = lambda *a, **k: False

def otlp_attrs(d):
    return [{"key": k, "value": {"stringValue": str(v)}} for k, v in d.items()]

_seq = [0]
def span(name, start, attrs):
    _seq[0] += 1
    return {"traceId": f"{_seq[0]:032d}", "spanId": f"{_seq[0]:016d}", "name": name,
            "kind": 1, "startTimeUnixNano": str(start),
            "endTimeUnixNano": str(start + 5_000_000),
            "status": {"code": 1}, "attributes": otlp_attrs(attrs)}

def post(client, key, service, spans):
    return client.post("/v1/traces", json={"resourceSpans": [{
        "resource": {"attributes": otlp_attrs({
            "service.name": service, "trovis.plugin.version": "1.0.0"})},
        "scopeSpans": [{"spans": spans}]}]},
        headers={"X-Trovis-Api-Key": key})

NS = 1_000_000_000
NOW = time.time_ns()

with TestClient(main.app) as c:
    r = c.post("/auth/signup", json={
        "email": "map@test.com", "password": "supersecret123",
        "name": "Map Tester", "account_type": "individual", "org_name": "Map Co"})
    assert r.status_code == 201, r.text
    key = r.json()["api_key"]
    tok = c.post("/auth/login", json={
        "email": "map@test.com", "password": "supersecret123"}).json()["token"]
    HB = {"Authorization": f"Bearer {tok}"}

    wf = c.post("/workflows", json={
        "name": "Signup triage",
        "stations": [
            {"holder_type": "agent", "holder": "triage-bot", "label": "scores it"},
            {"holder_type": "human", "holder": "Sarah", "label": "approves"},
        ],
        "match_hints": [{"field": "service_name", "op": "equals", "value": "triage-bot"}],
    }, headers=HB).json()
    wid = wf["id"]

    # Working loop at station 0.
    assert post(c, key, "triage-bot", [
        span("tool_call", NOW - 10 * NS, {"trovis.run.id": "m1", "trovis.tool.name": "exec"}),
    ]).status_code == 200
    # Waiting loop at station 1 (handed to a resolvable human named Sarah? use target_id text).
    assert post(c, key, "triage-bot", [
        span("ask", NOW - 3 * 3600 * NS, {"trovis.run.id": "m2",
             "trovis.handoff.direction": "to_human", "trovis.handoff.target_id": "Sarah"}),
    ]).status_code == 200
    # Done today (for the aggregate).
    assert post(c, key, "triage-bot", [
        span("fin", NOW - 5 * NS, {"trovis.run.id": "m3", "trovis.loop.close": "done"}),
    ]).status_code == 200

    m = c.get(f"/workflows/{wid}/map", headers=HB).json()
    by_run = {l["service_name"] + str(l["id"]): l for l in m["loops"]}
    positions = {l["id"]: l["position"] for l in m["loops"]}
    states = sorted(l["cached_state"] for l in m["loops"])
    check("map: only non-terminal loops carry dots-data",
          len(m["loops"]) == 2 and states == ["awaiting_human", "working"])
    working = next(l for l in m["loops"] if l["cached_state"] == "working")
    waiting = next(l for l in m["loops"] if l["cached_state"] == "awaiting_human")
    check("working loop sits on_path at station 0",
          working["position"] == {"status": "on_path", "station_index": 0})
    check("waiting loop sits on_path at station 1 (Sarah)",
          waiting["position"] == {"status": "on_path", "station_index": 1})
    check("done today counted", m["done_today"] == 1)
    check("stations round-trip on the map payload",
          [s["holder_type"] for s in m["stations"]] == ["agent", "human"])

    # Off-path: hand the SAME workflow's loop to an undeclared human.
    assert post(c, key, "triage-bot", [
        span("ask", NOW - 2 * NS, {"trovis.run.id": "m4",
             "trovis.handoff.direction": "to_human", "trovis.handoff.target_id": "Omar"}),
    ]).status_code == 200
    m2 = c.get(f"/workflows/{wid}/map", headers=HB).json()
    omar = next(l for l in m2["loops"] if l["id"] not in positions)
    check("undeclared holder -> off_path, list-only",
          omar["position"]["status"] == "off_path"
          and omar["position"]["station_index"] is None)

    # Hint-only workflow -> no_stations for every loop.
    wf2 = c.post("/workflows", json={
        "name": "Hint only",
        "stations": [],
        "match_hints": [{"field": "service_name", "op": "equals", "value": "hint-bot"}],
    }, headers=HB).json()
    assert post(c, key, "hint-bot", [
        span("w", NOW - 4 * NS, {"trovis.run.id": "m5"})]).status_code == 200
    m3 = c.get(f"/workflows/{wf2['id']}/map", headers=HB).json()
    check("hint-only workflow: loops report no_stations",
          m3["loops"] and all(l["position"]["status"] == "no_stations" for l in m3["loops"]))

print()
if failures:
    print(f"FAILED: {len(failures)} check(s):")
    for f in failures:
        print("  - " + f)
    raise SystemExit(1)
print("ALL CHECKS PASSED")
os.unlink(_tmp.name)
