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

// --- Board derivations (the board + story build) --------------------------------

import {
  barSegments,
  boardGroups,
  boardGroupSummary,
  boardTitle,
  chainDots,
} from '../src/loops.js'

const N = 1e9 // ns per second

test('bar geometry: proportional widths, normalized to 100', () => {
  const mini = [
    { holder_type: 'agent', start_ns: 0, end_ns: 60 * N, waiting: false },
    { holder_type: 'human', start_ns: 60 * N, end_ns: 120 * N, waiting: true },
    { holder_type: 'agent', start_ns: 120 * N, end_ns: 240 * N, waiting: false },
  ]
  const slices = barSegments(mini, 300 * N)
  assert.equal(slices.length, 3)
  const total = slices.reduce((a, s) => a + s.pct, 0)
  assert.ok(Math.abs(total - 100) < 1e-6)
  assert.deepEqual(slices.map((s) => s.kind), ['agent', 'wait', 'agent'])
  assert.ok(Math.abs(slices[2].pct - 2 * slices[0].pct) < 1e-6) // 120s vs 60s
})

test('bar geometry: 3% minimum floor for slivers', () => {
  const mini = [
    { holder_type: 'agent', start_ns: 0, end_ns: 1 * N, waiting: false }, // 0.1%
    { holder_type: 'agent', start_ns: 1 * N, end_ns: 1000 * N, waiting: false },
  ]
  const slices = barSegments(mini, 2000 * N)
  assert.ok(slices[0].pct >= 2.9, `sliver got ${slices[0].pct}%`)
})

test('bar geometry: trailing unfinished agent segment renders pending; live wait stays warm', () => {
  const running = barSegments(
    [{ holder_type: 'agent', start_ns: 0, end_ns: null, waiting: false }], 100 * N)
  assert.equal(running[0].kind, 'pending')
  const waiting = barSegments(
    [{ holder_type: 'human', start_ns: 0, end_ns: null, waiting: true }], 100 * N)
  assert.equal(waiting[0].kind, 'wait')
})

test('bar geometry: abandoned loops color the final segment error', () => {
  const slices = barSegments(
    [
      { holder_type: 'agent', start_ns: 0, end_ns: 50 * N, waiting: false },
      { holder_type: 'agent', start_ns: 50 * N, end_ns: 100 * N, waiting: false },
    ],
    100 * N,
    'abandoned',
  )
  assert.equal(slices[1].kind, 'error')
})

test('bar geometry: empty/null segments_mini renders one neutral bar (legacy loops)', () => {
  assert.deepEqual(barSegments(null), [{ kind: 'pending', pct: 100 }])
  assert.deepEqual(barSegments([]), [{ kind: 'pending', pct: 100 }])
})

