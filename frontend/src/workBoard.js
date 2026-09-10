// Job-shaped facts for Work rows.
//
// These helpers group lean /work/items onto declared jobs and produce a
// named verdict (health, cadence, expectation). They feed TABLE-ROW
// enrichment on Work home — a job name and a number under Task — not a
// kanban landing. The four Working | Waiting | Stuck | Done buckets stay
// here as data. They must not become the /work home surface.
//
// Everything in this file is pure, so the rules can be tested without
// mounting anything.

import { isNamedWorkTitle } from './board.js'

/** The four states a run can be in, as columns, in reading order. */
export const COLUMNS = [
  { key: 'working', label: 'Working' },
  { key: 'waiting', label: 'Waiting on a person' },
  { key: 'stuck', label: 'Stuck' },
  { key: 'done', label: 'Done today' },
]

/** Where runs with no job land. Its size is a matcher-health signal. */
export const UNMATCHED = 'Unmatched'

/**
 * Wire status -> column.
 *
 * `stuck` absorbs the run that went quiet: not-moving is one column with a
 * reason, not two columns that both mean "not moving". A fifth column would
 * make the reader classify before they can read.
 */
export function columnOf(status) {
  switch (status) {
    case 'moving': return 'working'
    case 'waiting_on_you':
    case 'waiting_on_other': return 'waiting'
    case 'stuck': return 'stuck'
    case 'done': return 'done'
    default: return null
  }
}

// Lines the record produces when it has nothing specific to say. They restate
// the state, which the column header already did, so they are not reasons.
const NOT_A_REASON = new Set([
  'needs attention', 'in progress', 'waiting on you', 'waiting on someone',
  'waiting on a person', 'done', '',
])

/**
 * What a run card says: the problem first, the identity underneath.
 *
 * "Payments tool timed out / #4468 · stuck 4h" beats "Refund #4468" with the
 * reason buried, because the reason is why you are looking.
 *
 * The fallback is the whole point of this function. A reason only exists when
 * the record produced one — a tool error, a named party. A merely slow run
 * produces nothing, and inventing a vague problem line for it would be worse
 * than having none: it would make every card look equally diagnosed. When
 * there is no reason, the run's own name leads and the condition sits under
 * it.
 */
export function runCardLead(row, { now = Date.now() } = {}) {
  const next = String(row?.whats_next || '').trim()
  const hasReason = Boolean(next) && !NOT_A_REASON.has(next.toLowerCase())
  const name = isNamedWorkTitle(row?.title) ? String(row.title).trim() : `Run ${row?.id}`
  const age = ageLabel(row?.updated_at, now)
  return hasReason
    ? { lead: next, sub: [name, age].filter(Boolean).join(' · '), synthesized: false }
    : { lead: name, sub: age, synthesized: false }
}

