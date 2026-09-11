"""Home's authoritative snapshot: one bounded read of the record.

This module is the orchestration layer behind `GET /home/snapshot`. It owns
period/timezone arithmetic, seat and scope resolution, the financial-visibility
policy, and the shaping of the response. The SQL it runs lives in
`database.get_home_snapshot_rows` — one connection, a fixed number of bounded
aggregates, no N+1 and no per-row fan-out.

What this endpoint is for
-------------------------
Home is about to become a page that shows what work got done, what needs a
person's attention, what it cost, and where it could be better. Every one of
those claims has to come from a record, and a page that assembles its own
numbers from half a dozen list endpoints ends up with numbers that disagree
with each other. So: one endpoint, one set of definitions, and every field
documented at the point it is produced.

Three rules this module exists to keep
--------------------------------------
1. **Current state and period totals are different kinds of measure.**
   "17 pieces of work are moving right now" and "43 finished this week" answer
   different questions and are never merged into one number. They live in two
   separate objects (`current_state`, `period`) and are labeled as such.

2. **Missing is not zero.** An org four days old did not "complete 0 last
   week" — it has no last week. Anything we cannot establish comes back as
   `null` with a named reason, never as a confident 0.

3. **Recorded is not verified.** A closed work item is a *recorded completion*.
   It is not proof the business outcome happened, and nothing here says or
   implies that it is.

No model is called on this path. Later AI analysis is expected to read this
snapshot rather than re-derive its own numbers.
"""
from __future__ import annotations

import bisect
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import database

# Bounds. A period is a window, not a history dump: 90 local days is already
# more than any Home visual asks for, and it keeps the completion scan inside
# one index range.
MIN_PERIOD_DAYS = 1
MAX_PERIOD_DAYS = 90
DEFAULT_PERIOD_DAYS = 7
DEFAULT_TIMEZONE = "UTC"

# How many job rows the breakdown returns before the tail is rolled into one
# labeled "other" row. The rollup exists so the breakdown still reconciles
# with the period total — a truncated list that silently loses rows is the
# kind of chart that makes a reader distrust the whole page.
JOB_ROW_LIMIT = 12

# The completion series folds raw `closed_at` values in Python (SQL date
# bucketing is not portable across SQLite and Postgres, and local-calendar
# buckets are not expressible in either without a timezone table). That fold
# is bounded: past this many completions in one period the series comes back
# unavailable with a reason, while the aggregate total stays exact.
SERIES_ROW_CAP = 50_000

# Whose-work choices, mirroring main._WHOSE_CHOICES. Repeated as a tuple here
# only so the snapshot can publish the *permitted* subset; the enforcement is
# still main._resolve_whose_work, which intersects with the seat.
WHOSE_CHOICES = ("everyone", "me", "team", "person")

# The seat surface that gates money. Named once; never branched on a scope
# level's NAME or a role title (a custom level composed with the Cost atom
# gets money, a preset renamed "Manager" without it does not).
FINANCIAL_SURFACE = "Cost"


class SnapshotInputError(ValueError):
    """Malformed query input. The endpoint turns this into a 400."""


# ---------------------------------------------------------------------------
# Period + timezone
# ---------------------------------------------------------------------------


