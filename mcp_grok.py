"""Trovis MCP server for Cursor Grok Bots — the report door.

A Grok Bot is a desktop assistant a customer runs in Cursor. It does NOT
export telemetry, and Trovis cannot pull from it: there is no OTLP exporter,
no outbound webhook, and the Cursor Enterprise OTLP export is metrics/logs
only. So the only honest V1 path is the Bot calling IN — the customer adds
this MCP server to their Bot, and the Bot's instructions tell it to report
when a job starts, when it needs a human, and when it's done.

That is what this module is: four report tools over MCP, mounted on the main
FastAPI app at /mcp/grok (Streamable HTTP) and /mcp/grok/sse (SSE).

  report_job_started(title, …)   → opens a NAMED job
  report_job_waiting(reason, …)  → the job is waiting on a human
  report_job_finished(summary)   → the job is done
  report_job_failed(reason)      → the job stopped, with the reason

Deliberately its own FastMCP instance, NOT more tools on the /mcp server:
that one exists for ChatGPT's Custom MCP, which requires exactly two tools
named `search` and `fetch`. Adding tools there would break that door.

Auth: the Trovis org API key as `Authorization: Bearer <key>` (the shared
ASGI wrapper resolves it per request, so this is multi-tenant safe). Clients
that can't set a header may pass `api_key` on any tool call instead — the
Connect page documents the header and mentions the fallback in one line.

Jobs: unlike the ChatGPT MCP path (which writes bare spans and so produces
activity, not named Work), these tools go through `ingest_spans_with_loops`
with a `trovis.loop.external_id` per job, so a report becomes a real Work
item with a real title, a waiting state, and a close.
"""

from __future__ import annotations

import contextvars
import logging
import time
import uuid
from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings as _TSS

import database

logger = logging.getLogger("trovis.mcp_grok")

# The platform stamp that makes these land on Agents as "Cursor Grok Bot"
# rather than an unlabeled service (see database._detect_platform).
PLATFORM = "cursor-grok-bot"

# Fallback display name when a Bot reports without naming itself. Deliberately
# generic-but-honest: it is a Grok Bot, we just weren't told which one.
DEFAULT_BOT_NAME = "Grok Bot"

# Sentinel service_name for the per-account "which job is open" cache, so a
# Bot that reports `finished` without echoing the job id still lands on the
# right job. Reuses agent_insights — no new table, same trick as mcp_server.
_SENTINEL = "__grok_bot__"

mcp = FastMCP(
    "trovis-grok-bot",
    stateless_http=True,
    streamable_http_path="/",
    host="0.0.0.0",
    transport_security=_TSS(enable_dns_rebinding_protection=False),
)

_ACCOUNT_CV: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "trovis_grok_account", default=None
)

_NO_AUTH = (
    "Not connected: Trovis couldn't read a valid API key. Add the header "
    "'Authorization: Bearer <your Trovis API key>' to this MCP server, or "
    "pass api_key on the call."
)
_NO_JOB = (
    "No open job for this bot. Call report_job_started first, or pass the "
    "job_id that report_job_started returned."
)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def _bearer(authorization: str | None) -> str | None:
    """Pull the token out of an `Authorization` header value."""
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip() or None
    return authorization.strip() or None


def _account_for_key(token: str | None) -> int | None:
    if not token:
        return None
    try:
        row = database.validate_api_key(token)
    except Exception:  # noqa: BLE001 — an auth lookup must never 500 the tool
        return None
    return row["account_id"] if row else None


def _resolve_account_id(ctx: Context | None, api_key: str | None = None) -> int | None:
    """Resolve the caller's account: the per-request contextvar first (the
    header path), then an explicitly-passed key, then the request on the MCP
    context. Returns None when unauthenticated — never guesses an account,
    and never falls back to an env key: this server is multi-tenant.
    """
    acct = _ACCOUNT_CV.get()
    if acct is not None:
        return acct
    acct = _account_for_key((api_key or "").strip() or None)
    if acct is not None:
        return acct
    try:
        req = ctx.request_context.request if ctx is not None else None
        if req is not None:
            return _account_for_key(_bearer(req.headers.get("authorization")))
    except Exception:  # noqa: BLE001 — request may be absent on some transports
        pass
    return None


# ---------------------------------------------------------------------------
# Job bookkeeping
# ---------------------------------------------------------------------------


def _remember_job(account_id: int, bot_name: str, job_id: str | None) -> None:
    database.save_insight(
        account_id, _SENTINEL, "main", "current_job",
        {"bot_name": bot_name, "job_id": job_id or ""},
    )


