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

const FIELDS = ['id', 'name', 'category', 'availability', 'methods', 'brandId', 'description']

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
