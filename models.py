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
    # THE agent status — computed server-side so every surface agrees.
    # 'healthy' | 'degraded' | 'idle'. There used to be three independent
    # classifiers (main._agent_status, Dashboard.deriveStatus,
    # utils.statusFor) with different thresholds and different bucket sets,
    # which is how the same agent in the same minute could be "healthy" on
    # one page and uncounted on another. Clients render this; they never
    # re-derive it.
    status: str = "idle"


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
    """Body for PUT /agents/{service_name}/owner.

    `user_id` is an org member — the way to assign an owner. `team_member_id`
    addresses the legacy directory and is accepted only so existing callers
    keep working; exactly one of the two must be set.
    """

    agent_id: str = "main"
    user_id: int | None = None
    team_member_id: int | None = None


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


class WorkflowExpectation(BaseModel):
    """What a job DECLARES about itself — the thing its observed numbers get
    measured against.

    Every field is optional and None means NOT DECLARED. None is never
    rendered as zero and never stands in for a default baseline: a job with
    no expectation reports what was observed and says "no expectation set".
    A verdict with no declared number behind it is an adjective, and this
    product does not ship adjectives.

    Expectations live on a VERSION, so changing one is a new version with the
    old preserved — and, like stations and match_hints, a version carries a
    FULL definition, so an omitted field clears it rather than inheriting.
    """

    # Two sentences, in the operator's own words, on what this job is.
    definition: str | None = None
    expected_per_day_min: int | None = None
    expected_per_day_max: int | None = None
    # Close-time ceiling, seconds.
    expected_close_s: int | None = None
    # Ceiling on the share of closed runs whose possession chain crosses a
    # PERSON. Agent-to-agent handoffs are not intervention.
    expected_intervention_pct: float | None = None
    expected_failure_pct: float | None = None
    # service_name + agent_id is the ROUTE to the agent's page; a display
    # label is never a route.
    owning_service_name: str | None = None
    owning_agent_id: str | None = None
    approval_routing: str | None = None
    # Per-job stall override. None = the global threshold.
    stall_threshold_s: int | None = None


class WorkflowObserved(BaseModel):
    """What the RECORD says, over the stated window. Computed once per
    request and read by every surface, so the board row, the health section
    and the path diagram cannot disagree with each other.

    A rate is None rather than 0 when there is nothing to divide: "no closed
    runs yet" and "nothing needed a person" are different facts.
    """

    # Runs that STARTED in the window — the cadence basis. Distinct from
    # closed_runs on purpose: "how often does this job run" and "how often
    # does it finish" are different questions, and a declared per-day
    # expectation is asking the first.
    started_runs: int = 0
    closed_runs: int = 0
    intervention_runs: int = 0
    failed_runs: int = 0
    cost_usd: float = 0.0
    cost_runs: int = 0
    median_close_s: int | None = None
    cost_per_run: float | None = None
    intervention_pct: float | None = None
    failure_pct: float | None = None
    window_days: int = 14
    # When this job last did anything, ignoring the window — a job that
    # STOPPED running is exactly what a board of live runs cannot show.
    last_run_at: str | None = None
    # True once at least one measurable ceiling is declared. Every verdict is
    # gated on this; a description alone is not an expectation.
    has_expectation: bool = False


class WorkflowStaleLink(BaseModel):
    """One open run holding a job that no current hint set would produce."""

    loop_id: int
    workflow_id: int
    workflow_name: str | None = None
    title: str | None = None
    # As far as the record can say: "workflow archived" or "hints no longer
    # match". Which of those is a mis-link cannot be decided from the data,
    # so this reports rather than acts.
    reason: str = ""


class WorkflowMatchHealth(BaseModel):
    """GET /workflows/match-health — the cost of sticky matching, made
    visible. `count` on its own is the signal worth watching."""

    count: int = 0
    links: list[WorkflowStaleLink] = Field(default_factory=list)


class WorkflowCreate(BaseModel):
    """POST /workflows body. stations describe who holds the work at each
    step (stored + validated, not used for matching yet); match_hints are
    ANDed conditions that recognize a loop as an instance of this
    workflow."""

    name: str
    stations: list[dict[str, Any]] = Field(default_factory=list)
    match_hints: list[dict[str, Any]] = Field(default_factory=list)
    note: str | None = None
    expectation: WorkflowExpectation | None = None


class WorkflowDraftRequest(BaseModel):
    """POST /workflows/draft body — a plain-English description of the
    process. Drafting only; nothing is persisted until the operator saves."""

    description: str


class WorkflowDraft(BaseModel):
    """A drafted declaration, in the same shape WorkflowCreate accepts back.
    Every field is editable in the UI before it's saved — the draft is a
    starting point, never an authority."""

    name: str = ""
    stations: list[dict[str, Any]] = Field(default_factory=list)
    match_hints: list[dict[str, Any]] = Field(default_factory=list)


class WorkflowVersionCreate(BaseModel):
    """POST /workflows/{id}/versions body — a FULL new definition (stations
    + hints), not a diff. note says what changed."""

    stations: list[dict[str, Any]] = Field(default_factory=list)
    match_hints: list[dict[str, Any]] = Field(default_factory=list)
    note: str | None = None
    expectation: WorkflowExpectation | None = None


class WorkflowVersionInfo(BaseModel):
    version: int
    note: str | None = None
    created_by: str = ""
    created_at: str | None = None


