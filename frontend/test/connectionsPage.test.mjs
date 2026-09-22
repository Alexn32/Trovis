// Connections page — the registry is the only list, and the truth rules hold.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { CONNECTORS, TILE_SETUP_TYPES, getConnector } from '../src/connectors.js'
import { allTiles } from '../src/connectSetup.js'
import {
  AI_CATEGORIES,
  SETUP_TILE_IDS,
  aiConnectors,
  isConnectable,
  moreConnectors,
  saasStatus,
  setupEntryFor,
  workSystemConnectors,
} from '../src/connectionsPage.js'

const src = (f) => readFileSync(new URL(`../src/${f}`, import.meta.url), 'utf8')
const strip = (s) => s.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')
const page = strip(src('Connections.jsx'))
const helpers = strip(src('connectionsPage.js'))

test('the page consumes the canonical registry and defines no connector list of its own', () => {
  assert.match(page, /from '\.\/connectionsPage\.js'/)
  assert.match(helpers, /from '\.\/connectors\.js'/)
  // No `{ id: 'stripe', name: ... }` style entries anywhere on the page side.
  assert.doesNotMatch(page, /\bid:\s*'[a-z-]+'\s*,\s*name:/)
  assert.doesNotMatch(helpers, /\bid:\s*'[a-z-]+'\s*,\s*name:/)
  // Every id the helpers hand the page is a registry id.
  const ids = new Set(CONNECTORS.map((c) => c.id))
  for (const c of [...aiConnectors(), ...workSystemConnectors(), ...moreConnectors()]) {
    assert.ok(ids.has(c.id), c.id)
  }
  // And nothing in the registry is silently dropped.
  const shown = new Set([...aiConnectors(), ...workSystemConnectors(), ...moreConnectors()].map((c) => c.id))
  for (const id of ids) assert.ok(shown.has(id), `${id} appears somewhere on the page`)
})

test('AI workers & platforms fold the three doing-work categories, available first', () => {
  assert.deepEqual([...AI_CATEGORIES], ['ai_worker', 'agent_platform', 'custom'])
  const rows = aiConnectors()
  for (const c of rows) assert.ok(AI_CATEGORIES.includes(c.category), c.id)
  const firstSoon = rows.findIndex((c) => c.availability !== 'available')
  const lastAvail = rows.map((c) => c.availability).lastIndexOf('available')
  assert.ok(firstSoon === -1 || firstSoon > lastAvail, 'available rows come first')
})

test('Grok and Grok Bot are both first-class available choices', () => {
  const ids = aiConnectors().filter(isConnectable).map((c) => c.id)
  assert.ok(ids.includes('grok'))
  assert.ok(ids.includes('grok-bot'))
  assert.deepEqual(setupEntryFor('grok'), { view: 'manual', platform: 'grok' })
  assert.deepEqual(setupEntryFor('grok-bot'), { view: 'manual', platform: 'grok-bot' })
})

test('Shopify, Stripe and HubSpot are the work systems', () => {
  assert.deepEqual(workSystemConnectors().map((c) => c.id).sort(), ['hubspot', 'shopify', 'stripe'])
  for (const c of workSystemConnectors()) assert.equal(c.category, 'work_system')
})

test('coming-soon connectors cannot initiate a connection', () => {
  const soon = moreConnectors()
  assert.ok(soon.length > 0)
  for (const c of soon) {
    assert.equal(isConnectable(c), false, c.id)
    assert.equal(setupEntryFor(c.id), null, `${c.id} has no setup entry`)
  }
  assert.equal(setupEntryFor('zendesk'), null)
  // The page renders a label, never a button, for those rows.
  assert.match(page, /<span className="cx-soon">Coming soon<\/span>/)
})

test('Connect on an AI connector reuses the existing Add Agent flow', () => {
  // Tiles open the manual wizard on that tile; the custom OTEL path opens
  // the AI guide, which already handles "Custom Python / other".
  for (const id of SETUP_TILE_IDS) {
    assert.deepEqual(setupEntryFor(id), { view: 'manual', platform: id })
  }
  assert.deepEqual(setupEntryFor('custom-otel'), { view: 'guide', platform: null })
  // The page carries no setup instructions of its own.
  assert.doesNotMatch(page, /pip install|npm install|trovis\.init|OTEL_EXPORTER|CodeBlock/)
  // App routes the click into the one Add Agent overlay.
  const app = strip(src('App.jsx'))
  assert.match(app, /setOverlay\(\{ kind: 'add', view: entry\.view, platform: entry\.platform, nonce: Date\.now\(\) \}\)/)
  assert.match(app, /<Connections active=\{connectionsVisible\} onConnect=\{openConnectorSetup\} \/>/)
  // AddAgent accepts the preselection and defaults to the landing otherwise.
  const addAgent = strip(src('AddAgent.jsx'))
  assert.match(addAgent, /initialView = null,\s*initialPlatform = null,/)
  assert.match(addAgent, /: 'landing'/)
})

