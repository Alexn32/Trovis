// Work Coverage in a manager's words: five dimensions, a state each, one
// sentence each — and Unknown is not missing, a bound is not a gap, five
// rows are not a score.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import {
  DIMENSION_LABELS,
  DIMENSION_ORDER,
  STATE_LABELS,
  boundedNote,
  dimensionRow,
  visibilityRows,
} from '../src/coverage.js'

const src = (f) => readFileSync(new URL(`../src/${f}`, import.meta.url), 'utf8')
const strip = (s) => s.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')

const dim = (id, state, reason, over = {}) => ({
  id, state, reason, evidence_count: 0, last_observed_at: null, evidence_types: [],
  sources: [], correlation_methods: [], from_bounded_evidence: false, details: {}, ...over,
})
const FULL = {
  item_id: 41, generated_at: '2026-09-16T10:06:00+00:00', evidence_bounded: false,
  dimensions: [
    dim('execution', 'observed', 'execution_evidence'),
    dim('actions', 'observed', 'action_reports'),
    dim('external_outcomes', 'unknown', 'no_external_observations'),
    dim('handoffs', 'observed', 'handoff_records'),
    dim('cost', 'partial', 'some_usage_cost_unknown', { details: { usage_spans: 3, cost_known_spans: 1, cost_covered_spans: 0, cost_unknown_spans: 2, amount_usd: 0.004, basis: 'estimated' } }),
  ],
}
const everyText = () => {
  const out = []
  for (const id of DIMENSION_ORDER) {
    for (const state of Object.keys(STATE_LABELS)) out.push(dimensionRow(dim(id, state, null)).text)
  }
  for (const reason of ['usage_cost_known', 'reported_cost', 'some_usage_cost_unknown', 'usage_cost_unknown', 'no_model_usage_observed']) {
    out.push(dimensionRow(dim('cost', 'observed', reason)).text)
  }
  out.push(boundedNote({ evidence_bounded: true }))
  return out
}

// --- order and vocabulary ----------------------------------------------------

test('1. the five dimensions, in the API order, and nothing else', () => {
  assert.deepEqual([...DIMENSION_ORDER], ['execution', 'actions', 'external_outcomes', 'handoffs', 'cost'])
  assert.deepEqual(Object.keys(DIMENSION_LABELS).sort(), [...DIMENSION_ORDER].sort())
})

test('2. dimension labels are plain words a manager would use', () => {
  assert.equal(DIMENSION_LABELS.execution, 'Execution')
  assert.equal(DIMENSION_LABELS.actions, 'Actions')
  assert.equal(DIMENSION_LABELS.external_outcomes, 'External outcomes')
  assert.equal(DIMENSION_LABELS.handoffs, 'Handoffs')
  assert.equal(DIMENSION_LABELS.cost, 'Cost')
})

test('3. the four states map to exactly the four agreed labels', () => {
  assert.deepEqual(STATE_LABELS, {
    observed: 'Observed', partial: 'Partially observed', not_observed: 'Not observed', unknown: 'Unknown',
  })
})

test('4. an unrecognised or absent state falls to Unknown — never to Observed, never to a fifth word', () => {
  assert.equal(dimensionRow(dim('execution', 'verified', null)).stateLabel, 'Unknown')
  assert.equal(dimensionRow(dim('execution', undefined, null)).state, 'unknown')
  assert.equal(dimensionRow({ id: 'handoffs' }).stateLabel, 'Unknown')
})

// --- per-dimension copy --------------------------------------------------------

test('5. execution: observed says execution was observed; unknown says the record cannot determine it', () => {
  assert.equal(dimensionRow(dim('execution', 'observed', 'execution_evidence')).text, 'AI execution was observed for this run.')
  assert.equal(dimensionRow(dim('execution', 'unknown', 'no_execution_evidence')).text, "Trovis can't determine execution visibility from this record.")
})

test('6. actions: observed keeps "reported by the worker" — a report, not a proof', () => {
  const t = dimensionRow(dim('actions', 'observed', 'action_reports')).text
  assert.equal(t, 'Actions reported by the worker were observed.')
  assert.doesNotMatch(t, /succeed|complete|verified/i)
  assert.equal(dimensionRow(dim('actions', 'unknown', 'no_action_reports')).text, "Trovis can't determine action visibility from this record.")
})

