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
  // Registration is optional — 404 is a normal "no registration yet"
  // result, so callers should accept null gracefully.
  async getAgentRegistration(name) {
    try {
      return await request(`/agents/${encodeURIComponent(name)}/registration`)
    } catch (e) {
      if (e.status === 404) return null
      throw e
    }
  },
  // Captured outputs (only populated when the plugin had captureOutputs
  // enabled at emit time). Returns [] when nothing's been captured.
  getAgentOutputs: (name, limit = 20) =>
    request(`/agents/${encodeURIComponent(name)}/outputs?limit=${limit}`),

  // --- ask ---
  // messages is the full chat thread; backend is stateless. Returns
  // { answer: string }.
  ask: (messages) =>
    request('/ask', {
      method: 'POST',
      body: JSON.stringify({ messages }),
    }),
  askAboutAgent: (name, messages) =>
    request(`/agents/${encodeURIComponent(name)}/ask`, {
      method: 'POST',
      body: JSON.stringify({ messages }),
    }),

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
