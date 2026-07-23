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
// attention-first sorting. awaiting_system is in the stuck set: a loop
// blocked on a dead webhook needs a human to notice like any other wait.
export const ATTENTION_STATES = ['stalled', 'awaiting_human', 'awaiting_system']
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
  // Round up, never suppress — same rule as the pill's "done · under a minute".
  if (s < 60) return 'under a minute'
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

// ---------------------------------------------------------------------------
// Board derivations (pure — the board and story render these)
// ---------------------------------------------------------------------------

/**
 * Possession-bar geometry from segments_mini. Returns proportional slices:
 *   [{kind: 'agent'|'wait'|'pending'|'error', pct: number}]
 * Widths are proportional to duration with a 3% floor, normalized to 100.
 * A trailing unfinished agent segment renders 'pending' (gray, pulsing edge);
 * a live wait keeps the warning kind — attention must stay visible. When the
 * loop is abandoned, the final slice becomes 'error'.
 */
export function barSegments(mini, nowNs = Date.now() * 1e6, loopState = null) {
  const segs = Array.isArray(mini) ? mini.filter((s) => s && s.start_ns != null) : []
  if (segs.length === 0) return [{ kind: 'pending', pct: 100 }]
  const start = Math.min(...segs.map((s) => s.start_ns))
  const end = Math.max(...segs.map((s) => (s.end_ns == null ? nowNs : s.end_ns)))
  const total = Math.max(1, end - start)
  let slices = segs.map((s) => {
    const ongoing = s.end_ns == null
    const dur = Math.max(0, (ongoing ? nowNs : s.end_ns) - s.start_ns)
    let kind = s.waiting ? 'wait' : 'agent'
    if (ongoing && !s.waiting) kind = 'pending'
    return { kind, pct: Math.max(3, (dur / total) * 100) }
  })
  if (loopState === 'abandoned' && slices.length > 0) {
    slices[slices.length - 1] = { ...slices[slices.length - 1], kind: 'error' }
  }
  const sum = slices.reduce((a, s) => a + s.pct, 0)
  slices = slices.map((s) => ({ ...s, pct: (s.pct / sum) * 100 }))
  return slices
}

/**
 * Possession chain dots from segments_mini. Cap at 4 dots; longer chains
 * collapse the middle to a '··' marker. The current (last) holder is marked.
 */
export function chainDots(mini, cap = 4) {
  const segs = Array.isArray(mini) ? mini : []
  const dots = segs.map((s, i) => ({
    holder_type: s.holder_type || 'agent',
    waiting: Boolean(s.waiting),
    current: i === segs.length - 1,
  }))
  if (dots.length <= cap) return { dots, collapsed: false }
  return { dots: [...dots.slice(0, 2), ...dots.slice(-2)], collapsed: true }
}

export function initialsOf(label) {
  const words = String(label || '')
    .split(/[\s\-_:./]+/)
    .filter(Boolean)
  if (words.length === 0) return '?'
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase()
  return (words[0][0] + words[1][0]).toUpperCase()
}

export function agentLabel(loop) {
  return loop?.agent_id && loop.agent_id !== 'main'
    ? `${loop.service_name} · ${loop.agent_id}`
    : loop?.service_name || 'agent'
}

// Board row title. Server-generated titles are the record; the only client
// fallback left is the agent identity for loops the server hasn't titled yet
// (open, pre-handoff — titles land at first handoff or terminal state).
export function boardTitle(loop) {
  return loop?.title || agentLabel(loop)
}

/**
 * Board grouping: loops matched to a declared workflow group under its name;
 * unmatched loops group under agent identity. Matched groups sort above
 * unmatched; group order and in-group order both derive from the
 * attention-first sorted list (first-encounter), so groups needing a human
 * float up and rows inside run attention-first then newest.
 */
export function boardGroups(loops, nowMs = Date.now()) {
  const ordered = sortLoopsAttentionFirst(loops, nowMs)
  const groups = new Map()
  for (const l of ordered) {
    const matched = l?.workflow_id != null
    const key = matched ? `wf:${l.workflow_id}` : `agent:${workflowGroupKey(l)}`
    let g = groups.get(key)
    if (!g) {
      g = {
        key,
        matched,
        name: matched ? l.workflow_name || 'Workflow' : agentLabel(l),
        loops: [],
      }
      groups.set(key, g)
    }
    g.loops.push(l)
  }
  const all = [...groups.values()].filter((g) => g.loops.length > 0)
  return [...all.filter((g) => g.matched), ...all.filter((g) => !g.matched)]
}

