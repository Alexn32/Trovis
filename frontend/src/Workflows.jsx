import { useState } from 'react'
import WorkflowList from './WorkflowList.jsx'
import WorkflowCanvas from './WorkflowCanvas.jsx'

// PARKED — unrouted since the Workflows tab consolidated into the Work tab;
// awaiting reintegration under Work's By-workflow rollup view.
// Workflows tab: a card list of workflows, or the spatial flow canvas for a
// selected one. (The agent-to-agent Connections Map lives in ConnectionsMap.jsx
// and is no longer surfaced here — workflow graphs are a distinct concept.)
export default function Workflows() {
  const [selectedId, setSelectedId] = useState(null)

  if (selectedId) {
    return (
      <WorkflowCanvas workflowId={selectedId} onBack={() => setSelectedId(null)} />
    )
  }
  return <WorkflowList onSelect={setSelectedId} />
}
