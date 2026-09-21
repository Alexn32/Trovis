// The Work Graph's pure helpers, the API helper, and where the graph may be
// read from: only the full Run page. No reconstruction lives in the client.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, readdirSync } from 'node:fs'
import {
  STEP_TYPES, STEP_TYPE_LABELS, actorDisplay, boundedNote, evidenceRecordFor, evidenceRowShown,
  holderLine, isEmptyGraph, normalizeGraph, possessionRows, stepContext, stepDetailRows,
  stepTechnicalRows, stepTimeLabels, supportLine,
} from '../src/workGraph.js'

const srcDir = new URL('../src/', import.meta.url)
const src = (f) => readFileSync(new URL(f, srcDir), 'utf8')
const strip = (s) => s.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')

const step = (over = {}) => ({
  id: 'work-step:event:10', type: 'handoff', at: '2026-09-18T10:03:14+00:00', label: 'Handed to a person',
  actor: { type: 'agent', label: 'returns-bot:main' }, system: null,
  details: { direction: 'to_human', handoff_id: 'h-1', reason: 'over limit', target_label: 'Sarah Chen', target_id: null },
  provenance: { event_id: 10, evidence_id: 'event:10', evidence_kind: 'handoff', execution_node_id: 'event:10',
    span_id: 'aaaaaaaaaaaaaaaa', trace_id: 'a'.repeat(32), correlation: 'explicit_key', source_type: 'agent',
    source_connector_id: 'grok-bot', external_object_id: null, external_event_id: null },
  ...over,
})

// --- shape ---------------------------------------------------------------------------

test('the vocabulary is the endpoint’s five types, and no label is a verdict', () => {
  assert.deepEqual([...STEP_TYPES], ['progress', 'handoff', 'wait', 'exception', 'completed'])
  for (const v of Object.values(STEP_TYPE_LABELS)) {
    assert.doesNotMatch(v, /success|succeed|fail|resolved|verified|done successfully/i, v)
  }
  assert.equal(STEP_TYPE_LABELS.completed, 'Record closed')
})

test('normalizeGraph keeps the endpoint’s steps in its order, never drops or re-types one, and reads no lifecycle', () => {
  const body = {
    steps: [step(), step({ id: 'work-step:event:11', type: 'mystery', label: 'Something new' }), 'junk', { type: 'wait' }],
    lifecycle: [{ id: 'event:12', type: 'handoff_completed' }],
    possession: { segments: [], current_holder: null }, bounded: true, span_limit: 2000,
  }
  const g = normalizeGraph(body)
  assert.deepEqual(g.steps.map((s) => s.id), ['work-step:event:10', 'work-step:event:11'])
  assert.equal(g.steps[1].type, 'other', 'an unknown type is a neutral step, not a guess at one of ours')
  assert.equal(g.steps[1].label, 'Something new')
  assert.equal(g.bounded, true)
  assert.equal(g.spanLimit, 2000)
  assert.ok(!('lifecycle' in g), 'lifecycle is not part of what the client arranges')
  assert.equal(isEmptyGraph({ steps: [] }), true)
  assert.equal(isEmptyGraph(null), true)
  assert.equal(isEmptyGraph(body), false)
})

test('actorDisplay: worker label minus :main, a person by the server’s name, a system by its label — nothing parsed from an id', () => {
  assert.deepEqual(actorDisplay({ type: 'agent', label: 'returns-bot:main' }), { kind: 'agent', label: 'returns-bot' })
  assert.deepEqual(actorDisplay({ type: 'agent', label: 'ops:triage' }), { kind: 'agent', label: 'ops:triage' })
  assert.deepEqual(actorDisplay({ type: 'human', label: 'a person' }), { kind: 'human', label: 'a person' })
  assert.deepEqual(actorDisplay({ type: 'human', label: null }), { kind: 'human', label: 'A person' })
  assert.deepEqual(actorDisplay({ type: 'system', label: 'Stripe' }), { kind: 'system', label: 'Stripe' })
  assert.equal(actorDisplay(null), null)
})

test('stepContext reads one recorded field per type and invents nothing', () => {
  assert.equal(stepContext(step()), 'Sarah Chen')
  assert.equal(stepContext(step({ details: { direction: 'to_agent', target_label: 'research-agent:main' } })), 'research-agent')
  assert.equal(stepContext(step({ details: { direction: 'sideways', target_label: null } })), null)
  assert.equal(stepContext(step({ type: 'wait', details: { waiting_on: 'payment processing', reason: 'x' } })), 'payment processing')
  assert.equal(stepContext(step({ type: 'wait', details: { waiting_on: null, reason: 'export queued' } })), 'export queued')
  assert.equal(stepContext(step({ type: 'exception', details: { reason: 'Your card was declined' } })), 'Your card was declined')
  assert.equal(stepContext(step({ type: 'exception', details: { handoff_id: 'h-2', reason: null } })), null, 'a decline with no recorded reason has no context line')
  assert.equal(stepContext(step({ type: 'completed', details: { reason: 'completed_by_agent', outcome: 'record_closed' } })), null)
})

