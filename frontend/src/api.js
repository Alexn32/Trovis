// Thin wrapper around the Trovis REST API. The base URL is configurable
// at build time via VITE_API_URL so the same bundle can point at local
// dev, staging, or a customer demo deployment.
//
// The API key lives in three places, in priority order:
//   1. module-level `API_KEY` — the live value used on every request
//   2. localStorage `trovis_api_key` — persisted across reloads, written
//      by setApiKey / cleared by clearApiKey (legacy `oversee_api_key` is
//      migrated to it on boot by migrate.js)
//   3. VITE_TROVIS_API_KEY env var (legacy VITE_OVERSEE_API_KEY) — build-time
//      seed for staging deploys where every visitor uses the same demo key
//
// On module load we pick (2) if present, else (3), else null. App.jsx
// then validates the chosen key on mount and falls back to the login
// screen on 401.

const BASE = import.meta.env.VITE_API_URL || 'http://localhost:8080'
const LS_KEY = 'trovis_api_key'
const LS_TOKEN = 'trovis_session_token'

function _readKeyFromStorage() {
  try {
    return localStorage.getItem(LS_KEY)
  } catch {
    // localStorage can throw in some sandboxed iframes / private modes.
    return null
  }
}

function _writeKeyToStorage(key) {
  try {
    if (key) {
      localStorage.setItem(LS_KEY, key)
    } else {
      localStorage.removeItem(LS_KEY)
    }
  } catch {
    // Quota exceeded, blocked, etc. — silently degrade. The in-memory
    // module state still works; the user just has to re-login on reload.
  }
}

let API_KEY =
  _readKeyFromStorage() ||
  import.meta.env.VITE_TROVIS_API_KEY ||
  import.meta.env.VITE_OVERSEE_API_KEY ||
  null

export function setApiKey(key) {
  API_KEY = key || null
  _writeKeyToStorage(API_KEY)
}

export function clearApiKey() {
  API_KEY = null
  _writeKeyToStorage(null)
}

export function getApiKey() {
  return API_KEY
}

// Session token (human dashboard login). Stored alongside the API key; the
// backend prefers the Bearer session when both are present.
let SESSION_TOKEN = (() => {
  try {
    return localStorage.getItem(LS_TOKEN)
  } catch {
    return null
  }
})()

export function setSessionToken(token) {
  SESSION_TOKEN = token || null
  try {
    if (SESSION_TOKEN) localStorage.setItem(LS_TOKEN, SESSION_TOKEN)
    else localStorage.removeItem(LS_TOKEN)
  } catch {
    // private mode / quota — in-memory still works this session.
  }
}

export function getSessionToken() {
  return SESSION_TOKEN
}

export function clearSessionToken() {
  setSessionToken(null)
}

async function request(path, options = {}) {
  const headers = { ...(options.headers || {}) }
  // Prefer the human session; the API key (agent/legacy) rides along too.
  if (SESSION_TOKEN && !headers['Authorization']) {
    headers['Authorization'] = `Bearer ${SESSION_TOKEN}`
  }
  if (API_KEY) {
    headers['X-Trovis-Api-Key'] = API_KEY
  }
  // Default content-type for POSTs with a body
  if (options.body && !headers['Content-Type']) {
    headers['Content-Type'] = 'application/json'
  }
  const res = await fetch(`${BASE}${path}`, { ...options, headers })
  if (!res.ok) {
    let body
    try {
      body = await res.json()
    } catch {
      // fall through to status text
    }
    // FastAPI uses `detail`; our auth middleware uses `error`.
    const msg = body?.detail || body?.error || `${res.status} ${res.statusText}`
    const err = new Error(msg)
    err.status = res.status
    throw err
  }
  // 204 No Content and other empty-body responses return null —
  // res.json() would throw on an empty body.
  if (res.status === 204 || res.headers.get('content-length') === '0') {
    return null
  }
  return res.json()
}

