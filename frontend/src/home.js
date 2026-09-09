// Home v2 composition — pure functions, no React, so the rules that decide
// what a person sees on Home are unit-testable without a renderer.
//
// Home reads the SAME work truth as the Work tab: named items from
// GET /work/items and counts from GET /work/overview. It must never call
// /work/board, /work/summary, or /agents — those are what starved the single
// replica. Nothing here fetches; callers pass data in.
//
// Vocabulary: everything returned from this module is read by someone who has
// never heard of Trovis. No ids, no "loop"/"handoff"/"station" — human titles
// and plain sentences only.

import { isNamedWorkTitle } from './board.js'

/**
 * How old a "waiting on someone else" item must be before it counts as
 * needing attention.
 *
 * This MIRRORS the server: loops.STALL_THRESHOLD_S (default 4h), which
 * /work/overview uses to build its needs_attention count. It is a mirror, not
 * a shared source — an operator who overrides LOOP_STALL_THRESHOLD_S on the
 * server would drift from this constant. That is why the section's COUNTS
 * come from /work/overview (authoritative) and only the ROWS are classified
 * here; a drift changes which rows we surface, never the number we print.
 */
export const ATTENTION_AGE_S = 14400

/** Age of an item in seconds, or null when it has no usable timestamp. */
export function ageSeconds(iso, nowMs = Date.now()) {
  if (!iso) return null
  const t = Date.parse(iso)
  if (Number.isNaN(t)) return null
  return Math.max(0, Math.floor((nowMs - t) / 1000))
}

/**
 * A named item is showable on Home only when its title reads like something a
 * person wrote. The API already filters generated titles and "Task from X"
 * shells; this is the second gate so a shell that slips through never lands
 * in a briefing bullet.
 */
export function isShowable(item) {
  return Boolean(item && item.id != null && isNamedWorkTitle(item.title))
}

/** waiting_on_other that has aged past the attention threshold. */
export function isAgingWait(item, nowMs = Date.now()) {
  if (!item || item.status !== 'waiting_on_other') return false
  const age = ageSeconds(item.updated_at, nowMs)
  return age !== null && age >= ATTENTION_AGE_S
}

/**
 * Split named items into the two Home buckets, using the Work v1.1 rule:
 *   needs you       = waiting_on_you
 *   needs attention = stuck + AGING waiting_on_other (never waiting_on_you)
 * Newest first inside each bucket. Done items never appear.
 */
export function partitionLookAt(items, nowMs = Date.now()) {
  const needsYou = []
  const needsAttention = []
  for (const it of items || []) {
    if (!isShowable(it) || it.status === 'done') continue
    if (it.status === 'waiting_on_you') needsYou.push(it)
    else if (it.status === 'stuck' || isAgingWait(it, nowMs)) needsAttention.push(it)
  }
  const recent = (a, b) =>
    (Date.parse(b.updated_at || '') || 0) - (Date.parse(a.updated_at || '') || 0)
  needsYou.sort(recent)
  needsAttention.sort(recent)
  return { needsYou, needsAttention }
}

/**
 * The Work card's four buckets, as the Work page itself splits them.
 *
 *   Moving  — in flight, nobody is blocked
 *   Waiting — every wait, on you OR on someone else. Deliberately a DIFFERENT
 *             cut from "needs attention": a fresh wait belongs here but is
 *             not yet something to act on.
 *   Stuck   — cannot move
 *   Done    — finished this week
 *
 * Moving/Waiting/Stuck are counted from the loaded page of items, so on a
 * truncated list they are a floor (the caller marks them "+"). Done comes
 * from /work/overview.completed_week, which is authoritative and is the same
 * number the Work tab prints — the two surfaces must not disagree.
 */
export function workSplit(items, overview) {
  const counts = { moving: 0, waiting: 0, stuck: 0, done: 0 }
  for (const it of items || []) {
    if (!isShowable(it)) continue
    if (it.status === 'moving') counts.moving += 1
    else if (it.status === 'waiting_on_you' || it.status === 'waiting_on_other') {
      counts.waiting += 1
    } else if (it.status === 'stuck') counts.stuck += 1
  }
  counts.done = Number(overview?.completed_week) || 0
  return counts
}

/** English list: "A", "A and B", "A, B and C". */
function joinCounts(parts) {
  if (parts.length === 0) return ''
  if (parts.length === 1) return parts[0]
  return `${parts.slice(0, -1).join(', ')} and ${parts[parts.length - 1]}`
}

function plural(n, one, many) {
  return `${n} ${n === 1 ? one : many}`
}

