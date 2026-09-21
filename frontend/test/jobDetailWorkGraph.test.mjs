// The Run page's "What happened" section, mounted: the Work Graph as the
// operational story under the header, its own loading / failure / retry, no
// stale run, truthful sparse state, possession from the endpoint only, and
// exact doors into Execution and Evidence.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { deferred, installDom, mount } from './mount.mjs'

installDom()

const React = await import('react')
const { api } = await import('../src/api.js')
const JobDetail = (await import('../src/JobDetail.jsx')).default

const T = '2026-09-18T10:03:14+00:00'
const later = (s) => new Date(Date.parse(T) + s * 1000).toISOString()
const NOW = Date.now()
const ago = (m) => new Date(NOW - m * 60000).toISOString()

// --- fixtures ---------------------------------------------------------------------

const detailFor = (id, title, over = {}) => ({
  id, title, status: 'waiting_on_other', holder: { kind: 'agent', name: 'returns-bot' },
  whats_next: null, updated_at: ago(3), awaiting_handoff_event_id: null, workflow_id: 7, workflow_name: 'Returns',
  whats_happening: null, process: null,
  timeline: [
    { at: ago(30), text: 'Started', actor: { kind: 'agent', name: 'returns-bot' } },
    { at: ago(10), text: 'Waiting on someone', actor: { kind: 'agent', name: 'returns-bot' } },
  ],
  provenance: { source: 'telemetry', suggestion_id: null },
  ...over,
})
const DETAILS = { 41: detailFor(41, 'Return #4471'), 42: detailFor(42, 'Return #4472') }
const RUNS = [{ name: 'tool_call', agent: 'returns-bot', service_name: 'returns-bot', agent_id: 'main', at: ago(25), errored: false, duration_ms: 1400, cost_usd: null, tool: 'stripe.refunds.create', error: null }]

const prov = (over = {}) => ({
  event_id: 10, evidence_id: 'event:10', evidence_kind: 'handoff', execution_node_id: 'event:10',
  span_id: 'aaaaaaaaaaaaaaaa', trace_id: 'a'.repeat(32), correlation: 'explicit_key', source_type: 'agent',
  source_connector_id: 'grok-bot', external_object_id: null, external_event_id: null, ...over,
})
const AGENT = { type: 'agent', label: 'returns-bot:main' }
const SARAH = { type: 'human', label: 'Sarah Chen' }
const STRIPE = { type: 'system', label: 'Stripe' }
const STRIPE_SYS = { label: 'Stripe', provider: 'stripe', target_id: 'Stripe' }

const S_HANDOFF = {
  id: 'work-step:event:10', type: 'handoff', at: later(0), label: 'Handed to a person', actor: AGENT, system: null,
  details: { direction: 'to_human', handoff_id: 'h-1', reason: 'over limit', target_label: 'Sarah Chen', target_id: null },
  provenance: prov(),
}
const S_DECLINED = {
  id: 'work-step:event:11', type: 'exception', at: later(120), label: 'Handoff declined', actor: SARAH, system: null,
  details: { handoff_id: 'h-1', reason: null },
  provenance: prov({ event_id: 11, evidence_id: 'event:11', execution_node_id: 'event:11', span_id: null, trace_id: null, correlation: 'direct', source_type: 'human', source_connector_id: null }),
}
const S_WAIT = {
  id: 'work-step:event:12', type: 'wait', at: later(180), label: 'Waiting on Stripe', actor: STRIPE, system: STRIPE_SYS,
  details: { direction: 'to_system', handoff_id: 'saas:stripe:pi_4471', reason: null, waiting_on: 'payment processing', saas_provider: 'stripe', saas_effect: 'wait', saas_object_id: 'pi_4471', saas_event_type: 'payment_intent.processing', saas_event_id: 'evt_proc_1' },
  provenance: prov({ event_id: 12, evidence_id: 'event:12', evidence_kind: 'external_state', execution_node_id: 'event:12', span_id: null, trace_id: null, source_type: 'system', source_connector_id: 'stripe', external_object_id: 'pi_4471', external_event_id: 'evt_proc_1' }),
}
const S_STUCK = {
  id: 'work-step:event:13', type: 'exception', at: later(300), label: 'Stuck on Stripe', actor: STRIPE, system: STRIPE_SYS,
  details: { direction: 'to_system', handoff_id: 'saas:stripe:pi_4471', reason: 'Your card was declined', waiting_on: null, saas_provider: 'stripe', saas_effect: 'stuck', saas_object_id: 'pi_4471', saas_event_type: 'payment_intent.payment_failed', saas_event_id: 'evt_fail_3' },
  provenance: prov({ event_id: 13, evidence_id: 'event:13', evidence_kind: 'external_state', execution_node_id: 'event:13', span_id: null, trace_id: null, source_type: 'system', source_connector_id: 'stripe', external_object_id: 'pi_4471', external_event_id: 'evt_fail_3' }),
}
const S_STALL = {
  id: 'work-step:event:14', type: 'exception', at: later(420), label: 'Stall detected', actor: { type: 'system', label: 'Trovis' }, system: null,
  details: { reason: 'no activity for 4h', detail: null },
  provenance: prov({ event_id: 14, evidence_id: null, evidence_kind: null, execution_node_id: 'event:14', span_id: null, trace_id: null, correlation: 'direct', source_type: 'system', source_connector_id: null }),
}
const S_CLOSED = {
  id: 'work-step:event:15', type: 'completed', at: later(660), label: 'Work record closed', actor: AGENT, system: null,
  details: { reason: 'completed_by_agent', detail: null, abandoned: false, outcome: 'record_closed' },
  provenance: prov({ event_id: 15, evidence_id: 'event:15', evidence_kind: 'completion', execution_node_id: 'event:15', span_id: 'bbbbbbbbbbbbbbbb' }),
}
const STEPS = [S_HANDOFF, S_DECLINED, S_WAIT, S_STUCK, S_STALL, S_CLOSED]
const SEGMENTS = [
  { holder_type: 'agent', holder: 'returns-bot:main', start: later(-60), end: later(0), waiting: false, touches: [{ name: 'stripe.refunds.create', count: 2 }], event_count: 3 },
  { holder_type: 'human', holder: 'Sarah Chen', start: later(0), end: later(120), waiting: true, touches: [], event_count: 1 },
  { holder_type: 'agent', holder: 'returns-bot:main', start: later(120), end: later(180), waiting: false, touches: [], event_count: 0 },
  { holder_type: 'system', holder: 'Stripe', start: later(180), end: later(600), waiting: true, touches: [], event_count: 0 },
  { holder_type: 'agent', holder: 'returns-bot:main', start: later(600), end: later(660), waiting: false, touches: [], event_count: 0 },
]
const LIFECYCLE = [
  { id: 'event:9', event_id: 9, type: 'loop_opened', at: later(-60), actor: AGENT, step_id: null, possession_only: false },
  { id: 'event:10', event_id: 10, type: 'handoff_initiated', at: later(0), actor: AGENT, direction: 'to_human', handoff_id: 'h-1', step_id: 'work-step:event:10', possession_only: false },
  { id: 'event:16', event_id: 16, type: 'handoff_completed', at: later(90), actor: SARAH, handoff_id: 'h-1', step_id: null, possession_only: true },
  { id: 'event:17', event_id: 17, type: 'handoff_completed', at: later(600), actor: STRIPE, saas_provider: 'stripe', saas_event_type: 'payment_intent.succeeded', external_object_id: 'pi_4471', external_event_id: 'evt_ok_2', step_id: null, possession_only: true },
]
function graphFor(id, over = {}) {
  return {
    item_id: id, generated_at: T, steps: STEPS, chronology: STEPS.map((s) => s.id),
    possession: { segments: SEGMENTS, current_holder: null }, lifecycle: LIFECYCLE,
    bounded: false, span_limit: 2000, spans_read: 7, events_read: 9,
    summary: { steps: 6, handoffs: 1, waits: 1, exceptions: 3, completed: 1, progress: 0, execution_only: { spans_read: 7, tool_call_spans: 3, errored_spans: 1, bounded: false } },
    ...over,
  }
}
/** An open run: one step by the agent, held by Stripe — the holder is not the last step's actor. */
const OPEN_GRAPH = graphFor(41, {
  steps: [S_HANDOFF, S_WAIT], chronology: [S_HANDOFF.id, S_WAIT.id],
  possession: { segments: SEGMENTS.slice(0, 4).map((s, i) => (i === 3 ? { ...s, end: null } : s)), current_holder: { ...SEGMENTS[3], end: null } },
})
const EMPTY_GRAPH = graphFor(41, { steps: [], chronology: [], possession: { segments: [SEGMENTS[0]], current_holder: { ...SEGMENTS[0], end: null } }, lifecycle: [LIFECYCLE[0]], summary: { steps: 0, execution_only: { spans_read: 7, tool_call_spans: 3, errored_spans: 1, bounded: false } } })

