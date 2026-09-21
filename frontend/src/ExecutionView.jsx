import { useEffect, useMemo, useRef, useState } from 'react'
import {
  TYPE_LABELS, boundedNote, buildTree, chronologyRows, defaultExpanded, diagnosticsNote,
  executionSummary, inspectorSections, nodeErrorLine, nodeMeta, nodeSubtitle, nodeTitle,
  rootGroups, subtreeErrors, technicalDetails, useChronologyFallback,
} from './execution.js'

// ---------------------------------------------------------------------------
// Execution — how a run technically executed, as a VIEW UNDER THE RUN.
//
// The Run page stays the anchor: same header, same title, same back link.
// This view answers the engineer's question ("what executed, in what
// structure, with which models and tools, where did it fail, what did it
// cost") from GET /work/items/:id/execution and nothing else:
//
//   ExecutionTree         a vertical tree drawn from the endpoint's recorded
//                         parent ids only. Roots are roots; several roots and
//                         several traces are normal and are shown as such.
//   ExecutionChronology   the fallback when the endpoint recorded no parent
//                         relationship at all (execution.js
//                         useChronologyFallback): observed activity in the
//                         endpoint's own time order, with no structure drawn.
//   ExecutionInspector    a right-side panel for the selected node: identity,
//                         timing, model, tool, status, event, provenance, and
//                         the raw ids behind a "Technical details" fold.
//
// Quiet by default: a normal node carries no badge; an error is the one thing
// that stands out. No verdict, no score, no "success", no "verified".
// ---------------------------------------------------------------------------

export default function ExecutionView({ body, failed, onRetry, selectedId, onSelect }) {
  const loading = body === null && !failed
  const nodes = Array.isArray(body?.nodes) ? body.nodes : []
  const tree = useMemo(() => buildTree(body), [body])
  const groups = useMemo(() => rootGroups(body, tree), [body, tree])
  const errorBranches = useMemo(() => subtreeErrors(tree), [tree])
  const fallback = useChronologyFallback(body)
  const summary = executionSummary(body)
  const selected = selectedId ? tree.byId.get(selectedId) || null : null

  // Expansion is per body: a new run's tree opens by the deterministic rule
  // (all nodes up to a count, else roots), never inheriting the last run's.
  const [expanded, setExpanded] = useState(() => new Set())
  useEffect(() => {
    setExpanded(defaultExpanded(tree))
  }, [tree])
  const toggle = (id) =>
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })

  // A selection that arrives from outside this view (a Work Step's "View in
  // Execution", by the step's own execution_node_id) may sit behind a
  // collapsed ancestor on a large tree. Open the recorded ancestors — the
  // endpoint's parent ids, nothing else — and bring the row into view. A
  // selectedId the body does not contain selects nothing and opens nothing.
  const rootRef = useRef(null)
  useEffect(() => {
    if (!selectedId || !tree.byId.has(selectedId)) return undefined
    setExpanded((prev) => {
      const next = new Set(prev)
      let cur = tree.byId.get(selectedId)
      const seen = new Set()
      while (cur && cur.parent_id && tree.byId.has(cur.parent_id) && !seen.has(cur.id)) {
        seen.add(cur.id)
        next.add(cur.parent_id)
        cur = tree.byId.get(cur.parent_id)
      }
      return next
    })
    const frame = requestAnimationFrame(() => {
      const el = rootRef.current?.querySelector(`[data-node-id="${selectedId}"]`)
      if (el && typeof el.scrollIntoView === 'function') el.scrollIntoView({ block: 'nearest' })
    })
    return () => cancelAnimationFrame(frame)
  }, [selectedId, tree])

  const facts = body
    ? [
        `${summary.nodes} ${summary.nodes === 1 ? 'node' : 'nodes'}`,
        summary.traces > 1 ? `${summary.traces} traces` : null,
        summary.observed ? `observed over ${summary.observed}` : null,
        summary.tokens,
        summary.cost,
        summary.errors ? `${summary.errors} ${summary.errors === 1 ? 'error' : 'errors'}` : null,
      ].filter(Boolean)
    : []

  return (
    <section className="jobd-section exec" aria-label="Execution" ref={rootRef}>
      <header className="exec-head">
        <h3 className="dash-caps">Execution</h3>
        <p className="exec-lead">How this run executed, as its telemetry recorded it.</p>
        {!loading && !failed && nodes.length > 0 && (
          <p className="exec-facts-row">
            {facts.map((f) => (
              <span key={f}>{f}</span>
            ))}
          </p>
        )}
      </header>

      {loading ? (
        <div className="dash-skel">
          <span style={{ width: '60%' }} />
          <span style={{ width: '45%' }} />
          <span style={{ width: '52%' }} />
        </div>
      ) : failed ? (
        <p className="dash-empty" role="alert">
          Execution couldn&apos;t be loaded.{' '}
          <button type="button" className="dash-link" onClick={onRetry}>
            Retry
          </button>
        </p>
      ) : nodes.length === 0 ? (
        <p className="dash-empty">No execution has been recorded for this run.</p>
      ) : (
        <div className={`exec-body${selected ? ' has-selection' : ''}`}>
          <div className="exec-main">
            {boundedNote(body) && <p className="exec-note">{boundedNote(body)}</p>}
            {diagnosticsNote(body) && <p className="exec-note exec-diag">{diagnosticsNote(body)}</p>}
            {fallback ? (
              <ExecutionChronology body={body} selectedId={selectedId} onSelect={onSelect} />
            ) : (
              <ExecutionTree
                tree={tree}
                groups={groups}
                expanded={expanded}
                onToggle={toggle}
                errorBranches={errorBranches}
                selectedId={selectedId}
                onSelect={onSelect}
              />
            )}
          </div>
          <ExecutionInspector node={selected} onClose={() => onSelect(null)} />
        </div>
      )}
    </section>
  )
}

