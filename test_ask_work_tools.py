"""Ask work-record tools: lean named Work only (never the fat board).

Hermetic (temp SQLite, no Claude). Seeds work via /v1/traces, then calls
asker._run_tool so we assert tool payloads, not the model.

Covers:
  - account scoping
  - waiting_on_you with a signed-in viewer
  - API key / no viewer → explicit sign-in
  - get_work_overview / get_work_items / find_tasks / get_task_story use
    lean named helpers — never get_work_board
  - untitled OTel / 'Task from main' flood is not Work truth
  - stuck items carry service_name so 'what agent is stuck?' is not empty
  - 'What's stuck?' cites fresh waiting_on_other (promis?) even when
    overview stuck/needs_attention is 0 — not a fleet telemetry pivot
"""
import json
import os
import tempfile
import time

os.environ.update({
    "OVERSEE_DISABLE_PRICING_SYNC": "1",
    "TROVIS_DISABLE_PRICING_SYNC": "1",
    "TROVIS_DISABLE_ALERTS": "1",
    "TROVIS_DISABLE_LOOP_SWEEP": "1",
    "TROVIS_LOOP_TITLES": "off",
})
os.environ.pop("DATABASE_URL", None)
os.environ["ANTHROPIC_API_KEY"] = "sk-test-dummy"

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
import database
database.SQLITE_PATH = _tmp.name
import asker
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

_n = [0]
def sp(name, off, attrs):
    _n[0] += 1
    return {
        "traceId": f"{_n[0]:032d}", "spanId": f"{_n[0]:016d}", "name": name,
        "kind": 1,
        "startTimeUnixNano": str(NOW - off * NS),
        "endTimeUnixNano": str(NOW - off * NS + 10**6),
        "status": {"code": 1}, "attributes": kv(attrs),
    }

NS = 10**9
NOW = time.time_ns()


def tool(name, inp, account_id, viewer=None):
    return json.loads(asker._run_tool(name, inp, account_id, viewer_user_id=viewer))


def _is_flood_title(title):
    t = (title or "").strip()
    if not t:
        return True
    return database._is_shell_work_title(t)


def _titles(payload):
    return [t.get("title") or "" for t in (payload.get("tasks") or [])]


