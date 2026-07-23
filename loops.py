"""Workloops: units of work derived from the immutable event stream.

A loop groups an agent's telemetry (spans) plus explicit lifecycle events
(loop_events rows) into one unit of work an operator can reason about: who
started it, who's participating, whether it's waiting on a human or an agent,
whether it stalled, and how it ended.

Loop STATE is always derived from events — `loops.cached_state` is a query
cache recomputed on every ingest and by the periodic sweep, never a source of
truth. Events themselves (spans and loop_events) are append-only; the only
mutable rows are on the `loops` read model itself.

This module holds the vocabulary constants, the pure state machine
(`compute_loop_state` — no DB access, unit-testable), event normalization
helpers, and the periodic sweep (`run_sweep`, mirroring alerts.run_sweep).
All SQL lives in database.py, per repo convention.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import database

logger = logging.getLogger("trovis.loops")

NS_PER_S = 1_000_000_000

STATES = (
    "open",
    "working",
    "awaiting_human",
    "awaiting_agent",
    "stalled",
    "done",
    "abandoned",
)
TERMINAL_STATES = ("done", "abandoned")

# loop_events.type vocabulary. Enforced in code (database.append_loop_event
# raises ValueError on unknown types) rather than a SQL CHECK: this list will
# grow, and SQLite can't alter a CHECK without a full table rebuild — the repo
# has no migration tooling beyond idempotent startup DDL. The stable
# vocabularies (cached_state, actor_type, participant_type, role) DO get
# CHECKs in the schema.
EVENT_TYPES = (
    "loop_opened",
    "loop_closed",
    "handoff_initiated",
    "handoff_accepted",
    "handoff_completed",
    "handoff_declined",
    "stall_detected",
)
ACTOR_TYPES = ("agent", "human", "system")

# Synthetic event type for span-derived activity in a loop's merged stream.
# Never stored in loop_events — spans ARE the activity record.
ACTIVITY = "activity"

_HANDOFF_RESOLUTIONS = ("handoff_accepted", "handoff_completed", "handoff_declined")
HANDOFF_DIRECTIONS = ("to_human", "to_agent")

# System actor for sweep-authored events (e.g. auto-abandon). loop_events has
# actor_type='system' natively, so no reserved uuid or boolean column needed.
SYSTEM_ACTOR = "system"

# Thresholds (seconds), env-overridable via TROVIS_*/OVERSEE_* like all config.
# STALL: an unresolved handoff older than this flips awaiting_* -> stalled.
STALL_THRESHOLD_S = int(database.env("LOOP_STALL_THRESHOLD_S", "14400") or 14400)  # 4h
# ABANDON: a loop idle longer than this is stalled; the sweep then closes it
# as abandoned (system-attributed loop_closed event).
ABANDON_THRESHOLD_S = int(
    database.env("LOOP_ABANDON_THRESHOLD_S", "172800") or 172800
)  # 48h
# GAP: implicit grouping — a keyless span joins the agent's most recent open
# implicit loop only if that loop's last event is newer than this.
GAP_THRESHOLD_S = int(database.env("LOOP_GAP_THRESHOLD_S", "1800") or 1800)  # 30min
# CLOSE GRACE: keyed spans arriving within this window after their loop
# closed attach to the closed loop instead of opening a new one. Exists for
# late-exported spans — the OpenClaw plugin defers ending model_call spans
# up to ~10s waiting for transcript token usage, so they land a batch or two
# after the close. Production measurement (2026-07-22): late spans arrive
# p50 13s / p95 14s / max 34s after close — 60s covers everything observed
# with ~2x headroom.
CLOSE_GRACE_S = int(database.env("LOOP_CLOSE_GRACE_S", "60") or 60)


def agent_actor(service_name: str, agent_id: str) -> str:
    """Composite agent identifier, matching the `svc:agent` subject_key idiom
    used by alerts. Display/equality only — never parsed back apart."""
    return f"{service_name}:{agent_id or 'main'}"


def activity_event(ts_ns: int, actor: str = "", payload: dict | None = None) -> dict:
    """Synthesize a normalized 'activity' event from a span (see the shape
    contract on compute_loop_state)."""
    return {
        "type": ACTIVITY,
        "ts": int(ts_ns or 0),
        "actor_type": "agent",
        "actor": actor or "",
        "payload": payload or {},
    }


def normalize_loop_event(row: dict[str, Any]) -> dict:
    """DB loop_events row -> normalized event shape. Fail-soft on payload
    JSON: a corrupt payload becomes {} rather than breaking state compute."""
    payload = row.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return {
        "type": row.get("type") or "",
        "ts": int(row.get("event_time_unix") or 0),
        "actor_type": row.get("actor_type") or "",
        "actor": row.get("actor") or "",
        "payload": payload,
    }


def _unresolved_handoffs(events: list[dict]) -> list[dict]:
    """The still-unresolved handoff_initiated events, in stream order.

    A resolution (accepted/completed/declined) matches by payload.handoff_id
    when it carries one; otherwise it resolves the most recent unresolved
    handoff. A resolution with an id that matches nothing is dropped (better
    to leave a real handoff pending than to resolve the wrong one).
    """
    unresolved: list[dict] = []
    for e in events:
        t = e.get("type")
        if t == "handoff_initiated":
            unresolved.append(e)
        elif t in _HANDOFF_RESOLUTIONS and unresolved:
            hid = (e.get("payload") or {}).get("handoff_id")
            if hid is not None:
                for h in reversed(unresolved):
                    if (h.get("payload") or {}).get("handoff_id") == hid:
                        unresolved.remove(h)
                        break
            else:
                unresolved.pop()
    return unresolved


def compute_loop_state(
    events: list[dict],
    now_ns: int | None = None,
    stall_threshold_s: int | None = None,
    abandon_threshold_s: int | None = None,
) -> str:
    """Derive loop state from the event stream. Pure function, no DB access.

    Each event is a dict:
        {"type": str,        # one of EVENT_TYPES, or "activity" (span-derived)
         "ts": int,          # unix nanoseconds
         "actor_type": str,  # "agent" | "human" | "system"
         "actor": str,       # "service:agent_id" | str(user_id) | "system"
         "payload": dict}

    Rules, evaluated in order:
    1. Any loop_closed with payload.reason == 'abandoned' -> 'abandoned'
    2. Any loop_closed -> 'done'
    3. Last unresolved handoff_initiated with direction 'to_human':
       initiated more than STALL_THRESHOLD ago -> 'stalled', else 'awaiting_human'
    4. Same for direction 'to_agent' -> 'stalled' / 'awaiting_agent'
    5. No events within ABANDON_THRESHOLD -> 'stalled'
       (the sweep converts this to a real abandoned close)
    6. Any event other than loop_opened (activity counts) -> 'working'
    7. Otherwise -> 'open'
    """
    stall_ns = (
        stall_threshold_s if stall_threshold_s is not None else STALL_THRESHOLD_S
    ) * NS_PER_S
    abandon_ns = (
        abandon_threshold_s if abandon_threshold_s is not None else ABANDON_THRESHOLD_S
    ) * NS_PER_S
    if now_ns is None:
        now_ns = time.time_ns()

    if not events:
        return "open"
    # Defensive re-sort. sorted() is stable, so ties keep the caller's order
    # (the DB adapter already breaks ties: loop events before spans, then id).
    evs = sorted(events, key=lambda e: int(e.get("ts") or 0))

    # Rules 1-2: a close is terminal regardless of anything after it.
    for e in reversed(evs):
        if e.get("type") == "loop_closed":
            reason = (e.get("payload") or {}).get("reason")
            return "abandoned" if reason == "abandoned" else "done"

    # Rules 3-4: unresolved handoffs. A pending to_human wins over a pending
    # to_agent even if the to_agent handoff came later (spec order).
    unresolved = _unresolved_handoffs(evs)
    for direction, awaiting in (
        ("to_human", "awaiting_human"),
        ("to_agent", "awaiting_agent"),
    ):
        pending = [
            h
            for h in unresolved
            if (h.get("payload") or {}).get("direction") == direction
        ]
        if pending:
            initiated_ts = int(pending[-1].get("ts") or 0)
            return "stalled" if now_ns - initiated_ts > stall_ns else awaiting

    # Rule 5: idle past the abandon threshold.
    last_ts = max(int(e.get("ts") or 0) for e in evs)
    if now_ns - last_ts > abandon_ns:
        return "stalled"

    # Rules 6-7.
    if any(e.get("type") != "loop_opened" for e in evs):
        return "working"
    return "open"


# ---------------------------------------------------------------------------
# Workflow matching
# ---------------------------------------------------------------------------
# A workflow is a named, VERSIONED declaration of a recurring process: an
# ordered list of stations (who holds the work at each step) plus match
# hints (how to recognize a loop as an instance). Definitions are
# append-only — every edit is a new version — and a matched loop records
# WHICH version it matched: that pairing is load-bearing for future drift
# detection. The loop's workflow_id/workflow_version/workflow_confidence
# columns are a cache like cached_state: recomputable while the loop is
# open, FROZEN once it reaches a terminal state.

# Hint vocabularies — enforced in code (writers raise ValueError), like
# EVENT_TYPES, so they can grow without a table rebuild.
MATCH_FIELDS = ("service_name", "agent_id", "title")
MATCH_OPS = ("equals", "contains", "prefix")

STATION_HOLDER_TYPES = ACTOR_TYPES  # agent | human | system


def validate_match_hints(hints) -> list:
    """Validate the match_hints JSON shape. ALL hints must pass for a loop
    to match (AND semantics). An empty list is legal — the workflow simply
    never auto-matches (a declaration without recognition rules yet)."""
    if not isinstance(hints, list):
        raise ValueError("match_hints must be a list")
    for h in hints:
        if not isinstance(h, dict):
            raise ValueError("each match hint must be an object")
        if h.get("field") not in MATCH_FIELDS:
            raise ValueError(
                f"unknown hint field {h.get('field')!r}; one of: {', '.join(MATCH_FIELDS)}"
            )
        if h.get("op") not in MATCH_OPS:
            raise ValueError(
                f"unknown hint op {h.get('op')!r}; one of: {', '.join(MATCH_OPS)}"
            )
        if not isinstance(h.get("value"), str) or not h["value"]:
            raise ValueError("hint value must be a non-empty string")
    return hints


def validate_stations(stations) -> list:
    """Validate the stations JSON shape. Stored and returned, NOT used for
    matching (that's a later inference tier)."""
    if not isinstance(stations, list):
        raise ValueError("stations must be a list")
    for s in stations:
        if not isinstance(s, dict):
            raise ValueError("each station must be an object")
        if s.get("holder_type") not in STATION_HOLDER_TYPES:
            raise ValueError(
                f"unknown holder_type {s.get('holder_type')!r}; "
                f"one of: {', '.join(STATION_HOLDER_TYPES)}"
            )
        for key in ("holder", "label"):
            if key in s and s[key] is not None and not isinstance(s[key], str):
                raise ValueError(f"station {key} must be a string")
        tools = s.get("tools")
        if tools is not None and (
            not isinstance(tools, list) or any(not isinstance(t, str) for t in tools)
        ):
            raise ValueError("station tools must be a list of strings")
    return stations


def _hint_passes(loop_row: dict, hint: dict) -> bool:
    value = loop_row.get(hint["field"])
    if not isinstance(value, str) or not value:
        return False  # a missing field never matches
    target = hint["value"]
    if hint["field"] == "title":  # title matching is case-insensitive
        value, target = value.lower(), target.lower()
    op = hint["op"]
    if op == "equals":
        return value == target
    if op == "contains":
        return target in value
    if op == "prefix":
        return value.startswith(target)
    return False


def match_workflow(loop_row: dict, workflow_versions: list[dict]):
    """Match a loop against workflow declarations. Pure function.

    loop_row needs service_name / agent_id / title. workflow_versions is
    the CURRENT version of each non-archived workflow:
        [{"workflow_id": int, "version": int, "match_hints": [...]}]
    (the caller filters to current+non-archived — and, structurally, to
    versioned workflows only, so legacy graph rows can never match).

    ALL hints must pass -> confidence 1.0 (a declared match; no partial or
    fuzzy scoring in this tier — the confidence column exists for future
    inference tiers). Hintless workflows never auto-match.

    Multiple matches: most hints wins (more specific declaration); tie ->
    most recently created wins (higher id — SERIAL ids are creation-
    ordered). Ties are logged; that log becomes a user-facing
    "overlapping workflows" warning later.

    Returns (workflow_id, version, confidence) or None.
    """
    candidates = []
    for wf in workflow_versions or []:
        hints = wf.get("match_hints") or []
        if not hints:
            continue
        if all(_hint_passes(loop_row, h) for h in hints):
            candidates.append((len(hints), wf["workflow_id"], wf["version"]))
    if not candidates:
        return None
    candidates.sort(reverse=True)  # most hints, then most recent (highest id)
    if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
        logger.info(
            "[loops] ambiguous workflow match for loop %s: workflows %s tie at %d hints",
            loop_row.get("id"),
            [c[1] for c in candidates if c[0] == candidates[0][0]],
            candidates[0][0],
        )
    _, workflow_id, version = candidates[0]
    return (workflow_id, version, 1.0)


# ---------------------------------------------------------------------------
# Sweep
# ---------------------------------------------------------------------------
# cached_state can lag for pure time-based transitions (awaiting_* -> stalled,
# idle -> stalled) because nothing recomputes it between events. The sweep
# covers those within its interval, and converts loops idle past
# ABANDON_THRESHOLD into a real abandoned close (system-attributed
# loop_closed event + closed_at) — the read model never silently mutates
# state without an event trail for terminal transitions.


def run_sweep_for_account(account_id: int | None) -> dict[str, int]:
    """Recompute state for one account's non-terminal loops, and re-match
    them against workflow declarations. Fail-soft per loop: one bad loop is
    logged and skipped, the sweep always finishes.

    The re-match pass is what lets a workflow created AFTER loops started
    catch its in-flight instances, and moves open loops onto a new version
    after an edit. Cost: one hint-set query per account, then an in-memory
    evaluation per open loop with a write only when the match changed.
    Terminal loops are never re-matched (frozen — enforced in
    rematch_open_loop and by this function only visiting non-terminal
    loops)."""
    now_ns = time.time_ns()
    checked = abandoned = restated = rematched = 0
    hint_sets = database.get_current_workflow_hints(account_id)
    for loop in database.get_open_loops_for_sweep(account_id):
        checked += 1
        try:
            last_ns = int(loop.get("last_event_unix") or 0)
            if last_ns and now_ns - last_ns > ABANDON_THRESHOLD_S * NS_PER_S:
                database.abandon_loop(loop["id"], account_id)
                abandoned += 1
            else:
                new_state = database.recompute_loop_state_standalone(
                    loop["id"], account_id, now_ns=now_ns
                )
                if new_state != loop.get("cached_state"):
                    restated += 1
                if hint_sets and database.rematch_open_loop(
                    loop["id"], account_id, hint_sets
                ):
                    rematched += 1
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "[loops] sweep failed for loop %s: %s", loop.get("id"), e
            )
    return {
        "checked": checked,
        "abandoned": abandoned,
        "restated": restated,
        "rematched": rematched,
    }


def run_sweep() -> dict[str, int]:
    """Sweep every account, plus one pass for NULL-account loops (open/dev
    mode — list_account_ids can't see them). Fail-soft per account."""
    totals = {"checked": 0, "abandoned": 0, "restated": 0}
    account_ids: list[int | None] = list(database.list_account_ids())
    account_ids.append(None)
    for aid in account_ids:
        try:
            summary = run_sweep_for_account(aid)
            for k in totals:
                totals[k] += summary.get(k, 0)
        except Exception as e:  # noqa: BLE001
            logger.warning("[loops] sweep failed for account %s: %s", aid, e)
    return totals
