// Thin wrapper around the Oversee REST API. The base URL is configurable
// at build time via VITE_API_URL so the same bundle can point at local
// dev, staging, or a customer demo deployment.

const BASE = import.meta.env.VITE_API_URL || 'http://localhost:8080'

async function request(path, options = {}) {
  const res = await fetch(`${BASE}${path}`, options)
  if (!res.ok) {
    let detail
    try {
      detail = (await res.json()).detail
    } catch {
      // fall through to status text
    }
    throw new Error(detail || `${res.status} ${res.statusText}`)
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
