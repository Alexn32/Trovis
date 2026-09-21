// The Run page's Execution view, mounted: a vertical tree from recorded
// parentage, a type-aware inspector, honest fallbacks, and no stale run.
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

const detailFor = (id, title) => ({
  id, title, status: 'done', holder: { kind: 'agent', name: 'returns-bot' },
  whats_next: null, updated_at: ago(3), awaiting_handoff_event_id: null, workflow_id: 7, workflow_name: 'Returns',
  whats_happening: null, process: null,
  timeline: [
    { at: ago(30), text: 'Started', actor: { kind: 'agent', name: 'returns-bot' } },
    { at: ago(10), text: 'Finished', actor: { kind: 'agent', name: 'returns-bot' } },
  ],
  provenance: { source: 'telemetry', suggestion_id: null },
})
const DETAILS = { 41: detailFor(41, 'Return #9'), 42: detailFor(42, 'Return #10') }
const RUNS = [{ name: 'tool_call', agent: 'returns-bot', service_name: 'returns-bot', agent_id: 'main', at: ago(25), errored: false, duration_ms: 1400, cost_usd: null, tool: 'lookup_order', error: null }]
const EVIDENCE = { item_id: 41, generated_at: T, spans_truncated: false, evidence: [{
  id: 'exec:grok-bot', item_id: 41, evidence_type: 'execution', observed_at: ago(30), source_type: 'agent', source_connector_id: 'grok-bot',
  source_label: 'returns-bot:main', correlation_method: 'explicit_key', event_id: null, span_id: null, trace_id: null,
  external_object_id: null, external_event_id: null, details: { span_count: 3, error_count: 0, last_observed_at: ago(10), trace_ids: [], correlation_methods: ['explicit_key'] },
}] }
const COVERAGE = { item_id: 41, generated_at: T, evidence_bounded: false, dimensions: [
  { id: 'execution', state: 'observed', reason: 'execution_evidence', evidence_count: 1, last_observed_at: ago(10), evidence_types: [], sources: [], correlation_methods: [], from_bounded_evidence: false, details: {} },
] }

