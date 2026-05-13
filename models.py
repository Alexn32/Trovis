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
    """Aggregate view of one agent (or one sub-agent within an instance),
    derived from observed spans.

    `description` is the most recent Claude-generated description of the
    instance, or None if one has not been requested yet. `has_registration`
    indicates whether the agent has sent its identity files (SOUL,
    IDENTITY, etc.) via an agent_registration span — when true,
    descriptions are far more accurate.

    `agent_id` is populated only when the summary is scoped to a sub-agent
    (via `?agent_id=` on the route); otherwise it stays None to represent
    the full instance aggregate.
    """

    service_name: str
    agent_id: str | None = None
    span_count: int
    error_count: int
    avg_duration_ms: float
    first_seen: str | None = None
    last_seen: str | None = None
    top_operations: list[str] = Field(default_factory=list)
    description: str | None = None
    has_registration: bool = False
    # Best-effort label inferred from resource attributes
    # (e.g. "OpenClaw Agent", "Python Agent"). None when no
    # identifying signal is present.
    platform: str | None = None


class AgentInstance(BaseModel):
    """One sub-agent inside an `AgentGroup`. A flat single-agent instance
    still emits one of these (with `agent_id='main'`) so the response
    shape is consistent for both shapes."""

    agent_id: str
    span_count: int
    error_count: int
    avg_duration_ms: float
    first_seen: str | None = None
    last_seen: str | None = None
    has_registration: bool = False


class AgentGroup(BaseModel):
    """The /agents response shape — one row per OTEL `service.name`, with
    a nested list of sub-agents inside. A single-agent instance returns
    one element in `agents` (its agent_id is 'main' for SDKs that don't
    set `oversee.agent.id`); the frontend collapses that case into a
    flat card.
    """

    service_name: str
    agents: list[AgentInstance] = Field(default_factory=list)
    total_spans: int
    total_errors: int
    avg_duration_ms: float
    first_seen: str | None = None
    last_seen: str | None = None
    top_operations: list[str] = Field(default_factory=list)
    description: str | None = None
    has_registration: bool = False
    platform: str | None = None


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


# ---------------------------------------------------------------------------
# Ask — conversational Q&A over agent telemetry
# ---------------------------------------------------------------------------


class AskMessage(BaseModel):
    """One turn in a chat thread. role is 'user' or 'assistant'."""

    role: str
    content: str


class AskRequest(BaseModel):
    """Caller sends the full thread; backend is stateless. The last
    message must have role='user'."""

    messages: list[AskMessage] = Field(default_factory=list)


class AskResponse(BaseModel):
    answer: str


# ---------------------------------------------------------------------------
# Captured outputs
# ---------------------------------------------------------------------------


class AgentOutput(BaseModel):
    """One captured message/response/tool-result, extracted from a span
    when the plugin had captureOutputs=true at the time it was emitted."""

    operation: str
    timestamp: str
    # 'message' | 'response' | 'tool_result'
    content_type: str
    content: str
    duration_ms: float


class SpanRecord(BaseModel):
    """One row from the spans table, with JSON columns parsed back into dicts."""

    id: int
    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    service_name: str
    agent_id: str = "main"
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
