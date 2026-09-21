// The Work page's presentation rules — pure, so node --test can hold them.
//
// The page answers three questions in order: what needs a person, where the
// work is right now (by job), and what got done. Every number rendered here
// either comes straight off a server contract (/work/overview, /workflows,
// /home/snapshot) or is counted from the very rows the page draws, and each
// tile says which. Nothing is recomputed to "fix" a disagreement — the
// sub-line explains it instead (honesty rule 5).
//
// Language rule (enforced by test): nothing a person reads here says loop,
// possession, segment, station or handoff.

import { ageLabel, needsCard, numOrNull } from './workBoard.js'

/** The three ways to look at the same work. */
export const WORK_VIEWS = [
  { key: 'jobs', label: 'By job' },
  { key: 'open', label: 'All open' },
  { key: 'done', label: 'Completed' },
]

export const DEFAULT_VIEW = 'jobs'

/** A persisted or navigated view key, or the default when it is not one. */
export function resolveView(key) {
  return WORK_VIEWS.some((v) => v.key === key) ? key : DEFAULT_VIEW
}

/**
 * The situation strip: four server counts, each naming what it counts.
 *
 * `null` while the overview has not arrived — the strip renders a skeleton,
 * never four zeros, because "0 need you" is a claim (rule 6). A tile carries
 * the filter (and, for Done, the view) it opens so the number is also the
 * way to the rows behind it.
 */
export function situationTiles(overview) {
  if (!overview) return null
  const needsYou = numOrNull(overview.needs_you)
  const attention = numOrNull(overview.needs_attention)
  const open = numOrNull(overview.open)
  const doneWeek = numOrNull(overview.completed_week)
  const delta = completedDelta(overview)
  return [
    {
      key: 'needs_you',
      label: 'Needs you',
      value: needsYou,
      sub: 'waiting on your decision',
      tone: needsYou ? 'warning' : 'quiet',
      filter: 'mine',
      view: 'open',
    },
    {
      key: 'needs_attention',
      label: 'Needs attention',
      value: attention,
      sub: 'stuck, or waiting too long',
      tone: attention ? 'error' : 'quiet',
      filter: 'attention',
      view: 'open',
    },
    {
      key: 'open',
      label: 'Open right now',
      value: open,
      sub: 'all unfinished work in scope',
      tone: 'neutral',
      filter: null,
      view: 'jobs',
    },
    {
      key: 'completed_week',
      label: 'Done this week',
      value: doneWeek,
      sub: delta ? delta.text : 'recorded completions, last 7 days',
      tone: 'neutral',
      delta,
      filter: null,
      view: 'done',
    },
  ]
}

/**
 * This week against last, only when the server says the comparison holds.
 * A delta between an established week and an unestablished one is not a
 * delta, and `has_prev_week` is the contract's word for that.
 */
export function completedDelta(overview) {
  if (!overview || overview.has_prev_week !== true) return null
  const now = numOrNull(overview.completed_week)
  const prev = numOrNull(overview.completed_prev_week)
  if (now === null || prev === null) return null
  const d = now - prev
  if (d === 0) return { delta: 0, direction: 'flat', text: 'same as last week' }
  return {
    delta: d,
    direction: d > 0 ? 'up' : 'down',
    text: `${d > 0 ? '+' : '−'}${Math.abs(d)} vs last week`,
  }
}

/** What a tile's number means on the rows below it. */
export const STATE_META = {
  working: { key: 'working', label: 'moving', tone: 'live' },
  waiting: { key: 'waiting', label: 'waiting on a person', tone: 'waiting' },
  stuck: { key: 'stuck', label: 'stuck', tone: 'stuck' },
  done: { key: 'done', label: 'done today', tone: 'done' },
}

/**
 * A job's shape as a stacked bar: each state's share of the runs on this
 * page, zeros dropped. The bar is proportional to the rows the page holds,
 * which is why the row beside it always prints the counts — a bar alone
 * cannot say whether it is 3 runs or 300.
 */
