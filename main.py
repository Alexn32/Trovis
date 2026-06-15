"""Oversee — OTEL receiver and agent management API.

This module is routing only. SQL lives in database.py; data shapes live in
models.py. The one piece of non-trivial logic that *does* live here is the
OTLP/JSON parser (_parse_otlp_json + _attrs_to_dict), because it's tightly
coupled to the HTTP request shape and isn't useful anywhere else.
"""

from __future__ import annotations

# Load .env *before* anything else imports — database.py reads DATABASE_URL
# at module load time, so the file has to be in process env by then. A no-op
# in production (Railway) since no .env exists there; the platform injects
# variables directly.
#
# override=True so .env wins over any pre-existing values in the inherited
# shell environment. Without this, an empty ANTHROPIC_API_KEY="" in the
# parent shell silently shadows the real key in .env (load_dotenv's
# default is to keep existing values).
from dotenv import load_dotenv
load_dotenv(override=True)

import asyncio
import logging
import os
import re
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

import asker
import database
import describer
import pricing_sync
# MCP server for ChatGPT agents. Two transports: Streamable HTTP (/mcp) for
# standard MCP clients, SSE (/mcp/sse + /mcp/messages/) for ChatGPT Custom MCP.
from mcp_server import mcp as oversee_mcp, http_app as oversee_mcp_app, sse_app as oversee_sse_app
from models import (
    AgentCosts,
    AgentDeleteResponse,
    AgentDescription,
    AgentRecord,
    AccountUsage,
    AgentRecordsResponse,
    AgentGroup,
    AgentOutput,
    AgentOwnerSet,
    AgentRegistration,
    AgentSummary,
    AskRequest,
    AskResponse,
    AcceptInviteRequest,
    ActivityItem,
    AttentionItem,
    BriefingResponse,
    ClaimRequest,
    ConnectAskResponse,
    Connection,
    ConnectionCreate,
    ConnectionStatusUpdate,
    ConnectionsFromDescription,
    AgentBudgetUpdate,
    BudgetUpdate,
    CostAgent,
    CostAgentRow,
    CostModelRow,
    CostOverview,
    CostResponse,
    DisplayNameRequest,
    HealthResponse,
    IngestResponse,
    InviteCreate,
    InviteCreateResponse,
    InvitePublic,
    ApiKeyInfo,
    LoginRequest,
    LoginResponse,
    MeResponse,
    NewKeyResponse,
    OrgPublic,
    OrgProfileUpdate,
    RevealKeysRequest,
    RevealKeysResponse,
    SetPasswordRequest,
    SignupRequest,
    SignupResponse,
    UserPublic,
    Capabilities,
    OwnedAgent,
    SpanRecord,
    TeamMember,
    TeamMemberCreate,
    WaitlistCountResponse,
    WaitlistDeleteResponse,
    WaitlistRequest,
    WaitlistResponse,
    WeeklySummary,
    WeeklyTrends,
    WorkFeedItem,
    Workflow,
    WorkflowAiEdit,
    WorkflowAiEditResult,
    WorkflowCreate,
    WorkflowDescribe,
    WorkflowEdge,
    WorkflowEdgeCreate,
    WorkflowEdgeUpdate,
    WorkflowGenerate,
    WorkflowFromDescription,
    WorkflowParticipant,
    WorkflowParticipantCreate,
    WorkflowReorder,
    WorkflowStats,
    WorkflowStep,
    WorkflowStepCreate,
    WorkflowStepUpdate,
    WorkflowUpdate,
    StepPosition,
)

VERSION = "0.1.0"

# Endpoints reachable without an existing credential. /health for uptime
# probes; signup/login because the user has no session yet; claim and
# accept-invite self-authenticate via a key/token in the request body. Keep
# this set MINIMAL — everything else stays behind the auth gate.
_OPEN_PATHS = {
    "/health",
    "/auth/signup",
    "/auth/login",
    "/auth/claim",
    "/auth/accept-invite",
    "/oauth/authorize",         # OAuth consent page (user authenticates there)
    "/oauth/authorize/submit",  # OAuth consent form submission
    "/oauth/token",              # OAuth token exchange (ChatGPT server-to-server)
    "/actions/openapi.json",     # OpenAPI spec (public, no auth)
    "/waitlist",                 # public marketing-site signup
    "/waitlist/count",           # public signup count for the marketing site
}


logger = logging.getLogger("oversee")

# How often to pull fresh model prices from the LiteLLM list. Daily is
# plenty — published list prices change on the order of weeks.
_PRICING_REFRESH_INTERVAL_S = 24 * 60 * 60


async def _pricing_refresh_loop() -> None:
    """Refresh model pricing on startup, then once a day. Telemetry-grade
    resilience: a failed fetch (network blip, GitHub down) is logged and
    swallowed so it can never take the API down, and the last good prices
    already in the table keep serving. Cost is frozen at ingest, so a missed
    refresh only delays accuracy for *new* spans — nothing breaks."""
    while True:
        try:
            summary = await asyncio.to_thread(pricing_sync.refresh_pricing)
            logger.info("[Oversee] pricing refresh: %s", summary)
            # Re-price stored spans so prior cost reflects current rates (e.g.
            # a newly-added model). Cost is frozen at ingest otherwise.
            recomputed = await asyncio.to_thread(database.recompute_span_costs)
            logger.info("[Oversee] cost recompute: %s", recomputed)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "[Oversee] pricing refresh failed (keeping existing prices): %s",
                e,
            )
        await asyncio.sleep(_PRICING_REFRESH_INTERVAL_S)


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()
    refresh_task: asyncio.Task | None = None
    # TROVIS_DISABLE_PRICING_SYNC=1 (legacy: OVERSEE_DISABLE_PRICING_SYNC) turns
    # off the network pull (offline dev, tests) — seeded prices cover common models.
    if (database.env("DISABLE_PRICING_SYNC", "") or "").lower() not in (
        "1",
        "true",
        "yes",
    ):
        refresh_task = asyncio.create_task(_pricing_refresh_loop())
    try:
        # Run the MCP Streamable-HTTP session manager for the app's lifetime so
        # the /mcp mount can serve ChatGPT agents.
        async with oversee_mcp.session_manager.run():
            yield
    finally:
        if refresh_task is not None:
            refresh_task.cancel()
        database.shutdown_db()


app = FastAPI(title="Trovis", version=VERSION, lifespan=lifespan)


# Middleware ordering note: in Starlette, the LAST-added middleware becomes
# the OUTERMOST wrapper. We need CORS outermost so cross-origin preflights
# (OPTIONS, sent by browsers without auth headers) pass through and so 401
# responses still carry CORS headers the dashboard can read. Therefore the
# auth middleware is registered first, CORS second.

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Resolve the tenant + (optional) user for each request.

    Two credential types coexist:
      - Humans send `Authorization: Bearer <session-token>` (dashboard).
      - Agents send `X-Oversee-Api-Key` (telemetry ingest, legacy dashboard).
    Either resolves `request.state.account_id` (the org/tenant); a session
    additionally sets `request.state.user` ({id,email,role}). Open paths and
    OPTIONS bypass. With no credential and an empty DB (no keys AND no users),
    we pass through (local dev); otherwise 401.
    """
    request.state.account_id = None
    request.state.user = None
    request.state.auth = None

    path = request.url.path
    if path in _OPEN_PATHS or request.method == "OPTIONS":
        return await call_next(request)

    # The MCP server does its own Bearer-API-key auth. Bypass auth middleware
    # for all MCP paths: /mcp (streamable HTTP), /sse (SSE stream), /messages
    # (SSE message POST — resolved relative to root by the SSE client).
    # Action endpoints (/actions/*) use their own OAuth Bearer token.
    if (
        path == "/mcp" or path.startswith("/mcp/")
        or path in ("/sse", "/sse/") or path.startswith("/messages")
        or path.startswith("/actions/")
    ):
        return await call_next(request)

    # 1. Bearer session — dashboard users.
    authz = request.headers.get("Authorization")
    if authz and authz.lower().startswith("bearer "):
        sess = database.resolve_session(authz[7:].strip())
        if not sess:
            return JSONResponse(
                status_code=401, content={"error": "Invalid or expired session"}
            )
        request.state.account_id = sess["account_id"]
        request.state.user = {
            "id": sess["user_id"],
            "email": sess["email"],
            "name": sess["name"],
            "role": sess["role"],
        }
        request.state.auth = "session"
        return await call_next(request)

    # 2. API key — agents + legacy dashboard. No user → no member-mgmt power.
    # Accept the new header and the legacy one (live pre-rename agents still
    # send X-Oversee-Api-Key) — keep both permanently.
    header_key = request.headers.get("X-Trovis-Api-Key") or request.headers.get(
        "X-Oversee-Api-Key"
    )
    if header_key:
        result = database.validate_api_key(header_key)
        if not result:
            return JSONResponse(
                status_code=401, content={"error": "Invalid or missing API key"}
            )
        request.state.account_id = result["account_id"]
        request.state.auth = "api_key"
        return await call_next(request)

    # 3. No credential. Only allowed when the DB has neither keys nor users —
    # the pre-signup / local-dev mode.
    if database.has_any_keys() or database.has_any_users():
        return JSONResponse(
            status_code=401, content={"error": "Authentication required"}
        )
    return await call_next(request)


def _require_owner(request: Request) -> int:
    """Return the account_id, asserting the caller is a session-authenticated
    owner. API-key auth has no user, so it can't manage members."""
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(
            status_code=403, detail="sign in as an owner to manage your organization"
        )
    if user.get("role") != "owner":
        raise HTTPException(status_code=403, detail="owner role required")
    return getattr(request.state, "account_id", None)


# Auth is header-based (Authorization: Bearer + X-Oversee-Api-Key), never
# cookies — so credentialed CORS is unnecessary. Keeping allow_credentials=False
# alongside allow_origins=["*"] is the safe pattern for a token-authenticated
# API: any origin may call it, but every protected route still requires a valid
# token the caller's site can't obtain. (allow_credentials=True + "*" would let
# any website make credentialed requests — a real hole, and invalid per the CORS
# spec.) To lock origins down further, set TROVIS_CORS_ORIGINS (legacy:
# OVERSEE_CORS_ORIGINS) to a comma list.
# The marketing site (trovisai.com) and local dev must always be able to call
# the public /waitlist endpoint from the browser. These origins are guaranteed
# allowed even when CORS_ORIGINS is set to a lock-down list — the default stays
# fully open ("*"), so existing clients (dashboard, agents) are unaffected.
_REQUIRED_CORS_ORIGINS = [
    "https://trovisai.com",
    "https://www.trovisai.com",
    "http://localhost:5173",   # Vite dev server
    "http://localhost:3000",
]
_cors_origins_env = (database.env("CORS_ORIGINS", "") or "").strip()
if _cors_origins_env:
    _cors_origins = [o.strip() for o in _cors_origins_env.split(",") if o.strip()]
    for _o in _REQUIRED_CORS_ORIGINS:
        if _o not in _cors_origins:
            _cors_origins.append(_o)
else:
    _cors_origins = ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Intercept /mcp* at the ASGI level BEFORE Starlette routing. Routes:
#   /mcp/sse       → SSE transport (GET, event stream) — ChatGPT Custom MCP
#   /mcp/messages* → SSE transport (POST, send messages) — ChatGPT Custom MCP
#   /mcp, /mcp/    → Streamable HTTP transport — standard MCP clients
# This avoids Starlette's 307 redirect (which drops POST bodies).

class _MCPInterceptMiddleware:
    """ASGI middleware that routes MCP paths to the correct transport.

    Streamable HTTP: /mcp and /mcp/ → oversee_mcp_app at path "/"
    SSE: /sse, /sse/, /messages/* → oversee_sse_app (mounted at root so the
         SSE app's internal /sse→/messages/ handoff works without path mangling)
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            path = scope.get("path", "")
            # Streamable HTTP transport at /mcp
            if path == "/mcp" or path == "/mcp/":
                await oversee_mcp_app(dict(scope, path="/"), receive, send)
                return
            # SSE transport: the SSE app serves /sse (GET, event stream) and
            # /messages/ (POST, send messages). We mount it at root so the
            # SSE→messages handoff (which uses absolute paths) works naturally.
            # Also handle /mcp/sse as an alias.
            if path in ("/sse", "/sse/") or path == "/mcp/sse":
                await oversee_sse_app(dict(scope, path="/sse"), receive, send)
                return
            if path.startswith("/messages"):
                await oversee_sse_app(scope, receive, send)
                return
        await self.app(scope, receive, send)


app.add_middleware(_MCPInterceptMiddleware)


# ---------------------------------------------------------------------------
# OTLP/JSON parsing
# ---------------------------------------------------------------------------
#
# OTLP/JSON wraps every attribute value in a one-key object identifying its
# type: {"stringValue": "x"}, {"intValue": "42"}, {"boolValue": true}, etc.
# Nanosecond timestamps arrive as *strings* (a 64-bit nanosecond count
# doesn't fit safely in a JSON number), so we coerce them to int here.


def _attr_value(v: dict[str, Any]) -> Any:
    """Unwrap an OTLP AnyValue into a plain Python value."""
    if not isinstance(v, dict):
        return v
    if "stringValue" in v:
        return v["stringValue"]
    if "boolValue" in v:
        return v["boolValue"]
    if "intValue" in v:
        # OTLP sends int64 as a string; tolerate both.
        try:
            return int(v["intValue"])
        except (TypeError, ValueError):
            return v["intValue"]
    if "doubleValue" in v:
        return v["doubleValue"]
    if "arrayValue" in v:
        return [_attr_value(x) for x in v["arrayValue"].get("values", [])]
    if "kvlistValue" in v:
        return _attrs_to_dict(v["kvlistValue"].get("values", []))
    if "bytesValue" in v:
        return v["bytesValue"]
    return None


def _attrs_to_dict(attrs: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Flatten OTLP's [{"key": k, "value": {...}}, ...] into {k: v, ...}."""
    if not attrs:
        return {}
    return {a.get("key", ""): _attr_value(a.get("value", {})) for a in attrs if "key" in a}


def _to_int_ns(ns: Any) -> int:
    """Coerce an OTLP nanosecond timestamp (string or int) to int."""
    if ns is None or ns == "":
        return 0
    try:
        return int(ns)
    except (TypeError, ValueError):
        return 0


