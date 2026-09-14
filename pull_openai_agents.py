"""OpenAI Managed Agents — a PULL adapter.

Every other door in Trovis is push: the agent, or its SDK, or its operator
has to call in. That selects for agents whose owners already decided to care.
This one is the other direction — an API key, and the agents that are already
running appear, with nobody adding a line of code or redeploying anything.

The source is OpenAI's Managed Agents API on api.openai.com (`GET /agents`,
`/agents/sessions`, `/agents/sessions/{id}/turns`). Its shapes are taken from
OpenAI's published OpenAPI spec, not guessed:

  agent    id, name, model, instructions, tools     -> a Trovis agent + its
                                                       registration, which is
                                                       what the description
                                                       pipeline reads first
  session  id, status, created_at, last_active_at   -> a Work loop
           status: idle | in_progress |
                   requires_action | failed
  turn     id, session_id, agent_id, subagent_id,   -> a span on that loop,
           status, started_at, completed_at,           with duration, outcome
           usage{input,output,total}_tokens            and token cost

`requires_action` is the one worth calling out: it means the session is
blocked waiting on something outside itself, which is exactly Trovis's
"waiting on a person" — a state we normally only learn when an agent is
instrumented to tell us.

Two deliberate V1 limits:

  - **No content.** Titles come from session metadata when the caller set
    one; they are never read out of a user's messages, and message bodies
    are not fetched. Same privacy default as the SDK's `capture_outputs`,
    which content capture here would have to follow.
  - **Nothing is closed.** A session with no turn running reads `idle`, not
    "done" — OpenAI has no terminal state to map. Inventing one would close
    live work on the board, so loops stay open and Trovis's own sweep
    decides.

Nothing here raises out: a sync failure must never take down the caller, and
a partial sync is better than none. Every network call goes through an
injectable `get`, so the mapping is testable without a key or a socket.
"""

from __future__ import annotations

import hashlib
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

import database

logger = logging.getLogger("trovis.pull.openai_agents")

PROVIDER = "openai_agents"
# Wire identity on spans; database._PLATFORM_LABELS turns it into a label.
# NOT "openai-agents" — the Connect page already uses that id for the OpenAI
# Agents SDK tile (the push door). Same vendor, different door; a span must
# say which one it came through.
PLATFORM = "openai-agents-api"
API_ROOT = "https://api.openai.com/v1"

# Sentinel service_name for the sync cursor. Reuses agent_insights — no new
# table, the same trick the report doors use for their job caches.
_CURSOR_SENTINEL = "__openai_agents_sync__"

_TIMEOUT_S = 30
_PAGE_LIMIT = 100
# A first sync must not walk an org's entire history into one poll.
_MAX_SESSIONS_PER_SYNC = 200
_MAX_TURNS_PER_SESSION = 500

# Session status -> what Trovis should show. Only `requires_action` changes
# the board; the rest are ordinary progress.
_STATUS_WAITING = "requires_action"
_STATUS_FAILED = "failed"

_TURN_FAILED = frozenset({"failed", "cancelled"})


# ---------------------------------------------------------------------------
# HTTP. One small client so tests can replace it wholesale.
# ---------------------------------------------------------------------------

def _http_get(path: str, api_key: str, params: dict[str, Any] | None = None) -> dict:
    """GET one page from the OpenAI API. Returns {} on any failure.

    Returning {} rather than raising is the rule for this module: a sync that
    hits a 429 mid-way should keep what it already wrote and stop, not blow
    up a request thread.
    """
    url = f"{API_ROOT}{path}"
    if params:
        clean = {k: v for k, v in params.items() if v not in (None, "")}
        if clean:
            url = f"{url}?{urllib.parse.urlencode(clean)}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            return json.loads(resp.read().decode("utf-8")) or {}
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8")[:200]
        except Exception:  # pragma: no cover — best effort
            pass
        logger.warning("openai agents GET %s -> HTTP %s %s", path, e.code, body)
    except Exception as e:  # noqa: BLE001 — network, DNS, JSON, all the same here
        logger.warning("openai agents GET %s failed: %s", path, e)
    return {}


