// Connections page, mounted: the work-system rows read the server's truth and
// the existing SaaS connect / disconnect calls still fire; AI rows are doors.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { installDom, mount } from './mount.mjs'

installDom()

const React = await import('react')
const { api } = await import('../src/api.js')
const Connections = (await import('../src/Connections.jsx')).default

function stubSaas(over = {}) {
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
  assert.doesNotMatch(m.text(), /Healthy|Receiving activity|Last observed|Verified/)
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
  stubSaas()
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
