// The job page: a PATTERN, not an instance.
//
// Everything here is an aggregate over many runs, and it has to LOOK like
// one. A run page states facts about one thing that happened; this page
// states averages, and the difference has to survive a glance — hence the
// provenance line that never leaves the diagram's side, and hence the health
// section refusing to grade anything without a declared number to grade it
// against.
//
// Pure, so the rules are testable without mounting anything.

import { kindPath, pathBasis } from './board.js'
import { ageLabel, durationLabel, numOrNull, observedPerDay } from './workBoard.js'

/** "$4.12" / "$0.0042" — null when there is nothing real to show. */
export function costLabel(v) {
  const n = numOrNull(v)
  if (n === null || n <= 0) return null
  return n < 0.01 ? `$${n.toFixed(4)}` : `$${n.toFixed(2)}`
}

/**
 * "Shape from 7 recent runs; intervention over 2 closed runs, last 14 days."
 *
 * This sentence is the whole reason the diagram is allowed to exist. It sits
 * with the diagram always — an average with no stated basis is an assertion,
 * and on a page one click from a page of recorded facts, the reader needs to
 * know which kind of claim they are looking at.
 *
 * It names TWO bases because the diagram genuinely has two. The shape comes
 * from the runs on this page, every state, including the ones still open; the
 * percentage on the human branch is measured over CLOSED runs in the window,
 * because a run that has not finished cannot yet be said to have needed a
 * person or not. Printing one number over both would be the exact failure
 * this line exists to prevent.
 *
 * The intervention clause is dropped when nothing has closed — there is no
 * rate over zero runs — and the whole line is null when there is no shape to
 * account for.
 */
export function computedFrom(job, runs) {
  const shape = pathBasis(runs)
  if (!shape) return null
  const lead = `Shape from ${shape} recent ${shape === 1 ? 'run' : 'runs'}`
  const closed = numOrNull(job?.closed_runs)
  const days = numOrNull(job?.window_days)
  if (closed === null || closed <= 0 || days === null || days <= 0) return `${lead}.`
  return `${lead}; intervention over ${closed} closed ${closed === 1 ? 'run' : 'runs'}, last ${days} days.`
}

/**
 * The typical route, with the one branch worth naming.
 *
 * The question this diagram answers is "where does this job usually go
 * differently", so it carries the main path and the divergence and nothing
 * else. Per-node cost and duration are deliberately absent: that detail
 * belongs to individual steps on a run page, and stacking boxes with costs,
 * durations and percentages turns this into the raw observability view the
 * product exists to replace.
 *
 * The DIVERGENCE is the human node — the branch where a person had to step
 * in — and its frequency is the intervention rate. That is not a coincidence
 * to be recomputed: it is the same number the health section reports, read
 * from the same field, so the two cannot drift apart.
 *
 * A MEASURED ZERO is not a divergence. When the closed runs all went through
 * without a person, the human node still appears — people are holding open
 * runs right now, which is why it is in the shape at all — but it is not
 * marked as the branch, because the branch is a claim about frequency and
 * the frequency is nil. It still prints its 0%: the reader is owed the
 * measurement, and the provenance line says what it was measured over.
 */
export function jobPath(job, runs) {
  const nodes = kindPath(runs)
  if (!nodes) return null
  return nodes.map((n) => {
    if (n.kind !== 'human') return { kind: n.kind, label: n.label, isDivergence: false, pct: null }
    const pct = numOrNull(job?.intervention_pct)
    return { kind: n.kind, label: n.label, isDivergence: pct !== null && pct > 0, pct }
  })
}

/**
 * The four facts at the top: last run, cadence, median close, cost per run.
 *
 * Each is `{ label, value }` with value null when the record cannot say. A
 * null renders as an em dash, never as zero — a job that has never closed a
 * run has no median, and printing "0s" would be a measurement of something
 * that did not happen.
 */
export function jobStats(job, { now = Date.now() } = {}) {
  const last = ageLabel(job?.last_run_at, now)
  const perDay = observedPerDay(job)
  return [
    { key: 'last', label: 'Last run', value: last ? `${last} ago` : null },
    { key: 'cadence', label: 'Cadence', value: perDay === null ? null : `${perDay}/day` },
    { key: 'close', label: 'Median close', value: durationLabel(job?.median_close_s) },
    { key: 'cost', label: 'Cost per run', value: costLabel(job?.cost_per_run) },
  ]
}

// The four things a job can declare, and the observed number each is read
// against. Order is fixed: volume, then speed, then how often a person was
// needed, then how often it failed.
const METRICS = [
  { key: 'volume', label: 'Volume' },
  { key: 'close', label: 'Close time' },
  { key: 'intervention', label: 'Intervention' },
  { key: 'failure', label: 'Failure rate' },
]

