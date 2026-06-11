import { stepWorkerKey } from './workerColors.js'

// Collapse a raw step list into "handoff altitude": consecutive steps owned by
// the SAME agent fold into one rendered block; triggers, decisions, humans and
// outputs always stand alone (they are the handoff points). Pure + testable.
//
// Returns { nodes, edges } where:
//   nodes: [{ id, kind: 'single'|'block', step|steps, step_type, workerKey,
//             flowIndex, ... }] — id is the FIRST step's id (drag positions
//             key off this).
//   edges: edges re-mapped to node ids; intra-block edges dropped, boundary
//          edges connect the surrounding nodes. Each carries the original
//          {label, is_branch} plus from_order/to_order (flow indices) so the
//          renderer can tell forward from backward (loop) edges.

// Order steps along the flow: topological from forward edges, falling back to
// step_order. Backward (loop) edges are ignored for ordering.
export function flowOrder(steps, edges) {
  const byId = new Map(steps.map((s) => [s.id, s]))
  const orderRank = new Map(steps.map((s) => [s.id, s.step_order ?? 0]))
  const indeg = new Map(steps.map((s) => [s.id, 0]))
  const adj = new Map(steps.map((s) => [s.id, []]))
  for (const e of edges || []) {
    if (!byId.has(e.from_step_id) || !byId.has(e.to_step_id)) continue
    if ((orderRank.get(e.to_step_id) ?? 0) < (orderRank.get(e.from_step_id) ?? 0)) continue // loop
    adj.get(e.from_step_id).push(e.to_step_id)
    indeg.set(e.to_step_id, indeg.get(e.to_step_id) + 1)
  }
  const ready = steps
    .filter((s) => indeg.get(s.id) === 0)
    .sort((a, b) => (a.step_order ?? 0) - (b.step_order ?? 0) || a.id - b.id)
    .map((s) => s.id)
  const out = []
  const seen = new Set()
  while (ready.length) {
    const id = ready.shift()
    if (seen.has(id)) continue
    seen.add(id)
    out.push(byId.get(id))
    const next = adj
      .get(id)
      .filter((t) => {
        indeg.set(t, indeg.get(t) - 1)
        return indeg.get(t) === 0
      })
      .sort((a, b) => (orderRank.get(a) ?? 0) - (orderRank.get(b) ?? 0) || a - b)
    ready.unshift(...next)
  }
  // Append any steps unreached by the topo walk (cycles / detached), by order.
  if (out.length < steps.length) {
    for (const s of [...steps].sort(
      (a, b) => (a.step_order ?? 0) - (b.step_order ?? 0) || a.id - b.id,
    )) {
      if (!seen.has(s.id)) {
        out.push(s)
        seen.add(s.id)
      }
    }
  }
  return out
}

// Re-map raw edges onto the given nodes. `stepToNode` maps a raw step id to its
// node id. Intra-node edges are dropped; boundary edges are deduped and tagged
// with flow indices.
function remapEdges(nodes, edges, stepToNode) {
  const nodeById = new Map(nodes.map((n) => [n.id, n]))
  const seenEdge = new Set()
  const out = []
  for (const e of edges || []) {
    const fromNode = stepToNode.get(e.from_step_id)
    const toNode = stepToNode.get(e.to_step_id)
    if (fromNode == null || toNode == null) continue
    if (fromNode === toNode) continue
    const key = `${fromNode}->${toNode}:${e.label || ''}`
    if (seenEdge.has(key)) continue
    seenEdge.add(key)
    out.push({
      from_id: fromNode,
      to_id: toNode,
      from_order: nodeById.get(fromNode).flowIndex,
      to_order: nodeById.get(toNode).flowIndex,
      label: e.label || null,
      is_branch: !!e.is_branch,
    })
  }
  return out
}

function indexNodes(nodes) {
  const stepToNode = new Map()
  nodes.forEach((n, i) => {
    n.flowIndex = i
    const members = n.kind === 'block' ? n.steps : [n.step]
    for (const m of members) stepToNode.set(m.id, n.id)
  })
  return stepToNode
}

export function collapseSteps(steps, edges) {
  const ordered = flowOrder(steps || [], edges || [])
  const nodes = []
  for (const s of ordered) {
    const prev = nodes[nodes.length - 1]
    if (
      s.step_type === 'agent' &&
      prev &&
      prev.kind === 'block' &&
      prev.workerKey === stepWorkerKey(s)
    ) {
      prev.steps.push(s)
    } else if (s.step_type === 'agent') {
      nodes.push({
        kind: 'block',
        id: s.id,
        step_type: 'agent',
        workerKey: stepWorkerKey(s),
        agent_service_name: s.agent_service_name,
        agent_id: s.agent_id,
        steps: [s],
      })
    } else {
      nodes.push({
        kind: 'single',
        id: s.id,
        step_type: s.step_type,
        workerKey: stepWorkerKey(s),
        step: s,
      })
    }
  }
  const stepToNode = indexNodes(nodes)
  return { nodes, edges: remapEdges(nodes, edges, stepToNode) }
}

// "All steps" mode: every raw step becomes its own single node (no collapsing).
export function expandSteps(steps, edges) {
  const ordered = flowOrder(steps || [], edges || [])
  const nodes = ordered.map((s) => ({
    kind: 'single',
    id: s.id,
    step_type: s.step_type,
    workerKey: stepWorkerKey(s),
    step: s,
  }))
  const stepToNode = indexNodes(nodes)
  return { nodes, edges: remapEdges(nodes, edges, stepToNode) }
}
