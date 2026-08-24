// Work board presentation logic — pure functions, no React, no DOM, so
// node --test can cover the copy without a component framework.
//
// THE LANGUAGE RULE, enforced by test: nothing a user reads here may contain
// "loop", "workloop", "possession", "segment", "station" or "handoff". The
// board is for someone who has never heard of Trovis. Work, tasks, steps,
// waiting on, with, done.

/** Compact time-in-state. Age IS the urgency signal on this board, so it is
 * never rounded away to "a while". */
export function boardAge(seconds) {
  if (seconds == null) return ''
  const s = Math.max(0, Math.floor(seconds))
  if (s < 60) return 'just now'
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h`
  const d = Math.floor(h / 24)
  return `${d}d`
}

/** Cost, only when there is one. A card should not carry "$0.00" — that is
 * noise pretending to be information. */
export function boardCostLabel(usd) {
  const n = Number(usd) || 0
  if (n < 0.01) return ''
  return `$${n.toFixed(2)}`
}

/** The panel's one-line subtitle: who has it, and for how long. */
export function holderLine(card) {
  const age = boardAge(card?.age_seconds)
  const who = card?.holder_name || 'an agent'
  const forPart = age && age !== 'just now' ? ` for ${age}` : ''
  if (card?.is_yours) return `Waiting on you${forPart}`
  if (card?.holder_type === 'human') return `With ${who}${forPart}`
  if (card?.waiting_on) return `${who} · waiting on ${card.waiting_on}${forPart}`
  return `With ${who}${forPart}`
}

/**
 * Empty states. Three genuinely different situations, and conflating them is
 * how a new account gets told "Nothing is stuck. All loops are moving." when
 * the truth is nothing is connected yet.
 */
export function boardEmpty(board) {
  if (!board) return null
  if (board.total > 0) return null
  if (!board.has_agents) {
    return {
      lead: 'No work yet — nothing is connected.',
      sub: 'Connect an agent and its work shows up here on its own. You will not have to enter any of it.',
      cta: 'Connect an agent',
    }
  }
  return {
    lead: 'No work yet.',
    sub: 'Your agents are connected. The moment one of them starts a task it appears here, moves across as it progresses, and lands in Done — by itself.',
    cta: null,
  }
}

// ---------------------------------------------------------------------------
// Level 1 — the Work screen: one card per kind of work
// ---------------------------------------------------------------------------

/**
 * A kind-of-work card's rollup, as display segments. All four counts are
 * always present — the shape teaches what a kind of work is — but a zero is
 * muted and only a NONZERO waiting/stuck count gets semantic color. A calm
 * card is a quiet card.
 *
 * Returns [{ label, value, tone }] where tone is 'warn' | 'stuck' | 'muted'.
 * Cost is appended only when nonzero.
 */
export function kindRollup(card) {
  const c = card || {}
  const seg = (value, label, tone) => ({
    value: value || 0,
    label,
    tone: value ? tone : 'muted',
  })
  const parts = [
    seg(c.in_motion, 'in motion', 'live'),
    seg(c.waiting_person, c.waiting_person === 1 ? 'waiting on a person' : 'waiting on a person', 'warn'),
    seg(c.stuck, 'stuck', 'stuck'),
    seg(c.done_today, 'done today', 'muted'),
  ]
  // Always-on work reads as a quiet count — never colored, never an alarm.
  // Its abnormal states (waiting/stuck) have already left "ongoing" and show
  // in those counts above.
  if (c.ongoing) parts.push({ value: c.ongoing, label: 'ongoing', tone: 'muted' })
  const cost = boardCostLabel(c.cost_today)
  if (cost) parts.push({ value: null, label: `${cost} today`, tone: 'muted' })
  return parts
}

// The board's quiet line for a kind's always-on work: "3 ongoing, running
// normally". Empty when there is none. Human words only.
export function ongoingLine(count) {
  const n = count || 0
  if (n <= 0) return ''
  return `${n} ongoing, running normally`
}

// True when a kind of work has anything a person needs to look at.
export function kindNeedsAttention(card) {
  return (card?.waiting_person || 0) + (card?.stuck || 0) > 0
}

// The one quiet line under "Other work" when its undeclared pile is the
// biggest — never a nag, just an offer. Empty string when not warranted.
export function otherNudge(card) {
  return card?.suggest_declare
    ? 'A lot of work here — declare a workflow to organize it'
    : ''
}

// Level-1 empty state, same three-way honesty as the board.
export function workScreenEmpty(summary) {
  if (!summary) return null
  if ((summary.total || 0) > 0) return null
  if (!summary.has_agents) {
    return {
      lead: 'No work yet — nothing is connected.',
      sub: 'Connect an agent and its work shows up here on its own, sorted into the kinds of work your company does.',
      cta: 'Connect an agent',
    }
  }
  return {
    lead: 'No work yet.',
    sub: 'Your agents are connected. As they start working, each kind of work appears here with a live count of what is moving, waiting, and done.',
    cta: null,
  }
}
