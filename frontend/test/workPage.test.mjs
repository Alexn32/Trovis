// The Work page's presentation rules — the tiles, the job shape, and which
// runs get a line. Pure functions, so these run without a DOM.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import {
  DENSITIES, WORK_VIEWS, calmLine, compactJobLine, completedDelta, countsLine, exceptionLine,
  exceptionRows, resolveDensity, resolveView, scopeLine, situationTiles, stateSegments, tileTarget,
} from '../src/workPage.js'
import { groupByJob } from '../src/workBoard.js'

const NOW = Date.parse('2026-09-21T12:00:00Z')
const ago = (s) => new Date(NOW - s * 1000).toISOString()
const run = (o) => ({
  id: 1, title: 'Refund #1', status: 'moving', holder: { kind: 'agent', name: 'refunds-agent' },
  whats_next: 'In progress', updated_at: ago(60), workflow_id: 1, workflow_name: 'Refunds', ...o,
})
const job = (o) => ({ id: 1, name: 'Refunds', loop_counts: {}, loops_today: 0, ...o })

// --- the situation strip ----------------------------------------------------

test('no overview means no tiles — a skeleton, never four zeros', () => {
  assert.equal(situationTiles(null), null)
  assert.equal(situationTiles(undefined), null)
})

test('the tiles are the server counts, each with the words for what it counts', () => {
  const tiles = situationTiles({
    needs_you: 2, needs_attention: 3, open: 11, completed_week: 40,
    completed_prev_week: 30, has_prev_week: true,
  })
  assert.deepEqual(tiles.map((t) => [t.key, t.value]), [
    ['needs_you', 2], ['needs_attention', 3], ['open', 11], ['completed_week', 40],
  ])
  for (const t of tiles) assert.ok(t.sub.length > 0, `${t.key} says what it counts`)
  assert.equal(tiles[3].sub, '+10 vs last week')
  assert.equal(tiles[3].delta.direction, 'up')
})

test('RULE 6 — a count the server did not return is null, not zero', () => {
  const tiles = situationTiles({ needs_you: 0, open: null })
  assert.equal(tiles[0].value, 0, 'a real zero survives')
  assert.equal(tiles[1].value, null, 'a missing count stays missing')
  assert.equal(tiles[2].value, null)
  assert.equal(tiles[0].tone, 'quiet', 'zero needs-you is quiet')
})

test('a nonzero count that needs a person carries its color; routine counts do not', () => {
  const tiles = situationTiles({ needs_you: 1, needs_attention: 2, open: 5, completed_week: 3 })
  assert.equal(tiles[0].tone, 'warning')
  assert.equal(tiles[1].tone, 'error')
  assert.equal(tiles[2].tone, 'neutral')
  assert.equal(tiles[3].tone, 'neutral')
})

test('the week-over-week delta exists only when the server says the weeks compare', () => {
  assert.equal(completedDelta({ completed_week: 5, completed_prev_week: 0, has_prev_week: false }), null)
  assert.equal(completedDelta({ completed_week: 5, has_prev_week: true }), null, 'no prev count')
  assert.deepEqual(completedDelta({ completed_week: 5, completed_prev_week: 5, has_prev_week: true }),
    { delta: 0, direction: 'flat', text: 'same as last week' })
  assert.equal(completedDelta({ completed_week: 2, completed_prev_week: 5, has_prev_week: true }).text,
    '−3 vs last week')
})

test('a tile is a door: it names the filter and the view that shows its rows', () => {
  const tiles = situationTiles({ needs_you: 1, needs_attention: 1, open: 1, completed_week: 1 })
  assert.deepEqual(tileTarget(tiles[0]), { filter: 'mine', view: 'open' })
  assert.deepEqual(tileTarget(tiles[1]), { filter: 'attention', view: 'open' })
  assert.deepEqual(tileTarget(tiles[2]), { filter: null, view: 'jobs' })
  assert.deepEqual(tileTarget(tiles[3]), { filter: null, view: 'done' })
})

test('views: three, and an unknown key lands on the default', () => {
  assert.deepEqual(WORK_VIEWS.map((v) => v.key), ['jobs', 'open', 'done'])
  assert.equal(resolveView('flat'), 'jobs')
  assert.equal(resolveView('done'), 'done')
  assert.equal(resolveView(null), 'jobs')
})

