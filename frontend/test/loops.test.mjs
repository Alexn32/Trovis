// Workloop presentation-logic tests. Pure functions only (src/loops.js has
// no React/DOM), so node's built-in test runner covers the spec's required
// cases without adding a framework:  npm test  →  node --test test/
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  LIFECYCLE_SENTENCES,
  groupLoopsByWorkflow,
  lifecycleSentence,
  loopCostLabel,
  loopStateMeta,
  loopStuckHeadline,
  loopTitle,
  showMarkDone,
  sortLoopsAttentionFirst,
  stuckCount,
  workflowGroupMeta,
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
  assert.equal(loopStateMeta(loop({ cached_state: 'open' }), NOW).label, 'working')
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

// --- Workflow rollup (Work tab, By-workflow view) -------------------------------

test('rollup: same service_name+agent_id collapses to one group with correct sums', () => {
  const loops = [
    loop({ id: 1, service_name: 'billing-bot', agent_id: 'main', total_cost_usd: 0.2 }),
    loop({ id: 2, service_name: 'billing-bot', agent_id: 'main', total_cost_usd: 0.11 }),
    loop({ id: 3, service_name: 'billing-bot', agent_id: 'helper', total_cost_usd: 1 }),
    loop({ id: 4, service_name: 'ops-bot', agent_id: 'main' }),
  ]
  const groups = groupLoopsByWorkflow(loops)
  assert.equal(groups.length, 3)
  const billing = groups.find((g) => g.key === 'billing-bot:main')
  assert.equal(billing.runCount, 2)
  assert.ok(Math.abs(billing.totalCost - 0.31) < 1e-9)
  assert.deepEqual(billing.loops.map((l) => l.id), [1, 2])
  assert.equal(groups.find((g) => g.key === 'billing-bot:helper').label, 'billing-bot · helper')
})

test('rollup meta: run count and summed cost strings', () => {
  const [g] = groupLoopsByWorkflow([
    loop({ id: 1, total_cost_usd: 0.2 }),
    loop({ id: 2, total_cost_usd: 0.11 }),
  ])
  const m = workflowGroupMeta(g)
  assert.equal(m.runLabel, 'ran 2×')
  assert.equal(m.cost, '$0.31')
  assert.equal(m.stateLabel, null)
  assert.equal(m.attention, false)
})

test('rollup state summary: any stalled/awaiting_human flags the group', () => {
  const [g] = groupLoopsByWorkflow([
    loop({ id: 1, cached_state: 'done' }),
    loop({ id: 2, cached_state: 'stalled', stalled_for_s: 100 }),
    loop({ id: 3, cached_state: 'awaiting_human', stalled_for_s: 50 }),
  ])
  const m = workflowGroupMeta(g)
  assert.equal(m.attention, true)
  assert.equal(m.stateLabel, '1 stalled · 1 waiting on you')
  const quiet = workflowGroupMeta(
    groupLoopsByWorkflow([loop({ id: 4, cached_state: 'done' }), loop({ id: 5 })])[0],
  )
  assert.equal(quiet.attention, false)
  assert.equal(quiet.stateLabel, null)
})

test('stuck badge count = stalled + awaiting_human loops', () => {
  assert.equal(
    stuckCount([
      loop({ cached_state: 'stalled' }),
      loop({ cached_state: 'awaiting_human' }),
      loop({ cached_state: 'working' }),
      loop({ cached_state: 'done' }),
    ]),
    2,
  )
  assert.equal(stuckCount([]), 0)
})

test('rollup strings: no raw state identifiers or jargon leak', () => {
  const [g] = groupLoopsByWorkflow([
    loop({ cached_state: 'awaiting_human', stalled_for_s: 10 }),
    loop({ cached_state: 'stalled', stalled_for_s: 20 }),
  ])
  const m = workflowGroupMeta(g)
  for (const s of [m.runLabel, m.stateLabel]) {
    assert.ok(!s.includes('_'), `raw identifier leaked: ${s}`)
    assert.ok(!/span|telemetry|awaiting_human/i.test(s), `jargon leaked: ${s}`)
  }
})

// --- Cost label ---------------------------------------------------------------

test('cost: two decimals, omitted entirely when zero or sub-cent', () => {
  assert.equal(loopCostLabel(0.31), '$0.31')
  assert.equal(loopCostLabel(0), null)
  assert.equal(loopCostLabel(0.001), null)
  assert.equal(loopCostLabel(null), null)
})

// --- Work-list + workflow-page derivations -----------------------------------

