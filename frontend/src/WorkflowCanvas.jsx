import { useEffect, useMemo, useRef, useState } from 'react'
import { api } from './api.js'
import { ArrowLeftIcon, ChevronDownIcon, PlusIcon, TrashIcon } from './Icons.jsx'
import { collapseSteps, expandSteps } from './collapseSteps.js'
import { assignWorkerColors, agentKey, HUMAN_COLOR } from './workerColors.js'
import WorkflowCreateModal from './WorkflowCreateModal.jsx'
import WorkflowStepEditor from './WorkflowStepEditor.jsx'

// Workflow canvas at "handoff altitude": consecutive same-agent steps collapse
// into one block, workers carry identity colors, loops sweep below the flow as
// dashed under-curves, and live flow is animated. The DB keeps every raw step;
// the collapse happens here at render time. An explicit Edit mode forces the
// raw "all steps" view (1:1 node↔step) and adds node/edge/roster editing.

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
  // Editing state.
  const [editing, setEditing] = useState(false)
  const [editor, setEditor] = useState(null) // {mode, step?|splice?|afterStepId?}
  const [edgePop, setEdgePop] = useState(null) // {id, label, is_branch, x, y}
  const [confirmDel, setConfirmDel] = useState(null) // node id pending delete
  const [connect, setConnect] = useState(null) // {from, ox, oy, x, y}
  const [rosterAdd, setRosterAdd] = useState(false)
  const [toast, setToast] = useState(null)
  // Conversational AI editing.
  const [aiText, setAiText] = useState('')
  const [aiBusy, setAiBusy] = useState(false)
  const [aiError, setAiError] = useState(null)
  const [aiSummary, setAiSummary] = useState(null)
  const dragRef = useRef(null)
  const draggedSet = useRef(new Set())
  const innerRef = useRef(null)
  const prevView = useRef('handoff')

  function refresh() {
    return api.getWorkflow(workflowId).then((data) => setWf(data))
  }

  useEffect(() => {
    let alive = true
    draggedSet.current = readDragged(workflowId)
    api
      .getWorkflow(workflowId)
      .then((data) => {
        if (!alive) return
        setWf(data)
        // Seed positions from server-saved coords so dragged nodes render
        // without a fresh drag and survive refresh().
        const seed = {}
        for (const s of data.steps || []) {
          if (s.pos_x || s.pos_y) seed[s.id] = { x: s.pos_x, y: s.pos_y }
        }
        setPositions(seed)
      })
      .catch((e) => alive && setError(e.message || 'Could not load workflow'))
    api
      .getWorkflowStats(workflowId)
      .then((s) => alive && setStats(s))
      .catch(() => {})
    return () => {
      alive = false
    }
  }, [workflowId])

  // Auto-dismiss toasts.
  useEffect(() => {
    if (!toast) return
    const t = setTimeout(() => setToast(null), 4000)
    return () => clearTimeout(t)
  }, [toast])

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

  const colors = useMemo(() => assignWorkerColors(wf?.participants || []), [wf])

  const { nodes, edges } = useMemo(
    () =>
      view === 'all' ? expandSteps(steps, rawEdges) : collapseSteps(steps, rawEdges),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [wf, view],
  )

  // Edit mode forces the raw "all" representation at full size for comfortable
  // editing; the read-only "all" view stays at 90%.
  const scale = view === 'all' && !editing ? 0.9 : 1
  const nodeH = NODE_H * scale

  const layout = useMemo(() => {
    const out = {}
    let x = START_X
    for (const n of nodes) {
      const w = nodeWidth(n, scale)
      const saved = positions[n.id]
      const useSaved = (draggedSet.current.has(n.id) || saved) && saved
      out[n.id] = useSaved ? { ...saved, w } : { x, y: ROW_TOP, w }
      x += w + GAP * scale
    }
    return out
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodes, positions, scale])

  const loops = useMemo(() => {
    const ls = edges
      .map((e, i) => ({ e, i }))
      .filter(({ e }) => e.to_order < e.from_order)
      .map(({ e, i }) => ({ ...e, idx: i, span: Math.abs(e.from_order - e.to_order) }))
      .sort((a, b) => a.span - b.span || a.from_order - b.from_order)
    return ls.map((l, rank) => ({ ...l, rank, loopY: LOOP_BASE + rank * LOOP_STEP }))
  }, [edges])

  const forwardEdges = edges.filter((e) => e.to_order > e.from_order)

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
    if (editing && e.target.closest('.wf2-port, .wf2-node-del, .wf2-confirm')) return
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
      if (editing) {
        // Click opens the step editor for this raw step.
        setEditor({ mode: 'edit', step: node.step })
        return
      }
      // Read mode: expand/collapse multi-step blocks.
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
        localStorage.setItem(draggedKey(workflowId), JSON.stringify([...draggedSet.current]))
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

  function toggleEdit() {
    if (editing) {
      setEditing(false)
      setView(prevView.current)
      setEditor(null)
      setEdgePop(null)
      setConfirmDel(null)
      setRosterAdd(false)
      setConnect(null)
      setAiError(null)
      setAiSummary(null)
    } else {
      prevView.current = view
      setEditing(true)
      setView('all')
      setExpanded(new Set())
    }
  }

  async function runAiEdit() {
    const instruction = aiText.trim()
    if (!instruction || aiBusy) return
    setAiBusy(true)
    setAiError(null)
    setAiSummary(null)
    try {
      const res = await api.aiEditWorkflow(workflowId, instruction)
      if (res?.workflow) setWf(res.workflow)
      setAiText('')
      setAiSummary(
        res?.applied
          ? res.summary || `Applied ${res.applied} change${res.applied === 1 ? '' : 's'}.`
          : 'No changes were needed for that request.',
      )
    } catch (e) {
      const msg = String(e?.message || '')
      setAiError(
        msg.includes('503')
          ? 'AI is unavailable — the backend needs an ANTHROPIC_API_KEY.'
          : msg || 'Could not apply that edit.',
      )
    } finally {
      setAiBusy(false)
    }
  }

  // ---- editing actions ----
  function localXY(e) {
    const r = innerRef.current?.getBoundingClientRect()
    if (!r) return { x: 0, y: 0 }
    return { x: e.clientX - r.left, y: e.clientY - r.top }
  }

  function onPortDown(e, node) {
    e.stopPropagation()
    const p = layout[node.id]
    if (!p) return
    setConnect({ from: node.id, ox: p.x + p.w, oy: p.y + nodeH / 2, x: p.x + p.w, y: p.y + nodeH / 2 })
    try {
      e.currentTarget.setPointerCapture(e.pointerId)
    } catch {
      /* ignore */
    }
  }
  function onPortMove(e) {
    if (!connect) return
    const { x, y } = localXY(e)
    setConnect((c) => (c ? { ...c, x, y } : c))
  }
  function onPortUp(e) {
    if (!connect) return
    const { x, y } = localXY(e)
    let target = null
    for (const n of nodes) {
      const p = layout[n.id]
      if (p && x >= p.x && x <= p.x + p.w && y >= p.y && y <= p.y + nodeH) {
        target = n.id
        break
      }
    }
    const from = connect.from
    setConnect(null)
    if (target == null || target === from) return
    api
      .addWorkflowEdge(workflowId, { from_step_id: from, to_step_id: target })
      .then(refresh)
      .catch((err) =>
        setToast(
          String(err?.message || '').includes('409')
            ? 'Those steps are already connected.'
            : 'Could not connect those steps.',
        ),
      )
  }

  function deleteStep(node) {
    const id = node.id
    const preds = [...new Set(rawEdges.filter((e) => e.to_step_id === id).map((e) => e.from_step_id))]
    const succs = [...new Set(rawEdges.filter((e) => e.from_step_id === id).map((e) => e.to_step_id))]
    setConfirmDel(null)
    api
      .deleteWorkflowStep(workflowId, id)
      .then(async () => {
        if (preds.length === 1 && succs.length === 1 && preds[0] !== succs[0]) {
          try {
            await api.addWorkflowEdge(workflowId, { from_step_id: preds[0], to_step_id: succs[0] })
          } catch {
            /* duplicate/relink failures are non-fatal */
          }
        } else if (preds.length + succs.length > 2) {
          setToast('Step deleted — reconnect the remaining steps as needed.')
        }
        return refresh()
      })
      .catch(() => setToast('Could not delete the step.'))
  }

  function saveEdgePop() {
    const ep = edgePop
    setEdgePop(null)
    if (ep?.id == null) return
    api
      .updateWorkflowEdge(workflowId, ep.id, { label: ep.label || null, is_branch: !!ep.is_branch })
      .then(refresh)
      .catch(() => setToast('Could not update the edge.'))
  }
  function deleteEdge(id) {
    setEdgePop(null)
    if (id == null) return
    api
      .deleteWorkflowEdge(workflowId, id)
      .then(refresh)
      .catch(() => setToast('Could not delete the edge.'))
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
  const lastNodeId = nodes.length ? nodes[nodes.length - 1].id : null

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
    <div className={`wf2 wf2-canvas-view ${editing ? 'is-editing' : ''}`}>
      <div className="wf2-proc-head">
        <div className="wf2-proc-left">
          <button type="button" className="wf2-back" onClick={onBack} aria-label="Back">
            <ArrowLeftIcon size={15} />
          </button>
          <span className="wf2-proc-name">{wf.name}</span>
          <div className="wf2-roster">
            {(wf.participants || []).map((p) =>
              p.type === 'human' ? (
                <span key={`h${p.id}`} className="wf2-chip">
                  <span className="wf2-chip-mark is-circle" style={{ background: HUMAN_COLOR }} />
                  👤 {p.role_name || 'Human'}
                  {editing && (
                    <button
                      type="button"
                      className="wf2-chip-del"
                      onClick={() => removeParticipant(p)}
                      aria-label="Remove"
                    >
                      ×
                    </button>
                  )}
                </span>
              ) : (
                <span key={`a${p.id}`} className="wf2-chip">
                  <span
                    className="wf2-chip-mark is-square"
                    style={{ background: colors[agentKey(p.agent_service_name, p.agent_id)] }}
                  />
                  {p.agent_service_name}
                  {p.agent_id && p.agent_id !== 'main' ? ` · ${p.agent_id}` : ''}
                  {editing && (
                    <button
                      type="button"
                      className="wf2-chip-del"
                      onClick={() => removeParticipant(p)}
                      aria-label="Remove"
                    >
                      ×
                    </button>
                  )}
                </span>
              ),
            )}
            {editing && (
              <span className="wf2-roster-add-wrap">
                <button type="button" className="wf2-roster-add" onClick={() => setRosterAdd((v) => !v)}>
                  + Worker
                </button>
                {rosterAdd && (
                  <RosterAdd
                    workflowId={workflowId}
                    onClose={() => setRosterAdd(false)}
                    onDone={() => {
                      setRosterAdd(false)
                      refresh()
                    }}
                  />
                )}
              </span>
            )}
          </div>
        </div>

        <div className="wf2-proc-stats">
          <HeadStat label="Runs 24h" value={stats ? stats.total_runs : '—'} />
          <HeadStat
            label="Success"
            value={stats?.success_rate == null ? '—' : `${Math.round(stats.success_rate)}%`}
            tone={
              stats?.success_rate == null ? undefined : stats.success_rate >= 90 ? 'good' : 'warn'
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

        <div className="wf2-head-controls">
          {editing && (
            <button
              type="button"
              className="wf2-add-step"
              onClick={() => setEditor({ mode: 'add', afterStepId: lastNodeId })}
            >
              <PlusIcon size={13} /> Add step
            </button>
          )}
          <div className={`wf2-view-toggle ${editing ? 'is-disabled' : ''}`} role="tablist">
            <button
              type="button"
              className={view === 'handoff' ? 'is-on' : ''}
              onClick={() => !editing && setViewPersist('handoff')}
              disabled={editing}
            >
              Handoffs
            </button>
            <button
              type="button"
              className={view === 'all' ? 'is-on' : ''}
              onClick={() => !editing && setViewPersist('all')}
              disabled={editing}
            >
              All steps
            </button>
          </div>
          <button
            type="button"
            className={`wf2-edit-btn ${editing ? 'is-on' : ''}`}
            onClick={toggleEdit}
          >
            {editing ? 'Done' : 'Edit'}
          </button>
        </div>
      </div>

      <div className="wf2-canvas" onPointerMove={onPortMove} onPointerUp={onPortUp}>
        <div className="wf2-canvas-inner" ref={innerRef} style={{ width: dims.w, height: dims.h }}>
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
                  {!editing && (
                    <circle className="wf2-flow-dot" r="3">
                      <animateMotion dur="1.5s" begin={`${i * 0.5}s`} repeatCount="indefinite" path={d} />
                    </circle>
                  )}
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
                  <path d={d} fill="none" stroke={color} strokeWidth="1.8" strokeDasharray="7,5" opacity="0.8" />
                  {!editing && (
                    <circle r="3.5" fill={color}>
                      <animateMotion dur="4s" repeatCount="indefinite" path={d} />
                    </circle>
                  )}
                  <foreignObject x={pillX - 80} y={l.loopY - 12} width="160" height="26" style={{ overflow: 'visible' }}>
                    <div className="wf2-loop-pill-wrap">
                      <span className="wf2-loop-pill" style={{ color, borderColor: `${color}40` }}>
                        ↻ {l.label || 'sent back'}
                      </span>
                    </div>
                  </foreignObject>
                </g>
              )
            })}

            {/* Connect rubber-band */}
            {connect && (
              <line
                x1={connect.ox}
                y1={connect.oy}
                x2={connect.x}
                y2={connect.y}
                className="wf2-rubber"
              />
            )}
          </svg>

          {/* Edge edit / insert affordances */}
          {forwardEdges.map((e, i) => {
            const fp = layout[e.from_id]
            const tp = layout[e.to_id]
            if (!fp || !tp) return null
            const mx = (fp.x + fp.w + tp.x) / 2
            const my = (fp.y + tp.y) / 2 + nodeH / 2
            return (
              <div key={`tools${i}`} className="wf2-edge-tools-wrap" style={{ left: mx, top: my }}>
                <button
                  type="button"
                  className={`wf2-insert ${hoverEdge === i ? 'is-shown' : ''} ${editing ? 'is-edit' : ''}`}
                  onMouseEnter={() => setHoverEdge(i)}
                  onMouseLeave={() => setHoverEdge((h) => (h === i ? null : h))}
                  onClick={() =>
                    editing
                      ? setEditor({
                          mode: 'add',
                          splice: {
                            fromStepId: e.from_step_id,
                            toStepId: e.to_step_id,
                            edge: { id: e.id, label: e.label, is_branch: e.is_branch },
                          },
                        })
                      : setRedescribe(true)
                  }
                  aria-label="Insert step"
                >
                  +
                </button>
                {editing && e.id != null && (
                  <button
                    type="button"
                    className="wf2-edge-edit"
                    onClick={() => setEdgePop({ id: e.id, label: e.label || '', is_branch: !!e.is_branch, x: mx, y: my })}
                    aria-label="Edit connection"
                  >
                    ✎
                  </button>
                )}
              </div>
            )
          })}

          {/* Loop edit affordance */}
          {editing &&
            loops.map((l) => {
              const sp = layout[l.from_id]
              const tp = layout[l.to_id]
              if (!sp || !tp || l.id == null) return null
              const pillX = (sp.x + sp.w / 2 + tp.x + tp.w / 2) / 2
              return (
                <button
                  key={`ledit${l.idx}`}
                  type="button"
                  className="wf2-edge-edit on-loop"
                  style={{ left: pillX + 60, top: l.loopY }}
                  onClick={() => setEdgePop({ id: l.id, label: l.label || '', is_branch: !!l.is_branch, x: pillX + 60, y: l.loopY })}
                  aria-label="Edit loop"
                >
                  ✎
                </button>
              )
            })}

          {/* Edge popover */}
          {edgePop && (
            <div className="wf2-edge-pop" style={{ left: edgePop.x, top: edgePop.y }} onClick={(e) => e.stopPropagation()}>
              <input
                className="wf2-input"
                value={edgePop.label}
                placeholder="Label (e.g. 14 pass)"
                onChange={(e) => setEdgePop((p) => ({ ...p, label: e.target.value }))}
                autoFocus
              />
              <label className="wf2-edge-pop-check">
                <input
                  type="checkbox"
                  checked={edgePop.is_branch}
                  onChange={(e) => setEdgePop((p) => ({ ...p, is_branch: e.target.checked }))}
                />
                Branch / decision path
              </label>
              <div className="wf2-edge-pop-actions">
                <button type="button" className="wf2-edge-pop-del" onClick={() => deleteEdge(edgePop.id)}>
                  <TrashIcon size={12} /> Delete
                </button>
                <div className="wf2-edge-pop-right">
                  <button type="button" className="btn btn-secondary" onClick={() => setEdgePop(null)}>
                    Cancel
                  </button>
                  <button type="button" className="btn btn-primary" onClick={saveEdgePop}>
                    Save
                  </button>
                </div>
              </div>
            </div>
          )}

          {/* Nodes */}
          {nodes.map((node) => {
            const p = layout[node.id]
            if (!p) return null
            const color = nodeColor(node, colors)
            const isExpanded = expanded.has(node.id)
            const ns = nodeStats(node)
            const stepCount = node.kind === 'block' ? node.steps.length : 1
            const canExpand = !editing && node.kind === 'block' && stepCount > 1
            const out = edges.filter((x) => x.from_id === node.id)
            const fwd = out.find((x) => x.to_order > x.from_order)
            const back = out.find((x) => x.to_order < x.from_order)
            const name = node.kind === 'block' ? node.steps[0].label : node.step.label
            const label = node.step_type === 'human' ? `👤 ${stripPerson(name)}` : name
            return (
              <div
                key={node.id}
                className={`wf2-node2 type-${node.step_type} ${isExpanded ? 'is-expanded' : ''} ${canExpand ? 'can-expand' : ''} ${editing ? 'is-editable' : ''}`}
                style={{
                  left: p.x,
                  top: p.y,
                  width: p.w,
                  minHeight: nodeH,
                  borderTopColor: color,
                  zIndex: isExpanded ? 4 : 1,
                  fontSize: view === 'all' && !editing ? '90%' : undefined,
                }}
                onPointerDown={(e) => onNodeDown(e, node.id)}
                onPointerMove={onNodeMove}
                onPointerUp={(e) => onNodeUp(e, node.id, node)}
              >
                {editing && (
                  <>
                    <button
                      type="button"
                      className="wf2-node-del"
                      onClick={(e) => {
                        e.stopPropagation()
                        setConfirmDel(node.id)
                      }}
                      onPointerDown={(e) => e.stopPropagation()}
                      aria-label="Delete step"
                    >
                      ×
                    </button>
                    <div
                      className="wf2-port"
                      onPointerDown={(e) => onPortDown(e, node)}
                      title="Drag to connect"
                    />
                    {confirmDel === node.id && (
                      <div className="wf2-confirm" onPointerDown={(e) => e.stopPropagation()} onClick={(e) => e.stopPropagation()}>
                        <span>Delete this step?</span>
                        <div>
                          <button type="button" onClick={() => setConfirmDel(null)}>
                            Cancel
                          </button>
                          <button type="button" className="is-danger" onClick={() => deleteStep(node)}>
                            Delete
                          </button>
                        </div>
                      </div>
                    )}
                  </>
                )}

                <div className="wf2-node2-badges">
                  <span className="wf2-type-pill" style={{ background: color }}>
                    {TYPE_LABEL[node.step_type] || 'Step'}
                  </span>
                  {stepCount > 1 && <span className="wf2-count-pill">{stepCount} steps</span>}
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

                  {node.step_type === 'decision' && <DecisionStats fwd={fwd} back={back} />}
                  {node.step_type === 'human' && (
                    <span className="wf2-wait">
                      wait {stats?.avg_human_wait_ms == null ? '—' : fmtDur(stats.avg_human_wait_ms)}
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

      {/* Conversational AI editing (edit mode only) */}
      {editing && (
        <div className="wf2-ai-bar">
          <div className="wf2-ai-row">
            <span className="wf2-ai-spark">✦</span>
            <input
              className="wf2-ai-input"
              value={aiText}
              onChange={(e) => setAiText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault()
                  runAiEdit()
                }
              }}
              placeholder="Describe a change — e.g. “add a QA review by qa-agent after triage, and loop back to triage if it fails”"
              disabled={aiBusy}
            />
            <button
              type="button"
              className="btn btn-primary wf2-ai-apply"
              onClick={runAiEdit}
              disabled={aiBusy || !aiText.trim()}
            >
              {aiBusy ? 'Applying…' : 'Apply'}
            </button>
          </div>
          {aiError ? (
            <div className="wf2-ai-msg error">{aiError}</div>
          ) : aiSummary ? (
            <div className="wf2-ai-msg ok">✓ {aiSummary}</div>
          ) : (
            <div className="wf2-ai-msg hint">
              AI edits the flow in place — add or remove steps, reroute, add agents or
              human reviewers. Existing steps keep their telemetry.
            </div>
          )}
        </div>
      )}

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
          <span className="wf2-insight-label">{editing ? 'Editing' : 'Reading the map'}</span>
          <p>
            {editing
              ? 'Click a node to edit it, drag the right-edge handle to connect steps, use + to insert between steps, and the roster to add workers.'
              : 'Node top-bars are colored by worker. Dashed curves below are loops. The solid line is the happy path.'}
          </p>
        </div>
      </div>

      {toast && <div className="wf2-toast">{toast}</div>}

      {editor && (
        <WorkflowStepEditor
          workflowId={workflowId}
          placement={editor}
          participants={wf.participants || []}
          onClose={() => setEditor(null)}
          onSaved={() => {
            setEditor(null)
            refresh()
          }}
        />
      )}

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

  function removeParticipant(p) {
    const stillUsed = (wf.steps || []).some((s) =>
      p.type === 'human'
        ? s.step_type === 'human' && ((s.config && s.config.role_name) || '') === (p.role_name || '')
        : s.step_type === 'agent' &&
          s.agent_service_name === p.agent_service_name &&
          (s.agent_id || 'main') === (p.agent_id || 'main'),
    )
    if (stillUsed && !window.confirm('A step still uses this worker. Remove from the roster anyway?')) {
      return
    }
    api
      .deleteWorkflowParticipant(workflowId, p.id)
      .then(refresh)
      .catch(() => setToast('Could not remove the worker.'))
  }
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

// Flatten one /agents group into selectable units. A flat group (single
// 'main' sub-agent) yields one unit labeled by the service; a multi-agent
// group yields one unit per sub-agent so a specific sub-agent can be
// dropped into the workflow roster.
function selectableAgents(group) {
  const list = group.agents || []
  const isFlat = list.length <= 1 && (list[0]?.agent_id ?? 'main') === 'main'
  if (isFlat) {
    return [
      {
        service_name: group.service_name,
        agent_id: 'main',
        label: group.display_name || group.service_name,
      },
    ]
  }
  return list.map((a) => ({
    service_name: group.service_name,
    agent_id: a.agent_id,
    label: a.display_name || a.agent_id,
  }))
}

// Small popover for adding an agent or human role to the roster.
function RosterAdd({ workflowId, onClose, onDone }) {
  const [mode, setMode] = useState('agent')
  const [agents, setAgents] = useState([])
  const [role, setRole] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    api.listAgents().then((l) => setAgents(l || [])).catch(() => {})
  }, [])

  function addAgent(svc, aid) {
    setBusy(true)
    api
      .addWorkflowParticipant(workflowId, {
        type: 'agent',
        agent_service_name: svc,
        agent_id: aid || 'main',
      })
      .then(onDone)
      .catch(() => {
        setBusy(false)
        onDone()
      })
  }
  function addHuman() {
    if (!role.trim()) return
    setBusy(true)
    api
      .addWorkflowParticipant(workflowId, { type: 'human', role_name: role.trim() })
      .then(onDone)
      .catch(() => {
        setBusy(false)
        onDone()
      })
  }

  return (
    <div className="wf2-roster-pop" onClick={(e) => e.stopPropagation()}>
      <div className="wf2-roster-tabs">
        <button type="button" className={mode === 'agent' ? 'is-on' : ''} onClick={() => setMode('agent')}>
          Agent
        </button>
        <button type="button" className={mode === 'human' ? 'is-on' : ''} onClick={() => setMode('human')}>
          Human role
        </button>
      </div>
      {mode === 'agent' ? (
        <div className="wf2-agent-groups">
          {agents.length === 0 && <span className="wf2-hint">No agents reporting telemetry.</span>}
          {(() => {
            const flat = agents.filter((g) => selectableAgents(g).length === 1)
            const grouped = agents.filter((g) => selectableAgents(g).length > 1)
            return (
              <>
                {flat.length > 0 && (
                  <div className="wf2-agent-pills">
                    {flat.map((g) => {
                      const u = selectableAgents(g)[0]
                      return (
                        <button
                          key={g.service_name}
                          type="button"
                          className="wf2-agent-pill"
                          disabled={busy}
                          onClick={() => addAgent(u.service_name, u.agent_id)}
                        >
                          {u.label}
                        </button>
                      )
                    })}
                  </div>
                )}
                {grouped.map((g) => (
                  <div className="wf2-agent-group" key={g.service_name}>
                    <span className="wf2-agent-group-label">
                      {g.display_name || g.service_name}
                      <small> · sub-agents</small>
                    </span>
                    <div className="wf2-agent-pills">
                      {selectableAgents(g).map((u) => (
                        <button
                          key={u.agent_id}
                          type="button"
                          className="wf2-agent-pill"
                          disabled={busy}
                          onClick={() => addAgent(u.service_name, u.agent_id)}
                        >
                          {u.label}
                        </button>
                      ))}
                    </div>
                  </div>
                ))}
              </>
            )
          })()}
        </div>
      ) : (
        <div className="wf2-roster-human">
          <input
            className="wf2-input"
            value={role}
            placeholder="e.g. Returns Lead"
            onChange={(e) => setRole(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && addHuman()}
            autoFocus
          />
          <button type="button" className="btn btn-primary" disabled={busy || !role.trim()} onClick={addHuman}>
            Add
          </button>
        </div>
      )}
      <button type="button" className="wf2-roster-pop-close" onClick={onClose} aria-label="Close">
        ×
      </button>
    </div>
  )
}