class WorkflowSummary(WorkflowExpectation, WorkflowObserved):
    """One workflow in GET /workflows: identity, its declared expectation,
    and what the record observed over the window."""

    id: int
    name: str
    current_version: int = 1
    created_by: str = ""
    created_at: str | None = None
    archived_at: str | None = None
    loop_counts: dict[str, int] = Field(default_factory=dict)  # state -> count
    loops_today: int = 0
    # The Work list renders each workflow's shape ("3 steps · triage-agent +
    # you") without a per-workflow fetch, so the summary carries the current
    # version's stations too.
    stations: list[dict[str, Any]] = Field(default_factory=list)
    # Seconds the oldest attention-state loop has sat unmoved (None when
    # nothing needs a human) — the "· 3h" in the list's warm chip.
    needs_you_for_s: int | None = None


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
    # Chart-editing ladder — deliberately NOT the same axis as view breadth.
    # A wide-view Exec is not an Org builder unless a builder made them one.
    org_builder: bool = False


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


class ScopeLevelPublic(BaseModel):
    """A named bundle of scope atoms. Customs compose the fixed atoms; they
    can never introduce a new one."""

    id: int
    key: str
    name: str
    breadth: str = "self"  # 'self' | 'subtree' | 'company'
    depth: str = "glance"  # 'glance' | 'technical'
    surfaces: list[str] = Field(default_factory=list)
    is_preset: bool = False


class Seat(BaseModel):
    """What a person sees and can change — resolved server-side from
    scope level → role → person. The client renders from this; it is never
    the authority for it (every list filter and chart edit is re-checked on
    the server).
    """

    breadth: str = "company"
    depth: str = "technical"
    surfaces: list[str] = Field(default_factory=list)
    org_builder: bool = False
    role_id: int | None = None
    role_title: str | None = None
    scope_level_id: int | None = None
    scope_level_name: str | None = None
    # People strictly below this person on the chart. Empty = no reports, so
    # the Whose-work control hides its team/person options.
    subtree_user_ids: list[int] = Field(default_factory=list)
    # Which people's work this seat may list. None = company-wide (no filter),
    # which is not the same as an empty list.
    visible_user_ids: list[int] | None = None
    can_edit_chart: bool = False


class ScopeLevelCreate(BaseModel):
    """Body for POST /org/scope-levels. Composes fixed atoms — an unknown
    breadth or depth is a 400, an unknown surface is dropped."""

    name: str
    breadth: str = "self"
    depth: str = "glance"
    surfaces: list[str] = Field(default_factory=list)
    key: str | None = None  # slug; derived from name when omitted


class RolePublic(BaseModel):
    """A box on the chart, as this caller may see it."""

    id: int
    title: str
    parent_role_id: int | None = None
    scope_level_id: int | None = None
    scope_level_name: str | None = None
    user_ids: list[int] = Field(default_factory=list)
    # Resolved server-side for THIS caller. The client uses it to grey out
    # controls; the server re-checks every write regardless.
    can_edit: bool = False
    can_add_child: bool = False


class RoleCreate(BaseModel):
    title: str
    parent_role_id: int | None = None
    scope_level_id: int | None = None


# No RoleUpdate model: PATCH /org/roles/{id} reads the raw body so an
# explicit `{"parent_role_id": null}` (detach) stays distinguishable from the
# field being absent (leave alone). A Pydantic model collapses both to None.


class RoleMemberAdd(BaseModel):
    user_id: int


class OrgChart(BaseModel):
    """GET /org/chart — the slice of the chart this caller may see, plus the
    people in it. `roles` is already filtered; it is not a full chart with a
    client-side mask over it."""

    roles: list[RolePublic] = Field(default_factory=list)
    members: list[UserPublic] = Field(default_factory=list)
    scope_levels: list[ScopeLevelPublic] = Field(default_factory=list)
    can_edit_chart: bool = False
    org_builder: bool = False


class OrgBuilderUpdate(BaseModel):
    org_builder: bool


class GraduateRequest(BaseModel):
    """Path A → Path B. Both fields optional: a workspace that already has a
    name and a chart just needs the account flipped."""

    org_name: str | None = None
    root_role_title: str | None = None


class GraduateResponse(BaseModel):
    org: OrgPublic
    root_role: RolePublic | None = None
    created_root_role: bool = False
    placed_founder: bool = False
    granted_org_builder: bool = False


class MeResponse(BaseModel):
    """GET /auth/me. `user` is None for API-key (agent/legacy) auth."""

    user: UserPublic | None = None
    org: OrgPublic | None = None
    auth: str = "session"  # 'session' | 'api_key'
    # None for API-key auth: a seat belongs to a person, not to a machine
    # credential. Agents ingest; they don't have a view.
    seat: Seat | None = None


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
    role: str = "member"  # login role: 'owner' | 'member'
    # The chart box the invitee lands in. Optional — an org that hasn't drawn
    # a chart still invites people the way it always did.
    role_id: int | None = None
    # What this person is CALLED. It works before they sign in: a handoff to
    # their email reads as their name rather than "a human". This is what
    # replaced the old team_members directory.
    name: str | None = None


class InviteCreateResponse(BaseModel):
    invite_url: str
    email: str
    role: str
    role_id: int | None = None
    display_name: str | None = None
    expires_at: str | None = None


