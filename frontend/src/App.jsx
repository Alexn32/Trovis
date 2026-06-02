import { useEffect, useRef, useState } from 'react'
import { ThemeProvider, useTheme } from './ThemeProvider.jsx'
import Fleet from './Fleet.jsx'
import AgentDetail from './AgentDetail.jsx'
import Ask from './Ask.jsx'
import AddAgent from './AddAgent.jsx'
import Login from './Login.jsx'
import Team from './Team.jsx'
import Workflows from './Workflows.jsx'
import Settings from './Settings.jsx'
import {
  api,
  clearApiKey,
  getApiKey,
  clearSessionToken,
  getSessionToken,
} from './api.js'
import {
  MonitorIcon,
  MoonIcon,
  PlusIcon,
  SunIcon,
} from './Icons.jsx'

// Top-level shell.
//   - ThemeProvider wraps everything; writes data-theme to <html>.
//   - Auth gate: no valid credential → Login; otherwise the dashboard.
//   - `me` ({user, org, auth}) is the resolved identity. Humans hold a
//     session token; agents/legacy hold an API key (user is null then).

// One-time read of an /accept-invite?token=… deep link.
function readInviteToken() {
  try {
    const u = new URL(window.location.href)
    if (u.pathname.replace(/\/+$/, '').endsWith('/accept-invite')) {
      return u.searchParams.get('token')
    }
  } catch {
    /* ignore */
  }
  return null
}

export default function App() {
  return (
    <ThemeProvider>
      <AppInner />
    </ThemeProvider>
  )
}

function AppInner() {
  const inviteToken = useRef(readInviteToken()).current
  const hadCredential = getSessionToken() || getApiKey()
  const [me, setMe] = useState(null)
  const [restoring, setRestoring] = useState(!!hadCredential && !inviteToken)
  const [tab, setTab] = useState('fleet') // 'fleet' | 'ask' | 'team' | 'workflows'
  // Overlays: {kind:'detail', serviceName, agentId?} | {kind:'add'} | {kind:'settings'}
  const [overlay, setOverlay] = useState(null)

  // Validate the saved credential on first mount (skip when landing on an
  // invite link — the visitor should see the accept form first).
  useEffect(() => {
    if (!hadCredential || inviteToken) return
    let cancelled = false
    api
      .validateSession()
      .then((payload) => {
        if (cancelled) return
        if (!payload) {
          clearSessionToken()
          clearApiKey()
          setMe(null)
        } else {
          setMe(payload)
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

  function handleAuthed(payload) {
    // Clear an invite/deep-link URL so a refresh doesn't re-trigger it.
    try {
      if (inviteToken) window.history.replaceState({}, '', '/')
    } catch {
      /* ignore */
    }
    setMe(payload)
  }

  async function logout() {
    try {
      await api.logout()
    } catch {
      /* best-effort */
    }
    clearSessionToken()
    clearApiKey()
    setMe(null)
    setTab('fleet')
    setOverlay(null)
  }

  function openDetail(serviceName, agentId) {
    setOverlay({ kind: 'detail', serviceName, agentId })
  }
  function openAddAgent() {
    setOverlay({ kind: 'add' })
  }
  function openSettings() {
    setOverlay({ kind: 'settings' })
  }
  function closeOverlay() {
    setOverlay(null)
  }

  async function refreshMe() {
    const payload = await api.validateSession()
    if (payload) setMe(payload)
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

  if (!me) {
    return (
      <Login
        onAuthed={handleAuthed}
        initialMode={inviteToken ? 'accept-invite' : 'choose'}
        inviteToken={inviteToken}
      />
    )
  }

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
  } else if (overlay?.kind === 'settings') {
    mainContent = <Settings me={me} onClose={closeOverlay} onUpdated={refreshMe} />
  } else if (tab === 'ask') {
    mainContent = <Ask />
  } else if (tab === 'team') {
    mainContent = <Team onSelectAgent={openDetail} />
  } else if (tab === 'workflows') {
    mainContent = <Workflows onSelectAgent={openDetail} />
  } else {
    mainContent = <Fleet onSelectAgent={openDetail} onAddAgent={openAddAgent} />
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
        me={me}
        onLogout={logout}
        onOpenSettings={openSettings}
      />
      <main className="app-main">{mainContent}</main>
    </div>
  )
}

function Header({ tab, onTabChange, onAddAgent, me, onLogout, onOpenSettings }) {
  const tabs = [
    ['fleet', 'Fleet'],
    ['ask', 'Ask'],
    ['team', 'Team'],
    ['workflows', 'Workflows'],
  ]
  return (
    <header className="app-header">
      <div className="app-header-left">
        <div className="brand">
          <span className="brand-dot" />
          Oversee
        </div>
        <nav className="tabs" role="tablist" aria-label="Views">
          {tabs.map(([id, label]) => (
            <button
              key={id}
              type="button"
              role="tab"
              aria-selected={tab === id}
              className={`tab ${tab === id ? 'tab-active' : ''}`}
              onClick={() => onTabChange(id)}
            >
              {label}
            </button>
          ))}
        </nav>
      </div>
      <div className="app-header-right">
        <ThemeToggle />
        <button type="button" className="btn btn-primary" onClick={onAddAgent}>
          <PlusIcon /> Add Agent
        </button>
        <AccountBadge me={me} onLogout={onLogout} onOpenSettings={onOpenSettings} />
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

function AccountBadge({ me, onLogout, onOpenSettings }) {
  const [open, setOpen] = useState(false)
  const ref = useRef(null)

  useEffect(() => {
    function onDoc(e) {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false)
    }
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [])

  const user = me?.user
  const org = me?.org
  const label = user ? user.name || user.email : 'Connected'
  const initials = (user?.name || user?.email || '?')
    .split(/[\s@]+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((s) => s[0].toUpperCase())
    .join('')

  return (
    <div className="account-menu" ref={ref}>
      <button
        type="button"
        className="account-badge-btn"
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="menu"
        aria-expanded={open}
      >
        <span className="account-avatar">{user ? initials : '•'}</span>
        <span className="account-badge-label">{label}</span>
      </button>
      {open && (
        <div className="account-dropdown" role="menu">
          <div className="account-dropdown-head">
            {user ? (
              <>
                <div className="account-dropdown-name">{user.name || user.email}</div>
                <div className="account-dropdown-sub">{user.email}</div>
              </>
            ) : (
              <div className="account-dropdown-name">API-key session</div>
            )}
            {org && (
              <div className="account-dropdown-org">
                {org.name || org.email}
                <span className="account-org-type">{org.account_type}</span>
              </div>
            )}
          </div>
          {user && (
            <button
              type="button"
              className="account-dropdown-item"
              onClick={() => {
                setOpen(false)
                onOpenSettings()
              }}
            >
              Settings
            </button>
          )}
          <button type="button" className="account-dropdown-item" onClick={onLogout}>
            Log out
          </button>
        </div>
      )}
    </div>
  )
}
