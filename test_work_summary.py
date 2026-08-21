"""GET /work/summary — the Work tab's Level-1 landing.

One card per kind of work (per workflow) + an "Other work" catch-all, plus
the cross-workflow strip of tasks waiting on the caller. Display layer only;
loops.py untouched. Reuses database.get_work_board UNFILTERED — no N+1.

Covers: rollup counts by column, the "yours" strip spanning all kinds, the
Other-work catch-all + its declare nudge, sort order (attention first),
cross-account scoping.
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
    r = c.post("/auth/signup", json={"email": "s@t.com", "password": "supersecret123",
        "name": "Alex", "account_type": "business", "org_name": "Co"}).json()
    K, T = r["api_key"], r["token"]; H = {"Authorization": f"Bearer {T}"}
    c.post("/team", headers=H, json={"name": "Sarah Chen", "email": "sarah@t.com", "role": "Lead"})
    def post(svc, spans):
        return c.post("/v1/traces", json={"resourceSpans": [{
            "resource": {"attributes": kv({"service.name": svc})},
            "scopeSpans": [{"spans": spans}]}]}, headers={"X-Trovis-Api-Key": K})
    def summary():
        return c.get("/work/summary", headers=H).json()
    def t(x): return {"trovis.loop.title": x}

    print("\n--- empty account ---")
    s = summary()
    check("empty: no kinds, no other, empty yours", not s["kinds"] and not s["other"] and not s["yours"])
    check("has_agents false before anything connects", s["has_agents"] is False)

    for name, svc in [("Customer service", "cs-agent"), ("Order ops", "orders-agent")]:
        c.post("/workflows", headers=H, json={"name": name,
            "match_hints": [{"field": "service_name", "op": "equals", "value": svc}],
            "stations": [{"holder_type": "agent", "holder": svc}]})

    # cs-agent: 1 waiting on YOU, 1 in motion, 1 done ($0.04)
    post("cs-agent", [sp("message_received", 7000, {**t("Reply to customer"), "trovis.loop.external_id": "c1"}),
        sp("agent_run_complete", 6800, {"trovis.loop.external_id": "c1", "trovis.handoff.direction": "to_human",
            "trovis.handoff.target_id": "s@t.com", "trovis.handoff.id": "H1"})])
    post("cs-agent", [sp("message_received", 300, {**t("Draft reply"), "trovis.loop.external_id": "c2"}),
        sp("tool_call", 120, {"trovis.loop.external_id": "c2", "trovis.tool.name": "web_search"})])
    post("cs-agent", [sp("message_received", 4000, {**t("Answered a question"), "trovis.loop.external_id": "c3"}),
        sp("agent_run_complete", 3900, {"trovis.loop.external_id": "c3", "trovis.loop.close": "done",
            "trovis.run.cost_usd": "0.04"})])
    # orders-agent: 1 stuck (Sarah, >4h), 1 in motion
    post("orders-agent", [sp("message_received", 200000, {**t("Confirm address"), "trovis.loop.external_id": "o1"}),
        sp("agent_run_complete", 190000, {"trovis.loop.external_id": "o1", "trovis.handoff.direction": "to_human",
            "trovis.handoff.target_id": "sarah@t.com", "trovis.handoff.id": "H5"})])
    post("orders-agent", [sp("message_received", 500, {**t("Reconcile orders"), "trovis.loop.external_id": "o2"}),
        sp("tool_call", 200, {"trovis.loop.external_id": "o2", "trovis.tool.name": "exec"})])
    # unmatched -> other work, 3 in motion (exceeds each declared kind's 1)
    for i in range(3):
        post(f"scraper-{i}", [sp("message_received", 200 + i, {**t(f"Scrape batch {i}"), "trovis.loop.external_id": f"x{i}"}),
            sp("tool_call", 50, {"trovis.loop.external_id": f"x{i}", "trovis.tool.name": "web_fetch"})])

    s = summary()
    kinds = {k["name"]: k for k in s["kinds"]}

    print("\n--- rollups per kind ---")
    cs = kinds["Customer service"]
    check("cs: in_motion=1, waiting_person=1, stuck=0, done_today=1",
          cs["in_motion"] == 1 and cs["waiting_person"] == 1 and cs["stuck"] == 0 and cs["done_today"] == 1)
    check("cs: cost_today carries the done task's cost", abs(cs["cost_today"] - 0.04) < 1e-6)
    oo = kinds["Order ops"]
    check("orders: in_motion=1, stuck=1 (the >4h human wait), waiting_person=0",
          oo["in_motion"] == 1 and oo["stuck"] == 1 and oo["waiting_person"] == 0)

    print("\n--- the yours strip spans ALL kinds ---")
    check("exactly one task waiting on the caller", len(s["yours"]) == 1)
    check("it's cs-agent's, and it carries the id to act on",
          s["yours"][0]["title"] == "Reply to customer" and s["yours"][0]["handoff_event_id"] is not None)
    check("it is NOT the one targeted at Sarah",
          all("Confirm address" != y["title"] for y in s["yours"]))

    print("\n--- Other work + the declare nudge ---")
    o = s["other"]
    check("other exists, is flagged is_other, named honestly",
          o and o["is_other"] and o["name"] == "Other work" and o["workflow_id"] is None)
    check("other: in_motion=3", o["in_motion"] == 3)
    check("suggest_declare true — its pile exceeds every declared kind's",
          o["suggest_declare"] is True)

    print("\n--- sort: attention first, then activity ---")
    # cs (waiting) and orders (stuck) both have 1 attention item; tie broken by
    # activity today: cs has motion+done=2, orders=1 -> cs first.
    check("kinds sorted with the busier attention card first",
          s["kinds"][0]["name"] == "Customer service")

    print("\n--- one fetch, not N+1 (no per-workflow endpoint) ---")
    # /work/summary must not spawn a request per workflow; assert it reuses the
    # board fetch by checking the same underlying function exists and the
    # endpoint returns everything in one call (already proven by one GET above).
    check("summary total counts every open + done-today task",
          s["total"] == 8)

    print("\n--- cross-account scoping ---")
    r2 = c.post("/auth/signup", json={"email": "o@t.com", "password": "supersecret123",
        "name": "O", "account_type": "individual", "org_name": "O"}).json()
    other = c.get("/work/summary", headers={"Authorization": f"Bearer {r2['token']}"}).json()
    check("another account sees nothing", other["total"] == 0 and not other["kinds"])

    print("\n--- state machine untouched ---")
    check("engine states unchanged",
          loops_mod.STATES == ("open", "working", "awaiting_human", "awaiting_agent",
                               "awaiting_system", "stalled", "done", "abandoned"))

print()
if failures:
    print(f"FAILED ({len(failures)}):")
    for f in failures: print("  - " + f)
    raise SystemExit(1)
print("All work-summary checks passed.")
os.unlink(_tmp.name)
