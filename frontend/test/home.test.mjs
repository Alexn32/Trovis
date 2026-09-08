// Home v2 composition rules.
//
// These are the decisions a person actually feels on Home: what counts as
// needing them, what earns a briefing bullet, when a section disappears, and
// whether the copy reads like something a human wrote. All pure — no renderer.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import {
  ATTENTION_AGE_S,
  asOfLabel,
  briefingBullets,
  briefingLead,
  isFirstRun,
  isShowable,
  lookAtRows,
  partitionLookAt,
  showCostPulse,
} from '../src/home.js'

const NOW = Date.parse('2026-03-10T12:00:00Z')
const agoS = (s) => new Date(NOW - s * 1000).toISOString()

function item(over = {}) {
  return {
    id: over.id ?? 1,
    title: over.title ?? 'Approve refund #4821',
    status: over.status ?? 'moving',
    holder: over.holder ?? { kind: 'agent', name: 'Support Bot' },
    whats_next: over.whats_next ?? 'In progress',
    updated_at: over.updated_at ?? agoS(60),
  }
}

test('needs you is waiting_on_you and nothing else', () => {
  const items = [
    item({ id: 1, status: 'waiting_on_you' }),
    item({ id: 2, status: 'moving' }),
    item({ id: 3, status: 'waiting_on_other', updated_at: agoS(60) }),
  ]
  const { needsYou } = partitionLookAt(items, NOW)
  assert.deepEqual(needsYou.map((i) => i.id), [1])
})

test('needs attention is stuck plus AGING waiting_on_other — fresh waits stay quiet', () => {
  const items = [
    item({ id: 1, status: 'stuck' }),
    item({ id: 2, status: 'waiting_on_other', updated_at: agoS(ATTENTION_AGE_S - 60) }),
    item({ id: 3, status: 'waiting_on_other', updated_at: agoS(ATTENTION_AGE_S + 60) }),
  ]
  const { needsAttention } = partitionLookAt(items, NOW)
  assert.deepEqual(needsAttention.map((i) => i.id).sort(), [1, 3])
})

test('the aging threshold mirrors the server stall threshold (4h)', () => {
  assert.equal(ATTENTION_AGE_S, 14400)
})

test('waiting_on_you is never double-counted into needs attention', () => {
  // Even long past the stall threshold, work waiting on YOU is "needs you".
  const items = [item({ id: 1, status: 'waiting_on_you', updated_at: agoS(ATTENTION_AGE_S * 3) })]
  const { needsYou, needsAttention } = partitionLookAt(items, NOW)
  assert.equal(needsYou.length, 1)
  assert.equal(needsAttention.length, 0)
})

test('done work never appears on Home', () => {
  const { needsYou, needsAttention } = partitionLookAt(
    [item({ id: 1, status: 'done' })],
    NOW,
  )
  assert.equal(needsYou.length + needsAttention.length, 0)
})

test('shell and id-shaped titles are gated out even if the API sends them', () => {
  assert.equal(isShowable(item({ title: 'Approve refund #4821' })), true)
  assert.equal(isShowable(item({ title: 'loop_42' })), false)
  assert.equal(isShowable(item({ title: '550e8400-e29b-41d4-a716-446655440000' })), false)
  assert.equal(isShowable(item({ title: '' })), false)
})

test('look-at puts needs-you first, caps the list, and reports the remainder', () => {
  const items = []
  for (let i = 1; i <= 6; i++) items.push(item({ id: i, status: 'stuck' }))
  for (let i = 7; i <= 9; i++) items.push(item({ id: i, status: 'waiting_on_you' }))
  const { rows, hidden, needsYouCount, needsAttentionCount } = lookAtRows(items, {
    nowMs: NOW,
    max: 7,
  })
  assert.equal(rows.length, 7)
  assert.equal(hidden, 2)
  assert.equal(needsYouCount, 3)
  assert.equal(needsAttentionCount, 6)
  // The first three rows are the ones waiting on the person.
  assert.deepEqual(rows.slice(0, 3).map((r) => r.status), [
    'waiting_on_you',
    'waiting_on_you',
    'waiting_on_you',
  ])
})

