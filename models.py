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
    # The 2-3 sentence extended description (shown behind the header "More"
    # toggle). None on pre-v2 rows → the detail endpoint regenerates on read.
    description_long: str | None = None
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
    # Detail-page status with a human reason (never a dot without a reason):
    # 'healthy' | 'attention' | 'error', plus the one-line explanation.
    status: str = "healthy"
    status_reason: str = ""
    # View-lock by plan. When locked, the detail page shows the "recording"
    # panel in place of the Work Feed. records_count + recording_since prove
    # the data exists; telemetry was never gated.
    locked: bool = False
    records_count: int | None = None
    recording_since: str | None = None


class DriftFinding(BaseModel):
    """One drift concern: declared identity vs. an observed behavior."""

    title: str
    evidence: str = ""
    severity: str = "low"  # 'low' | 'medium' | 'high'


class DriftReport(BaseModel):
    """GET /agents/{service}/drift — Claude's verdict on whether the agent's
    observed behavior stays within its declared job. `status` is
    'aligned' | 'minor' | 'drift' | 'unknown' ('unknown' = no declared identity
    on record, or the check couldn't run). `generated_at` is when the verdict
    was computed (it's cached server-side)."""

    status: str = "unknown"
    headline: str = ""
    findings: list[DriftFinding] = Field(default_factory=list)
    generated_at: str | None = None


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
    # View-locked when this sub-agent's first-seen position exceeds the plan
    # limit. Telemetry is still fully recorded.
    locked: bool = False


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
    # The instance card is locked only when every sub-agent is locked;
    # locked_count drives the "N recording" hint on a partially-locked group.
    locked: bool = False
    locked_count: int = 0


class AccountUsage(BaseModel):
    """GET /account/usage — drives the Fleet header + upgrade prompts."""

    plan: str = "free"
    agent_count: int = 0
    agent_limit: int | None = None  # None = unlimited
    locked_count: int = 0


class AccountPlanUpdate(BaseModel):
    """PUT /account/plan — request a plan change for the caller's own account.
    Reaching a paid tier is gated behind Stripe Checkout (see PlanChangeResult);
    only a no-op or a downgrade to 'free' applies directly. Validated
    server-side against the known plan tiers. `cycle` selects the monthly or
    annual (20%-off) price for paid upgrades."""

    plan: str
    cycle: str = "monthly"  # 'monthly' | 'annual'


class PlanChangeResult(BaseModel):
    """PUT /account/plan response. Either the change applied immediately
    (status='applied', `usage` populated) — only for a no-op or a downgrade to
    'free', which need no payment — or payment is required
    (status='checkout_required', `checkout_url` points at Stripe Checkout). In
    the checkout case the plan changes only once the signed
    `checkout.session.completed` webhook fires, never from this call."""

    status: str  # "applied" | "checkout_required"
    plan: str  # applied plan (status=applied) or requested tier (checkout_required)
    checkout_url: str | None = None
    usage: AccountUsage | None = None


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


class WaitlistRequest(BaseModel):
    """Body for POST /waitlist (public marketing-site signup)."""

    email: str
    source: str | None = None
    runtime_interest: str | None = None


class WaitlistResponse(BaseModel):
    """Result of a waitlist signup. `status` is "joined" for a new entry or
    "already_joined" when the email was already on the list (idempotent)."""

    status: str


class WaitlistCountResponse(BaseModel):
    """Public count of waitlist signups."""

    count: int


class WaitlistDeleteResponse(BaseModel):
    """Result of an operator deleting a waitlist signup. `deleted` is False
    when no row matched the email."""

    deleted: bool = False
    email: str


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
    # Token + cost totals for the same 7-day window as `runs`, so the
    # Agent Detail "This week" strip can render them. `cost` is None when
    # nothing priced (e.g. only token totals, no per-call input/output).
    tokens: int = 0
    cost: float | None = None
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


class Connection(BaseModel):
    """A directed agent→agent connection. `status` is detected (auto),
    confirmed/dismissed (operator decision on a detected edge), or manual
    (operator-drawn). Metrics come from shared-trace detection."""

    id: int
    source_service: str
    source_agent_id: str = "main"
    target_service: str
    target_agent_id: str = "main"
    status: str = "detected"
    call_count: int = 0
    trace_count: int = 0
    total_tokens: int = 0
    # What's transferred: top bridging operations [{operation, count}], and a
    # content sample when output-capture was on for the boundary span.
    via_operations: list[dict[str, Any]] = Field(default_factory=list)
    sample: str | None = None
    first_seen: str | None = None
    last_seen: str | None = None


