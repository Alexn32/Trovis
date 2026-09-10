import { looksInternal } from './askChips.js'

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

/** Wire status → table label. waiting_on_other stays the API enum. */
export function workItemStatusLabel(status) {
  switch (status) {
    case 'waiting_on_you':
      return 'Waiting on you'
    case 'waiting_on_other':
      return 'Waiting on someone'
    case 'stuck':
      return 'Stuck'
    case 'moving':
      return 'Moving'
    case 'done':
      return 'Done'
    default:
      return ''
  }
}

// Home table sort: Needs you → Stuck → waiting on someone → Moving → Done.
// waiting_on_other sits between stuck and moving so it is not an alarm column.
const WORK_STATUS_RANK = {
  waiting_on_you: 0,
  stuck: 1,
  waiting_on_other: 2,
  moving: 3,
  done: 4,
}

/** Monday table default order. Stable for unknown statuses (they sink). */
export function sortWorkItems(items) {
  return [...(items || [])].sort((a, b) => {
    const ra = WORK_STATUS_RANK[a?.status] ?? 50
    const rb = WORK_STATUS_RANK[b?.status] ?? 50
    if (ra !== rb) return ra - rb
    const ta = Date.parse(a?.updated_at || '') || 0
    const tb = Date.parse(b?.updated_at || '') || 0
    if (ta !== tb) return tb - ta
    return (b?.id || 0) - (a?.id || 0)
  })
}

/** Compact "2h" / "15m" — same grain as boardAge, from an ISO timestamp. */
export function workUpdatedLabel(iso) {
  if (!iso) return ''
  const ms = Date.now() - Date.parse(iso)
  if (Number.isNaN(ms)) return ''
  return boardAge(Math.max(0, Math.floor(ms / 1000)))
}

/**
 * Named-work display gate. Raw ids, UUIDs, snake_case, and Trovis jargon
 * stay off the table even if they slipped the API's title filter.
 */
export function isNamedWorkTitle(title) {
  const t = String(title || '').trim()
  if (!t) return false
  return !looksInternal(t)
}

/**
 * Adapt a lean /work/items row into the card shape TaskPanel expects, so the
 * Work table and Home's "What to look at" open the SAME detail pane from the
 * same row data. Shared here rather than duplicated per surface.
 */
export function itemToCard(row) {
  const ms = row?.updated_at ? Date.now() - Date.parse(row.updated_at) : NaN
  return {
    id: row?.id,
    title: row?.title,
    holder_name: row?.holder?.name || '',
    holder_type: row?.holder?.kind === 'human' ? 'human' : 'agent',
    is_yours: row?.status === 'waiting_on_you',
    age_seconds: Number.isNaN(ms) ? null : Math.max(0, Math.floor(ms / 1000)),
    standing: false,
    standing_reason: null,
  }
}

const HOLDER_KIND_PREFIX = {
  human: 'Person',
  agent: 'Agent',
  tool: 'Tool',
}

/**
 * Monday table holder cell. When lean `holder.kind` is present, prefix
 * You / Person / Agent / Tool, then the name. Missing or unknown kind
 * stays name-only. `status === 'waiting_on_you'` is the "You" signal.
 */
export function holderLabel(holder, status) {
  const name = String(holder?.name || '').trim()
  const kind = holder?.kind
  if (!kind) return name
  const prefix = status === 'waiting_on_you' ? 'You' : HOLDER_KIND_PREFIX[kind]
  if (!prefix) return name
  if (!name || (prefix === 'You' && /^you$/i.test(name))) return prefix
  return `${prefix} ${name}`
}

// ---------------------------------------------------------------------------
// Work home: kinds of work
// ---------------------------------------------------------------------------

/** The bucket unmatched work groups under. Never a hidden row. */
export const OTHER_KIND = 'Other work'

export function matchesKind(row, kindName) {
  if (!kindName) return true
  const name = String(row?.workflow_name || '').trim() || OTHER_KIND
  return name === kindName
}

const PATH_ORDER = ['agent', 'tool', 'human']
const PATH_LABEL = { agent: 'Agent', tool: 'Tool', human: 'Person' }

