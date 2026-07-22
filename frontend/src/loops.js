// Workloop presentation logic — pure functions, no React, no DOM.
// Everything user-facing about a loop (title fallback, state labels,
// lifecycle-event sentences, sort order, close-button visibility) lives
// here in one place so copy stays consistent and node --test can cover it
// without a component framework.

export const LOOP_STATES = [
  'open',
  'working',
  'awaiting_human',
  'awaiting_agent',
  'stalled',
  'done',
  'abandoned',
]

// States that mean "a person should look at this" — warning treatment and
// attention-first sorting.
export const ATTENTION_STATES = ['stalled', 'awaiting_human']
export const TERMINAL_STATES = ['done', 'abandoned']

// Backend timestamps are "YYYY-MM-DD HH:MM:SS" (SQLite) or ISO (Postgres).
// Normalize the space so Date.parse works everywhere.
export function parseTs(v) {
  if (!v) return NaN
  const t = Date.parse(String(v).replace(' ', 'T'))
  return Number.isNaN(t) ? NaN : t
}

// Compact age: "45s", "12m", "3h", "2d".
export function fmtAge(seconds) {
  const s = Math.max(0, Math.floor(seconds))
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h`
  return `${Math.floor(h / 24)}d`
}

// Long-form age for the Stuck headline: "3 hours", "2 days", "40 minutes".
export function fmtAgeLong(seconds) {
  const s = Math.max(0, Math.floor(seconds))
  const unit = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`
  if (s < 60) return 'moments'
  const m = Math.floor(s / 60)
  if (m < 60) return unit(m, 'minute')
  const h = Math.floor(m / 60)
  if (h < 24) return unit(h, 'hour')
  return unit(Math.floor(h / 24), 'day')
}

function fmtRelDate(iso, nowMs) {
  const t = parseTs(iso)
  if (Number.isNaN(t)) return ''
  const m = Math.floor((nowMs - t) / 60000)
  if (m < 1) return 'just now'
  if (m < 60) return `${m}m ago`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ago`
  return `${Math.floor(h / 24)}d ago`
}

// Seconds since the loop last moved. Prefers the backend-computed
// stalled_for_s (present on /loops/stalled rows), else derives from
// last_event_unix (unix nanoseconds).
export function loopAgeSeconds(loop, nowMs = Date.now()) {
  if (typeof loop?.stalled_for_s === 'number') return loop.stalled_for_s
  const last = loop?.last_event_unix
  if (!last) return null
  return Math.max(0, Math.floor((nowMs * 1e6 - last) / 1e9))
}

// closed_at - created_at, in seconds. Null when either end is missing.
export function loopDurationSeconds(loop) {
  const a = parseTs(loop?.created_at)
  const b = parseTs(loop?.closed_at)
  if (Number.isNaN(a) || Number.isNaN(b)) return null
  return Math.max(0, Math.floor((b - a) / 1000))
}

function fmtDurationShort(seconds) {
  if (seconds == null) return null
  const s = Math.max(0, Math.floor(seconds))
  // Round up, don't suppress: "done · 0 sec" is technically true for a
  // loop created and closed in one ingest batch, but "under a minute"
  // states it legibly without hiding the duration.
  if (s < 60) return 'under a minute'
  const m = Math.floor(s / 60)
  if (m < 60) return `${m} min`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h`
  return `${Math.floor(h / 24)}d`
}

// Line-1 title. Never "Untitled": when the loop has no generated title,
// fall back to "<agent> run · <relative date>".
export function loopTitle(loop, nowMs = Date.now()) {
  if (loop?.title) return loop.title
  const agent =
    loop?.agent_id && loop.agent_id !== 'main' ? loop.agent_id : loop?.service_name || 'agent'
  const rel = fmtRelDate(loop?.created_at, nowMs)
  return rel ? `${agent} run · ${rel}` : `${agent} run`
}

// Cost chip text. Omitted (null) when there is no meaningful cost — the
// spec says never render "$0.00".
export function loopCostLabel(v) {
  const n = Number(v)
  if (!Number.isFinite(n) || n < 0.005) return null
  return `$${n.toFixed(2)}`
}

/**
 * State → presentation. tone drives the CSS class:
 *   'warning' — status warning color + attention left-border
 *   'live'    — quiet running indicator (pulsing dot)
 *   'muted'   — terminal / waiting-on-agent, no color emphasis
 * label is the exact user-facing string.
 */
