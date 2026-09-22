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
import { NO_DATA, ageLabel, durationLabel, numOrNull, observedPerDay } from './workBoard.js'
import { stationHeat } from './loops.js'

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
 * The four facts at the top: last run, runs per day, typical time to finish,
 * cost per run.
 *
 * Each is `{ label, value, sub? }` with value null when the record cannot
 * say. A null renders as words, never as zero — a job that has never finished
 * a run has no typical time, and printing "0s" would be a measurement of
 * something that did not happen.
 *
 * The labels are the words a manager would use, not the statistician's:
 * "Runs per day" where the code says cadence, "Typical time to finish" where
 * it says median close. The sub-lines carry the basis — how many runs the
 * rate is over, how many the median is over — so the number is never a
 * bare assertion.
 */
export function jobStats(job, { now = Date.now() } = {}) {
  const last = ageLabel(job?.last_run_at, now)
  const perDay = observedPerDay(job)
  const started = numOrNull(job?.started_runs)
  const closed = numOrNull(job?.closed_runs)
  const days = numOrNull(job?.window_days)
  return [
    { key: 'last', label: 'Last run', value: last ? `${last} ago` : null },
    {
      key: 'perDay',
      label: 'Runs per day',
      value: perDay === null ? null : `${perDay}/day`,
      sub: started !== null && days !== null && days > 0
        ? `${started} ${started === 1 ? 'run' : 'runs'} in the last ${days} days`
        : null,
    },
    {
      key: 'finish',
      label: 'Typical time to finish',
      value: durationLabel(job?.median_close_s),
      sub: closed !== null && closed > 0 ? `over ${closed} finished ${closed === 1 ? 'run' : 'runs'}` : null,
      // The reason, when the reason is known: nothing has finished, so there
      // is nothing to time. Better than "No data", which reads as a fault.
      empty: closed === 0 ? NO_FINISHED : null,
    },
    { key: 'cost', label: 'Cost per run', value: costLabel(job?.cost_per_run), sub: costBasis(job) },
  ]
}

/** What an unmeasurable finish-side number says when the cause is known. */
export const NO_FINISHED = 'No finished runs yet'

/**
 * What the cost per run is an average OF: "over 52 of 59 runs".
 *
 * `cost_per_run` is `cost_usd / cost_runs`, and `cost_runs` is the runs in
 * the window that carried any recorded activity at all — not every run that
 * started. An average over part of the population is not an average over
 * the job, and the difference is invisible without the denominator, so the
 * server's own two counts (same window, same population) are printed. Null
 * when there is no average (the value already reads No data) and when the
 * basis is the whole window — a line that says "all" adds nothing.
 * Cost is never gated on Work: a person who can see the job can see what
 * its runs cost; the basis is the only thing the number is owed.
 */
export function costBasis(job) {
  const over = numOrNull(job?.cost_runs)
  const started = numOrNull(job?.started_runs)
  if (over === null || over <= 0 || started === null || started <= 0) return null
  if (over >= started) return null
  return `over ${over} of ${started} runs`
}

// The four things a job can declare, and the observed number each is read
// against. Order is fixed: how often it runs, then how long it takes, then
// how often a person was needed, then how often it failed. Plain words: a
// manager reads "Needed a person", not "Intervention".
const METRICS = [
  { key: 'volume', label: 'Runs per day' },
  { key: 'close', label: 'Time to finish' },
  { key: 'intervention', label: 'Needed a person' },
  { key: 'failure', label: 'Failed' },
]

/**
 * The expectations section: one row per metric, each naming the number it is
 * derived from.
 *
 * `{ key, label, observed, expected, over, noData, declared }`. `expected` is
 * null when nothing was declared — the row still carries what the record
 * observed, with the comparison column simply empty, and `declared` is false
 * so the page can leave the row out when the card is about what was
 * declared. That is the whole rule: a verdict needs a declared number, an
 * observation does not, and the page never turns the second into the first.
 *
 * `over` marks a breach, and is only ever true when BOTH numbers exist.
 *
 * `noData` is rule 6: the observation is missing. It renders as words, not
 * an em dash — a dash is punctuation the reader has to interpret, and next
 * to three rows carrying numbers it reads as a small value rather than as no
 * value. The words name the CAUSE where it is known: the three finish-side
 * numbers are measured over finished runs, so when none has finished they
 * read "No finished runs yet" rather than the bare "No data". It is set
 * whether or not an expectation was declared, but it MATTERS most where one
 * was: that row is a check that did not run, and the badge refuses to call
 * the job healthy while it stands.
 */
