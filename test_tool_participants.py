"""Tools as cast members + to_system waits, end to end.

Tool-call spans auto-upsert 'tool' participants (identifier = the tool name
as spans carry it, lowercased, MCP prefixes verbatim); LLM-call spans never
do. to_system handoffs flow through ingestion -> awaiting_system ->
/loops/stalled, and a second run proves the constraint MIGRATION path: a
database created under the OLD CHECKs (participant_type agent|human,
cached_state without awaiting_system) must accept both new values after
init_db — that's the test protecting the prod deploy.

Run:
  OVERSEE_DISABLE_PRICING_SYNC=1 python3 test_tool_participants.py
"""
import os
import sqlite3
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

# --- Simulate a prod-era database: the two tables as the PREVIOUS deploy
# created them, old CHECKs and all, with one row each so the rebuild has
# data to preserve (including a post-launch workflow_id column).
conn = sqlite3.connect(_tmp.name)
conn.executescript("""
CREATE TABLE loops (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id        INTEGER,
    external_id       TEXT,
    service_name      TEXT    NOT NULL,
    agent_id          TEXT    NOT NULL DEFAULT 'main',
    title             TEXT,
    initiated_by_type TEXT    NOT NULL DEFAULT 'agent'
                      CHECK (initiated_by_type IN ('agent', 'human')),
    initiated_by      TEXT    NOT NULL DEFAULT '',
    cached_state      TEXT    NOT NULL DEFAULT 'open'
                      CHECK (cached_state IN ('open', 'working', 'awaiting_human',
                             'awaiting_agent', 'stalled', 'done', 'abandoned')),
    last_event_unix   INTEGER,
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    closed_at         TIMESTAMP
);
CREATE TABLE loop_participants (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    loop_id          INTEGER NOT NULL REFERENCES loops(id),
    participant_type TEXT    NOT NULL CHECK (participant_type IN ('agent', 'human')),
    participant      TEXT    NOT NULL,
    role             TEXT    NOT NULL DEFAULT 'executor'
                     CHECK (role IN ('initiator', 'executor', 'reviewer')),
    added_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (loop_id, participant_type, participant, role)
);
ALTER TABLE loops ADD COLUMN workflow_id INTEGER DEFAULT NULL;
INSERT INTO loops (service_name, cached_state, last_event_unix, workflow_id)
    VALUES ('legacy-svc', 'working', 1, 42);
INSERT INTO loop_participants (loop_id, participant_type, participant, role)
    VALUES (1, 'agent', 'legacy-svc:main', 'initiator');
""")
conn.commit()
conn.close()

import database
database.SQLITE_PATH = _tmp.name

