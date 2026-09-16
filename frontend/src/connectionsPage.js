// Connections page — presentation helpers. Pure; no React.
//
// The page answers "what can Trovis see, and how do I connect another source
// of work?". Identity, name, category, availability and brand all come from
// the canonical registry (connectors.js); this module only decides how the
// registry is GROUPED and where a click goes. Nothing here is a second
// connector list — if a connector is missing from the page, fix the registry.

import { CONNECTORS, comingSoonConnectors, connectorsByCategory, getConnector } from './connectors.js'

// Section 1 — AI workers & platforms. The manager-facing grouping folds the
// registry's three "things that do work" categories together; the technical
// split (worker vs platform vs custom OTEL) is not the page's taxonomy.
export const AI_CATEGORIES = Object.freeze(['ai_worker', 'agent_platform', 'custom'])

/** Registry order, available first, coming_soon after. */
export function aiConnectors() {
  const rows = CONNECTORS.filter((c) => AI_CATEGORIES.includes(c.category))
  return [
    ...rows.filter((c) => c.availability === 'available'),
    ...rows.filter((c) => c.availability !== 'available'),
  ]
}

/** Section 2 — Work systems with a real connection path today. */
export function workSystemConnectors() {
  return connectorsByCategory('work_system').filter((c) => c.availability === 'available')
}

/** Section 3 — quiet "More connections": everything not yet connectable. */
export function moreConnectors() {
  return comingSoonConnectors()
}

export function isConnectable(connector) {
  return connector?.availability === 'available'
}

// The Add Agent manual wizard's picker tiles, by connector id. Kept here so
// AddAgent's tile→brand map and the Connections page's "where does Connect
// go" answer read the same list; a tile added to AddAgent without an entry
// here is caught by connectionsPage.test.mjs.
export const SETUP_TILE_IDS = Object.freeze([
  'openclaw', 'openai-agents', 'claude', 'chatgpt', 'grok', 'grok-bot', 'cursor',
])

/**
 * Where "Connect" on an AI connector lands inside the EXISTING Add Agent
 * flow. A connector with a picker tile opens the manual wizard on that tile;
 * anything else (the custom OpenTelemetry path) opens the AI guide, which is
 * already the setup path for "Custom Python / other". Returns null for a
 * connector that cannot be connected, so a caller never opens setup for a
 * coming-soon row.
 */
export function setupEntryFor(connectorId) {
  const c = getConnector(connectorId)
  if (!c || c.availability !== 'available') return null
  if (SETUP_TILE_IDS.includes(c.id)) return { view: 'manual', platform: c.id }
  return { view: 'guide', platform: null }
}

// --- normalized connection health (GET /connect/health) ---------------------
//
// The server folds what it actually recorded into one row per connector:
//   state            'not_connected' | 'waiting_for_data' | 'connected'
//   observed         Trovis received data attributable to the connector
//   last_observed_at when that data arrived — never an authorization time
//   configured       an OAuth row exists (work systems); null for telemetry
//   label            the provider account for an authorized work system
// There is no degraded state: nothing records a concrete failure yet, and
// silence is not one. The page shows what is here and nothing more — never
// a coverage figure, never "healthy" without a fact behind it.

export const HEALTH_STATES = Object.freeze(['not_connected', 'waiting_for_data', 'connected'])

/** The health row for a connector, or null when the response has none / failed. */
export function healthFor(health, connectorId) {
  const rows = Array.isArray(health?.connectors) ? health.connectors : null
  if (!rows) return null
  return rows.find((r) => r && r.connector_id === connectorId) || null
}

/**
 * What an AI / platform row may say. Telemetry proves the data path worked
 * at last_observed_at, so an observed connector reads Connected with that
 * time; anything else is a door with no state (there is no record that
 * setup began, so nothing to claim). `rel` formats a relative time.
 */
export function aiRowState(row, rel) {
  if (!row || !row.observed || row.state !== 'connected') {
    return { status: null, detail: null, action: 'Connect' }
  }
  const when = row.last_observed_at ? rel(row.last_observed_at) : null
  return {
    status: 'Connected',
    detail: when ? `Last observed ${when}` : null,
    action: 'Connect another',
  }
}

/**
 * What a work-system row may say from the health row, with the OAuth row
 * as the fallback when the health check itself failed. In the fallback the
 * word is "Authorized", not "Connected": authorization is the only fact in
 * hand, and Connected here means activity was observed.
 */
export function workRowState(row, saasRow, rel, providerName) {
  if (row) {
    const acct = shortAccount(row.label)
    if (row.state === 'connected') {
      const when = row.last_observed_at ? rel(row.last_observed_at) : null
      return {
        status: acct ? `Connected · ${acct}` : 'Connected',
        detail: when ? `Last observed ${when}` : null,
        connected: true,
      }
    }
    if (row.state === 'waiting_for_data') {
      return {
        status: 'Waiting for data',
        detail: `Authorized${acct ? ` as ${acct}` : ''} · no ${providerName} activity observed yet`,
        connected: false,
      }
    }
    return { status: 'Not connected', detail: null, connected: false }
  }
  const s = saasStatus(saasRow)
  if (!s.connected) return { status: 'Not connected', detail: null, connected: false }
  return {
    status: s.label.replace(/^Connected/, 'Authorized'),
    detail: 'Couldn’t check recent activity',
    connected: false,
  }
}

function shortAccount(acct) {
  const a = String(acct || '')
  if (!a) return ''
  return a.includes('.') ? a : a.length > 8 ? `…${a.slice(-6)}` : a
}

/**
 * What a work-system row may truthfully say from GET /saas/connections.
 * `status === 'connected'` is the only durable state the backend records;
 * the provider account id is shown short when it is an opaque id, whole when
 * it is a domain (a Shopify shop).
 */
export function saasStatus(row) {
  if (!row || row.status !== 'connected') return { connected: false, label: 'Not connected' }
  const acct = String(row.provider_account_id || '')
  const short = acct.includes('.') ? acct : acct.length > 8 ? `…${acct.slice(-6)}` : acct
  return { connected: true, label: short ? `Connected · ${short}` : 'Connected' }
}
