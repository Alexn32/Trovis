import { useEffect, useMemo, useRef, useState } from 'react'
import { api } from './api.js'
import { ArrowLeftIcon, ChevronDownIcon } from './Icons.jsx'
import { collapseSteps, expandSteps } from './collapseSteps.js'
import { assignWorkerColors, agentKey, HUMAN_COLOR } from './workerColors.js'
import WorkflowCreateModal from './WorkflowCreateModal.jsx'

// Workflow canvas at "handoff altitude": consecutive same-agent steps collapse
// into one block, workers carry identity colors, loops sweep below the flow as
// dashed under-curves, and live flow is animated. The DB keeps every raw step;
// the collapse happens here at render time.

const TYPE_LABEL = {
  trigger: 'Trigger',
  agent: 'Agent',
  human: 'Human',
  decision: 'Decision',
  output: 'Output',
}
const NEUTRAL_TRIGGER = '#8c8378'
const DECISION_COLOR = '#d4792a'
const OUTPUT_COLOR = '#2a9d6e'
const LOOP_COLORS = ['#d4792a', '#a78bda'] // first loop amber, rest light purple

// Layout geometry.
const NODE_H = 86 // nominal height used to vertically center nodes on the row
const MAIN_Y = 190 // center of the main flow row
const ROW_TOP = MAIN_Y - NODE_H / 2
const GAP = 50
const START_X = 40
const LOOP_BASE = MAIN_Y + 150 // first loop's depth below the row
const LOOP_STEP = 44 // each additional stacked loop drops this much

const LS_VIEW = 'trovis_wf_view'

function fmtDur(ms) {
  if (!ms || ms <= 0) return '—'
  const s = ms / 1000
  if (s < 1) return `${Math.round(ms)}ms`
  if (s < 60) return `${s.toFixed(1)}s`
  return `${Math.round(s / 60)}m`
}

// Leading integer from an edge label like "14 pass" → 14; null when none.
function leadingInt(label) {
  if (!label) return null
  const m = String(label).match(/-?\d+/)
  return m ? parseInt(m[0], 10) : null
}

function nodeWidth(node, scale = 1) {
  let w
  if (node.kind === 'block') {
    const n = node.steps.length
    w = n > 1 ? Math.min(210, 175 + n * 6) : 175
  } else if (node.step_type === 'human') {
    w = 195
  } else {
    w = 150 // trigger / output / decision
  }
  return Math.round(w * scale)
}

// The identity color for a node, given the workflow's color map.
function nodeColor(node, colors) {
  if (node.step_type === 'agent') return colors[node.workerKey] || '#5A7B7B'
  if (node.step_type === 'human') return HUMAN_COLOR
  if (node.step_type === 'decision') return DECISION_COLOR
  if (node.step_type === 'output') return OUTPUT_COLOR
  return NEUTRAL_TRIGGER
}

function draggedKey(workflowId) {
  return `trovis_wf_${workflowId}_dragged`
}
function readDragged(workflowId) {
  try {
    const raw = localStorage.getItem(draggedKey(workflowId))
    return new Set(raw ? JSON.parse(raw) : [])
  } catch {
    return new Set()
  }
}

