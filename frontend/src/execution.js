// The Execution Graph in an engineer's words. Pure — no React, no DOM.
//
// GET /work/items/:id/execution (work_execution.py) returns one run's
// technical execution: nodes with RECORDED parentage (parent_id), a separate
// CHRONOLOGY (node ids by persisted time), roots, trace ids, and per-node
// provenance. This module only arranges and words what the endpoint says.
// Four rules carry every function here:
//
//   STRUCTURE IS THE ENDPOINT'S. A node's parent is `parent_id` and nothing
//   else. Nothing here reads timestamps, neighbours, names, workers,
//   connectors, traces or runs to place a node under another. A null
//   parent_id is a root; `outside_read_set` stays a root with its recorded
//   parent id shown, never a fabricated one.
//
//   TYPES ARE THE ENDPOINT'S. `node.type` decides presentation. No span name
//   is inspected to invent a different type.
//
//   UNKNOWN IS NOT ZERO. A missing cost, duration or count is absent (or
//   "—" where the reader would otherwise wonder), never 0. An explicit 0 in
//   usage is a real observed zero and is shown as 0.
//
//   REPORTED IS NOT OBSERVED. A worker's tool report says the worker made
//   the call, not that anything happened in the world; completion says the
//   Work record closed, not that the outcome happened. No "success",
//   "verified" or business-outcome words are produced here.

import { getConnector } from './connectors.js'
import { runCost, runDuration } from './jobDetail.js'

/** Node types the endpoint emits, in the order the legend/inspector name them. */
export const NODE_TYPES = Object.freeze([
  'worker', 'model', 'tool', 'system', 'handoff', 'wait', 'completion', 'other',
])

export const TYPE_LABELS = Object.freeze({
  worker: 'Worker',
  model: 'Model',
  tool: 'Tool',
  system: 'External system',
  handoff: 'Handoff',
  wait: 'Wait',
  completion: 'Completion',
  other: 'Activity',
})

/** Trees at or under this many nodes open fully; above it, only roots open. */
export const EXPAND_ALL_MAX_NODES = 40

/**
 * PR 209's evidence kinds, in the words Evidence already uses. "Verified"
 * is not a word here; neither is "success".
 */
export const EVIDENCE_KIND_LABELS = Object.freeze({
  execution: 'Worker telemetry',
  action_reported: 'Reported by the worker',
  external_state: 'Observed from an external system',
  handoff: 'Handoff record',
  completion: 'Work record closure',
})

export const PARENT_STATUS_LABELS = Object.freeze({
  attached: 'Attached to its recorded parent',
  none: 'No parent recorded',
  outside_read_set: 'Parent recorded but not in this run’s read set',
  self_reference: 'Recorded parent is itself',
  cycle_broken: 'Recorded parents formed a cycle; this edge was removed',
})

const STATUS_LABELS = Object.freeze({
  ok: 'Completed',
  error: 'Error',
  unset: 'No status recorded',
  recorded: 'Recorded',
})

/** The stored worker label, minus only the default `:main` suffix. */
export function workerDisplay(label) {
  const s = String(label || '').trim()
  if (!s) return null
  return s.endsWith(':main') ? s.slice(0, -':main'.length) || s : s
}

/** A connector's registry name, or the raw id when the registry lacks it. */
export function connectorName(id) {
  if (!id) return null
  const c = getConnector(id)
  return c ? c.name : String(id)
}

/**
 * The system behind a tool, ONLY when the endpoint exposed one: an MCP
 * server name split deterministically by the backend (`tool.mcp_server`).
 * A dotted name like `stripe.refunds.create` names no system Trovis can
 * vouch for, so it stays a technical name and this returns null.
 */
export function toolSystem(tool) {
  const server = String(tool?.mcp_server || '').trim()
  if (!server) return null
  const c = getConnector(server.toLowerCase())
  return c ? c.name : server
}

/** "1,284 tokens" — 0 is a real observed zero; null/undefined is absent. */
export function fmtTokens(n) {
  if (n === null || n === undefined || n === '') return null
  const v = Number(n)
  if (!Number.isFinite(v) || v < 0) return null
  return `${v.toLocaleString('en-US')} ${v === 1 ? 'token' : 'tokens'}`
}

/**
 * A node's own cost, in words:
 *   reported / estimated with an amount  → "$0.02"
 *   covered                              → "in run total" (known, subsumed;
 *                                           NOT this node's own $0)
 *   absent                               → null (unknown, never $0)
 */
