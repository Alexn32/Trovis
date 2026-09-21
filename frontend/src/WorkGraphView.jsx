import {
  boundedNote, evidenceRecordFor, evidenceRowShown, normalizeGraph, stepDetailRows,
  stepHeadline, stepLine, stepTechnicalRows, stepTimeLabels, supportLine,
} from './workGraph.js'

// ---------------------------------------------------------------------------
// Activity — the Work Graph as the Run's story, told simply.
//
// Under the Current situation this is what a person reads: one row per Work
// Step the endpoint returned, oldest first, in the endpoint's order —
//
//   10:39 AM
//   Chief of Staff → Alex
//   Handed to a person · for review
//
// — time, who (and who or what they passed the work to, when the record
// names one), then the endpoint's own label with the one recorded detail
// that adds something. No type tags, no actor-kind caps, no event names in
// the row: those live in the details area a row opens (recorded context,
// the supporting Evidence record in Evidence's words, "View in Execution"
// when the step has an execution_node_id, "View evidence" when its
// evidence_id is a row Evidence draws, and the raw ids behind a fold).
//
// Zero steps is a truthful sparse record: Trovis has no explicit work
// change to show — never "no activity". One step is a one-row story and is
// meant to look like one. Not a node graph: no canvas, no edges. No step is
// ever generated here; `lifecycle` is not read; nothing is inferred from
// Evidence or Execution; who holds the work now is the Current situation's
// question, answered from possession.current_holder, never from a row.
// ---------------------------------------------------------------------------

export default function WorkGraphView({
  body, failed, onRetry, evidence, selectedId, onSelect, onOpenExecution, onShowEvidence,
}) {
  const loading = body === null && !failed
  const g = normalizeGraph(body)
  const times = stepTimeLabels(g.steps)
  const note = boundedNote(body)

  return (
    <section className="jobd-section jobd-work" aria-label="Activity">
      <h3 className="run-h">Activity</h3>
      {loading ? (
        <div className="dash-skel">
          <span style={{ width: '62%' }} />
          <span style={{ width: '48%' }} />
        </div>
      ) : failed ? (
        <p className="dash-empty" role="alert">
          Activity couldn&apos;t be loaded.{' '}
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
        <ol className="jobd-work-steps" aria-label="Activity in time order">
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
      {!loading && !failed && note && <p className="jobd-work-note">{note}</p>}
    </section>
  )
}

/**
 * One Work Step. The row is a button (Enter/Space toggle the details,
 * aria-expanded says which way); the selected state is a class AND
 * aria-expanded, never colour alone. Everything shown comes from the step's
 * own fields.
 */
function WorkStep({ step, time, selected, onSelect, evidence, onOpenExecution, onShowEvidence }) {
  const detailsId = `work-step-details-${step.id.replace(/[^a-zA-Z0-9_-]/g, '-')}`

  return (
    <li className={`jobd-work-step type-${step.type}${selected ? ' is-selected' : ''}`} data-step-id={step.id}>
      <button
        type="button"
        className="jobd-work-row"
        aria-expanded={selected}
        aria-controls={detailsId}
        onClick={() => onSelect(selected ? null : step.id)}
      >
        <time className="jobd-work-at" dateTime={step.at || undefined} title={step.at || undefined}>
          {time || '—'}
        </time>
        <span className="jobd-work-dot" aria-hidden="true" />
        <span className="jobd-work-main">
          <span className="jobd-work-headline">{stepHeadline(step)}</span>
          <span className="jobd-work-line">{stepLine(step)}</span>
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
