import { useEffect, useRef, useState } from 'react'
import { ThemeProvider, useTheme } from './ThemeProvider.jsx'
import Dashboard from './Dashboard.jsx'
import CostPage from './CostPage.jsx'
import WorkFeedPage from './WorkFeedPage.jsx'
import WorkTab from './WorkTab.jsx'
import WorkflowPage from './WorkflowPage.jsx'
import WorkflowEditor from './WorkflowEditor.jsx'
import Fleet from './Fleet.jsx'
import AgentDetail from './AgentDetail.jsx'
import AskPill from './AskPill.jsx'
import AddAgent from './AddAgent.jsx'
import Login from './Login.jsx'
import TrovisLanding from './TrovisLanding.jsx'
import TrovisLegal from './TrovisLegal.jsx'
import UpgradeModal from './UpgradeModal.jsx'
import Team from './Team.jsx'
import Settings from './Settings.jsx'
import Onboarding from './Onboarding.jsx'
import {
  api,
  clearApiKey,
  getApiKey,
  clearSessionToken,
  getSessionToken,
} from './api.js'
import { restoreSession } from './sessionRestore.js'
import { performLogout } from './sessionLogout.js'
import { isPaneVisible, nextRosterEpoch } from './tabs.js'
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

// A password-reset link lands at `/?reset=<token>` (see main.forgot_password).
// Read it once at mount; presence routes straight to the reset form.
function readResetToken() {
  try {
    return new URL(window.location.href).searchParams.get('reset')
  } catch {
    return null
  }
}

// Public legal pages served at /terms and /privacy (no auth). The app has no
// router, so we detect the path at mount and short-circuit to the legal view.
function readLegalPath() {
  try {
    const p = new URL(window.location.href).pathname.replace(/\/+$/, '')
    if (p === '/terms') return 'terms'
    if (p === '/privacy') return 'privacy'
  } catch {
    /* ignore */
  }
  return null
}

