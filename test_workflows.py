"""Versioned workflows: declarations, hint matching, version history, the
terminal-state freeze, archive, and legacy-row exclusion.

The workflows table is SHARED with the removed legacy graph feature —
legacy rows have no workflow_versions row and must be structurally
invisible to every new query and to the matching engine.

Run:
  OVERSEE_DISABLE_PRICING_SYNC=1 python3 test_workflows.py
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

import loops as loops_mod
import main
from fastapi.testclient import TestClient

main._auto_describe = lambda *a, **k: False

failures = []
def check(label, cond):
    print(("  PASS " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


# --- Pure matching-engine tests (no DB) --------------------------------------

def wf(wid, version, hints):
    return {"workflow_id": wid, "version": version, "match_hints": hints}

L = {"id": 1, "service_name": "billing-bot", "agent_id": "main",
     "title": "Reconcile July Invoices"}

check("equals hint matches",
      loops_mod.match_workflow(L, [wf(1, 1, [{"field": "service_name", "op": "equals", "value": "billing-bot"}])])
      == (1, 1, 1.0))
check("contains hint matches",
      loops_mod.match_workflow(L, [wf(1, 1, [{"field": "title", "op": "contains", "value": "invoices"}])])
      == (1, 1, 1.0))
check("prefix hint matches",
      loops_mod.match_workflow(L, [wf(1, 1, [{"field": "service_name", "op": "prefix", "value": "billing-"}])])
      == (1, 1, 1.0))
check("title matching is case-insensitive (equals too)",
      loops_mod.match_workflow(L, [wf(1, 1, [{"field": "title", "op": "equals", "value": "reconcile july invoices"}])])
      == (1, 1, 1.0))
check("non-title fields are case-sensitive",
      loops_mod.match_workflow(L, [wf(1, 1, [{"field": "service_name", "op": "equals", "value": "BILLING-BOT"}])])
      is None)
check("multi-hint AND: all must pass",
      loops_mod.match_workflow(L, [wf(1, 1, [
          {"field": "service_name", "op": "equals", "value": "billing-bot"},
          {"field": "title", "op": "contains", "value": "nope"},
      ])]) is None)
check("hintless workflows never auto-match",
      loops_mod.match_workflow(L, [wf(1, 1, [])]) is None)
check("missing field never matches",
      loops_mod.match_workflow({"id": 2, "service_name": "billing-bot", "agent_id": "main", "title": None},
                               [wf(1, 1, [{"field": "title", "op": "contains", "value": "x"}])])
      is None)
check("ambiguity: most hints wins",
      loops_mod.match_workflow(L, [
          wf(1, 1, [{"field": "service_name", "op": "equals", "value": "billing-bot"}]),
          wf(2, 1, [{"field": "service_name", "op": "equals", "value": "billing-bot"},
                    {"field": "title", "op": "contains", "value": "invoices"}]),
      ]) == (2, 1, 1.0))
check("ambiguity tie: most recently created (higher id) wins",
      loops_mod.match_workflow(L, [
          wf(1, 1, [{"field": "service_name", "op": "equals", "value": "billing-bot"}]),
          wf(9, 3, [{"field": "service_name", "op": "prefix", "value": "billing"}]),
      ]) == (9, 3, 1.0))
try:
    loops_mod.validate_match_hints([{"field": "cost", "op": "equals", "value": "x"}])
    check("validator rejects unknown field", False)
except ValueError:
    check("validator rejects unknown field", True)
try:
    loops_mod.validate_stations([{"holder_type": "robot"}])
    check("validator rejects unknown holder_type", False)
except ValueError:
    check("validator rejects unknown holder_type", True)


# --- API + ingestion + sweep --------------------------------------------------

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

def post_spans(client, key, service, spans):
    payload = {"resourceSpans": [{
        "resource": {"attributes": otlp_attrs({
            "service.name": service, "trovis.plugin.version": "1.0.0",
        })},
        "scopeSpans": [{"spans": spans}],
    }]}
    return client.post("/v1/traces", json=payload, headers={"X-Trovis-Api-Key": key})


NOW = time.time_ns()
MIN = 60 * 1_000_000_000

with TestClient(main.app) as c:
    r = c.post("/auth/signup", json={
        "email": "wf@test.com", "password": "supersecret123",
        "name": "WF Tester", "account_type": "individual", "org_name": "WF Co",
    })
    assert r.status_code == 201, r.text
    key = r.json()["api_key"]
    tok = c.post("/auth/login", json={
        "email": "wf@test.com", "password": "supersecret123",
    }).json()["token"]
    HB = {"Authorization": f"Bearer {tok}"}
    HK = {"X-Trovis-Api-Key": key}
    account_id = c.get("/auth/me", headers=HB).json()["org"]["id"]

    def loops_for(service):
        return [l for l in database.get_loops(account_id, limit=200)
                if l["service_name"] == service]

    # --- 1. Create -> v1; version -> v2; v1 untouched
    check("create requires a session user (api key -> 403)",
          c.post("/workflows", json={"name": "X"}, headers=HK).status_code == 403)
    r = c.post("/workflows", json={
        "name": "Invoice reconciliation",
        "stations": [{"holder_type": "agent", "holder": "wf-bot", "label": "reconcile"},
                     {"holder_type": "human", "label": "approve"}],
        "match_hints": [{"field": "service_name", "op": "equals", "value": "wf-bot"}],
        "note": "initial",
    }, headers=HB)
    check("create -> 201 with v1", r.status_code == 201 and r.json()["current_version"] == 1)
    wf_id = r.json()["id"]
    check("bad hints -> 400",
          c.post("/workflows", json={"name": "Y", "match_hints": [{"field": "x", "op": "equals", "value": "v"}]},
                 headers=HB).status_code == 400)

    r = c.post(f"/workflows/{wf_id}/versions", json={
        "stations": [{"holder_type": "agent", "holder": "wf-bot"}],
        "match_hints": [{"field": "service_name", "op": "equals", "value": "wf-bot"},
                        {"field": "title", "op": "contains", "value": "invoice"}],
        "note": "tightened hints",
    }, headers=HB)
    check("new version -> current_version=2", r.status_code == 200 and r.json()["current_version"] == 2)
    d = c.get(f"/workflows/{wf_id}", headers=HB).json()
    check("version history lists v2 then v1 with notes",
          [v["version"] for v in d["versions"]] == [2, 1]
          and d["versions"][1]["note"] == "initial")
    import sqlite3, json as _json
    conn = sqlite3.connect(_tmp.name)
    v1_hints = conn.execute(
        "SELECT match_hints FROM workflow_versions WHERE workflow_id=? AND version=1", (wf_id,)
    ).fetchone()[0]
    conn.close()
    check("v1 row untouched by the v2 edit (append-only)",
          _json.loads(v1_hints) == [{"field": "service_name", "op": "equals", "value": "wf-bot"}])

    # --- 3. Loop ingested matching -> cache columns with current version
    assert post_spans(c, key, "wf-bot", [span("step", NOW - 5 * MIN, {
        "trovis.run.id": "wr1", "trovis.loop.title": "Invoice batch 7",
    })]).status_code == 200
    l = loops_for("wf-bot")[0]
    check("ingested loop matched at current version with confidence",
          l["workflow_id"] == wf_id and l["workflow_version"] == 2
          and l["workflow_name"] == "Invoice reconciliation")
    d = c.get(f"/loops/{l['id']}", headers=HB).json()
    check("GET /loops/{id} carries workflow fields",
          d["workflow_id"] == wf_id and d["workflow_version"] == 2)

    # --- 5. THE FREEZE TEST: close a loop on v2, release v3, closed loop stays v2
    assert post_spans(c, key, "wf-bot", [span("fin", NOW - 4 * MIN, {
        "trovis.run.id": "wr1", "trovis.loop.close": "done",
        "trovis.loop.title": "Invoice batch 7",
    })]).status_code == 200
    closed = [x for x in loops_for("wf-bot") if x["cached_state"] == "done"]
    check("loop closed while v2 current records v2",
          len(closed) == 1 and closed[0]["workflow_version"] == 2)
    closed_id = closed[0]["id"]
    r = c.post(f"/workflows/{wf_id}/versions", json={
        "match_hints": [{"field": "service_name", "op": "prefix", "value": "wf-"}],
        "note": "v3 loosened",
    }, headers=HB)
    assert r.status_code == 200 and r.json()["current_version"] == 3
    # open loop for the same workflow, then sweep
    assert post_spans(c, key, "wf-bot", [span("go", NOW - 3 * MIN, {
        "trovis.run.id": "wr2", "trovis.loop.title": "Invoice batch 8",
    })]).status_code == 200
    loops_mod.run_sweep_for_account(account_id)
    rows = {x["id"]: x for x in loops_for("wf-bot")}
    open_row = [x for x in rows.values() if x["cached_state"] != "done"][0]
    check("open loop re-matched to v3 after the new version",
          open_row["workflow_version"] == 3)
    check("FREEZE: done loop stays on v2 forever",
          rows[closed_id]["workflow_version"] == 2)

    # --- 4. Workflow created AFTER loops exist
    assert post_spans(c, key, "late-bot", [span("a", NOW - 10 * MIN, {"trovis.run.id": "lb1"})]).status_code == 200
    assert post_spans(c, key, "late-bot", [span("b", NOW - 9 * MIN, {
        "trovis.run.id": "lb2", "trovis.loop.close": "done",
    })]).status_code == 200
    r = c.post("/workflows", json={
        "name": "Late declaration",
        "match_hints": [{"field": "service_name", "op": "equals", "value": "late-bot"}],
    }, headers=HB)
    late_id = r.json()["id"]
    check("pre-declaration loops start unmatched",
          all(x["workflow_id"] is None for x in loops_for("late-bot")))
    summary = loops_mod.run_sweep_for_account(account_id)
    lb = {x["cached_state"]: x for x in loops_for("late-bot")}
    check("sweep matches the in-flight loop to the late workflow",
          lb["working"]["workflow_id"] == late_id and summary["rematched"] >= 1)
    check("closed loop stays unmatched (frozen before the workflow existed)",
          lb["done"]["workflow_id"] is None)

    # --- 6. Archive
    assert c.post(f"/workflows/{late_id}/archive", headers=HK).status_code == 403
    r = c.post(f"/workflows/{late_id}/archive", headers=HB)
    check("archive sets archived_at", r.status_code == 200 and r.json()["archived_at"])
    assert post_spans(c, key, "late-bot", [span("c", NOW - 2 * MIN, {"trovis.run.id": "lb3"})]).status_code == 200
    lb3 = [x for x in loops_for("late-bot") if x["external_id"] == "lb3"][0]
    check("archived workflow no longer matches new loops", lb3["workflow_id"] is None)
    hist = c.get(f"/workflows/{late_id}/loops", headers=HB).json()
    check("matched history still readable via /workflows/{id}/loops",
          len(hist) == 1 and hist[0]["workflow_id"] == late_id)
    names = [w["name"] for w in c.get("/workflows", headers=HB).json()]
    check("archived workflow excluded from the default list", "Late declaration" not in names)
    names_all = [w["name"] for w in c.get("/workflows?include_archived=1", headers=HB).json()]
    check("include_archived=1 shows it", "Late declaration" in names_all)
    check("versioning an archived workflow -> 400",
          c.post(f"/workflows/{late_id}/versions", json={"note": "x"}, headers=HB).status_code == 400)

    # --- 7. Ambiguity through the API: most hints wins
    c.post("/workflows", json={
        "name": "Broad", "match_hints": [{"field": "service_name", "op": "prefix", "value": "amb-"}],
    }, headers=HB)
    r2 = c.post("/workflows", json={
        "name": "Specific", "match_hints": [
            {"field": "service_name", "op": "prefix", "value": "amb-"},
            {"field": "title", "op": "contains", "value": "deploy"},
        ],
    }, headers=HB)
    assert post_spans(c, key, "amb-bot", [span("d", NOW - MIN, {
        "trovis.run.id": "am1", "trovis.loop.title": "Deploy the thing",
    })]).status_code == 200
    amb = loops_for("amb-bot")[0]
    check("ambiguous match resolves to the most-specific workflow",
          amb["workflow_id"] == r2.json()["id"])

    # --- Legacy graph rows are invisible (merged-table reality)
    with database._connect() as conn2, database._cursor(conn2) as cur:
        cur.execute(
            f"INSERT INTO workflows (account_id, name, agent_service_name) "
            f"VALUES ({database.PH}, {database.PH}, {database.PH})",
            (account_id, "LEGACY GRAPH ROW", "wf-bot"),
        )
        legacy_id = cur.lastrowid
    check("legacy row (no versions) excluded from GET /workflows",
          "LEGACY GRAPH ROW" not in
          [w["name"] for w in c.get("/workflows?include_archived=1", headers=HB).json()])
    check("legacy row 404s on detail",
          c.get(f"/workflows/{legacy_id}", headers=HB).status_code == 404)
    check("legacy row invisible to the matching engine",
          all(h["workflow_id"] != legacy_id
              for h in database.get_current_workflow_hints(account_id)))
    # workflow_name join safety: no loop can reference a legacy row.
    check("no loop resolves workflow_name to a legacy row",
          all(x["workflow_name"] != "LEGACY GRAPH ROW"
              for x in database.get_loops(account_id, limit=200)))

    # --- List aggregates
    w = {x["name"]: x for x in c.get("/workflows", headers=HB).json()}["Invoice reconciliation"]
    check("list aggregates: loop counts by state + loops today",
          w["loop_counts"].get("done") == 1 and w["loop_counts"].get("working") == 1
          and w["loops_today"] == 2)

print()
if failures:
    print(f"FAILED: {len(failures)} check(s):")
    for f in failures:
        print("  - " + f)
    raise SystemExit(1)
print("ALL CHECKS PASSED")
os.unlink(_tmp.name)
