// The Run page with Visibility, mounted: five rows between the record and its
// evidence, a state in words for each, its own loading/failure/retry, and no
// stale run's rows ever under another run's title.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { deferred, installDom, mount } from './mount.mjs'

installDom()

const React = await import('react')
const { api } = await import('../src/api.js')
const JobDetail = (await import('../src/JobDetail.jsx')).default

const NOW = Date.now()
const ago = (m) => new Date(NOW - m * 60000).toISOString()

const detailFor = (id, title) => ({
  id, title, status: 'done', holder: { kind: 'agent', name: 'refunds-agent' },
  whats_next: null, updated_at: ago(3), awaiting_handoff_event_id: null, workflow_id: 7, workflow_name: 'Refunds',
  whats_happening: null, process: null,
  timeline: [
    { at: ago(30), text: 'Started', actor: { kind: 'agent', name: 'refunds-agent' } },
    { at: ago(20), text: 'Waiting on Ada Lovelace', actor: { kind: 'human', name: 'Ada Lovelace' } },
    { at: ago(10), text: 'Finished', actor: { kind: 'agent', name: 'refunds-agent' } },
  ],
  provenance: { source: 'telemetry', suggestion_id: null },
})
const DETAILS = { 41: detailFor(41, 'Refund order #4471'), 42: detailFor(42, 'Refund order #4472') }
const RUNS = [
  { name: 'tool_call', agent: 'refunds-agent', service_name: 'refunds-agent', agent_id: 'main', at: ago(25), errored: false, duration_ms: 1400, cost_usd: 0.0042, tool: 'refund_customer', error: null },
]
const EVIDENCE = {
  item_id: 41, generated_at: new Date(NOW).toISOString(), spans_truncated: false,
  evidence: [{
    id: 'exec:grok-bot', item_id: 41, evidence_type: 'execution', observed_at: ago(30),
    source_type: 'agent', source_connector_id: 'grok-bot', source_label: 'refunds-agent:main',
    correlation_method: 'explicit_key', event_id: null, span_id: null, trace_id: null,
    external_object_id: null, external_event_id: null,
    details: { span_count: 3, error_count: 0, last_observed_at: ago(10), trace_ids: [], correlation_methods: ['explicit_key'] },
  }],
}

const dim = (id, state, reason, over = {}) => ({
  id, state, reason, evidence_count: state === 'unknown' ? 0 : 1, last_observed_at: state === 'unknown' ? null : ago(20),
  evidence_types: [], sources: [], correlation_methods: [], from_bounded_evidence: false, details: {}, ...over,
})
const coverageFor = (id, over = {}) => ({
  item_id: id, generated_at: new Date(NOW).toISOString(), evidence_bounded: false,
  dimensions: [
    dim('execution', 'observed', 'execution_evidence'),
    dim('actions', 'observed', 'action_reports'),
    dim('external_outcomes', 'observed', 'external_observations'),
    dim('handoffs', 'unknown', 'no_handoff_records'),
    dim('cost', 'observed', 'usage_cost_known', { details: { usage_spans: 2, cost_known_spans: 2, cost_covered_spans: 0, cost_unknown_spans: 0, amount_usd: 0.0042, basis: 'estimated' } }),
  ],
  ...over,
})
const COVERAGE = coverageFor(41)
const withCost = (state, reason, details) => coverageFor(41, {
  dimensions: COVERAGE.dimensions.map((d) => (d.id === 'cost' ? dim('cost', state, reason, { details }) : d)),
})

function stub({ coverage = COVERAGE, coverageFail = false, evidence = EVIDENCE, evidenceFail = false, runs = RUNS } = {}) {
  const calls = { item: [], coverage: [], evidence: [] }
  api.getWorkItem = async (id, opts = {}) => {
    calls.item.push([id, opts.include || null])
    const d = DETAILS[id] || DETAILS[41]
    return opts.include === 'runs' ? { ...d, runs } : d
  }
  api.getWorkItemEvidence = async (id) => {
    calls.evidence.push(id)
    if (typeof evidenceFail === 'function' ? evidenceFail() : evidenceFail) throw new Error('evidence down')
    return typeof evidence === 'function' ? evidence(id) : evidence
  }
  api.getWorkItemCoverage = async (id) => {
    calls.coverage.push(id)
    if (typeof coverageFail === 'function' ? coverageFail() : coverageFail) throw new Error('coverage down')
    return typeof coverage === 'function' ? coverage(id) : coverage
  }
  // The page also reads the Work Graph (its own section, its own tests in
  // jobDetailWorkGraph.test.mjs); keep it off the network here.
  api.getWorkItemGraph = async (id) => ({
    item_id: id, generated_at: new Date(NOW).toISOString(), steps: [], chronology: [],
    possession: { segments: [], current_holder: null }, lifecycle: [], bounded: false, span_limit: 2000, spans_read: 0, events_read: 0, summary: {},
  })
  return calls
}