const rec = (over) => ({
  id: over.id, item_id: 41, evidence_type: over.evidence_type, observed_at: over.observed_at || later(0),
  source_type: 'agent', source_connector_id: 'grok-bot', source_label: 'returns-bot:main', correlation_method: 'explicit_key',
  event_id: null, span_id: null, trace_id: null, external_object_id: null, external_event_id: null, details: {}, ...over,
})
const EVIDENCE = { item_id: 41, generated_at: T, spans_truncated: false, evidence: [
  rec({ id: 'exec:grok-bot', evidence_type: 'execution', observed_at: later(-60), details: { span_count: 7, error_count: 1, last_observed_at: later(600), trace_ids: [], correlation_methods: ['explicit_key'] } }),
  rec({ id: 'span:t2', evidence_type: 'action_reported', observed_at: later(-30), span_id: 'cccccccccccccccc', details: { tool: 'stripe.refunds.create', span_name: 'tool_call', errored: true, error: 'Card declined', proves: 'reported' } }),
  rec({ id: 'event:10', evidence_type: 'handoff', event_id: 10, span_id: 'aaaaaaaaaaaaaaaa', details: { event: 'handoff_initiated', direction: 'to_human', handoff_id: 'h-1', reason: 'over limit' } }),
  rec({ id: 'event:11', evidence_type: 'handoff', observed_at: later(120), source_type: 'human', source_connector_id: null, source_label: 'Sarah Chen', correlation_method: 'direct', event_id: 11, details: { event: 'handoff_declined', handoff_id: 'h-1' } }),
  rec({ id: 'event:12', evidence_type: 'external_state', observed_at: later(180), source_type: 'system', source_connector_id: 'stripe', source_label: 'Stripe', event_id: 12, external_object_id: 'pi_4471', external_event_id: 'evt_proc_1', details: { provider_event_type: 'payment_intent.processing', effect: 'wait', event: 'handoff_initiated', waiting_on: 'payment processing', provider_ids_recorded: true } }),
  rec({ id: 'event:13', evidence_type: 'external_state', observed_at: later(300), source_type: 'system', source_connector_id: 'stripe', source_label: 'Stripe', event_id: 13, external_object_id: 'pi_4471', external_event_id: 'evt_fail_3', details: { provider_event_type: 'payment_intent.payment_failed', effect: 'stuck', event: 'handoff_initiated', reason: 'Your card was declined', provider_ids_recorded: true } }),
  rec({ id: 'event:15', evidence_type: 'completion', observed_at: later(660), event_id: 15, span_id: 'bbbbbbbbbbbbbbbb', details: { reason: 'completed_by_agent', proves: 'recorded_close' } }),
] }
const COVERAGE = { item_id: 41, generated_at: T, evidence_bounded: false, dimensions: [
  { id: 'execution', state: 'observed', reason: 'execution_evidence', evidence_count: 1, last_observed_at: later(600), evidence_types: [], sources: [], correlation_methods: [], from_bounded_evidence: false, details: {} },
  { id: 'handoffs', state: 'observed', reason: 'handoff_records', evidence_count: 2, last_observed_at: later(120), evidence_types: [], sources: [], correlation_methods: [], from_bounded_evidence: false, details: {} },
] }

