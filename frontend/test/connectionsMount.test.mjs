// Connections page, mounted: the work-system rows read the server's truth and
// the existing SaaS connect / disconnect calls still fire; AI rows are doors.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { installDom, mount } from './mount.mjs'

installDom()

const React = await import('react')
const { api } = await import('../src/api.js')
const Connections = (await import('../src/Connections.jsx')).default

const NOW = Date.now()
const MIN_AGO = (m) => new Date(NOW - m * 60000).toISOString()

function healthRow(connector_id, over = {}) {
  return {
    connector_id, state: 'not_connected', configured: null, observed: false,
    last_observed_at: null, connection_method: null, label: null, source_count: 0, ...over,
  }
}
// The default health fixture: Stripe authorized AND observed, HubSpot and
// Shopify not connected, Grok Bot observed 12 minutes ago, Grok (the SDK)
// never observed, everything else a bare door.
function healthFixture(rows = {}) {
  const base = {
    stripe: healthRow('stripe', { state: 'connected', configured: true, observed: true,
      last_observed_at: MIN_AGO(180), connection_method: 'oauth', label: 'acct_1AbCdEfGh123456', source_count: null }),
    hubspot: healthRow('hubspot', { configured: false, source_count: null }),
    shopify: healthRow('shopify', { configured: false, source_count: null }),
    'grok-bot': healthRow('grok-bot', { state: 'connected', observed: true,
      last_observed_at: MIN_AGO(12), connection_method: 'mcp', source_count: 1 }),
  }
  for (const id of ['openclaw', 'openai-agents', 'claude', 'chatgpt', 'grok', 'cursor', 'custom-otel']) {
    base[id] = base[id] || healthRow(id)
  }
  Object.assign(base, rows)
  return { generated_at: new Date(NOW).toISOString(), connectors: Object.values(base) }
}

function stubSaas(over = {}, health = healthFixture()) {
  const calls = { start: [], disconnect: [] }
  api.getSaasConnections = async () => ({
    connections: [
      { provider: 'stripe', status: 'connected', provider_account_id: 'acct_1AbCdEfGh123456', livemode: true },
      { provider: 'hubspot', status: 'disconnected', provider_account_id: '12345678' },
    ],
    stripe_oauth_configured: true,
    hubspot_oauth_configured: true,
    shopify_oauth_configured: true,
    ...over,
  })
  api.getConnectHealth = async () => {
    if (health instanceof Error) throw health
    return health
  }
  api.startStripeConnect = async () => { calls.start.push('stripe'); return {} }
  api.startHubSpotConnect = async () => { calls.start.push('hubspot'); return {} }
  api.startShopifyConnect = async (shop) => { calls.start.push(`shopify:${shop}`); return {} }
  api.disconnectStripe = async () => { calls.disconnect.push('stripe'); return { provider: 'stripe', status: 'disconnected' } }
  api.disconnectHubSpot = async () => { calls.disconnect.push('hubspot') }
  api.disconnectShopify = async () => { calls.disconnect.push('shopify') }
  return calls
}

const row = (m, id) => m.$(`.cx-row[data-connector="${id}"]`)

test('existing SaaS state renders truthfully: connected, not connected, never invented', async () => {
  stubSaas()
  const m = await mount(React.createElement(Connections, { onConnect: () => {} }))
  await m.settle()
  assert.match(row(m, 'stripe').textContent, /Connected · …123456/)
  assert.ok(row(m, 'stripe').querySelector('.cx-disconnect'), 'connected row offers a quiet Disconnect')
  assert.ok(!row(m, 'stripe').querySelector('button.btn-secondary'), 'no Connect button on a connected row')
  assert.match(row(m, 'hubspot').textContent, /Not connected/)
  assert.ok(row(m, 'hubspot').querySelector('button.btn-secondary'), 'HubSpot offers Connect')
  assert.match(row(m, 'shopify').textContent, /Not connected/)
  assert.doesNotMatch(m.text(), /Healthy|Receiving activity|Verified/)
  m.unmount()
})

