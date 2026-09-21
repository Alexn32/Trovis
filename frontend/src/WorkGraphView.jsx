import {
  ACTOR_KIND_LABELS, STEP_TYPE_LABELS, actorDisplay, boundedNote, evidenceRecordFor,
  evidenceRowShown, normalizeGraph, possessionRows, stepContext, stepDetailRows,
  stepTechnicalRows, stepTimeLabels, supportLine,
} from './workGraph.js'

// ---------------------------------------------------------------------------
// What happened — the Work Graph as the Run's operational story.
//
// The Run view IS the Work view: under the shared header this section is
// the first thing a person reads, before Visibility and Evidence. It draws
// GET /work/items/:id/graph as a calm vertical timeline of the endpoint's
// Work Steps — one row per step, oldest first, in the endpoint's order —
// and nothing else:
//
//   WorkStep        who (actor) · what (the endpoint's label) · recorded
//                   context · when. Selecting a row opens a small details
//                   area under it: recorded context, the supporting Evidence
//                   record in Evidence's own words, "View in Execution" when
//                   the step has an execution_node_id, "View evidence" when
//                   its evidence_id is a row Evidence draws, and the raw ids
//                   behind a "Technical details" fold.
//   empty state     zero steps is a truthful sparse record: Trovis has no
//                   explicit work change to show — never "no activity".
//   footer          who held the work, in order (the endpoint's possession
//                   segments, quietly, as history) and the bounded note.
//
// Not a node graph: no canvas, no edges, no arrows. Chronology is the only
// relationship drawn. No step is ever generated here; `lifecycle` is not
// read; nothing is inferred from Evidence or Execution.
// ---------------------------------------------------------------------------

export default function WorkGraphView({
  body, failed, onRetry, evidence, selectedId, onSelect, onOpenExecution, onShowEvidence,
}) {
  const loading = body === null && !failed
  const g = normalizeGraph(body)
  const times = stepTimeLabels(g.steps)
  const history = possessionRows(g.possession)
  const note = boundedNote(body)

  return (
    <section className="jobd-section jobd-work" aria-label="What happened">
      <h3 className="dash-caps">What happened</h3>
      {loading ? (
        <div className="dash-skel">
          <span style={{ width: '62%' }} />
          <span style={{ width: '48%' }} />
          <span style={{ width: '55%' }} />
        </div>
      ) : failed ? (
        <p className="dash-empty" role="alert">
          What happened couldn&apos;t be loaded.{' '}
          <button type="button" className="dash-link" onClick={onRetry}>
            Retry
          </button>
        </p>
      ) : g.steps.length === 0 ? (
        <div className="jobd-work-empty">
          <p className="dash-empty">
            Trovis doesn&apos;t have explicit work changes to show for this run yet.
          </p>
          <p className="jobd-work-empty-sub">
            Technical execution may still be available in Execution.
            {onOpenExecution && (
              <>
                {' '}
                <button type="button" className="dash-link" onClick={() => onOpenExecution(null)}>
                  View Execution →
                </button>
              </>
            )}
          </p>
        </div>
      ) : (
        <ol className="jobd-work-steps" aria-label="Work steps in time order">
          {g.steps.map((step) => (
            <WorkStep
              key={step.id}
              step={step}
              time={times.get(step.id)}
              selected={selectedId === step.id}
              onSelect={onSelect}
              evidence={evidence}
              onOpenExecution={onOpenExecution}
              onShowEvidence={onShowEvidence}
            />
          ))}
        </ol>
      )}
      {!loading && !failed && (history.length > 0 || note) && (
        <div className="jobd-work-foot">
          {history.length > 0 && (
            <details className="jobd-work-history">
              <summary>Who held the work</summary>
              <ol className="jobd-work-history-list" aria-label="Who held the work, in order">
                {history.map((h) => (
                  <li key={h.key} className={`kind-${h.kind}${h.current ? ' is-current' : ''}`}>
                    <span className="jobd-work-kind">{ACTOR_KIND_LABELS[h.kind]}</span>
                    <span className="jobd-work-history-holder">{h.label}</span>
                    {h.waiting && <span className="jobd-work-history-flag">waiting</span>}
                    {h.current && <span className="jobd-work-history-flag">now</span>}
                  </li>
                ))}
              </ol>
            </details>
          )}
          {note && <p className="jobd-work-note">{note}</p>}
        </div>
      )}
    </section>
  )
}