def _pages(
    get: Callable[..., dict], path: str, api_key: str, cap: int,
    params: dict[str, Any] | None = None,
) -> list[dict]:
    """Follow `after` pagination until the API says there is no more, or the
    cap is reached. The cap is what keeps a first sync bounded."""
    out: list[dict] = []
    after = None
    while len(out) < cap:
        page = get(path, api_key, {**(params or {}),
                                   "limit": _PAGE_LIMIT, "after": after})
        rows = page.get("data")
        if not isinstance(rows, list) or not rows:
            break
        out.extend(r for r in rows if isinstance(r, dict))
        if not page.get("has_more"):
            break
        after = page.get("last_id") or (rows[-1] or {}).get("id")
        if not after:
            break
    return out[:cap]


# ---------------------------------------------------------------------------
# Cursor. What we have already written, so a re-poll is not a duplicate.
# ---------------------------------------------------------------------------

def _read_cursor(account_id: int) -> tuple[int, set[str]]:
    """(high-water timestamp, turn ids already written at exactly that ts).

    Second-resolution timestamps mean `> ts` alone would drop turns that
    share the boundary second, and `>= ts` would rewrite them. Keeping the
    ids seen at the boundary is what makes the poll both complete and
    idempotent.
    """
    row = database.get_insight(account_id, _CURSOR_SENTINEL, "main", PROVIDER)
    data = (row or {}).get("data") if row else None
    data = data if isinstance(data, dict) else {}
    ts = data.get("ts")
    ids = data.get("ids")
    return (int(ts) if isinstance(ts, int) else 0,
            set(ids) if isinstance(ids, list) else set())


def _write_cursor(account_id: int, ts: int, ids: set[str]) -> None:
    database.save_insight(
        account_id, _CURSOR_SENTINEL, "main", PROVIDER,
        {"ts": int(ts), "ids": sorted(ids)[:500]},
    )


# ---------------------------------------------------------------------------
# Mapping. Pure functions — no network, no database.
# ---------------------------------------------------------------------------

def agent_name(agent: dict) -> str:
    """What this agent is called in Trovis.

    OpenAI allows a null name. An id is a poor thing to show a person, but it
    is stable and unambiguous, which beats collapsing every unnamed agent in
    an org into one row called "agent".
    """
    name = str(agent.get("name") or "").strip()
    return name or str(agent.get("id") or "").strip() or "openai-agent"


def _trace_id(account_id: int, session_id: str) -> str:
    """One trace per session, derived rather than stored — and account-scoped,
    so two orgs cannot land on one record."""
    return hashlib.sha256(
        f"{account_id}:{session_id}".encode()
    ).hexdigest()[:32]


def _span_id(turn_id: str) -> str:
    return hashlib.sha256(turn_id.encode()).hexdigest()[:16]


def _session_title(session: dict) -> str:
    """A name for the job, from metadata only.

    The caller may set `trovis.loop.title` (or a plain `title`) on the
    session. We do not read the user's first message to make one: that is
    content, and content here follows the same opt-in as everywhere else.
    """
    meta = session.get("metadata")
    meta = meta if isinstance(meta, dict) else {}
    for key in ("trovis.loop.title", "trovis_loop_title", "title"):
        val = str(meta.get(key) or "").strip()
        if val:
            return val[:200]
    return ""


def _usage_attrs(usage: Any) -> dict[str, Any]:
    """Token usage in the names ingest already reads (`gen_ai.usage.*`)."""
    if not isinstance(usage, dict):
        return {}
    out: dict[str, Any] = {}
    for src, dst in (
        ("input_tokens", "gen_ai.usage.input_tokens"),
        ("output_tokens", "gen_ai.usage.output_tokens"),
        ("total_tokens", "gen_ai.usage.total_tokens"),
    ):
        val = usage.get(src)
        if isinstance(val, int) and val >= 0:
            out[dst] = val
    return out


