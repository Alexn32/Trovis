// The job page states averages, and it must never state one as a fact.
//
// This page sits one click from a page of recorded events, and the two look
// alike if nobody stops them. So the diagram carries its provenance line
// always, and the health section grades nothing it was not given a number to
// grade against. Every assertion here defends one of those two.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import {
  computedFrom, costLabel, healthRows, jobPath, jobStats, recentRuns, settingsRows,
} from '../src/jobPage.js'
import { kindPath } from '../src/board.js'

const NOW = Date.parse('2026-03-10T12:00:00Z')
const ago = (s) => new Date(NOW - s * 1000).toISOString()
const src = (f) => readFileSync(new URL(`../src/${f}`, import.meta.url), 'utf8')

function job(o = {}) {
  return {
    id: 1, name: 'Refunds', current_version: 2,
    started_runs: 28, closed_runs: 28, window_days: 14,
    median_close_s: null, cost_per_run: null, last_run_at: null,
    intervention_pct: null, failure_pct: null,
    expected_per_day_min: null, expected_per_day_max: null,
    expected_close_s: null, expected_intervention_pct: null,
    expected_failure_pct: null, has_expectation: false,
    owning_service_name: null, owning_agent_id: null, approval_routing: null,
    stall_threshold_s: null, match_hints: [], stations: [],
    ...o,
  }
}
function run(o = {}) {
  return {
    id: o.id ?? 1, title: o.title ?? `Refund #${o.id ?? 1}`,
    status: o.status ?? 'moving', updated_at: o.updated_at ?? ago(60),
    holder: o.holder ?? { kind: 'agent', name: 'refunds-agent' },
    ...o,
  }
}

// --- the provenance line ----------------------------------------------------

const shape = [
  run({ id: 1, holder: { kind: 'agent', name: 'a' } }),
  run({ id: 2, holder: { kind: 'human', name: 'Alex' }, status: 'waiting_on_you' }),
  run({ id: 3, holder: { kind: 'agent', name: 'a' }, status: 'done' }),
]

test('the diagram says what it was computed from — BOTH bases', () => {
  // The shape is drawn from the runs on the page, every state. The
  // percentage on the human branch is measured over CLOSED runs only. One
  // number claiming to cover both is exactly the lie this line prevents.
  assert.equal(computedFrom(job({ closed_runs: 28, window_days: 14 }), shape),
               'Shape from 3 recent runs; intervention over 28 closed runs, last 14 days.')
  assert.equal(computedFrom(job({ closed_runs: 1, window_days: 14 }), shape.slice(0, 1)),
               'Shape from 1 recent run; intervention over 1 closed run, last 14 days.')
})

test('the stated shape count is the count the diagram actually drew from', () => {
  // Not the length of the array handed in: a row with no id, an internal
  // title, or a holder kind the spine has no node for contributes nothing,
  // and claiming it did would overstate the basis.
  const padded = [
    ...shape,
    run({ id: null, holder: { kind: 'agent', name: 'a' } }),
    run({ id: 9, title: 'loop_4f2a1b', holder: { kind: 'agent', name: 'a' } }),
    run({ id: 10, holder: { kind: 'unassigned', name: '' }, status: 'stuck' }),
  ]
  assert.equal(jobPath(job(), padded).length, jobPath(job(), shape).length)
  assert.match(computedFrom(job(), padded), /^Shape from 3 recent runs/)
})

test('nothing closed means no rate clause, but the shape still accounts for itself', () => {
  // A rate over zero runs does not exist. The shape does, and it is drawn
  // from open runs, so the line keeps its first half and drops the second.
  assert.equal(computedFrom(job({ closed_runs: 0 }), shape), 'Shape from 3 recent runs.')
  assert.equal(computedFrom(job({ closed_runs: null }), shape), 'Shape from 3 recent runs.')
  assert.equal(computedFrom(job({ window_days: 0 }), shape), 'Shape from 3 recent runs.')
})

test('no shape means no diagram, and so no line at all', () => {
  assert.equal(computedFrom(job({ closed_runs: 28 }), []), null)
  assert.equal(computedFrom(job({ closed_runs: 28 }), null), null)
  assert.equal(computedFrom(null, null), null)
})

// --- the path ---------------------------------------------------------------

