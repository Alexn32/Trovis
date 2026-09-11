// Home, as rules — pure functions, no React, so what a person is told about
// imperfect data is unit-testable without a renderer.
//
// The whole file exists to keep one distinction the backend works hard to
// establish, and which a careless renderer would throw away in a line of JSX:
//
//     a recorded ZERO is not the same as a number we could not establish,
//     and neither is the same as a number we can only put a FLOOR under.
//
// `/home/snapshot` says which of the three it is, per block, via `exact`,
// `qualifier`, `absence_established`, `scope_state` and the `completeness`
// summary. Nothing here invents a value; every function reads what the
// contract supplied and decides how to SAY it.
//
// Nothing here fetches. Callers pass the parsed response in.

// ---------------------------------------------------------------------------
// Counts: the three kinds of number
// ---------------------------------------------------------------------------

/**
 * How to render one count from a block that carries `exact` / `qualifier`.
 *
 * Returns `{ kind, value, text, note }` where kind is:
 *   'exact'    the record established this number
 *   'floor'    the search was bounded; this is "at least N", never a total
 *   'unknown'  it could not be established at all — NOT zero
 *
 * The caller renders `text`; `kind` drives the styling and the affordance.
 */
export function readCount(value, block, { unknownText = 'Not established' } = {}) {
  const n = typeof value === 'number' && Number.isFinite(value) ? value : null
  if (n === null) {
    return { kind: 'unknown', value: null, text: unknownText, note: null }
  }
  const exact = block?.exact !== false && block?.qualifier !== 'at_least'
  if (exact) return { kind: 'exact', value: n, text: formatCount(n), note: null }
  return {
    kind: 'floor',
    value: n,
    text: `${formatCount(n)}+`,
    note: 'At least this many — the search behind it was bounded.',
  }
}

export function formatCount(n) {
  if (typeof n !== 'number' || !Number.isFinite(n)) return '—'
  return n.toLocaleString()
}

/**
 * Is a ZERO in this response allowed to be read as "none exists"?
 *
 * `absence_established: false` means the scope's membership was incomplete, so
 * finding nothing established nothing — the row that would have been found may
 * be one the capped scan never read. A UI that prints "0" there is asserting
 * something the server explicitly refused to assert.
 */
export function zeroMeansNone(snapshot) {
  return (snapshot?.completeness?.absence_established ?? true) === true
}

// ---------------------------------------------------------------------------
// Which empty is this?
// ---------------------------------------------------------------------------

/**
 * The state the Work-getting-done section is actually in.
 *
 * Four different emptinesses, and conflating them is how a product tells
 * somebody their team did nothing when it simply could not see their team:
 *
 *   'first-run'      the ACCOUNT has no recorded work at all
 *   'scope-empty'    this scope has none, and the search was complete
 *   'scope-unknown'  this scope has none FOUND, and the search was not complete
 *   'populated'      there is work to show
 */
export function workState(snapshot) {
  const c = snapshot?.completeness || {}
  if (c.workspace_state === 'empty' || c.has_any_recorded_work === false) {
    return 'first-run'
  }
  if (c.scope_state === 'unknown') return 'scope-unknown'
  if (c.scope_state === 'empty') return 'scope-empty'
  return 'populated'
}

/** Copy for each of those, keyed off the selected work scope for accuracy. */
export function workEmptyCopy(state, { effective = 'everyone' } = {}) {
  const whose =
    effective === 'me' ? 'your work'
    : effective === 'team' ? "your team's work"
    : effective === 'person' ? "this person's work"
    : 'this workspace'
  switch (state) {
    case 'first-run':
      return {
        lead: 'No work has been recorded yet',
        sub: 'Connect an agent and Trovis will start recording what it does.',
        action: 'connect',
      }
    case 'scope-empty':
      return {
        lead: `Nothing was completed in ${whose} this period`,
        sub: 'The record is complete for this scope — there simply is nothing here.',
        action: null,
      }
    case 'scope-unknown':
      return {
        lead: `No completions found in ${whose} this period`,
        sub:
          'Trovis could not read the whole scope, so this is not the same as ' +
          'nothing having happened. Widen the scope or the period to see more.',
        action: null,
      }
    default:
      return null
  }
}

// ---------------------------------------------------------------------------
// The completion chart
// ---------------------------------------------------------------------------

/**
 * What to draw for the completion series, and what to say when we cannot.
 *
 * The rule that matters: history we do not have is NOT a flat zero line. A
 * chart of zeroes is a claim that nothing happened, and an unavailable series
 * makes no such claim. Returns `{ available, points, max, total, partial,
 * reason }`.
 */