def _parse_otlp_json(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Walk an OTLP/JSON ExportTraceServiceRequest and flatten it to span rows.

    Spans missing a service.name are dropped — without one we can't attribute
    the span to an agent, which is the whole point of Oversee.
    """
    parsed: list[dict[str, Any]] = []

    for rs in payload.get("resourceSpans", []) or []:
        resource = rs.get("resource") or {}
        resource_attrs = _attrs_to_dict(resource.get("attributes"))
        service_name = resource_attrs.get("service.name")
        if not service_name:
            continue

        for scope_spans in rs.get("scopeSpans", []) or []:
            for span in scope_spans.get("spans", []) or []:
                status = span.get("status") or {}
                parsed.append(
                    {
                        "trace_id": span.get("traceId", ""),
                        "span_id": span.get("spanId", ""),
                        "parent_span_id": span.get("parentSpanId") or None,
                        "service_name": service_name,
                        "span_name": span.get("name", ""),
                        "kind": int(span.get("kind", 0) or 0),
                        "start_time_unix": _to_int_ns(span.get("startTimeUnixNano")),
                        "end_time_unix": _to_int_ns(span.get("endTimeUnixNano")),
                        "status_code": int(status.get("code", 0) or 0),
                        "status_message": status.get("message", "") or "",
                        "attributes": _attrs_to_dict(span.get("attributes")),
                        "resource_attributes": resource_attrs,
                    }
                )

    return parsed


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok", version=VERSION)


def _auto_describe(
    service_name: str,
    account_id: int | None,
    reason: str,
    agent_id: str | None = None,
) -> bool:
    """Synchronously generate and persist a Claude description for one
    sub-agent of a service. Returns True on success. Swallows all errors
    so the trace ingest path never fails because of a Claude API hiccup.

    `reason` is just for the log line. `agent_id` scopes both the
    prompt's source telemetry AND the saved description row — so donny
    and main each get their own description from their own SOUL.md.
    """
    try:
        result = describer.describe_agent(
            service_name, account_id=account_id, agent_id=agent_id
        )
    except describer.AgentNotFoundError:
        # Can happen if the registration was extracted before any spans
        # for this service exist. The first-time path catches that case
        # after insert_spans runs.
        return False
    except describer.APIKeyMissingError:
        print(
            f"[Oversee] Auto-describe for '{service_name}/{agent_id or 'main'}' "
            f"skipped — ANTHROPIC_API_KEY not configured."
        )
        return False
    except Exception as e:
        print(
            f"[Oversee] Auto-describe for '{service_name}/{agent_id or 'main'}' "
            f"failed: {e}"
        )
        return False

    database.save_description(
        service_name=result["service_name"],
        description=result["description"],
        span_count_analyzed=result["span_count_analyzed"],
        account_id=account_id,
        agent_id=agent_id or "main",
    )
    print(
        f"[Oversee] Auto-described '{service_name}/{agent_id or 'main'}' "
        f"(reason={reason}, source={result.get('source')}, "
        f"chars={len(result['description'])})"
    )
    return True


def _extract_registrations(
    spans: list[dict[str, Any]], account_id: int | None
) -> set[tuple[str, str]]:
    """Pull out any agent_registration spans and persist them as
    registration rows. After each registration is saved, kick off a
    per-agent auto-describe.

    Dedup key is (service_name, agent_id) — each sub-agent gets its own
    description from its own SOUL.md / IDENTITY.md. Returns the set of
    (service_name, agent_id) pairs that were successfully auto-described
    so the caller can skip them in any first-time describe pass.
    """
    described: set[tuple[str, str]] = set()
    for span in spans:
        attrs = span.get("attributes") or {}
        if database.attr(attrs, "event.type") != "agent_registration":
            continue
        service_name = span["service_name"]
        agent_id = database.attr(attrs, "agent.id") or "main"
        database.save_registration(
            service_name=service_name,
            agent_id=agent_id,
            soul=database.attr(attrs, "agent.soul") or "",
            identity=database.attr(attrs, "agent.identity") or "",
            operating_manual=database.attr(attrs, "agent.operating_manual") or "",
            user_context=database.attr(attrs, "agent.user_context") or "",
            memory=database.attr(attrs, "agent.memory") or "",
            workspace_path=database.attr(attrs, "agent.workspace_path") or "",
            model=database.attr(attrs, "agent.model") or "",
            account_id=account_id,
        )
        # Don't re-describe the same (service, agent) twice in one batch.
        key = (service_name, agent_id)
        if key in described:
            continue
        if _auto_describe(
            service_name, account_id, reason="registration", agent_id=agent_id
        ):
            described.add(key)
    return described


@app.post("/v1/traces", response_model=IngestResponse)
async def ingest_traces(request: Request) -> IngestResponse:
    # We accept the raw JSON body rather than a Pydantic-modeled one — see
    # models.py for why. The OTLP/JSON schema is too loose to model strictly
    # without rejecting valid traffic from real agent SDKs.
    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"invalid JSON body: {e}")

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be a JSON object")

    account_id = getattr(request.state, "account_id", None)
    spans = _parse_otlp_json(payload)

    # Snapshot which (service, agent) pairs in this batch are
    # "first-time" — no prior spans for this account. We check before
    # inserting so we can distinguish first-batch telemetry from
    # ongoing telemetry. With per-agent descriptions, this needs to be
    # per-(service, agent), not just per-service.
    batch_pairs: set[tuple[str, str]] = {
        (s["service_name"], _agent_id_for_span(s))
        for s in spans
    }
    first_time_pairs = {
        pair
        for pair in batch_pairs
        if database.get_agent_summary(
            pair[0], account_id=account_id, agent_id=pair[1]
        )
        is None
    }

    # Insert spans first so any subsequent describe_agent calls see them.
    inserted = database.insert_spans(spans, account_id=account_id)

    # Save registrations + auto-describe on the registration path.
    described = _extract_registrations(spans, account_id=account_id)

    # Catch-up describe pass: covers every (service, agent) in this
    # batch that has a registration but no per-agent description yet.
    # This subsumes the old "first-time telemetry" trigger AND handles
    # the case where an agent has been emitting spans for a while but
    # its description still lives under the old service-name-only key
    # (e.g. donny on an instance that was described before per-agent
    # scoping shipped). The early-out on `get_latest_description` keeps
    # this cheap on hot batches — one indexed lookup per (service,
    # agent) pair, and it short-circuits the moment a description
    # exists.
    for service_name, agent_id in batch_pairs - described:
        if (
            database.get_latest_description(
                service_name, account_id=account_id, agent_id=agent_id
            )
            is not None
        ):
            continue
        if (
            database.get_latest_registration(
                service_name, account_id=account_id, agent_id=agent_id
            )
            is None
        ):
            continue
        reason = (
            "first-telemetry"
            if (service_name, agent_id) in first_time_pairs
            else "catchup"
        )
        _auto_describe(
            service_name,
            account_id,
            reason=reason,
            agent_id=agent_id,
        )

    return IngestResponse(status="ok", spans_received=inserted)


def _agent_id_for_span(span: dict[str, Any]) -> str:
    """Mirror of database._agent_id_from_attrs but local to main.py so
    ingest can compute the (service, agent) pair without importing
    private helpers."""
    attrs = span.get("attributes") or {}
    val = database.attr(attrs, "agent.id")
    if isinstance(val, str) and val:
        return val
    return "main"


@app.get("/agents", response_model=list[AgentGroup])
async def list_agents(request: Request) -> list[AgentGroup]:
    """Return the fleet grouped by `service.name`, with a nested list of
    sub-agents inside each instance.
    """
    account_id = getattr(request.state, "account_id", None)
    return [AgentGroup(**a) for a in database.get_agents(account_id=account_id)]


def _humanize_seconds(s: float) -> str:
    """Compact human duration: 45s / 12m / 3h / 2d."""
    s = max(0, int(s))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def _humanize_cadence(seconds: float | None) -> str:
    """Turn a median run-interval into a cadence phrase: hourly / daily / every 15m."""
    if not seconds or seconds <= 0:
        return "regularly"
    if seconds < 90:
        return "about every minute"
    if seconds < 3600:
        return f"about every {int(round(seconds / 60))}m"
    if seconds < 5400:
        return "hourly"
    if seconds < 86400:
        return f"about every {int(round(seconds / 3600))}h"
    if seconds < 129600:
        return "daily"
    return f"about every {int(round(seconds / 86400))}d"


def _detail_status(stats: dict) -> tuple[str, str]:
    """Derive the Agent Detail status + a human reason (never a dot without a
    reason). error → latest record errored; attention → quiet beyond the agent's
    usual cadence; healthy → recently active."""
    from time import time as _time

    last_ns = stats.get("last_record_ns")
    if stats.get("last_record_errored") and last_ns:
        rel = _humanize_seconds(_time() - last_ns / 1_000_000_000)
        op = stats.get("last_error_op") or "the last run"
        return "error", f"Last run failed — {op} errored {rel} ago"
    if last_ns is None:
        # Connected (maybe a registration span) but no real interaction yet.
        return "healthy", "Connected — waiting for its first run"
    age_s = _time() - last_ns / 1_000_000_000
    cadence_s = stats.get("cadence_seconds")
    # Quiet beyond ~2× the usual interval (or >24h when cadence is unknown).
    threshold = (cadence_s * 2) if cadence_s else 86400.0
    if age_s > threshold:
        return (
            "attention",
            f"No runs for {_humanize_seconds(age_s)}; this agent usually runs "
            f"{_humanize_cadence(cadence_s)}",
        )
    return "healthy", f"Active — last run {_humanize_seconds(age_s)} ago"


@app.get("/agents/{service_name}/summary", response_model=AgentSummary)
async def agent_summary(
    service_name: str,
    request: Request,
    agent_id: str | None = Query(default=None),
) -> AgentSummary:
    """Per-instance summary by default; per-agent when `?agent_id=` is set.
    Adds status-with-reason and (regenerating once if missing) the two-field
    description the redesigned detail page renders."""
    account_id = getattr(request.state, "account_id", None)
    summary = database.get_agent_summary(
        service_name, account_id=account_id, agent_id=agent_id
    )
    if summary is None:
        raise HTTPException(status_code=404, detail=f"agent '{service_name}' not found")

    # Status + reason from record-level stats (trace-grouped, registration excluded).
    stats = database.get_agent_record_stats(
        service_name, account_id=account_id, agent_id=agent_id
    )
    summary["status"], summary["status_reason"] = _detail_status(stats)

    # Description v2: regenerate once if the short/long pair is missing (pre-v2
    # rows have no description_long). Best-effort — never fail the page.
    if not summary.get("description_long"):
        try:
            result = describer.describe_agent(
                service_name, account_id=account_id, agent_id=agent_id
            )
            database.save_description(
                service_name=result["service_name"],
                description=result["description"],
                span_count_analyzed=result["span_count_analyzed"],
                account_id=account_id,
                agent_id=agent_id or "main",
                description_long=result.get("description_long"),
            )
            summary["description"] = result["description"]
            summary["description_long"] = result.get("description_long")
        except (describer.APIKeyMissingError, describer.AgentNotFoundError, Exception):
            pass  # keep whatever description we already had

    # View-lock by plan. The detail page still gets identity + status; when
    # locked it swaps the Work Feed for the "recording" panel (count proves the
    # data exists). Telemetry was never gated.
    lock = database.get_locked_state(account_id)
    key = (service_name, agent_id or "main")
    summary["locked"] = key in lock["locked"]
    if summary["locked"]:
        summary["records_count"] = database.count_agent_records(
            service_name, account_id=account_id, agent_id=agent_id
        )
        summary["recording_since"] = lock["first_seen"].get(key) or summary.get("first_seen")

    return AgentSummary(**summary)


@app.get("/agents/{service_name}/spans", response_model=list[SpanRecord])
async def agent_spans(
    service_name: str,
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    agent_id: str | None = Query(default=None),
) -> list[SpanRecord]:
    account_id = getattr(request.state, "account_id", None)
    return [
        SpanRecord(**s)
        for s in database.get_agent_spans(
            service_name, limit, account_id=account_id, agent_id=agent_id
        )
    ]


_REGISTRATION_SUMMARY = "Registered with the fleet and declared its identity"


@app.get("/agents/{service_name}/records", response_model=AgentRecordsResponse)
async def agent_records(
    service_name: str,
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None),
    agent_id: str | None = Query(default=None),
) -> AgentRecordsResponse:
    """The Work Feed: interactions (one per trace_id), newest first, cursor-
    paginated. Each record's plain-English `summary` is generated by Claude
    once and cached permanently by record id (records are immutable). System
    records (registration / no exchange) get a fixed summary — no Claude call.

    When the agent is view-locked by plan, the bodies/exchanges/spans are
    withheld: we return `locked=true` + a records_count + recording_since (proof
    it's being recorded) and no records. Telemetry is never gated, so the moment
    the plan rises these same records become visible with full history."""
    account_id = getattr(request.state, "account_id", None)

    # Withhold record payloads for a locked agent (identity/count only).
    lock = database.get_locked_state(account_id)
    if (service_name, agent_id or "main") in lock["locked"]:
        return AgentRecordsResponse(
            records=[],
            next_cursor=None,
            locked=True,
            records_count=database.count_agent_records(
                service_name, account_id=account_id, agent_id=agent_id
            ),
            recording_since=lock["first_seen"].get((service_name, agent_id or "main")),
        )

    before_ns = None
    if cursor:
        try:
            before_ns = int(cursor)
        except (TypeError, ValueError):
            before_ns = None

    rows, next_cursor = database.get_agent_records(
        service_name,
        account_id=account_id,
        agent_id=agent_id,
        limit=limit,
        before_ns=before_ns,
    )

    out: list[AgentRecord] = []
    for r in rows:
        exchange = r.get("exchange")
        is_system = bool(r.get("is_registration")) or exchange is None
        if is_system:
            summary = _REGISTRATION_SUMMARY
        else:
            # Cache permanently by the immutable record id (trace_id).
            cache_kind = f"record:{r['id']}"
            cached = database.get_insight(
                account_id, service_name, agent_id or "main", cache_kind
            )
            if cached and cached.get("data", {}).get("summary"):
                summary = cached["data"]["summary"]
            else:
                summary = ""
                try:
                    summary = describer.record_summary(
                        exchange.get("user"), exchange.get("agent")
                    )
                except Exception:  # noqa: BLE001 — best-effort
                    summary = ""
                if summary:
                    database.save_insight(
                        account_id,
                        service_name,
                        agent_id or "main",
                        cache_kind,
                        {"summary": summary},
                    )
            if not summary:
                summary = "Handled an interaction"  # graceful fallback
        out.append(
            AgentRecord(
                id=r["id"],
                summary=summary,
                time=r.get("time"),
                cost_usd=r.get("cost_usd"),
                duration_ms=r.get("duration_ms", 0.0),
                tokens=r.get("tokens", 0),
                kind="system" if is_system else "interaction",
                error=bool(r.get("error")),
                exchange=(None if is_system else exchange),
                spans=r.get("spans", []),
            )
        )
    return AgentRecordsResponse(records=out, next_cursor=next_cursor)


@app.post("/agents/{service_name}/describe", response_model=AgentDescription)
async def generate_description(
    service_name: str,
    request: Request,
    agent_id: str | None = Query(default=None),
) -> AgentDescription:
    """Generate a fresh Claude-written description and persist it.

    `agent_id` scopes both the prompt's source telemetry AND the saved
    description — each sub-agent gets its own description row, generated
    from its own SOUL.md / IDENTITY.md.
    """
    account_id = getattr(request.state, "account_id", None)
    try:
        result = describer.describe_agent(
            service_name, account_id=account_id, agent_id=agent_id
        )
    except describer.AgentNotFoundError:
        raise HTTPException(status_code=404, detail=f"agent '{service_name}' not found")
    except describer.APIKeyMissingError as e:
        raise HTTPException(status_code=503, detail=str(e))

    database.save_description(
        service_name=result["service_name"],
        description=result["description"],
        span_count_analyzed=result["span_count_analyzed"],
        account_id=account_id,
        agent_id=agent_id or "main",
        description_long=result.get("description_long"),
    )
    return AgentDescription(**result)


@app.get("/agents/{service_name}/description", response_model=AgentDescription)
async def latest_description(
    service_name: str,
    request: Request,
    agent_id: str | None = Query(default=None),
) -> AgentDescription:
    """Return the most recent saved description for this agent.

    Without `?agent_id=`, returns the most recent across any sub-agent
    (the headline description for the instance). With it, scoped to
    that sub-agent only.
    """
    account_id = getattr(request.state, "account_id", None)
    desc = database.get_latest_description(
        service_name, account_id=account_id, agent_id=agent_id
    )
    if desc is None:
        raise HTTPException(
            status_code=404,
            detail=f"no description has been generated for agent '{service_name}' yet",
        )
    return AgentDescription(**desc)


@app.put("/agents/{service_name}/display-name", status_code=204)
async def set_agent_display_name(
    service_name: str,
    request: Request,
    body: DisplayNameRequest,
) -> None:
    """Set or clear the operator's display name override for one
    sub-agent. Empty/whitespace `display_name` clears the override.
    No-content response (204) on success."""
    account_id = getattr(request.state, "account_id", None)
    database.set_display_name(
        service_name=service_name,
        agent_id=body.agent_id or "main",
        display_name=body.display_name,
        account_id=account_id,
    )


@app.delete(
    "/agents/{service_name}",
    response_model=AgentDeleteResponse,
)
async def delete_agent(
    service_name: str,
    request: Request,
    agent_id: str | None = Query(default=None),
) -> AgentDeleteResponse:
    """Hard-delete an agent. Sweeps every per-agent table — spans,
    descriptions, registrations, display names, owner assignments,
    cached insights, budgets — plus the agent's connection edges and
    any workflows it owns (with their steps, participants, and edges).

    Without `?agent_id=`, deletes the entire service (every sub-agent).
    With it, scopes to one sub-agent. The two cases differ only in
    the WHERE clause; the same helper handles both.

    Returns a per-table row count so the dashboard can show "deleted
    14 spans, 1 description, 1 registration" if it wants to be
    explicit. Idempotent: re-running against an empty service
    returns zeros, not 404.
    """
    account_id = getattr(request.state, "account_id", None)
    summary = database.delete_agent(
        service_name=service_name,
        account_id=account_id,
        agent_id=agent_id,
    )
    return AgentDeleteResponse(
        service_name=service_name,
        agent_id=agent_id,
        deleted_rows=summary,
    )


# ---------------------------------------------------------------------------
# Agent ownership (human owner per sub-agent)
# ---------------------------------------------------------------------------


@app.put("/agents/{service_name}/owner", status_code=204)
async def set_owner(
    service_name: str,
    request: Request,
    body: AgentOwnerSet,
) -> None:
    """Assign a team member as the human owner of one sub-agent.
    Re-assigns when an owner already exists. 204 No Content on success."""
    account_id = getattr(request.state, "account_id", None)
    database.set_agent_owner(
        account_id=account_id,
        service_name=service_name,
        agent_id=body.agent_id or "main",
        team_member_id=body.team_member_id,
    )


@app.delete("/agents/{service_name}/owner", status_code=204)
async def clear_owner(
    service_name: str,
    request: Request,
    agent_id: str = Query(default="main"),
) -> None:
    """Remove the owner assignment for one sub-agent. 204 even when
    nothing was assigned — idempotent."""
    account_id = getattr(request.state, "account_id", None)
    database.remove_agent_owner(
        account_id=account_id,
        service_name=service_name,
        agent_id=agent_id or "main",
    )


# ---------------------------------------------------------------------------
# Team members
# ---------------------------------------------------------------------------


@app.get("/team", response_model=list[TeamMember])
async def list_team(request: Request) -> list[TeamMember]:
    account_id = getattr(request.state, "account_id", None)
    return [
        TeamMember(**m)
        for m in database.get_team_members(account_id=account_id)
    ]


# ---------------------------------------------------------------------------
# Workflows — auto-generated, editable process flows (agent + human steps)
# ---------------------------------------------------------------------------


@app.get("/workflows", response_model=list[Workflow])
async def list_workflows(request: Request) -> list[Workflow]:
    """All workflows for the account (most recently updated first), each with
    step_count + participant_count + a derived health status (degraded when any
    participant agent is degraded/offline). Steps load on the detail endpoint."""
    account_id = getattr(request.state, "account_id", None)
    workflows = database.get_workflows(account_id=account_id)
    # Health map keyed by service_name (reuse the dashboard's _agent_status rule).
    health: dict[str, str] = {}
    try:
        for a in database.get_agents(account_id=account_id):
            health[a["service_name"]] = _agent_status(a)
    except Exception:  # noqa: BLE001 — status is best-effort; never fail the list
        health = {}
    out = []
    for w in workflows:
        status = "healthy"
        for p in w.get("participants", []):
            if p.get("type") == "agent" and health.get(p.get("agent_service_name")) in (
                "degraded",
                "offline",
            ):
                status = "degraded"
                break
        w["status"] = status
        out.append(Workflow(**w))
    return out


@app.post("/workflows", response_model=Workflow, status_code=201)
async def create_workflow(request: Request, body: WorkflowCreate) -> Workflow:
    account_id = getattr(request.state, "account_id", None)
    wf = database.create_workflow(
        account_id=account_id,
        name=body.name,
        description=body.description,
        agent_service_name=body.agent_service_name,
        agent_id=body.agent_id or "main",
    )
    return Workflow(**database.get_workflow(wf["id"], account_id=account_id))


@app.get("/workflows/{workflow_id}", response_model=Workflow)
async def get_workflow(workflow_id: int, request: Request) -> Workflow:
    account_id = getattr(request.state, "account_id", None)
    wf = database.get_workflow(workflow_id, account_id=account_id)
    if wf is None:
        raise HTTPException(status_code=404, detail="workflow not found")
    return Workflow(**wf)


@app.get("/workflows/{workflow_id}/stats", response_model=WorkflowStats)
async def workflow_stats(workflow_id: int, request: Request) -> WorkflowStats:
    """Live telemetry for the workflow: legacy single-agent all-time fields
    plus the multi-agent 24h overlay (per-step runs/duration/success + a
    workflow rollup). escalation_rate / avg_human_wait_ms are best-effort and
    null when not derivable from spans."""
    account_id = getattr(request.state, "account_id", None)
    legacy = database.get_workflow_stats(workflow_id, account_id=account_id)
    if legacy is None:
        raise HTTPException(status_code=404, detail="workflow not found")
    step_stats = database.get_workflow_step_stats(workflow_id, account_id=account_id)
    merged = {**legacy, **(step_stats or {})}
    return WorkflowStats(**merged)


@app.put("/workflows/{workflow_id}", response_model=Workflow)
async def update_workflow(
    workflow_id: int, request: Request, body: WorkflowUpdate
) -> Workflow:
    account_id = getattr(request.state, "account_id", None)
    wf = database.update_workflow(
        workflow_id, account_id=account_id, name=body.name, description=body.description
    )
    if wf is None:
        raise HTTPException(status_code=404, detail="workflow not found")
    return Workflow(**wf)


@app.delete("/workflows/{workflow_id}", status_code=204)
async def delete_workflow(workflow_id: int, request: Request) -> None:
    account_id = getattr(request.state, "account_id", None)
    if not database.delete_workflow(workflow_id, account_id=account_id):
        raise HTTPException(status_code=404, detail="workflow not found")


@app.post(
    "/workflows/{workflow_id}/steps",
    response_model=WorkflowStep,
    status_code=201,
)
async def add_workflow_step(
    workflow_id: int, request: Request, body: WorkflowStepCreate
) -> WorkflowStep:
    account_id = getattr(request.state, "account_id", None)
    # Ownership gate — only operate on a workflow this account owns.
    if database.get_workflow(workflow_id, account_id=account_id) is None:
        raise HTTPException(status_code=404, detail="workflow not found")
    step = database.add_workflow_step(workflow_id, body.model_dump())
    return WorkflowStep(**step)


@app.put(
    "/workflows/{workflow_id}/steps/{step_id}",
    response_model=WorkflowStep,
)
async def update_workflow_step(
    workflow_id: int, step_id: int, request: Request, body: WorkflowStepUpdate
) -> WorkflowStep:
    account_id = getattr(request.state, "account_id", None)
    if database.get_workflow(workflow_id, account_id=account_id) is None:
        raise HTTPException(status_code=404, detail="workflow not found")
    step = database.update_workflow_step(
        step_id, workflow_id, body.model_dump(exclude_unset=True)
    )
    if step is None:
        raise HTTPException(status_code=404, detail="step not found")
    return WorkflowStep(**step)


@app.delete(
    "/workflows/{workflow_id}/steps/{step_id}", status_code=204
)
async def delete_workflow_step(
    workflow_id: int, step_id: int, request: Request
) -> None:
    account_id = getattr(request.state, "account_id", None)
    if database.get_workflow(workflow_id, account_id=account_id) is None:
        raise HTTPException(status_code=404, detail="workflow not found")
    if not database.delete_workflow_step(step_id, workflow_id):
        raise HTTPException(status_code=404, detail="step not found")


@app.post("/workflows/{workflow_id}/steps/reorder", response_model=Workflow)
async def reorder_workflow_steps(
    workflow_id: int, request: Request, body: WorkflowReorder
) -> Workflow:
    account_id = getattr(request.state, "account_id", None)
    if database.get_workflow(workflow_id, account_id=account_id) is None:
        raise HTTPException(status_code=404, detail="workflow not found")
    database.reorder_workflow_steps(workflow_id, body.step_ids)
    return Workflow(**database.get_workflow(workflow_id, account_id=account_id))


# ---------------------------------------------------------------------------
# Graph editing — granular edge + participant CRUD (manual workflow editor).
# Ownership is gated on the workflow; DB helpers additionally scope by
# workflow_id so a forged child id from another workflow 404s.
# ---------------------------------------------------------------------------


@app.post(
    "/workflows/{workflow_id}/edges", response_model=WorkflowEdge, status_code=201
)
async def add_workflow_edge(
    workflow_id: int, request: Request, body: WorkflowEdgeCreate
) -> WorkflowEdge:
    account_id = getattr(request.state, "account_id", None)
    wf = database.get_workflow(workflow_id, account_id=account_id)
    if wf is None:
        raise HTTPException(status_code=404, detail="workflow not found")
    if body.from_step_id == body.to_step_id:
        raise HTTPException(status_code=400, detail="an edge cannot connect a step to itself")
    step_ids = {s["id"] for s in wf["steps"]}
    if body.from_step_id not in step_ids or body.to_step_id not in step_ids:
        raise HTTPException(status_code=404, detail="from_step_id or to_step_id not in this workflow")
    for e in wf["edges"]:
        if e["from_step_id"] == body.from_step_id and e["to_step_id"] == body.to_step_id:
            raise HTTPException(status_code=409, detail="edge already exists")
    edge = database.add_workflow_edge(
        workflow_id,
        from_step_id=body.from_step_id,
        to_step_id=body.to_step_id,
        label=body.label,
        is_branch=body.is_branch,
        edge_order=body.edge_order,
    )
    return WorkflowEdge(**edge)


@app.put(
    "/workflows/{workflow_id}/edges/{edge_id}", response_model=WorkflowEdge
)
async def update_workflow_edge(
    workflow_id: int, edge_id: int, request: Request, body: WorkflowEdgeUpdate
) -> WorkflowEdge:
    account_id = getattr(request.state, "account_id", None)
    if database.get_workflow(workflow_id, account_id=account_id) is None:
        raise HTTPException(status_code=404, detail="workflow not found")
    edge = database.update_workflow_edge(
        edge_id, workflow_id, body.model_dump(exclude_unset=True)
    )
    if edge is None:
        raise HTTPException(status_code=404, detail="edge not found")
    return WorkflowEdge(**edge)


@app.delete("/workflows/{workflow_id}/edges/{edge_id}", status_code=204)
async def delete_workflow_edge(
    workflow_id: int, edge_id: int, request: Request
) -> None:
    account_id = getattr(request.state, "account_id", None)
    if database.get_workflow(workflow_id, account_id=account_id) is None:
        raise HTTPException(status_code=404, detail="workflow not found")
    if not database.delete_workflow_edge(edge_id, workflow_id):
        raise HTTPException(status_code=404, detail="edge not found")


@app.post(
    "/workflows/{workflow_id}/participants",
    response_model=WorkflowParticipant,
    status_code=201,
)
async def add_workflow_participant(
    workflow_id: int, request: Request, body: WorkflowParticipantCreate
) -> WorkflowParticipant:
    account_id = getattr(request.state, "account_id", None)
    wf = database.get_workflow(workflow_id, account_id=account_id)
    if wf is None:
        raise HTTPException(status_code=404, detail="workflow not found")
    ptype = str(body.type or "").strip().lower()
    if ptype not in ("agent", "human"):
        raise HTTPException(status_code=400, detail="type must be 'agent' or 'human'")
    if ptype == "agent" and not body.agent_service_name:
        raise HTTPException(status_code=400, detail="agent participant requires agent_service_name")
    if ptype == "human" and not (body.role_name or "").strip():
        raise HTTPException(status_code=400, detail="human participant requires role_name")
    # Idempotent dedupe — agents on (service, agent_id), humans on role (lower).
    aid = (body.agent_id or "main")
    for p in wf["participants"]:
        if ptype == "agent" and p["type"] == "agent" and (
            p["agent_service_name"] == body.agent_service_name
            and (p["agent_id"] or "main") == aid
        ):
            raise HTTPException(status_code=409, detail="agent already a participant")
        if ptype == "human" and p["type"] == "human" and (
            (p["role_name"] or "").strip().lower() == body.role_name.strip().lower()
        ):
            raise HTTPException(status_code=409, detail="human role already a participant")
    participant = database.add_workflow_participant(
        workflow_id,
        ptype,
        agent_service_name=body.agent_service_name,
        agent_id=aid if ptype == "agent" else None,
        role_name=body.role_name,
        team_member_id=body.team_member_id,
    )
    return WorkflowParticipant(**participant)


@app.delete(
    "/workflows/{workflow_id}/participants/{participant_id}", status_code=204
)
async def delete_workflow_participant(
    workflow_id: int, participant_id: int, request: Request
) -> None:
    account_id = getattr(request.state, "account_id", None)
    if database.get_workflow(workflow_id, account_id=account_id) is None:
        raise HTTPException(status_code=404, detail="workflow not found")
    if not database.delete_workflow_participant(participant_id, workflow_id):
        raise HTTPException(status_code=404, detail="participant not found")


@app.post(
    "/workflows/{workflow_id}/ai-edit", response_model=WorkflowAiEditResult
)
async def ai_edit_workflow(
    workflow_id: int, request: Request, body: WorkflowAiEdit
) -> WorkflowAiEditResult:
    """Apply a plain-English edit instruction to an existing workflow. Claude
    returns a minimal set of edit operations (add/update/delete step, add/delete
    edge, add participant) that preserve existing step ids; we apply them and
    return the updated graph + a one-line summary."""
    account_id = getattr(request.state, "account_id", None)
    wf = database.get_workflow(workflow_id, account_id=account_id)
    if wf is None:
        raise HTTPException(status_code=404, detail="workflow not found")
    if not (body.instruction or "").strip():
        raise HTTPException(status_code=400, detail="instruction is required")
    agents = database.get_agents(account_id=account_id)
    try:
        result = describer.workflow_edit_operations(wf, body.instruction, agents)
    except describer.APIKeyMissingError:
        raise HTTPException(
            status_code=503,
            detail="AI is unavailable — the backend needs an ANTHROPIC_API_KEY.",
        )
    applied = database.apply_workflow_edit_operations(
        workflow_id, result.get("operations") or []
    )
    updated = database.get_workflow(workflow_id, account_id=account_id)
    return WorkflowAiEditResult(
        summary=result.get("summary") or "",
        applied=applied,
        workflow=Workflow(**updated),
    )


def _build_agents_context(
    account_id: int | None, agent_refs: list[dict]
) -> list[dict]:
    """Gather summary + description + top-ops + identity excerpt for each
    (service_name, agent_id) — the context the multi-agent graph builder needs."""
    ctx = []
    for ref in agent_refs:
        svc = ref.get("service_name")
        aid = ref.get("agent_id") or "main"
        if not svc:
            continue
        summary = database.get_agent_summary(svc, account_id=account_id, agent_id=aid)
        if summary is None:
            continue
        reg = database.get_latest_registration(svc, account_id=account_id, agent_id=aid) or {}
        reg_excerpt = " ".join(
            str(reg.get(k) or "") for k in ("soul", "identity", "operating_manual")
        ).strip()
        ctx.append(
            {
                "service_name": svc,
                "agent_id": aid,
                "display_name": summary.get("display_name"),
                "description": summary.get("description"),
                "top_operations": summary.get("top_operations") or [],
                "registration_excerpt": reg_excerpt,
            }
        )
    return ctx


def _merge_participants(graph_parts: list[dict], agent_refs: list[dict], human_roles: list[str]) -> list[dict]:
    """Ensure every requested agent + human role is represented, on top of
    whatever Claude returned. Dedupes by (service, agent_id) and role name."""
    parts = list(graph_parts or [])
    have_agents = {
        (p.get("agent_service_name"), p.get("agent_id") or "main")
        for p in parts
        if p.get("type") == "agent"
    }
    have_roles = {(p.get("role_name") or "").lower() for p in parts if p.get("type") == "human"}
    for r in agent_refs or []:
        key = (r.get("service_name"), r.get("agent_id") or "main")
        if key[0] and key not in have_agents:
            have_agents.add(key)
            parts.append({"type": "agent", "agent_service_name": key[0], "agent_id": key[1]})
    for role in human_roles or []:
        rl = (role or "").strip()
        if rl and rl.lower() not in have_roles:
            have_roles.add(rl.lower())
            parts.append({"type": "human", "role_name": rl})
    return parts


@app.post("/workflows/generate", response_model=Workflow, status_code=201)
async def generate_workflow(request: Request, body: WorkflowGenerate) -> Workflow:
    """Auto-generate a workflow from telemetry + identity. Single-agent
    (legacy): pass `agent_service_name`. Multi-agent: `method="agents"` with
    `agents[]` (+ optional `human_roles[]`) → Claude infers a multi-agent graph."""
    account_id = getattr(request.state, "account_id", None)
    is_multi = (body.method == "agents") or bool(body.agents)

    if is_multi:
        refs = [
            {"service_name": a.service_name, "agent_id": a.agent_id or "main"}
            for a in body.agents
        ]
        if not refs:
            raise HTTPException(status_code=400, detail="agents is required for method 'agents'")
        ctx = _build_agents_context(account_id, refs)
        if not ctx:
            raise HTTPException(status_code=404, detail="none of the specified agents have telemetry")
        try:
            graph = describer.workflow_graph_from_agents(ctx)
        except describer.APIKeyMissingError as e:
            raise HTTPException(status_code=503, detail=str(e))
        wf = database.create_workflow(
            account_id=account_id,
            name=body.name,
            description=None,
            agent_service_name=ctx[0]["service_name"],
            agent_id=ctx[0]["agent_id"],
            method="agents",
        )
        participants = _merge_participants(graph["participants"], refs, body.human_roles)
        database._replace_workflow_graph(
            wf["id"], participants, graph["steps"], graph["edges"]
        )
        return Workflow(**database.get_workflow(wf["id"], account_id=account_id))

    # Single-agent (legacy) path — unchanged behavior + a sole participant row.
    if not body.agent_service_name:
        raise HTTPException(status_code=400, detail="agent_service_name is required")
    try:
        steps = describer.generate_workflow(
            body.agent_service_name,
            account_id=account_id,
            agent_id=(body.agent_id or "main"),
        )
    except describer.AgentNotFoundError:
        raise HTTPException(
            status_code=404, detail=f"agent '{body.agent_service_name}' not found"
        )
    except describer.APIKeyMissingError as e:
        raise HTTPException(status_code=503, detail=str(e))

    wf = database.create_workflow(
        account_id=account_id,
        name=body.name,
        description=None,
        agent_service_name=body.agent_service_name,
        agent_id=(body.agent_id or "main"),
        method="generate",
    )
    database._replace_workflow_steps(wf["id"], steps)
    database.add_workflow_participant(
        wf["id"], "agent",
        agent_service_name=body.agent_service_name,
        agent_id=(body.agent_id or "main"),
    )
    return Workflow(**database.get_workflow(wf["id"], account_id=account_id))


@app.post("/workflows/describe", response_model=Workflow, status_code=201)
async def describe_workflow(request: Request, body: WorkflowDescribe) -> Workflow:
    """Build a full multi-agent workflow graph (participants, steps, edges,
    positions) from a plain-English description."""
    account_id = getattr(request.state, "account_id", None)
    agents_ctx = [
        {
            "service_name": g["service_name"],
            "display_name": g.get("display_name"),
            "description": g.get("description"),
        }
        for g in database.get_agents(account_id=account_id)
    ]
    try:
        graph = describer.workflow_graph_from_description(body.description, agents_ctx)
    except describer.APIKeyMissingError as e:
        raise HTTPException(status_code=503, detail=str(e))
    primary = None
    primary_aid = "main"
    for p in graph["participants"]:
        if p.get("type") == "agent" and p.get("agent_service_name"):
            primary = p["agent_service_name"]
            primary_aid = p.get("agent_id") or "main"
            break
    wf = database.create_workflow(
        account_id=account_id,
        name=body.name,
        description=body.description,
        agent_service_name=primary,
        agent_id=primary_aid,
        method="describe",
        source_description=body.description,
    )
    database._replace_workflow_graph(
        wf["id"], graph["participants"], graph["steps"], graph["edges"]
    )
    return Workflow(**database.get_workflow(wf["id"], account_id=account_id))


@app.put(
    "/workflows/{workflow_id}/steps/{step_id}/position", response_model=WorkflowStep
)
async def update_workflow_step_position(
    workflow_id: int, step_id: int, request: Request, body: StepPosition
) -> WorkflowStep:
    """Persist a node's canvas position (drag-to-reposition)."""
    account_id = getattr(request.state, "account_id", None)
    if database.get_workflow(workflow_id, account_id=account_id) is None:
        raise HTTPException(status_code=404, detail="workflow not found")
    step = database.update_step_position(step_id, workflow_id, body.pos_x, body.pos_y)
    if step is None:
        raise HTTPException(status_code=404, detail="step not found")
    return WorkflowStep(**step)


@app.post("/workflows/from-description", response_model=Workflow, status_code=201)
async def workflow_from_description(
    request: Request, body: WorkflowFromDescription
) -> Workflow:
    """Draft a workflow from a plain-English description (AI builder). Steps
    can reference the account's known agents."""
    account_id = getattr(request.state, "account_id", None)
    known = [g["service_name"] for g in database.get_agents(account_id=account_id)]
    try:
        steps = describer.workflow_from_description(body.description, known_agents=known)
    except describer.APIKeyMissingError as e:
        raise HTTPException(status_code=503, detail=str(e))
    wf = database.create_workflow(
        account_id=account_id,
        name=body.name,
        description=body.description,
        agent_service_name=body.agent_service_name,
        agent_id=(body.agent_id or "main"),
    )
    database._replace_workflow_steps(wf["id"], steps)
    return Workflow(**database.get_workflow(wf["id"], account_id=account_id))


@app.post("/team", response_model=TeamMember, status_code=201)
async def add_team_member(
    request: Request, body: TeamMemberCreate
) -> TeamMember:
    account_id = getattr(request.state, "account_id", None)
    try:
        m = database.create_team_member(
            account_id=account_id,
            name=body.name,
            email=body.email,
            role=body.role,
        )
    except database.TeamMemberEmailExistsError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return TeamMember(**m)


# ---------------------------------------------------------------------------
# Waitlist (public — no auth; see _OPEN_PATHS)
# ---------------------------------------------------------------------------

# Pragmatic email shape check (a non-empty local part, an @, and a dotted
# domain). Matches the client-side regex on the marketing site — we don't
# pull in a full RFC-5322 validator for a marketing funnel.
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


@app.post("/waitlist", response_model=WaitlistResponse)
async def join_waitlist(body: WaitlistRequest) -> WaitlistResponse:
    """Public marketing-site signup. No auth. Idempotent: a repeat email
    returns {"status": "already_joined"} with 200 rather than erroring."""
    email = (body.email or "").strip()
    if not _EMAIL_RE.match(email):
        raise HTTPException(status_code=422, detail="Please enter a valid email address.")
    status = database.add_waitlist_signup(
        email=email,
        source=body.source,
        runtime_interest=body.runtime_interest,
    )
    return WaitlistResponse(status=status)


@app.get("/waitlist/count", response_model=WaitlistCountResponse)
async def get_waitlist_count() -> WaitlistCountResponse:
    """Public count of waitlist signups (for display on the marketing site)."""
    return WaitlistCountResponse(count=database.get_waitlist_count())


@app.delete("/waitlist/{email}", response_model=WaitlistDeleteResponse)
async def delete_waitlist(email: str, request: Request) -> WaitlistDeleteResponse:
    """Remove a waitlist signup — operator-only. Unlike POST/GET /waitlist this
    path is NOT in _OPEN_PATHS, so the auth middleware requires a valid Trovis
    credential (session or API key). Used to clear test/bogus entries so they
    don't skew the marketing-site count."""
    account_id = getattr(request.state, "account_id", None)
    if account_id is None:
        raise HTTPException(status_code=401, detail="authentication required")
    deleted = database.delete_waitlist_signup(email)
    return WaitlistDeleteResponse(deleted=deleted > 0, email=(email or "").strip().lower())


@app.delete("/team/{member_id}", status_code=204)
async def remove_team_member(member_id: int, request: Request) -> None:
    """Delete a team member and clear any agent assignments that
    pointed to them. 204 even when no row was deleted — idempotent."""
    account_id = getattr(request.state, "account_id", None)
    database.delete_team_member(account_id=account_id, member_id=member_id)


@app.get("/team/{member_id}/agents", response_model=list[OwnedAgent])
async def get_team_member_agents(
    member_id: int, request: Request
) -> list[OwnedAgent]:
    """Return the agents owned by this team member, with display name
    and basic stats. Empty list when the member has no assignments."""
    account_id = getattr(request.state, "account_id", None)
    return [
        OwnedAgent(**a)
        for a in database.get_agents_for_team_member(
            account_id=account_id, member_id=member_id
        )
    ]


@app.get("/agents/{service_name}/registration", response_model=AgentRegistration)
async def latest_registration(
    service_name: str,
    request: Request,
    agent_id: str | None = Query(default=None),
) -> AgentRegistration:
    """Return the most recent registration payload (SOUL, IDENTITY, etc.).

    Without `?agent_id=`, returns the most recent across any sub-agent;
    with it, scoped to that sub-agent's registration row.
    """
    account_id = getattr(request.state, "account_id", None)
    reg = database.get_latest_registration(
        service_name, account_id=account_id, agent_id=agent_id
    )
    if reg is None:
        raise HTTPException(
            status_code=404,
            detail=f"no registration found for agent '{service_name}'",
        )
    return AgentRegistration(**reg)


@app.get("/agents/{service_name}/outputs", response_model=list[AgentOutput])
async def agent_outputs(
    service_name: str,
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    agent_id: str | None = Query(default=None),
) -> list[AgentOutput]:
    """Return recent captured outputs (message bodies, responses, tool
    results) for this agent. Empty list when nothing's been captured —
    typically because the plugin's captureOutputs flag is off."""
    account_id = getattr(request.state, "account_id", None)
    return [
        AgentOutput(**o)
        for o in database.get_agent_outputs(
            service_name,
            account_id=account_id,
            limit=limit,
            agent_id=agent_id,
        )
    ]


@app.get("/agents/{service_name}/costs", response_model=AgentCosts)
async def agent_costs(
    service_name: str,
    request: Request,
    agent_id: str | None = Query(default=None),
    days: int = Query(default=7, ge=1, le=365),
) -> AgentCosts:
    """Token usage + estimated USD cost for an agent over the last
    `days` days. Returns totals plus per-day and per-model breakdowns.
    Only spans that carried `gen_ai.usage.*` token counts contribute —
    everything else has NULL tokens and is ignored."""
    account_id = getattr(request.state, "account_id", None)
    return AgentCosts(
        **database.get_agent_costs(
            service_name,
            account_id=account_id,
            agent_id=agent_id,
            days=days,
        )
    )


# ---------------------------------------------------------------------------
# Model pricing admin
# ---------------------------------------------------------------------------
#
# Pricing is global (same list price for every tenant), so these endpoints
# aren't account-scoped — but they still sit behind the API-key middleware,
# so only an authenticated caller can read or trigger a refresh. The daily
# in-process scheduler keeps the table fresh on its own; these are for
# on-demand pulls and verification.


@app.get("/admin/pricing")
async def get_pricing(request: Request) -> dict[str, Any]:
    """Current pricing-table state: model count, per-source breakdown, last
    refresh time, and a small sample. Handy for confirming the daily sync is
    landing."""
    return database.get_pricing_summary()


@app.get("/admin/pricing/coverage")
async def get_pricing_coverage(
    request: Request,
    days: int = Query(default=30, ge=1, le=365),
) -> dict[str, Any]:
    """Which models seen in telemetry actually resolve to a price. The
    `unmatched` list (models burning tokens with NULL cost, biggest first)
    is the answer to "is cost being tracked?" — anything there is cost we're
    silently dropping and should add to the price table."""
    account_id = getattr(request.state, "account_id", None)
    return database.get_pricing_coverage(account_id=account_id, days=days)


@app.post("/admin/pricing/refresh")
async def refresh_pricing_now(request: Request) -> dict[str, Any]:
    """Pull the latest model prices from the LiteLLM list immediately,
    rather than waiting for the daily cycle. Runs the blocking fetch in a
    worker thread so the event loop isn't stalled. Then re-prices stored
    spans so historical cost reflects the refreshed rates."""
    try:
        summary = await asyncio.to_thread(pricing_sync.refresh_pricing)
        recomputed = await asyncio.to_thread(database.recompute_span_costs)
        return {**summary, "recompute": recomputed}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=502,
            detail=f"Pricing refresh failed: {e}",
        )


@app.post("/admin/pricing/recompute")
async def recompute_costs_now(request: Request) -> dict[str, Any]:
    """Re-price stored spans with the current pricing table (account-scoped),
    without re-fetching prices. Use after correcting a rate or adding a model
    so the displayed cost stops being frozen at ingest."""
    account_id = getattr(request.state, "account_id", None)
    return await asyncio.to_thread(database.recompute_span_costs, account_id)


# ---------------------------------------------------------------------------
# Weekly summary + capability map
# ---------------------------------------------------------------------------
#
# Both endpoints follow the same pattern: compute the cheap stuff (DB
# aggregates) every time, cache the expensive Claude-generated bits
# with a TTL. The summary text / capability JSON is what gets cached
# (via agent_insights); the underlying stats are always recomputed.

_WEEKLY_SUMMARY_TTL_SECONDS = 60 * 60  # 1 hour
_CAPABILITIES_TTL_SECONDS = 24 * 60 * 60  # 24 hours
_DASHBOARD_TTL_SECONDS = 60 * 60  # 1 hour — briefing/attention/work-feed
# Sentinel service_name for account-level (not per-agent) cached insights.
_DASHBOARD_SENTINEL = "__dashboard__"
_NS_PER_DAY = 24 * 60 * 60 * 1_000_000_000


def _pct_delta(current: float, previous: float) -> float | None:
    """Percent change from `previous` to `current`. None when no prior
    baseline (previous == 0 with current also 0 → no signal). Caps
    very large deltas at ±999% so the UI doesn't have to handle
    huge numbers from divide-by-near-zero cases.
    """
    if previous == 0:
        return None if current == 0 else 999.0
    raw = (current - previous) / previous * 100.0
    if raw > 999.0:
        return 999.0
    if raw < -999.0:
        return -999.0
    return raw


@app.get("/agents/{service_name}/weekly", response_model=WeeklySummary)
async def weekly_summary_endpoint(
    service_name: str,
    request: Request,
    agent_id: str | None = Query(default=None),
) -> WeeklySummary:
    """Weekly stats + Claude-generated plain-English summary. Stats
    are always fresh; the summary text is cached for 1 hour.

    Wrapped in a defensive try/except so any unexpected DB or
    serialization error logs a full traceback (visible in Railway
    logs) and returns a graceful unavailable state instead of a
    bare 500. We never want the AgentDetail page to break because
    of a weekly-summary bug.
    """
    try:
        return _weekly_summary_impl(service_name, request, agent_id)
    except Exception as e:  # noqa: BLE001
        import traceback as _tb

        print(
            f"[Oversee] /weekly failed for '{service_name}/"
            f"{agent_id or 'main'}': {type(e).__name__}: {e}"
        )
        _tb.print_exc()
        # Empty-but-valid response so the frontend renders the
        # "unavailable" callout rather than "Failed to fetch".
        return WeeklySummary(
            runs=0,
            errors=0,
            success_rate=0.0,
            avg_duration_ms=0.0,
            summary_unavailable=True,
        )


def _weekly_summary_impl(
    service_name: str,
    request: Request,
    agent_id: str | None,
) -> WeeklySummary:
    account_id = getattr(request.state, "account_id", None)
    aid = agent_id or "main"

    # Day-aligned windows: now-back-7d for "this week", and the prior
    # 7d for the comparison. Both denoted in nanoseconds because that's
    # what the spans table stores.
    from time import time as _time

    now_ns = int(_time() * 1_000_000_000)
    week_ns = 7 * _NS_PER_DAY
    this_week = database.get_window_aggregate(
        service_name=service_name,
        agent_id=aid,
        start_time_ns=now_ns - week_ns,
        end_time_ns=now_ns,
        account_id=account_id,
    )
    last_week = database.get_window_aggregate(
        service_name=service_name,
        agent_id=aid,
        start_time_ns=now_ns - 2 * week_ns,
        end_time_ns=now_ns - week_ns,
        account_id=account_id,
    )
    last_week_has_data = last_week["runs"] > 0
    lw_for_trends = last_week if last_week_has_data else None

    # Token + cost totals for the same 7-day window, so the "This week" strip
    # can show them. get_window_aggregate covers runs/success only; tokens and
    # cost live on the spans and are summed by get_agent_costs. Defensive —
    # a costs failure must not break the weekly summary.
    week_tokens = 0
    week_cost: float | None = None
    try:
        costs = database.get_agent_costs(
            service_name, account_id=account_id, agent_id=aid, days=7
        )
        week_tokens = int(costs.get("total_tokens") or 0)
        wc = costs.get("estimated_cost_usd")
        week_cost = float(wc) if wc else None
    except Exception as e:  # noqa: BLE001 — never 500 the page over a costs sum
        print(f"[Oversee] Weekly cost sum for '{service_name}/{aid}' failed: {e}")

    trends = WeeklyTrends()
    if lw_for_trends:
        trends = WeeklyTrends(
            runs_delta_pct=_pct_delta(this_week["runs"], lw_for_trends["runs"]),
            errors_delta_pct=_pct_delta(this_week["errors"], lw_for_trends["errors"]),
            success_rate_delta_pct=_pct_delta(
                this_week["success_rate"], lw_for_trends["success_rate"]
            ),
            avg_duration_delta_pct=_pct_delta(
                this_week["avg_duration_ms"], lw_for_trends["avg_duration_ms"]
            ),
        )

    # Cache lookup for the Claude-generated text. Stats are NEVER
    # cached — they're always recomputed from the database.
    cached = database.get_insight(
        account_id=account_id,
        service_name=service_name,
        agent_id=aid,
        kind="weekly_summary",
        max_age_seconds=_WEEKLY_SUMMARY_TTL_SECONDS,
    )
    summary_text = ""
    summary_unavailable = False
    generated_at: str | None = None

    if cached:
        summary_text = cached["data"].get("summary", "")
        generated_at = cached["generated_at"]
    else:
        # Pull registration + a few captured outputs to give Claude
        # extra context. Both are optional — the summary still works
        # without them.
        registration = database.get_latest_registration(
            service_name, account_id=account_id, agent_id=aid
        )
        outputs = database.get_agent_outputs(
            service_name, account_id=account_id, limit=3, agent_id=aid
        )
        try:
            summary_text = describer.weekly_summary(
                service_name=service_name,
                agent_id=aid,
                this_week=this_week,
                last_week=lw_for_trends,
                registration=registration,
                outputs=outputs,
            )
            database.save_insight(
                account_id=account_id,
                service_name=service_name,
                agent_id=aid,
                kind="weekly_summary",
                data={"summary": summary_text},
            )
        except describer.APIKeyMissingError:
            summary_unavailable = True
        except Exception as e:  # noqa: BLE001 — never 500 the page over Claude
            print(f"[Oversee] Weekly summary for '{service_name}/{aid}' failed: {e}")
            summary_unavailable = True

    return WeeklySummary(
        runs=this_week["runs"],
        errors=this_week["errors"],
        success_rate=this_week["success_rate"],
        avg_duration_ms=this_week["avg_duration_ms"],
        tools_used=this_week["tools_used"],
        operations=this_week["operations"],
        cost_estimate=week_cost,
        tokens=week_tokens,
        cost=week_cost,
        trends=trends,
        summary=summary_text,
        summary_unavailable=summary_unavailable,
        generated_at=generated_at,
    )


@app.get("/agents/{service_name}/capabilities", response_model=Capabilities)
async def capabilities_endpoint(
    service_name: str,
    request: Request,
    agent_id: str | None = Query(default=None),
) -> Capabilities:
    """Three-bucket capability map. Defensive try/except — see
    weekly_summary_endpoint for rationale."""
    try:
        return _capabilities_impl(service_name, request, agent_id)
    except Exception as e:  # noqa: BLE001
        import traceback as _tb

        print(
            f"[Oversee] /capabilities failed for '{service_name}/"
            f"{agent_id or 'main'}': {type(e).__name__}: {e}"
        )
        _tb.print_exc()
        return Capabilities(unavailable=True)


def _capabilities_impl(
    service_name: str,
    request: Request,
    agent_id: str | None,
) -> Capabilities:
    account_id = getattr(request.state, "account_id", None)
    aid = agent_id or "main"

    cached = database.get_insight(
        account_id=account_id,
        service_name=service_name,
        agent_id=aid,
        kind="capabilities",
        max_age_seconds=_CAPABILITIES_TTL_SECONDS,
    )
    if cached:
        data = cached["data"]
        return Capabilities(
            reads_from=data.get("reads_from", []) or [],
            writes_to=data.get("writes_to", []) or [],
            can_do=data.get("can_do", []) or [],
            generated_at=cached["generated_at"],
        )

    # Recompute. We use a 14-day window for the tool/op mining — wider
    # than weekly so we don't miss capabilities that fire infrequently.
    from time import time as _time

    now_ns = int(_time() * 1_000_000_000)
    window = database.get_window_aggregate(
        service_name=service_name,
        agent_id=aid,
        start_time_ns=now_ns - 14 * _NS_PER_DAY,
        end_time_ns=now_ns,
        account_id=account_id,
    )
    registration = database.get_latest_registration(
        service_name, account_id=account_id, agent_id=aid
    )

    try:
        caps = describer.capabilities(
            service_name=service_name,
            agent_id=aid,
            registration=registration,
            tools_used=window["tools_used"],
            operations=window["operations"],
        )
    except describer.APIKeyMissingError:
        return Capabilities(unavailable=True)
    except Exception as e:  # noqa: BLE001
        print(f"[Oversee] Capabilities for '{service_name}/{aid}' failed: {e}")
        return Capabilities(unavailable=True)

    database.save_insight(
        account_id=account_id,
        service_name=service_name,
        agent_id=aid,
        kind="capabilities",
        data=caps,
    )
    return Capabilities(
        reads_from=caps["reads_from"],
        writes_to=caps["writes_to"],
        can_do=caps["can_do"],
        generated_at=None,
    )


# ---------------------------------------------------------------------------
# Auth endpoints — real users + Individual/Business orgs
# ---------------------------------------------------------------------------
#
# Humans log in with email+password → a session token (Bearer). Agents keep
# using org API keys for telemetry. An account == an organization (tenant).

_MIN_PASSWORD_LEN = 10
_GENERIC_LOGIN_ERROR = "invalid email or password"


def _validate_password(pw: str) -> None:
    if not pw or len(pw) < _MIN_PASSWORD_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"password must be at least {_MIN_PASSWORD_LEN} characters",
        )


