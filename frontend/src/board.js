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
