import { useEffect, useMemo, useRef, useState } from 'react'
import { api, getApiKey } from './api.js'
import { statusColor } from './utils.js'

// Workflows — an SVG diagram of how work flows through an org's humans and
// agents. Pure SVG, no chart library. Nodes are draggable; layout +
// operator-drawn data-flow connections persist to localStorage (V1).
//
// Backend (/workflows) supplies agent + human nodes and 'owns' edges
// (human → their agents, from agent_owners). 'data_flow' edges (agent →
// agent) are drawn by the operator here and stored client-side until
// telemetry-based auto-detection lands.

// --- geometry ---------------------------------------------------------------
const AGENT_W = 184
const AGENT_H = 60
const HUMAN_R = 30
const COL_HUMAN_X = 110
const COL_AGENT_X = 420
const COL_GAP = 250
const ROW_GAP = 124
const TOP = 90
const AGENTS_PER_COL = 6
const PAD = 60

// --- localStorage (namespaced per account so browsers shared across
// accounts don't cross-contaminate layouts) -------------------------------
function lsKey(base) {
  const k = getApiKey() || 'anon'
  return `oversee_wf_${base}_${k.slice(-8)}`
}
function loadLS(base, fallback) {
  try {
    const v = localStorage.getItem(lsKey(base))
    return v ? JSON.parse(v) : fallback
  } catch {
    return fallback
  }
}
function saveLS(base, val) {
  try {
    localStorage.setItem(lsKey(base), JSON.stringify(val))
  } catch {
    // private mode / quota — degrade silently; the chart still works in-session.
  }
}

// --- small pure helpers -----------------------------------------------------
function trunc(s, n) {
  s = String(s || '')
  return s.length > n ? `${s.slice(0, n - 1)}…` : s
}
function initials(name) {
  const parts = String(name || '').trim().split(/\s+/)
  if (!parts[0]) return '?'
  return (parts[0][0] + (parts[1] ? parts[1][0] : '')).toUpperCase()
}
function platformLabel(p) {
  return p ? String(p).replace(/ Agent$/, '') : ''
}

function autoLayout(nodes) {
  const pos = {}
  const humans = nodes.filter((n) => n.type === 'human')
  const agents = nodes.filter((n) => n.type === 'agent')
  humans.forEach((n, i) => {
    pos[n.id] = { x: COL_HUMAN_X, y: TOP + i * ROW_GAP }
  })
  agents.forEach((n, i) => {
    const col = Math.floor(i / AGENTS_PER_COL)
    const row = i % AGENTS_PER_COL
    pos[n.id] = { x: COL_AGENT_X + col * COL_GAP, y: TOP + row * ROW_GAP }
  })
  return pos
}

// Half-extents of a node's hit/visual box, for bounds + edge anchoring.
function halfExtent(node) {
  return node.type === 'agent'
    ? { hw: AGENT_W / 2, hh: AGENT_H / 2 }
    : { hw: HUMAN_R, hh: HUMAN_R }
}

// Point on a node's border in the direction of `toward` — so arrows touch
// the edge of the box/circle, not the center.
function anchor(node, center, toward) {
  const dx = toward.x - center.x
  const dy = toward.y - center.y
  if (dx === 0 && dy === 0) return center
  if (node.type === 'human') {
    const len = Math.hypot(dx, dy)
    return { x: center.x + (dx / len) * HUMAN_R, y: center.y + (dy / len) * HUMAN_R }
  }
  const { hw, hh } = halfExtent(node)
  const scale = 1 / Math.max(Math.abs(dx) / hw, Math.abs(dy) / hh)
  return { x: center.x + dx * scale, y: center.y + dy * scale }
}

