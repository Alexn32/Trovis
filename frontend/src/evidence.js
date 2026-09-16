// Work Evidence, in a manager's words. Pure — no React, no DOM.
//
// GET /work/items/:id/evidence (work_evidence.py) says which stored
// observation supports each claim about a run, and what kind of support it
// is. This module turns those records into the two things the Run page
// shows under the work itself:
//
//   sources       who or what Trovis heard from, one row each, with what
//                 kind of thing each said (ran, reported an action, showed
//                 a state) and when it was last heard from.
//   observations  the records worth a line of their own — an action a
//                 worker REPORTED, a state an external system SHOWED, the
//                 record closing — each carrying its provenance phrase.
//
// Two words are load-bearing and never swapped:
//   "Reported by X"    the worker's own telemetry said it did this. It proves
//                      the report, not the outcome.
//   "Observed from X"  Trovis received this from X itself (a worker's spans;
//                      an external system's event about its own object).
// "Verified" does not appear: Trovis has not yet defined which systems are
// authoritative for which outcomes, so nothing here claims to be one.
//
// Sources are WORKERS, not connectors: two agents on the same connector are
// two rows, each with the connector as its quiet second line (sourceOf).
//
// Nothing is inferred. A tool named refund_customer reads as "Refund
// customer — Reported by refunds-agent · Grok Bot", never "Refund
// completed". An external
// state reads as what the provider event type says it is about, or simply
// "State observed" when the type is not one we can name. Missing source
// information stays "Source not recorded".

import { getConnector } from './connectors.js'

/** Mirrors work_evidence's span bound; the note below quotes it. */
export const EVIDENCE_SPAN_LIMIT = 2000

const TROVIS = 'Trovis'

/**
 * Who a record came from, as a person would name it.
 *
 * Worker identity is not connector identity. For agent evidence the backend
 * names the WORKER in `source_label` (`service:agent_id`, the actor idiom
 * from loops.agent_actor — display and equality only, never parsed apart)
 * and the CONNECTOR in `source_connector_id` (how the telemetry arrived).
 * Two workers on the same connector are two sources; the connector is the
 * quiet line under each. The one cosmetic step is dropping the default
 * `:main` suffix, the same way the action list shows a main agent by its
 * service name.
 *
 *   name  the worker (its stored label), a person's resolved name, a
 *         provider's registry name, or "Trovis" for the product itself.
 *         Falls back to the connector's name when no worker label exists.
 *   hint  the connector/platform under a worker ("Grok (xAI SDK)",
 *         "OpenTelemetry" for a generic source, "source not recorded" when
 *         the event predates span links), else null.
 *   kind  agent | human | system | unknown
 *   key   groups records from the same source: connector + worker.
 */
export function sourceOf(rec) {
  const type = rec?.source_type || null
  const cid = rec?.source_connector_id || null
  const label = String(rec?.source_label || '').trim()

  if (type === 'human') {
    return { key: `human:${label || '?'}`, name: label || 'A person', kind: 'human', hint: null }
  }
  if (type === 'system') {
    const c = cid ? getConnector(cid) : null
    if (c) return { key: `system:${cid}`, name: c.name, kind: 'system', hint: null }
    return { key: 'system:trovis', name: TROVIS, kind: 'system', hint: null }
  }
  if (type === 'agent') {
    const worker = workerName(label)
    const connector = cid ? getConnector(cid) : null
    if (connector && cid !== 'custom-otel') {
      return {
        key: `agent:${cid}:${label || '?'}`,
        name: worker || connector.name,
        kind: 'agent',
        hint: worker ? connector.name : null,
      }
    }
    if (cid === 'custom-otel') {
      return {
        key: `agent:custom-otel:${label || '?'}`,
        name: worker || 'An OpenTelemetry source',
        kind: 'agent',
        hint: 'OpenTelemetry',
      }
    }
    // The event predates span links: the record exists, its source does not.
    return {
      key: `agent:unknown:${label || '?'}`,
      name: worker || 'Source not recorded',
      kind: worker ? 'agent' : 'unknown',
      hint: worker ? 'source not recorded' : null,
    }
  }
  return { key: 'unknown', name: 'Source not recorded', kind: 'unknown', hint: null }
}

/** The stored worker label, minus only the default `:main` sub-agent suffix. */
function workerName(label) {
  if (!label) return null
  return label.endsWith(':main') ? label.slice(0, -':main'.length) || label : label
}

/**
 * `mcp__stripe__create_refund` -> "Create refund"; `refund_customer` ->
 * "Refund customer". Readable, not translated: the last segment of a
 * namespaced name, separators to spaces, first letter up. Nothing is
 * added that the name did not say.
 */