test('the divergence is the human branch, carrying the intervention rate', () => {
  const path = jobPath(job({ intervention_pct: 18 }), [
    run({ id: 1, holder: { kind: 'agent', name: 'a' } }),
    run({ id: 2, holder: { kind: 'human', name: 'Alex' }, status: 'waiting_on_you' }),
  ])
  const human = path.find((n) => n.kind === 'human')
  assert.equal(human.isDivergence, true)
  assert.equal(human.pct, 18)
  assert.ok(path.filter((n) => n.isDivergence).length === 1, 'exactly one divergence')
})

test('the divergence percentage and the health metric are ONE number', () => {
  // Two code paths for the same figure is how a page ends up disagreeing
  // with itself. Both read intervention_pct; neither recomputes it.
  const j = job({ intervention_pct: 18, expected_intervention_pct: 10 })
  const runs = [
    run({ id: 1, holder: { kind: 'agent', name: 'a' } }),
    run({ id: 2, holder: { kind: 'human', name: 'Alex' }, status: 'waiting_on_you' }),
  ]
  const fromPath = jobPath(j, runs).find((n) => n.isDivergence).pct
  const fromHealth = healthRows(j).find((r) => r.key === 'intervention').observed
  assert.equal(`${fromPath}%`, fromHealth)
  // ...and the source is a single field, asserted at the source.
  const page = src('jobPage.js')
  // The OBSERVED field, not expected_intervention_pct which contains it.
  assert.equal((page.match(/job\?\.intervention_pct\b/g) || []).length, 2,
               'read twice — once by the path, once by health — computed never')
  assert.doesNotMatch(page, /intervention_runs|closed_runs\s*\/|100\s*\*/,
                      'the rate is never recomputed on the client')
})

test('a MEASURED zero is not a branch, but it is still printed', () => {
  // Caught in the browser: people were holding open runs, so the human node
  // was in the shape, while every closed run had gone through without one.
  // The node was being flagged as "the divergence" and coloured, above the
  // words "0% of runs" — a highlighted claim of a thing that did not happen.
  const path = jobPath(job({ intervention_pct: 0 }), shape)
  const human = path.find((n) => n.kind === 'human')
  assert.equal(human.isDivergence, false, 'nil frequency is not a branch')
  assert.equal(human.pct, 0, 'the measurement is still owed to the reader')
  assert.equal(path.filter((n) => n.isDivergence).length, 0)
  // ...and an unmeasurable one claims nothing either way.
  const none = jobPath(job({ intervention_pct: null }), shape)
  const h2 = none.find((n) => n.kind === 'human')
  assert.equal(h2.isDivergence, false)
  assert.equal(h2.pct, null)
})

test('the diagram carries no per-node cost or duration', () => {
  // That detail belongs to a run page's steps. Stacking costs, durations and
  // percentages on a spine turns this into the raw view we replace.
  const path = jobPath(job({ intervention_pct: 5 }), [
    run({ id: 1, holder: { kind: 'agent', name: 'a' } }),
    run({ id: 2, holder: { kind: 'human', name: 'Alex' } }),
  ])
  for (const n of path) {
    assert.deepEqual(Object.keys(n).sort(), ['isDivergence', 'kind', 'label', 'pct'])
  }
})

test('one kind of holder is still not a path', () => {
  assert.equal(jobPath(job(), [run({ id: 1 }), run({ id: 2 })]), null)
  assert.equal(jobPath(job(), []), null)
})

// --- the stat row -----------------------------------------------------------

test('a stat the record cannot supply is absent, never zero', () => {
  const stats = jobStats(job(), { now: NOW })
  assert.deepEqual(stats.map((s) => s.key), ['last', 'cadence', 'close', 'cost'])
  assert.equal(stats.find((s) => s.key === 'close').value, null,
               'a job that never closed a run has no median close time')
  assert.equal(stats.find((s) => s.key === 'cost').value, null)
  assert.equal(stats.find((s) => s.key === 'last').value, null)
})

test('stats read the way a person says them', () => {
  const stats = jobStats(job({
    last_run_at: ago(4 * 3600 + 120), started_runs: 140, window_days: 14,
    median_close_s: 258, cost_per_run: 0.0042,
  }), { now: NOW })
  assert.deepEqual(stats.map((s) => s.value),
                   ['4h 02m ago', '10/day', '4m 18s', '$0.0042'])
})

