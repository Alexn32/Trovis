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

/**
 * The briefing's lead sentence — the state of today in one line.
 *
 * Composed from counts rather than written by Claude, so it is always
 * truthful and still renders when the Claude call fails. It states WHICH
 * conditions hold and prints NONE of the figures: the proof strip owns every
 * number on Home, and prose repeating one is how the two drift apart.
 *
 * Every clause is read off a count that is actually > 0 — nothing here is
 * inferred from the absence of something else. ("Work is moving" used to be
 * derived from "nothing is stuck", which read as a flat lie on a day with
 * zero moving work.)
 *
 * Returns '' when we have no work signal at all — the caller then says
 * nothing rather than inventing a state.
 */
export function briefingLead(counts) {
  if (!counts) return ''
  const needsYou = Number(counts.needs_you) || 0
  const attention = Number(counts.needs_attention) || 0
  const open = Number(counts.open) || 0

  const parts = []
  if (needsYou > 0) parts.push('work is waiting on you')
  // "needs attention", not "is stuck" — the bucket includes work merely
  // waiting too long on a person, which is not the same as stuck.
  if (attention > 0) parts.push('some work needs attention')
  if (parts.length > 0) {
    const rest = open > needsYou + attention ? '. The rest is in progress.' : '.'
    return `Today, ${joinCounts(parts)}${rest}`
  }
  if (open > 0) return 'Nothing needs you. Everything open is in progress.'
  return 'Nothing needs you right now.'
}

// ---------------------------------------------------------------------------
// Fleet pulse — the highest-level thing Home says about the agents, and the
// only thing it can say without loading the roster.
// ---------------------------------------------------------------------------

/**
 * The DATA packet the pulse insight is allowed to reason over.
 *
 * Assembled from what Home ALREADY fetched — no new request — which is also
 * what keeps the pulse from contradicting the proof strip: both are rendered
 * from these same numbers in this same browser.
 *
 * A key is present only when it is PROVEN. Absent means unknown, and a zero
 * is never used to mean "we couldn't tell":
 *
 *  - counts off the items page are omitted when the page was truncated, since
 *    a floor is not a count
 *  - the previous week is omitted unless the server says we actually have
 *    that much history, so a four-day-old org is never shown a "decline"
 *  - weekly cost is omitted below a cent: an org whose spend is unattributed
 *    reads as $0.00 on everything, and a confident "$0.00 this week" is a
 *    claim about attribution we cannot make
 *
 * Nothing here is a display string. The caption is built from these numbers
 * separately, so what the model may say and what the graphic shows come from
 * one source.
 */
export function pulsePacket({ overview, items, truncated, cost, attention, agentCount }) {
  const packet = {}
  if (Number.isFinite(Number(agentCount)) && agentCount !== null) {
    packet.agents_count = Number(agentCount)
  }

  const look = fleetPulse({ agentCount, attention }).needLook
  packet.need_a_look = look.map((a) => ({ name: String(a.agent) }))

  if (overview) {
    if (Number.isFinite(Number(overview.completed_week))) {
      packet.finished_this_week = Number(overview.completed_week)
    }
    // Only when the org is old enough for last week to mean something.
    if (overview.has_prev_week && Number.isFinite(Number(overview.completed_prev_week))) {
      packet.finished_last_week = Number(overview.completed_prev_week)
    }
  }

  // A truncated page gives floors, not counts — and a floor in a packet the
  // model quotes verbatim becomes a false number on screen.
  if (Array.isArray(items) && !truncated) {
    const c = proofCounts(items)
    packet.waiting_now = c.waiting
    packet.stuck_now = c.stuck
    packet.moving_now = c.moving
  }

  // 7-day windows off the daily series /dashboard/cost already returns.
  const daily = Array.isArray(cost?.daily) ? cost.daily.map(Number) : []
  if (daily.length >= 7) {
    const sum = (xs) => Math.round(xs.reduce((a, b) => a + (Number(b) || 0), 0) * 100) / 100
    const thisWeek = sum(daily.slice(-7))
    if (thisWeek >= 0.01) {
      packet.cost_this_week = thisWeek
      if (daily.length >= 14) {
        const lastWeek = sum(daily.slice(-14, -7))
        if (lastWeek >= 0.01) packet.cost_last_week = lastWeek
      }
    }
  }
  return packet
}

