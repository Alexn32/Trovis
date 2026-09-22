// Canonical connector registry — the identity/taxonomy layer for Connections.
//
// Vocabulary (product decision, Connections ↔ Work initiative):
//   Connections = systems Trovis connects to in order to observe work
//                 (AI workers, agent platforms, work systems, communication
//                 systems, custom telemetry sources). This module.
//   Work        = operational truth derived from what those connections
//                 let Trovis observe. A separate surface; nothing here
//                 touches the Work model.
//   Agent Flow  = relationships/handoffs between agents (ConnectionsMap.jsx).
//                 Agent-to-agent edges, NOT connecting an external system.
//
// Pure data + lookups. No React, no setup snippets — those stay with the
// Connect pages (AddAgent.jsx, connectSnippets.js). The Connections page,
// the manual wizard's tiles, the AI guide's opening chips and (later)
// capability reporting all read this; they extend the entries, never fork
// the list.
//
// Source of truth: the BACKEND registry (connectors.py). Identity, setup
// shape and capabilities come from its committed snapshot,
// connectors.registry.json (`python3 connectors.py > frontend/src/connectors.registry.json`);
// this module adds only the presentation fields the backend has no business
// owning (brand mark, one-line description). test_connectors_registry.py
// fails when the snapshot is stale; connectors.test.mjs fails when the two
// sides disagree. Registry-sourced fields keep the API's snake_case so a row
// here and a row from GET /connect/connectors read the same.
//
// Truth rule: `available` means the CURRENT codebase has a real, viable
// connection path (a live Connect door, a shipped SDK/plugin, an OAuth +
// webhook adapter, or the OTLP receiver). Anything planned is
// `coming_soon`, whatever its logo says on a landing page.

import REGISTRY from './connectors.registry.json' with { type: 'json' }

/** What kind of system a connector observes. */
export const CONNECTOR_CATEGORIES = Object.freeze([
  'ai_worker',      // a single AI assistant/worker that reports in (a GPT, a Bot)
  'agent_platform', // a framework/runtime that runs agents (SDKs, OpenClaw)
  'work_system',    // a business system where work lives (payments, CRM, orders)
  'communication',  // where people and agents talk (chat, support inbox)
  'custom',         // anything else that speaks OpenTelemetry
])

/** Shared product labels for the categories. */
export const CATEGORY_LABELS = Object.freeze({
  ai_worker: 'AI workers',
  agent_platform: 'Agent platforms',
  work_system: 'Work systems',
  communication: 'Communication',
  custom: 'Custom',
})

/** How a connector gets telemetry into (or out of) Trovis. */
export const CONNECTION_METHODS = Object.freeze([
  'otel',    // OTLP/HTTP spans to POST /v1/traces
  'sdk',     // trovis-agents pip package (wraps OTEL)
  'plugin',  // a first-party plugin inside the platform (trovis-openclaw-plugin)
  'mcp',     // the worker reports in over an MCP server Trovis mounts
  'oauth',   // Trovis is authorised into the system's account
  'webhook', // the system pushes events to Trovis
  'actions', // GPT Actions (OpenAPI) — a custom GPT calls Trovis
])

export const AVAILABILITY = Object.freeze(['available', 'coming_soon'])

/** Shared product labels for availability. */
export const AVAILABILITY_LABELS = Object.freeze({
  available: 'Available',
  coming_soon: 'Coming soon',
})

// Entry shape:
//   id, name, category, availability, methods    identity (registry)
//   setup_type                                   how a connection is set up:
//                                                sdk | plugin | actions | mcp |
//                                                recipe | guide | oauth | none
//   observes                                     Work Coverage dimensions a
//                                                connection CAN contribute —
//                                                capabilities, never a promise
//   discovers_agents, supports_multiple_instances, management
//   explicit_method, stamps                      how connect_health recognises it
//   tile_label, tile_subtitle, guide_label, variants, setup_notes
//                                                Connect-surface labels
//   brandId      brandMarks.js id for the mark (recognition, not identity)
//   description  one honest sentence about what the connection observes
//
// One connector per product-facing concept. Implementation variants
// (Claude Agent SDK vs Managed Agents, Python vs Node recipes) live under
// one id — the setup pages split them, the taxonomy does not.

