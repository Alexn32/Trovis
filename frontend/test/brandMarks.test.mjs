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
]

test('V1 catalog is locked — Zendesk is out', () => {
  assert.deepEqual(BRAND_ORDER, V1)
  assert.equal(Object.keys(BRANDS).sort().join(), [...V1].sort().join())
  assert.equal(resolveBrand('Zendesk'), null)
  assert.equal(resolveBrand('zendesk ticket'), null)
})

test('live doors are only the ones that work today', () => {
  assert.deepEqual(LIVE_BRAND_IDS, ['openclaw', 'claude', 'chatgpt'])
  assert.deepEqual(RECIPE_BRAND_IDS, ['cursor'])
  assert.deepEqual(COMING_BRAND_IDS, ['slack', 'github', 'hubspot', 'stripe', 'intercom'])
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
  for (const id of ['slack', 'github', 'hubspot', 'stripe', 'intercom', 'zendesk']) {
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
  for (const name of ['Slack', 'GitHub', 'HubSpot', 'Stripe', 'Intercom', 'Zendesk']) {
    assert.doesNotMatch(block, new RegExp(name))
  }
})
