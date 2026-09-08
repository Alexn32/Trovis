"""Lean Work home: GET /work/overview + GET /work/items + /health survival.

Home must not depend on fat /work/board or loop-scanning /work/summary.
Named work only (plugin-provided human titles). Untitled OTel loops,
Trovis-generated template/LLM titles, and "Task from …" shells are excluded.

Locked shapes:

  overview: { needs_you, needs_attention, open, completed_week? }
  items:    { id, title, status, holder:{kind,name}, whats_next, updated_at }

Status enum includes waiting_on_other (UI label is FE's "Waiting on someone").
needs_you = waiting_on_you ONLY.
needs_attention = stuck + aging waiting_on_other (never waiting_on_you).
"""
import os, tempfile, time, threading

os.environ.update({"OVERSEE_DISABLE_PRICING_SYNC": "1", "TROVIS_DISABLE_ALERTS": "1",
                   "TROVIS_DISABLE_LOOP_SWEEP": "1", "TROVIS_LOOP_TITLES": "off"})
os.environ.pop("DATABASE_URL", None)
os.environ.pop("ANTHROPIC_API_KEY", None)
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False); _tmp.close()
import database; database.SQLITE_PATH = _tmp.name
import loops as loops_mod, main
from fastapi.testclient import TestClient
main._auto_describe = lambda *a, **k: False

failures = []
def check(label, cond):
    print(("  PASS " if cond else "  FAIL ") + label)
    if not cond: failures.append(label)

def kv(d): return [{"key": k, "value": {"stringValue": str(v)}} for k, v in d.items()]
_n = [0]
def sp(name, off, attrs):
    _n[0] += 1
    return {"traceId": f"{_n[0]:032d}", "spanId": f"{_n[0]:016d}", "name": name, "kind": 1,
            "startTimeUnixNano": str(NOW - off * NS), "endTimeUnixNano": str(NOW - off * NS + 10**6),
            "status": {"code": 1}, "attributes": kv(attrs)}
NS = 10**9
NOW = time.time_ns()

ITEM_KEYS = {"id", "title", "status", "holder", "whats_next", "updated_at"}
STATUSES = {"waiting_on_you", "waiting_on_other", "stuck", "moving", "done"}
HOLDER_KINDS = {"human", "agent", "tool", "unassigned"}

