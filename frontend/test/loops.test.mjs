// Workloop presentation-logic tests. Pure functions only (src/loops.js has
// no React/DOM), so node's built-in test runner covers the spec's required
// cases without adding a framework:  npm test  →  node --test test/
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  LIFECYCLE_SENTENCES,
  lifecycleSentence,
  loopCostLabel,
  loopStateMeta,
  loopStuckHeadline,
  loopTitle,
  showMarkDone,
  sortLoopsAttentionFirst,
} from '../src/loops.js'

const NOW = Date.parse('2026-07-22T12:00:00Z')
const ns = (iso) => Date.parse(iso) * 1e6

function loop(over = {}) {
  return {
    id: 1,
    title: null,
    service_name: 'billing-bot',
    agent_id: 'main',
    cached_state: 'working',
    created_at: '2026-07-22 09:00:00',
    closed_at: null,
    last_event_unix: ns('2026-07-22T11:00:00Z'),
    stalled_for_s: null,
    total_cost_usd: 0,
    event_count: 3,
    ...over,
  }
}

// --- 1. Title fallback ------------------------------------------------------

test('title fallback: never "Untitled", uses agent + relative date', () => {
  const t = loopTitle(loop({ title: null, created_at: '2026-07-22T09:00:00Z' }), NOW)
  assert.match(t, /^billing-bot run · 3h ago$/)
  assert.ok(!/untitled/i.test(t))
})

test('title fallback prefers a non-main agent_id over service_name', () => {
  const t = loopTitle(loop({ agent_id: 'helper', created_at: '2026-07-22T09:00:00Z' }), NOW)
  assert.match(t, /^helper run · /)
})

test('real titles pass through untouched', () => {
  assert.equal(loopTitle(loop({ title: 'Resolve ticket 42' }), NOW), 'Resolve ticket 42')
})

// --- 2. State → visual treatment -------------------------------------------

test('awaiting_human: warning tone + attention + age in label', () => {
  const m = loopStateMeta(
    loop({ cached_state: 'awaiting_human', last_event_unix: ns('2026-07-22T09:00:00Z') }),
    NOW,
  )
  assert.equal(m.tone, 'warning')
  assert.equal(m.attention, true)
  assert.equal(m.label, 'waiting on you · 3h')
})

test('stalled: warning tone, prefers backend stalled_for_s for the age', () => {
  const m = loopStateMeta(loop({ cached_state: 'stalled', stalled_for_s: 2 * 86400 }), NOW)
  assert.equal(m.tone, 'warning')
  assert.equal(m.attention, true)
  assert.equal(m.label, 'stalled · 2d')
})

test('done: muted, no emphasis, duration from closed_at - created_at', () => {
  const m = loopStateMeta(
    loop({ cached_state: 'done', created_at: '2026-07-22 09:00:00', closed_at: '2026-07-22 09:04:00' }),
    NOW,
  )
  assert.equal(m.tone, 'muted')
  assert.equal(m.attention, false)
  assert.equal(m.label, 'done · 4 min')
})

test('done under a minute rounds up instead of suppressing', () => {
  const m = loopStateMeta(
    loop({ cached_state: 'done', created_at: '2026-07-22 09:00:00', closed_at: '2026-07-22 09:00:30' }),
    NOW,
  )
  assert.equal(m.label, 'done · under a minute')
})

test('working/open: quiet live tone; abandoned: muted', () => {
  assert.deepEqual(loopStateMeta(loop({ cached_state: 'working' }), NOW).tone, 'live')
  assert.equal(loopStateMeta(loop({ cached_state: 'open' }), NOW).label, 'running')
  const ab = loopStateMeta(loop({ cached_state: 'abandoned' }), NOW)
  assert.equal(ab.tone, 'muted')
  assert.equal(ab.label, 'abandoned')
})

test('stuck headline leads with the age as the fact', () => {
  const h = loopStuckHeadline(
    loop({ cached_state: 'awaiting_human', stalled_for_s: 3 * 3600 }),
    NOW,
  )
  assert.equal(h, 'waiting on you for 3 hours')
})

// --- 3. Attention-first sort -------------------------------------------------