import loops
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
    return {
        "traceId": f"{_seq[0]:032d}", "spanId": f"{_seq[0]:016d}", "name": name,
        "kind": 1, "startTimeUnixNano": str(start),
        "endTimeUnixNano": str(start + 5_000_000),
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
NOW = time.time_ns()

with TestClient(main.app) as c:  # lifespan runs init_db -> the rebuild
    # --- 6. Migration on an existing old-CHECK database ---
    conn = sqlite3.connect(_tmp.name)
    ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='loop_participants'"
    ).fetchone()[0]
    check("migration: participant_type CHECK gone after startup",
          "participant_type IN" not in ddl)
    ddl2 = conn.execute("SELECT sql FROM sqlite_master WHERE name='loops'").fetchone()[0]
    check("migration: cached_state CHECK gone after startup",
          "cached_state IN" not in ddl2)
    row = conn.execute(
        "SELECT service_name, cached_state, workflow_id FROM loops WHERE id=1"
    ).fetchone()
    check("migration: pre-existing row survives, post-launch column intact",
          row == ("legacy-svc", "working", 42))
    part = conn.execute(
        "SELECT participant_type, participant FROM loop_participants WHERE loop_id=1"
    ).fetchone()
    check("migration: pre-existing participant survives", part == ("agent", "legacy-svc:main"))
    conn.execute("INSERT INTO loop_participants (loop_id, participant_type, participant, role) "
                 "VALUES (1, 'tool', 'exec', 'executor')")
    conn.execute("UPDATE loops SET cached_state='awaiting_system' WHERE id=1")
    conn.commit()
    check("migration: rebuilt tables accept 'tool' and 'awaiting_system'", True)
    conn.execute("DELETE FROM loop_participants WHERE participant='exec'")
    conn.execute("UPDATE loops SET cached_state='working' WHERE id=1")
    conn.commit()
    conn.close()
    # Second startup must be a no-op (idempotence).
    with database._connect() as conn2, database._cursor(conn2) as cur:
        database._sqlite_rebuild_if_check(cur, "loops", "cached_state IN", database._LOOPS_DDL_SQLITE)
    check("migration: rebuild is idempotent (marker gone -> no-op)", True)

    r = c.post("/auth/signup", json={
        "email": "cast@test.com", "password": "supersecret123",
        "name": "Cast Tester", "account_type": "individual", "org_name": "Cast Co",
    })
    assert r.status_code == 201, r.text
    key = r.json()["api_key"]
    tok = c.post("/auth/login", json={
        "email": "cast@test.com", "password": "supersecret123",
    }).json()["token"]
    HB = {"Authorization": f"Bearer {tok}"}
    account_id = c.get("/auth/me", headers=HB).json()["org"]["id"]

    def the_loop(service):
        return [l for l in database.get_loops(account_id, limit=50)
                if l["service_name"] == service][0]

    # --- 1 + 7. Tool cast from ingestion ---
    assert post(c, key, "cast-bot", [
        span("tool_call", NOW - 9 * NS, {"trovis.run.id": "c1", "trovis.tool.name": "Exec"}),
        span("tool_call", NOW - 8 * NS, {"trovis.run.id": "c1", "trovis.tool.name": "exec"}),
        span("model_call", NOW - 7 * NS, {"trovis.run.id": "c1"}),
        span("llm_output", NOW - 6 * NS, {"trovis.run.id": "c1"}),
        span("tool_call", NOW - 5 * NS, {"trovis.run.id": "c1", "trovis.tool.name": "web_search"}),
        span("tool_call", NOW - 4 * NS, {"trovis.run.id": "c1",
             "trovis.tool.name": "mcp__shopify__lookup_orders_by_email"}),
        span("tool_call", NOW - 3 * NS, {"trovis.run.id": "c1"}),  # unnamed: no cast row
    ]).status_code == 200
    lid = the_loop("cast-bot")["id"]
    detail = c.get(f"/loops/{lid}", headers=HB).json()
    tools = sorted(p["participant"] for p in detail["participants"]
                   if p["participant_type"] == "tool")
    check("cast completeness: all three tools listed via GET /loops/{id}",
          tools == ["exec", "mcp__shopify__lookup_orders_by_email", "web_search"])
    check("same tool twice (case-insensitive) -> one row; LLM spans -> none",
          tools.count("exec") == 1
          and not any("model" in t or "llm" in t for t in tools))
    check("tool participants ride role=executor",
          all(p["role"] == "executor" for p in detail["participants"]
              if p["participant_type"] == "tool"))

    # --- 2 + 3. to_system end to end ---
    assert post(c, key, "block-bot", [span("export", NOW - 5 * 3600 * NS, {
        "trovis.run.id": "c2", "trovis.handoff.direction": "to_system",
        "trovis.handoff.target_id": "stripe", "trovis.handoff.reason": "slow export",
    })]).status_code == 200
    row = the_loop("block-bot")
    check("to_system -> stalled past threshold at ingest (5h > 4h)",
          row["cached_state"] == "stalled")
    assert post(c, key, "wait-bot", [span("export", NOW - 60 * NS, {
        "trovis.run.id": "c3", "trovis.handoff.direction": "to_system",
        "trovis.handoff.target_id": "hubspot",
    })]).status_code == 200
    row3 = the_loop("wait-bot")
    check("to_system -> awaiting_system immediately (same-tx recompute)",
          row3["cached_state"] == "awaiting_system")
    stalled = c.get("/loops/stalled", headers=HB).json()
    names = {l["service_name"]: l for l in stalled}
    check("/loops/stalled surfaces awaiting_system with age",
          "wait-bot" in names and names["wait-bot"]["stalled_for_s"] is not None
          and "block-bot" in names)
    seg = c.get(f"/loops/{row3['id']}", headers=HB).json()["segments"][-1]
    check("story segments: system holds the wait, named",
          seg["holder_type"] == "system" and seg["holder"] == "hubspot"
          and seg["waiting"] is True)
    check("list rows carry the system wait in segments_mini",
          row3["segments_mini"][-1]["holder_type"] == "system"
          and row3["segments_mini"][-1]["waiting"] is True)

    # resolution -> working
    with database._connect() as conn3, database._cursor(conn3) as cur:
        database.append_loop_event(cur, row3["id"], "handoff_completed", "system",
                                   "hubspot", account_id=account_id)
        database.recompute_loop_state(cur, row3["id"])
    check("handoff_completed resolves awaiting_system -> working",
          the_loop("wait-bot")["cached_state"] == "working")

    # --- vocabulary still enforced (in code now) ---
    try:
        with database._connect() as conn4, database._cursor(conn4) as cur:
            database._upsert_loop_participant(cur, lid, "gadget", "x", "executor")
        check("unknown participant_type raises ValueError", False)
    except ValueError:
        check("unknown participant_type raises ValueError", True)

print()
if failures:
    print(f"FAILED: {len(failures)} check(s):")
    for f in failures:
        print("  - " + f)
    raise SystemExit(1)
print("ALL CHECKS PASSED")
os.unlink(_tmp.name)