// The current view (tab + overlay) lives in React state, not the URL — so a
// browser reload would otherwise reset to Work. Persist it to
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
  const resetToken = useRef(readResetToken()).current
  const legalPath = useRef(readLegalPath()).current
  const hadCredential = getSessionToken() || getApiKey()
  const [me, setMe] = useState(null)
  const [restoring, setRestoring] = useState(!!hadCredential && !inviteToken && !resetToken)
  // True when /auth/me timed out or the network failed. Token is KEPT so Retry
  // can reuse it; `restoring` is cleared so this cannot spin forever.
  const [restoreFailed, setRestoreFailed] = useState(false)
  // Logged-out front door: show the marketing landing first, then the Login
  // flow when the visitor clicks a CTA. authMode picks which Login panel opens.
  const [authView, setAuthView] = useState('landing') // 'landing' | 'auth'
  const [upgradeOpen, setUpgradeOpen] = useState(false) // plan-picker → Stripe
  const [authMode, setAuthMode] = useState('signup')  // 'signup' | 'login'
  // Restore the last view on mount so a browser reload stays put (see VIEW_KEY).
  const persistedView = useRef(readPersistedView()).current
  // The old "Workflows" and "Stuck" tabs consolidated into "Work" — remap any
  // stale persisted view from before the change (Stuck keeps its sub-view).
  const legacyTab = persistedView.tab
  const initialTab =
    legacyTab === 'workflows' || legacyTab === 'stuck' ? 'work' : legacyTab || 'work'
  const [tab, setTab] = useState(initialTab) // 'dashboard' | 'fleet' | 'team' | 'work'
  // Work tab sub-view: 'loops' | 'workflow' | 'stuck'
  const [workView, setWorkView] = useState(
    persistedView.workView || (legacyTab === 'stuck' ? 'stuck' : 'loops'),
  )
  // Overlays: {kind:'detail', serviceName, agentId?} | {kind:'add'} | {kind:'settings'} | {kind:'cost'} | {kind:'workfeed'}
  const [overlay, setOverlay] = useState(persistedView.overlay || null)

  // Roster invalidation for the keep-alive panes (see the pane block below).
  // Dashboard and Fleet both list agents and both used to refetch simply
  // because a tab switch remounted them. They no longer unmount, so an agent
  // added or deleted elsewhere in the shell has to be announced: bumping a
  // pane's epoch remounts it once — and `shownEpoch` defers that remount to
  // the moment the pane is next on screen, so an invalidated background pane
  // never refetches behind the user's back. `source` is the pane that made
  // the change; it already shows the result and is left alone.
  // Filter a Home card navigated in with, e.g. the Work card's "Stuck" tile.
  // Carries a nonce so re-clicking the same tile re-applies it — the Work pane
  // is kept alive, so an unchanged value would be a no-op.
  const [workFilter, setWorkFilter] = useState(null)

  const [rosterEpoch, setRosterEpoch] = useState({ dashboard: 0, fleet: 0 })
  const shownEpoch = useRef({ dashboard: 0, fleet: 0 })
  function rosterChanged(source) {
    setRosterEpoch((prev) => nextRosterEpoch(prev, source))
  }

  // Persist the current view (tab + overlay + Work sub-view) on every change
  // so a reload returns here instead of the Dashboard.
  useEffect(() => {
    persistView({ tab, overlay, workView })
  }, [tab, overlay, workView])

  // Validate the saved credential on first mount (skip when landing on an
  // invite link — the visitor should see the accept form first).
  //
  // Bug: `validateSession` → GET /auth/me used a bare fetch with no
  // AbortSignal. `restoring` only cleared when that promise settled, so a
  // hung Railway/API left the shell on "Restoring session..." forever.
  // `validateSession` now aborts at RESTORE_TIMEOUT_MS (10s); timeout /
  // network fail-softs (one retry with backoff) and never spins.
  function applyRestore(decision) {
    if (decision.clearCredentials) {
      clearSessionToken()
      clearApiKey()
    }
    setMe(decision.me)
    setRestoring(false)
    setRestoreFailed(decision.restoreFailed)
    if (decision.signedOut) {
      setAuthView('auth')
      setAuthMode('login')
    }
  }

  function runRestore() {
    setRestoreFailed(false)
    setRestoring(true)
    return restoreSession({ validate: () => api.validateSession() }).then(applyRestore)
  }

  useEffect(() => {
    if (!hadCredential || inviteToken) return
    let cancelled = false
    restoreSession({ validate: () => api.validateSession() }).then((decision) => {
      if (!cancelled) applyRestore(decision)
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

  function logout() {
    // Abort in-flight Dashboard GETs (briefing up to 120s) and do not await
    // the logout POST — otherwise Log out waits on a hung briefing.
    performLogout({
      logoutRequest: () => api.logout(),
      clearSessionToken,
      clearApiKey,
      clearPersistedView,
      // Hard navigate to a clean root: this drops any stale ?reset=/invite token
      // held in the mount-time refs, so logging out after a password reset lands
      // on the landing/login instead of re-showing the consumed reset form.
      navigate: () => window.location.assign('/'),
    })
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

  // Public legal pages (/terms, /privacy) — no auth, no session restore.
  if (legalPath) {
    return <TrovisLegal page={legalPath} />
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

  // API unreachable (timeout / network). Token is still in localStorage —
  // Retry re-runs validateSession; Sign in opens the login form.
  if (restoreFailed && !me) {
    return (
      <div className="login-shell">
        <div className="login-card">
          <header className="login-header">
            <TrovisLogo />
          </header>
          <div className="login-body">
            <p className="login-prompt">Can't reach Trovis — retry</p>
            <p className="login-note">
              The API didn't respond. Your session is still saved — retry, or sign in again.
            </p>
            <div className="login-actions">
              <button type="button" className="btn btn-primary btn-block" onClick={runRestore}>
                Retry
              </button>
              <button
                type="button"
                className="btn btn-link"
                onClick={() => setRestoreFailed(false)}
              >
                Sign in
              </button>
            </div>
          </div>
        </div>
      </div>
    )
  }

  if (!me) {
    // An invite or password-reset link goes straight to its flow (skip the
    // landing). Otherwise: the landing page is the front door; its CTAs open Login.
    const deepLink = inviteToken || resetToken
    if (!deepLink && authView === 'landing') {
      return (
        <TrovisLanding
          onGetStarted={() => { setAuthMode('signup'); setAuthView('auth') }}
          onSignIn={() => { setAuthMode('login'); setAuthView('auth') }}
        />
      )
    }
    const initialMode = inviteToken
      ? 'accept-invite'
      : resetToken
        ? 'reset'
        : authMode
    return (
      <Login
        onAuthed={handleAuthed}
        initialMode={initialMode}
        inviteToken={inviteToken}
        resetToken={resetToken}
        onBackToLanding={deepLink ? undefined : () => setAuthView('landing')}
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

  // ---- Overlays -----------------------------------------------------------
  // Built first, and rendered in place of the tab panes: agent detail, add
  // agent, settings, cost, work feed and the workflow views cover the main
  // content exactly as they did before keep-alive. An overlay object with an
  // unrecognised kind leaves overlayContent null and falls through to the
  // panes, same as the old if/else chain.
  let overlayContent = null
  if (overlay?.kind === 'detail') {
    overlayContent = (
      <AgentDetail
        serviceName={overlay.serviceName}
        agentId={overlay.agentId}
        account={account}
        onBack={closeOverlay}
        // Deleting the agent closes the overlay and invalidates the panes
        // that list agents, so neither keeps rendering the deleted row.
        onDeleted={() => {
          rosterChanged('overlay')
          closeOverlay()
        }}
        onUpgrade={openUpgrade}
      />
    )
  } else if (overlay?.kind === 'add') {
    // The wizard has no success callback, so closing it counts as a possible
    // roster change — cheap, since it's a deliberate one-off action, and it
    // keeps the first agent from landing behind a stale "no agents yet".
    overlayContent = (
      <AddAgent
        onClose={() => {
          rosterChanged('overlay')
          closeOverlay()
        }}
        onUpgrade={openUpgrade}
      />
    )
  } else if (overlay?.kind === 'settings') {
    overlayContent = <Settings me={me} onClose={closeOverlay} onUpdated={refreshMe} onUpgrade={openUpgrade} />
  } else if (overlay?.kind === 'cost') {
    overlayContent = <CostPage onBack={closeOverlay} onOpenAgent={openDetail} />
  } else if (overlay?.kind === 'workfeed') {
    overlayContent = (
      <WorkFeedPage
        onBack={closeOverlay}
        onOpenAgent={openDetail}
        sessionUser={Boolean(me?.user)}
      />
    )
  } else if (overlay?.kind === 'workflow') {
    overlayContent = (
      <WorkflowPage
        workflowId={overlay.id}
        onBack={closeOverlay}
        onEdit={(wf) => setOverlay({ kind: 'workflow-edit', id: wf.id })}
        onOpenAgent={openDetail}
        sessionUser={Boolean(me?.user)}
      />
    )
  } else if (overlay?.kind === 'workflow-new' || overlay?.kind === 'workflow-edit') {
    overlayContent = (
      <WorkflowEditorLoader
        workflowId={overlay.kind === 'workflow-edit' ? overlay.id : null}
        onBack={closeOverlay}
        onSaved={(id) => setOverlay({ kind: 'workflow', id })}
      />
    )
  }
  const overlayOpen = Boolean(overlayContent)

  // ---- Keep-alive tab panes ----------------------------------------------
  // Dashboard / Fleet / Work (+ Team on business orgs) are all rendered here,
  // unconditionally, and mounted for as long as the user is signed in. Tab
  // switching only flips which pane is visible (`hidden` + aria-hidden +
  // inert, see TabPane) — it never unmounts a page.
  //
  // Why: these pages fetch in useEffect on mount (Dashboard's briefing is a
  // Claude call up to 120s). Rendering one at a time meant every tab switch
  // unmounted the old page, aborted its requests, and re-ran the new page's
  // effects from scratch — so Dashboard ↔ Fleet ↔ Work felt like a full
  // reload and re-fetched the same data over and over. Staying mounted keeps
  // each page's state, scroll and last-good data. Each pane's mount fetches
  // now happen once, at sign-in, instead of on every visit; nothing polls
  // more often than before.
  //
  // If you add a tab, render its pane here too — never behind `tab === …`.
  const paneState = { tab, isBusiness, overlayOpen }
  const dashboardVisible = isPaneVisible('dashboard', paneState)
  const fleetVisible = isPaneVisible('fleet', paneState)
  const workVisible = isPaneVisible('work', paneState)
  // Take up a pending roster invalidation only while the pane is on screen,
  // so the refetch happens when the user arrives — not in the background.
  // (`key` remounts that one pane; the other panes are untouched.)
  if (dashboardVisible) shownEpoch.current.dashboard = rosterEpoch.dashboard
  if (fleetVisible) shownEpoch.current.fleet = rosterEpoch.fleet
  const panes = (
    <>
      <TabPane id="dashboard" visible={dashboardVisible}>
        <Dashboard
          key={`dashboard-${shownEpoch.current.dashboard}`}
          // Off screen, Home stops re-syncing on focus (same rule as Work).
          active={dashboardVisible}
          onOpenAgent={openDetail}
          onGoFleet={() => {
            setTab('fleet')
            setOverlay(null)
          }}
          // Home's cards preview real pages: a Work card or tile opens Work,
          // optionally filtered to the bucket that was clicked.
          onGoWork={(filter = null) => {
            setWorkFilter({ value: filter, nonce: Date.now() })
            setTab('work')
            setOverlay(null)
          }}
          onOpenCost={() => setOverlay({ kind: 'cost' })}
          onViewAllWorkFeed={() => setOverlay({ kind: 'workfeed' })}
          userName={account.userName}
        />
      </TabPane>
      <TabPane id="fleet" visible={fleetVisible}>
        <Fleet
          key={`fleet-${shownEpoch.current.fleet}`}
          onSelectAgent={openDetail}
          onAddAgent={openAddAgent}
          // Fleet deletes agents in place (optimistic). It doesn't need to
          // reload itself, but Dashboard's copy of the roster is now stale.
          onAgentsChanged={() => rosterChanged('fleet')}
          onUpgrade={openUpgrade}
        />
      </TabPane>
      {isBusiness && (
        <TabPane id="team" visible={isPaneVisible('team', paneState)}>
          <Team onSelectAgent={openDetail} />
        </TabPane>
      )}
      <TabPane id="work" visible={workVisible}>
        <WorkTab
          // Hidden panes stay mounted, so tell Work when it is off screen:
          // its background poll skips a tick instead of refetching for a
          // pane nobody is looking at (same rule it already applies to
          // document.hidden). No new polling is introduced here.
          active={workVisible}
          incomingFilter={workFilter}
          onConnectAgent={openAddAgent}
          onNewWorkflow={() => setOverlay({ kind: 'workflow-new' })}
          onOpenWorkflow={(id) => id && setOverlay({ kind: 'workflow', id })}
        />
      </TabPane>
    </>
  )

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
      <main className="app-main">
        {overlayContent}
        {panes}
      </main>
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

// One keep-alive tab pane. Its children mount once and stay mounted; only
// visibility changes. `hidden` takes the pane out of layout (the UA rule plus
// an explicit `.tab-pane[hidden]` rule in styles.css), aria-hidden takes it
// out of the accessibility tree, and `inert` keeps its buttons and inputs out
// of the tab order — a hidden pane must not be focusable. `inert` isn't a
// supported JSX attribute on React 18, so it's set on the node directly.
function TabPane({ id, visible, children }) {
  const ref = useRef(null)
  useEffect(() => {
    const el = ref.current
    if (el && 'inert' in el) el.inert = !visible
  }, [visible])
  return (
    <div
      ref={ref}
      id={`pane-${id}`}
      className="tab-pane"
      role="tabpanel"
      aria-labelledby={`tab-${id}`}
      hidden={!visible}
      aria-hidden={!visible}
    >
      {children}
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
    // Everything the agents are doing: loops, the by-workflow rollup, and
    // the Stuck filter — one tab, three zoom levels.
    ['work', 'Work'],
  ]
  return (
    <header className="app-header">
      <div className="app-header-left">
        <TrovisLogo />
        <nav className="tabs" role="tablist" aria-label="Views">
          {tabs.map(([id, label]) => (
            <button
              key={id}
              id={`tab-${id}`}
              type="button"
              role="tab"
              aria-selected={tab === id}
              aria-controls={`pane-${id}`}
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

// Fetches the workflow before rendering the editor in edit mode, so the
// form opens pre-filled with the current version's definition.
function WorkflowEditorLoader({ workflowId, onBack, onSaved }) {
  const [wf, setWf] = useState(workflowId ? null : undefined) // undefined = create mode
  useEffect(() => {
    if (!workflowId) return
    let alive = true
    api.getWorkflow(workflowId).then((w) => alive && setWf(w)).catch(() => alive && setWf(undefined))
    return () => { alive = false }
  }, [workflowId])
  if (workflowId && wf === null) {
    return <div className="dash"><div className="dash-skel"><span style={{ height: 200 }} /></div></div>
  }
  return <WorkflowEditor workflow={wf || null} onBack={onBack} onSaved={onSaved} />
}