class InvitePublic(BaseModel):
    id: int
    email: str
    role: str
    role_id: int | None = None
    display_name: str | None = None
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
    """Response for POST /v1/traces.

    `status` stays "ok" for a partially-accepted batch: a batch carrying some
    unattributable spans is not a failed request, and rejecting the whole thing
    would lose the good spans too. The counts are what make that honest —
    "ok" alone used to hide the loss entirely.

    `accepted` is the number of spans actually stored. `dropped` counts spans
    discarded before insert (today: no service.name, so nothing to attribute
    them to). `reason` names the cause in one line and is omitted when nothing
    was dropped.

    Renamed from `spans_received` in the same change that added the counts —
    a bare received-count could not distinguish "we took all 40" from "we took
    12 and binned 28". Neither the Python SDK nor the OpenClaw plugin reads
    this body (both branch on HTTP status only), and third-party OTLP clients
    read the OTLP-standard shape, not these fields.
    """

    status: str
    accepted: int = 0
    dropped: int = 0
    reason: str | None = None


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


class PulseInsightRequest(BaseModel):
    """POST /dashboard/pulse-insight — the DATA packet Home assembled.

    Home builds this from what it ALREADY fetched (work overview + items,
    cost, attention), so the pulse cannot disagree with the proof strip: both
    read the same numbers in the same browser. The server does not re-query;
    it only generates and, crucially, validates.
    """

    packet: dict[str, Any] = Field(default_factory=dict)


class PulseInsightResponse(BaseModel):
    """The one generated sentence, or none.

    `insight` is empty whenever the model was slow, unavailable, or said
    something the packet does not entail — an empty string is a normal
    answer, not an error. `graphic` is always usable: it comes from the
    deterministic chooser unless the model picked a series we actually have.
    """

    insight: str = ""
    graphic: str = "none"
    used: list[str] = Field(default_factory=list)
    cached: bool = False


class AttentionItem(BaseModel):
    """One needs-attention row. `severity` is 'critical' | 'warning' | 'info'.

    `agent` is the human LABEL — a display name when the operator set one, and
    for drift rows on a multi-agent service it also carries the sub-agent
    ("Support Bot · researcher"). It is for reading only. Route with
    `service_name` + `agent_id`, never with `agent`.

    Enrichment fields are Claude-written and may be empty when the key is unset.
    """

    severity: str
    agent: str
    service_name: str | None = None
    agent_id: str = "main"
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
    # `agents` is the TOP SPENDERS, capped — never count it. `agent_count` is
    # how many are actually reporting, which is what Home's fleet pulse says
    # out loud; counting a capped list would print "8 agents" for a fleet of 30.
    agent_count: int = 0
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


class CostDayPoint(BaseModel):
    """One UTC calendar day on the cost trend. Days with no usage are still
    emitted (cost 0) so the chart's x-axis is evenly spaced."""

    date: str  # YYYY-MM-DD (UTC)
    cost: float = 0.0
    tokens: int = 0


class CostOverview(BaseModel):
    """Response for GET /cost/overview — the dedicated cost page."""

    today: float = 0.0
    month_total: float = 0.0
    month_budget: float = 0.0
    budget_pct: float = 0.0
    over_budget: bool = False
    # `daily` is the bare cost series (oldest → newest) kept for older clients;
    # `series` carries the same days with their date + token count so the chart
    # can label and inspect each point. `days` echoes the requested window.
    days: int = 30
    daily: list[float] = Field(default_factory=list)
    series: list[CostDayPoint] = Field(default_factory=list)
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
    # `agent` is the human LABEL (display name when one is set). It is for
    # reading, never for routing — see service_name.
    agent: str
    # Where the row actually points. The label can be "Support Bot" while the
    # route needs "support-agent"; navigating by the label 404s the agent page.
    service_name: str = ""
    agent_id: str = "main"
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
    # Set when the job has been archived. Matching is sticky, so a run can
    # outlive its job's retirement: the name stays (the run really did run
    # under it) and this says it is no longer current.
    workflow_archived_at: str | None = None
    workflow_version: int | None = None
    last_event_unix: int | None = None
    created_at: str | None = None
    closed_at: str | None = None
    participant_count: int = 0
    span_count: int = 0
    event_count: int = 0
    total_cost_usd: float = 0.0
    stalled_for_s: int | None = None  # populated by GET /loops/stalled only
    # Who the loop is waiting on, resolved SERVER-side. "waiting on you" is a
    # claim about a person, and only the server knows which person the
    # authenticated caller is — the client must never infer it from state
    # alone (that bug is why every member of an org saw the same list and was
    # each told it was theirs). All three are null/false unless the loop has
    # an unresolved to_human handoff.
    awaiting_human_name: str | None = None   # resolved display name, when it resolves
    awaiting_is_you: bool = False            # target IS the authenticated user
    awaiting_handoff_event_id: int | None = None  # loop_events.id to resolve against
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


class HandoffResolveRequest(BaseModel):
    """Body for the handoff-resolution endpoints. Only decline uses `reason`;
    accept/complete accept an empty body (the verb IS the payload)."""

    reason: str | None = None


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


class LoopMapPosition(BaseModel):
    """Where a live loop sits on its workflow's station map.
    status: 'on_path' (dot under station_index) | 'off_path' (list-only,
    no dot) | 'no_stations' (hint-only workflow)."""

    status: str
    station_index: int | None = None


class WorkflowMapLoop(BaseModel):
    id: int
    title: str | None = None
    cached_state: str
    last_event_unix: int | None = None
    service_name: str
    agent_id: str = "main"
    position: LoopMapPosition


