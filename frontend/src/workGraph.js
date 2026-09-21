// The Work Graph in an operator's words. Pure — no React, no DOM.
//
// GET /work/items/:id/graph (work_graph.py) is the deterministic operational
// projection of one run: its Work Steps (progress / handoff / wait /
// exception / completed), the canonical possession chain, and per-step
// provenance back to the loop event, its Evidence record and its Execution
// node. This module only arranges and words what the endpoint says. Four
// rules carry every function here:
//
//   STEPS ARE THE ENDPOINT'S. `steps` is the timeline. Nothing here reads
//   `lifecycle`, Evidence, Execution, tool names, model output or timing to
//   add, merge, split or reorder a step. Zero steps is a truthful sparse
//   record, not "nothing happened".
//
//   POSSESSION IS THE ENDPOINT'S. Who holds the work is
//   `possession.current_holder` and nothing else — never the latest step,
//   never a step's actor, never a step's system, never the shape of the
//   last segment. `possession.segments` is recorded history and is shown
//   as exactly that; the client derives neither field from the other.
//
//   REFERENCES ARE EXACT. A step links to Evidence or Execution only through
//   its own `evidence_id` / `execution_node_id`, which are nullable. A null
//   reference is simply no link; nothing is matched by time, actor or label.
//
//   NO VERDICT. `completed` means the Work RECORD closed; `exception` means
//   an explicit exception record. No "success", "failed", "verified", score
//   or colour-as-judgement is produced here.

import { correlationLabel, observations, provenanceLine } from './evidence.js'
import { EVIDENCE_KIND_LABELS, connectorName, workerDisplay } from './execution.js'

/** The step vocabulary the endpoint emits, in presentation order of mention. */
export const STEP_TYPES = Object.freeze(['progress', 'handoff', 'wait', 'exception', 'completed'])

/**
 * A quiet tag per type. Deliberately not a verdict: "Record closed" is the
 * record ending, not the work succeeding; "Exception" is an explicit
 * exception record (a decline, a stall, a provider marking the object
 * stuck), not a failure grade.
 */
export const STEP_TYPE_LABELS = Object.freeze({
  progress: 'Progress',
  handoff: 'Passed on',
  wait: 'Waiting',
  exception: 'Exception',
  completed: 'Record closed',
  other: 'Step',
})

export const ACTOR_KIND_LABELS = Object.freeze({
  agent: 'Agent',
  human: 'Person',
  system: 'System',
})

const isObj = (v) => v !== null && typeof v === 'object' && !Array.isArray(v)
const str = (v) => (v === null || v === undefined ? null : String(v).trim() || null)

/**
 * A safe shape over the response: steps as the endpoint ordered them (its
 * chronology), each with an object `details` and `provenance`; the
 * possession block as given; the bound flags. A step of a type the client
 * does not know keeps its label under a neutral `other` tag — it is still a
 * step the endpoint said, never dropped and never re-typed.
 */
export function normalizeGraph(body) {
  const rawSteps = Array.isArray(body?.steps) ? body.steps : []
  const steps = rawSteps
    .filter((s) => isObj(s) && str(s.id))
    .map((s) => ({
      ...s,
      id: String(s.id),
      type: STEP_TYPES.includes(s.type) ? s.type : 'other',
      label: str(s.label) || STEP_TYPE_LABELS[STEP_TYPES.includes(s.type) ? s.type : 'other'],
      actor: isObj(s.actor) ? s.actor : null,
      system: isObj(s.system) ? s.system : null,
      details: isObj(s.details) ? s.details : {},
      provenance: isObj(s.provenance) ? s.provenance : {},
    }))
  const possession = isObj(body?.possession) ? body.possession : null
  return {
    steps,
    possession,
    bounded: Boolean(body?.bounded),
    spanLimit: Number.isFinite(Number(body?.span_limit)) ? Number(body.span_limit) : null,
  }
}

/** Whether the graph is the sparse-but-truthful zero-step record. */
export function isEmptyGraph(body) {
  return normalizeGraph(body).steps.length === 0
}

