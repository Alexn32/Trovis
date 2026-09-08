// Tab → pane resolution for the keep-alive shell (see App.jsx).
//
// Dashboard / Fleet / Work (+ Team on business orgs) are mounted once, for as
// long as the user is signed in, and shown or hidden with CSS. This module
// answers only "which pane is on screen right now" — never "which pane
// exists" — so switching tabs can't unmount a page and re-run its fetches.

export const TAB_IDS = ['dashboard', 'fleet', 'team', 'work']

// The pane a tab value resolves to. 'fleet' is the fallback, matching the old
// if/else chain: an unknown or stale persisted tab (and 'team' on an
// individual account, which has no Team tab) lands on Fleet.
export function resolveTab(tab, { isBusiness = false } = {}) {
  if (tab === 'dashboard' || tab === 'work') return tab
  if (tab === 'team') return (isBusiness ? 'team' : 'fleet')
  return 'fleet'
}

// Overlays (agent detail, add agent, settings, cost, work feed, workflows)
// replace the main content as they always have: while one is open every tab
// pane is hidden, but all of them stay mounted underneath, so closing the
// overlay returns to a pane that already has its data.
export function isPaneVisible(paneId, { tab, isBusiness = false, overlayOpen = false } = {}) {
  if (overlayOpen) return false
  return resolveTab(tab, { isBusiness }) === paneId
}

// Roster invalidation. Dashboard and Fleet both list agents; a mounted pane
// can't notice an agent added or deleted somewhere else, so each carries an
// epoch and a bump means "reload the next time you're shown" (App.jsx keys
// the pane on it). `source` is the pane that made the change — it already
// shows the result, so its epoch is left where it is.
export const ROSTER_PANES = ['dashboard', 'fleet']

export function nextRosterEpoch(prev, source) {
  const next = {}
  for (const pane of ROSTER_PANES) {
    next[pane] = pane === source ? prev[pane] : prev[pane] + 1
  }
  return next
}
