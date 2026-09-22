// Connector registry — the canonical Connections vocabulary. These lock the
// shape and the truth rule: "available" means the current codebase has a
// real connection path, not a logo on a landing page.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  AVAILABILITY,
  AVAILABILITY_LABELS,
  CATEGORY_LABELS,
  CONNECTION_METHODS,
  CONNECTOR_CATEGORIES,
  CONNECTORS,
  availableConnectors,
  brandIdForConnector,
  comingSoonConnectors,
  connectorHasMethod,
  connectorsByCategory,
  getConnector,
} from '../src/connectors.js'
import { BRANDS } from '../src/brandMarks.js'
import { readFileSync } from 'node:fs'
import {
  allTiles,
  connectorForGuideOption,
  guideOpeningOptions,
  pickerTiles,
  recipeTiles,
  variantsFor,
} from '../src/connectSetup.js'

// Registry-sourced fields (connectors.registry.json, snake_case like the API)
// plus the two presentation fields this module owns.
const REGISTRY_FIELDS = [
  'id', 'name', 'category', 'availability', 'methods', 'setup_type', 'observes',
  'discovers_agents', 'supports_multiple_instances', 'management', 'explicit_method',
  'stamps', 'tile_label', 'tile_subtitle', 'guide_label', 'variants', 'setup_notes',
]
const FIELDS = [...REGISTRY_FIELDS, 'brandId', 'description']
const SNAPSHOT = JSON.parse(
  readFileSync(new URL('../src/connectors.registry.json', import.meta.url), 'utf8'),
)

test('every connector has exactly the registry shape', () => {
  for (const c of CONNECTORS) {
    assert.deepEqual(Object.keys(c).sort(), [...FIELDS].sort(), `${c.id} shape`)
    assert.match(c.id, /^[a-z0-9]+(-[a-z0-9]+)*$/, `${c.id} is kebab-case`)
    assert.ok(typeof c.name === 'string' && c.name.trim(), `${c.id} has a name`)
    assert.ok(typeof c.description === 'string' && c.description.trim(), `${c.id} has a description`)
  }
})

test('ids are unique', () => {
  const ids = CONNECTORS.map((c) => c.id)
  assert.equal(new Set(ids).size, ids.length)
})

test('every connector has a valid category, and every category has a label', () => {
  for (const c of CONNECTORS) {
    assert.ok(CONNECTOR_CATEGORIES.includes(c.category), `${c.id}: ${c.category}`)
  }
  assert.deepEqual(Object.keys(CATEGORY_LABELS).sort(), [...CONNECTOR_CATEGORIES].sort())
})

test('every connector has at least one valid method', () => {
  for (const c of CONNECTORS) {
    assert.ok(Array.isArray(c.methods) && c.methods.length >= 1, `${c.id} has a method`)
    assert.equal(new Set(c.methods).size, c.methods.length, `${c.id} methods are unique`)
    for (const m of c.methods) {
      assert.ok(CONNECTION_METHODS.includes(m), `${c.id}: ${m}`)
    }
  }
})

test('every connector has a valid availability, and every availability has a label', () => {
  for (const c of CONNECTORS) {
    assert.ok(AVAILABILITY.includes(c.availability), `${c.id}: ${c.availability}`)
  }
  assert.deepEqual(Object.keys(AVAILABILITY_LABELS).sort(), [...AVAILABILITY].sort())
})

test('brandId is a real brandMarks id or null', () => {
  for (const c of CONNECTORS) {
    if (c.brandId === null) continue
    assert.ok(BRANDS[c.brandId], `${c.id} → brand ${c.brandId}`)
  }
})

test('availability agrees with the brand catalog: coming marks are never available', () => {
  for (const c of CONNECTORS) {
    const brand = c.brandId && BRANDS[c.brandId]
    if (!brand) continue
    if (brand.role === 'coming') {
      assert.equal(c.availability, 'coming_soon', `${c.id} rides a coming-only mark`)
    } else {
      assert.equal(c.availability, 'available', `${c.id} rides a live/recipe mark`)
    }
  }
})