/**
 * Who performed or reported the step, as the endpoint named them. An agent's
 * stored label loses only its default `:main` suffix; a person is the
 * account-resolved name the server sent (or its own "a person"); a system
 * is the provider label or Trovis. Nothing is parsed out of an id.
 */
export function actorDisplay(actor) {
  if (!isObj(actor)) return null
  const type = actor.type === 'human' || actor.type === 'system' ? actor.type : 'agent'
  const label = str(actor.label)
  if (type === 'agent') return { kind: 'agent', label: workerDisplay(label) || label || 'Agent' }
  if (type === 'human') return { kind: 'human', label: label || 'A person' }
  return { kind: 'system', label: label || 'System' }
}

/**
 * The one line of recorded context under a step's label, or null. Only a
 * field the record carries, by type:
 *   handoff    the target as the server labelled it ("Sarah", "a person",
 *              another agent's worker label)
 *   wait       what it is waiting on, else the recorded reason
 *   exception  the recorded reason, else the recorded detail
 *   completed  nothing — the closure reason is bookkeeping, shown in details
 *   progress   the recorded reason or detail
 */
export function stepContext(step) {
  const d = step?.details || {}
  switch (step?.type) {
    case 'handoff': {
      const t = str(d.target_label)
      if (!t) return null
      return d.direction === 'to_agent' ? workerDisplay(t) || t : t
    }
    case 'wait':
      return str(d.waiting_on) || str(d.reason) || null
    case 'exception':
      return str(d.reason) || str(d.detail) || null
    case 'progress':
      return str(d.reason) || str(d.detail) || null
    default:
      return null
  }
}

const humanize = (v) => (str(v) ? String(v).replace(/_/g, ' ') : null)

function pair(label, value, opts = {}) {
  const v = str(value)
  return v === null ? null : { label, value: v, ...opts }
}

/**
 * Recorded context for the details area — plain words, only what the
 * record carries. Ids and correlation live in technicalRows instead.
 */
export function stepDetailRows(step) {
  const d = step?.details || {}
  const sys = step?.system
  const rows = []
  if (step?.type === 'handoff') {
    const t = str(d.target_label)
    rows.push(pair('To', t && d.direction === 'to_agent' ? workerDisplay(t) || t : t))
  }
  if (sys) rows.push(pair('System', sys.label))
  rows.push(pair('Waiting on', d.waiting_on))
  rows.push(pair('Reason', d.reason))
  rows.push(pair('Detail', d.detail))
  if (step?.type === 'completed') {
    rows.push(pair('Recorded as', humanize(d.reason)))
    if (d.abandoned === true) rows.push(pair('Closure', 'abandoned'))
  }
  // "Reason" and "Recorded as" would repeat each other on a closure.
  const out = rows.filter(Boolean)
  if (step?.type === 'completed') return out.filter((r) => r.label !== 'Reason')
  return out
}

/** The ids behind a step, for the folded "Technical details" block. */
export function stepTechnicalRows(step) {
  const p = step?.provenance || {}
  const d = step?.details || {}
  return [
    pair('Event id', p.event_id !== null && p.event_id !== undefined ? String(p.event_id) : null, { mono: true }),
    pair('Evidence record', p.evidence_id, { mono: true }),
    pair('Evidence kind', EVIDENCE_KIND_LABELS[p.evidence_kind] || p.evidence_kind),
    pair('Execution node', p.execution_node_id, { mono: true }),
    pair('Span id', p.span_id, { mono: true }),
    pair('Trace id', p.trace_id, { mono: true }),
    pair('Link', p.correlation ? correlationLabel(p.correlation) : null),
    pair('Source', p.source_type),
    pair('Connector', connectorName(p.source_connector_id)),
    pair('External object', p.external_object_id, { mono: true }),
    pair('Provider event id', p.external_event_id, { mono: true }),
    pair('Provider event', d.saas_event_type, { mono: true }),
    pair('Reference', d.handoff_id, { mono: true }),
  ].filter(Boolean)
}

// --- time -----------------------------------------------------------------------

function ms(iso) {
  const t = Date.parse(iso || '')
  return Number.isFinite(t) ? t : null
}

/** "10:03 AM" in the viewer's clock, or null. */
export function stepClock(iso) {
  const t = ms(iso)
  if (t === null) return null
  return new Date(t).toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' })
}