import {
  WORKFLOW_STRINGS,
  boardTitle,
  buildWorkflowPayload,
  chainGlyph,
  doneTodayLabel,
  heatLabel,
  inMotionLine,
  isCreatedToday,
  isDoneToday,
  moreItemsLabel,
  otherWorkLine,
  saveVersionLabel,
  stationHeat,
  stationName,
  workflowChip,
  workflowShapeLine,
} from '../src/loops.js'

const N = 1e9 // ns per second

test('board title: server title wins; untitled open loops show agent identity', () => {
  assert.equal(boardTitle(loop({ title: 'Reconcile July invoices' })), 'Reconcile July invoices')
  assert.equal(boardTitle(loop({ title: null, service_name: 'ops-bot' })), 'ops-bot')
  assert.equal(
    boardTitle(loop({ title: null, service_name: 'ops-bot', agent_id: 'helper' })),
    'ops-bot · helper',
  )
})

test('unknown/future states render neutrally, never crash', () => {
  const m = loopStateMeta(loop({ cached_state: 'awaiting_review' }), NOW)
  assert.equal(m.tone, 'muted')
  assert.equal(m.label, 'awaiting review') // humanized, no underscore
  assert.ok(!m.label.includes('_'))
})

test('awaiting_system: warning attention state, in the stuck set', () => {
  const m = loopStateMeta(
    loop({ cached_state: 'awaiting_system', stalled_for_s: 3600 }),
    NOW,
  )
  assert.equal(m.tone, 'warning')
  assert.equal(m.attention, true)
  assert.equal(m.label, 'waiting on a system · 1h')
  assert.equal(stuckCount([loop({ cached_state: 'awaiting_system' })]), 1)
  assert.equal(showMarkDone(loop({ cached_state: 'awaiting_system' }), true), true)
})

// --- The Work list (level 1) ---------------------------------------------------

const wfStations = [
  { holder_type: 'agent', holder: 'triage-agent', label: 'scores the signup' },
  { holder_type: 'human', holder: 'Sarah' },
  { holder_type: 'agent', holder: 'triage-agent', label: 'files the outcome' },
]

test('shape line: steps · cast · today', () => {
  const wf = { stations: wfStations, loops_today: 14 }
  assert.equal(workflowShapeLine(wf), '3 steps · triage-agent + you · 14 today')
  assert.equal(
    workflowShapeLine({ stations: [], loops_today: 0 }),
    '0 steps · none today',
  )
  assert.equal(
    workflowShapeLine({ stations: [wfStations[0]], loops_today: 1 }),
    '1 step · triage-agent · 1 today',
  )
})

test('chip: warm when a human holds work, with count and age', () => {
  const c = workflowChip({
    loop_counts: { awaiting_human: 1, working: 3 },
    needs_you_for_s: 3 * 3600,
  })
  assert.equal(c.warm, true)
  assert.equal(c.label, '1 waiting on you · 3h')
})

test('chip: quiet running / quiet today otherwise', () => {
  assert.deepEqual(workflowChip({ loop_counts: { working: 2 } }),
    { label: 'running', warm: false })
  assert.deepEqual(workflowChip({ loop_counts: { done: 9 } }),
    { label: 'quiet today', warm: false })
})

test('other-work line grammar', () => {
  assert.equal(otherWorkLine(6), 'Other agent work · 6 runs today ›')
  assert.equal(otherWorkLine(1), 'Other agent work · 1 run today ›')
})

test('created-today / done-today calendar checks', () => {
  assert.equal(isCreatedToday(loop({ created_at: new Date(NOW).toISOString() }), NOW), true)
  assert.equal(isCreatedToday(loop({ created_at: '2020-01-01 00:00:00' }), NOW), false)
  assert.equal(
    isDoneToday(loop({ cached_state: 'done', closed_at: new Date(NOW).toISOString() }), NOW),
    true,
  )
  assert.equal(
    isDoneToday(loop({ cached_state: 'working', closed_at: new Date(NOW).toISOString() }), NOW),
    false,
  )
})

// --- The workflow drawing (level 2) ----------------------------------------------

function mapLoop(over = {}) {
  return {
    id: 1,
    title: 'Score the signup',
    cached_state: 'working',
    last_event_unix: (NOW - 3600 * 1000) * 1e6,
    service_name: 'triage-bot',
    agent_id: 'main',
    position: { status: 'on_path', station_index: 0 },
    ...over,
  }
}