const prov = (over = {}) => ({
  record: 'span', span_id: 'aaaaaaaaaaaaaaaa', trace_id: 'a'.repeat(32), parent_span_id: null,
  parent_status: 'none', event_id: null, evidence_kind: 'execution', correlation: 'explicit_key',
  loop_link: 'key_batch', classification_basis: 'event_type', span_kind: 'internal', event_type: null, ...over,
})
const node = (over = {}) => ({
  id: 'span:x', type: 'other', parent_id: null, started_at: T, ended_at: later(1), duration_ms: 1000,
  label: 'x', worker: { label: 'returns-bot:main', service_name: 'returns-bot', agent_id: 'main' },
  connector: { id: 'grok-bot', method: 'mcp' }, status: 'unset', error: null, model: null, tool: null,
  usage: null, cost: null, event: null, provenance: prov(), ...over,
})
function execBody(itemId = 41, over = {}) {
  const kid = (o) => node({ parent_id: 'span:root', provenance: prov({ parent_span_id: 'r', parent_status: 'attached' }), ...o })
  const nodes = [
    node({ id: 'span:root', type: 'worker', label: 'agent_run', duration_ms: 12400, ended_at: later(12.4), provenance: prov({ span_id: 'r', event_type: 'agent_run' }) }),
    kid({ id: 'span:m1', type: 'model', label: 'model_call', started_at: later(0.1), duration_ms: 1800, model: { name: 'grok-4', provider: 'xai' },
      usage: { input_tokens: 1000, output_tokens: 284, total_tokens: 1284, cache_creation_input_tokens: null, cache_read_input_tokens: null },
      cost: { known: true, source: 'estimated', amount_usd: 0.02 } }),
    kid({ id: 'span:t1', type: 'tool', label: 'tool_call', started_at: later(2), duration_ms: 382,
      tool: { name: 'mcp__shopify__lookup_order', identifier: 'mcp__shopify__lookup_order', display_name: 'lookup_order', mcp_server: 'shopify', call_id: 'c1', reported_success: true },
      provenance: prov({ parent_span_id: 'r', parent_status: 'attached', evidence_kind: 'action_reported', span_id: 'bbbbbbbbbbbbbbbb' }) }),
    kid({ id: 'span:m2', type: 'model', label: 'model_call', started_at: later(3), duration_ms: 2100, model: { name: 'grok-4', provider: 'xai' },
      usage: { input_tokens: 0, output_tokens: 0, total_tokens: 0, cache_creation_input_tokens: null, cache_read_input_tokens: null },
      cost: { known: true, source: 'covered', amount_usd: null } }),
    kid({ id: 'span:m3', type: 'model', label: 'model_call', started_at: later(4), duration_ms: 900, model: { name: 'no-such-model', provider: null },
      usage: { input_tokens: 40, output_tokens: 8, total_tokens: 48, cache_creation_input_tokens: null, cache_read_input_tokens: null }, cost: null }),
    kid({ id: 'span:t2', type: 'tool', label: 'tool_call', started_at: later(5), duration_ms: 843, status: 'error', error: 'Card declined',
      tool: { name: 'stripe.refunds.create', identifier: 'stripe.refunds.create', display_name: 'stripe.refunds.create', mcp_server: null, call_id: 'c2', reported_success: false },
      provenance: prov({ parent_span_id: 'r', parent_status: 'attached', evidence_kind: 'action_reported', span_id: 'cccccccccccccccc' }) }),
    kid({ id: 'span:t3', type: 'tool', label: 'tool_call', started_at: later(6), duration_ms: 611,
      tool: { name: 'stripe.refunds.create', identifier: 'stripe.refunds.create', display_name: 'stripe.refunds.create', mcp_server: null, call_id: 'c3', reported_success: true },
      provenance: prov({ parent_span_id: 'r', parent_status: 'attached', evidence_kind: 'action_reported' }) }),
    node({ id: 'span:h', type: 'other', label: 'HTTP GET', parent_id: 'span:t3', started_at: later(6.1), duration_ms: 200, provenance: prov({ parent_span_id: 't3', parent_status: 'attached' }) }),
    kid({ id: 'span:done', type: 'worker', label: 'agent_run_complete', started_at: later(12), duration_ms: 5, cost: { known: true, source: 'reported', amount_usd: 0.03 },
      provenance: prov({ parent_span_id: 'r', parent_status: 'attached', event_type: 'agent_run_complete' }) }),
    node({ id: 'event:9', type: 'completion', label: 'loop_closed', started_at: later(12.5), ended_at: null, duration_ms: null, status: 'recorded', worker: null, connector: null,
      event: { type: 'loop_closed', direction: null, actor: { type: 'agent', label: 'returns-bot:main' }, reason: 'completed_by_agent' },
      provenance: prov({ record: 'loop_event', span_id: null, trace_id: null, event_id: 9, evidence_kind: 'completion', correlation: 'direct', loop_link: null, span_kind: null }) }),
  ]
  return {
    item_id: itemId, generated_at: T, trace_ids: ['a'.repeat(32)], nodes, roots: ['span:root', 'event:9'],
    chronology: nodes.map((n) => n.id), bounded: false, span_limit: 2000, spans_read: 9, events_read: 1,
    duplicate_spans_dropped: 0, cycles_broken: 0, summary: {}, ...over,
  }
}
/** Grok Bot's flat reports: nothing attached anywhere. */
function flatBody() {
  const rep = (id, name, s) => node({ id, type: 'worker', label: name, started_at: later(s), ended_at: later(s), duration_ms: 0,
    provenance: prov({ trace_id: 'd'.repeat(32), span_id: id.slice(5), event_type: 'agent_activity' }) })
  const nodes = [rep('span:j1', 'job_started', 0), rep('span:j2', 'job_waiting', 4), rep('span:j3', 'job_finished', 48)]
  return { item_id: 41, generated_at: T, trace_ids: ['d'.repeat(32)], nodes, roots: nodes.map((n) => n.id),
    chronology: nodes.map((n) => n.id), bounded: false, span_limit: 2000, spans_read: 3, events_read: 0, duplicate_spans_dropped: 0, cycles_broken: 0, summary: {} }
}

