// The judgment ribbon — what Trovis thinks you should do about today.
//
// Everything here is composed from data Home has ALREADY fetched (work
// overview + items, and the agent-health rows). No endpoint, no Claude call,
// nothing added to first paint: the ribbon is free.
//
// The bar for shipping an insight is that it says something the rows below it
// do not. "Two things need you" is not an insight — the counts say that, and
// the queue shows it. "Sarah Chen is holding three of them" is, because
// nothing else on the page puts those rows together. When we have nothing at
// that bar, the ribbon renders nothing at all; a row of restated counts would
// be worse than silence.

import { boardAge } from './board.js'
import { partitionLookAt } from './home.js'

/** Never more than this many, however much we find. Three is a glance. */
export const MAX_INSIGHTS = 3

/** A person or tool must hold at least this many before it is a pattern. */
const PATTERN_MIN = 2

function ageMs(item, nowMs) {
  const t = Date.parse(item?.updated_at || '')
  return Number.isNaN(t) ? 0 : Math.max(0, nowMs - t)
}

/**
 * "9h" / "20m", in the same grain the work rows use — but measured against
 * the SAME `nowMs` as everything else here. board.js's workUpdatedLabel reads
 * Date.now() itself, which would let the ribbon and the row it points at
 * print different ages for the same item.
 */
function ageLabel(item, nowMs) {
  return boardAge(Math.floor(ageMs(item, nowMs) / 1000))
}

/** Oldest first — the one that has been sitting longest leads. */
function byOldest(nowMs) {
  return (a, b) => ageMs(b, nowMs) - ageMs(a, nowMs)
}

function plural(n, one, many) {
  return `${n} ${n === 1 ? one : many}`
}

/**
 * Group items by who is holding them, for a given holder kind. Only holders
 * with a real name count — "Unassigned" is not a bottleneck, it is a gap.
 */
function groupByHolder(items, kind) {
  const groups = new Map()
  for (const it of items) {
    if (it.holder?.kind !== kind) continue
    const name = String(it.holder?.name || '').trim()
    if (!name || /^unassigned$/i.test(name)) continue
    if (!groups.has(name)) groups.set(name, [])
    groups.get(name).push(it)
  }
  return groups
}

/**
 * Build the ribbon.
 *
 * @param items    named work rows from /work/items
 * @param health   rows from /dashboard/attention (agent-level, not work)
 * @returns array of { id, kind, tone, text, item?, agent? } — at most
 *          MAX_INSIGHTS, and empty when nothing clears the bar.
 */
export function buildInsights({ items = [], health = [], nowMs = Date.now() } = {}) {
  const { needsYou, needsAttention } = partitionLookAt(items || [], nowMs)
  const out = []

  // 1. A person holding several things is the most actionable thing on the
  //    page: one nudge unblocks all of them. Beats naming any single row.
  const people = groupByHolder(needsAttention, 'human')
  let topPerson = null
  for (const [name, rows] of people) {
    if (rows.length < PATTERN_MIN) continue
    if (!topPerson || rows.length > topPerson.rows.length) topPerson = { name, rows }
  }
  if (topPerson) {
    const oldest = [...topPerson.rows].sort(byOldest(nowMs))[0]
    out.push({
      id: `person:${topPerson.name}`,
      kind: 'bottleneck-person',
      tone: 'act',
      text: `${topPerson.name} is holding ${plural(topPerson.rows.length, 'thing', 'things')} — the oldest since ${ageLabel(oldest, nowMs)}.`,
      item: oldest,
    })
  }

  // 2. Several things stuck on the same tool is one broken integration, not
  //    several unlucky tasks — and it is fixed in one place.
  const tools = groupByHolder(needsAttention, 'tool')
  let topTool = null
  for (const [name, rows] of tools) {
    if (rows.length < PATTERN_MIN) continue
    if (!topTool || rows.length > topTool.rows.length) topTool = { name, rows }
  }
  if (topTool) {
    const oldest = [...topTool.rows].sort(byOldest(nowMs))[0]
    out.push({
      id: `tool:${topTool.name}`,
      kind: 'bottleneck-tool',
      tone: 'risk',
      text: `${plural(topTool.rows.length, 'thing is', 'things are')} waiting on ${topTool.name} — the oldest since ${ageLabel(oldest, nowMs)}.`,
      item: oldest,
    })
  }

  // 3. Where to start, but only when there is a real choice to make. With one
  //    item waiting the queue below already answers this.
  if (needsYou.length >= 2) {
    const oldest = [...needsYou].sort(byOldest(nowMs))[0]
    out.push({
      id: `start:${oldest.id}`,
      kind: 'start-here',
      tone: 'act',
      text: `Start with "${oldest.title}" — it has been waiting on you longest (${ageLabel(oldest, nowMs)}).`,
      item: oldest,
    })
  }

  // 4. An agent in trouble never reaches the work queue (agent health is not
  //    named work), so the ribbon is the only place it can surface.
  for (const h of health || []) {
    if (!h || (h.severity !== 'critical' && h.severity !== 'warning')) continue
    const line = String(h.title || h.detail || '').trim()
    if (!line) continue
    out.push({
      id: `agent:${h.agent}`,
      kind: 'agent-risk',
      tone: h.severity === 'critical' ? 'risk' : 'note',
      text: line.endsWith('.') ? line : `${line}.`,
      agent: h.agent,
    })
    break // one agent line is a heads-up; a list of them is a different page
  }

  return out.slice(0, MAX_INSIGHTS)
}