export function nodeCostLabel(cost) {
  if (!cost || !cost.known) return null
  if (cost.source === 'covered') return 'in run total'
  const amount = runCost(cost.amount_usd)
  return amount
}

export function costSourceLabel(cost) {
  if (!cost || !cost.known) return null
  return {
    reported: 'Reported by the SDK',
    estimated: 'Estimated from tokens',
    covered: 'Covered by a reported run total',
  }[cost.source] || null
}

/**
 * The row's first line, by TYPE and the endpoint's own fields:
 *   worker      the worker's stored label (never the connector's name)
 *   model       "Model · <model.name>"; "Model" when unnamed
 *   tool        "Tool · <system>" when the endpoint exposed an MCP server,
 *               else "Tool"; the technical name is the second line
 *   system      "<provider> · state observed"
 *   handoff     "Handoff · to a person / to another agent"
 *   wait        "Waiting on <provider or target>"
 *   completion  "Work record closed"
 *   other       the raw label (span name / event type)
 */
export function nodeTitle(node) {
  const t = node?.type
  if (t === 'worker') return workerDisplay(node.worker?.label) || node.label || 'Worker'
  if (t === 'model') return node.model?.name ? `Model · ${node.model.name}` : 'Model'
  if (t === 'tool') {
    const sys = toolSystem(node.tool)
    return sys ? `Tool · ${sys}` : 'Tool'
  }
  if (t === 'system') {
    const who = node.event?.actor?.label || connectorName(node.event?.provider) || 'External system'
    return `${who} · state observed`
  }
  if (t === 'handoff') {
    const dir = node.event?.direction
    const to = dir === 'to_human' ? 'to a person' : dir === 'to_agent' ? 'to another agent' : null
    const ev = node.event?.type || ''
    if (ev === 'handoff_initiated') return to ? `Handoff · ${to}` : 'Handoff'
    if (ev === 'handoff_completed') return 'Handoff completed'
    if (ev === 'handoff_accepted') return 'Handoff accepted'
    if (ev === 'handoff_declined') return 'Handoff declined'
    return 'Handoff'
  }
  if (t === 'wait') {
    const on = node.event?.actor?.label || connectorName(node.event?.provider) || node.event?.target_id
    return on ? `Waiting on ${on}` : 'Waiting on a system'
  }
  if (t === 'completion') return 'Work record closed'
  return node?.label || 'Activity'
}

/** The row's second line: the technical identity under the title, or null. */
export function nodeSubtitle(node) {
  const t = node?.type
  if (t === 'tool') return node.tool?.name || node.label || null
  if (t === 'model') return node.model?.provider || null
  if (t === 'worker') return node.label && node.label !== workerDisplay(node.worker?.label) ? node.label : null
  if (t === 'wait') return node.event?.waiting_on || null
  if (t === 'system') return node.event?.provider_event_type || null
  if (t === 'handoff') return node.event?.reason && !/^[a-z0-9]+(_[a-z0-9]+)+$/.test(node.event.reason) ? node.event.reason : null
  if (t === 'completion') return node.event?.reason ? String(node.event.reason).replace(/_/g, ' ') : null
  return null
}

/** Right-aligned facts for the row: duration, then tokens · cost. */
export function nodeMeta(node) {
  const duration = runDuration(node?.duration_ms)
  const tokens = fmtTokens(node?.usage?.total_tokens)
  const cost = nodeCostLabel(node?.cost)
  return {
    duration,
    facts: [tokens, cost].filter(Boolean),
  }
}

/** "Error · Card declined" / "Error" for an errored node; null otherwise. */
export function nodeErrorLine(node) {
  if (node?.status !== 'error') return null
  const msg = String(node.error || '').trim()
  return msg ? `Error · ${msg}` : 'Error'
}

// --- structure -----------------------------------------------------------------

/**
 * The tree, from `parent_id` alone.
 *
 *   byId      node id → node
 *   children  node id → child ids, in the endpoint's node order (which is
 *             chronology) — order within a parent is display order, not
 *             a relationship
 *   roots     the endpoint's `roots` (ids whose parent_id is null), in order
 *
 * A parent_id naming a node that is not in the body is treated as a root
 * with its recorded status kept — the endpoint should never emit that, but
 * the client still refuses to invent a parent.
 */
