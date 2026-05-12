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
from dotenv import load_dotenv
load_dotenv()

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware

import database
import describer
from models import AgentDescription, AgentSummary, HealthResponse, IngestResponse, SpanRecord

VERSION = "0.1.0"


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()
    yield
    database.shutdown_db()


app = FastAPI(title="Oversee", version=VERSION, lifespan=lifespan)

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

    spans = _parse_otlp_json(payload)
    inserted = database.insert_spans(spans)
    return IngestResponse(status="ok", spans_received=inserted)


@app.get("/agents", response_model=list[AgentSummary])
async def list_agents() -> list[AgentSummary]:
    return [AgentSummary(**a) for a in database.get_agents()]


@app.get("/agents/{service_name}/summary", response_model=AgentSummary)
async def agent_summary(service_name: str) -> AgentSummary:
    summary = database.get_agent_summary(service_name)
    if summary is None:
        raise HTTPException(status_code=404, detail=f"agent '{service_name}' not found")
    return AgentSummary(**summary)


@app.get("/agents/{service_name}/spans", response_model=list[SpanRecord])
async def agent_spans(
    service_name: str,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[SpanRecord]:
    return [SpanRecord(**s) for s in database.get_agent_spans(service_name, limit)]


@app.post("/agents/{service_name}/describe", response_model=AgentDescription)
async def generate_description(service_name: str) -> AgentDescription:
    """Generate a fresh Claude-written description and persist it."""
    try:
        result = describer.describe_agent(service_name)
    except describer.AgentNotFoundError:
        raise HTTPException(status_code=404, detail=f"agent '{service_name}' not found")
    except describer.APIKeyMissingError as e:
        raise HTTPException(status_code=503, detail=str(e))

    database.save_description(
        service_name=result["service_name"],
        description=result["description"],
        span_count_analyzed=result["span_count_analyzed"],
    )
    return AgentDescription(**result)


@app.get("/agents/{service_name}/description", response_model=AgentDescription)
async def latest_description(service_name: str) -> AgentDescription:
    """Return the most recent saved description for this agent."""
    desc = database.get_latest_description(service_name)
    if desc is None:
        raise HTTPException(
            status_code=404,
            detail=f"no description has been generated for agent '{service_name}' yet",
        )
    return AgentDescription(**desc)