test('chain collapses past 4 dots, current holder marked', () => {
  const seg = (i, waiting = false) => ({
    holder_type: waiting ? 'human' : 'agent',
    start_ns: i, end_ns: i + 1, waiting,
  })
  const short = chainDots([seg(0), seg(1, true), seg(2)])
  assert.equal(short.dots.length, 3)
  assert.equal(short.collapsed, false)
  assert.ok(short.dots[2].current && !short.dots[0].current)
  const long = chainDots([seg(0), seg(1), seg(2), seg(3), seg(4), seg(5)])
  assert.equal(long.collapsed, true)
  assert.equal(long.dots.length, 4)
  assert.ok(long.dots[3].current)
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

test('board groups: matched workflows sort above unmatched agent groups', () => {
  const loops2 = [
    loop({ id: 1, service_name: 'free-bot' }),
    loop({ id: 2, service_name: 'wf-bot', workflow_id: 9, workflow_name: 'Invoice run' }),
    loop({ id: 3, service_name: 'wf-bot', workflow_id: 9, workflow_name: 'Invoice run' }),
  ]
  const groups = boardGroups(loops2, NOW)
  assert.equal(groups.length, 2)
  assert.equal(groups[0].name, 'Invoice run')
  assert.equal(groups[0].matched, true)
  assert.equal(groups[0].loops.length, 2)
  assert.equal(groups[1].name, 'free-bot')
})

test('board groups: attention-first within a group', () => {
  const loops2 = [
    loop({ id: 1, workflow_id: 9, workflow_name: 'W', cached_state: 'done' }),
    loop({ id: 2, workflow_id: 9, workflow_name: 'W', cached_state: 'stalled', stalled_for_s: 100 }),
  ]
  const g = boardGroups(loops2, NOW)[0]
  assert.equal(g.loops[0].id, 2)
})

test('group summary counts and grammar', () => {
  const today = new Date(NOW).toISOString()
  const g = {
    loops: [
      loop({ created_at: today, cached_state: 'awaiting_human', stalled_for_s: 5 }),
      loop({ created_at: today, cached_state: 'done' }),
      loop({ created_at: '2020-01-01 00:00:00', cached_state: 'done' }),
    ],
  }
  assert.equal(boardGroupSummary(g, NOW), '2 today · 1 needs you')
  const quiet = { loops: [loop({ created_at: today, cached_state: 'done' })] }
  assert.equal(boardGroupSummary(quiet, NOW), '1 today · all moving')
})

test('board title: server title wins; untitled open loops show agent identity', () => {
  assert.equal(boardTitle(loop({ title: 'Reconcile July invoices' })), 'Reconcile July invoices')
  assert.equal(boardTitle(loop({ title: null, service_name: 'ops-bot' })), 'ops-bot')
  assert.equal(
    boardTitle(loop({ title: null, service_name: 'ops-bot', agent_id: 'helper' })),
    'ops-bot · helper',
  )
})

test('jargon: no raw identifiers in board strings', () => {
  const strings = [
    boardGroupSummary({ loops: [loop({ cached_state: 'stalled', stalled_for_s: 5 })] }, NOW),
    loopStateMeta(loop({ cached_state: 'awaiting_system' }), NOW).label,
    boardTitle(loop({ title: null })),
  ]
  for (const s of strings) {
    assert.ok(!/model_call|tool_call|llm_output|span|telemetry/i.test(s), s)
    assert.ok(!s.includes('_'), `underscore leaked: ${s}`)
  }
})

// --- Workflow map derivations (the station map build) -----------------------------

import {
  WORKFLOW_STRINGS,
  buildWorkflowPayload,
  doneTodayLabel,
  mapDotLabel,
  mapDots,
  moreDotsLabel,
  saveVersionLabel,
  waitingStations,
} from '../src/loops.js'

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

test('dots position by station_index; off_path/no_stations get none', () => {
  const dots = mapDots([
    mapLoop({ id: 1, position: { status: 'on_path', station_index: 0 } }),
    mapLoop({ id: 2, position: { status: 'on_path', station_index: 2 } }),
    mapLoop({ id: 3, position: { status: 'off_path', station_index: null } }),
    mapLoop({ id: 4, position: { status: 'no_stations', station_index: null } }),
  ])
  assert.deepEqual([...dots.keys()].sort(), [0, 2])
  assert.equal(dots.get(0).dots.length, 1)
  assert.equal(dots.get(2).dots.length, 1)
})

test('stacking: waiting dots sort first; cap 3 with +n overflow', () => {
  const stack = mapDots([
    mapLoop({ id: 1, cached_state: 'working' }),
    mapLoop({ id: 2, cached_state: 'awaiting_human', stalled_for_s: 100 }),
    mapLoop({ id: 3, cached_state: 'working' }),
    mapLoop({ id: 4, cached_state: 'working' }),
    mapLoop({ id: 5, cached_state: 'working' }),
  ]).get(0)
  assert.equal(stack.dots.length, 3)
  assert.equal(stack.dots[0].id, 2) // waiting first
  assert.equal(stack.overflow, 2)
  assert.equal(moreDotsLabel(stack.overflow), '+2 more')
})

test('waiting vs working dot labels', () => {
  const w = mapLoop({ cached_state: 'awaiting_human', stalled_for_s: 3 * 3600 })
  assert.equal(mapDotLabel(w, NOW), 'Score the signup · waiting 3h')
  assert.equal(mapDotLabel(mapLoop(), NOW), 'Score the signup')
})

test('stations with waiting work get the warm treatment', () => {
  const warm = waitingStations([
    mapLoop({ id: 1, cached_state: 'awaiting_human',
              position: { status: 'on_path', station_index: 1 } }),
    mapLoop({ id: 2, cached_state: 'working',
              position: { status: 'on_path', station_index: 0 } }),
    mapLoop({ id: 3, cached_state: 'awaiting_system',
              position: { status: 'off_path', station_index: null } }),
  ])
  assert.deepEqual([...warm], [1]) // off_path never warms a station
})

test('editor payload: shapes round-trip, empties dropped, tools split', () => {
  const p = buildWorkflowPayload(
    '  Signup triage ',
    [
      { holder_type: 'agent', holder: ' triage-bot ', label: 'scores it', tools: 'exec, read' },
      { holder_type: 'human', holder: 'Sarah', label: '', tools: '' },
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
      { holder_type: 'agent', holder: 'triage-bot', label: 'scores it', tools: ['exec', 'read'] },
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

test('jargon: workflow-surface strings are clean', () => {
  const strings = [
    ...Object.values(WORKFLOW_STRINGS),
    doneTodayLabel(4),
    moreDotsLabel(2),
    saveVersionLabel(2),
    mapDotLabel(mapLoop({ cached_state: 'awaiting_system', stalled_for_s: 60 }), NOW),
  ]
  for (const s of strings) {
    assert.ok(!/model_call|tool_call|llm_output|span|telemetry|drift|conformance/i.test(s), s)
  }
})