/** Registry setup types that are picker tiles in the manual wizard. */
export const TILE_SETUP_TYPES = Object.freeze(['sdk', 'plugin', 'actions', 'mcp', 'recipe'])

// Presentation-only fields, keyed by registry id. Every registry id must
// have an entry (connectors.test.mjs), so a connector added to the backend
// shows up here as a failing test rather than a blank row.
const PRESENTATION = Object.freeze({
  openclaw: {
    brandId: 'openclaw',
    description: 'OpenClaw agents connect themselves through the Trovis plugin.',
  },
  'openai-agents': {
    brandId: 'chatgpt',
    description: 'Agents built on the OpenAI Agents SDK, instrumented with the trovis-agents package.',
  },
  claude: {
    brandId: 'claude',
    description: 'Claude Agent SDK (the Claude Code engine) and Claude Managed Agents, via the trovis-agents package.',
  },
  chatgpt: {
    brandId: 'chatgpt',
    description: 'A custom GPT reports its own activity to Trovis through GPT Actions — no code.',
  },
  grok: {
    brandId: 'grok',
    description: 'Agents built on the xAI SDK, which already emits OpenTelemetry; trovis.init() points it at Trovis.',
  },
  'grok-bot': {
    brandId: 'cursor',
    description: 'A Grok Bot desktop assistant reports in over the Trovis MCP server.',
  },
  cursor: {
    brandId: 'cursor',
    description: 'Cursor sends traces to Trovis over OpenTelemetry — a recipe, not a plugin.',
  },
  'custom-otel': {
    brandId: null,
    description: 'Anything that emits OpenTelemetry spans can send them to the Trovis OTLP/HTTP receiver.',
  },
  stripe: {
    brandId: 'stripe',
    description: 'See when a run is waiting on a payment, and when that payment clears or fails.',
  },
  hubspot: {
    brandId: 'hubspot',
    description: 'See when a run is waiting on a deal or a support ticket, and when it moves.',
  },
  shopify: {
    brandId: 'shopify',
    description: 'See when a run is waiting on an order, payment, or fulfillment, and when it completes.',
  },
  // Recognised in Work today (brandMarks.js `coming`), no direct door yet.
  slack: {
    brandId: 'slack',
    description: 'Recognised when it shows up in work; a direct connect is coming.',
  },
  github: {
    brandId: 'github',
    description: 'Recognised when it shows up in work; a direct connect is coming.',
  },
  intercom: {
    brandId: 'intercom',
    description: 'Recognised when it shows up in work; a direct connect is coming.',
  },
})

export const CONNECTORS = Object.freeze(
  REGISTRY.map((entry) => {
    const pres = PRESENTATION[entry.id]
    if (!pres) throw new Error(`connectors.js: no presentation for registry connector ${entry.id}`)
    return Object.freeze({ ...entry, ...pres })
  }),
)

const BY_ID = new Map(CONNECTORS.map((c) => [c.id, c]))

/** Connector by id. Unknown → null, never a throw. */
export function getConnector(id) {
  return BY_ID.get(id) || null
}

export function connectorsByCategory(category) {
  return CONNECTORS.filter((c) => c.category === category)
}

export function availableConnectors() {
  return CONNECTORS.filter((c) => c.availability === 'available')
}

export function comingSoonConnectors() {
  return CONNECTORS.filter((c) => c.availability === 'coming_soon')
}

/** brandMarks.js id for a connector. Unknown connector or no mark → null. */
export function brandIdForConnector(id) {
  return getConnector(id)?.brandId ?? null
}

export function connectorHasMethod(id, method) {
  return (getConnector(id)?.methods || []).includes(method)
}

/** Available connectors whose setup is a manual-wizard tile, registry order. */
export function tileConnectors() {
  return CONNECTORS.filter(
    (c) => c.availability === 'available' && TILE_SETUP_TYPES.includes(c.setup_type),
  )
}