export function readSeries(snapshot) {
  const s = snapshot?.completions_series || {}
  const points = Array.isArray(s.points) ? s.points : []
  if (s.available === false || !points.length) {
    return {
      available: false,
      points: [],
      max: 0,
      total: null,
      partial: false,
      reason:
        s.unavailable_reason ||
        (points.length ? null : 'no_series_for_this_period'),
    }
  }
  const values = points.map((p) => (typeof p?.completed === 'number' ? p.completed : 0))
  // `completion_series_complete` is derived from membership AND retrieval AND
  // reconciliation, so a chart that reconciles with an aggregate drawn from the
  // same partial membership is still not a complete chart.
  const partial =
    s.exact === false ||
    s.qualifier === 'at_least' ||
    (snapshot?.completeness?.completion_series_complete ?? true) === false
  return {
    available: true,
    points: points.map((p, i) => ({
      bucketStart: p?.bucket_start || p?.bucket_start_utc || null,
      completed: values[i],
    })),
    max: Math.max(...values, 0),
    total: typeof s.total === 'number' ? s.total : null,
    partial,
    reason: null,
  }
}

/** Short axis label for one bucket, in the period's own timezone. */
export function bucketLabel(iso, locale = undefined, timeZone = undefined) {
  if (!iso) return ''
  const t = Date.parse(iso)
  if (Number.isNaN(t)) return ''
  try {
    return new Date(t).toLocaleDateString(locale, {
      weekday: 'short', timeZone,
    })
  } catch {
    return new Date(t).toLocaleDateString(locale, { weekday: 'short' })
  }
}

/**
 * The job breakdown as bars, with the tail and the unclassified work kept
 * VISIBLE rather than folded away.
 *
 * Work the matcher never claimed is the row most likely to be quietly dropped
 * by a renderer, and it is exactly the row an operator needs to see.
 */
export function readJobs(snapshot) {
  const b = snapshot?.by_job || {}
  const rows = (Array.isArray(b.rows) ? b.rows : []).map((r) => ({
    id: r?.workflow_id ?? null,
    name: r?.name || 'Unnamed job',
    completed: typeof r?.completed === 'number' ? r.completed : 0,
    kind: 'job',
  }))
  const extra = []
  if (b.other_completed > 0) {
    extra.push({
      id: null, kind: 'other', completed: b.other_completed,
      name: `${b.total_job_count > rows.length
        ? b.total_job_count - rows.length : 'Other'} other jobs`,
    })
  }
  if (b.unclassified_completed > 0) {
    extra.push({
      id: null, kind: 'unclassified', completed: b.unclassified_completed,
      name: 'Not assigned to a job',
    })
  }
  const all = [...rows, ...extra]
  return {
    rows: all,
    max: Math.max(...all.map((r) => r.completed), 0),
    truncated: Boolean(b.truncated),
    partial:
      b.exact === false ||
      b.qualifier === 'at_least' ||
      (snapshot?.completeness?.job_breakdown_complete ?? true) === false,
    empty: all.length === 0,
  }
}

/**
 * The previous-period comparison, or null.
 *
 * Rendered ONLY when the contract says the windows are comparable. The server
 * withholds it when the scope's membership is incomplete, because a delta
 * between two lower bounds is not a lower bound on the delta.
 */
export function readComparison(snapshot) {
  const c = snapshot?.period?.comparison || {}
  if (!c.available) return null
  if ((snapshot?.completeness?.comparison_available ?? false) !== true) return null
  const delta = typeof c.delta === 'number' ? c.delta : null
  if (delta === null) return null
  return {
    delta,
    previous: typeof c.previous_completed === 'number' ? c.previous_completed : null,
    direction: delta > 0 ? 'up' : delta < 0 ? 'down' : 'flat',
    text:
      delta === 0 ? 'Same as the period before'
      : `${delta > 0 ? '+' : '−'}${formatCount(Math.abs(delta))} vs the period before`,
  }
}

// ---------------------------------------------------------------------------
// Personal attention
// ---------------------------------------------------------------------------

/**
 * What is waiting on the SIGNED-IN PERSON.
 *
 * Three outcomes, and the middle one is the point: a machine session has no
 * person, so it gets no personal count at all rather than an invented one.
 * A bounded scan yields a floor, never a total, and "nothing needs you" is
 * the worst thing this block could get wrong.
 */