/** "Sep 18" in the viewer's calendar, or null. */
export function stepDay(iso) {
  const t = ms(iso)
  if (t === null) return null
  return new Date(t).toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
}

/**
 * The time label per step id. Clock time alone when every step falls on one
 * local day; day + clock when the record spans days, so "10:03 AM" cannot
 * quietly mean a different day from the row above it.
 */
export function stepTimeLabels(steps) {
  const days = new Set()
  for (const s of steps || []) {
    const d = stepDay(s.at)
    if (d) days.add(d)
  }
  const withDay = days.size > 1
  const out = new Map()
  for (const s of steps || []) {
    const clock = stepClock(s.at)
    const day = stepDay(s.at)
    out.set(s.id, clock ? (withDay && day ? `${day} · ${clock}` : clock) : null)
  }
  return out
}

// --- possession ---------------------------------------------------------------------

function holderLabel(seg) {
  const label = str(seg?.holder)
  if (!label) return null
  return seg.holder_type === 'agent' ? workerDisplay(label) || label : label
}

/**
 * Who has the work now — from `possession.current_holder` and nothing else.
 * Null when the endpoint names no current holder (a closed record, or no
 * possession block at all): the client never fills it from a step.
 *
 *   { label, kind, waiting, text }   text: "Held by Stripe · waiting"
 */
export function holderLine(possession) {
  const h = isObj(possession) && isObj(possession.current_holder) ? possession.current_holder : null
  if (!h) return null
  const label = holderLabel(h)
  if (!label) return null
  const waiting = Boolean(h.waiting)
  return {
    label,
    kind: h.holder_type === 'human' || h.holder_type === 'system' ? h.holder_type : 'agent',
    waiting,
    text: `Held by ${label}${waiting ? ' · waiting' : ''}`,
  }
}

/**
 * The possession history, one row per segment as the endpoint listed them,
 * for presentation only: holder, holder type, the segment's own `waiting`
 * flag, its recorded start and end. Order is the endpoint's; nothing here
 * says one segment caused the next, and nothing here says which segment is
 * current — an open-ended segment (`end: null`) is a recorded fact about
 * that segment, not a client judgement about possession. Who holds the work
 * now is `possession.current_holder` (holderLine) and nothing else.
 */
export function possessionRows(possession) {
  const segs = isObj(possession) && Array.isArray(possession.segments) ? possession.segments : []
  return segs
    .filter(isObj)
    .map((s, i) => ({
      key: `${i}:${s.start || ''}`,
      label: holderLabel(s) || 'Unknown holder',
      kind: s.holder_type === 'human' || s.holder_type === 'system' ? s.holder_type : 'agent',
      waiting: Boolean(s.waiting),
      start: s.start || null,
      end: s.end || null,
    }))
}

// --- the row, in a person's words ------------------------------------------------------------

/**
 * The first line of an Activity row: who, and — when the record names one —
 * who or what they passed the work to. "Chief of Staff → Alex" for a
 * handoff; "export-agent → warehouse" for a wait an agent declared on a
 * system; just "Stripe" when the system itself is the actor of its own
 * record; just the actor otherwise. The arrow is the record's target field,
 * never a guess about who acted next.
 */
export function stepHeadline(step) {
  const actor = actorDisplay(step?.actor)
  const who = actor?.label || null
  if (step?.type === 'handoff') {
    const to = stepContext(step)
    if (who && to) return `${who} → ${to}`
    return who || to || step?.label || 'Step'
  }
  if (step?.type === 'wait' || step?.type === 'exception') {
    const sys = str(step?.system?.label)
    if (who && sys && sys !== who) return `${who} → ${sys}`
    return who || sys || step?.label || 'Step'
  }
  return who || step?.label || 'Step'
}

/**
 * The second line: the endpoint's own label, then the one recorded detail
 * that adds something the headline does not already say — a wait's
 * `waiting_on`, an exception's reason. Nothing is reworded into an outcome.
 *
 * A handoff whose headline already reads "who → whom" does not repeat the
 * mechanics ("Handed to a person"): the line is the recorded reason when
 * one exists, else the plain "Handed off" — allowed only because the
 * endpoint itself typed the step `handoff`. Presentation deduplication;
 * nothing ("for review", "needs approval") is invented. A handoff with no
 * target in the headline keeps the endpoint's label so the row still says
 * what happened.
 */