def _bearer_token(request: Request) -> str | None:
    authz = request.headers.get("Authorization") or ""
    return authz[7:].strip() if authz.lower().startswith("bearer ") else None


@app.post("/auth/signup", response_model=SignupResponse, status_code=201)
async def signup(body: SignupRequest) -> SignupResponse:
    """Create a new organization + its owner login, mint an initial org API
    key (for agents), and start a session."""
    if body.account_type not in ("individual", "business"):
        raise HTTPException(status_code=400, detail="invalid account_type")
    _validate_password(body.password)
    try:
        account = database.create_account(
            body.email, account_type=body.account_type, name=body.org_name
        )
    except database.EmailAlreadyExistsError:
        raise HTTPException(status_code=409, detail="email already registered")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        user = database.create_user(
            account_id=account["id"],
            email=body.email,
            name=body.name,
            role="owner",
            password_hash=database.hash_password(body.password),
        )
    except database.UserEmailExistsError:
        raise HTTPException(status_code=409, detail="email already registered")

    api_key = database.generate_api_key(account["id"])
    token = database.create_session(user["id"], account["id"])
    return SignupResponse(
        token=token,
        user=UserPublic(**user),
        org=OrgPublic(**account),
        api_key=api_key,
        message="Welcome to Trovis. Use the API key to connect agents.",
    )