/**
 * Which graphic to draw, decided HERE and not by the server.
 *
 * MIRRORS pulse.choose_graphic — keep the two in step. The client owns this
 * because the pulse must draw immediately: the graphic is a fact about the
 * packet the browser is already holding, so waiting on a round trip to learn
 * it means showing nothing when the endpoint is slow, unreachable, or has no
 * model key behind it. (It did exactly that: `graphic` was read off the
 * insight response, so a 404 left the pulse with no chart at all.)
 *
 * A model choice only wins when it names a series we can actually draw. Its
 * "none" is not honoured over our own reading — the server reached "none"
 * through this same order, so re-deriving agrees with it, and treating a
 * missing answer as "none" is what suppressed the chart.
 */
export function chooseGraphic(packet, modelChoice) {
  const p = packet || {}
  const drawable = (g) => pulseGraphic(g, p) !== null
  if (modelChoice && modelChoice !== 'none' && drawable(modelChoice)) return modelChoice
  if (drawable('week_finished')) return 'week_finished'
  if (drawable('week_stuck')) return 'week_stuck'
  // need_a_look draws no chart; it is still the honest answer for "what would
  // this show", and pulseGraphic returns null for it.
  if ((p.need_a_look || []).length > 0) return 'need_a_look'
  return 'none'
}

/**
 * What the graphic draws and what its caption says, from the packet alone.
 *
 * Returns null when the chosen series is not in the packet — the caller then
 * renders no graphic rather than an empty frame. The caption quotes the raw
 * numbers, so the picture and the words cannot drift apart.
 */
export function pulseGraphic(kind, packet) {
  const p = packet || {}
  if (kind === 'week_finished') {
    if (!('finished_this_week' in p) || !('finished_last_week' in p)) return null
    // Two empty bars prove nothing. A zero week against a nonzero one is real
    // news; zero against zero is just an org with no finished work yet, which
    // the strip already says.
    if (!p.finished_this_week && !p.finished_last_week) return null
    return {
      kind,
      bars: [
        { label: 'last week', value: p.finished_last_week },
        { label: 'this week', value: p.finished_this_week },
      ],
      caption: `${p.finished_this_week} finished this week · ${p.finished_last_week} last week`,
      filter: 'done',
    }
  }
  // Reachable and tested, but no packet carries these keys today — see the
  // GRAPHICS note in pulse.py. It returns null rather than an empty frame.
  if (kind === 'week_stuck') {
    if (!('stuck_this_week' in p) || !('stuck_last_week' in p)) return null
    if (!p.stuck_this_week && !p.stuck_last_week) return null
    return {
      kind,
      bars: [
        { label: 'last week', value: p.stuck_last_week },
        { label: 'this week', value: p.stuck_this_week },
      ],
      caption: `${p.stuck_this_week} stuck this week · ${p.stuck_last_week} last week`,
      filter: 'stuck',
    }
  }
  // need_a_look draws no chart — the names are already on the strip.
  return null
}

/**
 * `count` is how many agents are reporting; `needLook` is the ones already
 * flagged by /dashboard/attention, which Home fetches anyway.
 *
 * Home must NOT load the roster (that request is what made it expensive), so
 * the count comes from /dashboard/cost's `agent_count` — the honest total,
 * not `agents.length`, which is a truncated top-spender list.
 *
 * Because we never see the roster, we can never say "all healthy": not being
 * flagged is not the same as being checked. `count` is null until we know it,
 * and the caller stays silent rather than printing a number it is guessing.
 */
