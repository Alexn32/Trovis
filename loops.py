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

# cached_state vocabulary. Enforced in CODE (the writers), not a SQL CHECK —
# this list grew once already (awaiting_system) and SQLite can't alter a
# CHECK without a table rebuild. Same treatment as loop_events.type.
STATES = (
    "open",
    "working",
    "awaiting_human",
    "awaiting_agent",
    "awaiting_system",
    "stalled",
    "done",
    "abandoned",
)
TERMINAL_STATES = ("done", "abandoned")

# loop_events.type vocabulary. Enforced in code (database.append_loop_event
# raises ValueError on unknown types) rather than a SQL CHECK: this list will
# grow, and SQLite can't alter a CHECK without a full table rebuild — the repo
# has no migration tooling beyond idempotent startup DDL. cached_state and
# participant_type moved to the same code-enforced posture when they grew
# (awaiting_system / 'tool'); only truly frozen vocabularies (actor_type,
# role) keep CHECKs in the schema.
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
# to_system: a genuine blocking wait on an external system (rate limit,
# slow export, unfired webhook). Declared, never inferred — same posture as
# human handoffs. target_id is conventionally the system's name.
HANDOFF_DIRECTIONS = ("to_human", "to_agent", "to_system")

# Participant vocabulary. 'tool' entered when tools became cast members:
# a tool is a PARTICIPANT (it appears in the loop's cast, auto-populated
# from tool-call spans) but almost never a HOLDER — holders are actors.
# The one exception is a declared to_system handoff. Enforced in code
# (database._upsert_loop_participant raises ValueError), not a CHECK.
PARTICIPANT_TYPES = ("agent", "human", "tool")
PARTICIPANT_ROLES = ("initiator", "executor", "reviewer")

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
    # 'ingestion_artifact' (the one-off phantom reclassification) maps to
    # abandoned so a recompute can never flip an artifact-closed loop to
    # 'done' — the payload reason carries the real story.
    for e in reversed(evs):
        if e.get("type") == "loop_closed":
            reason = (e.get("payload") or {}).get("reason")
            return (
                "abandoned"
                if reason in ("abandoned", "ingestion_artifact")
                else "done"
            )

    # Rules 3-4: unresolved handoffs. A pending to_human wins over a pending
    # to_agent, which wins over to_system, regardless of arrival order.
    unresolved = _unresolved_handoffs(evs)
    for direction, awaiting in (
        ("to_human", "awaiting_human"),
        ("to_agent", "awaiting_agent"),
        ("to_system", "awaiting_system"),
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
# Story derivations: logical stream order, possession segments, narration
# ---------------------------------------------------------------------------
# A loop's story is a chain of POSSESSIONS: at any moment exactly one actor
# holds the work. Handoffs are the seams between possessions; tool calls are
# touches WITHIN a possession, never possessions themselves. Segments and
# sentences are computed on read, never stored (titles are the one stored
# derivation — they cost an LLM call).


def stream_position(ev: dict) -> int:
    """Logical position class for merged-stream ordering: loop_opened is
    always first and loop_closed always last, regardless of raw timestamps.
    Exists because late-exported spans (the plugin's ~10s token-usage wait)
    carry start times EARLIER than the loop_opened stamp and used to sort
    before it — the "Started renders mid-stream" bug."""
    t = ev.get("type")
    if t == "loop_opened":
        return 0
    if t == "loop_closed":
        return 2
    return 1


def sort_stream(events: list[dict]) -> list[dict]:
    """Order a merged stream logically: opened first, closed last, everything
    else by timestamp (stable, so the adapter's tiebreaks survive)."""
    return sorted(events, key=lambda e: (stream_position(e), int(e.get("ts") or 0)))


def span_tool(span_name: str | None, tool: str | None) -> str | None:
    """THE tool identifier: the tool name as spans carry it, lowercased, no
    normalization (MCP-prefixed names stay verbatim — display mapping is
    narration's job). None for LLM-call spans and spans with no named tool.
    Single source for segments, narration, and tool participants."""
    name = span_name or ""
    if name in ("model_call", "llm_output"):
        return None
    if tool:
        return str(tool).lower()
    return None


def activity_kind(ev: dict) -> str:
    """Classify a span-derived activity event: 'tool' | 'llm' | 'other'.
    Tool identity defers to span_tool() — one identifier, everywhere."""
    p = ev.get("payload") or {}
    name = p.get("span_name") or ""
    if span_tool(name, p.get("tool")):
        return "tool"
    if name == "tool_call":
        return "tool"  # unnamed tool call: still tool-shaped activity
    if name in ("model_call", "llm_output"):
        return "llm"
    return "other"


def compute_loop_segments(events: list[dict], now_ns: int | None = None) -> list[dict]:
    """Derive the loop's possession chain. Pure function, no DB access.

    Returns segments in order:
        {"holder_type": "agent"|"human"|"system",
         "holder": str,            # "service:agent", user id str, or name
         "start_ns": int,
         "end_ns": int | None,     # None = ongoing (unclosed loop)
         "waiting": bool,          # handoff-target possessions await activity
         "touches": [{"name": str, "count": int}],
         "event_count": int}

    Rules: the loop opens with a possession by the initiator; handoff_initiated
    ends the current segment and hands possession to the target (waiting);
    accepted/completed — or agent activity resuming — returns possession to
    the agent; declined returns it to the prior holder; loop_closed ends the
    chain. Stragglers (events timestamped inside an already-ended segment, or
    arriving after close via the grace window) FOLD into the segment covering
    their timestamp — they never extend the chain past close.
    """
    if now_ns is None:
        now_ns = time.time_ns()
    evs = sort_stream(events)
    segments: list[dict] = []
    current: dict | None = None
    closed = False

    def start(holder_type: str, holder: str, ts: int, waiting: bool) -> dict:
        return {
            "holder_type": holder_type,
            "holder": holder,
            "start_ns": ts,
            "end_ns": None,
            "waiting": waiting,
            "touches": [],
            "event_count": 0,
        }

    def end_current(ts: int) -> None:
        nonlocal current
        if current is not None:
            current["end_ns"] = ts
            segments.append(current)
            current = None

    def covering(ts: int) -> dict | None:
        for seg in segments:
            if seg["start_ns"] <= ts and (seg["end_ns"] is None or ts < seg["end_ns"]):
                return seg
        if current is not None and current["start_ns"] <= ts:
            return current
        return segments[0] if segments else current

    def add_touch(seg: dict, name: str) -> None:
        for t in seg["touches"]:
            if t["name"] == name:
                t["count"] += 1
                return
        seg["touches"].append({"name": name, "count": 1})

    def record_activity(seg: dict | None, ev: dict) -> None:
        if seg is None:
            return
        seg["event_count"] += 1
        if activity_kind(ev) == "tool":
            p = ev.get("payload") or {}
            add_touch(seg, str(p.get("tool") or p.get("span_name") or "tool"))

    def last_agent() -> tuple[str, str]:
        pool = segments + ([current] if current is not None else [])
        for seg in reversed(pool):
            if seg["holder_type"] == "agent" and not seg["waiting"]:
                return "agent", seg["holder"]
        return "agent", ""

    for ev in evs:
        t = ev.get("type")
        ts = int(ev.get("ts") or 0)
        p = ev.get("payload") or {}

        if t == "loop_opened":
            if current is None and not segments:
                current = start(
                    ev.get("actor_type") or "agent", ev.get("actor") or "", ts, False
                )
            continue

        if current is None and not segments and not closed:
            # Defensive: a stream with no loop_opened still gets a chain.
            current = start("agent", ev.get("actor") or "", ts, False)

        if t == "loop_closed":
            end_current(ts)
            closed = True
            continue

        # Straggler folding: post-close events, or events timestamped before
        # the current segment began, count where they belong — no new links.
        if closed or (current is not None and ts < current["start_ns"]):
            if t == ACTIVITY:
                record_activity(covering(ts), ev)
            continue

        if t == "handoff_initiated":
            end_current(ts)
            direction = p.get("direction")
            if direction == "to_human":
                holder_type = "human"
                holder = str(p.get("target_name") or p.get("target_id") or "human")
            elif direction == "to_system":
                holder_type = "system"
                holder = str(p.get("target_id") or "system")
            else:
                holder_type = "agent"
                holder = str(p.get("target_id") or "agent")
            current = start(holder_type, holder, ts, True)
            continue

        if t in ("handoff_accepted", "handoff_completed"):
            if current is not None and current["waiting"]:
                end_current(ts)
                if ev.get("actor_type") == "agent" and ev.get("actor"):
                    current = start("agent", ev["actor"], ts, False)
                else:
                    ht, h = last_agent()
                    current = start(ht, h, ts, False)
            continue

        if t == "handoff_declined":
            if current is not None and current["waiting"]:
                prior = segments[-1] if segments else None
                end_current(ts)
                if prior is not None:
                    current = start(prior["holder_type"], prior["holder"], ts, False)
                else:
                    ht, h = last_agent()
                    current = start(ht, h, ts, False)
            continue

        if t == ACTIVITY:
            if (
                current is not None
                and current["waiting"]
                # STRICTLY after the wait began: the handoff-carrying span
                # itself lands at the same timestamp as handoff_initiated
                # (the attrs ride the span) — it's the act of handing off,
                # never a resumption.
                and ts > current["start_ns"]
                and (ev.get("actor_type") or "agent") == "agent"
            ):
                # Agent activity resumes possession from a waiting holder.
                end_current(ts)
                current = start("agent", ev.get("actor") or "", ts, False)
            record_activity(current, ev)
            continue

        # Other lifecycle events (stall_detected, ...) change no possession.

    if current is not None:
        segments.append(current)  # unclosed loop: end_ns stays None
    return segments


def segments_mini(segments: list[dict]) -> list[dict]:
    """The list-endpoint shape: just enough to draw a proportional bar."""
    return [
        {
            "holder_type": s["holder_type"],
            "start_ns": s["start_ns"],
            "end_ns": s["end_ns"],
            "waiting": s["waiting"],
        }
        for s in segments
    ]


# ---------------------------------------------------------------------------
# Station-position derivation (the workflow map)
# ---------------------------------------------------------------------------
# NOTE: built here because the map UI consumes it; the endpoint contract
# (position.station_index, on_path/off_path/no_stations) comes from the
# approved map spec. v1 alignment is a GREEDY MONOTONE walk — stations are
# an ordered declaration, so a loop is on-path when its possession chain
# can be read left-to-right along the stations without backtracking:
# each segment advances the cursor to the next station (at or after the
# cursor) whose holder_type matches and whose declared holder name (when
# the station names one) matches the segment's holder. Anything that
# can't align is off_path — rendered only in the loop list, never dotted
# on the track. No fuzzy matching, no inference; refinements are a later,
# data-informed problem.


def _station_matches(station: dict, seg: dict) -> bool:
    if (station.get("holder_type") or "") != (seg.get("holder_type") or ""):
        return False
    want = str(station.get("holder") or "").strip().lower()
    if not want:
        return True  # unnamed station: any holder of the right type
    have = str(seg.get("holder") or "").strip().lower()
    if have == want:
        return True
    # Agent holders are "service:agent" composites — accept either part.
    if ":" in have and want in have.split(":"):
        return True
    return False


def align_loop_to_stations(segments: list[dict], stations: list[dict]) -> dict:
    """Where a loop currently sits on its workflow's station map.

    Returns {"status": "on_path"|"off_path"|"no_stations",
             "station_index": int|None}.
    station_index is the station holding the CURRENT possession; None
    unless on_path.
    """
    if not stations:
        return {"status": "no_stations", "station_index": None}
    if not segments:
        return {"status": "off_path", "station_index": None}
    cursor = 0
    idx = None
    for seg in segments:
        found = None
        for j in range(cursor, len(stations)):
            if _station_matches(stations[j], seg):
                found = j
                break
        if found is None:
            return {"status": "off_path", "station_index": None}
        cursor = found
        idx = found
    return {"status": "on_path", "station_index": idx}


# ---------------------------------------------------------------------------
# Narration (template tier)
# ---------------------------------------------------------------------------

# Human names for common tools, seeded from the spec plus the top tools
# observed in production spans (exec/read/process/edit/web_fetch/write/...).
# Unmapped tools render "Used {name}" — never a raw span_name.
TOOL_SENTENCES = {
    "exec": "Ran a command",
    "read": "Read a file",
    "edit": "Edited a file",
    "write": "Wrote a file",
    "process": "Managed a process",
    "web_fetch": "Fetched a web page",
    "web_search": "Searched the web",
    "browser": "Used the browser",
    "message": "Sent a message",
    "sessions_send": "Messaged another agent session",
    "sessions_list": "Checked other agent sessions",
    "memory_search": "Searched its memory",
    "send_slack_message": "Sent a Slack message",
    "send_email": "Sent an email",
    "request_approval": "Requested approval",
}


def tool_sentence(name: str, count: int = 1) -> str:
    base = TOOL_SENTENCES.get(name)
    if base is None and name.startswith("mcp__"):
        # MCP-prefixed tools (mcp__{server}__{tool}): the server is the
        # readable identity — "Used Shopify", not the full triple-barreled id.
        server = name.split("__")[1] if name.count("__") >= 2 else ""
        if server:
            base = f"Used {server.replace('-', ' ').replace('_', ' ').title()}"
    if base is None:
        base = f"Used {name}"
    return base if count <= 1 else f"{base} · {count}×"


def _handoff_sentence(p: dict) -> str:
    targets = {"to_human": "a human", "to_agent": "another agent"}
    who = p.get("target_name") or targets.get(p.get("direction"), "someone")
    reason = f" — {p['reason']}" if p.get("reason") else ""
    return f"Handed to {who}{reason}"


def _close_sentence(p: dict) -> str:
    reason = p.get("reason")
    if reason == "abandoned":
        return "Closed automatically — no activity for 2 days"
    if reason == "ingestion_artifact":
        return "Closed — stray telemetry from a completed run"
    if reason == "closed_by_user":
        return "Marked done"
    if reason == "completed_by_agent":
        return f"Closed by agent — {p['detail']}" if p.get("detail") else "Closed by agent — completed"
    return "Closed"


# One place for lifecycle sentences (server-side mirror of the frontend map;
# the frontend half consumes `sentence` and retires its copy next session).
LIFECYCLE_SENTENCES = {
    "loop_opened": lambda p: "Started",
    "handoff_initiated": _handoff_sentence,
    "handoff_accepted": lambda p: "Handoff accepted",
    "handoff_completed": lambda p: "Handoff completed",
    "handoff_declined": lambda p: "Handoff declined",
    "loop_closed": _close_sentence,
    "stall_detected": lambda p: "Stalled — no recent activity",
}


def lifecycle_sentence(ev: dict) -> str:
    fn = LIFECYCLE_SENTENCES.get(ev.get("type"))
    if fn:
        return fn(ev.get("payload") or {})
    return str(ev.get("type") or "").replace("_", " ")


def narrate_events(events: list[dict]) -> list[dict]:
    """Attach a plain-English `sentence` to each stream entry, collapsing
    consecutive same-tool calls ("Sent a Slack message · 3×") and consecutive
    LLM calls ("Worked through it (4 steps)") into single entries. Raw
    span_names stay in the payload; the sentence is the only display string.

    agent_run_complete spans are omitted — the loop_closed line covers them.
    """
    mode = (database.env("LOOP_NARRATION", "template") or "template").lower()
    if mode == "llm":
        # LLM narration tier: one Claude pass over the loop's shape producing
        # richer prose per entry. Follow the _auto_describe precedent
        # (describer client, fail-soft, cached) when building this.
        raise NotImplementedError("TROVIS_LOOP_NARRATION=llm is not implemented yet")

    out: list[dict] = []
    for ev in sort_stream(events):
        if ev.get("type") != ACTIVITY:
            entry = dict(ev)
            entry["sentence"] = lifecycle_sentence(ev)
            entry["_group"] = None
            out.append(entry)
            continue
        p = ev.get("payload") or {}
        name = p.get("span_name") or ""
        if name == "agent_run_complete":
            continue
        kind = activity_kind(ev)
        group = (
            ("tool", str(p.get("tool") or name))
            if kind == "tool"
            else ("llm", None)
            if kind == "llm"
            else ("other", name)
        )
        if out and out[-1].get("_group") == group:
            prev = out[-1]
            prev["payload"] = dict(prev.get("payload") or {})
            prev["payload"]["count"] = int(prev["payload"].get("count") or 1) + 1
            continue
        entry = dict(ev)
        entry["payload"] = dict(p)
        entry["_group"] = group
        out.append(entry)

    for entry in out:
        group = entry.pop("_group", None)
        if not group:
            continue
        kind, name = group
        n = int((entry.get("payload") or {}).get("count") or 1)
        if kind == "llm":
            entry["sentence"] = (
                "Thought it through" if n <= 1 else f"Worked through it ({n} steps)"
            )
        elif kind == "tool":
            entry["sentence"] = tool_sentence(name, n)
        else:
            entry["sentence"] = str(name or "activity").replace("_", " ").capitalize()
    return out


# ---------------------------------------------------------------------------
# Titles (the one STORED derivation — they cost an LLM call)
# ---------------------------------------------------------------------------
# Pipeline, first hit wins: (1) plugin-provided trovis.loop.title, never
# overwritten; (2) LLM title from the loop's SHAPE (metadata only — agent
# identity, tool names, handoff target, duration, close reason; never span
# attribute values); (3) template fallback. All stored via the single
# NULL-guarded UPDATE in database.set_loop_title_if_missing — generated
# titles never overwrite, plugin titles never get replaced.

# Cap per sweep pass, same idea as the alert sweep's _MAX_FRESH_DRIFT: bounds
# Claude spend per 15-min interval regardless of backlog.
MAX_TITLES_PER_SWEEP = int(database.env("LOOP_TITLES_PER_SWEEP", "25") or 25)


def template_title(shape: dict) -> str:
    tools = shape.get("tools") or []
    top = tools[0] if tools else "run"
    n = int(shape.get("action_count") or 0)
    return f"{shape.get('agent') or 'agent'} · {top} · {n} actions"


def ensure_loop_title(loop_id: int, account_id: int | None) -> bool:
    """Generate and store a title for one untitled loop. Fail-soft (never
    raises); returns True only when a title was written. One LLM call per
    loop ever — the NULL-only write makes retries idempotent."""
    try:
        shape = database.get_loop_title_shape(loop_id, account_id)
        if not shape:
            return False  # missing, other-account, or already titled
        title = None
        if (database.env("LOOP_TITLES", "llm") or "llm").lower() == "llm":
            try:
                import describer  # noqa: PLC0415  (lazy: heavy import)

                title = describer.loop_title(shape)
            except Exception as e:  # noqa: BLE001
                logger.warning("[loops] title generation failed for %s: %s", loop_id, e)
        if not title:
            title = template_title(shape)
        return database.set_loop_title_if_missing(loop_id, title, account_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("[loops] ensure_loop_title(%s) failed: %s", loop_id, e)
        return False


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
    # Title pass: loops that reached terminal state without a title (the
    # ingestion trigger covers handoff moments). Capped per sweep so Claude
    # spend stays bounded regardless of backlog.
    titled = 0
    for lid in database.get_untitled_terminal_loops(
        account_id, limit=MAX_TITLES_PER_SWEEP
    ):
        if ensure_loop_title(lid, account_id):
            titled += 1
    return {
        "checked": checked,
        "abandoned": abandoned,
        "restated": restated,
        "rematched": rematched,
        "titled": titled,
    }


def run_sweep() -> dict[str, int]:
    """Sweep every account, plus one pass for NULL-account loops (open/dev
    mode — list_account_ids can't see them). Fail-soft per account."""
    totals = {"checked": 0, "abandoned": 0, "restated": 0, "rematched": 0, "titled": 0}
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


# ---------------------------------------------------------------------------
# One-off: phantom reclassification (pre-grace-window stragglers)
# ---------------------------------------------------------------------------


def reclassify_phantom_loops(
    account_id: int | None = None,
    dry_run: bool = False,
    force: bool = False,
) -> dict[str, int]:
    """One-off repair for the pre-grace-window phantoms: open loops whose
    spans are exclusively late-exported model_call stragglers of a run whose
    real loop closed just before. Left alone they'd sweep to a plain
    'abandoned' — false history. Each clean match gets a system-attributed
    loop_closed with payload.reason='ingestion_artifact' (plus the run and
    real-loop pointers) and goes terminal; the reason string carries the
    truth, and compute_loop_state maps it to 'abandoned' mechanically.

    Guarded to run once: if any ingestion_artifact close already exists in
    scope, the pass is skipped unless force=True (it is also idempotent by
    construction — only OPEN loops ever match). Ambiguous candidates (the
    signature isn't clean) are counted and left alone — no guessing. The
    ~1,700 pre-0.5.5 loops that never had a close are NOT touched; plain
    'abandoned' is honest for those.
    """
    if not force and database.any_artifact_closes(account_id):
        return {"reclassified": 0, "skipped_ambiguous": 0, "examined": 0, "already_ran": 1}
    reclassified = skipped = examined = 0
    scopes: list[int | None]
    if account_id is not None:
        scopes = [account_id]
    else:
        scopes = list(database.list_account_ids())
        scopes.append(None)
    for aid in scopes:
        for cand in database.find_phantom_candidates(aid):
            examined += 1
            if not cand.get("clean"):
                skipped += 1
                continue
            if dry_run:
                reclassified += 1
                continue
            try:
                database.artifact_close_loop(
                    cand["id"], cand["external_id"], cand["real_loop_id"], aid
                )
                reclassified += 1
            except Exception as e:  # noqa: BLE001
                logger.warning("[loops] reclassify failed for %s: %s", cand["id"], e)
                skipped += 1
    return {
        "reclassified": reclassified,
        "skipped_ambiguous": skipped,
        "examined": examined,
        "already_ran": 0,
    }
