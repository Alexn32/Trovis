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
  COLUMNS, UNMATCHED, ageLabel, boardSummary, boardTotals, cellCards, columnOf,
  durationLabel, idleJobLine, jobSubline,
  firstOverCeiling, groupByJob, healthBadge, needsCard, observedPerDay, positive,
  pinLoudVerdict, quietLine, rowJobLine, runCardLead, tableJobMeta, touchedToday,
  cardLines,
} from '../src/workBoard.js'
import { buildPath, parsePath } from '../src/route.js'
import { errorRatePercent } from '../src/utils.js'

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
  // A cell holds every run in its column; the CELL decides how many to draw
  // (two), and `exceptions` stays the count of runs that actually need a
  // person — that is what ranks one job against another.
  assert.equal(r.columns.working.length, 8)
  assert.equal(r.columns.waiting.length, 2)
  assert.equal(r.exceptions, 2, 'the stuck run and the aged wait, not the fresh one')
  // Ordering is what makes a two-card cell honest: the exception cannot be
  // the one dropped just because it arrived last.
  assert.equal(r.columns.waiting[0].id, 21, 'the aged wait outranks the fresh one')
  assert.deepEqual(cellCards(r, 'waiting').cards.map((c) => c.id), [21, 22])
  assert.equal(cellCards(r, 'waiting').more, 0)
  const working = cellCards(r, 'working')
  assert.equal(working.cards.length, 2, 'a cell draws at most two')
  assert.equal(working.more, 6, '...and says how many it did not')
  assert.deepEqual(cellCards(r, 'stuck').cards.map((c) => c.id), [20])
  assert.deepEqual(cellCards(null, 'stuck'), { cards: [], more: 0 })
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