export function readAttention(snapshot) {
  const a = snapshot?.attention || {}
  if (a.available === false || a.viewer_user_id == null) {
    const reason = a.unavailable_reason || null
    return {
      kind: 'unavailable',
      count: null,
      atLeast: typeof a.needs_you_at_least === 'number' ? a.needs_you_at_least : null,
      reason,
      text:
        a.viewer_user_id == null
          ? 'Personal attention needs a signed-in person'
          : 'Trovis could not establish what is waiting on you',
    }
  }
  if (typeof a.needs_you !== 'number') {
    return {
      kind: 'unavailable',
      count: null,
      atLeast: typeof a.needs_you_at_least === 'number' ? a.needs_you_at_least : null,
      reason: a.unavailable_reason || null,
      text: 'Trovis could not establish what is waiting on you',
    }
  }
  if (a.resolution_complete === false) {
    return {
      kind: 'floor', count: a.needs_you, atLeast: a.needs_you, reason: null,
      text: `${formatCount(a.needs_you)}+`,
    }
  }
  return { kind: 'exact', count: a.needs_you, atLeast: null, reason: null,
           text: formatCount(a.needs_you) }
}

// ---------------------------------------------------------------------------
// Findings
// ---------------------------------------------------------------------------

export const FINDING_GROUPS = ['attention', 'opportunity', 'positive_change']

/**
 * Group findings by category, PRESERVING the server's order inside each group.
 *
 * The backend ranked these for this reader — by supported consequence,
 * urgency, responsibility and recurrence. Re-sorting client-side would replace
 * a ranking built from evidence with one built from whatever field was handy,
 * so this only partitions.
 */
export function groupFindings(findings) {
  const out = { attention: [], opportunity: [], positive_change: [] }
  for (const f of findings || []) {
    if (!f || typeof f !== 'object') continue
    if (f.category in out) out[f.category].push(f)
  }
  return out
}

/**
 * The qualification to show beside a finding, or null when none is warranted.
 *
 * Read off the finding's OWN coverage block — never invented from the
 * category, and never a severity. `qualified` means something real was found
 * and its explanation could not be established; that is publishable and it is
 * not a guess dressed up, so it is said plainly rather than as a warning.
 */
export function findingQualifier(finding) {
  const cov = finding?.coverage || {}
  const parts = []
  if (finding?.confidence === 'qualified') {
    parts.push(
      finding?.claim_kind === 'hypothesis'
        ? 'A proposed explanation, not an established one'
        : 'What was observed is solid; the explanation is not established',
    )
  }
  if (cov.retrieval_complete === false) {
    parts.push('Trovis could not read the whole scope for this')
  } else if (cov.counts_exact === false || cov.scope_membership_complete === false) {
    parts.push('Counts behind this are a floor, not a total')
  }
  if (cov.deadline_hit === true) parts.push('The analysis ran out of time')
  return parts.length ? parts.join(' · ') : null
}

// ---------------------------------------------------------------------------
// The analysis lifecycle
// ---------------------------------------------------------------------------

/**
 * How to present `analysis.state`, and — separately — whether to poll.
 *
 * The states are the backend's, exactly as defined. Two rules the shape
 * enforces, because both are ways a UI lies about AI:
 *
 *   * Absent findings are NOT "everything is healthy". Trovis abstaining means
 *     it looked and had nothing worth saying, which is a different sentence.
 *   * An unavailable or unsuccessful analysis must not sit under a spinner
 *     forever. Only `queued` and `running` are in-flight; everything else has
 *     settled, including the ones that settled badly.
 *
 * `poll` is true ONLY for the two genuinely in-flight states. `debounced`,
 * `incomplete` and `failed` are settled answers — polling them would be
 * retry pressure against a backend that has already told us to wait.
 */
