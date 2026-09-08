"""Lean Work home: GET /work/overview + GET /work/items + /health survival.

Home must not depend on fat /work/board or loop-scanning /work/summary.
Named work only (titled loops). Locked shapes:

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

    ov = overview()
    page = items(limit=50)
    by_title = {it["title"]: it for it in page["items"]}

    print("\n--- overview counts ---")
    # named open: n1 (yours fresh), n2 (sarah fresh), n3 (sarah aging), n4 (stripe),
    # n5 (moving), n7 (yours aging). n6 is done. untitled raw1 excluded.
    check("open counts named open only (untitled flood excluded)",
          ov["open"] == 6)
    check("completed_week counts the named close",
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