export function actionName(tool) {
  const raw = String(tool || '').trim()
  if (!raw) return 'Action'
  const parts = raw.split(/__|::|[./:]/).filter(Boolean)
  const last = parts[parts.length - 1] || raw
  const words = last.replace(/[_-]+/g, ' ').replace(/\s+/g, ' ').trim()
  if (!words) return 'Action'
  return words.charAt(0).toUpperCase() + words.slice(1)
}

// What an external event is ABOUT, from its provider event type. A closed
// list; anything else is "State observed" — never a guess.
const STATE_KINDS = [
  [/refund/i, 'Refund'],
  [/dispute/i, 'Dispute'],
  [/fulfil/i, 'Fulfillment'],
  [/payment|invoice|charge/i, 'Payment'],
  [/return/i, 'Return'],
  [/order/i, 'Order'],
  [/deal/i, 'Deal'],
  [/ticket/i, 'Ticket'],
]

export function externalStateHeadline(rec) {
  const t = String(rec?.details?.provider_event_type || '')
  for (const [re, kind] of STATE_KINDS) {
    if (re.test(t)) return `${kind} state observed`
  }
  return 'State observed'
}

/**
 * The provenance phrase for one record. Names the worker when Trovis knows
 * it, with the connector as trailing context ("Reported by refunds-agent ·
 * Grok (xAI SDK)"); a worker with no recorded connector is named without one.
 */
export function provenanceLine(rec) {
  const src = sourceOf(rec)
  const type = rec?.evidence_type
  const who = src.kind === 'agent' && src.hint && src.hint !== 'source not recorded'
    ? `${src.name} · ${src.hint}`
    : src.name
  if (type === 'action_reported') {
    if (src.kind === 'unknown') return 'Source not recorded'
    return rec?.details?.errored ? `Reported error by ${who}` : `Reported by ${who}`
  }
  if (type === 'execution' || type === 'external_state') {
    return src.kind === 'unknown' ? 'Source not recorded' : `Observed from ${who}`
  }
  if (type === 'handoff' || type === 'completion') {
    if (src.kind === 'human') return `Recorded by ${src.name}`
    if (src.kind === 'system') return `Recorded by ${src.name}`
    if (src.kind === 'agent') return `Reported by ${who}`
    return 'Source not recorded'
  }
  if (type === 'cost') return costProvenanceLine(rec)
  return src.kind === 'unknown' ? 'Source not recorded' : `Observed from ${src.name}`
}

/** "Reported by the SDK" / "Estimated from observed model usage" / mixed. */
export function costProvenanceLine(rec) {
  const basis = rec?.details?.basis
  if (basis === 'reported') return 'Cost as reported by the agent SDK'
  if (basis === 'estimated') return 'Cost estimated from observed model usage'
  return 'Cost based on observed model usage'
}

/** Internal correlation vocabulary → a phrase for the Details disclosure. */
export function correlationLabel(method) {
  switch (method) {
    case 'explicit_key': return 'Directly linked to this run'
    case 'time_adjacency': return 'Associated with this run by timing'
    case 'origin': return 'Started this run'
    case 'direct': return 'Recorded directly on this run'
    default: return 'Link not recorded'
  }
}

const CLOSE_TITLES = {
  completed_by_agent: 'Work record closed',
  closed_by_user: 'Work record closed',
  abandoned: 'Work record abandoned',
  ingestion_artifact: 'Work record reclassified as an artifact',
}

/**
 * The records that earn a line of their own, oldest first. Execution and
 * cost are summarized in `sources` instead; handoffs are already the
 * page's steps and passes, so they are counted in sources, not repeated.
 *
 * Each row: { id, kind, title, provenance, at, errored, note, details }
 * where `details` is [[label, value], …] for the collapsed disclosure —
 * exact ids, exact times, never the default view.
 */
