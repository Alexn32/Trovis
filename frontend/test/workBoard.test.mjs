// The board is about JOBS, and it never grades without a number.
//
// Two failure modes these tests exist to prevent. The first is the board
// giving every run equal weight, which buries the two that need a person
// under ten that do not — and makes the job that stopped running today
// completely invisible, since it has no runs to draw. The second is a verdict
// with nothing behind it: "Healthy ✓" is an adjective unless a declared
// number produced it.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import {
  COLUMNS, UNMATCHED, ageLabel, boardTotals, columnOf, durationLabel,
  firstOverCeiling, groupByJob, healthBadge, needsCard, observedPerDay,
  pinLoudVerdict, quietLine, rowJobLine, runCardLead, tableJobMeta,
} from '../src/workBoard.js'
import { buildPath, parsePath } from '../src/route.js'

const NOW = Date.parse('2026-03-10T12:00:00Z')
const ago = (s) => new Date(NOW - s * 1000).toISOString()

function run(o = {}) {
  return {
    id: o.id ?? 1,
    title: o.title ?? `Approve refund #${o.id ?? 1}`,
    status: o.status ?? 'moving',
    whats_next: o.whats_next ?? 'In progress',
    updated_at: o.updated_at ?? ago(60),
    workflow_id: o.workflow_id ?? 1,
    ...o,
  }
}
function job(o = {}) {
  return {
    id: o.id ?? 1, name: o.name ?? 'Refunds',
    has_expectation: false, started_runs: 0, closed_runs: 0, window_days: 14,
    intervention_pct: null, failure_pct: null, median_close_s: null,
    expected_per_day_min: null, expected_intervention_pct: null,
    expected_failure_pct: null, expected_close_s: null, last_run_at: null,
    ...o,
  }
}

// --- routing ----------------------------------------------------------------

test('the three levels are real URLs, and they round-trip', () => {
  assert.deepEqual(parsePath('/work'), { tab: 'work', job: null, run: null })
  assert.deepEqual(parsePath('/work/jobs/12'), { tab: 'work', job: 12, run: null })
  assert.deepEqual(parsePath('/work/runs/4471'), { tab: 'work', job: null, run: 4471 })
  for (const p of ['/', '/fleet', '/org', '/work', '/work/jobs/12', '/work/runs/4471']) {
    assert.equal(buildPath(parsePath(p)), p, p)
  }
  // /team was the pane Org replaced. An old bookmark still lands somewhere
  // real; it just normalises to the URL the tab actually has now.
  assert.equal(parsePath('/team').tab, 'org')
  assert.equal(buildPath({ tab: 'org' }), '/org')
})

test('a junk id is not an id', () => {
  // Better the board than a fetch for /work/jobs/NaN.
  for (const bad of ['abc', '12.5', '-3', '0', ' 7 ', '1e3', '']) {
    assert.deepEqual(parsePath(`/work/jobs/${bad}`), { tab: 'work', job: null, run: null },
                     `"${bad}" is not an id`)
  }
  assert.deepEqual(parsePath('/work/runs/'), { tab: 'work', job: null, run: null })
  assert.deepEqual(parsePath('/nonsense'), { tab: 'dashboard', job: null, run: null })
})

// --- columns ----------------------------------------------------------------

test('gone quiet is a reason inside Stuck, not a fifth column', () => {
  // Two columns that both mean "not moving" make the reader classify before
  // they can read.
  assert.equal(columnOf('stuck'), 'stuck')
  assert.equal(columnOf('waiting_on_you'), 'waiting')
  assert.equal(columnOf('waiting_on_other'), 'waiting')
  assert.equal(columnOf('moving'), 'working')
  assert.equal(columnOf('done'), 'done')
  assert.equal(columnOf('quiet'), null, 'no state invents its own column')
})

// --- which runs earn a card -------------------------------------------------

test('only exceptions get a card; routine work is a count', () => {
  // A job with twelve runs needs the two exceptional ones, not two arbitrary
  // normal ones per column.
  assert.equal(needsCard(run({ status: 'stuck' }), { now: NOW }), true)
  assert.equal(needsCard(run({ status: 'moving' }), { now: NOW }), false)
  assert.equal(needsCard(run({ status: 'done' }), { now: NOW }), false)
})