class ConnectionCreate(BaseModel):
    source_service: str
    source_agent_id: str = "main"
    target_service: str
    target_agent_id: str = "main"


class ConnectionStatusUpdate(BaseModel):
    status: str  # 'confirmed' | 'dismissed' | 'detected' | 'manual'


class ConnectionsFromDescription(BaseModel):
    """Body for POST /connections/from-description — AI proposes agent→agent
    connections from a description."""

    description: str


# ---------------------------------------------------------------------------
# Workflows — named, VERSIONED declarations of recurring processes. (The
# legacy graph-workflow models were removed with their routes; the canvas
# will be rebuilt against this model.)
# ---------------------------------------------------------------------------


class WorkflowCreate(BaseModel):
    """POST /workflows body. stations describe who holds the work at each
    step (stored + validated, not used for matching yet); match_hints are
    ANDed conditions that recognize a loop as an instance of this
    workflow."""

    name: str
    stations: list[dict[str, Any]] = Field(default_factory=list)
    match_hints: list[dict[str, Any]] = Field(default_factory=list)
    note: str | None = None


class WorkflowVersionCreate(BaseModel):
    """POST /workflows/{id}/versions body — a FULL new definition (stations
    + hints), not a diff. note says what changed."""

    stations: list[dict[str, Any]] = Field(default_factory=list)
    match_hints: list[dict[str, Any]] = Field(default_factory=list)
    note: str | None = None


class WorkflowVersionInfo(BaseModel):
    version: int
    note: str | None = None
    created_by: str = ""
    created_at: str | None = None


class WorkflowSummary(BaseModel):
    """One workflow in GET /workflows: identity + live loop aggregates."""

    id: int
    name: str
    current_version: int = 1
    created_by: str = ""
    created_at: str | None = None
    archived_at: str | None = None
    loop_counts: dict[str, int] = Field(default_factory=dict)  # state -> count
    loops_today: int = 0


class WorkflowDetail(WorkflowSummary):
    """GET /workflows/{id}: the current version's definition + history."""

    stations: list[dict[str, Any]] = Field(default_factory=list)
    match_hints: list[dict[str, Any]] = Field(default_factory=list)
    versions: list[WorkflowVersionInfo] = Field(default_factory=list)


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
    description_long: str | None = None
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
    # Set when the owner finishes/skips onboarding; null → wizard still shows.
    onboarded_at: str | None = None
    # Plan tier — gates how many agents are viewable, never how many record.
    plan: str = "free"


class OrgProfileUpdate(BaseModel):
    """Body for PUT /org — set the workspace (org) display name."""

    name: str = ""


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


class ForgotPasswordRequest(BaseModel):
    email: str


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str


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


class RevealKeysRequest(BaseModel):
    """Body for POST /org/api-keys/reveal — step-up re-auth with the caller's
    password before exposing the org's existing API key(s)."""

    password: str


class ApiKeyInfo(BaseModel):
    key: str
    name: str = "default"
    created_at: str | None = None


class RevealKeysResponse(BaseModel):
    keys: list[ApiKeyInfo] = Field(default_factory=list)


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


class AskVisual(BaseModel):
    """An inline generative-UI block returned alongside an Ask answer. `type`
    maps to a frontend component; `props` are passed straight through."""

    type: str
    props: dict[str, Any] = Field(default_factory=dict)


class AskResponse(BaseModel):
    answer: str
    # Optional inline visual (Dashboard Ask pill). None for plain-text replies.
    visual: AskVisual | None = None


class ConnectCodeBlock(BaseModel):
    """One copy-paste setup snippet in a guided-connect reply. `content` may
    contain the literal placeholders TROVIS_API_KEY / TROVIS_ENDPOINT — the
    frontend substitutes the org's real values before render."""

    title: str | None = None
    language: str | None = None
    content: str


class ConnectAskResponse(BaseModel):
    """A guided add-agent chat turn: short answer, optional quick-reply
    chips, optional code snippets."""

    answer: str
    options: list[str] = Field(default_factory=list)
    code: list[ConnectCodeBlock] = Field(default_factory=list)


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


# ---------------------------------------------------------------------------
# Work Feed records (Agent Detail — one record == one trace == one interaction)
# ---------------------------------------------------------------------------


class RecordSpanItem(BaseModel):
    """A single span inside a record, shown in the deepest Work Feed view."""

    operation: str
    duration: str  # pre-formatted, e.g. "8.85s" / "38µs"
    status: str = "ok"  # 'ok' | 'error'


