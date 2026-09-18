"""Execution Graph — the technical execution underneath one run, from stored
truth only: structure from recorded parents, chronology from persisted time,
types with a stated basis, and nothing invented.

Run:
  TROVIS_DISABLE_PRICING_SYNC=1 python3 test_work_execution.py
"""
from __future__ import annotations

import inspect
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
os.environ.pop("ANTHROPIC_API_KEY", None)

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()

import database
database.SQLITE_PATH = _tmp.name

import main
import work_execution
from fastapi.testclient import TestClient

main._auto_describe = lambda *a, **k: False

failures = []


def check(label, cond):
    print(("  PASS " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


def kv(d):
    out = []
    for k, v in d.items():
        if isinstance(v, bool):
            out.append({"key": k, "value": {"boolValue": v}})
        elif isinstance(v, int):
            out.append({"key": k, "value": {"intValue": str(v)}})
        else:
            out.append({"key": k, "value": {"stringValue": str(v)}})
    return out


NS = 1_000_000_000
NOW = time.time_ns()
_n = [0]


def sp(name, off_s, attrs, *, trace=None, span=None, parent=None, status=1, msg=None,
       dur_ns=5_000_000, kind=1):
    """One OTLP/JSON span. `trace`/`span`/`parent` are hex ids; defaults give
    every span its own trace so a shared one must be asked for."""
    _n[0] += 1
    start = NOW - off_s * NS
    s = {
        "traceId": trace or f"{_n[0]:032d}", "spanId": span or f"{_n[0]:016d}", "name": name, "kind": kind,
        "startTimeUnixNano": str(start), "endTimeUnixNano": str(start + dur_ns),
        "status": {"code": status}, "attributes": kv(attrs),
    }
    if parent:
        s["parentSpanId"] = parent
    if msg is not None:
        s["status"]["message"] = msg
    return s


USAGE = {"gen_ai.request.model": "no-such-model-xyz", "gen_ai.usage.input_tokens": 120, "gen_ai.usage.output_tokens": 30}
GROK = {"trovis.sdk.platform": "xai"}
KEY = {"trovis.loop.external_id": "order-4471", "trovis.run.id": "order-4471"}

print("shape and vocabulary")
src = inspect.getsource(work_execution)
check("39. no model client, no LLM dependency in the execution module", not any(
    w in src for w in ("anthropic", "import asker", "import describer", "investigator", "openai")))
check("40. no global Work summary / board / item-list read", not any(
    w in src for w in ("get_work_board", "get_work_summary", "get_work_items(", "list_loops", "get_home_snapshot")))
check("node types are the audited eight, 'other' included", work_execution.NODE_TYPES == (
    "worker", "model", "tool", "system", "handoff", "wait", "completion", "other"))
for banned in ("retry", "retries", "verified", "confirmed", "success_rate", "score", "confidence"):
    check(f"vocabulary never says '{banned}'", banned not in " ".join(
        work_execution.NODE_TYPES + work_execution.PARENT_STATUSES + work_execution.CLASSIFICATION_BASES + work_execution.STATUSES))
for mod in ("home_snapshot", "investigation_tools", "findings", "investigator", "pulse"):
    text = open(f"/home/user/Trovis/{mod}.py", encoding="utf-8").read() if os.path.exists(f"/home/user/Trovis/{mod}.py") else ""
    check(f"40. {mod}.py never reads the execution graph", "work_execution" not in text and "execution_rows" not in text)

with TestClient(main.app) as c:
    a = c.post("/auth/signup", json={
        "email": "ada@exec.test", "password": "supersecret123",
        "name": "Ada Lovelace", "account_type": "business", "org_name": "Exec Co",
    }).json()
    b = c.post("/auth/signup", json={
        "email": "bob@exec.test", "password": "supersecret123",
        "name": "Bob", "account_type": "business", "org_name": "Other Co",
    }).json()
    KA, TA = a["api_key"], a["token"]
    KB, TB = b["api_key"], b["token"]
    HA = {"Authorization": f"Bearer {TA}"}
    HB = {"Authorization": f"Bearer {TB}"}
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

    def execution(h, item_id):
        return c.get(f"/work/items/{item_id}/execution", headers=h)

    def graph(h, item_id):
        r = execution(h, item_id)
        assert r.status_code == 200, r.text
        body = r.json()
        return body, {n["id"]: n for n in body["nodes"]}

    def node_of(nodes, span):
        return nodes[f"span:{span['spanId']}"]

    # ------------------------------------------------------------------
    # Run 1 — the refund run: one trace with a real tree, two Grok workers,
    # a Grok Bot, a human handoff, a Stripe wait/clear, a close.
    # ------------------------------------------------------------------
    print("\nrun 1: structure, chronology, identity, classification")
    T1 = "a" * 32
    R = sp("agent_run", 900, {"trovis.loop.title": "Refund order #4471", **KEY},
           trace=T1, span="0000000000000001", dur_ns=60 * NS)
    M1 = sp("model_call", 895, {**KEY, **USAGE, "gen_ai.system": "xai"},
            trace=T1, span="0000000000000002", parent=R["spanId"], kind=3)
    T_TOOL = sp("tool_call", 890, {**KEY, "trovis.tool.name": "mcp__shopify__lookup_orders_by_email",
                                    "trovis.tool.call_id": "call_1"},
                trace=T1, span="0000000000000003", parent=R["spanId"])
    H1 = sp("HTTP GET", 889, {**KEY, "http.request.method": "GET", "url.full": "https://api.shopify.com/orders",
                              "http.response.status_code": 200},
            trace=T1, span="0000000000000004", parent=T_TOOL["spanId"], kind=3)
    M2 = sp("model_call", 885, {**KEY, "gen_ai.request.model": "no-such-model-xyz",
                                "gen_ai.usage.input_tokens": 0, "gen_ai.usage.output_tokens": 0},
            trace=T1, span="0000000000000005", parent=R["spanId"], kind=3)
    T_ERR = sp("tool_call", 880, {**KEY, "trovis.tool.name": "stripe.refunds.create"},
               trace=T1, span="0000000000000006", parent=R["spanId"], status=2, msg="Card declined\nstack…")
    T_AGAIN = sp("tool_call", 875, {**KEY, "trovis.tool.name": "stripe.refunds.create"},
                 trace=T1, span="0000000000000007", parent=R["spanId"])
    PLAN = sp("plan", 872, {**KEY, "gen_ai.request.model": "no-such-model-xyz"},
              trace=T1, span="0000000000000008", parent=R["spanId"])
    DONE = sp("agent_run_complete", 870, {**KEY, **USAGE, "trovis.event.type": "agent_run_complete",
                                          "trovis.run.cost_usd": "0.0042", "trovis.run.success": True},
              trace=T1, span="0000000000000009", parent=R["spanId"])
    # A model name in the RESOURCE proves nothing about any span.
    post(KA, "refunds-agent", [R, M1, T_TOOL, H1, M2, T_ERR, T_AGAIN, PLAN, DONE],
         {**GROK, "gen_ai.request.model": "resource-level-model"})
    # Second worker on the same connector; own trace, no parent.
    ASSIST = sp("assist", 868, {**KEY, "trovis.agent.id": "reviewer"})
    post(KA, "refunds-agent", [ASSIST], GROK)
    # Grok Bot — a different connector, same worker label, own trace, no parent.
    BOT = sp("report", 866, {**KEY, "trovis.tool.name": "refund_customer"})
    post(KA, "refunds-agent", [BOT], {"trovis.platform": "cursor-grok-bot", "trovis.connector.id": "grok-bot"})

    item = item_by_title(HA, "Refund order #4471")
    body, nodes = graph(HA, item["id"])

    check("1. another account's session gets 404, never 403", execution(HB, item["id"]).status_code == 404)
    check("response carries item_id and the trace ids", body["item_id"] == item["id"] and T1 in body["trace_ids"])
    check("7. three traces in one run are all named", len(body["trace_ids"]) == 3)

    # Structure
    r_n, m1, tt, h1, m2, terr, tagain, plan, done = (node_of(nodes, s) for s in (R, M1, T_TOOL, H1, M2, T_ERR, T_AGAIN, PLAN, DONE))
    check("3. parent/child preserved: model under the agent span", m1["parent_id"] == r_n["id"]
          and m1["provenance"]["parent_status"] == "attached")
    check("3. nested: HTTP span under the tool span, tool span under the agent span",
          h1["parent_id"] == tt["id"] and tt["parent_id"] == r_n["id"])
    check("3. the agent span is a root with no parent recorded", r_n["parent_id"] is None
          and r_n["provenance"]["parent_status"] == "none" and r_n["id"] in body["roots"])
    check("the raw parent id is kept verbatim in provenance", h1["provenance"]["parent_span_id"] == T_TOOL["spanId"])

    # Chronology
    chron = body["chronology"]
    body2, _ = graph(HA, item["id"])
    check("4. chronology is deterministic across reads", chron == body2["chronology"])
    check("4. chronology follows persisted start time", chron.index(r_n["id"]) < chron.index(m1["id"]) < chron.index(tt["id"])
          < chron.index(h1["id"]) < chron.index(m2["id"]) < chron.index(terr["id"]) < chron.index(tagain["id"]))
    check("4. nodes are listed in chronology order", [n["id"] for n in body["nodes"]] == chron)
    check("5. chronology creates no parentage: the second stripe call follows the first in time but is a child of the agent span",
          chron.index(tagain["id"]) > chron.index(terr["id"]) and tagain["parent_id"] == r_n["id"])
    bot = node_of(nodes, BOT)
    assist = node_of(nodes, ASSIST)
    check("5. a root-level span stays a root although it occurs after every span of the tree",
          bot["parent_id"] is None and chron.index(bot["id"]) > chron.index(done["id"]))
    check("6. multiple roots: the agent span, the reviewer's span, the Bot's report",
          {r_n["id"], assist["id"], bot["id"]} <= set(body["roots"]))
    check("roots are listed in chronology order", body["roots"] == [i for i in chron if i in set(body["roots"])])

    # Identity
    check("13. worker and connector are separate fields on every span node",
          r_n["worker"]["label"] == "refunds-agent:main" and r_n["connector"]["id"] == "grok"
          and r_n["connector"]["method"] == "sdk")
    check("14. two workers on the same connector remain two workers",
          assist["worker"]["label"] == "refunds-agent:reviewer" and assist["connector"]["id"] == "grok"
          and assist["worker"]["label"] != r_n["worker"]["label"])
    check("15. Grok Bot is a different connector from Grok, for the same worker label",
          bot["connector"]["id"] == "grok-bot" and bot["worker"]["label"] == r_n["worker"]["label"]
          and bot["connector"]["id"] != r_n["connector"]["id"])

    # Classification
    check("17. a named tool span is a tool, verbatim, with the MCP triple split deterministically",
          tt["type"] == "tool" and tt["tool"]["name"] == "mcp__shopify__lookup_orders_by_email"
          and tt["tool"]["mcp_server"] == "shopify" and tt["tool"]["display_name"] == "lookup_orders_by_email"
          and tt["tool"]["identifier"] == "mcp__shopify__lookup_orders_by_email" and tt["tool"]["call_id"] == "call_1"
          and tt["provenance"]["classification_basis"] == "span_name")
    ATTR_ONLY = sp("do_thing", 871, {**KEY, "trovis.tool.name": "web_search"}, trace=T1, span="000000000000000a", parent=R["spanId"])
    post(KA, "refunds-agent", [ATTR_ONLY], GROK)
    _, nodes = graph(HA, item["id"])
    check("17. a span whose only tool signal is the tool attribute is a tool on that basis",
          node_of(nodes, ATTR_ONLY)["type"] == "tool" and node_of(nodes, ATTR_ONLY)["tool"]["name"] == "web_search"
          and node_of(nodes, ATTR_ONLY)["provenance"]["classification_basis"] == "tool_attribute")
    check("17. a dotted tool name stays as it is", terr["tool"]["name"] == "stripe.refunds.create"
          and terr["tool"]["display_name"] == "stripe.refunds.create" and terr["tool"]["mcp_server"] is None)
    check("18. an unnamed span with no basis is 'other', not a worker or a model",
          r_n["type"] == "other" and r_n["provenance"]["classification_basis"] == "none")
    check("18. a span merely NAMED agent_run by some exporter, with no Trovis event type, is still 'other'",
          r_n["label"] == "agent_run" and r_n["type"] == "other")
    RUN_ROOT = sp("agent_run", 867, {**KEY, "trovis.event.type": "agent_run"}, trace=T1, span="000000000000000b")
    post(KA, "refunds-agent", [RUN_ROOT], GROK)
    _, nodes = graph(HA, item["id"])
    rr = node_of(nodes, RUN_ROOT)
    check("the doors' explicit trovis.event.type=agent_run classifies as a worker lifecycle node, by event type only",
          rr["type"] == "worker" and rr["provenance"]["classification_basis"] == "event_type"
          and rr["provenance"]["event_type"] == "agent_run")
    check("19. model classification from the span's own usage / name — not from a model name in the resource",
          m1["type"] == "model" and m1["provenance"]["classification_basis"] == "span_name"
          and r_n["model"] is None and r_n["type"] != "model")
    check("19. a model NAME on a span that carried no usage classifies nothing (type other), the name is still exposed",
          plan["type"] == "other" and plan["model"]["name"] == "no-such-model-xyz")
    check("19. the run-complete span carries usage but is a worker lifecycle node by its explicit event type",
          done["type"] == "worker" and done["provenance"]["classification_basis"] == "event_type"
          and done["usage"]["total_tokens"] == 150)
    check("20. an HTTP-shaped span is 'other' — not a system action, not a tool",
          h1["type"] == "other" and h1["tool"] is None and h1["provenance"]["span_kind"] == "client")
    check("m1 names the model and provider from its own attributes",
          m1["model"] == {"name": "no-such-model-xyz", "provider": "xai"})

    # Errors, repeats
    check("21. an explicit error is preserved: status error, first line of the message",
          terr["status"] == "error" and terr["error"] == "Card declined")
    check("21. an ok span has no error", tagain["status"] == "ok" and tagain["error"] is None)
    check("22. a repeated similar span is just another span — nothing says retry",
          tagain["type"] == "tool" and "retry" not in json.dumps(body).lower())
    check("duration is the span's own, in ms", m1["duration_ms"] == 5.0 and r_n["duration_ms"] == 60000.0)

    # Usage and cost (PR 211 semantics)
    check("27. usage known on a usage span, from its own counts",
          m1["usage"] == {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150,
                          "cache_creation_input_tokens": None, "cache_read_input_tokens": None})
    check("28. zero-token observed usage is 0, not absent", m2["usage"] is not None and m2["usage"]["total_tokens"] == 0)
    check("29. a span with no usage attributes has usage None", r_n["usage"] is None and tt["usage"] is None)
    check("30. the SDK-reported run cost is known on the span that reported it",
          done["cost"] == {"known": True, "source": "reported", "amount_usd": 0.0042})
    check("32. usage spans inside that run total are covered: known, source covered, no amount",
          m1["cost"] == {"known": True, "source": "covered", "amount_usd": None}
          and m2["cost"] == {"known": True, "source": "covered", "amount_usd": None})
    check("33. the run total is not distributed: non-usage spans carry no cost at all",
          r_n["cost"] is None and tt["cost"] is None and h1["cost"] is None and terr["cost"] is None)
    check("no node says $0 for an unknown cost", all(
        n["cost"] is None or n["cost"]["source"] in ("reported", "estimated", "covered") for n in body["nodes"]))

    # Provenance
    check("provenance names the record, ids and the recorded correlation",
          m1["provenance"]["record"] == "span" and m1["provenance"]["span_id"] == M1["spanId"]
          and m1["provenance"]["trace_id"] == T1 and m1["provenance"]["correlation"] == "explicit_key"
          and m1["provenance"]["loop_link"] in ("key_batch", "key_open", "key_created"))
    check("34. a tool span's evidence kind is action_reported — the worker's report, never external_state",
          tt["provenance"]["evidence_kind"] == "action_reported" and terr["provenance"]["evidence_kind"] == "action_reported"
          and all(n["provenance"]["evidence_kind"] != "external_state" for n in body["nodes"] if n["provenance"]["record"] == "span"))
    check("a non-tool span's evidence kind is execution", m1["provenance"]["evidence_kind"] == "execution")

    # ------------------------------------------------------------------
    # Handoffs, waits, system state, completion — from existing truth
    # ------------------------------------------------------------------
    print("\nrun 1: handoffs, waits, external state, completion")
    # Stripe first (its events carry their own provider times), then the
    # person (whose completion is stamped at the real clock), then the close.
    tw = NOW - 860 * NS
    res = database.apply_saas_loop_effect(
        aid, item["id"], provider="stripe", effect="wait", object_id="pi_4471",
        target_id="Stripe", waiting_on="payment processing",
        event_id="evt_proc_1", event_type="payment_intent.processing", event_time_unix=tw,
    )
    assert res["status"] == "applied", res
    res = database.apply_saas_loop_effect(
        aid, item["id"], provider="stripe", effect="clear", object_id="pi_4471",
        target_id="Stripe", event_id="evt_ok_2", event_type="payment_intent.succeeded",
        event_time_unix=NOW - 855 * NS,
    )
    assert res["status"] == "applied", res
    ASK = sp("ask_human", 850, {**KEY, "trovis.handoff.direction": "to_human",
                                "trovis.handoff.target_id": "ada@exec.test", "trovis.handoff.reason": "over limit"})
    post(KA, "refunds-agent", [ASK], GROK)
    detail = c.get(f"/work/items/{item['id']}", headers=HA).json()
    r = c.post(f"/loops/{item['id']}/handoffs/{detail['awaiting_handoff_event_id']}/complete", headers=HA)
    assert r.status_code == 200, r.text
    CLOSE = sp("done", 840, {**KEY, "trovis.loop.close": "done"})
    post(KA, "refunds-agent", [CLOSE], GROK)

    body, nodes = graph(HA, item["id"])
    events = [n for n in body["nodes"] if n["provenance"]["record"] == "loop_event"]
    handoffs = [n for n in events if n["type"] == "handoff"]
    waits = [n for n in events if n["type"] == "wait"]
    systems = [n for n in events if n["type"] == "system"]
    completions = [n for n in events if n["type"] == "completion"]

    init = next(n for n in handoffs if n["event"]["type"] == "handoff_initiated")
    check("23. the agent's to_human handoff is a handoff node from the stored event, tied to its span",
          init["event"]["direction"] == "to_human" and init["provenance"]["span_id"] == ASK["spanId"]
          and init["worker"]["label"] == "refunds-agent:main" and init["connector"]["id"] == "grok"
          and init["provenance"]["evidence_kind"] == "handoff")
    check("23. the person is named by resolved name, never by address",
          init["event"]["target_label"] == "Ada Lovelace" and init["event"]["target_id"] is None
          and "ada@exec.test" not in json.dumps(body))
    comp = next(n for n in handoffs if n["event"]["type"] == "handoff_completed")
    check("23. the person's completion is a handoff node with a human actor and direct correlation",
          comp["event"]["actor"] == {"type": "human", "label": "Ada Lovelace"}
          and comp["provenance"]["correlation"] == "direct" and comp["worker"] is None)
    check("events are never children of spans; the span is provenance", all(n["parent_id"] is None for n in events))

    check("24. a SaaS wait is a wait node: to_system, the provider, what it waits on",
          len(waits) == 1 and waits[0]["event"]["direction"] == "to_system"
          and waits[0]["event"]["provider"] == "stripe" and waits[0]["event"]["waiting_on"] == "payment processing"
          and waits[0]["event"]["provider_object_id"] == "pi_4471" and waits[0]["event"]["effect"] == "wait")
    check("24. the wait is independently sourced: system actor, stripe connector, external_state",
          waits[0]["event"]["actor"]["type"] == "system" and waits[0]["connector"]["id"] == "stripe"
          and waits[0]["provenance"]["evidence_kind"] == "external_state")
    check("35. the clearing event is a system node — Stripe's own report with its exact ids",
          len(systems) == 1 and systems[0]["event"]["provider_event_id"] == "evt_ok_2"
          and systems[0]["event"]["provider_event_type"] == "payment_intent.succeeded"
          and systems[0]["event"]["effect"] == "clear" and systems[0]["provenance"]["evidence_kind"] == "external_state"
          and systems[0]["provenance"]["correlation"] == "explicit_key")
    check("35. the system node has no worker: it is not something the worker reported", systems[0]["worker"] is None)
    check("25. the ordinary stripe tool call stays a tool node — no wait, no handoff, no possession",
          node_of(nodes, T_ERR)["type"] == "tool" and node_of(nodes, T_AGAIN)["type"] == "tool"
          and not any(n["event"] and n["event"].get("target_id") == "stripe.refunds.create" for n in events))
    check("25. exactly one wait exists, and it is the declared SaaS one", len(waits) == 1)
    check("26. the close is a completion node: the record closed, by the agent, with the stored reason — no outcome words",
          len(completions) == 1 and completions[0]["event"]["reason"] == "completed_by_agent"
          and completions[0]["provenance"]["span_id"] == CLOSE["spanId"] and completions[0]["label"] == "loop_closed"
          and not any(w in json.dumps(completions[0]).lower() for w in ("succeeded", "verified", "outcome")))
    ch = body["chronology"]
    check("chronology mixes spans and events by persisted time",
          ch.index(waits[0]["id"]) < ch.index(systems[0]["id"])
          < min(ch.index(node_of(nodes, ASK)["id"]), ch.index(init["id"]))
          and max(ch.index(node_of(nodes, ASK)["id"]), ch.index(init["id"])) < ch.index(completions[0]["id"]))
    # The handoff event is stamped with its span's own time: an exact tie,
    # broken by node id so the order is stable across reads.
    check("ties in time are broken by node id, deterministically",
          init["started_at"] == node_of(nodes, ASK)["started_at"]
          and ch.index(init["id"]) == ch.index(node_of(nodes, ASK)["id"]) - 1)
    check("summary counts agree with the nodes", body["summary"]["nodes"] == len(body["nodes"])
          and body["summary"]["by_type"]["wait"] == 1 and body["summary"]["by_type"]["completion"] == 1)

    # ------------------------------------------------------------------
    # Run 2 — generic OTEL, imperfect parentage: missing, outside, malformed,
    # self, cycle; unknown cost.
    # ------------------------------------------------------------------
    print("\nrun 2: generic OTEL and imperfect parentage")
    T2 = "b" * 32
    K2 = {"trovis.loop.external_id": "sync-1"}
    ROOT2 = sp("run", 700, {"trovis.loop.title": "Nightly sync", **K2}, trace=T2, span="1000000000000001")
    ORPHAN = sp("step", 699, K2, trace=T2, span="1000000000000002", parent="f" * 16)
    CROSS = sp("step", 698, K2, trace=T1, span="1000000000000003", parent=R["spanId"])
    MALFORMED = sp("step", 697, K2, trace=T2, span="1000000000000004", parent="not-a-hex-id!!")
    SELF = sp("step", 696, K2, trace=T2, span="1000000000000005", parent="1000000000000005")
    CX = sp("loop_a", 695, K2, trace=T2, span="1000000000000006", parent="1000000000000007")
    CY = sp("loop_b", 694, K2, trace=T2, span="1000000000000007", parent="1000000000000006")
    CZ = sp("under_a", 693, K2, trace=T2, span="1000000000000008", parent="1000000000000006")
    UNPRICED = sp("model_call", 692, {**K2, **USAGE}, trace=T2, span="1000000000000009", parent=ROOT2["spanId"])
    post(KA, "sync-agent", [ROOT2, ORPHAN, CROSS, MALFORMED, SELF, CX, CY, CZ, UNPRICED])
    item2 = item_by_title(HA, "Nightly sync")
    body2, nodes2 = graph(HA, item2["id"])

    check("2. one run only: run 2's spans are not in run 1, and run 1's are not in run 2",
          f"span:{ROOT2['spanId']}" not in nodes and f"span:{R['spanId']}" not in nodes2)
    check("16. unstamped telemetry stays generic OpenTelemetry",
          node_of(nodes2, ROOT2)["connector"] == {"id": "custom-otel", "method": "otel"})
    orphan = node_of(nodes2, ORPHAN)
    check("8. a parent that was never ingested: root, outside_read_set, raw id kept",
          orphan["parent_id"] is None and orphan["provenance"]["parent_status"] == "outside_read_set"
          and orphan["provenance"]["parent_span_id"] == "f" * 16 and orphan["id"] in body2["roots"])
    cross = node_of(nodes2, CROSS)
    check("9. a parent that exists in the DB but in another run is outside this run's read set — never attached across runs",
          cross["parent_id"] is None and cross["provenance"]["parent_status"] == "outside_read_set"
          and cross["provenance"]["parent_span_id"] == R["spanId"])
    check("11. a malformed parent reference does not crash and is not attached",
          node_of(nodes2, MALFORMED)["provenance"]["parent_status"] == "outside_read_set")
    check("11. a self-referencing parent is named as such and is a root",
          node_of(nodes2, SELF)["provenance"]["parent_status"] == "self_reference" and node_of(nodes2, SELF)["parent_id"] is None)
    cx, cy, cz = node_of(nodes2, CX), node_of(nodes2, CY), node_of(nodes2, CZ)
    check("12. a two-span cycle is broken exactly once, deterministically at the later span",
          body2["cycles_broken"] == 1 and cy["parent_id"] is None and cy["provenance"]["parent_status"] == "cycle_broken"
          and cx["parent_id"] == cy["id"] and cx["provenance"]["parent_status"] == "attached")
    check("12. the node under the cycle keeps its real parent", cz["parent_id"] == cx["id"])

    def depth(nid, seen=()):
        n = nodes2[nid]
        if n["parent_id"] is None:
            return 0
        assert nid not in seen, "cycle survived"
        return 1 + depth(n["parent_id"], seen + (nid,))
    check("12. every chain terminates", all(depth(nid) >= 0 for nid in nodes2))
    check("12. the same read twice breaks the same edge", graph(HA, item2["id"])[1][cy["id"]]["parent_id"] is None)
    unpriced = node_of(nodes2, UNPRICED)
    check("31. usage observed but the model is unpriced and no run total covers it: cost None, usage present",
          unpriced["usage"]["total_tokens"] == 150 and unpriced["cost"] is None)
    check("summary reports parent statuses honestly", body2["summary"]["by_parent_status"]["outside_read_set"] == 3
          and body2["summary"]["by_parent_status"]["self_reference"] == 1
          and body2["summary"]["by_parent_status"]["cycle_broken"] == 1)

    # 10. historical rows: parent id and link never recorded
    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute("UPDATE spans SET parent_span_id = NULL, loop_link = NULL WHERE span_id = ?", (UNPRICED["spanId"],))
    _, nodes2 = graph(HA, item2["id"])
    hist = node_of(nodes2, UNPRICED)
    check("10. a historical row with no parent id and no link: parent none, correlation None — not rebuilt",
          hist["parent_id"] is None and hist["provenance"]["parent_status"] == "none"
          and hist["provenance"]["correlation"] is None and hist["provenance"]["loop_link"] is None)

    # ------------------------------------------------------------------
    # Bounded read, empty run, imperfect telemetry
    # ------------------------------------------------------------------
    print("\nbounded, empty, imperfect")
    saved = database._WORK_EXECUTION_SPAN_LIMIT
    database._WORK_EXECUTION_SPAN_LIMIT = 3
    try:
        bb, bn = graph(HA, item["id"])
        check("36. a bounded read says so, names the bound and how many it read",
              bb["bounded"] is True and bb["span_limit"] == 3 and bb["spans_read"] == 3)
        check("36. the bounded prefix is the oldest spans; a parent past the bound is outside the read set",
              set(bn) >= {f"span:{R['spanId']}", f"span:{M1['spanId']}", f"span:{T_TOOL['spanId']}"}
              and f"span:{H1['spanId']}" not in bn)
        check("36. events are still read whole", bb["events_read"] == len(body["nodes"]) - len([n for n in body["nodes"] if n["provenance"]["record"] == "span"]) + 1)
    finally:
        database._WORK_EXECUTION_SPAN_LIMIT = saved
    full, _ = graph(HA, item["id"])
    check("36. the unbounded read is not bounded", full["bounded"] is False and full["span_limit"] == saved)

    EMPTY = sp("run", 300, {"trovis.loop.title": "Ghost run", "trovis.loop.external_id": "ghost-1"})
    post(KA, "ghost-agent", [EMPTY])
    ghost = item_by_title(HA, "Ghost run")
    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute("DELETE FROM spans WHERE span_id = ?", (EMPTY["spanId"],))
    eb, en = graph(HA, ghost["id"])
    check("37. a run with no spans is an empty graph, not an error — and loop_opened is not a node",
          eb["nodes"] == [] and eb["roots"] == [] and eb["chronology"] == [] and eb["trace_ids"] == []
          and eb["spans_read"] == 0 and eb["events_read"] == 1)

    BAD = sp("weird", 200, {"trovis.loop.title": "Broken telemetry", "trovis.loop.external_id": "bad-1"},
             span="2000000000000001", dur_ns=-3 * NS, kind=99, status=7)
    DUP = sp("weird-again", 199, {"trovis.loop.external_id": "bad-1"}, trace=BAD["traceId"], span="2000000000000001")
    post(KA, "bad-agent", [BAD, DUP])
    bad = item_by_title(HA, "Broken telemetry")
    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute("UPDATE spans SET attributes = 'not json', resource_attributes = '[1,2]', status_message = NULL "
                    "WHERE span_id = ? AND span_name = 'weird'", (BAD["spanId"],))
        cur.execute("UPDATE loop_events SET payload = '{broken' WHERE loop_id = ?", (bad["id"],))
    r = execution(HA, bad["id"])
    check("38. imperfect telemetry never crashes the endpoint", r.status_code == 200)
    if r.status_code == 200:
        wb = r.json()
        weird = [n for n in wb["nodes"] if n["id"] == f"span:{BAD['spanId']}"]
        check("38. an end before the start yields no duration; an unknown kind and status read as such",
              len(weird) == 1 and weird[0]["duration_ms"] is None and weird[0]["provenance"]["span_kind"] == "unknown"
              and weird[0]["status"] == "unset" and weird[0]["type"] == "other")
        check("38. a duplicated span id keeps its first row and reports the drop", wb["duplicate_spans_dropped"] == 1
              and wb["summary"]["spans"] == 1)

    check("a missing item is 404", execution(HA, 99999).status_code == 404)

print()
if failures:
    print(f"{len(failures)} FAILED:")
    for f in failures:
        print("  -", f)
    raise SystemExit(1)
print("all work-execution checks passed")
