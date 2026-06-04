import { useEffect, useRef, useState } from 'react'
import { api } from './api.js'
import {
  ArrowLeftIcon,
  ClockIcon,
  RobotIcon,
  UserIcon,
  DiamondIcon,
  CheckCircleIcon,
} from './Icons.jsx'

// Spatial flow canvas: positioned step nodes + SVG edges + a read-only
// click-to-inspect detail panel + a stats header bar. Drag a node to
// reposition (persisted via PUT position). Generation/editing of the graph
// itself happens via the create modal (v1 is generate + reposition).

const NODE_TYPES = {
  trigger: { label: 'Trigger', Icon: ClockIcon },
  agent: { label: 'Agent', Icon: RobotIcon },
  human: { label: 'Human', Icon: UserIcon },
  decision: { label: 'Decision', Icon: DiamondIcon },
  output: { label: 'Output', Icon: CheckCircleIcon },
}
const TYPE_ORDER = ['trigger', 'agent', 'human', 'decision', 'output']

function fmtDur(ms) {
  if (!ms || ms <= 0) return '—'
  const s = ms / 1000
  if (s < 1) return `${Math.round(ms)}ms`
  if (s < 60) return `${s.toFixed(1)}s`
  return `${Math.round(s / 60)}m`
}

// Lay out positionless (pre-redesign) workflows left-to-right by step_order.
function withLayout(steps) {
  const allZero = steps.every((s) => !s.pos_x && !s.pos_y)
  if (!allZero) return steps.map((s) => ({ ...s }))
  return steps.map((s, i) => ({ ...s, pos_x: 60 + i * 230, pos_y: 200 }))
}

function edgeGeom(from, to) {
  const fx = from.x + from.w
  const fy = from.y + from.h / 2
  const tx = to.x
  const ty = to.y + to.h / 2
  const mx = (fx + tx) / 2
  const d =
    Math.abs(fy - ty) < 1
      ? `M${fx},${fy} L${tx},${ty}`
      : `M${fx},${fy} C${mx},${fy} ${mx},${ty} ${tx},${ty}`
  return { d, mx, my: (fy + ty) / 2 }
}

