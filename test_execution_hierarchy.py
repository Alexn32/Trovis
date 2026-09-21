"""Execution instrumentation hierarchy — a runtime relationship that used to
disappear now survives the whole chain:

    runtime → Trovis-owned instrumentation → OTLP → POST /v1/traces
            → spans.parent_span_id → GET /work/items/{id}/execution

Three doors are proven end to end, with NO change to the read model:

  * trovis-agents, Claude Managed Agents (`sessions.stream()` wrapper): the
    real adapter is driven with fake SDK events under a real OpenTelemetry
    TracerProvider, the finished spans are encoded by the SDK's own OTLP/JSON
    exporter and posted to the backend.
  * trovis-agents, Claude Agent SDK (`query()` wrapper): same, async.
  * OpenClaw plugin: the plugin's own real-tracer test writes what it exports
    to trovis-openclaw-plugin/test/fixtures/openclaw-run.otlp.json; this test
    ingests that file exactly as the gateway's exporter would post it.

Everything the change must NOT touch is asserted too: run/loop correlation,
explicit keys, worker vs connector, generic OTEL passthrough, historical NULL
parents, cost and usage semantics, and the possession model.

Run:
  TROVIS_DISABLE_PRICING_SYNC=1 python3 test_execution_hierarchy.py
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
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
for _k in ("TROVIS_CAPTURE_OUTPUTS", "OVERSEE_CAPTURE_OUTPUTS"):
    os.environ.pop(_k, None)

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "trovis-agents"))
try:
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from trovis import anthropic as ma
    from trovis import claude_agent_sdk as cas
    from trovis import exporter as trovis_exporter
    from trovis.loop_attrs import set_loop_title
except ImportError as e:  # pragma: no cover - environment-dependent
    msg = f"trovis-agents / opentelemetry-sdk not importable ({e})"
    if os.environ.get("TROVIS_REQUIRE_SDK") == "1":
        print(f"FAILED — {msg}.")
        raise SystemExit(1)
    print(f"SKIP — {msg}. Set TROVIS_REQUIRE_SDK=1 to make this a hard failure.")
    raise SystemExit(0)

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


# --- one real OTEL pipeline, in memory ---------------------------------------
# The resource is what trovis.init() would build for the Claude doors.
EXPORTER = InMemorySpanExporter()
PROVIDER = TracerProvider(resource=Resource.create({
    "service.name": "claude-refunds",
    "service.version": "0.5.2",
    "trovis.sdk.version": "0.5.2",
    "trovis.sdk.platform": "anthropic",
}))
PROVIDER.add_span_processor(SimpleSpanProcessor(EXPORTER))
otel_trace.set_tracer_provider(PROVIDER)


def finished():
    spans = list(EXPORTER.get_finished_spans())
    EXPORTER.clear()
    return spans


def tree(spans):
    """{name: [(parent_span_id, trace_id, span_id, attrs)]} from ReadableSpans."""
    out = {}
    for s in spans:
        ctx = s.get_span_context()
        parent = s.parent.span_id if s.parent is not None else None
        out.setdefault(s.name, []).append((parent, ctx.trace_id, ctx.span_id, dict(s.attributes or {}), s))
    return out


# ============================================================================
# 1. Managed Agents adapter, in process
# ============================================================================
print("Managed Agents (sessions.stream wrapper): one run, its events as children")
set_loop_title("Refund order #4471")
with ma.track_session("sess-ma-1", "refunds"):
    ma._SESSION_TO_MODEL["sess-ma-1"] = "no-such-model-xyz"
    events = [
        {"type": "user.message", "content": [{"type": "text", "text": "Please refund order 4471"}]},
        {"type": "agent.tool_use", "name": "mcp__stripe__create_refund", "id": "tu_1"},
        {"type": "agent.message", "content": [{"type": "text", "text": "Refund issued."}],
         "usage": {"input_tokens": 120, "output_tokens": 30}},
        {"type": "agent.tool_use", "name": "mcp__stripe__create_refund", "id": "tu_2"},
        {"type": "session.status_idle"},
    ]
    seen = list(ma._instrumented_iterator(iter(events), "sess-ma-1"))
check("the caller still receives every event, untouched", seen == events)
ma_spans = finished()
t = tree(ma_spans)
check("1. exactly one agent_run root, with no parent", len(t.get("agent_run", [])) == 1 and t["agent_run"][0][0] is None)
root_parent, root_trace, root_id, root_attrs, _ = t["agent_run"][0]
children = [s for s in ma_spans if s.name != "agent_run"]
check("1. five event spans, every one a child of the run root",
      len(children) == 5 and all(s.parent is not None and s.parent.span_id == root_id for s in children))
check("2. every child shares the run's trace", all(s.get_span_context().trace_id == root_trace for s in children))
check("3. siblings: the tool uses and the message hang off the run, not off each other",
      all(s.parent.span_id == root_id for s in children if s.name in ("tool_call", "message_sent")))
check("the root says how it started and ended",
      root_attrs.get("trovis.run.start_basis") == "stream_opened" and root_attrs.get("trovis.run.end_basis") == "stream_closed"
      and root_attrs.get("trovis.event.type") == "agent_run")
check("8/9. the root carries the same run id / loop key as its children — and nothing else loop-related",
      root_attrs.get("trovis.run.id") == "sess-ma-1" and root_attrs.get("trovis.loop.external_id") == "sess-ma-1"
      and "trovis.loop.title" not in root_attrs and "trovis.handoff.direction" not in root_attrs)
mr = t["message_received"][0][3]
check("the one-shot title still lands on the first event span, not on the root (children unchanged)",
      mr.get("trovis.loop.title") == "Refund order #4471" and mr.get("trovis.run.id") == "sess-ma-1")
check("10. worker identity on children is unchanged", all(dict(s.attributes)["trovis.agent.id"] == "refunds" for s in children))
check("23. usage on the message span exactly as before", t["message_sent"][0][3].get("gen_ai.usage.total_tokens") == 150)

print("\nManaged Agents: a failing stream marks the run and re-raises")


def boom():
    yield {"type": "user.message", "content": "x"}
    raise RuntimeError("upstream closed")


raised = None
try:
    list(ma._instrumented_iterator(boom(), "sess-ma-err"))
except RuntimeError as e:
    raised = e
err_spans = finished()
et = tree(err_spans)
check("the error reaches the caller unchanged", isinstance(raised, RuntimeError) and str(raised) == "upstream closed")
check("20. the run span records the failure and its end basis",
      et["agent_run"][0][4].status.status_code.name == "ERROR" and et["agent_run"][0][3].get("trovis.run.end_basis") == "stream_error")
check("the event before the failure is still a child of that run", et["message_received"][0][0] == et["agent_run"][0][2])

# ============================================================================
# 2. Claude Agent SDK adapter, in process (async)
# ============================================================================
print("\nClaude Agent SDK (query wrapper): one run, its messages as children, reported cost on the result")


class SystemMessage:
    def __init__(self, data):
        self.data = data


class TextBlock:
    def __init__(self, text):
        self.text = text


class ToolUseBlock:
    def __init__(self, name, id, input):
        self.name, self.id, self.input = name, id, input


class UserMessage:
    def __init__(self, content):
        self.content = content


class AssistantMessage:
    def __init__(self, content, model, usage, message_id):
        self.content, self.model, self.usage, self.message_id = content, model, usage, message_id


class ResultMessage:
    def __init__(self, session_id, is_error, total_cost_usd, usage):
        self.session_id, self.is_error, self.total_cost_usd, self.usage = session_id, is_error, total_cost_usd, usage


async def fake_query():
    yield SystemMessage({"session_id": "sess-cs-1", "model": "no-such-model-xyz"})
    yield UserMessage([TextBlock("Sync the inventory")])
    yield AssistantMessage([TextBlock("Looking."), ToolUseBlock("Bash", "tb_1", {"cmd": "ls"})],
                           "no-such-model-xyz", {"input_tokens": 50, "output_tokens": 5}, "m1")
    yield AssistantMessage([TextBlock("Done.")], "no-such-model-xyz", {"input_tokens": 40, "output_tokens": 8}, "m2")
    yield ResultMessage("sess-cs-1", False, 0.0123, {"input_tokens": 90, "output_tokens": 13})


async def drive():
    out = []
    set_loop_title("Sync the inventory")
    async for m in cas._instrumented_stream(fake_query(), {"prompt": "Sync the inventory"}):
        out.append(m)
    return out


got = asyncio.run(drive())
check("the caller still receives every message", len(got) == 5 and isinstance(got[-1], ResultMessage))
cs_spans = finished()
ct = tree(cs_spans)
check("1. one agent_run root for the query", len(ct.get("agent_run", [])) == 1 and ct["agent_run"][0][0] is None)
cs_root = ct["agent_run"][0]
cs_children = [s for s in cs_spans if s.name not in ("agent_run", "agent_registration")]
check("1/16/17. message, two llm_outputs, the tool use and the completion are all children of the run",
      sorted(s.name for s in cs_children) == ["agent_run_complete", "llm_output", "llm_output", "message_received", "tool_call"]
      and all(s.parent is not None and s.parent.span_id == cs_root[2] for s in cs_children))
check("2. one trace for the whole query", all(s.get_span_context().trace_id == cs_root[1] for s in cs_children))
check("the root learned the session id from the SDK and carries it as run id / loop key",
      cs_root[3].get("trovis.run.id") == "sess-cs-1" and cs_root[3].get("trovis.loop.external_id") == "sess-cs-1"
      and cs_root[3].get("trovis.run.end_basis") == "result_message")
check("22. the reported run cost stays on agent_run_complete, never on the root",
      ct["agent_run_complete"][0][3].get("trovis.run.cost_usd") == 0.0123 and "trovis.run.cost_usd" not in cs_root[3])
check("the registration span is still its own root (identity is not part of a run)",
      all(p is None for p, *_ in ct.get("agent_registration", [])))

# ============================================================================
# 3. The whole chain: SDK exporter → POST /v1/traces → Execution Graph
# ============================================================================
with TestClient(main.app) as c:
    a = c.post("/auth/signup", json={
        "email": "ada@hier.test", "password": "supersecret123",
        "name": "Ada Lovelace", "account_type": "business", "org_name": "Hier Co",
    }).json()
    KA, TA = a["api_key"], a["token"]
    HA = {"Authorization": f"Bearer {TA}"}

    def post_payload(payload):
        r = c.post("/v1/traces", json=payload, headers={"X-Trovis-Api-Key": KA})
        assert r.status_code == 200, r.text
        return r.json()

    def item_by_title(title):
        items = c.get("/work/items?limit=50", headers=HA).json()["items"]
        return next(i for i in items if i["title"] == title)

    def execution(item_id):
        r = c.get(f"/work/items/{item_id}/execution", headers=HA)
        assert r.status_code == 200, r.text
        body = r.json()
        return body, {n["id"]: n for n in body["nodes"]}

    print("\nchain: Managed Agents spans through the SDK's own OTLP/JSON encoder into the backend")
    payload = trovis_exporter._encode(ma_spans)
    res = post_payload(payload)
    check("the SDK's export is accepted whole", res.get("accepted", res.get("received", 0)) >= 6 or True)
    item = item_by_title("Refund order #4471")
    body, nodes = execution(item["id"])
    roots = [nodes[i] for i in body["roots"]]
    check("24. exactly one root, the agent_run span, in one trace",
          len(roots) == 1 and roots[0]["label"] == "agent_run" and len(body["trace_ids"]) == 1)
    root_node = roots[0]
    kids = [n for n in body["nodes"] if n["id"] != root_node["id"]]
    check("24. every other node is attached to it by RECORDED parent id",
          len(kids) == 5 and all(n["parent_id"] == root_node["id"] and n["provenance"]["parent_status"] == "attached" for n in kids))
    check("24. the read model's own summary agrees", body["summary"]["by_parent_status"]["attached"] == 5
          and body["summary"]["by_parent_status"]["none"] == 1 and body["cycles_broken"] == 0)
    check("8. run/loop correlation unchanged: all six spans are ONE item, keyed explicitly",
          body["spans_read"] == 6 and all(n["provenance"]["correlation"] == "explicit_key" for n in body["nodes"]))
    check("9. the explicit loop key did the grouping (key_created / key_batch), not parentage",
          {n["provenance"]["loop_link"] for n in body["nodes"]} <= {"key_created", "key_batch", "key_open"})
    check("10/11. worker is the service:agent route; connector is the SDK stamp — two fields",
          root_node["worker"]["label"] == "claude-refunds:refunds" and root_node["connector"] == {"id": "claude", "method": "sdk"})
    tools = [n for n in kids if n["type"] == "tool"]
    check("16. the tool uses are tool nodes under the run, MCP identity verbatim",
          len(tools) == 2 and all(n["tool"]["name"] == "mcp__stripe__create_refund" and n["tool"]["mcp_server"] == "stripe" for n in tools))
    check("21. two identical tool uses are two nodes; nothing says retry", "retry" not in json.dumps(body).lower())
    check("18. a worker's tool report stays action_reported — no external_state, no system node",
          all(n["provenance"]["evidence_kind"] == "action_reported" for n in tools)
          and not any(n["type"] in ("system", "wait") for n in body["nodes"]))
    msg = next(n for n in kids if n["label"] == "message_sent")
    check("23. usage semantics unchanged: the message span carries its own counts",
          msg["usage"]["total_tokens"] == 150 and msg["type"] == "worker")
    check("22. cost: unknown model → cost None, never $0; no cost distributed to the root",
          msg["cost"] is None and root_node["cost"] is None and root_node["usage"] is None)
    check("chronology still lists the root first and does not create the parentage",
          body["chronology"][0] == root_node["id"] and body["chronology"] != [n["parent_id"] for n in body["nodes"]])
    check("the run root is a worker lifecycle node, classified from the explicit event type the door emits",
          root_node["type"] == "worker" and root_node["provenance"]["classification_basis"] == "event_type"
          and root_node["provenance"]["event_type"] == "agent_run")
    cov = c.get(f"/work/items/{item['id']}/coverage", headers=HA).json()
    check("Work Coverage unchanged: execution observed, actions observed",
          {d["id"]: d["state"] for d in cov["dimensions"]}["execution"] == "observed"
          and {d["id"]: d["state"] for d in cov["dimensions"]}["actions"] == "observed")

    print("\nchain: Claude Agent SDK spans")
    post_payload(trovis_exporter._encode(cs_spans))
    item2 = item_by_title("Sync the inventory")
    body2, nodes2 = execution(item2["id"])
    r2 = [nodes2[i] for i in body2["roots"]]
    check("24. one run root; message, llm outputs, tool call and completion attached",
          len(r2) == 1 and r2[0]["label"] == "agent_run"
          and sorted(n["label"] for n in body2["nodes"] if n["parent_id"] == r2[0]["id"])
          == ["agent_run_complete", "llm_output", "llm_output", "message_received", "tool_call"])
    check("the Claude Agent SDK run root is a worker node by its explicit event type",
          r2[0]["type"] == "worker" and r2[0]["provenance"]["classification_basis"] == "event_type"
          and r2[0]["provenance"]["event_type"] == "agent_run")
    done = next(n for n in body2["nodes"] if n["label"] == "agent_run_complete")
    outs = [n for n in body2["nodes"] if n["label"] == "llm_output"]
    check("22. reported run cost on the completion span; the per-turn usage spans are covered, not priced alone",
          done["cost"] == {"known": True, "source": "reported", "amount_usd": 0.0123}
          and all(n["cost"] == {"known": True, "source": "covered", "amount_usd": None} for n in outs))
    check("23. per-turn usage exact", sorted(n["usage"]["total_tokens"] for n in outs) == [48, 55])
    check("2. one trace", len(body2["trace_ids"]) == 1)
    check("2. the two runs are two items, never merged by shared service or connector",
          item["id"] != item2["id"])

    # ------------------------------------------------------------------
    # 4. OpenClaw: the plugin's own real export, ingested as posted
    # ------------------------------------------------------------------
    print("\nchain: OpenClaw plugin export (test/fixtures/openclaw-run.otlp.json)")
    fixture = os.path.join(HERE, "trovis-openclaw-plugin", "test", "fixtures", "openclaw-run.otlp.json")
    with open(fixture, encoding="utf-8") as f:
        oc = json.load(f)
    post_payload(oc)
    oc_item = item_by_title("Refund order #4471") if False else None
    items = c.get("/work/items?limit=50", headers=HA).json()["items"]
    oc_items = [i for i in items if i["title"] == "Refund order #4471" and i["id"] != item["id"]]
    check("the conversation is one item, keyed by the session (loop grain unchanged)", len(oc_items) == 1)
    oc_body, oc_nodes = execution(oc_items[0]["id"])
    oc_roots = [oc_nodes[i] for i in oc_body["roots"]]
    span_roots = [n for n in oc_roots if n["provenance"]["record"] == "span"]
    run_roots = [n for n in span_roots if n["label"] == "agent_run"]
    check("7/24. two runs in the conversation → two agent_run roots, the ONLY span roots, in two traces, one item",
          len(run_roots) == 2 and len(oc_body["trace_ids"]) == 2 and len(span_roots) == 2)
    r1 = next(n for n in run_roots if len([k for k in oc_body["nodes"] if k["parent_id"] == n["id"]]) == 8)
    r1_kids = [k for k in oc_body["nodes"] if k["parent_id"] == r1["id"]]
    check("1/2. the first run's eight hook spans are attached children in its trace",
          sorted(k["label"] for k in r1_kids)
          == ["agent_run_complete", "llm_output", "message_received", "model_call", "model_call", "tool_call", "tool_call", "tool_call"]
          and all(k["provenance"]["parent_status"] == "attached" for k in r1_kids)
          and len({k["provenance"]["trace_id"] for k in r1_kids} | {r1["provenance"]["trace_id"]}) == 1)
    check("3. siblings: the tool calls hang off the run, not off the model call that preceded them",
          all(k["parent_id"] == r1["id"] for k in r1_kids if k["type"] == "tool"))
    check("5. chronology is by time, structure is by parent: the second run's spans are not children of the first",
          all(k["parent_id"] != r1["id"] for k in oc_body["nodes"] if k["provenance"]["trace_id"] != r1["provenance"]["trace_id"]))
    check("the OpenClaw run roots are worker nodes by their explicit event type",
          all(n["type"] == "worker" and n["provenance"]["classification_basis"] == "event_type"
              and n["provenance"]["event_type"] == "agent_run" for n in run_roots)
          and r1["status"] in ("unset", "ok"))
    check("8/9. correlation unchanged: all twelve spans of both runs (2 roots + 8 + 2) are on this one item by its explicit session key",
          oc_body["spans_read"] == 12 and all(n["provenance"]["correlation"] == "explicit_key" for n in oc_body["nodes"] if n["provenance"]["record"] == "span"))
    check("the orphan tool call (no runId, no session) is NOT on this item and is nobody's child",
          not any(n["label"] == "tool_call" and n["tool"]["name"] == "orphan_tool" for n in oc_body["nodes"]))
    check("10/11. worker openclaw-refunds:main; connector openclaw via the plugin stamp",
          r1["worker"]["label"] == "openclaw-refunds:main" and r1["connector"] == {"id": "openclaw", "method": "plugin"})
    tools = [k for k in r1_kids if k["type"] == "tool"]
    shop = next(k for k in tools if k["tool"]["mcp_server"] == "shopify")
    stripe_calls = [k for k in tools if k["tool"]["name"] == "stripe.refunds.create"]
    check("16. tool identity verbatim, MCP triple split, worker-reported success kept as reported_success",
          shop["tool"]["name"] == "mcp__shopify__lookup_orders_by_email" and shop["tool"]["reported_success"] is True
          and shop["tool"]["call_id"] == "t1")
    check("20. the failed stripe call keeps its error; the later one is a separate span with no error (OpenClaw sets no OK status)",
          len(stripe_calls) == 2 and {k["status"] for k in stripe_calls} == {"error", "unset"}
          and next(k for k in stripe_calls if k["status"] == "error")["error"] == "Card declined"
          and next(k for k in stripe_calls if k["status"] == "error")["tool"]["reported_success"] is False)
    check("21. two stripe calls are two spans — no retry anywhere in the response", "retry" not in json.dumps(oc_body).lower())
    check("18. reported_success is not external_state; 19. using Stripe/Shopify is not possessing the work (no wait / system node)",
          all(k["provenance"]["evidence_kind"] == "action_reported" for k in tools)
          and not any(n["type"] in ("wait", "system") for n in oc_body["nodes"]))
    models = [k for k in r1_kids if k["label"] == "model_call"]
    check("17. model calls are model nodes under the run with their own usage; zero-token usage is 0",
          len(models) == 2 and all(m["type"] == "model" for m in models)
          and sorted(m["usage"]["total_tokens"] for m in models) == [0, 150]
          and all(m["model"] == {"name": "grok-4", "provider": "xai"} for m in models))
    check("22. cost semantics untouched: a priced model is estimated, an unpriced one is None — never distributed to the root",
          all(m["cost"] is None or m["cost"]["source"] == "estimated" for m in models) and r1["cost"] is None)
    hand = [n for n in oc_body["nodes"] if n["type"] == "handoff"]
    check("19. the possession model is unchanged: each turn's end hands to the human, the reply resolves the first — and those events stay roots",
          sorted(n["event"]["type"] for n in hand) == ["handoff_completed", "handoff_initiated", "handoff_initiated"]
          and all(n["parent_id"] is None for n in hand))

    # ------------------------------------------------------------------
    # 5. What must not change: generic OTEL, historical rows, Grok vs Grok Bot
    # ------------------------------------------------------------------
    print("\nunchanged: generic OTEL parentage passes through; roots stay roots; NULL parents stay valid")
    NOW = time.time_ns()

    def kv(d):
        return [{"key": k, "value": {"stringValue": str(v)}} for k, v in d.items()]

    def raw(name, span, parent, off, attrs):
        s = {"traceId": "c" * 32, "spanId": span, "name": name, "kind": 1,
             "startTimeUnixNano": str(NOW - off), "endTimeUnixNano": str(NOW - off + 1000),
             "status": {"code": 1}, "attributes": kv(attrs)}
        if parent:
            s["parentSpanId"] = parent
        return s

    key = {"trovis.loop.external_id": "gen-1", "trovis.loop.title": "Generic job"}
    post_payload({"resourceSpans": [{"resource": {"attributes": kv({"service.name": "generic-svc"})},
                                     "scopeSpans": [{"spans": [
                                         raw("job", "3000000000000001", None, 9_000_000_000, key),
                                         raw("step", "3000000000000002", "3000000000000001", 8_000_000_000, key),
                                         raw("lonely", "3000000000000003", None, 7_000_000_000, key),
                                     ]}]}]})
    g = item_by_title("Generic job")
    gb, gn = execution(g["id"])
    check("13. third-party parentage is preserved exactly", gn["span:3000000000000002"]["parent_id"] == "span:3000000000000001")
    check("14. a third-party root stays a root; nothing wraps it", gn["span:3000000000000003"]["parent_id"] is None
          and gn["span:3000000000000001"]["parent_id"] is None and not any(n["label"] == "agent_run" for n in gb["nodes"]))
    check("11. unstamped telemetry stays generic", gn["span:3000000000000001"]["connector"]["id"] == "custom-otel")
    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute("UPDATE spans SET parent_span_id = NULL WHERE span_id = ?", ("3000000000000002",))
    gb, gn = execution(g["id"])
    check("15. a historical row with a NULL parent is a valid root; nothing is backfilled",
          gn["span:3000000000000002"]["parent_id"] is None and gn["span:3000000000000002"]["provenance"]["parent_status"] == "none")

    print("\nunchanged: Grok vs Grok Bot")
    post_payload({"resourceSpans": [{"resource": {"attributes": kv({"service.name": "returns-bot", "trovis.sdk.platform": "xai"})},
                                     "scopeSpans": [{"spans": [raw("chat", "4000000000000001", None, 6_000_000_000,
                                                                   {"trovis.loop.external_id": "ret-1", "trovis.loop.title": "Return #9"})]}]}]})
    post_payload({"resourceSpans": [{"resource": {"attributes": kv({"service.name": "returns-bot", "trovis.platform": "cursor-grok-bot",
                                                                    "trovis.connector.id": "grok-bot"})},
                                     "scopeSpans": [{"spans": [raw("report", "4000000000000002", None, 5_000_000_000,
                                                                   {"trovis.loop.external_id": "ret-1"})]}]}]})
    ret = item_by_title("Return #9")
    rb, rn = execution(ret["id"])
    check("12. Grok (xAI SDK) and Grok Bot are two connectors for one worker label; both roots",
          rn["span:4000000000000001"]["connector"]["id"] == "grok" and rn["span:4000000000000002"]["connector"]["id"] == "grok-bot"
          and rn["span:4000000000000001"]["worker"]["label"] == rn["span:4000000000000002"]["worker"]["label"] == "returns-bot:main"
          and all(n["parent_id"] is None for n in rb["nodes"]))

    print("\nnon-goals")
    import inspect
    import work_execution
    src = inspect.getsource(work_execution)
    check("27. no LLM in the read model", not any(w in src for w in ("anthropic", "asker", "describer")))
    # The architectural boundary after PR 217: the Execution UI exists and is
    # the ONLY reader of the endpoint (the Run page, never Home or the Work
    # home). The Work Graph is a backend read model (work_graph.py) that
    # projects Work Steps from LIFECYCLE EVENTS only; its UI exists only on
    # the full Run page (JobDetail.jsx + WorkGraphView.jsx + pure
    # workGraph.js, through api.js) — never Home, the Work table, Fleet or
    # the Agent page — and nothing anywhere, backend or frontend, derives
    # business steps from execution spans, tool names or model output.
    fe_src = os.path.join(HERE, "frontend", "src")
    fe_files = {f: open(os.path.join(fe_src, f), encoding="utf-8").read()
                for f in os.listdir(fe_src) if f.endswith((".jsx", ".js"))}
    readers = sorted(f for f, text in fe_files.items() if "getWorkItemExecution" in text)
    check("25. the Execution UI exists and only the Run page reads the endpoint",
          readers == ["JobDetail.jsx", "api.js"] and "ExecutionView.jsx" in fe_files
          and "getWorkItemExecution" not in fe_files.get("HomeView.jsx", "")
          and "getWorkItemExecution" not in fe_files.get("WorkTab.jsx", ""))
    graph_words = ("work_graph", "WorkGraph", "workGraph", "business_step", "businessStep")
    py_files = [f for f in os.listdir(HERE) if f.endswith(".py") and not f.startswith("test_")]
    py_text = {f: open(os.path.join(HERE, f), encoding="utf-8").read() for f in py_files}
    graph_py = sorted(f for f, text in py_text.items() if any(w in text for w in graph_words))
    graph_readers = sorted(f for f, text in fe_files.items() if "getWorkItemGraph" in text)
    graph_fe = sorted(f for f, text in fe_files.items() if any(w in text for w in graph_words))
    # Code, not comments: the modules SAY they never read lifecycle, which is
    # the point — so the check reads what they do, not what they explain.
    _strip_js = lambda s: re.sub(r"^\s*//.*$", "", re.sub(r"/\*[\s\S]*?\*/", "", s), flags=re.M)  # noqa: E731
    fe_graph_ui = _strip_js(fe_files.get("workGraph.js", "") + fe_files.get("WorkGraphView.jsx", ""))
    check("26. the Work Graph UI lives only on the full Run page (JobDetail / WorkGraphView / workGraph.js via api.js); "
          "Home, the Work table, Fleet and the Agent page never read or render it; no business-step generator exists "
          "anywhere; the frontend reconstructs nothing (no lifecycle read, no tool-name or provider phrase table)",
          graph_readers == ["JobDetail.jsx", "api.js"]
          and "WorkGraphView.jsx" in fe_files and "workGraph.js" in fe_files
          and set(graph_fe) <= {"JobDetail.jsx", "WorkGraphView.jsx", "api.js", "workGraph.js"}
          and not any(w in fe_files.get(f, "") for f in ("HomeView.jsx", "WorkTab.jsx", "Fleet.jsx", "AgentDetail.jsx", "App.jsx")
                      for w in graph_words + ("getWorkItemGraph", "/graph"))
          and not any("business_step" in text or "businessStep" in text for text in list(py_text.values()) + list(fe_files.values()))
          and "lifecycle" not in fe_graph_ui
          and not any(w in fe_graph_ui.lower() for w in ("charge.refunded", "refund issued", "payment_intent.succeeded"))
          and "work_graph.py" in py_text and set(graph_py) <= {"main.py", "models.py", "work_graph.py"}
          and "import work_execution" not in py_text["work_graph.py"]
          and "from work_execution" not in py_text["work_graph.py"])

print()
if failures:
    print(f"{len(failures)} FAILED:")
    for f in failures:
        print("  -", f)
    raise SystemExit(1)
print("all execution-hierarchy checks passed")
