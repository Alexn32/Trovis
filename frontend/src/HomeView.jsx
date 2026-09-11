// Home.
//
// Two independent reads over one question. `/home/snapshot` supplies every
// number and every chart value; `/home/findings` supplies Trovis's
// evidence-backed interpretation. They are fetched separately and fail
// separately, because the numbers must not disappear when a model does — and
// because a page that waits for AI before showing what got done is a page that
// is usually blank.
//
// The division of labour is the product:
//
//   the RECORD says how much work was completed, when, and by which job.
//   TROVIS says what is worth knowing about that, and shows its evidence.
//
// Neither is allowed to speak for the other. A recorded completion is a
// recorded completion, not a verified business outcome; an absent finding is
// Trovis having nothing to raise, not a clean bill of health.
//
// Scope, period and timezone are held in one place and sent to both endpoints
// through `api.homeQuery`, so the two reads can never describe different
// slices. Every response carries the nonce of the request that asked for it,
// and a response whose nonce has moved on is dropped — a slow answer for the
// previous scope must never land in the new view.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api } from './api.js'
import { hasSurface } from './seat.js'
import { reconcileWhose, whoseOptions, whoseParams } from './whoseWork.js'
import { openAsk } from './askOpen.js'
import HomeFindingPanel from './HomeFindingPanel.jsx'
import {
  AnalysisNote, Caveat, CompletionChart, CostCard, CurrentActivity,
  FindingCard, FreshnessPanel, JobBreakdown, Section, SectionError, Skeleton,
} from './HomeSections.jsx'
import {
  askQuestionFor, createRaceGuard, formatCount, groupFindings, pollDelay, readAnalysis,
  readAttention, readComparison, readCount, readFinancial, readFreshness,
  readJobs, readSeries, workEmptyCopy, workState, zeroMeansNone,
} from './homeView.js'

const PERIODS = [
  { days: 7, label: '7 days' },
  { days: 14, label: '14 days' },
  { days: 30, label: '30 days' },
]

// How many findings each group shows before "show all". Small on purpose:
// Home is a place to notice things, not a queue to work through.
const VISIBLE_PER_GROUP = 3

function localZone() {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
  } catch {
    return 'UTC'
  }
}

/**
 * One endpoint's state, STAMPED with the context that authorized it.
 *
 * The race guard stops a late response landing. It does not remove what is
 * already on screen — and that was the hole: `setState` kept `data` while the
 * next request ran, so after a scope narrowed or a permission was removed, the
 * previous answer stayed rendered until the replacement arrived. For Cost that
 * means financial findings visible to someone who just lost the Cost surface.
 *
 * So every stored response carries the `context` that asked for it, and the
 * comparison happens DURING RENDER, not in an effect. The moment the context
 * changes, `data` reads as null — before any effect runs, before any request
 * is issued, and regardless of what is in flight. Nothing authorized by the
 * old context can be rendered under the new one.
 *
 * `reload` is the one same-context refresh: it re-runs the same question and
 * is represented as loading beside the data it is refreshing.
 */
function useHomeRead(load, context, { active = true } = {}) {
  const [state, setState] = useState({
    data: null, error: null, loading: true, context: null,
  })
  const guard = useRef(createRaceGuard()).current
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    if (!active) return undefined
    const mine = guard.next()
    const controller = new AbortController()
    setState((s) =>
      s.context === context
        // Same question, asked again: keep what is on screen and say it is
        // refreshing. This is the only case where old data may survive.
        ? { ...s, loading: true, error: null }
        : { data: null, error: null, loading: true, context: null },
    )
    load(controller.signal)
      .then((data) => {
        if (!guard.isCurrent(mine)) return
        setState({ data, error: null, loading: false, context })
      })
      .catch((err) => {
        if (!guard.isCurrent(mine)) return
        if (err?.name === 'AbortError') return
        setState({ data: null, error: err, loading: false, context })
      })
    return () => {
      // Aborting alone is not enough: a response already in flight can still
      // resolve. After this, nothing issued for the old context is current.
      guard.invalidate()
      controller.abort()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [context, active, attempt])

  // THE RENDER-TIME GATE. A body stamped with another context is not this
  // context's data, whatever the effects have or have not done yet.
  const mine = state.context === context
  return {
    data: mine ? state.data : null,
    error: mine ? state.error : null,
    loading: !mine || state.loading,
    reload: () => setAttempt((a) => a + 1),
  }
}