// Append `?agent_id=…` to a URL when the caller is scoped to a sub-agent.
// Multi-agent OpenClaw instances ship many distinct `agent_id`s under one
// `service.name`; without the filter every per-instance endpoint returns
// the aggregate.
function _withAgent(path, agentId) {
  if (!agentId) return path
  const sep = path.includes('?') ? '&' : '?'
  return `${path}${sep}agent_id=${encodeURIComponent(agentId)}`
}

export const api = {
  // --- data ---
  listAgents: () => request('/agents'),
  getAgentSummary: (name, agentId) =>
    request(_withAgent(`/agents/${encodeURIComponent(name)}/summary`, agentId)),
  // Drift verdict (declared identity vs. observed behavior). Cached server-side;
  // pass refresh=true to force a re-check.
  getDrift: (name, agentId, refresh = false) =>
    request(
      _withAgent(
        `/agents/${encodeURIComponent(name)}/drift${refresh ? '?refresh=true' : ''}`,
        agentId,
      ),
    ),
  getAgentSpans: (name, limit = 50, agentId) =>
    request(
      _withAgent(
        `/agents/${encodeURIComponent(name)}/spans?limit=${limit}`,
        agentId,
      ),
    ),
  describeAgent: (name, agentId) =>
    request(
      _withAgent(`/agents/${encodeURIComponent(name)}/describe`, agentId),
      { method: 'POST' },
    ),
  // Registration is optional — 404 is a normal "no registration yet"
  // result, so callers should accept null gracefully.
  async getAgentRegistration(name, agentId) {
    try {
      return await request(
        _withAgent(`/agents/${encodeURIComponent(name)}/registration`, agentId),
      )
    } catch (e) {
      if (e.status === 404) return null
      throw e
    }
  },
  // Captured outputs (only populated when the plugin had captureOutputs
  // enabled at emit time). Returns [] when nothing's been captured.
  getAgentOutputs: (name, limit = 20, agentId) =>
    request(
      _withAgent(
        `/agents/${encodeURIComponent(name)}/outputs?limit=${limit}`,
        agentId,
      ),
    ),
  // Weekly summary: stats + Claude-generated paragraph. The
  // paragraph is cached server-side for 1 hour; the stats are
  // always fresh. `summary_unavailable: true` when ANTHROPIC_API_KEY
  // is missing — the stats still come through.
  getWeeklySummary: (name, agentId) =>
    request(
      _withAgent(`/agents/${encodeURIComponent(name)}/weekly`, agentId),
    ),
  // Capability map. Three lists (reads_from / writes_to / can_do).
  // Cached for 24 hours.
  getAgentCapabilities: (name, agentId) =>
    request(
      _withAgent(
        `/agents/${encodeURIComponent(name)}/capabilities`,
        agentId,
      ),
    ),
  // Token usage + estimated cost over the last `days` days, with
  // per-day and per-model breakdowns.
  getAgentCosts: (name, agentId, days = 7) => {
    const base = `/agents/${encodeURIComponent(name)}/costs?days=${days}`
    return request(_withAgent(base, agentId))
  },
  // Hard-delete an agent. With agentId set, scopes to one sub-agent;
  // without, drops the whole service_name. Returns the delete summary.
  deleteAgent(name, agentId) {
    return request(
      _withAgent(`/agents/${encodeURIComponent(name)}`, agentId),
      { method: 'DELETE' },
    )
  },

  // Operator-set human-readable label for one sub-agent. Empty
  // displayName clears the override. Returns no body (204) on success.
  setDisplayName(name, agentId, displayName) {
    return request(
      `/agents/${encodeURIComponent(name)}/display-name`,
      {
        method: 'PUT',
        body: JSON.stringify({
          agent_id: agentId || 'main',
          display_name: displayName ?? '',
        }),
      },
    )
  },

  // --- workflows ---
  // Named, ordered process flows (agent + human steps), auto-generated
  // from telemetry + identity and operator-editable.
  getWorkflows: () => request('/workflows'),
  getWorkflow: (id) => request(`/workflows/${id}`),
  // Live telemetry stats for a workflow's source agent.
  getWorkflowStats: (id) => request(`/workflows/${id}/stats`),
  createWorkflow: (data) =>
    request('/workflows', { method: 'POST', body: JSON.stringify(data) }),
  updateWorkflow: (id, data) =>
    request(`/workflows/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteWorkflow: (id) => request(`/workflows/${id}`, { method: 'DELETE' }),
  // Auto-build a workflow from one agent's telemetry. data:
  // { name, agent_service_name, agent_id }. Returns the full workflow.
  // Single-agent (legacy) OR multi-agent: { method:'agents', agents:[...], human_roles:[...] }.
  generateWorkflow: (data) =>
    request('/workflows/generate', { method: 'POST', body: JSON.stringify(data) }),
  // AI builds a full multi-agent graph (participants + steps + edges + positions).
  describeWorkflow: (data) =>
    request('/workflows/describe', { method: 'POST', body: JSON.stringify(data) }),
  // Legacy: draft a vertical step list from a description.
  createWorkflowFromDescription: (data) =>
    request('/workflows/from-description', { method: 'POST', body: JSON.stringify(data) }),
  // Drag-to-reposition a node on the canvas.
  updateStepPosition: (workflowId, stepId, pos) =>
    request(`/workflows/${workflowId}/steps/${stepId}/position`, {
      method: 'PUT',
      body: JSON.stringify(pos),
    }),
  addWorkflowStep: (workflowId, data) =>
    request(`/workflows/${workflowId}/steps`, {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  updateWorkflowStep: (workflowId, stepId, data) =>
    request(`/workflows/${workflowId}/steps/${stepId}`, {
      method: 'PUT',
      body: JSON.stringify(data),
    }),
  deleteWorkflowStep: (workflowId, stepId) =>
    request(`/workflows/${workflowId}/steps/${stepId}`, { method: 'DELETE' }),
  reorderWorkflowSteps: (workflowId, stepIds) =>
    request(`/workflows/${workflowId}/steps/reorder`, {
      method: 'POST',
      body: JSON.stringify({ step_ids: stepIds }),
    }),
  // --- workflow graph editing (manual editor) ---
  // Create an edge. data: { from_step_id, to_step_id, label?, is_branch? }.
  // A backward edge (target before source in flow order) is a loop.
  addWorkflowEdge: (workflowId, data) =>
    request(`/workflows/${workflowId}/edges`, {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  // Patch an edge's label / is_branch.
  updateWorkflowEdge: (workflowId, edgeId, data) =>
    request(`/workflows/${workflowId}/edges/${edgeId}`, {
      method: 'PUT',
      body: JSON.stringify(data),
    }),
  deleteWorkflowEdge: (workflowId, edgeId) =>
    request(`/workflows/${workflowId}/edges/${edgeId}`, { method: 'DELETE' }),
  // Add an agent or human role to the workflow roster. data:
  // { type:'agent'|'human', agent_service_name?, agent_id?, role_name? }.
  addWorkflowParticipant: (workflowId, data) =>
    request(`/workflows/${workflowId}/participants`, {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  deleteWorkflowParticipant: (workflowId, participantId) =>
    request(`/workflows/${workflowId}/participants/${participantId}`, {
      method: 'DELETE',
    }),
  // Apply a plain-English edit instruction to a workflow. Returns
  // { summary, applied, workflow } — the workflow is the full updated graph.
  aiEditWorkflow: (workflowId, instruction) =>
    request(`/workflows/${workflowId}/ai-edit`, {
      method: 'POST',
      body: JSON.stringify({ instruction }),
    }),

  // --- team + ownership ---
  getTeamMembers: () => request('/team'),
  createTeamMember: (data) =>
    request('/team', { method: 'POST', body: JSON.stringify(data) }),
  deleteTeamMember: (id) =>
    request(`/team/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  // Agents owned by one team member. Returns [] when none assigned.
  getTeamMemberAgents: (id) =>
    request(`/team/${encodeURIComponent(id)}/agents`),
  // Assign a team member as the human owner of one sub-agent. The data
  // payload is `{ agent_id, team_member_id }`. Returns null (204).
  setAgentOwner: (serviceName, data) =>
    request(`/agents/${encodeURIComponent(serviceName)}/owner`, {
      method: 'PUT',
      body: JSON.stringify({
        agent_id: data.agent_id || 'main',
        team_member_id: data.team_member_id,
      }),
    }),
  removeAgentOwner: (serviceName, agentId) =>
    request(
      _withAgent(
        `/agents/${encodeURIComponent(serviceName)}/owner`,
        agentId || 'main',
      ),
      { method: 'DELETE' },
    ),

  // --- ask ---
  // messages is the full chat thread; backend is stateless. Returns
  // { answer: string }.
  ask: (messages) =>
    request('/ask', {
      method: 'POST',
      body: JSON.stringify({ messages }),
    }),
  askAboutAgent: (name, messages, agentId) =>
    request(
      _withAgent(`/agents/${encodeURIComponent(name)}/ask`, agentId),
      {
        method: 'POST',
        body: JSON.stringify({ messages }),
      },
    ),

  // Work Feed: trace-grouped interaction records, newest first.
  // Returns { records: [...], next_cursor }. Pass next_cursor back as `cursor`.
  getAgentRecords: (name, { limit = 20, cursor = null, agentId = null } = {}) => {
    let path = `/agents/${encodeURIComponent(name)}/records?limit=${limit}`
    if (cursor) path += `&cursor=${encodeURIComponent(cursor)}`
    return request(_withAgent(path, agentId))
  },

  // Plan usage for the Fleet header + upgrade prompts:
  // { plan, agent_count, agent_limit (null=unlimited), locked_count }.
  getAccountUsage: () => request('/account/usage'),
  // Request a plan change. Paid tiers return { status:'checkout_required',
  // checkout_url } — redirect the browser there; the plan only flips after the
  // Stripe webhook confirms payment. A no-op/downgrade returns { status:'applied' }.
  setPlan: (plan, cycle = 'monthly') =>
    request('/account/plan', { method: 'PUT', body: JSON.stringify({ plan, cycle }) }),
  // Opens a Stripe Customer Portal session → { portal_url }. 400 when the
  // account has never subscribed (no Stripe customer yet).
  billingPortal: () => request('/account/billing-portal', { method: 'POST' }),

  // --- proactive alerts (Settings → Alerts) ---
  getAlerts: () => request('/account/alerts'),
  updateAlerts: (patch) =>
    request('/account/alerts', { method: 'PUT', body: JSON.stringify(patch) }),
  testAlert: () => request('/account/alerts/test', { method: 'POST' }),

  // --- dashboard (daily briefing) ---
  getBriefing: () => request('/dashboard/briefing'),
  getAttention: () => request('/dashboard/attention'),
  getCost: () => request('/dashboard/cost'),
  getWorkFeed: () => request('/dashboard/work-feed'),
  // Chronological, fleet-wide activity stream for the Work Feed page.
  getActivity: (hours = 24, limit = 200) =>
    request(`/dashboard/activity?hours=${hours}&limit=${limit}`),
  // --- workloops (units of work derived from the event stream) ---
  getLoops: (state = null, limit = 50, offset = 0) =>
    request(
      `/loops?limit=${limit}&offset=${offset}${state ? `&state=${encodeURIComponent(state)}` : ''}`,
    ),
  // Loops needing a human — stalled or waiting on you, oldest first.
  getStalledLoops: (limit = 50) => request(`/loops/stalled?limit=${limit}`),
  getLoop: (loopId) => request(`/loops/${loopId}`),
  // Session auth only (the backend 403s api-key auth). Idempotent.
  closeLoop: (loopId) => request(`/loops/${loopId}/close`, { method: 'POST' }),
  // --- dedicated cost page ---
  getCostOverview: () => request('/cost/overview'),
  // Per-day / per-model cost audit — surfaces tokens that landed unpriced
  // (cost undercounted) so a pricing/capture gap is visible, not silent.
  getCostAudit: (service, days = 30) =>
    request(
      `/cost/audit?days=${days}${service ? `&service=${encodeURIComponent(service)}` : ''}`,
    ),
  setBudget: (monthlyBudget) =>
    request('/cost/budget', {
      method: 'PUT',
      body: JSON.stringify({ monthly_budget: monthlyBudget }),
    }),
  setAgentBudget: (serviceName, agentId, monthlyCap) =>
    request('/cost/agent-budget', {
      method: 'PUT',
      body: JSON.stringify({
        service_name: serviceName,
        agent_id: agentId || 'main',
        monthly_cap: monthlyCap,
      }),
    }),
  // Concise fleet Q&A for the floating Ask pill. Returns { answer }.
  askDashboard: (messages) =>
    request('/dashboard/ask', {
      method: 'POST',
      body: JSON.stringify({ messages }),
    }),

  // Guided add-agent chat ("Set up with AI"). Returns { answer, options, code }.
  askConnect: (messages) =>
    request('/connect/ask', {
      method: 'POST',
      body: JSON.stringify({ messages }),
    }),

  // --- auth (real users + orgs) ---
  signup: (data) =>
    request('/auth/signup', { method: 'POST', body: JSON.stringify(data) }),
  login: (data) =>
    request('/auth/login', { method: 'POST', body: JSON.stringify(data) }),
  logout: () => request('/auth/logout', { method: 'POST' }),
  me: () => request('/auth/me'),
  claim: (data) =>
    request('/auth/claim', { method: 'POST', body: JSON.stringify(data) }),
  setPassword: (data) =>
    request('/auth/set-password', { method: 'POST', body: JSON.stringify(data) }),
  forgotPassword: (email) =>
    request('/auth/forgot-password', { method: 'POST', body: JSON.stringify({ email }) }),
  resetPassword: (token, new_password) =>
    request('/auth/reset-password', { method: 'POST', body: JSON.stringify({ token, new_password }) }),
  acceptInvite: (data) =>
    request('/auth/accept-invite', { method: 'POST', body: JSON.stringify(data) }),

  // --- organization (members + invites) ---
  getOrg: () => request('/org'),
  updateOrg: (data) => request('/org', { method: 'PUT', body: JSON.stringify(data) }),
  // Onboarding: mark the post-signup wizard done (idempotent).
  completeOnboarding: () =>
    request('/auth/onboarding/complete', { method: 'POST' }),
  getMembers: () => request('/org/members'),
  createInvite: (data) =>
    request('/org/invites', { method: 'POST', body: JSON.stringify(data) }),
  getInvites: () => request('/org/invites'),
  revokeInvite: (id) => request(`/org/invites/${id}`, { method: 'DELETE' }),
  deleteMember: (id) => request(`/org/members/${id}`, { method: 'DELETE' }),
  // Re-show the org's API key(s) — owner only, requires the caller's password.
  // No-password key fetch (user is already authenticated).
  getApiKeys: () => request('/org/api-keys'),
  // Password-gated reveal (Settings page, step-up auth).
  revealApiKeys: (password) =>
    request('/org/api-keys/reveal', {
      method: 'POST',
      body: JSON.stringify({ password }),
    }),

  // --- agent-to-agent connections ---
  getConnections: () => request('/connections'),
  detectConnections: () => request('/connections/detect', { method: 'POST' }),
  // AI builder: propose agent→agent connections from a description.
  proposeConnections: (description) =>
    request('/connections/from-description', {
      method: 'POST',
      body: JSON.stringify({ description }),
    }),
  addConnection: (data) =>
    request('/connections', { method: 'POST', body: JSON.stringify(data) }),
  updateConnection: (id, status) =>
    request(`/connections/${id}`, {
      method: 'PATCH',
      body: JSON.stringify({ status }),
    }),
  deleteConnection: (id) => request(`/connections/${id}`, { method: 'DELETE' }),

  // Validate the current credential (session or API key) via /auth/me.
  // Returns the {user, org, auth} payload on success, null on 401.
  async validateSession() {
    try {
      return await request('/auth/me')
    } catch (e) {
      if (e.status === 401) return null
      throw e
    }
  },
}