// "14 today · 1 needs you" / "14 today · all moving"
export function boardGroupSummary(group, nowMs = Date.now()) {
  const today = new Date(nowMs)
  const isToday = (iso) => {
    const t = parseTs(iso)
    if (Number.isNaN(t)) return false
    const d = new Date(t)
    return (
      d.getFullYear() === today.getFullYear() &&
      d.getMonth() === today.getMonth() &&
      d.getDate() === today.getDate()
    )
  }
  const n = group.loops.filter((l) => isToday(l.created_at)).length
  const needs = stuckCount(group.loops)
  const left = n === 1 ? '1 today' : `${n} today`
  const right =
    needs > 0 ? `${needs} ${needs === 1 ? 'needs' : 'need'} you` : 'all moving'
  return `${left} · ${right}`
}

// ---------------------------------------------------------------------------
// Workflow map derivations + copy (the workflow page)
// ---------------------------------------------------------------------------

// Every user-facing workflow-surface string lives here (the sentence-
// constants module) so the jargon test can sweep them.
export const WORKFLOW_STRINGS = {
  newWorkflow: 'New workflow',
  editStations: 'Edit stations',
  nameLabel: 'Name',
  stationsLabel: 'Stations',
  stationsEmptyNudge: 'No stations yet — the map stays empty until you describe the process.',
  hintsTitle: 'How Trovis recognizes this work',
  hintsExplainer: 'Loops matching these rules belong to this workflow.',
  noteLabel: 'What changed (optional)',
  mapEmpty: 'No stations declared yet — describe the process to see work move through it',
  loopList: 'Loops in this workflow',
  historyLabel: 'history ›',
  waitingWord: 'waiting',
}

export function saveVersionLabel(currentVersion) {
  return currentVersion ? `Save as v${currentVersion + 1}` : 'Create workflow'
}

export function doneTodayLabel(n) {
  return `${n} done today`
}

export function moreDotsLabel(n) {
  return `+${n} more`
}

export function workflowHeaderLine(wf, nowMs = Date.now()) {
  const needs =
    (wf?.loop_counts?.stalled || 0) +
    (wf?.loop_counts?.awaiting_human || 0) +
    (wf?.loop_counts?.awaiting_system || 0)
  const parts = [`${wf?.loops_today ?? 0} today`]
  parts.push(needs > 0 ? `${needs} ${needs === 1 ? 'needs' : 'need'} you` : 'all moving')
  const latest = wf?.versions?.[0]?.created_at || wf?.created_at
  const t = parseTs(latest)
  if (!Number.isNaN(t)) {
    parts.push(`updated ${new Date(t).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}`)
  }
  return parts.join(' · ')
}

// Dot label under the track: waiting dots carry the age; working dots just
// the short title. Titles ellipsize in CSS; this is the text content.
export function mapDotLabel(loop, nowMs = Date.now()) {
  const title = loop?.title || agentLabel(loop)
  const age = loopAgeSeconds(loop, nowMs)
  const waiting = ATTENTION_STATES.includes(loop?.cached_state)
  if (waiting && age != null) {
    return `${title} · ${WORKFLOW_STRINGS.waitingWord} ${fmtAge(age)}`
  }
  return title
}

/**
 * Group the map's live loops into per-station dot stacks. Only on_path
 * loops get dots — off_path/no_stations render solely in the loop list.
 * Waiting dots sort first within a stack; stacks cap at `cap` dots with an
 * overflow count.
 */
export function mapDots(mapLoops, cap = 3) {
  const byStation = new Map()
  for (const l of Array.isArray(mapLoops) ? mapLoops : []) {
    if (l?.position?.status !== 'on_path') continue
    const idx = l.position.station_index
    if (idx == null) continue
    if (!byStation.has(idx)) byStation.set(idx, [])
    byStation.get(idx).push(l)
  }
  const out = new Map()
  for (const [idx, ls] of byStation) {
    const sorted = [...ls].sort((a, b) => {
      const aw = ATTENTION_STATES.includes(a.cached_state) ? 0 : 1
      const bw = ATTENTION_STATES.includes(b.cached_state) ? 0 : 1
      return aw - bw
    })
    out.set(idx, {
      dots: sorted.slice(0, cap),
      overflow: Math.max(0, sorted.length - cap),
    })
  }
  return out
}

// Stations that currently hold waiting work get the warm treatment.
export function waitingStations(mapLoops) {
  const set = new Set()
  for (const l of Array.isArray(mapLoops) ? mapLoops : []) {
    if (
      l?.position?.status === 'on_path' &&
      l.position.station_index != null &&
      ATTENTION_STATES.includes(l.cached_state)
    ) {
      set.add(l.position.station_index)
    }
  }
  return set
}

