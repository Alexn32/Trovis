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
# host="0.0.0.0" prevents FastMCP from auto-enabling localhost-only DNS rebinding
# protection (which rejects the Railway hostname). We do our own Bearer-API-key
# auth, so DNS rebinding protection is unnecessary.
from mcp.server.transport_security import TransportSecuritySettings as _TSS

mcp = FastMCP(
    "oversee",
    stateless_http=True,
    streamable_http_path="/",
    host="0.0.0.0",
    transport_security=_TSS(enable_dns_rebinding_protection=False),
)

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
    token = database.env("MCP_API_KEY") or database.env("API_KEY")
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
    attrs: dict[str, Any] = {"trovis.agent.id": "main"}
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
                    "trovis.platform": "chatgpt",
                },
            }
        ],
        account_id=account_id,
    )


# ---------------------------------------------------------------------------
# MCP tools. ChatGPT Custom MCP requires exactly two tools named "search" and
# "fetch" that return a specific document-retrieval schema WITH output_schema
# declared (per OpenAI docs). The tools must be async and use Pydantic models
# for the return type so FastMCP generates the correct outputSchema.
#
# We encode our monitoring operations as "documents": the query string carries
# the action (connect/log/complete/status) and parameters as a structured
# prefix, and the results/text carry the response.
# ---------------------------------------------------------------------------

import json as _json
from pydantic import BaseModel, Field


class SearchResult(BaseModel):
    id: str
    title: str
    url: str


class SearchOutput(BaseModel):
    results: list[SearchResult] = Field(default_factory=list)


class FetchOutput(BaseModel):
    id: str
    title: str
    text: str
    url: str
    metadata: dict[str, str] = Field(default_factory=dict)


def _make_search_result(doc_id: str, title: str, text: str = "") -> str:
    """Return the ChatGPT-required search result format as JSON text."""
    return _json.dumps({
        "results": [{"id": doc_id, "title": title, "url": f"oversee://{doc_id}"}]
    })


def _make_fetch_result(doc_id: str, title: str, text: str, metadata: dict | None = None) -> str:
    """Return the ChatGPT-required fetch result format as JSON text."""
    return _json.dumps({
        "id": doc_id,
        "title": title,
        "text": text,
        "url": f"oversee://{doc_id}",
        "metadata": metadata or {},
    })


@mcp.tool()
async def search(query: str, ctx: Context = None) -> SearchOutput:
    """Search Oversee monitoring data. Accepts queries like 'connect:AgentName|Role|Instructions', 'log:StepName|Description', 'complete:Summary', or 'status'.

    Args:
        query: A search query string
    """
    account_id = _resolve_account_id(ctx)
    q = (query or "status").strip()

    def _result(doc_id: str, title: str) -> SearchOutput:
        return SearchOutput(results=[SearchResult(id=doc_id, title=title, url=f"oversee://{doc_id}")])

    if account_id is None:
        return _result("error", "Auth required")

    if q.lower().startswith("connect:"):
        parts = q[8:].split("|", 2)
        name = (parts[0].strip() if parts else "") or "ChatGPT Agent"
        role = parts[1].strip() if len(parts) > 1 else ""
        instructions = parts[2].strip() if len(parts) > 2 else ""
        database.save_registration(
            service_name=name, agent_id="main", soul=instructions,
            identity=role, operating_manual="", user_context="",
            memory="", workspace_path="", model="chatgpt",
            account_id=account_id,
        )
        _set_current_agent(account_id, name)
        _create_span(name, "agent_registration",
                      {"trovis.event.type": "agent_registration", "trovis.agent.role": role},
                      account_id)
        return _result("connected", f"Connected as {name}")

    if q.lower().startswith("log:"):
        service = _current_agent(account_id)
        if not service:
            return _result("error", "Call connect first")
        parts = q[4:].split("|", 1)
        step = (parts[0].strip() if parts else "") or "activity"
        desc = parts[1].strip() if len(parts) > 1 else ""
        _create_span(service, step,
                      {"trovis.event.type": "agent_activity", "trovis.step.name": step,
                       "trovis.step.description": desc},
                      account_id)
        return _result("logged", f"Logged: {step}")

    if q.lower().startswith("complete:"):
        service = _current_agent(account_id)
        if not service:
            return _result("error", "Call connect first")
        summary = q[9:].strip()
        _create_span(service, "agent_run_complete",
                      {"trovis.event.type": "agent_run_complete",
                       "trovis.task.summary": summary},
                      account_id)
        return _result("completed", f"Task complete: {summary}")

    service = _current_agent(account_id)
    label = f"Active as {service}" if service else "Not connected"
    return _result("status", label)


@mcp.tool()
async def fetch(id: str, ctx: Context = None) -> FetchOutput:
    """Fetch details for an Oversee monitoring result by ID.

    Args:
        id: The result ID from a search
    """
    account_id = _resolve_account_id(ctx)
    service = _current_agent(account_id) if account_id else None
    label = service or "Not connected"
    return FetchOutput(
        id=id or "status",
        title=f"Oversee: {label}",
        text=f"Agent '{label}' monitored by Oversee.",
        url=f"oversee://{id or 'status'}",
        metadata={"agent": label, "platform": "chatgpt"},
    )


# ---------------------------------------------------------------------------
# ASGI apps: Streamable HTTP (/mcp) + SSE (/mcp/sse) transports.
#
# ChatGPT's Custom MCP client expects SSE transport (the URL placeholder in
# ChatGPT's UI literally says "https://example.com/sse"). We expose both so
# standard MCP clients (Streamable HTTP) and ChatGPT (SSE) work.
#
# Both share the same FastMCP instance (same 4 tools). The per-request
# Bearer→account_id auth wrapper is shared too.
# ---------------------------------------------------------------------------

_streamable_app = mcp.streamable_http_app()
_sse_app = mcp.sse_app()


def _resolve_auth_from_scope(scope) -> int | None:
    """Extract the Bearer token from ASGI scope headers and resolve account."""
    authorization = None
    for k, v in scope.get("headers") or []:
        if k == b"authorization":
            authorization = v.decode("latin-1")
            break
    token = _bearer(authorization)
    if token:
        try:
            row = database.validate_api_key(token)
            return row["account_id"] if row else None
        except Exception:  # noqa: BLE001
            pass
    return None


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


async def sse_app(scope, receive, send):
    """SSE transport with per-request auth (for ChatGPT Custom MCP)."""
    if scope.get("type") != "http":
        await _sse_app(scope, receive, send)
        return
    cv_token = _ACCOUNT_CV.set(_resolve_auth_from_scope(scope))
    try:
        await _sse_app(scope, receive, send)
    finally:
        _ACCOUNT_CV.reset(cv_token)