export function buildTree(body) {
  const nodes = Array.isArray(body?.nodes) ? body.nodes : []
  const byId = new Map(nodes.map((n) => [n.id, n]))
  const children = new Map()
  const roots = []
  for (const n of nodes) {
    const pid = n.parent_id
    if (pid && byId.has(pid) && pid !== n.id) {
      if (!children.has(pid)) children.set(pid, [])
      children.get(pid).push(n.id)
    } else {
      roots.push(n.id)
    }
  }
  return { byId, children, roots }
}

/**
 * Root groups, for a run whose roots span more than one trace.
 *
 * Span roots are grouped by their recorded trace id, ordered by first
 * appearance in the endpoint's node order; loop-event roots (handoffs,
 * waits, system state, completion — records with no trace of their own)
 * form one final "Work record events" group. Trace ids are recorded
 * facts, so grouping by them invents nothing; the groups are labelled by
 * ordinal ("Trace 1") because the endpoint does not say what a trace IS
 * (a run, a turn, a retry…), and the id itself belongs in the inspector.
 *
 * With one group, no heading is shown.
 */
export function rootGroups(body, tree) {
  const t = tree || buildTree(body)
  const groups = []
  const byKey = new Map()
  let ordinal = 0
  const events = []
  for (const id of t.roots) {
    const n = t.byId.get(id)
    if (!n) continue
    if (n.provenance?.record === 'loop_event' || !n.provenance?.trace_id) {
      events.push(id)
      continue
    }
    const key = n.provenance.trace_id
    if (!byKey.has(key)) {
      ordinal += 1
      const g = { key, label: `Trace ${ordinal}`, traceId: key, roots: [] }
      byKey.set(key, g)
      groups.push(g)
    }
    byKey.get(key).roots.push(id)
  }
  if (events.length) groups.push({ key: 'events', label: 'Work record events', traceId: null, roots: events })
  return groups
}

/**
 * THE fallback rule, exactly: chronology replaces the tree only when the
 * endpoint recorded NO parent relationship at all among two or more
 * nodes — every span node's parent_status is something other than
 * `attached`. Then there is no structure to draw, and drawing N roots as N
 * one-node trees would dress a flat list as a hierarchy.
 *
 * Multiple roots with at least one attached child keep the tree: several
 * truthful roots are still structure. A single node is a one-node tree.
 * Nothing about timing enters this decision.
 */
export function useChronologyFallback(body) {
  const nodes = Array.isArray(body?.nodes) ? body.nodes : []
  if (nodes.length < 2) return false
  return !nodes.some((n) => n.provenance?.parent_status === 'attached')
}

/** Ids of nodes whose subtree (below them) contains an errored node. */
export function subtreeErrors(tree) {
  const out = new Set()
  const visit = (id) => {
    let has = false
    for (const c of tree.children.get(id) || []) {
      const child = tree.byId.get(c)
      if (visit(c) || child?.status === 'error') has = true
    }
    if (has) out.add(id)
    return has
  }
  for (const r of tree.roots) visit(r)
  return out
}

/**
 * Which nodes start expanded: everything, for a tree of at most
 * EXPAND_ALL_MAX_NODES nodes; otherwise the roots only. Deterministic —
 * a client-side threshold on count, never on timing or content.
 */
export function defaultExpanded(tree) {
  const total = tree.byId.size
  const out = new Set()
  if (total <= EXPAND_ALL_MAX_NODES) {
    for (const id of tree.byId.keys()) if (tree.children.has(id)) out.add(id)
  } else {
    for (const id of tree.roots) if (tree.children.has(id)) out.add(id)
  }
  return out
}

/** Node depth by parent links, for indentation. */
export function depthOf(tree, id) {
  let d = 0
  let cur = tree.byId.get(id)
  const seen = new Set()
  while (cur && cur.parent_id && tree.byId.has(cur.parent_id) && !seen.has(cur.id)) {
    seen.add(cur.id)
    cur = tree.byId.get(cur.parent_id)
    d += 1
  }
  return d
}

// --- header summary ------------------------------------------------------------

function ms(iso) {
  const t = Date.parse(iso || '')
  return Number.isFinite(t) ? t : null
}