test('7. external outcomes: observed says an external system REPORTED state — not that the outcome was confirmed', () => {
  const t = dimensionRow(dim('external_outcomes', 'observed', 'external_observations')).text
  assert.equal(t, 'An external system reported state related to this run.')
  assert.doesNotMatch(t, /confirm|verif|succeed/i)
  assert.equal(dimensionRow(dim('external_outcomes', 'unknown', 'no_external_observations')).text, "Trovis can't determine external outcome visibility from this record.")
})

test('8. handoffs: observed says responsibility changed; unknown does not say nothing changed hands', () => {
  assert.equal(dimensionRow(dim('handoffs', 'observed', 'handoff_records')).text, 'Changes of responsibility were observed.')
  const u = dimensionRow(dim('handoffs', 'unknown', 'no_handoff_records')).text
  assert.equal(u, "Trovis can't determine handoff visibility from this record.")
  assert.doesNotMatch(u, /no handoff|nothing|never/i)
})

test('9. cost is worded by the backend reason, which carries the denominator fact', () => {
  assert.equal(dimensionRow(dim('cost', 'observed', 'usage_cost_known')).text, 'Cost is recorded for the observed model usage.')
  assert.equal(dimensionRow(dim('cost', 'observed', 'reported_cost')).text, 'A run-level cost was reported.')
  assert.equal(dimensionRow(dim('cost', 'partial', 'some_usage_cost_unknown')).text, 'Cost is recorded for some of the observed model usage.')
  assert.equal(dimensionRow(dim('cost', 'not_observed', 'usage_cost_unknown')).text, "Model usage was observed, but its cost wasn't recorded.")
  assert.equal(dimensionRow(dim('cost', 'unknown', 'no_model_usage_observed')).text, "Trovis can't determine cost visibility from this record.")
})

test('10. cost with an unfamiliar reason still reads by state, and never invents a number', () => {
  assert.equal(dimensionRow(dim('cost', 'observed', 'future_reason')).text, 'Cost is recorded for the observed model usage.')
  assert.equal(dimensionRow(dim('cost', 'partial', null)).text, 'Cost is recorded for some of the observed model usage.')
  assert.equal(dimensionRow(dim('cost', 'not_observed', null)).text, "Model usage was observed, but its cost wasn't recorded.")
  assert.equal(dimensionRow(dim('cost', 'unknown', null)).text, "Trovis can't determine cost visibility from this record.")
})

test('11. the cost sentence never quotes counts, an amount or a ratio — those live in Evidence', () => {
  const rows = [
    dimensionRow(dim('cost', 'partial', 'some_usage_cost_unknown', { details: { usage_spans: 3, cost_known_spans: 1, cost_unknown_spans: 2, amount_usd: 0.004 } })),
    dimensionRow(dim('cost', 'observed', 'usage_cost_known', { details: { usage_spans: 4, cost_known_spans: 4, cost_covered_spans: 4, amount_usd: 1.25 } })),
  ]
  for (const r of rows) {
    assert.doesNotMatch(r.text, /\d/)
    assert.doesNotMatch(r.text, /\$|%|of \d/)
  }
})

test('12. covered spans are never described as priced individually', () => {
  const r = dimensionRow(dim('cost', 'observed', 'usage_cost_known', { details: { usage_spans: 2, cost_known_spans: 2, cost_covered_spans: 2, cost_unknown_spans: 0, amount_usd: 0.02, basis: 'reported' } }))
  assert.equal(r.stateLabel, 'Observed')
  assert.doesNotMatch(r.text, /priced|individually|each span|per span|covered/i)
})

test('13. "model calls" is not a phrase this section uses — the record knows usage spans', () => {
  for (const t of everyText()) assert.doesNotMatch(t, /model call/i)
  assert.doesNotMatch(strip(src('coverage.js')), /model call/i)
})

// --- rows from a response ------------------------------------------------------

test('14. visibilityRows returns the five rows in display order whatever order the response used', () => {
  const shuffled = { ...FULL, dimensions: [...FULL.dimensions].reverse() }
  assert.deepEqual(visibilityRows(shuffled).map((r) => r.id), [...DIMENSION_ORDER])
  assert.deepEqual(visibilityRows(FULL).map((r) => r.stateLabel), ['Observed', 'Observed', 'Unknown', 'Observed', 'Partially observed'])
})

