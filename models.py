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

    `description` is the most recent Claude-generated description for
    this scope (per-instance when `agent_id` is None, per-sub-agent
    otherwise). `has_registration` indicates whether the agent has sent
    its identity files via an agent_registration span — when true,
    descriptions are far more accurate.

    `display_name` is the operator-set human-readable label for this
    agent, or None when no override exists. `agent_id` is populated
    only when the summary is scoped to a sub-agent.
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
    display_name: str | None = None
    owner_id: int | None = None
    owner_name: str | None = None
    owner_role: str | None = None
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0


class AgentInstance(BaseModel):
    """One sub-agent inside an `AgentGroup`. A flat single-agent instance
    still emits one of these (with `agent_id='main'`) so the response
    shape is consistent for both shapes.

    Each sub-agent carries its own description (generated from its own
    registration / telemetry), its own display_name override, and its
    own optional human owner.
    """

    agent_id: str
    span_count: int
    error_count: int
    avg_duration_ms: float
    first_seen: str | None = None
    last_seen: str | None = None
    has_registration: bool = False
    description: str | None = None
    display_name: str | None = None
    owner_id: int | None = None
    owner_name: str | None = None
    owner_role: str | None = None
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0
    cost_today: float = 0.0
    cost_7d: float = 0.0


class AgentGroup(BaseModel):
    """The /agents response shape — one row per OTEL `service.name`, with
    a nested list of sub-agents inside. A single-agent instance returns
    one element in `agents` (its agent_id is 'main' for SDKs that don't
    set `oversee.agent.id`); the frontend collapses that case into a
    flat card.

    Group-level `description` / `display_name` / `owner_*` default to
    the values from the `'main'` sub-agent when present (else the first
    agent listed), so the Fleet card has something sensible to show.
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
    display_name: str | None = None
    owner_name: str | None = None
    owner_role: str | None = None
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0
    cost_today: float = 0.0
    cost_7d: float = 0.0


class CostByDay(BaseModel):
    date: str
    tokens: int
    cost: float


class CostByModel(BaseModel):
    model: str
    tokens: int
    cost: float


class AgentCosts(BaseModel):
    """Response for GET /agents/{service_name}/costs. Token totals +
    estimated USD cost over the requested window, with per-day and
    per-model breakdowns for the cost chart."""

    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0
    cost_by_day: list[CostByDay] = Field(default_factory=list)
    cost_by_model: list[CostByModel] = Field(default_factory=list)


class AgentDeleteResponse(BaseModel):
    """Response shape for `DELETE /agents/{service_name}`. `agent_id`
    is None when the whole service was deleted; populated when only a
    single sub-agent was. `deleted_rows` is a per-table count so the
    caller can audit what just happened."""

    deleted: bool = True
    service_name: str
    agent_id: str | None = None
    deleted_rows: dict[str, int] = Field(default_factory=dict)


class DisplayNameRequest(BaseModel):
    """Body for PUT /agents/{service_name}/display-name. `agent_id`
    scopes the override to one sub-agent — pass 'main' for the default
    sub-agent in a single-agent instance. Empty `display_name` clears
    the override.
    """

    agent_id: str = "main"
    display_name: str = ""


# ---------------------------------------------------------------------------
# Team members + agent ownership
# ---------------------------------------------------------------------------


class TeamMember(BaseModel):
    """A human team member managed by the operator. One row per
    (account, email) when email is set."""

    id: int
    name: str
    email: str | None = None
    role: str | None = None
    created_at: str


class TeamMemberCreate(BaseModel):
    """Body for POST /team."""

    name: str
    email: str | None = None
    role: str | None = None


class AgentOwnerSet(BaseModel):
    """Body for PUT /agents/{service_name}/owner."""

    agent_id: str = "main"
    team_member_id: int


class WeeklyTrends(BaseModel):
    """Week-over-week percent deltas. Each field is None when there's
    no previous-week data to compare against. Positive = up, negative
    = down. For `errors_delta_pct` the frontend renders the inverse
    color (down is good); the other fields render up-is-good."""

    runs_delta_pct: float | None = None
    errors_delta_pct: float | None = None
    success_rate_delta_pct: float | None = None
    avg_duration_delta_pct: float | None = None


class WeeklySummary(BaseModel):
    """Response for GET /agents/{service_name}/weekly. `summary` is
    the 2-3 sentence Claude-generated paragraph (or empty when the
    Anthropic API key isn't configured). `generated_at` reflects when
    the cached summary was produced; the stats themselves are always
    fresh."""

    runs: int
    errors: int
    success_rate: float
    avg_duration_ms: float
    tools_used: list[str] = Field(default_factory=list)
    operations: list[str] = Field(default_factory=list)
    cost_estimate: float | None = None
    trends: WeeklyTrends = Field(default_factory=WeeklyTrends)
    summary: str = ""
    summary_unavailable: bool = False
    generated_at: str | None = None


class Capabilities(BaseModel):
    """Response for GET /agents/{service_name}/capabilities. Each
    field is a list of plain-English phrases. Empty list when Claude
    couldn't infer any items (or when the key is missing)."""

    reads_from: list[str] = Field(default_factory=list)
    writes_to: list[str] = Field(default_factory=list)
    can_do: list[str] = Field(default_factory=list)
    generated_at: str | None = None
    unavailable: bool = False


class OwnedAgent(BaseModel):
    """One (sub-)agent assignment for a team member, as returned by
    GET /team/{member_id}/agents. Carries enough to render a clickable
    row that links to the agent's detail page, plus a couple of stats
    so the team member's "agents" list is informative on its own."""

    service_name: str
    agent_id: str
    display_name: str | None = None
    last_seen: str | None = None
    span_count: int = 0


class WorkflowStep(BaseModel):
    """One step in a workflow's process. `step_type` is one of
    trigger|agent|human|decision|output. `operation` is the tool name for
    agent steps; `team_member_id` the assignee for human steps;
    `inferred_from` records how an auto-generated step was derived
    (telemetry|identity|gap_analysis|manual). `config` holds extra per-step
    data (e.g. decision branch labels) as a JSON object."""

    id: int
    workflow_id: int | None = None
    step_order: int
    step_type: str
    label: str
    description: str | None = None
    agent_service_name: str | None = None
    agent_id: str | None = None
    team_member_id: int | None = None
    team_member_name: str | None = None
    operation: str | None = None
    duration_estimate_ms: int | None = None
    inferred_from: str | None = None
    config: dict[str, Any] | None = None


class Workflow(BaseModel):
    """A named, ordered process flow for an agent. `steps` is populated on
    the detail endpoint; the list endpoint leaves it empty and sets
    `step_count` instead."""

    id: int
    account_id: int | None = None
    name: str
    description: str | None = None
    agent_service_name: str | None = None
    agent_id: str | None = "main"
    steps: list[WorkflowStep] = Field(default_factory=list)
    step_count: int = 0
    created_at: str | None = None
    updated_at: str | None = None


class WorkflowCreate(BaseModel):
    """Body for POST /workflows."""

    name: str
    agent_service_name: str | None = None
    agent_id: str | None = "main"
    description: str | None = None


class WorkflowUpdate(BaseModel):
    """Body for PUT /workflows/{id}. Only provided fields change."""

    name: str | None = None
    description: str | None = None


class WorkflowStepCreate(BaseModel):
    """Body for POST /workflows/{id}/steps. Only step_type + label are
    required; everything else is optional."""

    step_type: str
    label: str
    description: str | None = None
    operation: str | None = None
    duration_estimate_ms: int | None = None
    agent_service_name: str | None = None
    agent_id: str | None = None
    team_member_id: int | None = None
    inferred_from: str | None = "manual"
    config: dict[str, Any] | None = None
    step_order: int | None = None


class WorkflowStepUpdate(BaseModel):
    """Body for PUT /workflows/{id}/steps/{step_id}. All fields optional —
    only those present are patched."""

    step_type: str | None = None
    label: str | None = None
    description: str | None = None
    operation: str | None = None
    duration_estimate_ms: int | None = None
    agent_service_name: str | None = None
    agent_id: str | None = None
    team_member_id: int | None = None
    inferred_from: str | None = None
    config: dict[str, Any] | None = None
    step_order: int | None = None


class WorkflowReorder(BaseModel):
    """Body for POST /workflows/{id}/steps/reorder."""

    step_ids: list[int] = Field(default_factory=list)


class WorkflowGenerate(BaseModel):
    """Body for POST /workflows/generate — auto-build a workflow from one
    agent's telemetry + identity."""

    name: str
    agent_service_name: str
    agent_id: str | None = "main"


class WorkflowStats(BaseModel):
    """Live telemetry stats for a workflow's source agent (all-time).
    `has_agent` is False when the workflow has no agent to pull from."""

    has_agent: bool = False
    runs: int = 0
    spans: int = 0
    errors: int = 0
    success_rate: float = 0.0
    avg_duration_ms: float = 0.0
    last_run: str | None = None
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0


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
# Auth — real users + Individual/Business orgs
# ---------------------------------------------------------------------------


class UserPublic(BaseModel):
    """A user (login) — never carries the password hash."""

    id: int
    account_id: int
    email: str
    name: str | None = None
    role: str = "member"  # 'owner' | 'member'
    created_at: str | None = None
    last_login_at: str | None = None


class OrgPublic(BaseModel):
    """An account (organization/tenant)."""

    id: int
    email: str
    name: str | None = None
    account_type: str = "individual"  # 'individual' | 'business'
    created_at: str | None = None


class SignupRequest(BaseModel):
    email: str
    password: str
    name: str | None = None
    account_type: str = "individual"  # 'individual' | 'business'
    org_name: str | None = None


class SignupResponse(BaseModel):
    token: str
    user: UserPublic
    org: OrgPublic
    api_key: str  # initial org key for connecting agents
    message: str = ""


class LoginRequest(BaseModel):
    email: str
    password: str


class LoginResponse(BaseModel):
    token: str
    user: UserPublic
    org: OrgPublic


class MeResponse(BaseModel):
    """GET /auth/me. `user` is None for API-key (agent/legacy) auth."""

    user: UserPublic | None = None
    org: OrgPublic | None = None
    auth: str = "session"  # 'session' | 'api_key'


class ClaimRequest(BaseModel):
    """One-time migration: prove org ownership with an existing API key, then
    set the owner login. Only works when the org has no users yet."""

    api_key: str
    email: str
    password: str
    name: str | None = None


class SetPasswordRequest(BaseModel):
    new_password: str
    current_password: str | None = None


class InviteCreate(BaseModel):
    email: str
    role: str = "member"


class InviteCreateResponse(BaseModel):
    invite_url: str
    email: str
    role: str
    expires_at: str | None = None


class InvitePublic(BaseModel):
    id: int
    email: str
    role: str
    created_at: str | None = None
    expires_at: str | None = None


class AcceptInviteRequest(BaseModel):
    token: str
    name: str | None = None
    password: str


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
