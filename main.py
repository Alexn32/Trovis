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

import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

import asker
import database
import describer
from models import (
    AgentDescription,
    AgentGroup,
    AgentOutput,
    AgentOwnerSet,
    AgentRegistration,
    AgentSummary,
    AskRequest,
    AskResponse,
    DisplayNameRequest,
    HealthResponse,
    IngestResponse,
    LoginRequest,
    LoginResponse,
    NewKeyResponse,
    SignupRequest,
    SignupResponse,
    Capabilities,
    OwnedAgent,
    SpanRecord,
    TeamMember,
    TeamMemberCreate,
    WeeklySummary,
    WeeklyTrends,
)

VERSION = "0.1.0"

# Endpoints that are always reachable without an API key. /health for
# uptime probes; /auth/signup and /auth/login because the user can't have
# a key yet when calling them.
_OPEN_PATHS = {"/health", "/auth/signup", "/auth/login"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()
    yield
    database.shutdown_db()


app = FastAPI(title="Oversee", version=VERSION, lifespan=lifespan)


# Middleware ordering note: in Starlette, the LAST-added middleware becomes
# the OUTERMOST wrapper. We need CORS outermost so cross-origin preflights
# (OPTIONS, sent by browsers without auth headers) pass through and so 401
# responses still carry CORS headers the dashboard can read. Therefore the
# auth middleware is registered first, CORS second.

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Multi-tenant API-key gate.

    - /health, /auth/signup, /auth/login, and all OPTIONS preflights bypass.
    - If the database has no active API keys (fresh install / local dev),
      every request passes through with `request.state.account_id = None`
      so existing handlers behave as before.
    - Otherwise, X-Oversee-Api-Key is required and must match an active
      key. The looked-up account_id is attached to request.state for
      handlers to scope their queries.
    """
    path = request.url.path
    if path in _OPEN_PATHS or request.method == "OPTIONS":
        request.state.account_id = None
        return await call_next(request)

    header_key = request.headers.get("X-Oversee-Api-Key")
    if header_key:
        result = database.validate_api_key(header_key)
        if not result:
            return JSONResponse(
                status_code=401,
                content={"error": "Invalid or missing API key"},
            )
        request.state.account_id = result["account_id"]
        return await call_next(request)

    # No key provided. Only allowed if no keys exist anywhere — that's
    # the pre-signup / local-dev mode.
    if database.has_any_keys():
        return JSONResponse(
            status_code=401,
            content={"error": "Invalid or missing API key"},
        )
    request.state.account_id = None
    return await call_next(request)


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
# Auth endpoints — multi-tenant v1
# ---------------------------------------------------------------------------
#
# /auth/signup and /auth/login are intentionally simple: email is the only
# identifier and there is no password. The API key IS the credential. This
# is enough for v1 demo / early users; proper email verification + password
# reset is a later concern.


@app.post("/auth/signup", response_model=SignupResponse, status_code=201)
async def signup(body: SignupRequest) -> SignupResponse:
    """Create an account and mint its first API key."""
    try:
        account = database.create_account(body.email)
    except database.EmailAlreadyExistsError:
        raise HTTPException(
            status_code=409,
            detail=f"an account with email '{body.email}' already exists",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    api_key = database.generate_api_key(account["id"])
    return SignupResponse(
        email=account["email"],
        api_key=api_key,
        message="Save your API key — you'll need it to connect agents and access your dashboard.",
    )


@app.post("/auth/login", response_model=LoginResponse)
async def login(body: LoginRequest) -> LoginResponse:
    """Look up an account by email and return its active API keys.

    No password in v1 — knowing the email is sufficient. This is a
    deliberate v1 simplification; proper auth comes later.
    """
    account = database.get_account_by_email(body.email)
    if account is None:
        raise HTTPException(
            status_code=404,
            detail=f"no account found for '{body.email}'",
        )
    keys = database.get_api_keys_for_account(account["id"])
    return LoginResponse(
        email=account["email"],
        api_keys=[k["key"] for k in keys if k["active"]],
    )


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