class WorkflowMap(BaseModel):
    """GET /workflows/{id}/map — stations + live loop positions + the one
    allowed aggregate (done today). Computed live, never stored."""

    workflow_id: int
    name: str
    version: int
    stations: list[dict[str, Any]] = Field(default_factory=list)
    loops: list[WorkflowMapLoop] = Field(default_factory=list)
    done_today: int = 0

class BoardCard(BaseModel):
    """One task on the Work board. Four facts and the actions — nothing that
    requires Trovis vocabulary to read. `state` is the raw engine state, kept
    for client-side logic only; nothing renders it."""

    id: int
    title: str
    column: str            # working | waiting_person | stuck | done
    state: str             # raw cached_state — logic only, never displayed
    holder_type: str       # agent | human
    holder_name: str
    waiting_on: str | None = None   # blocked-on note for agent/system waits
    age_seconds: int | None = None  # time in the current state
    cost_usd: float = 0.0
    is_yours: bool = False
    handoff_event_id: int | None = None
    workflow_id: int | None = None
    workflow_name: str | None = None
    stuck_reason: str | None = None  # why, in plain words, for the Stuck column
    closed_at: str | None = None
    # Standing (always-on) work — display classification only. A standing
    # task keeps its normal state quiet (see WorkBoard.ongoing) and shows the
    # reason on its Level-3 detail so a misclassification is one click to spot.
    standing: bool = False
    standing_reason: str | None = None


class BoardColumn(BaseModel):
    key: str
    label: str
    cards: list[BoardCard] = Field(default_factory=list)
    count: int = 0


class BoardWorkflow(BaseModel):
    id: int
    name: str
    count: int = 0


class WorkKindCard(BaseModel):
    """One kind of work on the Level-1 Work screen — a workflow, or the
    catch-all "Other work". Rollups are live counts of the tasks underneath
    it; nothing here needs Trovis vocabulary to read."""

    workflow_id: int | None = None   # None = the "Other work" catch-all
    name: str
    in_motion: int = 0
    waiting_person: int = 0
    stuck: int = 0
    done_today: int = 0
    ongoing: int = 0   # always-on work; quiet, not a finite card
    cost_today: float = 0.0
    is_other: bool = False
    # Only the "Other work" card, and only when its in-motion pile is bigger
    # than every declared kind's — the nudge to declare a workflow.
    suggest_declare: bool = False


class WorkSummary(BaseModel):
    """GET /work/summary — the Work tab's Level-1 landing, in one request.
    `yours` is the cross-workflow strip of tasks waiting on the caller,
    reusing the board's card so it renders identically."""

    yours: list[BoardCard] = Field(default_factory=list)
    kinds: list[WorkKindCard] = Field(default_factory=list)
    other: WorkKindCard | None = None
    has_agents: bool = False
    total: int = 0


class WorkBoard(BaseModel):
    """GET /work/board — the whole board in one request."""

    columns: list[BoardColumn] = Field(default_factory=list)
    workflows: list[BoardWorkflow] = Field(default_factory=list)
    # Always-on work, kept OUT of the Working column so it can't accumulate as
    # permanent cards. Its normal state is quiet (a collapsed line); its
    # exceptions (waiting on a person / stuck) leave "standing" and appear as
    # ordinary cards. See database._classify_standing.
    ongoing: list[BoardCard] = Field(default_factory=list)
    total: int = 0
    has_agents: bool = False


# ---------------------------------------------------------------------------
# Lean Work home (overview + items). Fat /work/board and /work/summary must
# not be the home path — they scan every open loop's events and starve the
# single Uvicorn replica. These shapes are the Frontend/UX locked contract.
# ---------------------------------------------------------------------------

WORK_ITEM_STATUSES = (
    "waiting_on_you",
    "waiting_on_other",  # UI label is "Waiting on someone"; wire value stays this
    "stuck",
    "moving",
    "done",
)
WORK_HOLDER_KINDS = ("human", "agent", "tool", "unassigned")


class WorkOverview(BaseModel):
    """GET /work/overview — counts only. Named work (human titles). No board dump.

    Untitled / generated / "Task from …" shells are excluded.
    needs_you = waiting_on_you ONLY.
    needs_attention = stuck + aging waiting_on_other (never waiting_on_you).
    """

    needs_you: int = 0
    needs_attention: int = 0
    open: int = 0
    completed_week: int = 0
    # The week before, so Home's pulse can compare. `has_prev_week` says
    # whether that comparison is meaningful at all: a zero prior week in an
    # org that is four days old is not a decline, and drawing it as one would
    # be the pulse's first lie.
    completed_prev_week: int = 0
    has_prev_week: bool = False


# ---------------------------------------------------------------------------
# GET /home/snapshot — the authoritative Home snapshot.
#
# Field semantics are documented here AND in HOME_SNAPSHOT.md. The rules the
# shape encodes:
#   * current_state (now) and period (a window) are separate objects. They are
#     different kinds of measure and are never added together.
#   * anything we cannot establish is null + a named reason, never 0.
#   * a completion is a RECORDED completion, not a verified business outcome.
# ---------------------------------------------------------------------------


