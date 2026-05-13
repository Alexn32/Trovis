import { useEffect, useState } from 'react'
import { ThemeProvider, useTheme } from './ThemeProvider.jsx'
import Fleet from './Fleet.jsx'
import AgentDetail from './AgentDetail.jsx'
import Ask from './Ask.jsx'
import AddAgent from './AddAgent.jsx'
import Login from './Login.jsx'
import { api, clearApiKey, getApiKey } from './api.js'
import {
  MonitorIcon,
  MoonIcon,
  PlusIcon,
  SunIcon,
} from './Icons.jsx'

// Top-level shell.
//   - ThemeProvider wraps everything and writes data-theme to <html>.
//   - Auth gate: null key → Login; otherwise the dashboard.
//   - The dashboard has three views (Fleet / Ask / AgentDetail / AddAgent)
//     tracked in local state. Fleet and Ask are tab-selectable; AgentDetail
//     and AddAgent are pushed on top of whichever tab is active.

export default function App() {
  return (
    <ThemeProvider>
      <AppInner />
    </ThemeProvider>
  )
}

function AppInner() {
  const initial = getApiKey()
  const [authed, setAuthed] = useState(initial)
  const [restoring, setRestoring] = useState(initial !== null)
  const [tab, setTab] = useState('fleet') // 'fleet' | 'ask'
  // Overlay variants:
  //   {kind: 'detail', serviceName, agentId?} — agentId set when drilling
  //     into a specific sub-agent of a multi-agent instance.
  //   {kind: 'add'}
  const [overlay, setOverlay] = useState(null)

  // Validate saved key on first mount.
  useEffect(() => {
    if (!initial) return
    let cancelled = false
    api
      .validateCurrentKey()
      .then((ok) => {
        if (cancelled) return
        if (!ok) {
          clearApiKey()
          setAuthed(null)
        }
        setRestoring(false)
      })
      .catch(() => {
        if (!cancelled) setRestoring(false)
      })
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  function logout() {
    clearApiKey()
    setAuthed(null)
    setTab('fleet')
    setOverlay(null)
  }

  function openDetail(serviceName, agentId) {
    setOverlay({ kind: 'detail', serviceName, agentId })
  }

  function openAddAgent() {
    setOverlay({ kind: 'add' })
  }

  function closeOverlay() {
    setOverlay(null)
  }

  if (restoring) {
    return (
      <div className="login-shell">
        <div className="login-card">
          <header className="login-header">
            <div className="brand">
              <span className="brand-dot" />
              Oversee
            </div>
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

  // Decide what to render in the main area. Overlays win over the tab.
  let mainContent
  if (overlay?.kind === 'detail') {
    mainContent = (
      <AgentDetail
        serviceName={overlay.serviceName}
        agentId={overlay.agentId}
        onBack={closeOverlay}
      />
    )
  } else if (overlay?.kind === 'add') {
    mainContent = <AddAgent onClose={closeOverlay} />
  } else if (tab === 'ask') {
    mainContent = <Ask />
  } else {
    mainContent = (
      <Fleet onSelectAgent={openDetail} onAddAgent={openAddAgent} />
    )
  }

  return (
    <div className="app">
      <Header
        tab={tab}
        onTabChange={(t) => {
          setTab(t)
          setOverlay(null)
        }}
        onAddAgent={openAddAgent}
        apiKey={authed}
        onLogout={logout}
      />
      <main className="app-main">{mainContent}</main>
    </div>
  )
}

function Header({ tab, onTabChange, onAddAgent, apiKey, onLogout }) {
  return (
    <header className="app-header">
      <div className="app-header-left">
        <div className="brand">
          <span className="brand-dot" />
          Oversee
        </div>
        <nav className="tabs" role="tablist" aria-label="Views">
          <button
            type="button"
            role="tab"
            aria-selected={tab === 'fleet'}
            className={`tab ${tab === 'fleet' ? 'tab-active' : ''}`}
            onClick={() => onTabChange('fleet')}
          >
            Fleet
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={tab === 'ask'}
            className={`tab ${tab === 'ask' ? 'tab-active' : ''}`}
            onClick={() => onTabChange('ask')}
          >
            Ask
          </button>
        </nav>
      </div>
      <div className="app-header-right">
        <ThemeToggle />
        <button type="button" className="btn btn-primary" onClick={onAddAgent}>
          <PlusIcon /> Add Agent
        </button>
        <AccountBadge apiKey={apiKey} onLogout={onLogout} />
      </div>
    </header>
  )
}

function ThemeToggle() {
  const { theme, cycle } = useTheme()
  const label = theme === 'system' ? 'System' : theme === 'light' ? 'Light' : 'Dark'
  const Icon =
    theme === 'system' ? MonitorIcon : theme === 'light' ? SunIcon : MoonIcon
  return (
    <button
      type="button"
      className="btn-icon"
      onClick={cycle}
      aria-label={`Theme: ${label}. Click to cycle.`}
      title={`Theme: ${label}`}
    >
      <Icon size={15} />
    </button>
  )
}

function AccountBadge({ apiKey, onLogout }) {
  const tail = apiKey ? apiKey.slice(-4) : '----'
  return (
    <div className="account-badge">
      <span className="account-dot" />
      <span>Connected</span>
      <code className="account-tail">…{tail}</code>
      <button type="button" className="account-logout" onClick={onLogout}>
        Log out
      </button>
    </div>
  )
}