export function readAnalysis(analysis, { findingCount = 0 } = {}) {
  const a = analysis || {}
  const state = a.state || 'unavailable'
  const fromPrevious = a.findings_from_previous_analysis === true
  const newer = a.newer_evidence_available === true
  const base = {
    state,
    poll: state === 'queued' || state === 'running',
    fromPrevious,
    newerEvidence: newer,
    gaps: Array.isArray(a.completion_gaps) ? a.completion_gaps : [],
    tone: 'muted',
    label: '',
    detail: null,
  }
  switch (state) {
    case 'current':
      return {
        ...base,
        label: findingCount ? 'Investigation up to date' : 'Trovis looked and found nothing to raise',
        detail: findingCount
          ? null
          : 'Not a health verdict — it means nothing here met the bar for a finding.',
      }
    case 'queued':
      return { ...base, label: 'Investigation queued', detail: 'Numbers above are already current.' }
    case 'running':
      return { ...base, label: 'Trovis is investigating', detail: 'Numbers above are already current.' }
    case 'debounced':
      return {
        ...base,
        label: newer ? 'New evidence is waiting to be analysed' : 'Analysed recently',
        detail: a.debounce_seconds
          ? `The next investigation can start in up to ${Math.round(a.debounce_seconds / 60)} min.`
          : 'The next investigation will cover what has arrived since.',
      }
    case 'incomplete':
      return {
        ...base,
        tone: 'warn',
        label: 'The last investigation did not finish',
        detail:
          (fromPrevious
            ? 'What is shown below is from an earlier analysis. '
            : '') + describeGaps(base.gaps),
      }
    case 'failed':
      return {
        ...base,
        tone: 'warn',
        label: 'The last investigation failed',
        detail:
          (fromPrevious ? 'Earlier findings are kept and still shown. ' : '') +
          (a.reason || 'Trovis will try again.'),
      }
    case 'unavailable':
      return {
        ...base,
        label:
          a.reason === 'no_model_configured'
            ? 'AI investigation is not configured'
            : 'AI investigation is unavailable',
        detail:
          a.reason === 'no_model_configured'
            ? 'The numbers above do not depend on it.'
            : null,
      }
    default:
      return { ...base, label: 'Investigation status unknown' }
  }
}

/** Plain English for the backend's completion-gap vocabulary. */
export function describeGaps(gaps) {
  const map = {
    candidates_not_examined: 'it ran out of time before checking everything',
    candidate_undecided: 'one question could not be decided',
    candidate_uncomposed: 'one result could not be written up',
    wording_withheld: 'one result could not be stated within the evidence',
    validation_rejected: 'one draft failed Trovis’s own checks',
  }
  const said = (gaps || []).map((g) => map[g]).filter(Boolean)
  if (!said.length) return 'Earlier findings are kept rather than retired.'
  return `Specifically, ${said.join('; ')}. Earlier findings are kept rather than retired.`
}

/**
 * Poll delay in ms, or null for "do not poll".
 *
 * Bounded and backing off: Home is not a progress bar for a model. After
 * `maxAttempts` in-flight checks it stops and leaves the settled label on
 * screen rather than spinning indefinitely.
 */
export function pollDelay(state, attempt, { base = 8000, maxAttempts = 8 } = {}) {
  if (state !== 'queued' && state !== 'running') return null
  if (attempt >= maxAttempts) return null
  return Math.min(base * Math.pow(1.5, attempt), 60000)
}

// ---------------------------------------------------------------------------
// Freshness and cost
// ---------------------------------------------------------------------------

/**
 * The three different "when" a reader can meaningfully be told apart.
 *
 * `retrieved` is when this page's numbers were read; `analysed` is when the
 * investigation behind the findings last ran; `source` is how current the
 * underlying record itself is. Showing one and calling it "updated" would
 * conflate a fast page load with a fresh record.
 */
export function readFreshness(snapshot, findings) {
  const f = snapshot?.freshness || {}
  return {
    retrieved: snapshot?.generated_at || null,
    analysed: findings?.analysis?.completed_at
      || findings?.analysis?.previous_analysis_at || null,
    sourceActivity: f.latest_work_activity_at || null,
    sourceTelemetry: f.latest_telemetry_at || null,
    lastCompletion: f.latest_recorded_completion_at || null,
    // A null timestamp under an incomplete scope means "not found", not
    // "never happened", and the panel says which.
    absenceEstablished: f.absence_established !== false,
  }
}

/**
 * Money, only when the seat allows it, and always labelled for what it is.
 *
 * Returns null when the financial surface is absent — the caller renders
 * nothing at all rather than an empty card that advertises a number the
 * reader may not see. No ratio is computed here: cost hangs off the account
 * and the agent, not off a work scope, so there is no supported denominator
 * for cost-per-completion.
 */