class HomeScope(BaseModel):
    """Work scope: what was asked for, what was granted, what is possible."""

    requested: str  # everyone | me | team | person
    # The selector the server ACTUALLY applied. Derived from the resolution,
    # never from whether the selector appears in `choices` — a company-breadth
    # person with no reports who asks for `team` gets their own work, and
    # calling that `everyone` would describe the opposite of the filter.
    effective: str
    # The raw value was not a known selector. It narrowed nothing (existing
    # API behavior) and `effective` is `everyone`.
    request_unreadable: bool = False
    person_id: int | None = None
    # What a CONTROL should offer. Advisory display affordance — not the
    # permission ceiling, and not what ran.
    choices: list[str] = Field(default_factory=list)
    selector_offered: bool = True  # is `effective` one a control would draw?
    # The seat removed people the selector asked for (requests narrow, never
    # widen).
    clamped_by_seat: bool = False
    breadth: str | None = None  # seat atom: self | subtree | company
    scope_level_id: int | None = None
    role_id: int | None = None
    filtered: bool = False  # False = company-wide, no person filter at all
    people_in_scope: int | None = None  # null when unfiltered, NOT "nobody"
    # False when the whose-work filter's waiting-on leg hit its scan cap. Every
    # count over this scope is then a LOWER BOUND, not a total.
    membership_complete: bool = True
    membership_incomplete_reason: str | None = None
    account_id: int | None = None
    viewer_user_id: int | None = None


class HomeComparison(BaseModel):
    """The equal-duration window immediately before the period, or why not."""

    available: bool = False
    unavailable_reason: str | None = None
    previous_start_utc: str | None = None
    previous_end_utc: str | None = None
    previous_completed: int | None = None
    delta: int | None = None


class HomePeriod(BaseModel):
    """Explicit period boundaries + the period's completion total.

    `completed` counts recorded work completions (closed named work items)
    whose completion timestamp falls in [start_utc, end_utc). Never spans,
    tool calls, nested child activity, or agent registrations.
    """

    start: str  # local ISO-8601 with offset
    end: str
    start_utc: str
    end_utc: str
    timezone: str  # IANA zone the boundaries and buckets were computed in
    days: int
    completed: int = 0
    # Terminal closes in the window that were NOT completions (abandoned by
    # the sweep, or an ingestion artifact). Reported so abandoned work stays
    # visible as itself instead of being rewritten as success or dropped.
    abandoned: int = 0
    # False when the scope's membership is incomplete: `completed` is then an
    # explicitly labeled lower bound, and `qualifier` says "at_least".
    exact: bool = True
    qualifier: str = "exact"  # exact | at_least
    comparison: HomeComparison


class HomeCurrentState(BaseModel):
    """Work in flight RIGHT NOW. A state, not a period measure.

    Buckets come from `loops.cached_state` — the same engine state
    /work/overview counts needs_attention from.
    """

    as_of: str = "now"
    open: int = 0
    moving: int = 0
    waiting_on_person: int = 0
    blocked: int = 0
    exact: bool = True
    qualifier: str = "exact"  # exact | at_least
    # There is no history of these values in the record, so no trend is
    # offered. Manufacturing one from today's numbers would be a chart of
    # nothing.
    trend_available: bool = False
    trend_unavailable_reason: str | None = None


class HomeSeriesPoint(BaseModel):
    bucket_start: str  # local ISO-8601 with offset
    bucket_start_utc: str
    completed: int = 0


class HomeCompletionSeries(BaseModel):
    """Recorded completions per local calendar day, bucketed on closed_at.

    `reconciles` is checked against the independent aggregate COUNT rather
    than assumed.
    """

    available: bool = True
    unavailable_reason: str | None = None
    bucket: str = "local_day"
    timezone: str = "UTC"
    points: list[HomeSeriesPoint] = Field(default_factory=list)
    total: int | None = None  # sum of the buckets
    aggregate_total: int = 0  # the period COUNT, computed separately
    # Buckets agree with the aggregate. NOT a completeness claim: when `exact`
    # is False both are drawn from the same subset, so they agree while both
    # under-count.
    reconciles: bool | None = None
    exact: bool = True
    qualifier: str = "exact"  # exact | at_least


class HomeJobRow(BaseModel):
    workflow_id: int
    name: str | None = None
    completed: int = 0


class HomeJobBreakdown(BaseModel):
    """Completions by EXISTING job identity (`loops.workflow_id`).

    No outcome classification is invented. Work the matcher never claimed is
    reported as `unclassified_completed`, never dropped.
    """

    rows: list[HomeJobRow] = Field(default_factory=list)
    row_limit: int = 12
    truncated: bool = False
    other_completed: int = 0  # the tail past row_limit, so charts still add up
    unclassified_completed: int = 0
    total_job_count: int = 0
    aggregate_total: int = 0
    reconciles: bool = True
    exact: bool = True
    qualifier: str = "exact"  # exact | at_least


class HomeAttention(BaseModel):
    """What is waiting on the SIGNED-IN PERSON.

    Tied to session identity and unaffected by the whose-work selection. Null
    (not 0) for a machine session, which has no person.
    """

    available: bool = True
    unavailable_reason: str | None = None
    # The exact count, or None when it cannot be established: a machine
    # session (no person), or a truncated assignee scan. Never a confident 0
    # standing in for either — "nothing needs you" is the worst thing this
    # endpoint could get wrong.
    needs_you: int | None = None
    # What the (possibly capped) scan did find: an explicitly labeled lower
    # bound, useful even when the total is unavailable.
    needs_you_at_least: int | None = None
    viewer_user_id: int | None = None
    scoped_to: str = "session_identity"
    unplaced_viewer: bool | None = None
    # False when the candidate cap or the handoff-event budget bit, so the
    # matched set is a subset of positively established matches.
    resolution_complete: bool = True