const page = (over = {}) =>
  React.createElement(JobDetail, {
    variant: 'page', item: { id: 41 }, onClose: () => {}, onOpenAgent: () => {}, backLabel: '← Refunds', ...over,
  })

const rowsOf = (m) => m.$$('.jobd-visibility .jobd-vis-row')
const labelsOf = (m) => rowsOf(m).map((r) => r.querySelector('.jobd-vis-label').textContent)
const statesOf = (m) => rowsOf(m).map((r) => r.querySelector('.jobd-vis-state').textContent)
const rowFor = (m, id) => rowsOf(m).find((r) => r.dataset.dimension === id)

// --- placement and shape ---------------------------------------------------------

test('1. the page fetches coverage once, for the viewed item, and renders a Visibility section', async () => {
  const calls = stub()
  const m = await mount(page())
  await m.settle()
  assert.deepEqual(calls.coverage, [41])
  const sec = m.$('.jobd-visibility')
  assert.ok(sec, 'section present')
  assert.equal(sec.getAttribute('aria-label'), 'Visibility')
  assert.equal(sec.querySelector('h3').textContent, 'Visibility')
  m.unmount()
})

test('2. Visibility sits after What happened (and the demoted moves) and before Evidence — the record first, then what could be seen, then what was seen', async () => {
  stub()
  const m = await mount(page())
  await m.settle()
  const text = m.text()
  assert.ok(text.indexOf('Refund order #4471') < text.indexOf('Visibility'))
  assert.ok(text.indexOf('What happened') < text.indexOf('Visibility'))
  assert.ok(text.indexOf('How this job ran') < text.indexOf('Visibility'))
  assert.ok(text.indexOf('Visibility') < text.indexOf('Evidence'))
  const sections = m.$$('.jobd-section').map((s) => s.getAttribute('aria-label'))
  const vis = sections.indexOf('Visibility')
  assert.equal(sections[vis - 2], 'What happened')
  assert.equal(sections[vis - 1], 'How this job ran')
  assert.equal(sections[vis + 1], 'Evidence')
  m.unmount()
})

test('3. five rows, in the fixed order, with the agreed labels', async () => {
  stub()
  const m = await mount(page())
  await m.settle()
  assert.deepEqual(labelsOf(m), ['Execution', 'Actions', 'External outcomes', 'Handoffs', 'Cost'])
  m.unmount()
})

