"""Work Graph — the deterministic operational projection of one run.

The same run has two read models over ONE persisted record (its spans and
loop events):

  Execution Graph   (work_execution.py) technical truth: what ran, in what
                    recorded structure, with which models and tools.
  Work Graph        (this module) operational truth: which meaningful
                    changes to the WORK can Trovis state, who or what held
                    the work, where it waited, how the record ended.

They are not separate records. Every Work Step here points back at the
loop event it came from, at the Evidence record for that event (`event:<id>`,
work_evidence.py), and at the Execution node for it (also `event:<id>`,
work_execution.py) — so Work Graph → Evidence → Execution is a lookup, not
an inference.

THE RULE. A record enters the Work Graph only when Trovis has evidence of a
meaningful change in the work — never merely evidence that computation
occurred. Sparse but supported is correct. Nothing here gets richer by
getting less truthful.

Three levels, kept apart:

  WORK STEP              a meaningful operational change, from an explicit
                         lifecycle record (below).
  SUPPORTING EVIDENCE    records that explain a step: the span whose
                         attributes declared a handoff; a tool report; the
                         provider ids on a SaaS event. Referenced, not
                         promoted.
  EXECUTION-ONLY         technical machinery that makes no operational
                         claim: model calls, generic spans, HTTP-shaped
                         spans, trace boundaries, connector identity, cost.
                         Counted (so the reader knows they exist), never
                         turned into steps.

What becomes a Work Step, exactly (loop_events only — no span ever does):

  handoff_initiated  to_human   → `handoff`   a person now holds the work
                     to_agent   → `handoff`   another agent now holds it
                                              (that it ACTED is not claimed)
                     to_system  → `wait`      a declared blocking wait on a
                                              system (an agent's declaration,
                                              a person's, or the SaaS spine's
                                              `wait` effect);
                                → `exception` when the SaaS spine recorded
                                              effect `stuck` (failure/dispute)
  handoff_declined              → `exception` an explicit decline; reason
                                              only when one was recorded
  stall_detected                → `exception` with the recorded reason only
  loop_closed                   → `completed` "Work record closed" — the
                                              record ended; reason/detail as
                                              stored; NEVER an outcome

What deliberately does NOT become a Work Step:

  loop_opened            lifecycle/provenance. Not "work started": nothing
                         persisted establishes that business work began.
  agent_run, model calls, generic / HTTP-shaped spans, trace boundaries
                         execution only.
  tool spans / action_reported
                         the worker REPORTED invoking a tool. Supporting
                         evidence only: `stripe.refunds.create` says nothing
                         about whether a refund happened, and no tool name
                         is read for business meaning.
  model output text      never read.
  handoff_accepted / handoff_completed (a person, an agent, or a SaaS clear)
                         they resolve possession and are kept as lifecycle
                         records; a standalone "wait resolved" step would be
                         noise. A SaaS clear is the provider's state, kept
                         with its ids on the lifecycle record — it is not an
                         invented business outcome.
  technical errors       an errored span stays Execution-only; it becomes a
                         Work exception only through an explicit lifecycle
                         record (decline, stall, SaaS stuck).

`progress` is in the vocabulary for the structural meaning "the work moved
forward", but no record Trovis persists today establishes it independently
of a tool report or model output — so nothing produces it yet. That is the
truthful state, not a gap to paper over.

Possession is the existing canonical chain — loops.compute_loop_segments —
over the same merged stream the Run detail uses (lifecycle events plus one
activity event per span), returned in a typed shape. There is no second
state machine: tool usage is not possession, a to_system handoff is, a
person or agent resolution returns the work, agent activity after a wait
resumes it, stragglers fold. People are named through the account-scoped
resolver or read "a person"; a raw address is never echoed.

Chronology is persisted event time, ties broken by the event id. It is
order, not causality: no edge is drawn between adjacent steps.

Bounded: spans are read up to database._WORK_EXECUTION_SPAN_LIMIT (the
same bound Execution and Evidence use). Lifecycle events are read whole,
so every Work Step is present regardless of the bound; the bound can only
affect the activity that RESUMES possession after a wait and the
execution-only counts, and the response says so.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import database
import work_evidence
from connect_health import identify_connector

STEP_TYPES: tuple[str, ...] = ("progress", "handoff", "wait", "exception", "completed")

# loop_closed reasons that mean the record was closed WITHOUT the agent or a
# person finishing the work (loops.compute_loop_state's rule 1).
_ABANDONED_REASONS = frozenset({"abandoned", "ingestion_artifact"})
_SAAS_LABELS = {"stripe": "Stripe", "hubspot": "HubSpot", "shopify": "Shopify"}
_OTLP_STATUS_ERROR = 2
_A_PERSON = "a person"


def _iso(ns: Any) -> str | None:
    try:
        return database._ns_to_iso(int(ns)) if ns else None
    except (TypeError, ValueError):
        return None


def _str(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _system_label(provider: Any, fallback: Any = None) -> str | None:
    p = _str(provider)
    if p:
        return _SAAS_LABELS.get(p.lower(), p)
    return _str(fallback)


class _People:
    """Account-scoped, memoised person naming. Never a raw address."""

    def __init__(self, account_id: int | None):
        self.account_id = account_id
        self.cache: dict[str, str | None] = {}

    def name(self, target: Any) -> str | None:
        t = _str(target)
        if not t:
            return None
        if t not in self.cache:
            try:
                self.cache[t] = database.resolve_human_label(self.account_id, t)
            except Exception:  # noqa: BLE001 — naming never breaks the graph
                self.cache[t] = None
        return self.cache[t]

    def label(self, target: Any) -> str:
        return self.name(target) or _A_PERSON


def _span_index(spans: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """span_id → worker label / connector / correlation, for the events
    ingest wrote from a span (the same lookup Evidence uses)."""
    lp = database._loops_mod()
    out: dict[str, dict[str, Any]] = {}
    for s in spans:
        sid = _str(s.get("span_id"))
        if not sid:
            continue
        connector, _method = identify_connector(s.get("resource_attributes"))
        out[sid] = {
            "worker": lp.agent_actor(s.get("service_name") or "", s.get("agent_id") or "main"),
            "connector": connector,
            "correlation": work_evidence._span_correlation(s.get("loop_link")),
        }
    return out


def _actor(e: dict[str, Any], payload: dict[str, Any], by_span: dict[str, dict[str, Any]], people: _People) -> dict[str, Any]:
    """Who performed or reported the record. Worker identity (the stored
    service:agent label) — never the connector, which rides in provenance."""
    actor_type = e.get("actor_type") or ""
    actor = _str(e.get("actor"))
    provider = payload.get("saas_provider")
    if actor_type == "system" and provider:
        return {"type": "system", "label": _system_label(provider)}
    if actor_type == "system":
        return {"type": "system", "label": "Trovis"}
    if actor_type == "human":
        return {"type": "human", "label": people.label(actor)}
    src = by_span.get(_str(e.get("span_id")) or "") if e.get("span_id") else None
    return {"type": "agent", "label": (src or {}).get("worker") or actor}


def _provenance(e: dict[str, Any], payload: dict[str, Any], by_span: dict[str, dict[str, Any]], evidence_kind: str) -> dict[str, Any]:
    eid = int(e["id"])
    span_id = _str(e.get("span_id"))
    src = by_span.get(span_id) if span_id else None
    provider = payload.get("saas_provider")
    if e.get("actor_type") == "system" and provider:
        correlation: str | None = "explicit_key"
        connector: str | None = str(provider).lower()
    elif e.get("actor_type") == "agent":
        correlation = (src or {}).get("correlation")
        connector = (src or {}).get("connector")
    else:
        correlation, connector = "direct", None
    return {
        "event_id": eid,
        "evidence_id": f"event:{eid}",
        "evidence_kind": evidence_kind,
        "execution_node_id": f"event:{eid}",
        "span_id": span_id,
        "trace_id": _str(e.get("trace_id")),
        "correlation": correlation,
        "source_type": e.get("actor_type") or None,
        "source_connector_id": connector,
        "external_object_id": _str(payload.get("saas_object_id")),
        "external_event_id": _str(payload.get("saas_event_id")),
    }


def _step(
    e: dict[str, Any], *, step_type: str, label: str, actor: dict[str, Any],
    system: dict[str, Any] | None, details: dict[str, Any], provenance: dict[str, Any],
) -> dict[str, Any]:
    eid = int(e["id"])
    return {
        "id": f"work-step:event:{eid}",
        "type": step_type,
        "at": _iso(e.get("event_time_unix")),
        "label": label,
        "actor": actor,
        "system": system,
        "details": details,
        "provenance": provenance,
        "_ts": int(e.get("event_time_unix") or 0),
        "_eid": eid,
    }


def _step_for_event(
    e: dict[str, Any], by_span: dict[str, dict[str, Any]], people: _People,
) -> dict[str, Any] | None:
    """The Work Step one lifecycle record earns, or None. Only the rules in
    the module docstring; only recorded facts; nothing filled in."""
    etype = str(e.get("type") or "")
    payload = work_evidence._loads(e.get("payload"))
    actor = _actor(e, payload, by_span, people)
    provider = _str(payload.get("saas_provider"))
    direction = _str(payload.get("direction"))
    reason = _str(payload.get("reason"))

    if etype == "handoff_initiated":
        common = {
            "direction": direction,
            "handoff_id": _str(payload.get("handoff_id")),
            "reason": reason,
        }
        prov = _provenance(e, payload, by_span, "external_state" if (e.get("actor_type") == "system" and provider) else "handoff")
        if direction == "to_human":
            # A person's address is never echoed: their resolved name, or
            # "a person". target_id is omitted for people on purpose.
            return _step(
                e, step_type="handoff", label="Handed to a person", actor=actor, system=None,
                details={**common, "target_label": people.label(payload.get("target_id")) if payload.get("target_id") else _A_PERSON,
                         "target_id": None},
                provenance=prov,
            )
        if direction == "to_agent":
            return _step(
                e, step_type="handoff", label="Handed to another agent", actor=actor, system=None,
                # The target now holds the work. Nothing here says it acted.
                details={**common, "target_label": _str(payload.get("target_id")), "target_id": _str(payload.get("target_id"))},
                provenance=prov,
            )
        if direction == "to_system":
            system = {
                "label": _system_label(provider, payload.get("target_id")) or "a system",
                "provider": provider.lower() if provider else None,
                "target_id": _str(payload.get("target_id")),
            }
            effect = _str(payload.get("saas_effect"))
            details = {
                **common,
                "waiting_on": _str(payload.get("waiting_on")),
                "saas_provider": provider.lower() if provider else None,
                "saas_effect": effect,
                "saas_object_id": _str(payload.get("saas_object_id")),
                "saas_event_type": _str(payload.get("saas_event_type")),
                "saas_event_id": _str(payload.get("saas_event_id")),
            }
            if effect == "stuck":
                # The provider's own state says this object is stuck (a failed
                # payment, a dispute). The recorded event type and reason are
                # the whole claim; nothing about the business outcome is added.
                return _step(
                    e, step_type="exception", label=f"Stuck on {system['label']}", actor=actor,
                    system=system, details=details, provenance=prov,
                )
            return _step(
                e, step_type="wait", label=f"Waiting on {system['label']}", actor=actor,
                system=system, details=details, provenance=prov,
            )
        # An unknown or missing direction: the record says a handoff was
        # initiated and no more. A handoff to nobody-in-particular.
        return _step(
            e, step_type="handoff", label="Handed off", actor=actor, system=None,
            details={**common, "target_label": None, "target_id": None}, provenance=prov,
        )

    if etype == "handoff_declined":
        return _step(
            e, step_type="exception", label="Handoff declined", actor=actor, system=None,
            details={"handoff_id": _str(payload.get("handoff_id")), "reason": reason},
            provenance=_provenance(e, payload, by_span, "handoff"),
        )

    if etype == "stall_detected":
        return _step(
            e, step_type="exception", label="Stall detected", actor=actor, system=None,
            details={"reason": reason, "detail": _str(payload.get("detail"))},
            provenance=_provenance(e, payload, by_span, None),
        )

    if etype == "loop_closed":
        abandoned = reason in _ABANDONED_REASONS
        return _step(
            e, step_type="completed",
            label="Work record closed as abandoned" if abandoned else "Work record closed",
            actor=actor, system=None,
            details={
                "reason": reason,
                "detail": _str(payload.get("detail")),
                "abandoned": abandoned,
                # The record ended. Whether the work's intended outcome happened
                # is not something this record can say.
                "outcome": "record_closed",
            },
            provenance=_provenance(e, payload, by_span, "completion"),
        )

    # loop_opened, handoff_accepted, handoff_completed, anything unknown:
    # lifecycle and possession only.
    return None


def _lifecycle_row(e: dict[str, Any], step: dict[str, Any] | None, by_span: dict[str, dict[str, Any]], people: _People) -> dict[str, Any]:
    payload = work_evidence._loads(e.get("payload"))
    eid = int(e["id"])
    provider = _str(payload.get("saas_provider"))
    return {
        "id": f"event:{eid}",
        "event_id": eid,
        "type": str(e.get("type") or ""),
        "at": _iso(e.get("event_time_unix")),
        "actor": _actor(e, payload, by_span, people),
        "direction": _str(payload.get("direction")),
        "handoff_id": _str(payload.get("handoff_id")),
        "saas_provider": provider.lower() if provider else None,
        "saas_effect": _str(payload.get("saas_effect")),
        "saas_event_type": _str(payload.get("saas_event_type")),
        "external_object_id": _str(payload.get("saas_object_id")),
        "external_event_id": _str(payload.get("saas_event_id")),
        "step_id": step["id"] if step else None,
        "possession_only": step is None and str(e.get("type") or "") != "loop_opened",
    }


def _possession_stream(
    spans: list[dict[str, Any]], events: list[dict[str, Any]], people: _People,
) -> list[dict[str, Any]]:
    """The same merged stream database._fetch_loop_stream(full=True) hands
    compute_loop_segments — lifecycle events plus one activity event per
    span, keyed (position, ts, lifecycle-before-span, id) — built from the
    rows already read, with to_human targets named the way
    _decorate_handoff_names names them (resolved, else "a person"; never
    the address)."""
    lp = database._loops_mod()
    keyed: list[tuple[tuple, dict[str, Any]]] = []
    for e in events:
        ev = lp.normalize_loop_event({
            "type": e.get("type"), "event_time_unix": e.get("event_time_unix"),
            "actor_type": e.get("actor_type"), "actor": e.get("actor"), "payload": e.get("payload"),
        })
        p = ev["payload"]
        if ev["type"] == "handoff_initiated" and p.get("direction") == "to_human" and p.get("target_id"):
            p["target_name"] = people.label(p.get("target_id"))
        if ev["actor_type"] == "human":
            ev["actor"] = people.label(ev.get("actor"))
        keyed.append(((lp.stream_position(ev), ev["ts"], 0, int(e["id"])), ev))
    for s in spans:
        attrs = work_evidence._loads(s.get("attributes"))
        tool = database.attr(attrs, "tool.name")
        failed = int(s.get("status_code") or 0) == _OTLP_STATUS_ERROR
        payload: dict[str, Any] = {
            "span_name": s.get("span_name"),
            "trace_id": s.get("trace_id"),
            "span_id": s.get("span_id"),
            "cost_usd": s.get("estimated_cost_usd"),
        }
        if tool:
            payload["tool"] = str(tool)
        if failed:
            payload["failed"] = True
            if s.get("status_message"):
                payload["error"] = str(s["status_message"])
        ev = lp.activity_event(
            s.get("start_time_unix"),
            actor=lp.agent_actor(s.get("service_name") or "", s.get("agent_id") or "main"),
            payload=payload,
        )
        keyed.append(((1, ev["ts"], 1, int(s.get("id") or 0)), ev))
    keyed.sort(key=lambda kv: kv[0])
    return [ev for _, ev in keyed]


def _segments(stream: list[dict[str, Any]], people: _People) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    lp = database._loops_mod()
    raw = lp.compute_loop_segments(stream)
    out = []
    for s in raw:
        holder_type = s.get("holder_type") or "agent"
        holder = _str(s.get("holder"))
        if holder_type == "human":
            # compute_loop_segments used target_name (already resolved above)
            # or "human"; never let an address through.
            label = holder if holder and holder not in ("human", "") and "@" not in holder else _A_PERSON
        elif holder_type == "system":
            label = _system_label(holder) or "a system"
        else:
            label = holder or None
        out.append({
            "holder_type": holder_type,
            "holder": label,
            "start": _iso(s.get("start_ns")),
            "end": _iso(s.get("end_ns")),
            "waiting": bool(s.get("waiting")),
            # Tools the holder reached for while holding the work — technical
            # names as recorded; using them is not possessing anything.
            "touches": [{"name": str(t.get("name")), "count": int(t.get("count") or 0)} for t in (s.get("touches") or [])],
            "event_count": int(s.get("event_count") or 0),
        })
    current = out[-1] if out and out[-1]["end"] is None else None
    return out, current


def graph_from_rows(account_id: int | None, item_id: int, raw: dict[str, Any]) -> dict[str, Any]:
    """The Work Graph for rows already read by
    database.get_work_item_execution_rows. Deterministic; fail-soft on
    malformed records; no model, no inference."""
    spans = raw.get("spans") or []
    events = raw.get("events") or []
    people = _People(account_id)
    by_span = _span_index(spans)

    steps: list[dict[str, Any]] = []
    lifecycle: list[dict[str, Any]] = []
    for e in events:
        try:
            step = _step_for_event(e, by_span, people)
        except (KeyError, TypeError, ValueError):
            step = None
        if step is not None:
            steps.append(step)
        try:
            lifecycle.append(_lifecycle_row(e, step, by_span, people))
        except (KeyError, TypeError, ValueError):
            continue

    # Chronology: persisted time, then the source id. Order, not causality.
    steps.sort(key=lambda s: (s["_ts"], s["_eid"]))
    lifecycle.sort(key=lambda r: (r["at"] or "", r["event_id"]))
    chronology = [s["id"] for s in steps]
    for s in steps:
        s.pop("_ts", None)
        s.pop("_eid", None)

    try:
        segments, current = _segments(_possession_stream(spans, events, people), people)
    except Exception:  # noqa: BLE001 — a malformed stream leaves possession unknown, never guessed
        segments, current = [], None

    by_type = {t: 0 for t in STEP_TYPES}
    for s in steps:
        by_type[s["type"]] = by_type.get(s["type"], 0) + 1
    tool_spans = 0
    errored_spans = 0
    for s in spans:
        attrs = work_evidence._loads(s.get("attributes"))
        if database.attr(attrs, "tool.name"):
            tool_spans += 1
        if int(s.get("status_code") or 0) == _OTLP_STATUS_ERROR:
            errored_spans += 1

    bounded = bool(raw.get("spans_truncated"))
    return {
        "item_id": item_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "steps": steps,
        "chronology": chronology,
        "possession": {"segments": segments, "current_holder": current},
        "lifecycle": lifecycle,
        "bounded": bounded,
        "span_limit": int(raw.get("span_limit") or 0),
        "spans_read": len(spans),
        "events_read": len(events),
        "summary": {
            "steps": len(steps),
            "handoffs": by_type["handoff"],
            "waits": by_type["wait"],
            "exceptions": by_type["exception"],
            "completed": by_type["completed"],
            "progress": by_type["progress"],
            # Level 3, counted so the reader knows it exists and did not
            # become steps: the execution underneath.
            "execution_only": {
                "spans_read": len(spans),
                "tool_call_spans": tool_spans,
                "errored_spans": errored_spans,
                "bounded": bounded,
            },
        },
    }


def build_work_item_graph(account_id: int | None, item_id: int) -> dict[str, Any] | None:
    """The Work Graph for one work item, or None when the item is not this
    account's. Reuses the Execution read (two bounded, account-scoped
    queries); no model calls."""
    limit = int(database._WORK_EXECUTION_SPAN_LIMIT)
    raw = database.get_work_item_execution_rows(account_id, item_id, span_limit=limit)
    if raw.get("loop") is None:
        return None
    raw["span_limit"] = limit
    return graph_from_rows(account_id, item_id, raw)