/**
 * The briefing's lead sentence — the state of today in one line.
 *
 * Composed from counts rather than written by Claude, so it is always
 * truthful, always plain, and still renders when the Claude call fails. Pass
 * the /work/overview counts when they loaded; they are the same numbers the
 * Work tab prints.
 *
 * Returns '' when we have no work signal at all — the caller then leans on
 * the narrative line instead of inventing a state.
 */
export function briefingLead(counts) {
  if (!counts) return ''
  const needsYou = Number(counts.needs_you) || 0
  const attention = Number(counts.needs_attention) || 0
  const open = Number(counts.open) || 0

  const parts = []
  if (needsYou > 0) {
    parts.push(
      `${plural(needsYou, 'thing', 'things')} ${needsYou === 1 ? 'needs' : 'need'} you`,
    )
  }
  if (attention > 0) {
    // "need attention", not "are stuck" — the bucket includes work that is
    // merely waiting too long on a person, which is not the same as stuck.
    parts.push(`${attention} ${attention === 1 ? 'needs' : 'need'} attention`)
  }
  if (parts.length > 0) {
    // "2 things need you and 1 is stuck."
    return `${joinCounts(parts)}.`
  }
  if (open > 0) {
    return `Nothing needs you. ${plural(open, 'thing', 'things')} in progress.`
  }
  return 'Nothing needs you right now.'
}

/**
 * "As of 9:42 AM" for the briefing footer. Empty when the server sent no
 * generated_at, so the footer omits the segment rather than printing "As of —".
 */
export function asOfLabel(iso, locale = undefined) {
  if (!iso) return ''
  const t = Date.parse(iso)
  if (Number.isNaN(t)) return ''
  return `As of ${new Date(t).toLocaleTimeString(locale, {
    hour: 'numeric',
    minute: '2-digit',
  })}`
}

/**
 * Nothing is connected yet — as opposed to still loading, failing, or simply
 * having a quiet day.
 *
 * `agents` is the agent list /dashboard/cost already returns, so this costs no
 * extra request. It is the honest signal: an account can have agents running
 * and no named work yet, and that is a quiet day, not a first run.
 *
 * Only true when every input came back and came back empty; a failed section
 * returns false so we show its Retry, not an "all set up" story that isn't
 * earned.
 */
export function isFirstRun({ overview, items, agents }) {
  if (!overview || !items || !agents) return false
  const noWork = (Number(overview.open) || 0) === 0 && items.length === 0
  return noWork && agents.length === 0
}

// ---------------------------------------------------------------------------
// Home as the worker's opening: what is waiting on ME, is anything stuck, is
// the rest of the day moving.
// ---------------------------------------------------------------------------

/**
 * Your desk: work waiting on the SIGNED-IN user and nothing else.
 *
 * `waiting_on_you` is resolved by the server against the session identity
 * (database._attach_awaiting_human), which is the same resolution the Work
 * board uses — the client never guesses who "you" is. So this is deliberately
 * NOT "needs a human": a teammate's wait is their desk, not yours, and
 * org-wide stuck work belongs under Trovis noticed.
 *
 * Newest first: a wait that just landed is the one you have not seen.
 */
export function deskItems(items) {
  const rows = (items || []).filter(
    (it) => isShowable(it) && it.status === 'waiting_on_you',
  )
  return rows.sort(
    (a, b) => (Date.parse(b.updated_at || '') || 0) - (Date.parse(a.updated_at || '') || 0),
  )
}

/**
 * Work that cannot move at all, OLDEST first — on this list age is the whole
 * story, so the longest-stuck thing leads.
 */
export function stuckItems(items) {
  const rows = (items || []).filter((it) => isShowable(it) && it.status === 'stuck')
  return rows.sort(
    (a, b) => (Date.parse(a.updated_at || '') || 0) - (Date.parse(b.updated_at || '') || 0),
  )
}

/**
 * The shape of the day, in one sentence, from counts.
 *
 * TEMPLATE, never a model: it is on the first paint, so it cannot wait on a
 * Claude call, and it must be true before anything else has loaded. It carries
 * NO numbers and NO money on purpose — the proof strip owns every count on
 * Home, and two places printing the same figure is how they end up disagreeing.
 *
 * `hasRecord` is false while we are still reading; the caller renders nothing
 * rather than a sentence it might have to take back.
 */
export function dayShape({ hasRecord, connected, deskCount, stuckCount }) {
  if (!hasRecord) return ''
  if (!connected) return 'Nothing is connected yet.'
  const stuck = (Number(stuckCount) || 0) > 0
  if ((Number(deskCount) || 0) === 0) {
    return stuck
      ? 'Nothing is waiting on you. Some work is stuck.'
      : 'Nothing is waiting on you. Work is moving.'
  }
  return stuck
    ? 'You have work waiting. Some work is stuck.'
    : 'You have work waiting. The rest is moving.'
}

