// Thin wrapper around the Oversee REST API. The base URL is configurable
// at build time via VITE_API_URL so the same bundle can point at local
// dev, staging, or a customer demo deployment.
//
// The API key is held in module-level state and set by App.jsx after the
// user logs in. VITE_OVERSEE_API_KEY (if set at build time) seeds the
// initial value — handy for staging deploys where every visitor uses the
// same demo key — but normal users provide their own key via the login
// screen.

const BASE = import.meta.env.VITE_API_URL || 'http://localhost:8080'

let API_KEY = import.meta.env.VITE_OVERSEE_API_KEY || null

export function setApiKey(key) {
  API_KEY = key || null
}

export function clearApiKey() {
  API_KEY = null
}

export function getApiKey() {
  return API_KEY
}

async function request(path, options = {}) {
  const headers = { ...(options.headers || {}) }
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
  return res.json()
}

export const api = {
  // --- data ---
  listAgents: () => request('/agents'),
  getAgentSummary: (name) =>
    request(`/agents/${encodeURIComponent(name)}/summary`),
  getAgentSpans: (name, limit = 50) =>
    request(`/agents/${encodeURIComponent(name)}/spans?limit=${limit}`),
  describeAgent: (name) =>
    request(`/agents/${encodeURIComponent(name)}/describe`, { method: 'POST' }),

  // --- auth ---
  signup: (email) =>
    request('/auth/signup', {
      method: 'POST',
      body: JSON.stringify({ email }),
    }),
  loginByEmail: (email) =>
    request('/auth/login', {
      method: 'POST',
      body: JSON.stringify({ email }),
    }),
  // Validate the currently-set API key by hitting a protected endpoint.
  // Returns true on 200, false on 401, throws on any other failure.
  async validateCurrentKey() {
    try {
      await request('/agents')
      return true
    } catch (e) {
      if (e.status === 401) return false
      throw e
    }
  },
}
