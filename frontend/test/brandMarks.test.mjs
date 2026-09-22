// V1 Connect brand marks — resolver honesty + catalog lock.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import {
  BRANDS,
  BRAND_ORDER,
  COMING_BRAND_IDS,
  LIVE_BRAND_IDS,
  RECIPE_BRAND_IDS,
  brandTooltip,
  resolveBrand,
} from '../src/brandMarks.js'
import { allTiles, guideOpeningOptions, pickerTiles, recipeTiles, workSystemOptions } from '../src/connectSetup.js'

const V1 = [
  'openclaw',
  'claude',
  'cursor',
  'chatgpt',
  'grok',
  'slack',
  'github',
  'hubspot',
  'stripe',
  'intercom',
  'shopify',
]

test('V1 catalog is locked — Zendesk is out', () => {
  assert.deepEqual(BRAND_ORDER, V1)
  assert.equal(Object.keys(BRANDS).sort().join(), [...V1].sort().join())
  assert.equal(resolveBrand('Zendesk'), null)
  assert.equal(resolveBrand('zendesk ticket'), null)
})

test('live doors are only the ones that work today', () => {
  assert.deepEqual(LIVE_BRAND_IDS, ['openclaw', 'claude', 'chatgpt', 'grok', 'hubspot', 'stripe', 'shopify'])
  assert.deepEqual(RECIPE_BRAND_IDS, ['cursor'])
  assert.deepEqual(COMING_BRAND_IDS, ['slack', 'github', 'intercom'])
})

test('resolveBrand maps platform / holder / tool text; unknown is silent', () => {
  assert.equal(resolveBrand('OpenClaw Agent'), 'openclaw')
  assert.equal(resolveBrand('Claude Agent SDK / Claude Code'), 'claude')
  assert.equal(resolveBrand('openai-agents'), 'chatgpt')
  assert.equal(resolveBrand('ChatGPT (custom GPT)'), 'chatgpt')
  assert.equal(resolveBrand('Grok (xAI SDK)'), 'grok')
  assert.equal(resolveBrand('xai-sdk'), 'grok')
  assert.equal(resolveBrand('Cursor (OpenTelemetry)'), 'cursor')
  assert.equal(resolveBrand('Stripe'), 'stripe')
  assert.equal(resolveBrand('waiting on HubSpot'), 'hubspot')
  assert.equal(resolveBrand('slack-alerts'), 'slack')
  assert.equal(resolveBrand('GitHub'), 'github')
  assert.equal(resolveBrand('Intercom'), 'intercom')
  assert.equal(resolveBrand('Shopify'), 'shopify')
  assert.equal(resolveBrand('waiting on shopify'), 'shopify')
  assert.equal(resolveBrand('Python Agent'), null)
  assert.equal(resolveBrand('Trovis-instrumented Agent'), null)
  assert.equal(resolveBrand('billing-agent'), null)
  assert.equal(resolveBrand(''), null)
  assert.equal(resolveBrand(null, undefined), null)
})

test('longer alias wins when several could match', () => {
  assert.equal(resolveBrand('OpenAI Agents SDK'), 'chatgpt')
})

test('tooltips never claim a coming mark is connected', () => {
  for (const id of COMING_BRAND_IDS) {
    const t = brandTooltip(id)
    assert.match(t, /coming/i)
    assert.doesNotMatch(t, /connected/i)
  }
  assert.match(brandTooltip('cursor'), /OpenTelemetry/i)
  assert.doesNotMatch(brandTooltip('cursor'), /plugin/i)
  assert.doesNotMatch(brandTooltip('openclaw'), /connected/i)
  assert.match(brandTooltip('stripe'), /connect today/i)
  assert.match(brandTooltip('hubspot'), /connect today/i)
  assert.match(brandTooltip('shopify'), /connect today/i)
})

