import { useState } from 'react'
import AgentRegistry from './AgentRegistry.jsx'
import AgentDetail from './AgentDetail.jsx'
import AddAgent from './AddAgent.jsx'

// Three-view SPA. No router — view state lives here.
//   view='registry'  → list of agents
//   view='detail'    → one agent's detail page (selected holds the service name)
//   view='addAgent'  → onboarding wizard
export default function App() {
  const [view, setView] = useState('registry')
  const [selected, setSelected] = useState(null)

  function showRegistry() {
    setView('registry')
    setSelected(null)
  }
  function showDetail(name) {
    setSelected(name)
    setView('detail')
  }
  function showAddAgent() {
    setView('addAgent')
  }

  return (
    <div className="app">
      <header className="header">
        <h1 className="logo">Oversee</h1>
        <p className="subtitle">Agent Management System</p>
      </header>
      <main className="main">
        {view === 'addAgent' && <AddAgent onClose={showRegistry} />}
        {view === 'detail' && (
          <AgentDetail serviceName={selected} onBack={showRegistry} />
        )}
        {view === 'registry' && (
          <AgentRegistry onSelect={showDetail} onAddAgent={showAddAgent} />
        )}
      </main>
    </div>
  )
}
