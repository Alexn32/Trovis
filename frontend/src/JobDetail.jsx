import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from './api.js'
import { startAbortable } from './abortable.js'
import { WorkLoadFailed } from './ui.jsx'
import { workItemStatusLabel, workUpdatedLabel } from './board.js'
import { openAsk } from './askOpen.js'
import {
  ACTOR_LABEL, askPrompt, canDecide, jobActions, processSteps, runAgentRoute,
  jobTotals, runCost, runDuration, runErrorLine, shortHistory,
} from './jobDetail.js'
import { costProvenance, observations, sources, truncationNote } from './evidence.js'
import { boundedNote, visibilityRows } from './coverage.js'
import { ACTOR_KIND_LABELS, holderLine, possessionRows, situationEyebrow, situationFor } from './workGraph.js'
import ExecutionView from './ExecutionView.jsx'
import WorkGraphView from './WorkGraphView.jsx'

// ---------------------------------------------------------------------------
// Job detail — what a work item IS and how it moves, not a second table.
//
//   1. Title
//   2. Status + who is holding it now
//   3. Steps in order, each marked Person / Agent / Tool
//   4. The current handoff, highlighted — with judgment CTAs when it is on you
//   5. A short handoff history
//   6. Underlying agent runs, collapsed and fetched only when opened
//   7. (page only) Visibility — which parts of this run Trovis directly
//      observed, dimension by dimension; a statement of what was seen,
//      never a score, and "Unknown" is not "missing"
//   8. (page only) Evidence — who Trovis heard from and what each source
//      reported or showed, under the record it supports; never above it
//   9. (page only) Execution — a second VIEW of the same run, switched with
//      the Run | Execution control under the header: the technical tree from
//      GET /work/items/:id/execution (ExecutionView.jsx). The header, title,
//      status and back link stay; sections 3–8 are the Run view. Its fetch is
//      its own — independent of the detail, runs, evidence and coverage
//      reads — and, like theirs, resets on every item change so Run A never
//      shows inside Run B.
//  10. (page only) The Run page proper — built so a person understands the
//      work in a few seconds, in three levels under one quiet header (back
//      link, title, the job it belongs to, the Activity | Execution switch):
//
//        CURRENT SITUATION  one statement of what is happening now
//                           (workGraph.situationFor: the lean status plus
//                           possession.current_holder, and nothing else),
//                           with at most one supporting sentence built from
//                           the last Work Step ONLY when that record itself
//                           names the holder; the decision buttons when the
//                           work is waiting on you.
//        ACTIVITY           the Work Graph's steps as a vertical timeline
//                           (WorkGraphView.jsx + pure workGraph.js) — exactly
//                           `graph.steps`, one row each, in a person's words;
//                           each row opens the exact Execution node or
//                           Evidence record its provenance names.
//        DETAILS            one closed disclosure holding the record Trovis
//                           keeps: run information, Visibility (coverage),
//                           who held the work (possession history, never a
//                           "now"), Evidence, and the demoted "How this job
//                           ran" moves.
//
//      Execution is the sibling view, one click away, unchanged. Nothing in
//      the header repeats the situation (no status pill, no holder line).
//      The panel keeps its own steps, passes, current-handoff block and runs
//      fold, and never fetches the graph.
//
// Reads GET /work/items/:id (the lean detail), and /work/items/:id?include=runs
// only when someone expands the runs section. Never the fat board, never a
// span dump: the point of this screen is the process, and the raw runs are the
// one thing deliberately folded away.
//
// Two shapes, one component:
//
//   variant="panel"  a slide-over. Home's desk uses it — you act on a row and
//                    you are done, and losing your place on Home to do that
//                    would be the wrong trade.
//   variant="page"   a full page inside the Work pane. Work is where you go
//                    to look INTO a job, so the job gets the screen, keeps a
//                    back link to exactly where you came from, and shows the
//                    action list rather than folding the runs away.
//
// On the page the action list IS the runs, so there is no separate raw fold
// under it: the same eight rows twice is not more depth. The panel keeps the
// fold, because the panel has no action list.
// ---------------------------------------------------------------------------