test('Stripe, HubSpot, and Shopify are available work systems over OAuth + webhook', () => {
  for (const id of ['stripe', 'hubspot', 'shopify']) {
    const c = getConnector(id)
    assert.ok(c, id)
    assert.equal(c.category, 'work_system')
    assert.equal(c.availability, 'available')
    assert.ok(connectorHasMethod(id, 'oauth'), `${id} oauth`)
    assert.ok(connectorHasMethod(id, 'webhook'), `${id} webhook`)
  }
  assert.deepEqual(
    connectorsByCategory('work_system').filter((c) => c.availability === 'available').map((c) => c.id),
    ['stripe', 'hubspot', 'shopify'],
  )
})

test('Grok (xAI SDK) and Grok Bot are separate first-class current connectors', () => {
  const grok = getConnector('grok')
  const bot = getConnector('grok-bot')
  assert.equal(grok?.availability, 'available')
  assert.equal(bot?.availability, 'available')
  assert.equal(grok.category, 'agent_platform')
  assert.equal(bot.category, 'ai_worker')
  assert.ok(connectorHasMethod('grok', 'sdk'))
  assert.deepEqual(bot.methods, ['mcp'])
  // Same vendor, different marks: the SDK shows xAI, the Bot is a Cursor
  // desktop assistant (mirrors TILE_BRAND in AddAgent).
  assert.equal(grok.brandId, 'grok')
  assert.equal(bot.brandId, 'cursor')
})

test('the custom OpenTelemetry path exists and is available', () => {
  const c = getConnector('custom-otel')
  assert.ok(c)
  assert.equal(c.category, 'custom')
  assert.equal(c.availability, 'available')
  assert.deepEqual(c.methods, ['otel'])
  assert.equal(brandIdForConnector('custom-otel'), null)
})

test('the current connection families are all present', () => {
  const ids = new Set(CONNECTORS.map((c) => c.id))
  for (const id of [
    'openclaw', 'openai-agents', 'claude', 'chatgpt', 'grok', 'grok-bot', 'cursor',
    'custom-otel', 'stripe', 'hubspot', 'shopify',
  ]) {
    assert.ok(ids.has(id), id)
  }
})

test('no planned connector is marked available without an implementation', () => {
  // Nothing in this list has a connection path in the codebase today. If
  // one lands, move it here deliberately — with the door, not before it.
  const PLANNED = [
    'slack', 'github', 'intercom', 'microsoft', 'teams', 'azure', 'aws', 'bedrock',
    'google', 'gemini', 'workspace', 'langgraph', 'langchain', 'crewai', 'zendesk',
  ]
  for (const id of PLANNED) {
    const c = getConnector(id)
    if (c) assert.equal(c.availability, 'coming_soon', `${id} advertised as available`)
  }
  // And the available set is exactly the doors that work today.
  assert.deepEqual(
    availableConnectors().map((c) => c.id),
    ['openclaw', 'openai-agents', 'claude', 'chatgpt', 'grok', 'grok-bot', 'cursor',
      'custom-otel', 'stripe', 'hubspot', 'shopify'],
  )
  assert.deepEqual(comingSoonConnectors().map((c) => c.id), ['slack', 'github', 'intercom'])
})

test('lookups are quiet on unknown ids', () => {
  assert.equal(getConnector('zendesk'), null)
  assert.equal(brandIdForConnector('zendesk'), null)
  assert.equal(connectorHasMethod('zendesk', 'otel'), false)
  assert.deepEqual(connectorsByCategory('nope'), [])
})

// --- the backend registry is canonical; this module mirrors it -------------