test('the rendered product copy carries no correlation implementation details', async () => {
  stubSaas()
  const m = await mount(React.createElement(Connections, { onConnect: () => {} }))
  await m.settle()
  // Includes the collapsed fine print — it is in the DOM whether open or not.
  assert.doesNotMatch(m.text(), /trovis_loop_external_id/)
  assert.doesNotMatch(m.text(), /Trovis loop key/i)
  assert.doesNotMatch(m.text(), /loop key|metadata/i)
  for (const id of ['stripe', 'hubspot', 'shopify']) {
    assert.match(row(m, id).textContent, /work it can reliably link/, `${id} states the truth boundary`)
  }
  m.unmount()
})

test('a failed status check is reported, not rendered as Not connected', async () => {
  stubSaas()
  api.getSaasConnections = async () => { throw new Error('boom') }
  const m = await mount(React.createElement(Connections, { onConnect: () => {} }))
  await m.settle()
  assert.match(m.text(), /Couldn’t check which work systems are connected/)
  assert.doesNotMatch(row(m, 'stripe').textContent, /Not connected|Connected/)
  m.unmount()
})

test('connect and disconnect still go through the existing SaaS API', async () => {
  const calls = stubSaas()
  const m = await mount(React.createElement(Connections, { onConnect: () => {} }))
  await m.settle()
  // Connect HubSpot → the existing OAuth start.
  await m.click(row(m, 'hubspot').querySelector('button.btn-secondary'))
  await m.settle()
  assert.deepEqual(calls.start, ['hubspot'])
  // No authorize_url came back, so the row says so instead of pretending.
  assert.match(row(m, 'hubspot').textContent, /Could not start HubSpot Connect/)
  // Shopify needs a shop domain before it will start.
  const shopBtn = row(m, 'shopify').querySelector('button.btn-secondary')
  assert.equal(shopBtn.disabled, true, 'no shop → Connect disabled')
  const input = row(m, 'shopify').querySelector('input.cx-shop')
  assert.ok(input)
  // Disconnect Stripe → the existing DELETE, then a re-read.
  await m.click(row(m, 'stripe').querySelector('.cx-disconnect'))
  await m.settle()
  assert.deepEqual(calls.disconnect, ['stripe'])
  m.unmount()
})

test('an OAuth not configured on this deploy disables Connect and says why', async () => {
  stubSaas({ hubspot_oauth_configured: false })
  const m = await mount(React.createElement(Connections, { onConnect: () => {} }))
  await m.settle()
  const btn = row(m, 'hubspot').querySelector('button.btn-secondary')
  assert.equal(btn.disabled, true)
  assert.match(btn.title, /isn’t configured on this deploy/)
  m.unmount()
})

test('AI connectors are doors into setup; coming-soon rows have no button', async () => {
  // Nothing observed: every AI row is a bare door.
  stubSaas({}, healthFixture({ 'grok-bot': healthRow('grok-bot') }))
  const opened = []
  const m = await mount(React.createElement(Connections, { onConnect: (id) => opened.push(id) }))
  await m.settle()
  // Grok and Grok Bot both present, both connectable.
  for (const id of ['grok', 'grok-bot', 'openclaw', 'openai-agents', 'claude', 'chatgpt', 'cursor', 'custom-otel']) {
    const r = row(m, id)
    assert.ok(r, `${id} row`)
    assert.ok(r.querySelector('button'), `${id} has Connect`)
    assert.doesNotMatch(r.textContent, /Connected|Installed/, `${id} claims no installed state`)
  }
  await m.click(row(m, 'grok-bot').querySelector('button'))
  await m.click(row(m, 'grok').querySelector('button'))
  assert.deepEqual(opened, ['grok-bot', 'grok'])
  // Coming soon: named, quiet, not a button.
  for (const id of ['slack', 'github', 'intercom']) {
    assert.ok(!row(m, id), `${id} is not a primary row`)
  }
  const more = m.$('.cx-more')
  assert.ok(more)
  assert.match(more.textContent, /Slack|GitHub|Intercom/)
  assert.equal(more.querySelectorAll('button').length, 0)
  m.unmount()
})

// --- normalized health (GET /connect/health) ---------------------------------