const xprov = (over = {}) => ({
  record: 'loop_event', span_id: null, trace_id: null, parent_span_id: null, parent_status: 'none', event_id: null,
  evidence_kind: 'handoff', correlation: 'direct', loop_link: null, classification_basis: 'event_type', span_kind: null, event_type: null, ...over,
})
const xnode = (over = {}) => ({
  id: 'event:10', type: 'handoff', parent_id: null, started_at: later(0), ended_at: null, duration_ms: null, label: 'handoff_initiated',
  worker: null, connector: null, status: 'recorded', error: null, model: null, tool: null, usage: null, cost: null,
  event: { type: 'handoff_initiated', direction: 'to_human', actor: { type: 'agent', label: 'returns-bot:main' }, target_label: 'Sarah Chen', reason: 'over limit' },
  provenance: xprov({ event_id: 10 }), ...over,
})
function executionFor(id, { without = [] } = {}) {
  const root = {
    id: 'span:root', type: 'worker', parent_id: null, started_at: later(-60), ended_at: later(-1), duration_ms: 59000, label: 'agent_run',
    worker: { label: 'returns-bot:main', service_name: 'returns-bot', agent_id: 'main' }, connector: { id: 'grok-bot', method: 'mcp' },
    status: 'unset', error: null, model: null, tool: null, usage: null, cost: null, event: null,
    provenance: xprov({ record: 'span', span_id: 'r', trace_id: 'a'.repeat(32), evidence_kind: 'execution', correlation: 'explicit_key', event_type: 'agent_run', loop_link: 'key_batch', span_kind: 'internal' }),
  }
  const tool = { ...root, id: 'span:t2', type: 'tool', parent_id: 'span:root', label: 'tool_call', started_at: later(-30), ended_at: later(-29), duration_ms: 843, status: 'error', error: 'Card declined',
    tool: { name: 'stripe.refunds.create', identifier: 'stripe.refunds.create', display_name: 'stripe.refunds.create', mcp_server: null, call_id: 'c2', reported_success: false },
    provenance: xprov({ record: 'span', span_id: 'cccccccccccccccc', trace_id: 'a'.repeat(32), parent_span_id: 'r', parent_status: 'attached', evidence_kind: 'action_reported', correlation: 'explicit_key' }) }
  const events = [
    xnode(),
    xnode({ id: 'event:11', label: 'handoff_declined', started_at: later(120), event: { type: 'handoff_declined', direction: null, actor: { type: 'human', label: 'Sarah Chen' } }, provenance: xprov({ event_id: 11 }) }),
    xnode({ id: 'event:12', type: 'wait', label: 'handoff_initiated', started_at: later(180), event: { type: 'handoff_initiated', direction: 'to_system', actor: { type: 'system', label: 'Stripe' }, provider: 'stripe', waiting_on: 'payment processing', target_id: 'Stripe' }, provenance: xprov({ event_id: 12, evidence_kind: 'external_state', correlation: 'explicit_key' }) }),
    xnode({ id: 'event:13', type: 'wait', label: 'handoff_initiated', started_at: later(300), event: { type: 'handoff_initiated', direction: 'to_system', actor: { type: 'system', label: 'Stripe' }, provider: 'stripe', reason: 'Your card was declined', target_id: 'Stripe' }, provenance: xprov({ event_id: 13, evidence_kind: 'external_state', correlation: 'explicit_key' }) }),
    xnode({ id: 'event:14', type: 'other', label: 'stall_detected', started_at: later(420), event: { type: 'stall_detected', direction: null, actor: { type: 'system', label: 'Trovis' }, reason: 'no activity for 4h' }, provenance: xprov({ event_id: 14, evidence_kind: null }) }),
    xnode({ id: 'event:15', type: 'completion', label: 'loop_closed', started_at: later(660), event: { type: 'loop_closed', direction: null, actor: { type: 'agent', label: 'returns-bot:main' }, reason: 'completed_by_agent' }, provenance: xprov({ event_id: 15, evidence_kind: 'completion' }) }),
  ].filter((n) => !without.includes(n.id))
  const nodes = [root, tool, ...events]
  return {
    item_id: id, generated_at: T, trace_ids: ['a'.repeat(32)], nodes, roots: nodes.filter((n) => n.parent_id === null).map((n) => n.id),
    chronology: nodes.map((n) => n.id), bounded: false, span_limit: 2000, spans_read: 2, events_read: events.length,
    duplicate_spans_dropped: 0, cycles_broken: 0, summary: {},
  }
}

function stub({
  graph = graphFor(41), graphFail = false, evidence = EVIDENCE, evidenceFail = false, coverageFail = false,
  execution = executionFor(41), executionFail = false, details = DETAILS,
} = {}) {
  const calls = { item: [], graph: [], execution: [], evidence: [], coverage: [] }
  api.getWorkItem = async (id, opts = {}) => {
    calls.item.push([id, opts.include || null])
    const d = details[id] || details[41]
    return opts.include === 'runs' ? { ...d, runs: RUNS } : d
  }
  api.getWorkItemEvidence = async (id) => { calls.evidence.push(id); if (evidenceFail) throw new Error('down'); return typeof evidence === 'function' ? evidence(id) : evidence }
  api.getWorkItemCoverage = async (id) => { calls.coverage.push(id); if (coverageFail) throw new Error('down'); return COVERAGE }
  api.getWorkItemExecution = async (id) => { calls.execution.push(id); if (executionFail) throw new Error('down'); return typeof execution === 'function' ? execution(id) : execution }
  api.getWorkItemGraph = async (id) => {
    calls.graph.push(id)
    if (typeof graphFail === 'function' ? graphFail() : graphFail) throw new Error('graph down')
    return typeof graph === 'function' ? graph(id) : graph
  }
  return calls
}

const page = (over = {}) =>
  React.createElement(JobDetail, { variant: 'page', item: { id: 41 }, onClose: () => {}, onOpenAgent: () => {}, backLabel: '← Returns', ...over })
const tab = (m, label) => m.$$('.jobd-view').find((b) => b.textContent === label)
const stepEl = (m, id) => m.container.querySelector(`[data-step-id="${id}"]`)
const rowText = (el) => el.querySelector(':scope > .jobd-work-row').textContent
const work = (m) => m.$('.jobd-work')
async function select(m, id) {
  await m.click(stepEl(m, id).querySelector(':scope > .jobd-work-row'))
  return stepEl(m, id).querySelector('.jobd-work-details')
}
const FORBIDDEN_EMPTY = /no activity|nothing happened|did nothing|failed to start|was empty|no work/i
const VERDICTS = /succeeded|successful|success\b|resolved|done successfully|verified|\d+\s?%|score/i

// --- 1, 2, 13: entry, the page-only read, and the information architecture ------------------

