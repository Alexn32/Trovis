// Worker identity colors — shared by WorkflowCanvas and WorkflowList so a worker
// reads the same color on the card and on the flow. Agents are colored by order
// of first appearance; humans are ALWAYS purple ("a person touches this" is a
// global Trovis convention).

export const HUMAN_COLOR = '#7c3aed'
export const AGENT_PALETTE = [
  '#5A7B7B', // brand teal — first agent
  '#6366f1', // indigo — second
  '#2a9d6e', // green — third
  '#d4792a', // amber — fourth
  '#b8860b', // dark gold — fifth
]

// Stable key for a worker. Agents key off service + agent_id; humans off role.
export function agentKey(serviceName, agentId) {
  return `${serviceName}:${agentId || 'main'}`
}
export function humanKey(roleName) {
  return `role:${(roleName || '').trim()}`
}

// Key for a step's worker (used when laying out canvas nodes).
export function stepWorkerKey(step) {
  if (step.step_type === 'human') {
    return humanKey((step.config && step.config.role_name) || step.team_member_name || step.label)
  }
  if (step.agent_service_name) return agentKey(step.agent_service_name, step.agent_id)
  return null
}

// Returns { [key]: color }. `participants` is the workflow's participant list
// ({type, agent_service_name, agent_id, role_name}). Agents get palette colors
// by order of appearance (wrapping if there are more than the palette length);
// humans always HUMAN_COLOR.
export function assignWorkerColors(participants) {
  const colors = {}
  let agentIdx = 0
  for (const p of participants || []) {
    if (p.type === 'human') {
      colors[humanKey(p.role_name)] = HUMAN_COLOR
    } else if (p.agent_service_name) {
      const k = agentKey(p.agent_service_name, p.agent_id)
      if (!(k in colors)) {
        colors[k] = AGENT_PALETTE[agentIdx % AGENT_PALETTE.length]
        agentIdx += 1
      }
    }
  }
  return colors
}