function stub({ execution = execBody(), executionFail = false, evidenceFail = false, coverageFail = false } = {}) {
  const calls = { item: [], execution: [], evidence: [], coverage: [], graph: [] }
  api.getWorkItem = async (id, opts = {}) => {
    calls.item.push([id, opts.include || null])
    const d = DETAILS[id] || DETAILS[41]
    return opts.include === 'runs' ? { ...d, runs: RUNS } : d
  }
  // The page also reads the Work Graph (its own section, its own tests in
  // jobDetailWorkGraph.test.mjs); keep it off the network here.
  api.getWorkItemGraph = async (id) => {
    calls.graph.push(id)
    return { item_id: id, generated_at: T, steps: [], chronology: [], possession: { segments: [], current_holder: null }, lifecycle: [], bounded: false, span_limit: 2000, spans_read: 0, events_read: 0, summary: {} }
  }
  api.getWorkItemEvidence = async (id) => { calls.evidence.push(id); if (evidenceFail) throw new Error('down'); return EVIDENCE }
  api.getWorkItemCoverage = async (id) => { calls.coverage.push(id); if (coverageFail) throw new Error('down'); return COVERAGE }
  api.getWorkItemExecution = async (id) => {
    calls.execution.push(id)
    if (typeof executionFail === 'function' ? executionFail() : executionFail) throw new Error('execution down')
    return typeof execution === 'function' ? execution(id) : execution
  }
  return calls
}

const page = (over = {}) =>
  React.createElement(JobDetail, { variant: 'page', item: { id: 41 }, onClose: () => {}, onOpenAgent: () => {}, backLabel: '← Returns', ...over })

const tab = (m, label) => m.$$('.jobd-view').find((b) => b.textContent === label)
async function openExecution(m) {
  await m.click(tab(m, 'Execution'))
  await m.settle()
}
const nodeEl = (m, id) => m.container.querySelector(`[data-node-id="${id}"]`)
const rowText = (el) => el.querySelector(':scope > .exec-row').textContent

// --- entry and context -----------------------------------------------------------