test('1. the full Run page fetches the Work Graph once, for the viewed item, and What happened leads the Run view', async () => {
  const calls = stub()
  const m = await mount(page())
  await m.settle()
  assert.deepEqual(calls.graph, [41])
  assert.deepEqual(calls.execution, [], 'the Execution view is not opened by the Work Graph')
  const sec = work(m)
  assert.ok(sec && sec.getAttribute('aria-label') === 'What happened')
  assert.equal(sec.querySelector('h3').textContent, 'What happened')
  const sections = m.$$('.jobd-section').map((s) => s.getAttribute('aria-label'))
  const i = sections.indexOf('What happened')
  assert.ok(i >= 0)
  assert.deepEqual(sections.slice(i), ['What happened', 'How this job ran', 'Visibility', 'Evidence'])
  assert.ok(tab(m, 'Run') && tab(m, 'Execution'), 'the Run | Execution switch is unchanged — no third tab')
  assert.equal(m.$$('.jobd-view').length, 2)
  assert.ok(!m.text().includes('Recent passes'), 'the page tells the story once: no Recent passes beside it')
  const moves = m.$('.jobd-moves')
  assert.ok(moves && moves.tagName === 'DETAILS' && !moves.hasAttribute('open'), 'How this job ran is demoted to a closed fold')
  m.unmount()
})

test('2/26. the Home desk panel neither fetches nor renders the Work Graph, and has no view switch', async () => {
  const calls = stub()
  const m = await mount(React.createElement(JobDetail, { variant: 'panel', item: { id: 41, title: 'x' }, onClose: () => {} }))
  await m.settle()
  assert.deepEqual(calls.graph, [])
  assert.ok(!m.$('.jobd-work') && !m.$('.jobd-views'))
  assert.match(m.text(), /Recent passes|How this job runs/, 'the panel keeps its own spine')
  m.unmount()
})

// --- 3, 29: run switching ---------------------------------------------------------------------

