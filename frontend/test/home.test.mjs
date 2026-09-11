// Home composition rules.
//
// These are the decisions a person actually feels on Home: what counts as
// needing them, how the Work card splits the buckets, when a card disappears,
// and whether the copy reads like something a human wrote. All pure — no
// renderer.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import {
  ATTENTION_AGE_S,
  asOfLabel,
  briefingLead,
  isFirstRun,
  isShowable,
  partitionLookAt,
  workSplit,
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

test('needs attention lists needs-you first, then the rest', () => {
  const items = []
  for (let i = 1; i <= 3; i++) items.push(item({ id: i, status: 'stuck' }))
  for (let i = 4; i <= 5; i++) items.push(item({ id: i, status: 'waiting_on_you' }))
  const { needsYou, needsAttention } = partitionLookAt(items, NOW)
  const rows = [...needsYou, ...needsAttention]
  assert.equal(rows.length, 5)
  assert.deepEqual(rows.slice(0, 2).map((r) => r.status), ['waiting_on_you', 'waiting_on_you'])
})

test('the Work card splits the buckets the way the Work page does', () => {
  const items = [
    item({ id: 1, status: 'moving' }),
    item({ id: 2, status: 'moving' }),
    item({ id: 3, status: 'waiting_on_you' }),
    item({ id: 4, status: 'waiting_on_other' }),
    item({ id: 5, status: 'stuck' }),
    item({ id: 6, status: 'done' }),
  ]
  const c = workSplit(items, { completed_week: 9 })
  assert.equal(c.moving, 2)
  // Waiting is every wait — on you AND on someone else. Deliberately a wider
  // cut than needs-attention, which is only work that has stopped moving.
  assert.equal(c.waiting, 2)
  assert.equal(c.stuck, 1)
  // Done comes from the authoritative weekly count, not from this page — so
  // Home and the Work tab print the same number.
  assert.equal(c.done, 9)
})

test('Work counts ignore shell titles and survive missing data', () => {
  const c = workSplit(
    [item({ id: 1, status: 'moving', title: 'loop_42' }), item({ id: 2, status: 'moving' })],
    null,
  )
  assert.equal(c.moving, 1, 'id-shaped titles are not counted')
  // Rule 6. `done` is read off the overview payload, so no overview means we
  // were not told how much finished — not that nothing did. It was 0 here,
  // which is a count nobody counted. The item-derived tallies stay 0: those
  // come from a list that WAS supplied, and an empty list really is none.
  assert.equal(c.done, null, 'no overview yet -> unknown, never a zero')
  assert.deepEqual(workSplit(null, null), { moving: 0, waiting: 0, stuck: 0, done: null })
})

test('the briefing lead states today in plain words, and prints no figures', () => {
  // It says WHICH conditions hold; the proof strip says how many. Prose that
  // repeats a count is how the two drift apart.
  assert.equal(
    briefingLead({ needs_you: 2, needs_attention: 1, open: 9 }, { moving: 4 }),
    'Today, work is waiting on you and some work needs attention. The rest is in progress.',
  )
  // Same counts, nothing actually moving → the clause is dropped rather than
  // printed above a strip that reads 0 moving.
  assert.equal(
    briefingLead({ needs_you: 2, needs_attention: 1, open: 9 }),
    'Today, work is waiting on you and some work needs attention.',
  )
  assert.equal(
    briefingLead({ needs_you: 1, needs_attention: 0, open: 1 }),
    'Today, work is waiting on you.',
  )
  // "needs attention", never "is stuck": the bucket includes work merely
  // waiting too long on a person, which is not the same thing as stuck.
  assert.equal(
    briefingLead({ needs_you: 0, needs_attention: 3, open: 3 }),
    'Today, some work needs attention.',
  )
  // Healthy silence, not an alarm. "Everything open is in progress" is only
  // earned when a moving count says so — see homeBriefing.test.mjs.
  assert.equal(
    briefingLead({ needs_you: 0, needs_attention: 0, open: 4 }),
    'Nothing needs you right now.',
  )
  assert.equal(
    briefingLead({ needs_you: 0, needs_attention: 0, open: 4 }, { moving: 2 }),
    'Nothing needs you. Everything open is in progress.',
  )
  assert.equal(
    briefingLead({ needs_you: 0, needs_attention: 0, open: 0 }),
    'Nothing needs you right now.',
  )
  // No counts (the work call failed) → no invented state.
  assert.equal(briefingLead(null), '')
})

