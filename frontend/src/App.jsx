import { useEffect, useRef, useState } from 'react'
import { ThemeProvider, useTheme } from './ThemeProvider.jsx'
import Dashboard from './Dashboard.jsx'
import CostPage from './CostPage.jsx'
import WorkFeedPage from './WorkFeedPage.jsx'
import Fleet from './Fleet.jsx'
import AgentDetail from './AgentDetail.jsx'
import AskPill from './AskPill.jsx'
import AddAgent from './AddAgent.jsx'
import Login from './Login.jsx'
import TrovisLanding from './TrovisLanding.jsx'
import UpgradeModal from './UpgradeModal.jsx'
import Team from './Team.jsx'
import Workflows from './Workflows.jsx'
import Settings from './Settings.jsx'
import Onboarding from './Onboarding.jsx'
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
  TrovisLogo,
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

// The current view (tab + overlay) lives in React state, not the URL — so a
// browser reload would otherwise reset to the Dashboard. Persist it to
// sessionStorage and restore on mount so reload keeps you on the page you were
// on. sessionStorage (not local) so it's scoped to the tab and cleared on
// logout; the URL is intentionally left unchanged (no router).
const VIEW_KEY = 'trovis_view'
function readPersistedView() {
  try {
    const v = JSON.parse(sessionStorage.getItem(VIEW_KEY) || '{}')
    return v && typeof v === 'object' ? v : {}
  } catch {
    return {}
  }
}
function persistView(view) {
  try {
    sessionStorage.setItem(VIEW_KEY, JSON.stringify(view))
  } catch {
    /* ignore */
  }
}
function clearPersistedView() {
  try {
    sessionStorage.removeItem(VIEW_KEY)
  } catch {
    /* ignore */
  }
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
  // Logged-out front door: show the marketing landing first, then the Login
  // flow when the visitor clicks a CTA. authMode picks which Login panel opens.
  const [authView, setAuthView] = useState('landing') // 'landing' | 'auth'
  const [upgradeOpen, setUpgradeOpen] = useState(false) // plan-picker → Stripe
  const [authMode, setAuthMode] = useState('signup')  // 'signup' | 'login'
  // Restore the last view on mount so a browser reload stays put (see VIEW_KEY).
  const persistedView = useRef(readPersistedView()).current
  const [tab, setTab] = useState(persistedView.tab || 'dashboard') // 'dashboard' | 'fleet' | 'team' | 'workflows'
  // Overlays: {kind:'detail', serviceName, agentId?} | {kind:'add'} | {kind:'settings'} | {kind:'cost'} | {kind:'workfeed'}
  const [overlay, setOverlay] = useState(persistedView.overlay || null)

  // Persist the current view (tab + overlay) on every change so a reload
  // returns here instead of the Dashboard.
  useEffect(() => {
    persistView({ tab, overlay })
  }, [tab, overlay])

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
    clearPersistedView()
    setMe(null)
    setTab('dashboard')
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
  // Opens the plan-picker modal → PUT /account/plan → Stripe Checkout. The plan
  // flips only after Stripe's webhook confirms payment (see billing.py).
  function openUpgrade() {
    setUpgradeOpen(true)
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
            <TrovisLogo />
          </header>
          <div className="login-body">
            <p className="login-prompt">Restoring session…</p>
          </div>
        </div>
      </div>
    )
  }

  if (!me) {
    // An invite link goes straight to the accept-invite flow (skip the landing).
    // Otherwise: the landing page is the front door; its CTAs open Login.
    if (!inviteToken && authView === 'landing') {
      return (
        <TrovisLanding
          onGetStarted={() => { setAuthMode('signup'); setAuthView('auth') }}
          onSignIn={() => { setAuthMode('login'); setAuthView('auth') }}
        />
      )
    }
    return (
      <Login
        onAuthed={handleAuthed}
        initialMode={inviteToken ? 'accept-invite' : authMode}
        inviteToken={inviteToken}
        onBackToLanding={inviteToken ? undefined : () => setAuthView('landing')}
      />
    )
  }

  // First-run onboarding: a brand-new org owner (session auth) hasn't been
  // through setup yet. Full-screen takeover until they finish or skip; one
  // POST persists `onboarded_at` so it never reappears. API-key sessions and
  // invited members never see it.
  const needsOnboarding =
    me?.auth === 'session' &&
    me?.user?.role === 'owner' &&
    !me?.org?.onboarded_at
  if (needsOnboarding) {
    return (
      <div className="app">
        <TextureOverlay />
        <Onboarding
          me={me}
          onDone={() => {
            setMe((prev) =>
              prev
                ? { ...prev, org: { ...prev.org, onboarded_at: new Date().toISOString() } }
                : prev
            )
            setTab('dashboard')
          }}
        />
      </div>
    )
  }

  // Individual accounts have no team to manage — agents are implicitly the
  // user's. Business accounts get the Team tab + per-agent owner assignment.
  const isBusiness = me?.org?.account_type === 'business'
  const account = {
    type: me?.org?.account_type,
    userName: me?.user?.name || me?.user?.email || null,
  }

  let mainContent
  if (overlay?.kind === 'detail') {
    mainContent = (
      <AgentDetail
        serviceName={overlay.serviceName}
        agentId={overlay.agentId}
        account={account}
        onBack={closeOverlay}
        onUpgrade={openUpgrade}
      />
    )
  } else if (overlay?.kind === 'add') {
    mainContent = <AddAgent onClose={closeOverlay} />
  } else if (overlay?.kind === 'settings') {
    mainContent = <Settings me={me} onClose={closeOverlay} onUpdated={refreshMe} onUpgrade={openUpgrade} />
  } else if (overlay?.kind === 'cost') {
    mainContent = <CostPage onBack={closeOverlay} onOpenAgent={openDetail} />
  } else if (overlay?.kind === 'workfeed') {
    mainContent = <WorkFeedPage onBack={closeOverlay} onOpenAgent={openDetail} />
  } else if (tab === 'dashboard') {
    mainContent = (
      <Dashboard
        onOpenAgent={openDetail}
        onGoFleet={() => setTab('fleet')}
        onOpenCost={() => setOverlay({ kind: 'cost' })}
        onViewAllWorkFeed={() => setOverlay({ kind: 'workfeed' })}
        userName={account.userName}
      />
    )
  } else if (tab === 'team' && isBusiness) {
    mainContent = <Team onSelectAgent={openDetail} />
  } else if (tab === 'workflows') {
    mainContent = <Workflows onSelectAgent={openDetail} />
  } else {
    mainContent = (
      <Fleet onSelectAgent={openDetail} onAddAgent={openAddAgent} onUpgrade={openUpgrade} />
    )
  }

  return (
    <div className="app">
      <TextureOverlay />
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
      {/* Global Trovis assistant — floating ⌘K pill, reachable on every page. */}
      <AskPill />
      <UpgradeModal
        open={upgradeOpen}
        me={me}
        onClose={() => setUpgradeOpen(false)}
        onApplied={refreshMe}
      />
    </div>
  )
}