test('Work home is the board, and it still stays on the lean reads', () => {
  // The board is the landing again (the Monday table lock is reversed), but
  // the endpoint ban is not: /work/board and /work/summary loop-scan and
  // starve the single replica. The board is built from /workflows +
  // /work/items and nothing else.
  const work = readFileSync(new URL('../src/WorkTab.jsx', import.meta.url), 'utf8')
  assert.doesNotMatch(work, /getWorkBoard|getWorkSummary/)
  assert.match(work, /api\.getWorkItems\(/)
  assert.match(work, /getWorkflows/)
  assert.match(work, /function WorkHome/)
  // Four column headers, rendered ONCE above every row.
  assert.match(work, /jb-colheads/)
  assert.equal((work.match(/className="jb-colheads"/g) || []).length, 2,
               'once for the grouped board, once for Flat — never per job row')
  assert.match(work, /function JobRow/)
  assert.match(work, /function FlatBoard/)
})

test('every board view is a view of the same rows, and Mine is server-resolved', () => {
  const work = readFileSync(new URL('../src/WorkTab.jsx', import.meta.url), 'utf8')
    .replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')
  const home = work.slice(work.indexOf('function WorkHome'), work.indexOf('// `active` is false'))
  for (const label of ['By job', 'Flat', 'Mine', 'Today']) {
    assert.ok(work.includes(`label: '${label}'`), `${label} view is offered`)
  }
  // `Mine` reads the status the SERVER resolved. The client never decides
  // who "you" is — that was the whole point of _attach_awaiting_human.
  assert.match(home, /r\.status === 'waiting_on_you'/)
  assert.doesNotMatch(home, /viewer|currentUser|myEmail/)
})

test('the Today pill actually scopes, and the empty state needs a real count', () => {
  const work = readFileSync(new URL('../src/WorkTab.jsx', import.meta.url), 'utf8')
    .replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')
  const home = work.slice(work.indexOf('function WorkHome'), work.indexOf('// `active` is false'))
  // A pill that changes the scope LABEL and not the rows is a control that
  // lies. Browser-caught: Today read "Touched today" over the full list.
  assert.match(home, /view === 'today' \? all\.filter\(\(r\) => touchedToday\(r, now\)\)/)
  // Rule 6 on the board's own empty state: "No named work yet" is an
  // assertion about the account, and an unreadable count must not produce it.
  assert.match(home, /const openCount = numOrNull\(overview\?\.open\)/)
  assert.match(home, /all\.length === 0 && openCount === 0/)
  assert.doesNotMatch(home, /openCount \?\? 0|\(overview\.open \|\| 0\)/)
})

test('a job row opens the job; a card opens the run', () => {
  const work = readFileSync(new URL('../src/WorkTab.jsx', import.meta.url), 'utf8')
    .replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')
  const rowFn = work.slice(work.indexOf('function JobRow'), work.indexOf('const BOARD_VIEWS'))
  assert.match(rowFn, /onClick=\{\(\) => onOpenJob\(grouped\.job\.id\)\}/)
  assert.match(rowFn, /onOpen=\{onOpenItem\}/)
  // An Unmatched row is not a job and must not pretend to open one.
  assert.match(rowFn, /className="jb-name is-unmatched"/)
})

// --- the board header, sublines and idle rows -------------------------------

test('the header summary is also the navigation, and states its own bases', () => {
  const jobs = [job({ id: 1, loops_today: 12, cost_usd: 3.2, window_days: 14 }),
                job({ id: 2, name: 'Onboarding', loops_today: 18, cost_usd: 0.92 })]
  const rows = groupByJob(jobs, [
    run({ id: 1, workflow_id: 1, status: 'stuck' }),
    run({ id: 2, workflow_id: 1, status: 'waiting_on_you' }),
    run({ id: 3, workflow_id: 2, status: 'moving' }),
  ], { now: NOW })
  const parts = boardSummary(rows, jobs)
  assert.deepEqual(parts.map((p) => `${p.value} ${p.label}`), [
    '2 jobs', '30 runs started today', '1 waiting', '1 stuck', '$4.12 last 14 days',
  ])
  // Waiting and stuck are the way TO waiting and stuck.
  assert.deepEqual(parts.filter((p) => p.filter).map((p) => p.filter), ['waiting', 'stuck'])
})

test('a truncated page reports a floor, not a total', () => {
  // The counts come from the rows the grid draws, so they agree with it by
  // construction — but on a page that was cut short they are a minimum, and
  // presenting a minimum as a total is the contradiction rule 5 exists for.
  const jobs = [job({ id: 1, loops_today: 4 })]
  const rows = groupByJob(jobs, [run({ id: 1, workflow_id: 1, status: 'stuck' })], { now: NOW })
  const full = boardSummary(rows, jobs, { truncated: false })
  const cut = boardSummary(rows, jobs, { truncated: true })
  for (const k of ['waiting', 'stuck']) {
    assert.equal(full.find((p) => p.key === k).floor, false, k)
    assert.equal(cut.find((p) => p.key === k).floor, true, `${k} is a floor when cut short`)
  }
  assert.equal(cut.find((p) => p.key === 'jobs').floor, undefined,
               'the job count is a server total and is never a floor')
})

test('the header never prints a cost that rounds to nothing', () => {
  // $0.00 beside four real counts reads as "this was free", which is a claim
  // about attribution the board cannot make.
  const jobs = [job({ id: 1, cost_usd: 0.004 })]
  const rows = groupByJob(jobs, [], { now: NOW })
  assert.ok(!boardSummary(rows, jobs).some((p) => p.isCost))
  assert.ok(boardSummary(rows, [job({ id: 1, cost_usd: 2 })]).some((p) => p.isCost))
})

test('the job subline says STARTED today, and drops what it does not have', () => {
  // loops_today counts runs created today, so "0 runs today" appeared above
  // two cards still open from yesterday — true, and impossible to read
  // correctly. The verb fixes it in both directions.
  const g = groupByJob([job({
    id: 1, loops_today: 0, owning_service_name: 'refunds-agent', cost_per_run: 0.18,
  })], [run({ id: 1, workflow_id: 1, status: 'moving' })], { now: NOW })[0]
  assert.deepEqual(jobSubline(g), ['0 started today · refunds-agent', '$0.18 / run'])
  // An undeclared job has no owner and no priced runs; neither becomes a dash.
  const bare = groupByJob([job({ id: 1, loops_today: 3 })], [], { now: NOW })[0]
  assert.deepEqual(jobSubline(bare), ['3 started today'])
  assert.deepEqual(jobSubline(null), [])
})

test('an idle job states what was observed and what was declared, never a conclusion', () => {
  const never = groupByJob([job({
    id: 1, expected_per_day_min: 8, expected_per_day_max: 12, last_run_at: null,
  })], [], { now: NOW })[0]
  assert.deepEqual(idleJobLine(never, { now: NOW }),
                   { observed: 'Never run', expected: 'expected 8–12/day' })
  const stale = groupByJob([job({
    id: 1, expected_per_day_min: 8, last_run_at: ago(4 * 86400),
  })], [], { now: NOW })[0]
  assert.deepEqual(idleJobLine(stale, { now: NOW }),
                   { observed: 'Last ran 4d ago', expected: 'expected 8+/day' })
  // No declared cadence means no expectation half — the record still says
  // when it last ran, and stops there.
  const undeclared = groupByJob([job({ id: 1, last_run_at: ago(86400) })], [], { now: NOW })[0]
  assert.equal(idleJobLine(undeclared, { now: NOW }).expected, null)
  // A job with runs is not idle, and Unmatched is not a job.
  const live = groupByJob([job({ id: 1 })], [run({ id: 1, workflow_id: 1 })], { now: NOW })[0]
  assert.equal(idleJobLine(live, { now: NOW }), null)
  assert.equal(idleJobLine(null), null)
})

test('a card says what its own column needs, and never says it twice', () => {
  const stuck = run({ id: 1, status: 'stuck', title: 'Refund #4471',
                      whats_next: 'Payment timed out', updated_at: ago(31 * 60) })
  assert.deepEqual(cardLines(stuck, 'stuck', { now: NOW }),
                   { lead: 'Payment timed out', sub: 'Refund #4471 · 31m' })
  // Working and waiting have no reason to lead with, so the holder does the
  // second line. "You" rather than the viewer's own name.
  const working = run({ id: 2, status: 'moving', title: 'Refund #4478',
                        whats_next: 'In progress', holder: { kind: 'agent', name: 'Mara' },
                        updated_at: ago(72) })
  assert.deepEqual(cardLines(working, 'working', { now: NOW }),
                   { lead: 'Refund #4478', sub: 'Mara · 1m' })
  const mine = run({ id: 3, status: 'waiting_on_you', title: 'Refund #4476',
                     whats_next: 'Waiting on you', holder: { kind: 'human', name: 'Alex' },
                     updated_at: ago(18 * 60) })
  assert.deepEqual(cardLines(mine, 'waiting', { now: NOW }),
                   { lead: 'Refund #4476', sub: 'You · 18m' })
  // ...but when the record DID produce a reason, that reason leads and the
  // holder does not overwrite it. Printing "Waiting on Alex" and then "Alex"
  // underneath is the same fact twice.
  const named = run({ id: 5, status: 'waiting_on_other', title: 'Refund #4472',
                      whats_next: 'Waiting on Alex', holder: { kind: 'human', name: 'Alex' },
                      updated_at: ago(2 * 3600 + 14 * 60) })
  assert.deepEqual(cardLines(named, 'waiting', { now: NOW }),
                   { lead: 'Waiting on Alex', sub: 'Refund #4472 · 2h 14m' })
  // Done says it closed. There is no cost on a lean row, and it must not
  // invent one.
  const done = run({ id: 4, status: 'done', title: 'Refund #4469',
                     whats_next: 'Done', updated_at: ago(2 * 86400) })
  assert.deepEqual(cardLines(done, 'done', { now: NOW }), { lead: 'Refund #4469', sub: 'Closed · 2d' })
  assert.ok(!/\$/.test(JSON.stringify(cardLines(done, 'done', { now: NOW }))))
})

test('Today means touched today, on the viewer\'s own day', () => {
  const midnight = new Date(NOW); midnight.setHours(0, 0, 0, 0)
  assert.equal(touchedToday(run({ updated_at: ago(60) }), NOW), true)
  assert.equal(touchedToday(run({ updated_at: new Date(midnight.getTime()).toISOString() }), NOW), true)
  assert.equal(touchedToday(run({ updated_at: new Date(midnight.getTime() - 1000).toISOString() }), NOW), false)
  assert.equal(touchedToday(run({ updated_at: null }), NOW), false)
  assert.equal(touchedToday(null, NOW), false)
})

test('RULE 6 — positive() refuses a value that is not a finite measurement', () => {
  // This is the one behaviour that separates it from `Number(x) || 0 > 0`,
  // which the two agree on for every ordinary input. A corrupt payload
  // carrying Infinity is not "some", it is nonsense, and coercion waves it
  // through as a positive count.
  assert.equal(positive(Infinity), false, 'Infinity is not a count')
  assert.equal(positive('Infinity'), false)
  assert.equal(positive(NaN), false)
  assert.equal(positive(null), false)
  assert.equal(positive(undefined), false)
  assert.equal(positive(''), false)
  assert.equal(positive(0), false, 'a measured zero is not positive')
  assert.equal(positive(-3), false)
  assert.equal(positive(1), true)
  assert.equal(positive('4'), true, 'a numeric string from the wire still counts')
})

test('RULE 6 — an agent with no spans has no error rate', () => {
  // Returning 0 reported a perfect record for an agent that has never run:
  // a pass derived from absence, the same shape as the never-run job that
  // badged Healthy. Callers render `No data`.
  assert.equal(errorRatePercent({ span_count: 0, error_count: 0 }), null)
  assert.equal(errorRatePercent({ span_count: null, error_count: 0 }), null)
  assert.equal(errorRatePercent({}), null)
  assert.equal(errorRatePercent(null), null)
  assert.equal(errorRatePercent({ span_count: 10, error_count: null }), null,
               'spans without an error count is still not a rate')
  // Non-finite is not a denominator. `Number(x) || 0` waves Infinity through
  // and divides by it, producing a serene 0% from a corrupt payload.
  assert.equal(errorRatePercent({ span_count: Infinity, error_count: 3 }), null)
  assert.equal(errorRatePercent({ span_count: 10, error_count: Infinity }), null)
  // A real zero is a real measurement and must survive.
  assert.equal(errorRatePercent({ span_count: 10, error_count: 0 }), 0)
  assert.equal(errorRatePercent({ span_count: 10, error_count: 2 }), 20)
})

test('RULE 5 — the board header states the basis of every number it shows', () => {
  // The header replaced the overview pills, and inherits their problem: its
  // five numbers come from three different places. Jobs and runs-started are
  // server totals; waiting and stuck are counted from the rows the grid
  // draws; cost is the job window, which is not today. Left unlabelled the
  // screen reads as contradicting itself.
  const src = readFileSync(new URL('../src/WorkTab.jsx', import.meta.url), 'utf8')
  // The two page-derived counts can be floors, and say so.
  assert.match(src, /\{p\.floor \? '\+' : ''\}/)
  // The scope of "these rows" is on the page, not assumed.
  assert.match(src, /className="jb-scope"/)
  assert.match(src, /Touched today|All open work/)
  // Cost carries its own window rather than sitting silently beside counts
  // that mean today.
  const wb = readFileSync(new URL('../src/workBoard.js', import.meta.url), 'utf8')
  assert.match(wb, /label: days \? `last \$\{days\} days` : 'recorded'/)
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