test('detail rows are recorded context only; technical rows carry the ids; null references are simply absent', () => {
  const rows = stepDetailRows(step()).map((r) => [r.label, r.value])
  assert.deepEqual(rows, [['To', 'Sarah Chen'], ['Reason', 'over limit']])
  const closed = stepDetailRows(step({ type: 'completed', details: { reason: 'abandoned', detail: null, abandoned: true, outcome: 'record_closed' } }))
  assert.deepEqual(closed.map((r) => [r.label, r.value]), [['Recorded as', 'abandoned'], ['Closure', 'abandoned']])
  const tech = stepTechnicalRows(step())
  assert.ok(tech.find((r) => r.label === 'Evidence record' && r.value === 'event:10'))
  assert.ok(tech.find((r) => r.label === 'Execution node' && r.value === 'event:10'))
  assert.ok(tech.find((r) => r.label === 'Connector' && r.value === 'Grok Bot'))
  assert.ok(tech.find((r) => r.label === 'Link' && r.value === 'Directly linked to this run'))
  const stall = stepTechnicalRows(step({ type: 'exception', provenance: { event_id: 14, evidence_id: null, evidence_kind: null, execution_node_id: 'event:14', correlation: 'direct', source_type: 'system' } }))
  assert.ok(!stall.find((r) => r.label === 'Evidence record'), 'no Evidence row for a null evidence_id')
  assert.ok(stall.find((r) => r.label === 'Execution node' && r.value === 'event:14'))
  assert.doesNotMatch(JSON.stringify(stall), /null|undefined/)
})

test('time labels: clock alone on one day, day + clock when the record spans days', () => {
  const one = stepTimeLabels([step({ id: 'a', at: '2026-09-18T10:03:00Z' }), step({ id: 'b', at: '2026-09-18T10:08:00Z' })])
  assert.match(one.get('a'), /^\d{1,2}:\d{2} (AM|PM)$/)
  const two = stepTimeLabels([step({ id: 'a', at: '2026-09-18T10:03:00Z' }), step({ id: 'b', at: '2026-09-20T10:08:00Z' })])
  assert.match(two.get('a'), /^[A-Z][a-z]{2} \d{1,2} · \d{1,2}:\d{2} (AM|PM)$/)
  assert.equal(stepTimeLabels([step({ id: 'a', at: null })]).get('a'), null)
})

// --- possession ------------------------------------------------------------------------

test('holderLine reads possession.current_holder only — null when the endpoint names none, whatever the steps say', () => {
  const held = holderLine({ segments: [], current_holder: { holder_type: 'system', holder: 'Stripe', start: 'x', end: null, waiting: true, touches: [], event_count: 0 } })
  assert.deepEqual(held, { label: 'Stripe', kind: 'system', waiting: true, text: 'Held by Stripe · waiting' })
  const agent = holderLine({ current_holder: { holder_type: 'agent', holder: 'returns-bot:main', waiting: false } })
  assert.equal(agent.text, 'Held by returns-bot')
  assert.equal(holderLine({ segments: [{ holder_type: 'human', holder: 'Sarah' }], current_holder: null }), null)
  assert.equal(holderLine(null), null)
  assert.equal(holderLine({ current_holder: { holder_type: 'agent', holder: '' } }), null)
})

test('possessionRows is the endpoint’s segments in its order, flags as given', () => {
  const rows = possessionRows({ segments: [
    { holder_type: 'agent', holder: 'returns-bot:main', start: 's1', end: 'e1', waiting: false },
    { holder_type: 'human', holder: 'Sarah Chen', start: 'e1', end: 'e2', waiting: true },
    { holder_type: 'system', holder: 'Stripe', start: 'e2', end: null, waiting: true },
  ] })
  assert.deepEqual(rows.map((r) => [r.label, r.kind, r.waiting, r.current]), [
    ['returns-bot', 'agent', false, false], ['Sarah Chen', 'human', true, false], ['Stripe', 'system', true, true],
  ])
  assert.deepEqual(possessionRows(null), [])
})

// --- notes and links -------------------------------------------------------------------

test('bounded is one quiet sentence about supporting detail, never “incomplete” steps', () => {
  assert.equal(boundedNote({ bounded: false }), null)
  const note = boundedNote({ bounded: true, span_limit: 3 })
  assert.equal(note, 'Some supporting execution detail is outside this read.')
  assert.doesNotMatch(note, /incomplete|truncated|missing steps/i)
})