@app.post("/auth/login", response_model=LoginResponse)
async def login(body: LoginRequest) -> LoginResponse:
    """Email + password → session token. Returns a single generic error for
    unknown email / no-password / wrong password to avoid enumeration."""
    user = database.get_user_by_email(body.email)
    if user is None or not database.verify_password(
        body.password, user.get("password_hash")
    ):
        raise HTTPException(status_code=401, detail=_GENERIC_LOGIN_ERROR)

    # Transparent hash upgrade if the work factor was bumped.
    if database.needs_rehash(user.get("password_hash")):
        database.set_user_password(user["id"], database.hash_password(body.password))
    database.touch_user_login(user["id"])
    org = database.get_account(user["account_id"])
    token = database.create_session(user["id"], user["account_id"])
    public = {k: v for k, v in user.items() if k != "password_hash"}
    return LoginResponse(token=token, user=UserPublic(**public), org=OrgPublic(**org))


@app.post("/auth/logout", status_code=204)
async def logout(request: Request) -> None:
    """Invalidate the caller's session token."""
    database.delete_session(_bearer_token(request))


@app.get("/auth/me", response_model=MeResponse)
async def me(request: Request) -> MeResponse:
    """Current identity. `user` is None for API-key (agent/legacy) auth."""
    account_id = getattr(request.state, "account_id", None)
    auth = getattr(request.state, "auth", None) or "session"
    state_user = getattr(request.state, "user", None)
    org = database.get_account(account_id) if account_id is not None else None
    user = None
    if state_user:
        full = database.get_user_by_id(state_user["id"])
        if full:
            user = UserPublic(**full)
    return MeResponse(
        user=user,
        org=OrgPublic(**org) if org else None,
        auth=auth,
    )


