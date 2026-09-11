// One finding's evidence, in the app's existing slide-over geometry.
//
// What this panel is for: a finding is a claim about this account's work, and
// the only reason to trust one is that it can be re-opened. So the panel shows
// what was observed, what backs it, what CUTS AGAINST it, what is still
// unknown, and where to go and look — and it shows the evidence the analysis
// actually read, flagged when the record has moved since.
//
// What it deliberately does not do: execute anything. There is no "Fix",
// "Approve fix" or "Retry agent" here, because Trovis does not do those things
// and a button that implies it would be a promise the product cannot keep. The
// strongest control is a link to the work and a next step a person takes.
//
// Acknowledge and dismiss mean exactly what they say. Neither is "resolved" —
// the server refuses that word outright.

import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from './api.js'
import { Skeleton } from './HomeSections.jsx'
import { createRaceGuard, findingQualifier, findingTargets, relTime } from './homeView.js'

const CLAIM_WORD = {
  observation: 'The record shows',
  calculation: 'Trovis computed',
  hypothesis: 'Proposed explanation',
}

export default function HomeFindingPanel({
  findingId, summary, query, onClose, onGoWork, onOpenAgent, onOpenJob, onAsk,
  onStateChanged,
}) {
  const [detail, setDetail] = useState(null)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)
  const [mutationError, setMutationError] = useState(null)
  const closeRef = useRef(null)
  const guard = useRef(createRaceGuard()).current

  useEffect(() => {
    const mine = guard.next()
    const controller = new AbortController()
    setDetail(null)
    setError(null)
    api
      .getHomeFinding(findingId, { ...query, signal: controller.signal })
      .then((d) => {
        if (!guard.isCurrent(mine)) return
        setDetail(d)
      })
      .catch((err) => {
        if (!guard.isCurrent(mine) || err?.name === 'AbortError') return
        setError(err)
      })
    return () => {
      guard.invalidate()
      controller.abort()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [findingId, query.days, query.tz, query.whose, query.personId])

  // Escape closes, and focus starts inside the panel — the same contract the
  // app's other slide-overs keep.
  useEffect(() => {
    function onKey(e) {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    closeRef.current?.focus()
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const setState = useCallback(
    async (state) => {
      setBusy(true)
      setMutationError(null)
      try {
        await api.setFindingState(findingId, state, query)
        onStateChanged && onStateChanged()
        if (state === 'dismissed') onClose()
      } catch (err) {
        // The finding stays on screen with its evidence intact. A mutation
        // that failed has changed nothing, and hiding the row would suggest
        // otherwise.
        setMutationError(err?.message || 'That did not save. Try again.')
      } finally {
        setBusy(false)
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [findingId, query.days, query.tz, query.whose, query.personId],
  )

  const finding = detail?.finding || summary
  const qualifier = findingQualifier(finding)
  const targets = findingTargets(detail)
  const stale = detail?.stale_evidence || []
  const staleKeys = new Set(stale.map((s) => `${s.kind}:${s.ref}`))

  function openTarget(t) {
    if (t.kind === 'agent' && onOpenAgent) onOpenAgent(t.id)
    else if (t.kind === 'job' && onOpenJob) onOpenJob(t.id)
    else if (onGoWork) onGoWork(null)
  }

  return (
    <>
      <div className="bpanel-scrim" onClick={onClose} />
      <aside
        className="bpanel hv-panel"
        role="dialog"
        aria-modal="true"
        aria-label={finding?.title || 'Finding'}
      >
        <button
          type="button"
          className="bpanel-close"
          onClick={onClose}
          ref={closeRef}
        >
          ← Back
        </button>

        {error ? (
          <div className="hv-error" role="alert">
            <p className="hv-error-lead">
              {error.status === 404
                ? 'This finding is no longer in view for the current scope.'
                : "Trovis couldn't load this finding's evidence."}
            </p>
          </div>
        ) : null}

        <h2 className="hv-panel-title">{finding?.title}</h2>
        <p className="hv-panel-explain">{finding?.explanation}</p>
        {finding?.consequence ? (
          <p className="hv-panel-conseq">
            <span className="hv-panel-eyebrow">Why it matters</span>
            {finding.consequence}
          </p>
        ) : null}
        {qualifier ? <p className="hv-panel-qual">{qualifier}</p> : null}

        {!detail && !error ? <Skeleton lines={4} label="Loading evidence" /> : null}

        {detail ? (
          <>
            {detail.claims?.length ? (
              <section className="hv-panel-sec">
                <h3 className="hv-panel-h">What was observed</h3>
                <ul className="hv-claims">
                  {detail.claims.map((c, i) => (
                    <li key={i} className={`hv-claim hv-claim-${c.kind}`}>
                      <span className="hv-claim-kind">
                        {CLAIM_WORD[c.kind] || c.kind}
                      </span>
                      <span className="hv-claim-text">{c.text}</span>
                      {c.partial ? (
                        <span className="hv-claim-partial">
                          a floor, not a total
                        </span>
                      ) : null}
                    </li>
                  ))}
                </ul>
              </section>
            ) : null}

            <section className="hv-panel-sec">
              <h3 className="hv-panel-h">
                Evidence{' '}
                <span className="hv-panel-count">
                  {detail.evidence?.length || 0} record
                  {(detail.evidence?.length || 0) === 1 ? '' : 's'}
                </span>
              </h3>
              {detail.evidence?.length ? (
                <ul className="hv-evidence">
                  {detail.evidence.map((e, i) => {
                    const key = `${e.kind}:${e.ref}`
                    const isStale = staleKeys.has(key)
                    return (
                      <li key={i} className={`hv-ev${isStale ? ' is-stale' : ''}`}>
                        <span className="hv-ev-kind">{readableKind(e.kind)}</span>
                        <span className="hv-ev-note">{e.note || readableKind(e.kind)}</span>
                        {isStale ? (
                          <span className="hv-ev-stale">
                            {stale.find((s) => `${s.kind}:${s.ref}` === key)?.status === 'missing'
                              ? 'This record is no longer there'
                              : 'This record changed after the finding was written'}
                          </span>
                        ) : null}
                      </li>
                    )
                  })}
                </ul>
              ) : (
                <p className="hv-none">No re-openable evidence on this finding.</p>
              )}
            </section>

            {detail.uncertainty?.length ? (
              <section className="hv-panel-sec">
                <h3 className="hv-panel-h">What is still unknown</h3>
                <ul className="hv-unknown">
                  {detail.uncertainty.map((u, i) => (
                    <li key={i}>{u}</li>
                  ))}
                </ul>
              </section>
            ) : null}

            <CoverageNote finding={finding} />

            {targets.length ? (
              <section className="hv-panel-sec">
                <h3 className="hv-panel-h">Go and look</h3>
                <ul className="hv-targets">
                  {targets.map((t, i) => (
                    <li key={i}>
                      <button
                        type="button"
                        className="btn btn-secondary btn-sm"
                        onClick={() => openTarget(t)}
                      >
                        {t.label}
                      </button>
                      {!t.exact ? (
                        <span className="hv-target-note">
                          {t.note ||
                            'Opens the wider list — Work cannot filter to this finding’s period.'}
                        </span>
                      ) : null}
                    </li>
                  ))}
                </ul>
              </section>
            ) : null}

            {finding?.next_step && finding.next_step.kind !== 'no_action' ? (
              <section className="hv-panel-sec">
                <h3 className="hv-panel-h">Suggested next step</h3>
                <p className="hv-step">{finding.next_step.text}</p>
                <p className="hv-step-note">
                  A step for a person. Trovis does not act on findings.
                </p>
              </section>
            ) : null}

            <p className="hv-panel-meta">
              {finding?.analyzed_at ? `Investigated ${relTime(finding.analyzed_at)}` : null}
              {finding?.evidence_cutoff
                ? ` · records as of ${finding.evidence_cutoff.slice(0, 16).replace('T', ' ')} UTC`
                : null}
            </p>
          </>
        ) : null}

        {mutationError ? (
          <p className="hv-panel-mut-error" role="alert">{mutationError}</p>
        ) : null}

        <div className="hv-panel-actions">
          {onAsk ? (
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              onClick={() => onAsk(finding)}
            >
              Ask about this
            </button>
          ) : null}
          {finding?.state !== 'acknowledged' ? (
            <button
              type="button"
              className="btn btn-secondary btn-sm"
              disabled={busy}
              onClick={() => setState('acknowledged')}
            >
              Mark seen
            </button>
          ) : null}
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            disabled={busy}
            onClick={() => setState('dismissed')}
          >
            Dismiss
          </button>
        </div>
        <p className="hv-panel-foot">
          “Seen” and “Dismiss” record what you did with this finding. Neither
          says the condition ended — only a later investigation can retire it.
          Dismissed findings stay readable via the include-dismissed view.
        </p>
      </aside>
    </>
  )
}

function readableKind(kind) {
  switch (kind) {
    case 'run': return 'Work item'
    case 'run_event': return 'Lifecycle event'
    case 'failed_span': return 'Failing operation'
    case 'calculation': return 'Server calculation'
    case 'snapshot': return 'Snapshot figure'
    case 'agent_context': return 'Agent record'
    default: return kind
  }
}

/** Coverage limits, said in one compact place rather than per number. */
function CoverageNote({ finding }) {
  const cov = finding?.coverage || {}
  const notes = []
  if (cov.retrieval_complete === false) {
    notes.push(
      `Trovis could not read the whole scope for this${
        (cov.retrieval_limitations || []).length
          ? ` (${cov.retrieval_limitations.join(', ')})`
          : ''
      }.`,
    )
  }
  if (cov.counts_exact === false) notes.push('Counts behind it are a floor, not a total.')
  if (cov.absence_established === false) {
    notes.push('A zero in this finding means “none found”, not “none exist”.')
  }
  if (cov.deadline_hit === true) notes.push('The analysis reached its time limit.')
  if (!notes.length) return null
  return (
    <section className="hv-panel-sec">
      <h3 className="hv-panel-h">Coverage limits</h3>
      <ul className="hv-coverage">
        {notes.map((n, i) => (
          <li key={i}>{n}</li>
        ))}
      </ul>
    </section>
  )
}
