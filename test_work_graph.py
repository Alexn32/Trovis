"""Work Graph — the operational projection of one run, from explicit
lifecycle records only. Every adversarial case here is a way the graph could
have been made richer by being less truthful, and is not.

Run:
  TROVIS_DISABLE_PRICING_SYNC=1 python3 test_work_graph.py
"""
from __future__ import annotations

import inspect
import json
import re
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
os.environ.pop("ANTHROPIC_API_KEY", None)

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()

import database
database.SQLITE_PATH = _tmp.name

import loops
import main
import work_graph
from fastapi.testclient import TestClient

main._auto_describe = lambda *a, **k: False

failures = []


def check(label, cond):
    print(("  PASS " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


def kv(d):
    return [{"key": k, "value": {"stringValue": str(v)}} for k, v in d.items()]


NS = 1_000_000_000
NOW = time.time_ns()
_n = [0]


def sp(name, off_s, attrs, *, trace=None, span=None, parent=None, status=1, msg=None, dur_ns=5_000_000):
    _n[0] += 1
    start = NOW - off_s * NS
    s = {
        "traceId": trace or f"{_n[0]:032d}", "spanId": span or f"{_n[0]:016d}", "name": name, "kind": 1,
        "startTimeUnixNano": str(start), "endTimeUnixNano": str(start + dur_ns),
        "status": {"code": status}, "attributes": kv(attrs),
    }
    if parent:
        s["parentSpanId"] = parent
    if msg is not None:
        s["status"]["message"] = msg
    return s


USAGE = {"gen_ai.request.model": "no-such-model-xyz", "gen_ai.usage.input_tokens": "120", "gen_ai.usage.output_tokens": "30"}
GROK = {"trovis.sdk.platform": "xai"}
BUSINESS_WORDS = ("refund issued", "refund requested", "refund completed", "processed refund", "succeeded", "successful",
                  "outcome achieved", "business goal", "verified", "work started", "started")

print("shape and vocabulary")
src = inspect.getsource(work_graph)
check("no model client, no LLM dependency", not any(w in src for w in ("anthropic", "import asker", "import describer", "investigator", "openai")))
check("no account-wide read", not any(w in src for w in ("get_work_board", "get_work_summary", "get_work_items(", "list_loops", "get_home_snapshot")))
check("vocabulary is progress / handoff / wait / exception / completed — no 'started'",
      work_graph.STEP_TYPES == ("progress", "handoff", "wait", "exception", "completed"))
check("possession reuses loops.compute_loop_segments and does not define a state machine of its own",
      "compute_loop_segments" in src and "def compute_loop" not in src and "_unresolved_handoffs" not in src.replace("loops._unresolved_handoffs", ""))
check("no tool-name semantics: no provider event → business phrase table",
      not any(w in src.lower() for w in ("charge.refunded", "refund issued", "refund requested", "payment_intent.succeeded")))
check("no model output is read", "response.content" not in src and "message.content" not in src)

with TestClient(main.app) as c:
    a = c.post("/auth/signup", json={
        "email": "ada@graph.test", "password": "supersecret123",
        "name": "Ada Lovelace", "account_type": "business", "org_name": "Graph Co",
    }).json()
    b = c.post("/auth/signup", json={
        "email": "bob@graph.test", "password": "supersecret123",
        "name": "Bob", "account_type": "business", "org_name": "Other Co",
    }).json()
    KA, TA = a["api_key"], a["token"]
    HA = {"Authorization": f"Bearer {TA}"}
    HB = {"Authorization": f"Bearer {b['token']}"}
    aid = database.resolve_session(TA)["account_id"]

    def post(key, svc, spans, resource=None):
        res = {"service.name": svc}
        res.update(resource or {})
        r = c.post("/v1/traces", json={"resourceSpans": [{
            "resource": {"attributes": kv(res)},
            "scopeSpans": [{"spans": spans}],
        }]}, headers={"X-Trovis-Api-Key": key})
        assert r.status_code == 200, r.text
        return r

    def item_by_title(h, title):
        items = c.get("/work/items?limit=50", headers=h).json()["items"]
        return next(i for i in items if i["title"] == title)

    def graph(h, item_id):
        r = c.get(f"/work/items/{item_id}/graph", headers=h)
        assert r.status_code == 200, r.text
        return r.json()

    def types(g):
        return [s["type"] for s in g["steps"]]

    def no_business_words(text):
        # Provider event types are the provider's own records, kept verbatim
        # (Stripe's payment_intent.succeeded is Stripe's state, not a Trovis
        # verdict). Everything Trovis phrases itself is scanned.
        low = re.sub(r'"saas_event_type": "[^"]*"', '"saas_event_type": ""', text).lower()
        return not any(w in low for w in BUSINESS_WORDS)

    # ------------------------------------------------------------------
    # 1/2/3/4/7/12/19. Execution alone earns nothing
    # ------------------------------------------------------------------
    print("\n1–4, 7, 12, 19: execution alone earns no Work Step")
    T1 = "a" * 32
    KEY = {"trovis.loop.external_id": "order-4471", "trovis.run.id": "order-4471"}
    R = sp("agent_run", 900, {"trovis.loop.title": "Refund order #4471", "trovis.event.type": "agent_run", **KEY},
           trace=T1, span="0000000000000001", dur_ns=60 * NS)
    M1 = sp("model_call", 895, {**KEY, **USAGE, "trovis.response.content": "Refund issued to the customer. Done!"},
            trace=T1, span="0000000000000002", parent=R["spanId"])
    T_SHOP = sp("tool_call", 890, {**KEY, "trovis.tool.name": "mcp__shopify__lookup_orders_by_email"},
                trace=T1, span="0000000000000003", parent=R["spanId"])
    T_FAIL = sp("tool_call", 885, {**KEY, "trovis.tool.name": "stripe.refunds.create", "trovis.tool.success": "false"},
                trace=T1, span="0000000000000004", parent=R["spanId"], status=2, msg="Card declined")
    T_OK = sp("tool_call", 880, {**KEY, "trovis.tool.name": "stripe.refunds.create", "trovis.tool.success": "true"},
              trace=T1, span="0000000000000005", parent=R["spanId"])
    H = sp("HTTP POST", 879, {**KEY, "http.request.method": "POST", "url.full": "https://api.stripe.com/v1/refunds"},
           trace=T1, span="0000000000000006", parent=T_OK["spanId"])
    post(KA, "refunds-agent", [R, M1, T_SHOP, T_FAIL, T_OK, H], GROK)
    # A second trace in the same run.
    post(KA, "refunds-agent", [sp("model_call", 870, {**KEY, **USAGE}, trace="b" * 32)], GROK)
    item = item_by_title(HA, "Refund order #4471")
    g = graph(HA, item["id"])
    check("1. agent_run + model calls + tools → zero Work Steps", g["steps"] == [] and g["chronology"] == [])
    check("2. loop_opened alone → no 'Work started' step; it stays lifecycle bookkeeping",
          [e["type"] for e in g["lifecycle"]] == ["loop_opened"] and g["lifecycle"][0]["step_id"] is None
          and g["lifecycle"][0]["possession_only"] is False)
    check("3. stripe.refunds.create → no refund step of any wording", no_business_words(json.dumps(g)))
    check("4. business-looking model output → no step", "Refund issued" not in json.dumps(g))
    check("7. ordinary Shopify/Stripe tool usage → the agent still holds the work; no system possession",
          [s["holder_type"] for s in g["possession"]["segments"]] == ["agent"]
          and g["possession"]["current_holder"]["holder_type"] == "agent"
          and g["possession"]["current_holder"]["holder"] == "refunds-agent:main")
    check("7. tool usage is recorded as touches on the holder, not as a holder",
          {t["name"] for t in g["possession"]["segments"][0]["touches"]} == {"mcp__shopify__lookup_orders_by_email", "stripe.refunds.create"})
    check("12. the failed Stripe call stays execution-only: counted, no Work exception",
          g["summary"]["exceptions"] == 0 and g["summary"]["execution_only"]["errored_spans"] == 1
          and g["summary"]["execution_only"]["tool_call_spans"] == 3)
    check("19. two traces in one run → still zero steps", g["spans_read"] == 7 and g["summary"]["steps"] == 0)
    check("the three levels are explicit: steps 0, lifecycle 1, execution_only spans 7",
          g["summary"]["steps"] == 0 and len(g["lifecycle"]) == 1 and g["summary"]["execution_only"]["spans_read"] == 7)

    # ------------------------------------------------------------------
    # 5/9/10. Human handoff, resolution, decline
    # ------------------------------------------------------------------
    print("\n5, 9, 10: human handoff, resolution, decline")
    ASK = sp("ask_human", 860, {**KEY, "trovis.handoff.direction": "to_human",
                                "trovis.handoff.target_id": "ada@graph.test", "trovis.handoff.reason": "over limit",
                                "trovis.handoff.id": "h-1"})
    post(KA, "refunds-agent", [ASK], GROK)
    g = graph(HA, item["id"])
    hand = [s for s in g["steps"] if s["type"] == "handoff"]
    check("5. a human handoff is a visible handoff step with target, reason, id and source",
          len(hand) == 1 and hand[0]["label"] == "Handed to a person"
          and hand[0]["details"] == {"direction": "to_human", "handoff_id": "h-1", "reason": "over limit",
                                     "target_label": "Ada Lovelace", "target_id": None}
          and hand[0]["actor"] == {"type": "agent", "label": "refunds-agent:main"})
    check("5. step id is source-derived and provenance points at the event, its evidence, its execution node and its span",
          hand[0]["id"] == f"work-step:event:{hand[0]['provenance']['event_id']}"
          and hand[0]["provenance"]["evidence_id"] == hand[0]["provenance"]["execution_node_id"] == f"event:{hand[0]['provenance']['event_id']}"
          and hand[0]["provenance"]["span_id"] == ASK["spanId"] and hand[0]["provenance"]["evidence_kind"] == "handoff"
          and hand[0]["provenance"]["source_connector_id"] == "grok" and hand[0]["provenance"]["correlation"] == "explicit_key")
    check("the person's address is never echoed", "ada@graph.test" not in json.dumps(g))
    segs = g["possession"]["segments"]
    check("5. possession follows the existing semantics: the agent's segment ends, a waiting human segment opens",
          [s["holder_type"] for s in segs] == ["agent", "human"] and segs[1]["waiting"] is True
          and segs[1]["holder"] == "Ada Lovelace" and g["possession"]["current_holder"]["holder_type"] == "human")
    check("connector identity is not worker identity: actor is the worker; the connector rides in provenance only",
          "Grok" not in json.dumps(hand[0]["actor"]) and hand[0]["provenance"]["source_connector_id"] == "grok")

    detail = c.get(f"/work/items/{item['id']}", headers=HA).json()
    r = c.post(f"/loops/{item['id']}/handoffs/{detail['awaiting_handoff_event_id']}/complete", headers=HA)
    assert r.status_code == 200, r.text
    g = graph(HA, item["id"])
    check("9. the person's completion returns possession to the agent WITHOUT a standalone 'wait resolved' step",
          types(g) == ["handoff"] and [s["holder_type"] for s in g["possession"]["segments"]] == ["agent", "human", "agent"]
          and g["possession"]["current_holder"]["holder_type"] == "agent")
    done_ev = next(e for e in g["lifecycle"] if e["type"] == "handoff_completed")
    check("9. the completion is kept as a lifecycle record: human actor by resolved name, possession-only",
          done_ev["actor"] == {"type": "human", "label": "Ada Lovelace"} and done_ev["possession_only"] is True
          and done_ev["step_id"] is None and done_ev["handoff_id"] == "h-1")

    # A second handoff, declined by the person, with no reason recorded.
    ASK2 = sp("ask_human", 850, {**KEY, "trovis.handoff.direction": "to_human",
                                 "trovis.handoff.target_id": "ada@graph.test", "trovis.handoff.id": "h-2"})
    post(KA, "refunds-agent", [ASK2], GROK)
    detail = c.get(f"/work/items/{item['id']}", headers=HA).json()
    r = c.post(f"/loops/{item['id']}/handoffs/{detail['awaiting_handoff_event_id']}/decline", headers=HA)
    assert r.status_code == 200, r.text
    g = graph(HA, item["id"])
    exc = [s for s in g["steps"] if s["type"] == "exception"]
    check("10. an explicit decline is an exception step with no invented reason",
          len(exc) == 1 and exc[0]["label"] == "Handoff declined" and exc[0]["details"] == {"handoff_id": "h-2", "reason": None}
          and exc[0]["actor"]["type"] == "human" and exc[0]["provenance"]["correlation"] == "direct")
    check("10. possession after a decline returns to the prior holder (the existing rule)",
          g["possession"]["current_holder"]["holder_type"] == "agent")
    check("the handoff that was declined has no target_id and a resolved name only",
          [s for s in g["steps"] if s["details"].get("handoff_id") == "h-2" and s["type"] == "handoff"][0]["details"]["target_label"] == "Ada Lovelace")

    # ------------------------------------------------------------------
    # 6/8. Agent handoff; explicit to_system
    # ------------------------------------------------------------------
    print("\n6, 8: agent handoff, explicit to_system")
    K3 = {"trovis.loop.external_id": "tri-1"}
    post(KA, "triage-agent", [
        sp("run", 700, {"trovis.loop.title": "Triage inbox", **K3}),
        sp("delegate", 690, {**K3, "trovis.handoff.direction": "to_agent", "trovis.handoff.target_id": "research-agent:main",
                             "trovis.handoff.id": "h-3"}),
    ])
    ti = item_by_title(HA, "Triage inbox")
    g = graph(HA, ti["id"])
    hand = [s for s in g["steps"] if s["type"] == "handoff"]
    check("6. an agent handoff names the explicit target; nothing says the target acted",
          len(hand) == 1 and hand[0]["label"] == "Handed to another agent"
          and hand[0]["details"]["target_id"] == "research-agent:main" and hand[0]["details"]["target_label"] == "research-agent:main"
          and "acted" not in json.dumps(hand[0]).lower())
    check("6. possession: the target agent holds it, waiting",
          g["possession"]["current_holder"]["holder_type"] == "agent" and g["possession"]["current_holder"]["holder"] == "research-agent:main"
          and g["possession"]["current_holder"]["waiting"] is True)

    post(KA, "export-agent", [
        sp("run", 600, {"trovis.loop.title": "Nightly export", "trovis.loop.external_id": "exp-1"}),
        sp("wait", 590, {"trovis.loop.external_id": "exp-1", "trovis.handoff.direction": "to_system",
                         "trovis.handoff.target_id": "warehouse", "trovis.handoff.reason": "export queued", "trovis.handoff.id": "h-4"}),
    ])
    ei = item_by_title(HA, "Nightly export")
    g = graph(HA, ei["id"])
    waits = [s for s in g["steps"] if s["type"] == "wait"]
    check("8. an explicit to_system handoff is a system wait, with the declared system and reason",
          len(waits) == 1 and waits[0]["label"] == "Waiting on warehouse" and waits[0]["system"] == {"label": "warehouse", "provider": None, "target_id": "warehouse"}
          and waits[0]["details"]["reason"] == "export queued" and waits[0]["details"]["saas_effect"] is None)
    check("8. possession: the system holds it, waiting — only because it was declared",
          g["possession"]["current_holder"]["holder_type"] == "system" and g["possession"]["current_holder"]["holder"] == "warehouse")

    # ------------------------------------------------------------------
    # 13/14/15/16. The SaaS spine
    # ------------------------------------------------------------------
    print("\n13–16: SaaS wait, clear, stuck; worker report vs independent state")
    # The person's completion and decline above were written at wall-clock
    # time; the provider events and the close follow them, in order.
    LATER = time.time_ns() + 1 * NS
    tw = LATER
    res = database.apply_saas_loop_effect(
        aid, item["id"], provider="stripe", effect="wait", object_id="pi_4471", target_id="Stripe",
        waiting_on="payment processing", event_id="evt_proc_1", event_type="payment_intent.processing", event_time_unix=tw,
    )
    assert res["status"] == "applied", res
    g = graph(HA, item["id"])
    sw = [s for s in g["steps"] if s["type"] == "wait"]
    check("13. an explicitly correlated SaaS wait is a Work wait with external-state provenance and the provider's ids",
          len(sw) == 1 and sw[0]["label"] == "Waiting on Stripe" and sw[0]["actor"] == {"type": "system", "label": "Stripe"}
          and sw[0]["system"] == {"label": "Stripe", "provider": "stripe", "target_id": "Stripe"}
          and sw[0]["details"]["waiting_on"] == "payment processing" and sw[0]["details"]["saas_effect"] == "wait"
          and sw[0]["details"]["saas_event_type"] == "payment_intent.processing" and sw[0]["details"]["saas_object_id"] == "pi_4471"
          and sw[0]["provenance"]["evidence_kind"] == "external_state" and sw[0]["provenance"]["correlation"] == "explicit_key"
          and sw[0]["provenance"]["external_event_id"] == "evt_proc_1" and sw[0]["provenance"]["external_object_id"] == "pi_4471"
          and sw[0]["provenance"]["source_connector_id"] == "stripe")
    check("13. the SaaS event has no execution span behind it — zero span refs is correct",
          sw[0]["provenance"]["span_id"] is None and sw[0]["provenance"]["trace_id"] is None)
    check("13. possession: Stripe holds it, waiting", g["possession"]["current_holder"]["holder_type"] == "system"
          and g["possession"]["current_holder"]["holder"] == "Stripe")
    res = database.apply_saas_loop_effect(
        aid, item["id"], provider="stripe", effect="clear", object_id="pi_4471", target_id="Stripe",
        event_id="evt_ok_2", event_type="payment_intent.succeeded", event_time_unix=LATER + 5 * NS,
    )
    assert res["status"] == "applied", res
    g = graph(HA, item["id"])
    check("14. a SaaS clear resolves possession without a standalone step and without a business outcome",
          types(g).count("wait") == 1 and g["possession"]["current_holder"]["holder_type"] == "agent"
          and no_business_words(json.dumps(g["steps"])))
    clear_ev = next(e for e in g["lifecycle"] if e["type"] == "handoff_completed" and e["saas_provider"] == "stripe")
    check("14. the clear is kept as a lifecycle record with the provider's exact ids — the state, not a verdict",
          clear_ev["possession_only"] is True and clear_ev["external_event_id"] == "evt_ok_2"
          and clear_ev["saas_event_type"] == "payment_intent.succeeded" and clear_ev["actor"] == {"type": "system", "label": "Stripe"})
    res = database.apply_saas_loop_effect(
        aid, item["id"], provider="stripe", effect="stuck", object_id="pi_4471", target_id="Stripe",
        reason="Your card was declined", event_id="evt_fail_3", event_type="payment_intent.payment_failed", event_time_unix=LATER + 10 * NS,
    )
    assert res["status"] == "applied", res
    g = graph(HA, item["id"])
    stuck = [s for s in g["steps"] if s["type"] == "exception" and s["details"].get("saas_effect") == "stuck"]
    check("15. a SaaS stuck is an exception carrying only the recorded provider, event type and reason",
          len(stuck) == 1 and stuck[0]["label"] == "Stuck on Stripe" and stuck[0]["details"]["reason"] == "Your card was declined"
          and stuck[0]["details"]["saas_event_type"] == "payment_intent.payment_failed"
          and stuck[0]["provenance"]["external_event_id"] == "evt_fail_3")
    tool_reports = [n for n in g["lifecycle"] if n["type"] == "activity"]
    check("16. the worker's stripe.refunds.create reports never became external state; only Stripe's own records do",
          tool_reports == [] and all(s["provenance"]["evidence_kind"] == "external_state" for s in g["steps"] if s["system"] and s["system"]["provider"] == "stripe")
          and all(s["provenance"]["source_type"] == "system" for s in g["steps"] if s["provenance"]["evidence_kind"] == "external_state"))
    check("16. no step comes from a span: every step's evidence id is an event",
          all(s["provenance"]["evidence_id"].startswith("event:") for s in g["steps"]))

    # ------------------------------------------------------------------
    # 17/18. Closure
    # ------------------------------------------------------------------
    print("\n17, 18: closure is a record closing, never success")
    res = database.apply_saas_loop_effect(
        aid, item["id"], provider="stripe", effect="clear", object_id="pi_4471", target_id="Stripe",
        event_id="evt_ok_4", event_type="charge.refunded", event_time_unix=LATER + 15 * NS,
    )
    CLOSE = sp("done", -((LATER - NOW) // NS + 20), {**KEY, "trovis.loop.close": "done"})
    post(KA, "refunds-agent", [CLOSE], GROK)
    g = graph(HA, item["id"])
    comp = [s for s in g["steps"] if s["type"] == "completed"]
    check("17. loop_closed → 'Work record closed' with reason, closer and provenance",
          len(comp) == 1 and comp[0]["label"] == "Work record closed" and comp[0]["details"]["reason"] == "completed_by_agent"
          and comp[0]["details"]["outcome"] == "record_closed" and comp[0]["details"]["abandoned"] is False
          and comp[0]["actor"] == {"type": "agent", "label": "refunds-agent:main"} and comp[0]["provenance"]["span_id"] == CLOSE["spanId"]
          and comp[0]["provenance"]["evidence_kind"] == "completion")
    check("17. charge.refunded is kept as the provider's event type, never translated into 'Refund issued'",
          any(e["saas_event_type"] == "charge.refunded" for e in g["lifecycle"]) and no_business_words(json.dumps(g)))
    check("17. the whole graph never says succeeded / successful / verified", no_business_words(json.dumps(g)))
    check("possession chain ends at the close", g["possession"]["current_holder"] is None
          and g["possession"]["segments"][-1]["end"] is not None)
    check("23/24. chronology is (time, event id), stable across reads, and ids are source-derived",
          g["chronology"] == [s["id"] for s in g["steps"]] and graph(HA, item["id"])["chronology"] == g["chronology"]
          and all(s["id"].startswith("work-step:event:") for s in g["steps"]))
    check("summary counts agree", g["summary"]["steps"] == len(g["steps"]) and g["summary"]["handoffs"] == 2
          and g["summary"]["waits"] == 1 and g["summary"]["exceptions"] == 2 and g["summary"]["completed"] == 1 and g["summary"]["progress"] == 0)

    ab = post(KA, "sweep-agent", [sp("run", 500, {"trovis.loop.title": "Forgotten job", "trovis.loop.external_id": "old-1"})])
    fi = item_by_title(HA, "Forgotten job")
    assert database.abandon_loop(fi["id"], account_id=aid)
    g = graph(HA, fi["id"])
    comp = [s for s in g["steps"] if s["type"] == "completed"]
    check("18. an abandoned close is closure-as-abandoned, by Trovis, not success",
          len(comp) == 1 and comp[0]["label"] == "Work record closed as abandoned" and comp[0]["details"]["abandoned"] is True
          and comp[0]["details"]["reason"] == "abandoned" and comp[0]["actor"] == {"type": "system", "label": "Trovis"})

    # ------------------------------------------------------------------
    # 11. stall_detected
    # ------------------------------------------------------------------
    print("\n11: stall_detected")
    post(KA, "slow-agent", [sp("run", 400, {"trovis.loop.title": "Slow job", "trovis.loop.external_id": "slow-1"})])
    si = item_by_title(HA, "Slow job")
    with database._connect() as conn, database._cursor(conn) as cur:
        database.append_loop_event(cur, si["id"], "stall_detected", "system", "system",
                                   payload={"reason": "no activity for 4h"}, account_id=aid, event_time_unix=NOW - 390 * NS)
        database.append_loop_event(cur, si["id"], "stall_detected", "system", "system",
                                   payload={}, account_id=aid, event_time_unix=NOW - 380 * NS)
    g = graph(HA, si["id"])
    stalls = [s for s in g["steps"] if s["type"] == "exception"]
    check("11. stall_detected → exception with the recorded reason only; a reasonless stall stays reasonless",
          len(stalls) == 2 and stalls[0]["details"] == {"reason": "no activity for 4h", "detail": None}
          and stalls[1]["details"] == {"reason": None, "detail": None} and stalls[0]["actor"] == {"type": "system", "label": "Trovis"})
    check("11. a stall changes no possession (the existing rule)", g["possession"]["current_holder"]["holder_type"] == "agent")

    # ------------------------------------------------------------------
    # 20. Flat Grok Bot telemetry
    # ------------------------------------------------------------------
    print("\n20: flat Grok Bot reports")
    BOT = {"trovis.platform": "cursor-grok-bot", "trovis.connector.id": "grok-bot"}
    JK = {"trovis.loop.external_id": "grok-1"}
    post(KA, "Returns Bot", [sp("job_started", 300, {**JK, "trovis.loop.title": "Return #9", "trovis.event.type": "agent_activity",
                                                      "trovis.step.name": "job_started"})], BOT)
    post(KA, "Returns Bot", [sp("job_finished", 290, {**JK, "trovis.event.type": "agent_run_complete", "trovis.loop.close": "done",
                                                       "trovis.response.content": "Refund completed successfully for the customer."})], BOT)
    gi = item_by_title(HA, "Return #9")
    g = graph(HA, gi["id"])
    check("20. flat reports → only the closure step; no invented relationships or business steps",
          types(g) == ["completed"] and g["steps"][0]["details"]["reason"] == "completed_by_agent"
          and no_business_words(json.dumps(g)))
    check("20. worker is 'Returns Bot:main'; connector grok-bot rides in provenance",
          g["steps"][0]["actor"]["label"] == "Returns Bot:main" and g["steps"][0]["provenance"]["source_connector_id"] == "grok-bot")

    # ------------------------------------------------------------------
    # 21. Malformed records fail soft
    # ------------------------------------------------------------------
    print("\n21: malformed records")
    post(KA, "odd-agent", [sp("run", 200, {"trovis.loop.title": "Odd job", "trovis.loop.external_id": "odd-1"})])
    oi = item_by_title(HA, "Odd job")
    with database._connect() as conn, database._cursor(conn) as cur:
        database.append_loop_event(cur, oi["id"], "handoff_initiated", "agent", "odd-agent:main",
                                   payload={"direction": "to_human"}, account_id=aid, event_time_unix=NOW - 195 * NS)
        database.append_loop_event(cur, oi["id"], "handoff_initiated", "system", "stripe",
                                   payload={"direction": "to_system", "saas_provider": "stripe"}, account_id=aid, event_time_unix=NOW - 194 * NS)
        database.append_loop_event(cur, oi["id"], "handoff_initiated", "agent", "odd-agent:main",
                                   payload={"direction": "sideways"}, account_id=aid, event_time_unix=NOW - 193 * NS)
        cur.execute("UPDATE loop_events SET payload = '{broken json' WHERE loop_id = ? AND type = 'loop_opened'", (oi["id"],))
        database.append_loop_event(cur, oi["id"], "handoff_declined", "human", "nobody@elsewhere.test",
                                   payload={}, account_id=aid, event_time_unix=NOW - 192 * NS)
    r = c.get(f"/work/items/{oi['id']}/graph", headers=HA)
    check("21. malformed payloads and missing fields never crash the endpoint", r.status_code == 200)
    if r.status_code == 200:
        g = r.json()
        st = g["steps"]
        check("21. missing target → 'a person', no address; missing provider ids → None; unknown direction → a bare handoff",
              st[0]["details"]["target_label"] == "a person" and st[0]["details"]["target_id"] is None
              and st[1]["type"] == "wait" and st[1]["details"]["saas_object_id"] is None and st[1]["provenance"]["external_event_id"] is None
              and st[2]["type"] == "handoff" and st[2]["label"] == "Handed off" and st[2]["details"]["target_label"] is None)
        check("21. a person from nowhere is 'a person' — never their address, never an invented name",
              st[3]["actor"] == {"type": "human", "label": "a person"} and "nobody@elsewhere.test" not in json.dumps(g))
        check("21. possession stays a chain (unknown holders read generically), no guessed names",
              "nobody@elsewhere.test" not in json.dumps(g["possession"]) and all(s["holder"] for s in g["possession"]["segments"]))

    # ------------------------------------------------------------------
    # 22/25. Scoping, boundedness, contract
    # ------------------------------------------------------------------
    print("\n22, 25: account scoping and boundedness")
    check("22. another account's session gets 404, never 403", c.get(f"/work/items/{item['id']}/graph", headers=HB).status_code == 404)
    check("22. a missing item is 404", c.get("/work/items/99999/graph", headers=HA).status_code == 404)
    saved = database._WORK_EXECUTION_SPAN_LIMIT
    database._WORK_EXECUTION_SPAN_LIMIT = 3
    try:
        g = graph(HA, item["id"])
        check("25. a bounded span read is disclosed with its bound; the steps are still all there (events read whole)",
              g["bounded"] is True and g["span_limit"] == 3 and g["spans_read"] == 3 and g["summary"]["execution_only"]["bounded"] is True
              and g["summary"]["steps"] == 6)
    finally:
        database._WORK_EXECUTION_SPAN_LIMIT = saved
    g = graph(HA, item["id"])
    check("25. the unbounded read says so", g["bounded"] is False and g["span_limit"] == saved)

    # Possession parity with the canonical chain over the Run detail's stream.
    with database._connect() as conn, database._cursor(conn) as cur:
        canonical = loops.compute_loop_segments(database._fetch_loop_stream(cur, item["id"], full=True))
    ours = g["possession"]["segments"]
    check("possession parity: same segment count, holder types, waiting flags and boundaries as the canonical chain",
          len(ours) == len(canonical)
          and [s["holder_type"] for s in ours] == [s["holder_type"] for s in canonical]
          and [s["waiting"] for s in ours] == [bool(s["waiting"]) for s in canonical]
          and [s["start"] for s in ours] == [database._ns_to_iso(s["start_ns"]) for s in canonical]
          and [s["end"] for s in ours] == [database._ns_to_iso(s["end_ns"]) if s["end_ns"] else None for s in canonical])
    check("Evidence and Execution ids line up: every step's evidence id is an Evidence record and an Execution node",
          {s["provenance"]["evidence_id"] for s in g["steps"]}
          <= {e["id"] for e in c.get(f"/work/items/{item['id']}/evidence", headers=HA).json()["evidence"]}
          and {s["provenance"]["execution_node_id"] for s in g["steps"]}
          <= {n["id"] for n in c.get(f"/work/items/{item['id']}/execution", headers=HA).json()["nodes"]})

print()
if failures:
    print(f"{len(failures)} FAILED:")
    for f in failures:
        print("  -", f)
    raise SystemExit(1)
print("all work-graph checks passed")