test('the page consumes the normalized health endpoint alongside the OAuth rows', async () => {
  let healthCalls = 0
  stubSaas()
  const inner = api.getConnectHealth
  api.getConnectHealth = async () => { healthCalls += 1; return inner() }
  const m = await mount(React.createElement(Connections, { onConnect: () => {} }))
  await m.settle()
  assert.equal(healthCalls, 1)
  m.unmount()
})

test('an observed AI connector reads Connected with last observed; an unobserved one stays a door', async () => {
  stubSaas()
  const m = await mount(React.createElement(Connections, { onConnect: () => {} }))
  await m.settle()
  const bot = row(m, 'grok-bot')
  assert.match(bot.textContent, /Connected/)
  assert.match(bot.textContent, /Last observed 12m ago/)
  assert.equal(bot.querySelector('button').textContent, 'Connect another')
  assert.equal(bot.querySelector('.cx-observed').getAttribute('title'), MIN_AGO(12), 'exact time on hover')
  // Grok (the SDK) was never observed — separate row, separate state.
  const grok = row(m, 'grok')
  assert.doesNotMatch(grok.textContent, /Connected|Last observed/)
  assert.equal(grok.querySelector('button').textContent, 'Connect')
  for (const id of ['claude', 'openai-agents', 'openclaw', 'chatgpt', 'cursor', 'custom-otel']) {
    assert.doesNotMatch(row(m, id).textContent, /Connected|Last observed/, `${id} claims nothing`)
    assert.equal(row(m, id).querySelector('button').textContent, 'Connect')
  }
  m.unmount()
})

test('SaaS words follow the health state: connected, waiting for data, not connected', async () => {
  stubSaas({
    connections: [
      { provider: 'stripe', status: 'connected', provider_account_id: 'acct_1AbCdEfGh123456' },
      { provider: 'hubspot', status: 'connected', provider_account_id: '12345678' },
    ],
  }, healthFixture({
    hubspot: healthRow('hubspot', { state: 'waiting_for_data', configured: true, observed: false,
      connection_method: 'oauth', label: '12345678', source_count: null }),
  }))
  const m = await mount(React.createElement(Connections, { onConnect: () => {} }))
  await m.settle()
  assert.match(row(m, 'stripe').textContent, /Connected · …123456/)
  assert.match(row(m, 'stripe').textContent, /Last observed 3h ago/)
  assert.match(row(m, 'hubspot').textContent, /Waiting for data/)
  assert.match(row(m, 'hubspot').textContent, /Authorized as 12345678 · no HubSpot activity observed yet/)
  assert.doesNotMatch(row(m, 'hubspot').textContent, /Connected/)
  // Authorized → the disconnect stays available, quietly.
  assert.ok(row(m, 'hubspot').querySelector('.cx-disconnect'))
  assert.match(row(m, 'shopify').textContent, /Not connected/)
  m.unmount()
})

test('a health API failure never renders every connector Not connected', async () => {
  stubSaas({}, new Error('health down'))
  const m = await mount(React.createElement(Connections, { onConnect: () => {} }))
  await m.settle()
  assert.match(m.text(), /Couldn’t check which connections have reported in/)
  // Stripe is authorized per the OAuth row: say that, not Connected, not Not connected.
  assert.match(row(m, 'stripe').textContent, /Authorized · …123456/)
  assert.match(row(m, 'stripe').textContent, /Couldn’t check recent activity/)
  assert.doesNotMatch(row(m, 'stripe').textContent, /Connected/)
  assert.ok(row(m, 'stripe').querySelector('.cx-disconnect'), 'disconnect still offered')
  // Not authorized per the OAuth row is still simply not connected.
  assert.match(row(m, 'hubspot').textContent, /Not connected/)
  // AI rows are doors, with no state claimed either way.
  for (const id of ['grok', 'grok-bot', 'claude']) {
    assert.doesNotMatch(row(m, id).textContent, /Connected|Not connected/)
    assert.equal(row(m, id).querySelector('button').textContent, 'Connect')
  }
  m.unmount()
})

test('the page never implies coverage', async () => {
  stubSaas()
  const m = await mount(React.createElement(Connections, { onConnect: () => {} }))
  await m.settle()
  assert.doesNotMatch(m.text(), /fully visible|coverage|verified|healthy|\d+\s?%/i)
  m.unmount()
})