test('cadence counts runs STARTED, not runs finished', () => {
  // Caught in the browser: a job that ran seven times and closed two read as
  // "0.1/day, expected 8–12" and was flagged red. That is not a slow job, it
  // is the wrong question — a declared per-day expectation asks how often the
  // job runs. The two counts must be able to disagree without the page
  // reading the wrong one.
  const j = job({ started_runs: 140, closed_runs: 28, window_days: 14 })
  assert.equal(jobStats(j, { now: NOW }).find((s) => s.key === 'cadence').value, '10/day')
  assert.equal(healthRows(j).find((r) => r.key === 'volume').observed, '10/day')
  // A job mid-flight — nothing closed yet — still has a cadence.
  const open = job({ started_runs: 28, closed_runs: 0, window_days: 14 })
  assert.equal(healthRows(open).find((r) => r.key === 'volume').observed, '2/day')
})

// --- health -----------------------------------------------------------------

test('all four metrics report, each naming its own number', () => {
  const rows = healthRows(job({
    started_runs: 140, window_days: 14, median_close_s: 258,
    intervention_pct: 18, failure_pct: 3.2,
    expected_per_day_min: 8, expected_per_day_max: 12,
    expected_close_s: 360, expected_intervention_pct: 10,
    expected_failure_pct: 2,
  }))
  assert.deepEqual(rows.map((r) => [r.label, r.observed, r.expected, r.over]), [
    ['Volume', '10/day', 'expected 8–12', false],
    ['Close time', '4m 18s', 'expected under 6m 00s', false],
    ['Intervention', '18%', 'expected under 10%', true],
    ['Failure rate', '3.2%', 'expected under 2%', true],
  ])
})

test('an undeclared metric shows the observation with an EMPTY comparison', () => {
  // Not a verdict, not a dash pretending to be one. The number is real; the
  // judgement is not ours to make without a declared ceiling.
  const rows = healthRows(job({
    started_runs: 140, window_days: 14, median_close_s: 258,
    intervention_pct: 18, failure_pct: 3.2,
  }))
  for (const r of rows) {
    assert.equal(r.expected, null, `${r.label} has nothing to compare against`)
    assert.equal(r.over, false, `${r.label} cannot be a breach with no ceiling`)
    assert.ok(r.observed, `${r.label} still reports what was observed`)
  }
})

test('a metric with no observation and no ceiling claims nothing at all', () => {
  const rows = healthRows(job({ started_runs: 0, closed_runs: 0 }))
  const iv = rows.find((r) => r.key === 'intervention')
  assert.equal(iv.observed, null)
  assert.equal(iv.expected, null)
  assert.equal(iv.over, false)
})

test('volume breaches in BOTH directions', () => {
  const under = healthRows(job({ started_runs: 14, window_days: 14, expected_per_day_min: 8 }))
  assert.equal(under.find((r) => r.key === 'volume').over, true, 'below the floor is a breach')
  const over = healthRows(job({ started_runs: 280, window_days: 14, expected_per_day_max: 12 }))
  assert.equal(over.find((r) => r.key === 'volume').over, true, 'above the ceiling is too')
})

test('a ceiling exactly met is not a breach', () => {
  const rows = healthRows(job({ intervention_pct: 10, expected_intervention_pct: 10 }))
  assert.equal(rows.find((r) => r.key === 'intervention').over, false)
})

// --- settings ---------------------------------------------------------------

test('settings list only what is actually declared', () => {
  // A list padded with "—" for every field the schema never got is a list of
  // promises. Undeclared fields are omitted.
  assert.deepEqual(settingsRows(job()).map((r) => r.label), ['Version'])
})

test('the owning agent carries a ROUTE, never just a label', () => {
  const [owner] = settingsRows(job({
    owning_service_name: 'refunds-agent', owning_agent_id: 'researcher',
  }))
  assert.equal(owner.label, 'Owning agent')
  assert.deepEqual(owner.route, ['refunds-agent', 'researcher'])
  const [dflt] = settingsRows(job({ owning_service_name: 'refunds-agent' }))
  assert.deepEqual(dflt.route, ['refunds-agent', 'main'])
})