/**
 * Everything that decides what a reader may be shown, as one string.
 *
 * Not just the query: the ACCOUNT and the SESSION identity, the seat's
 * surfaces and breadth, and — the one a breadth check misses — the resolved
 * membership list. A reorg that moves people in or out of someone's subtree
 * changes what they may see without changing either atom, so the ids
 * themselves are in the key.
 *
 * `visible_user_ids: null` means company-wide (no filter), which is a
 * different key from `[]` (nobody), exactly as it is server-side.
 */
export function homeContextKey({ me, seat, days, tz, whose, personId }) {
  const visible = seat?.visible_user_ids
  return JSON.stringify({
    account: me?.org?.id ?? null,
    viewer: me?.user?.id ?? null,
    surfaces: [...(seat?.surfaces || [])].sort(),
    breadth: seat?.breadth ?? null,
    visible: visible == null ? null : [...visible].sort((a, b) => a - b),
    subtree: [...(seat?.subtree_user_ids || [])].sort((a, b) => a - b),
    days, tz, whose, personId: personId ?? null,
  })
}

export default function HomeView({
  seat,
  me,
  people = [],
  active = true,
  onGoWork,
  onOpenCost,
  onOpenAgent,
  onOpenJob,
  onOpenRun,
  onConnectAgent,
}) {
  const [whose, setWhose] = useState('everyone')
  const [days, setDays] = useState(7)
  const [openFinding, setOpenFinding] = useState(null)
  const [expanded, setExpanded] = useState({})
  const [mutating, setMutating] = useState(null)
  // Acknowledgement failures, keyed by finding id AND by the context they
  // happened in, so an error from a scope the reader has left never decorates
  // a card in the new one.
  const [ackError, setAckError] = useState({ context: null, byId: {} })
  const [showDismissed, setShowDismissed] = useState(false)
  const tz = useMemo(() => localZone(), [])
  const locale = undefined

  // A seat that arrives late, or a reorg that removes this person's reports,
  // must not leave an unreadable selection on screen. `reconcileWhose` is the
  // same rule the Work tab applies.
  const options = useMemo(() => whoseOptions(seat, people), [seat, people])
  const effectiveWhose = useMemo(() => reconcileWhose(whose, seat), [whose, seat])
  useEffect(() => {
    if (effectiveWhose !== whose) setWhose(effectiveWhose)
  }, [effectiveWhose, whose])

  const { whose: whoseParam, personId } = whoseParams(effectiveWhose)
  // The identity of the question AND of the authority behind it. Both reads
  // take this, so the page can never show a snapshot for one slice beside
  // findings for another — and the moment account, identity, surfaces,
  // breadth or resolved membership move, every body below reads as absent.
  const contextKey = useMemo(
    () => homeContextKey({ me, seat, days, tz, whose: whoseParam, personId }),
    [me, seat, days, tz, whoseParam, personId],
  )
  const query = useMemo(
    () => ({ days, tz, whose: whoseParam, personId }),
    [days, tz, whoseParam, personId],
  )

  const snapshot = useHomeRead(
    useCallback((signal) => api.getHomeSnapshot({ ...query, signal }), [query]),
    contextKey,
    { active },
  )
  const findings = useHomeRead(
    useCallback((signal) => api.getHomeFindings({ ...query, signal }), [query]),
    contextKey,
    { active },
  )

  // Close a detail panel whenever the context changes. A finding opened under
  // the previous scope is not necessarily readable under this one — the server
  // would 404 it — and leaving the old body on screen would present it as
  // though it still applied. The state is cleared in an effect AND the panel
  // is gated on the context during render, so nothing survives even one frame.
  // A LIVE read of the context for async work already in flight. `contextKey`
  // captured in a closure is the value at call time; this is the value now.
  const contextRef = useRef(contextKey)
  contextRef.current = contextKey
  const openContext = useRef(contextKey)
  if (openContext.current !== contextKey && openFinding) openContext.current = null
  useEffect(() => {
    openContext.current = contextKey
    setOpenFinding(null)
    setExpanded({})
    setShowDismissed(false)
    setAckError({ context: contextKey, byId: {} })
  }, [contextKey])
  const panelFinding = openContext.current === contextKey ? openFinding : null

  const snap = snapshot.data
  const finds = findings.data
  const analysis = useMemo(
    () => readAnalysis(finds?.analysis, { findingCount: (finds?.findings || []).length }),
    [finds],
  )

  // Bounded, state-aware polling. ONLY the two genuinely in-flight states
  // poll; `debounced`, `incomplete` and `failed` are settled answers, and
  // polling them would be retry pressure against a backend that has already
  // said to wait. It backs off, stops after a fixed number of checks, and is
  // torn down when Home goes off screen or the question changes.
  const pollAttempt = useRef(0)
  useEffect(() => {
    pollAttempt.current = 0
  }, [contextKey])
  useEffect(() => {
    if (!active || !analysis.poll) return undefined
    const delay = pollDelay(analysis.state, pollAttempt.current)
    if (delay == null) return undefined
    const t = setTimeout(() => {
      pollAttempt.current += 1
      findings.reload()
    }, delay)
    return () => clearTimeout(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, analysis.poll, analysis.state, finds, contextKey])

  const financialSurface = hasSurface(seat, 'Cost')
  const grouped = useMemo(() => groupFindings(finds?.findings), [finds])

  // Acknowledging from a card. Three things this has to get right:
  //   * a FAILURE is visible and retryable, not swallowed — the previous
  //     handler caught the error and told the reader nothing, so "Mark seen"
  //     silently did nothing;
  //   * the finding and its prior state survive a failure untouched;
  //   * a completion that lands after the context moved changes nothing,
  //     because it belongs to a scope the reader has left.
  const acknowledge = useCallback(
    async (finding) => {
      const inContext = contextKey
      setMutating(finding.id)
      setAckError((e) => ({
        context: inContext,
        byId: { ...(e.context === inContext ? e.byId : {}), [finding.id]: null },
      }))
      try {
        await api.setFindingState(finding.id, 'acknowledged', query)
        if (contextRef.current !== inContext) return
        findings.reload()
      } catch (err) {
        if (contextRef.current !== inContext) return
        setAckError((e) => ({
          context: inContext,
          byId: {
            ...(e.context === inContext ? e.byId : {}),
            [finding.id]: err?.message || 'That did not save.',
          },
        }))
      } finally {
        if (contextRef.current === inContext) setMutating(null)
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [contextKey, query],
  )
  const ackErrors = ackError.context === contextKey ? ackError.byId : {}

  // ---- navigation ---------------------------------------------------------
  // Home's work sections are scoped; their destinations must arrive scoped
  // too, or the reader lands on a wider list than the one they tapped away
  // from. `whose`/`person_id` are the existing Work selectors; Work reconciles
  // them against the seat on arrival, so this can only ever ask.
  const goWorkScoped = useCallback(
    (filter = null) => {
      onGoWork && onGoWork(filter, { whose: whoseParam, personId })
    },
    [onGoWork, whoseParam, personId],
  )
  // The personal link is DELIBERATELY different. `needs_you` answers to the
  // signed-in person and is never narrowed by the work scope — so sending the
  // work scope with it would hand Work a filter that can exclude the very
  // items being counted. It goes as the widest selection the seat allows,
  // with Work's canonical personal filter: 'mine', which Work resolves to
  // `waiting_on_you`. ('waiting_on_you' is not a value Work knows; it fell
  // through to an unfiltered list.)
  const goMyWork = useCallback(() => {
    onGoWork && onGoWork('mine', { whose: 'everyone', personId: null })
  }, [onGoWork])

  return (
    <div className="hv">
      <header className="hv-head">
        <div className="hv-head-titles">
          <h1 className="hv-title">Home</h1>
          <p className="hv-lede">
            What got done, what needs you, and what Trovis found in the record.
          </p>
        </div>
        <div className="hv-controls">
          {options.length > 1 ? (
            <label className="hv-control">
              <span className="hv-control-label">Work scope</span>
              <select
                className="hv-select"
                value={effectiveWhose}
                onChange={(e) => setWhose(e.target.value)}
              >
                {options.map((o) => (
                  <option key={o.value} value={o.value}>{o.label}</option>
                ))}
              </select>
            </label>
          ) : null}
          <label className="hv-control">
            <span className="hv-control-label">Period</span>
            <select
              className="hv-select"
              value={days}
              onChange={(e) => setDays(Number(e.target.value))}
            >
              {PERIODS.map((p) => (
                <option key={p.days} value={p.days}>{p.label}</option>
              ))}
            </select>
          </label>
          {snap ? (
            <FreshnessPanel
              fresh={readFreshness(snap, finds)}
              analysisLabel={analysis.label}
            />
          ) : null}
        </div>
      </header>

      <WorkDone
        snapshot={snapshot}
        onGoWork={goWorkScoped}
        onOpenJob={onOpenJob}
        onConnectAgent={onConnectAgent}
        tz={tz}
        locale={locale}
      />

      <Attention
        snapshot={snapshot}
        findings={findings}
        analysis={analysis}
        items={grouped.attention}
        onGoMyWork={goMyWork}
        onOpen={setOpenFinding}
        onAcknowledge={acknowledge}
        mutating={mutating}
        ackErrors={ackErrors}
        expanded={Boolean(expanded.attention)}
        onExpand={() => setExpanded((e) => ({ ...e, attention: true }))}
      />

      {financialSurface ? (
        <CostContext snapshot={snapshot} onOpenCost={onOpenCost} locale={locale} />
      ) : null}

      <FindingGroup
        id="hv-opps"
        title="Opportunities"
        sub="Specific improvements Trovis found evidence for."
        items={grouped.opportunity}
        loading={findings.loading && !finds}
        onOpen={setOpenFinding}
        onAcknowledge={acknowledge}
        mutating={mutating}
        ackErrors={ackErrors}
        expanded={Boolean(expanded.opportunity)}
        onExpand={() => setExpanded((e) => ({ ...e, opportunity: true }))}
      />

      <FindingGroup
        id="hv-good"
        title="Progress worth noting"
        sub="Meaningful changes, beyond the completion total above."
        items={grouped.positive_change}
        loading={findings.loading && !finds}
        onOpen={setOpenFinding}
        onAcknowledge={acknowledge}
        mutating={mutating}
        ackErrors={ackErrors}
        expanded={Boolean(expanded.positive_change)}
        onExpand={() => setExpanded((e) => ({ ...e, positive_change: true }))}
      />

      <DismissedFindings
        open={showDismissed}
        onToggle={setShowDismissed}
        query={query}
        context={contextKey}
        active={active}
        onOpen={setOpenFinding}
      />

      {panelFinding ? (
        <HomeFindingPanel
          findingId={panelFinding.id}
          summary={panelFinding}
          query={query}
          onClose={() => setOpenFinding(null)}
          onOpenRun={onOpenRun}
          onOpenAgent={onOpenAgent}
          onOpenJob={onOpenJob}
          onAsk={(f) => {
            const q = askQuestionFor(f)
            if (q) openAsk(q)
          }}
          onStateChanged={() => findings.reload()}
        />
      ) : null}
    </div>
  )
}

/* ── work getting done ─────────────────────────────────────────────── */

function WorkDone({ snapshot, onGoWork, onOpenJob, onConnectAgent, tz, locale }) {
  if (snapshot.error) {
    return (
      <Section title="Work getting done" id="hv-work">
        <SectionError
          lead="Trovis couldn't load the record. The numbers here are unavailable."
          onRetry={snapshot.reload}
        />
      </Section>
    )
  }
  if (!snapshot.data) {
    return (
      <Section title="Work getting done" id="hv-work">
        <Skeleton lines={4} label="Loading the record" />
      </Section>
    )
  }
  const snap = snapshot.data
  const state = workState(snap)
  const period = snap.period || {}
  const completed = readCount(period.completed, period)
  const comparison = readComparison(snap)
  const series = readSeries(snap)
  const jobs = readJobs(snap)
  const cs = snap.current_state || {}
  const current = {
    moving: readCount(cs.moving, cs, { unknownText: '—' }),
    waiting: readCount(cs.waiting_on_person, cs, { unknownText: '—' }),
    blocked: readCount(cs.blocked, cs, { unknownText: '—' }),
  }

  if (state !== 'populated') {
    const copy = workEmptyCopy(state, { effective: snap.scope?.effective })
    return (
      <Section title="Work getting done" id="hv-work">
        <div className="hv-empty">
          <p className="hv-empty-lead">{copy.lead}</p>
          <p className="hv-empty-sub">{copy.sub}</p>
          {copy.action === 'connect' && onConnectAgent ? (
            <button type="button" className="btn btn-primary" onClick={onConnectAgent}>
              Connect an agent
            </button>
          ) : null}
        </div>
      </Section>
    )
  }

  return (
    <Section
      title="Work getting done"
      sub={`Recorded completions · ${period.days || 7} days`}
      id="hv-work"
      aside={
        <button type="button" className="btn btn-ghost btn-sm" onClick={() => onGoWork && onGoWork(null)}>
          Open Work
        </button>
      }
    >
      <div className="hv-work-grid">
        <div className="hv-work-headline">
          <span className={`hv-big hv-big-${completed.kind}`}>{completed.text}</span>
          <span className="hv-big-label">
            recorded completions
            {comparison ? <em className={`hv-delta hv-delta-${comparison.direction}`}>{comparison.text}</em> : null}
          </span>
          {period.abandoned > 0 ? (
            <span className="hv-abandoned">
              {formatCount(period.abandoned)} closed without finishing
            </span>
          ) : null}
          <p className="hv-big-note">
            A recorded completion is work the record shows as closed and done.
            It is not an independently verified business outcome.
          </p>
        </div>
        <div className="hv-work-chart">
          <CompletionChart series={series} timezone={period.timezone || tz} locale={locale} />
        </div>
      </div>

      {completed.note ? <Caveat detail={snap.scope?.membership_incomplete_reason}>{completed.note}</Caveat> : null}
      {series.available && series.partial ? (
        <Caveat detail="Some of the work in this scope could not be read, so each bar is a floor rather than a total.">
          This chart is drawn from part of the scope, not all of it.
        </Caveat>
      ) : null}
      {!zeroMeansNone(snap) ? (
        <Caveat detail="Membership of the selected scope is incomplete, so a zero here means none were found — not that none exist.">
          A zero in this section means “none found”, not “none exist”.
        </Caveat>
      ) : null}

      {!jobs.empty ? (
        <div className="hv-sub">
          <h3 className="hv-sub-title">By job</h3>
          <JobBreakdown jobs={jobs} onOpenJob={onOpenJob} />
          {jobs.truncated ? (
            <p className="hv-sub-note">Showing the largest jobs; the rest are grouped.</p>
          ) : null}
        </div>
      ) : null}

      <CurrentActivity current={current} onGoWork={onGoWork} />
      {cs.exact === false ? (
        <Caveat>These current-state counts are a floor, not a total.</Caveat>
      ) : null}
    </Section>
  )
}

/* ── needs your attention ──────────────────────────────────────────── */

/**
 * The personal signal and the AI findings, side by side and NEVER summed.
 *
 * `needs_you` counts work waiting on this person; an attention finding is a
 * condition Trovis investigated. They are different measures over possibly
 * overlapping work, and adding them would produce a number that counts
 * nothing.
 */
function Attention({
  snapshot, findings, analysis, items, onGoMyWork, onOpen, onAcknowledge,
  mutating, ackErrors, expanded, onExpand,
}) {
  const snap = snapshot.data
  const att = snap ? readAttention(snap) : null
  const shown = expanded ? items : items.slice(0, VISIBLE_PER_GROUP)
  // Five outcomes, kept apart. The old version asked only "is there data?",
  // so a FAILED snapshot sat under a loading skeleton for ever — the one
  // state that definitely was not loading.
  const attState =
    snapshot.error ? 'error'
    : !snap ? 'loading'
    : att.kind === 'unavailable' ? 'unavailable'
    : att.kind

  return (
    <Section
      title="Needs your attention"
      sub="Work waiting on you, and conditions Trovis investigated."
      id="hv-attention"
    >
      <div className="hv-att-grid">
        <div className="hv-att-personal" data-att-state={attState}>
          {attState === 'error' ? (
            <div className="hv-att-failed" role="alert">
              <span className="hv-att-value hv-att-unknown">—</span>
              <span className="hv-att-label">
                Trovis couldn&rsquo;t load what is waiting on you
              </span>
              <button
                type="button"
                className="btn btn-secondary btn-sm"
                onClick={snapshot.reload}
              >
                Retry
              </button>
            </div>
          ) : attState === 'loading' ? (
            <Skeleton lines={2} label="Loading personal attention" />
          ) : attState === 'unavailable' ? (
            <>
              <span className="hv-att-value hv-att-unknown">—</span>
              <span className="hv-att-label">{att.text}</span>
              {att.atLeast != null ? (
                <p className="hv-att-note">
                  At least {formatCount(att.atLeast)} found before the search was
                  bounded.
                </p>
              ) : null}
            </>
          ) : (
            <>
              <span className="hv-att-value">{att.text}</span>
              <span className="hv-att-label">
                {att.count === 1 ? 'item is waiting on you' : 'items are waiting on you'}
              </span>
              <button
                type="button"
                className="btn btn-secondary btn-sm"
                onClick={() => onGoMyWork && onGoMyWork()}
              >
                Open your work
              </button>
              {att.kind === 'floor' ? (
                <p className="hv-att-note">A floor — the scan behind it was bounded.</p>
              ) : null}
            </>
          )}
          <p className="hv-att-scope">
            Always yours, whatever work scope is selected above.
          </p>
        </div>

        <div className="hv-att-findings">
          {findings.error ? (
            <SectionError
              lead="Trovis couldn't load its findings. The numbers above are unaffected."
              onRetry={findings.reload}
            />
          ) : (
            <>
              <AnalysisNote read={analysis} onRefresh={findings.reload} />
              {findings.loading && !findings.data ? (
                <Skeleton lines={2} label="Loading findings" />
              ) : shown.length ? (
                <>
                  <ul className="hv-findings">
                    {shown.map((f) => (
                      <FindingCard
                        key={f.id}
                        finding={f}
                        onOpen={onOpen}
                        onAcknowledge={onAcknowledge}
                        busy={mutating === f.id}
                        ackError={ackErrors?.[f.id] || null}
                      />
                    ))}
                  </ul>
                  {!expanded && items.length > shown.length ? (
                    <button type="button" className="btn btn-ghost btn-sm" onClick={onExpand}>
                      Show {items.length - shown.length} more
                    </button>
                  ) : null}
                </>
              ) : (
                <p className="hv-none">
                  Nothing raised for your attention in this period.
                </p>
              )}
            </>
          )}
        </div>
      </div>
    </Section>
  )
}

/* ── cost ──────────────────────────────────────────────────────────── */

function CostContext({ snapshot, onOpenCost, locale }) {
  const snap = snapshot.data
  if (!snap) return null
  // Nothing has run here yet, so there is no spend to give context TO. A
  // "$0.00" card on a first-run screen is noise beside "connect an agent".
  if (workState(snap) === 'first-run') return null
  const fin = readFinancial(snap)
  // The seat allows Cost but this response carries no financial block: say so
  // rather than reaching for another endpoint to work around the gate.
  if (!fin) {
    const reason = snap.financial?.unavailable_reason
    if (!reason) return null
    return (
      <Section title="Cost" id="hv-cost">
        <p className="hv-none">Cost is not available for this view ({reason}).</p>
      </Section>
    )
  }
  const period =
    fin.periodStart && fin.periodEnd
      ? `${fin.periodStart.slice(0, 10)} → ${fin.periodEnd.slice(0, 10)}`
      : `${snap.period?.days || 7} days`
  return (
    <Section title="Cost" sub="What the recorded work cost to run." id="hv-cost">
      <CostCard fin={fin} period={period} onOpenCost={onOpenCost} locale={locale} />
    </Section>
  )
}

/* ── dismissed findings ────────────────────────────────────────────── */

/**
 * Findings a person has dismissed, still reachable.
 *
 * "Dismiss" removes something from Home. Offering that with no way back would
 * make a finding unreachable through the UI, which is a worse outcome than not
 * offering it — so this is the way back. It reads the SAME scope and period
 * through the same `include_dismissed` capability the API already has, keeps
 * dismissed items out of the active counts and groups (the default read
 * excludes them, so they were never in either), and opens the same evidence
 * panel.
 *
 * There is no "restore" control, because the backend has no such state: a
 * person may set `open`, `acknowledged` or `dismissed`, and only re-analysis
 * retires a finding. Re-opening the evidence is what this offers.
 *
 * It fetches only when opened, and it is stamped with the same context as
 * everything else — so a permission change empties it during render.
 */
function DismissedFindings({ open, onToggle, query, context, active, onOpen }) {
  const read = useHomeRead(
    useCallback(
      (signal) => api.getHomeFindings({ ...query, includeDismissed: true, signal }),
      [query],
    ),
    context,
    { active: active && open },
  )
  const items = useMemo(
    () => (read.data?.findings || []).filter((f) => f.state === 'dismissed'),
    [read.data],
  )
  return (
    <details
      className="hv-dismissed"
      open={open}
      onToggle={(e) => onToggle(e.currentTarget.open)}
    >
      <summary className="hv-dismissed-summary">
        Dismissed findings
        <span aria-hidden="true">›</span>
      </summary>
      <div className="hv-dismissed-body">
        {!open ? null : read.error ? (
          <SectionError
            lead="Trovis couldn't load dismissed findings."
            onRetry={read.reload}
          />
        ) : read.loading ? (
          <Skeleton lines={2} label="Loading dismissed findings" />
        ) : items.length ? (
          <>
            <p className="hv-none">
              Dismissed means you asked Trovis to stop showing it. It does not
              say the condition ended, and these are not counted anywhere above.
            </p>
            <ul className="hv-findings">
              {items.map((f) => (
                <FindingCard
                  key={f.id}
                  finding={f}
                  onOpen={onOpen}
                  dismissed
                />
              ))}
            </ul>
          </>
        ) : (
          <p className="hv-none">Nothing dismissed in this scope and period.</p>
        )}
      </div>
    </details>
  )
}

/* ── opportunity / positive groups ─────────────────────────────────── */

function FindingGroup({
  id, title, sub, items, loading, onOpen, onAcknowledge, mutating, ackErrors,
  expanded, onExpand, emptyNote,
}) {
  // Nothing is forced into these sections. No canned copy, no client-generated
  // "insight" — an empty opportunities list means Trovis found no specific
  // improvement it could evidence, and inventing one would be the exact lie
  // this layer exists to avoid.
  if (!loading && !items.length) return null
  const shown = expanded ? items : items.slice(0, VISIBLE_PER_GROUP)
  return (
    <Section title={title} sub={sub} id={id}>
      {loading && !items.length ? (
        <Skeleton lines={2} label={`Loading ${title.toLowerCase()}`} />
      ) : (
        <>
          <ul className="hv-findings hv-findings-grid">
            {shown.map((f) => (
              <FindingCard
                key={f.id}
                finding={f}
                onOpen={onOpen}
                onAcknowledge={onAcknowledge}
                busy={mutating === f.id}
                ackError={ackErrors?.[f.id] || null}
              />
            ))}
          </ul>
          {!expanded && items.length > shown.length ? (
            <button type="button" className="btn btn-ghost btn-sm" onClick={onExpand}>
              Show {items.length - shown.length} more
            </button>
          ) : null}
        </>
      )}
    </Section>
  )
}
