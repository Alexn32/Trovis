// Thin wrapper around the Oversee REST API. The base URL is configurable
// at build time via VITE_API_URL so the same bundle can point at local
// dev, staging, or a customer demo deployment.
//
// The API key lives in three places, in priority order:
//   1. module-level `API_KEY` — the live value used on every request
//   2. localStorage `oversee_api_key` — persisted across reloads, written
//      by setApiKey / cleared by clearApiKey
//   3. VITE_OVERSEE_API_KEY env var — build-time seed for staging deploys
//      where every visitor uses the same demo key
//
// On module load we pick (2) if present, else (3), else null. App.jsx
// then validates the chosen key on mount and falls back to the login
// screen on 401.

const BASE = import.meta.env.VITE_API_URL || 'http://localhost:8080'
const LS_KEY = 'oversee_api_key'
const LS_TOKEN = 'oversee_session_token'

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
    headers['X-Oversee-Api-Key'] = API_KEY
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
  generateWorkflow: (data) =>
    request('/workflows/generate', { method: 'POST', body: JSON.stringify(data) }),
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
  acceptInvite: (data) =>
    request('/auth/accept-invite', { method: 'POST', body: JSON.stringify(data) }),

  // --- organization (members + invites) ---
  getOrg: () => request('/org'),
  getMembers: () => request('/org/members'),
  createInvite: (data) =>
    request('/org/invites', { method: 'POST', body: JSON.stringify(data) }),
  getInvites: () => request('/org/invites'),
  revokeInvite: (id) => request(`/org/invites/${id}`, { method: 'DELETE' }),
  deleteMember: (id) => request(`/org/members/${id}`, { method: 'DELETE' }),
  // Re-show the org's API key(s) — owner only, requires the caller's password.
  revealApiKeys: (password) =>
    request('/org/api-keys/reveal', {
      method: 'POST',
      body: JSON.stringify({ password }),
    }),

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