test('what recognises a run is not called a trigger', () => {
  // Nothing here fires the job. It claims runs that already ran, and calling
  // that a trigger would overstate what the record does.
  const rows = settingsRows(job({
    match_hints: [{ field: 'service_name', op: 'equals', value: 'refunds-agent' }],
  }))
  const hint = rows.find((r) => r.label === 'Recognised by')
  assert.equal(hint.value, 'service_name equals refunds-agent')
  assert.ok(!rows.some((r) => /trigger/i.test(r.label)))
})

test('tools come from the declared stations, de-duplicated', () => {
  const rows = settingsRows(job({
    stations: [{ tools: 'stripe, ledger' }, { tools: 'stripe' }, { tools: '' }],
  }))
  assert.equal(rows.find((r) => r.label === 'Tools').value, 'stripe, ledger')
})

test('thresholds and routing appear only when set', () => {
  const rows = settingsRows(job({ stall_threshold_s: 7200, approval_routing: 'alex@t.com' }))
  const by = Object.fromEntries(rows.map((r) => [r.label, r.value]))
  assert.equal(by['Stall threshold'], '2h 00m')
  assert.equal(by['Approval routing'], 'alex@t.com')
})

// --- recent runs ------------------------------------------------------------

test('recent runs are every state, newest first', () => {
  // A repeated failure mode is only visible as a cluster if the failures sit
  // in the same list as the successes.
  const rows = recentRuns([
    run({ id: 1, status: 'done', updated_at: ago(3600) }),
    run({ id: 2, status: 'stuck', updated_at: ago(60) }),
    run({ id: 3, status: 'moving', updated_at: ago(7200) }),
  ])
  assert.deepEqual(rows.map((r) => r.id), [2, 1, 3])
  assert.deepEqual([...new Set(rows.map((r) => r.status))].sort(),
                   ['done', 'moving', 'stuck'])
})

test('recent runs are bounded', () => {
  const many = Array.from({ length: 40 }, (_, i) =>
    run({ id: i + 1, updated_at: ago(i * 60) }))
  assert.equal(recentRuns(many).length, 12)
  assert.equal(recentRuns(many, { limit: 3 }).length, 3)
  assert.deepEqual(recentRuns(null), [])
})

// --- house rules ------------------------------------------------------------

test('cost is real or absent', () => {
  assert.equal(costLabel(0), null)
  assert.equal(costLabel(null), null)
  assert.equal(costLabel(-1), null)
  assert.equal(costLabel(0.0042), '$0.0042')
  assert.equal(costLabel(4.1), '$4.10')
})

test('the page stays on lean reads', () => {
  const work = src('WorkTab.jsx')
  assert.doesNotMatch(work, /getWorkBoard|getWorkSummary/)
})

// --- the spine kindPath still supplies (folded in from the kind-page suite)

test('the path is the hands this job passes through', () => {
  const path = kindPath([
    run({ id: 1, holder: { kind: 'agent', name: 'refunds-agent' } }),
    run({ id: 2, status: 'stuck', holder: { kind: 'tool', name: 'stripe' } }),
    run({ id: 3, status: 'waiting_on_you', holder: { kind: 'human', name: 'Alex' } }),
  ])
  assert.deepEqual(path.map((n) => n.kind), ['agent', 'tool', 'human'])
  assert.equal(path[1].label, 'stripe', 'a tool worth naming is named')
})

test('the path omits Done until this job has actually finished something', () => {
  const live = kindPath([
    run({ id: 1, holder: { kind: 'agent', name: 'a' } }),
    run({ id: 2, holder: { kind: 'human', name: 'Alex' } }),
  ])
  assert.ok(!live.some((n) => n.kind === 'done'), 'no ending the record has not seen')
  const done = kindPath([
    run({ id: 1, holder: { kind: 'agent', name: 'a' } }),
    run({ id: 2, holder: { kind: 'human', name: 'Alex' } }),
    run({ id: 3, status: 'done' }),
  ])
  assert.equal(done[done.length - 1].kind, 'done')
})