test('1/2. Execution is a view under the Run: the switch sits under the header, the title, status and back link stay, and the fetch is per view', async () => {
  const calls = stub()
  const m = await mount(page())
  await m.settle()
  assert.ok(tab(m, 'Run') && tab(m, 'Execution'), 'Run | Execution switch present')
  assert.equal(tab(m, 'Run').getAttribute('aria-selected'), 'true')
  assert.deepEqual(calls.execution, [], 'nothing fetched while the Run view is showing')
  assert.ok(m.$('.jobd-evidence') && m.$('.jobd-visibility'), 'Run view keeps Visibility and Evidence')
  await openExecution(m)
  assert.deepEqual(calls.execution, [41])
  assert.match(m.$('.jobd-title').textContent, /Return #9/)
  assert.match(m.$('.jobd-close').textContent, /← Returns/)
  assert.ok(m.$('.work-status-pill'), 'status pill still in the header')
  assert.ok(m.$('.exec'), 'Execution section rendered')
  assert.ok(!m.$('.jobd-evidence') && !m.$('.jobd-visibility'), 'the Run sections are the Run view, not stacked under Execution')
  await m.click(tab(m, 'Run'))
  await m.settle()
  assert.ok(m.$('.jobd-evidence') && !m.$('.exec'), 'switching back restores the Run view unchanged')
  m.unmount()
})

test('32/33. Evidence and Visibility still load independently of Execution, and an Execution failure touches neither', async () => {
  const calls = stub({ executionFail: true })
  const m = await mount(page())
  await m.settle()
  assert.deepEqual(calls.evidence, [41])
  assert.deepEqual(calls.coverage, [41])
  assert.match(m.$('.jobd-evidence').textContent, /returns-bot/)
  await openExecution(m)
  assert.match(m.$('.exec').textContent, /Execution couldn.t be loaded/)
  await m.click(tab(m, 'Run'))
  await m.settle()
  assert.match(m.$('.jobd-visibility').textContent, /Observed/)
  m.unmount()
})

// --- the tree -----------------------------------------------------------------------

test('3. one agent_run root renders its children vertically, indented under it, in the endpoint order', async () => {
  stub()
  const m = await mount(page())
  await m.settle()
  await openExecution(m)
  const root = nodeEl(m, 'span:root')
  assert.equal(root.dataset.depth, '0')
  const kids = [...root.querySelectorAll(':scope > .exec-children > li')]
  assert.deepEqual(kids.map((k) => k.dataset.nodeId), ['span:m1', 'span:t1', 'span:m2', 'span:m3', 'span:t2', 'span:t3', 'span:done'])
  assert.ok(kids.every((k) => k.dataset.depth === '1'))
  assert.equal(nodeEl(m, 'span:h').dataset.depth, '2', 'the HTTP span sits under the tool span that recorded it as parent')
  assert.ok(m.$('.exec-tree[role="tree"]'))
  m.unmount()
})

test('4/5. the worker row is the worker label; the connector is not in the row and IS in the inspector', async () => {
  stub()
  const m = await mount(page())
  await m.settle()
  await openExecution(m)
  const root = nodeEl(m, 'span:root')
  assert.match(rowText(root), /returns-bot/)
  assert.doesNotMatch(rowText(root), /Grok Bot/)
  await m.click(root.querySelector(':scope > .exec-row .exec-select'))
  const insp = m.$('.exec-inspector')
  assert.match(insp.textContent, /Worker\s*returns-bot/)
  assert.match(insp.textContent, /Connector\s*Grok Bot/)
  m.unmount()
})

test('6/7/8/9. model rows: known model + tokens + duration + cost; missing cost is not $0; zero usage stays 0; covered reads "in run total"', async () => {
  stub()
  const m = await mount(page())
  await m.settle()
  await openExecution(m)
  const m1 = rowText(nodeEl(m, 'span:m1'))
  assert.match(m1, /Model · grok-4/)
  assert.match(m1, /1\.8s/)
  assert.match(m1, /1,284 tokens · \$0\.02/)
  const m3 = rowText(nodeEl(m, 'span:m3'))
  assert.match(m3, /48 tokens/)
  assert.doesNotMatch(m3, /\$/, 'unknown cost shows no dollar figure at all')
  const m2 = rowText(nodeEl(m, 'span:m2'))
  assert.match(m2, /0 tokens · in run total/)
  assert.doesNotMatch(m2, /\$0/)
  m.unmount()
})

test('10/11. tool rows keep the technical identity; the MCP one reads Tool · Shopify over its mcp__ name', async () => {
  stub()
  const m = await mount(page())
  await m.settle()
  await openExecution(m)
  const t1 = rowText(nodeEl(m, 'span:t1'))
  assert.match(t1, /Tool · Shopify/)
  assert.match(t1, /mcp__shopify__lookup_order/)
  assert.match(t1, /382ms/)
  const t3 = rowText(nodeEl(m, 'span:t3'))
  assert.match(t3, /stripe\.refunds\.create/)
  assert.doesNotMatch(t3, /Tool · Stripe/, 'a dotted name is not turned into a brand')
  m.unmount()
})

test('12/13/14/34. reported success is silent; the failed call shows its error; two stripe calls are two rows, neither a retry; no wall of badges', async () => {
  stub()
  const m = await mount(page())
  await m.settle()
  await openExecution(m)
  const ok = rowText(nodeEl(m, 'span:t3'))
  assert.doesNotMatch(ok, /success|succeeded|verified|refunded|completed|✓|✔/i)
  const bad = nodeEl(m, 'span:t2')
  assert.ok(bad.classList.contains('has-error'))
  assert.match(bad.querySelector('.exec-error').textContent, /Error · Card declined/)
  const stripe = m.$$('.exec-node').filter((n) => rowText(n).includes('stripe.refunds.create'))
  assert.equal(stripe.length, 2)
  const text = m.$('.exec').textContent
  assert.doesNotMatch(text, /retry|attempt/i)
  assert.equal(m.$$('.exec-error').length, 1, 'exactly one error treatment on the page')
  assert.equal(m.$$('.exec-node').filter((n) => /success|✓/i.test(rowText(n))).length, 0)
  assert.doesNotMatch(text, /healthy|score|grade|verdict/i)
  m.unmount()
})

test('18/19. other nodes are visible with a neutral label; completion reads as the record closing, not a business outcome', async () => {
  stub()
  const m = await mount(page())
  await m.settle()
  await openExecution(m)
  assert.match(rowText(nodeEl(m, 'span:h')), /Activity\s*HTTP GET/)
  const done = nodeEl(m, 'event:9')
  assert.match(rowText(done), /Work record closed/)
  assert.doesNotMatch(rowText(done), /verified|outcome|success/i)
  assert.equal(done.dataset.depth, '0', 'a Work record event is a root, never hung under a span')
  m.unmount()
})

test('header facts come from the record: nodes, observed span, tokens, cost (covered not double-counted), errors — and no verdict', async () => {
  stub()
  const m = await mount(page())
  await m.settle()
  await openExecution(m)
  const facts = m.$('.exec-facts-row').textContent
  assert.match(facts, /10 nodes/)
  assert.match(facts, /observed over 12s/)
  assert.match(facts, /1,332 tokens/)
  assert.match(facts, /\$0\.05/)
  assert.match(facts, /1 error/)
  assert.doesNotMatch(m.$('.exec-head').textContent, /%|health|score|success/i)
  m.unmount()
})

// --- roots, traces, fallback ------------------------------------------------------------

test('15/17. multiple roots render as roots with no fake parentage; outside_read_set is a root that says so', async () => {
  const b = execBody()
  b.nodes.push(node({ id: 'span:orphan', type: 'tool', label: 'tool_call', started_at: later(1), parent_id: null,
    tool: { name: 'exec', identifier: 'exec', display_name: 'exec', mcp_server: null, call_id: null, reported_success: null },
    provenance: prov({ parent_span_id: 'ffffffffffffffff', parent_status: 'outside_read_set', span_id: 'eeeeeeeeeeeeeeee' }) }))
  b.roots.push('span:orphan')
  b.chronology.push('span:orphan')
  stub({ execution: b })
  const m = await mount(page())
  await m.settle()
  await openExecution(m)
  const orphan = nodeEl(m, 'span:orphan')
  assert.equal(orphan.dataset.depth, '0')
  assert.match(rowText(orphan), /parent not in this record/)
  assert.ok(!nodeEl(m, 'span:root').contains(orphan), 'not slotted under the run it happened during')
  assert.ok(m.$('.exec-tree'), 'several roots with attached children still draw the tree')
  m.unmount()
})

test('16. multiple traces group roots by recorded trace, in order, with no cross-trace edge and no invented run name', async () => {
  const b = execBody()
  const t2 = 'b'.repeat(32)
  b.nodes.push(node({ id: 'span:r2', type: 'worker', label: 'agent_run', started_at: later(20), provenance: prov({ trace_id: t2, span_id: 'q', event_type: 'agent_run' }) }))
  b.nodes.push(node({ id: 'span:r2m', type: 'model', label: 'model_call', parent_id: 'span:r2', started_at: later(21), model: { name: 'grok-4', provider: null },
    provenance: prov({ trace_id: t2, parent_span_id: 'q', parent_status: 'attached' }) }))
  b.roots.push('span:r2')
  b.trace_ids.push(t2)
  b.chronology.push('span:r2', 'span:r2m')
  stub({ execution: b })
  const m = await mount(page())
  await m.settle()
  await openExecution(m)
  const heads = m.$$('.exec-group-title').map((h) => h.textContent)
  assert.match(heads[0], /^Trace 1/)
  assert.match(heads[1], /^Trace 2/)
  assert.match(heads[2], /^Work record events/)
  assert.doesNotMatch(heads.join(' '), /Run 1|Run 2/)
  assert.equal(nodeEl(m, 'span:r2m').dataset.depth, '1')
  assert.ok(nodeEl(m, 'span:r2').contains(nodeEl(m, 'span:r2m')))
  assert.ok(!nodeEl(m, 'span:root').contains(nodeEl(m, 'span:r2')))
  assert.doesNotMatch(m.$('.exec-tree').textContent, new RegExp(t2), 'raw trace ids stay out of the tree')
  assert.match(m.$('.exec-facts-row').textContent, /2 traces/)
  m.unmount()
})

test('20/21. Grok Bot flat reports: no attached node → chronology in endpoint order, no indentation, no parent, no "broken"', async () => {
  stub({ execution: flatBody() })
  const m = await mount(page())
  await m.settle()
  await openExecution(m)
  assert.ok(!m.$('.exec-tree'), 'no tree is drawn')
  const note = m.$('.exec-fallback-note').textContent
  assert.match(note, /No parent relationships were recorded/)
  assert.match(note, /time order/)
  assert.doesNotMatch(note, /broken|should|missing/i)
  const rows = m.$$('.exec-chrono-row')
  assert.deepEqual(rows.map((r) => r.dataset.nodeId), ['span:j1', 'span:j2', 'span:j3'])
  assert.ok(rows.every((r) => /\d{2}:\d{2}:\d{2}/.test(r.querySelector('.exec-at').textContent)))
  assert.equal(m.$$('.exec-children').length, 0)
  assert.equal(m.$$('[data-depth]').length, 0)
  assert.match(m.$('.exec').textContent, /job_started[\s\S]*job_waiting[\s\S]*job_finished/)
  m.unmount()
})

test('a single root with no children is a one-node tree, not a chronology', async () => {
  const one = flatBody()
  one.nodes = one.nodes.slice(0, 1); one.roots = ['span:j1']; one.chronology = ['span:j1']
  stub({ execution: one })
  const m = await mount(page())
  await m.settle()
  await openExecution(m)
  assert.ok(m.$('.exec-tree'))
  assert.ok(!m.$('.exec-chrono'))
  m.unmount()
})

// --- states -------------------------------------------------------------------------

test('22/23. API failure is not "no execution": a Retry line, then rows after a successful retry', async () => {
  let fail = true
  const calls = stub({ executionFail: () => fail })
  const m = await mount(page())
  await m.settle()
  await openExecution(m)
  const sec = m.$('.exec')
  assert.match(sec.textContent, /Execution couldn.t be loaded\./)
  assert.doesNotMatch(sec.textContent, /No execution has been recorded/)
  assert.ok(sec.querySelector('[role="alert"] button'))
  assert.match(m.$('.jobd-title').textContent, /Return #9/, 'the Run stays')
  fail = false
  await m.click(sec.querySelector('[role="alert"] button'))
  await m.settle()
  assert.deepEqual(calls.execution, [41, 41])
  assert.ok(nodeEl(m, 'span:root'))
  m.unmount()
})

test('22. an empty execution is an honest sentence, distinct from the failure copy', async () => {
  stub({ execution: { ...execBody(), nodes: [], roots: [], chronology: [], trace_ids: [] } })
  const m = await mount(page())
  await m.settle()
  await openExecution(m)
  assert.match(m.$('.exec').textContent, /No execution has been recorded for this run\./)
  assert.doesNotMatch(m.$('.exec').textContent, /couldn.t be loaded/)
  m.unmount()
})

test('loading shows a skeleton and never blanks the Run header', async () => {
  const gate = deferred()
  stub({ execution: () => gate.promise })
  const m = await mount(page())
  await m.settle()
  await openExecution(m)
  assert.ok(m.$('.exec .dash-skel'))
  assert.match(m.$('.jobd-title').textContent, /Return #9/)
  gate.resolve(execBody())
  await m.settle()
  assert.ok(!m.$('.exec .dash-skel') && nodeEl(m, 'span:root'))
  m.unmount()
})

test('24. bounded=true adds one quiet sentence; node rows are unchanged', async () => {
  stub({ execution: execBody(41, { bounded: true, span_limit: 2000, duplicate_spans_dropped: 1 }) })
  const m = await mount(page())
  await m.settle()
  await openExecution(m)
  const notes = m.$$('.exec-note').map((n) => n.textContent)
  assert.ok(notes.some((n) => /bounded: the first 2,000 spans were read/.test(n)))
  assert.ok(notes.some((n) => /1 duplicate span dropped/.test(n)))
  assert.match(rowText(nodeEl(m, 'span:m1')), /Model · grok-4/)
  assert.equal(m.$$('.exec-node').length, 10)
  m.unmount()
})

// --- selection and the inspector -------------------------------------------------------

test('27/28/29. selecting the failed tool call opens the inspector beside the tree and marks the row', async () => {
  stub()
  const m = await mount(page())
  await m.settle()
  await openExecution(m)
  assert.ok(m.$('.exec-inspector.is-empty'), 'a quiet placeholder until something is selected')
  const bad = nodeEl(m, 'span:t2')
  await m.click(bad.querySelector(':scope > .exec-row .exec-select'))
  assert.ok(bad.classList.contains('is-selected'))
  assert.equal(bad.getAttribute('aria-selected'), 'true')
  assert.equal(bad.querySelector(':scope > .exec-row .exec-select').getAttribute('aria-pressed'), 'true')
  const insp = m.$('.exec-inspector:not(.is-empty)')
  assert.ok(insp, 'inspector open')
  assert.match(insp.querySelector('.exec-insp-type').textContent, /Tool/)
  assert.match(insp.textContent, /stripe\.refunds\.create/)
  assert.match(insp.textContent, /Duration\s*843ms/)
  assert.match(insp.textContent, /Status\s*Error/)
  assert.match(insp.textContent, /Error\s*Card declined/)
  assert.match(insp.textContent, /Worker\s*returns-bot/)
  assert.match(insp.textContent, /Connector\s*Grok Bot/)
  assert.match(insp.textContent, /Reported by worker\s*Call failed/)
  assert.match(insp.textContent, /Evidence kind\s*Reported by the worker/)
  const tech = insp.querySelector('.exec-insp-tech')
  assert.ok(tech && !tech.hasAttribute('open'), 'technical details fold closed by default')
  assert.match(tech.textContent, /Trace id\s*a{32}/)
  assert.match(tech.textContent, /Span id\s*c{16}/)
  assert.match(tech.textContent, /Recorded parent span id\s*r/)
  assert.ok(nodeEl(m, 'span:root'), 'the tree is still there beside it')
  assert.match(m.$('.jobd-title').textContent, /Return #9/, 'no navigation away from the Run')
  m.unmount()
})

test('12/30. the inspector never says verified, never prints null, and omits sections a node has nothing for', async () => {
  stub()
  const m = await mount(page())
  await m.settle()
  await openExecution(m)
  await m.click(nodeEl(m, 'span:t3').querySelector(':scope > .exec-row .exec-select'))
  const insp = m.$('.exec-inspector:not(.is-empty)')
  assert.match(insp.textContent, /Reported by worker\s*Call completed/)
  assert.doesNotMatch(insp.textContent, /verified|business|refund succeeded|null|undefined|None/i)
  assert.ok(!insp.textContent.includes('Model'), 'no Model section on a tool span with no usage')
  await m.click(nodeEl(m, 'span:m3').querySelector(':scope > .exec-row .exec-select'))
  const insp2 = m.$('.exec-inspector:not(.is-empty)')
  assert.match(insp2.textContent, /Total tokens\s*48/)
  assert.doesNotMatch(insp2.textContent, /Cost/, 'unknown cost: no Cost row, no $0')
  assert.ok(!insp2.textContent.includes('Tool'), 'no Tool section on a model span')
  await m.click(insp2.querySelector('.exec-insp-close'))
  assert.ok(m.$('.exec-inspector.is-empty'))
  m.unmount()
})

test('clicking a selected node again deselects it; selecting in chronology also opens the inspector', async () => {
  stub({ execution: flatBody() })
  const m = await mount(page())
  await m.settle()
  await openExecution(m)
  const row = m.$('[data-node-id="span:j2"]')
  await m.click(row.querySelector('.exec-select'))
  assert.ok(row.classList.contains('is-selected'))
  assert.match(m.$('.exec-inspector:not(.is-empty)').textContent, /job_waiting/)
  await m.click(row.querySelector('.exec-select'))
  assert.ok(!row.classList.contains('is-selected') && m.$('.exec-inspector.is-empty'))
  m.unmount()
})

test('collapse hides a branch and says how many children it hides, and that it contains an error', async () => {
  stub()
  const m = await mount(page())
  await m.settle()
  await openExecution(m)
  const root = nodeEl(m, 'span:root')
  assert.equal(root.getAttribute('aria-expanded'), 'true')
  await m.click(root.querySelector(':scope > .exec-row .exec-toggle'))
  assert.equal(root.getAttribute('aria-expanded'), 'false')
  assert.ok(!nodeEl(m, 'span:m1'))
  assert.match(rowText(root), /7 children hidden · contains an error/)
  await m.click(root.querySelector(':scope > .exec-row .exec-toggle'))
  assert.ok(nodeEl(m, 'span:m1'))
  m.unmount()
})

// --- run switching --------------------------------------------------------------------

test('25/31. Run A → Run B clears the execution and closes the inspector at once; B shows a skeleton until its own body arrives', async () => {
  const gates = {}
  stub({ execution: (id) => { if (id === 41) return execBody(41); gates[id] = deferred(); return gates[id].promise } })
  const m = await mount(page())
  await m.settle()
  await openExecution(m)
  await m.click(nodeEl(m, 'span:t2').querySelector(':scope > .exec-row .exec-select'))
  assert.ok(m.$('.exec-inspector:not(.is-empty)'))
  await m.render(page({ item: { id: 42 } }))
  await m.settle()
  assert.ok(!nodeEl(m, 'span:root'), 'A’s tree is gone immediately')
  assert.ok(!m.$('.exec-inspector:not(.is-empty)'), 'A’s inspector is closed')
  assert.ok(m.$('.exec .dash-skel'), 'B loads')
  gates[42].resolve(execBody(42, { nodes: execBody(42).nodes.slice(0, 2), roots: ['span:root'], chronology: ['span:root', 'span:m1'] }))
  await m.settle()
  assert.match(m.$('.jobd-title').textContent, /Return #10/)
  assert.equal(m.$$('.exec-node').length, 2)
  assert.ok(m.$('.exec-inspector.is-empty'), 'nothing preselected for B')
  m.unmount()
})

test('26. a late response for Run A cannot populate Run B', async () => {
  const gates = {}
  stub({ execution: (id) => { gates[id] = deferred(); return gates[id].promise } })
  const m = await mount(page())
  await m.settle()
  await openExecution(m)
  await m.render(page({ item: { id: 42 } }))
  await m.settle()
  gates[42].resolve(flatBody())
  await m.settle()
  assert.ok(m.$('.exec-chrono'))
  gates[41].resolve(execBody(41))
  await m.settle()
  assert.ok(m.$('.exec-chrono') && !nodeEl(m, 'span:root'), 'A’s tree never appears under B')
  m.unmount()
})

test('the Home desk panel has no Execution switch and never fetches execution', async () => {
  const calls = stub()
  const m = await mount(React.createElement(JobDetail, { variant: 'panel', item: { id: 41, title: 'x' }, onClose: () => {} }))
  await m.settle()
  assert.ok(!m.$('.jobd-views'))
  assert.deepEqual(calls.execution, [])
  assert.deepEqual(calls.graph, [], 'nor the Work Graph')
  m.unmount()
})
