// One module holds the work-system OAuth doors; both the Connections page
// and the guided Connect flow read it, so "Connect Shopify" is the same door
// wherever it is clicked.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { SAAS_DOORS, doorFor } from '../src/saasDoors.js'
import { CONNECTORS } from '../src/connectors.js'
import { connectRequest, parsePath, saasReturn } from '../src/route.js'

const src = (f) => readFileSync(new URL(`../src/${f}`, import.meta.url), 'utf8')

test('the doors are exactly the available OAuth connectors in the registry', () => {
  const oauth = CONNECTORS.filter((c) => c.availability === 'available' && c.setup_type === 'oauth').map((c) => c.id)
  assert.deepEqual(Object.keys(SAAS_DOORS).sort(), [...oauth].sort())
  for (const id of oauth) {
    const d = doorFor(id)
    assert.ok(d, id)
    for (const k of ['start', 'disconnect', 'configured', 'notConfigured', 'startError', 'disconnectError', 'fineprint']) {
      assert.ok(d[k], `${id}.${k}`)
    }
    assert.match(d.fineprint, /work it can reliably link/, `${id} states the truth boundary`)
    assert.doesNotMatch(d.fineprint, /trovis_loop_external_id|metadata/, `${id} carries no implementation detail`)
  }
  assert.equal(doorFor('openclaw'), null, 'a telemetry connector has no OAuth door')
  assert.equal(doorFor('slack'), null, 'coming soon has no door')
  assert.equal(doorFor('nope'), null)
  assert.equal(SAAS_DOORS.shopify.needsShop, true)
})

test('both surfaces read the shared module and neither keeps its own door table', () => {
  const page = src('Connections.jsx')
  const guide = src('ConnectGuide.jsx')
  assert.match(page, /from '\.\/saasDoors\.js'/)
  assert.match(guide, /from '\.\/saasDoors\.js'/)
  assert.doesNotMatch(page, /const SAAS_DOORS/)
  assert.doesNotMatch(guide, /startStripeConnect|startShopifyConnect|startHubSpotConnect/)
})

// --- the two one-shot query flags App reads at boot -------------------------

test('/connections?connect=<id> is a request to open Connect on that connector', () => {
  assert.equal(connectRequest('/connections', '?connect=stripe'), 'stripe')
  assert.equal(connectRequest('/connections', 'connect=grok-bot&x=1'), 'grok-bot')
  assert.equal(connectRequest('/connections', ''), null)
  assert.equal(connectRequest('/work', '?connect=stripe'), null, 'only on the Connections path')
  assert.equal(connectRequest('/connections', '?connect=Not%20An%20Id'), null, 'registry-shaped ids only')
  // A request, not a place: the view is unchanged and builds no URL for it.
  assert.deepEqual(parsePath('/connections'), { tab: 'connections', job: null, run: null })
})

test('the SaaS OAuth return flag is read into {provider, ok}', () => {
  assert.deepEqual(saasReturn('?saas=stripe_connected'), { provider: 'stripe', ok: true })
  assert.deepEqual(saasReturn('saas=shopify_error'), { provider: 'shopify', ok: false })
  assert.equal(saasReturn('?saas=stripe'), null)
  assert.equal(saasReturn(''), null)
})