test('Connections exposes Stripe, HubSpot, and Shopify Connect; Add Agent stays an ingest picker', () => {
  // The doors moved from Settings → Integrations to the Connections page;
  // the honesty copy moved with them.
  const connections = readFileSync(new URL('../src/Connections.jsx', import.meta.url), 'utf8')
  // The product surface explains value and the truth boundary; how
  // correlation works belongs in setup docs, never on this page.
  assert.doesNotMatch(connections, /trovis_loop_external_id/)
  assert.doesNotMatch(connections, /Trovis loop key/)
  // The doors and their honesty copy live in one shared module (saasDoors.js)
  // so the Connections page and the guided Connect flow offer the same OAuth
  // door with the same words.
  assert.match(connections, /from '\.\/saasDoors\.js'/)
  const doors = readFileSync(new URL('../src/saasDoors.js', import.meta.url), 'utf8')
  assert.doesNotMatch(doors, /trovis_loop_external_id/)
  assert.doesNotMatch(doors, /Trovis loop key/)
  assert.match(doors, /work it can reliably link/)
  assert.match(doors, /not Trovis billing/)
  assert.match(doors, /not CRM or contact sync/)
  assert.match(doors, /not catalog, product, or/)
  assert.match(doors, /startStripeConnect/)
  assert.match(doors, /startHubSpotConnect/)
  assert.match(doors, /startShopifyConnect/)
  assert.match(doors, /disconnectStripe/)
  assert.match(doors, /disconnectHubSpot/)
  assert.match(doors, /disconnectShopify/)
  // Settings keeps a doorway, not a second manager.
  const settings = readFileSync(new URL('../src/Settings.jsx', import.meta.url), 'utf8')
  assert.match(settings, /Manage connections/)
  assert.doesNotMatch(settings, /startStripeConnect|startHubSpotConnect|startShopifyConnect/)
  assert.doesNotMatch(settings, /disconnectStripe|disconnectHubSpot|disconnectShopify/)
  assert.doesNotMatch(settings, /getSaasConnections/)
})

test('Add Agent live tiles stay the real doors; SaaS is not a picker door', () => {
  // The picker is derived from the registry (connectSetup.js), so assert on
  // the derived tiles rather than a literal array in the component.
  const live = pickerTiles().map((t) => t.id)
  for (const id of ['openclaw', 'openai-agents', 'claude', 'chatgpt', 'grok']) {
    assert.ok(live.includes(id), `${id} is a live door`)
  }
  for (const id of ['slack', 'github', 'hubspot', 'stripe', 'intercom', 'shopify', 'zendesk']) {
    assert.ok(!live.includes(id), `${id} is not a picker door`)
  }
  // Cursor may be a recipe tile, never a fake plugin.
  assert.deepEqual(recipeTiles().map((t) => t.id), ['cursor'])
  const src = readFileSync(new URL('../src/AddAgent.jsx', import.meta.url), 'utf8')
  assert.match(src, /OpenTelemetry/)
  assert.match(src, /no plugin/i)
  assert.doesNotMatch(src, /Install the Cursor plugin/i)
  for (const id of COMING_BRAND_IDS) {
    assert.ok(!allTiles().some((t) => t.id === id), `${id} has no tile`)
  }
})

test('Work holder mark uses holder name only — never whats_next', () => {
  const src = readFileSync(new URL('../src/WorkTab.jsx', import.meta.url), 'utf8')
  const quiet = src.match(/QuietBrand texts=\{\[([^\]]+)\]\}/)
  assert.ok(quiet, 'holder QuietBrand exists')
  assert.match(quiet[1], /holder\?\.name/)
  assert.doesNotMatch(quiet[1], /whats_next/)
  assert.doesNotMatch(src, /work-td-next[^>]*>[\s\S]*QuietBrand/)
  assert.match(src, /work-td-holder/)
  assert.match(src, /holderLabel/)
})

test('Work table brand marks are muted; Connect tiles stay loud', () => {
  const css = readFileSync(new URL('../src/styles.css', import.meta.url), 'utf8')
  const block = css.match(/\.work-td-holder \.brand-quiet\s*\{([^}]+)\}/)
  assert.ok(block, 'Work-table mute rule exists')
  const opacity = Number((block[1].match(/opacity:\s*([\d.]+)/) || [])[1])
  assert.ok(opacity >= 0.55 && opacity <= 0.65, `Work mute opacity ${opacity}`)
  assert.match(block[1], /--text-muted/)
  assert.doesNotMatch(css, /\.platform-card-logo[^{]*\{[^}]*opacity:\s*0\.[0-6]/)
})

test('Connect opening chips are live doors only — AI, and the work systems; never coming-soon', () => {
  // The chips come from the registry (connectSetup.js): AI guide_labels plus
  // the work systems with an OAuth door. The guide reads them, it does not
  // keep its own list. A recognised-only logo (Slack, GitHub, Intercom) is
  // never offered as something to connect.
  const src = readFileSync(new URL('../src/ConnectGuide.jsx', import.meta.url), 'utf8')
  assert.match(src, /options: \[\.\.\.guideOpeningOptions\(\), \.\.\.workSystemOptions\(\)\]/)
  const block = [...guideOpeningOptions(), ...workSystemOptions()].join('\n')
  assert.match(block, /OpenClaw/)
  assert.match(block, /OpenAI/)
  assert.match(block, /Claude/)
  assert.match(block, /ChatGPT/)
  assert.match(block, /Cursor/)
  for (const name of ['Stripe', 'HubSpot', 'Shopify']) assert.match(block, new RegExp(name))
  for (const name of ['Slack', 'GitHub', 'Intercom', 'Zendesk']) {
    assert.doesNotMatch(block, new RegExp(name))
  }
})