// --- the job's shape --------------------------------------------------------

test('the state bar drops zeros and its counts add up to the rows', () => {
  const g = groupByJob([job()], [
    run({ id: 1, status: 'stuck' }), run({ id: 2, status: 'moving' }), run({ id: 3, status: 'moving' }),
    run({ id: 4, status: 'done' }),
  ], { now: NOW })[0]
  const { total, segments } = stateSegments(g)
  assert.equal(total, 4)
  assert.deepEqual(segments.map((s) => [s.key, s.count]), [['stuck', 1], ['working', 2], ['done', 1]])
  assert.ok(Math.abs(segments.reduce((n, s) => n + s.pct, 0) - 100) < 1e-9)
  assert.equal(countsLine(g), '1 stuck · 2 moving · 1 done today')
})

test('a job with no runs on the page has no bar and says so in words', () => {
  const g = groupByJob([job()], [], { now: NOW })[0]
  assert.deepEqual(stateSegments(g), { total: 0, segments: [] })
  assert.equal(calmLine(g), 'Nothing open')
})

// --- which runs get a line --------------------------------------------------

test('exceptions: stuck always, waiting on you always, other waits only once aged', () => {
  const g = groupByJob([job()], [
    run({ id: 1, status: 'moving' }),
    run({ id: 2, status: 'waiting_on_other', updated_at: ago(600) }),
    run({ id: 3, status: 'waiting_on_other', updated_at: ago(5 * 3600) }),
    run({ id: 4, status: 'waiting_on_you', updated_at: ago(60) }),
    run({ id: 5, status: 'stuck', updated_at: ago(30) }),
    run({ id: 6, status: 'done' }),
  ], { now: NOW })[0]
  const { rows, more, total } = exceptionRows(g, { now: NOW })
  assert.deepEqual(rows.map((r) => r.id), [5, 4, 3], 'stuck, then you, then the aged wait')
  assert.equal(more, 0)
  assert.equal(total, 3)
})

test('past the limit the rest is a count, and the ones shown are the worst', () => {
  const g = groupByJob([job()], [
    run({ id: 1, status: 'waiting_on_you', updated_at: ago(60) }),
    run({ id: 2, status: 'waiting_on_you', updated_at: ago(120) }),
    run({ id: 3, status: 'stuck', updated_at: ago(10) }),
    run({ id: 4, status: 'stuck', updated_at: ago(20) }),
    run({ id: 5, status: 'waiting_on_other', updated_at: ago(9 * 3600) }),
  ], { now: NOW })[0]
  const { rows, more } = exceptionRows(g, { now: NOW, limit: 3 })
  assert.deepEqual(rows.map((r) => r.id), [4, 3, 2], 'oldest stuck first, then the oldest you')
  assert.equal(more, 2)
})

test('an exception line leads with the run and states who has it and for how long', () => {
  const stuck = exceptionLine(run({ status: 'stuck', whats_next: 'Needs attention', updated_at: ago(3600) }), { now: NOW })
  assert.equal(stuck.lead, 'Refund #1')
  assert.equal(stuck.sub, 'Stuck · 1h 00m', 'a whats_next that only restates the state is not a reason')
  assert.equal(stuck.tone, 'stuck')

  const you = exceptionLine(run({ status: 'waiting_on_you', whats_next: 'Waiting on you', updated_at: ago(120) }), { now: NOW })
  assert.equal(you.sub, 'Waiting on you · 2m')
  assert.equal(you.tone, 'you')

  const other = exceptionLine(run({
    status: 'waiting_on_other', holder: { kind: 'human', name: 'Sarah Chen' },
    whats_next: 'Waiting on Sarah Chen', updated_at: ago(5 * 3600),
  }), { now: NOW })
  assert.equal(other.sub, 'Waiting on Sarah Chen · 5h 00m')

  const reason = exceptionLine(run({ status: 'stuck', whats_next: 'Payments tool timed out', updated_at: ago(60) }), { now: NOW })
  assert.equal(reason.sub, 'Stuck · 1m · Payments tool timed out', 'a real reason is kept')
})

// --- the scope line ---------------------------------------------------------

test('the scope line names whose work and how many rows the page holds, floor when truncated', () => {
  assert.deepEqual(scopeLine({ whoseLabel: 'My team', shown: 12, truncated: true }), ['My team', '12+ rows loaded'])
  assert.deepEqual(scopeLine({ shown: 1 }), ['All workers', '1 row loaded'])
  assert.deepEqual(scopeLine({}), ['All workers'])
})