/**
 * Editor → API payload. Drops fully-empty station rows and hint rows with
 * no value; trims strings; tools split on commas. Shape matches
 * WorkflowCreate / WorkflowVersionCreate exactly.
 */
export function buildWorkflowPayload(name, stations, hints, note) {
  const cleanStations = (Array.isArray(stations) ? stations : [])
    .map((s) => {
      const out = { holder_type: s.holder_type || 'agent' }
      if (s.holder && s.holder.trim()) out.holder = s.holder.trim()
      if (s.label && s.label.trim()) out.label = s.label.trim()
      const tools = (Array.isArray(s.tools) ? s.tools : String(s.tools || '').split(','))
        .map((t) => String(t).trim())
        .filter(Boolean)
      if (tools.length > 0) out.tools = tools
      return out
    })
    .filter((s) => s.holder || s.label || (s.tools && s.tools.length) || stations.length > 0)
  const cleanHints = (Array.isArray(hints) ? hints : [])
    .map((h) => ({
      field: h.field || 'service_name',
      op: h.op || 'equals',
      value: String(h.value || '').trim(),
    }))
    .filter((h) => h.value)
  return {
    name: String(name || '').trim(),
    stations: cleanStations,
    match_hints: cleanHints,
    ...(note && note.trim() ? { note: note.trim() } : {}),
  }
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
    case 'awaiting_system':
      return { label: withAge('waiting on a system'), tone: 'warning', attention: true }
    case 'working':
    case 'open':
      return { label: 'working', tone: 'live', attention: false }
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

// ---------------------------------------------------------------------------
// Workflow rollup (the Work tab's "By workflow" view)
// ---------------------------------------------------------------------------

// v1 workflow identity is a deliberate heuristic: the group key is
// service_name + agent_id — one rollup row per agent identity. No
// title-pattern clustering, no similarity matching; smarter grouping is a
// later, data-informed problem.
export function workflowGroupKey(loop) {
  return `${loop?.service_name || ''}:${loop?.agent_id || 'main'}`
}

// The number badged on the Stuck segment: loops needing a human.
export function stuckCount(loops) {
  return (Array.isArray(loops) ? loops : []).filter((l) =>
    ATTENTION_STATES.includes(l?.cached_state),
  ).length
}

/**
 * Group an (already attention-first-sorted) loop list into workflow rollups.
 * Groups appear in first-encounter order, so groups containing attention
 * loops naturally float to the top. Each group:
 *   { key, service_name, agent_id, label, loops, runCount, totalCost,
 *     stalledCount, awaitingCount, attention }
 */
export function groupLoopsByWorkflow(loops) {
  const groups = new Map()
  for (const l of Array.isArray(loops) ? loops : []) {
    const key = workflowGroupKey(l)
    let g = groups.get(key)
    if (!g) {
      g = {
        key,
        service_name: l?.service_name || '',
        agent_id: l?.agent_id || 'main',
        label:
          l?.agent_id && l.agent_id !== 'main'
            ? `${l.service_name} · ${l.agent_id}`
            : l?.service_name || 'agent',
        loops: [],
        runCount: 0,
        totalCost: 0,
        stalledCount: 0,
        awaitingCount: 0,
        attention: false,
      }
      groups.set(key, g)
    }
    g.loops.push(l)
    g.runCount += 1
    g.totalCost += Number(l?.total_cost_usd) || 0
    if (l?.cached_state === 'stalled') g.stalledCount += 1
    if (l?.cached_state === 'awaiting_human') g.awaitingCount += 1
    g.attention = g.stalledCount + g.awaitingCount > 0
  }
  return [...groups.values()]
}

// Rollup row strings. Reuses the loop-state vocabulary ("stalled",
// "waiting on you") — never raw state identifiers.
export function workflowGroupMeta(group) {
  const parts = []
  if (group?.stalledCount > 0) {
    parts.push(`${group.stalledCount} stalled`)
  }
  if (group?.awaitingCount > 0) {
    parts.push(`${group.awaitingCount} waiting on you`)
  }
  return {
    runLabel: `ran ${group?.runCount ?? 0}×`,
    cost: loopCostLabel(group?.totalCost),
    stateLabel: parts.length > 0 ? parts.join(' · ') : null,
    attention: Boolean(group?.attention),
  }
}

// "service:agent" composite actor → display pieces.
export function splitActor(actor) {
  const s = String(actor || '')
  const i = s.indexOf(':')
  if (i === -1) return { service: s, agent: 'main' }
  return { service: s.slice(0, i), agent: s.slice(i + 1) || 'main' }
}
