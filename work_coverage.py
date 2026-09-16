"""Work Coverage — which dimensions of one run Trovis can actually see.

Coverage answers, dimension by dimension, "does this work record carry a
direct observation of this kind?" It is a READ MODEL over Work Evidence
(work_evidence.py) plus the per-span cost facts already stored. It persists
nothing, calls no model, scores nothing, and never says how good the work
was, how well it went, or how confident Trovis is.

The chain this sits in, and must not collapse:

  Connections  what sources can Trovis receive from?      (connect_health)
  Evidence     what observations support this run?        (work_evidence)
  Coverage     which dimensions of this run are observed?  (this module)
  later        visibility gaps, then recommendations

Unit: one work item (a loop — the frontend's "run"). Job-level roll-ups
raise questions this data does not answer yet (over how many runs, what
window, which sources were expected) and are deliberately absent.

Dimensions — the smallest set the persisted record supports:

  execution          the worker's own telemetry shows it ran   (evidence `execution`)
  actions            the worker REPORTED an action              (`action_reported`)
  external_outcomes  an external system SHOWED its own state    (`external_state`)
  handoffs           possession moved, or was offered, between  (`handoff`)
                     an agent and a person or another agent. A
                     SaaS wait is filed by the evidence model as
                     an external observation (the provider spoke
                     about its own object), so it counts under
                     external_outcomes, not here.
  cost               cost for the observed model usage          (spans + `cost`)

States — every one has a precise meaning, and a dimension may use only
the states it can prove:

  observed       a persisted observation directly supports the dimension.
  partial        Trovis KNOWS the denominator and only part of it is
                 observed. Cost only: usage spans are the denominator;
                 those with a recorded cost the numerator (below).
  not_observed   Trovis KNOWS the dimension applies and has no supporting
                 observation. Cost only: model usage was observed on the
                 run's spans and no cost was recorded for any of it.
  unknown        the record cannot establish applicability or completeness.
                 The honest default for absence: no execution evidence does
                 not mean no worker ran; no action report does not mean no
                 action; no external observation does not mean no external
                 system was involved; no handoff record does not mean
                 nothing changed hands. The record shows what Trovis saw,
                 not what the workflow should have contained.

`not_applicable` is NOT a state here: nothing persisted proves a dimension
did not apply to a run. `partial` is used for cost alone, because only
there is the denominator known; "an action was reported and some external
state was observed" does not establish that they correspond or that
anything is missing, so external_outcomes is never partial.

Completion is not coverage: a closed record says the work finished, not
that its outcome was seen. Failure is not coverage either: a run that
errored can be fully observed, and a closed run with a thin record earns
nothing from having closed.

The cost denominator, exactly (the invariant is documented where ingest
writes it, database._insert_span_rows):

  usage span      a span whose OWN attributes carried model usage at ingest
                  — stored as non-NULL `spans.total_tokens` (0 when the usage
                  reported zero tokens; NULL only when no usage attribute
                  was present). Never set from a run-level aggregate or a
                  default. The unit is the span as the exporter cut it: one
                  model call for every Trovis-owned door, but Trovis cannot
                  prove a third-party exporter never put usage on a parent
                  span too, so the API says "usage spans", not "model
                  calls".
  cost known      `estimated_cost_usd IS NOT NULL` — the same predicate the
                  cost aggregates use for priced_n. It covers three things
                  ingest records deliberately: a token-derived estimate
                  (a number, 0.0 included), a cost the SDK reported, and
                  `cost_source='covered'`: a usage span whose cost is
                  SUBSUMED in a run total the SDK reported, stored as 0.
                  Covered means "inside a known total", not "this span was
                  priced on its own"; the count of such spans is exposed.
  cost unknown    a usage span with NULL cost — the model was not in the
                  pricing table, or the row predates pricing. The cost
                  aggregates call this unpriced_n; it is the one case where
                  Trovis knows cost applied and has none.

Reasons — a small deterministic vocabulary a UI can explain without a model:

  execution_evidence / no_execution_evidence
  action_reports / no_action_reports
  external_observations / no_external_observations
  handoff_records / no_handoff_records
  reported_cost               a cost was reported although no usage span exists
  usage_cost_known            every usage span has a recorded cost (own or covered)
  some_usage_cost_unknown     partial: some usage spans have no recorded cost
  usage_cost_unknown          usage observed, no recorded cost for any of it
  no_model_usage_observed     no usage span and no cost on the record

Bounded evidence: the evidence read is capped at database.
_WORK_EVIDENCE_SPAN_LIMIT spans. The span-derived dimensions (execution,
actions, cost) then say `from_bounded_evidence: true`. A bound is not
missing coverage: it never changes a state, it only tells the reader the
result was computed from a prefix of the record. Event-derived dimensions
(external_outcomes, handoffs) read the whole event record and are never
bounded.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import database
import work_evidence

DIMENSIONS: tuple[str, ...] = (
    "execution", "actions", "external_outcomes", "handoffs", "cost",
)

STATE_OBSERVED = "observed"
STATE_PARTIAL = "partial"
STATE_NOT_OBSERVED = "not_observed"
STATE_UNKNOWN = "unknown"
STATES: tuple[str, ...] = (STATE_OBSERVED, STATE_PARTIAL, STATE_NOT_OBSERVED, STATE_UNKNOWN)

REASONS: tuple[str, ...] = (
    "execution_evidence", "no_execution_evidence",
    "action_reports", "no_action_reports",
    "external_observations", "no_external_observations",
    "handoff_records", "no_handoff_records",
    "reported_cost", "usage_cost_known", "some_usage_cost_unknown",
    "usage_cost_unknown", "no_model_usage_observed",
)

# dimension -> (evidence types it counts, reason when present, reason when absent,
#               derived from the bounded span read?)
_EVIDENCE_DIMENSIONS: dict[str, tuple[tuple[str, ...], str, str, bool]] = {
    "execution": (("execution",), "execution_evidence", "no_execution_evidence", True),
    "actions": (("action_reported",), "action_reports", "no_action_reports", True),
    "external_outcomes": (("external_state",), "external_observations", "no_external_observations", False),
    "handoffs": (("handoff",), "handoff_records", "no_handoff_records", False),
}

def _iso_max(values: list[str | None]) -> str | None:
    present = [v for v in values if v]
    return max(present) if present else None


def _source_key(rec: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    return (rec.get("source_type"), rec.get("source_connector_id"), rec.get("source_label"))


def _summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    """The auditable shape common to every dimension: how many records,
    the newest actual observation, which evidence types, which distinct
    sources, and which correlation methods (None recorded as
    "unrecorded"), all from the records themselves."""
    seen: dict[tuple, dict[str, Any]] = {}
    for r in records:
        k = _source_key(r)
        if k not in seen:
            seen[k] = {
                "source_type": r.get("source_type"),
                "source_connector_id": r.get("source_connector_id"),
                "source_label": r.get("source_label"),
            }
    last = _iso_max(
        [r.get("observed_at") for r in records]
        + [(r.get("details") or {}).get("last_observed_at") for r in records],
    )
    # A roll-up record (execution, cost) carries the full set of methods its
    # spans were tied by in details; a single record carries one. Either
    # way the set is what the evidence recorded, "unrecorded" standing for
    # a span with no stored link.
    methods_set: set[str] = set()
    for r in records:
        listed = (r.get("details") or {}).get("correlation_methods")
        if isinstance(listed, list) and listed:
            methods_set.update(str(m) for m in listed)
        else:
            methods_set.add(r.get("correlation_method") or "unrecorded")
    methods = sorted(methods_set)
    return {
        "evidence_count": len(records),
        "last_observed_at": last,
        "evidence_types": sorted({r["evidence_type"] for r in records}),
        "sources": list(seen.values()),
        "correlation_methods": methods,
    }


def _evidence_dimension(
    dim: str, evidence: list[dict[str, Any]], bounded: bool,
) -> dict[str, Any]:
    types, present, absent, span_derived = _EVIDENCE_DIMENSIONS[dim]
    recs = [r for r in evidence if r.get("evidence_type") in types]
    summary = _summarize(recs)
    return {
        "id": dim,
        "state": STATE_OBSERVED if recs else STATE_UNKNOWN,
        "reason": present if recs else absent,
        **summary,
        "from_bounded_evidence": bool(bounded and span_derived),
        "details": {},
    }


def _cost_dimension(
    spans: list[dict[str, Any]], evidence: list[dict[str, Any]], bounded: bool,
) -> dict[str, Any]:
    """Cost against the observed model usage, by the predicates the cost
    aggregates already use (module docstring): a usage span has non-NULL
    total_tokens; its cost is known when estimated_cost_usd is non-NULL
    (own estimate, 0.0 included; reported; or covered = subsumed in a
    reported run total). Nothing is summed to zero."""
    usage = 0
    known = 0
    covered = 0
    any_cost = False
    for s in spans:
        has_usage = s.get("total_tokens") is not None
        cost = s.get("estimated_cost_usd")
        try:
            cost_f = float(cost) if cost is not None else None
        except (TypeError, ValueError):
            cost_f = None
        if cost_f is not None and cost_f > 0:
            any_cost = True
        if has_usage:
            usage += 1
            if cost_f is not None:
                known += 1
            if s.get("cost_source") == "covered":
                covered += 1
    unknown = usage - known

    if usage == 0 and not any_cost:
        state, reason = STATE_UNKNOWN, "no_model_usage_observed"
    elif usage == 0:
        state, reason = STATE_OBSERVED, "reported_cost"
    elif unknown == 0:
        state, reason = STATE_OBSERVED, "usage_cost_known"
    elif known == 0:
        state, reason = STATE_NOT_OBSERVED, "usage_cost_unknown"
    else:
        state, reason = STATE_PARTIAL, "some_usage_cost_unknown"

    cost_recs = [r for r in evidence if r.get("evidence_type") == "cost"]
    summary = _summarize(cost_recs)
    amount = None
    bases: set[str] = set()
    for r in cost_recs:
        d = r.get("details") or {}
        try:
            amount = (amount or 0.0) + float(d.get("amount_usd") or 0.0)
        except (TypeError, ValueError):
            pass
        if d.get("basis"):
            bases.add(str(d["basis"]))
    return {
        "id": "cost",
        "state": state,
        "reason": reason,
        **summary,
        "from_bounded_evidence": bool(bounded),
        "details": {
            # Spans, as the exporter cut them — not "model calls".
            "usage_spans": usage,
            "cost_known_spans": known,
            # Of the known: subsumed in a reported run total, not priced alone.
            "cost_covered_spans": covered,
            "cost_unknown_spans": unknown,
            # None, never 0, when no cost was recorded.
            "amount_usd": round(amount, 6) if amount else None,
            "basis": sorted(bases)[0] if len(bases) == 1 else ("mixed" if bases else None),
        },
    }


def build_work_item_coverage(account_id: int | None, item_id: int) -> dict[str, Any] | None:
    """Coverage for one work item, or None when the item is not this
    account's. One evidence read (the same bounded, account-scoped rows the
    evidence endpoint uses); deterministic; no model calls."""
    raw = database.get_work_item_evidence_rows(
        account_id, item_id, span_limit=database._WORK_EVIDENCE_SPAN_LIMIT,
    )
    if raw.get("loop") is None:
        return None
    evidence = work_evidence.evidence_from_rows(account_id, item_id, raw)
    bounded = bool(raw.get("spans_truncated"))
    dimensions = [
        _evidence_dimension("execution", evidence, bounded),
        _evidence_dimension("actions", evidence, bounded),
        _evidence_dimension("external_outcomes", evidence, bounded),
        _evidence_dimension("handoffs", evidence, bounded),
        _cost_dimension(raw["spans"], evidence, bounded),
    ]
    return {
        "item_id": item_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "evidence_bounded": bounded,
        "dimensions": dimensions,
    }