test('evidence links are exact ids: a handoff record exists but draws no row; a stall has no record at all', () => {
  const evidence = { evidence: [
    { id: 'event:10', evidence_type: 'handoff', source_type: 'agent', source_connector_id: 'grok-bot', source_label: 'returns-bot:main', details: { event: 'handoff_initiated' } },
    { id: 'event:12', evidence_type: 'external_state', source_type: 'system', source_connector_id: 'stripe', source_label: 'Stripe', details: { provider_event_type: 'payment_intent.processing', effect: 'wait' } },
  ] }
  assert.equal(evidenceRecordFor(evidence, 'event:10').id, 'event:10')
  assert.equal(evidenceRecordFor(evidence, 'event:14'), null)
  assert.equal(evidenceRecordFor(evidence, null), null)
  assert.equal(evidenceRowShown(evidence, 'event:12'), true)
  assert.equal(evidenceRowShown(evidence, 'event:10'), false, 'handoffs are counted in sources, not drawn as rows')
  assert.equal(evidenceRowShown(evidence, null), false)
  assert.equal(supportLine(evidenceRecordFor(evidence, 'event:10')), 'a handoff record — Reported by returns-bot · Grok Bot')
  assert.equal(supportLine(evidenceRecordFor(evidence, 'event:12')), "an external system's own record — Observed from Stripe")
  assert.equal(supportLine(null), null)
})

// --- boundaries ------------------------------------------------------------------------

test('api.getWorkItemGraph reads /work/items/:id/graph with an abort signal, like evidence, coverage and execution', () => {
  const api = strip(src('api.js'))
  assert.match(api, /getWorkItemGraph:\s*\(id, \{ signal = undefined \} = \{\}\)/)
  assert.ok(api.includes('/work/items/${encodeURIComponent(id)}/graph'))
  assert.match(api, /getWorkItemGraph[\s\S]{0,200}timeoutMs: WORK_TIMEOUT_MS/)
})

test('26. only the Run page reads the Work Graph — never Home, the Work table, the job roll-up, Fleet or the Agent page', () => {
  const readers = new Set(['api.js', 'JobDetail.jsx'])
  const renderers = new Set(['JobDetail.jsx', 'WorkGraphView.jsx', 'workGraph.js'])
  const files = readdirSync(srcDir).filter((f) => /\.(jsx?|mjs)$/.test(f))
  for (const f of files) {
    const code = strip(src(f))
    if (!readers.has(f)) assert.doesNotMatch(code, /getWorkItemGraph/, `${f} reads the graph`)
    if (!renderers.has(f)) assert.doesNotMatch(code, /WorkGraph|workGraph|work-step|jobd-work/, `${f} renders the graph`)
  }
  for (const f of ['HomeView.jsx', 'WorkTab.jsx', 'Fleet.jsx', 'AgentDetail.jsx', 'App.jsx']) {
    assert.doesNotMatch(strip(src(f)), /getWorkItemGraph|WorkGraphView|\/graph/, f)
  }
  const page = strip(src('JobDetail.jsx'))
  assert.match(page, /api\s*\.getWorkItemGraph\(item\.id, \{ signal \}\)/)
  assert.match(page, /if \(!isPage\) return undefined\s+setGraph\(null\)\s+setGraphErr\(null\)\s+setSelectedStep\(null\)/,
    'page-only, and body + selected step reset before every load')
  assert.match(page, /\}, \[isPage, item\.id, graphReload\]\)/, 'keyed on the item, not on the view')
})

test('30. no reconstruction in the client: no lifecycle, no tool-name or provider phrase tables, no holder from a step', () => {
  for (const f of ['workGraph.js', 'WorkGraphView.jsx']) {
    const code = strip(src(f))
    assert.doesNotMatch(code, /lifecycle/, `${f} reads lifecycle`)
    assert.doesNotMatch(code, /refund issued|refund requested|charge\.refunded|payment_intent\.succeeded/i, `${f} maps provider events to business phrases`)
    assert.doesNotMatch(code, /getWorkItem|fetch\(/, `${f} fetches`)
    assert.doesNotMatch(code, /steps\[steps\.length - 1\]\.actor|\.actor\b[^\n]*holder|lastStep/, `${f} infers a holder from a step`)
  }
  const page = strip(src('JobDetail.jsx'))
  assert.match(page, /holderLine\(graph\?\.possession\)/, 'the header reads possession, not the steps')
  assert.doesNotMatch(page, /graph\?\.steps\[/, 'the page never reaches into the steps for a holder')
})