export default function WorkflowCanvas({ workflowId, onBack }) {
  const [wf, setWf] = useState(null)
  const [stats, setStats] = useState(null)
  const [error, setError] = useState(null)
  const [positions, setPositions] = useState({}) // node id -> {x,y}
  const [expanded, setExpanded] = useState(() => new Set()) // node ids
  const [view, setView] = useState(() => {
    try {
      return localStorage.getItem(LS_VIEW) === 'all' ? 'all' : 'handoff'
    } catch {
      return 'handoff'
    }
  })
  const [hoverEdge, setHoverEdge] = useState(null) // edge index for insert "+"
  const [redescribe, setRedescribe] = useState(false)
  const dragRef = useRef(null)
  const draggedSet = useRef(new Set())

  useEffect(() => {
    let alive = true
    draggedSet.current = readDragged(workflowId)
    api
      .getWorkflow(workflowId)
      .then((data) => alive && setWf(data))
      .catch((e) => alive && setError(e.message || 'Could not load workflow'))
    api
      .getWorkflowStats(workflowId)
      .then((s) => alive && setStats(s))
      .catch(() => {})
    return () => {
      alive = false
    }
  }, [workflowId])

  const steps = wf?.steps || []
  let rawEdges = wf?.edges || []
  // Back-compat: synthesize sequential edges when none exist.
  if (rawEdges.length === 0 && steps.length > 1) {
    const ordered = [...steps].sort((a, b) => a.step_order - b.step_order)
    rawEdges = ordered.slice(0, -1).map((s, i) => ({
      from_step_id: s.id,
      to_step_id: ordered[i + 1].id,
      is_branch: false,
      label: null,
    }))
  }

  const colors = useMemo(
    () => assignWorkerColors(wf?.participants || []),
    [wf],
  )

  const { nodes, edges } = useMemo(
    () =>
      view === 'all' ? expandSteps(steps, rawEdges) : collapseSteps(steps, rawEdges),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [wf, view],
  )

  const scale = view === 'all' ? 0.9 : 1
  const nodeH = NODE_H * scale

  // Auto-layout: left-to-right, centered on the main row. Drag positions (kept
  // in localStorage, keyed by the first step id of each node) override.
  const layout = useMemo(() => {
    const out = {}
    let x = START_X
    for (const n of nodes) {
      const w = nodeWidth(n, scale)
      const saved = positions[n.id]
      const useSaved = draggedSet.current.has(n.id) && saved
      out[n.id] = useSaved ? { ...saved, w } : { x, y: ROW_TOP, w }
      x += w + GAP * scale
    }
    return out
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodes, positions, scale])

  // Loop edges (target comes before source in flow order), stacked shortest-span
  // closest to the row so they never cross.
  const loops = useMemo(() => {
    const ls = edges
      .map((e, i) => ({ e, i }))
      .filter(({ e }) => e.to_order < e.from_order)
      .map(({ e, i }) => ({ ...e, idx: i, span: Math.abs(e.from_order - e.to_order) }))
      .sort((a, b) => a.span - b.span || a.from_order - b.from_order)
    return ls.map((l, rank) => ({ ...l, rank, loopY: LOOP_BASE + rank * LOOP_STEP }))
  }, [edges])

  const forwardEdges = edges.filter((e) => e.to_order > e.from_order)

  // Canvas content size.
  const dims = useMemo(() => {
    let maxX = 600
    for (const n of nodes) {
      const p = layout[n.id]
      if (p) maxX = Math.max(maxX, p.x + p.w + 60)
    }
    let maxY = 470
    for (const id of expanded) {
      const n = nodes.find((x) => x.id === id)
      const p = layout[id]
      if (n && p && n.kind === 'block') {
        maxY = Math.max(maxY, p.y + nodeH + 16 + n.steps.length * 36 + 40)
      }
    }
    if (loops.length) maxY = Math.max(maxY, LOOP_BASE + loops.length * LOOP_STEP + 70)
    return { w: maxX, h: maxY }
  }, [nodes, layout, expanded, loops, nodeH])

  function onNodeDown(e, id) {
    e.stopPropagation()
    const p = layout[id]
    if (!p) return
    dragRef.current = { id, sx: e.clientX, sy: e.clientY, ox: p.x, oy: p.y, moved: false }
    try {
      e.currentTarget.setPointerCapture(e.pointerId)
    } catch {
      /* ignore */
    }
  }
  function onNodeMove(e) {
    const d = dragRef.current
    if (!d) return
    const dx = e.clientX - d.sx
    const dy = e.clientY - d.sy
    if (Math.hypot(dx, dy) > 3) d.moved = true
    if (d.moved) {
      setPositions((prev) => ({
        ...prev,
        [d.id]: { x: Math.max(0, d.ox + dx), y: Math.max(0, d.oy + dy) },
      }))
    }
  }
  function onNodeUp(e, id, node) {
    const d = dragRef.current
    if (!d) return
    dragRef.current = null
    try {
      e.currentTarget.releasePointerCapture(e.pointerId)
    } catch {
      /* ignore */
    }
    if (!d.moved) {
      // Click: expand/collapse blocks (and singles with internal detail).
      if (node.kind === 'block' && node.steps.length > 1) {
        setExpanded((prev) => {
          const next = new Set(prev)
          next.has(id) ? next.delete(id) : next.add(id)
          return next
        })
      }
      return
    }
    const p = layout[id]
    if (p) {
      draggedSet.current.add(id)
      try {
        localStorage.setItem(
          draggedKey(workflowId),
          JSON.stringify([...draggedSet.current]),
        )
      } catch {
        /* ignore */
      }
      api
        .updateStepPosition(workflowId, id, { pos_x: Math.round(p.x), pos_y: Math.round(p.y) })
        .catch(() => {})
    }
  }

  function setViewPersist(v) {
    setView(v)
    setExpanded(new Set())
    try {
      localStorage.setItem(LS_VIEW, v)
    } catch {
      /* ignore */
    }
  }

  if (error) {
    return (
      <div className="wf2 wf2-canvas-view">
        <button type="button" className="wf2-back" onClick={onBack}>
          <ArrowLeftIcon size={14} /> Workflows
        </button>
        <div className="wf2-info-box">{error}</div>
      </div>
    )
  }
  if (!wf) {
    return (
      <div className="wf2 wf2-canvas-view">
        <div className="wf2-skel">
          <span />
        </div>
      </div>
    )
  }

  const perStep = stats?.per_step || {}
  const hasLoops = loops.length > 0
  const loopDerivable = stats?.loop_rate != null

  // Per-node display stats. Agent block: max runs across internal steps, total
  // duration (time spent in the block).
  function nodeStats(node) {
    const ids = node.kind === 'block' ? node.steps.map((s) => s.id) : [node.step.id]
    let runs = 0
    let dur = 0
    let any = false
    for (const id of ids) {
      const ps = perStep[String(id)]
      if (ps) {
        any = true
        runs = Math.max(runs, ps.runs || 0)
        dur += ps.avg_duration_ms || 0
      }
    }
    return any ? { runs, dur } : null
  }

  return (
    <div className="wf2 wf2-canvas-view">
      <div className="wf2-proc-head">
        <div className="wf2-proc-left">
          <button type="button" className="wf2-back" onClick={onBack} aria-label="Back">
            <ArrowLeftIcon size={15} />
          </button>
          <span className="wf2-proc-name">{wf.name}</span>
          <div className="wf2-roster">
            {(wf.participants || []).map((p, i) =>
              p.type === 'human' ? (
                <span key={`h${i}`} className="wf2-chip">
                  <span
                    className="wf2-chip-mark is-circle"
                    style={{ background: HUMAN_COLOR }}
                  />
                  👤 {p.role_name || 'Human'}
                </span>
              ) : (
                <span key={`a${i}`} className="wf2-chip">
                  <span
                    className="wf2-chip-mark is-square"
                    style={{ background: colors[agentKey(p.agent_service_name, p.agent_id)] }}
                  />
                  {p.agent_service_name}
                  {p.agent_id && p.agent_id !== 'main' ? ` · ${p.agent_id}` : ''}
                </span>
              ),
            )}
          </div>
        </div>

        <div className="wf2-proc-stats">
          <HeadStat label="Runs 24h" value={stats ? stats.total_runs : '—'} />
          <HeadStat
            label="Success"
            value={stats?.success_rate == null ? '—' : `${Math.round(stats.success_rate)}%`}
            tone={
              stats?.success_rate == null
                ? undefined
                : stats.success_rate >= 90
                  ? 'good'
                  : 'warn'
            }
          />
          {loopDerivable && (
            <HeadStat
              label="First-pass rate"
              value={`${Math.round(100 - stats.loop_rate)}%`}
              tone={100 - stats.loop_rate < 70 ? 'warn' : undefined}
            />
          )}
          {stats?.avg_rounds != null && (
            <HeadStat label="Avg rounds" value={stats.avg_rounds.toFixed(1)} />
          )}
          <HeadStat
            label="Human wait"
            value={stats?.avg_human_wait_ms == null ? '—' : fmtDur(stats.avg_human_wait_ms)}
            tone={stats?.avg_human_wait_ms == null ? undefined : 'human'}
          />
        </div>

        <div className="wf2-view-toggle" role="tablist">
          <button
            type="button"
            className={view === 'handoff' ? 'is-on' : ''}
            onClick={() => setViewPersist('handoff')}
          >
            Handoffs
          </button>
          <button
            type="button"
            className={view === 'all' ? 'is-on' : ''}
            onClick={() => setViewPersist('all')}
          >
            All steps
          </button>
        </div>
      </div>

      <div className="wf2-canvas">
        <div className="wf2-canvas-inner" style={{ width: dims.w, height: dims.h }}>
          <svg className="wf2-edges" width={dims.w} height={dims.h} aria-hidden="true">
            <defs>
              <marker
                id="wf2-arrow"
                viewBox="0 0 10 10"
                refX="9"
                refY="5"
                markerWidth="7"
                markerHeight="7"
                orient="auto-start-reverse"
              >
                <path d="M0,0 L10,5 L0,10 z" fill="#b8b0a4" />
              </marker>
            </defs>

            {/* Forward edges */}
            {forwardEdges.map((e, i) => {
              const fp = layout[e.from_id]
              const tp = layout[e.to_id]
              if (!fp || !tp) return null
              const fx = fp.x + fp.w
              const fy = fp.y + nodeH / 2
              const tx = tp.x
              const ty = tp.y + nodeH / 2
              const mx = (fx + tx) / 2
              const my = (fy + ty) / 2
              const d =
                Math.abs(fy - ty) < 1
                  ? `M${fx},${fy} L${tx},${ty}`
                  : `M${fx},${fy} C${mx},${fy} ${mx},${ty} ${tx},${ty}`
              return (
                <g key={`f${i}`}>
                  <path d={d} className="wf2-fedge" markerEnd="url(#wf2-arrow)" />
                  <circle className="wf2-flow-dot" r="3">
                    <animateMotion
                      dur="1.5s"
                      begin={`${i * 0.5}s`}
                      repeatCount="indefinite"
                      path={d}
                    />
                  </circle>
                  {e.label && (
                    <text x={mx} y={my - 7} textAnchor="middle" className="wf2-fedge-label">
                      {e.label}
                    </text>
                  )}
                </g>
              )
            })}

            {/* Loop edges (under the flow) */}
            {loops.map((l) => {
              const sp = layout[l.from_id]
              const tp = layout[l.to_id]
              if (!sp || !tp) return null
              const srcX = sp.x + sp.w / 2
              const srcBottomY = sp.y + nodeH
              const tgtX = tp.x + tp.w / 2
              const tgtBottomY = tp.y + nodeH
              const color = LOOP_COLORS[Math.min(l.rank, LOOP_COLORS.length - 1)]
              const d = `M ${srcX} ${srcBottomY} C ${srcX} ${l.loopY}, ${tgtX + 20} ${l.loopY}, ${tgtX + 10} ${tgtBottomY}`
              const pillX = (srcX + tgtX) / 2
              return (
                <g key={`l${l.idx}`}>
                  <path
                    d={d}
                    fill="none"
                    stroke={color}
                    strokeWidth="1.8"
                    strokeDasharray="7,5"
                    opacity="0.8"
                  />
                  <circle r="3.5" fill={color}>
                    <animateMotion dur="4s" repeatCount="indefinite" path={d} />
                  </circle>
                  <foreignObject
                    x={pillX - 80}
                    y={l.loopY - 12}
                    width="160"
                    height="26"
                    style={{ overflow: 'visible' }}
                  >
                    <div className="wf2-loop-pill-wrap">
                      <span
                        className="wf2-loop-pill"
                        style={{
                          color,
                          borderColor: `${color}40`,
                        }}
                      >
                        ↻ {l.label || 'sent back'}
                      </span>
                    </div>
                  </foreignObject>
                </g>
              )
            })}
          </svg>

          {/* Insert affordances (visual hint only, V1) */}
          {forwardEdges.map((e, i) => {
            const fp = layout[e.from_id]
            const tp = layout[e.to_id]
            if (!fp || !tp) return null
            const mx = (fp.x + fp.w + tp.x) / 2
            const my = (fp.y + tp.y) / 2 + nodeH / 2
            return (
              <button
                key={`ins${i}`}
                type="button"
                className={`wf2-insert ${hoverEdge === i ? 'is-shown' : ''}`}
                style={{ left: mx - 11, top: my - 11 }}
                onMouseEnter={() => setHoverEdge(i)}
                onMouseLeave={() => setHoverEdge((h) => (h === i ? null : h))}
                onClick={() => setRedescribe(true)}
                aria-label="Insert step"
              >
                +
              </button>
            )
          })}

          {/* Nodes */}
          {nodes.map((node) => {
            const p = layout[node.id]
            if (!p) return null
            const color = nodeColor(node, colors)
            const isExpanded = expanded.has(node.id)
            const ns = nodeStats(node)
            const stepCount = node.kind === 'block' ? node.steps.length : 1
            const canExpand = node.kind === 'block' && stepCount > 1
            const out = edges.filter((x) => x.from_id === node.id)
            const fwd = out.find((x) => x.to_order > x.from_order)
            const back = out.find((x) => x.to_order < x.from_order)
            const name =
              node.kind === 'block'
                ? node.step_type === 'agent'
                  ? node.steps[0].label
                  : node.steps[0].label
                : node.step.label
            const label =
              node.step_type === 'human' ? `👤 ${stripPerson(name)}` : name
            return (
              <div
                key={node.id}
                className={`wf2-node2 type-${node.step_type} ${isExpanded ? 'is-expanded' : ''} ${canExpand ? 'can-expand' : ''}`}
                style={{
                  left: p.x,
                  top: p.y,
                  width: p.w,
                  minHeight: nodeH,
                  borderTopColor: color,
                  zIndex: isExpanded ? 4 : 1,
                  fontSize: view === 'all' ? '90%' : undefined,
                }}
                onPointerDown={(e) => onNodeDown(e, node.id)}
                onPointerMove={onNodeMove}
                onPointerUp={(e) => onNodeUp(e, node.id, node)}
              >
                <div className="wf2-node2-badges">
                  <span className="wf2-type-pill" style={{ background: color }}>
                    {TYPE_LABEL[node.step_type] || 'Step'}
                  </span>
                  {stepCount > 1 && (
                    <span className="wf2-count-pill">{stepCount} steps</span>
                  )}
                  {canExpand && (
                    <span className={`wf2-chev ${isExpanded ? 'is-open' : ''}`}>
                      <ChevronDownIcon size={12} />
                    </span>
                  )}
                </div>

                <div className="wf2-node2-name">{label}</div>

                <div className="wf2-node2-stats">
                  {ns ? (
                    <>
                      <span>{ns.runs} runs</span>
                      <span>{fmtDur(ns.dur)}</span>
                    </>
                  ) : node.step_type === 'agent' || node.step_type === 'output' ? (
                    <span className="dim">— runs</span>
                  ) : null}

                  {node.step_type === 'decision' && (
                    <DecisionStats fwd={fwd} back={back} />
                  )}
                  {node.step_type === 'human' && (
                    <span className="wf2-wait">
                      wait{' '}
                      {stats?.avg_human_wait_ms == null ? '—' : fmtDur(stats.avg_human_wait_ms)}
                    </span>
                  )}
                </div>

                {node.step_type === 'human' && (fwd || back) && (
                  <div className="wf2-human-pills">
                    {leadingInt(fwd?.label) != null && (
                      <span className="wf2-hpill good">✓ {leadingInt(fwd.label)}</span>
                    )}
                    {leadingInt(back?.label) != null && (
                      <span className="wf2-hpill warn">↻ {leadingInt(back.label)}</span>
                    )}
                  </div>
                )}

                {isExpanded && node.kind === 'block' && (
                  <div className="wf2-expand">
                    {node.steps.map((s, idx) => {
                      const ps = perStep[String(s.id)]
                      return (
                        <div key={s.id} className="wf2-expand-step">
                          <span className="wf2-expand-num">{idx + 1}</span>
                          <div className="wf2-expand-body">
                            <div className="wf2-expand-name">{s.label}</div>
                            <div className="wf2-expand-meta">
                              {s.operation || s.step_type}
                              {ps ? ` · ${fmtDur(ps.avg_duration_ms)}` : ''}
                            </div>
                          </div>
                        </div>
                      )
                    })}
                  </div>
                )}
              </div>
            )
          })}
        </div>
      </div>

      {/* Insight strip */}
      <div className="wf2-insights">
        {hasLoops && loopDerivable && (
          <div className="wf2-insight loop">
            <span className="wf2-insight-label">Loop insight</span>
            <p>
              {stats.loop_rate}% of runs loop back at least once
              {stats.avg_rounds != null
                ? `, averaging ${stats.avg_rounds.toFixed(1)} rounds through the looped section`
                : ''}
              . The dashed curve below the flow marks where work returns for another pass.
            </p>
          </div>
        )}
        <div className="wf2-insight map">
          <span className="wf2-insight-label">Reading the map</span>
          <p>
            Node top-bars are colored by worker. Dashed curves below are loops. The solid
            line is the happy path.
          </p>
        </div>
      </div>

      {redescribe && (
        <WorkflowCreateModal
          title="Re-describe workflow"
          initialMethod="describe"
          initialName={wf.name}
          initialDescription={wf.source_description || ''}
          onClose={() => setRedescribe(false)}
          onCreated={(newWf) => {
            setRedescribe(false)
            if (newWf?.id) window.location.reload()
          }}
        />
      )}
    </div>
  )
}

function stripPerson(s) {
  return String(s || '').replace(/^👤\s*/, '')
}

function DecisionStats({ fwd, back }) {
  const pass = leadingInt(fwd?.label)
  const fail = leadingInt(back?.label)
  if (pass == null && fail == null) return null
  return (
    <span className="wf2-decision-stats">
      {pass != null && <span className="good">{pass} ✓</span>}
      {fail != null && <span className="warn">{fail} ↻</span>}
    </span>
  )
}

function HeadStat({ label, value, tone }) {
  return (
    <div className="wf2-head-stat">
      <span className="wf2-head-stat-label">{label}</span>
      <span className={`wf2-head-stat-value ${tone ? `tone-${tone}` : ''}`}>{value}</span>
    </div>
  )
}