class RecordExchange(BaseModel):
    """The cleaned user prompt + agent response for an interaction record.
    None on system records (no exchange to show)."""

    user: str = ""
    agent: str = ""


class AgentRecord(BaseModel):
    """One Work Feed record. `kind` is 'interaction' (has an exchange) or
    'system' (registration/heartbeat — fixed summary, no exchange)."""

    id: str  # the trace_id (immutable → summary cache key)
    summary: str = ""
    time: str | None = None
    cost_usd: float | None = None
    duration_ms: float = 0.0
    tokens: int = 0
    kind: str = "interaction"
    error: bool = False
    exchange: RecordExchange | None = None
    spans: list[RecordSpanItem] = Field(default_factory=list)


class AgentRecordsResponse(BaseModel):
    """Cursor-paginated page of Work Feed records, newest first.

    When the agent is view-locked by plan, `locked` is true, `records` is empty
    (bodies/exchanges/spans withheld), and `records_count` + `recording_since`
    prove the data exists and is still being recorded — it just unlocks on
    upgrade."""

    records: list[AgentRecord] = Field(default_factory=list)
    next_cursor: str | None = None
    locked: bool = False
    records_count: int | None = None
    recording_since: str | None = None


class IngestResponse(BaseModel):
    status: str
    spans_received: int


# ---------------------------------------------------------------------------
# Dashboard — daily briefing, needs-attention, cost intelligence, work feed
# ---------------------------------------------------------------------------


class BriefingResponse(BaseModel):
    """Response for GET /dashboard/briefing. `summary` is the Claude-written
    2-3 sentence briefing (may be a non-AI fallback line). Counts are total
    spans (tasks); `tasks_delta` is a signed percentage string like '+12%'."""

    summary: str = ""
    tasks_yesterday: int = 0
    tasks_last_week: int = 0
    tasks_delta: str = "0%"
    generated_at: str | None = None


class AttentionItem(BaseModel):
    """One needs-attention row. `severity` is 'critical' | 'warning' | 'info';
    `agent` is the service_name (or display name). Enrichment fields are
    Claude-written and may be empty when the key is unset."""

    severity: str
    agent: str
    title: str = ""
    detail: str = ""
    recommendation: str = ""
    impact: str = ""
    last_seen: str | None = None


class CostAgent(BaseModel):
    """Per-agent cost row in the Cost Intelligence card. `trend` is
    'up' | 'down' | 'flat' (today vs. the trailing daily average)."""

    name: str
    cost: float = 0.0
    trend: str = "flat"


class CostResponse(BaseModel):
    """Response for GET /dashboard/cost. `today` matches the Fleet page (rolling
    24h). `daily` is up to 30 floats (oldest → newest) for the sparkline."""

    today: float = 0.0
    month_total: float = 0.0
    month_budget: float = 0.0
    budget_pct: float = 0.0
    agents: list[CostAgent] = Field(default_factory=list)
    daily: list[float] = Field(default_factory=list)


# --- dedicated cost page (overview + budgets) ---


class CostModelRow(BaseModel):
    model: str
    tokens: int = 0
    cost: float = 0.0


class CostAgentRow(BaseModel):
    """Per-agent (per service group) cost row on the cost page. `mtd` is
    month-to-date spend; `monthly_cap` is the editable per-agent limit (None =
    unset); `over_cap` is mtd > cap."""

    service_name: str
    agent_id: str = "main"
    name: str
    status: str = "healthy"
    today: float = 0.0
    cost_7d: float = 0.0
    total: float = 0.0
    mtd: float = 0.0
    monthly_cap: float | None = None
    over_cap: bool = False
    trend: str = "flat"


class CostOverview(BaseModel):
    """Response for GET /cost/overview — the dedicated cost page."""

    today: float = 0.0
    month_total: float = 0.0
    month_budget: float = 0.0
    budget_pct: float = 0.0
    over_budget: bool = False
    daily: list[float] = Field(default_factory=list)
    agents: list[CostAgentRow] = Field(default_factory=list)
    by_model: list[CostModelRow] = Field(default_factory=list)


class BudgetUpdate(BaseModel):
    """Body for PUT /cost/budget. None clears the budget (env default applies)."""

    monthly_budget: float | None = None


class AgentBudgetUpdate(BaseModel):
    """Body for PUT /cost/agent-budget. `monthly_cap` None clears the cap."""

    service_name: str
    agent_id: str = "main"
    monthly_cap: float | None = None


