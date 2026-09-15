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