class HomeCostCoverage(BaseModel):
    """What share of cost-bearing spans carry a stored price.

    A span count, NOT a share of dollars — the value of unpriced spans is
    exactly what is unknown. `ratio` is null when there is no denominator.
    """

    measure: str = "priced_cost_bearing_spans"
    definition: str = ""
    priced_spans: int = 0
    unpriced_token_spans: int = 0
    denominator: int = 0
    ratio: float | None = None
    unavailable_reason: str | None = None


class HomeFinancial(BaseModel):
    """Money, only when the resolved seat includes the Cost surface.

    Always ORGANIZATION-WIDE: stored span cost hangs off the account and the
    agent, not off a work scope. No cost-per-completion is offered, because
    dividing org-wide spend by a narrowed completion count describes nothing.
    """

    visible: bool = False
    unavailable_reason: str | None = None
    scope: str | None = None  # 'organization_wide' when visible
    scope_note: str | None = None
    attributable_to_shown_work: bool | None = None
    currency: str | None = None
    period_start_utc: str | None = None
    period_end_utc: str | None = None
    spend_usd: float | None = None
    coverage: HomeCostCoverage | None = None


class HomeFreshness(BaseModel):
    """How current the underlying record is. All UTC ISO-8601.

    A timestamp is positively established. A `null` is NOT its mirror image:
    when `absence_established` is False (the scope's membership is incomplete)
    a null means "no such record was found", not "no such record exists" — the
    row carrying a later timestamp may be one the capped scan never read.
    `latest_telemetry_at` is account-wide and membership-independent, so it is
    always exact.
    """

    latest_recorded_completion_at: str | None = None
    latest_work_activity_at: str | None = None
    latest_telemetry_at: str | None = None
    first_recorded_work_at: str | None = None
    absence_established: bool = True
    unavailable_reason: str | None = None


class HomeUnavailable(BaseModel):
    field: str
    reason: str


class HomeCompleteness(BaseModel):
    """Everything the snapshot could not establish, in one place, so a
    renderer can hide a visual without re-inspecting each block.

    Every flag here is derived from ALL of its conditions — membership
    completeness, retrieval completeness, and reconciliation — not from
    whichever one its own block happened to notice. A chart that reconciles
    with an aggregate drawn from the same incomplete membership is not a
    complete chart.

    Two different "empty" questions, deliberately kept apart:

      workspace_state  the ACCOUNT, from an unfiltered EXISTS. Always `empty`
                       or `populated`, never `unknown`. This is what an
                       onboarding / connect-your-first-agent screen keys off.
      scope_state      the SELECTED SCOPE. `populated` when rows were found,
                       `empty` when none were found and the search was
                       complete, `unknown` when none were found by a partial
                       search — finding nothing in an incomplete scope does not
                       establish that nothing exists.
    """

    # The account. Membership-independent, so always establishable.
    has_any_recorded_work: bool = False
    workspace_state: str = "empty"  # empty | populated
    # The selected scope.
    scope_state: str = "empty"  # empty | populated | unknown
    has_any_work_in_scope: bool | None = None  # None = not established
    # May a null or a zero anywhere in this response be read as "none exists"?
    absence_established: bool = True
    completion_series_complete: bool = True
    job_breakdown_complete: bool = True
    comparison_available: bool = False
    financial_available: bool = False
    # False when the whose-work filter's assignment resolution hit a bound:
    # every work count in the snapshot is then a lower bound, not a total.
    scope_membership_complete: bool = True
    # False when the candidate cap or the handoff-event budget bit.
    assignment_resolution_complete: bool = True
    counts_exact: bool = True
    unavailable: list[HomeUnavailable] = Field(default_factory=list)


class HomeNavigation(BaseModel):
    """Canonical identifiers a later Home UI needs to drill in WITHOUT losing
    the scope and period on screen."""

    account_id: int | None = None
    carry_query: dict[str, Any] = Field(default_factory=dict)
    work_items_path: str = "/work/items"
    work_overview_path: str = "/work/overview"
    work_items_supports: list[str] = Field(default_factory=list)


class HomeSnapshot(BaseModel):
    """GET /home/snapshot. One bounded read of the record; no LLM on this path.

    Not a data dump: no raw traces and no full work records. Drill-in stays
    with /work/items.
    """

    generated_at: str
    scope: HomeScope
    period: HomePeriod
    current_state: HomeCurrentState
    completions_series: HomeCompletionSeries
    by_job: HomeJobBreakdown
    attention: HomeAttention
    financial: HomeFinancial
    freshness: HomeFreshness
    completeness: HomeCompleteness
    navigation: HomeNavigation


# ---------------------------------------------------------------------------
# GET /home/findings — Trovis's evidence-backed investigation of this account
#
# A finding is not a rendering of a metric. The metrics live in
# /home/snapshot; a finding exists only where investigation established
# something the reader would otherwise miss, and every material claim points
# at evidence that can be re-opened. Contract: HOME_FINDINGS.md.
# ---------------------------------------------------------------------------


class FindingEntity(BaseModel):
    """A canonical thing the finding is about. Existing ids only — this layer
    invents no taxonomy and no relationships."""

    kind: str  # run | job | agent | person
    id: Any
    label: str | None = None