export default function Workflows({ onSelectAgent, onOpenTeam }) {
  const [graph, setGraph] = useState({ nodes: [], edges: [] })
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  const [positions, setPositions] = useState({})
  const [dataFlows, setDataFlows] = useState(() => loadLS('flows', []))
  const [connectMode, setConnectMode] = useState(false)
  const [connectSource, setConnectSource] = useState(null)

  const svgRef = useRef(null)
  const dragRef = useRef(null)
  const loadedRef = useRef(false)

  useEffect(() => {
    let cancelled = false
    api
      .getWorkflows()
      .then((data) => {
        if (cancelled) return
        setGraph({ nodes: data?.nodes || [], edges: data?.edges || [] })
        setLoading(false)
      })
      .catch((e) => {
        if (cancelled) return
        setError(e.message || 'Could not load workflows')
        setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [])

  // Once nodes arrive, seed positions: saved spots win, missing nodes get
  // an auto-layout slot. Prune saved positions for nodes that no longer exist.
  useEffect(() => {
    if (!graph.nodes.length) return
    const saved = loadLS('positions', {})
    const auto = autoLayout(graph.nodes)
    const next = {}
    for (const n of graph.nodes) {
      next[n.id] = saved[n.id] || auto[n.id]
    }
    setPositions(next)
    loadedRef.current = true
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [graph.nodes])

  // Persist layout + connections after the initial seed.
  useEffect(() => {
    if (loadedRef.current && Object.keys(positions).length) saveLS('positions', positions)
  }, [positions])
  useEffect(() => {
    if (loadedRef.current) saveLS('flows', dataFlows)
  }, [dataFlows])

  const nodeById = useMemo(() => {
    const m = {}
    for (const n of graph.nodes) m[n.id] = n
    return m
  }, [graph.nodes])

  const agentIds = useMemo(
    () => new Set(graph.nodes.filter((n) => n.type === 'agent').map((n) => n.id)),
    [graph.nodes],
  )

  // Owns edges from the backend + operator data-flow edges (both endpoints
  // must still exist as agents).
  const renderEdges = useMemo(() => {
    const owns = (graph.edges || []).filter(
      (e) => nodeById[e.source] && nodeById[e.target],
    )
    const flows = dataFlows
      .filter((f) => agentIds.has(f.source) && agentIds.has(f.target))
      .map((f) => ({ source: f.source, target: f.target, type: 'data_flow', label: '' }))
    return [...owns, ...flows]
  }, [graph.edges, dataFlows, nodeById, agentIds])

  // Canvas size from the spread of nodes.
  const bounds = useMemo(() => {
    let maxX = 600
    let maxY = 360
    for (const n of graph.nodes) {
      const p = positions[n.id]
      if (!p) continue
      const { hw, hh } = halfExtent(n)
      maxX = Math.max(maxX, p.x + hw)
      maxY = Math.max(maxY, p.y + hh + (n.type === 'human' ? 26 : 0))
    }
    return { w: maxX + PAD, h: maxY + PAD }
  }, [graph.nodes, positions])

  function toSvg(e) {
    const rect = svgRef.current.getBoundingClientRect()
    return { x: e.clientX - rect.left, y: e.clientY - rect.top }
  }

  function onNodePointerDown(e, id) {
    e.stopPropagation()
    const p = toSvg(e)
    const pos = positions[id] || { x: p.x, y: p.y }
    dragRef.current = { id, ox: p.x - pos.x, oy: p.y - pos.y, downX: p.x, downY: p.y, moved: false }
    try {
      svgRef.current.setPointerCapture(e.pointerId)
    } catch {
      /* not all environments support capture */
    }
  }
  function onPointerMove(e) {
    const d = dragRef.current
    if (!d) return
    const p = toSvg(e)
    if (Math.hypot(p.x - d.downX, p.y - d.downY) > 4) d.moved = true
    setPositions((prev) => ({ ...prev, [d.id]: { x: p.x - d.ox, y: p.y - d.oy } }))
  }
  function onPointerUp(e) {
    const d = dragRef.current
    if (!d) return
    dragRef.current = null
    try {
      svgRef.current.releasePointerCapture(e.pointerId)
    } catch {
      /* ignore */
    }
    if (!d.moved) handleNodeClick(d.id)
  }

  function handleNodeClick(id) {
    const node = nodeById[id]
    if (!node) return
    if (connectMode) {
      if (node.type !== 'agent') return // data flow connects agents only
      if (!connectSource) {
        setConnectSource(id)
      } else if (connectSource === id) {
        setConnectSource(null)
      } else {
        addDataFlow(connectSource, id)
        setConnectSource(null)
      }
      return
    }
    if (node.type === 'agent') {
      onSelectAgent?.(node.service_name)
    } else {
      onOpenTeam?.()
    }
  }

  function addDataFlow(source, target) {
    setDataFlows((prev) =>
      prev.some((f) => f.source === source && f.target === target)
        ? prev
        : [...prev, { source, target }],
    )
  }
  function removeDataFlow(source, target) {
    setDataFlows((prev) => prev.filter((f) => !(f.source === source && f.target === target)))
  }

  function toggleConnect() {
    setConnectMode((m) => !m)
    setConnectSource(null)
  }
  function resetLayout() {
    const auto = autoLayout(graph.nodes)
    setPositions(auto)
    saveLS('positions', auto)
  }

  return (
    <div className="view workflow-view">
      <header className="workflow-header">
        <div>
          <h2 className="section-label">Workflows</h2>
          <p className="workflow-subtitle">
            How work flows through your humans and agents. Drag to arrange;
            connect agents to map how their output feeds the next.
          </p>
        </div>
        <div className="workflow-actions">
          <button
            type="button"
            className={`btn btn-secondary ${connectMode ? 'btn-active' : ''}`}
            onClick={toggleConnect}
          >
            {connectMode ? 'Done connecting' : 'Add connection'}
          </button>
          <button type="button" className="btn btn-secondary" onClick={resetLayout}>
            Reset layout
          </button>
        </div>
      </header>

      {connectMode && (
        <div className="workflow-hint">
          {connectSource
            ? 'Now click the target agent (its output flows from the highlighted one).'
            : 'Click a source agent, then a target agent, to draw a data-flow connection.'}
        </div>
      )}

      <WorkflowLegend />

      {loading && <div className="state-card">Loading workflow…</div>}
      {error && !loading && (
        <div className="state-card error">
          <h2>Couldn't load workflows</h2>
          <p>{error}</p>
        </div>
      )}
      {!loading && !error && graph.nodes.length === 0 && (
        <div className="state-card">
          <h2>Nothing to map yet</h2>
          <p>
            Once agents are sending telemetry and you've added team members,
            they'll appear here. Assign owners from each agent's page to draw
            the connections.
          </p>
        </div>
      )}

      {!loading && !error && graph.nodes.length > 0 && (
        <div className={`workflow-canvas ${connectMode ? 'is-connecting' : ''}`}>
          <svg
            ref={svgRef}
            className="workflow-svg"
            width={bounds.w}
            height={bounds.h}
            onPointerMove={onPointerMove}
            onPointerUp={onPointerUp}
            onPointerLeave={onPointerUp}
          >
            <defs>
              <marker
                id="wf-arrow"
                viewBox="0 0 10 10"
                refX="9"
                refY="5"
                markerWidth="7"
                markerHeight="7"
                orient="auto-start-reverse"
              >
                <path d="M0,0 L10,5 L0,10 z" style={{ fill: 'var(--wf-edge)' }} />
              </marker>
            </defs>

            {renderEdges.map((e) => (
              <EdgePath
                key={`${e.type}:${e.source}->${e.target}`}
                edge={e}
                nodeById={nodeById}
                positions={positions}
                onRemove={e.type === 'data_flow' ? removeDataFlow : null}
              />
            ))}

            {graph.nodes.map((n) => {
              const p = positions[n.id]
              if (!p) return null
              const isSource = connectSource === n.id
              return n.type === 'agent' ? (
                <AgentNode
                  key={n.id}
                  node={n}
                  pos={p}
                  highlighted={isSource}
                  onPointerDown={(e) => onNodePointerDown(e, n.id)}
                />
              ) : (
                <HumanNode
                  key={n.id}
                  node={n}
                  pos={p}
                  onPointerDown={(e) => onNodePointerDown(e, n.id)}
                />
              )
            })}
          </svg>
        </div>
      )}
    </div>
  )
}

function WorkflowLegend() {
  return (
    <div className="workflow-legend" aria-hidden="true">
      <span className="wf-legend-item">
        <span className="wf-legend-swatch wf-swatch-agent" /> Agent
      </span>
      <span className="wf-legend-item">
        <span className="wf-legend-swatch wf-swatch-human" /> Person
      </span>
      <span className="wf-legend-item">
        <svg width="34" height="10">
          <line x1="2" y1="5" x2="32" y2="5" className="wf-legend-line wf-legend-owns" />
        </svg>
        Owns
      </span>
      <span className="wf-legend-item">
        <svg width="34" height="10">
          <line x1="2" y1="5" x2="32" y2="5" className="wf-legend-line wf-legend-flow" />
        </svg>
        Data flow
      </span>
    </div>
  )
}

function EdgePath({ edge, nodeById, positions, onRemove }) {
  const s = nodeById[edge.source]
  const t = nodeById[edge.target]
  const sp = positions[edge.source]
  const tp = positions[edge.target]
  if (!s || !t || !sp || !tp) return null

  const a = anchor(s, sp, tp)
  const b = anchor(t, tp, sp)
  const mx = (a.x + b.x) / 2
  const d = `M${a.x},${a.y} C ${mx},${a.y} ${mx},${b.y} ${b.x},${b.y}`
  const cls = edge.type === 'data_flow' ? 'wf-edge wf-edge-flow' : 'wf-edge wf-edge-owns'
  const midX = (a.x + b.x) / 2
  const midY = (a.y + b.y) / 2

  return (
    <g>
      <path d={d} className={cls} markerEnd="url(#wf-arrow)" fill="none" />
      {onRemove && (
        <g
          className="wf-edge-remove"
          onClick={(e) => {
            e.stopPropagation()
            onRemove(edge.source, edge.target)
          }}
        >
          <title>Remove connection</title>
          <circle cx={midX} cy={midY} r="8" />
          <text x={midX} y={midY + 3} textAnchor="middle">
            ×
          </text>
        </g>
      )}
    </g>
  )
}

function AgentNode({ node, pos, highlighted, onPointerDown }) {
  const x = pos.x - AGENT_W / 2
  const y = pos.y - AGENT_H / 2
  const plat = platformLabel(node.platform)
  return (
    <g
      className={`wf-node wf-node-agent ${highlighted ? 'is-source' : ''}`}
      onPointerDown={onPointerDown}
    >
      <title>{node.name}</title>
      <rect
        x={x}
        y={y}
        width={AGENT_W}
        height={AGENT_H}
        rx="12"
        className="wf-agent-box"
      />
      <circle cx={x + 16} cy={pos.y - 8} r="5" style={{ fill: statusColor(node.status) }} />
      <text x={x + 30} y={pos.y - 4} className="wf-agent-name">
        {trunc(node.name, 20)}
      </text>
      {plat && (
        <text x={x + 30} y={pos.y + 14} className="wf-agent-platform">
          {trunc(plat, 24)}
        </text>
      )}
    </g>
  )
}

function HumanNode({ node, pos, onPointerDown }) {
  return (
    <g className="wf-node wf-node-human" onPointerDown={onPointerDown}>
      <title>{node.name}</title>
      <circle cx={pos.x} cy={pos.y} r={HUMAN_R} className="wf-human-circle" />
      <text x={pos.x} y={pos.y + 5} textAnchor="middle" className="wf-human-initials">
        {initials(node.name)}
      </text>
      <text x={pos.x} y={pos.y + HUMAN_R + 16} textAnchor="middle" className="wf-human-name">
        {trunc(node.name, 18)}
      </text>
      {node.role && (
        <text
          x={pos.x}
          y={pos.y + HUMAN_R + 30}
          textAnchor="middle"
          className="wf-human-role"
        >
          {trunc(node.role, 18)}
        </text>
      )}
    </g>
  )
}