export function loopStateMeta(loop, nowMs = Date.now()) {
  const state = loop?.cached_state
  const age = loopAgeSeconds(loop, nowMs)
  const withAge = (base) => (age != null ? `${base} · ${fmtAge(age)}` : base)
  switch (state) {
    case 'awaiting_human':
      return { label: withAge('waiting on you'), tone: 'warning', attention: true }
    case 'stalled':
      return { label: withAge('stalled'), tone: 'warning', attention: true }
    case 'awaiting_agent':
      return { label: withAge('waiting on an agent'), tone: 'muted', attention: false }
    case 'working':
    case 'open':
      return { label: 'running', tone: 'live', attention: false }
    case 'done': {
      const dur = fmtDurationShort(loopDurationSeconds(loop))
      return { label: dur ? `done · ${dur}` : 'done', tone: 'muted', attention: false }
    }
    case 'abandoned':
      return { label: 'abandoned', tone: 'muted', attention: false }
    default:
      return { label: String(state || '').replace(/_/g, ' ') || 'unknown', tone: 'muted', attention: false }
  }
}

// Stuck-view headline: the age IS the fact. "waiting on you for 3 hours".
export function loopStuckHeadline(loop, nowMs = Date.now()) {
  const age = loopAgeSeconds(loop, nowMs)
  const forPart = age != null ? ` for ${fmtAgeLong(age)}` : ''
  return loop?.cached_state === 'awaiting_human'
    ? `waiting on you${forPart}`
    : `stalled${forPart}`
}

// Attention first: stalled/awaiting_human float to the top, oldest stall
// first; everything else keeps the backend's newest-first order below.
// (This deliberately overrides the flat feed's pure-chronological paradigm.)
export function sortLoopsAttentionFirst(loops, nowMs = Date.now()) {
  const list = Array.isArray(loops) ? loops : []
  const attention = list
    .filter((l) => ATTENTION_STATES.includes(l?.cached_state))
    .sort((a, b) => (loopAgeSeconds(b, nowMs) ?? 0) - (loopAgeSeconds(a, nowMs) ?? 0))
  const rest = list.filter((l) => !ATTENTION_STATES.includes(l?.cached_state))
  return [...attention, ...rest]
}

// "Mark done" is only offered to a signed-in human (the backend refuses
// api-key auth with a 403) and only for loops that are actually waiting.
export function showMarkDone(loop, hasSessionUser) {
  return Boolean(hasSessionUser) && ATTENTION_STATES.includes(loop?.cached_state)
}

// ---------------------------------------------------------------------------
// Lifecycle events → plain-English sentences. One map, no scattered strings,
// and no raw event-type identifiers ever reach the UI.
// ---------------------------------------------------------------------------

const HANDOFF_TARGET = { to_human: 'a human', to_agent: 'another agent' }

export const LIFECYCLE_SENTENCES = {
  loop_opened: () => 'Started',
  handoff_initiated: (p) => {
    // target_name is the backend's org-scoped resolution of target_id to a
    // real person ("Handed to Sarah"). Absent → the honest generic.
    const who = p?.target_name || HANDOFF_TARGET[p?.direction] || 'someone'
    const reason = p?.reason ? ` — ${p.reason}` : ''
    return `Handed to ${who}${reason}`
  },
  handoff_accepted: () => 'Handoff accepted',
  handoff_completed: () => 'Handoff completed',
  handoff_declined: () => 'Handoff declined',
  loop_closed: (p) => {
    if (p?.reason === 'abandoned') return 'Closed automatically — no activity for 2 days'
    if (p?.reason === 'closed_by_user') return 'Marked done'
    if (p?.reason === 'completed_by_agent') {
      return p?.detail ? `Closed by agent — ${p.detail}` : 'Closed by agent — completed'
    }
    return 'Closed'
  },
  stall_detected: () => 'Stalled — no recent activity',
}

export function lifecycleSentence(event) {
  const fn = LIFECYCLE_SENTENCES[event?.type]
  if (fn) return fn(event?.payload || {})
  // Unknown future types degrade to readable words, never raw identifiers.
  return String(event?.type || '').replace(/_/g, ' ')
}

// "service:agent" composite actor → display pieces.
export function splitActor(actor) {
  const s = String(actor || '')
  const i = s.indexOf(':')
  if (i === -1) return { service: s, agent: 'main' }
  return { service: s.slice(0, i), agent: s.slice(i + 1) || 'main' }
}