def turn_span(
    account_id: int, service: str, session: dict, turn: dict, model: str = "",
) -> dict | None:
    """One OpenAI turn -> one Trovis span on that session's trace."""
    turn_id = str(turn.get("id") or "").strip()
    session_id = str(session.get("id") or "").strip()
    if not turn_id or not session_id:
        return None

    created = _as_ns(turn.get("created_at"))
    start = _as_ns(turn.get("started_at")) or created
    end = _as_ns(turn.get("completed_at")) or start
    status = str(turn.get("status") or "").strip().lower()
    failed = status in _TURN_FAILED
    err = turn.get("error")
    err_msg = ""
    if isinstance(err, dict):
        err_msg = str(err.get("message") or err.get("code") or "")[:500]
    elif err:
        err_msg = str(err)[:500]

    attrs: dict[str, Any] = {
        "trovis.agent.id": "main",
        "trovis.loop.external_id": session_id,
        "trovis.run.id": session_id,
        "trovis.event.type": "agent_run_complete" if status == "completed"
                             else "agent_activity",
        "trovis.step.name": f"turn {status}" if status else "turn",
        # Pulled, not reported. Anyone reading a span should be able to tell
        # which of the two it was without guessing from the shape.
        "trovis.source": "openai-agents-api",
        "openai.turn.id": turn_id,
        "openai.session.id": session_id,
        "openai.turn.status": status,
    }
    title = _session_title(session)
    if title:
        attrs["trovis.loop.title"] = title
    if model:
        attrs["gen_ai.request.model"] = model
    sub = str(turn.get("subagent_id") or "").strip()
    if sub:
        # A subagent turn is another participant doing the work, which is a
        # handoff in Trovis's vocabulary.
        attrs["trovis.handoff.direction"] = "to_agent"
        attrs["trovis.handoff.target_id"] = sub
        attrs["openai.subagent.id"] = sub
    attrs.update(_usage_attrs(turn.get("usage")))
    if err_msg:
        attrs["trovis.error.message"] = err_msg

    return {
        "trace_id": _trace_id(account_id, session_id),
        "span_id": _span_id(turn_id),
        "parent_span_id": None,
        "service_name": service,
        "span_name": f"turn.{status}" if status else "turn",
        "kind": 0,
        "start_time_unix": start,
        "end_time_unix": end,
        "status_code": 2 if failed else 0,
        "status_message": err_msg if failed else "",
        "attributes": attrs,
        "resource_attributes": {
            "service.name": service,
            "trovis.platform": PLATFORM,
        },
    }


def waiting_span(account_id: int, service: str, session: dict) -> dict | None:
    """A session in `requires_action` is blocked on something outside itself.

    That is the state Trovis normally only learns from an instrumented agent,
    and it is the one a person actually looks for on the board.
    """
    session_id = str(session.get("id") or "").strip()
    if not session_id:
        return None
    reasons = []
    for action in (session.get("required_actions") or []):
        if isinstance(action, dict):
            label = str(action.get("type") or "").strip()
            if label:
                reasons.append(label)
    reason = ", ".join(reasons[:5]) or "the session is waiting on an action"
    when = _as_ns(session.get("last_active_at")) or _as_ns(session.get("created_at"))
    attrs: dict[str, Any] = {
        "trovis.agent.id": "main",
        "trovis.loop.external_id": session_id,
        "trovis.run.id": session_id,
        "trovis.event.type": "agent_activity",
        "trovis.handoff.direction": "to_human",
        "trovis.handoff.reason": reason,
        "trovis.handoff.id": hashlib.sha256(
            f"{session_id}:{when}".encode()
        ).hexdigest()[:32],
        "trovis.step.name": "requires_action",
        "trovis.step.description": reason,
        "trovis.source": "openai-agents-api",
        "openai.session.id": session_id,
    }
    title = _session_title(session)
    if title:
        attrs["trovis.loop.title"] = title
    return {
        "trace_id": _trace_id(account_id, session_id),
        # Derived from the session and its moment, so re-polling the same
        # blocked session does not stack up identical waits.
        "span_id": _span_id(f"{session_id}:requires_action:{when}"),
        "parent_span_id": None,
        "service_name": service,
        "span_name": "requires_action",
        "kind": 0,
        "start_time_unix": when,
        "end_time_unix": when,
        "status_code": 0,
        "status_message": "",
        "attributes": attrs,
        "resource_attributes": {
            "service.name": service,
            "trovis.platform": PLATFORM,
        },
    }


def _as_ns(value: Any) -> int:
    """OpenAI sends Unix seconds; Trovis stores nanoseconds."""
    try:
        secs = int(value)
    except (TypeError, ValueError):
        return 0
    return secs * 1_000_000_000 if secs > 0 else 0


# ---------------------------------------------------------------------------
# The sync itself.
# ---------------------------------------------------------------------------