export default function JobDetail({
  item, onClose, onResolved, onOpenAgent, onOpenJob, variant = 'panel', backLabel = '← Back',
}) {
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
  const isPage = variant === 'page'

  // The page's action list is the runs, so the page loads them on enter —
  // ONE request for the whole list, never one per action. The panel does not
  // (its fold still fetches lazily on open), so a Home desk row costs exactly
  // what it did before.
  const [runs, setRuns] = useState(null)
  const [runsErr, setRunsErr] = useState(null)
  const [runsReload, setRunsReload] = useState(0)
  useEffect(() => {
    if (!isPage) return undefined
    setRunsErr(null)
    return startAbortable(({ signal, isAlive }) => {
      api
        .getWorkItem(item.id, { include: 'runs', signal })
        .then((d) => isAlive() && setRuns(Array.isArray(d?.runs) ? d.runs : []))
        .catch((e) => isAlive() && setRunsErr(e))
    })
  }, [isPage, item.id, runsReload])
  const retryRuns = useCallback(() => {
    setRuns(null)
    setRunsErr(null)
    setRunsReload((n) => n + 1)
  }, [])
  const runsLoading = isPage && runs === null && !runsErr
  const actions = jobActions(runs, { status: view.status })

  // Evidence is supplemental and page-only: it loads for the run being
  // looked at, never for a Home desk row, and a failure here is a failed
  // section, never a failed page.
  const [evidence, setEvidence] = useState(null)
  const [evidenceErr, setEvidenceErr] = useState(null)
  const [evidenceReload, setEvidenceReload] = useState(0)
  useEffect(() => {
    if (!isPage) return undefined
    // Reset on every (re)load so a previous run's evidence never shows
    // under the next run's title while its own request is in flight.
    setEvidence(null)
    setEvidenceErr(null)
    return startAbortable(({ signal, isAlive }) => {
      api
        .getWorkItemEvidence(item.id, { signal })
        .then((d) => isAlive() && setEvidence(d && Array.isArray(d.evidence) ? d : { evidence: [] }))
        .catch((e) => isAlive() && setEvidenceErr(e))
    })
  }, [isPage, item.id, evidenceReload])
  const retryEvidence = useCallback(() => {
    setEvidence(null)
    setEvidenceErr(null)
    setEvidenceReload((n) => n + 1)
  }, [])
  const costNote = isPage ? costProvenance(evidence?.evidence) : null

  // Visibility (coverage) is its own request with its own loading, error
  // and retry: a coverage failure never hides evidence, and vice versa.
  // Page-only for the same reason evidence is. A failed request is a failed
  // section — it is never rendered as five "Unknown" rows, because Unknown
  // is a thing the server says about a dimension, not a thing the client
  // says about a request.
  const [coverage, setCoverage] = useState(null)
  const [coverageErr, setCoverageErr] = useState(null)
  const [coverageReload, setCoverageReload] = useState(0)
  useEffect(() => {
    if (!isPage) return undefined
    setCoverage(null)
    setCoverageErr(null)
    return startAbortable(({ signal, isAlive }) => {
      api
        .getWorkItemCoverage(item.id, { signal })
        .then((d) => isAlive() && setCoverage(d && Array.isArray(d.dimensions) ? d : { dimensions: [] }))
        .catch((e) => isAlive() && setCoverageErr(e))
    })
  }, [isPage, item.id, coverageReload])
  const retryCoverage = useCallback(() => {
    setCoverage(null)
    setCoverageErr(null)
    setCoverageReload((n) => n + 1)
  }, [])

  // Execution: the technical view of the same run. Fetched only while that
  // view is open, page-only, its own request with its own failure. Every
  // item change clears the body AND the selected node before anything is
  // requested, and a late response for the previous item is dropped by
  // startAbortable's isAlive — so an inspector open on Run A's tool call
  // can never sit under Run B's title.
  const [pageView, setPageView] = useState('run')
  const [execution, setExecution] = useState(null)
  const [executionErr, setExecutionErr] = useState(null)
  const [executionReload, setExecutionReload] = useState(0)
  const [selectedNode, setSelectedNode] = useState(null)
  // A Work Step's "View in Execution" names the node to select by the step's
  // own execution_node_id. The view switch refetches and resets the
  // selection, so the id waits here and is applied only once the body is
  // in — and only when that body contains exactly that node. A node the
  // read set lacks selects nothing: normal Execution, no guessed match.
  const pendingNodeRef = useRef(null)
  useEffect(() => {
    if (!isPage) return undefined
    setExecution(null)
    setExecutionErr(null)
    setSelectedNode(null)
    if (pageView !== 'execution') return undefined
    return startAbortable(({ signal, isAlive }) => {
      api
        .getWorkItemExecution(item.id, { signal })
        .then((d) => {
          if (!isAlive()) return
          const body = d && Array.isArray(d.nodes) ? d : { nodes: [], roots: [], chronology: [], trace_ids: [] }
          setExecution(body)
          const want = pendingNodeRef.current
          pendingNodeRef.current = null
          if (want && body.nodes.some((n) => n && n.id === want)) setSelectedNode(want)
        })
        .catch((e) => isAlive() && setExecutionErr(e))
    })
  }, [isPage, item.id, pageView, executionReload])
  const retryExecution = useCallback(() => {
    setExecution(null)
    setExecutionErr(null)
    setExecutionReload((n) => n + 1)
  }, [])
  const showExecution = isPage && pageView === 'execution'

  // What happened: the Work Graph, page-only, its own request with its own
  // loading, failure and Retry — a graph failure is a failed section, never
  // a failed page, and never a timeline of invented "Unknown" steps. Every
  // item change clears the body AND the selected step before anything is
  // requested; a late response for the previous item is dropped by
  // startAbortable's isAlive, so Run A's story never flashes under Run B.
  const [graph, setGraph] = useState(null)
  const [graphErr, setGraphErr] = useState(null)
  const [graphReload, setGraphReload] = useState(0)
  const [selectedStep, setSelectedStep] = useState(null)
  useEffect(() => {
    if (!isPage) return undefined
    setGraph(null)
    setGraphErr(null)
    setSelectedStep(null)
    pendingNodeRef.current = null
    return startAbortable(({ signal, isAlive }) => {
      api
        .getWorkItemGraph(item.id, { signal })
        .then((d) => isAlive() && setGraph(d && Array.isArray(d.steps) ? d : { steps: [], possession: null }))
        .catch((e) => isAlive() && setGraphErr(e))
    })
  }, [isPage, item.id, graphReload])
  const retryGraph = useCallback(() => {
    setGraph(null)
    setGraphErr(null)
    setGraphReload((n) => n + 1)
  }, [])
  // Who has the work now — the endpoint's possession.current_holder and
  // nothing else. Null until the graph is in, or when the record names no
  // current holder; the situation then says only what the status says.
  const held = isPage ? holderLine(graph?.possession) : null
  // The one statement at the top of Activity, from the lean status and that
  // canonical holder (workGraph.situationFor). Null until the detail is in.
  const situation = isPage ? situationFor(view, graph) : null

  // Work → Execution: switch views and hand the step's node id to the
  // execution fetch above. Null (from the empty state's "View Execution")
  // just switches.
  const openExecutionAt = useCallback((nodeId) => {
    pendingNodeRef.current = typeof nodeId === 'string' && nodeId ? nodeId : null
    setPageView('execution')
  }, [])

  // Work → Evidence: mark and scroll to the row whose id IS the step's
  // evidence_id. Nothing is matched by time, actor or label; a row Evidence
  // does not draw is simply not offered (WorkGraphView decides that).
  const [highlightEvidence, setHighlightEvidence] = useState(null)
  // Details is closed by default; a step's "View evidence" opens it so the
  // row it points at can be seen.
  const [detailsOpen, setDetailsOpen] = useState(false)
  const showEvidence = useCallback((id) => {
    setDetailsOpen(true)
    setHighlightEvidence(id)
  }, [])
  const bodyRef = useRef(null)
  useEffect(() => {
    setHighlightEvidence(null)
    setDetailsOpen(false)
  }, [item.id])
  useEffect(() => {
    if (!highlightEvidence) return undefined
    const frame = requestAnimationFrame(() => {
      const el = bodyRef.current?.querySelector(`[data-evidence-id="${highlightEvidence}"]`)
      if (!el) return
      if (typeof el.scrollIntoView === 'function') el.scrollIntoView({ block: 'center' })
      if (typeof el.focus === 'function') el.focus({ preventScroll: true })
    })
    return () => cancelAnimationFrame(frame)
  }, [highlightEvidence])

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
  const ask = () => openAsk(askPrompt(view))
  const jobName = String(view.workflow_name || '').trim()
  const canOpenJob = Boolean(onOpenJob && view.workflow_id)

  const body = (
    <>
        <header className="jobd-head">
          <button type="button" className="jobd-close" onClick={onClose}>
            {isPage ? backLabel : '← Back'}
          </button>
          <h2 className="jobd-title">{view.title}</h2>
          {/* The page header carries identity only: the title above, the
              job it belongs to here. What is happening is the Current
              situation's one statement, not a pill and a holder line
              repeating it. The panel keeps its compact status line. */}
          {isPage ? (
            jobName && (
              <p className="run-job">
                Part of{' '}
                {canOpenJob ? (
                  <button type="button" className="run-job-link" onClick={() => onOpenJob(view.workflow_id)}>
                    {jobName}
                  </button>
                ) : (
                  <span className="run-job-name">{jobName}</span>
                )}
              </p>
            )
          ) : (
            <div className="jobd-status">
              <span className={`work-status-pill ${view.status || ''}`}>
                {workItemStatusLabel(view.status)}
              </span>
              <span className="jobd-holder">{holderSentence(view)}</span>
              {view.updated_at && (
                <span className="jobd-age">{workUpdatedLabel(view.updated_at)}</span>
              )}
            </div>
          )}
          {/* Two views of one run. The record above this line is shared;
              only what sits below it changes. */}
          {isPage && (
            <nav className="jobd-views" role="tablist" aria-label="Run views">
              <button
                type="button"
                role="tab"
                aria-selected={pageView === 'run'}
                className={`jobd-view${pageView === 'run' ? ' is-active' : ''}`}
                onClick={() => setPageView('run')}
              >
                Activity
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={pageView === 'execution'}
                className={`jobd-view${pageView === 'execution' ? ' is-active' : ''}`}
                onClick={() => setPageView('execution')}
              >
                Execution
              </button>
            </nav>
          )}
        </header>

        {showExecution ? (
          <ExecutionView
            body={execution}
            failed={Boolean(executionErr)}
            onRetry={retryExecution}
            selectedId={selectedNode}
            onSelect={setSelectedNode}
          />
        ) : err && !detail ? (
          <WorkLoadFailed
            lead="Can't load this job"
            onRetry={() => {
              setErr(null)
              setReload((n) => n + 1)
            }}
          />
        ) : isPage ? (
          <>
            {/* LEVEL 1 — what is happening now, said once. */}
            <RunSituation
              situation={situation}
              view={view}
              held={held}
              decidable={decidable}
              busy={busy}
              actionErr={actionErr}
              onApprove={() => resolve('approve')}
              onSendBack={() => resolve('decline')}
            />

            {/* LEVEL 2 — what meaningful things happened, from the Work Graph. */}
            <WorkGraphView
              body={graph}
              failed={Boolean(graphErr)}
              onRetry={retryGraph}
              evidence={evidence}
              selectedId={selectedStep}
              onSelect={setSelectedStep}
              onOpenExecution={openExecutionAt}
              onShowEvidence={showEvidence}
            />

            {/* LEVEL 3 — the record Trovis keeps, one disclosure away. */}
            <details
              className="jobd-section run-details"
              aria-label="Details"
              open={detailsOpen}
              onToggle={(e) => setDetailsOpen(Boolean(e.currentTarget.open))}
            >
              <summary className="run-details-summary">
                <span className="run-details-title">Details</span>
              </summary>
              <div className="run-details-body">
                <RunInformation
                  view={view}
                  runs={runs}
                  costNote={costNote}
                  jobName={jobName}
                  canOpenJob={canOpenJob}
                  onOpenJob={onOpenJob}
                />
                <VisibilitySection
                  body={coverage}
                  failed={Boolean(coverageErr)}
                  onRetry={retryCoverage}
                />
                <PossessionHistory possession={graph?.possession} />
                <EvidenceSection
                  body={evidence}
                  failed={Boolean(evidenceErr)}
                  onRetry={retryEvidence}
                  highlightId={highlightEvidence}
                />
                <ActionList
                  actions={actions}
                  loading={runsLoading}
                  failed={Boolean(runsErr)}
                  onRetry={retryRuns}
                  steps={steps}
                  hidden={hidden}
                  detail={detail}
                  onOpenAgent={onOpenAgent}
                  folded
                />
              </div>
            </details>
          </>
        ) : (
          <>
            <CurrentHandoff
              view={view}
              decidable={decidable}
              busy={busy}
              actionErr={actionErr}
              onApprove={() => resolve('approve')}
              onSendBack={() => resolve('decline')}
              onAsk={ask}
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

            {/* The page tells this story in Activity, from the same
                lifecycle records; the log stays for the panel, which has no
                Work Graph. */}
            {!isPage && history.length > 0 && (
              <section className="jobd-section" aria-label="Recent passes">
                <h3 className="dash-caps">Recent passes</h3>
                <ul className="jobd-history">
                  {history.map((h, i) => (
                    <li key={i}>
                      <span className="jobd-hist-text">{h.text}</span>
                      {/* Repeats are collapsed, and the count says so rather
                          than the row quietly standing for five. */}
                      {h.count > 1 && (
                        <span className="jobd-hist-count">×{h.count}</span>
                      )}
                      {h.at && (
                        <span className="jobd-hist-at">{workUpdatedLabel(h.at)} ago</span>
                      )}
                    </li>
                  ))}
                </ul>
              </section>
            )}

            {/* The page's action list already IS these runs, so folding the
                same rows underneath it would be depth in name only. */}
            {!isPage && <AgentRuns itemId={item.id} onOpenAgent={onOpenAgent} />}
          </>
        )}
    </>
  )

  if (isPage) {
    return (
      <div className="view job-page" aria-label={view.title} ref={bodyRef}>
        {body}
      </div>
    )
  }
  return (
    <>
      <div className="bpanel-scrim" onClick={onClose} />
      <aside className="jobd" role="dialog" aria-label={view.title}>
        {body}
      </aside>
    </>
  )
}

