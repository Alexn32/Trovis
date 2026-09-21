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
  { id: 'actions', state: 'unknown', reason: 'no_action_reports', evidence_count: 0, last_observed_at: null, evidence_types: [], sources: [], correlation_methods: [], from_bounded_evidence: false, details: {} },
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
const rowText = (el) => [...el.querySelector(':scope > .jobd-work-row').querySelectorAll('.jobd-work-at, .jobd-work-headline, .jobd-work-line')].map((c) => c.textContent).join(' ')
const work = (m) => m.$('.jobd-work')
const now = (m) => m.$('.run-now')
const state = (m) => m.$('.run-now-state')?.textContent ?? null
const support = (m) => m.$('.run-now-support')?.textContent ?? null
const details = (m) => m.$('.run-details')
async function select(m, id) {
  await m.click(stepEl(m, id).querySelector(':scope > .jobd-work-row'))
  return stepEl(m, id).querySelector('.jobd-work-details')
}
const FORBIDDEN_EMPTY = /no activity|nothing happened|did nothing|failed to start|was empty|no work/i
const VERDICTS = /succeeded|successful|success\b|resolved|done successfully|verified|\d+\s?%|score/i

// --- 1, 2, 13: entry, the page-only read, and the information architecture ------------------

test('1. the full Run page fetches the Work Graph once and reads in three levels: situation, Activity, then Details closed', async () => {
  const calls = stub()
  const m = await mount(page())
  await m.settle()
  assert.deepEqual(calls.graph, [41])
  assert.deepEqual(calls.execution, [], 'the Execution view is not opened by the Work Graph')
  const sec = work(m)
  assert.ok(sec && sec.getAttribute('aria-label') === 'Activity')
  assert.equal(sec.querySelector('h3').textContent, 'Activity')
  assert.ok(now(m), 'the current situation comes first')
  assert.ok(now(m).compareDocumentPosition(sec) & 4, 'situation before Activity')
  const sections = m.$$('.jobd-section').map((s) => s.getAttribute('aria-label'))
  assert.deepEqual(sections, ['Activity', 'Details', 'Run information', 'Visibility', 'Who held the work', 'Evidence', 'How this job ran'])
  const d = details(m)
  assert.ok(d && d.tagName === 'DETAILS' && !d.hasAttribute('open'), 'Details is one closed disclosure')
  assert.ok(d.contains(m.$('.jobd-visibility')) && d.contains(m.$('.jobd-evidence')) && d.contains(m.$('.jobd-moves')) && d.contains(m.$('.run-held')))
  assert.ok(tab(m, 'Activity') && tab(m, 'Execution'), 'Activity | Execution — no third tab')
  assert.equal(m.$$('.jobd-view').length, 2)
  assert.ok(!m.$$('.jobd-view').some((b) => b.textContent === 'Run'), 'the Run tab is now Activity')
  assert.ok(!m.text().includes('Recent passes'), 'the page tells the story once: no Recent passes beside it')
  // The header carries identity only: no status pill, no holder line, no ids, no source.
  const head = m.$('.jobd-head')
  assert.ok(!head.querySelector('.work-status-pill') && !head.querySelector('.jobd-holder'))
  assert.doesNotMatch(head.textContent, /Grok|grok-bot|event:|span|Held by|Waiting/)
  assert.match(head.textContent, /Part of\s*Returns/)
  const moves = m.$('.jobd-moves')
  assert.ok(moves && moves.tagName === 'DETAILS' && !moves.hasAttribute('open'), 'How this job ran stays a closed fold inside Details')
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
  assert.ok(now(m), 'the situation block stands')
  assert.equal(state(m), 'Waiting on someone', 'with no graph the situation says only what the status says')
  assert.match(m.$('.jobd-visibility').textContent, /Observed/)
  assert.match(m.$('.jobd-evidence').textContent, /returns-bot/)
  const sec = work(m)
  assert.match(sec.textContent, /Activity couldn.t be loaded\./)
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

test('7. a handoff row reads time · who → whom · the endpoint’s label: no actor-kind caps, no type tag, no connector, no :main', async () => {
  stub()
  const m = await mount(page())
  await m.settle()
  const el = stepEl(m, S_HANDOFF.id)
  assert.match(el.querySelector('.jobd-work-at').textContent, /\d{1,2}:\d{2} (AM|PM)/)
  assert.equal(el.querySelector('.jobd-work-at').getAttribute('dateTime'), S_HANDOFF.at)
  assert.equal(el.querySelector('.jobd-work-headline').textContent, 'returns-bot → Sarah Chen')
  assert.equal(el.querySelector('.jobd-work-line').textContent, 'over limit', 'the headline already says it was handed to Sarah; the line is the recorded reason, not the mechanics')
  const text = rowText(el)
  assert.doesNotMatch(text, /returns-bot:main|Grok|AGENT|Agent\b|PASSED ON|Passed on|handoff_initiated|event:/)
  assert.ok(!el.querySelector('.jobd-work-type') && !el.querySelector('.jobd-work-kind'), 'no type or kind tags in the row')
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
  assert.match(rowText(stepEl(m, anon.id)), /returns-bot → a person\s*over limit/)
  assert.match(rowText(stepEl(m, decline.id)), /a person\s*Handoff declined/)
  assert.match(rowText(stepEl(m, toAgent.id)), /returns-bot → research-agent\s*Handed off$/, 'no recorded reason → the plain "Handed off", allowed because the endpoint typed the step handoff')
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
  const saasEl = stepEl(m, S_WAIT.id)
  assert.equal(saasEl.querySelector('.jobd-work-headline').textContent, 'Stripe', 'the system is the actor of its own record — named once, not "Stripe → Stripe"')
  assert.equal(saasEl.querySelector('.jobd-work-line').textContent, 'Waiting on Stripe · payment processing')
  const ownEl = stepEl(m, declared.id)
  assert.equal(ownEl.querySelector('.jobd-work-headline').textContent, 'export-agent → warehouse', 'the agent declared the wait; the system is where the work went')
  assert.equal(ownEl.querySelector('.jobd-work-line').textContent, 'Waiting on warehouse · export queued')
  const d = await select(m, declared.id)
  assert.match(d.textContent, /System\s*warehouse/)
  assert.doesNotMatch(d.textContent, /System\s*export-agent/, 'the actor is never shown as the system')
  m.unmount()
})

test('10. an exception carries the recorded reason and nothing more: a stuck payment says what Stripe said; a reasonless decline says nothing invented', async () => {
  stub()
  const m = await mount(page())
  await m.settle()
  const stuckEl = stepEl(m, S_STUCK.id)
  assert.equal(stuckEl.querySelector('.jobd-work-headline').textContent, 'Stripe')
  assert.equal(stuckEl.querySelector('.jobd-work-line').textContent, 'Stuck on Stripe · Your card was declined')
  assert.doesNotMatch(rowText(stuckEl), /failed|failure|refund|outcome/i)
  const declined = stepEl(m, S_DECLINED.id)
  assert.equal(declined.querySelector('.jobd-work-headline').textContent, 'Sarah Chen')
  assert.equal(declined.querySelector('.jobd-work-line').textContent, 'Handoff declined', 'no reason was recorded, so none is written')
  assert.doesNotMatch(rowText(declined), /no reason|unknown|rejected because|Exception/i)
  const stallEl = stepEl(m, S_STALL.id)
  assert.equal(stallEl.querySelector('.jobd-work-headline').textContent, 'Trovis')
  assert.equal(stallEl.querySelector('.jobd-work-line').textContent, 'Stall detected · no activity for 4h')
  m.unmount()
})

test('11/25. completion reads “Work record closed”, tagged “Record closed” — no Success, Succeeded, Resolved, verdict or score anywhere in the section', async () => {
  stub()
  const m = await mount(page())
  await m.settle()
  const el = stepEl(m, S_CLOSED.id)
  assert.equal(el.querySelector('.jobd-work-headline').textContent, 'returns-bot')
  assert.equal(el.querySelector('.jobd-work-line').textContent, 'Work record closed')
  assert.doesNotMatch(work(m).textContent, VERDICTS)
  assert.doesNotMatch(now(m).textContent, VERDICTS)
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
  assert.match(rowText(el), /Trovis\s*Work record closed as abandoned/)
  assert.ok(el.classList.contains('type-completed'))
  const d = await select(m, abandoned.id)
  assert.match(d.textContent, /Closure\s*abandoned/)
  assert.doesNotMatch(work(m).textContent, /failed|failure|success|gave up/i)
  m.unmount()
})

// --- 13, 14, 15: possession ---------------------------------------------------------------------

test('13/14/A. the situation reads possession.current_holder only — “Waiting for Stripe” though the last step is Stripe’s own wait and the first is the agent’s handoff; said once', async () => {
  stub({ graph: OPEN_GRAPH })
  const m = await mount(page())
  await m.settle()
  assert.equal(state(m), 'Waiting for Stripe')
  assert.equal(now(m).dataset.holder, 'Stripe')
  assert.match(support(m), /^Stripe: payment processing .* ago\.$/, 'the supporting sentence is the last step’s own record, because that record names the holder')
  // One presentation of the state: no pill, no holder line, no second phrasing anywhere above Details.
  assert.ok(!m.$('.jobd-holder') && !m.$('.work-status-pill') && !m.$('.jobd-handoff'))
  const aboveDetails = [now(m).textContent, work(m).textContent].join(' ')
  assert.doesNotMatch(aboveDetails, /Waiting on someone|Held by|With Stripe/)
  m.unmount()
})

test('A2. a person holds it: “Waiting for Alex”, with “Chief of Staff handed this to Alex … ago.” only because the last step is that handoff', async () => {
  const handoff = { ...S_HANDOFF, actor: { type: 'agent', label: 'Chief of Staff:main' }, details: { ...S_HANDOFF.details, target_label: 'Alex', reason: 'for review' } }
  const segs = [{ holder_type: 'agent', holder: 'Chief of Staff:main', start: later(-60), end: later(0), waiting: false, touches: [], event_count: 1 },
    { holder_type: 'human', holder: 'Alex', start: later(0), end: null, waiting: true, touches: [], event_count: 0 }]
  stub({ graph: graphFor(41, { steps: [handoff], chronology: [handoff.id], possession: { segments: segs, current_holder: segs[1] } }) })
  const m = await mount(page())
  await m.settle()
  assert.equal(state(m), 'Waiting for Alex')
  assert.match(support(m), /^Chief of Staff handed this to Alex .* ago\.$/)
  assert.equal(m.$$('.jobd-work-step').length, 1, 'one truthful event, no invented Started/Reviewed rows')
  assert.equal(stepEl(m, handoff.id).querySelector('.jobd-work-headline').textContent, 'Chief of Staff → Alex')
  assert.equal(stepEl(m, handoff.id).querySelector('.jobd-work-line').textContent, 'for review')
  assert.doesNotMatch(work(m).textContent, /Handed to a person/, 'the mechanics are not repeated under a who → whom headline')
  // The first screen — situation and Activity — says it once; Details may carry the status word.
  assert.doesNotMatch(now(m).textContent + work(m).textContent, /Started|Reviewed|Processed|Opened|Held by|Waiting on someone/)
  m.unmount()
})

test('A3. the supporting sentence is withheld when the last step does not itself name the holder', async () => {
  // Last step: a handoff to Sarah; endpoint’s current holder: Stripe. No sentence links them.
  const segs = SEGMENTS.slice(0, 4).map((s, i) => (i === 3 ? { ...s, end: null } : s))
  stub({ graph: graphFor(41, { steps: [S_HANDOFF], chronology: [S_HANDOFF.id], possession: { segments: segs, current_holder: segs[3] } }) })
  const m = await mount(page())
  await m.settle()
  assert.equal(state(m), 'Waiting for Stripe')
  assert.equal(support(m), null)
  m.unmount()
})

test('14b/B. no current holder in the response: the situation says only what the status says — nothing inferred from the latest step or an open segment', async () => {
  // Steps end with a handoff to Sarah, yet the endpoint names no current holder.
  stub({ graph: graphFor(41, { steps: [S_HANDOFF], chronology: [S_HANDOFF.id], possession: { segments: SEGMENTS.slice(0, 1), current_holder: null } }) })
  const m = await mount(page())
  await m.settle()
  assert.equal(state(m), 'Waiting on someone')
  assert.equal(support(m), null)
  assert.doesNotMatch(now(m).textContent, /Sarah|Held by|Waiting for/)
  assert.equal(now(m).dataset.holder, undefined)
  m.unmount()
  // And a graph with no possession block at all is the same.
  stub({ graph: graphFor(41, { possession: null }) })
  const m2 = await mount(page())
  await m2.settle()
  assert.equal(state(m2), 'Waiting on someone')
  m2.unmount()
})

test('14c/G. “Waiting for you” and “Work record closed” are the session’s and the status’s words; a closed record is never a success', async () => {
  const details = { 41: detailFor(41, 'Return #4471', { status: 'waiting_on_you', awaiting_handoff_event_id: 10, holder: { kind: 'human', name: 'Alex' } }) }
  stub({ graph: OPEN_GRAPH, details })
  const m = await mount(page())
  await m.settle()
  assert.equal(state(m), 'Waiting for you')
  assert.ok(now(m).classList.contains('is-you'))
  assert.deepEqual([...now(m).querySelectorAll('button')].map((b) => b.textContent), ['Approve', 'Send back'],
    'the two genuine decisions on the handoff, and nothing else — Ask is the page’s global pill, not a disposition of the work')
  m.unmount()
  stub({ graph: graphFor(41), details: { 41: detailFor(41, 'Return #4471', { status: 'done' }) } })
  const m2 = await mount(page())
  await m2.settle()
  assert.equal(state(m2), 'Work record closed')
  assert.match(support(m2), /^returns-bot closed the record .* ago\.$/)
  assert.doesNotMatch(now(m2).textContent, /Finished|Success|Succeeded|Completed|Done|Resolved/i)
  assert.equal(m2.$$('.run-now button').length, 0, 'no decisions on a closed record')
  m2.unmount()
  // A closed status with no closure step in the graph still says only that the record closed.
  stub({ graph: EMPTY_GRAPH, details: { 41: detailFor(41, 'Return #4471', { status: 'done' }) } })
  const m3 = await mount(page())
  await m3.settle()
  assert.equal(state(m3), 'Work record closed')
  assert.equal(support(m3), null)
  m3.unmount()
})

test('decisions: secondary, under the statement, functional, and only when the work is on you with a handoff to resolve', async () => {
  const calls = { complete: [], decline: [] }
  api.completeHandoff = async (loopId, eventId) => { calls.complete.push([loopId, eventId]); return { ok: true } }
  api.declineHandoff = async (loopId, eventId) => { calls.decline.push([loopId, eventId]); return { ok: true } }
  const onYou = { 41: detailFor(41, 'Return #4471', { status: 'waiting_on_you', awaiting_handoff_event_id: 10, holder: { kind: 'human', name: 'Alex' } }) }
  const handoff = { ...S_HANDOFF, actor: { type: 'agent', label: 'Chief of Staff:main' }, details: { ...S_HANDOFF.details, target_label: 'Alex', reason: 'for review' } }
  const graph = graphFor(41, { steps: [handoff], chronology: [handoff.id] })
  let resolved = 0
  stub({ graph, details: onYou })
  const m = await mount(page({ onResolved: () => { resolved += 1 } }))
  await m.settle()
  const sec = now(m)
  // Structure: statement, sentence, then the controls — the statement leads.
  const kids = [...sec.children].map((c) => c.className)
  assert.deepEqual(kids, ['run-now-eyebrow', 'run-now-state', 'run-now-support', 'run-now-actions'])
  assert.equal(state(m), 'Waiting for you')
  assert.match(support(m), /^Chief of Staff handed this to you .* ago\.$/)
  const buttons = [...sec.querySelectorAll('button')]
  assert.deepEqual(buttons.map((b) => b.textContent), ['Approve', 'Send back'])
  assert.ok(buttons.every((b) => b.classList.contains('btn-ghost') && !b.classList.contains('btn-primary')), 'secondary controls, none dressed as the primary thing on the page')
  assert.ok(!sec.textContent.includes('Ask'), 'no Ask in the situation block')
  await m.click(buttons[0])
  await m.settle()
  assert.deepEqual(calls.complete, [[41, 10]], 'Approve completes the open handoff by its id')
  assert.equal(resolved, 1)
  m.unmount()
  // Send back declines the same handoff.
  stub({ graph, details: onYou })
  const m2 = await mount(page({ onResolved: () => { resolved += 1 } }))
  await m2.settle()
  await m2.click([...now(m2).querySelectorAll('button')].find((b) => b.textContent === 'Send back'))
  await m2.settle()
  assert.deepEqual(calls.decline, [[41, 10]])
  m2.unmount()
  // On you but with no handoff id from the server: no controls at all, no dead buttons.
  stub({ graph, details: { 41: detailFor(41, 'Return #4471', { status: 'waiting_on_you', awaiting_handoff_event_id: null, holder: { kind: 'human', name: 'Alex' } }) } })
  const m3 = await mount(page())
  await m3.settle()
  assert.equal(state(m3), 'Waiting for you')
  assert.equal(m3.$$('.run-now button').length, 0)
  assert.ok(!m3.$('.run-now-actions'))
  m3.unmount()
  // Not on you: no decision group in any state.
  for (const status of ['waiting_on_other', 'moving', 'stuck', 'done']) {
    stub({ graph: OPEN_GRAPH, details: { 41: detailFor(41, 'Return #4471', { status, awaiting_handoff_event_id: 10 }) } })
    const mx = await mount(page())
    await mx.settle()
    assert.ok(!mx.$('.run-now-actions') && mx.$$('.run-now button').length === 0, `no decision group when ${status}`)
    mx.unmount()
  }
})

test('14d. a stuck run reads “Needs attention”, with the holder’s own exception record when the last step is one', async () => {
  const stuckGraph = graphFor(41, { steps: [S_HANDOFF, S_WAIT, S_STUCK], chronology: [S_HANDOFF.id, S_WAIT.id, S_STUCK.id],
    possession: { segments: SEGMENTS.slice(0, 4), current_holder: { ...SEGMENTS[3], end: null } } })
  stub({ graph: stuckGraph, details: { 41: detailFor(41, 'Return #4471', { status: 'stuck' }) } })
  const m = await mount(page())
  await m.settle()
  assert.equal(state(m), 'Needs attention')
  assert.match(support(m), /^Stuck on Stripe · Your card was declined .* ago\.$/)
  assert.doesNotMatch(now(m).textContent, /failed|failure|error/i)
  m.unmount()
})

test('15. the holder history sits inside Details as history: the endpoint’s segments, in order, each with its own waiting flag — never a “now”', async () => {
  stub({ graph: OPEN_GRAPH })
  const m = await mount(page())
  await m.settle()
  const hist = m.$('.run-held')
  assert.ok(hist && details(m).contains(hist), 'inside Details, not beside the story')
  assert.equal(hist.querySelector('h3').textContent, 'Who held the work')
  const rows = [...hist.querySelectorAll('li')].map((li) => [...li.children].map((c) => c.textContent).join(' '))
  assert.deepEqual(rows, [
    'Agent returns-bot', 'Person Sarah Chen waiting', 'Agent returns-bot', 'System Stripe waiting',
  ])
  assert.doesNotMatch(hist.textContent, /\bnow\b|current/i, 'who has it now is the situation’s, from current_holder — the history does not repeat or re-derive it')
  assert.doesNotMatch(hist.textContent, /because|caused|led to|→/, 'history, not causality')
  assert.equal(state(m), 'Waiting for Stripe', 'the situation answers "now", from possession.current_holder')
  m.unmount()
})

test('15b/B. an open-ended last segment with no current_holder: the history lists it, nothing says “now”, and the situation never names Stripe', async () => {
  const openSegs = SEGMENTS.slice(0, 4).map((s, i) => (i === 3 ? { ...s, end: null } : s))
  stub({ graph: graphFor(41, { steps: [S_HANDOFF, S_WAIT], chronology: [S_HANDOFF.id, S_WAIT.id], possession: { segments: openSegs, current_holder: null } }) })
  const m = await mount(page())
  await m.settle()
  const rows = [...m.$('.run-held').querySelectorAll('li')].map((li) => [...li.children].map((c) => c.textContent).join(' '))
  assert.equal(rows[3], 'System Stripe waiting')
  assert.doesNotMatch(m.$('.run-held').textContent, /\bnow\b|current/i)
  assert.equal(state(m), 'Waiting on someone', 'no current holder from the endpoint → the status alone, not the open segment')
  assert.equal(support(m), null)
  assert.doesNotMatch(now(m).textContent, /Stripe|Held by|Waiting for|has this/)
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
  assert.ok(details(m).hasAttribute('open'), 'Details opens so the row can be seen')
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
  await m.click(tab(m, 'Activity'))
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
  await m.click(tab(m, 'Activity'))
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
  assert.ok(!m.$('.jobd-work') && !m.$('.run-now') && !m.$('.run-details'), 'the Activity levels are the Activity view, not stacked under Execution')
  m.unmount()
})

// --- C, H, I, header: the five-second read ---------------------------------------------------------

test('C. one Work Step is a one-row story: nothing synthetic is added to make it look richer', async () => {
  stub({ graph: graphFor(41, { steps: [S_HANDOFF], chronology: [S_HANDOFF.id] }) })
  const m = await mount(page())
  await m.settle()
  assert.equal(m.$$('.jobd-work-step').length, 1)
  assert.doesNotMatch(work(m).textContent, /Started|Reviewed|Processed|Opened|Model|Tool|tool_call|agent_run|HTTP|loop_opened|action_reported|cost/i)
  assert.ok(!work(m).querySelector('.jobd-work-empty'), 'one step is a story, not an empty state')
  const only = m.$('.jobd-work-step')
  assert.ok(only.matches(':first-child') && only.matches(':last-child'), 'a single row is the last row — the connector rule draws nothing past it')
  assert.ok(only.querySelector('.jobd-work-at') && only.querySelector('.jobd-work-dot') && only.querySelector('.jobd-work-headline'), 'time, marker, headline — the timeline shape holds with one event')
  m.unmount()
})

test('polish: a real timeline — Activity heading, time · marker · text per row, rows in the endpoint’s order, a plain Details summary closed by default', async () => {
  stub()
  const m = await mount(page())
  await m.settle()
  const h = work(m).querySelector('h3')
  assert.equal(h.textContent, 'Activity')
  assert.ok(h.classList.contains('run-h') && !h.classList.contains('dash-caps'), 'a section heading, not a tiny caps label')
  const rows = m.$$('.jobd-work-step')
  assert.deepEqual(rows.map((li) => li.dataset.stepId), STEPS.map((s) => s.id), 'chronological, as the endpoint ordered them')
  for (const li of rows) {
    const kids = [...li.querySelector('.jobd-work-row').children].map((c) => c.className.split(' ')[0])
    assert.deepEqual(kids, ['jobd-work-at', 'jobd-work-dot', 'jobd-work-main'], 'time column, marker column, then the event')
  }
  const summary = details(m).querySelector('summary')
  assert.equal(summary.textContent.trim(), 'Details', 'no inventory of internal sections in the collapsed summary')
  assert.ok(!details(m).hasAttribute('open'))
  assert.ok(!m.$('.run-details-hint'))
  // Opened, everything PR 218 put inside is still there.
  await m.click(summary)
  await m.settle()
  const inside = [...details(m).querySelectorAll('.jobd-section')].map((s) => s.getAttribute('aria-label'))
  assert.deepEqual(inside, ['Run information', 'Visibility', 'Who held the work', 'Evidence', 'How this job ran'])
  m.unmount()
})

test('polish: the situation carries one eyebrow word from the status, and none when it would repeat the headline', async () => {
  stub({ graph: OPEN_GRAPH })
  const m = await mount(page())
  await m.settle()
  assert.equal(now(m).querySelector('.run-now-eyebrow')?.textContent, 'Waiting')
  assert.equal(state(m), 'Waiting for Stripe')
  assert.deepEqual([...now(m).children].map((c) => c.className), ['run-now-eyebrow', 'run-now-state', 'run-now-support'])
  m.unmount()
  stub({ graph: OPEN_GRAPH, details: { 41: detailFor(41, 'Return #4471', { status: 'stuck' }) } })
  const m2 = await mount(page())
  await m2.settle()
  assert.equal(state(m2), 'Needs attention')
  assert.ok(!m2.$('.run-now-eyebrow'), 'eyebrow dropped: it would only repeat the headline')
  m2.unmount()
  stub({ graph: EMPTY_GRAPH, details: { 41: detailFor(41, 'Return #4471', { status: 'done' }) } })
  const m3 = await mount(page())
  await m3.settle()
  assert.equal(now(m3).querySelector('.run-now-eyebrow')?.textContent, 'Closed')
  assert.equal(state(m3), 'Work record closed')
  assert.doesNotMatch(now(m3).textContent, /success|complete/i)
  m3.unmount()
})

test('H. Visibility inside Details keeps its states: Unknown stays Unknown, and observed execution earns no “healthy” verdict anywhere', async () => {
  stub()
  const m = await mount(page())
  await m.settle()
  const vis = m.$('.jobd-visibility')
  assert.ok(details(m).contains(vis))
  const actions = vis.querySelector('[data-dimension="actions"]')
  assert.match(actions.textContent, /Unknown/)
  assert.match(actions.textContent, /can.t determine action visibility/)
  assert.doesNotMatch(actions.textContent, /missing|failed|not connected|none/i)
  assert.doesNotMatch(m.$('.job-page').textContent, /healthy|health score|\d+\s?%|verified|on track/i)
  assert.equal(m.$$('.job-page [class*="good"], .job-page [class*="healthy"], .job-page [class*="success"]').length, 0)
  m.unmount()
})

test('I. no fake controls: the only buttons are navigation, rows, the two record doors, Retry, and the real decisions when the work is on you', async () => {
  stub({ graph: OPEN_GRAPH })
  const m = await mount(page())
  await m.settle()
  const labels = m.$$('button').map((b) => b.textContent.trim())
  assert.doesNotMatch(labels.join('|'), /Take action|Reassign|Add note|Approve|Resolve|Send back|Escalate|Mark/i, 'no decision controls when the work is not on you, none invented at all')
  await select(m, S_WAIT.id)
  const after = m.$$('button').map((b) => b.textContent.trim())
  const allowed = /^(← Returns|Returns|Activity|Execution|View in Execution|View evidence|Retry|)$/
  for (const l of after) {
    if (l && !allowed.test(l)) {
      const isRow = m.$$('.jobd-work-row').some((r) => r.textContent.trim() === l)
      // The agent's name in "How this job ran" has always opened its Fleet page — real, pre-existing navigation.
      const isAgentLink = m.$$('.jobd-run-agent').some((b) => b.textContent.trim() === l)
      assert.ok(isRow || isAgentLink, `unexpected control: ${l}`)
    }
  }
  m.unmount()
})

test('header: title dominates; the job it belongs to is one link; no ids, source names or observability metadata', async () => {
  let opened = null
  stub()
  const m = await mount(page({ onOpenJob: (id) => { opened = id } }))
  await m.settle()
  const head = m.$('.jobd-head')
  assert.equal(head.querySelector('.jobd-title').textContent, 'Return #4471')
  const job = head.querySelector('.run-job-link')
  assert.equal(job.textContent, 'Returns')
  await m.click(job)
  assert.equal(opened, 7, 'opens the job by the workflow id the detail carries')
  assert.doesNotMatch(head.textContent, /Run ID|event:|span|trace|grok|Grok|telemetry|Observed|Held by|Waiting|\$/)
  // Run information keeps the bookkeeping inside Details.
  const info = m.$('.run-info')
  assert.ok(details(m).contains(info))
  assert.match(info.textContent, /Job\s*Returns/)
  assert.match(info.textContent, /Run ID\s*41/)
  assert.match(info.textContent, /Status\s*Waiting on someone/)
  assert.match(info.textContent, /Last updated\s*\S+ ago/)
  assert.doesNotMatch(info.textContent, /Priority|Owner|Outcome|Source|Confirmed/i)
  m.unmount()
})

test('header without a job link: the job name stays text; without a job, no line at all', async () => {
  stub()
  const m = await mount(page({ onOpenJob: undefined }))
  await m.settle()
  assert.ok(!m.$('.run-job-link') && m.$('.run-job-name')?.textContent === 'Returns')
  m.unmount()
  stub({ details: { 41: detailFor(41, 'Return #4471', { workflow_id: null, workflow_name: null }) } })
  const m2 = await mount(page())
  await m2.settle()
  assert.ok(!m2.$('.run-job'))
  m2.unmount()
})