class FindingClaim(BaseModel):
    """One statement inside a finding, with its kind and its backing.

    `kind` is load-bearing: `observation` is what the record says, `calculation`
    is what the server computed, `hypothesis` is a proposed explanation. A
    hypothesis rendered as an observation is how an analytics product stops
    being trustworthy.

    A claim carrying a number must carry `metric_ref` too — `snapshot:<path>`
    or `calc:<id>` — and the value is checked against it. The model never does
    arithmetic.
    """

    text: str
    kind: str  # observation | calculation | hypothesis
    value: float | None = None
    metric_ref: str | None = None
    evidence: list[str] = Field(default_factory=list)


class FindingEvidence(BaseModel):
    """A re-openable reference. `digest` pins what the record said when the
    finding was written, so a later read can report the basis as CHANGED
    rather than silently showing today's row as the original reason."""

    kind: str  # run | run_event | failed_span | calculation | snapshot | agent_context
    ref: str
    note: str | None = None
    digest: str | None = None
    status: str | None = None  # set on detail reads: changed | missing


class FindingNextStep(BaseModel):
    """Something a PERSON does. This layer executes nothing — no agent edits,
    no retries, no messages, no external tasks — and the closed `kind` set is
    how that stays true."""

    kind: str
    text: str | None = None


class FindingGraphicPoint(BaseModel):
    label: str
    metric_ref: str
    value: float | None = None  # always the server's number, never the model's


class FindingGraphic(BaseModel):
    kind: str  # none | run_outcome_split | period_comparison | wait_concentration
    series: list[FindingGraphicPoint] = Field(default_factory=list)


class FindingSummary(BaseModel):
    """One finding as a list shows it."""

    id: int
    category: str  # attention | opportunity | positive_change
    claim_kind: str
    confidence: str  # supported | qualified
    title: str
    explanation: str
    consequence: str | None = None
    entities: list[FindingEntity] = Field(default_factory=list)
    next_step: FindingNextStep | None = None
    graphic: FindingGraphic | None = None
    coverage: dict[str, Any] = Field(default_factory=dict)
    state: str  # open | acknowledged | dismissed | resolved | superseded
    analyzed_at: str | None = None
    evidence_cutoff: str | None = None
    period_start_utc: str | None = None
    period_end_utc: str | None = None
    timezone: str | None = None
    evidence_count: int = 0
    requires_financial: bool = False


class AnalysisStatus(BaseModel):
    """Whether more analysis is coming, and why not when it is not.

    `unavailable` with `no_model_configured` is a real product state: the
    snapshot stays fully usable and analysis is explicitly absent. There is no
    deterministic fallback copy presented as an AI finding.
    """

    # current | queued | running | debounced | incomplete | failed | unavailable
    #
    # `current` means one specific thing: a COMPLETED analysis read the records
    # the reader is looking at now. `incomplete` is its opposite number — the
    # job finished but the analysis did not (an unreadable discovery reply, an
    # expired deadline, a failed retrieval), so what is on screen came from an
    # earlier analysis and this one established nothing.
    state: str
    reason: str | None = None
    # What that last analysis concluded about itself. `complete` covers a real
    # abstention ("we looked, there is nothing"); the unsuccessful values never
    # stand in for one.
    analysis_outcome: str | None = None
    enqueued: bool = False
    job_id: int | None = None
    stale_findings: bool = False
    # True when the findings served alongside this status came from an EARLIER
    # analysis than the one now queued, running, or failed. A refresh in flight
    # must not read as the answer it has not produced yet, and a failed refresh
    # must not read as a successful investigation that found nothing.
    findings_from_previous_analysis: bool = False
    previous_analysis_at: str | None = None
    completed_at: str | None = None
    # How long this audience waits before another investigation may start,
    # whatever evidence arrives meanwhile. Set only on `debounced`.
    debounce_seconds: int | None = None
    # What the record hashes to RIGHT NOW.
    evidence_version: str | None = None
    # What the last completed analysis actually read. When these differ,
    # findings on screen do not cover everything that has arrived — which is
    # acceptable, and is the thing that must be said rather than hidden behind
    # `state: current`.
    analyzed_evidence_version: str | None = None
    newer_evidence_available: bool = False
    # True when this read joined a pending analysis queued under a different
    # scheduling key for the same audience, instead of starting a second one.
    joined_pending_analysis: bool = False
    prompt_version: str | None = None


class FindingsResponse(BaseModel):
    """GET /home/findings. Returns what is authorized and already known,
    promptly; it may enqueue analysis but never waits for it."""

    findings: list[FindingSummary] = Field(default_factory=list)
    analysis: AnalysisStatus
    scope: HomeScope
    generated_at: str


class FindingDetail(BaseModel):
    """GET /home/findings/{id}. What was observed, why it matters, what backs
    it, what cuts against it, what is still unknown, and one supported step."""

    finding: FindingSummary
    claims: list[FindingClaim] = Field(default_factory=list)
    evidence: list[FindingEvidence] = Field(default_factory=list)
    uncertainty: list[str] = Field(default_factory=list)
    stale_evidence: list[FindingEvidence] = Field(default_factory=list)
    navigation: dict[str, Any] = Field(default_factory=dict)
    prompt_version: str | None = None
    model: str | None = None
    evidence_version: str | None = None


class FindingStateUpdate(BaseModel):
    """What a PERSON did with a finding. Deliberately cannot say `resolved` —
    dismissing something is not evidence the condition ended."""

    state: str  # open | acknowledged | dismissed
    reason: str | None = None


class WorkItemHolder(BaseModel):
    kind: str  # human | agent | tool | unassigned
    name: str