test('a wait earns a card only once it is old', () => {
  const fresh = run({ status: 'waiting_on_you', updated_at: ago(600) })
  const stale = run({ status: 'waiting_on_you', updated_at: ago(31 * 3600) })
  assert.equal(needsCard(fresh, { now: NOW }), false)
  assert.equal(needsCard(stale, { now: NOW }), true)
})

// --- card copy --------------------------------------------------------------

test('the problem leads and the run supports it', () => {
  const { lead, sub } = runCardLead(
    run({ id: 4468, title: 'Refund #4468', status: 'stuck',
          whats_next: 'Payments tool timed out', updated_at: ago(4 * 3600 + 120) }),
    { now: NOW })
  assert.equal(lead, 'Payments tool timed out')
  assert.match(sub, /Refund #4468/)
  assert.match(sub, /4h 02m/)
})

test('with no recorded reason the run leads — a vague problem line is worse than none', () => {
  // "Needs attention" restates the column header. Leading with it would make
  // an undiagnosed run look diagnosed.
  for (const next of ['Needs attention', 'In progress', 'Waiting on you', '']) {
    const { lead } = runCardLead(
      run({ title: 'Refund #99', status: 'stuck', whats_next: next }), { now: NOW })
    assert.equal(lead, 'Refund #99', `${JSON.stringify(next)} must not lead`)
  }
})

test('a run with no human title still gets an identity', () => {
  const { lead } = runCardLead(
    run({ id: 77, title: null, status: 'stuck', whats_next: '' }), { now: NOW })
  assert.equal(lead, 'Run 77')
})

test('a named party IS a reason', () => {
  assert.equal(runCardLead(run({ status: 'waiting_on_other',
    whats_next: 'Waiting on Alex' }), { now: NOW }).lead, 'Waiting on Alex')
  assert.equal(runCardLead(run({ status: 'stuck',
    whats_next: 'Waiting on stripe' }), { now: NOW }).lead, 'Waiting on stripe')
})

// --- grouping ---------------------------------------------------------------

test('a job that ran nothing today still gets a row', () => {
  // The whole reason this page groups: work that stopped happening is
  // invisible on a board of live runs.
  const rows = groupByJob([job({ id: 1 }), job({ id: 2, name: 'Onboarding' })], [
    run({ id: 1, workflow_id: 1 }),
  ], { now: NOW })
  assert.deepEqual(rows.map((r) => r.name).sort(), ['Onboarding', 'Refunds'])
  const quiet = rows.find((r) => r.name === 'Onboarding')
  assert.equal(quiet.total, 0)
})

test('runs with no job land in Unmatched, always last', () => {
  const rows = groupByJob([job({ id: 1 })], [
    run({ id: 1, workflow_id: 1, status: 'stuck' }),
    run({ id: 2, workflow_id: null }),
    run({ id: 3, workflow_id: 999 }),  // a job the list does not carry
  ], { now: NOW })
  const last = rows[rows.length - 1]
  assert.equal(last.name, UNMATCHED)
  assert.equal(last.total, 2, 'both the null and the unknown id land here')
  assert.equal(rows.filter((r) => r.isUnmatched).length, 1)
})

test('Unmatched sinks even when it is the loudest row', () => {
  // Rows otherwise sort by how much needs attention, so a big unmatched pile
  // would lead the page. It is not a job and must never outrank one.
  const noisy = groupByJob([job({ id: 1 })], [
    run({ id: 1, workflow_id: 1, status: 'moving' }),
    run({ id: 2, workflow_id: null, status: 'stuck' }),
    run({ id: 3, workflow_id: null, status: 'stuck' }),
    run({ id: 4, workflow_id: null, status: 'stuck' }),
  ], { now: NOW })
  assert.equal(noisy[0].name, 'Refunds')
  assert.equal(noisy[noisy.length - 1].name, UNMATCHED)
  assert.ok(noisy[noisy.length - 1].exceptions > noisy[0].exceptions,
            'and it sinks despite carrying more exceptions')
})

test('counts cover every run; cards cover only the exceptional ones', () => {
  const rows = groupByJob([job({ id: 1 })], [
    ...Array.from({ length: 8 }, (_, i) => run({ id: i + 1, status: 'moving' })),
    run({ id: 20, status: 'stuck' }),
    run({ id: 21, status: 'waiting_on_you', updated_at: ago(31 * 3600) }),
    run({ id: 22, status: 'waiting_on_you', updated_at: ago(60) }),
    run({ id: 23, status: 'done' }),
  ], { now: NOW })
  const r = rows[0]
  assert.deepEqual(r.counts, { working: 8, waiting: 2, stuck: 1, done: 1 })
  assert.equal(r.columns.working.length, 0, 'eight normal runs draw no cards')
  assert.equal(r.columns.stuck.length, 1)
  assert.equal(r.columns.waiting.length, 1, 'the fresh wait is a count, not a card')
  assert.equal(r.exceptions, 2)
})

test('the oldest exception is first in its cell', () => {
  const rows = groupByJob([job({ id: 1 })], [
    run({ id: 1, status: 'stuck', updated_at: ago(3600) }),
    run({ id: 2, status: 'stuck', updated_at: ago(9 * 3600) }),
  ], { now: NOW })
  assert.deepEqual(rows[0].columns.stuck.map((r) => r.id), [2, 1])
})

test('header counts come from the same rows the board draws', () => {
  // Honesty rule 5: two numbers on one screen disagreeing is the failure
  // this product sells against. So the header is derived, never re-queried.
  const rows = groupByJob([job({ id: 1 }), job({ id: 2 })], [
    run({ id: 1, workflow_id: 1, status: 'stuck' }),
    run({ id: 2, workflow_id: 2, status: 'moving' }),
    run({ id: 3, workflow_id: null, status: 'moving' }),
  ], { now: NOW })
  const t = boardTotals(rows)
  assert.equal(t.jobs, 2, 'Unmatched is not a job')
  assert.equal(t.stuck, 1)
  assert.equal(t.working, 2, 'the unmatched run is still counted')
})

// --- the health badge -------------------------------------------------------

test('one badge, and it names its number', () => {
  const rows = groupByJob([job({ id: 1 })], [
    run({ id: 1, status: 'stuck' }), run({ id: 2, status: 'stuck' }),
    ...Array.from({ length: 10 }, (_, i) => run({ id: 10 + i, status: 'moving' })),
  ], { now: NOW })
  assert.deepEqual(healthBadge(rows[0], { now: NOW }),
                   { tone: 'error', label: 'Failing 2 of 12' })
})

test('stuck outranks an old wait — highest severity wins, once', () => {
  const rows = groupByJob([job({ id: 1 })], [
    run({ id: 1, status: 'stuck' }),
    run({ id: 2, status: 'waiting_on_you', updated_at: ago(31 * 3600) }),
  ], { now: NOW })
  assert.equal(healthBadge(rows[0], { now: NOW }).label, 'Failing 1 of 2')
})

test('an old wait is named with its age', () => {
  const rows = groupByJob([job({ id: 1 })], [
    run({ id: 2, status: 'waiting_on_other', updated_at: ago(31 * 3600) }),
  ], { now: NOW })
  assert.deepEqual(healthBadge(rows[0], { now: NOW }),
                   { tone: 'warning', label: '1 waiting 31h' })
})

test('no declared expectation means NO verdict, only the observed number', () => {
  // This is the rule the whole page rests on. Without a ceiling there is no
  // such thing as too quiet or too expensive.
  const rows = groupByJob([job({ id: 1, started_runs: 28, window_days: 14 })],
                          [run({ id: 1, status: 'moving' })], { now: NOW })
  const badge = healthBadge(rows[0], { now: NOW })
  assert.equal(badge.tone, 'none')
  assert.equal(badge.label, '2/day, no expectation set')
  assert.notEqual(badge.label, 'Healthy')
})

test('a job with an expectation and nothing wrong is Healthy', () => {
  const rows = groupByJob([job({
    id: 1, has_expectation: true, started_runs: 140, window_days: 14,
    expected_per_day_min: 8, expected_per_day_max: 12,
    intervention_pct: 4, expected_intervention_pct: 10,
    last_run_at: ago(600),
  })], [run({ id: 1, status: 'moving' })], { now: NOW })
  assert.deepEqual(healthBadge(rows[0], { now: NOW }), { tone: 'ok', label: 'Healthy' })
})

test('a job that declared a cadence and never ran is NOT healthy', () => {
  // The emptiest possible result against a stated expectation. It read as a
  // green tick because every observed number was null, so no ceiling could
  // be exceeded — a verdict arrived at by absence.
  const rows = groupByJob([job({
    id: 1, has_expectation: true, expected_per_day_min: 1, last_run_at: null,
  })], [], { now: NOW })
  assert.deepEqual(healthBadge(rows[0], { now: NOW }),
                   { tone: 'warning', label: 'Never run, expected 1/day' })
})

test('below the declared floor is a finding even when it ran a minute ago', () => {
  // "It ran recently" does not answer "it is running at a tenth of the rate
  // you asked for".
  const rows = groupByJob([job({
    id: 1, has_expectation: true, started_runs: 2, window_days: 14,
    expected_per_day_min: 8, expected_per_day_max: 12, last_run_at: ago(600),
  })], [run({ id: 1, status: 'moving' })], { now: NOW })
  assert.deepEqual(healthBadge(rows[0], { now: NOW }),
                   { tone: 'warning', label: '0.1/day, expected 8–12' })
})

test('quiet only fires against a declared cadence', () => {
  const base = {
    id: 1, started_runs: 2, window_days: 14, last_run_at: ago(3 * 86400),
  }
  const undeclared = groupByJob([job(base)], [], { now: NOW })[0]
  assert.equal(healthBadge(undeclared, { now: NOW }).tone, 'none',
               'no cadence declared, so nothing is "too quiet"')
  const declared = groupByJob(
    [job({ ...base, has_expectation: true, expected_per_day_min: 8 })], [], { now: NOW })[0]
  assert.deepEqual(healthBadge(declared, { now: NOW }),
                   { tone: 'warning', label: 'Quiet 3 days' })
})

test('an off-expectation metric names both numbers', () => {
  const rows = groupByJob([job({
    id: 1, has_expectation: true, started_runs: 140, window_days: 14,
    intervention_pct: 18, expected_intervention_pct: 10, last_run_at: ago(60),
  })], [run({ id: 1, status: 'moving' })], { now: NOW })
  assert.deepEqual(healthBadge(rows[0], { now: NOW }),
                   { tone: 'warning', label: 'Intervention 18%, expected under 10%' })
})

test('a ceiling that is met is not a finding', () => {
  assert.equal(firstOverCeiling(job({ intervention_pct: 10, expected_intervention_pct: 10 })), null)
  assert.equal(firstOverCeiling(job({ intervention_pct: null, expected_intervention_pct: 10 })), null)
  assert.equal(firstOverCeiling(job({ intervention_pct: 18, expected_intervention_pct: null })), null,
               'an observed number with no ceiling is not a breach')
})

test('close time over its ceiling reads in time, not seconds', () => {
  assert.equal(
    firstOverCeiling(job({ median_close_s: 3960, expected_close_s: 360 })),
    'Close time 1h 06m, expected under 6m 00s')
})

// --- the quiet line ---------------------------------------------------------

test('a healthy job collapses to one line instead of an empty grid', () => {
  const rows = groupByJob([job({ id: 1, median_close_s: 258 })], [
    run({ id: 1, status: 'moving' }), run({ id: 2, status: 'moving' }),
    ...Array.from({ length: 7 }, (_, i) => run({ id: 10 + i, status: 'done' })),
  ], { now: NOW })
  assert.equal(quietLine(rows[0]),
               'Nothing needs attention. 2 moving, 7 closed today, median close 4m 18s.')
})

test('a job with nothing at all says so plainly', () => {
  const rows = groupByJob([job({ id: 1 })], [], { now: NOW })
  assert.equal(quietLine(rows[0]), 'Nothing running.')
})

// --- shared formatting ------------------------------------------------------

test('observed per day is null when there is nothing to divide', () => {
  assert.equal(observedPerDay(job({ started_runs: 0, window_days: 14 })), 0)
  assert.equal(observedPerDay(job({ started_runs: null, window_days: 14 })), null)
  assert.equal(observedPerDay(job({ started_runs: 5, window_days: 0 })), null)
})

test('cadence is runs STARTED — a job mid-flight is not a job standing still', () => {
  // Reading closed runs here graded a job that ran 140 times and finished 28
  // as running at a fifth of its declared rate. The two counts have to be
  // able to disagree without the rate following the wrong one.
  assert.equal(observedPerDay(job({ started_runs: 140, closed_runs: 28, window_days: 14 })), 10)
  assert.equal(observedPerDay(job({ started_runs: 28, closed_runs: 0, window_days: 14 })), 2)
})

test('durations and ages read the way a person says them', () => {
  assert.equal(durationLabel(45), '45s')
  assert.equal(durationLabel(258), '4m 18s')
  assert.equal(durationLabel(3960), '1h 06m')
  assert.equal(durationLabel(null), null)
  assert.equal(ageLabel(ago(30), NOW), '30s')
  assert.equal(ageLabel(ago(45 * 60), NOW), '45m')
  assert.equal(ageLabel(ago(4 * 3600 + 120), NOW), '4h 02m')
  assert.equal(ageLabel(ago(31 * 3600), NOW), '31h')
  assert.equal(ageLabel(ago(5 * 86400), NOW), '5d')
  assert.equal(ageLabel(null, NOW), null, 'no timestamp is no age, not "0s"')
})

// --- what the board must not do (ported from the kind-map suite it replaced)

test('a loud job verdict rides under the Task title, not as its own column', () => {
  const stuck = groupByJob([job({ id: 1 })], [
    run({ id: 1, status: 'stuck' }), run({ id: 2, status: 'moving' }),
  ], { now: NOW })[0]
  assert.deepEqual(rowJobLine(stuck, { now: NOW }), {
    name: 'Refunds',
    badge: { tone: 'error', label: 'Failing 1 of 2' },
  })
  const calm = groupByJob([job({
    id: 1, has_expectation: true, started_runs: 140, window_days: 14,
    expected_per_day_min: 8, last_run_at: ago(600),
  })], [run({ id: 1, status: 'moving' })], { now: NOW })[0]
  assert.deepEqual(rowJobLine(calm, { now: NOW }), { name: 'Refunds', badge: null })
  assert.equal(rowJobLine(null), null)
})

test('a failing job alarms one sibling, not every calm row in the cluster', () => {
  // Clustering rule: hottest attention row (stuck → waiting_on_you →
  // waiting_on_other, oldest first). Moving / done siblings keep the name.
  const jobs = [job({ id: 1 })]
  const items = [
    run({ id: 10, status: 'stuck', updated_at: ago(9 * 3600) }),
    run({ id: 11, status: 'stuck', updated_at: ago(3600) }),
    run({ id: 12, status: 'moving' }),
    run({ id: 13, status: 'done' }),
  ]
  const grouped = groupByJob(jobs, items, { now: NOW })[0]
  const pinned = pinLoudVerdict(grouped, items, { now: NOW })
  assert.equal(pinned.badge.label, 'Failing 2 of 4')
  assert.equal(pinned.badgeOnId, 10, 'oldest stuck row carries the alarm')

  const meta = tableJobMeta(jobs, items, items, { now: NOW })
  assert.deepEqual(meta.get(10), {
    name: 'Refunds',
    badge: { tone: 'error', label: 'Failing 2 of 4' },
  })
  assert.deepEqual(meta.get(11), { name: 'Refunds', badge: null })
  assert.deepEqual(meta.get(12), { name: 'Refunds', badge: null })
  assert.deepEqual(meta.get(13), { name: 'Refunds', badge: null })
  const loud = [...meta.values()].filter((m) => m.badge)
  assert.equal(loud.length, 1, 'exactly one loud badge in the cluster')
})

test('a Moving-only slice still names the problem once, on the first sibling', () => {
  // A Home/Work filter can hide the stuck row. Vanishing the verdict would
  // make the remaining calm rows look like a healthy job.
  const jobs = [job({ id: 1 })]
  const items = [
    run({ id: 10, status: 'stuck' }),
    run({ id: 12, status: 'moving', updated_at: ago(120) }),
    run({ id: 13, status: 'moving', updated_at: ago(60) }),
  ]
  const visible = items.filter((r) => r.status === 'moving')
  const grouped = groupByJob(jobs, items, { now: NOW })[0]
  const pinned = pinLoudVerdict(grouped, visible, { now: NOW })
  assert.equal(pinned.badge.label, 'Failing 1 of 3')
  assert.equal(pinned.badgeOnId, 12)
  const meta = tableJobMeta(jobs, items, visible, { now: NOW })
  assert.deepEqual(meta.get(12).badge, pinned.badge)
  assert.equal(meta.get(13).badge, null)
})

test('waiting outranks moving when nothing is stuck', () => {
  const jobs = [job({ id: 1 })]
  const items = [
    run({ id: 20, status: 'waiting_on_you', updated_at: ago(31 * 3600) }),
    run({ id: 21, status: 'moving' }),
  ]
  const pinned = pinLoudVerdict(groupByJob(jobs, items, { now: NOW })[0], items, { now: NOW })
  assert.match(pinned.badge.label, /waiting/)
  assert.equal(pinned.badgeOnId, 20)
})

test('Work home stays on the lean trio; jobs enrich rows, they do not land a board', () => {
  // Grouping by job is what made the fat board tempting. It stays banned:
  // /work/overview + /work/items + suggestions are the home reads.
  // /workflows is a declaration list used only to name a row's job.
  const work = readFileSync(new URL('../src/WorkTab.jsx', import.meta.url), 'utf8')
  assert.doesNotMatch(work, /getWorkBoard|getWorkSummary/)
  assert.match(work, /getWorkOverview/)
  assert.match(work, /api\.getWorkItems\(/)
  assert.match(work, /function WorkHome/)
  assert.doesNotMatch(work, /function BoardHome/)
  assert.doesNotMatch(work, /jb-colheads/)
  assert.match(work, />Task</)
  assert.match(work, /What&apos;s next/)
  assert.doesNotMatch(work, /Other views/)
})

test('home row click opens the job; the run is a nested title click', () => {
  const work = readFileSync(new URL('../src/WorkTab.jsx', import.meta.url), 'utf8')
    .replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')
  const home = work.slice(work.indexOf('function WorkHome'), work.indexOf('export default function'))
  const table = work.slice(work.indexOf('function WorkTable'), work.indexOf('// --- the job page'))
  // Primary: a named job on the row, unmatched falls through to the run.
  assert.match(home, /if \(row\.workflow_id != null\) onOpenJob\(row\.workflow_id\)/)
  assert.match(home, /else onOpenItem\(row\)/)
  assert.match(home, /onOpenRun=\{onOpenItem\}/)
  // Job name stays a job door so click-in cannot vanish.
  assert.match(table, /onOpenJob\(row\.workflow_id\)/)
  // Nested task title is the run.
  assert.match(table, /onOpenRun\(row\)/)
  assert.match(home, /onOpenJob=\{onOpenJob\}/)
})

test('filtered to nothing is not the same as having no work', () => {
  // The answer to one is "clear a chip"; to the other, "connect an agent".
  // Telling an over-filterer to connect an agent points them at the wrong
  // problem entirely.
  const empty = groupByJob([], [], { now: NOW })
  assert.deepEqual(empty, [], 'no jobs and no runs is genuinely empty')
  const filteredOut = groupByJob([job({ id: 1 })], [], { now: NOW })
  assert.equal(filteredOut.length, 1, 'a declared job still has a row to explain itself')
  assert.equal(filteredOut[0].total, 0)
})

test('no jargon on Work home', () => {
  const FORBIDDEN = /\b(loops?|workloops?|possession|segments?|stations?|handoffs?)\b/i
  const work = readFileSync(new URL('../src/WorkTab.jsx', import.meta.url), 'utf8')
    .replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')
  const home = work.slice(work.indexOf('function WorkHome'), work.indexOf('export default function'))
  for (const m of home.matchAll(/>([^<>{}]{3,})</g)) {
    assert.ok(!FORBIDDEN.test(m[1]), `Work home ships jargon: ${JSON.stringify(m[1])}`)
  }
  for (const c of COLUMNS) assert.ok(!FORBIDDEN.test(c.label), c.label)
  assert.ok(!FORBIDDEN.test(UNMATCHED))
})