@app.post("/auth/claim", response_model=LoginResponse, status_code=201)
async def claim_account(body: ClaimRequest) -> LoginResponse:
    """One-time migration for a legacy passwordless account: prove ownership
    with a valid org API key, then create the owner login. Only works while
    the org has zero users (otherwise it would let any key-holder mint a new
    owner — takeover)."""
    _validate_password(body.password)
    result = database.validate_api_key(body.api_key)
    if not result:
        raise HTTPException(status_code=401, detail="invalid API key")
    account_id = result["account_id"]
    if database.get_org_users(account_id):
        raise HTTPException(
            status_code=409,
            detail="this account has already been claimed — sign in instead",
        )
    try:
        user = database.create_user(
            account_id=account_id,
            email=body.email,
            name=body.name,
            role="owner",
            password_hash=database.hash_password(body.password),
        )
    except database.UserEmailExistsError:
        raise HTTPException(status_code=409, detail="email already registered")
    org = database.get_account(account_id)
    token = database.create_session(user["id"], account_id)
    return LoginResponse(token=token, user=UserPublic(**user), org=OrgPublic(**org))


@app.post("/auth/set-password", status_code=204)
async def set_password(request: Request, body: SetPasswordRequest) -> None:
    """Set/change the logged-in user's password. Requires the current
    password when one is already set. Rotates the user's other sessions."""
    state_user = getattr(request.state, "user", None)
    if not state_user:
        raise HTTPException(status_code=401, detail="sign in to set a password")
    _validate_password(body.new_password)
    full = database.get_user_by_email(state_user["email"])
    if full and full.get("password_hash"):
        if not database.verify_password(
            body.current_password or "", full["password_hash"]
        ):
            raise HTTPException(status_code=403, detail="current password is incorrect")
    database.set_user_password(state_user["id"], database.hash_password(body.new_password))
    # Invalidate other sessions; keep the caller's current one.
    database.delete_sessions_for_user(state_user["id"], except_raw_token=_bearer_token(request))


@app.post("/auth/accept-invite", response_model=LoginResponse, status_code=201)
async def accept_invite(body: AcceptInviteRequest) -> LoginResponse:
    """Redeem a one-time invite link → create the member login + a session."""
    _validate_password(body.password)
    try:
        user = database.accept_invite(
            body.token, body.name, database.hash_password(body.password)
        )
    except LookupError:
        raise HTTPException(status_code=400, detail="invite is invalid, expired, or already used")
    except database.UserEmailExistsError:
        raise HTTPException(status_code=409, detail="email already registered — sign in instead")
    org = database.get_account(user["account_id"])
    token = database.create_session(user["id"], user["account_id"])
    return LoginResponse(token=token, user=UserPublic(**user), org=OrgPublic(**org))