export default function WorkflowCanvas({ workflowId, onBack }) {
  const [wf, setWf] = useState(null)
  const [stats, setStats] = useState(null)
  const [error, setError] = useState(null)
  const [positions, setPositions] = useState({}) // id -> {x,y}
  const [sizes, setSizes] = useState({}) // id -> {w,h}
  const [selected, setSelected] = useState(null) // step id
  const dragRef = useRef(null)

  useEffect(() => {
    let alive = true
    api
      .getWorkflow(workflowId)
      .then((data) => {
        if (!alive) return
        const steps = withLayout(data.steps || [])
        const pos = {}
        const sz = {}
        for (const s of steps) {
          pos[s.id] = { x: s.pos_x, y: s.pos_y }
          sz[s.id] = { w: s.node_width || 170, h: s.node_height || 72 }
        }
        setWf(data)
        setPositions(pos)
        setSizes(sz)
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

  function onNodeDown(e, id) {
    e.stopPropagation()
    const p = positions[id]
    dragRef.current = {
      id,
      sx: e.clientX,
      sy: e.clientY,
      ox: p.x,
      oy: p.y,
      moved: false,
    }
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
    setPositions((prev) => ({ ...prev, [d.id]: { x: Math.max(0, d.ox + dx), y: Math.max(0, d.oy + dy) } }))
  }
  function onNodeUp(e, id) {
    const d = dragRef.current
    if (!d) return
    dragRef.current = null
    try {
      e.currentTarget.releasePointerCapture(e.pointerId)
    } catch {
      /* ignore */
    }
    if (!d.moved) {
      setSelected((cur) => (cur === id ? null : id))
      return
    }
    const p = positions[id]
    if (p) {
      api
        .updateStepPosition(workflowId, id, { pos_x: Math.round(p.x), pos_y: Math.round(p.y) })
        .catch(() => {})
    }
  }

  if (error) {
    return (
      <div className="wf2">
        <button type="button" className="wf2-back" onClick={onBack}>
          <ArrowLeftIcon size={14} /> Workflows
        </button>
        <div className="wf2-info-box">{error}</div>
      </div>
    )
  }
  if (!wf) {
    return (
      <div className="wf2">
        <div className="wf2-skel">
          <span />
        </div>
      </div>
    )
  }

  const steps = wf.steps || []
  let edges = wf.edges || []
  // Back-compat: synthesize sequential edges when none exist.
  if (edges.length === 0 && steps.length > 1) {
    const ordered = [...steps].sort((a, b) => a.step_order - b.step_order)
    edges = ordered.slice(0, -1).map((s, i) => ({
      from_step_id: s.id,
      to_step_id: ordered[i + 1].id,
      is_branch: false,
      label: null,
    }))
  }

  // Canvas content size from the rightmost / bottommost node.
  let maxX = 600
  let maxY = 360
  for (const s of steps) {
    const p = positions[s.id]
    const z = sizes[s.id] || { w: 170, h: 72 }
    if (p) {
      maxX = Math.max(maxX, p.x + z.w + 120)
      maxY = Math.max(maxY, p.y + z.h + 120)
    }
  }

  const selStep = steps.find((s) => s.id === selected) || null
  const perStep = stats?.per_step || {}

  return (
    <div className="wf2 wf2-canvas-view">
      <div className="wf2-proc-head">
        <div className="wf2-proc-left">
          <button type="button" className="wf2-back" onClick={onBack} aria-label="Back">
            <ArrowLeftIcon size={15} />
          </button>
          <span className="wf2-proc-name">{wf.name}</span>
          <div className="wf2-proc-parts">
            {(wf.participants || []).map((p, i) =>
              p.type === 'human' ? (
                <span key={i} className="wf2-part-pill human">
                  👤 {p.role_name}
                </span>
              ) : (
                <span key={i} className="wf2-part-pill agent">
                  <span className="wf2-part-dot" />
                  {p.agent_service_name}
                </span>
              ),
            )}
          </div>
          <span className="wf2-proc-meta">· {steps.length} steps</span>
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
          <HeadStat
            label="Escalation"
            value={stats?.escalation_rate == null ? '—' : `${Math.round(stats.escalation_rate)}%`}
          />
          <HeadStat
            label="Avg human wait"
            value={stats?.avg_human_wait_ms == null ? '—' : fmtDur(stats.avg_human_wait_ms)}
          />
        </div>
        <div className="wf2-legend">
          {TYPE_ORDER.map((t) => (
            <span key={t} className="wf2-legend-item">
              <span className={`wf2-legend-sq type-${t}`} />
              {NODE_TYPES[t].label}
            </span>
          ))}
        </div>
      </div>

      <div className="wf2-canvas" onClick={() => setSelected(null)}>
        <div className="wf2-canvas-inner" style={{ width: maxX, height: maxY }}>
          <svg className="wf2-edges" width={maxX} height={maxY} aria-hidden="true">
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
                <path d="M0,0 L10,5 L0,10 z" fill="context-stroke" />
              </marker>
            </defs>
            {edges.map((e, i) => {
              const fp = positions[e.from_step_id]
              const tp = positions[e.to_step_id]
              if (!fp || !tp) return null
              const fz = sizes[e.from_step_id] || { w: 170, h: 72 }
              const tz = sizes[e.to_step_id] || { w: 170, h: 72 }
              const g = edgeGeom({ ...fp, ...fz }, { ...tp, ...tz })
              return (
                <g key={i}>
                  <path
                    d={g.d}
                    className={`wf2-edge ${e.is_branch ? 'is-branch' : ''}`}
                    fill="none"
                    markerEnd="url(#wf2-arrow)"
                  />
                  {e.label && (
                    <text x={g.mx} y={g.my - 6} textAnchor="middle" className="wf2-edge-label">
                      {e.label}
                    </text>
                  )}
                </g>
              )
            })}
          </svg>

          {steps.map((s) => {
            const p = positions[s.id]
            const z = sizes[s.id] || { w: 170, h: 72 }
            if (!p) return null
            const cfg = NODE_TYPES[s.step_type] || NODE_TYPES.agent
            const ps = perStep[String(s.id)]
            return (
              <div
                key={s.id}
                className={`wf2-node type-${s.step_type} ${selected === s.id ? 'is-selected' : ''}`}
                style={{ left: p.x, top: p.y, width: z.w, minHeight: z.h }}
                onPointerDown={(e) => onNodeDown(e, s.id)}
                onPointerMove={onNodeMove}
                onPointerUp={(e) => onNodeUp(e, s.id)}
                onClick={(e) => e.stopPropagation()}
              >
                <div className="wf2-node-top">
                  <span className={`wf2-node-sq type-${s.step_type}`}>
                    <cfg.Icon size={11} />
                  </span>
                  <span className="wf2-node-type">{cfg.label}</span>
                  {s.step_type === 'agent' && s.agent_service_name && (
                    <span className="wf2-node-agent">{s.agent_service_name}</span>
                  )}
                </div>
                <div className="wf2-node-name">{s.label}</div>
                {ps ? (
                  <div className="wf2-node-stats">
                    <span>{ps.runs} runs</span>
                    <span className={ps.success_rate < 100 ? 'warn' : ''}>{fmtDur(ps.avg_duration_ms)}</span>
                  </div>
                ) : s.step_type === 'human' ? (
                  <div className="wf2-node-stats">
                    <span className="wait">wait —</span>
                  </div>
                ) : null}
              </div>
            )
          })}
        </div>

        {selStep && (
          <DetailPanel
            step={selStep}
            stat={perStep[String(selStep.id)]}
            onClose={() => setSelected(null)}
          />
        )}
      </div>
    </div>
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

function DetailPanel({ step, stat, onClose }) {
  const cfg = NODE_TYPES[step.step_type] || NODE_TYPES.agent
  const role = (step.config || {}).role_name
  return (
    <div className="wf2-detail" onClick={(e) => e.stopPropagation()}>
      <div className="wf2-detail-head">
        <span className={`wf2-node-sq type-${step.step_type}`}>
          <cfg.Icon size={12} />
        </span>
        <span className="wf2-detail-type">{cfg.label}</span>
        <button type="button" className="wf2-detail-close" onClick={onClose} aria-label="Close">
          ×
        </button>
      </div>
      <div className="wf2-detail-name">{step.label}</div>
      {step.step_type === 'agent' && step.agent_service_name && (
        <div className="wf2-detail-sub">
          {step.agent_service_name}
          {step.agent_id && step.agent_id !== 'main' ? ` · ${step.agent_id}` : ''}
        </div>
      )}
      {step.step_type === 'human' && role && <div className="wf2-detail-sub">{role}</div>}
      {step.description && <p className="wf2-detail-desc">{step.description}</p>}

      <div className="wf2-detail-stats">
        {stat ? (
          <>
            <DetailStat label="Runs (24h)" value={stat.runs} />
            <DetailStat label="Avg duration" value={fmtDur(stat.avg_duration_ms)} />
            <DetailStat
              label="Success rate"
              value={`${Math.round(stat.success_rate)}%`}
              tone={stat.success_rate >= 90 ? 'good' : 'warn'}
            />
          </>
        ) : step.step_type === 'human' ? (
          <DetailStat label="Avg wait time" value="—" />
        ) : (
          <DetailStat label="Runs (24h)" value="—" />
        )}
        {step.operation && <DetailStat label="Operation" value={step.operation} mono />}
      </div>
    </div>
  )
}

function DetailStat({ label, value, tone, mono }) {
  return (
    <div className="wf2-detail-stat">
      <span className="wf2-detail-stat-label">{label}</span>
      <span className={`wf2-detail-stat-value ${tone ? `tone-${tone}` : ''} ${mono ? 'mono' : ''}`}>
        {value}
      </span>
    </div>
  )
}