with TestClient(main.app) as c:
    r = c.post("/auth/signup", json={
        "email": "s@t.com", "password": "supersecret123",
        "name": "Alex", "account_type": "business", "org_name": "Co",
    }).json()
    K, T = r["api_key"], r["token"]
    H = {"Authorization": f"Bearer {T}"}
    uid = r["user"]["id"]
    aid = r["org"]["id"]
    # A named pending invite: she has no login, and work handed to her
    # address still reads as her name. (POST /team is closed — 410.)
    c.post("/org/invites", headers=H, json={"email": "sarah@t.com", "name": "Sarah Chen"})

    def post(svc, spans):
        return c.post("/v1/traces", json={"resourceSpans": [{
            "resource": {"attributes": kv({"service.name": svc})},
            "scopeSpans": [{"spans": spans}],
        }]}, headers={"X-Trovis-Api-Key": K})

    def t(x):
        return {"trovis.loop.title": x}

    for name, svc in [("Customer service", "cs-agent"), ("Order ops", "orders-agent")]:
        c.post("/workflows", headers=H, json={
            "name": name,
            "match_hints": [{"field": "service_name", "op": "equals", "value": svc}],
            "stations": [{"holder_type": "agent", "holder": svc}],
        })

    # cs-agent: 1 waiting on YOU, 1 in motion, 1 done
    post("cs-agent", [
        sp("message_received", 7000, {**t("Reply to customer"), "trovis.loop.external_id": "c1"}),
        sp("agent_run_complete", 6800, {
            "trovis.loop.external_id": "c1",
            "trovis.handoff.direction": "to_human",
            "trovis.handoff.target_id": "s@t.com",
            "trovis.handoff.id": "H1",
        }),
    ])
    post("cs-agent", [
        sp("message_received", 300, {**t("Draft reply"), "trovis.loop.external_id": "c2"}),
        sp("tool_call", 120, {"trovis.loop.external_id": "c2", "trovis.tool.name": "web_search"}),
    ])
    post("cs-agent", [
        sp("message_received", 4000, {**t("Answered a question"), "trovis.loop.external_id": "c3"}),
        sp("agent_run_complete", 3900, {
            "trovis.loop.external_id": "c3",
            "trovis.loop.close": "done",
            "trovis.run.cost_usd": "0.04",
        }),
    ])
    # orders-agent: aging wait on Sarah (needs_attention, waiting_on_other)
    post("orders-agent", [
        sp("message_received", 200000, {**t("Confirm address"), "trovis.loop.external_id": "o1"}),
        sp("agent_run_complete", 190000, {
            "trovis.loop.external_id": "o1",
            "trovis.handoff.direction": "to_human",
            "trovis.handoff.target_id": "sarah@t.com",
            "trovis.handoff.id": "H5",
        }),
    ])
    post("orders-agent", [
        sp("message_received", 500, {**t("Reconcile orders"), "trovis.loop.external_id": "o2"}),
        sp("tool_call", 200, {"trovis.loop.external_id": "o2", "trovis.tool.name": "exec"}),
    ])
    # Named agent-stuck item (stalled, no human handoff) — holder is the agent.
    post("orders-agent", [
        sp("message_received", 200000, {**t("Retry shipment"), "trovis.loop.external_id": "o3"}),
        sp("tool_call", 190000, {"trovis.loop.external_id": "o3", "trovis.tool.name": "ship"}),
    ])
    # Fresh wait on someone (not aged → NOT needs_attention). Same shape as
    # QA Hammocks "promis?" — open named work that status=stuck misses.
    post("main", [
        sp("message_received", 400, {**t("promis?"), "trovis.loop.external_id": "p1"}),
        sp("agent_run_complete", 300, {
            "trovis.loop.external_id": "p1",
            "trovis.handoff.direction": "to_human",
            "trovis.handoff.target_id": "sarah@t.com",
            "trovis.handoff.id": "H8",
        }),
    ])
    # Untitled OTel flood + stored "Task from main" shells — must not be Work.
    flood_ids = []
    for i in range(12):
        post("main", [sp("message_received", 80 + i, {"trovis.loop.external_id": f"raw{i}"})])
        post("main", [sp("message_received", 40 + i, {
            **t("Task from main"), "trovis.loop.external_id": f"tfm{i}",
        })])
    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute(
            "UPDATE loops SET cached_state = 'stalled' "
            "WHERE external_id = ? AND account_id = ?",
            ("o3", aid),
        )
        cur.execute(
            "SELECT id FROM loops WHERE account_id = ? "
            "AND (title IS NULL OR title = '' OR title LIKE 'Task from %')",
            (aid,),
        )
        flood_ids = [int(row["id"]) for row in cur.fetchall()]
        conn.commit()

    print("\n--- get_waiting_on_me is named waiting_on_you ---")
    mine = tool("get_waiting_on_me", {}, aid, viewer=uid)
    check("signed in", mine.get("signed_in") is True)
    check("exactly one named task waiting on the viewer",
          mine.get("count") == 1 and len(mine.get("tasks") or []) == 1)
    check("matches Reply to customer",
          mine["tasks"][0]["title"] == "Reply to customer")
    check("yours row has holder + service_name",
          mine["tasks"][0].get("holder") and mine["tasks"][0].get("service_name") == "cs-agent")
    check("Sarah's task is NOT in yours",
          all(t["title"] != "Confirm address" for t in mine["tasks"]))
    check("yours is not a Task-from-main flood",
          all(not _is_flood_title(t.get("title")) for t in mine["tasks"]))

    print("\n--- no viewer: sign in, never fake is_yours ---")
    anon = tool("get_waiting_on_me", {}, aid, viewer=None)
    check("API-key path asks to sign in",
          anon.get("signed_in") is False and "sign in" in (anon.get("message") or "").lower())
    check("API-key path returns no tasks", anon.get("tasks") == [])

    overview_anon = tool("get_work_overview", {}, aid, viewer=None)
    check("overview without viewer has no yours list",
          "yours" not in overview_anon and overview_anon.get("yours_count") is None)
    check("overview without viewer is lean named counts",
          overview_anon.get("needs_attention") >= 1
          and overview_anon.get("open") >= 1
          and overview_anon.get("named_only") is True)
    check("anonymous overview does not claim a fat board total",
          "kinds" not in overview_anon and "other" not in overview_anon)

    print("\n--- get_work_overview is lean named-only ---")
    overview = tool("get_work_overview", {}, aid, viewer=uid)
    check("overview keys are the lean contract",
          overview.get("needs_you") == 1
          and overview.get("needs_attention") >= 1
          and overview.get("open") >= 4
          and overview.get("named_only") is True)
    check("yours_count is needs_you when signed in", overview.get("yours_count") == 1)
    check("overview does not dump Other-work / kinds",
          "kinds" not in overview and "other" not in overview and "yours" not in overview)
    check("overview note bans treating OTel as Work",
          "telemetry" in (overview.get("note") or "").lower()
          or "named" in (overview.get("note") or "").lower())
    # Flood is 12 untitled + 12 Task-from-main. Named open is far smaller.
    check("overview open is not the OTel flood",
          overview["open"] < 20)

    print("\n--- account scoping ---")
    r2 = c.post("/auth/signup", json={
        "email": "o@t.com", "password": "supersecret123",
        "name": "O", "account_type": "individual", "org_name": "O",
    }).json()
    other_aid, other_uid = r2["org"]["id"], r2["user"]["id"]
    other_mine = tool("get_waiting_on_me", {}, other_aid, viewer=other_uid)
    other_over = tool("get_work_overview", {}, other_aid, viewer=other_uid)
    other_find = tool("find_tasks", {"query": "Reply"}, other_aid, viewer=other_uid)
    check("other account: nothing waiting", other_mine.get("count") == 0)
    check("other account: empty overview", other_over.get("open") == 0)
    check("other account: find_tasks misses this org's tasks", other_find.get("count") == 0)
    stolen = tool("get_task_story", {"loop_id": mine["tasks"][0]["id"]}, other_aid, viewer=other_uid)
    check("other account cannot read this org's task story",
          stolen.get("error") == "task not found")

    print("\n--- find_tasks + get_work_items are named-only ---")
    found = tool("find_tasks", {"query": "Confirm address"}, aid, viewer=uid)
    check("find_tasks locates the named waiting task by title",
          found.get("count") == 1 and found["tasks"][0]["title"] == "Confirm address")
    waiting = found["tasks"][0]
    check("Confirm address is waiting_on_other (Sarah), not a board 'stuck' dump",
          waiting["status"] == "waiting_on_other"
          and "sarah" in (waiting.get("holder") or "").lower())
    check("Confirm address is not is_yours for Alex",
          waiting.get("is_yours") is False)
    check("named item carries service_name for agent follow-ups",
          waiting.get("service_name") == "orders-agent")

    story = tool("get_task_story", {"loop_id": waiting["id"]}, aid, viewer=uid)
    check("story is the Confirm address loop",
          story.get("title") == "Confirm address" and story.get("id") == waiting["id"])
    check("story cites lean waiting state + Sarah",
          story.get("status") == "waiting_on_other"
          and "sarah" in (story.get("holder") or "").lower())
    check("story is_yours is false for Alex (it's Sarah's)",
          story.get("is_yours") is False)
    events = story.get("events") or []
    event_blob = json.dumps(events).lower()
    check("story includes live loop events (handoff to human)",
          any(e.get("type") == "handoff_initiated" for e in events)
          or "handoff" in event_blob)
    check("story does not invent from spans/fleet — events are loop events",
          all(e.get("type") != "span" for e in events))
    check("story title is not a Task-from shell",
          not _is_flood_title(story.get("title")))

    by_id = tool("find_tasks", {"query": str(waiting["id"])}, aid, viewer=uid)
    check("find_tasks matches id substring",
          by_id.get("count") >= 1 and any(t["id"] == waiting["id"] for t in by_id["tasks"]))

    print("\n--- stuck Ask path is named-only (no Task-from-main flood) ---")
    stuck = tool("get_work_items", {"status": "stuck"}, aid, viewer=uid)
    stuck_find = tool("find_tasks", {"query": "", "status": "stuck"}, aid, viewer=uid)
    check("stuck tools return the named Retry shipment",
          any(t["title"] == "Retry shipment" for t in stuck.get("tasks") or [])
          and any(t["title"] == "Retry shipment" for t in stuck_find.get("tasks") or []))
    check("stuck items are named-only",
          stuck.get("named_only") is True
          and all(not _is_flood_title(t.get("title")) for t in stuck.get("tasks") or []))
    check("stuck find_tasks is named-only",
          all(not _is_flood_title(t.get("title")) for t in stuck_find.get("tasks") or []))
    check("stuck list is not a 25-item Task-from-main dump",
          stuck.get("count") < 10 and stuck_find.get("count") < 10)
    retry = next(t for t in stuck["tasks"] if t["title"] == "Retry shipment")
    check("stuck item has service_name so 'what agent is stuck?' is not empty",
          retry.get("service_name") == "orders-agent" and retry.get("holder"))

    agents = tool("list_agents", {}, aid, viewer=uid)
    check("list_agents still returns the fleet for the agent follow-up",
          agents.get("count") >= 1
          and any(a.get("service_name") == "orders-agent" for a in agents.get("agents") or []))

    attention = tool("get_work_items", {"status": "needs_attention"}, aid, viewer=uid)
    att_titles = set(_titles(attention))
    check("needs_attention includes named stuck + aging waiting (Sarah)",
          "Retry shipment" in att_titles and "Confirm address" in att_titles)
    check("needs_attention excludes the OTel flood",
          all(not _is_flood_title(t) for t in att_titles))

    empty_q = tool("find_tasks", {"query": ""}, aid, viewer=uid)
    check("empty find_tasks is capped and named-only",
          empty_q["count"] <= 25
          and all(not _is_flood_title(t) for t in _titles(empty_q))
          and "Reply to customer" in _titles(empty_q))
    check("empty find_tasks never returns Task from main",
          all("task from " not in t.lower() for t in _titles(empty_q)))
    check("empty find_tasks defaults to open (includes fresh waiting promis?)",
          "promis?" in _titles(empty_q))
    check("empty find_tasks defaults to open (excludes done this week)",
          "Answered a question" not in _titles(empty_q))

    listed = tool("get_work_items", {}, aid, viewer=uid)
    check("get_work_items with no status lists open named work including promis?",
          "promis?" in _titles(listed)
          and all(t.get("status") != "done" for t in listed.get("tasks") or []))
    blocking = tool("get_work_items", {"status": "blocking"}, aid, viewer=uid)
    check("status=blocking includes waiting_on_other promis? + stuck Retry",
          "promis?" in _titles(blocking) and "Retry shipment" in _titles(blocking))
    check("status=blocking excludes moving Draft reply",
          "Draft reply" not in _titles(blocking))

    if flood_ids:
        flood_story = tool("get_task_story", {"loop_id": flood_ids[0]}, aid, viewer=uid)
        check("story on untitled / Task-from-main is not-found (not a fake title)",
              flood_story.get("error") == "task not found")

    print("\n--- Ask work tools never call get_work_board ---")
    orig = database.get_work_board
    def boom(*a, **k):
        raise AssertionError("get_work_board must not run on the Ask work path")
    database.get_work_board = boom
    try:
        ov2 = tool("get_work_overview", {}, aid, viewer=uid)
        it2 = tool("get_work_items", {"status": "stuck"}, aid, viewer=uid)
        ft2 = tool("find_tasks", {"query": ""}, aid, viewer=uid)
        me2 = tool("get_waiting_on_me", {}, aid, viewer=uid)
        st2 = tool("get_task_story", {"loop_id": waiting["id"]}, aid, viewer=uid)
        check("overview survives with get_work_board broken", ov2.get("named_only") is True)
        check("items survive with get_work_board broken",
              any(t["title"] == "Retry shipment" for t in it2.get("tasks") or []))
        check("find_tasks survives with get_work_board broken",
              all(not _is_flood_title(t) for t in _titles(ft2)))
        check("waiting-on-me survives with get_work_board broken",
              me2.get("count") == 1)
        check("task story survives with get_work_board broken",
              st2.get("title") == "Confirm address")
    finally:
        database.get_work_board = orig

    print("\n--- What's stuck? cites fresh waiting_on_other (QA promis?) ---")
    r3 = c.post("/auth/signup", json={
        "email": "qa@t.com", "password": "supersecret123",
        "name": "QA", "account_type": "business", "org_name": "QA Hammocks",
    }).json()
    qa_k, qa_t = r3["api_key"], r3["token"]
    qa_h = {"Authorization": f"Bearer {qa_t}"}
    qa_aid, qa_uid = r3["org"]["id"], r3["user"]["id"]
    c.post("/org/invites", headers=qa_h, json={
        "email": "sam@t.com", "name": "Sam",
    })
    c.post("/v1/traces", json={"resourceSpans": [{
        "resource": {"attributes": kv({"service.name": "main"})},
        "scopeSpans": [{"spans": [
            sp("message_received", 400, {**t("promis?"), "trovis.loop.external_id": "qa1"}),
            sp("agent_run_complete", 300, {
                "trovis.loop.external_id": "qa1",
                "trovis.handoff.direction": "to_human",
                "trovis.handoff.target_id": "sam@t.com",
                "trovis.handoff.id": "HQA",
            }),
        ]}],
    }]}, headers={"X-Trovis-Api-Key": qa_k})
    qa_ov = tool("get_work_overview", {}, qa_aid, viewer=qa_uid)
    check("QA overview is 0 needs_attention / 1 open (fresh waiting_on_other)",
          qa_ov.get("needs_attention") == 0 and qa_ov.get("open") == 1
          and qa_ov.get("needs_you") == 0)
    check("QA overview next tells the model to cite get_work_items titles",
          "get_work_items" in (qa_ov.get("next") or "")
          and "waiting_on_other" in (qa_ov.get("next") or ""))
    qa_stuck = tool("get_work_items", {"status": "stuck"}, qa_aid, viewer=qa_uid)
    check("QA status=stuck is empty (promis? is waiting_on_other, not stuck)",
          qa_stuck.get("count") == 0)
    check("QA stuck-empty payload still names promis? as open_or_waiting",
          any(t.get("title") == "promis?" for t in qa_stuck.get("open_or_waiting") or []))
    check("QA stuck-empty hint forbids a fleet telemetry pivot",
          "telemetry" in (qa_stuck.get("hint") or "").lower())
    qa_open = tool("get_work_items", {}, qa_aid, viewer=qa_uid)
    check("QA unfiltered get_work_items names promis?",
          qa_open.get("count") == 1 and qa_open["tasks"][0]["title"] == "promis?"
          and qa_open["tasks"][0]["status"] == "waiting_on_other"
          and qa_open["tasks"][0].get("service_name") == "main")

    print("\n--- prompt prefers lean named Work ---")
    instr = asker._AGENTIC_INSTRUCTIONS
    check("instructions name get_work_overview + get_work_items",
          "get_work_overview" in instr and "get_work_items" in instr)
    check("instructions ban /work/board and Task from main flood",
          "/work/board" in instr and "Task from main" in instr)
    check("instructions tell the model not to blank on which agent is stuck",
          "do NOT" in instr and "list_agents" in instr and "service_name" in instr)
    check("instructions treat waiting_on_other as stuck/blocking/attention",
          "waiting_on_other" in instr and "what's blocking" in instr)
    check("instructions require citing get_work_items titles, not stuck-only",
          "cite" in instr.lower() and "do NOT filter status=stuck" in instr)
    check("instructions prefer named work over fleet error-rate pivots",
          "error-rate" in instr and "named open or waiting" in instr)
    check("fleet concise prompt prefers named titles over error-rate pivots",
          "waiting_on_other" in asker.SYSTEM_FLEET_CONCISE
          and "error-rate" in asker.SYSTEM_FLEET_CONCISE)

print()
if failures:
    print(f"FAILED ({len(failures)}):")
    for f in failures:
        print("  - " + f)
    raise SystemExit(1)
print("All ask-work-tool checks passed.")
os.unlink(_tmp.name)