/**
 * The spine for one kind of work, inferred from the rows already loaded.
 *
 * Each node is a kind of holder actually seen, plus the exceptions sitting on
 * it. Null when fewer than two kinds of holder appear: with one holder there
 * is no path, only a holder.
 */
export function kindPath(items) {
  const nodes = new Map()
  let doneCount = 0
  for (const it of items || []) {
    if (!isNamedWorkTitle(it?.title) || it?.id == null) continue
    if (it.status === 'done') {
      doneCount += 1
      continue
    }
    const kind = it?.holder?.kind
    if (!PATH_ORDER.includes(kind)) continue
    let n = nodes.get(kind)
    if (!n) {
      n = { kind, label: PATH_LABEL[kind], names: new Set(), waiting: 0, stuck: 0 }
      nodes.set(kind, n)
    }
    const name = String(it?.holder?.name || '').trim()
    if (name) n.names.add(name)
    if (it.status === 'stuck') n.stuck += 1
    else if (it.status === 'waiting_on_you' || it.status === 'waiting_on_other') n.waiting += 1
  }

  const path = PATH_ORDER.filter((k) => nodes.has(k)).map((k) => {
    const n = nodes.get(k)
    return {
      kind: n.kind,
      // A tool node is worth naming — "Stripe" says more than "Tool" — but
      // only when the work all sits on the same one.
      label: n.kind === 'tool' && n.names.size === 1 ? [...n.names][0] : n.label,
      waiting: n.kind === 'human' ? n.waiting : 0,
      stuck: n.stuck,
    }
  })
  // One holder is not a path.
  if (path.length < 2) return null
  if (doneCount > 0) path.push({ kind: 'done', label: 'Done', waiting: 0, stuck: 0 })
  return path
}

/**
 * Finished and stuck work for this kind, newest first — the technical band.
 *
 * `Sent back` is not inferable from a list row (it lives in the item's own
 * history), so a closed job reads as Done and an open one that cannot move
 * reads as Stuck. Naming an outcome we did not observe would be worse than
 * naming the two we did.
 *
 * Takes CLOSED rows only — the `status=done` payload. Pass it open rows and
 * it will happily report a live stuck job as a past run, which is the one
 * thing this band must not do; the caller owns that guarantee because a lean
 * row carries no "closed" flag to check here. Ids are still de-duplicated:
 * a run listed twice reads as two runs.
 */
export function pastRuns(finishedItems, limit = 8) {
  const items = finishedItems
  const seen = new Set()
  const rows = (items || [])
    .filter((it) => isNamedWorkTitle(it?.title) && it?.id != null)
    .filter((it) => it.status === 'done' || it.status === 'stuck')
    .filter((it) => {
      if (seen.has(it.id)) return false
      seen.add(it.id)
      return true
    })
    .map((it) => ({
      id: it.id,
      title: it.title,
      result: it.status === 'done' ? 'Done' : 'Stuck',
      status: it.status,
      at: it.updated_at || null,
      // Work language, already on the row. Never a stack trace.
      reason: it.status === 'stuck' ? String(it.whats_next || '').trim() : '',
      item: it,
    }))
  rows.sort((a, b) => (Date.parse(b.at || '') || 0) - (Date.parse(a.at || '') || 0))
  return rows.slice(0, limit)
}

/** The one live thing worth naming on a kind page: what needs a person most. */
export function hottestOpen(items) {
  const open = (items || []).filter(
    (it) => isNamedWorkTitle(it?.title) && it?.id != null && it.status !== 'done',
  )
  if (open.length === 0) return null
  const rank = { stuck: 0, waiting_on_you: 1, waiting_on_other: 2, moving: 3 }
  const sorted = [...open].sort((a, b) => {
    const ra = rank[a.status] ?? 9
    const rb = rank[b.status] ?? 9
    if (ra !== rb) return ra - rb
    // Oldest first inside a bucket: age is the urgency signal.
    return (Date.parse(a.updated_at || '') || 0) - (Date.parse(b.updated_at || '') || 0)
  })
  return sorted[0]
}
