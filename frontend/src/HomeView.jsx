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
 * One endpoint's state, with the staleness rule built in.
 *
 * `nonce` is the request generation. A response only lands if its nonce still
 * matches the live one, so a late answer for a scope the reader has already
 * left is discarded rather than rendered under the new heading.
 */
function useHomeRead(load, deps, { active = true } = {}) {
  const [state, setState] = useState({ data: null, error: null, loading: true })
  const guard = useRef(createRaceGuard()).current
  const [attempt, setAttempt] = useState(0)

  const run = useCallback(() => {
    const mine = guard.next()
    const controller = new AbortController()
    setState((s) => ({ ...s, loading: true, error: null }))
    load(controller.signal)
      .then((data) => {
        if (!guard.isCurrent(mine)) return
        setState({ data, error: null, loading: false })
      })
      .catch((err) => {
        if (!guard.isCurrent(mine)) return
        if (err?.name === 'AbortError') return
        setState({ data: null, error: err, loading: false })
      })
    return () => controller.abort()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  useEffect(() => {
    if (!active) return undefined
    // Invalidating on teardown is what makes a scope change safe: aborting
    // alone is not enough, because a response already in flight can still
    // resolve. After this, nothing issued for the OLD deps is current.
    const abort = run()
    return () => {
      guard.invalidate()
      if (abort) abort()
    }
  }, [run, active, attempt])

  return { ...state, reload: () => setAttempt((a) => a + 1) }
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
  onConnectAgent,
}) {
  const [whose, setWhose] = useState('everyone')
  const [days, setDays] = useState(7)
  const [openFinding, setOpenFinding] = useState(null)
  const [expanded, setExpanded] = useState({})
  const [mutating, setMutating] = useState(null)
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
  // The identity of the question. Both reads take this, so the page cannot
  // show a snapshot for one slice beside findings for another. The seat's own
  // surface list is in here too: when permissions change, every cached body
  // below is discarded rather than re-shown.
  const queryKey = [
    days, tz, whoseParam, personId,
    (seat?.surfaces || []).join(','), seat?.breadth || '',
    me?.user?.id ?? me?.id ?? '',
  ].join('|')
  const query = { days, tz, whose: whoseParam, personId }

  const snapshot = useHomeRead(
    (signal) => api.getHomeSnapshot({ ...query, signal }),
    [queryKey],
    { active },
  )
  const findings = useHomeRead(
    (signal) => api.getHomeFindings({ ...query, signal }),
    [queryKey],
    { active },
  )

  // Close a detail panel whenever the question changes. A finding opened under
  // the previous scope is not necessarily readable under this one — the server
  // would 404 it — and leaving the old body on screen would present it as
  // though it still applied.
  useEffect(() => {
    setOpenFinding(null)
    setExpanded({})
  }, [queryKey])

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
  }, [queryKey])
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
  }, [active, analysis.poll, analysis.state, finds, queryKey])

  const financialSurface = hasSurface(seat, 'Cost')
  const grouped = useMemo(() => groupFindings(finds?.findings), [finds])

  const acknowledge = useCallback(
    async (finding) => {
      setMutating(finding.id)
      try {
        await api.setFindingState(finding.id, 'acknowledged', query)
        findings.reload()
      } catch {
        // The finding stays on screen. A failed mutation must not remove what
        // it failed to change.
        setMutating(null)
      } finally {
        setMutating(null)
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [queryKey],
  )

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
        onGoWork={onGoWork}
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
        onGoWork={onGoWork}
        onOpen={setOpenFinding}
        onAcknowledge={acknowledge}
        mutating={mutating}
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
        expanded={Boolean(expanded.positive_change)}
        onExpand={() => setExpanded((e) => ({ ...e, positive_change: true }))}
      />

      {openFinding ? (
        <HomeFindingPanel
          findingId={openFinding.id}
          summary={openFinding}
          query={query}
          onClose={() => setOpenFinding(null)}
          onGoWork={onGoWork}
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
  snapshot, findings, analysis, items, onGoWork, onOpen, onAcknowledge,
  mutating, expanded, onExpand,
}) {
  const snap = snapshot.data
  const att = snap ? readAttention(snap) : null
  const shown = expanded ? items : items.slice(0, VISIBLE_PER_GROUP)

  return (
    <Section
      title="Needs your attention"
      sub="Work waiting on you, and conditions Trovis investigated."
      id="hv-attention"
    >
      <div className="hv-att-grid">
        <div className="hv-att-personal">
          {!snap ? (
            <Skeleton lines={2} label="Loading personal attention" />
          ) : att.kind === 'unavailable' ? (
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
                onClick={() => onGoWork && onGoWork('waiting_on_you')}
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

/* ── opportunity / positive groups ─────────────────────────────────── */

function FindingGroup({
  id, title, sub, items, loading, onOpen, onAcknowledge, mutating, expanded, onExpand,
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
