"""Execution Graph — the technical execution underneath one run.

Trovis keeps two representations of the SAME work, at two resolutions:

  Work Graph        (operational; future) what happened, who had it, where it
                    waited, how it ended — for operators and managers.
  Execution Graph   (technical; this module) which worker ran, which model
                    activity and tool calls the worker recorded, what called
                    what, in what order, how long it took, where it errored,
                    what usage and cost were recorded — for engineers, agent
                    builders and FDEs.

They are not separate products and not separate records: both are read
models over the one persisted record of a run (its spans and loop events).
This module is the technical one. It persists nothing, calls no model, and
never groups execution into business steps — that reconstruction is the
Work Graph's job, later, ON TOP of this.

Unit: one work item (a loop — the frontend's "run"). Per item only; never a
job roll-up, never a global query.

Two concepts are kept apart and both are served:

  STRUCTURE    `parent_id` — what technically called or contained what, from
               the parent span id the exporter wrote and ingest stored
               verbatim (spans.parent_span_id). Preserved when it resolves to
               a span in this run's read set; never invented from timing.
  CHRONOLOGY   `chronology` — node ids in persisted time order. "Occurred
               after" is not "was caused by": chronology creates no edges.

Node types — each has a deterministic basis in persisted data, and the basis
is stated on the node (`provenance.classification_basis`):

  worker      a worker lifecycle or messaging span: `trovis.event.type` (or
              the span name) is one of message_received / message_sent /
              message_sending / agent_run_complete / agent_error /
              agent_registration / heartbeat / handoff, or a Grok Bot report
              (job_started / job_waiting / job_finished / job_failed).
  model       `trovis.event.type` or span name is model_call / llm_output, OR
              the span's OWN attributes carried model usage (the ingest
              invariant behind spans.total_tokens IS NOT NULL). A model NAME
              alone — in resource metadata or on a span that carried no
              usage — classifies nothing.
  tool        `trovis.event.type` or span name is tool_call, or the span
              carries a tool name (`trovis.tool.name`). The tool's identity
              is kept as the span carried it; MCP prefixes stay verbatim.
  system      an external system reported the state of its own object (a
              SaaS-spine loop event with actor_type 'system' and a
              saas_provider) that is not a wait. Independently sourced —
              never derived from a worker's report.
  wait        a declared blocking wait: a handoff_initiated event with
              direction to_system (SaaS spine, an agent's declaration, or a
              person's). Using a system (a tool span) is not waiting on it.
  handoff     a handoff_initiated with direction to_human / to_agent, or a
              handoff_accepted / _completed / _declined by a person or agent.
  completion  a loop_closed event — the Work record closed. Not an outcome.
  other       everything else, including HTTP-shaped spans: nothing
              persisted proves an HTTP call was a business system action,
              so it stays neutral. Unknown beats false precision.

What is NOT here, because the record cannot prove it: retries (repeated
similar spans stay repeated spans), tool results as separate records (a
door that captures a result puts it on the tool span itself), business
outcomes, success, verification.

Parentage rules (`provenance.parent_status`):

  attached          parent_span_id names a span in this run's read set
                    (matched by trace id + span id).
  none              no parent span id was recorded.
  outside_read_set  a parent id was recorded but no such span is in the read
                    set — another run, never ingested, past the bound, or
                    malformed. The node is a root; the raw id is kept.
  self_reference    the span names itself as parent. Root.
  cycle_broken      the recorded parents formed a cycle; this node's edge was
                    the one removed. Deterministic: within a cycle the node
                    with the latest (start, span id) is detached.

A span id seen twice in the read set (a re-export) keeps its first row; the
count of dropped duplicates is reported. Events are never attached as
children of spans: an ingest-written event carries `provenance.span_id`
naming the span whose attributes caused it, and a UI may show it beside
that span, but "declared on" is not "contained by".

Chronology: every node, sorted by (persisted time, node id) — span start or
event time; ties broken by id so the order is stable across reads.

Worker identity vs connector identity (PR 210): `worker` is who performed
or reported (service:agent, never parsed apart); `connector` is how Trovis
received the observation (connect_health.identify_connector on the resource
stamp). Two workers on one connector stay two workers; Grok and Grok Bot
stay two connectors; unstamped telemetry stays custom-otel.

Cost and usage (PR 211): usage is the span's own stored counts, present iff
the span carried usage (zero is a real zero); cost is known iff a cost was
stored — reported (the SDK's figure), estimated (from tokens), or covered
(subsumed in a reported run total: known, but NOT priced alone, so its
amount is None). Unknown cost is None, never 0, and a run total is never
distributed across nodes.

Bounded read: spans are read oldest-first up to a limit; `bounded` says the
run has more, and a parent past the bound reads outside_read_set. Events
are read whole.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import database
import work_evidence
from connect_health import identify_connector

NODE_TYPES: tuple[str, ...] = (
    "worker", "model", "tool", "system", "handoff", "wait", "completion", "other",
)

PARENT_STATUSES: tuple[str, ...] = (
    "attached", "none", "outside_read_set", "self_reference", "cycle_broken",
)

CLASSIFICATION_BASES: tuple[str, ...] = (
    "event_type", "span_name", "tool_attribute", "usage_attributes",
    "event_direction", "event_actor", "none",
)

STATUSES: tuple[str, ...] = ("ok", "error", "unset", "recorded")

# The Trovis-owned event vocabulary (`trovis.event.type`, mirrored in span
# names by every Trovis door). Anything else classifies nothing by name.
_WORKER_EVENTS = frozenset({
    "message_received", "message_sent", "message_sending", "agent_run_complete",
    "agent_error", "agent_registration", "heartbeat", "handoff",
    "job_started", "job_waiting", "job_finished", "job_failed",
})
_MODEL_EVENTS = frozenset({"model_call", "llm_output"})
_TOOL_EVENTS = frozenset({"tool_call"})

_SPAN_KINDS = {
    0: "unspecified", 1: "internal", 2: "server", 3: "client", 4: "producer", 5: "consumer",
}
_OTLP_STATUS_OK = 1
_OTLP_STATUS_ERROR = 2
_SAAS_LABELS = {"stripe": "Stripe", "hubspot": "HubSpot", "shopify": "Shopify"}


def _iso(ns: Any) -> str | None:
    try:
        return database._ns_to_iso(int(ns)) if ns else None
    except (TypeError, ValueError):
        return None


def _int_or_none(v: Any) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _float_or_none(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _bool_or_none(v: Any) -> bool | None:
    """A reported boolean, as the span carried it; anything else is None."""
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("true", "1", "yes"):
            return True
        if s in ("false", "0", "no"):
            return False
    if isinstance(v, (int, float)) and v in (0, 1):
        return bool(v)
    return None


def _classify_span(span_name: str, event_type: Any, tool_name: Any, has_usage: bool) -> tuple[str, str]:
    """(type, basis). Explicit Trovis vocabulary first, then the span name in
    that same vocabulary, then a tool attribute, then the span's own usage.
    Nothing else classifies."""
    et = str(event_type) if isinstance(event_type, str) and event_type else None
    for candidate, basis in ((et, "event_type"), (span_name, "span_name")):
        if not candidate:
            continue
        if candidate in _TOOL_EVENTS:
            return "tool", basis
        if candidate in _MODEL_EVENTS:
            return "model", basis
        if candidate in _WORKER_EVENTS:
            return "worker", basis
    if tool_name:
        return "tool", "tool_attribute"
    if has_usage:
        return "model", "usage_attributes"
    return "other", "none"


def _tool_fields(attrs: dict[str, Any], span_name: str, node_type: str) -> dict[str, Any] | None:
    raw = database.attr(attrs, "tool.name")
    raw = str(raw) if raw not in (None, "") else None
    if node_type != "tool" and raw is None:
        return None
    lp = database._loops_mod()
    identifier = lp.span_tool(span_name, raw)
    display = raw
    server = None
    if raw and raw.startswith("mcp__") and raw.count("__") >= 2:
        # mcp__{server}__{tool}: deterministic string split, nothing more.
        parts = raw.split("__", 2)
        server = parts[1] or None
        display = parts[2] or raw
    call_id = database.attr(attrs, "tool.call_id")
    return {
        "name": raw,
        "identifier": identifier,
        "display_name": display,
        "mcp_server": server,
        "call_id": str(call_id) if call_id not in (None, "") else None,
        # The worker's own report of the call's success, when a door records
        # one (OpenClaw). Never an outcome Trovis observed.
        "reported_success": _bool_or_none(database.attr(attrs, "tool.success")),
    }


def _model_fields(attrs: dict[str, Any]) -> dict[str, Any] | None:
    name = database.model_from_attrs(attrs)
    provider = attrs.get("gen_ai.system")
    if not name and not provider:
        return None
    return {"name": name, "provider": str(provider) if provider else None}


def _usage_fields(s: dict[str, Any]) -> dict[str, Any] | None:
    total = _int_or_none(s.get("total_tokens"))
    if total is None:
        return None
    return {
        "input_tokens": _int_or_none(s.get("input_tokens")),
        "output_tokens": _int_or_none(s.get("output_tokens")),
        "total_tokens": total,
        "cache_creation_input_tokens": _int_or_none(s.get("cache_creation_input_tokens")),
        "cache_read_input_tokens": _int_or_none(s.get("cache_read_input_tokens")),
    }


def _cost_fields(s: dict[str, Any]) -> dict[str, Any] | None:
    amount = _float_or_none(s.get("estimated_cost_usd"))
    if amount is None:
        return None
    source = s.get("cost_source")
    if source == "covered":
        # Inside a reported run total: known to be accounted for, not priced
        # on its own — so no amount, rather than the stored placeholder 0.
        return {"known": True, "source": "covered", "amount_usd": None}
    if source == "reported":
        return {"known": True, "source": "reported", "amount_usd": round(amount, 6)}
    return {"known": True, "source": "estimated", "amount_usd": round(amount, 6)}


def _span_node(s: dict[str, Any]) -> dict[str, Any]:
    attrs = work_evidence._loads(s.get("attributes"))
    connector, method = identify_connector(s.get("resource_attributes"))
    lp = database._loops_mod()
    service = s.get("service_name") or ""
    agent_id = s.get("agent_id") or "main"
    span_name = str(s.get("span_name") or "")
    sid = str(s.get("span_id") or "")
    tid = str(s.get("trace_id") or "")
    parent_raw = s.get("parent_span_id")
    parent_raw = str(parent_raw) if parent_raw not in (None, "") else None
    has_usage = s.get("total_tokens") is not None
    tool_name = database.attr(attrs, "tool.name")
    event_type = database.attr(attrs, "event.type")
    node_type, basis = _classify_span(span_name, event_type, tool_name, has_usage)

    start = _int_or_none(s.get("start_time_unix")) or 0
    end = _int_or_none(s.get("end_time_unix"))
    duration_ms: float | None = None
    if end is not None and start and end >= start:
        duration_ms = round((end - start) / 1_000_000, 3)
    code = _int_or_none(s.get("status_code")) or 0
    status = "error" if code == _OTLP_STATUS_ERROR else ("ok" if code == _OTLP_STATUS_OK else "unset")
    error = database._run_error_line(s.get("status_message")) if status == "error" else None

    tool = _tool_fields(attrs, span_name, node_type)
    evidence_kind = "action_reported" if (node_type == "tool" and tool and tool.get("identifier")) else "execution"
    kind = _int_or_none(s.get("kind"))
    return {
        "id": f"span:{sid}",
        "type": node_type,
        "parent_id": None,  # resolved in _resolve_parents
        "started_at": _iso(start),
        "ended_at": _iso(end) if end else None,
        "duration_ms": duration_ms,
        "label": span_name,
        "worker": {"label": lp.agent_actor(service, agent_id), "service_name": service, "agent_id": agent_id},
        "connector": {"id": connector, "method": method},
        "status": status,
        "error": error,
        "model": _model_fields(attrs),
        "tool": tool,
        "usage": _usage_fields(s),
        "cost": _cost_fields(s),
        "event": None,
        "provenance": {
            "record": "span",
            "span_id": sid or None,
            "trace_id": tid or None,
            "parent_span_id": parent_raw,
            "parent_status": "none" if parent_raw is None else "outside_read_set",
            "event_id": None,
            "evidence_kind": evidence_kind,
            "correlation": work_evidence._span_correlation(s.get("loop_link")),
            "loop_link": s.get("loop_link") if isinstance(s.get("loop_link"), str) else None,
            "classification_basis": basis,
            "span_kind": _SPAN_KINDS.get(kind, "unknown") if kind is not None else "unspecified",
            "event_type": str(event_type) if isinstance(event_type, str) and event_type else None,
        },
        "_ts": start,
        "_trace": tid,
        "_span": sid,
    }


def _event_node(
    account_id: int | None,
    e: dict[str, Any],
    by_span: dict[str, dict[str, Any]],
    human_cache: dict[str, str | None],
) -> dict[str, Any] | None:
    etype = str(e.get("type") or "")
    if etype == "loop_opened":
        # Record bookkeeping, not execution: the opening span (link created /
        # key_created) is already a node, and a person's approval is Work.
        return None
    payload = work_evidence._loads(e.get("payload"))
    actor_type = e.get("actor_type") or ""
    actor = str(e.get("actor") or "")
    ts = _int_or_none(e.get("event_time_unix")) or 0
    eid = int(e["id"])
    span_id = e.get("span_id") or None
    trace_id = e.get("trace_id") or None
    provider = payload.get("saas_provider")
    direction = payload.get("direction")

    def human(target: str) -> str | None:
        if target not in human_cache:
            human_cache[target] = database.resolve_human_label(account_id, target)
        return human_cache[target]

    src = by_span.get(str(span_id)) if span_id else None
    worker = None
    connector = None
    correlation: str | None = None
    if actor_type == "system" and provider:
        pid = str(provider).lower()
        actor_desc = {"type": "system", "label": _SAAS_LABELS.get(pid, str(provider))}
        connector = {"id": pid, "method": "webhook"}
        correlation = "explicit_key"
    elif actor_type == "agent":
        if src is not None:
            worker = src["worker"]
            connector = src["connector"]
            correlation = src["provenance"]["correlation"]
        else:
            # The composite actor is display-only and never parsed apart.
            worker = {"label": actor or None, "service_name": None, "agent_id": None}
        actor_desc = {"type": "agent", "label": worker["label"]}
    elif actor_type == "human":
        actor_desc = {"type": "human", "label": human(actor)}
        correlation = "direct"
    else:
        actor_desc = {"type": "system", "label": "Trovis"}
        correlation = "direct"

    if etype == "handoff_initiated":
        if direction == "to_system":
            node_type, basis = "wait", "event_direction"
        else:
            node_type, basis = "handoff", "event_direction"
        evidence_kind = "external_state" if (actor_type == "system" and provider) else "handoff"
    elif etype in ("handoff_accepted", "handoff_completed", "handoff_declined"):
        if actor_type == "system" and provider:
            node_type, basis, evidence_kind = "system", "event_actor", "external_state"
        else:
            node_type, basis, evidence_kind = "handoff", "event_type", "handoff"
    elif etype == "loop_closed":
        node_type, basis, evidence_kind = "completion", "event_type", "completion"
    else:
        node_type, basis, evidence_kind = "other", "none", None

    target_id = payload.get("target_id")
    event: dict[str, Any] = {
        "type": etype,
        "direction": direction if isinstance(direction, str) else None,
        "actor": actor_desc,
        # A person's address is never echoed; their resolved name is.
        "target_id": str(target_id) if (target_id is not None and direction != "to_human") else None,
        "target_label": human(str(target_id)) if (direction == "to_human" and target_id is not None) else None,
        "handoff_id": payload.get("handoff_id"),
        "reason": payload.get("reason"),
        "detail": payload.get("detail"),
        "waiting_on": payload.get("waiting_on"),
        "provider": str(provider).lower() if provider else None,
        "provider_object_id": payload.get("saas_object_id"),
        "provider_event_id": payload.get("saas_event_id"),
        "provider_event_type": payload.get("saas_event_type"),
        "effect": payload.get("saas_effect") or (
            "clear" if (provider and etype == "handoff_completed") else None
        ),
    }
    label = etype if not event["direction"] else f"{etype} {event['direction']}"
    return {
        "id": f"event:{eid}",
        "type": node_type,
        "parent_id": None,
        "started_at": _iso(ts),
        "ended_at": None,
        "duration_ms": None,
        "label": label,
        "worker": worker,
        "connector": connector,
        "status": "recorded",
        "error": None,
        "model": None,
        "tool": None,
        "usage": None,
        "cost": None,
        "event": event,
        "provenance": {
            "record": "loop_event",
            "span_id": str(span_id) if span_id else None,
            "trace_id": str(trace_id) if trace_id else None,
            "parent_span_id": None,
            "parent_status": "none",
            "event_id": eid,
            "evidence_kind": evidence_kind,
            "correlation": correlation,
            "loop_link": None,
            "classification_basis": basis,
            "span_kind": None,
            "event_type": None,
        },
        "_ts": ts,
        "_trace": trace_id or "",
        "_span": None,
    }


def _resolve_parents(span_nodes: list[dict[str, Any]]) -> int:
    """Attach each span node to its recorded parent when that parent is in
    the read set (same trace), then break any cycle deterministically.
    Returns the number of edges removed for cycles."""
    index: dict[tuple[str, str], dict[str, Any]] = {
        (n["_trace"], n["_span"]): n for n in span_nodes
    }
    for n in span_nodes:
        raw = n["provenance"]["parent_span_id"]
        if raw is None:
            continue
        if raw == n["_span"]:
            n["provenance"]["parent_status"] = "self_reference"
            continue
        parent = index.get((n["_trace"], raw))
        if parent is None or parent is n:
            n["provenance"]["parent_status"] = "outside_read_set"
            continue
        n["parent_id"] = parent["id"]
        n["provenance"]["parent_status"] = "attached"

    by_id = {n["id"]: n for n in span_nodes}
    broken = 0
    for start_id in sorted(by_id):
        path: list[str] = []
        pos: dict[str, int] = {}
        cur: str | None = start_id
        while cur is not None:
            if cur in pos:
                cycle = path[pos[cur]:]
                victim = max(cycle, key=lambda k: (by_id[k]["_ts"], k))
                by_id[victim]["parent_id"] = None
                by_id[victim]["provenance"]["parent_status"] = "cycle_broken"
                broken += 1
                break
            pos[cur] = len(path)
            path.append(cur)
            cur = by_id[cur]["parent_id"]
    return broken


def execution_from_rows(account_id: int | None, item_id: int, raw: dict[str, Any]) -> dict[str, Any]:
    """The graph for rows already read by database.get_work_item_execution_rows.
    Deterministic; never raises on imperfect telemetry."""
    span_nodes: list[dict[str, Any]] = []
    seen: set[str] = set()
    duplicates = 0
    for s in raw.get("spans") or []:
        node = _span_node(s)
        if not node["_span"] or node["_span"] in seen:
            duplicates += 1
            continue
        seen.add(node["_span"])
        span_nodes.append(node)
    cycles = _resolve_parents(span_nodes)
    by_span = {n["_span"]: n for n in span_nodes}

    human_cache: dict[str, str | None] = {}
    event_nodes: list[dict[str, Any]] = []
    for e in raw.get("events") or []:
        try:
            node = _event_node(account_id, e, by_span, human_cache)
        except (KeyError, TypeError, ValueError):
            node = None
        if node is not None:
            event_nodes.append(node)

    nodes = span_nodes + event_nodes
    nodes.sort(key=lambda n: (n["_ts"], n["id"]))
    chronology = [n["id"] for n in nodes]
    roots = [n["id"] for n in nodes if n["parent_id"] is None]
    trace_ids = sorted({n["_trace"] for n in nodes if n["_trace"]})

    by_type: dict[str, int] = {t: 0 for t in NODE_TYPES}
    by_parent: dict[str, int] = {p: 0 for p in PARENT_STATUSES}
    for n in nodes:
        by_type[n["type"]] = by_type.get(n["type"], 0) + 1
        if n["provenance"]["record"] == "span":
            ps = n["provenance"]["parent_status"]
            by_parent[ps] = by_parent.get(ps, 0) + 1
    for n in nodes:
        for k in ("_ts", "_trace", "_span"):
            n.pop(k, None)

    return {
        "item_id": item_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "trace_ids": trace_ids,
        "nodes": nodes,
        "roots": roots,
        "chronology": chronology,
        "bounded": bool(raw.get("spans_truncated")),
        "span_limit": int(raw.get("span_limit") or 0),
        "spans_read": len(raw.get("spans") or []),
        "events_read": len(raw.get("events") or []),
        "duplicate_spans_dropped": duplicates,
        "cycles_broken": cycles,
        "summary": {
            "nodes": len(nodes),
            "spans": len(span_nodes),
            "events": len(event_nodes),
            "by_type": by_type,
            "by_parent_status": by_parent,
        },
    }


def build_work_item_execution(account_id: int | None, item_id: int) -> dict[str, Any] | None:
    """The Execution Graph for one work item, or None when the item is not
    this account's. Two bounded, index-backed reads; no model calls."""
    limit = int(database._WORK_EXECUTION_SPAN_LIMIT)
    raw = database.get_work_item_execution_rows(account_id, item_id, span_limit=limit)
    if raw.get("loop") is None:
        return None
    raw["span_limit"] = limit
    return execution_from_rows(account_id, item_id, raw)