export function observations(evidence) {
  const rows = []
  for (const rec of evidence || []) {
    const type = rec?.evidence_type
    const d = rec?.details || {}
    const src = sourceOf(rec)
    const base = {
      id: rec.id,
      kind: type,
      at: rec.observed_at || null,
      provenance: provenanceLine(rec),
      errored: false,
      note: null,
      details: [],
    }
    if (type === 'action_reported') {
      rows.push({
        ...base,
        title: actionName(d.tool),
        errored: Boolean(d.errored),
        // Only what the run said. A message it did not give is not written.
        note: d.errored && d.error ? String(d.error) : null,
        details: compact([
          ['Source', src.hint ? `${src.name} (${src.hint})` : src.name],
          ['Observed', rec.observed_at],
          ['Reported action', d.tool],
          ['Operation', d.span_name],
          ['Span', rec.span_id],
          ['Trace', rec.trace_id],
          ['Link', correlationLabel(rec.correlation_method)],
        ]),
      })
    } else if (type === 'external_state') {
      rows.push({
        ...base,
        title: externalStateHeadline(rec),
        // The provider's own reason, when the spine kept one ("Card declined").
        note: d.reason ? String(d.reason) : null,
        details: compact([
          ['Source', src.name],
          ['Observed', rec.observed_at],
          ['Provider event', d.provider_event_type],
          ['External object', rec.external_object_id],
          ['Provider event id', rec.external_event_id],
          ['Effect on the work', effectLabel(d.effect)],
          ['Link', correlationLabel(rec.correlation_method)],
        ]),
      })
    } else if (type === 'completion') {
      rows.push({
        ...base,
        title: CLOSE_TITLES[d.reason] || 'Work record closed',
        // The record closing is not an outcome. Say so where it is read.
        note: 'The record closed; this does not by itself show the outcome.',
        details: compact([
          ['Source', src.name],
          ['Observed', rec.observed_at],
          ['Reason', d.reason],
          ['Detail', d.detail],
          ['Span', rec.span_id],
          ['Link', correlationLabel(rec.correlation_method)],
        ]),
      })
    }
  }
  rows.sort((a, b) => (Date.parse(a.at || '') || 0) - (Date.parse(b.at || '') || 0))
  return rows
}

function effectLabel(effect) {
  if (effect === 'wait') return 'Work waited on this'
  if (effect === 'clear') return 'A wait was cleared'
  if (effect === 'stuck') return 'Work was marked as needing attention'
  return null
}

function compact(pairs) {
  return pairs.filter(([, v]) => v !== null && v !== undefined && String(v).trim() !== '')
}

/**
 * One row per source Trovis heard from, most recently heard first.
 * { key, name, kind, hint, lines: [..], lastAt }
 */
export function sources(evidence) {
  const by = new Map()
  for (const rec of evidence || []) {
    const src = sourceOf(rec)
    let s = by.get(src.key)
    if (!s) {
      s = {
        key: src.key, name: src.name, kind: src.kind, hint: src.hint,
        execution: 0, actions: 0, actionErrors: 0, states: 0, handoffs: 0,
        completions: 0, lastAt: null,
      }
      by.set(src.key, s)
    }
    const type = rec.evidence_type
    if (type === 'execution') s.execution += Number(rec.details?.span_count) || 0
    else if (type === 'action_reported') {
      s.actions += 1
      if (rec.details?.errored) s.actionErrors += 1
    } else if (type === 'external_state') s.states += 1
    else if (type === 'handoff') s.handoffs += 1
    else if (type === 'completion') s.completions += 1
    // cost is a roll-up of the same spans; it adds no new "heard from" moment.
    const at = rec.observed_at || rec.details?.last_observed_at || null
    const last = rec.details?.last_observed_at || null
    for (const t of [at, last]) {
      if (t && (!s.lastAt || t > s.lastAt)) s.lastAt = t
    }
  }
  const out = []
  for (const s of by.values()) {
    const lines = []
    if (s.execution > 0) lines.push('Execution observed')
    if (s.actions > 0) {
      lines.push(
        `${s.actions} action${s.actions === 1 ? '' : 's'} reported` +
          (s.actionErrors > 0 ? ` · ${s.actionErrors} with errors` : ''),
      )
    }
    if (s.states > 0) lines.push(`${s.states} state observation${s.states === 1 ? '' : 's'}`)
    if (s.handoffs > 0) {
      lines.push(
        s.kind === 'human'
          ? `${s.handoffs} decision${s.handoffs === 1 ? '' : 's'} recorded`
          : `${s.handoffs} handoff${s.handoffs === 1 ? '' : 's'} recorded`,
      )
    }
    if (s.completions > 0) lines.push('Record closed')
    out.push({ key: s.key, name: s.name, kind: s.kind, hint: s.hint, lines, lastAt: s.lastAt })
  }
  out.sort((a, b) => (Date.parse(b.lastAt || '') || 0) - (Date.parse(a.lastAt || '') || 0))
  return out
}

/** The header's cost provenance line, or null when there is no cost record. */
export function costProvenance(evidence) {
  const costs = (evidence || []).filter((r) => r?.evidence_type === 'cost')
  if (costs.length === 0) return null
  const bases = new Set(costs.map((r) => r.details?.basis))
  if (bases.size === 1) return costProvenanceLine(costs[0])
  return 'Cost based on observed model usage'
}

/** The truncation note, or null. Quotes the backend's own bound. */
export function truncationNote(body) {
  if (!body?.spans_truncated) return null
  return `Showing evidence from this run's first ${EVIDENCE_SPAN_LIMIT.toLocaleString()} observations.`
}