def resolve_period(
    days: int | None,
    tz_name: str | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Resolve a bounded period in an IANA timezone.

    The period runs from local midnight `days - 1` days ago up to *now*. The
    last bucket is therefore a partial local day, which is what a person
    looking at Home actually wants ("this week, so far").

    Buckets are LOCAL CALENDAR DAYS, so a daylight-saving day is one bucket of
    23 or 25 hours rather than two ragged ones. Boundaries are computed in the
    requested zone and converted to UTC for the query, so a period never
    silently becomes a UTC period.

    The comparison window is the equal-duration window immediately preceding
    the period: `[start - (end - start), start)`. It is anchored to the period
    start rather than to local midnight, deliberately — anchoring to midnight
    while the current period is mid-day would make the two windows different
    lengths, and a comparison between unequal windows is not a comparison.
    """
    if days is None:
        days = DEFAULT_PERIOD_DAYS
    try:
        days = int(days)
    except (TypeError, ValueError):
        raise SnapshotInputError("days must be a whole number") from None
    if not MIN_PERIOD_DAYS <= days <= MAX_PERIOD_DAYS:
        raise SnapshotInputError(
            f"days must be between {MIN_PERIOD_DAYS} and {MAX_PERIOD_DAYS}"
        )

    raw_tz = (tz_name or DEFAULT_TIMEZONE).strip() or DEFAULT_TIMEZONE
    if len(raw_tz) > 64:
        raise SnapshotInputError("tz is not a valid IANA timezone")
    try:
        tz = ZoneInfo(raw_tz)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        raise SnapshotInputError(f"unknown IANA timezone: {raw_tz}") from None

    now_utc = now.astimezone(timezone.utc) if now else datetime.now(timezone.utc)
    now_local = now_utc.astimezone(tz)

    # Local calendar-day boundaries. Built from dates, not by subtracting 24h,
    # so a DST day stays one day.
    first_date = now_local.date() - timedelta(days=days - 1)
    bucket_dates = [first_date + timedelta(days=i) for i in range(days)]
    bucket_starts_local = [
        datetime(d.year, d.month, d.day, tzinfo=tz) for d in bucket_dates
    ]
    start_local = bucket_starts_local[0]
    start_utc = start_local.astimezone(timezone.utc)
    end_utc = now_utc

    duration = end_utc - start_utc
    prev_start_utc = start_utc - duration
    prev_end_utc = start_utc

    return {
        "timezone": raw_tz,
        "tz": tz,
        "days": days,
        "start": start_local.isoformat(),
        "end": now_local.isoformat(),
        "start_utc": start_utc,
        "end_utc": end_utc,
        "previous_start_utc": prev_start_utc,
        "previous_end_utc": prev_end_utc,
        "bucket_starts_local": bucket_starts_local,
    }


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


def scope_choices(seat: dict[str, Any] | None) -> list[str]:
    """The whose-work selectors this seat may actually use.

    Mirrors the client's `whoseOptions` rule without importing its labels:
    team/person only exist for someone with reports. A machine session has no
    seat and therefore exactly one choice — the account-wide view it has
    always had.
    """
    if not seat:
        return ["everyone"]
    choices = ["everyone", "me"]
    if seat.get("subtree_user_ids"):
        choices.extend(["team", "person"])
    return choices


def describe_scope(
    *,
    seat: dict[str, Any] | None,
    requested: str | None,
    person_id: int | None,
    effective_user_ids: list[int] | None,
    viewer_user_id: int | None,
    account_id: int | None,
) -> dict[str, Any]:
    """The scope block: what was asked for, what was granted, what is possible.

    `effective_user_ids` is the ALREADY-INTERSECTED answer from
    `main._resolve_whose_work` — this function reports it, it does not
    re-derive it, so there is exactly one place where a request can narrow
    (and never widen) what a person sees.

    `people_in_scope` is `null` for a company-breadth seat because the filter
    is skipped entirely there. That is not "nobody": work can be held by a
    person with no login, and an id list would silently drop those rows.
    """
    choices = scope_choices(seat)
    asked = (requested or "").strip().lower() or "everyone"
    if asked not in WHOSE_CHOICES:
        asked = "everyone"
    effective = asked if asked in choices else "everyone"
    return {
        "requested": asked,
        "effective": effective,
        "person_id": person_id if effective == "person" else None,
        "choices": choices,
        "narrowed_from_request": effective != asked,
        # Seat atoms, never the scope level's name or a role title.
        "breadth": (seat or {}).get("breadth"),
        "scope_level_id": (seat or {}).get("scope_level_id"),
        "role_id": (seat or {}).get("role_id"),
        "filtered": effective_user_ids is not None,
        "people_in_scope": (
            None if effective_user_ids is None else len(effective_user_ids)
        ),
        "account_id": account_id,
        "viewer_user_id": viewer_user_id,
    }


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _epoch_iso(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def build_snapshot(
    *,
    account_id: int | None,
    viewer_user_id: int | None,
    seat: dict[str, Any] | None,
    effective_user_ids: list[int] | None,
    requested_whose: str | None,
    person_id: int | None,
    period: dict[str, Any],
) -> dict[str, Any]:
    """Assemble the snapshot. One DB round trip, no model call, no cache.

    Every count below is over the COMPLETE permitted dataset — these are SQL
    aggregates, never a fold over the first page of `/work/items`.
    """
    financial_visible = bool(seat) and FINANCIAL_SURFACE in (seat or {}).get(
        "surfaces", []
    )
    if not seat:
        # A machine credential has no person and therefore no seat. Money is a
        # seat-gated surface, and inventing a seat for an API key would be
        # inventing a person. Documented, deliberate, and tested.
        financial_reason = "no_seat_for_machine_session"
    elif financial_visible:
        financial_reason = None
    else:
        financial_reason = "seat_excludes_financial_surface"

    rows = database.get_home_snapshot_rows(
        account_id,
        only_user_ids=effective_user_ids,
        viewer_user_id=viewer_user_id,
        start_utc=period["start_utc"],
        end_utc=period["end_utc"],
        previous_start_utc=period["previous_start_utc"],
        previous_end_utc=period["previous_end_utc"],
        include_cost=financial_visible,
        series_cap=SERIES_ROW_CAP,
    )

    completed = int(rows["completed_total"])
    series = _completion_series(rows, period, completed)
    jobs = _job_breakdown(rows, completed)
    comparison = _comparison(rows, period, completed)
    attention = _attention(rows, viewer_user_id, seat)
    financial = _financial(
        rows, period, visible=financial_visible, reason=financial_reason
    )

    has_any_work = bool(rows["has_any_work"])
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": describe_scope(
            seat=seat,
            requested=requested_whose,
            person_id=person_id,
            effective_user_ids=effective_user_ids,
            viewer_user_id=viewer_user_id,
            account_id=account_id,
        ),
        "period": {
            "start": period["start"],
            "end": period["end"],
            "start_utc": _iso(period["start_utc"]),
            "end_utc": _iso(period["end_utc"]),
            "timezone": period["timezone"],
            "days": period["days"],
            # Recorded completions whose close timestamp falls in the window.
            # A "completion" is one closed work item — never a span, a tool
            # call, a nested child run, or an agent registration.
            "completed": completed,
            "comparison": comparison,
        },
        "current_state": _current_state(rows),
        "completions_series": series,
        "by_job": jobs,
        "attention": attention,
        "financial": financial,
        "freshness": {
            "latest_recorded_completion_at": _epoch_iso(
                rows["latest_completion_epoch"]
            ),
            "latest_work_activity_at": _epoch_iso(rows["latest_work_event_epoch"]),
            "latest_telemetry_at": _epoch_iso(rows["latest_span_epoch"]),
            "first_recorded_work_at": _epoch_iso(rows["first_work_epoch"]),
        },
        "completeness": {
            # An empty workspace and a workspace with a quiet week are
            # different answers, and Home must be able to tell them apart.
            "has_any_recorded_work": has_any_work,
            "workspace_state": "empty" if not has_any_work else "populated",
            "completion_series_complete": series["available"],
            "job_breakdown_complete": not jobs["truncated"],
            "comparison_available": comparison["available"],
            "financial_available": financial["visible"],
            "unavailable": _unavailable_reasons(
                series, jobs, comparison, financial, attention
            ),
        },
        "navigation": _navigation(
            account_id=account_id,
            whose=requested_whose,
            person_id=person_id,
            period=period,
        ),
    }


def _current_state(rows: dict[str, Any]) -> dict[str, Any]:
    """Work as it stands RIGHT NOW. Not a period measure, and never mixed
    with one.

    The split comes from `loops.cached_state`, the same engine state
    `/work/overview` counts `needs_attention` from — not a second definition
    of what "moving" means. `blocked` is the engine's stalled/awaiting-system
    states; `waiting_on_person` is awaiting_human.

    There is deliberately NO historical series for these. Current state is a
    snapshot of the present; the record does not retain what it was last
    Tuesday, and reconstructing a trend from today's values would be a chart
    of nothing.
    """
    by_state = rows["open_by_state"]
    return {
        "as_of": "now",
        "open": int(rows["open_total"]),
        "moving": int(by_state.get("moving", 0)),
        "waiting_on_person": int(by_state.get("waiting_on_person", 0)),
        "blocked": int(by_state.get("blocked", 0)),
        "trend_available": False,
        "trend_unavailable_reason": "current_state_is_not_retained_historically",
    }


def _completion_series(
    rows: dict[str, Any], period: dict[str, Any], completed: int
) -> dict[str, Any]:
    """Recorded completions per local calendar day.

    Bucketed on the COMPLETION timestamp (`loops.closed_at`), never on when
    the work started or was last touched. Buckets are local days in the
    requested zone, so a DST day is one bucket.

    `reconciles` is asserted, not assumed: the bucket sum is compared against
    the independent aggregate COUNT, and a mismatch is reported rather than
    smoothed over.
    """
    starts = period["bucket_starts_local"]
    edges = [s.astimezone(timezone.utc).timestamp() for s in starts]
    points = [
        {
            "bucket_start": s.isoformat(),
            "bucket_start_utc": s.astimezone(timezone.utc).isoformat(),
            "completed": 0,
        }
        for s in starts
    ]

    epochs = rows["completed_epochs"]
    if epochs is None:
        return {
            "available": False,
            "unavailable_reason": "too_many_completions_to_bucket",
            "bucket": "local_day",
            "timezone": period["timezone"],
            "points": [],
            "total": None,
            "reconciles": None,
            "aggregate_total": completed,
        }

    for e in epochs:
        idx = bisect.bisect_right(edges, e) - 1
        if idx < 0:
            idx = 0
        if idx >= len(points):
            idx = len(points) - 1
        points[idx]["completed"] += 1

    bucket_sum = sum(p["completed"] for p in points)
    return {
        "available": True,
        "unavailable_reason": None,
        "bucket": "local_day",
        "timezone": period["timezone"],
        "points": points,
        "total": bucket_sum,
        "aggregate_total": completed,
        "reconciles": bucket_sum == completed,
    }


def _job_breakdown(rows: dict[str, Any], completed: int) -> dict[str, Any]:
    """Recorded completions by the job (declared workflow) the run belongs to.

    Uses the EXISTING job identity — `loops.workflow_id` and the workflow's
    own name. Nothing here classifies work by outcome, quality, or success;
    Trovis has no such taxonomy and this PR does not invent one.

    Work the matcher never claimed is reported explicitly as `unclassified`
    rather than dropped or folded into a neighbour.
    """
    raw = rows["by_job"]
    unclassified = 0
    named: list[dict[str, Any]] = []
    for r in raw:
        if r["workflow_id"] is None:
            unclassified += int(r["completed"])
        else:
            named.append(
                {
                    "workflow_id": int(r["workflow_id"]),
                    "name": r["name"],
                    "completed": int(r["completed"]),
                }
            )
    named.sort(key=lambda r: (-r["completed"], r["workflow_id"]))

    truncated = len(named) > JOB_ROW_LIMIT
    head = named[:JOB_ROW_LIMIT]
    tail_total = sum(r["completed"] for r in named[JOB_ROW_LIMIT:])

    accounted = sum(r["completed"] for r in head) + tail_total + unclassified
    return {
        "rows": head,
        "row_limit": JOB_ROW_LIMIT,
        "truncated": truncated,
        # Present so a chart built from `rows` still adds up to the period
        # total instead of quietly losing the tail.
        "other_completed": tail_total,
        "unclassified_completed": unclassified,
        "total_job_count": len(named),
        "aggregate_total": completed,
        "reconciles": accounted == completed,
    }


def _comparison(
    rows: dict[str, Any], period: dict[str, Any], completed: int
) -> dict[str, Any]:
    """Equal-duration previous window, or an honest unavailable.

    Unavailable when the record holds no work at all from before that window:
    a zero previous period in a workspace that did not exist yet is not a
    decline, and drawing it as one would be the page's first lie.
    """
    if not rows["has_history_before_previous"]:
        return {
            "available": False,
            "unavailable_reason": "no_recorded_work_before_previous_period",
            "previous_start_utc": _iso(period["previous_start_utc"]),
            "previous_end_utc": _iso(period["previous_end_utc"]),
            "previous_completed": None,
            "delta": None,
        }
    prev = int(rows["completed_previous_total"])
    return {
        "available": True,
        "unavailable_reason": None,
        "previous_start_utc": _iso(period["previous_start_utc"]),
        "previous_end_utc": _iso(period["previous_end_utc"]),
        "previous_completed": prev,
        "delta": completed - prev,
    }


def _attention(
    rows: dict[str, Any], viewer_user_id: int | None, seat: dict[str, Any] | None
) -> dict[str, Any]:
    """What is waiting on the SIGNED-IN PERSON.

    This count answers to the session identity and nothing else. Choosing a
    different whose-work scope changes the work counts above it and leaves
    this one exactly where it was — the desk is yours whichever team you are
    looking at. That is the same rule `/work/overview` keeps for `needs_you`.

    A machine session has no person, so the count is unavailable rather than
    zero: zero would read as "nothing needs you", which is a claim about a
    person who does not exist here.
    """
    if viewer_user_id is None:
        return {
            "available": False,
            "unavailable_reason": "no_personal_identity_for_machine_session",
            "needs_you": None,
            "viewer_user_id": None,
            "scoped_to": "session_identity",
        }
    return {
        "available": True,
        "unavailable_reason": None,
        "needs_you": int(rows["needs_you"] or 0),
        "viewer_user_id": viewer_user_id,
        "scoped_to": "session_identity",
        "unplaced_viewer": bool(seat and seat.get("role_id") is None),
    }


def _financial(
    rows: dict[str, Any],
    period: dict[str, Any],
    *,
    visible: bool,
    reason: str | None,
) -> dict[str, Any]:
    """Money, when the seat includes the financial surface — and only then.

    Everything here is ORGANIZATION-WIDE. Stored span cost hangs off the
    account and the agent, not off the work scope you are looking at, so it
    cannot honestly be called "this team's spend". Because of that:

      * `scope` says `organization_wide`, always.
      * There is no cost-per-completion. Dividing org-wide spend by a narrowed
        completion count would produce a number that describes nothing.
      * `attributable_to_shown_work` is False, stated rather than implied.

    Coverage is a span count, not a share of money: `priced_spans /
    (priced_spans + unpriced_token_spans)` — the share of cost-bearing spans
    that carry a stored price. It is NOT "we know 92% of the money", because
    the value of the unpriced spans is exactly what we do not know. When the
    denominator is zero (no cost-bearing spans in the window at all) coverage
    is null: there is no percentage to state.

    Missing cost is unknown, not free.
    """
    if not visible:
        return {
            "visible": False,
            "unavailable_reason": reason,
            "scope": None,
            "spend_usd": None,
            "coverage": None,
        }
    cost = rows["cost"] or {}
    priced = int(cost.get("priced_spans") or 0)
    unpriced = int(cost.get("unpriced_token_spans") or 0)
    denom = priced + unpriced
    coverage = round(priced / denom, 4) if denom else None
    return {
        "visible": True,
        "unavailable_reason": None,
        "scope": "organization_wide",
        "scope_note": (
            "Stored span cost is recorded per account and agent, not per work "
            "scope. This figure is the whole organization's spend in the "
            "period regardless of the selected work scope."
        ),
        "attributable_to_shown_work": False,
        "currency": "USD",
        "period_start_utc": _iso(period["start_utc"]),
        "period_end_utc": _iso(period["end_utc"]),
        "spend_usd": round(float(cost.get("spend_usd") or 0.0), 6),
        "coverage": {
            "measure": "priced_cost_bearing_spans",
            "definition": (
                "Share of spans carrying usage or a stored cost that also "
                "carry a stored price. Not a share of dollars — the value of "
                "unpriced spans is unknown."
            ),
            "priced_spans": priced,
            "unpriced_token_spans": unpriced,
            "denominator": denom,
            "ratio": coverage,
            "unavailable_reason": (
                None if denom else "no_cost_bearing_spans_in_period"
            ),
        },
    }


def _unavailable_reasons(
    series: dict[str, Any],
    jobs: dict[str, Any],
    comparison: dict[str, Any],
    financial: dict[str, Any],
    attention: dict[str, Any],
) -> list[dict[str, str]]:
    """Every thing this snapshot could not establish, named in one place, so a
    renderer can decide what to hide without re-inspecting each block."""
    out: list[dict[str, str]] = []
    if not series["available"]:
        out.append({"field": "completions_series", "reason": series["unavailable_reason"]})
    elif not series["reconciles"]:
        out.append({"field": "completions_series", "reason": "buckets_do_not_reconcile"})
    if jobs["truncated"]:
        out.append({"field": "by_job", "reason": "job_list_truncated_tail_in_other"})
    if not comparison["available"]:
        out.append({"field": "period.comparison", "reason": comparison["unavailable_reason"]})
    if not financial["visible"]:
        out.append({"field": "financial", "reason": financial["unavailable_reason"]})
    elif financial["coverage"]["ratio"] is None:
        out.append({"field": "financial.coverage", "reason": "no_cost_bearing_spans_in_period"})
    if not attention["available"]:
        out.append({"field": "attention", "reason": attention["unavailable_reason"]})
    return out


def _navigation(
    *,
    account_id: int | None,
    whose: str | None,
    person_id: int | None,
    period: dict[str, Any],
) -> dict[str, Any]:
    """Canonical identifiers a later Home UI needs to click one level deeper
    WITHOUT losing the scope and period the person is looking at.

    Home links must carry these through verbatim. A drill-in that silently
    resets to "everyone, last 7 days" turns an investigation into a different
    question.
    """
    carry: dict[str, Any] = {
        "days": period["days"],
        "tz": period["timezone"],
    }
    if whose:
        carry["whose"] = whose
    if person_id is not None:
        carry["person_id"] = person_id
    return {
        "account_id": account_id,
        "carry_query": carry,
        "work_items_path": "/work/items",
        "work_overview_path": "/work/overview",
        # `/work/items` takes whose/person_id but not days/tz — the period is
        # Home's, and the next PR that adds a period filter to the work list
        # should reuse these same names.
        "work_items_supports": ["whose", "person_id", "workflow_id", "status"],
    }