export function stateSegments(grouped) {
  const counts = grouped?.counts || {}
  const order = ['stuck', 'waiting', 'working', 'done']
  const total = order.reduce((n, k) => n + (counts[k] || 0), 0)
  if (total === 0) return { total: 0, segments: [] }
  return {
    total,
    segments: order
      .filter((k) => (counts[k] || 0) > 0)
      .map((k) => ({ ...STATE_META[k], count: counts[k], pct: (counts[k] / total) * 100 })),
  }
}

/** "2 moving · 1 waiting on a person · 9 done today" — the counts, in words. */
export function countsLine(grouped) {
  const { segments } = stateSegments(grouped)
  return segments.map((s) => `${s.count} ${s.label}`).join(' · ')
}

// The order exceptions read in: the run a person must act on first.
const EXCEPTION_RANK = { stuck: 0, waiting_on_you: 1, waiting_on_other: 2 }

/**
 * The runs in a job that need a person, and how many did not fit.
 *
 * Stuck always; anything waiting on YOU always (the server resolved that, and
 * a decision the reader owns is never routine); a wait on someone else only
 * once it has aged past the attention threshold (`needsCard`). Everything
 * else in the job is a count, not a card — twelve routine runs shown as
 * twelve cards is how a board stops being readable.
 */
export function exceptionRows(grouped, { now = Date.now(), limit = 3 } = {}) {
  const all = []
  for (const col of ['stuck', 'waiting', 'working']) {
    for (const r of grouped?.columns?.[col] || []) {
      if (r?.status === 'waiting_on_you' || needsCard(r, { now })) all.push(r)
    }
  }
  all.sort((a, b) => {
    const ra = EXCEPTION_RANK[a.status] ?? 9
    const rb = EXCEPTION_RANK[b.status] ?? 9
    if (ra !== rb) return ra - rb
    return (Date.parse(a.updated_at || '') || 0) - (Date.parse(b.updated_at || '') || 0)
  })
  return { rows: all.slice(0, limit), more: Math.max(0, all.length - limit), total: all.length }
}

/**
 * One exception, as a line: what is wrong, then who has it and for how long.
 *
 * The reason leads only when the record produced one — the same rule as the
 * old card (`runCardLead`). A stuck run with no recorded reason leads with
 * its own name; inventing "needs attention" for it would make every row look
 * equally diagnosed.
 */
export function exceptionLine(row, { now = Date.now() } = {}) {
  const age = ageLabel(row?.updated_at, now)
  const who = String(row?.holder?.name || '').trim()
  const state = row?.status === 'stuck' ? 'Stuck'
    : row?.status === 'waiting_on_you' ? 'Waiting on you'
      : 'Waiting on ' + (who || 'someone')
  const next = String(row?.whats_next || '').trim()
  // A `whats_next` that only restates the state is not a reason.
  const restates = !next || /^(waiting on|needs attention|in progress|done)/i.test(next)
  const reason = restates ? null : next
  const withFor = age ? `${state} · ${age}` : state
  return {
    lead: row?.title || `Run ${row?.id}`,
    sub: reason ? `${withFor} · ${reason}` : withFor,
    tone: row?.status === 'stuck' ? 'stuck' : row?.status === 'waiting_on_you' ? 'you' : 'waiting',
  }
}

/**
 * The job's one-line status when nothing on it needs a person.
 * Observed facts only; the verdict is the badge's job.
 */
export function calmLine(grouped) {
  const line = countsLine(grouped)
  return line ? line : 'Nothing open'
}

/**
 * Which filter the tile hands to the rows, and which rows it means. `mine`
 * and `attention` are Home's vocabulary too (workFilter.js), so a tile here
 * and a tile on Home land on the same rows.
 */
export function tileTarget(tile) {
  return { filter: tile?.filter ?? null, view: resolveView(tile?.view) }
}

/** The scope line under the controls: "All workers · 43 open rows shown". */
export function scopeLine({ whoseLabel, shown, truncated }) {
  const parts = [whoseLabel || 'All workers']
  if (typeof shown === 'number') {
    parts.push(`${shown}${truncated ? '+' : ''} ${shown === 1 ? 'row' : 'rows'} loaded`)
  }
  return parts
}
