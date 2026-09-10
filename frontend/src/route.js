// URLs for a pane-based app.
//
// Trovis has never had a router: every tab is mounted at sign-in and kept
// alive (#130), and navigation is local view state. That is a good fit for
// tabs — switching back to Fleet should not refetch it — and a bad fit for a
// job or a run, which are things people want to link to, reopen tomorrow, and
// walk back out of with the browser's own Back button.
//
// So this is not a router framework. It is a two-way translation between the
// address bar and the view state the panes already hold, plus the two History
// calls that keep them in step. Nothing here owns state; App and WorkTab still
// do. If this file disappeared the app would keep working with stale URLs.
//
// Vercel already rewrites /(.*) -> /index.html, so a deep link is served.

/** Tabs, in the order the nav shows them. `dashboard` lives at the root. */
export const TAB_PATHS = { dashboard: '/', fleet: '/fleet', team: '/team', work: '/work' }
const PATH_TABS = { '': 'dashboard', fleet: 'fleet', team: 'team', work: 'work' }

/**
 * Read a pathname into view state.
 *
 * Returns `{ tab, job, run }` — `job` and `run` are ids, or null. An
 * unrecognised path lands on the dashboard rather than a 404 screen: the app
 * has no route table to be missing from, and a blank pane is worse than the
 * home page.
 */
export function parsePath(pathname) {
  const parts = String(pathname || '/').split('?')[0].split('/').filter(Boolean)
  const tab = PATH_TABS[parts[0] || ''] || 'dashboard'
  if (tab !== 'work') return { tab, job: null, run: null }
  // /work/jobs/:id and /work/runs/:id. An id that is not a positive integer
  // is not an id — fall back to the board rather than fetching nonsense.
  const [, kind, raw] = parts
  const id = /^\d+$/.test(String(raw || '')) ? Number(raw) : null
  if (kind === 'jobs' && id) return { tab: 'work', job: id, run: null }
  if (kind === 'runs' && id) return { tab: 'work', job: null, run: id }
  return { tab: 'work', job: null, run: null }
}

/** The inverse: view state -> pathname. */
export function buildPath({ tab = 'dashboard', job = null, run = null } = {}) {
  if (tab === 'work') {
    if (run) return `/work/runs/${run}`
    if (job) return `/work/jobs/${job}`
    return '/work'
  }
  return TAB_PATHS[tab] || '/'
}

/**
 * Point the address bar at this view, without reloading.
 *
 * `replace` is for corrections that should not cost the user a Back press —
 * normalising a path we did not recognise, say. Everything a person clicks is
 * a push, so Back means what they expect.
 *
 * A no-op when the URL already matches, so re-rendering never litters history
 * with duplicate entries.
 */
export function navigate(view, { replace = false } = {}) {
  if (typeof window === 'undefined' || !window.history) return
  const next = buildPath(view)
  if (next === window.location.pathname) return
  window.history[replace ? 'replaceState' : 'pushState']({}, '', next)
}

/** Current view, read from the address bar. */
export function currentView() {
  if (typeof window === 'undefined') return { tab: 'dashboard', job: null, run: null }
  return parsePath(window.location.pathname)
}

/**
 * Call `fn` with the new view whenever the user goes Back or Forward.
 * Returns the unsubscribe. Only popstate — a pushState we made ourselves is
 * already reflected in the state that caused it.
 */
export function onPopState(fn) {
  if (typeof window === 'undefined') return () => {}
  const handler = () => fn(currentView())
  window.addEventListener('popstate', handler)
  return () => window.removeEventListener('popstate', handler)
}
