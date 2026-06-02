import { useEffect, useMemo, useRef, useState } from 'react'
import { api } from './api.js'
import { statusFor, statusColor } from './utils.js'

// Multi-agent connections map. A directional system diagram: agent nodes +
// directed edges (who feeds whom), derived from telemetry (shared traces)
// and operator-curated. Lives on the Workflows tab.
//
// Edges: detected = dashed gray (with ✓/× to confirm/dismiss), confirmed =
// solid teal, manual = solid accent. Nodes are draggable (positions persist
// to localStorage); click a node to open its detail.

const NODE_W = 176
const NODE_H = 54
const COL_GAP = 250
const ROW_GAP = 96
const TOP = 70
const LEFT = 80
const PAD = 60
const LS_POS = 'oversee_map_positions'

function loadPos() {
  try {
    return JSON.parse(localStorage.getItem(LS_POS) || '{}')
  } catch {
    return {}
  }
}
function savePos(p) {
  try {
    localStorage.setItem(LS_POS, JSON.stringify(p))
  } catch {
    /* ignore */
  }
}

function trunc(s, n) {
  s = String(s || '')
  return s.length > n ? `${s.slice(0, n - 1)}…` : s
}
function platformLabel(p) {
  return p ? String(p).replace(/ Agent$/, '') : ''
}

// Left→right layered layout: entry agents (no incoming edges) on the left,
// downstream agents stacked by BFS depth. Draggable overrides win.
function autoLayout(nodeIds, edges) {
  const incoming = {}
  const out = {}
  nodeIds.forEach((id) => {
    incoming[id] = 0
    out[id] = []
  })
  edges.forEach((e) => {
    if (e.source in out && e.target in incoming && e.source !== e.target) {
      out[e.source].push(e.target)
      incoming[e.target] += 1
    }
  })
  const depth = {}
  const queue = nodeIds.filter((id) => incoming[id] === 0)
  if (queue.length === 0) queue.push(...nodeIds) // all in a cycle → flat
  queue.forEach((id) => (depth[id] = 0))
  let head = 0
  while (head < queue.length) {
    const id = queue[head++]
    for (const t of out[id] || []) {
      if (depth[t] === undefined || depth[t] < depth[id] + 1) {
        depth[t] = depth[id] + 1
        queue.push(t)
      }
    }
  }
  nodeIds.forEach((id) => {
    if (depth[id] === undefined) depth[id] = 0
  })
  const byDepth = {}
  const pos = {}
  nodeIds.forEach((id) => {
    const d = depth[id]
    byDepth[d] = byDepth[d] || []
    const row = byDepth[d].length
    byDepth[d].push(id)
    pos[id] = { x: LEFT + d * COL_GAP, y: TOP + row * ROW_GAP }
  })
  return pos
}

// Point on a node's border toward another point, so arrows touch the edge.
function anchor(center, toward) {
  const dx = toward.x - center.x
  const dy = toward.y - center.y
  if (dx === 0 && dy === 0) return center
  const hw = NODE_W / 2
  const hh = NODE_H / 2
  const scale = 1 / Math.max(Math.abs(dx) / hw, Math.abs(dy) / hh)
  return { x: center.x + dx * scale, y: center.y + dy * scale }
}

