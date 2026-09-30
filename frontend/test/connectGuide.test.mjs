// The guided Connect flow, mounted: one door for AI workers AND work systems.
// A work-system chip renders its Connect card in the thread with no model
// round-trip; a model reply that names a work system gets the same card; the
// landing's sentence becomes the first user turn; the card uses the same
// OAuth door as the Connections page.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { installDom, mount } from './mount.mjs'

installDom()

const React = await import('react')
const { api } = await import('../src/api.js')
const ConnectGuide = (await import('../src/ConnectGuide.jsx')).default

function stub({ saas = {}, ask = null, status = null } = {}) {
  const calls = { ask: [], start: [] }
  const statusReply = status || { connection_id: 42, connector_id: 'claude', state: 'setup_started', attribution: null, services: [], sees: {}, span_count: 0 }
  api.getApiKeys = async () => ({ keys: [{ key: 'ov_sk_test' }] })
  api.listAgents = async () => []
  api.getAccountUsage = async () => ({ agent_count: 0, agent_limit: null })
  api.getSaasConnections = async () => ({
    connections: [],
    stripe_oauth_configured: true,
    hubspot_oauth_configured: true,
    shopify_oauth_configured: true,
    ...saas,
  })
  api.askConnect = async (msgs) => {
    calls.ask.push(msgs)
    return ask || { answer: 'ok', options: [], code: [], connectors: [] }
  }
  // Connection instances (Phase 4/5): the guide records one when a telemetry
  // connector is chosen and polls its status instead of diffing agent names.
  calls.created = []
  calls.statusPolls = 0
  api.listConnections = async () => ({ connections: [] })
  api.createConnection = async (body) => {
    calls.created.push(body)
    return { id: 42, connector_id: body.connector_id, connection_key: 'cn_test0000000000000000ab', setup_status: 'started', state: 'setup_started' }
  }
  api.getConnectionStatus = async () => {
    calls.statusPolls += 1
    return statusReply
  }
  api.completeConnection = async () => ({ id: 42, setup_status: 'completed', state: 'waiting_for_data' })
  api.startStripeConnect = async () => { calls.start.push('stripe'); return {} }
  api.startShopifyConnect = async (shop) => { calls.start.push(`shopify:${shop}`); return {} }
  api.startHubSpotConnect = async () => { calls.start.push('hubspot'); return {} }
  return calls
}

const guide = (props = {}) =>
  React.createElement(ConnectGuide, {
    active: true, onBack: () => {}, onClose: null, onSkipToManual: () => {}, onUpgrade: null, ...props,
  })

const chip = (m, label) => m.$$('.connect-chip').find((b) => b.textContent.trim() === label)

test('the opening turn asks what to see and offers AI doors and work systems together', async () => {
  stub()
  const m = await mount(guide())
  await m.settle()
  assert.match(m.text(), /What do you want Trovis to see\?/)
  for (const label of ['OpenClaw', 'Grok Bot', 'Custom Python / other', 'Stripe', 'Shopify']) {
    assert.ok(chip(m, label), `${label} chip`)
  }
  assert.ok(!chip(m, 'Slack'), 'no coming-soon chip')
  m.unmount()
})

test('a work-system chip renders its Connect card in the thread — no model turn', async () => {
  const calls = stub()
  const m = await mount(guide())
  await m.settle()
  await m.click(chip(m, 'Shopify'))
  await m.settle()
  assert.equal(calls.ask.length, 0, 'the door is a button; nothing to ask the model')
  const card = m.$('.connect-card[data-connector="shopify"]')
  assert.ok(card, 'Shopify card')
  assert.ok(card.querySelector('input[aria-label="Shopify store domain"]'), 'Shopify asks for the shop')
  const cta = card.querySelector('button[aria-label="Connect Shopify"]')
  assert.ok(cta)
  assert.equal(cta.disabled, true, 'no shop yet → cannot connect')
  // The pick reads as the user's words, so the conversation stays straight.
  assert.ok(m.$$('.dash-msg.user').some((b) => b.textContent.trim() === 'Shopify'))
  // The card never carries the link key: that is taught where the agent is set up.
  assert.doesNotMatch(card.textContent, /trovis_loop_external_id|metadata/)
  m.unmount()
})

test('a Stripe card connects through the same door the Connections page uses', async () => {
  const calls = stub()
  const m = await mount(guide())
  await m.settle()
  await m.click(chip(m, 'Stripe'))
  await m.settle()
  const card = m.$('.connect-card[data-connector="stripe"]')
  await m.click(card.querySelector('button[aria-label="Connect Stripe"]'))
  await m.settle()
  assert.deepEqual(calls.start, ['stripe'])
  m.unmount()
})

test('an already-authorized work system says so instead of offering a second authorization', async () => {
  stub({ saas: { connections: [{ provider: 'stripe', status: 'connected', provider_account_id: 'acct_9' }] } })
  const m = await mount(guide({ initialConnector: 'stripe' }))
  await m.settle()
  const card = m.$('.connect-card[data-connector="stripe"]')
  assert.ok(card, 'initialConnector opens straight on the card')
  assert.match(card.textContent, /Already authorized as acct_9/)
  assert.ok(!card.querySelector('button[aria-label="Connect Stripe"]'))
  m.unmount()
})