/** "4h 02m" / "31h" / "2d" — null when the record has no timestamp. */
export function ageLabel(at, now = Date.now()) {
  const t = Date.parse(at || '')
  if (!Number.isFinite(t)) return null
  const s = Math.max(0, Math.floor((now - t) / 1000))
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m`
  const h = Math.floor(m / 60)
  if (h < 48) return h < 10 ? `${h}h ${String(m - h * 60).padStart(2, '0')}m` : `${h}h`
  return `${Math.floor(h / 24)}d`
}

/**
 * Which runs get a card, and which are only a count.
 *
 * A job with twelve runs needs the two exceptional ones shown, not two
 * arbitrary normal ones per column. So: stuck always, a wait past the
 * threshold always, and nothing else. Routine activity is text.
 */
export const WAIT_ATTENTION_S = 4 * 3600

export function needsCard(row, { now = Date.now(), waitAfterS = WAIT_ATTENTION_S } = {}) {
  if (row?.status === 'stuck') return true
  if (row?.status === 'waiting_on_you' || row?.status === 'waiting_on_other') {
    const t = Date.parse(row?.updated_at || '')
    if (!Number.isFinite(t)) return false
    return (now - t) / 1000 >= waitAfterS
  }
  return false
}

/**
 * One row per job: its runs bucketed, its exceptions picked out, its counts.
 *
 * `jobs` is the declared list (so a job with no runs today still gets a row —
 * work that stopped happening is invisible on a flat board, and that is the
 * main reason this page groups at all). `runs` is the lean item list.
 *
 * Runs whose job is not in `jobs` — unmatched, or matched to an archived job
 * that the list did not include — land in a trailing Unmatched row rather
 * than vanishing. Its size is worth seeing.
 */
export function groupByJob(jobs, runs, { now = Date.now() } = {}) {
  const rows = new Map()
  const add = (key, name, job) => {
    if (!rows.has(key)) {
      rows.set(key, {
        key, name, job: job || null,
        columns: { working: [], waiting: [], stuck: [], done: [] },
        counts: { working: 0, waiting: 0, stuck: 0, done: 0 },
        isUnmatched: key === UNMATCHED,
      })
    }
    return rows.get(key)
  }

  for (const j of jobs || []) {
    if (!j || j.id == null) continue
    add(String(j.id), j.name || 'Untitled job', j)
  }

  for (const r of runs || []) {
    const col = columnOf(r?.status)
    if (!col) continue
    const known = r?.workflow_id != null && rows.has(String(r.workflow_id))
    const row = known
      ? rows.get(String(r.workflow_id))
      : add(UNMATCHED, UNMATCHED, null)
    row.counts[col] += 1
    if (needsCard(r, { now })) row.columns[col].push(r)
  }

  for (const row of rows.values()) {
    for (const c of Object.keys(row.columns)) {
      // Worst first inside a cell: the oldest exception is the one to read.
      row.columns[c].sort(
        (a, b) => (Date.parse(a.updated_at || '') || 0) - (Date.parse(b.updated_at || '') || 0),
      )
    }
    row.open = row.counts.working + row.counts.waiting + row.counts.stuck
    row.total = row.open + row.counts.done
    row.exceptions = Object.values(row.columns).reduce((n, l) => n + l.length, 0)
  }

  // Jobs with something to look at first; Unmatched always last.
  return [...rows.values()].sort((a, b) => {
    if (a.isUnmatched !== b.isUnmatched) return a.isUnmatched ? 1 : -1
    if (a.exceptions !== b.exceptions) return b.exceptions - a.exceptions
    return b.total - a.total
  })
}

/**
 * ONE badge per job, highest severity wins, and it always names its number.
 *
 * A job can be several things at once, and a row that carries three badges
 * makes the reader rank them. Precedence, worst first:
 *
 *   1. stuck runs                      Failing 2 of 12
 *   2. a wait past the threshold       1 waiting 31h
 *   3. quiet past the expected cadence Quiet 3 days
 *   4. a metric past its ceiling       Intervention 18%, expected under 10%
 *   5. nothing wrong                   Healthy
 *
 * Levels 3 and 4 need a DECLARED expectation and simply cannot fire without
 * one: with nothing to compare against there is no such thing as too quiet.
 * A job with no expectation reports its observed numbers and no verdict —
 * `tone: 'none'` — because a verdict with no number behind it is an adjective.
 */
export function healthBadge(row, { now = Date.now(), waitAfterS = WAIT_ATTENTION_S } = {}) {
  const job = row?.job || null
  const counts = row?.counts || {}
  const stuck = counts.stuck || 0
  if (stuck > 0) {
    return { tone: 'error', label: `Failing ${stuck} of ${row.total}` }
  }

  const waits = [...(row?.columns?.waiting || [])]
  const oldest = waits
    .map((r) => Date.parse(r.updated_at || ''))
    .filter((t) => Number.isFinite(t))
    .sort((a, b) => a - b)[0]
  if (oldest !== undefined && (now - oldest) / 1000 >= waitAfterS) {
    return {
      tone: 'warning',
      label: `${counts.waiting} waiting ${ageLabel(new Date(oldest).toISOString(), now)}`,
    }
  }

  const declared = Boolean(job?.has_expectation)
  const perDay = observedPerDay(job)
  const min = job?.expected_per_day_min
  if (declared && min != null) {
    const quietDays = quietFor(job, now)
    // A job that declared a cadence and has never run is not healthy. It is
    // the emptiest possible result against a stated expectation, and it was
    // reading as a green tick because every observed number was null and so
    // no ceiling could be exceeded.
    if (quietDays === null) {
      return { tone: 'warning', label: `Never run, expected ${min}/day` }
    }
    if (perDay !== null && perDay < min) {
      // Below the declared floor is below it whether or not the job has been
      // quiet for a whole day — "it ran an hour ago" does not answer "it is
      // running at a tenth of the rate you asked for".
      if (quietDays >= 1) {
        return { tone: 'warning', label: `Quiet ${quietDays} ${quietDays === 1 ? 'day' : 'days'}` }
      }
      const max = job.expected_per_day_max
      return {
        tone: 'warning',
        label: `${perDay}/day, expected ${max != null ? `${min}–${max}` : `${min}+`}`,
      }
    }
  }

  if (declared) {
    const over = firstOverCeiling(job)
    if (over) return { tone: 'warning', label: over }
  }

  if (!declared) {
    // Observed, no verdict. The number is real; the judgement is not ours to
    // make without a declared ceiling.
    const n = perDay
    return {
      tone: 'none',
      label: n === null ? 'No expectation set' : `${n}/day, no expectation set`,
    }
  }
  return { tone: 'ok', label: 'Healthy' }
}

/** Runs per day over the job's own stated window, or null with no data. */
export function observedPerDay(job) {
  const days = numOrNull(job?.window_days)
  const closed = numOrNull(job?.closed_runs)
  if (days === null || days <= 0 || closed === null) return null
  const n = closed / days
  return n >= 10 ? Math.round(n) : Math.round(n * 10) / 10
}

/** Whole days since this job last ran, or null when it never has. */
export function quietFor(job, now = Date.now()) {
  const t = Date.parse(job?.last_run_at || '')
  if (!Number.isFinite(t)) return null
  return Math.floor((now - t) / 86400000)
}

/**
 * The first declared ceiling the record exceeds, phrased with both numbers.
 * Never "off expectation" — the reader needs to see what and by how much.
 */
export function firstOverCeiling(job) {
  const checks = [
    ['intervention_pct', 'expected_intervention_pct', 'Intervention', '%'],
    ['failure_pct', 'expected_failure_pct', 'Failure rate', '%'],
  ]
  for (const [obs, exp, label, unit] of checks) {
    const o = job?.[obs]
    const e = job?.[exp]
    if (o != null && e != null && o > e) {
      return `${label} ${o}${unit}, expected under ${e}${unit}`
    }
  }
  const close = job?.median_close_s
  const closeMax = job?.expected_close_s
  if (close != null && closeMax != null && close > closeMax) {
    return `Close time ${durationLabel(close)}, expected under ${durationLabel(closeMax)}`
  }
  return null
}

/**
 * A number, or null — guarding the COERCION, not just the result.
 *
 * `Number(null)` and `Number('')` are both 0, so every "is it finite?" check
 * quietly turns a missing value into a measured zero. That is the single
 * mistake behind "$0.00" on an unpriced run and "0s" on a job that has never
 * closed one, and it has now been made three times in this codebase. Absent
 * has to be rejected before the cast, never after.
 */
export function numOrNull(v) {
  if (v === null || v === undefined || v === '') return null
  const n = Number(v)
  return Number.isFinite(n) ? n : null
}

/** "4m 18s" / "1h 06m" / "45s" — for a duration in whole seconds. */
export function durationLabel(s) {
  const n = numOrNull(s)
  if (n === null || n < 0) return null
  if (n < 60) return `${Math.round(n)}s`
  const m = Math.floor(n / 60)
  if (m < 60) return `${m}m ${String(Math.round(n - m * 60)).padStart(2, '0')}s`
  const h = Math.floor(m / 60)
  return `${h}h ${String(m - h * 60).padStart(2, '0')}m`
}

/**
 * The one prose line a job with nothing to look at gets, instead of an empty
 * four-column grid. An empty grid spends a lot of screen saying nothing.
 */
export function quietLine(row) {
  const parts = []
  if (row.counts.working) parts.push(`${row.counts.working} moving`)
  if (row.counts.done) parts.push(`${row.counts.done} closed today`)
  const median = durationLabel(row.job?.median_close_s)
  if (median) parts.push(`median close ${median}`)
  if (parts.length === 0) return 'Nothing running.'
  return `Nothing needs attention. ${sentence(parts)}.`
}

function sentence(parts) {
  const [first, ...rest] = parts
  const head = first.charAt(0).toUpperCase() + first.slice(1)
  return [head, ...rest].join(', ')
}

/**
 * `Mine` and `Today`, applied to the run list before grouping.
 *
 * `Mine` is `waiting_on_you`, and that status is resolved SERVER-side against
 * the session identity (database._attach_awaiting_human) — the client never
 * decides who "you" is. Filtering on it here is reading the server's answer,
 * not recomputing it.
 *
 * `Today` is the runs that moved today, on the viewer's own clock, which is
 * the clock the word "today" means to them.
 */
export function applyScope(rows, scope, { now = Date.now() } = {}) {
  if (scope === 'mine') return (rows || []).filter((r) => r.status === 'waiting_on_you')
  if (scope === 'today') {
    const midnight = new Date(now)
    midnight.setHours(0, 0, 0, 0)
    return (rows || []).filter((r) => {
      const t = Date.parse(r.updated_at || '')
      return Number.isFinite(t) && t >= midnight.getTime()
    })
  }
  return rows || []
}

/**
 * What a Monday-table Task cell may show under the title: the job's name,
 * and a verdict only when it names a problem.
 *
 * Calm jobs stay a name. "Healthy" and "no expectation set" are real
 * readings, but repeating them on every row is a fourth column of noise.
 * Stuck / quiet / off-expectation keep their number.
 */
export function rowJobLine(grouped, { now = Date.now() } = {}) {
  if (!grouped || grouped.isUnmatched) return null
  const badge = healthBadge(grouped, { now })
  const loud = badge && (badge.tone === 'error' || badge.tone === 'warning')
  return { name: grouped.name, badge: loud ? badge : null }
}

// Same heat as hottestOpen: the row that needs a person, oldest first.
const LOUD_PIN_RANK = { stuck: 0, waiting_on_you: 1, waiting_on_other: 2 }

/**
 * Pin a loud job verdict to one row in a visible cluster.
 *
 * Clustering rule: one badge per job, on the hottest attention sibling
 * (stuck → waiting_on_you → waiting_on_other, oldest first). Healthy /
 * moving / done siblings keep the job *name* and drop the alarm. If the
 * visible slice has no attention row (e.g. a Moving filter), pin to the
 * first sibling in table order so the problem does not vanish.
 */
export function pinLoudVerdict(grouped, visibleRows, { now = Date.now() } = {}) {
  const line = rowJobLine(grouped, { now })
  if (!line) return null
  if (!line.badge) return { name: line.name, badge: null, badgeOnId: null }
  const siblings = (visibleRows || []).filter(
    (r) => r?.workflow_id != null && String(r.workflow_id) === String(grouped.key),
  )
  const attention = siblings
    .filter((r) => LOUD_PIN_RANK[r.status] != null)
    .sort((a, b) => {
      const ra = LOUD_PIN_RANK[a.status]
      const rb = LOUD_PIN_RANK[b.status]
      if (ra !== rb) return ra - rb
      return (Date.parse(a.updated_at || '') || 0) - (Date.parse(b.updated_at || '') || 0)
    })
  const pin = attention[0] || siblings[0] || null
  return { name: line.name, badge: line.badge, badgeOnId: pin?.id ?? null }
}

/**
 * Per-row Task sublines for the Monday table.
 *
 * Keyed by run id so siblings of one job can share a name and still
 * differ on the badge. Unmatched / undeclared names fall through to
 * `workflow_name` on the row.
 */
export function tableJobMeta(jobs, items, visibleRows, { now = Date.now() } = {}) {
  const grouped = groupByJob(jobs || [], items || [], { now })
  const pinnedByKey = new Map()
  for (const g of grouped) {
    const pinned = pinLoudVerdict(g, visibleRows, { now })
    if (pinned) pinnedByKey.set(g.key, pinned)
  }
  const map = new Map()
  for (const row of visibleRows || []) {
    const key = row?.workflow_id != null ? String(row.workflow_id) : null
    const pinned = key ? pinnedByKey.get(key) : null
    const name = pinned?.name || row?.workflow_name || null
    if (!name) continue
    map.set(row.id, {
      name,
      badge: pinned?.badgeOnId === row.id ? pinned.badge : null,
    })
  }
  return map
}

/** The header's counts, from the same arrays the rows are built from. */
export function boardTotals(rows) {
  const t = { jobs: 0, working: 0, waiting: 0, stuck: 0, done: 0 }
  for (const r of rows || []) {
    if (!r.isUnmatched) t.jobs += 1
    t.working += r.counts.working
    t.waiting += r.counts.waiting
    t.stuck += r.counts.stuck
    t.done += r.counts.done
  }
  return t
}
