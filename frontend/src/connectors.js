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
// Connect pages (AddAgent.jsx, connectSnippets.js). Later PRs read this
// for the Connections page, the AI setup flow, capability reporting and
// Work coverage; they should extend the entries, not fork the list.
//
// Truth rule: `available` means the CURRENT codebase has a real, viable
// connection path (a live Connect door, a shipped SDK/plugin, an OAuth +
// webhook adapter, or the OTLP receiver). Anything planned is
// `coming_soon`, whatever its logo says on a landing page.

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

// Entry shape (keep it this small — no evidence/coverage/recommendation
// fields until a PR actually reads them):
//   id           stable kebab-case id; matches the AddAgent platform id where
//                one exists so the two never need a mapping table
//   name         product-facing name
//   category     one of CONNECTOR_CATEGORIES
//   availability one of AVAILABILITY
//   methods      non-empty subset of CONNECTION_METHODS
//   brandId      brandMarks.js id for the mark (recognition, not identity)
//   description  one honest sentence about what the connection observes
//
// One connector per product-facing concept. Implementation variants
// (Claude Agent SDK vs Managed Agents, Python vs Node recipes) live under
// one id — the setup pages split them, the taxonomy does not.
export const CONNECTORS = Object.freeze([
  {
    id: 'openclaw',
    name: 'OpenClaw',
    category: 'agent_platform',
    availability: 'available',
    methods: ['plugin'],
    brandId: 'openclaw',
    description: 'OpenClaw agents connect themselves through the Trovis plugin.',
  },
  {
    id: 'openai-agents',
    name: 'OpenAI Agents SDK',
    category: 'agent_platform',
    availability: 'available',
    methods: ['sdk'],
    brandId: 'chatgpt',
    description: 'Agents built on the OpenAI Agents SDK, instrumented with the trovis-agents package.',
  },
  {
    id: 'claude',
    name: 'Claude Agents',
    category: 'agent_platform',
    availability: 'available',
    methods: ['sdk'],
    brandId: 'claude',
    description: 'Claude Agent SDK (the Claude Code engine) and Claude Managed Agents, via the trovis-agents package.',
  },
  {
    id: 'chatgpt',
    name: 'ChatGPT custom GPT',
    category: 'ai_worker',
    availability: 'available',
    methods: ['actions', 'oauth'],
    brandId: 'chatgpt',
    description: 'A custom GPT reports its own activity to Trovis through GPT Actions — no code.',
  },
  {
    id: 'grok',
    name: 'Grok (xAI SDK)',
    category: 'agent_platform',
    availability: 'available',
    methods: ['sdk', 'otel'],
    brandId: 'grok',
    description: 'Agents built on the xAI SDK, which already emits OpenTelemetry; trovis.init() points it at Trovis.',
  },
  {
    id: 'grok-bot',
    name: 'Grok Bot',
    category: 'ai_worker',
    availability: 'available',
    methods: ['mcp'],
    brandId: 'cursor',
    description: 'A Grok Bot desktop assistant reports in over the Trovis MCP server.',
  },
  {
    id: 'cursor',
    name: 'Cursor',
    category: 'ai_worker',
    availability: 'available',
    methods: ['otel'],
    brandId: 'cursor',
    description: 'Cursor sends traces to Trovis over OpenTelemetry — a recipe, not a plugin.',
  },
  {
    id: 'custom-otel',
    name: 'Custom (OpenTelemetry)',
    category: 'custom',
    availability: 'available',
    methods: ['otel'],
    brandId: null,
    description: 'Anything that emits OpenTelemetry spans can send them to the Trovis OTLP/HTTP receiver.',
  },
  {
    id: 'stripe',
    name: 'Stripe',
    category: 'work_system',
    availability: 'available',
    methods: ['oauth', 'webhook'],
    brandId: 'stripe',
    description: 'See when a job is waiting on a payment, and when that payment clears or fails.',
  },
  {
    id: 'hubspot',
    name: 'HubSpot',
    category: 'work_system',
    availability: 'available',
    methods: ['oauth', 'webhook'],
    brandId: 'hubspot',
    description: 'See when a job is waiting on a deal or a support ticket, and when it moves.',
  },
  {
    id: 'shopify',
    name: 'Shopify',
    category: 'work_system',
    availability: 'available',
    methods: ['oauth', 'webhook'],
    brandId: 'shopify',
    description: 'See when a job is waiting on an order, payment, or fulfillment, and when it completes.',
  },
  // Recognised in Work today (brandMarks.js `coming`), no direct door yet.
  {
    id: 'slack',
    name: 'Slack',
    category: 'communication',
    availability: 'coming_soon',
    methods: ['oauth'],
    brandId: 'slack',
    description: 'Recognised when it shows up in work; a direct connect is coming.',
  },
  {
    id: 'github',
    name: 'GitHub',
    category: 'work_system',
    availability: 'coming_soon',
    methods: ['oauth'],
    brandId: 'github',
    description: 'Recognised when it shows up in work; a direct connect is coming.',
  },
  {
    id: 'intercom',
    name: 'Intercom',
    category: 'communication',
    availability: 'coming_soon',
    methods: ['oauth'],
    brandId: 'intercom',
    description: 'Recognised when it shows up in work; a direct connect is coming.',
  },
])

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