test('briefing bullets cap at 3 / 3 / 2 and omit moving when there is none', () => {
  const items = []
  for (let i = 1; i <= 5; i++) items.push(item({ id: i, status: 'waiting_on_you' }))
  for (let i = 6; i <= 10; i++) items.push(item({ id: i, status: 'stuck' }))
  const b = briefingBullets(items, NOW)
  assert.equal(b.needsYou.length, 3)
  assert.equal(b.stuck.length, 3)
  assert.equal(b.moving.length, 0)

  const withMoving = briefingBullets(
    [item({ id: 1, status: 'moving' }), item({ id: 2, status: 'moving' }), item({ id: 3, status: 'moving' })],
    NOW,
  )
  assert.equal(withMoving.moving.length, 2)
})

test('the lead sentence states today in plain words, and survives a missing briefing', () => {
  assert.equal(
    briefingLead({ needs_you: 2, needs_attention: 1, open: 9 }),
    '2 things need you and 1 needs attention.',
  )
  assert.equal(
    briefingLead({ needs_you: 1, needs_attention: 0, open: 4 }),
    '1 thing needs you.',
  )
  // "need attention", never "are stuck": the bucket includes work merely
  // waiting too long on a person, which is not the same thing as stuck.
  assert.equal(
    briefingLead({ needs_you: 0, needs_attention: 3, open: 5 }),
    '3 need attention.',
  )
  // Healthy silence, not an alarm.
  assert.equal(
    briefingLead({ needs_you: 0, needs_attention: 0, open: 4 }),
    'Nothing needs you. 4 things in progress.',
  )
  assert.equal(
    briefingLead({ needs_you: 0, needs_attention: 0, open: 0 }),
    'Nothing needs you right now.',
  )
  // No counts (the work call failed) → no invented state.
  assert.equal(briefingLead(null), '')
})

test('as-of footer is omitted rather than printing a placeholder', () => {
  assert.equal(asOfLabel(null), '')
  assert.equal(asOfLabel('not a date'), '')
  assert.match(asOfLabel('2026-03-10T12:00:00Z'), /^As of /)
})

test('cost pulse hides at $0 and at sub-cent noise', () => {
  assert.equal(showCostPulse(null), false)
  assert.equal(showCostPulse({ today: 0 }), false)
  assert.equal(showCostPulse({ today: 0.004 }), false)
  assert.equal(showCostPulse({ today: 0.68 }), true)
})

test('first run needs every section to have LANDED empty, not merely be missing', () => {
  const empty = { overview: { open: 0 }, items: [], feed: [] }
  assert.equal(isFirstRun(empty), true)
  // Still loading / failed → not first run, so we show Retry, not a story.
  assert.equal(isFirstRun({ ...empty, items: null }), false)
  assert.equal(isFirstRun({ ...empty, feed: null }), false)
  assert.equal(isFirstRun({ ...empty, overview: null }), false)
  // Real work exists.
  assert.equal(isFirstRun({ ...empty, overview: { open: 3 } }), false)
})

// --- copy + module discipline ----------------------------------------------

const FORBIDDEN = /\b(loops?|workloops?|possession|segments?|stations?|handoffs?|span|otel|telemetry)\b/i

function userFacingStrings(code) {
  const out = []
  // JSX text between tags, plus quoted strings in prop position.
  for (const m of code.matchAll(/>([^<>{}]+)</g)) {
    const s = m[1].trim()
    if (s && /[a-z]/i.test(s)) out.push(s)
  }
  for (const m of code.matchAll(/(?:lead|emptyText|label|title|placeholder)=["']([^"']+)["']/g)) {
    out.push(m[1])
  }
  return out
}

test('no Trovis jargon on Home', () => {
  const src = readFileSync(new URL('../src/Dashboard.jsx', import.meta.url), 'utf8')
  const code = src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')
  for (const s of userFacingStrings(code)) {
    assert.ok(!FORBIDDEN.test(s), `Home ships jargon: ${JSON.stringify(s)}`)
  }
})

test('home.js stays pure — it must not fetch', () => {
  const src = readFileSync(new URL('../src/home.js', import.meta.url), 'utf8')
  const code = src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')
  assert.doesNotMatch(code, /\bfetch\(/)
  assert.doesNotMatch(code, /from '\.\/api\.js'/)
})
