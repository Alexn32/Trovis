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

/** Max rows in "What to look at" before we defer to the Work tab. */
export const LOOK_AT_MAX = 7

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
 * The "What to look at" queue: needs-you first, then needs-attention, capped.
 * Returns `hidden` so the caller can say "+N more in Work" instead of
 * silently dropping rows.
 */
export function lookAtRows(items, { nowMs = Date.now(), max = LOOK_AT_MAX } = {}) {
  const { needsYou, needsAttention } = partitionLookAt(items, nowMs)
  const all = [...needsYou, ...needsAttention]
  return {
    rows: all.slice(0, max),
    hidden: Math.max(0, all.length - max),
    needsYouCount: needsYou.length,
    needsAttentionCount: needsAttention.length,
  }
}

/**
 * Briefing bullets: at most 3 needs-you, 3 stuck, 2 moving. Each is a real
 * named item so the reader can click straight through. "Moving" is the
 * optional noteworthy line and is omitted entirely when empty.
 */
export function briefingBullets(items, nowMs = Date.now()) {
  const { needsYou, needsAttention } = partitionLookAt(items, nowMs)
  const moving = (items || [])
    .filter((it) => isShowable(it) && it.status === 'moving')
    .sort(
      (a, b) =>
        (Date.parse(b.updated_at || '') || 0) - (Date.parse(a.updated_at || '') || 0),
    )
  return {
    needsYou: needsYou.slice(0, 3),
    stuck: needsAttention.slice(0, 3),
    moving: moving.slice(0, 2),
  }
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
 * The cost pulse is a whisper, and only when there is something to whisper.
 * Hidden at $0 (and at sub-cent noise) so a quiet day shows no cost chrome.
 */
export function showCostPulse(cost) {
  if (!cost) return false
  return (Number(cost.today) || 0) >= 0.01
}

/**
 * Home is genuinely empty — no work, no activity — as opposed to still
 * loading or failing. Only true when every section came back and came back
 * empty; a failed section returns false so we show its Retry, not an
 * "all set up" story that isn't earned.
 */
export function isFirstRun({ overview, items, feed }) {
  if (!overview || !items || !feed) return false
  const noWork = (Number(overview.open) || 0) === 0 && items.length === 0
  return noWork && feed.length === 0
}