test('3/29. Run A → Run B clears A’s story at once, shows B’s skeleton, and a late A response never lands under B', async () => {
  const gates = {}
  stub({ graph: (id) => { gates[id] = deferred(); return gates[id].promise } })
  const m = await mount(page())
  await m.settle()
  gates[41].resolve(graphFor(41))
  await m.settle()
  assert.ok(stepEl(m, S_HANDOFF.id))
  await select(m, S_HANDOFF.id)
  assert.ok(m.$('.jobd-work-details'))
  await m.render(page({ item: { id: 42 } }))
  await m.settle()
  assert.ok(!stepEl(m, S_HANDOFF.id), 'A’s steps are gone immediately')
  assert.ok(!m.$('.jobd-work-details'), 'A’s open details are gone')
  assert.ok(work(m).querySelector('.dash-skel'), 'B loads')
  assert.match(m.$('.jobd-title').textContent, /Return #4472/)
  gates[41] = deferred()
  gates[42].resolve(graphFor(42, { steps: [S_CLOSED], chronology: [S_CLOSED.id] }))
  await m.settle()
  assert.equal(m.$$('.jobd-work-step').length, 1)
  m.unmount()
})

test('29b. a response for Run A that arrives after B’s cannot populate B', async () => {
  const gates = {}
  stub({ graph: (id) => { gates[id] = deferred(); return gates[id].promise } })
  const m = await mount(page())
  await m.settle()
  await m.render(page({ item: { id: 42 } }))
  await m.settle()
  gates[42].resolve(graphFor(42, { steps: [S_CLOSED], chronology: [S_CLOSED.id] }))
  await m.settle()
  assert.equal(m.$$('.jobd-work-step').length, 1)
  gates[41].resolve(graphFor(41))
  await m.settle()
  assert.equal(m.$$('.jobd-work-step').length, 1, 'still B’s single step')
  assert.ok(!stepEl(m, S_HANDOFF.id))
  m.unmount()
})

// --- 4, 28: independence --------------------------------------------------------------------

test('4/28. a Work Graph failure is a failed section with Retry — the header, Visibility and Evidence stand, and no fake steps appear', async () => {
  let fail = true
  const calls = stub({ graphFail: () => fail })
  const m = await mount(page())
  await m.settle()
  assert.match(m.$('.jobd-title').textContent, /Return #4471/)
  assert.ok(m.$('.work-status-pill'))
  assert.match(m.$('.jobd-visibility').textContent, /Observed/)
  assert.match(m.$('.jobd-evidence').textContent, /returns-bot/)
  const sec = work(m)
  assert.match(sec.textContent, /What happened couldn.t be loaded\./)
  assert.doesNotMatch(sec.textContent, /Unknown|No activity|explicit work changes/)
  assert.equal(sec.querySelectorAll('.jobd-work-step').length, 0)
  const retry = sec.querySelector('[role="alert"] button')
  assert.ok(retry)
  fail = false
  await m.click(retry)
  await m.settle()
  assert.deepEqual(calls.graph, [41, 41])
  assert.ok(stepEl(m, S_HANDOFF.id))
  m.unmount()
})

test('28b. Evidence and Visibility failures leave What happened intact, each with its own Retry', async () => {
  stub({ evidenceFail: true, coverageFail: true })
  const m = await mount(page())
  await m.settle()
  assert.ok(stepEl(m, S_HANDOFF.id), 'the story renders')
  assert.match(m.$('.jobd-evidence').textContent, /Evidence couldn.t be loaded/)
  assert.match(m.$('.jobd-visibility').textContent, /Visibility couldn.t be loaded/)
  assert.ok(m.$('.jobd-evidence [role="alert"] button') && m.$('.jobd-visibility [role="alert"] button'))
  // With no Evidence body, a step still opens; it just offers no evidence door.
  const d = await select(m, S_WAIT.id)
  assert.ok(d)
  assert.ok(![...d.querySelectorAll('button')].some((b) => /View evidence/.test(b.textContent)))
  assert.ok([...d.querySelectorAll('button')].some((b) => /View in Execution/.test(b.textContent)))
  m.unmount()
})

test('loading shows a skeleton inside the section only; the header is already usable', async () => {
  const gate = deferred()
  stub({ graph: () => gate.promise })
  const m = await mount(page())
  await m.settle()
  assert.ok(work(m).querySelector('.dash-skel'))
  assert.match(m.$('.jobd-title').textContent, /Return #4471/)
  assert.match(m.$('.jobd-evidence').textContent, /returns-bot/, 'evidence did not wait for the graph')
  gate.resolve(graphFor(41))
  await m.settle()
  assert.ok(!work(m).querySelector('.dash-skel') && stepEl(m, S_HANDOFF.id))
  m.unmount()
})

// --- 5, 6, 17: the sparse record ---------------------------------------------------------------

test('5/6/17. zero steps is a calm, truthful empty state — never “no activity” — and Execution is one click away', async () => {
  const calls = stub({ graph: EMPTY_GRAPH })
  const m = await mount(page())
  await m.settle()
  const sec = work(m)
  assert.equal(sec.querySelectorAll('.jobd-work-step').length, 0)
  assert.match(sec.textContent, /Trovis doesn.t have explicit work changes to show for this run yet\./)
  assert.match(sec.textContent, /Technical execution may still be available in Execution\./)
  assert.doesNotMatch(sec.textContent, FORBIDDEN_EMPTY)
  assert.doesNotMatch(sec.textContent, /couldn.t be loaded/)
  // The execution behind it (a run root, a failed Stripe call) is never turned into steps here.
  assert.doesNotMatch(sec.textContent, /stripe\.refunds\.create|tool_call|agent_run|Card declined/)
  const go = [...sec.querySelectorAll('button')].find((b) => /View Execution/.test(b.textContent))
  assert.ok(go)
  await m.click(go)
  await m.settle()
  assert.equal(tab(m, 'Execution').getAttribute('aria-selected'), 'true')
  assert.deepEqual(calls.execution, [41])
  assert.ok(m.$('.exec') && m.$('.exec-inspector.is-empty'), 'plain Execution, nothing preselected')
  m.unmount()
})

test('17b. Evidence never fills the timeline: the reported Stripe call in Evidence is not a Work Step', async () => {
  stub({ graph: EMPTY_GRAPH })
  const m = await mount(page())
  await m.settle()
  assert.match(m.$('.jobd-evidence').textContent, /Refunds create|stripe/i, 'Evidence has the reported action')
  assert.equal(m.$$('.jobd-work-step').length, 0)
  m.unmount()
})

// --- 7–12: what a step says -------------------------------------------------------------------

test('7. a handoff row: the agent (its worker label, not :main or a connector), the endpoint’s label, the target, a clock time', async () => {
  stub()
  const m = await mount(page())
  await m.settle()
  const el = stepEl(m, S_HANDOFF.id)
  const text = rowText(el)
  assert.match(text, /Agent\s*returns-bot/)
  assert.doesNotMatch(text, /returns-bot:main|Grok/)
  assert.match(text, /Handed to a person/)
  assert.match(text, /Sarah Chen/)
  assert.match(el.querySelector('.jobd-work-at').textContent, /\d{1,2}:\d{2} (AM|PM)/)
  assert.equal(el.querySelector('.jobd-work-at').getAttribute('dateTime'), S_HANDOFF.at)
  assert.match(el.querySelector('.jobd-work-type').textContent, /Passed on/)
  assert.ok(el.classList.contains('type-handoff'))
  m.unmount()
})

test('8. people appear only as the server named them — “a person” stays “a person”, no address, no name minted from an id', async () => {
  const anon = { ...S_HANDOFF, details: { ...S_HANDOFF.details, target_label: 'a person', target_id: null } }
  const decline = { ...S_DECLINED, actor: { type: 'human', label: 'a person' } }
  const toAgent = { ...S_HANDOFF, id: 'work-step:event:20', at: later(60), label: 'Handed to another agent',
    details: { direction: 'to_agent', handoff_id: 'h-3', reason: null, target_label: 'research-agent:main', target_id: 'research-agent:main' } }
  stub({ graph: graphFor(41, { steps: [anon, toAgent, decline], chronology: [anon.id, toAgent.id, decline.id] }) })
  const m = await mount(page())
  await m.settle()
  assert.match(rowText(stepEl(m, anon.id)), /Handed to a person\s*a person/)
  assert.match(rowText(stepEl(m, decline.id)), /Person\s*a person/)
  assert.match(rowText(stepEl(m, toAgent.id)), /Handed to another agent\s*research-agent/)
  assert.doesNotMatch(work(m).textContent, /@|research-agent:main|h-3/, 'no address, no raw composite id, no reference id in the row')
  m.unmount()
})

test('9. a wait names the system apart from the actor: Stripe’s own record reads System · Stripe; an agent’s declared wait keeps the agent as actor and the system as system', async () => {
  const declared = { id: 'work-step:event:30', type: 'wait', at: later(30), label: 'Waiting on warehouse', actor: { type: 'agent', label: 'export-agent:main' },
    system: { label: 'warehouse', provider: null, target_id: 'warehouse' },
    details: { direction: 'to_system', handoff_id: 'h-4', reason: 'export queued', waiting_on: null, saas_provider: null, saas_effect: null, saas_object_id: null, saas_event_type: null, saas_event_id: null },
    provenance: prov({ event_id: 30, evidence_id: 'event:30', execution_node_id: 'event:30' }) }
  stub({ graph: graphFor(41, { steps: [S_WAIT, declared], chronology: [S_WAIT.id, declared.id] }) })
  const m = await mount(page())
  await m.settle()
  const saas = rowText(stepEl(m, S_WAIT.id))
  assert.match(saas, /System\s*Stripe/)
  assert.match(saas, /Waiting on Stripe/)
  assert.match(saas, /payment processing/)
  assert.match(stepEl(m, S_WAIT.id).querySelector('.jobd-work-type').textContent, /Waiting/)
  const own = rowText(stepEl(m, declared.id))
  assert.match(own, /Agent\s*export-agent/)
  assert.match(own, /Waiting on warehouse/)
  assert.match(own, /export queued/)
  const d = await select(m, declared.id)
  assert.match(d.textContent, /System\s*warehouse/)
  assert.doesNotMatch(d.textContent, /System\s*export-agent/, 'the actor is never shown as the system')
  m.unmount()
})

test('10. an exception carries the recorded reason and nothing more: a stuck payment says what Stripe said; a reasonless decline says nothing invented', async () => {
  stub()
  const m = await mount(page())
  await m.settle()
  const stuck = rowText(stepEl(m, S_STUCK.id))
  assert.match(stuck, /Stuck on Stripe/)
  assert.match(stuck, /Your card was declined/)
  assert.doesNotMatch(stuck, /failed|failure|refund|outcome/i)
  const declined = stepEl(m, S_DECLINED.id)
  assert.match(rowText(declined), /Person\s*Sarah Chen/)
  assert.match(rowText(declined), /Handoff declined/)
  assert.ok(!declined.querySelector('.jobd-work-context'), 'no context line for a reason that was not recorded')
  assert.doesNotMatch(rowText(declined), /no reason|unknown|rejected because/i)
  assert.match(declined.querySelector('.jobd-work-type').textContent, /Exception/)
  const stall = rowText(stepEl(m, S_STALL.id))
  assert.match(stall, /System\s*Trovis/)
  assert.match(stall, /Stall detected\s*no activity for 4h/)
  m.unmount()
})

test('11/25. completion reads “Work record closed”, tagged “Record closed” — no Success, Succeeded, Resolved, verdict or score anywhere in the section', async () => {
  stub()
  const m = await mount(page())
  await m.settle()
  const el = stepEl(m, S_CLOSED.id)
  assert.match(rowText(el), /Agent\s*returns-bot\s*Work record closed/)
  assert.match(el.querySelector('.jobd-work-type').textContent, /^Record closed$/)
  assert.doesNotMatch(work(m).textContent, VERDICTS)
  const d = await select(m, S_CLOSED.id)
  assert.match(d.textContent, /Recorded as\s*completed by agent/)
  assert.doesNotMatch(d.textContent, VERDICTS)
  m.unmount()
})

test('12. an abandoned closure stays a closure marked abandoned — not a failure, not a success', async () => {
  const abandoned = { ...S_CLOSED, label: 'Work record closed as abandoned', actor: { type: 'system', label: 'Trovis' },
    details: { reason: 'abandoned', detail: null, abandoned: true, outcome: 'record_closed' } }
  stub({ graph: graphFor(41, { steps: [abandoned], chronology: [abandoned.id] }) })
  const m = await mount(page())
  await m.settle()
  const el = stepEl(m, abandoned.id)
  assert.match(rowText(el), /System\s*Trovis\s*Work record closed as abandoned/)
  assert.ok(el.classList.contains('type-completed'))
  const d = await select(m, abandoned.id)
  assert.match(d.textContent, /Closure\s*abandoned/)
  assert.doesNotMatch(work(m).textContent, /failed|failure|success|gave up/i)
  m.unmount()
})

// --- 13, 14, 15: possession ---------------------------------------------------------------------

test('13/14. the header’s holder line comes from possession.current_holder only — Stripe holds it even though the last step’s actor is Stripe’s wait and the first is the agent', async () => {
  stub({ graph: OPEN_GRAPH })
  const m = await mount(page())
  await m.settle()
  assert.equal(m.$('.jobd-holder').textContent, 'Held by Stripe · waiting')
  assert.equal(m.$$('.jobd-holder').length, 1, 'one holder line, not a second one under the story')
  m.unmount()
})

test('14b. with no current holder in the response the header keeps the lean detail’s sentence — nothing is inferred from the latest step', async () => {
  // Steps end with a handoff to Sarah, yet the endpoint names no current holder.
  stub({ graph: graphFor(41, { steps: [S_HANDOFF], chronology: [S_HANDOFF.id], possession: { segments: SEGMENTS.slice(0, 1), current_holder: null } }) })
  const m = await mount(page())
  await m.settle()
  assert.equal(m.$('.jobd-holder').textContent, 'With returns-bot')
  assert.doesNotMatch(m.$('.jobd-holder').textContent, /Sarah|Held by/)
  m.unmount()
  // And a graph with no possession block at all is the same.
  stub({ graph: graphFor(41, { possession: null }) })
  const m2 = await mount(page())
  await m2.settle()
  assert.equal(m2.$('.jobd-holder').textContent, 'With returns-bot')
  m2.unmount()
})

test('14c. “With you” and “Finished” are the session’s and the status’s words; possession never overrides them', async () => {
  const details = { 41: detailFor(41, 'Return #4471', { status: 'waiting_on_you', awaiting_handoff_event_id: 10, holder: { kind: 'human', name: 'Alex' } }) }
  stub({ graph: OPEN_GRAPH, details })
  const m = await mount(page())
  await m.settle()
  assert.equal(m.$('.jobd-holder').textContent, 'With you')
  m.unmount()
  stub({ graph: OPEN_GRAPH, details: { 41: detailFor(41, 'Return #4471', { status: 'done' }) } })
  const m2 = await mount(page())
  await m2.settle()
  assert.equal(m2.$('.jobd-holder').textContent, 'Finished')
  m2.unmount()
})

test('15. the holder history is the endpoint’s segments, in order, with each segment’s own waiting flag — history only, never a “now”', async () => {
  stub({ graph: OPEN_GRAPH })
  const m = await mount(page())
  await m.settle()
  const hist = work(m).querySelector('.jobd-work-history')
  assert.ok(hist && hist.tagName === 'DETAILS' && !hist.hasAttribute('open'))
  assert.match(hist.querySelector('summary').textContent, /Who held the work/)
  const rows = [...hist.querySelectorAll('li')].map((li) => [...li.children].map((c) => c.textContent).join(' '))
  assert.deepEqual(rows, [
    'Agent returns-bot', 'Person Sarah Chen waiting', 'Agent returns-bot', 'System Stripe waiting',
  ])
  assert.doesNotMatch(hist.textContent, /\bnow\b|current/i, 'who has it now is the header’s line, from current_holder — the history does not repeat or re-derive it')
  assert.doesNotMatch(hist.textContent, /because|caused|led to|→/, 'history, not causality')
  assert.equal(m.$('.jobd-holder').textContent, 'Held by Stripe · waiting', 'the header answers "now", from possession.current_holder')
  m.unmount()
})

test('15b. an open-ended last segment with no current_holder: the history lists it, nothing says “now”, and the header does not name it', async () => {
  const openSegs = SEGMENTS.slice(0, 4).map((s, i) => (i === 3 ? { ...s, end: null } : s))
  stub({ graph: graphFor(41, { steps: [S_HANDOFF, S_WAIT], chronology: [S_HANDOFF.id, S_WAIT.id], possession: { segments: openSegs, current_holder: null } }) })
  const m = await mount(page())
  await m.settle()
  const hist = work(m).querySelector('.jobd-work-history')
  const rows = [...hist.querySelectorAll('li')].map((li) => [...li.children].map((c) => c.textContent).join(' '))
  assert.equal(rows[3], 'System Stripe waiting')
  assert.doesNotMatch(hist.textContent, /\bnow\b|current/i)
  assert.equal(m.$('.jobd-holder').textContent, 'With returns-bot', 'no current holder from the endpoint → the lean detail’s sentence, not the open segment')
  assert.doesNotMatch(m.$('.jobd-holder').textContent, /Stripe|Held by/)
  m.unmount()
})

// --- 16, 30: lifecycle stays out of the timeline --------------------------------------------------

test('16/30. possession-only lifecycle records (a person’s completion, a SaaS clear) never become rows; the timeline is exactly `steps`', async () => {
  stub()
  const m = await mount(page())
  await m.settle()
  assert.deepEqual(m.$$('.jobd-work-step').map((li) => li.dataset.stepId), STEPS.map((s) => s.id))
  const text = work(m).textContent
  assert.doesNotMatch(text, /handoff_completed|payment_intent\.succeeded|evt_ok_2|Wait resolved|cleared/i)
  m.unmount()
})

// --- 18: boundedness ------------------------------------------------------------------------------

test('18. bounded=true is one quiet sentence about supporting detail; the steps are all there and nothing says the timeline is incomplete', async () => {
  stub({ graph: graphFor(41, { bounded: true, span_limit: 3, spans_read: 3, summary: { steps: 6, execution_only: { spans_read: 3, bounded: true } } }) })
  const m = await mount(page())
  await m.settle()
  assert.equal(m.$$('.jobd-work-step').length, 6)
  const note = work(m).querySelector('.jobd-work-note')
  assert.ok(note)
  assert.equal(note.textContent, 'Some supporting execution detail is outside this read.')
  assert.doesNotMatch(work(m).textContent, /incomplete|truncated|missing steps|3 spans/i)
  m.unmount()
  stub()
  const m2 = await mount(page())
  await m2.settle()
  assert.ok(!work(m2).querySelector('.jobd-work-note'))
  m2.unmount()
})

// --- 19–24: details, provenance and the two doors ---------------------------------------------------

test('24. a step is a button: aria-expanded flips, the details area is aria-controlled, selection is a class and an attribute, and toggling again closes it', async () => {
  stub()
  const m = await mount(page())
  await m.settle()
  const el = stepEl(m, S_WAIT.id)
  const row = el.querySelector(':scope > .jobd-work-row')
  assert.equal(row.tagName, 'BUTTON')
  assert.equal(row.getAttribute('type'), 'button')
  assert.equal(row.getAttribute('aria-expanded'), 'false')
  assert.ok(!el.querySelector('.jobd-work-details'))
  await m.click(row)
  assert.equal(row.getAttribute('aria-expanded'), 'true')
  assert.ok(el.classList.contains('is-selected'))
  const details = el.querySelector('.jobd-work-details')
  assert.ok(details && details.id === row.getAttribute('aria-controls'))
  assert.equal(m.$$('.jobd-work-details').length, 1)
  await m.click(stepEl(m, S_STUCK.id).querySelector(':scope > .jobd-work-row'))
  assert.equal(row.getAttribute('aria-expanded'), 'false', 'one step open at a time')
  assert.ok(stepEl(m, S_STUCK.id).classList.contains('is-selected'))
  await m.click(stepEl(m, S_STUCK.id).querySelector(':scope > .jobd-work-row'))
  assert.equal(m.$$('.jobd-work-details').length, 0)
  assert.ok(m.$$('.jobd-work-row').every((b) => b.getAttribute('aria-label') === null || b.getAttribute('aria-label')))
  m.unmount()
})

test('the timeline shows no ids or correlation words; the details area folds them under Technical details, exact when shown', async () => {
  stub()
  const m = await mount(page())
  await m.settle()
  const rows = m.$$('.jobd-work-row').map((b) => b.textContent).join(' ')
  assert.doesNotMatch(rows, /event:1\d|work-step|aaaaaaaaaaaaaaaa|a{32}|pi_4471|evt_|explicit_key|direct|h-1|saas:/)
  const d = await select(m, S_WAIT.id)
  const tech = d.querySelector('.jobd-work-tech')
  assert.ok(tech && tech.tagName === 'DETAILS' && !tech.hasAttribute('open'))
  assert.match(tech.textContent, /Event id\s*12/)
  assert.match(tech.textContent, /Evidence record\s*event:12/)
  assert.match(tech.textContent, /Execution node\s*event:12/)
  assert.match(tech.textContent, /External object\s*pi_4471/)
  assert.match(tech.textContent, /Provider event id\s*evt_proc_1/)
  assert.match(tech.textContent, /Provider event\s*payment_intent\.processing/)
  assert.match(tech.textContent, /Link\s*Directly linked to this run/)
  assert.match(tech.textContent, /Connector\s*Stripe/)
  assert.doesNotMatch(tech.textContent, /null|undefined|None/)
  const visible = [...d.children].filter((c) => c.tagName !== 'DETAILS').map((c) => c.textContent).join(' ')
  assert.doesNotMatch(visible, /event:12|pi_4471|evt_proc_1/)
  assert.match(visible, /Waiting on\s*payment processing/)
  assert.match(visible, /System\s*Stripe/)
  assert.match(visible, /Supported by an external system's own record — Observed from Stripe/)
  m.unmount()
})

test('19. “View evidence” uses the step’s exact evidence_id: it marks and focuses that one Evidence row and no other', async () => {
  stub()
  const m = await mount(page())
  await m.settle()
  const d = await select(m, S_STUCK.id)
  const btn = [...d.querySelectorAll('button')].find((b) => b.textContent === 'View evidence')
  assert.ok(btn)
  await m.click(btn)
  await m.settle(5)
  const hit = m.$('.jobd-ev-row.is-highlighted')
  assert.ok(hit)
  assert.equal(hit.dataset.evidenceId, 'event:13')
  assert.equal(m.$$('.jobd-ev-row.is-highlighted').length, 1)
  assert.match(hit.textContent, /Your card was declined/)
  assert.equal(document.activeElement, hit, 'focus moved to the row')
  // Another step's door moves the mark; the first row is no longer marked.
  const d2 = await select(m, S_WAIT.id)
  await m.click([...d2.querySelectorAll('button')].find((b) => b.textContent === 'View evidence'))
  await m.settle(5)
  assert.equal(m.$('.jobd-ev-row.is-highlighted').dataset.evidenceId, 'event:12')
  m.unmount()
})

test('20. a null evidence_id (a stall) offers no Evidence door and no supporting record; a handoff whose record Evidence lists but does not draw shows the record inline and no door', async () => {
  stub()
  const m = await mount(page())
  await m.settle()
  const stall = await select(m, S_STALL.id)
  const stallButtons = [...stall.querySelectorAll('button')].map((b) => b.textContent)
  assert.ok(!stallButtons.includes('View evidence'))
  assert.ok(stallButtons.includes('View in Execution'), 'the stall does have an Execution node')
  assert.doesNotMatch(stall.textContent, /Supported by|Evidence record/)
  assert.ok(!stall.querySelector('button[disabled]'), 'no disabled placeholder')
  const hand = await select(m, S_HANDOFF.id)
  const handButtons = [...hand.querySelectorAll('button')].map((b) => b.textContent)
  assert.ok(!handButtons.includes('View evidence'), 'Evidence draws no row for a handoff record')
  assert.match(hand.textContent, /Supported by a handoff record — Reported by returns-bot · Grok Bot/)
  assert.match(hand.querySelector('.jobd-work-tech').textContent, /Evidence record\s*event:10/)
  m.unmount()
})

test('21. “View in Execution” switches the view and selects exactly the step’s execution_node_id in the existing inspector', async () => {
  const calls = stub()
  const m = await mount(page())
  await m.settle()
  const d = await select(m, S_WAIT.id)
  await m.click([...d.querySelectorAll('button')].find((b) => b.textContent === 'View in Execution'))
  await m.settle()
  assert.equal(tab(m, 'Execution').getAttribute('aria-selected'), 'true')
  assert.deepEqual(calls.execution, [41])
  const node = m.container.querySelector('[data-node-id="event:12"]')
  assert.ok(node && node.classList.contains('is-selected'))
  assert.equal(node.getAttribute('aria-selected'), 'true')
  assert.equal(m.$$('.exec-node.is-selected, .exec-chrono-row.is-selected').length, 1)
  const insp = m.$('.exec-inspector:not(.is-empty)')
  assert.ok(insp)
  assert.match(insp.textContent, /Waiting on Stripe/)
  assert.match(insp.querySelector('.exec-insp-tech').textContent, /Event id\s*12/)
  assert.match(m.$('.jobd-title').textContent, /Return #4471/, 'same Run, same header')
  // Back to Run: the story is still there; switching to Execution again preselects nothing.
  await m.click(tab(m, 'Run'))
  await m.settle()
  assert.ok(stepEl(m, S_WAIT.id))
  await m.click(tab(m, 'Execution'))
  await m.settle()
  assert.ok(m.$('.exec-inspector.is-empty'))
  m.unmount()
})

test('22. a null execution_node_id offers no Execution door', async () => {
  const noNode = { ...S_HANDOFF, provenance: prov({ execution_node_id: null }) }
  stub({ graph: graphFor(41, { steps: [noNode], chronology: [noNode.id] }) })
  const m = await mount(page())
  await m.settle()
  const d = await select(m, noNode.id)
  const buttons = [...d.querySelectorAll('button')].map((b) => b.textContent)
  assert.ok(!buttons.includes('View in Execution'))
  assert.ok(!d.querySelector('button[disabled]'))
  assert.doesNotMatch(d.querySelector('.jobd-work-tech').textContent, /Execution node/)
  m.unmount()
})

test('23. when the referenced node is not in the Execution read set, Execution opens normally with nothing selected — no guessed match', async () => {
  stub({ execution: executionFor(41, { without: ['event:13'] }) })
  const m = await mount(page())
  await m.settle()
  const d = await select(m, S_STUCK.id)
  await m.click([...d.querySelectorAll('button')].find((b) => b.textContent === 'View in Execution'))
  await m.settle()
  assert.equal(tab(m, 'Execution').getAttribute('aria-selected'), 'true')
  assert.ok(m.$('.exec-tree'), 'Execution renders')
  assert.equal(m.$$('.exec-node.is-selected, .exec-chrono-row.is-selected').length, 0)
  assert.ok(m.$('.exec-inspector.is-empty'))
  assert.ok(!m.container.querySelector('[data-node-id="event:13"]'))
  assert.ok(m.container.querySelector('[data-node-id="event:12"]') && !m.container.querySelector('[data-node-id="event:12"]').classList.contains('is-selected'),
    'the neighbouring Stripe wait is not selected in its place')
  m.unmount()
})

test('23b. a deep link that lands on an Execution failure is the normal failure with Retry; the Retry that loads the body still selects the exact node, and only it', async () => {
  let fail = true
  stub({ executionFail: true })
  api.getWorkItemExecution = async () => { if (fail) throw new Error('down'); return executionFor(41) }
  const m = await mount(page())
  await m.settle()
  const d = await select(m, S_WAIT.id)
  await m.click([...d.querySelectorAll('button')].find((b) => b.textContent === 'View in Execution'))
  await m.settle()
  assert.match(m.$('.exec').textContent, /Execution couldn.t be loaded/)
  assert.ok(!m.$('.exec-inspector:not(.is-empty)'))
  fail = false
  await m.click(m.$('.exec [role="alert"] button'))
  await m.settle()
  const node = m.container.querySelector('[data-node-id="event:12"]')
  assert.ok(node && node.classList.contains('is-selected'), 'the step’s own node id, applied once a body actually holds it')
  assert.equal(m.$$('.exec-node.is-selected').length, 1)
  // Leaving and re-entering Execution carries nothing over.
  await m.click(tab(m, 'Run'))
  await m.settle()
  await m.click(tab(m, 'Execution'))
  await m.settle()
  assert.equal(m.$$('.exec-node.is-selected').length, 0)
  m.unmount()
})

// --- 27: Execution opened directly is unchanged ----------------------------------------------------

test('27. Execution opened from the switch still renders its tree and inspector as before', async () => {
  stub()
  const m = await mount(page())
  await m.settle()
  await m.click(tab(m, 'Execution'))
  await m.settle()
  assert.ok(m.$('.exec-tree') && m.$('.exec-inspector.is-empty'))
  const root = m.container.querySelector('[data-node-id="span:root"]')
  await m.click(root.querySelector(':scope > .exec-row .exec-select'))
  assert.match(m.$('.exec-inspector:not(.is-empty)').textContent, /Worker\s*returns-bot/)
  assert.ok(!m.$('.jobd-work'), 'the Run sections are the Run view, not stacked under Execution')
  m.unmount()
})