/** Local clock for a recent timestamp: "4pm", "4:30pm". */
export function clockLabel(iso, locale = undefined) {
  const t = Date.parse(iso || '')
  if (Number.isNaN(t)) return ''
  const d = new Date(t)
  const opts = { hour: 'numeric', ...(d.getMinutes() ? { minute: '2-digit' } : {}) }
  return d.toLocaleTimeString(locale, opts).replace(/\s/g, '').toLowerCase()
}

/**
 * "since 4pm" while that still means today, "for 3d" once a clock time would
 * be misleading. Empty when the timestamp is unusable — the caller then says
 * "stuck" with no time rather than "stuck since —".
 */
export function stuckSince(iso, nowMs = Date.now(), locale = undefined) {
  const age = ageSeconds(iso, nowMs)
  if (age === null) return ''
  if (age >= 86400) {
    const d = Math.floor(age / 86400)
    return `for ${d}d`
  }
  const clock = clockLabel(iso, locale)
  return clock ? `since ${clock}` : ''
}

/**
 * The counts on the proof strip — the ONE place on Home that prints numbers.
 *
 *   moving  — in flight, nobody blocked
 *   waiting — EVERYONE's waits, yours and your teammates'. Deliberately a
 *             wider cut than the desk above it, which is only yours.
 *   stuck   — cannot move
 *   done    — finished today (not this week: the strip says "done today")
 *
 * All four are counted off the loaded page of items, so on a truncated list
 * they are a floor and the caller marks them "+". They come from the same
 * rows the sections above render, which is what keeps the page from
 * contradicting itself.
 */
export function proofCounts(items, nowMs = Date.now()) {
  const counts = { moving: 0, waiting: 0, stuck: 0, done: 0 }
  const dayStart = new Date(nowMs)
  dayStart.setHours(0, 0, 0, 0)
  const startMs = dayStart.getTime()
  for (const it of items || []) {
    if (!isShowable(it)) continue
    if (it.status === 'moving') counts.moving += 1
    else if (it.status === 'waiting_on_you' || it.status === 'waiting_on_other') {
      counts.waiting += 1
    } else if (it.status === 'stuck') counts.stuck += 1
    else if (it.status === 'done') {
      const t = Date.parse(it.updated_at || '')
      if (!Number.isNaN(t) && t >= startMs) counts.done += 1
    }
  }
  return counts
}

/** The stuck line, composed from the record with no model. '' when none. */
export function stuckNotice(items, nowMs = Date.now(), locale = undefined) {
  const stuck = stuckItems(items)
  if (stuck.length === 0) return ''
  const since = stuckSince(stuck[0].updated_at, nowMs, locale)
  const tail = since ? ` ${since}` : ''
  if (stuck.length === 1) return `${stuck[0].title} is stuck${tail}`
  return `${stuck.length} tasks stuck${tail}`
}

/**
 * Trovis noticed — at most a few lines, every one of them derived from the
 * record rather than written by a model on the critical path.
 *
 * The stuck line always leads when anything is stuck. Agent-health lines come
 * from /dashboard/attention, which is already computed and already honest
 * (quiet agents, error clusters). Cost anomalies are deliberately absent: a
 * spend blip is not something to act on, and the strip already shows the day's
 * money.
 *
 * Each line carries where it goes, so nothing here is a dead tap.
 */
export function noticedLines({ items, attention, nowMs = Date.now(), limit = 3, locale }) {
  const lines = []
  const stuck = stuckItems(items)
  if (stuck.length > 0) {
    lines.push({
      key: 'stuck',
      kind: 'stuck',
      text: stuckNotice(items, nowMs, locale),
      // One stuck thing opens itself; several open Work already filtered.
      target: stuck.length === 1 ? { to: 'work-item', item: stuck[0] } : { to: 'work', filter: 'stuck' },
    })
  }
  for (const a of attention || []) {
    if (lines.length >= limit) break
    if (!a || !a.agent) continue
    const title = String(a.title || '').trim()
    if (!title) continue
    lines.push({
      key: `agent:${a.service_name || a.agent}:${a.agent_id || 'main'}`,
      kind: a.severity === 'critical' ? 'critical' : 'warning',
      text: `${a.agent} — ${title}`,
      target: { to: 'agent', row: a },
    })
  }
  return lines.slice(0, limit)
}
