import { useEffect, useState } from 'react'
import AgentRegistry from './AgentRegistry.jsx'
import AgentDetail from './AgentDetail.jsx'
import AddAgent from './AddAgent.jsx'
import Login from './Login.jsx'
import { api, clearApiKey, getApiKey } from './api.js'

// Top-level routing + auth gate.
//   authed = null    → show <Login />
//   authed = '<key>' → show the dashboard, key already set on api client
//
// The key lives in localStorage (persists across reloads). On mount we
// validate any saved key against /agents — if it 401s we clear storage
// and drop to the login screen. `restoring` covers the brief window
// between mount and validation completing so we don't flash the
// dashboard with a stale key.
export default function App() {
  const initial = getApiKey()
  const [authed, setAuthed] = useState(initial)
  // Only need to "restore" if we already have a key to validate. Fresh
  // visitors with no saved key go straight to the login screen.
  const [restoring, setRestoring] = useState(initial !== null)
  const [view, setView] = useState('registry')
  const [selected, setSelected] = useState(null)

  useEffect(() => {
    if (!initial) return
    let cancelled = false
    api
      .validateCurrentKey()
      .then((ok) => {
        if (cancelled) return
        if (!ok) {
          // Saved key got revoked/rotated since last visit. Wipe and
          // send the user to the login screen.
          clearApiKey()
          setAuthed(null)
        }
        setRestoring(false)
      })
      .catch(() => {
        // Network error etc. — don't lock the user out, let them through
        // and the dashboard's own error states will surface the problem.
        if (!cancelled) setRestoring(false)
      })
    return () => {
      cancelled = true
    }
    // One-shot on mount: subsequent logins/logouts set state directly,
    // they don't need re-validation.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

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

  if (restoring) {
    return (
      <div className="login-shell">
        <div className="login-card">
          <header className="login-header">
            <h1 className="logo">Oversee</h1>
            <p className="subtitle">Agent Management System</p>
          </header>
          <div className="login-body">
            <p className="login-prompt">Restoring session…</p>
          </div>
        </div>
      </div>
    )
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