def _recall_job(account_id: int) -> tuple[str, str | None]:
    """(bot_name, job_id) for this account's last started job."""
    row = database.get_insight(account_id, _SENTINEL, "main", "current_job")
    data = row.get("data") if row else None
    if isinstance(data, dict):
        return (data.get("bot_name") or DEFAULT_BOT_NAME, data.get("job_id") or None)
    return (DEFAULT_BOT_NAME, None)


def _clean(value: Any, limit: int = 500) -> str:
    """Trim a model-supplied string. Bots write these, so nothing here is
    trusted for length."""
    return " ".join(str(value or "").split())[:limit]


def _bot_name(name: Any, account_id: int) -> str:
    """The Bot's own name wins; otherwise the name it last reported under;
    otherwise the honest generic."""
    explicit = _clean(name, 120)
    if explicit:
        return explicit
    remembered, _ = _recall_job(account_id)
    return remembered or DEFAULT_BOT_NAME


def _report_span(
    account_id: int,
    bot_name: str,
    job_id: str,
    span_name: str,
    attributes: dict[str, Any],
    status_code: int = 0,
    status_message: str = "",
) -> None:
    """Write one report as a span through the loop-aware ingest path, so it
    creates or updates a real Work job (not just an activity row)."""
    now = time.time_ns()
    attrs: dict[str, Any] = {
        "trovis.agent.id": "main",
        "trovis.loop.external_id": job_id,
        "trovis.run.id": job_id,
    }
    attrs.update({k: v for k, v in attributes.items() if v not in (None, "")})
    database.ingest_spans_with_loops(
        [
            {
                "trace_id": uuid.uuid4().hex,
                "span_id": uuid.uuid4().hex[:16],
                "parent_span_id": None,
                "service_name": bot_name,
                "span_name": span_name,
                "kind": 0,
                "start_time_unix": now,
                "end_time_unix": now,
                "status_code": status_code,
                "status_message": status_message,
                "attributes": attrs,
                "resource_attributes": {
                    "service.name": bot_name,
                    "trovis.platform": PLATFORM,
                },
            }
        ],
        account_id=account_id,
    )


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
#
# Each returns a short plain-text line: the Bot reads it back to its user, so
# "Tracking job …" beats a JSON blob. Failures say what to fix rather than
# raising — a reporting problem must never derail the Bot's actual work.


@mcp.tool()
async def report_job_started(
    title: str,
    bot_name: str = "",
    job_id: str = "",
    note: str = "",
    api_key: str = "",
    ctx: Context = None,
) -> str:
    """Tell Trovis you started a job. Call this when you begin a task a person asked for.

    Args:
        title: What you're doing, in plain English, as you'd say it to a colleague — e.g. "Draft the Q3 board update". Not an id or a slug.
        bot_name: This bot's name, so Trovis can tell your bots apart (e.g. "Trovis PM"). Send the same name every time.
        job_id: Optional id for this job. Omit it and Trovis returns one — pass that back on the later calls for this job.
        note: Optional one-line detail about the job.
        api_key: Only if this MCP server has no Authorization header.
    """
    account_id = _resolve_account_id(ctx, api_key)
    if account_id is None:
        return _NO_AUTH
    clean_title = _clean(title, 200)
    if not clean_title:
        return "Trovis needs a plain-English title for the job — what are you doing?"
    name = _bot_name(bot_name, account_id)
    jid = _clean(job_id, 120) or f"grok-{uuid.uuid4().hex[:12]}"
    _report_span(
        account_id, name, jid, "job_started",
        {
            "trovis.event.type": "agent_activity",
            "trovis.loop.title": clean_title,
            "trovis.step.name": "job_started",
            "trovis.step.description": _clean(note),
        },
    )
    _remember_job(account_id, name, jid)
    return f"Tracking job {jid} in Trovis: {clean_title}"


