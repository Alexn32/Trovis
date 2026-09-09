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

const V1 = [
  'openclaw',
  'claude',
  'cursor',
  'chatgpt',
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
  assert.deepEqual(LIVE_BRAND_IDS, ['openclaw', 'claude', 'chatgpt', 'hubspot', 'stripe', 'shopify'])
  assert.deepEqual(RECIPE_BRAND_IDS, ['cursor'])
  assert.deepEqual(COMING_BRAND_IDS, ['slack', 'github', 'intercom'])
})

test('resolveBrand maps platform / holder / tool text; unknown is silent', () => {
  assert.equal(resolveBrand('OpenClaw Agent'), 'openclaw')
  assert.equal(resolveBrand('Claude Agent SDK / Claude Code'), 'claude')
  assert.equal(resolveBrand('openai-agents'), 'chatgpt')
  assert.equal(resolveBrand('ChatGPT (custom GPT)'), 'chatgpt')
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

test('Settings exposes Stripe, HubSpot, and Shopify Connect; Add Agent stays an ingest picker', () => {
  const settings = readFileSync(new URL('../src/Settings.jsx', import.meta.url), 'utf8')
  assert.match(settings, /Connect Stripe/)
  assert.match(settings, /Connect HubSpot/)
  assert.match(settings, /Connect Shopify/)
  assert.match(settings, /trovis_loop_external_id/)
  assert.match(settings, /not Trovis billing/)
  assert.match(settings, /not CRM or contact sync/)
  assert.match(settings, /not catalog, product, or/)
  assert.match(settings, /startStripeConnect/)
  assert.match(settings, /startHubSpotConnect/)
  assert.match(settings, /startShopifyConnect/)
})

test('Add Agent live tiles stay the real doors; SaaS is not a picker door', () => {
  const src = readFileSync(new URL('../src/AddAgent.jsx', import.meta.url), 'utf8')
  // Live PLATFORMS block — the four doors that work today.
  const liveBlock = src.match(/const PLATFORMS = \[([\s\S]*?)\]/)
  assert.ok(liveBlock, 'PLATFORMS array exists')
  const live = liveBlock[1]
  for (const id of ['openclaw', 'openai-agents', 'claude', 'chatgpt']) {
    assert.match(live, new RegExp(`id: '${id}'`))
  }
  for (const id of ['slack', 'github', 'hubspot', 'stripe', 'intercom', 'shopify', 'zendesk']) {
    assert.doesNotMatch(live, new RegExp(`id: '${id}'`))
  }
  // Cursor may be a recipe tile, never a fake plugin.
  const recipe = src.match(/const RECIPE_PLATFORMS = \[([\s\S]*?)\]/)
  assert.ok(recipe, 'RECIPE_PLATFORMS exists')
  assert.match(recipe[1], /id: 'cursor'/)
  assert.match(src, /OpenTelemetry/)
  assert.match(src, /no plugin/i)
  assert.doesNotMatch(src, /Install the Cursor plugin/i)
  for (const id of COMING_BRAND_IDS) {
    assert.doesNotMatch(src, new RegExp(`id: '${id}'`))
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

test('Connect opening chips are live/recipe only — no SaaS doors', () => {
  const src = readFileSync(new URL('../src/ConnectGuide.jsx', import.meta.url), 'utf8')
  const opts = src.match(/options: \[([\s\S]*?)\]/)
  assert.ok(opts, 'opening options exist')
  const block = opts[1]
  assert.match(block, /OpenClaw/)
  assert.match(block, /OpenAI/)
  assert.match(block, /Claude/)
  assert.match(block, /ChatGPT/)
  assert.match(block, /Cursor/)
  for (const name of ['Slack', 'GitHub', 'HubSpot', 'Stripe', 'Intercom', 'Shopify', 'Zendesk']) {
    assert.doesNotMatch(block, new RegExp(name))
  }
})
