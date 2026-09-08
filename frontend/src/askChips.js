// Suggestion chips for the Ask panel (⌘K). Pure functions so node --test
// can lock the copy and the stuck-title truncate without a component.

const FORBIDDEN = /\b(loops?|workloops?|possession|segments?|stations?|handoffs?)\b/i

/** Middle of the 40–48 char window Design locked for stuck titles. */
export const TITLE_MAX = 44

export const WAITING_ON_ME = "What's waiting on me?"
export const WHATS_STUCK = "What's stuck?"

export const FALLBACK_CHIPS = [
  { label: WAITING_ON_ME, query: WAITING_ON_ME },
  { label: WHATS_STUCK, query: WHATS_STUCK },
]

/** True for empty, numeric ids, UUIDs, snake_case, or Trovis jargon. */
export function looksInternal(s) {
  const t = String(s || '').trim()
  if (!t) return true
  if (/^\d+$/.test(t)) return true
  if (/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(t)) return true
  if (/^[a-z][a-z0-9]*(_[a-z0-9]+)+$/.test(t)) return true
  if (FORBIDDEN.test(t)) return true
  return false
}

/** One-line title for a chip: 40–48 chars, ellipsis, prefer a word break. */
export function clipTitle(s, max = TITLE_MAX) {
  const t = String(s || '').trim()
  if (t.length <= max) return t
  const cut = t.slice(0, max - 1)
  const sp = cut.lastIndexOf(' ')
  const minWord = Math.min(24, Math.floor(max * 0.55))
  const base = sp >= minWord ? cut.slice(0, sp) : cut
  return `${base.replace(/[.,;:–—\-\s]+$/, '')}…`
}

/**
 * Stuck chip: "Why is {title} stuck?" when the board has a human title,
 * otherwise the generic "What's stuck?". Click `query` keeps the full title
 * so Ask can find the task; `title` is the hover hint.
 */
export function stuckChip(card) {
  const full = (card?.title || '').trim()
  const reason = (card?.stuck_reason || '').trim()
  if (!full || looksInternal(full)) {
    return { label: WHATS_STUCK, query: WHATS_STUCK }
  }
  const noun = clipTitle(full, TITLE_MAX)
  const hint = reason && !looksInternal(reason) ? `${full} — ${reason}` : full
  return {
    label: `Why is ${noun} stuck?`,
    query: `Why is ${full} stuck?`,
    title: hint,
  }
}

/** Chip order locked with #117: waiting on me, then stuck. */
export function askSuggestions(board) {
  const stuckCol = board?.columns?.find((c) => c.key === 'stuck')
  return [
    { label: WAITING_ON_ME, query: WAITING_ON_ME },
    stuckChip(stuckCol?.cards?.[0]),
  ]
}