/**
 * How this job ran: one row per MOVE, oldest first.
 *
 * A different cut from both neighbours, which is why it earns its own list.
 * The kind page's path is coarse hands (Agent → Tool → Person) and says
 * nothing about how many times each acted; Recent passes is who was holding
 * the job. Two tool calls are two moves here, one Tool node there, and no
 * pass at all.
 *
 * When the record has no action-shaped rows we fall back to the old spine
 * rather than drawing an empty frame — the process is still true even when
 * the moves behind it were never exported.
 *
 * `folded` (the page, since the Work Graph took the story): the same list
 * one disclosure away, closed by default. The moves are the agent's own
 * reports of what it reached for — technical detail whose full form is the
 * Execution view — so they sit under What happened rather than beside it.
 */
function ActionList({ actions, loading, failed, onRetry, steps, hidden, detail, onOpenAgent, folded = false }) {
  const frame = (inner) =>
    folded ? (
      <details className="jobd-section jobd-moves" aria-label="How this job ran">
        <summary className="dash-caps">
          How this job ran
          <span className="jobd-moves-hint">moves the agent reported</span>
        </summary>
        {inner}
      </details>
    ) : (
      <section className="jobd-section" aria-label="How this job ran">
        <h3 className="dash-caps">How this job ran</h3>
        {inner}
      </section>
    )

  if (loading) {
    return frame(
      <div className="dash-skel">
        <span style={{ width: '70%' }} />
        <span style={{ width: '50%' }} />
      </div>,
    )
  }

  if (actions.length === 0) {
    return frame(
      <>
        {failed && (
          <p className="dash-empty" role="alert">
            Couldn&apos;t load what this job did.{' '}
            <button type="button" className="dash-link" onClick={onRetry}>
              Retry
            </button>
          </p>
        )}
        {/* No moves on the record — show the route it took instead of an
            empty frame. Absent beats invented. */}
        {!detail ? null : steps.length === 0 ? (
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
                  {s.text && !s.isCurrent && (
                    <span className="jobd-step-text">{s.text}</span>
                  )}
                </li>
              ))}
            </ol>
          </>
        )}
      </>,
    )
  }

  return frame(
      <ol className="jobd-acts">
        {actions.map((a, i) => (
          <li
            key={i}
            className={`jobd-act${a.errored ? ' is-errored' : ''}${a.isCurrent ? ' is-current' : ''}`}
          >
            <span className="jobd-act-dot" aria-hidden="true" />
            <div className="jobd-act-main">
              <span className="jobd-act-what">{a.what}</span>
              <span className={`jobd-act-result${a.errored ? ' is-errored' : ''}`}>
                {a.result}
              </span>
            </div>
            <div className="jobd-act-meta">
              {a.who.name &&
                (onOpenAgent && a.route ? (
                  <button
                    type="button"
                    className="jobd-run-agent"
                    onClick={() => onOpenAgent(a.route[0], a.route[1])}
                  >
                    {a.who.name}
                  </button>
                ) : (
                  <span className="jobd-run-agent is-plain">{a.who.name}</span>
                ))}
              {a.system && <span className="jobd-act-system">{a.system}</span>}
              {a.at && <span>{workUpdatedLabel(a.at)} ago</span>}
              {a.duration && <span>{a.duration}</span>}
              {a.cost && <span>{a.cost}</span>}
              {a.isCurrent && <span className="jobd-act-now">now</span>}
            </div>
            {a.reason && <p className="jobd-run-why">{a.reason}</p>}
          </li>
        ))}
      </ol>,
  )
}

