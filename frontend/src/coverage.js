// Work Coverage, in a manager's words: "what parts of this run can Trovis
// actually see?" Pure — no React, no DOM.
//
// GET /work/items/:id/coverage (work_coverage.py) answers per dimension
// with one of four states. This module only translates; it never
// reinterprets. Two rules carry the section:
//
//   UNKNOWN IS NOT MISSING. "Unknown" means the record cannot establish
//   whether the dimension applied or was seen — maybe nothing happened
//   there, maybe it happened unobserved. The copy never picks one.
//
//   NO VERDICT. Five dimensions are not a checklist; nothing here counts,
//   scores, grades or colours them into an overall answer.
//
// Only cost can read "partially observed" or "not observed": it is the one
// dimension whose denominator is stored (spans that carried model usage).
// Those spans are named as the backend names them — observed model usage —
// never "model calls", which the record cannot prove for arbitrary
// exporters. Counts stay out of the row; the sentence is enough.

/** Display order — fixed, the same as the API's. */
export const DIMENSION_ORDER = Object.freeze([
  'execution', 'actions', 'external_outcomes', 'handoffs', 'cost',
])

export const DIMENSION_LABELS = Object.freeze({
  execution: 'Execution',
  actions: 'Actions',
  external_outcomes: 'External outcomes',
  handoffs: 'Handoffs',
  cost: 'Cost',
})

export const STATE_LABELS = Object.freeze({
  observed: 'Observed',
  partial: 'Partially observed',
  not_observed: 'Not observed',
  unknown: 'Unknown',
})

// Deterministic copy per dimension and state. Absence is always "can't
// determine … from this record" — never "none", "missing" or "failed".
const OBSERVED = {
  execution: 'AI execution was observed for this run.',
  actions: 'Actions reported by the worker were observed.',
  external_outcomes: 'An external system reported state related to this run.',
  handoffs: 'Changes of responsibility were observed.',
}
const UNKNOWN = {
  execution: "Trovis can't determine execution visibility from this record.",
  actions: "Trovis can't determine action visibility from this record.",
  external_outcomes: "Trovis can't determine external outcome visibility from this record.",
  handoffs: "Trovis can't determine handoff visibility from this record.",
  cost: "Trovis can't determine cost visibility from this record.",
}
// Cost is keyed on the backend's reason, which carries the denominator fact.
const COST = {
  usage_cost_known: 'Cost is recorded for the observed model usage.',
  reported_cost: 'A run-level cost was reported.',
  some_usage_cost_unknown: 'Cost is recorded for some of the observed model usage.',
  usage_cost_unknown: "Model usage was observed, but its cost wasn't recorded.",
  no_model_usage_observed: UNKNOWN.cost,
}

/** One row's words: { id, label, state, stateLabel, text }. */
export function dimensionRow(dim) {
  const id = dim?.id
  const state = STATE_LABELS[dim?.state] ? dim.state : 'unknown'
  let text
  if (id === 'cost') {
    text = COST[dim?.reason] || (state === 'observed'
      ? COST.usage_cost_known
      : state === 'partial'
        ? COST.some_usage_cost_unknown
        : state === 'not_observed'
          ? COST.usage_cost_unknown
          : UNKNOWN.cost)
  } else {
    text = state === 'observed' ? OBSERVED[id] : UNKNOWN[id]
  }
  return {
    id,
    label: DIMENSION_LABELS[id] || String(id || ''),
    state,
    stateLabel: STATE_LABELS[state],
    text: text || UNKNOWN[id] || '',
  }
}

/**
 * The five rows in display order, from the response. A dimension the
 * response lacks is not invented — it is simply absent, so the section
 * never shows an "Unknown" the server did not say.
 */
export function visibilityRows(body) {
  const dims = Array.isArray(body?.dimensions) ? body.dimensions : []
  const byId = new Map(dims.map((d) => [d?.id, d]))
  return DIMENSION_ORDER.filter((id) => byId.has(id)).map((id) => dimensionRow(byId.get(id)))
}

/** The bounded-read note, or null. A cap on the read, never a verdict. */
export function boundedNote(body) {
  if (!body?.evidence_bounded) return null
  return 'Based on a bounded set of this run’s observations; later observations were not read.'
}