/**
 * The tree. One <ul> per root group; a group heading only when there is
 * more than one group (several traces, or spans plus Work record events).
 * Structure is `tree.children` — the endpoint's parent ids — and nothing
 * else. Sibling order is the endpoint's node order, which is chronology;
 * that is display order, not a relationship.
 */
function ExecutionTree({ tree, groups, expanded, onToggle, errorBranches, selectedId, onSelect }) {
  const showHeadings = groups.length > 1
  return (
    <div className="exec-tree" role="tree" aria-label="Execution structure">
      {groups.map((g) => (
        <div key={g.key} className="exec-group">
          {showHeadings && (
            <h4 className="exec-group-title">
              {g.label}
              <span className="exec-group-count">
                {g.roots.length} {g.roots.length === 1 ? 'root' : 'roots'}
              </span>
            </h4>
          )}
          <ul className="exec-roots">
            {g.roots.map((id) => (
              <ExecutionNode
                key={id}
                id={id}
                depth={0}
                tree={tree}
                expanded={expanded}
                onToggle={onToggle}
                errorBranches={errorBranches}
                selectedId={selectedId}
                onSelect={onSelect}
              />
            ))}
          </ul>
        </div>
      ))}
    </div>
  )
}

function ExecutionNode({ id, depth, tree, expanded, onToggle, errorBranches, selectedId, onSelect }) {
  const node = tree.byId.get(id)
  if (!node) return null
  const kids = tree.children.get(id) || []
  const open = expanded.has(id)
  const title = nodeTitle(node)
  const subtitle = nodeSubtitle(node)
  const meta = nodeMeta(node)
  const error = nodeErrorLine(node)
  const isSelected = selectedId === id
  const parentStatus = node.provenance?.parent_status
  // A root whose recorded parent is not in this read set says so in one
  // quiet word — it is not attached to anything here, and never will be.
  const rootNote =
    depth === 0 && node.parent_id === null && parentStatus && parentStatus !== 'none'
      ? { outside_read_set: 'parent not in this record', self_reference: 'parent recorded as itself', cycle_broken: 'parent link removed (cycle)' }[parentStatus]
      : null

  return (
    <li
      className={`exec-node type-${node.type} status-${node.status || 'unset'}${isSelected ? ' is-selected' : ''}${error ? ' has-error' : ''}`}
      data-node-id={id}
      data-depth={depth}
      role="treeitem"
      aria-expanded={kids.length ? open : undefined}
      aria-selected={isSelected}
    >
      <div className="exec-row">
        {kids.length ? (
          <button
            type="button"
            className="exec-toggle"
            aria-label={open ? `Collapse ${title}` : `Expand ${title}`}
            onClick={() => onToggle(id)}
          >
            {open ? '▾' : '▸'}
          </button>
        ) : (
          <span className="exec-toggle is-leaf" aria-hidden="true" />
        )}
        <button
          type="button"
          className="exec-select"
          aria-pressed={isSelected}
          onClick={() => onSelect(isSelected ? null : id)}
        >
          <span className="exec-line">
            <span className="exec-type">{TYPE_LABELS[node.type] || node.type}</span>
            <span className="exec-title">{title}</span>
            {meta.duration && <span className="exec-dur">{meta.duration}</span>}
          </span>
          {(subtitle || meta.facts.length > 0) && (
            <span className="exec-line exec-line-sub">
              {subtitle && <span className="exec-sub">{subtitle}</span>}
              {meta.facts.length > 0 && <span className="exec-facts">{meta.facts.join(' · ')}</span>}
            </span>
          )}
          {error && <span className="exec-error">{error}</span>}
          {rootNote && <span className="exec-root-note">{rootNote}</span>}
          {!open && kids.length > 0 && (
            <span className="exec-collapsed">
              {kids.length} {kids.length === 1 ? 'child' : 'children'} hidden
              {errorBranches.has(id) ? ' · contains an error' : ''}
            </span>
          )}
        </button>
      </div>
      {open && kids.length > 0 && (
        <ul className="exec-children" role="group">
          {kids.map((c) => (
            <ExecutionNode
              key={c}
              id={c}
              depth={depth + 1}
              tree={tree}
              expanded={expanded}
              onToggle={onToggle}
              errorBranches={errorBranches}
              selectedId={selectedId}
              onSelect={onSelect}
            />
          ))}
        </ul>
      )}
    </li>
  )
}

