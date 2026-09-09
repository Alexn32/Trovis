"""P0 survival: GET /agents and GET /work/overview on a large-tenant fixture.

Hammocks-shaped: ~6 agent groups, tens of thousands of untitled loops, one
agent with thousands of spans. First paint must not correlated-fold every
span or seq-scan every loop for named-only counts.

Also documents the query plan:
  - idx_loops_account_title_closed (account_id, title_source, closed_at)
  - agents_list_agg_sql has no per-row correlated subqueries
"""
import inspect
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

import main
from fastapi.testclient import TestClient

main._auto_describe = lambda *a, **k: False

failures = []


def check(label, cond):
    print(("  PASS " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


def _span(svc, agent_id, i, now, account_id):
    return {
        "trace_id": uuid.uuid4().hex,
        "span_id": uuid.uuid4().hex[:16],
        "parent_span_id": None,
        "service_name": svc,
        "span_name": "op",
        "kind": 0,
        "start_time_unix": now - i * 1_000_000,
        "end_time_unix": now - i * 1_000_000 + 1000,
        "status_code": 0,
        "status_message": "",
        "attributes": {"trovis.agent.id": agent_id},
        "resource_attributes": {"service.name": svc, "telemetry.sdk.language": "python"},
    }


def _explain(sql, args=()):
    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute("EXPLAIN QUERY PLAN " + sql, tuple(args))
        return " ".join(str(dict(r)) for r in cur.fetchall()).lower()


database.init_db()

print("\n--- lean GET /agents SQL (no per-row fold) ---")
sql, _args = database.agents_list_agg_sql(1, 1, 1)
check("agg SQL has no descriptions correlated subquery",
      "from descriptions" not in sql.lower())
check("agg SQL has no agent_registrations correlated subquery",
      "agent_registrations" not in sql.lower())
check("agg SQL has no display_names correlated subquery",
      "agent_display_names" not in sql.lower())
check("agg SQL has no owners correlated subquery",
      "agent_owners" not in sql.lower())
check("agg SQL has no sample_resource self-join",
      "s2" not in sql.lower() and "resource_attributes" not in sql.lower())
check("agg SQL still GROUPs by service + agent (cheap aggregate)",
      "group by service_name, agent_id" in sql.lower())

print("\n--- indexes exist ---")
with database._connect() as conn, database._cursor(conn) as cur:
    cur.execute("SELECT name FROM sqlite_master WHERE type='index'")
    idx = {r["name"] for r in cur.fetchall()}
check("idx_loops_account_title_closed exists",
      "idx_loops_account_title_closed" in idx)
check("idx_spans_account_service_agent exists",
      "idx_spans_account_service_agent" in idx)

print("\n--- large untitled-loop flood (overview) ---")
acct = database.create_account("hammocks@t.com", account_type="business", name="Hammocks")
aid = acct["id"]
now_ns = time.time_ns()

N_UNTITLED = 8000
N_NAMED_OPEN = 12
named_titles = [f"Named item {i}" for i in range(N_NAMED_OPEN)]

with database._connect() as conn, database._cursor(conn) as cur:
    untitled = [
        (aid, f"raw-{i}", "flood-agent", "main", None, None, "open", now_ns - i)
        for i in range(N_UNTITLED)
    ]
    named = [
        (aid, f"named-{i}", "cs-agent", "main", named_titles[i], "provided",
         "open", now_ns - i)
        for i in range(N_NAMED_OPEN)
    ]
    # Generated titles + Task-from shells must stay out of named counts.
    shells = [
        (aid, "shell-1", "shell-agent", "main", "shell-agent · exec · 3 actions",
         "generated", "open", now_ns),
        (aid, "tfm-1", "main", "main", "Task from main", "provided", "open", now_ns),
        (aid, "done-named", "cs-agent", "main", "Closed named", "provided",
         "done", now_ns),
    ]
    cur.executemany(
        "INSERT INTO loops (account_id, external_id, service_name, agent_id, "
        "title, title_source, cached_state, last_event_unix) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        untitled + named + shells,
    )
    cur.execute(
        "UPDATE loops SET closed_at = datetime('now') "
        "WHERE external_id = 'done-named' AND account_id = ?",
        (aid,),
    )
    cur.execute("ANALYZE")

ov_sql, ov_args = database.work_overview_open_sql(aid)
plan = _explain(ov_sql, ov_args)
check("overview open COUNT uses idx_loops_account_title_closed",
      "idx_loops_account_title_closed" in plan)
print(f"    plan: {plan[:240]}")

t0 = time.perf_counter()
ov = database.get_work_overview(aid, viewer_user_id=None)
ov_dt = time.perf_counter() - t0
check("overview open counts named open only (untitled flood excluded)",
      ov["open"] == N_NAMED_OPEN)
check("overview completed_week counts the named close",
      ov["completed_week"] == 1)
check("overview returns in well under a second on 8k-loop fixture",
      ov_dt < 1.0)
print(f"    overview_dt={ov_dt:.3f}s open={ov['open']} done_week={ov['completed_week']}")

print("\n--- many spans, few groups (GET /agents) ---")
now = int(time.time() * 1e9)
# 6 groups like Hammocks. One fat agent (~4k spans) + five small.
rows = []
for i in range(4000):
    rows.append(_span("main-agent", "main", i, now, aid))
for n, svc in enumerate(["ops", "cs", "writer", "billing", "scout"]):
    rows.append(_span(svc, "main", n, now, aid))
database.insert_spans(rows, account_id=aid)

database.save_description("main-agent", "Does the main work", 1, account_id=aid)
database.set_display_name("ops", "main", "Ops Bot", account_id=aid)

t0 = time.perf_counter()
groups = database.get_agents(account_id=aid)
dt1 = time.perf_counter() - t0
by = {g["service_name"]: g for g in groups}
check("six real groups (not an empty fleet)", len(groups) == 6)
check("fat agent span_count is the cheap GROUP BY total",
      by["main-agent"]["total_spans"] == 4000)
check("list paint skips top_operations (would COUNT 4000 spans per group)",
      by["main-agent"]["top_operations"] == [])
check("sidecar description survived the lean path",
      by["main-agent"]["description"] == "Does the main work")
check("sidecar display_name survived the lean path",
      by["ops"]["display_name"] == "Ops Bot")
check("get_agents first paint is well under a second", dt1 < 1.0)
print(f"    get_agents_dt={dt1:.3f}s groups={len(groups)}")

t0 = time.perf_counter()
groups2 = database.get_agents(account_id=aid)
dt2 = time.perf_counter() - t0
check("in-process cache collapses the Dashboard double-fetch",
      dt2 < 0.05 and len(groups2) == 6)
print(f"    get_agents_cached_dt={dt2:.3f}s")

print("\n--- HTTP: /agents and /work/overview ---")
with TestClient(main.app) as c:
    r = c.post("/auth/signup", json={
        "email": "op@t.com", "password": "supersecret123",
        "name": "Op", "account_type": "business", "org_name": "Co",
    }).json()
    H = {"Authorization": f"Bearer {r['token']}"}
    key = r["api_key"]
    post = c.post("/v1/traces", json={"resourceSpans": [{
        "resource": {"attributes": [
            {"key": "service.name", "value": {"stringValue": "http-bot"}}
        ]},
        "scopeSpans": [{"spans": [{
            "traceId": "a" * 32, "spanId": "b" * 16, "name": "op", "kind": 1,
            "startTimeUnixNano": str(now), "endTimeUnixNano": str(now + 1000),
            "status": {"code": 1}, "attributes": [],
        }]}],
    }]}, headers={"X-Trovis-Api-Key": key})
    check("ingest 200", post.status_code == 200)

    t0 = time.perf_counter()
    ag = c.get("/agents", headers=H)
    http_dt = time.perf_counter() - t0
    check("GET /agents 200", ag.status_code == 200)
    names = [g["service_name"] for g in ag.json()]
    check("GET /agents returns the ingested agent (not empty-on-timeout)",
          "http-bot" in names)
    check("GET /agents HTTP is fast on a tiny tenant", http_dt < 1.0)

    t0 = time.perf_counter()
    ovh = c.get("/work/overview", headers=H)
    ovh_dt = time.perf_counter() - t0
    check("GET /work/overview 200", ovh.status_code == 200)
    body = ovh.json()
    check("overview shape locked",
          set(body.keys()) == {
              "needs_you", "needs_attention", "open", "completed_week",
              # Home's fleet pulse compares finished work week over week.
              # Both come off the SAME scan as completed_week — no extra
              # query, which is what this file exists to guard.
              "completed_prev_week", "has_prev_week",
          })
    check("GET /work/overview HTTP is fast", ovh_dt < 1.0)
    print(f"    http /agents={http_dt:.3f}s /work/overview={ovh_dt:.3f}s")

    check("GET /agents handler is sync def (not event-loop)",
          not inspect.iscoroutinefunction(main.list_agents))
    check("GET /dashboard/briefing handler is sync def (not event-loop)",
          not inspect.iscoroutinefunction(main.dashboard_briefing))

print()
if failures:
    print(f"FAILED ({len(failures)}):")
    for f in failures:
        print("  - " + f)
    raise SystemExit(1)
print("All large-tenant survival checks passed.")
os.unlink(_tmp.name)
