// Thin wrapper around the Oversee REST API. The base URL is configurable
// at build time via VITE_API_URL so the same bundle can point at local
// dev, staging, or a customer demo deployment.
//
// Optional: VITE_OVERSEE_API_KEY, sent as X-Oversee-Api-Key on every
// request. Required when the backend has OVERSEE_INGEST_KEY set; unused
// otherwise. Both vars are baked in at build time, so production needs
// them set in Vercel before the build runs.

const BASE = import.meta.env.VITE_API_URL || 'http://localhost:8080'
const API_KEY = import.meta.env.VITE_OVERSEE_API_KEY

async function request(path, options = {}) {
  const headers = { ...(options.headers || {}) }
  if (API_KEY) {
    headers['X-Oversee-Api-Key'] = API_KEY
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
    throw new Error(msg)
  }
  return res.json()
}

export const api = {
  listAgents: () => request('/agents'),
  getAgentSummary: (name) =>
    request(`/agents/${encodeURIComponent(name)}/summary`),
  getAgentSpans: (name, limit = 50) =>
    request(`/agents/${encodeURIComponent(name)}/spans?limit=${limit}`),
  describeAgent: (name) =>
    request(`/agents/${encodeURIComponent(name)}/describe`, { method: 'POST' }),
}
