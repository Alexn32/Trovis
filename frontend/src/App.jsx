import { useState } from 'react'
import AgentRegistry from './AgentRegistry.jsx'
import AgentDetail from './AgentDetail.jsx'
import AddAgent from './AddAgent.jsx'
import Login from './Login.jsx'
import { clearApiKey, getApiKey } from './api.js'

// Top-level routing + auth gate.
//   authed = null    → show <Login />
//   authed = '<key>' → show the dashboard, key already set on api client
//
// The key is held in React state only (in-memory). Reload = re-login.
// VITE_OVERSEE_API_KEY (if set at build time) seeds the initial state so
// staging deploys can skip the login screen.
export default function App() {
  const [authed, setAuthed] = useState(getApiKey())
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
  function logout() {
    clearApiKey()
    setAuthed(null)
    setView('registry')
    setSelected(null)
  }

  if (!authed) {
    return <Login onAuthed={(key) => setAuthed(key)} />
  }

  return (
    <div className="app">
      <header className="header header-with-account">
        <div>
          <h1 className="logo">Oversee</h1>
          <p className="subtitle">Agent Management System</p>
        </div>
        <AccountBadge apiKey={authed} onLogout={logout} />
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

function AccountBadge({ apiKey, onLogout }) {
  // Show a 4-char tail of the key so the user can visually confirm which
  // account they're on without exposing the whole secret in the UI.
  const tail = apiKey ? apiKey.slice(-4) : '----'
  return (
    <div className="account-badge">
      <span className="account-dot" />
      <span className="account-label">
        Connected <code className="account-tail">…{tail}</code>
      </span>
      <button type="button" className="btn btn-link" onClick={onLogout}>
        Log out
      </button>
    </div>
  )
}