test('heat: on-path loops warm their station with count + oldest age', () => {
  const heat = stationHeat(
    [
      mapLoop({ id: 1, last_event_unix: (NOW - 3 * 3600 * 1000) * 1e6 }),
      mapLoop({ id: 2, last_event_unix: (NOW - 600 * 1000) * 1e6 }),
      mapLoop({ id: 3, position: { status: 'off_path', station_index: null } }),
    ],
    NOW,
  )
  assert.deepEqual([...heat.keys()], [0]) // off_path never warms a station
  assert.equal(heat.get(0).count, 2)
  assert.equal(heatLabel(heat.get(0)), '2 here · 3h')
})

test('station names: holders win, honest fallbacks otherwise', () => {
  assert.equal(stationName({ holder_type: 'agent', holder: 'triage-agent' }), 'triage-agent')
  assert.equal(stationName({ holder_type: 'human' }), 'a person')
  assert.equal(stationName({ holder_type: 'system' }), 'a system')
  assert.equal(stationName({ holder_type: 'agent' }), 'an agent')
})

test('in-motion line: station position + age, honest fallback off the path', () => {
  const l = loop({ last_event_unix: (NOW - 12 * 60 * 1000) * 1e6 })
  assert.equal(inMotionLine(l, { holder_type: 'human', holder: 'Sarah' }, NOW), 'with Sarah · 12m')
  assert.equal(inMotionLine(l, null, NOW), 'moving · 12m')
})

test('chain glyph: readable breadcrumb from segments_mini, no names invented', () => {
  const seg = (t, w = false) => ({ holder_type: t, start_ns: 0, end_ns: 1, waiting: w })
  const l = loop({
    service_name: 'triage-agent',
    segments_mini: [seg('agent'), seg('human', true), seg('agent')],
  })
  assert.equal(chainGlyph(l), 'tr → you → tr')
  assert.equal(chainGlyph(loop({ segments_mini: [] })), '')
  const long = loop({
    service_name: 'triage-agent',
    segments_mini: [seg('agent'), seg('human'), seg('agent'), seg('system'), seg('agent'), seg('human')],
  })
  assert.equal(chainGlyph(long), 'tr → you → … → tr → you')
})

test('done + more labels', () => {
  assert.equal(doneTodayLabel(4), 'Done today · 4')
  assert.equal(moreItemsLabel(2), '+2 more ›')
})

test('editor payload: shapes round-trip, empties dropped, tools split, carrier kept', () => {
  const p = buildWorkflowPayload(
    '  Signup triage ',
    [
      { holder_type: 'agent', holder: ' triage-bot ', label: 'scores it', tools: 'exec, read', carrier: ' Slack ' },
      { holder_type: 'human', holder: 'Sarah', label: '', tools: '', carrier: '' },
    ],
    [
      { field: 'service_name', op: 'equals', value: ' triage-bot ' },
      { field: 'title', op: 'contains', value: '' }, // dropped: no value
    ],
    ' first cut ',
  )
  assert.deepEqual(p, {
    name: 'Signup triage',
    stations: [
      { holder_type: 'agent', holder: 'triage-bot', label: 'scores it', carrier: 'Slack', tools: ['exec', 'read'] },
      { holder_type: 'human', holder: 'Sarah' },
    ],
    match_hints: [{ field: 'service_name', op: 'equals', value: 'triage-bot' }],
    note: 'first cut',
  })
})

test('version-bump button labeling', () => {
  assert.equal(saveVersionLabel(0), 'Create workflow')
  assert.equal(saveVersionLabel(3), 'Save as v4')
})

test('jargon: every workflow-surface string is clean', () => {
  const strings = [
    ...Object.values(WORKFLOW_STRINGS),
    workflowShapeLine({ stations: wfStations, loops_today: 3 }),
    workflowChip({ loop_counts: { stalled: 1 }, needs_you_for_s: 60 }).label,
    workflowChip({ loop_counts: {} }).label,
    otherWorkLine(2),
    heatLabel({ count: 2, oldestS: 3600 }),
    inMotionLine(loop(), { holder_type: 'agent', holder: 'ops-bot' }, NOW),
    chainGlyph(loop({ segments_mini: [{ holder_type: 'system', start_ns: 0, end_ns: 1 }] })),
    doneTodayLabel(4),
    moreItemsLabel(2),
    saveVersionLabel(2),
    boardTitle(loop({ title: null })),
    loopStateMeta(loop({ cached_state: 'awaiting_system' }), NOW).label,
  ]
  for (const s of strings) {
    assert.ok(
      !/model_call|tool_call|llm_output|span|telemetry|drift|conformance|loop_|cached|workflow_id/i.test(s),
      `jargon leaked: ${s}`,
    )
    assert.ok(!/_/.test(s), `underscore leaked: ${s}`)
  }
})