// Paper-grain texture — a fixed, full-screen overlay rendered on every page,
// behind all content (z-index 0; content sits at z-index 1). The vignette
// color is theme-aware via --vignette (warm in light, black in dark).
function TextureOverlay() {
  return (
    <div
      aria-hidden="true"
      style={{ position: 'fixed', inset: 0, pointerEvents: 'none', zIndex: 0 }}
    >
      <svg width="100%" height="100%" style={{ position: 'absolute', inset: 0 }}>
        <filter id="grain-coarse">
          <feTurbulence type="fractalNoise" baseFrequency="0.65" numOctaves="4" stitchTiles="stitch" />
          <feColorMatrix type="saturate" values="0" />
        </filter>
        <rect width="100%" height="100%" filter="url(#grain-coarse)" opacity="0.035" />
      </svg>
      <svg width="100%" height="100%" style={{ position: 'absolute', inset: 0 }}>
        <filter id="grain-fine">
          <feTurbulence type="fractalNoise" baseFrequency="1.8" numOctaves="3" stitchTiles="stitch" />
          <feColorMatrix type="saturate" values="0" />
        </filter>
        <rect width="100%" height="100%" filter="url(#grain-fine)" opacity="0.025" />
      </svg>
      <div
        style={{
          position: 'absolute',
          inset: 0,
          background:
            'radial-gradient(ellipse at 50% 30%, transparent 50%, var(--vignette) 100%)',
        }}
      />
    </div>
  )
}

function Header({ tab, onTabChange, onAddAgent, me, onLogout, onOpenSettings }) {
  // The Team tab manages multi-person ownership — only meaningful for
  // Business orgs. Individual accounts own all their agents implicitly.
  const isBusiness = me?.org?.account_type === 'business'
  // No Ask tab — the global AskPill (⌘K) covers asking from every page.
  const tabs = [
    ['dashboard', 'Dashboard'],
    ['fleet', 'Fleet'],
    ...(isBusiness ? [['team', 'Team']] : []),
    ['workflows', 'Workflows'],
  ]
  return (
    <header className="app-header">
      <div className="app-header-left">
        <TrovisLogo />
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