/**
 * The fallback: the endpoint's chronology, one row per node, when it
 * recorded no parent relationship at all. Time order is the only fact
 * drawn — no indentation, no lines, no arrows.
 */
function ExecutionChronology({ body, selectedId, onSelect }) {
  const rows = chronologyRows(body)
  return (
    <div className="exec-chrono-wrap">
      <p className="exec-note exec-fallback-note">
        No parent relationships were recorded for this execution, so there is no structure to draw.
        Showing observed activity in time order.
      </p>
      <ol className="exec-chrono" aria-label="Observed activity in time order">
        {rows.map((r) => {
          const error = nodeErrorLine(r.node)
          const isSelected = selectedId === r.id
          return (
            <li
              key={r.id}
              className={`exec-chrono-row type-${r.node.type} status-${r.node.status || 'unset'}${isSelected ? ' is-selected' : ''}${error ? ' has-error' : ''}`}
              data-node-id={r.id}
            >
              <button
                type="button"
                className="exec-select"
                aria-pressed={isSelected}
                onClick={() => onSelect(isSelected ? null : r.id)}
              >
                <span className="exec-at">{r.at || '—'}</span>
                <span className="exec-chrono-main">
                  <span className="exec-line">
                    <span className="exec-type">{TYPE_LABELS[r.node.type] || r.node.type}</span>
                    <span className="exec-title">{r.title}</span>
                  </span>
                  {r.subtitle && <span className="exec-sub">{r.subtitle}</span>}
                  {error && <span className="exec-error">{error}</span>}
                </span>
              </button>
            </li>
          )
        })}
      </ol>
    </div>
  )
}

/**
 * What Trovis recorded about one node. Type-aware: a section appears only
 * when the node carries something for it; a row appears only when the
 * value exists. The ids live behind "Technical details".
 */
function ExecutionInspector({ node, onClose }) {
  if (!node) {
    return (
      <aside className="exec-inspector is-empty" aria-label="Execution node">
        <p className="dash-empty">Select a node to see what Trovis recorded about it.</p>
      </aside>
    )
  }
  const sections = inspectorSections(node)
  const tech = technicalDetails(node)
  const subtitle = nodeSubtitle(node)
  return (
    <aside className="exec-inspector" aria-label="Execution node">
      <header className="exec-insp-head">
        <span className="exec-insp-type">{TYPE_LABELS[node.type] || node.type}</span>
        <h4 className="exec-insp-title">{nodeTitle(node)}</h4>
        {subtitle && <p className="exec-insp-sub">{subtitle}</p>}
        <button type="button" className="exec-insp-close" aria-label="Close inspector" onClick={onClose}>
          ×
        </button>
      </header>
      {sections.map((s) => (
        <section key={s.title} className="exec-insp-section">
          <h5 className="dash-caps">{s.title}</h5>
          <dl className="jobd-ev-dl">
            {s.rows.map((r) => (
              <div key={r.label} className="exec-insp-row">
                <dt>{r.label}</dt>
                <dd className={r.mono ? 'mono' : ''}>{r.value}</dd>
              </div>
            ))}
          </dl>
        </section>
      ))}
      {tech.length > 0 && (
        <details className="jobd-ev-details exec-insp-tech">
          <summary>Technical details</summary>
          <dl className="jobd-ev-dl">
            {tech.map((r) => (
              <div key={r.label} className="exec-insp-row">
                <dt>{r.label}</dt>
                <dd className={r.mono ? 'mono' : ''}>{r.value}</dd>
              </div>
            ))}
          </dl>
        </details>
      )}
    </aside>
  )
}