test('the path is built from list rows — never a detail fetch per row', () => {
  const page = src('jobPage.js')
  assert.match(page, /kindPath\(runs\)/)
  assert.doesNotMatch(page, /getWorkItem\(|api\./, 'a pure module does not fetch')
})

// --- the page itself --------------------------------------------------------

const work = src('WorkTab.jsx').replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')
const pageSrc = work.slice(work.indexOf('function JobPage'), work.indexOf('function WorkHome'))

test('the job page loads THIS job by id, not a slice of the board page', () => {
  // The board's page is ordered by recency across every job, so slicing it
  // gives whichever of this job's runs survived the crowd. And matching by
  // id rather than name means renaming a job does not empty its own page.
  assert.match(work, /workflowId: kindWorkflowId === null \? 'none' : kindWorkflowId/)
  assert.doesNotMatch(pageSrc, /matchesKind/, 'no name matching on the job page')
  assert.match(work, /api\s*\.getWorkflow\(route\.job/)
})

test('a job row opens the job page, and All work returns to the board', () => {
  assert.match(work, /onOpenJob=\{\(id\) => onRoute\(\{ job: Number\(id\), run: null \}\)\}/)
  assert.match(work, /onBack=\{\(\) => onRoute\(\{ job: null, run: null \}\)\}/)
  assert.match(pageSrc, /← All work/)
})

test('a run on this page opens the run page, carrying its OWN job', () => {
  // it.workflow_id, falling back to the page's job — so a run opened from
  // here keeps the breadcrumb it actually belongs to rather than inheriting
  // whichever page happened to launch it.
  assert.match(work, /onOpenItem=\{\(it\) => onRoute\(\{ job: it\.workflow_id \?\? route\.job, run: it\.id \}\)\}/)
})

test('a filter carried in still applies here and stays dismissible', () => {
  assert.match(pageSrc, /filter \? mine\.filter\(\(r\) => matchesWorkFilter\(r, filter\)\) : mine/)
  assert.match(pageSrc, /onClick=\{onClearFilter\}/)
  assert.match(pageSrc, /Nothing matches these filters/)
  assert.doesNotMatch(pageSrc, /Connect an agent/)
})

test('loading is not the same as an empty record', () => {
  // "No runs on the record yet" is a claim about the record. While the
  // request is in flight the page has not earned it.
  assert.match(pageSrc, /runsLoading && mine\.length === 0/)
})

test('a stuck run under Recent runs is honest — it carries its state', () => {
  // The old page put stuck runs under "Past runs", which claimed the run
  // had ended. "Recent runs" claims only recency, and every row shows its
  // own status pill.
  assert.match(work, /<h2 className="dash-caps">Recent runs<\/h2>/)
  assert.doesNotMatch(work, /Past runs/)
  assert.match(work, /workItemStatusLabel\(r\.status\)/)
})

test('the provenance line is rendered from the runs the diagram drew, not the job alone', () => {
  // Passing only the job let the line report closed_runs while the diagram
  // was drawn from every run on the page — a stated basis that was not the
  // basis. The call has to carry both.
  assert.match(pageSrc, /computedFrom\(job, mine\)/)
  assert.match(work, /\{provenance && <p className="jobp-computed">\{provenance\}<\/p>\}/)
})

test('the branch percentage renders on the MEASUREMENT, not on the highlight', () => {
  // Gating the chip on isDivergence meant a measured 0% vanished from the
  // page the moment it stopped being a branch — the reader lost the number
  // rather than losing the colour. Browser-caught; the helper alone could
  // not see it, because the helper was right.
  assert.match(work, /\{n\.pct !== null && <span className="kind-node-pct">/)
  assert.doesNotMatch(work, /n\.isDivergence && n\.pct/)
})

test('no second Ask on the job page', () => {
  assert.doesNotMatch(pageSrc, /openAsk|AskPill|home-ask/)
})

test('no jargon on the job page', () => {
  const FORBIDDEN = /\b(loops?|workloops?|possession|segments?|stations?|handoffs?)\b/i
  for (const m of pageSrc.matchAll(/>([^<>{}]{3,})</g)) {
    assert.ok(!FORBIDDEN.test(m[1]), `job page ships jargon: ${JSON.stringify(m[1])}`)
  }
  for (const s of ['How this job usually runs', 'Recent runs', 'Health', 'Settings']) {
    assert.ok(!FORBIDDEN.test(s), s)
  }
})