test('the setup tile list is the Add Agent picker, exactly', () => {
  // Both derive from the registry's setup_type, so they cannot drift — but
  // pin it, because setupEntryFor routes on one and the wizard renders the
  // other.
  const tiles = allTiles().map((t) => t.id)
  assert.deepEqual([...tiles].sort(), [...SETUP_TILE_IDS].sort())
  assert.ok(tiles.length >= 7)
  // And every tile is an available registry connector with a tile setup type.
  for (const id of SETUP_TILE_IDS) {
    const c = getConnector(id)
    assert.ok(c, id)
    assert.equal(c.availability, 'available', id)
    assert.ok(TILE_SETUP_TYPES.includes(c.setup_type), `${id}: ${c.setup_type}`)
  }
  // The AddAgent picker reads the same derivation.
  const addAgent = src('AddAgent.jsx')
  assert.match(addAgent, /const PLATFORMS = pickerTiles\(\)/)
  assert.match(addAgent, /const RECIPE_PLATFORMS = recipeTiles\(\)/)
})

test('saasStatus reads only what /saas/connections records', () => {
  assert.deepEqual(saasStatus(null), { connected: false, label: 'Not connected' })
  assert.deepEqual(saasStatus({ status: 'disconnected', provider_account_id: 'acct_123' }), {
    connected: false, label: 'Not connected',
  })
  assert.deepEqual(saasStatus({ status: 'connected' }), { connected: true, label: 'Connected' })
  assert.deepEqual(saasStatus({ status: 'connected', provider_account_id: 'acct_1AbCdEfGh123456' }), {
    connected: true, label: 'Connected · …123456',
  })
  assert.deepEqual(saasStatus({ status: 'connected', provider_account_id: 'shop.myshopify.com' }), {
    connected: true, label: 'Connected · shop.myshopify.com',
  })
  // Nothing the backend does not record is claimed anywhere on the page.
  assert.doesNotMatch(page, /Healthy|Receiving activity|Verified|coverage/i)
  // AI rows claim state only through aiRowState, i.e. only from an
  // observation the server recorded — never "Installed", never a literal.
  const aiRow = page.match(/function AiRow[\s\S]*?\n}\n/)[0]
  assert.match(aiRow, /aiRowState\(health, relativeTime\)/)
  assert.doesNotMatch(aiRow, /Installed|'Connected'|Not connected/)
})

test('no Work API and no backend permission atom changed', () => {
  const api = src('api.js')
  for (const path of ['/work/overview', '/work/items', '/work/suggestions', '/saas/connections', "'/connections'"]) {
    assert.ok(api.includes(path), `${path} still present`)
  }
  assert.doesNotMatch(api, /\/connections-page|\/connectors/)
  const seat = src('seat.js')
  assert.match(seat, /'Connect'/)
  assert.doesNotMatch(seat, /'Connections'/)
})

// --- normalized health helpers ---------------------------------------------

import { HEALTH_STATES, aiRowState, healthFor, linkedDetail, workRowState } from '../src/connectionsPage.js'

const rel = (iso) => `REL(${iso})`

test('health rows are looked up by connector id; a failed response has none', () => {
  const h = { connectors: [{ connector_id: 'grok', state: 'connected', observed: true }] }
  assert.equal(healthFor(h, 'grok').state, 'connected')
  assert.equal(healthFor(h, 'grok-bot'), null)
  assert.equal(healthFor({ error: true }, 'grok'), null)
  assert.equal(healthFor(null, 'grok'), null)
  assert.deepEqual([...HEALTH_STATES], ['not_connected', 'waiting_for_data', 'connected'])
})

test('an AI row claims Connected only from an observation, and never Not connected', () => {
  assert.deepEqual(aiRowState(null, rel), { status: null, detail: null, action: 'Connect' })
  assert.deepEqual(aiRowState({ state: 'not_connected', observed: false }, rel),
    { status: null, detail: null, action: 'Connect' })
  assert.deepEqual(aiRowState({ state: 'connected', observed: true, last_observed_at: 'T' }, rel),
    { status: 'Connected', detail: 'Last observed REL(T)', action: 'Connect another' })
  // observed without a timestamp: Connected, but no invented time.
  assert.deepEqual(aiRowState({ state: 'connected', observed: true, last_observed_at: null }, rel),
    { status: 'Connected', detail: null, action: 'Connect another' })
})

