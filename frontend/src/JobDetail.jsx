import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from './api.js'
import { startAbortable } from './abortable.js'
import { WorkLoadFailed } from './ui.jsx'
import { workItemStatusLabel, workUpdatedLabel } from './board.js'
import { openAsk } from './askOpen.js'
import { ACTOR_LABEL, askPrompt, canDecide, processSteps, shortHistory } from './jobDetail.js'

// ---------------------------------------------------------------------------
// Job detail — what a work item IS and how it moves, not a second table.
//
//   1. Title
//   2. Status + who is holding it now
//   3. Steps in order, each marked Person / Agent / Tool
//   4. The current handoff, highlighted — with judgment CTAs when it is on you
//   5. A short handoff history
//   6. Underlying agent runs, collapsed and fetched only when opened
//
// Reads GET /work/items/:id (the lean detail), and /work/items/:id?include=runs
// only when someone expands the runs section. Never the fat board, never a
// span dump: the point of this screen is the process, and the raw runs are the
// one thing deliberately folded away.
// ---------------------------------------------------------------------------

export default function JobDetail({ item, onClose, onResolved }) {
  const [detail, setDetail] = useState(null)
  const [err, setErr] = useState(null)
  const [reload, setReload] = useState(0)
  const [busy, setBusy] = useState(null)
  const [actionErr, setActionErr] = useState(null)

  useEffect(
    () =>
      startAbortable(({ signal, isAlive }) => {
        setErr(null)
        api
          .getWorkItem(item.id, { signal })
          .then((d) => isAlive() && setDetail(d))
          .catch((e) => isAlive() && setErr(e))
      }),
    [item.id, reload],
  )

  // Close on Escape as well as the scrim — a panel you can only leave by
  // hunting for the X is a trap on a keyboard.
  useEffect(() => {
    function onKey(e) {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  // The row we came from is already correct for the header, so the panel opens
  // with the title and status filled in and only the spine streams in.
  const view = detail || item
  const { steps, hidden } = processSteps(detail?.timeline, {
    holder: view.holder,
    status: view.status,
  })
  const history = shortHistory(detail?.timeline)
  const decidable = canDecide({ ...view, ...(detail || {}) })

  async function resolve(kind) {
    const handoffId = detail?.awaiting_handoff_event_id
    if (!handoffId || busy) return
    setBusy(kind)
    setActionErr(null)
    try {
      if (kind === 'approve') await api.completeHandoff(item.id, handoffId)
      else await api.declineHandoff(item.id, handoffId)
      onResolved ? onResolved() : setReload((n) => n + 1)
    } catch (e) {
      setActionErr(e?.message || "That didn't go through")
      setBusy(null)
    }
  }

  return (
    <>
      <div className="bpanel-scrim" onClick={onClose} />
      <aside className="jobd" role="dialog" aria-label={view.title}>
        <header className="jobd-head">
          <button type="button" className="jobd-close" onClick={onClose}>
            ← Back
          </button>
          <h2 className="jobd-title">{view.title}</h2>
          <div className="jobd-status">
            <span className={`work-status-pill ${view.status || ''}`}>
              {workItemStatusLabel(view.status)}
            </span>
            <span className="jobd-holder">{holderSentence(view)}</span>
            {view.updated_at && (
              <span className="jobd-age">{workUpdatedLabel(view.updated_at)}</span>
            )}
          </div>
        </header>

        {err && !detail ? (
          <WorkLoadFailed
            lead="Can't load this job"
            onRetry={() => {
              setErr(null)
              setReload((n) => n + 1)
            }}
          />
        ) : (
          <>
            <CurrentHandoff
              view={view}
              decidable={decidable}
              busy={busy}
              actionErr={actionErr}
              onApprove={() => resolve('approve')}
              onSendBack={() => resolve('decline')}
              onAsk={() => openAsk(askPrompt(view))}
            />

            <section className="jobd-section" aria-label="Steps">
              <h3 className="dash-caps">How this job runs</h3>
              {!detail ? (
                <div className="dash-skel">
                  <span style={{ width: '70%' }} />
                  <span style={{ width: '50%' }} />
                </div>
              ) : steps.length === 0 ? (
                <p className="dash-empty">No steps recorded yet.</p>
              ) : (
                <>
                  {hidden > 0 && (
                    <p className="jobd-truncated">
                      {hidden} earlier {hidden === 1 ? 'step' : 'steps'} not shown
                    </p>
                  )}
                  <ol className="jobd-steps">
                    {steps.map((s, i) => (
                      <li
                        key={`${s.actor.kind}-${s.actor.name}-${i}`}
                        className={`jobd-step kind-${s.actor.kind}${s.isCurrent ? ' is-current' : ''}`}
                      >
                        <span className="jobd-step-dot" aria-hidden="true" />
                        <span className="jobd-step-who">
                          <span className="jobd-step-kind">
                            {s.isYou ? 'You' : ACTOR_LABEL[s.actor.kind]}
                          </span>
                          {s.actor.name && !s.isYou && (
                            <span className="jobd-step-name">{s.actor.name}</span>
                          )}
                        </span>
                        {/* The current leg's state is already stated by the
                            handoff block above, and the raw event label reads
                            wrong on it ("Waiting on someone" under YOU). */}
                        {s.text && !s.isCurrent && (
                          <span className="jobd-step-text">{s.text}</span>
                        )}
                      </li>
                    ))}
                  </ol>
                </>
              )}
            </section>

            {history.length > 0 && (
              <section className="jobd-section" aria-label="History">
                <h3 className="dash-caps">Recent handoffs</h3>
                <ul className="jobd-history">
                  {history.map((h, i) => (
                    <li key={i}>
                      <span className="jobd-hist-text">{h.text}</span>
                      {h.at && (
                        <span className="jobd-hist-at">{workUpdatedLabel(h.at)} ago</span>
                      )}
                    </li>
                  ))}
                </ul>
              </section>
            )}

            <AgentRuns itemId={item.id} />
          </>
        )}
      </aside>
    </>
  )
}

/** "With you" / "With Sarah Chen" / "Waiting on Stripe" — never a bare enum. */
function holderSentence(view) {
  const name = String(view?.holder?.name || '').trim()
  if (view?.status === 'waiting_on_you') return 'With you'
  if (view?.status === 'done') return 'Finished'
  if (!name || /^unassigned$/i.test(name)) return 'Not assigned'
  return `With ${name}`
}

// --- 4. current handoff ----------------------------------------------------

// The one block on the screen allowed to ask for something. When the work is
// waiting on YOU it carries the decision; otherwise it just says who has it.
function CurrentHandoff({ view, decidable, busy, actionErr, onApprove, onSendBack, onAsk }) {
  const waitingOnYou = view.status === 'waiting_on_you'
  if (view.status === 'done') return null

  return (
    <section
      className={`jobd-handoff${waitingOnYou ? ' is-you' : ''}`}
      aria-label="Current handoff"
    >
      <p className="jobd-handoff-line">
        {waitingOnYou ? 'This is waiting on you.' : view.whats_next || holderSentence(view)}
      </p>
      {waitingOnYou && (
        <>
          <div className="jobd-actions">
            {/* Approve and Send back resolve the open handoff. They only
                render when the server gave us its id — a decision button
                with nothing to call is worse than no button. */}
            {decidable && (
              <>
                <button
                  type="button"
                  className="btn btn-primary"
                  disabled={!!busy}
                  onClick={onApprove}
                >
                  {busy === 'approve' ? 'Approving…' : 'Approve'}
                </button>
                <button
                  type="button"
                  className="btn btn-ghost"
                  disabled={!!busy}
                  onClick={onSendBack}
                >
                  {busy === 'decline' ? 'Sending back…' : 'Send back'}
                </button>
              </>
            )}
            <button type="button" className="btn btn-ghost" onClick={onAsk}>
              Ask
            </button>
          </div>
          {actionErr && (
            <p className="jobd-action-err" role="alert">
              {actionErr}
            </p>
          )}
        </>
      )}
    </section>
  )
}

// --- 6. underlying runs, collapsed ----------------------------------------

// Folded away on purpose: this screen is the process, and the raw runs are the
// thing it exists to spare you. Fetched only when opened, so the spine never
// pays for them.
function AgentRuns({ itemId }) {
  const [open, setOpen] = useState(false)
  const [runs, setRuns] = useState(null)
  const [err, setErr] = useState(null)
  const asked = useRef(false)

  useEffect(() => {
    if (!open || asked.current) return undefined
    asked.current = true
    return startAbortable(({ signal, isAlive }) => {
      api
        .getWorkItem(itemId, { include: 'runs', signal })
        .then((d) => isAlive() && setRuns(Array.isArray(d?.runs) ? d.runs : []))
        .catch((e) => isAlive() && setErr(e))
    })
  }, [open, itemId])

  const retry = useCallback(() => {
    asked.current = false
    setErr(null)
    setRuns(null)
    setOpen(true)
  }, [])

  return (
    <section className="jobd-runs" aria-label="Agent runs">
      <button
        type="button"
        className="jobd-runs-toggle"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
      >
        <span>Agent runs</span>
        <span aria-hidden="true">{open ? '▾' : '▸'}</span>
      </button>
      {open && (
        <div className="jobd-runs-body">
          {err ? (
            <p className="dash-empty" role="alert">
              Couldn&apos;t load the runs.{' '}
              <button type="button" className="dash-link" onClick={retry}>
                Retry
              </button>
            </p>
          ) : runs === null ? (
            <div className="dash-skel">
              <span style={{ width: '60%' }} />
            </div>
          ) : runs.length === 0 ? (
            <p className="dash-empty">No runs recorded for this job.</p>
          ) : (
            <ul className="jobd-run-list">
              {runs.map((r, i) => (
                <li key={i} className={r.errored ? 'is-errored' : ''}>
                  <span className="jobd-run-name">{r.name}</span>
                  <span className="jobd-run-meta">
                    {r.agent}
                    {r.at ? ` · ${workUpdatedLabel(r.at)} ago` : ''}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  )
}