export function readFinancial(snapshot) {
  const fin = snapshot?.financial || {}
  if (!fin.visible) return null
  const cov = fin.coverage || {}
  const unpriced = typeof cov.unpriced_token_spans === 'number' ? cov.unpriced_token_spans : 0
  return {
    spend: typeof fin.spend_usd === 'number' ? fin.spend_usd : null,
    currency: fin.currency || 'USD',
    orgWide: fin.scope === 'organization_wide',
    scopeNote: fin.scope_note || null,
    attributable: fin.attributable_to_shown_work === true,
    coverageRatio: typeof cov.ratio === 'number' ? cov.ratio : null,
    unpricedSpans: unpriced,
    // Unpriced is UNKNOWN cost, never zero cost. The copy has to say so, or a
    // low number reads as a cheap month.
    unpricedNote:
      unpriced > 0
        ? `${formatCount(unpriced)} cost-bearing ${unpriced === 1 ? 'call carries' : 'calls carry'} no stored price — their cost is unknown, not zero.`
        : null,
    periodStart: fin.period_start_utc || null,
    periodEnd: fin.period_end_utc || null,
  }
}

export function formatMoney(amount, currency = 'USD', locale = undefined) {
  if (typeof amount !== 'number' || !Number.isFinite(amount)) return '—'
  try {
    return amount.toLocaleString(locale, {
      style: 'currency', currency,
      // Cents below a thousand: rounding $12.50 to "$13" on a cost figure is
      // a small lie, and cost figures are where small lies matter.
      minimumFractionDigits: amount < 1000 ? 2 : 0,
      maximumFractionDigits: amount < 1000 ? 2 : 0,
    })
  } catch {
    return `${currency} ${amount.toFixed(2)}`
  }
}

// ---------------------------------------------------------------------------
// Request generations
// ---------------------------------------------------------------------------

/**
 * The guard that stops a late answer landing in a view that has moved on.
 *
 * Aborting is necessary and not sufficient: a response already in flight when
 * `abort()` is called can still resolve, and `fetch` on a cached body may not
 * reject at all. So every request carries the GENERATION that asked for it,
 * and a result is only accepted while that generation is still current.
 *
 * `invalidate()` is what a teardown calls — after it, nothing issued before is
 * current, which is exactly the rule for a scope or permission change: the
 * answer to the old question must not be rendered under the new heading.
 *
 * A factory rather than a hook so the rule is testable with real promises.
 */
export function createRaceGuard() {
  let generation = 0
  return {
    next: () => ++generation,
    isCurrent: (mine) => mine === generation,
    invalidate: () => { generation += 1 },
    get current() { return generation },
  }
}

// ---------------------------------------------------------------------------
// Navigation
// ---------------------------------------------------------------------------

/**
 * Where a finding's entity can actually take someone, and how honestly.
 *
 * `/work/items` still has no period filter, so a job link cannot promise the
 * finding's window. The backend's own navigation contract says so
 * (`exact: false` + a note) and this carries that through rather than
 * implying a drill-through the destination does not support.
 */
export function findingTargets(detail) {
  const targets = detail?.navigation?.targets
  if (!Array.isArray(targets)) return []
  return targets
    .filter((t) => t && t.kind && t.id != null)
    .map((t) => ({
      kind: t.kind,
      id: t.id,
      exact: t.exact !== false,
      note: t.note || null,
      label:
        t.kind === 'run' ? 'Open this work item'
        : t.kind === 'job' ? 'Open this job'
        : t.kind === 'agent' ? 'Open this agent'
        : 'Open',
    }))
}

/**
 * A specific, grounded question for Ask, or null.
 *
 * Ask receives a TEXT question through the existing `openAsk` channel; there
 * is no structured entity/finding context API today (documented as a gap). So
 * the question has to carry its own context, and if it cannot be made
 * specific, no Ask action is offered — an unrelated generic chat opened from
 * a finding is worse than no button.
 */
export function askQuestionFor(finding) {
  const title = String(finding?.title || '').trim()
  if (!title) return null
  const named = (finding?.entities || [])
    .map((e) => e?.label)
    .filter((l) => typeof l === 'string' && l.trim())
    .slice(0, 3)
  if (!named.length) return `${title}. What does the record show about this?`
  return `${title}. What does the record show about ${named.join(', ')}?`
}

/** Relative time, matching the app's existing phrasing. */
export function relTime(iso, nowMs = Date.now()) {
  if (!iso) return null
  const t = Date.parse(String(iso).includes('T') ? iso : `${iso}Z`.replace(' ', 'T'))
  if (Number.isNaN(t)) return null
  const m = Math.floor((nowMs - t) / 60000)
  if (m < 1) return 'just now'
  if (m < 60) return `${m}m ago`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ago`
  return `${Math.floor(h / 24)}d ago`
}