test('a work-system row separates authorization from activity', () => {
  const connected = { state: 'connected', configured: true, observed: true, last_observed_at: 'T', label: 'acct_1234567890' }
  assert.deepEqual(workRowState(connected, null, rel, 'Stripe'),
    { status: 'Connected · …567890', detail: 'Last observed REL(T)', connected: true })
  // Connected means events ARRIVE. Whether any reached a run is a recorded
  // fact the row carries; the page says which, never a bare "Connected".
  assert.deepEqual(workRowState({ ...connected, events_received: 3, events_linked: 0 }, null, rel, 'Stripe'),
    { status: 'Connected · …567890',
      detail: 'Last observed REL(T) · 3 events received, none linked to a run yet',
      connected: true })
  assert.deepEqual(workRowState({ ...connected, events_received: 3, events_linked: 2 }, null, rel, 'Stripe'),
    { status: 'Connected · …567890', detail: 'Last observed REL(T) · 2 of 3 events linked to runs', connected: true })
  assert.equal(linkedDetail({ events_received: 1, events_linked: 0 }), '1 event received, none linked to a run yet')
  // The page states the fact; the link key itself is taught in Add Agent.
  assert.doesNotMatch(linkedDetail({ events_received: 1, events_linked: 0 }), /trovis_loop_external_id|metadata/)
  assert.equal(linkedDetail({ events_received: 0, events_linked: 0 }), null)
  assert.equal(linkedDetail({}), null, 'an older server without the counts says nothing')
  const waiting = { state: 'waiting_for_data', configured: true, observed: false, label: 'shop.myshopify.com' }
  assert.deepEqual(workRowState(waiting, null, rel, 'Shopify'),
    { status: 'Waiting for data', detail: 'Authorized as shop.myshopify.com · no Shopify activity observed yet', connected: false })
  assert.deepEqual(workRowState({ state: 'not_connected', configured: false }, null, rel, 'HubSpot'),
    { status: 'Not connected', detail: null, connected: false })
  // Health failed: the OAuth row alone says Authorized, never Connected.
  assert.deepEqual(workRowState(null, { status: 'connected', provider_account_id: 'acct_1234567890' }, rel, 'Stripe'),
    { status: 'Authorized · …567890', detail: 'Couldn’t check recent activity', connected: false })
  assert.deepEqual(workRowState(null, { status: 'disconnected' }, rel, 'Stripe'),
    { status: 'Not connected', detail: null, connected: false })
  assert.deepEqual(workRowState(null, null, rel, 'Stripe'),
    { status: 'Not connected', detail: null, connected: false })
})

test('a recorded setup shows on the AI row before any data arrives', () => {
  // Instances (connection_instances) give telemetry connectors the fact they
  // never had: setup began or finished. The row says so instead of a bare
  // Connect, and offers Connect another.
  const started = { state: 'not_connected', observed: false, instances: [{ setup_status: 'started', state: 'setup_started' }] }
  assert.deepEqual(aiRowState(started, rel),
    { status: 'Waiting for data', detail: 'Setup started · waiting for data', action: 'Connect another' })
  const complete = { state: 'waiting_for_data', observed: false, instances: [{ setup_status: 'completed', state: 'waiting_for_data' }] }
  assert.deepEqual(aiRowState(complete, rel),
    { status: 'Waiting for data', detail: 'Setup complete · waiting for data', action: 'Connect another' })
  const two = { state: 'not_connected', observed: false, instances: [
    { setup_status: 'started', state: 'setup_started' }, { setup_status: 'completed', state: 'waiting_for_data' }] }
  assert.equal(aiRowState(two, rel).detail, '2 set up · waiting for data')
  // Disconnected rows do not count, and a row without instances is unchanged.
  const gone = { state: 'not_connected', observed: false, instances: [{ setup_status: 'disconnected', state: 'disconnected' }] }
  assert.deepEqual(aiRowState(gone, rel), { status: null, detail: null, action: 'Connect' })
  assert.deepEqual(aiRowState({ state: 'not_connected', observed: false }, rel), { status: null, detail: null, action: 'Connect' })
})

test('the health API is one Connections-oriented read; Work and Agent Flow APIs are untouched', () => {
  const api = src('api.js')
  assert.match(api, /getConnectHealth: \(\) => request\('\/connect\/health'\)/)
  assert.match(api, /getConnections: \(\) => request\('\/connections'\)/)
  assert.match(page, /api\s*\.getConnectHealth\(\)/)
  assert.doesNotMatch(page, /getWorkOverview|getWorkItems|\/work\//)
})