def sync_account(
    account_id: int,
    api_key: str | None = None,
    *,
    get: Callable[..., dict] | None = None,
) -> dict[str, Any]:
    """Pull this account's OpenAI agents and their recent work into Trovis.

    Returns a summary rather than raising: callers (a route, a scheduler) want
    to report what happened, not handle an exception from someone else's API.
    """
    fetch = get or _http_get
    key = (api_key or "").strip()
    if not key:
        secrets = database.get_saas_connection_secrets(account_id, PROVIDER)
        key = str((secrets or {}).get("access_token") or "").strip()
    if not key:
        return {"status": "not_connected", "agents": 0, "sessions": 0, "spans": 0}

    agents = _pages(fetch, "/agents", key, cap=_PAGE_LIMIT)
    names: dict[str, str] = {}
    models: dict[str, str] = {}
    for agent in agents:
        agent_id = str(agent.get("id") or "").strip()
        if not agent_id:
            continue
        service = agent_name(agent)
        names[agent_id] = service
        models[agent_id] = str(agent.get("model") or "").strip()
        # The instructions ARE the agent's identity. Registering them is what
        # lets the description come from what the agent is for, rather than
        # from whatever it happened to do first.
        instructions = str(agent.get("instructions") or "").strip()
        if instructions:
            try:
                database.save_registration(
                    service_name=service, agent_id="main",
                    soul=instructions, identity=instructions,
                    operating_manual="", user_context="", memory="",
                    workspace_path="", model=models[agent_id] or "openai",
                    account_id=account_id,
                )
            except Exception as e:  # noqa: BLE001 — never fail a sync on this
                logger.warning("registration for %s failed: %s", service, e)

    cursor_ts, cursor_ids = _read_cursor(account_id)
    sessions = _pages(fetch, "/agents/sessions", key, cap=_MAX_SESSIONS_PER_SYNC)

    spans: list[dict] = []
    seen_ids: set[str] = set(cursor_ids)
    high_ts = cursor_ts
    touched = 0

    for session in sessions:
        session_id = str(session.get("id") or "").strip()
        if not session_id:
            continue
        # Nothing has happened here since the last sync.
        last_active = int(session.get("last_active_at") or 0)
        if last_active and last_active < cursor_ts:
            continue

        agent_ref = session.get("agent")
        agent_ref = agent_ref if isinstance(agent_ref, dict) else {}
        agent_id = str(agent_ref.get("id") or "").strip()
        service = (names.get(agent_id) or agent_name(agent_ref)
                   or "openai-agent")
        model = models.get(agent_id) or str(agent_ref.get("model") or "")

        turns = _pages(fetch, f"/agents/sessions/{session_id}/turns", key,
                       cap=_MAX_TURNS_PER_SESSION, params={"order": "asc"})
        wrote_here = False
        for turn in turns:
            turn_id = str(turn.get("id") or "").strip()
            created = int(turn.get("created_at") or 0)
            if not turn_id or turn_id in seen_ids:
                continue
            if created and created < cursor_ts:
                continue
            span = turn_span(account_id, service, session, turn, model)
            if span is None:
                continue
            spans.append(span)
            seen_ids.add(turn_id)
            wrote_here = True
            high_ts = max(high_ts, created)

        if str(session.get("status") or "").lower() == _STATUS_WAITING:
            wait = waiting_span(account_id, service, session)
            if wait is not None:
                marker = f"wait:{session_id}:{wait['start_time_unix']}"
                if marker not in seen_ids:
                    spans.append(wait)
                    seen_ids.add(marker)
                    wrote_here = True
        if wrote_here:
            touched += 1

    written = 0
    if spans:
        try:
            database.ingest_spans_with_loops(spans, account_id=account_id)
            written = len(spans)
        except Exception as e:  # noqa: BLE001 — report, don't explode
            logger.warning("openai agents ingest failed: %s", e)
            return {"status": "error", "agents": len(names),
                    "sessions": touched, "spans": 0, "detail": str(e)[:200]}

    # Only the ids at the boundary second need carrying; older ones are
    # excluded by the timestamp alone.
    boundary = {t for t in seen_ids if t.startswith("wait:")} | {
        s["attributes"]["openai.turn.id"] for s in spans
        if s["attributes"].get("openai.turn.id")
        and s["start_time_unix"] // 1_000_000_000 >= high_ts
    }
    _write_cursor(account_id, high_ts, boundary or cursor_ids)

    return {"status": "ok", "agents": len(names), "sessions": touched,
            "spans": written}