// --- language ---------------------------------------------------------------

test('no jargon in anything a person reads from workPage.js', () => {
  const FORBIDDEN = /\b(loops?|workloops?|possession|segments?|stations?|handoffs?)\b/i
  const tiles = situationTiles({ needs_you: 1, needs_attention: 1, open: 1, completed_week: 1 })
  for (const t of tiles) {
    assert.ok(!FORBIDDEN.test(t.label), t.label)
    assert.ok(!FORBIDDEN.test(t.sub), t.sub)
  }
  const src = readFileSync(new URL('../src/workPage.js', import.meta.url), 'utf8')
    .replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')
  for (const m of src.matchAll(/'([^'\n]{3,})'/g)) {
    assert.ok(!FORBIDDEN.test(m[1]), `workPage.js ships jargon: ${m[1]}`)
  }
})

test('Work says how current its picture is, and never implies live', () => {
  // The overview carries the account-wide newest-span time (the same MAX
  // Home's freshness panel reads). Work prints it under the tiles; with no
  // telemetry at all it says so rather than showing a stale relative time.
  const work = readFileSync(new URL('../src/WorkTab.jsx', import.meta.url), 'utf8')
  assert.match(work, /function FreshnessLine\(\{ overview \}\)/)
  assert.match(work, /const at = overview\.latest_telemetry_at/)
  assert.match(work, /\{at \? relTime\(at\) : 'No data yet'\}/)
  assert.match(work, /<FreshnessLine overview=\{overview\} \/>/)
})

// --- the compact job list ---------------------------------------------------

test('the compact line carries the same verdict as the row, and empty cells stay empty', () => {
  const g = groupByJob([job({
    id: 1, name: 'Refunds', last_run_at: ago(120), started_runs: 28, window_days: 14,
    cost_per_run: 0.06, has_expectation: true, expected_per_day_min: 1,
  })], [run({ id: 1, status: 'stuck', updated_at: ago(60) }), run({ id: 2, status: 'moving' })], { now: NOW })[0]
  const line = compactJobLine(g, { now: NOW })
  assert.equal(line.name, 'Refunds')
  assert.equal(line.openable, true)
  assert.equal(line.open, 2)
  assert.equal(line.badge.label, 'Failing 1 of 2', 'the one badge the row shows')
  assert.equal(line.loud, true)
  assert.equal(line.lastRun, '2m ago')
  assert.equal(line.cadence, '2/day')
  assert.equal(line.costPerRun, '$0.06')

  const bare = compactJobLine(groupByJob([job({ id: 2, name: 'New' })], [], { now: NOW })[0], { now: NOW })
  assert.equal(bare.open, 0)
  assert.equal(bare.lastRun, null, 'never ran is not "0m ago"')
  assert.equal(bare.cadence, null)
  assert.equal(bare.costPerRun, null, 'unpriced is not $0.00')
  assert.equal(bare.loud, false, 'no expectation → observed, no verdict, nothing loud')
})

test('an Unmatched line is not a job: no door, no verdict', () => {
  const g = groupByJob([], [run({ id: 9, workflow_id: 77, status: 'stuck' })], { now: NOW })[0]
  const line = compactJobLine(g, { now: NOW })
  assert.equal(line.openable, false)
  assert.equal(line.badge, null)
})

test('density is a remembered convenience, and an unknown value means rows', () => {
  assert.deepEqual(DENSITIES, ['rows', 'list'])
  assert.equal(resolveDensity('list'), 'list')
  assert.equal(resolveDensity('grid'), 'rows')
  assert.equal(resolveDensity(null), 'rows')
  const work = readFileSync(new URL('../src/WorkTab.jsx', import.meta.url), 'utf8')
  // Storage reads and writes are wrapped: a blocked store is the default, never a crash.
  assert.match(work, /try \{ return resolveDensity\(localStorage\.getItem\(DENSITY_KEY\)\) \} catch \{ return 'rows' \}/)
  assert.match(work, /density === 'list' && \(\s*<JobList grouped=\{grouped\} now=\{now\} onOpenJob=\{onOpenJob\} \/>/)
  assert.match(work, /density === 'rows' && \(/)
})