test('RULE 6 — a lead is never spoken from counts that were not read', () => {
  // Found by the lint rule, not the browser. `Number(null) || 0` made a
  // payload with unreadable counts indistinguishable from a genuinely calm
  // account, and the lead said "Nothing needs you right now" — a confident
  // claim about the account, from nothing.
  assert.equal(briefingLead({ needs_you: null, needs_attention: null, open: null }), '')
  assert.equal(briefingLead({}), '')
  // One readable count is enough to speak about, and the others stay silent
  // rather than being read as zero.
  assert.equal(briefingLead({ needs_you: 2, needs_attention: null, open: null }),
               'Today, work is waiting on you.')
  // A real zero still speaks — that is the distinction the rule turns on.
  assert.equal(briefingLead({ needs_you: 0, needs_attention: 0, open: 0 }),
               'Nothing needs you right now.')
})

test('RULE 6 — "nothing is connected" is never claimed from an unread count', () => {
  // The sharpest case on Home: this gates the first-run empty state, which
  // asserts something about the whole account. An unreadable `open` coerced
  // to 0 produced that assertion from no evidence.
  const empty = { overview: { open: 0 }, items: [], agentCount: 0 }
  assert.equal(isFirstRun(empty), true, 'a genuine empty account still shows it')
  assert.equal(isFirstRun({ ...empty, overview: { open: null } }), false)
  assert.equal(isFirstRun({ ...empty, overview: {} }), false)
  assert.equal(isFirstRun({ ...empty, overview: { open: '' } }), false)
})

test('the briefing lead never claims a state it did not read', () => {
  // The bug this replaces: a clause inferred from the ABSENCE of another one.
  // "Work is moving" was derived from "nothing is stuck" and printed above a
  // strip reading 0 moving. Every clause here is gated on its own count, and
  // none of them carries a digit or a dollar.
  for (const needs_you of [0, 1, 5]) {
    for (const needs_attention of [0, 1, 4]) {
      for (const open of [0, 1, 9]) {
        const s = briefingLead({ needs_you, needs_attention, open })
        assert.doesNotMatch(s, /\d/, `lead has a digit: ${s}`)
        assert.doesNotMatch(s, /[$€£]/, `lead has money: ${s}`)
        if (needs_you === 0) {
          assert.doesNotMatch(s, /waiting on you/, `claims a wait it did not read: ${s}`)
        }
        // "in progress" may only appear when something is actually open.
        if (open === 0) {
          assert.doesNotMatch(s, /in progress/, `claims progress with nothing open: ${s}`)
        }
      }
    }
  }
})

test('as-of footer is omitted rather than printing a placeholder', () => {
  assert.equal(asOfLabel(null), '')
  assert.equal(asOfLabel('not a date'), '')
  assert.match(asOfLabel('2026-03-10T12:00:00Z'), /^As of /)
})

test('first run needs every input to have LANDED empty, not merely be missing', () => {
  const empty = { overview: { open: 0 }, items: [], agentCount: 0 }
  assert.equal(isFirstRun(empty), true)
  // Still loading / failed → not first run, so we show Retry, not a story.
  assert.equal(isFirstRun({ ...empty, items: null }), false)
  assert.equal(isFirstRun({ ...empty, agentCount: null }), false)
  assert.equal(isFirstRun({ ...empty, agentCount: undefined }), false)
  assert.equal(isFirstRun({ ...empty, overview: null }), false)
  // Real work exists.
  assert.equal(isFirstRun({ ...empty, overview: { open: 3 } }), false)
  // Agents are connected and simply have not produced named work yet. That is
  // a quiet day, NOT "nothing is connected" — the difference is the whole
  // point of reading the agent count rather than only the work.
  assert.equal(isFirstRun({ ...empty, agentCount: 3 }), false)
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

test('home.js stays pure — it must not fetch', () => {
  const src = readFileSync(new URL('../src/home.js', import.meta.url), 'utf8')
  const code = src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')
  assert.doesNotMatch(code, /\bfetch\(/)
  assert.doesNotMatch(code, /from '\.\/api\.js'/)
})
