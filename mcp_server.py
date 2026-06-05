"""Oversee MCP server for ChatGPT (and other MCP-capable) agents.

Exposes four tools — connect, log_activity, report_complete, status — over
Streamable HTTP, mounted on the main FastAPI app at /mcp. Unlike the OpenClaw
plugin / SDK (silent, automatic), ChatGPT agents *report* their own activity by
calling these tools.

Auth: the agent sends its Oversee API key as `Authorization: Bearer <key>`. We
resolve that key to an account on EVERY call (an ASGI middleware stashes it in a
contextvar) — no global mutable "current key", so it's multi-tenant safe.

Storage: we're in-process with the REST API, so tools write straight to the same
database as parsed OTEL-style spans (NOT over HTTP). ChatGPT agents then show up
in the fleet exactly like SDK/plugin agents. The "which agent is this account
talking about" mapping reuses the existing insights cache (sentinel `__mcp__`),
so no new table/migration is needed.
"""

from __future__ import annotations

import contextvars
import os
import time
import uuid
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

import database

# streamable_http_path="/" because we mount this app under "/mcp" on the main
# FastAPI app (Starlette strips the "/mcp" prefix before the inner app sees it);
# the public endpoint is therefore https://<host>/mcp.
mcp = FastMCP("oversee", stateless_http=True, streamable_http_path="/")

# Per-request account id, set by the ASGI auth wrapper around the MCP app and
# read by the tools. Defaults to None (unauthenticated).
_ACCOUNT_CV: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "oversee_mcp_account", default=None
)

# Sentinel "service_name" under which we cache each account's currently-connected
# agent name (so log/report/status know which agent to attribute to).
_MCP_SENTINEL = "__mcp__"

_NO_AUTH = (
    "Not connected: Oversee couldn't read a valid API key. Make sure your MCP "
    "app sends the header 'Authorization: Bearer <your Oversee API key>'."
)
_NO_AGENT = "Call oversee_connect first so Oversee knows which agent this is."


def _bearer(authorization: str | None) -> str | None:
    """Pull the token out of an `Authorization` header value."""
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip() or None
    return authorization.strip() or None


def _resolve_account_id(ctx: Context | None) -> int | None:
    """Resolve the caller's account from the Bearer API key. Tries, in order:
    the contextvar set by the ASGI wrapper; the request on the MCP context; then
    an env fallback (single-tenant local/dev). Returns None when unauthenticated.
    """
    acct = _ACCOUNT_CV.get()
    if acct is not None:
        return acct
    try:
        req = ctx.request_context.request if ctx is not None else None
        token = _bearer(req.headers.get("authorization")) if req is not None else None
        if token:
            row = database.validate_api_key(token)
            if row:
                return row["account_id"]
    except Exception:  # noqa: BLE001 — context/request may be absent on some transports
        pass
    token = os.environ.get("OVERSEE_MCP_API_KEY") or os.environ.get("OVERSEE_API_KEY")
    if token:
        row = database.validate_api_key(token)
        if row:
            return row["account_id"]
    return None


def _set_current_agent(account_id: int, service_name: str) -> None:
    database.save_insight(
        account_id, _MCP_SENTINEL, "main", "current_agent",
        {"service_name": service_name},
    )


def _current_agent(account_id: int) -> str | None:
    row = database.get_insight(account_id, _MCP_SENTINEL, "main", "current_agent")
    if row and isinstance(row.get("data"), dict):
        name = row["data"].get("service_name")
        return name or None
    return None


def _create_span(
    service_name: str,
    span_name: str,
    attributes: dict[str, Any],
    account_id: int,
    duration_seconds: float = 0.0,
    status_code: int = 0,
) -> None:
    """Write one parsed span straight into the DB (same shape insert_spans
    expects — NOT raw OTLP)."""
    now = int(time.time() * 1_000_000_000)
    try:
        dur = int(max(0.0, float(duration_seconds or 0)) * 1_000_000_000)
    except (TypeError, ValueError):
        dur = 0
    attrs: dict[str, Any] = {"oversee.agent.id": "main"}
    attrs.update({k: v for k, v in attributes.items() if v not in (None, "")})
    database.insert_spans(
        [
            {
                "trace_id": uuid.uuid4().hex,
                "span_id": uuid.uuid4().hex[:16],
                "parent_span_id": None,
                "service_name": service_name,
                "span_name": span_name,
                "kind": 0,
                "start_time_unix": now - dur,
                "end_time_unix": now,
                "status_code": status_code,
                "status_message": "",
                "attributes": attrs,
                "resource_attributes": {
                    "service.name": service_name,
                    "oversee.platform": "chatgpt",
                },
            }
        ],
        account_id=account_id,
    )