test('a model reply naming a work system gets its card under the reply', async () => {
  const calls = stub({
    ask: {
      answer: 'Your Grok Bot is set. Shopify adds the order side — connect it below.',
      options: [], code: [], connectors: ['shopify', 'grok-bot'],
    },
  })
  const m = await mount(guide({ initialMessage: 'Our Grok Bot processes Shopify returns' }))
  await m.settle()
  assert.equal(calls.ask.length, 1, 'the landing sentence is the first user turn')
  const wire = calls.ask[0]
  assert.equal(wire[wire.length - 1].role, 'user')
  assert.equal(wire[wire.length - 1].content, 'Our Grok Bot processes Shopify returns')
  assert.match(m.text(), /connect it below/)
  assert.ok(m.$('.connect-card[data-connector="shopify"]'), 'Shopify card follows the reply')
  assert.ok(!m.$('.connect-card[data-connector="grok-bot"]'), 'an AI connector is walked, not carded')
  m.unmount()
})

test("a landing intent answers locally — the registry already knows 'which one?'", async () => {
  const calls = stub()
  const m = await mount(guide({
    initialMessage: 'I want to connect a work system like Stripe, HubSpot or Shopify.',
    initialLocalTurn: { content: 'Which work system?', options: ['Stripe', 'HubSpot', 'Shopify'] },
  }))
  await m.settle()
  assert.equal(calls.ask.length, 0, 'no model turn for a list the registry holds')
  assert.match(m.text(), /Which work system\?/)
  // The chips of that local turn are live (it is the latest assistant turn).
  const shopify = m.$$('.connect-chip').filter((b) => b.textContent.trim() === 'Shopify').pop()
  assert.equal(shopify.disabled, false)
  await m.click(shopify)
  await m.settle()
  assert.ok(m.$('.connect-card[data-connector="shopify"]'))
  m.unmount()
})

test('a work-system door this deploy has not configured is offered honestly, not as a dead button', async () => {
  stub({ saas: { hubspot_oauth_configured: false } })
  const m = await mount(guide({ initialConnector: 'hubspot' }))
  await m.settle()
  const card = m.$('.connect-card[data-connector="hubspot"]')
  assert.equal(card.querySelector('button[aria-label="Connect HubSpot"]').disabled, true)
  assert.match(card.textContent, /isn’t configured on this deploy yet/)
  m.unmount()
})

// --- connector-aware verification: the instance, its key, its status --------

test('picking a telemetry connector records an instance and stamps its key into snippets', async () => {
  const calls = stub({
    ask: {
      answer: 'Add these two lines.', options: [], connectors: ['claude'],
      code: [{ title: 'Init', language: 'python',
        content: 'init(api_key="TROVIS_API_KEY", agent_name="x", connection_id="TROVIS_CONNECTION_ID")' }],
    },
  })
  const m = await mount(guide())
  await m.settle()
  await m.click(chip(m, 'Claude Agent SDK / Claude Code'))
  await m.settle(20)
  assert.deepEqual(calls.created, [{ connector_id: 'claude', setup_source: 'guide' }])
  assert.match(m.text(), /connection_id="cn_test0000000000000000ab"/)
  assert.doesNotMatch(m.text(), /TROVIS_CONNECTION_ID/)
  // The lifecycle line reads the recorded fact, not an agent-name diff.
  assert.match(m.text(), /Set up Claude Agents, then run it/)
  assert.ok(calls.statusPolls >= 1, 'polls the instance status')
  m.unmount()
})

test('a work-system pick never records a telemetry instance', async () => {
  const calls = stub()
  const m = await mount(guide())
  await m.settle()
  await m.click(chip(m, 'Stripe'))
  await m.settle()
  assert.deepEqual(calls.created, [])
  m.unmount()
})

test('connected: the banner says what Trovis can see and what it cannot, with the doors that add it', async () => {
  stub({
    ask: { answer: 'ok', options: [], code: [], connectors: ['grok-bot'] },
    status: {
      connection_id: 42, connector_id: 'grok-bot', state: 'connected', attribution: 'instance',
      last_observed_at: new Date().toISOString(), services: [{ service_name: 'Trovis PM' }],
      sees: { execution: true, actions: true, model_usage: false, named_work: true }, span_count: 3,
    },
  })
  const m = await mount(guide({ initialConnector: 'grok-bot' }))
  await m.settle(20)
  assert.match(m.text(), /Grok Bot connected/)
  assert.match(m.text(), /Trovis can see: execution, tool activity, named runs/)
  assert.match(m.text(), /cannot independently see/)
  assert.ok(m.$$('.connect-chip').some((b) => b.textContent.trim() === 'Connect Shopify'))
  m.unmount()
})

test('connector-level traffic without the key is said plainly, never promoted to connected', async () => {
  stub({
    status: {
      connection_id: 42, connector_id: 'claude', state: 'setup_started', attribution: 'connector',
      services: [{ service_name: 'refund-helper' }], sees: { execution: true }, span_count: 1,
    },
  })
  const m = await mount(guide({ initialConnector: 'claude' }))
  await m.settle(20)
  assert.match(m.text(), /telemetry is arriving from refund-helper/)
  assert.match(m.text(), /does not carry this setup’s id/)
  assert.doesNotMatch(m.text(), /Claude Agents connected/)
  m.unmount()
})