// --- 7. visibility -----------------------------------------------------------

/**
 * Which parts of this run Trovis directly observed — five fixed dimensions,
 * each with one state and one sentence. Nothing is totalled: five rows are
 * not a checklist, and there is no percentage, grade, colour or verdict.
 * "Unknown" reads as exactly that — the record cannot say — never as
 * "missing" or "failed". Sources belong to Evidence, below, not here.
 *
 * Three states that must never blur: loading (skeleton), failed (a local
 * error with Retry, and NOT five Unknown rows), and a body with no
 * dimensions (an honest sentence).
 */
function VisibilitySection({ body, failed, onRetry }) {
  const loading = body === null && !failed
  const rows = visibilityRows(body)
  const note = boundedNote(body)

  return (
    <section className="jobd-section jobd-visibility" aria-label="Visibility">
      <h3 className="dash-caps">Visibility</h3>
      {loading ? (
        <div className="dash-skel">
          <span style={{ width: '45%' }} />
          <span style={{ width: '60%' }} />
        </div>
      ) : failed ? (
        <p className="dash-empty" role="alert">
          Visibility couldn&apos;t be loaded.{' '}
          <button type="button" className="dash-link" onClick={onRetry}>
            Retry
          </button>
        </p>
      ) : rows.length === 0 ? (
        <p className="dash-empty">Visibility isn&apos;t available for this run.</p>
      ) : (
        <>
          <p className="jobd-vis-lead">Which parts of this run Trovis directly observed.</p>
          <ul className="jobd-vis-list">
            {rows.map((row) => (
              <li key={row.id} className={`jobd-vis-row state-${row.state}`} data-dimension={row.id}>
                <span className="jobd-vis-label">{row.label}</span>
                <span className={`jobd-vis-state state-${row.state}`}>{row.stateLabel}</span>
                <span className="jobd-vis-text">{row.text}</span>
              </li>
            ))}
          </ul>
          {note && <p className="jobd-vis-note">{note}</p>}
        </>
      )}
    </section>
  )
}