export function stepLine(step) {
  const label = str(step?.label) || STEP_TYPE_LABELS[step?.type] || STEP_TYPE_LABELS.other
  const d = step?.details || {}
  if (step?.type === 'handoff') {
    const reason = str(d.reason)
    const who = actorDisplay(step.actor)?.label || null
    const to = stepContext(step)
    if (who && to) return reason || 'Handed off'
    return reason ? `${label} · ${reason}` : label
  }
  const extra = stepContext(step)
  return extra ? `${label} · ${extra}` : label
}

// The situation's eyebrow: the lean status in one word, and only that —
// no inference. Omitted when it would repeat the headline.
const STATUS_EYEBROWS = Object.freeze({
  waiting_on_you: 'Waiting',
  waiting_on_other: 'Waiting',
  moving: 'In progress',
  stuck: 'Needs attention',
  done: 'Closed',
})

/** "Waiting" / "In progress" / "Needs attention" / "Closed", or null. */
export function situationEyebrow(status, headline) {
  const word = STATUS_EYEBROWS[str(status)] || null
  if (!word) return null
  if (str(headline) && str(headline).toLowerCase() === word.toLowerCase()) return null
  return word
}

// --- the current situation -----------------------------------------------------------------

// The lean status, said plainly. `done` and `waiting_on_you` are handled
// before this table is read; these are the fallbacks when the endpoint names
// no current holder. None of them is a business verdict.
const STATUS_HEADLINES = Object.freeze({
  moving: 'In progress',
  waiting_on_other: 'Waiting on someone',
  stuck: 'Needs attention',
})

const CLOSED = 'Work record closed'

/**
 * ONE statement of the current situation, and at most one supporting
 * sentence — both built only from canonical fields:
 *
 *   status (lean detail)   `done` → the closure step's own label, else
 *                          "Work record closed" — the record ended, never
 *                          "completed" or "succeeded";
 *                          `waiting_on_you` → "Waiting for you" (the session
 *                          knows this; possession does not know the viewer).
 *   possession.current_holder   who has it: "Waiting for Alex" / "Waiting for
 *                          Stripe" when the holder is waiting; "Chief of Staff
 *                          is working on this" for an agent that is not;
 *                          "With Alex" otherwise. `stuck` reads "Needs
 *                          attention" over the holder.
 *   no current holder      the status alone ("In progress", "Waiting on
 *                          someone", "Needs attention") — never a holder
 *                          inferred from a segment, a step or an actor.
 *
 * The supporting sentence exists only when the LAST Work Step the endpoint
 * recorded is itself the record that explains the holder: a handoff whose
 * `target_label` IS the current holder ("Chief of Staff handed this to Alex"),
 * a wait whose `system` IS the current holder (its label and recorded
 * `waiting_on`), or, for a stuck run, an exception whose system or actor IS
 * the holder. Anything else — a holder the last step does not name — gets no
 * sentence at all. Chronology is read here only to pick that last record;
 * nothing says it caused anything.
 *
 * Returns { headline, support: { text, at } | null } or null before the
 * status is known.
 */
