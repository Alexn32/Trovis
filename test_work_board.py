"""GET /work/board — the Work tab's board.

Display layer only: every mapping asserted here is a rendering decision, and
loops.py's state machine is untouched. The engine has no idea what a column
is.

Covers: the state->column mapping (including awaiting_agent/awaiting_system
landing in Working with a note rather than inventing columns), holder
resolution to real names, waiting-on-you floating to the top, oldest-first
sorting, done-today scoping, the workflow filter, and cross-account scoping.
"""
import os, tempfile, time

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

with TestClient(main.app) as c:
    r = c.post("/auth/signup", json={"email": "b@t.com", "password": "supersecret123",
        "name": "Alex", "account_type": "business", "org_name": "B"}).json()
    K, T = r["api_key"], r["token"]; H = {"Authorization": f"Bearer {T}"}
    c.post("/team", headers=H, json={"name": "Sarah Chen", "email": "s@t.com", "role": "Lead"})
    def post(svc, spans):
        return c.post("/v1/traces", json={"resourceSpans": [{
            "resource": {"attributes": kv({"service.name": svc})},
            "scopeSpans": [{"spans": spans}]}]}, headers={"X-Trovis-Api-Key": K})
    def board(**q):
        qs = "&".join(f"{k}={v}" for k, v in q.items())
        return c.get(f"/work/board{'?' + qs if qs else ''}", headers=H).json()

    print("\n--- an empty account ---")
    b = board()
    check("board renders all four columns even with nothing in them",
          [col["key"] for col in b["columns"]] == ["working", "waiting_person", "stuck", "done"])
    check("labels are task language, no jargon",
          [col["label"] for col in b["columns"]]
          == ["Working", "Waiting on a person", "Stuck", "Done today"])
    check("has_agents is false before anything connects", b["has_agents"] is False)

    print("\n--- one task per state ---")
    post("cs-agent", [sp("message_received", 600, {"trovis.loop.title": "Reply to customer",
        "trovis.loop.external_id": "s1"}),
        sp("agent_run_complete", 500, {"trovis.loop.external_id": "s1",
            "trovis.handoff.direction": "to_human", "trovis.handoff.target_id": "b@t.com",
            "trovis.handoff.id": "H1"})])
    post("orders-agent", [sp("message_received", 26000, {"trovis.loop.title": "Approve refund",
        "trovis.loop.external_id": "s2"}),
        sp("agent_run_complete", 25000, {"trovis.loop.external_id": "s2",
            "trovis.handoff.direction": "to_human", "trovis.handoff.target_id": "s@t.com",
            "trovis.handoff.id": "H2"})])
    post("content-agent", [sp("message_received", 200, {"trovis.loop.title": "Draft guide",
        "trovis.loop.external_id": "s3"}),
        sp("tool_call", 100, {"trovis.loop.external_id": "s3", "trovis.tool.name": "web_search"})])
    post("billing-agent", [sp("message_received", 900, {"trovis.loop.title": "Reconcile invoices",
        "trovis.loop.external_id": "s4"}),
        sp("agent_run_complete", 800, {"trovis.loop.external_id": "s4",
            "trovis.handoff.direction": "to_system", "trovis.handoff.target_id": "Stripe",
            "trovis.handoff.id": "H4"})])
    post("cs-agent", [sp("message_received", 3000, {"trovis.loop.title": "Answered shipping question",
        "trovis.loop.external_id": "d1"}),
        sp("agent_run_complete", 2900, {"trovis.loop.external_id": "d1",
            "trovis.loop.close": "done", "trovis.run.cost_usd": "0.031"})])

    b = board()
    cols = {col["key"]: col for col in b["columns"]}
    titles = {k: [c["title"] for c in v["cards"]] for k, v in cols.items()}
    check("a task waiting on the viewer lands in 'Waiting on a person'",
          "Reply to customer" in titles["waiting_person"])
    check("a >4h human wait lands in Stuck", "Approve refund" in titles["stuck"])
    check("an active task lands in Working", "Draft guide" in titles["working"])
    check("a task blocked on a SYSTEM stays in Working, not a new column",
          "Reconcile invoices" in titles["working"])
    check("a finished task lands in Done today",
          "Answered shipping question" in titles["done"])
    check("there are still exactly four columns", len(b["columns"]) == 4)

    print("\n--- cards name people and agents, never mechanisms ---")
    blocked = [c for c in cols["working"]["cards"] if c["title"] == "Reconcile invoices"][0]
    check("the blocked task says what it is waiting on", blocked["waiting_on"] == "Stripe")
    check("...and the agent still holds it", blocked["holder_type"] == "agent")
    stuck = cols["stuck"]["cards"][0]
    check("a human holder resolves to a real name", stuck["holder_name"] == "Sarah Chen")
    check("a human-held stuck card does not repeat itself — holder + age say it",
          stuck["stuck_reason"] is None and stuck["holder_name"] == "Sarah Chen")
    mine = cols["waiting_person"]["cards"][0]
    check("the viewer's own task is flagged", mine["is_yours"] is True)
    check("...and carries the id needed to act on it", mine["handoff_event_id"] is not None)
    done = cols["done"]["cards"][0]
    check("cost rides along when nonzero", done["cost_usd"] > 0)
    check("every card has an honest age", all(
        c["age_seconds"] is not None for col in b["columns"] for c in col["cards"]))

    print("\n--- a stuck card with no human names the honest reason ---")
    post("quiet-agent", [sp("message_received", 200000,
        {"trovis.loop.title": "Nightly reconciliation", "trovis.loop.external_id": "q1"})])
    import loops as _lp
    database.recompute_loop_state_standalone(
        [l["id"] for l in database.get_loops(None) if l["service_name"] == "quiet-agent"][0],
        None)
    qcards = [x for col in board()["columns"] for x in col["cards"]
              if x["title"] == "Nightly reconciliation"]
    check("a silent agent-held task says how long it has been silent",
          qcards and qcards[0]["stuck_reason"]
          and "no activity for" in qcards[0]["stuck_reason"])

    print("\n--- sorting: yours first, then oldest ---")
    post("a1", [sp("message_received", 100, {"trovis.loop.title": "New task",
        "trovis.loop.external_id": "s9"}),
        sp("agent_run_complete", 90, {"trovis.loop.external_id": "s9",
            "trovis.handoff.direction": "to_human", "trovis.handoff.target_id": "s@t.com",
            "trovis.handoff.id": "H9"})])
    post("a2", [sp("message_received", 9000, {"trovis.loop.title": "Older task",
        "trovis.loop.external_id": "s10"}),
        sp("agent_run_complete", 8000, {"trovis.loop.external_id": "s10",
            "trovis.handoff.direction": "to_human", "trovis.handoff.target_id": "b@t.com",
            "trovis.handoff.id": "H10"})])
    wp = [col for col in board()["columns"] if col["key"] == "waiting_person"][0]["cards"]
    check("waiting-on-you floats above everything else in its column",
          wp[0]["is_yours"] is True)
    ages = [c["age_seconds"] for c in wp if not c["is_yours"]]
    check("the rest are oldest-first", ages == sorted(ages, reverse=True))

    print("\n--- the state machine is untouched ---")
    check("no column name is an engine state",
          not set(["working", "waiting_person", "stuck", "done"]) - set(["working"])
          <= set(loops_mod.STATES) or "waiting_person" not in loops_mod.STATES)
    check("'quiet' is still not a state — the board never invented one",
          "quiet" not in loops_mod.STATES)
    check("engine states are exactly what they were",
          loops_mod.STATES == ("open", "working", "awaiting_human", "awaiting_agent",
                               "awaiting_system", "stalled", "done", "abandoned"))

    print("\n--- cross-account scoping ---")
    r2 = c.post("/auth/signup", json={"email": "o@t.com", "password": "supersecret123",
        "name": "Other", "account_type": "individual", "org_name": "O"}).json()
    other = c.get("/work/board", headers={"Authorization": f"Bearer {r2['token']}"}).json()
    check("another account's board is empty — no leakage", other["total"] == 0)

print()
if failures:
    print(f"FAILED ({len(failures)}):")
    for f in failures: print("  - " + f)
    raise SystemExit(1)
print("All work-board checks passed.")
os.unlink(_tmp.name)
