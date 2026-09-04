"""Ask work-record tools: read-only board/loop access for ⌘K.

Hermetic (temp SQLite, no Claude). Seeds work the same way as
test_work_summary.py — ingest via /v1/traces — then calls asker._run_tool
directly so we assert the tool payloads, not the model.

Covers:
  - account scoping (another org sees nothing)
  - is_yours / yours strip with a signed-in viewer (matches /work/summary)
  - API key / no viewer → explicit sign-in, never a fake yours list
  - find_tasks + get_task_story cite live stuck state and loop events
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


with TestClient(main.app) as c:
    r = c.post("/auth/signup", json={
        "email": "s@t.com", "password": "supersecret123",
        "name": "Alex", "account_type": "business", "org_name": "Co",
    }).json()
    K, T = r["api_key"], r["token"]
    H = {"Authorization": f"Bearer {T}"}
    uid = r["user"]["id"]
    aid = r["org"]["id"]
    c.post("/team", headers=H, json={"name": "Sarah Chen", "email": "sarah@t.com", "role": "Lead"})

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
    # orders-agent: 1 stuck (Sarah, >4h), 1 in motion
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

    summary = c.get("/work/summary", headers=H).json()

    print("\n--- get_waiting_on_me mirrors /work/summary yours ---")
    mine = tool("get_waiting_on_me", {}, aid, viewer=uid)
    check("signed in", mine.get("signed_in") is True)
    check("exactly one task waiting on the viewer",
          mine.get("count") == 1 and len(mine.get("tasks") or []) == 1)
    yours = summary.get("yours") or []
    check("matches the summary yours strip title",
          yours and mine["tasks"][0]["title"] == yours[0]["title"] == "Reply to customer")
    check("carries id + handoff_event_id to act on",
          mine["tasks"][0]["id"] == yours[0]["id"]
          and mine["tasks"][0]["handoff_event_id"] == yours[0]["handoff_event_id"]
          and mine["tasks"][0]["handoff_event_id"] is not None)
    check("holder and workflow are present",
          mine["tasks"][0].get("holder") and mine["tasks"][0].get("workflow") == "Customer service")
    check("Sarah's stuck task is NOT in yours",
          all(t["title"] != "Confirm address" for t in mine["tasks"]))

    print("\n--- no viewer: sign in, never fake is_yours ---")
    anon = tool("get_waiting_on_me", {}, aid, viewer=None)
    check("API-key path asks to sign in",
          anon.get("signed_in") is False and "sign in" in (anon.get("message") or "").lower())
    check("API-key path returns no tasks", anon.get("tasks") == [])

    overview_anon = tool("get_work_overview", {}, aid, viewer=None)
    check("overview without viewer has no yours list",
          "yours" not in overview_anon and overview_anon.get("yours_count") is None)
    check("overview without viewer still has kind/stuck rollups",
          overview_anon.get("stuck") == 1 and overview_anon.get("waiting_person") == 1)

    print("\n--- get_work_overview with viewer ---")
    overview = tool("get_work_overview", {}, aid, viewer=uid)
    kinds = {k["name"]: k for k in overview.get("kinds") or []}
    check("cs kind rollup matches summary",
          kinds["Customer service"]["waiting_person"] == 1
          and kinds["Customer service"]["in_motion"] == 1
          and kinds["Customer service"]["stuck"] == 0)
    check("orders kind has the stuck task",
          kinds["Order ops"]["stuck"] == 1)
    check("yours_count is 1 when signed in", overview.get("yours_count") == 1)

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
    check("other account: empty overview", other_over.get("total") == 0)
    check("other account: find_tasks misses this org's tasks", other_find.get("count") == 0)
    stolen = tool("get_task_story", {"loop_id": mine["tasks"][0]["id"]}, other_aid, viewer=other_uid)
    check("other account cannot read this org's task story",
          stolen.get("error") == "task not found")

    print("\n--- find_tasks + get_task_story for a stuck task ---")
    found = tool("find_tasks", {"query": "Confirm address"}, aid, viewer=uid)
    check("find_tasks locates the stuck task by title",
          found.get("count") == 1 and found["tasks"][0]["title"] == "Confirm address")
    stuck = found["tasks"][0]
    check("find_tasks includes column/state/waiting_on",
          stuck["column"] == "stuck"
          and stuck["state"] in ("stalled", "awaiting_human")
          and (stuck.get("waiting_on") or stuck.get("holder")))
    check("Confirm address is not is_yours for Alex",
          stuck.get("is_yours") is False)

    story = tool("get_task_story", {"loop_id": stuck["id"]}, aid, viewer=uid)
    check("story is the Confirm address loop",
          story.get("title") == "Confirm address" and story.get("id") == stuck["id"])
    check("story cites live stuck/waiting state",
          story.get("column") == "stuck"
          and story.get("state") in ("stalled", "awaiting_human"))
    holder_blob = " ".join(str(story.get(k) or "") for k in (
        "holder", "waiting_on", "awaiting_human_name",
    )).lower()
    check("story names Sarah as the holder/waiting_on",
          "sarah" in holder_blob)
    check("story is_yours is false for Alex (it's Sarah's)",
          story.get("is_yours") is False)
    events = story.get("events") or []
    event_blob = json.dumps(events).lower()
    check("story includes live loop events (handoff to human)",
          any(e.get("type") == "handoff_initiated" for e in events)
          or "handoff" in event_blob)
    check("story does not invent from spans/fleet — events are loop events",
          all(e.get("type") != "span" for e in events))

    by_id = tool("find_tasks", {"query": str(stuck["id"])}, aid, viewer=uid)
    check("find_tasks matches id substring",
          by_id.get("count") >= 1 and any(t["id"] == stuck["id"] for t in by_id["tasks"]))

    wf_id = kinds["Order ops"]["workflow_id"]
    scoped = tool("find_tasks", {"query": "", "workflow_id": wf_id}, aid, viewer=uid)
    check("find_tasks workflow_id filter stays inside Order ops",
          scoped["count"] >= 1
          and all(t.get("workflow") == "Order ops" for t in scoped["tasks"]))

    print("\n--- find_tasks cap ---")
    all_found = tool("find_tasks", {"query": ""}, aid, viewer=uid)
    check("find_tasks without query is capped at 25",
          all_found["count"] <= 25)

print()
if failures:
    print(f"FAILED ({len(failures)}):")
    for f in failures:
        print("  - " + f)
    raise SystemExit(1)
print("All ask-work-tool checks passed.")
os.unlink(_tmp.name)