/**
 * Compact facts for the header, each only when the record carries it:
 *   nodes        count of nodes
 *   traces       count of trace ids (when more than one)
 *   observed     "observed over 12.4s": last recorded end minus first recorded
 *                start among span nodes — a span of observation, not the
 *                run's duration, which nothing records
 *   tokens       Σ usage.total_tokens over nodes that carry usage
 *   cost         Σ amounts of reported and estimated costs; covered spans are
 *                inside a reported total and are not added again; null when
 *                no node has a priced cost — never $0
 * Nothing here is a score or a verdict.
 */
export function executionSummary(body) {
  const nodes = Array.isArray(body?.nodes) ? body.nodes : []
  const spans = nodes.filter((n) => n.provenance?.record === 'span')
  let start = null
  let end = null
  for (const s of spans) {
    const a = ms(s.started_at)
    const b = ms(s.ended_at)
    if (a !== null && (start === null || a < start)) start = a
    if (b !== null && (end === null || b > end)) end = b
  }
  const observed = start !== null && end !== null && end >= start ? runDuration(end - start) : null
  let tokens = null
  for (const n of nodes) {
    const v = n.usage?.total_tokens
    if (v === null || v === undefined) continue
    tokens = (tokens || 0) + Number(v)
  }
  let cost = null
  let costNodes = 0
  for (const n of nodes) {
    const c = n.cost
    if (!c || !c.known || c.source === 'covered') continue
    const v = Number(c.amount_usd)
    if (!Number.isFinite(v)) continue
    cost = (cost || 0) + v
    costNodes += 1
  }
  return {
    nodes: nodes.length,
    traces: Array.isArray(body?.trace_ids) ? body.trace_ids.length : 0,
    observed,
    tokens: fmtTokens(tokens),
    cost: costNodes ? runCost(cost) : null,
    errors: nodes.filter((n) => n.status === 'error').length,
  }
}

/** The bounded-read sentence, or null. Never changes a node. */
export function boundedNote(body) {
  if (!body?.bounded) return null
  const n = Number(body.span_limit)
  return Number.isFinite(n) && n > 0
    ? `This execution record is bounded: the first ${n.toLocaleString('en-US')} spans were read and later spans were not.`
    : 'This execution record is bounded: later spans were not read.'
}

/** Quiet diagnostics the endpoint reports about its own read, or null. */
export function diagnosticsNote(body) {
  const parts = []
  const dup = Number(body?.duplicate_spans_dropped) || 0
  const cyc = Number(body?.cycles_broken) || 0
  if (dup) parts.push(`${dup} duplicate ${dup === 1 ? 'span' : 'spans'} dropped`)
  if (cyc) parts.push(`${cyc} recorded parent ${cyc === 1 ? 'cycle' : 'cycles'} broken`)
  return parts.length ? parts.join(' · ') : null
}

// --- chronology ----------------------------------------------------------------

