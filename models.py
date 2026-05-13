"""Pydantic models for Oversee's request and response shapes.

Kept deliberately thin. The OTEL ingest payload is parsed with manual dict
walking in main.py rather than modeled here, because the OTLP/JSON wire format
is loose (mixed value types, optional fields) and a strict Pydantic model
would reject valid traffic from real-world agent SDKs.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str
    version: str


class AgentSummary(BaseModel):
    """Aggregate view of one agent, derived from its observed spans.

    `description` is the most recent Claude-generated description of this
    agent, or None if one has not been requested yet. `has_registration`
    indicates whether the agent has sent its identity files (SOUL, IDENTITY,
    etc.) via an agent_registration span — when true, descriptions are far
    more accurate.
    """

    service_name: str
    span_count: int
    error_count: int
    avg_duration_ms: float
    first_seen: str | None = None
    last_seen: str | None = None
    top_operations: list[str] = Field(default_factory=list)
    description: str | None = None
    has_registration: bool = False


class AgentDescription(BaseModel):
    """A single Claude-generated description of an agent.

    `source` is set on the POST /describe response to indicate which prompt
    path generated the text — "registration" when the agent's identity
    files were used, "telemetry_only" when we had to infer from spans
    alone. Not persisted, so the field is None when read back from the
    descriptions table via GET /description.
    """

    service_name: str
    description: str
    span_count_analyzed: int | None = None
    generated_at: str
    source: str | None = None


class AgentRegistration(BaseModel):
    """An agent's identity payload sent via an agent_registration span."""

    service_name: str
    agent_id: str = "main"
    soul: str = ""
    identity: str = ""
    operating_manual: str = ""
    user_context: str = ""
    memory: str = ""
    workspace_path: str = ""
    model: str = ""
    created_at: str


# ---------------------------------------------------------------------------
# Auth — multi-tenant v1
# ---------------------------------------------------------------------------


class SignupRequest(BaseModel):
    email: str


class SignupResponse(BaseModel):
    email: str
    api_key: str
    message: str


class LoginRequest(BaseModel):
    email: str


class LoginResponse(BaseModel):
    email: str
    api_keys: list[str] = Field(default_factory=list)


class NewKeyResponse(BaseModel):
    """Response shape for POST /auth/keys — a freshly-minted key for the
    currently authenticated account."""

    api_key: str
    name: str = "default"


class SpanRecord(BaseModel):
    """One row from the spans table, with JSON columns parsed back into dicts."""

    id: int
    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    service_name: str
    span_name: str
    kind: int = 0
    start_time_unix: int
    end_time_unix: int
    status_code: int = 0
    status_message: str = ""
    attributes: dict[str, Any] = Field(default_factory=dict)
    resource_attributes: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class IngestResponse(BaseModel):
    status: str
    spans_received: int