@mcp.tool()
def oversee_connect(
    agent_name: str, agent_role: str, agent_instructions: str, ctx: Context = None
) -> str:
    """Register this agent with Oversee monitoring. Call once at the start of a conversation.

    Args:
        agent_name: The name of this agent
        agent_role: What this agent does (e.g. 'Email marketing', 'Customer support')
        agent_instructions: The full system instructions for this agent
    """
    account_id = _resolve_account_id(ctx)
    if account_id is None:
        return _NO_AUTH
    name = (agent_name or "ChatGPT Agent").strip() or "ChatGPT Agent"
    database.save_registration(
        service_name=name,
        agent_id="main",
        soul=(agent_instructions or ""),
        identity=(agent_role or ""),
        operating_manual="",
        user_context="",
        memory="",
        workspace_path="",
        model="chatgpt",
        account_id=account_id,
    )
    _set_current_agent(account_id, name)
    _create_span(
        name,
        "agent_registration",
        {
            "oversee.event.type": "agent_registration",
            "oversee.agent.role": agent_role,
        },
        account_id,
    )
    return f"Connected to Oversee as '{name}'. Your activity is now being monitored."


@mcp.tool()
def oversee_log_activity(
    step_name: str,
    description: str,
    duration_seconds: float = 0,
    tools_used: str = "",
    output_summary: str = "",
    ctx: Context = None,
) -> str:
    """Log a completed activity or task step. Call after completing each major step in your workflow.

    Args:
        step_name: Name of the step completed (e.g. 'Drafted email', 'Searched database')
        description: What happened in this step
        duration_seconds: How long this step took (optional)
        tools_used: Comma-separated list of tools used (optional)
        output_summary: Brief summary of what was produced (optional)
    """
    account_id = _resolve_account_id(ctx)
    if account_id is None:
        return _NO_AUTH
    service = _current_agent(account_id)
    if not service:
        return _NO_AGENT
    _create_span(
        service,
        (step_name or "activity").strip() or "activity",
        {
            "oversee.event.type": "agent_activity",
            "oversee.step.name": step_name,
            "oversee.step.description": description,
            "oversee.tools.used": tools_used,
            "oversee.output.summary": output_summary,
        },
        account_id,
        duration_seconds=duration_seconds,
    )
    return f"Logged: {step_name}"


@mcp.tool()
def oversee_report_complete(
    task_summary: str,
    steps_completed: int = 0,
    success: bool = True,
    output_description: str = "",
    ctx: Context = None,
) -> str:
    """Report that a task or conversation is complete. Call when you finish a task.

    Args:
        task_summary: Summary of what was accomplished
        steps_completed: Number of steps completed in this task
        success: Whether the task completed successfully
        output_description: Description of the final output (optional)
    """
    account_id = _resolve_account_id(ctx)
    if account_id is None:
        return _NO_AUTH
    service = _current_agent(account_id)
    if not service:
        return _NO_AGENT
    _create_span(
        service,
        "agent_run_complete",
        {
            "oversee.event.type": "agent_run_complete",
            "oversee.task.summary": task_summary,
            "oversee.steps.completed": steps_completed,
            "oversee.run.success": success,
            "oversee.output.description": output_description,
        },
        account_id,
        # A failed run records as an error span so success-rate stats reflect it.
        status_code=0 if success else 2,
    )
    return f"Task complete. Summary: {task_summary}"


@mcp.tool()
def oversee_status(ctx: Context = None) -> str:
    """Check your Oversee monitoring connection status."""
    account_id = _resolve_account_id(ctx)
    if account_id is None:
        return _NO_AUTH
    service = _current_agent(account_id)
    if service:
        return f"Oversee monitoring active — reporting as '{service}'."
    return "Oversee monitoring active. Call oversee_connect to register this agent."


# ---------------------------------------------------------------------------
# ASGI app (Streamable HTTP) + per-request Bearer→account auth wrapper.
# `streamable_http_app()` lazily creates mcp.session_manager, so call it once at
# import; main.py runs that session manager in its lifespan and mounts http_app.
# ---------------------------------------------------------------------------

_base_app = mcp.streamable_http_app()


async def http_app(scope, receive, send):
    """Wrap the MCP ASGI app: resolve the Bearer API key to an account_id and
    stash it in the contextvar for the duration of the request."""
    if scope.get("type") != "http":
        await _base_app(scope, receive, send)
        return
    authorization = None
    for k, v in scope.get("headers") or []:
        if k == b"authorization":
            authorization = v.decode("latin-1")
            break
    account_id: int | None = None
    token = _bearer(authorization)
    if token:
        try:
            row = database.validate_api_key(token)
            account_id = row["account_id"] if row else None
        except Exception:  # noqa: BLE001
            account_id = None
    cv_token = _ACCOUNT_CV.set(account_id)
    try:
        await _base_app(scope, receive, send)
    finally:
        _ACCOUNT_CV.reset(cv_token)