with TestClient(main.app) as c:
    r = c.post("/auth/signup", json={"email": "s@t.com", "password": "supersecret123",
        "name": "Alex", "account_type": "business", "org_name": "Co"}).json()
    K, T = r["api_key"], r["token"]; H = {"Authorization": f"Bearer {T}"}
    c.post("/team", headers=H, json={"name": "Sarah Chen", "email": "sarah@t.com", "role": "Lead"})
    def post(svc, spans):
        return c.post("/v1/traces", json={"resourceSpans": [{
            "resource": {"attributes": kv({"service.name": svc})},
            "scopeSpans": [{"spans": spans}]}]}, headers={"X-Trovis-Api-Key": K})
    def overview():
        return c.get("/work/overview", headers=H).json()
    def items(**q):
        qs = "&".join(f"{k}={v}" for k, v in q.items() if v is not None)
        return c.get(f"/work/items{'?' + qs if qs else ''}", headers=H).json()

    print("\n--- empty account ---")
    ov = overview()
    check("overview keys are the locked contract",
          set(ov.keys()) == {"needs_you", "needs_attention", "open", "completed_week"})
    check("empty: all zeros",
          ov["needs_you"] == 0 and ov["needs_attention"] == 0
          and ov["open"] == 0 and ov["completed_week"] == 0)
    page = items()
    check("empty items list + no cursor",
          page["items"] == [] and page["next_cursor"] is None)

    sug = c.get("/work/suggestions", headers=H).json()
    check("suggestions stub is an empty list (no invented titles)",
          sug == {"suggestions": []})

    print("\n--- named vs untitled (no OTel flood) ---")
    post("cs-agent", [sp("message_received", 600, {"trovis.loop.title": "Reply to customer",
        "trovis.loop.external_id": "n1"}),
        sp("agent_run_complete", 500, {"trovis.loop.external_id": "n1",
            "trovis.handoff.direction": "to_human", "trovis.handoff.target_id": "s@t.com",
            "trovis.handoff.id": "H1"})])
    # Untitled raw OTel — must not appear on home.
    post("flood-agent", [sp("message_received", 400, {"trovis.loop.external_id": "raw1"}),
        sp("tool_call", 200, {"trovis.loop.external_id": "raw1", "trovis.tool.name": "exec"})])
    # Untitled + first handoff: ingest generates a template title
    # (`{agent} · {tool} · N actions`). Generated ≠ named work.
    post("shell-agent", [sp("message_received", 380, {"trovis.loop.external_id": "shell1"}),
        sp("agent_run_complete", 370, {"trovis.loop.external_id": "shell1",
            "trovis.handoff.direction": "to_system", "trovis.handoff.target_id": "Pager",
            "trovis.handoff.id": "HS"})])
    # Display-fallback shell stored as if it were a title.
    post("main", [sp("message_received", 360, {"trovis.loop.title": "Task from main",
        "trovis.loop.external_id": "tfm"})])
    # Closed untitled OTel — sweep-style generated title must not inflate
    # completed_week.
    post("done-flood", [sp("message_received", 5000, {"trovis.loop.external_id": "df1"}),
        sp("agent_run_complete", 4900, {"trovis.loop.external_id": "df1",
            "trovis.loop.close": "done"})])
    # Fresh wait on someone else (Sarah) — waiting_on_other, NOT needs_attention.
    post("orders-agent", [sp("message_received", 300, {"trovis.loop.title": "Confirm address",
        "trovis.loop.external_id": "n2"}),
        sp("agent_run_complete", 200, {"trovis.loop.external_id": "n2",
            "trovis.handoff.direction": "to_human", "trovis.handoff.target_id": "sarah@t.com",
            "trovis.handoff.id": "H2"})])
    # Aging wait on Sarah (> stall threshold) — waiting_on_other AND needs_attention.
    post("ops-agent", [sp("message_received", 200000, {"trovis.loop.title": "Approve refund",
        "trovis.loop.external_id": "n3"}),
        sp("agent_run_complete", 190000, {"trovis.loop.external_id": "n3",
            "trovis.handoff.direction": "to_human", "trovis.handoff.target_id": "sarah@t.com",
            "trovis.handoff.id": "H3"})])
    # Stuck on a system/tool (awaiting_system, named).
    post("bill-agent", [sp("message_received", 900, {"trovis.loop.title": "Reconcile invoices",
        "trovis.loop.external_id": "n4"}),
        sp("agent_run_complete", 800, {"trovis.loop.external_id": "n4",
            "trovis.handoff.direction": "to_system", "trovis.handoff.target_id": "Stripe",
            "trovis.handoff.id": "H4"})])
    # Moving named work.
    post("writer-agent", [sp("message_received", 120, {"trovis.loop.title": "Draft guide",
        "trovis.loop.external_id": "n5"}),
        sp("tool_call", 60, {"trovis.loop.external_id": "n5", "trovis.tool.name": "web_search"})])
    # Done this week.
    post("cs-agent", [sp("message_received", 4000, {"trovis.loop.title": "Answered a question",
        "trovis.loop.external_id": "n6"}),
        sp("agent_run_complete", 3900, {"trovis.loop.external_id": "n6",
            "trovis.loop.close": "done"})])
    # Aging wait on YOU (stalled + is_you) — needs_you ONLY, not needs_attention.
    post("you-stale", [sp("message_received", 200000, {"trovis.loop.title": "Old thing for Alex",
        "trovis.loop.external_id": "n7"}),
        sp("agent_run_complete", 190000, {"trovis.loop.external_id": "n7",
            "trovis.handoff.direction": "to_human", "trovis.handoff.target_id": "s@t.com",
            "trovis.handoff.id": "H7"})])

    # Stamp a generated title on the closed untitled flood the way the
    # sweep does — title present, source=generated, still not named work.
    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute("SELECT id, account_id, title, title_source FROM loops WHERE external_id = ?",
                    ("df1",))
        df1 = dict(cur.fetchone())
        cur.execute("SELECT id, title, title_source FROM loops WHERE external_id = ?",
                    ("shell1",))
        shell1 = dict(cur.fetchone())
        cur.execute("SELECT id, title, title_source FROM loops WHERE external_id = ?",
                    ("tfm",))
        tfm = dict(cur.fetchone())
    check("handoff untitled loop got a generated template title",
          bool((shell1.get("title") or "").strip())
          and shell1.get("title_source") == "generated")
    check("Task-from-main shell stored as provided (ingest) but is a shell",
          tfm.get("title") == "Task from main" and tfm.get("title_source") == "provided")
    check("closed untitled flood starts untitled",
          not (df1.get("title") or "").strip())
    database.set_loop_title_if_missing(
        df1["id"], "done-flood · exec · 2 actions", df1["account_id"])
    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute("SELECT title, title_source FROM loops WHERE id = ?", (df1["id"],))
        df1_titled = dict(cur.fetchone())
    check("sweep-style write marks title_source=generated",
          df1_titled.get("title_source") == "generated" and bool(df1_titled.get("title")))

    ov = overview()
    page = items(limit=50)
    by_title = {it["title"]: it for it in page["items"]}
    item_ids = {it["id"] for it in page["items"]}

    print("\n--- overview counts ---")
    # named open: n1 (yours fresh), n2 (sarah fresh), n3 (sarah aging), n4 (stripe),
    # n5 (moving), n7 (yours aging). n6 is done. untitled raw1, generated
    # shell1, Task-from-main tfm, generated closed df1 all excluded.
    check("open counts named open only (untitled flood excluded)",
          ov["open"] == 6)
    check("completed_week counts the named close",
          ov["completed_week"] == 1)
    check("generated closed title does not inflate completed_week",
          ov["completed_week"] == 1)
    # needs_you: n1 + n7 (both waiting_on_you). NOT n3.
    check("needs_you is waiting_on_you only (2: fresh + aging yours)",
          ov["needs_you"] == 2)
    # needs_attention: n3 (aging waiting_on_other) + n4 (stuck). NOT n1/n7, NOT n2.
    check("needs_attention is stuck + aging waiting_on_other, not yours",
          ov["needs_attention"] == 2)

    print("\n--- items shape + enums ---")
    check("untitled flood is not in the table",
          "raw1" not in by_title and all("flood" not in (it["title"] or "") for it in page["items"]))
    check("generated template title is not in the table",
          shell1["id"] not in item_ids
          and all(" · " not in (it["title"] or "") or not (it["title"] or "").endswith(" actions")
                  for it in page["items"]))
    check("Task-from-main shell is not in the table",
          tfm["id"] not in item_ids
          and all(not (it["title"] or "").lower().startswith("task from ") for it in page["items"]))
    check("items never return untitled/raw-loop rows",
          all((it.get("title") or "").strip() and not database._is_shell_work_title(it["title"])
              for it in page["items"]))

    print("\n--- title_source backfill (pre-hotfix rows) ---")
    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute("SELECT id FROM loops WHERE title = ?", ("Reply to customer",))
        named_id = int(cur.fetchone()["id"])
        cur.execute("UPDATE loops SET title_source = NULL WHERE id = ?", (named_id,))
        database._backfill_loop_title_source(cur)
        cur.execute("SELECT title_source FROM loops WHERE id = ?", (named_id,))
        restored = cur.fetchone()["title_source"]
        cur.execute("SELECT id FROM loops WHERE external_id = ?", ("shell1",))
        gen_id = int(cur.fetchone()["id"])
        cur.execute("UPDATE loops SET title_source = NULL WHERE id = ?", (gen_id,))
        database._backfill_loop_title_source(cur)
        cur.execute("SELECT title_source FROM loops WHERE id = ?", (gen_id,))
        gen_restored = cur.fetchone()["title_source"]
    check("backfill restores plugin title as provided", restored == "provided")
    check("backfill marks generated template as generated", gen_restored == "generated")
    check("every item has the locked keys",
          all(set(it.keys()) == ITEM_KEYS for it in page["items"]))
    check("status is the locked enum (waiting_on_other stays that wire value)",
          all(it["status"] in STATUSES for it in page["items"]))
    check("holder.kind is the locked enum",
          all((it["holder"] or {}).get("kind") in HOLDER_KINDS for it in page["items"]))

    yours = by_title["Reply to customer"]
    check("yours: status=waiting_on_you, holder human",
          yours["status"] == "waiting_on_you" and yours["holder"]["kind"] == "human")
    other = by_title["Confirm address"]
    check("fresh other: status=waiting_on_other (not a renamed enum)",
          other["status"] == "waiting_on_other")
    aging = by_title["Approve refund"]
    check("aging other still waiting_on_other, not stuck",
          aging["status"] == "waiting_on_other")
    toolish = by_title["Reconcile invoices"]
    check("to_system: status=stuck, holder.kind=tool",
          toolish["status"] == "stuck" and toolish["holder"]["kind"] == "tool")
    moving = by_title["Draft guide"]
    check("in-flight named work is moving",
          moving["status"] == "moving")
    done = by_title["Answered a question"]
    check("closed named work is done",
          done["status"] == "done")
    aging_yours = by_title["Old thing for Alex"]
    check("aging yours stays waiting_on_you (not stuck, not needs_attention)",
          aging_yours["status"] == "waiting_on_you")

    print("\n--- home does not touch get_work_board ---")
    orig = database.get_work_board
    def boom(*a, **k):
        raise AssertionError("get_work_board must not run on the lean home path")
    database.get_work_board = boom
    try:
        ov2 = c.get("/work/overview", headers=H)
        it2 = c.get("/work/items", headers=H)
        sg2 = c.get("/work/suggestions", headers=H)
        check("overview survives with get_work_board broken", ov2.status_code == 200)
        check("items survive with get_work_board broken", it2.status_code == 200)
        check("suggestions survive with get_work_board broken", sg2.status_code == 200)
    finally:
        database.get_work_board = orig

    print("\n--- pagination ---")
    p1 = items(limit=2)
    check("limit=2 returns 2 + cursor",
          len(p1["items"]) == 2 and bool(p1["next_cursor"]))
    p2 = items(limit=2, cursor=p1["next_cursor"])
    check("page 2 is a different pair",
          {it["id"] for it in p2["items"]}.isdisjoint({it["id"] for it in p1["items"]})
          and len(p2["items"]) == 2)

    print("\n--- cross-account ---")
    r2 = c.post("/auth/signup", json={"email": "o@t.com", "password": "supersecret123",
        "name": "O", "account_type": "individual", "org_name": "O"}).json()
    other_h = {"Authorization": f"Bearer {r2['token']}"}
    ov_o = c.get("/work/overview", headers=other_h).json()
    it_o = c.get("/work/items", headers=other_h).json()
    check("other account: empty overview",
          ov_o["open"] == 0 and ov_o["needs_you"] == 0 and ov_o["needs_attention"] == 0)
    check("other account: no items", it_o["items"] == [])

    print("\n--- /health stays fast while fat summary blocks ---")
    def slow_board(*a, **k):
        time.sleep(1.2)
        return orig(*a, **k)
    database.get_work_board = slow_board
    summary_status = {"code": None}
    def hit_summary():
        summary_status["code"] = c.get("/work/summary", headers=H).status_code
    th = threading.Thread(target=hit_summary)
    th.start()
    time.sleep(0.15)
    t0 = time.perf_counter()
    health = c.get("/health")
    health_dt = time.perf_counter() - t0
    th.join(timeout=8)
    database.get_work_board = orig
    check("health 200 while summary is blocked", health.status_code == 200)
    check("health body is the liveness shape",
          health.json().get("status") == "ok" and "version" in health.json())
    check("health returns in well under a second (not waiting on summary)",
          health_dt < 0.5)
    print(f"    health_dt={health_dt:.3f}s summary_status={summary_status['code']}")

    print("\n--- engine states untouched ---")
    check("engine states unchanged",
          loops_mod.STATES == ("open", "working", "awaiting_human", "awaiting_agent",
                               "awaiting_system", "stalled", "done", "abandoned"))

print()
if failures:
    print(f"FAILED ({len(failures)}):")
    for f in failures: print("  - " + f)
    raise SystemExit(1)
print("All lean work-home checks passed.")
os.unlink(_tmp.name)