# ---------------------------------------------------------------------------
# Organization endpoints (members + invites)
# ---------------------------------------------------------------------------


@app.get("/org", response_model=OrgPublic)
async def get_org(request: Request) -> OrgPublic:
    account_id = getattr(request.state, "account_id", None)
    org = database.get_account(account_id) if account_id is not None else None
    if org is None:
        raise HTTPException(status_code=404, detail="organization not found")
    return OrgPublic(**org)


@app.get("/account/usage", response_model=AccountUsage)
async def account_usage(request: Request) -> AccountUsage:
    """Plan + agent count + limit + locked count — for the Fleet header and
    upgrade prompts. Agent count is distinct (service_name, agent_id); limit is
    None for unlimited plans."""
    account_id = getattr(request.state, "account_id", None)
    lock = database.get_locked_state(account_id)
    return AccountUsage(
        plan=lock["plan"],
        agent_count=lock["agent_count"],
        agent_limit=lock["limit"],
        locked_count=lock["locked_count"],
    )


@app.put("/org", response_model=OrgPublic)
async def update_org(request: Request, body: OrgProfileUpdate) -> OrgPublic:
    """Owner-only: set the workspace (org) display name. Used by onboarding."""
    account_id = _require_owner(request)
    database.update_account_profile(account_id, body.name)
    org = database.get_account(account_id)
    if org is None:
        raise HTTPException(status_code=404, detail="organization not found")
    return OrgPublic(**org)


@app.post("/auth/onboarding/complete", status_code=204)
async def complete_onboarding(request: Request) -> None:
    """Mark the account's onboarding wizard as done (idempotent). The owner
    calls this when they finish or skip the post-signup wizard."""
    account_id = getattr(request.state, "account_id", None)
    if account_id is None:
        raise HTTPException(status_code=401, detail="authentication required")
    database.mark_account_onboarded(account_id)


@app.get("/org/members", response_model=list[UserPublic])
async def list_members(request: Request) -> list[UserPublic]:
    """All users in the caller's org (owner + members can view)."""
    account_id = getattr(request.state, "account_id", None)
    if account_id is None:
        raise HTTPException(status_code=401, detail="authentication required")
    return [UserPublic(**u) for u in database.get_org_users(account_id)]


@app.post("/org/invites", response_model=InviteCreateResponse, status_code=201)
async def create_invite(request: Request, body: InviteCreate) -> InviteCreateResponse:
    """Owner-only: mint a one-time invite link for a Business org."""
    account_id = _require_owner(request)
    org = database.get_account(account_id)
    if not org or org.get("account_type") != "business":
        raise HTTPException(
            status_code=400, detail="only Business organizations can add members"
        )
    role = body.role if body.role in ("owner", "member") else "member"
    inviter = getattr(request.state, "user", None)
    inv = database.create_invite(
        account_id, body.email, role, inviter["id"] if inviter else None
    )
    base = (database.env("APP_URL") or str(request.base_url)).rstrip("/")
    return InviteCreateResponse(
        invite_url=f"{base}/accept-invite?token={inv['token']}",
        email=inv["email"],
        role=inv["role"],
        expires_at=inv["expires_at"],
    )


@app.get("/org/invites", response_model=list[InvitePublic])
async def list_org_invites(request: Request) -> list[InvitePublic]:
    """Owner-only: pending invites for the org."""
    account_id = _require_owner(request)
    return [InvitePublic(**i) for i in database.list_invites(account_id)]


@app.delete("/org/invites/{invite_id}", status_code=204)
async def revoke_org_invite(invite_id: int, request: Request) -> None:
    account_id = _require_owner(request)
    if not database.revoke_invite(account_id, invite_id):
        raise HTTPException(status_code=404, detail="invite not found")


@app.delete("/org/members/{user_id}", status_code=204)
async def remove_member(user_id: int, request: Request) -> None:
    """Owner-only: remove a member from the org. Refuses to remove the last
    owner. Account-scoped so an owner can't touch another org's users."""
    account_id = _require_owner(request)
    target = database.get_user_by_id(user_id)
    if target is None or target["account_id"] != account_id:
        raise HTTPException(status_code=404, detail="member not found")
    if target["role"] == "owner" and database.count_owners(account_id) <= 1:
        raise HTTPException(status_code=409, detail="cannot remove the last owner")
    database.delete_user(account_id, user_id)


@app.get("/org/api-keys", response_model=RevealKeysResponse)
async def get_api_keys(request: Request) -> RevealKeysResponse:
    """Return the org's API key(s) for the currently authenticated user. No
    password required — the user is already logged in. Used by the AddAgent
    setup page so the key can be copied into ChatGPT / SDK snippets."""
    account_id = getattr(request.state, "account_id", None)
    if account_id is None:
        raise HTTPException(status_code=401, detail="authentication required")
    keys = [
        ApiKeyInfo(key=k["key"], name=k["name"], created_at=k.get("created_at"))
        for k in database.get_api_keys_for_account(account_id)
        if k["active"]
    ]
    return RevealKeysResponse(keys=keys)


@app.post("/org/api-keys/reveal", response_model=RevealKeysResponse)
async def reveal_api_keys(
    request: Request, body: RevealKeysRequest
) -> RevealKeysResponse:
    """Re-show the org's API key(s) — owner only, gated by re-entering the
    caller's password (step-up auth). The key is the org's long-lived agent
    credential, so we don't expose it from a passive session alone."""
    account_id = _require_owner(request)
    state_user = getattr(request.state, "user", None)
    full = database.get_user_by_email(state_user["email"]) if state_user else None
    if not full or not database.verify_password(body.password, full.get("password_hash")):
        raise HTTPException(status_code=403, detail="incorrect password")
    keys = [
        ApiKeyInfo(key=k["key"], name=k["name"], created_at=k.get("created_at"))
        for k in database.get_api_keys_for_account(account_id)
        if k["active"]
    ]
    return RevealKeysResponse(keys=keys)


# ---------------------------------------------------------------------------
# Agent-to-agent connections (auto-detected + operator-curated)
# ---------------------------------------------------------------------------


@app.get("/connections", response_model=list[Connection])
async def list_connections(request: Request) -> list[Connection]:
    """All agent→agent connection edges for the account."""
    account_id = getattr(request.state, "account_id", None)
    return [Connection(**c) for c in database.get_connections(account_id)]


@app.post("/connections/detect", response_model=list[Connection])
async def detect_connections(request: Request) -> list[Connection]:
    """Re-scan recent shared traces for agent→agent edges, then return the
    refreshed list. Detection preserves operator confirm/dismiss/manual."""
    account_id = getattr(request.state, "account_id", None)
    database.detect_agent_connections(account_id)
    return [Connection(**c) for c in database.get_connections(account_id)]


@app.post("/connections/from-description", response_model=list[Connection])
async def connections_from_description(
    request: Request, body: ConnectionsFromDescription
) -> list[Connection]:
    """AI builder: propose agent→agent connections from a description, create
    them (manual), and return the full edge list."""
    account_id = getattr(request.state, "account_id", None)
    known = [g["service_name"] for g in database.get_agents(account_id=account_id)]
    try:
        pairs = describer.connections_from_description(body.description, known)
    except describer.APIKeyMissingError as e:
        raise HTTPException(status_code=503, detail=str(e))
    for p in pairs:
        database.add_manual_connection(
            account_id, p["source"], "main", p["target"], "main"
        )
    return [Connection(**c) for c in database.get_connections(account_id)]


@app.post("/connections", response_model=Connection, status_code=201)
async def add_connection(request: Request, body: ConnectionCreate) -> Connection:
    """Operator-drawn (manual) connection."""
    account_id = getattr(request.state, "account_id", None)
    c = database.add_manual_connection(
        account_id,
        body.source_service,
        body.source_agent_id or "main",
        body.target_service,
        body.target_agent_id or "main",
    )
    return Connection(**c)


@app.patch("/connections/{conn_id}", response_model=Connection)
async def update_connection(
    conn_id: int, request: Request, body: ConnectionStatusUpdate
) -> Connection:
    """Confirm / dismiss / re-detect an edge."""
    account_id = getattr(request.state, "account_id", None)
    try:
        c = database.set_connection_status(account_id, conn_id, body.status)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if c is None:
        raise HTTPException(status_code=404, detail="connection not found")
    return Connection(**c)


@app.delete("/connections/{conn_id}", status_code=204)
async def remove_connection(conn_id: int, request: Request) -> None:
    account_id = getattr(request.state, "account_id", None)
    if not database.delete_connection(account_id, conn_id):
        raise HTTPException(status_code=404, detail="connection not found")


# ---------------------------------------------------------------------------
# Dashboard — daily briefing, needs-attention, cost intelligence, work feed
#
# All read endpoints are account-scoped via request.state.account_id. The
# cheap parts (counts, costs) recompute every call; the Claude-written parts
# (briefing summary, attention enrichment, work-feed summaries) cache for an
# hour via the agent_insights table. Every Claude path degrades gracefully —
# a missing/erroring key never 500s the dashboard.
# ---------------------------------------------------------------------------


def _monthly_budget(account_id: int | None = None) -> float:
    """The org's monthly cost budget: the per-account value when set, else the
    OVERSEE_MONTHLY_BUDGET env default."""
    if account_id is not None:
        saved = database.get_account_budget(account_id)
        if saved is not None:
            return saved
    try:
        return float(database.env("MONTHLY_BUDGET", "500") or 500)
    except (TypeError, ValueError):
        return 500.0