export function healthRows(job) {
  const closed = numOrNull(job?.closed_runs)
  const finishSide = closed === 0 ? NO_FINISHED : NO_DATA
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
  return METRICS.map((m) => {
    const r = rows[m.key]
    return {
      ...m,
      ...r,
      observed: r.observed === null ? (m.key === 'volume' ? NO_DATA : finishSide) : r.observed,
      noData: r.observed === null,
      declared: r.expected !== null,
    }
  })
}

/**
 * How this job runs, drawn: the declared steps with today's work placed on
 * them, or — when nobody has declared enough steps to draw — the route the
 * record observed.
 *
 *   { mode: 'declared', steps: [{ kind, who, label, tools, carrier, heat }],
 *     doneToday, offPath, live }
 *   { mode: 'observed' }       → the caller draws `jobPath` with its provenance
 *   { mode: 'none' }           → nothing to draw yet
 *
 * A declared drawing needs at least TWO steps. A single step is the owning
 * agent named once, which every derived job carries and which tells the
 * reader nothing they did not already see in the header. That single step
 * is the noise this rule removes.
 *
 * `heat` is where the open runs are right now — `{ count, oldestS }` from
 * the live map, or null when the map has not loaded or nothing is there.
 * `offPath` counts open runs the map could not place on these steps: they
 * are real work, and a drawing that hid them would be claiming the steps
 * are complete when the record says otherwise. `live` says whether a map
 * arrived at all, so the drawing can say "where the work is" only when it
 * knows.
 */
export function jobFlow(job, map, { now = Date.now() } = {}) {
  const raw = Array.isArray(job?.stations) ? job.stations.filter((s) => s && typeof s === 'object') : []
  if (raw.length < 2) {
    // The one name the record has for who does this job, so the page can say
    // "every run stayed with X" instead of pretending it has no runs.
    const s = raw[0]
    const solo = String(s?.holder || job?.owning_service_name || job?.derived_from || '').trim()
    return { mode: 'observed', solo: solo || null }
  }
  const live = Boolean(map && Array.isArray(map.loops))
  const heat = live ? stationHeat(map.loops, now) : new Map()
  const steps = raw.map((s, i) => {
    const kind = s.holder_type === 'human' ? 'human' : s.holder_type === 'system' ? 'tool' : 'agent'
    const who = String(s.holder || '').trim()
      || (kind === 'human' ? 'A person' : kind === 'tool' ? 'A system' : 'An agent')
    return {
      kind,
      who,
      label: String(s.label || '').trim(),
      // The API stores tools as a list; the editor's draft is a comma string.
      tools: (Array.isArray(s.tools) ? s.tools : String(s.tools || '').split(','))
        .map((t) => String(t).trim()).filter(Boolean),
      carrier: String(s.carrier || '').trim(),
      heat: heat.get(i) || null,
    }
  })
  const offPath = live ? map.loops.filter((l) => l?.position?.status === 'off_path').length : 0
  const doneToday = live ? numOrNull(map.done_today) : null
  return { mode: 'declared', steps, doneToday, offPath, live }
}

/**
 * "2 here · 3h" — who is holding work at a step, and how long the oldest of
 * it has been there. Copied in words, so the drawing never shows a bare count.
 */
export function flowHeatLabel(heat) {
  if (!heat || !heat.count) return null
  const base = `${heat.count} here`
  return heat.oldestS != null ? `${base} · ${ageWords(heat.oldestS)}` : base
}

function ageWords(seconds) {
  const s = Math.max(0, Math.floor(seconds))
  if (s < 60) return 'under a minute'
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h`
  return `${Math.floor(h / 24)}d`
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
 * The steps the operator DECLARED, in order — who holds the work at each.
 * Read off the job's current version; empty until somebody declares them.
 * Kept here so the job page never spells the internal field name.
 */
export function declaredSteps(job) {
  const raw = Array.isArray(job?.stations) ? job.stations : []
  return raw
    .filter((s) => s && typeof s === 'object')
    .map((s) => ({
      kind: s.holder_type === 'human' ? 'human' : s.holder_type === 'system' ? 'tool' : 'agent',
      label: String(s.label || s.holder || s.holder_type || '').trim(),
      holder: s.label && s.holder ? String(s.holder).trim() : '',
    }))
    .filter((s) => s.label)
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