test('the committed snapshot and this module agree on every registry field', () => {
  // connectors.py → connectors.registry.json → here. test_connectors_registry.py
  // fails when the snapshot is stale against the backend; this fails when the
  // frontend stops mirroring the snapshot.
  assert.deepEqual(CONNECTORS.map((c) => c.id), SNAPSHOT.map((c) => c.id), 'same ids, same order')
  for (const entry of SNAPSHOT) {
    const c = getConnector(entry.id)
    for (const f of REGISTRY_FIELDS) {
      assert.deepEqual(c[f], entry[f], `${entry.id}.${f}`)
    }
  }
})

test('every registry connector has a presentation entry, and nothing else does', () => {
  // The module throws at import when a registry id lacks presentation, so the
  // fact that this test file loaded proves half of it; the other half is
  // that a presentation row cannot outlive its registry entry.
  assert.equal(CONNECTORS.length, SNAPSHOT.length)
})

test('setup_type is the one field the Connect surfaces route on', () => {
  const tileTypes = ['sdk', 'plugin', 'actions', 'mcp', 'recipe']
  for (const c of CONNECTORS) {
    if (c.availability === 'coming_soon') assert.equal(c.setup_type, 'none', c.id)
    else assert.notEqual(c.setup_type, 'none', c.id)
    if (c.category === 'work_system' && c.availability === 'available') {
      assert.equal(c.setup_type, 'oauth', c.id)
    }
    if (tileTypes.includes(c.setup_type)) {
      assert.ok(c.tile_label && c.tile_subtitle, `${c.id} tile has labels`)
    }
  }
  assert.equal(getConnector('custom-otel').setup_type, 'guide')
})

test('observes lists coverage dimensions only — capabilities, not observations', () => {
  const DIMENSIONS = ['execution', 'actions', 'external_outcomes', 'handoffs', 'cost']
  for (const c of CONNECTORS) {
    for (const d of c.observes) assert.ok(DIMENSIONS.includes(d), `${c.id}: ${d}`)
    // A work system enriches Work; it never invents workers.
    if (c.category === 'work_system') assert.equal(c.discovers_agents, false, c.id)
  }
  assert.deepEqual(getConnector('stripe').observes, ['external_outcomes'])
})

// --- connectSetup.js: the surfaces derive from the registry ------------------

test('the wizard tiles are the available tile-type connectors, in registry order', () => {
  const ids = allTiles().map((t) => t.id)
  assert.deepEqual(ids, ['openclaw', 'openai-agents', 'claude', 'chatgpt', 'grok', 'grok-bot', 'cursor'])
  assert.deepEqual(recipeTiles().map((t) => t.id), ['cursor'])
  assert.ok(!pickerTiles().some((t) => t.id === 'cursor'))
  for (const t of allTiles()) {
    assert.deepEqual(Object.keys(t).sort(), ['id', 'label', 'needsProvider', 'subtitle'])
    assert.equal(t.needsProvider, false)
    assert.ok(t.label && t.subtitle, t.id)
  }
})

test('the Claude tile splits into the registry variants', () => {
  assert.deepEqual(variantsFor('claude').map((v) => v.id), ['claude-agent-sdk', 'claude-agents'])
  assert.deepEqual(variantsFor('openclaw'), [])
  assert.deepEqual(variantsFor('nope'), [])
})

test("the guide opening chips are the available AI connectors' guide labels, catch-all last", () => {
  const chips = guideOpeningOptions()
  assert.deepEqual(chips, [
    'OpenClaw',
    'OpenAI Agents SDK',
    'Claude Agent SDK / Claude Code',
    'ChatGPT (custom GPT)',
    'Grok (xAI SDK)',
    'Grok Bot',
    'Cursor (OpenTelemetry)',
    'Custom Python / other',
  ])
  for (const chip of chips) {
    assert.ok(chip.length < 40, `${chip} fits a chip`)
    assert.ok(connectorForGuideOption(chip), chip)
  }
  assert.equal(connectorForGuideOption('custom python / OTHER').id, 'custom-otel')
  assert.equal(connectorForGuideOption('Stripe'), null)
  assert.equal(connectorForGuideOption(''), null)
})