export function fleetPulse({ agentCount, attention }) {
  const seen = new Set()
  const needLook = []
  for (const a of attention || []) {
    if (!a || !a.agent) continue
    const key = `${a.service_name || a.agent}:${a.agent_id || 'main'}`
    if (seen.has(key)) continue
    seen.add(key)
    needLook.push(a)
  }
  const count = Number.isFinite(Number(agentCount)) && agentCount !== null
    ? Number(agentCount)
    : null
  return { count, needLook }
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
 * `agentCount` is /dashboard/cost's `agent_count`, which Home fetches for the
 * strip anyway — no extra request, and honest in a way "no work yet" is not:
 * an account can have agents running and no named work, and that is a quiet
 * day, not a first run.
 *
 * Only true when every input came back and came back empty; a section that is
 * still loading or failed returns false, so we show its Retry rather than an
 * "all set up" story that isn't earned. `agentCount` null means we do not yet
 * know, which is not the same as zero.
 */
export function isFirstRun({ overview, items, agentCount }) {
  if (!overview || !items) return false
  if (agentCount === null || agentCount === undefined) return false
  const noWork = (Number(overview.open) || 0) === 0 && items.length === 0
  return noWork && Number(agentCount) === 0
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
 * The desk box's own copy when there is nothing on it.
 *
 * This replaced a shape-of-the-day slogan that inferred a second clause from
 * the absence of the first — "Nothing is waiting on you. Work is moving."
 * printed on a day with zero moving work, directly contradicting the strip
 * underneath it. An empty desk is one fact, so it gets one fact: what is not
 * waiting on you, and nothing about the rest of the day.
 *
 * `connected` false is the genuinely different case: no agents at all, so
 * "nothing is waiting on you" would understate it.
 */
export function deskEmptyCopy({ connected }) {
  if (!connected) {
    return {
      lead: 'Nothing is connected yet.',
      sub: 'Connect an agent and the work it hands you shows up here.',
    }
  }
  return {
    lead: 'Nothing is waiting on you.',
    sub: 'No tasks on your plate.',
  }
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
 * Every line carries BOTH where it goes and what to do about it. A notice you
 * cannot act on is just worry, so a row without a target is never emitted —
 * and the caller renders the action as a real button, not a hover affordance.
 */
export function noticedLines({ items, attention, nowMs = Date.now(), limit = 3, locale }) {
  const lines = []
  const stuck = stuckItems(items)
  if (stuck.length > 0) {
    const one = stuck.length === 1
    lines.push({
      key: 'stuck',
      kind: 'stuck',
      text: stuckNotice(items, nowMs, locale),
      // One stuck thing opens itself; several open Work already filtered.
      action: one ? 'Open it' : 'See stuck work',
      target: one ? { to: 'work-item', item: stuck[0] } : { to: 'work', filter: 'stuck' },
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
      action: 'Review agent',
      target: { to: 'agent', row: a },
    })
  }
  return lines.slice(0, limit)
}

// ---------------------------------------------------------------------------
// Ask presets
// ---------------------------------------------------------------------------

/**
 * Chips for the Ask field, built from what is actually on the page right now.
 *
 * A chip is only offered when its answer exists: "What's stuck?" on a day with
 * nothing stuck asks Trovis to describe an empty set, which is a worse first
 * impression than no chip at all. So every one is gated on its own condition
 * and the row simply shrinks.
 *
 * The cost chip is the one place outside the strip allowed to carry the day's
 * figure — it is quoting the strip into a question, not stating a second
 * number, and it is built from the same `cost.today` the strip renders.
 */
export function askChips({ desk, counts, attention, costToday, limit = 4 }) {
  const chips = []
  const c = counts || {}
  if ((desk || []).length > 0) {
    chips.push({ key: 'mine', label: "What's waiting on me?", query: "What's waiting on me?" })
  }
  if ((Number(c.waiting) || 0) > 0) {
    chips.push({
      key: 'others',
      label: "What's waiting on someone?",
      query: "What's waiting on someone else?",
    })
  }
  if ((Number(c.stuck) || 0) > 0) {
    chips.push({ key: 'stuck', label: "What's stuck?", query: "What's stuck?" })
  }
  const flagged = (attention || []).find((a) => a && a.agent)
  if (flagged) {
    chips.push({
      key: 'agent',
      label: `Why does ${flagged.agent} need a look?`,
      query: `Why does ${flagged.agent} need a look?`,
    })
  }
  const money = Number(costToday) || 0
  if (money >= 0.01) {
    const amount = `$${money.toFixed(2)}`
    chips.push({
      key: 'cost',
      label: `What cost ${amount} today?`,
      query: `What did we spend ${amount} on today?`,
    })
  }
  return chips.slice(0, limit)
}