/**
 * One Work Step. The row is a button (keyboard first: Enter/Space toggle
 * the details, aria-expanded says which way); the selected state is a class
 * AND aria-expanded, never colour alone. Everything shown comes from the
 * step's own fields.
 */
function WorkStep({ step, time, selected, onSelect, evidence, onOpenExecution, onShowEvidence }) {
  const actor = actorDisplay(step.actor)
  const context = stepContext(step)
  const detailsId = `work-step-details-${step.id.replace(/[^a-zA-Z0-9_-]/g, '-')}`
  const typeLabel = STEP_TYPE_LABELS[step.type] || STEP_TYPE_LABELS.other

  return (
    <li className={`jobd-work-step type-${step.type}${selected ? ' is-selected' : ''}`} data-step-id={step.id}>
      <span className="jobd-work-dot" aria-hidden="true" />
      <button
        type="button"
        className="jobd-work-row"
        aria-expanded={selected}
        aria-controls={detailsId}
        onClick={() => onSelect(selected ? null : step.id)}
      >
        <span className="jobd-work-main">
          {actor && (
            <span className="jobd-work-who">
              <span className="jobd-work-kind">{ACTOR_KIND_LABELS[actor.kind]}</span>
              <span className="jobd-work-actor">{actor.label}</span>
            </span>
          )}
          <span className="jobd-work-label">{step.label}</span>
          {context && <span className="jobd-work-context">{context}</span>}
        </span>
        <span className="jobd-work-meta">
          <span className="jobd-work-type">{typeLabel}</span>
          {time && (
            <time className="jobd-work-at" dateTime={step.at || undefined} title={step.at || undefined}>
              {time}
            </time>
          )}
        </span>
      </button>
      {selected && (
        <WorkStepDetails
          id={detailsId}
          step={step}
          evidence={evidence}
          onOpenExecution={onOpenExecution}
          onShowEvidence={onShowEvidence}
        />
      )}
    </li>
  )
}

/**
 * The details area under a selected step. Recorded context first; then the
 * supporting record, named the way Evidence names it; then the two doors —
 * each only when the step's OWN reference exists (both are nullable, and a
 * null reference is no button, not a disabled one); then the ids, folded.
 */
function WorkStepDetails({ id, step, evidence, onOpenExecution, onShowEvidence }) {
  const rows = stepDetailRows(step)
  const tech = stepTechnicalRows(step)
  const p = step.provenance || {}
  const evidenceId = typeof p.evidence_id === 'string' && p.evidence_id ? p.evidence_id : null
  const nodeId = typeof p.execution_node_id === 'string' && p.execution_node_id ? p.execution_node_id : null
  const rec = evidenceId ? evidenceRecordFor(evidence, evidenceId) : null
  const support = rec ? supportLine(rec) : null
  const canShowEvidence = Boolean(evidenceId && onShowEvidence && evidenceRowShown(evidence, evidenceId))

  return (
    <div className="jobd-work-details" id={id}>
      {rows.length > 0 && (
        <dl className="jobd-work-dl">
          {rows.map((r) => (
            <div key={r.label} className="jobd-work-dl-row">
              <dt>{r.label}</dt>
              <dd>{r.value}</dd>
            </div>
          ))}
        </dl>
      )}
      {support && <p className="jobd-work-support">Supported by {support}</p>}
      {(nodeId && onOpenExecution) || canShowEvidence ? (
        <div className="jobd-work-actions">
          {nodeId && onOpenExecution && (
            <button type="button" className="dash-link" onClick={() => onOpenExecution(nodeId)}>
              View in Execution
            </button>
          )}
          {canShowEvidence && (
            <button type="button" className="dash-link" onClick={() => onShowEvidence(evidenceId)}>
              View evidence
            </button>
          )}
        </div>
      ) : null}
      {tech.length > 0 && (
        <details className="jobd-ev-details jobd-work-tech">
          <summary>Technical details</summary>
          <dl className="jobd-work-dl">
            {tech.map((r) => (
              <div key={r.label} className="jobd-work-dl-row">
                <dt>{r.label}</dt>
                <dd className={r.mono ? 'mono' : ''}>{r.value}</dd>
              </div>
            ))}
          </dl>
        </details>
      )}
    </div>
  )
}