class AlertSettings(BaseModel):
    """The account's proactive-alert config (GET /account/alerts response)."""

    email_enabled: bool = True
    slack_webhook_url: str | None = None
    webhook_url: str | None = None
    rule_drift: bool = True
    rule_budget: bool = True
    rule_loop: bool = True
    rule_error: bool = True
    budget_warn_pct: int = 80
    loop_threshold: int = 50


class AlertSettingsUpdate(BaseModel):
    """Body for PUT /account/alerts. Every field optional — partial updates
    supported; unset fields keep their current value."""

    email_enabled: bool | None = None
    slack_webhook_url: str | None = None
    webhook_url: str | None = None
    rule_drift: bool | None = None
    rule_budget: bool | None = None
    rule_loop: bool | None = None
    rule_error: bool | None = None
    budget_warn_pct: int | None = None
    loop_threshold: int | None = None


class WorkFeedItem(BaseModel):
    """One Work Feed row — a plain-English summary of what an agent recently
    did. `time` is the ISO timestamp of the agent's latest span; `tasks` is
    its span count in the window."""

    time: str | None = None
    agent: str
    summary: str = ""
    tasks: int = 0


class ActivityItem(BaseModel):
    """One row in the chronological, fleet-wide Work Feed — a single real work
    event (span), newest first. `content`/`content_type` are populated only
    when the span carried captured output (message / response / tool result);
    `tool` is the tool name when the event was a tool call."""

    time: str | None = None
    agent: str
    service_name: str
    agent_id: str = "main"
    operation: str
    status: str = "ok"  # 'ok' | 'error'
    duration_ms: float = 0.0
    content: str | None = None
    content_type: str | None = None  # 'message' | 'response' | 'tool_result'
    tool: str | None = None
    loop_id: int | None = None  # workloop this action belongs to; NULL = ungrouped


class LoopParticipant(BaseModel):
    """One participant in a workloop. `participant` is the composite
    "service_name:agent_id" for agents, the user id (as a string) for
    humans."""

    participant_type: str  # 'agent' | 'human'
    participant: str
    role: str  # 'initiator' | 'executor' | 'reviewer'
    added_at: str | None = None


class LoopEventRecord(BaseModel):
    """One event in a loop's merged, ordered stream: a lifecycle event
    (loop_opened, handoff_*, loop_closed, ...) or a span-derived 'activity'
    event. `ts` is unix nanoseconds — the stream's ordering key."""

    type: str
    ts: int
    actor_type: str = ""  # 'agent' | 'human' | 'system'
    actor: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    # Plain-English display string (loops.narrate_events). The raw
    # span_name stays in payload; this is what the UI renders.
    sentence: str | None = None


class LoopSummary(BaseModel):
    """One workloop as listed by GET /loops. State is derived from events
    (loops.compute_loop_state); cached_state is a recomputed cache, never
    the source of truth. total_cost_usd is a live SUM over the loop's
    spans — no stored aggregate."""

    id: int
    external_id: str | None = None
    service_name: str
    agent_id: str = "main"
    title: str | None = None
    initiated_by_type: str
    initiated_by: str
    cached_state: str
    # Workflow match cache (null when unmatched). Records WHICH version the
    # loop matched — frozen once the loop reaches a terminal state.
    workflow_id: int | None = None
    workflow_name: str | None = None
    workflow_version: int | None = None
    last_event_unix: int | None = None
    created_at: str | None = None
    closed_at: str | None = None
    participant_count: int = 0
    span_count: int = 0
    event_count: int = 0
    total_cost_usd: float = 0.0
    stalled_for_s: int | None = None  # populated by GET /loops/stalled only
    # Possession bar data (loops.segments_mini) — enough for a proportional bar.
    segments_mini: list[SegmentMini] = Field(default_factory=list)


class LoopDetail(LoopSummary):
    """Full loop view: summary fields + participants + the complete ordered
    event stream."""

    participants: list[LoopParticipant] = Field(default_factory=list)
    events: list[LoopEventRecord] = Field(default_factory=list)
    # The possession chain (loops.compute_loop_segments) — computed live,
    # never stored.
    segments: list[LoopSegment] = Field(default_factory=list)


class LoopTouch(BaseModel):
    """One tool touched during a possession segment."""

    name: str
    count: int = 1


class SegmentMini(BaseModel):
    """List-row possession bar: who held the work, when, waiting or not."""

    holder_type: str  # 'agent' | 'human' | 'system'
    start_ns: int
    end_ns: int | None = None  # None = ongoing
    waiting: bool = False


class LoopSegment(SegmentMini):
    """Full possession segment for the loop detail's story view."""

    holder: str = ""
    touches: list[LoopTouch] = Field(default_factory=list)
    event_count: int = 0
