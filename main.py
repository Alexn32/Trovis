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
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

import asker
import database
import describer
import pricing_sync
from models import (
    AgentCosts,
    AgentDeleteResponse,
    AgentDescription,
    AgentGroup,
    AgentOutput,
    AgentOwnerSet,
    AgentRegistration,
    AgentSummary,
    AskRequest,
    AskResponse,
    AcceptInviteRequest,
    ClaimRequest,
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
    WeeklySummary,
    WeeklyTrends,
    Workflow,
    WorkflowCreate,
    WorkflowGenerate,
    WorkflowReorder,
    WorkflowStats,
    WorkflowStep,
    WorkflowStepCreate,
    WorkflowStepUpdate,
    WorkflowUpdate,
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
    # OVERSEE_DISABLE_PRICING_SYNC=1 turns off the network pull (offline dev,
    # tests) — the seeded prices still cover the common models.
    if os.getenv("OVERSEE_DISABLE_PRICING_SYNC", "").lower() not in (
        "1",
        "true",
        "yes",
    ):
        refresh_task = asyncio.create_task(_pricing_refresh_loop())
    try:
        yield
    finally:
        if refresh_task is not None:
            refresh_task.cancel()
        database.shutdown_db()


app = FastAPI(title="Oversee", version=VERSION, lifespan=lifespan)


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
    header_key = request.headers.get("X-Oversee-Api-Key")
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


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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
        if attrs.get("oversee.event.type") != "agent_registration":
            continue
        service_name = span["service_name"]
        agent_id = attrs.get("oversee.agent.id") or "main"
        database.save_registration(
            service_name=service_name,
            agent_id=agent_id,
            soul=attrs.get("oversee.agent.soul") or "",
            identity=attrs.get("oversee.agent.identity") or "",
            operating_manual=attrs.get("oversee.agent.operating_manual") or "",
            user_context=attrs.get("oversee.agent.user_context") or "",
            memory=attrs.get("oversee.agent.memory") or "",
            workspace_path=attrs.get("oversee.agent.workspace_path") or "",
            model=attrs.get("oversee.agent.model") or "",
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
    val = attrs.get("oversee.agent.id")
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


@app.get("/agents/{service_name}/summary", response_model=AgentSummary)
async def agent_summary(
    service_name: str,
    request: Request,
    agent_id: str | None = Query(default=None),
) -> AgentSummary:
    """Per-instance summary by default; per-agent when `?agent_id=` is set."""
    account_id = getattr(request.state, "account_id", None)
    summary = database.get_agent_summary(
        service_name, account_id=account_id, agent_id=agent_id
    )
    if summary is None:
        raise HTTPException(status_code=404, detail=f"agent '{service_name}' not found")
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
    cached insights.

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
    a step_count. Steps are loaded on the detail endpoint."""
    account_id = getattr(request.state, "account_id", None)
    return [Workflow(**w) for w in database.get_workflows(account_id=account_id)]


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
    """Live telemetry stats for the workflow's source agent (runs, errors,
    success rate, avg duration, last run, tokens, cost)."""
    account_id = getattr(request.state, "account_id", None)
    stats = database.get_workflow_stats(workflow_id, account_id=account_id)
    if stats is None:
        raise HTTPException(status_code=404, detail="workflow not found")
    return WorkflowStats(**stats)


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


@app.post("/workflows/generate", response_model=Workflow, status_code=201)
async def generate_workflow(request: Request, body: WorkflowGenerate) -> Workflow:
    """Auto-generate a workflow from one agent's telemetry + identity. Creates
    the workflow and all inferred steps, then returns the full workflow."""
    account_id = getattr(request.state, "account_id", None)
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
    worker thread so the event loop isn't stalled."""
    try:
        return await asyncio.to_thread(pricing_sync.refresh_pricing)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=502,
            detail=f"Pricing refresh failed: {e}",
        )


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
        cost_estimate=None,
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
        message="Welcome to Oversee. Use the API key to connect agents.",
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
    base = (os.environ.get("OVERSEE_APP_URL") or str(request.base_url)).rstrip("/")
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


@app.post("/ask", response_model=AskResponse)
async def ask_fleet(request: Request, body: AskRequest) -> AskResponse:
    """Answer a question about the user's whole fleet."""
    account_id = getattr(request.state, "account_id", None)
    msgs = [m.model_dump() for m in body.messages]
    try:
        answer = asker.ask_about_fleet(account_id, msgs)
    except asker.AskApiKeyMissingError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return AskResponse(answer=answer)


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