// --- 8. evidence -------------------------------------------------------------

/**
 * What supports the record above. Sources first — who Trovis heard from and
 * what kind of thing each said — then the observations that add something
 * the steps and passes do not already say: an action a worker REPORTED, a
 * state an external system SHOWED, the record closing. "Reported by" and
 * "Observed from" are kept apart on purpose, and "verified" is not a word
 * this section uses. Ids live behind Details.
 *
 * Three states that must never blur: loading (skeleton), failed (a local
 * error with Retry — the page stays), and empty (an honest sentence, not
 * "nothing happened" and not "not connected").
 */
function EvidenceSection({ body, failed, onRetry, highlightId = null }) {
  const loading = body === null && !failed
  const records = body?.evidence || []
  const srcs = sources(records)
  const obs = observations(records)
  const trunc = truncationNote(body)

  return (
    <section className="jobd-section jobd-evidence" aria-label="Evidence">
      <h3 className="dash-caps">Evidence</h3>
      {loading ? (
        <div className="dash-skel">
          <span style={{ width: '55%' }} />
          <span style={{ width: '40%' }} />
        </div>
      ) : failed ? (
        <p className="dash-empty" role="alert">
          Evidence couldn&apos;t be loaded.{' '}
          <button type="button" className="dash-link" onClick={onRetry}>
            Retry
          </button>
        </p>
      ) : records.length === 0 ? (
        <p className="dash-empty">No supporting evidence is available for this run.</p>
      ) : (
        <>
          {trunc && <p className="jobd-ev-note-trunc">{trunc}</p>}
          <ul className="jobd-ev-sources" aria-label="Sources">
            {srcs.map((s) => (
              <li key={s.key} className={`jobd-ev-source kind-${s.kind}`}>
                <span className="jobd-ev-source-name">{s.name}</span>
                {s.hint && <span className="jobd-ev-source-hint">{s.hint}</span>}
                <span className="jobd-ev-source-lines">
                  {s.lines.map((l) => <span key={l}>{l}</span>)}
                </span>
                {s.lastAt && (
                  <span className="jobd-ev-source-at" title={s.lastAt}>
                    last {workUpdatedLabel(s.lastAt)} ago
                  </span>
                )}
              </li>
            ))}
          </ul>
          {obs.length > 0 && (
            <ul className="jobd-ev-list" aria-label="Observations">
              {obs.map((o) => (
                <li
                  key={o.id}
                  className={`jobd-ev-row kind-${o.kind}${o.errored ? ' is-errored' : ''}${highlightId === o.id ? ' is-highlighted' : ''}`}
                  data-evidence-id={o.id}
                  tabIndex={highlightId === o.id ? -1 : undefined}
                >
                  <div className="jobd-ev-top">
                    <span className="jobd-ev-title">{o.title}</span>
                    {o.at && (
                      <span className="jobd-ev-at" title={o.at}>{workUpdatedLabel(o.at)} ago</span>
                    )}
                  </div>
                  <span className="jobd-ev-prov">{o.provenance}</span>
                  {o.note && <p className="jobd-ev-note">{o.note}</p>}
                  {o.details.length > 0 && (
                    <details className="jobd-ev-details">
                      <summary>Details</summary>
                      <dl className="jobd-ev-dl">
                        {o.details.map(([k, v]) => (
                          <EvidenceDetail key={k} label={k} value={v} />
                        ))}
                      </dl>
                    </details>
                  )}
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </section>
  )
}

const MONO_DETAILS = new Set(['Span', 'Trace', 'External object', 'Provider event id', 'Provider event', 'Reported action', 'Operation'])

function EvidenceDetail({ label, value }) {
  return (
    <>
      <dt>{label}</dt>
      <dd className={MONO_DETAILS.has(label) ? 'mono' : ''}>{String(value)}</dd>
    </>
  )
}

// --- the page: current situation, run information, who held the work -------

/**
 * LEVEL 1. One statement of what is happening now (workGraph.situationFor),
 * one supporting sentence when the record itself supports one, and — only
 * when the work is waiting on the reader AND the server gave us the handoff
 * to resolve — the two genuine decisions on that handoff, as quiet
 * secondary controls under the sentence. Nothing else: no pill, no holder
 * line, no second phrasing of the same state, and no Ask here (the page
 * already has the global Ask pill; Ask is not a disposition of the work).
 */
function RunSituation({ situation, view, held, decidable, busy, actionErr, onApprove, onSendBack }) {
  const waitingOnYou = view.status === 'waiting_on_you'
  if (!situation) {
    return (
      <section className="run-now" aria-label="Current situation">
        <div className="dash-skel">
          <span style={{ width: '38%' }} />
        </div>
      </section>
    )
  }
  const ago = situation.support?.at ? workUpdatedLabel(situation.support.at) : ''
  // One quiet word from the lean status ("Waiting", "Needs attention",
  // "Closed"); dropped when it would only repeat the headline.
  const eyebrow = situationEyebrow(view.status, situation.headline)
  return (
    <section
      className={`run-now${waitingOnYou ? ' is-you' : ''}`}
      aria-label="Current situation"
      data-holder={held ? held.label : undefined}
    >
      {eyebrow && <p className="run-now-eyebrow">{eyebrow}</p>}
      <p className="run-now-state">{situation.headline}</p>
      {situation.support && (
        <p className="run-now-support">
          {situation.support.text}
          {ago ? ` ${ago} ago` : ''}.
        </p>
      )}
      {waitingOnYou && decidable && (
        <div className="run-now-actions">
          <button type="button" className="btn btn-ghost" disabled={!!busy} onClick={onApprove}>
            {busy === 'approve' ? 'Approving…' : 'Approve'}
          </button>
          <button type="button" className="btn btn-ghost" disabled={!!busy} onClick={onSendBack}>
            {busy === 'decline' ? 'Sending back…' : 'Send back'}
          </button>
        </div>
      )}
      {waitingOnYou && actionErr && (
        <p className="jobd-action-err" role="alert">
          {actionErr}
        </p>
      )}
    </section>
  )
}

/**
 * Run information, inside Details: the identity and bookkeeping the header
 * no longer carries. Every row exists only when the record has the value;
 * nothing here is a priority, an owner, a source name or a verdict.
 */
function RunInformation({ view, runs, costNote, jobName, canOpenJob, onOpenJob }) {
  const totals = jobTotals(runs)
  const rows = []
  if (jobName) {
    rows.push(['Job', canOpenJob ? (
      <button type="button" className="run-job-link" onClick={() => onOpenJob(view.workflow_id)}>{jobName}</button>
    ) : jobName])
  }
  if (view.id) rows.push(['Run ID', String(view.id)])
  if (view.status) rows.push(['Status', workItemStatusLabel(view.status)])
  if (view.updated_at) rows.push(['Last updated', `${workUpdatedLabel(view.updated_at)} ago`])
  // Totals for the whole job, and only when the record has them. Same rule
  // as a run's own line: $0.00 is not a cost. The cost's provenance rides
  // beside the figure, only when the evidence says how it was priced.
  for (const t of totals) {
    const isCost = t.startsWith('$')
    rows.push([isCost ? 'Cost' : 'Duration', isCost && costNote ? `${t} · ${costNote}` : t])
  }
  if (rows.length === 0) return null
  return (
    <section className="jobd-section run-info" aria-label="Run information">
      <h3 className="dash-caps">Run information</h3>
      <dl className="run-info-dl">
        {rows.map(([k, v]) => (
          <div key={k} className="run-info-row">
            <dt>{k}</dt>
            <dd>{v}</dd>
          </div>
        ))}
      </dl>
    </section>
  )
}

/**
 * Who held the work, in order — possession.segments as history and nothing
 * more: holder, kind, each segment's own waiting flag. No row is "now":
 * current possession is the situation's, from possession.current_holder.
 */
function PossessionHistory({ possession }) {
  const rows = possessionRows(possession)
  if (rows.length === 0) return null
  return (
    <section className="jobd-section run-held" aria-label="Who held the work">
      <h3 className="dash-caps">Who held the work</h3>
      <ol className="jobd-work-history-list" aria-label="Who held the work, in order">
        {rows.map((h) => (
          <li key={h.key} className={`kind-${h.kind}`}>
            <span className="jobd-work-kind">{ACTOR_KIND_LABELS[h.kind]}</span>
            <span className="jobd-work-history-holder">{h.label}</span>
            {h.waiting && <span className="jobd-work-history-flag">waiting</span>}
          </li>
        ))}
      </ol>
    </section>
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
        <DecisionActions
          decidable={decidable}
          busy={busy}
          actionErr={actionErr}
          onApprove={onApprove}
          onSendBack={onSendBack}
          onAsk={onAsk}
        />
      )}
    </section>
  )
}

/**
 * The panel's decision block: the two real calls on an open handoff, plus
 * the panel's own Ask (the panel has no global pill). The page's Current
 * situation renders the two decisions itself, quieter and without Ask.
 */
function DecisionActions({ decidable, busy, actionErr, onApprove, onSendBack, onAsk }) {
  return (
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
  )
}

/**
 * One run in the fold. Everything here is optional and everything absent is
 * simply not drawn: this is the last thing on the page and the only place a
 * technical reader is served, so it must not pad itself with placeholders
 * that read like measurements.
 *
 * The agent's name is the door out — it opens that agent's page in Fleet, but
 * only when we were handed both a route and somewhere to send it. Otherwise
 * it stays text rather than becoming a button that goes nowhere.
 */
function RunRow({ run, onOpenAgent }) {
  const route = runAgentRoute(run)
  const canOpen = Boolean(onOpenAgent && route)
  const duration = runDuration(run.duration_ms)
  const cost = runCost(run.cost_usd)
  const reason = runErrorLine(run)

  return (
    <li className={run.errored ? 'is-errored' : ''}>
      <div className="jobd-run-top">
        <span className="jobd-run-name">{run.name}</span>
        <span className={`jobd-run-result${run.errored ? ' is-errored' : ''}`}>
          {run.errored ? 'Error' : 'OK'}
        </span>
      </div>
      <div className="jobd-run-meta">
        {run.agent &&
          (canOpen ? (
            <button
              type="button"
              className="jobd-run-agent"
              onClick={() => onOpenAgent(route[0], route[1])}
            >
              {run.agent}
            </button>
          ) : (
            <span className="jobd-run-agent is-plain">{run.agent}</span>
          ))}
        {run.at && <span>{workUpdatedLabel(run.at)} ago</span>}
        {duration && <span>{duration}</span>}
        {cost && <span>{cost}</span>}
      </div>
      {reason && <p className="jobd-run-why">{reason}</p>}
    </li>
  )
}

// --- 6. underlying runs, collapsed ----------------------------------------

// Folded away on purpose: this screen is the process, and the raw runs are the
// thing it exists to spare you. Fetched only when opened, so the spine never
// pays for them.
function AgentRuns({ itemId, onOpenAgent }) {
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
                <RunRow key={i} run={r} onOpenAgent={onOpenAgent} />
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  )
}
