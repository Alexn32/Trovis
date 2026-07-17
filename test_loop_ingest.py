"""Workloop ingestion tests — loop creation/joining via /v1/traces.

Covers: implicit loops from run.id, explicit external_id + title, the 30-min
gap rule, handoff attributes, agent-close via trovis.loop.close, legacy
oversee.* prefixes, participants, and the regression guarantee that plain
payloads (no loop attributes) still ingest and get an implicit loop.

Run:
  OVERSEE_DISABLE_PRICING_SYNC=1 python3 test_loop_ingest.py
(isolated temp SQLite DB; never touches the dev/prod DB)
"""
import os
import tempfile
import time

os.environ["OVERSEE_DISABLE_PRICING_SYNC"] = "1"
os.environ["TROVIS_DISABLE_ALERTS"] = "1"
os.environ["TROVIS_DISABLE_LOOP_SWEEP"] = "1"
os.environ.pop("DATABASE_URL", None)
os.environ.pop("ANTHROPIC_API_KEY", None)

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()

import database
database.SQLITE_PATH = _tmp.name

import main
from fastapi.testclient import TestClient

main._auto_describe = lambda *a, **k: False

failures = []
def check(label, cond):
    print(("  PASS " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


def otlp_attrs(d):
    return [{"key": k, "value": {"stringValue": str(v)}} for k, v in d.items()]


_seq = [0]
def span(name, start, attrs):
    _seq[0] += 1
    sid = f"{_seq[0]:016d}"
    return {
        "traceId": f"{_seq[0]:032d}", "spanId": sid, "name": name, "kind": 1,
        "startTimeUnixNano": str(start), "endTimeUnixNano": str(start + 5_000_000),
        "status": {"code": 1}, "attributes": otlp_attrs(attrs),
    }


def post(client, key, service, spans):
    payload = {"resourceSpans": [{
        "resource": {"attributes": otlp_attrs({
            "service.name": service, "trovis.plugin.version": "1.0.0",
        })},
        "scopeSpans": [{"spans": spans}],
    }]}
    return client.post("/v1/traces", json=payload, headers={"X-Trovis-Api-Key": key})


NS = 1_000_000_000
MIN = 60 * NS
NOW = time.time_ns()
T = NOW - 3 * 3600 * NS  # 3h ago: room for +60min offsets, past the clamp floor


def loops_for(account_id, service):
    return [l for l in database.get_loops(account_id, limit=200)
            if l["service_name"] == service]


with TestClient(main.app) as c:
    r = c.post("/auth/signup", json={
        "email": "loops@test.com", "password": "supersecret123",
        "name": "Loop Tester", "account_type": "individual", "org_name": "Loop Co",
    })
    assert r.status_code == 201, r.text
    key = r.json()["api_key"]
    account_id = c.get("/auth/me", headers={"Authorization": f"Bearer {c.post('/auth/login', json={'email': 'loops@test.com', 'password': 'supersecret123'}).json()['token']}"}).json()["org"]["id"]

    # --- 1. run.id creates one loop; a second batch with the same run.id joins it
    assert post(c, key, "looper", [span("step", T, {"trovis.run.id": "r1"})]).status_code == 200
    assert post(c, key, "looper", [span("step", T + 5 * MIN, {"trovis.run.id": "r1"})]).status_code == 200
    ls = loops_for(account_id, "looper")
    check("run.id: two batches, one loop", len(ls) == 1)
    check("run.id stored as external_id", ls and ls[0]["external_id"] == "r1")
    check("initiated_by is the agent", ls and ls[0]["initiated_by_type"] == "agent"
          and ls[0]["initiated_by"] == "looper:main")
    check("state is working after activity", ls and ls[0]["cached_state"] == "working")
    check("event_count = loop_opened + 2 spans", ls and ls[0]["event_count"] == 3)

    # --- 2. Explicit loop block: external_id + title
    assert post(c, key, "titled", [span("step", T, {
        "trovis.loop.external_id": "ticket-42",
        "trovis.loop.title": "Resolve billing ticket 42",
    })]).status_code == 200
    ls = loops_for(account_id, "titled")
    check("explicit loop created with title",
          len(ls) == 1 and ls[0]["title"] == "Resolve billing ticket 42"
          and ls[0]["external_id"] == "ticket-42")
    stream = database.get_loop_stream(ls[0]["id"], account_id)
    opened = [e for e in stream if e["type"] == "loop_opened"]
    check("loop_opened event, agent-attributed",
          len(opened) == 1 and opened[0]["actor_type"] == "agent"
          and opened[0]["actor"] == "titled:main")
    check("stream ordered: loop_opened before activity",
          stream and stream[0]["type"] == "loop_opened")

    # --- 3. Gap rule (keyless spans)
    assert post(c, key, "gappy", [span("a", T, {})]).status_code == 200
    assert post(c, key, "gappy", [span("b", T + 29 * MIN, {})]).status_code == 200
    check("gap rule: second span within 29 min joins the loop",
          len(loops_for(account_id, "gappy")) == 1)
    assert post(c, key, "gappy", [span("c", T + 60 * MIN, {})]).status_code == 200
    check("gap rule: span 31 min after last event opens a new loop",
          len(loops_for(account_id, "gappy")) == 2)

    # --- 4. Handoff block -> handoff_initiated + awaiting_human (same-tx recompute)
    assert post(c, key, "handy", [span("ask", T, {
        "trovis.run.id": "rh1",
        "trovis.handoff.direction": "to_human",
        "trovis.handoff.reason": "needs approval",
    })]).status_code == 200
    ls = loops_for(account_id, "handy")
    check("handoff: state is awaiting_human immediately after ingest",
          len(ls) == 1 and ls[0]["cached_state"] == "awaiting_human")
    stream = database.get_loop_stream(ls[0]["id"], account_id)
    hi = [e for e in stream if e["type"] == "handoff_initiated"]
    check("handoff_initiated event with direction + reason payload",
          len(hi) == 1 and hi[0]["payload"].get("direction") == "to_human"
          and hi[0]["payload"].get("reason") == "needs approval"
          and hi[0]["actor"] == "handy:main")

    # --- 5. Legacy oversee.* prefix groups identically
    assert post(c, key, "legacy", [span("a", T, {"oversee.loop.external_id": "L1"})]).status_code == 200
    assert post(c, key, "legacy", [span("b", T + MIN, {"trovis.loop.external_id": "L1"})]).status_code == 200
    check("oversee.loop.external_id and trovis.* group into one loop",
          len(loops_for(account_id, "legacy")) == 1)

    # --- 6. Regression: plain payload (no loop/run attrs) still ingests,
    #        gets an implicit loop, and the fleet view is unaffected.
    r = post(c, key, "plain", [
        span("work", T, {"trovis.event.type": "llm_output"}),
        span("work2", T + MIN, {}),
    ])
    check("plain payload: 200 with correct span count",
          r.status_code == 200 and r.json()["spans_received"] == 2)
    import sqlite3
    conn = sqlite3.connect(_tmp.name)
    null_loops = conn.execute(
        "SELECT COUNT(*) FROM spans WHERE service_name='plain' AND loop_id IS NULL"
    ).fetchone()[0]
    distinct = conn.execute(
        "SELECT COUNT(DISTINCT loop_id) FROM spans WHERE service_name='plain'"
    ).fetchone()[0]
    conn.close()
    check("plain payload: every span linked to one implicit loop",
          null_loops == 0 and distinct == 1)
    agents = {g["service_name"] for g in c.get("/agents", headers={"X-Trovis-Api-Key": key}).json()}
    check("fleet view unaffected (all services present, no phantom agents)",
          "plain" in agents and "looper" in agents
          and not any("system" in a for a in agents))

    # --- 7. Participants: two agent_ids in one run.id share the loop
    assert post(c, key, "multi", [
        span("plan", T, {"trovis.run.id": "rm1", "trovis.agent.id": "main"}),
        span("exec", T + MIN, {"trovis.run.id": "rm1", "trovis.agent.id": "helper"}),
    ]).status_code == 200
    ls = loops_for(account_id, "multi")
    check("multi-agent run: one loop", len(ls) == 1)
    parts = database.get_loop_participants(ls[0]["id"], account_id)
    roles = {(p["participant"], p["role"]) for p in parts}
    check("participants: initiator + both executors",
          ("multi:main", "initiator") in roles
          and ("multi:main", "executor") in roles
          and ("multi:helper", "executor") in roles)

    # --- 8. Agent close: trovis.loop.close -> done; same run.id later -> NEW loop
    assert post(c, key, "closer", [span("work", T, {"trovis.run.id": "rc1"})]).status_code == 200
    assert post(c, key, "closer", [span("finish", T + MIN, {
        "trovis.run.id": "rc1", "trovis.loop.close": "done",
    })]).status_code == 200
    ls = loops_for(account_id, "closer")
    check("agent close: loop is done with closed_at set",
          len(ls) == 1 and ls[0]["cached_state"] == "done" and ls[0]["closed_at"])
    stream = database.get_loop_stream(ls[0]["id"], account_id)
    closed = [e for e in stream if e["type"] == "loop_closed"]
    check("agent close: agent-attributed loop_closed with completed_by_agent",
          len(closed) == 1 and closed[0]["actor_type"] == "agent"
          and closed[0]["actor"] == "closer:main"
          and closed[0]["payload"].get("reason") == "completed_by_agent")
    assert post(c, key, "closer", [span("more", T + 2 * MIN, {"trovis.run.id": "rc1"})]).status_code == 200
    ls = loops_for(account_id, "closer")
    check("closed loops never accept new events: same run.id opens a NEW loop",
          len(ls) == 2 and sorted(l["cached_state"] for l in ls) == ["done", "working"])
    check("close with a reason string keeps detail",
          post(c, key, "closer", [span("fin", T + 3 * MIN, {
              "trovis.run.id": "rc1", "trovis.loop.close": "blocked on credentials",
          })]).status_code == 200)
    ls = loops_for(account_id, "closer")
    done2 = [l for l in ls if l["cached_state"] == "done"]
    detail_ok = False
    for l in done2:
        for e in database.get_loop_stream(l["id"], account_id):
            if e["type"] == "loop_closed" and e["payload"].get("detail") == "blocked on credentials":
                detail_ok = True
    check("loop_closed payload.detail carries the reason string", detail_ok)

print()
if failures:
    print(f"FAILED: {len(failures)} check(s):")
    for f in failures:
        print("  - " + f)
    raise SystemExit(1)
print("ALL CHECKS PASSED")
os.unlink(_tmp.name)