test('4. each row shows its state in words and one explanatory sentence', async () => {
  stub()
  const m = await mount(page())
  await m.settle()
  assert.deepEqual(statesOf(m), ['Observed', 'Observed', 'Observed', 'Unknown', 'Observed'])
  for (const r of rowsOf(m)) {
    const t = r.querySelector('.jobd-vis-text').textContent
    assert.equal((t.match(/[.!?]/g) || []).length, 1, `one sentence: ${t}`)
  }
  assert.match(rowFor(m, 'execution').textContent, /AI execution was observed for this run\./)
  assert.match(rowFor(m, 'actions').textContent, /Actions reported by the worker were observed\./)
  assert.match(rowFor(m, 'external_outcomes').textContent, /An external system reported state related to this run\./)
  assert.match(rowFor(m, 'handoffs').textContent, /Trovis can't determine handoff visibility from this record\./)
  m.unmount()
})

test('5. the section carries a short lead and nothing that reads as a verdict', async () => {
  stub()
  const m = await mount(page())
  await m.settle()
  const sec = m.$('.jobd-visibility')
  assert.match(sec.textContent, /Which parts of this run Trovis directly observed\./)
  assert.doesNotMatch(sec.textContent, /\d\s*%|percent|score|grade|verdict|verified|confiden|health|\d+\s*(of|\/)\s*\d+/i)
  m.unmount()
})

test('6. no counts, no amounts, no ratios in any row — the sentence is enough', async () => {
  stub({ coverage: withCost('partial', 'some_usage_cost_unknown', { usage_spans: 7, cost_known_spans: 3, cost_covered_spans: 1, cost_unknown_spans: 4, amount_usd: 0.31, basis: 'mixed' }) })
  const m = await mount(page())
  await m.settle()
  for (const r of rowsOf(m)) assert.doesNotMatch(r.textContent, /\d/, r.textContent)
  m.unmount()
})

test('7. no call to action inside Visibility: no buttons, no links, no "connect" or "recommend"', async () => {
  stub()
  const m = await mount(page())
  await m.settle()
  const sec = m.$('.jobd-visibility')
  assert.equal(sec.querySelectorAll('button, a').length, 0)
  assert.doesNotMatch(sec.textContent, /connect|recommend|should|set up|add a/i)
  m.unmount()
})

test('8. state is carried as a class name without traffic-light words', async () => {
  stub()
  const m = await mount(page())
  await m.settle()
  for (const r of rowsOf(m)) {
    const cls = [...r.classList].join(' ')
    assert.match(cls, /state-(observed|partial|not_observed|unknown)/)
    assert.doesNotMatch(cls, /ok|good|bad|warn|danger|error|success|green|red/i)
  }
  m.unmount()
})

// --- cost, by reason ---------------------------------------------------------------

test('9. cost partial → "Partially observed" and "some of the observed model usage"', async () => {
  stub({ coverage: withCost('partial', 'some_usage_cost_unknown', { usage_spans: 3, cost_known_spans: 1, cost_covered_spans: 0, cost_unknown_spans: 2, amount_usd: 0.001, basis: 'estimated' }) })
  const m = await mount(page())
  await m.settle()
  const cost = rowFor(m, 'cost')
  assert.equal(cost.querySelector('.jobd-vis-state').textContent, 'Partially observed')
  assert.match(cost.textContent, /Cost is recorded for some of the observed model usage\./)
  m.unmount()
})

test('10. cost not observed → "Not observed" and "cost wasn\'t recorded" — usage seen, price unknown', async () => {
  stub({ coverage: withCost('not_observed', 'usage_cost_unknown', { usage_spans: 2, cost_known_spans: 0, cost_covered_spans: 0, cost_unknown_spans: 2, amount_usd: null, basis: null }) })
  const m = await mount(page())
  await m.settle()
  const cost = rowFor(m, 'cost')
  assert.equal(cost.querySelector('.jobd-vis-state').textContent, 'Not observed')
  assert.match(cost.textContent, /Model usage was observed, but its cost wasn't recorded\./)
  assert.doesNotMatch(cost.textContent, /\$0|free|zero/i)
  m.unmount()
})

test('11. a run-level reported cost with no usage spans → Observed, "A run-level cost was reported."', async () => {
  stub({ coverage: withCost('observed', 'reported_cost', { usage_spans: 0, cost_known_spans: 0, cost_covered_spans: 0, cost_unknown_spans: 0, amount_usd: 0.05, basis: 'reported' }) })
  const m = await mount(page())
  await m.settle()
  const cost = rowFor(m, 'cost')
  assert.equal(cost.querySelector('.jobd-vis-state').textContent, 'Observed')
  assert.match(cost.textContent, /A run-level cost was reported\./)
  m.unmount()
})

test('12. no model usage observed → cost is Unknown, worded as "can\'t determine", never "$0" or "free"', async () => {
  stub({ coverage: withCost('unknown', 'no_model_usage_observed', { usage_spans: 0, cost_known_spans: 0, cost_covered_spans: 0, cost_unknown_spans: 0, amount_usd: null, basis: null }) })
  const m = await mount(page())
  await m.settle()
  const cost = rowFor(m, 'cost')
  assert.equal(cost.querySelector('.jobd-vis-state').textContent, 'Unknown')
  assert.match(cost.textContent, /Trovis can't determine cost visibility from this record\./)
  assert.doesNotMatch(cost.textContent, /\$|free|zero|no cost/i)
  m.unmount()
})

test('13. covered spans (subsumed in a reported total) read as recorded cost, never as priced individually', async () => {
  stub({ coverage: withCost('observed', 'usage_cost_known', { usage_spans: 2, cost_known_spans: 2, cost_covered_spans: 2, cost_unknown_spans: 0, amount_usd: 0.02, basis: 'reported' }) })
  const m = await mount(page())
  await m.settle()
  const cost = rowFor(m, 'cost')
  assert.equal(cost.querySelector('.jobd-vis-state').textContent, 'Observed')
  assert.match(cost.textContent, /Cost is recorded for the observed model usage\./)
  assert.doesNotMatch(cost.textContent, /priced|individually|per span|each span|covered/i)
  m.unmount()
})

test('14. "model calls" never appears in the section', async () => {
  stub({ coverage: withCost('partial', 'some_usage_cost_unknown', { usage_spans: 3, cost_known_spans: 1, cost_covered_spans: 0, cost_unknown_spans: 2, amount_usd: 0.001, basis: 'estimated' }) })
  const m = await mount(page())
  await m.settle()
  assert.doesNotMatch(m.$('.jobd-visibility').textContent, /model call/i)
  m.unmount()
})

// --- unknown is not missing -------------------------------------------------------

test('15. an all-Unknown response shows five Unknown rows, each saying the record cannot determine it', async () => {
  const all = coverageFor(41, {
    dimensions: [
      dim('execution', 'unknown', 'no_execution_evidence'), dim('actions', 'unknown', 'no_action_reports'),
      dim('external_outcomes', 'unknown', 'no_external_observations'), dim('handoffs', 'unknown', 'no_handoff_records'),
      dim('cost', 'unknown', 'no_model_usage_observed'),
    ],
  })
  stub({ coverage: all })
  const m = await mount(page())
  await m.settle()
  assert.deepEqual(statesOf(m), ['Unknown', 'Unknown', 'Unknown', 'Unknown', 'Unknown'])
  const sec = m.$('.jobd-visibility')
  assert.doesNotMatch(sec.textContent, /missing|none|no evidence|not connected|nothing happened|failed|couldn.t be loaded/i)
  assert.equal((sec.textContent.match(/can't determine/g) || []).length, 5)
  m.unmount()
})

test('16. a dimension the server omitted is absent, not shown as Unknown', async () => {
  stub({ coverage: coverageFor(41, { dimensions: COVERAGE.dimensions.filter((d) => d.id !== 'cost') }) })
  const m = await mount(page())
  await m.settle()
  assert.deepEqual(labelsOf(m), ['Execution', 'Actions', 'External outcomes', 'Handoffs'])
  assert.ok(!rowFor(m, 'cost'))
  m.unmount()
})

test('17. a response with no dimensions is an honest sentence — not five Unknown rows, not an error', async () => {
  stub({ coverage: coverageFor(41, { dimensions: [] }) })
  const m = await mount(page())
  await m.settle()
  const sec = m.$('.jobd-visibility')
  assert.equal(rowsOf(m).length, 0)
  assert.match(sec.textContent, /Visibility isn't available for this run\./)
  assert.doesNotMatch(sec.textContent, /Unknown|couldn.t be loaded/)
  m.unmount()
})

// --- bounded evidence ----------------------------------------------------------------

test('18. evidence_bounded shows one quiet note and changes no state label', async () => {
  stub({ coverage: coverageFor(41, { evidence_bounded: true, dimensions: COVERAGE.dimensions.map((d) => ({ ...d, from_bounded_evidence: true })) }) })
  const m = await mount(page())
  await m.settle()
  const note = m.$('.jobd-visibility .jobd-vis-note')
  assert.ok(note, 'bounded note present')
  assert.match(note.textContent, /bounded set of this run’s observations; later observations were not read\./)
  assert.doesNotMatch(note.textContent, /incomplete|missing|partial|gap/i)
  assert.deepEqual(statesOf(m), ['Observed', 'Observed', 'Observed', 'Unknown', 'Observed'])
  m.unmount()
})

test('19. no bounded note when the read was not bounded', async () => {
  stub()
  const m = await mount(page())
  await m.settle()
  assert.ok(!m.$('.jobd-visibility .jobd-vis-note'))
  assert.doesNotMatch(m.$('.jobd-visibility').textContent, /bounded/)
  m.unmount()
})

// --- loading, failure, retry: independent of Evidence -----------------------------

test('20. while coverage loads the section shows a skeleton and the record and Evidence are already usable', async () => {
  const gate = deferred()
  stub({ coverage: () => gate.promise })
  const m = await mount(page())
  await m.settle()
  assert.match(m.text(), /Refund order #4471/)
  assert.match(m.text(), /How this job ran/)
  assert.ok(m.$('.jobd-visibility .dash-skel'), 'visibility skeleton')
  assert.equal(rowsOf(m).length, 0)
  assert.doesNotMatch(m.$('.jobd-visibility').textContent, /Unknown/)
  assert.match(m.$('.jobd-evidence').textContent, /refunds-agent/, 'evidence already rendered')
  gate.resolve(COVERAGE)
  await m.settle()
  assert.ok(!m.$('.jobd-visibility .dash-skel'))
  assert.equal(rowsOf(m).length, 5)
  m.unmount()
})

test('21. a coverage failure is local: a Retry line, zero rows, never five Unknowns, and the page stays', async () => {
  stub({ coverageFail: true })
  const m = await mount(page())
  await m.settle()
  assert.match(m.text(), /Refund order #4471/)
  const sec = m.$('.jobd-visibility')
  assert.match(sec.textContent, /Visibility couldn.t be loaded\./)
  assert.equal(sec.getAttribute('aria-label'), 'Visibility')
  assert.ok(sec.querySelector('[role="alert"]'))
  assert.equal(rowsOf(m).length, 0)
  assert.doesNotMatch(sec.textContent, /Unknown|Observed|isn't available/)
  assert.ok(sec.querySelector('button'), 'Retry offered')
  assert.equal(sec.querySelector('button').textContent, 'Retry')
  // Evidence, an independent request, is unaffected.
  assert.match(m.$('.jobd-evidence').textContent, /refunds-agent/)
  assert.doesNotMatch(m.$('.jobd-evidence').textContent, /couldn.t be loaded/)
  m.unmount()
})

test('22. an evidence failure leaves Visibility fully rendered', async () => {
  stub({ evidenceFail: true })
  const m = await mount(page())
  await m.settle()
  assert.match(m.$('.jobd-evidence').textContent, /Evidence couldn.t be loaded\./)
  assert.equal(rowsOf(m).length, 5)
  assert.doesNotMatch(m.$('.jobd-visibility').textContent, /couldn.t be loaded/)
  m.unmount()
})

test('23. Retry re-requests coverage only, and renders the rows when it succeeds', async () => {
  let fail = true
  const calls = stub({ coverageFail: () => fail })
  const m = await mount(page())
  await m.settle()
  assert.deepEqual(calls.coverage, [41])
  const evidenceCalls = calls.evidence.length
  fail = false
  await m.click(m.$('.jobd-visibility button'))
  await m.settle()
  assert.deepEqual(calls.coverage, [41, 41])
  assert.equal(calls.evidence.length, evidenceCalls, 'evidence was not refetched')
  assert.equal(rowsOf(m).length, 5)
  assert.doesNotMatch(m.$('.jobd-visibility').textContent, /couldn.t be loaded/)
  m.unmount()
})

test('24. Retry shows the skeleton while the retry is in flight — never the old error and never Unknown rows', async () => {
  let gate = null
  const calls = stub({
    coverageFail: () => calls.coverage.length === 1,
    coverage: () => { gate = deferred(); return gate.promise },
  })
  const m = await mount(page())
  await m.settle()
  assert.match(m.$('.jobd-visibility').textContent, /couldn.t be loaded/)
  await m.click(m.$('.jobd-visibility button'))
  await m.settle()
  assert.ok(m.$('.jobd-visibility .dash-skel'))
  assert.doesNotMatch(m.$('.jobd-visibility').textContent, /couldn.t be loaded|Unknown/)
  gate.resolve(COVERAGE)
  await m.settle()
  assert.equal(rowsOf(m).length, 5)
  m.unmount()
})

test('25. when both requests fail there are two Retry lines and each retries only its own request', async () => {
  let covFail = true
  let evFail = true
  const calls = stub({ coverageFail: () => covFail, evidenceFail: () => evFail })
  const m = await mount(page())
  await m.settle()
  assert.match(m.$('.jobd-visibility').textContent, /Visibility couldn.t be loaded/)
  assert.match(m.$('.jobd-evidence').textContent, /Evidence couldn.t be loaded/)
  covFail = false
  await m.click(m.$('.jobd-visibility button'))
  await m.settle()
  assert.deepEqual(calls.coverage, [41, 41])
  assert.deepEqual(calls.evidence, [41])
  assert.equal(rowsOf(m).length, 5)
  assert.match(m.$('.jobd-evidence').textContent, /Evidence couldn.t be loaded/, 'evidence still failed until its own Retry')
  evFail = false
  await m.click(m.$('.jobd-evidence button'))
  await m.settle()
  assert.deepEqual(calls.coverage, [41, 41])
  assert.deepEqual(calls.evidence, [41, 41])
  assert.match(m.$('.jobd-evidence').textContent, /refunds-agent/)
  m.unmount()
})

test('26. an evidence failure never makes the coverage rows read Unknown, and vice versa', async () => {
  stub({ evidenceFail: true })
  const m = await mount(page())
  await m.settle()
  assert.deepEqual(statesOf(m), ['Observed', 'Observed', 'Observed', 'Unknown', 'Observed'])
  m.unmount()
  stub({ coverageFail: true })
  const m2 = await mount(page())
  await m2.settle()
  assert.match(m2.$('.jobd-evidence').textContent, /Execution observed/)
  m2.unmount()
})

// --- page-only -------------------------------------------------------------------------

test('27. the Home desk panel does not fetch coverage and shows no Visibility section', async () => {
  const calls = stub()
  const m = await mount(React.createElement(JobDetail, { variant: 'panel', item: { id: 41, title: 'x' }, onClose: () => {} }))
  await m.settle()
  assert.deepEqual(calls.coverage, [])
  assert.ok(!m.$('.jobd-visibility'))
  assert.doesNotMatch(m.text(), /Visibility/)
  m.unmount()
})

// --- run A → run B ----------------------------------------------------------------------

test('28. switching from Run A to Run B resets the section: B shows a skeleton, never A\'s rows, until B\'s own coverage arrives', async () => {
  const gates = {}
  const calls = stub({
    coverage: (id) => {
      if (id === 41) return coverageFor(41)
      gates[id] = deferred()
      return gates[id].promise
    },
  })
  const m = await mount(page())
  await m.settle()
  assert.deepEqual(statesOf(m), ['Observed', 'Observed', 'Observed', 'Unknown', 'Observed'])
  await m.render(page({ item: { id: 42 } }))
  await m.settle()
  assert.deepEqual(calls.coverage, [41, 42])
  assert.ok(m.$('.jobd-visibility .dash-skel'), 'B shows a skeleton while its coverage loads')
  assert.equal(rowsOf(m).length, 0, 'A rows never flash under B')
  gates[42].resolve(coverageFor(42, {
    dimensions: COVERAGE.dimensions.map((d) => (d.id === 'execution' ? dim('execution', 'unknown', 'no_execution_evidence') : d)),
  }))
  await m.settle()
  assert.match(m.text(), /Refund order #4472/)
  assert.deepEqual(statesOf(m), ['Unknown', 'Observed', 'Observed', 'Unknown', 'Observed'])
  m.unmount()
})

test('29. a late response for Run A, arriving after the switch to Run B, is ignored', async () => {
  const gates = {}
  stub({
    coverage: (id) => {
      gates[id] = deferred()
      return gates[id].promise
    },
  })
  const m = await mount(page())
  await m.settle()
  await m.render(page({ item: { id: 42 } }))
  await m.settle()
  gates[42].resolve(coverageFor(42, {
    dimensions: COVERAGE.dimensions.map((d) => (d.id === 'cost' ? dim('cost', 'not_observed', 'usage_cost_unknown') : d)),
  }))
  await m.settle()
  assert.deepEqual(statesOf(m), ['Observed', 'Observed', 'Observed', 'Unknown', 'Not observed'])
  gates[41].resolve(coverageFor(41))
  await m.settle()
  assert.deepEqual(statesOf(m), ['Observed', 'Observed', 'Observed', 'Unknown', 'Not observed'], 'A late A response does not overwrite B')
  m.unmount()
})

test('30. switching runs also resets Evidence, so Run A\'s sources never sit under Run B\'s title', async () => {
  const gates = {}
  stub({
    evidence: (id) => {
      if (id === 41) return EVIDENCE
      gates[id] = deferred()
      return gates[id].promise
    },
  })
  const m = await mount(page())
  await m.settle()
  assert.match(m.$('.jobd-evidence').textContent, /refunds-agent/)
  await m.render(page({ item: { id: 42 } }))
  await m.settle()
  assert.ok(m.$('.jobd-evidence .dash-skel'), 'evidence skeleton for B')
  assert.doesNotMatch(m.$('.jobd-evidence').textContent, /refunds-agent/)
  gates[42].resolve({ ...EVIDENCE, item_id: 42, evidence: [] })
  await m.settle()
  assert.match(m.$('.jobd-evidence').textContent, /No supporting evidence is available for this run\./)
  m.unmount()
})

test('31. unmounting mid-request leaves no rows and no error behind (abort is honoured)', async () => {
  const gate = deferred()
  stub({ coverage: () => gate.promise })
  const m = await mount(page())
  await m.settle()
  m.unmount()
  gate.resolve(COVERAGE)
  await new Promise((r) => setTimeout(r, 0))
  assert.equal(document.querySelectorAll('.jobd-visibility').length, 0)
})