test('15. a dimension the response lacks is absent, not invented as Unknown', () => {
  const three = { ...FULL, dimensions: FULL.dimensions.filter((d) => d.id !== 'handoffs' && d.id !== 'cost') }
  assert.deepEqual(visibilityRows(three).map((r) => r.id), ['execution', 'actions', 'external_outcomes'])
  assert.deepEqual(visibilityRows(null), [])
  assert.deepEqual(visibilityRows({}), [])
  assert.deepEqual(visibilityRows({ dimensions: 'nope' }), [])
})

test('16. a dimension id the UI does not know is dropped rather than shown with made-up words', () => {
  const extra = { ...FULL, dimensions: [...FULL.dimensions, dim('quality', 'observed', 'x')] }
  assert.deepEqual(visibilityRows(extra).map((r) => r.id), [...DIMENSION_ORDER])
})

test('17. each row carries exactly a label, a state label and one sentence', () => {
  for (const r of visibilityRows(FULL)) {
    assert.ok(r.label && r.stateLabel && r.text)
    assert.equal((r.text.match(/[.!?]/g) || []).length, 1, `one sentence: ${r.text}`)
    assert.ok(Object.values(STATE_LABELS).includes(r.stateLabel))
  }
})

// --- bounded evidence ------------------------------------------------------------

test('18. the bounded note appears only when the server says the read was bounded', () => {
  assert.equal(boundedNote({ evidence_bounded: false }), null)
  assert.equal(boundedNote({}), null)
  assert.equal(boundedNote(null), null)
  assert.equal(boundedNote({ evidence_bounded: true }), 'Based on a bounded set of this run’s observations; later observations were not read.')
})

test('19. a bound changes no state and no sentence — it is a note about the read, not a gap', () => {
  const bounded = { ...FULL, evidence_bounded: true, dimensions: FULL.dimensions.map((d) => ({ ...d, from_bounded_evidence: true })) }
  assert.deepEqual(visibilityRows(bounded), visibilityRows(FULL))
  assert.doesNotMatch(boundedNote(bounded), /missing|incomplete|partial|gap|unknown/i)
})

// --- what the words never say ------------------------------------------------------

test('20. no verdicts: no verified, score, grade, confidence, health, percentage or recommendation anywhere in the copy', () => {
  const banned = /verified|verify|score|grade|confiden|health|\d+\s*%|percent|recommend|should|coverage score|complete visibility|fully visible/i
  for (const t of everyText()) assert.doesNotMatch(t, banned, t)
})

test('21. unknown is never worded as missing, none, failed or not connected', () => {
  for (const id of DIMENSION_ORDER) {
    const t = dimensionRow(dim(id, 'unknown', null)).text
    assert.match(t, /can't determine/)
    assert.doesNotMatch(t, /missing|none|no evidence|not connected|fail|absent|lack/i)
  }
})

test('22. coverage.js is a pure module: no React, no DOM, no fetch', () => {
  const s = strip(src('coverage.js'))
  assert.doesNotMatch(s, /from 'react'|document\.|window\.|fetch\(|api\./)
})

// --- where it is read ----------------------------------------------------------------

test('23. the Run page reads coverage for the viewed item with an abort signal, and nowhere else does', () => {
  const page = strip(src('JobDetail.jsx'))
  assert.match(page, /api\s*\.getWorkItemCoverage\(item\.id, \{ signal \}\)/)
  assert.match(page, /from '\.\/coverage\.js'/)
  assert.match(page, /if \(!isPage\) return undefined\s+setCoverage\(null\)/, 'page-only, and reset before each load')
  assert.doesNotMatch(src('HomeView.jsx') + src('WorkTab.jsx'), /getWorkItemCoverage/, 'Work home never fetches coverage')
})

test('24. the Visibility section renders exactly the rows the helper gives it and offers Retry on failure', () => {
  const page = strip(src('JobDetail.jsx'))
  assert.match(page, /visibilityRows\(body\)/)
  assert.match(page, /Visibility couldn&apos;t be loaded\./)
  // The section is its own request: its own state, error and reload counter.
  assert.match(page, /\[coverage, setCoverage\]/)
  assert.match(page, /\[coverageErr, setCoverageErr\]/)
  assert.match(page, /\[isPage, item\.id, coverageReload\]/)
})
