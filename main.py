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
    AgentOutput,
    AgentRegistration,
    AgentSummary,
    AskRequest,
    AskResponse,
    HealthResponse,
    IngestResponse,
    LoginRequest,
    LoginResponse,
    NewKeyResponse,
    SignupRequest,
    SignupResponse,
    SpanRecord,
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
    service_name: str, account_id: int | None, reason: str
) -> bool:
    """Synchronously generate and persist a Claude description for an agent.
    Returns True on success. Swallows all errors so the trace ingest path
    never fails because of a Claude API hiccup.

    `reason` is just for the log line so we can tell which trigger fired
    (a registration span vs. first-time telemetry arriving).
    """
    try:
        result = describer.describe_agent(service_name, account_id=account_id)
    except describer.AgentNotFoundError:
        # Can happen if the registration was extracted before any spans
        # for this service exist. The first-time path catches that case
        # after insert_spans runs.
        return False
    except describer.APIKeyMissingError:
        print(
            f"[Oversee] Auto-describe for '{service_name}' skipped — "
            f"ANTHROPIC_API_KEY not configured."
        )
        return False
    except Exception as e:
        print(f"[Oversee] Auto-describe for '{service_name}' failed: {e}")
        return False

    database.save_description(
        service_name=result["service_name"],
        description=result["description"],
        span_count_analyzed=result["span_count_analyzed"],
        account_id=account_id,
    )
    print(
        f"[Oversee] Auto-described '{service_name}' "
        f"(reason={reason}, source={result.get('source')}, "
        f"chars={len(result['description'])})"
    )
    return True


def _extract_registrations(
    spans: list[dict[str, Any]], account_id: int | None
) -> set[str]:
    """Pull out any agent_registration spans and persist them as registration
    rows. The spans themselves still get inserted into the spans table —
    this is an *additional* extraction so the registration data is queryable
    as structured rows.

    After each registration is saved, automatically generate a Claude
    description for the agent. Returns the set of service_names that were
    successfully auto-described so the caller can skip them in any
    follow-up first-time describe pass.
    """
    described: set[str] = set()
    for span in spans:
        attrs = span.get("attributes") or {}
        if attrs.get("oversee.event.type") != "agent_registration":
            continue
        service_name = span["service_name"]
        database.save_registration(
            service_name=service_name,
            agent_id=attrs.get("oversee.agent.id") or "main",
            soul=attrs.get("oversee.agent.soul") or "",
            identity=attrs.get("oversee.agent.identity") or "",
            operating_manual=attrs.get("oversee.agent.operating_manual") or "",
            user_context=attrs.get("oversee.agent.user_context") or "",
            memory=attrs.get("oversee.agent.memory") or "",
            workspace_path=attrs.get("oversee.agent.workspace_path") or "",
            model=attrs.get("oversee.agent.model") or "",
            account_id=account_id,
        )
        # Don't re-describe the same agent twice in one request if
        # multiple registration spans arrived for it.
        if service_name in described:
            continue
        if _auto_describe(service_name, account_id, reason="registration"):
            described.add(service_name)
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

    # Snapshot which services in this batch are "first-time" — no prior
    # spans for this account. We do this BEFORE inserting so we can tell
    # apart "first telemetry batch" from "ongoing telemetry."
    batch_services = {s["service_name"] for s in spans}
    first_time_services = {
        sn
        for sn in batch_services
        if database.get_agent_summary(sn, account_id=account_id) is None
    }

    # Insert spans first so any subsequent describe_agent calls see them.
    inserted = database.insert_spans(spans, account_id=account_id)

    # Save registrations + auto-describe on the registration path.
    described = _extract_registrations(spans, account_id=account_id)

    # First-time describe path: a registration that arrived in the SAME
    # batch as the first telemetry, or arrived in a prior batch but
    # didn't trigger _auto_describe because we'd never seen telemetry
    # for it yet. Skip anything already described in this request.
    for sn in first_time_services - described:
        if database.get_latest_registration(sn, account_id=account_id) is None:
            continue
        _auto_describe(sn, account_id, reason="first-telemetry")

    return IngestResponse(status="ok", spans_received=inserted)


@app.get("/agents", response_model=list[AgentSummary])
async def list_agents(request: Request) -> list[AgentSummary]:
    account_id = getattr(request.state, "account_id", None)
    return [AgentSummary(**a) for a in database.get_agents(account_id=account_id)]


@app.get("/agents/{service_name}/summary", response_model=AgentSummary)
async def agent_summary(service_name: str, request: Request) -> AgentSummary:
    account_id = getattr(request.state, "account_id", None)
    summary = database.get_agent_summary(service_name, account_id=account_id)
    if summary is None:
        raise HTTPException(status_code=404, detail=f"agent '{service_name}' not found")
    return AgentSummary(**summary)


@app.get("/agents/{service_name}/spans", response_model=list[SpanRecord])
async def agent_spans(
    service_name: str,
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[SpanRecord]:
    account_id = getattr(request.state, "account_id", None)
    return [
        SpanRecord(**s)
        for s in database.get_agent_spans(service_name, limit, account_id=account_id)
    ]


@app.post("/agents/{service_name}/describe", response_model=AgentDescription)
async def generate_description(
    service_name: str, request: Request
) -> AgentDescription:
    """Generate a fresh Claude-written description and persist it."""
    account_id = getattr(request.state, "account_id", None)
    try:
        result = describer.describe_agent(service_name, account_id=account_id)
    except describer.AgentNotFoundError:
        raise HTTPException(status_code=404, detail=f"agent '{service_name}' not found")
    except describer.APIKeyMissingError as e:
        raise HTTPException(status_code=503, detail=str(e))

    database.save_description(
        service_name=result["service_name"],
        description=result["description"],
        span_count_analyzed=result["span_count_analyzed"],
        account_id=account_id,
    )
    return AgentDescription(**result)


@app.get("/agents/{service_name}/description", response_model=AgentDescription)
async def latest_description(
    service_name: str, request: Request
) -> AgentDescription:
    """Return the most recent saved description for this agent."""
    account_id = getattr(request.state, "account_id", None)
    desc = database.get_latest_description(service_name, account_id=account_id)
    if desc is None:
        raise HTTPException(
            status_code=404,
            detail=f"no description has been generated for agent '{service_name}' yet",
        )
    return AgentDescription(**desc)


@app.get("/agents/{service_name}/registration", response_model=AgentRegistration)
async def latest_registration(
    service_name: str, request: Request
) -> AgentRegistration:
    """Return the most recent registration payload (SOUL, IDENTITY, etc.) for this agent."""
    account_id = getattr(request.state, "account_id", None)
    reg = database.get_latest_registration(service_name, account_id=account_id)
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
) -> list[AgentOutput]:
    """Return recent captured outputs (message bodies, responses, tool
    results) for this agent. Empty list when nothing's been captured —
    typically because the plugin's captureOutputs flag is off."""
    account_id = getattr(request.state, "account_id", None)
    return [
        AgentOutput(**o)
        for o in database.get_agent_outputs(
            service_name, account_id=account_id, limit=limit
        )
    ]


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
    service_name: str, request: Request, body: AskRequest
) -> AskResponse:
    """Answer a question scoped to one agent."""
    account_id = getattr(request.state, "account_id", None)
    msgs = [m.model_dump() for m in body.messages]
    try:
        answer = asker.ask_about_agent(service_name, account_id, msgs)
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