/** "10:03:14" in the viewer's clock, or null. */
export function clockLabel(iso) {
  const t = ms(iso)
  if (t === null) return null
  const d = new Date(t)
  const p = (n) => String(n).padStart(2, '0')
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

/**
 * Rows for the chronology view: the endpoint's `chronology` order, each
 * node once, with its clock label and title. No parent, no arrow, no
 * "then" — order is the only fact carried.
 */
export function chronologyRows(body) {
  const byId = new Map((body?.nodes || []).map((n) => [n.id, n]))
  const ids = Array.isArray(body?.chronology) ? body.chronology : [...byId.keys()]
  return ids.filter((id) => byId.has(id)).map((id) => {
    const n = byId.get(id)
    return { id, node: n, at: clockLabel(n.started_at), title: nodeTitle(n), subtitle: nodeSubtitle(n) }
  })
}

// --- inspector -------------------------------------------------------------------

function row(label, value, opts = {}) {
  if (value === null || value === undefined || value === '') return null
  if (typeof value === 'boolean') return { label, value: value ? 'Yes' : 'No', ...opts }
  return { label, value: String(value), ...opts }
}

function absoluteTime(iso) {
  const t = ms(iso)
  if (t === null) return null
  return new Date(t).toISOString().replace('T', ' ').replace(/\.\d{3}Z$/, ' UTC')
}

/**
 * Type-aware sections for the inspector. Every row is present only when
 * the endpoint carried the value; "—" appears only for the few fields a
 * reader would otherwise think were forgotten (duration, status). Nothing
 * prints "null", "undefined" or "None".
 */
export function inspectorSections(node) {
  if (!node) return []
  const sections = []
  const push = (title, rows) => {
    const kept = rows.filter(Boolean)
    if (kept.length) sections.push({ title, rows: kept })
  }

  push('Identity', [
    row('Type', TYPE_LABELS[node.type] || node.type),
    row('Label', node.label),
    row('Worker', workerDisplay(node.worker?.label)),
    row('Connector', connectorName(node.connector?.id)),
  ])

  push('Timing', [
    row('Started', absoluteTime(node.started_at)),
    row('Ended', absoluteTime(node.ended_at)),
    row('Duration', runDuration(node.duration_ms) || (node.provenance?.record === 'span' ? '—' : null)),
  ])

  if (node.model || node.usage || node.cost) {
    push('Model', [
      row('Provider', node.model?.provider),
      row('Model', node.model?.name),
      row('Input tokens', node.usage?.input_tokens !== null && node.usage?.input_tokens !== undefined ? Number(node.usage.input_tokens).toLocaleString('en-US') : null),
      row('Output tokens', node.usage?.output_tokens !== null && node.usage?.output_tokens !== undefined ? Number(node.usage.output_tokens).toLocaleString('en-US') : null),
      row('Total tokens', node.usage?.total_tokens !== null && node.usage?.total_tokens !== undefined ? Number(node.usage.total_tokens).toLocaleString('en-US') : null),
      row('Cost', node.cost ? (nodeCostLabel(node.cost) || (node.cost.source === 'covered' ? 'in run total' : '—')) : null),
      row('Cost source', costSourceLabel(node.cost)),
    ])
  }

  if (node.tool) {
    push('Tool', [
      row('Name', node.tool.display_name && node.tool.display_name !== node.tool.name ? node.tool.display_name : null),
      row('Technical name', node.tool.name, { mono: true }),
      row('MCP server', node.tool.mcp_server),
      row('Call id', node.tool.call_id, { mono: true }),
      // The worker's own report of its call. Not an outcome anyone observed.
      row('Reported by worker', node.tool.reported_success === null || node.tool.reported_success === undefined
        ? null
        : node.tool.reported_success ? 'Call completed' : 'Call failed'),
    ])
  }

  push('Status', [
    row('Status', STATUS_LABELS[node.status] || node.status || '—'),
    row('Error', node.error),
  ])

  if (node.event) {
    const e = node.event
    push('Event', [
      row('Event', e.type),
      row('Direction', e.direction ? String(e.direction).replace(/_/g, ' ') : null),
      row('Actor', e.actor?.label ? `${e.actor.label}${e.actor.type ? ` (${e.actor.type})` : ''}` : null),
      row('To', e.target_label || e.target_id),
      row('Waiting on', e.waiting_on),
      row('Reason', e.reason),
      row('Detail', e.detail),
      row('Provider', connectorName(e.provider)),
      row('Provider event', e.provider_event_type),
      row('Provider object id', e.provider_object_id, { mono: true }),
      row('Provider event id', e.provider_event_id, { mono: true }),
      row('Effect', e.effect),
    ])
  }

  const p = node.provenance || {}
  push('Provenance', [
    row('Record', p.record === 'loop_event' ? 'Work record event' : p.record === 'span' ? 'Span' : p.record),
    row('Evidence kind', EVIDENCE_KIND_LABELS[p.evidence_kind] || p.evidence_kind),
    row('Correlation', p.correlation ? String(p.correlation).replace(/_/g, ' ') : 'not recorded'),
    row('Classified by', p.classification_basis ? String(p.classification_basis).replace(/_/g, ' ') : null),
    row('Parent', PARENT_STATUS_LABELS[p.parent_status] || p.parent_status),
  ])

  return sections
}

/** The ids behind a node, for the collapsed "Technical details" block. */
export function technicalDetails(node) {
  const p = node?.provenance || {}
  return [
    row('Trace id', p.trace_id, { mono: true }),
    row('Span id', p.span_id, { mono: true }),
    row('Recorded parent span id', p.parent_span_id, { mono: true }),
    row('Event id', p.event_id !== null && p.event_id !== undefined ? String(p.event_id) : null, { mono: true }),
    row('Loop link', p.loop_link, { mono: true }),
    row('Span kind', p.span_kind),
    row('Event type', p.event_type, { mono: true }),
    row('Node id', node?.id, { mono: true }),
  ].filter(Boolean)
}