export function situationFor(view, graph) {
  const status = str(view?.status)
  const g = normalizeGraph(graph)
  const steps = g.steps
  const lastRecorded = steps.length ? steps[steps.length - 1] : null
  const held = holderLine(g.possession)
  const ago = (s) => (s?.at ? s.at : null)

  if (status === 'done') {
    const closure = [...steps].reverse().find((s) => s.type === 'completed') || null
    const closer = actorDisplay(closure?.actor)?.label || null
    return {
      headline: str(closure?.label) || CLOSED,
      support: closure && closer ? { text: `${closer} closed the record`, at: ago(closure) } : null,
    }
  }

  if (status === 'waiting_on_you') {
    const fromHandoff = lastRecorded?.type === 'handoff' && lastRecorded.details?.direction === 'to_human'
      ? actorDisplay(lastRecorded.actor)?.label || null
      : null
    return {
      headline: 'Waiting for you',
      support: fromHandoff ? { text: `${fromHandoff} handed this to you`, at: ago(lastRecorded) } : null,
    }
  }

  if (held) {
    const who = held.label
    const explains = lastRecorded ? recordNamesHolder(lastRecorded, who) : null
    if (status === 'stuck') {
      return {
        headline: 'Needs attention',
        support: explains && lastRecorded.type === 'exception'
          ? { text: stepLine(lastRecorded), at: ago(lastRecorded) }
          : { text: `${who} has this${held.waiting ? ' and is waiting' : ''}`, at: null },
      }
    }
    if (held.waiting) {
      let support = null
      if (explains && lastRecorded.type === 'handoff') {
        support = { text: `${actorDisplay(lastRecorded.actor)?.label || 'Someone'} handed this to ${who}`, at: ago(lastRecorded) }
      } else if (explains && lastRecorded.type === 'wait') {
        // The system's own record ("Stripe: payment processing") or the
        // agent's declared wait on it — the recorded waiting_on / reason.
        const by = actorDisplay(lastRecorded.actor)?.label || null
        const what = stepContext(lastRecorded)
        if (by && by !== who) support = { text: `${by} is waiting on ${who}${what ? ` · ${what}` : ''}`, at: ago(lastRecorded) }
        else if (what) support = { text: `${who}: ${what}`, at: ago(lastRecorded) }
      }
      return { headline: `Waiting for ${who}`, support }
    }
    return {
      headline: held.kind === 'agent' ? `${who} is working on this` : `With ${who}`,
      support: null,
    }
  }

  if (!status) return null
  return { headline: STATUS_HEADLINES[status] || 'In progress', support: null }
}

/** Whether this step's own record names `who` as where the work went. */
function recordNamesHolder(step, who) {
  if (!step || !who) return false
  if (step.type === 'handoff') return stepContext(step) === who
  if (step.type === 'wait') return str(step.system?.label) === who
  if (step.type === 'exception') {
    return str(step.system?.label) === who || actorDisplay(step.actor)?.label === who
  }
  return false
}

// --- notes ------------------------------------------------------------------------------

/**
 * The bounded-read sentence, or null. Loop events are read whole, so the
 * steps are complete even when the supporting span read was capped; the
 * note says exactly that much and never calls the timeline incomplete.
 */
export function boundedNote(body) {
  if (!body?.bounded) return null
  return 'Some supporting execution detail is outside this read.'
}

// --- evidence links ---------------------------------------------------------------------

/** The Evidence record with exactly this id, from the body the page already loaded, or null. */
export function evidenceRecordFor(evidenceBody, id) {
  if (!str(id)) return null
  const list = Array.isArray(evidenceBody?.evidence) ? evidenceBody.evidence : []
  return list.find((r) => isObj(r) && r.id === id) || null
}

/**
 * Whether the Evidence section renders a row for this exact id. Handoff
 * records are counted in Evidence's sources rather than drawn as rows, so a
 * handoff step's record exists without a row to scroll to; then the step
 * shows the record inline (supportLine) and offers no "View evidence".
 */
export function evidenceRowShown(evidenceBody, id) {
  if (!str(id)) return false
  const list = Array.isArray(evidenceBody?.evidence) ? evidenceBody.evidence : []
  return observations(list).some((o) => o.id === id)
}

// What kind of record supports a step, as a noun phrase; the provenance
// phrase after it is Evidence's own ("Observed from Stripe").
const SUPPORT_NOUNS = Object.freeze({
  handoff: 'a handoff record',
  external_state: "an external system's own record",
  completion: 'the closure record',
  action_reported: 'a reported action',
  execution: 'worker telemetry',
  cost: 'a cost record',
})

/** "a handoff record — Recorded by Ada Lovelace" — the record, in Evidence's own words. */
export function supportLine(rec) {
  if (!isObj(rec)) return null
  const noun = SUPPORT_NOUNS[rec.evidence_type] || null
  const prov = provenanceLine(rec)
  return noun ? `${noun} — ${prov}` : prov
}