export default function ConnectionsMap({ onSelectAgent }) {
  const [agents, setAgents] = useState([])
  const [connections, setConnections] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)
  const [positions, setPositions] = useState({})
  const [connectMode, setConnectMode] = useState(false)
  const [connectSource, setConnectSource] = useState(null)
  const svgRef = useRef(null)
  const dragRef = useRef(null)
  const loadedRef = useRef(false)

  async function load(detect) {
    try {
      const [ag, conns] = await Promise.all([
        api.listAgents(),
        detect ? api.detectConnections() : api.getConnections(),
      ])
      setAgents(ag || [])
      setConnections(conns || [])
    } catch (e) {
      setError(e.message || 'Could not load the map')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load(true) // refresh detection on open
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Visible edges (exclude dismissed) keyed to existing node ids.
  const edges = useMemo(
    () =>
      connections
        .filter((c) => c.status !== 'dismissed')
        .map((c) => ({
          id: c.id,
          source: c.source_service,
          target: c.target_service,
          status: c.status,
          calls: c.call_count,
        })),
    [connections],
  )

  // Node set = agents ∪ any service referenced by an edge.
  const agentByService = useMemo(() => {
    const m = {}
    for (const g of agents) m[g.service_name] = g
    return m
  }, [agents])

  const nodeIds = useMemo(() => {
    const set = new Set(agents.map((g) => g.service_name))
    edges.forEach((e) => {
      set.add(e.source)
      set.add(e.target)
    })
    return [...set]
  }, [agents, edges])

  // Seed positions once nodes are known: saved spots win, rest auto-laid.
  useEffect(() => {
    if (!nodeIds.length) return
    const saved = loadPos()
    const auto = autoLayout(nodeIds, edges)
    const next = {}
    nodeIds.forEach((id) => {
      next[id] = saved[id] || auto[id]
    })
    setPositions(next)
    loadedRef.current = true
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodeIds.length])

  useEffect(() => {
    if (loadedRef.current && Object.keys(positions).length) savePos(positions)
  }, [positions])

  const bounds = useMemo(() => {
    let w = 700
    let h = 360
    Object.values(positions).forEach((p) => {
      w = Math.max(w, p.x + NODE_W / 2)
      h = Math.max(h, p.y + NODE_H / 2)
    })
    return { w: w + PAD, h: h + PAD }
  }, [positions])

  function toSvg(e) {
    const r = svgRef.current.getBoundingClientRect()
    return { x: e.clientX - r.left, y: e.clientY - r.top }
  }
  function onNodeDown(e, id) {
    e.stopPropagation()
    const p = toSvg(e)
    const pos = positions[id] || { x: p.x, y: p.y }
    dragRef.current = { id, ox: p.x - pos.x, oy: p.y - pos.y, dx: p.x, dy: p.y, moved: false }
    try {
      svgRef.current.setPointerCapture(e.pointerId)
    } catch {
      /* ignore */
    }
  }
  function onMove(e) {
    const d = dragRef.current
    if (!d) return
    const p = toSvg(e)
    if (Math.hypot(p.x - d.dx, p.y - d.dy) > 4) d.moved = true
    setPositions((prev) => ({ ...prev, [d.id]: { x: p.x - d.ox, y: p.y - d.oy } }))
  }
  function onUp(e) {
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
    if (connectMode) {
      if (!connectSource) setConnectSource(id)
      else if (connectSource === id) setConnectSource(null)
      else {
        addConnection(connectSource, id)
        setConnectSource(null)
      }
      return
    }
    onSelectAgent?.(id)
  }

  async function addConnection(source, target) {
    setBusy(true)
    try {
      await api.addConnection({ source_service: source, target_service: target })
      await load(false)
    } catch (e) {
      setError(e.message || 'Could not add connection')
    } finally {
      setBusy(false)
    }
  }
  async function setStatus(id, status) {
    try {
      const updated = await api.updateConnection(id, status)
      setConnections((prev) => prev.map((c) => (c.id === id ? updated : c)))
    } catch {
      /* ignore */
    }
  }
  async function removeEdge(id) {
    try {
      await api.deleteConnection(id)
      setConnections((prev) => prev.filter((c) => c.id !== id))
    } catch {
      /* ignore */
    }
  }
  async function rescan() {
    setBusy(true)
    await load(true)
    setBusy(false)
  }
  function resetLayout() {
    const auto = autoLayout(nodeIds, edges)
    setPositions(auto)
    savePos(auto)
  }

  return (
    <div className="map-view">
      <div className="map-toolbar">
        <div>
          <h2 className="section-label">Connections map</h2>
          <p className="map-subtitle">
            How your agents feed into each other. Edges come from shared
            traces; confirm the ones that are real or draw your own.
          </p>
        </div>
        <div className="map-actions">
          <button
            type="button"
            className={`btn btn-secondary btn-sm ${connectMode ? 'btn-active' : ''}`}
            onClick={() => {
              setConnectMode((m) => !m)
              setConnectSource(null)
            }}
          >
            {connectMode ? 'Done' : 'Add connection'}
          </button>
          <button type="button" className="btn btn-secondary btn-sm" onClick={resetLayout}>
            Reset layout
          </button>
          <button type="button" className="btn btn-secondary btn-sm" disabled={busy} onClick={rescan}>
            Rescan
          </button>
        </div>
      </div>

      <div className="map-legend" aria-hidden="true">
        <span className="map-legend-item"><svg width="30" height="8"><line x1="2" y1="4" x2="28" y2="4" className="map-leg-detected" /></svg>Detected</span>
        <span className="map-legend-item"><svg width="30" height="8"><line x1="2" y1="4" x2="28" y2="4" className="map-leg-confirmed" /></svg>Confirmed</span>
        <span className="map-legend-item"><svg width="30" height="8"><line x1="2" y1="4" x2="28" y2="4" className="map-leg-manual" /></svg>Manual</span>
      </div>

      {connectMode && (
        <div className="map-hint">
          {connectSource ? 'Now click the target agent.' : 'Click a source agent, then a target, to draw a connection.'}
        </div>
      )}

      {loading && <div className="state-card">Loading map…</div>}
      {error && !loading && (
        <div className="state-card error"><h2>Couldn't load the map</h2><p>{error}</p></div>
      )}
      {!loading && !error && nodeIds.length === 0 && (
        <div className="state-card"><h2>No agents yet</h2><p>Once agents send telemetry they'll appear here, with connections drawn from shared traces.</p></div>
      )}

      {!loading && !error && nodeIds.length > 0 && (
        <div className={`map-canvas ${connectMode ? 'is-connecting' : ''}`}>
          <svg
            ref={svgRef}
            className="map-svg"
            width={bounds.w}
            height={bounds.h}
            onPointerMove={onMove}
            onPointerUp={onUp}
            onPointerLeave={onUp}
          >
            <defs>
              <marker id="map-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
                <path d="M0,0 L10,5 L0,10 z" fill="context-stroke" />
              </marker>
            </defs>

            {edges.map((e) => {
              const sp = positions[e.source]
              const tp = positions[e.target]
              if (!sp || !tp || e.source === e.target) return null
              const a = anchor(sp, tp)
              const b = anchor(tp, sp)
              const mx = (a.x + b.x) / 2
              const path = `M${a.x},${a.y} C ${mx},${a.y} ${mx},${b.y} ${b.x},${b.y}`
              const midX = (a.x + b.x) / 2
              const midY = (a.y + b.y) / 2
              return (
                <g key={e.id}>
                  <path d={path} className={`map-edge map-edge-${e.status}`} markerEnd="url(#map-arrow)" fill="none" />
                  {e.calls > 0 && (
                    <text x={midX} y={midY - 6} textAnchor="middle" className="map-edge-label">{e.calls}</text>
                  )}
                  {e.status === 'detected' ? (
                    <g className="map-edge-confirm">
                      <circle cx={midX - 9} cy={midY + 8} r="8" className="map-edge-btn" onClick={() => setStatus(e.id, 'confirmed')} />
                      <text x={midX - 9} y={midY + 11} textAnchor="middle" className="map-edge-btn-txt confirm" onClick={() => setStatus(e.id, 'confirmed')}>✓</text>
                      <circle cx={midX + 9} cy={midY + 8} r="8" className="map-edge-btn" onClick={() => setStatus(e.id, 'dismissed')} />
                      <text x={midX + 9} y={midY + 11} textAnchor="middle" className="map-edge-btn-txt dismiss" onClick={() => setStatus(e.id, 'dismissed')}>×</text>
                    </g>
                  ) : (
                    <g className="map-edge-remove">
                      <circle cx={midX} cy={midY + 8} r="7" className="map-edge-btn" onClick={() => removeEdge(e.id)} />
                      <text x={midX} y={midY + 11} textAnchor="middle" className="map-edge-btn-txt dismiss" onClick={() => removeEdge(e.id)}>×</text>
                    </g>
                  )}
                </g>
              )
            })}

            {nodeIds.map((id) => {
              const p = positions[id]
              if (!p) return null
              const g = agentByService[id]
              const status = g
                ? statusFor({ span_count: g.total_spans, error_count: g.total_errors, last_seen: g.last_seen })
                : 'gray'
              const name = g?.display_name || id
              const plat = platformLabel(g?.platform)
              const x = p.x - NODE_W / 2
              const y = p.y - NODE_H / 2
              return (
                <g
                  key={id}
                  className={`map-node ${connectSource === id ? 'is-source' : ''}`}
                  onPointerDown={(e) => onNodeDown(e, id)}
                >
                  <title>{name}</title>
                  <rect x={x} y={y} width={NODE_W} height={NODE_H} rx="11" className="map-node-box" />
                  <circle cx={x + 15} cy={p.y - 7} r="5" style={{ fill: statusColor(status) }} />
                  <text x={x + 28} y={p.y - 3} className="map-node-name">{trunc(name, 18)}</text>
                  {plat && <text x={x + 28} y={p.y + 13} className="map-node-plat">{trunc(plat, 22)}</text>}
                </g>
              )
            })}
          </svg>
        </div>
      )}
    </div>
  )
}
