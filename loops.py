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
# Sweep
# ---------------------------------------------------------------------------
# cached_state can lag for pure time-based transitions (awaiting_* -> stalled,
# idle -> stalled) because nothing recomputes it between events. The sweep
# covers those within its interval, and converts loops idle past
# ABANDON_THRESHOLD into a real abandoned close (system-attributed
# loop_closed event + closed_at) — the read model never silently mutates
# state without an event trail for terminal transitions.


def run_sweep_for_account(account_id: int | None) -> dict[str, int]:
    """Recompute state for one account's non-terminal loops. Fail-soft per
    loop: one bad loop is logged and skipped, the sweep always finishes."""
    now_ns = time.time_ns()
    checked = abandoned = restated = 0
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
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "[loops] sweep failed for loop %s: %s", loop.get("id"), e
            )
    return {"checked": checked, "abandoned": abandoned, "restated": restated}


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
