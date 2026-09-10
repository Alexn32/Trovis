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

import {
  DEFAULT_TIMEOUT_MS,
  LLM_TIMEOUT_MS,
  RESTORE_TIMEOUT_MS,
  WORK_TIMEOUT_MS,
  fetchWithTimeout,
  isTimeoutError,
  isUnreachableError,
  unreachableMessage,
} from './httpTimeout.js'

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
  // `timeoutMs` is ours — native fetch would ignore (or in some runtimes
  // reject) an unknown option. Strip it before the wire call.
  const { timeoutMs = DEFAULT_TIMEOUT_MS, headers: extraHeaders, ...fetchOpts } = options
  const headers = { ...(extraHeaders || {}) }
  // Prefer the human session; the API key (agent/legacy) rides along too.
  if (SESSION_TOKEN && !headers['Authorization']) {
    headers['Authorization'] = `Bearer ${SESSION_TOKEN}`
  }
  if (API_KEY) {
    headers['X-Trovis-Api-Key'] = API_KEY
  }
  // Default content-type for POSTs with a body
  if (fetchOpts.body && !headers['Content-Type']) {
    headers['Content-Type'] = 'application/json'
  }
  let res
  try {
    res = await fetchWithTimeout(`${BASE}${path}`, { ...fetchOpts, headers }, timeoutMs)
  } catch (e) {
    // Never leak "Failed to fetch" / AbortError to the UI. Timeout and a
    // dead Railway look the same to the operator: Trovis didn't respond.
    // Login/auth uses "Can't reach Trovis — retry".
    if (isTimeoutError(e) || isUnreachableError(e)) {
      const err = new Error(unreachableMessage(path))
      err.code = isTimeoutError(e) ? 'timeout' : 'network'
      err.status = 0
      throw err
    }
    throw e
  }
  if (!res.ok) {
    let body
    try {
      body = await res.json()
    } catch {
      // fall through to status text
    }
    // FastAPI uses `detail`; our auth middleware uses `error`. `detail` is
    // usually a string, but a structured 409 (handoff-vs-terminal-loop)
    // sends an object — String()ing that yields "[object Object]", so keep
    // the parsed value on the error and only stringify for `message`.
    const detail = body?.detail ?? body?.error
    const msg =
      typeof detail === 'string' && detail
        ? detail
        : `${res.status} ${res.statusText}`
    const err = new Error(msg)
    err.status = res.status
    err.detail = detail
    if (res.status === 504 || res.status === 408) {
      err.code = 'timeout'
      err.name = 'TimeoutError'
    }
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
  listAgents: (opts = {}) => request('/agents', opts),
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
      { timeoutMs: LLM_TIMEOUT_MS },
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
      { method: 'POST', timeoutMs: LLM_TIMEOUT_MS },
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
      { timeoutMs: LLM_TIMEOUT_MS },
    ),
  // Capability map. Three lists (reads_from / writes_to / can_do).
  // Cached for 24 hours.
  getAgentCapabilities: (name, agentId) =>
    request(
      _withAgent(
        `/agents/${encodeURIComponent(name)}/capabilities`,
        agentId,
      ),
      { timeoutMs: LLM_TIMEOUT_MS },
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
  // A workflow is a named, VERSIONED declaration of a recurring process:
  // ordered stations (who holds the work at each step) + match hints (how a
  // loop is recognized as an instance). Definitions are append-only.
  getWorkflows: (opts = {}) => request('/workflows', opts),
  getWorkflow: (id, opts = {}) => request(`/workflows/${id}`, opts),
  // Live station map: where every non-terminal matched loop currently sits.
  getWorkflowMap: (id) => request(`/workflows/${id}/map`),
  getWorkflowLoops: (id, state = null) =>
    request(`/workflows/${id}/loops${state ? `?state=${encodeURIComponent(state)}` : ''}`),
  createWorkflow: (data) =>
    request('/workflows', { method: 'POST', body: JSON.stringify(data) }),
  // Every edit is a new version (full definition, not a diff).
  createWorkflowVersion: (id, data) =>
    request(`/workflows/${id}/versions`, { method: 'POST', body: JSON.stringify(data) }),
  archiveWorkflow: (id) =>
    request(`/workflows/${id}/archive`, { method: 'POST' }),
  // Draft a declaration from plain English. Returns { name, stations,
  // match_hints } for the editor to prefill — nothing is persisted until
  // the operator saves through createWorkflow.
  draftWorkflow: (description) =>
    request('/workflows/draft', {
      method: 'POST',
      body: JSON.stringify({ description }),
      timeoutMs: LLM_TIMEOUT_MS,
    }),

  // --- agent ownership ---
  // Who is responsible for an agent. An org member, by user_id — this is
  // also one of the two things Whose work attributes a row by (the other
  // is who the work is waiting on), so an unowned agent's work belongs to
  // nobody in particular and only a company-breadth seat sees it.
  setAgentOwner: (serviceName, { agentId = 'main', userId }) =>
    request(`/agents/${encodeURIComponent(serviceName)}/owner`, {
      method: 'PUT',
      body: JSON.stringify({ agent_id: agentId || 'main', user_id: userId }),
    }),
  removeAgentOwner: (serviceName, agentId = 'main') =>
    request(
      _withAgent(`/agents/${encodeURIComponent(serviceName)}/owner`, agentId || 'main'),
      { method: 'DELETE' },
    ),

  // --- the legacy `team_members` directory ---
  // No client calls these any more, so the wrappers are gone. People, roles
  // and invites live on the Org page (/org/*), and nothing in the product
  // creates a team_members row — that parallel directory was the second
  // source of truth this ship closed. The SERVER still reads it:
  // agent_owners.team_member_id, and the name shown on a handoff to a human,
  // both resolve through it. Re-pointing those at `users` is a backend
  // migration, not a UI change, so the endpoints stay and these do not.

  // --- ask ---
  // messages is the full chat thread; backend is stateless. Returns
  // { answer: string }.
  ask: (messages) =>
    request('/ask', {
      method: 'POST',
      body: JSON.stringify({ messages }),
      timeoutMs: LLM_TIMEOUT_MS,
    }),
  askAboutAgent: (name, messages, agentId) =>
    request(
      _withAgent(`/agents/${encodeURIComponent(name)}/ask`, agentId),
      {
        method: 'POST',
        body: JSON.stringify({ messages }),
        timeoutMs: LLM_TIMEOUT_MS,
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

  // SaaS Connect (Stripe / HubSpot / Shopify Work adapters — not Trovis billing).
  getSaasConnections: () => request('/saas/connections'),
  startStripeConnect: () => request('/saas/stripe/oauth/start'),
  disconnectStripe: () => request('/saas/stripe', { method: 'DELETE' }),
  startHubSpotConnect: () => request('/saas/hubspot/oauth/start'),
  disconnectHubSpot: () => request('/saas/hubspot', { method: 'DELETE' }),
  startShopifyConnect: (shop) =>
    request(`/saas/shopify/oauth/start?shop=${encodeURIComponent(shop || '')}`),
  disconnectShopify: () => request('/saas/shopify', { method: 'DELETE' }),

  // --- proactive alerts (Settings → Alerts) ---
  getAlerts: () => request('/account/alerts'),
  updateAlerts: (patch) =>
    request('/account/alerts', { method: 'PUT', body: JSON.stringify(patch) }),
  testAlert: () => request('/account/alerts/test', { method: 'POST' }),

  // --- dashboard (daily briefing) ---
  // Optional `{ signal }` so Dashboard unmount / tab switch can abort
  // in-flight Claude calls instead of waiting out LLM_TIMEOUT (120s).
  //
  // `localHour` / `timeZone` are the READER's clock (home.viewerClock). The
  // model writes this text, and a server-side UTC hour is how an opener ends
  // up calling someone's late afternoon "a quiet morning". Both optional: a
  // caller that omits them gets the same briefing as before.
  getBriefing: ({ localHour, timeZone, ...opts } = {}) => {
    const qs = new URLSearchParams()
    if (Number.isInteger(localHour)) qs.set('local_hour', String(localHour))
    if (timeZone) qs.set('tz', timeZone)
    const query = qs.toString()
    return request(`/dashboard/briefing${query ? `?${query}` : ''}`, {
      timeoutMs: LLM_TIMEOUT_MS,
      ...opts,
    })
  },
  // Home's fleet-pulse sentence. The packet is assembled client-side from
  // data already on the page, so this adds no read to the database. The
  // server waits only a short budget for the model and otherwise answers with
  // an empty insight, so this never holds up a paint.
  getPulseInsight: (packet, opts = {}) =>
    request('/dashboard/pulse-insight', {
      method: 'POST',
      body: JSON.stringify({ packet }),
      ...opts,
    }),
  getAttention: (opts = {}) => request('/dashboard/attention', opts),
  getCost: (opts = {}) => request('/dashboard/cost', opts),
  getWorkFeed: (opts = {}) => request('/dashboard/work-feed', opts),
  // Chronological, fleet-wide activity stream for the Work Feed page.
  getActivity: (hours = 24, limit = 200) =>
    request(`/dashboard/activity?hours=${hours}&limit=${limit}`),
  // --- workloops (units of work derived from the event stream) ---
  // `assignee: 'me'` narrows to loops whose unresolved handoff targets the
  // signed-in user. Session auth only — an api key has no "me".
  getLoops: (state = null, limit = 50, offset = 0, assignee = null) =>
    request(
      `/loops?limit=${limit}&offset=${offset}` +
        `${state ? `&state=${encodeURIComponent(state)}` : ''}` +
        `${assignee ? `&assignee=${encodeURIComponent(assignee)}` : ''}`,
    ),
  // The whole Work board in one request: open work + today's finished work,
  // already bucketed, sorted, and with holders resolved server-side.
  // FAT — do not call from home / Work L1. Use getWorkOverview + getWorkItems.
  getWorkBoard: (workflowId = null) =>
    request(
      `/work/board${workflowId ? `?workflow_id=${encodeURIComponent(workflowId)}` : ''}`,
      { timeoutMs: WORK_TIMEOUT_MS },
    ),
  // Legacy Level-1 rollup. FAT (loop-scans get_work_board). Home must not
  // call this — Frontend wires the Monday table against overview + items.
  getWorkSummary: () => request('/work/summary', { timeoutMs: WORK_TIMEOUT_MS }),
  // Lean Work home. Counts only: needs_you, needs_attention, open, completed_week.
  // Optional `{ signal }` so Home can abort on unmount / tab switch.
  // `whose` / `personId` are the Whose-work selection. The server resolves
  // them against the seat and clamps anything wider — the control is a
  // courtesy, not the enforcement.
  getWorkOverview: ({ whose = null, personId = null, ...opts } = {}) => {
    const q = new URLSearchParams()
    if (whose && whose !== 'everyone') q.set('whose', whose)
    if (personId) q.set('person_id', String(personId))
    const qs = q.toString()
    return request(`/work/overview${qs ? `?${qs}` : ''}`, {
      timeoutMs: WORK_TIMEOUT_MS,
      ...opts,
    })
  },
  // Paginated named items for the Monday table. cursor from the previous
  // page's next_cursor. Untitled OTel loops are excluded.
  // `signal` is ours (Home aborts it); the rest of the object is query state.
  // `workflowId` narrows to one kind of work ('none' = the undeclared ones)
  // and `status: 'done'` to work that has closed. Both are column filters on
  // the same lean scan — the Work kind page uses them instead of reaching for
  // the board.
  getWorkItems: ({
    cursor = null, limit = 50, workflowId = null, status = null,
    whose = null, personId = null, signal = undefined,
  } = {}) => {
    const q = new URLSearchParams()
    if (limit) q.set('limit', String(limit))
    if (cursor) q.set('cursor', cursor)
    if (workflowId !== null && workflowId !== undefined) q.set('workflow_id', String(workflowId))
    if (status) q.set('status', status)
    // Omitted for the default: the server already applies the seat's own
    // breadth, so sending "everyone" would only make the URL noisier.
    if (whose && whose !== 'everyone') q.set('whose', whose)
    if (personId) q.set('person_id', String(personId))
    const qs = q.toString()
    return request(`/work/items${qs ? `?${qs}` : ''}`, {
      timeoutMs: WORK_TIMEOUT_MS,
      ...(signal ? { signal } : {}),
    })
  },
  // Pending suggestions for the home strip. Empty until a generator inserts
  // rows — never invent titles. Shape: { suggestions: [{ id, title, why, source?, draft_holder? }] }
  getWorkSuggestions: () => request('/work/suggestions', { timeoutMs: WORK_TIMEOUT_MS }),
  // One named item plus the detail spine. `include: 'runs'` adds the
  // underlying agent runs — opt-in, because the job detail folds them away and
  // the default read must not pay for them.
  getWorkItem: (id, { include = null, signal = undefined } = {}) => {
    const qs = include ? `?include=${encodeURIComponent(include)}` : ''
    return request(`/work/items/${encodeURIComponent(id)}${qs}`, {
      timeoutMs: WORK_TIMEOUT_MS,
      ...(signal ? { signal } : {}),
    })
  },
  // Approve → named work item `{ item }` in /work/items. Optional body is
  // edit-then-approve: { title?, why?, draft_holder? }. Garbage titles 400.
  approveWorkSuggestion: (id, patch = null) =>
    request(`/work/suggestions/${encodeURIComponent(id)}/approve`, {
      method: 'POST',
      body: JSON.stringify(patch || {}),
      timeoutMs: WORK_TIMEOUT_MS,
    }),
  // Edit a pending suggestion in place (still in the strip).
  editWorkSuggestion: (id, patch) =>
    request(`/work/suggestions/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      body: JSON.stringify(patch || {}),
      timeoutMs: WORK_TIMEOUT_MS,
    }),
  // Remove from the strip. 204. Does not create a work item.
  declineWorkSuggestion: (id) =>
    request(`/work/suggestions/${encodeURIComponent(id)}/decline`, {
      method: 'POST',
      timeoutMs: WORK_TIMEOUT_MS,
    }),
  // Loops needing a human — stalled or waiting on you, oldest first.
  getStalledLoops: (limit = 50) => request(`/loops/stalled?limit=${limit}`),
  getLoop: (loopId) => request(`/loops/${loopId}`, { timeoutMs: WORK_TIMEOUT_MS }),
  // Session auth only (the backend 403s api-key auth). Idempotent.
  closeLoop: (loopId) => request(`/loops/${loopId}/close`, { method: 'POST' }),
  // --- handoff resolution (the human half of a workloop) ---
  // handoffEventId is loop_events.id, served as awaiting_handoff_event_id on
  // any loop summary/detail. All three are session-only and idempotent;
  // they 409 with {state, closed_at, close_reason} when the loop is already
  // terminal (see handoffTerminalMessage in loops.js).
  acceptHandoff: (loopId, handoffEventId) =>
    request(`/loops/${loopId}/handoffs/${handoffEventId}/accept`, { method: 'POST' }),
  completeHandoff: (loopId, handoffEventId) =>
    request(`/loops/${loopId}/handoffs/${handoffEventId}/complete`, { method: 'POST' }),
  declineHandoff: (loopId, handoffEventId, reason = null) =>
    request(`/loops/${loopId}/handoffs/${handoffEventId}/decline`, {
      method: 'POST',
      body: JSON.stringify(reason ? { reason } : {}),
    }),
  // --- dedicated cost page ---
  // `days` is the trend window (7–90) the chart is showing; the budget writes
  // below echo it so the returned overview keeps the same series.
  getCostOverview: (days = 30) => request(`/cost/overview?days=${days}`),
  // Per-day / per-model cost audit — surfaces tokens that landed unpriced
  // (cost undercounted) so a pricing/capture gap is visible, not silent.
  getCostAudit: (service, days = 30) =>
    request(
      `/cost/audit?days=${days}${service ? `&service=${encodeURIComponent(service)}` : ''}`,
    ),
  setBudget: (monthlyBudget, days = 30) =>
    request(`/cost/budget?days=${days}`, {
      method: 'PUT',
      body: JSON.stringify({ monthly_budget: monthlyBudget }),
    }),
  setAgentBudget: (serviceName, agentId, monthlyCap, days = 30) =>
    request(`/cost/agent-budget?days=${days}`, {
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
      timeoutMs: LLM_TIMEOUT_MS,
    }),

  // Guided add-agent chat ("Set up with AI"). Returns { answer, options, code }.
  askConnect: (messages) =>
    request('/connect/ask', {
      method: 'POST',
      body: JSON.stringify({ messages }),
      timeoutMs: LLM_TIMEOUT_MS,
    }),

  // --- auth (real users + orgs) ---
  signup: (data) =>
    request('/auth/signup', { method: 'POST', body: JSON.stringify(data) }),
  login: (data) =>
    request('/auth/login', { method: 'POST', body: JSON.stringify(data) }),
  // keepalive: the POST can finish after we navigate away. Logout itself
  // must never await hung Dashboard GETs — see performLogout.
  logout: () =>
    request('/auth/logout', { method: 'POST', keepalive: true }),
  me: () => request('/auth/me', { timeoutMs: RESTORE_TIMEOUT_MS }),
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

  // --- org chart (roles, reporting lines, seats) ---
  // The chart comes back already filtered to what this caller may see, and
  // each role carries can_edit / can_add_child. Those flags are for greying
  // out controls only — every write is re-authorized server-side.
  getOrgChart: () => request('/org/chart'),
  getScopeLevels: () => request('/org/scope-levels'),
  createScopeLevel: (data) =>
    request('/org/scope-levels', { method: 'POST', body: JSON.stringify(data) }),
  createRole: (data) =>
    request('/org/roles', { method: 'POST', body: JSON.stringify(data) }),
  // Only the keys present in `data` are changed; pass an explicit null to
  // detach a parent or a scope level.
  updateRole: (id, data) =>
    request(`/org/roles/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
  deleteRole: (id) => request(`/org/roles/${id}`, { method: 'DELETE' }),
  addRoleMember: (roleId, userId) =>
    request(`/org/roles/${roleId}/members`, {
      method: 'POST',
      body: JSON.stringify({ user_id: userId }),
    }),
  removeRoleMember: (roleId, userId) =>
    request(`/org/roles/${roleId}/members/${userId}`, { method: 'DELETE' }),
  setOrgBuilder: (userId, orgBuilder) =>
    request(`/org/members/${userId}/org-builder`, {
      method: 'PUT',
      body: JSON.stringify({ org_builder: orgBuilder }),
    }),
  // Path A → Path B: a one-seat workspace becomes a company. Keeps agents,
  // jobs and API keys; idempotent.
  graduateOrg: (data = {}) =>
    request('/org/graduate', { method: 'POST', body: JSON.stringify(data) }),
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
      timeoutMs: LLM_TIMEOUT_MS,
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
  // Hard-timeout: a hung Railway/API must abort, not pin `restoring`.
  async validateSession() {
    try {
      return await request('/auth/me', { timeoutMs: RESTORE_TIMEOUT_MS })
    } catch (e) {
      if (e.status === 401) return null
      throw e
    }
  },
}