/**
 * The health section: four rows, each naming the number it is derived from.
 *
 * `{ key, label, observed, expected, over }`. `expected` is null when nothing
 * was declared — the row still shows what the record observed, with the
 * comparison column simply empty. That is the whole rule: a verdict needs a
 * declared number, an observation does not, and the page never turns the
 * second into the first.
 *
 * `over` marks a breach, and is only ever true when BOTH numbers exist.
 */
export function healthRows(job) {
  const perDay = observedPerDay(job)
  const closeS = numOrNull(job?.median_close_s)
  const iv = numOrNull(job?.intervention_pct)
  const fail = numOrNull(job?.failure_pct)
  const minD = numOrNull(job?.expected_per_day_min)
  const maxD = numOrNull(job?.expected_per_day_max)
  const maxClose = numOrNull(job?.expected_close_s)
  const maxIv = numOrNull(job?.expected_intervention_pct)
  const maxFail = numOrNull(job?.expected_failure_pct)

  const rows = {
    volume: {
      observed: perDay === null ? null : `${perDay}/day`,
      expected:
        minD !== null && maxD !== null ? `expected ${minD}–${maxD}`
          : minD !== null ? `expected ${minD}+`
            : maxD !== null ? `expected under ${maxD}` : null,
      over: perDay !== null && ((minD !== null && perDay < minD) || (maxD !== null && perDay > maxD)),
    },
    close: {
      observed: durationLabel(closeS),
      expected: maxClose === null ? null : `expected under ${durationLabel(maxClose)}`,
      over: closeS !== null && maxClose !== null && closeS > maxClose,
    },
    intervention: {
      observed: iv === null ? null : `${iv}%`,
      expected: maxIv === null ? null : `expected under ${maxIv}%`,
      over: iv !== null && maxIv !== null && iv > maxIv,
    },
    failure: {
      observed: fail === null ? null : `${fail}%`,
      expected: maxFail === null ? null : `expected under ${maxFail}%`,
      over: fail !== null && maxFail !== null && fail > maxFail,
    },
  }
  return METRICS.map((m) => ({ ...m, ...rows[m.key] }))
}

/**
 * The technical block, below the fold — and only what is actually declared.
 *
 * A settings list padded with "—" for every field the schema never got is a
 * list of promises. Undeclared fields are omitted entirely; the ones that
 * exist appear with their value.
 *
 * `agentRoute` is service_name + agent_id when both are present, because a
 * display label must never become a URL (agentRoute.js).
 */
export function settingsRows(job) {
  const out = []
  const push = (label, value) => {
    const v = typeof value === 'string' ? value.trim() : value
    if (v !== null && v !== undefined && v !== '') out.push({ label, value: v })
  }
  const owner = String(job?.owning_service_name || '').trim()
  if (owner) {
    out.push({
      label: 'Owning agent',
      value: owner,
      route: [owner, String(job?.owning_agent_id || '').trim() || 'main'],
    })
  }
  push('Approval routing', job?.approval_routing)
  const stall = numOrNull(job?.stall_threshold_s)
  push('Stall threshold', stall === null ? null : durationLabel(stall))
  // How runs are RECOGNISED as instances of this job. It is the closest
  // thing the record has to a trigger, and calling it one would overstate
  // it: nothing here fires the job, it only claims runs that already ran.
  const hints = Array.isArray(job?.match_hints) ? job.match_hints : []
  for (const h of hints) {
    if (!h?.field) continue
    push('Recognised by', `${h.field} ${h.op || 'equals'} ${h.value ?? ''}`.trim())
  }
  const tools = new Set()
  for (const s of Array.isArray(job?.stations) ? job.stations : []) {
    for (const t of String(s?.tools || '').split(',')) {
      const name = t.trim()
      if (name) tools.add(name)
    }
  }
  if (tools.size) push('Tools', [...tools].join(', '))
  push('Version', job?.current_version == null ? null : `v${job.current_version}`)
  return out
}

/**
 * Recent runs, newest first — every state, not just the closed ones.
 *
 * The job page's list is a sample of what this job has been doing lately, so
 * a stuck run and a finished one belong side by side: a repeated failure
 * mode is only visible as a cluster if the failures are in the same list as
 * the successes.
 */
export function recentRuns(runs, { limit = 12 } = {}) {
  return [...(runs || [])]
    .filter((r) => r && r.id != null)
    .sort((a, b) => (Date.parse(b.updated_at || '') || 0) - (Date.parse(a.updated_at || '') || 0))
    .slice(0, limit)
}