def _last_seen_age_days(last_seen: str | None) -> float | None:
    """Age in days of an ISO last_seen timestamp; None when missing/unparseable."""
    if not last_seen:
        return None
    from datetime import datetime, timezone

    try:
        dt = datetime.fromisoformat(str(last_seen).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0
    except (ValueError, AttributeError):
        return None


def _agent_error_rate(a: dict) -> float:
    spans = a.get("total_spans") or 0
    errs = a.get("total_errors") or 0
    return (errs / spans * 100.0) if spans else 0.0


def _agent_status(a: dict) -> str:
    """healthy | degraded | offline — must match the frontend deriveStatus():
    offline when no telemetry in 24h, degraded when error rate > 2%."""
    age = _last_seen_age_days(a.get("last_seen"))
    if age is None or age > 1.0:
        return "offline"
    if _agent_error_rate(a) > 2.0:
        return "degraded"
    return "healthy"


def _briefing_stats(
    agents: list[dict], tasks_yesterday: int, tasks_last_week: int, tasks_delta: str
) -> dict:
    healthy = degraded = offline = 0
    top_errors: list[dict] = []
    cost_today = 0.0
    for a in agents:
        st = _agent_status(a)
        healthy += st == "healthy"
        degraded += st == "degraded"
        offline += st == "offline"
        rate = _agent_error_rate(a)
        if rate > 2.0 and (a.get("total_spans") or 0) > 0:
            top_errors.append(
                {"agent": a["service_name"], "error_rate_pct": round(rate, 1)}
            )
        cost_today += a.get("cost_today") or 0.0
    top_errors.sort(key=lambda x: x["error_rate_pct"], reverse=True)
    return {
        "agent_count": len(agents),
        "healthy": healthy,
        "degraded": degraded,
        "offline": offline,
        "top_error_agents": top_errors[:5],
        "cost_today_usd": round(cost_today, 4),
        "tasks_last_24h": tasks_yesterday,
        "tasks_last_7d": tasks_last_week,
        "tasks_wow_delta": tasks_delta,
    }


def _briefing_fallback(stats: dict) -> str:
    """Non-AI briefing line used when Claude is unavailable."""
    n = stats["agent_count"]
    if n == 0:
        return (
            "No agents are reporting telemetry yet. Connect an agent to start "
            "seeing activity here."
        )
    bits = [f"{n} agent{'s' if n != 1 else ''} reporting"]
    bits.append(f"{stats['tasks_last_24h']} tasks in the last 24 hours")
    if stats["degraded"] or stats["offline"]:
        bits.append(f"{stats['degraded']} degraded and {stats['offline']} offline")
    return ", ".join(bits) + "."


def _flag_attention(agents: list[dict]) -> list[dict]:
    """Derive needs-attention flags: error rate > 10% critical, > 2% warning,
    offline > 30 days info. Sorted critical → warning → info, then by error rate."""
    flagged: list[dict] = []
    for a in agents:
        spans = a.get("total_spans") or 0
        rate = _agent_error_rate(a)
        age = _last_seen_age_days(a.get("last_seen"))
        if spans > 0 and rate > 10.0:
            severity = "critical"
        elif spans > 0 and rate > 2.0:
            severity = "warning"
        elif age is not None and age > 30.0:
            severity = "info"
        else:
            continue
        flagged.append(
            {
                "agent": a["service_name"],
                "severity": severity,
                "error_rate_pct": round(rate, 1),
                "span_count": spans,
                "error_count": a.get("total_errors") or 0,
                "days_since_seen": round(age, 1) if age is not None else None,
                "description": a.get("description"),
                "last_seen": a.get("last_seen"),
            }
        )
    rank = {"critical": 0, "warning": 1, "info": 2}
    flagged.sort(key=lambda f: (rank.get(f["severity"], 9), -f["error_rate_pct"]))
    return flagged


def _attention_fallback(flagged: list[dict]) -> list[dict]:
    """Non-AI attention items used when Claude is unavailable."""
    out: list[dict] = []
    for f in flagged:
        if f["severity"] == "info":
            title = "Agent has gone quiet"
            detail = (
                f"No telemetry from {f['agent']} in {f['days_since_seen']} days."
            )
            rec = "Confirm whether this agent is still running or should be retired."
        else:
            title = "Elevated error rate"
            detail = (
                f"{f['agent']} is failing {f['error_rate_pct']}% of "
                f"{f['span_count']} tasks."
            )
            rec = "Inspect the recent error spans to find the failing operation."
        out.append(
            {
                "severity": f["severity"],
                "agent": f["agent"],
                "title": title,
                "detail": detail,
                "recommendation": rec,
                "impact": "",
                "last_seen": f.get("last_seen"),
            }
        )
    return out


def _work_activity(spans: list[dict]) -> dict:
    """Compact evidence (top operations + a few captured content samples) for
    the work-feed summary prompt."""
    ops: dict[str, int] = {}
    samples: list[str] = []
    for s in spans:
        name = s.get("span_name") or ""
        ops[name] = ops.get(name, 0) + 1
        attrs = s.get("attributes") or {}
        for suffix in ("tool.result", "response.content", "message.content"):
            v = database.attr(attrs, suffix)
            if v:
                samples.append(str(v)[:200])
                break
    top_ops = sorted(ops.items(), key=lambda kv: kv[1], reverse=True)[:6]
    return {
        "task_count": len(spans),
        "top_operations": [{"operation": k, "count": v} for k, v in top_ops],
        "samples": samples[:8],
    }


@app.get("/dashboard/briefing", response_model=BriefingResponse)
async def dashboard_briefing(request: Request) -> BriefingResponse:
    """AI daily briefing + task counts. Counts are always fresh; the Claude
    summary is cached for an hour and falls back to a plain line on failure."""
    account_id = getattr(request.state, "account_id", None)
    from time import time as _time

    now_ns = int(_time() * 1_000_000_000)
    # activity_only → exclude agent_registration spans, so "tasks" reflect real
    # work (and a just-connected agent reads as 0 → dashboard shows its
    # "waiting for telemetry" state instead of a fake task count).
    tasks_yesterday = database.count_fleet_spans(
        account_id, now_ns - _NS_PER_DAY, activity_only=True
    )
    tasks_last_week = database.count_fleet_spans(
        account_id, now_ns - 7 * _NS_PER_DAY, activity_only=True
    )
    tasks_prev_week = database.count_fleet_spans(
        account_id, now_ns - 14 * _NS_PER_DAY, now_ns - 7 * _NS_PER_DAY, activity_only=True
    )
    delta = _pct_delta(tasks_last_week, tasks_prev_week)
    tasks_delta = (
        "—" if delta is None else f"{'+' if delta >= 0 else ''}{delta:.0f}%"
    )

    cached = database.get_insight(
        account_id=account_id,
        service_name=_DASHBOARD_SENTINEL,
        agent_id="main",
        kind="briefing",
        max_age_seconds=_DASHBOARD_TTL_SECONDS,
    )
    if cached and cached["data"].get("summary"):
        return BriefingResponse(
            summary=cached["data"]["summary"],
            tasks_yesterday=tasks_yesterday,
            tasks_last_week=tasks_last_week,
            tasks_delta=tasks_delta,
            generated_at=cached["generated_at"],
        )

    agents = database.get_agents(account_id=account_id)
    stats = _briefing_stats(agents, tasks_yesterday, tasks_last_week, tasks_delta)
    summary = ""
    try:
        summary = describer.fleet_briefing(stats).get("summary", "")
    except describer.APIKeyMissingError:
        summary = ""
    except Exception as e:  # noqa: BLE001
        print(f"[Oversee] /dashboard/briefing claude failed: {type(e).__name__}: {e}")
        summary = ""
    if summary:
        database.save_insight(
            account_id=account_id,
            service_name=_DASHBOARD_SENTINEL,
            agent_id="main",
            kind="briefing",
            data={"summary": summary},
        )
    else:
        summary = _briefing_fallback(stats)
    return BriefingResponse(
        summary=summary,
        tasks_yesterday=tasks_yesterday,
        tasks_last_week=tasks_last_week,
        tasks_delta=tasks_delta,
    )


@app.get("/dashboard/attention", response_model=list[AttentionItem])
async def dashboard_attention(request: Request) -> list[AttentionItem]:
    """Needs-attention rows, derived from fleet health and enriched by Claude
    (cached an hour, keyed on the current flag set). Empty list when all clear."""
    account_id = getattr(request.state, "account_id", None)
    agents = database.get_agents(account_id=account_id)
    flagged = _flag_attention(agents)
    if not flagged:
        return []

    import hashlib
    import json as _json

    fingerprint = hashlib.sha256(
        _json.dumps([(f["agent"], f["severity"]) for f in flagged]).encode()
    ).hexdigest()
    cached = database.get_insight(
        account_id=account_id,
        service_name=_DASHBOARD_SENTINEL,
        agent_id="main",
        kind="attention",
        max_age_seconds=_DASHBOARD_TTL_SECONDS,
    )
    if (
        cached
        and cached["data"].get("fingerprint") == fingerprint
        and cached["data"].get("items")
    ):
        return [AttentionItem(**it) for it in cached["data"]["items"]]

    try:
        items = describer.attention_items(flagged)
        if items:
            database.save_insight(
                account_id=account_id,
                service_name=_DASHBOARD_SENTINEL,
                agent_id="main",
                kind="attention",
                data={"fingerprint": fingerprint, "items": items},
            )
    except describer.APIKeyMissingError:
        items = _attention_fallback(flagged)
    except Exception as e:  # noqa: BLE001
        print(f"[Oversee] /dashboard/attention claude failed: {type(e).__name__}: {e}")
        items = _attention_fallback(flagged)
    return [AttentionItem(**it) for it in items]


def _agent_cost_trend(a: dict) -> str:
    """today (UTC calendar day) vs the trailing 7-day daily average. Note:
    early in the UTC day the partial-day total reads low against a full-day
    average, so the arrow leans down in the morning and fills in through the
    day — acceptable for a soft indicator."""
    c_today = a.get("cost_today") or 0.0
    avg = (a.get("cost_7d") or 0.0) / 7.0
    if c_today > avg * 1.1:
        return "up"
    if c_today < avg * 0.9:
        return "down"
    return "flat"


def _daily_series(daily_rows: list[dict]) -> tuple[list[float], float, str]:
    """30-element daily cost series (oldest→newest) + month-to-date + month prefix."""
    from datetime import datetime, timezone, timedelta

    today = datetime.now(timezone.utc).date()
    by_date = {r["date"]: r["cost"] for r in daily_rows}
    daily = [
        round(by_date.get((today - timedelta(days=i)).strftime("%Y-%m-%d"), 0.0), 6)
        for i in range(29, -1, -1)
    ]
    month_prefix = today.strftime("%Y-%m-")
    mtd = round(sum(r["cost"] for r in daily_rows if r["date"].startswith(month_prefix)), 6)
    return daily, mtd, month_prefix


@app.get("/dashboard/cost", response_model=CostResponse)
async def dashboard_cost(request: Request) -> CostResponse:
    """Cost Intelligence card. `today` is the UTC-calendar-day fleet spend (sum
    of each agent's cost_today) — identical to the Fleet page's "cost today" and
    to the last point of the 30-day sparkline. Month-to-date vs. the org
    budget; 30-day sparkline. Pure DB, no Claude."""
    account_id = getattr(request.state, "account_id", None)
    agents = database.get_agents(account_id=account_id)
    daily, month_total, _ = _daily_series(
        database.get_fleet_daily_cost(account_id, days=30)
    )
    # Match the Fleet summary exactly: sum of rolling-24h per-agent cost_today.
    today_cost = round(sum(a.get("cost_today") or 0.0 for a in agents), 6)
    budget = _monthly_budget(account_id)
    budget_pct = round(month_total / budget * 100.0, 1) if budget > 0 else 0.0

    cost_agents = [
        CostAgent(
            name=a.get("display_name") or a["service_name"],
            cost=round(a.get("cost_today") or 0.0, 6),
            trend=_agent_cost_trend(a),
        )
        for a in agents
    ]
    cost_agents.sort(key=lambda x: x.cost, reverse=True)

    return CostResponse(
        today=today_cost,
        month_total=month_total,
        month_budget=budget,
        budget_pct=budget_pct,
        agents=cost_agents[:8],
        daily=daily,
    )


@app.get("/cost/overview", response_model=CostOverview)
async def cost_overview(request: Request) -> CostOverview:
    """The dedicated cost page: today (UTC calendar day), month-to-date vs. the org
    budget, a 30-day trend, per-agent breakdown (today/7d/all-time/MTD + the
    editable monthly cap + over-cap flag), and an org-wide by-model breakdown."""
    account_id = getattr(request.state, "account_id", None)
    agents = database.get_agents(account_id=account_id)
    daily, month_total, _ = _daily_series(
        database.get_fleet_daily_cost(account_id, days=30)
    )
    breakdown = database.get_cost_breakdown(account_id)
    mtd_by_service = breakdown.get("by_service_mtd", {})
    caps = {
        (b["service_name"], b["agent_id"]): b["monthly_cap_usd"]
        for b in database.get_agent_budgets(account_id)
    }

    today_cost = round(sum(a.get("cost_today") or 0.0 for a in agents), 6)
    budget = _monthly_budget(account_id)
    budget_pct = round(month_total / budget * 100.0, 1) if budget > 0 else 0.0

    rows: list[CostAgentRow] = []
    for a in agents:
        svc = a["service_name"]
        cap = caps.get((svc, "main"))
        mtd = round(mtd_by_service.get(svc, 0.0), 6)
        rows.append(
            CostAgentRow(
                service_name=svc,
                agent_id="main",
                name=a.get("display_name") or svc,
                status=_agent_status(a),
                today=round(a.get("cost_today") or 0.0, 6),
                cost_7d=round(a.get("cost_7d") or 0.0, 6),
                total=round(a.get("estimated_cost_usd") or 0.0, 6),
                mtd=mtd,
                monthly_cap=cap,
                over_cap=bool(cap is not None and mtd > cap),
                trend=_agent_cost_trend(a),
            )
        )
    rows.sort(key=lambda r: r.mtd, reverse=True)

    by_model = [CostModelRow(**m) for m in breakdown.get("by_model", [])]
    return CostOverview(
        today=today_cost,
        month_total=month_total,
        month_budget=budget,
        budget_pct=budget_pct,
        over_budget=bool(budget > 0 and month_total > budget),
        daily=daily,
        agents=rows,
        by_model=by_model,
    )


@app.get("/cost/audit")
async def cost_audit(
    request: Request,
    service: str | None = Query(default=None),
    days: int = Query(default=30, ge=1, le=365),
) -> dict[str, Any]:
    """Per-day, per-model cost audit for debugging a wrong total. Shows, per UTC
    day, how many spans carried usage tokens vs how many got a price, the total
    tokens/cost, and the tokens that landed UNPRICED (cost NULL) with the models
    responsible — separating a price-table gap from an SDK usage-capture gap.
    Account-scoped; pass `?service=` to focus one agent."""
    account_id = getattr(request.state, "account_id", None)
    return database.get_cost_audit(
        account_id=account_id, service_name=service, days=days
    )


@app.put("/cost/budget", response_model=CostOverview)
async def set_cost_budget(request: Request, body: BudgetUpdate) -> CostOverview:
    """Set (or clear) the org's monthly budget, then return the fresh overview."""
    account_id = getattr(request.state, "account_id", None)
    if account_id is None:
        raise HTTPException(status_code=401, detail="no account to set a budget on")
    database.set_account_budget(account_id, body.monthly_budget)
    return await cost_overview(request)


@app.put("/cost/agent-budget", response_model=CostOverview)
async def set_cost_agent_budget(request: Request, body: AgentBudgetUpdate) -> CostOverview:
    """Set (or clear) a per-agent monthly cap, then return the fresh overview."""
    account_id = getattr(request.state, "account_id", None)
    if account_id is None:
        raise HTTPException(status_code=401, detail="no account to set a cap on")
    database.set_agent_budget(
        account_id, body.service_name, body.agent_id or "main", body.monthly_cap
    )
    return await cost_overview(request)


@app.get("/dashboard/work-feed", response_model=list[WorkFeedItem])
async def dashboard_work_feed(request: Request) -> list[WorkFeedItem]:
    """Plain-English feed of what each active agent recently did (last 24h).
    One Claude summary per agent, cached an hour, with a non-AI fallback."""
    account_id = getattr(request.state, "account_id", None)
    from time import time as _time

    now_ns = int(_time() * 1_000_000_000)
    cutoff = now_ns - _NS_PER_DAY

    agents = database.get_agents(account_id=account_id)
    active = [
        a
        for a in agents
        if (_last_seen_age_days(a.get("last_seen")) or 999) <= 1.0
    ]
    active.sort(key=lambda a: a.get("last_seen") or "", reverse=True)

    feed: list[WorkFeedItem] = []
    for a in active[:8]:  # cap Claude calls
        svc = a["service_name"]
        label = a.get("display_name") or svc
        spans = database.get_agent_spans(svc, limit=40, account_id=account_id)
        recent = [s for s in spans if (s.get("start_time_unix") or 0) >= cutoff]
        if not recent:
            continue
        task_count = len(recent)

        cached = database.get_insight(
            account_id=account_id,
            service_name=svc,
            agent_id="main",
            kind="work_feed",
            max_age_seconds=_DASHBOARD_TTL_SECONDS,
        )
        summary = cached["data"].get("summary", "") if cached else ""
        if not summary:
            try:
                summary = describer.work_feed_summary(label, _work_activity(recent))
                if summary:
                    database.save_insight(
                        account_id=account_id,
                        service_name=svc,
                        agent_id="main",
                        kind="work_feed",
                        data={"summary": summary},
                    )
            except describer.APIKeyMissingError:
                summary = ""
            except Exception as e:  # noqa: BLE001
                print(
                    f"[Oversee] /dashboard/work-feed claude failed for "
                    f"'{svc}': {type(e).__name__}: {e}"
                )
                summary = ""
        if not summary:
            ops = a.get("top_operations") or []
            summary = f"Ran {task_count} tasks" + (
                f" — mostly {', '.join(ops[:3])}." if ops else "."
            )
        feed.append(
            WorkFeedItem(
                time=a.get("last_seen"),
                agent=label,
                summary=summary,
                tasks=task_count,
            )
        )
    return feed


@app.get("/dashboard/activity", response_model=list[ActivityItem])
async def dashboard_activity(
    request: Request,
    hours: int = 24,
    limit: int = 200,
) -> list[ActivityItem]:
    """Chronological, fleet-wide Work Feed — the actual work events across all
    agents in the last `hours`, newest first (the detail behind the dashboard's
    per-agent rollup). Pure DB, no Claude, so it always renders fast."""
    account_id = getattr(request.state, "account_id", None)
    hours = max(1, min(24 * 30, int(hours)))
    limit = max(1, min(500, int(limit)))
    from time import time as _time

    now_ns = int(_time() * 1_000_000_000)
    since_ns = now_ns - hours * (_NS_PER_DAY // 24)  # _NS_PER_DAY // 24 = ns/hour
    rows = database.get_fleet_activity(account_id, since_ns, limit=limit)
    return [ActivityItem(**r) for r in rows]


@app.post("/dashboard/ask", response_model=AskResponse)
async def dashboard_ask(request: Request, body: AskRequest) -> AskResponse:
    """The floating Ask pill — concise, plain-prose fleet Q&A. Reuses the
    fleet context builder with a tighter system prompt."""
    account_id = getattr(request.state, "account_id", None)
    msgs = [m.model_dump() for m in body.messages]
    try:
        result = asker.ask_about_fleet(account_id, msgs, concise=True)
    except asker.AskApiKeyMissingError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return AskResponse(answer=result["answer"], visual=result.get("visual"))


@app.post("/connect/ask", response_model=ConnectAskResponse)
async def connect_ask(request: Request, body: AskRequest) -> ConnectAskResponse:
    """The guided add-agent chat ("Set up with AI"). Stateless — the client
    posts the full thread each turn. Replies carry optional quick-reply
    chips (`options`) and copy-paste snippets (`code`)."""
    account_id = getattr(request.state, "account_id", None)
    msgs = [m.model_dump() for m in body.messages]
    try:
        result = asker.ask_connect(account_id, msgs)
    except asker.AskApiKeyMissingError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return ConnectAskResponse(**result)


@app.post("/ask", response_model=AskResponse)
async def ask_fleet(request: Request, body: AskRequest) -> AskResponse:
    """Answer a question about the user's whole fleet."""
    account_id = getattr(request.state, "account_id", None)
    msgs = [m.model_dump() for m in body.messages]
    try:
        result = asker.ask_about_fleet(account_id, msgs)
    except asker.AskApiKeyMissingError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return AskResponse(answer=result["answer"], visual=result.get("visual"))


@app.post("/agents/{service_name}/ask", response_model=AskResponse)
async def ask_agent(
    service_name: str,
    request: Request,
    body: AskRequest,
    agent_id: str | None = Query(default=None),
) -> AskResponse:
    """Answer a question scoped to one instance, or one sub-agent when
    `?agent_id=` is set."""
    account_id = getattr(request.state, "account_id", None)
    msgs = [m.model_dump() for m in body.messages]
    try:
        answer = asker.ask_about_agent(
            service_name, account_id, msgs, agent_id=agent_id
        )
    except asker.AgentNotFoundError:
        raise HTTPException(
            status_code=404, detail=f"agent '{service_name}' not found"
        )
    except asker.AskApiKeyMissingError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return AskResponse(answer=answer)


# ---------------------------------------------------------------------------
# OAuth 2.0 + GPT Actions (ChatGPT agent integration)
#
# Flow: ChatGPT redirects user → GET /oauth/authorize (consent page) →
# user logs in / approves → redirect back to ChatGPT with ?code= → ChatGPT
# POSTs /oauth/token to exchange code for access_token → ChatGPT calls
# /actions/* with Bearer access_token.
# ---------------------------------------------------------------------------

_OVERSEE_APP_URL = database.env("APP_URL", "https://oversee-pi.vercel.app")
_OVERSEE_API_URL = database.env("API_URL", "https://web-production-e6bc4.up.railway.app")
# OAuth client_id/secret — set in env for production; defaults for dev. Default
# literals stay `oversee-*` (registered values, not brand identifiers).
_OAUTH_CLIENT_ID = database.env("OAUTH_CLIENT_ID", "oversee-chatgpt")
_OAUTH_CLIENT_SECRET = database.env("OAUTH_CLIENT_SECRET", "oversee-dev-secret")


from starlette.responses import HTMLResponse, RedirectResponse


@app.get("/oauth/authorize", include_in_schema=False)
async def oauth_authorize(
    client_id: str = Query(default=""),
    redirect_uri: str = Query(default=""),
    response_type: str = Query(default="code"),
    scope: str = Query(default=""),
    state: str = Query(default=""),
):
    """OAuth consent page. Renders a simple branded login+approve form.
    On submit, validates credentials, creates an auth code, and redirects
    back to ChatGPT's callback with ?code=&state=."""
    # Render a self-contained consent page (no React needed — it's a redirect flow)
    html = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Trovis — Authorize</title>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #f8f9fb; display:flex; justify-content:center; align-items:center;
         min-height:100vh; }}
  .card {{ background:#fff; border-radius:16px; border:1px solid #eaedf0;
           padding:32px; max-width:400px; width:100%; }}
  .logo {{ display:flex; align-items:center; gap:8px; margin-bottom:20px; }}
  .dot {{ width:8px; height:8px; border-radius:50%; background:#10b981; }}
  .brand {{ font-size:16px; font-weight:800; color:#0f172a; letter-spacing:-0.04em; }}
  h2 {{ font-size:18px; font-weight:700; color:#0f172a; margin-bottom:8px; }}
  p {{ font-size:13px; color:#64748b; margin-bottom:20px; line-height:1.5; }}
  .perms {{ background:#f8f9fb; border-radius:8px; padding:12px; margin-bottom:20px;
            font-size:12px; color:#374151; }}
  .perms li {{ margin:4px 0; }}
  label {{ display:block; font-size:13px; font-weight:600; color:#374151; margin-bottom:6px; }}
  input {{ width:100%; padding:10px 12px; border-radius:8px; border:1.5px solid #e2e5e9;
          font-size:14px; margin-bottom:12px; }}
  input:focus {{ outline:none; border-color:#0f172a; }}
  .btn {{ width:100%; padding:11px; border-radius:8px; border:none; background:#0f172a;
          color:#fff; font-size:14px; font-weight:600; cursor:pointer; margin-bottom:8px; }}
  .btn:hover {{ background:#1e293b; }}
  .btn-cancel {{ background:#fff; color:#64748b; border:1.5px solid #e2e5e9; }}
  .err {{ color:#dc2626; font-size:12px; margin-bottom:12px; display:none; }}
</style>
</head><body>
<div class="card">
  <div class="logo"><span class="dot"></span><span class="brand">trovis</span></div>
  <h2>Authorize ChatGPT</h2>
  <p>ChatGPT wants to connect to your Trovis account to monitor agent activity.</p>
  <ul class="perms">
    <li>Report agent activity and task completions</li>
    <li>Register agents in your fleet</li>
    <li>Read agent status</li>
  </ul>
  <form method="POST" action="/oauth/authorize/submit">
    <input type="hidden" name="client_id" value="{client_id}">
    <input type="hidden" name="redirect_uri" value="{redirect_uri}">
    <input type="hidden" name="scope" value="{scope}">
    <input type="hidden" name="state" value="{state}">
    <label>Email</label>
    <input type="email" name="email" required placeholder="you@company.com">
    <label>Password</label>
    <input type="password" name="password" required placeholder="Your Trovis password">
    <div class="err" id="err"></div>
    <button type="submit" class="btn">Authorize</button>
    <button type="button" class="btn btn-cancel" onclick="window.close()">Cancel</button>
  </form>
</div>
</body></html>"""
    return HTMLResponse(html)


@app.post("/oauth/authorize/submit", include_in_schema=False)
async def oauth_authorize_submit(request: Request):
    """Process the consent form: validate credentials, issue a code, redirect."""
    form = await request.form()
    email = str(form.get("email", "")).strip().lower()
    password = str(form.get("password", ""))
    client_id = str(form.get("client_id", ""))
    redirect_uri = str(form.get("redirect_uri", ""))
    scope = str(form.get("scope", ""))
    state = str(form.get("state", ""))

    # Authenticate the user
    user_row = database.get_user_by_email(email)
    if not user_row or not user_row.get("password_hash"):
        return HTMLResponse("<h3>Invalid email or password.</h3><a href='javascript:history.back()'>Try again</a>", status_code=401)
    if not database.verify_password(password, user_row["password_hash"]):
        return HTMLResponse("<h3>Invalid email or password.</h3><a href='javascript:history.back()'>Try again</a>", status_code=401)

    account_id = user_row["account_id"]
    user_id = user_row["id"]

    # Create the authorization code
    code = database.create_oauth_code(
        account_id=account_id,
        user_id=user_id,
        client_id=client_id,
        redirect_uri=redirect_uri,
        scope=scope,
        state=state,
    )

    # Redirect back to ChatGPT with the code
    sep = "&" if "?" in redirect_uri else "?"
    location = f"{redirect_uri}{sep}code={code}"
    if state:
        location += f"&state={state}"
    return RedirectResponse(url=location, status_code=302)


@app.post("/oauth/token", include_in_schema=False)
async def oauth_token(request: Request):
    """Exchange an authorization code for an access token (ChatGPT server-to-server)."""
    # Accept both form-encoded and JSON bodies
    ct = request.headers.get("content-type", "")
    if "application/json" in ct:
        body = await request.json()
    else:
        form = await request.form()
        body = dict(form)

    grant_type = str(body.get("grant_type", ""))
    code = str(body.get("code", ""))
    client_id = str(body.get("client_id", ""))
    client_secret = str(body.get("client_secret", ""))
    redirect_uri = str(body.get("redirect_uri", ""))

    if grant_type != "authorization_code":
        raise HTTPException(status_code=400, detail="unsupported_grant_type")
    if not code:
        raise HTTPException(status_code=400, detail="missing code")

    result = database.exchange_oauth_code(code, client_id, redirect_uri)
    if result is None:
        raise HTTPException(status_code=400, detail="invalid_grant")
    return result


# --- Action endpoints (called by ChatGPT with the OAuth Bearer token) ---

def _resolve_action_account(request: Request) -> int | None:
    """Extract Bearer token from the request and resolve to account_id via
    the sessions table (the OAuth flow creates a session token)."""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        raw_token = auth[7:].strip()
        session = database.resolve_session(raw_token)
        if session:
            return session["account_id"]
    return None


# Per-account agent name tracking for the ChatGPT action session (same
# pattern as the MCP tools, but using request-scoped state).
_action_agents: dict[int, str] = {}


@app.post("/actions/connect")
async def action_connect(request: Request):
    """Register a ChatGPT agent with Trovis. Call at the start of a conversation."""
    account_id = _resolve_action_account(request)
    if account_id is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    body = await request.json()
    name = str(body.get("agent_name", "ChatGPT Agent")).strip() or "ChatGPT Agent"
    role = str(body.get("agent_role", "")).strip()
    instructions = str(body.get("agent_instructions", "")).strip()

    database.save_registration(
        service_name=name, agent_id="main", soul=instructions,
        identity=role, operating_manual="", user_context="",
        memory="", workspace_path="", model="chatgpt",
        account_id=account_id,
    )
    _action_agents[account_id] = name
    # Create a registration span
    import time as _time, uuid as _uuid
    now = int(_time.time() * 1_000_000_000)
    database.insert_spans([{
        "trace_id": _uuid.uuid4().hex,
        "span_id": _uuid.uuid4().hex[:16],
        "parent_span_id": None,
        "service_name": name,
        "span_name": "agent_registration",
        "kind": 0,
        "start_time_unix": now,
        "end_time_unix": now,
        "status_code": 0,
        "status_message": "",
        "attributes": {
            "trovis.event.type": "agent_registration",
            "trovis.agent.role": role,
        },
        "resource_attributes": {
            "service.name": name,
            "trovis.platform": "chatgpt",
        },
    }], account_id=account_id)
    return {"status": "connected", "agent_name": name,
            "message": f"Connected to Trovis as '{name}'. Activity is now being monitored."}


@app.post("/actions/log")
async def action_log(request: Request):
    """Log a completed activity or task step."""
    account_id = _resolve_action_account(request)
    if account_id is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    body = await request.json()
    service = _action_agents.get(account_id, "ChatGPT Agent")
    step = str(body.get("step_name", "activity")).strip() or "activity"
    desc = str(body.get("description", "")).strip()

    import time as _time, uuid as _uuid
    now = int(_time.time() * 1_000_000_000)
    dur = float(body.get("duration_seconds", 0) or 0)
    dur_ns = int(dur * 1_000_000_000) if dur > 0 else 0
    database.insert_spans([{
        "trace_id": _uuid.uuid4().hex,
        "span_id": _uuid.uuid4().hex[:16],
        "parent_span_id": None,
        "service_name": service,
        "span_name": step,
        "kind": 0,
        "start_time_unix": now - dur_ns,
        "end_time_unix": now,
        "status_code": 0,
        "status_message": "",
        "attributes": {
            "trovis.event.type": "agent_activity",
            "trovis.step.name": step,
            "trovis.step.description": desc,
            "trovis.tools.used": str(body.get("tools_used", "")),
            "trovis.output.summary": str(body.get("output_summary", "")),
        },
        "resource_attributes": {
            "service.name": service,
            "trovis.platform": "chatgpt",
        },
    }], account_id=account_id)
    return {"status": "logged", "step_name": step}


@app.post("/actions/complete")
async def action_complete(request: Request):
    """Report that a task or conversation is complete."""
    account_id = _resolve_action_account(request)
    if account_id is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    body = await request.json()
    service = _action_agents.get(account_id, "ChatGPT Agent")
    summary = str(body.get("task_summary", "")).strip()
    success = body.get("success", True)

    import time as _time, uuid as _uuid
    now = int(_time.time() * 1_000_000_000)
    database.insert_spans([{
        "trace_id": _uuid.uuid4().hex,
        "span_id": _uuid.uuid4().hex[:16],
        "parent_span_id": None,
        "service_name": service,
        "span_name": "agent_run_complete",
        "kind": 0,
        "start_time_unix": now,
        "end_time_unix": now,
        "status_code": 0 if success else 2,
        "status_message": "" if success else "task failed",
        "attributes": {
            "trovis.event.type": "agent_run_complete",
            "trovis.task.summary": summary,
            "trovis.run.success": success,
        },
        "resource_attributes": {
            "service.name": service,
            "trovis.platform": "chatgpt",
        },
    }], account_id=account_id)
    return {"status": "completed", "task_summary": summary}


@app.get("/actions/status")
async def action_status(request: Request):
    """Check monitoring connection status."""
    account_id = _resolve_action_account(request)
    if account_id is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    service = _action_agents.get(account_id)
    return {
        "status": "active",
        "agent_name": service,
        "message": f"Trovis monitoring active{' — reporting as ' + repr(service) if service else ''}.",
    }


@app.get("/actions/openapi.json", include_in_schema=False)
async def actions_openapi():
    """Serve the OpenAPI spec for the ChatGPT GPT Action."""
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Trovis Agent Monitoring",
            "description": "Monitor your AI agents with Trovis. Track activity, log steps, and report completions.",
            "version": "1.0.0",
        },
        "servers": [{"url": _OVERSEE_API_URL}],
        "paths": {
            "/actions/connect": {
                "post": {
                    "operationId": "connectAgent",
                    "summary": "Register an agent with Trovis monitoring",
                    "description": "Call at the start of each conversation to register this agent. Provide the agent's name, role, and instructions.",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "agent_name": {"type": "string", "description": "Name of this agent"},
                                        "agent_role": {"type": "string", "description": "What this agent does"},
                                        "agent_instructions": {"type": "string", "description": "The agent's system instructions"},
                                    },
                                    "required": ["agent_name"],
                                },
                            },
                        },
                    },
                    "responses": {"200": {"description": "Agent connected successfully"}},
                },
            },
            "/actions/log": {
                "post": {
                    "operationId": "logActivity",
                    "summary": "Log a completed activity or task step",
                    "description": "Call after completing each major step in a workflow. Describe what the agent did.",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "step_name": {"type": "string", "description": "Name of the step completed"},
                                        "description": {"type": "string", "description": "What happened"},
                                        "duration_seconds": {"type": "number", "description": "How long it took"},
                                        "tools_used": {"type": "string", "description": "Tools used (comma-separated)"},
                                        "output_summary": {"type": "string", "description": "Summary of output"},
                                    },
                                    "required": ["step_name", "description"],
                                },
                            },
                        },
                    },
                    "responses": {"200": {"description": "Activity logged"}},
                },
            },
            "/actions/complete": {
                "post": {
                    "operationId": "reportComplete",
                    "summary": "Report task completion",
                    "description": "Call when finishing a task or conversation. Summarize what was accomplished.",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "task_summary": {"type": "string", "description": "Summary of what was accomplished"},
                                        "steps_completed": {"type": "integer", "description": "Number of steps completed"},
                                        "success": {"type": "boolean", "description": "Whether the task succeeded"},
                                    },
                                    "required": ["task_summary"],
                                },
                            },
                        },
                    },
                    "responses": {"200": {"description": "Task completion reported"}},
                },
            },
            "/actions/status": {
                "get": {
                    "operationId": "checkStatus",
                    "summary": "Check monitoring connection status",
                    "description": "Check if Trovis monitoring is active and which agent is being tracked.",
                    "responses": {"200": {"description": "Monitoring status"}},
                },
            },
        },
    }


@app.post("/auth/keys", response_model=NewKeyResponse)
async def new_key(request: Request) -> NewKeyResponse:
    """Mint a new API key for the currently authenticated account.

    Goes through the auth middleware just like every other protected
    endpoint, so `request.state.account_id` is guaranteed to be set when
    keys exist in the DB. (When no keys exist, the middleware passes
    account_id=None — but then this endpoint has nothing to attach the
    new key to, so we 401 explicitly.)
    """
    account_id = getattr(request.state, "account_id", None)
    if account_id is None:
        raise HTTPException(
            status_code=401,
            detail="authentication required to mint additional keys",
        )
    api_key = database.generate_api_key(account_id)
    return NewKeyResponse(api_key=api_key, name="default")
