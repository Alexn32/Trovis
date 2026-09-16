"""Work Evidence — provenance for the claims Trovis makes about one run.

Work stays the primary object. Its events (loop_events) keep saying what
Trovis believes happened; this module answers a narrower question for one
work item: "which stored observation supports that statement, and what does
it prove?" It is a READ MODEL over records that already exist — spans, loop
events, and the provider identifiers the SaaS spine writes onto them. It
persists nothing, calls no model, and never infers.

Vocabulary (the repo's, mapped): a work item == a loop == the frontend's
"run"; the executions inside it are spans; a "job" is a workflow.

Evidence types — the KIND OF SUPPORT an observation gives, never the vendor:

  execution         the worker's own telemetry shows it ran (its spans).
                    One record per attributable source (service, agent,
                    connector), not one per span: the claim is "this worker
                    executed", and a span waterfall is not evidence a
                    manager can use.
  action_reported   the worker's telemetry REPORTS an action — a named tool
                    call. It proves the worker said it did this, and whether
                    the call errored. It never proves the outcome.
  external_state    an independent external system (Stripe, HubSpot,
                    Shopify) reported the state of ITS OWN object, correlated
                    to this run by an explicit key the agent stamped on that
                    object. Carries the provider's event and object ids
                    exactly. This is what a later "verified by Stripe" can be
                    built on; the word is not used here, because Trovis has
                    not yet defined which systems are authoritative for
                    which outcomes.
  handoff           possession was offered, accepted, completed or declined,
                    by an agent, a person or a system.
  completion        the record says the work finished, or was abandoned —
                    with who closed it and why. Not an outcome verdict.
  cost              cost attributed to this run, pointing at the spans it
                    was summed from, with its basis (reported by the SDK,
                    estimated from tokens, or mixed).

Source identity: `source_type` is agent | human | system. `source_connector_id`
is the canonical connector (connect_health.identify_connector on the
span's resource stamp; the provider id for SaaS) — a bare service name never
becomes a vendor. `source_label` is the service/agent route, the person's
resolved name, or the provider label. Where a record was written without a
span and predates the span link, connector is None: not recorded.

Correlation — the mechanism Trovis KNOWS tied the observation to this run.
For spans it is read from `spans.loop_link`, which the ingest resolver
writes at the one moment it knows why it chose the loop (database.
_LOOP_LINKS). It is never rebuilt afterwards from the span's attributes: a
span carrying some key is not proof this loop was chosen through it, and a
keyless span sitting in a loop is not proof the gap rule put it there.

  explicit_key    the resolver matched the span's key to the loop (the open
                  loop with that key, a loop closed within the grace window,
                  the loop the key itself opened, or an earlier span in the
                  same batch that resolved that same key); for a SaaS event,
                  the spine resolved the explicit key in the provider
                  object's metadata to this loop.
  time_adjacency  the span carried no key and was placed with other keyless
                  spans of the same service and agent by proximity: the gap
                  rule (within the idle window) or arrival in the same export
                  batch as a keyless span so placed. Trovis really performs
                  this association, so it is named for what it is.
  origin          the span carried no key and opened this loop itself — the
                  item exists because of this observation.
  direct          the actor addressed this run by id — a person resolving a
                  handoff or closing it in the product, the sweep abandoning
                  it.
  None            not recorded: a span ingested before loop_link existed, or
                  a lifecycle event from before span links. Missing stays
                  missing — it is never filled in by guessing.

Execution and cost roll-ups carry one method only when every span rolled up
agrees; otherwise None, with the set listed in details.

Nothing here is Work Coverage. A run with rich evidence is a run Trovis can
account for, not a promise that Trovis saw everything.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import database
from connect_health import identify_connector

EVIDENCE_TYPES: tuple[str, ...] = (
    "execution",
    "action_reported",
    "external_state",
    "handoff",
    "completion",
    "cost",
)

CORRELATION_METHODS: tuple[str, ...] = ("explicit_key", "time_adjacency", "origin", "direct")

SOURCE_TYPES: tuple[str, ...] = ("agent", "human", "system")

# spans.loop_link (database._LOOP_LINKS, recorded at ingest) → public method.
# Anything not in this table — including NULL — is None: not recorded.
_LINK_TO_METHOD: dict[str, str] = {
    "key_open": "explicit_key",
    "key_grace": "explicit_key",
    "key_created": "explicit_key",
    "key_batch": "explicit_key",
    "gap": "time_adjacency",
    "keyless_batch": "time_adjacency",
    "created": "origin",
}

_SAAS_LABELS = {"stripe": "Stripe", "hubspot": "HubSpot", "shopify": "Shopify"}
_OTLP_STATUS_ERROR = 2


def _loads(text: Any) -> dict[str, Any]:
    if isinstance(text, dict):
        return text
    if not text:
        return {}
    try:
        v = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


def _iso(ns: Any) -> str | None:
    return database._ns_to_iso(int(ns)) if ns else None


def _span_correlation(loop_link: Any) -> str | None:
    """The recorded mechanism, normalized — or None when none was recorded.
    Deliberately takes only the stored link: the span's attributes are not
    consulted, so nothing is inferred from a key that happens to be present
    or absent."""
    if not isinstance(loop_link, str):
        return None
    return _LINK_TO_METHOD.get(loop_link)


def _record(
    *,
    rid: str,
    item_id: int,
    evidence_type: str,
    observed_at: str | None,
    source_type: str | None,
    source_connector_id: str | None,
    source_label: str | None,
    correlation_method: str | None,
    event_id: int | None = None,
    span_id: str | None = None,
    trace_id: str | None = None,
    external_object_id: str | None = None,
    external_event_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": rid,
        "item_id": item_id,
        "evidence_type": evidence_type,
        "observed_at": observed_at,
        "source_type": source_type,
        "source_connector_id": source_connector_id,
        "source_label": source_label,
        "correlation_method": correlation_method,
        "event_id": event_id,
        "span_id": span_id,
        "trace_id": trace_id,
        "external_object_id": external_object_id,
        "external_event_id": external_event_id,
        "details": details or {},
    }


def _span_evidence(
    item_id: int, spans: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """execution (rolled up per source), action_reported (per tool span),
    cost (rolled up per source). Also returns the span index the event pass
    uses to find the connector behind an ingest-written event."""
    lp = database._loops_mod()
    out: list[dict[str, Any]] = []
    by_span: dict[str, dict[str, Any]] = {}
    execs: dict[tuple[str, str, str], dict[str, Any]] = {}
    costs: dict[tuple[str, str, str], dict[str, Any]] = {}

    for s in spans:
        attrs = _loads(s.get("attributes"))
        connector, _method = identify_connector(s.get("resource_attributes"))
        service = s.get("service_name") or ""
        agent_id = s.get("agent_id") or "main"
        label = lp.agent_actor(service, agent_id)
        at = _iso(s.get("start_time_unix"))
        link = s.get("loop_link")
        corr = _span_correlation(link)
        sid = str(s.get("span_id") or "") or None
        tid = str(s.get("trace_id") or "") or None
        if sid:
            by_span[sid] = {"connector": connector, "label": label, "correlation": corr}

        key = (service, agent_id, connector)
        ex = execs.get(key)
        if ex is None:
            ex = execs[key] = {
                "first": at, "last": at, "count": 0, "errors": 0, "traces": set(),
                "correlations": set(), "label": label,
            }
        ex["count"] += 1
        if at and (ex["first"] is None or at < ex["first"]):
            ex["first"] = at
        if at and (ex["last"] is None or at > ex["last"]):
            ex["last"] = at
        if int(s.get("status_code") or 0) == _OTLP_STATUS_ERROR:
            ex["errors"] += 1
        if tid:
            ex["traces"].add(tid)
        ex["correlations"].add(corr)

        tool = lp.span_tool(s.get("span_name"), database.attr(attrs, "tool.name"))
        if tool:
            errored = int(s.get("status_code") or 0) == _OTLP_STATUS_ERROR
            out.append(_record(
                rid=f"span:{sid}" if sid else f"span:{service}:{at}",
                item_id=item_id,
                evidence_type="action_reported",
                observed_at=at,
                source_type="agent",
                source_connector_id=connector,
                source_label=label,
                correlation_method=corr,
                span_id=sid,
                trace_id=tid,
                details={
                    "tool": tool,
                    "span_name": s.get("span_name"),
                    "errored": errored,
                    "error": database._run_error_line(s.get("status_message")) if errored else None,
                    # The worker's report of an action, not its outcome.
                    "proves": "reported",
                    # The resolver's own word for how this span was placed.
                    "assignment": link if isinstance(link, str) else None,
                },
            ))

        cost = s.get("estimated_cost_usd")
        try:
            cost = float(cost) if cost is not None else None
        except (TypeError, ValueError):
            cost = None
        if cost is not None and cost > 0:
            c = costs.get(key)
            if c is None:
                c = costs[key] = {"amount": 0.0, "spans": [], "sources": set(), "last": at, "label": label}
            c["amount"] += cost
            if sid:
                c["spans"].append(sid)
            # cost_source: 'reported' (SDK-authoritative), NULL = token estimate.
            c["sources"].add(s.get("cost_source") or "estimated")
            if at and (c["last"] is None or at > c["last"]):
                c["last"] = at

    for (service, agent_id, connector), ex in execs.items():
        methods = sorted(m for m in ex["correlations"] if m is not None)
        unrecorded = None in ex["correlations"]
        # One method only when every rolled-up span carries that same
        # recorded method; a mix, or any span with none recorded, is None.
        corr = methods[0] if (len(methods) == 1 and not unrecorded) else None
        out.append(_record(
            rid=f"exec:{service}:{agent_id}:{connector}",
            item_id=item_id,
            evidence_type="execution",
            observed_at=ex["first"],
            source_type="agent",
            source_connector_id=connector,
            source_label=ex["label"],
            # None when the source's spans were tied in more than one way.
            correlation_method=corr,
            details={
                "span_count": ex["count"],
                "error_count": ex["errors"],
                "last_observed_at": ex["last"],
                "trace_ids": sorted(ex["traces"])[:50],
                # Every method seen across the rolled-up spans; "unrecorded"
                # stands for spans ingested before the link was stored.
                "correlation_methods": methods + (["unrecorded"] if unrecorded else []),
            },
        ))

    for (service, agent_id, connector), c in costs.items():
        basis = c["sources"].pop() if len(c["sources"]) == 1 else "mixed"
        out.append(_record(
            rid=f"cost:{service}:{agent_id}:{connector}",
            item_id=item_id,
            evidence_type="cost",
            observed_at=c["last"],
            source_type="agent",
            source_connector_id=connector,
            source_label=c["label"],
            correlation_method=None,
            details={
                "amount_usd": round(c["amount"], 6),
                "basis": basis,
                "span_count": len(c["spans"]),
                "span_ids": c["spans"][:50],
            },
        ))
    return out, by_span


def _event_evidence(
    account_id: int | None,
    item_id: int,
    events: list[dict[str, Any]],
    by_span: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    human_names: dict[str, str | None] = {}

    def human(actor: str) -> str | None:
        if actor not in human_names:
            human_names[actor] = database.resolve_human_label(account_id, actor)
        return human_names[actor]

    for e in events:
        etype = e.get("type") or ""
        payload = _loads(e.get("payload"))
        actor_type = e.get("actor_type") or ""
        actor = e.get("actor") or ""
        at = _iso(e.get("event_time_unix"))
        eid = int(e["id"])
        span_id = e.get("span_id") or None
        trace_id = e.get("trace_id") or None
        provider = payload.get("saas_provider")

        if etype == "loop_opened":
            # Not a claim about the work; the execution / suggestion origin
            # is carried elsewhere (WorkItemProvenance).
            continue

        if actor_type == "system" and provider:
            # The SaaS spine: an external system reported its object's state,
            # tied to this run by the explicit key in that object's metadata.
            out.append(_record(
                rid=f"event:{eid}",
                item_id=item_id,
                evidence_type="external_state",
                observed_at=at,
                source_type="system",
                source_connector_id=str(provider).lower(),
                source_label=_SAAS_LABELS.get(str(provider).lower(), str(provider)),
                correlation_method="explicit_key",
                event_id=eid,
                external_object_id=payload.get("saas_object_id"),
                external_event_id=payload.get("saas_event_id"),
                details={
                    "provider_event_type": payload.get("saas_event_type"),
                    "effect": payload.get("saas_effect") or ("clear" if etype == "handoff_completed" else None),
                    "event": etype,
                    "waiting_on": payload.get("waiting_on"),
                    "reason": payload.get("reason"),
                    # Event / object ids of a CLEAR written before this PR
                    # were not recorded; a None here means exactly that.
                    "provider_ids_recorded": bool(payload.get("saas_event_id") or payload.get("saas_object_id")),
                },
            ))
            continue

        if actor_type == "agent":
            src = by_span.get(span_id) if span_id else None
            connector = src["connector"] if src else None
            corr = src["correlation"] if src else None
            label = src["label"] if src else (actor or None)
        elif actor_type == "human":
            connector, corr, label = None, "direct", human(actor)
        else:
            connector, corr, label = None, "direct", "Trovis"

        if etype.startswith("handoff_"):
            out.append(_record(
                rid=f"event:{eid}",
                item_id=item_id,
                evidence_type="handoff",
                observed_at=at,
                source_type=actor_type or None,
                source_connector_id=connector,
                source_label=label,
                correlation_method=corr,
                event_id=eid,
                span_id=span_id,
                trace_id=trace_id,
                details={
                    "event": etype,
                    "direction": payload.get("direction"),
                    "target_id": payload.get("target_id"),
                    "handoff_id": payload.get("handoff_id"),
                    "reason": payload.get("reason"),
                },
            ))
        elif etype == "loop_closed":
            out.append(_record(
                rid=f"event:{eid}",
                item_id=item_id,
                evidence_type="completion",
                observed_at=at,
                source_type=actor_type or None,
                source_connector_id=connector,
                source_label=label,
                correlation_method=corr,
                event_id=eid,
                span_id=span_id,
                trace_id=trace_id,
                details={
                    "reason": payload.get("reason"),
                    "detail": payload.get("detail"),
                    # The record says it finished; nothing here says it went well.
                    "proves": "recorded_close",
                },
            ))
    return out


def build_work_item_evidence(account_id: int | None, item_id: int) -> dict[str, Any] | None:
    """Evidence for one work item, or None when the item is not this
    account's. Deterministic; two bounded reads; no model calls."""
    raw = database.get_work_item_evidence_rows(account_id, item_id)
    loop = raw.get("loop")
    if loop is None:
        return None
    span_records, by_span = _span_evidence(item_id, raw["spans"])
    event_records = _event_evidence(account_id, item_id, raw["events"], by_span)
    evidence = span_records + event_records
    evidence.sort(key=lambda r: (r["observed_at"] or "", r["id"]))
    return {
        "item_id": item_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "spans_truncated": bool(raw.get("spans_truncated")),
        "evidence": evidence,
    }