@mcp.tool()
async def report_job_waiting(
    reason: str,
    waiting_on: str = "",
    job_id: str = "",
    bot_name: str = "",
    api_key: str = "",
    ctx: Context = None,
) -> str:
    """Tell Trovis the job is blocked on a person. Call this whenever you ask your user a question and stop working.

    Args:
        reason: What you need, in one line — e.g. "Needs approval on the refund amount".
        waiting_on: Optional email of the person you're waiting on, so Trovis can show it as waiting on them.
        job_id: The id report_job_started returned. Omit to use this bot's most recent job.
        bot_name: This bot's name (same value you started the job with).
        api_key: Only if this MCP server has no Authorization header.
    """
    account_id = _resolve_account_id(ctx, api_key)
    if account_id is None:
        return _NO_AUTH
    name = _bot_name(bot_name, account_id)
    jid = _clean(job_id, 120) or _recall_job(account_id)[1]
    if not jid:
        return _NO_JOB
    clean_reason = _clean(reason)
    _report_span(
        account_id, name, jid, "job_waiting",
        {
            "trovis.event.type": "agent_activity",
            "trovis.handoff.direction": "to_human",
            "trovis.handoff.target_id": _clean(waiting_on, 200),
            "trovis.handoff.reason": clean_reason,
            "trovis.handoff.id": uuid.uuid4().hex,
            "trovis.step.name": "job_waiting",
        },
    )
    _remember_job(account_id, name, jid)
    who = f" ({_clean(waiting_on, 200)})" if _clean(waiting_on, 200) else ""
    return f"Marked job {jid} as waiting on a human{who} in Trovis."


@mcp.tool()
async def report_job_finished(
    summary: str = "",
    job_id: str = "",
    bot_name: str = "",
    api_key: str = "",
    ctx: Context = None,
) -> str:
    """Tell Trovis the job is done. Call this when you finish the task — otherwise it sits open on the Work board.

    Args:
        summary: One line on what you delivered.
        job_id: The id report_job_started returned. Omit to use this bot's most recent job.
        bot_name: This bot's name (same value you started the job with).
        api_key: Only if this MCP server has no Authorization header.
    """
    account_id = _resolve_account_id(ctx, api_key)
    if account_id is None:
        return _NO_AUTH
    name = _bot_name(bot_name, account_id)
    jid = _clean(job_id, 120) or _recall_job(account_id)[1]
    if not jid:
        return _NO_JOB
    _report_span(
        account_id, name, jid, "job_finished",
        {
            "trovis.event.type": "agent_run_complete",
            "trovis.loop.close": "done",
            "trovis.task.summary": _clean(summary),
            "trovis.step.name": "job_finished",
        },
    )
    _remember_job(account_id, name, None)
    return f"Closed job {jid} in Trovis."


@mcp.tool()
async def report_job_failed(
    reason: str,
    job_id: str = "",
    bot_name: str = "",
    api_key: str = "",
    ctx: Context = None,
) -> str:
    """Tell Trovis the job stopped without finishing, and why. Call this instead of report_job_finished when you gave up or hit an error.

    Args:
        reason: What went wrong, in one line.
        job_id: The id report_job_started returned. Omit to use this bot's most recent job.
        bot_name: This bot's name (same value you started the job with).
        api_key: Only if this MCP server has no Authorization header.
    """
    account_id = _resolve_account_id(ctx, api_key)
    if account_id is None:
        return _NO_AUTH
    name = _bot_name(bot_name, account_id)
    jid = _clean(job_id, 120) or _recall_job(account_id)[1]
    if not jid:
        return _NO_JOB
    clean_reason = _clean(reason) or "failed"
    _report_span(
        account_id, name, jid, "job_failed",
        {
            "trovis.event.type": "agent_run_complete",
            "trovis.loop.close": clean_reason,
            "trovis.task.summary": clean_reason,
            "trovis.step.name": "job_failed",
        },
        status_code=2,
        status_message=clean_reason,
    )
    _remember_job(account_id, name, None)
    return f"Recorded job {jid} as failed in Trovis: {clean_reason}"


# ---------------------------------------------------------------------------
# ASGI app — Streamable HTTP at /mcp/grok
# ---------------------------------------------------------------------------
#
# Streamable HTTP only. The SSE transport's message POSTs are addressed
# relative to the root (/messages/…), which the ChatGPT MCP server already
# owns — two SSE servers on one app would race for that path. Cursor speaks
# Streamable HTTP, so there is nothing to gain by risking it.

_streamable_app = mcp.streamable_http_app()


def _resolve_auth_from_scope(scope) -> int | None:
    authorization = None
    for k, v in scope.get("headers") or []:
        if k == b"authorization":
            authorization = v.decode("latin-1")
            break
    return _account_for_key(_bearer(authorization))


async def http_app(scope, receive, send):
    """Streamable HTTP transport with per-request auth."""
    if scope.get("type") != "http":
        await _streamable_app(scope, receive, send)
        return
    cv_token = _ACCOUNT_CV.set(_resolve_auth_from_scope(scope))
    try:
        await _streamable_app(scope, receive, send)
    finally:
        _ACCOUNT_CV.reset(cv_token)