class WorkItem(BaseModel):
    """One named-work row for the Monday table. No raw OTel untitled loops,
    no Trovis-generated titles, no Task-from-X shells."""

    id: int
    title: str
    status: str  # waiting_on_you | waiting_on_other | stuck | moving | done
    holder: WorkItemHolder
    whats_next: str
    updated_at: str | None = None
    # loop_events.id of the open decision, when there is one. Home's desk puts
    # Done / I've got this / Not mine ON the row, and without this it would
    # need a detail fetch per row to know whether those buttons are real.
    # Free: the list decorator already resolves it while working out who the
    # work is waiting on (database._decorate_work_items).
    awaiting_handoff_event_id: int | None = None
    # Which KIND of work this is — the declared workflow the matcher claimed
    # it for. Both None means unmatched, which Work home groups as "Other
    # work"; it never means the row is hidden. Read straight off the page's
    # own rows so Work home can group without GET /work/summary, which
    # reuses the entire loop-scanning board to get at the same two fields.
    workflow_id: int | None = None
    workflow_name: str | None = None


class WorkItemsResponse(BaseModel):
    """GET /work/items?cursor=&limit= — paginated named items."""

    items: list[WorkItem] = Field(default_factory=list)
    next_cursor: str | None = None


class WorkSuggestion(BaseModel):
    """One pending suggestion for the Work home strip.

    Never invent a title here — empty list until a real suggestion exists.
    `source` and `draft_holder` are optional.
    """

    id: str
    title: str
    why: str
    source: str | None = None
    draft_holder: WorkItemHolder | None = None


class WorkSuggestionsResponse(BaseModel):
    """GET /work/suggestions — pending rows only. Declined/approved are gone."""

    suggestions: list[WorkSuggestion] = Field(default_factory=list)


class WorkSuggestionPatch(BaseModel):
    """Edit a pending suggestion, or the optional body on approve (edit+approve)."""

    title: str | None = None
    why: str | None = None
    draft_holder: WorkItemHolder | None = None


class WorkSuggestionApproveResponse(BaseModel):
    """POST /work/suggestions/{id}/approve — the named work item now in /work/items."""

    item: WorkItem


class WorkItemProcess(BaseModel):
    id: int
    name: str


class WorkItemActor(BaseModel):
    """Who a step sits with. Reuses the holder vocabulary already on the wire
    (human | agent | tool) — a SaaS destination is a tool named e.g. 'Stripe'.
    No 'saas' kind is invented here."""

    kind: str = "agent"  # human | agent | tool
    name: str = ""


class WorkItemTimelineEntry(BaseModel):
    at: str | None = None
    text: str
    # Present so the detail pane can mark each step Human / Agent / Tool
    # instead of rendering a bare sentence.
    actor: WorkItemActor | None = None


class WorkItemProvenance(BaseModel):
    source: str  # telemetry | suggestion
    suggestion_id: str | None = None


class WorkItemRun(BaseModel):
    """One underlying agent run behind a work item.

    The technical fold of the job pane: what ran, whose it was, whether it
    failed and in one line why, how long, how much. No tokens and no span
    attributes — that depth lives on the agent's own page, and this fold's
    job is to hand someone off to it, not to reproduce it.
    """

    name: str
    # Display label. `service_name` + `agent_id` are the ROUTE to that agent's
    # page; a label has to stay out of URLs (see agentRoute.js).
    agent: str = ""
    service_name: str = ""
    agent_id: str | None = None
    at: str | None = None
    errored: bool = False
    duration_ms: int | None = None
    # None rather than 0.0 when there is no cost to report: a column of
    # $0.00 reads as a measurement rather than an absence.
    cost_usd: float | None = None
    tool: str | None = None
    # Only on a failed run, and only when the run actually said something.
    error: str | None = None


class WorkItemDetail(WorkItem):
    """GET /work/items/{id} — table row plus the v1.1 detail spine.

    `whats_happening` is the current-state sentence (same idea as `whats_next`).
    `process` / `timeline` / `provenance` are present even when empty so the
    FE can render a detail pane without a second fat loop fetch.
    """

    whats_happening: str = ""
    process: WorkItemProcess | None = None
    timeline: list[WorkItemTimelineEntry] = Field(default_factory=list)
    provenance: WorkItemProvenance | None = None
    # loop_events.id of the open handoff, so the pane can Approve / Send back
    # without a second fat fetch. None when nothing is awaiting a decision.
    awaiting_handoff_event_id: int | None = None
    # Only populated for ?include=runs. The collapsed section, never the spine.
    runs: list[WorkItemRun] | None = None


class SaaSConnection(BaseModel):
    """One connected SaaS provider (Stripe / HubSpot / Shopify). Tokens never leave the server."""

    provider: str
    status: str  # connected | disconnected
    provider_account_id: str | None = None
    livemode: bool = False
    connected_at: str | None = None
    updated_at: str | None = None


class SaaSConnectionsResponse(BaseModel):
    connections: list[SaaSConnection] = Field(default_factory=list)
    # True when this deploy can start Stripe Connect OAuth.
    stripe_oauth_configured: bool = False
    # True when this deploy can start HubSpot OAuth.
    hubspot_oauth_configured: bool = False
    # True when this deploy can start Shopify OAuth.
    shopify_oauth_configured: bool = False


class SaaSStripeOAuthStart(BaseModel):
    authorize_url: str


class SaaSHubSpotOAuthStart(BaseModel):
    authorize_url: str


class SaaSShopifyOAuthStart(BaseModel):
    authorize_url: str