test('stalled/awaiting_human float above newer done/working loops, oldest stall first', () => {
  const loops = [
    loop({ id: 10, cached_state: 'done' }), // newest (backend order)
    loop({ id: 11, cached_state: 'working' }),
    loop({ id: 12, cached_state: 'awaiting_human', stalled_for_s: 3600 }),
    loop({ id: 13, cached_state: 'stalled', stalled_for_s: 7200 }), // older stall
    loop({ id: 14, cached_state: 'done' }),
  ]
  const ids = sortLoopsAttentionFirst(loops, NOW).map((l) => l.id)
  assert.deepEqual(ids, [13, 12, 10, 11, 14])
})

// --- 4. Mark-done visibility --------------------------------------------------

test('close button: awaiting_human + session user → visible', () => {
  assert.equal(showMarkDone(loop({ cached_state: 'awaiting_human' }), true), true)
  assert.equal(showMarkDone(loop({ cached_state: 'stalled' }), true), true)
})

test('close button: hidden for terminal and running states', () => {
  for (const s of ['done', 'abandoned', 'working', 'open', 'awaiting_agent']) {
    assert.equal(showMarkDone(loop({ cached_state: s }), true), false, s)
  }
})

test('close button: hidden without a session user (api-key auth)', () => {
  assert.equal(showMarkDone(loop({ cached_state: 'awaiting_human' }), false), false)
})

// --- 5. Lifecycle sentences ----------------------------------------------------

const BACKEND_EVENT_TYPES = [
  'loop_opened',
  'loop_closed',
  'handoff_initiated',
  'handoff_accepted',
  'handoff_completed',
  'handoff_declined',
  'stall_detected',
]

test('every backend lifecycle type has a sentence; no raw identifiers leak', () => {
  for (const type of BACKEND_EVENT_TYPES) {
    assert.ok(LIFECYCLE_SENTENCES[type], `missing sentence for ${type}`)
    const s = lifecycleSentence({ type, payload: {} })
    assert.ok(s.length > 0)
    assert.ok(!s.includes('_'), `raw identifier leaked for ${type}: ${s}`)
    assert.ok(!/span|telemetry/i.test(s), `jargon in ${type}: ${s}`)
  }
})

test('sentences reflect payload: handoff direction/reason, close reasons', () => {
  assert.equal(
    lifecycleSentence({
      type: 'handoff_initiated',
      payload: { direction: 'to_human', reason: 'needs approval' },
    }),
    'Handed to a human — needs approval',
  )
  assert.equal(
    lifecycleSentence({ type: 'handoff_initiated', payload: { direction: 'to_agent' } }),
    'Handed to another agent',
  )
  // Backend-resolved names win over the generic; the generic stays the
  // honest fallback when no name resolved.
  assert.equal(
    lifecycleSentence({
      type: 'handoff_initiated',
      payload: { direction: 'to_human', target_id: 'sarah@acme.com', target_name: 'Sarah Chen', reason: 'needs approval' },
    }),
    'Handed to Sarah Chen — needs approval',
  )
  assert.equal(
    lifecycleSentence({
      type: 'handoff_initiated',
      payload: { direction: 'to_human', target_id: 'nobody@else.com' },
    }),
    'Handed to a human',
  )
  assert.equal(
    lifecycleSentence({ type: 'loop_closed', payload: { reason: 'closed_by_user' } }),
    'Marked done',
  )
  assert.equal(
    lifecycleSentence({ type: 'loop_closed', payload: { reason: 'abandoned' } }),
    'Closed automatically — no activity for 2 days',
  )
  assert.equal(
    lifecycleSentence({ type: 'loop_closed', payload: { reason: 'completed_by_agent' } }),
    'Closed by agent — completed',
  )
})

test('unknown future event types degrade to words, not identifiers', () => {
  assert.equal(lifecycleSentence({ type: 'review_requested' }), 'review requested')
})

// --- Cost label ---------------------------------------------------------------

test('cost: two decimals, omitted entirely when zero or sub-cent', () => {
  assert.equal(loopCostLabel(0.31), '$0.31')
  assert.equal(loopCostLabel(0), null)
  assert.equal(loopCostLabel(0.001), null)
  assert.equal(loopCostLabel(null), null)
})
