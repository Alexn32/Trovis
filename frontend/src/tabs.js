// Tab → pane resolution for the keep-alive shell (see App.jsx).
//
// Home / Fleet / Work / Org are mounted once, for as long as the user is
// signed in, and shown or hidden with CSS. This module answers only "which
// pane is on screen right now" — never "which pane exists" — so switching
// tabs can't unmount a page and re-run its fetches.
//
// Which tabs a person is OFFERED now comes from their seat's surfaces
// (/auth/me), not from the account type. Hiding a tab is a courtesy: the API
// refuses anything the seat doesn't allow regardless, so a stale or missing
// seat costs nothing but a visible tab.

import { ALL_SURFACES } from './seat.js'

export const TAB_IDS = ['dashboard', 'fleet', 'work', 'org']

// Pane id → the surface name a seat uses for it. 'Ask' and 'Connect' are not
// tabs (Ask is the ⌘K pill, Connect lives inside Add Agent), so they never
// appear here; a seat that omits them still hides them where they surface.
export const PANE_SURFACE = {
  dashboard: 'Home',
  fleet: 'Fleet',
  work: 'Work',
  org: 'Org',
}

// The tabs to render, in product order, filtered to the seat's surfaces.
// An empty result would be a shell with no way to navigate, so it falls back
// to the full set — same rule as seat.js: widen, never blank.
export function visibleTabs(surfaces) {
  const allowed = new Set(
    Array.isArray(surfaces) && surfaces.length ? surfaces : ALL_SURFACES,
  )
  const tabs = [
    // The pane id stays 'dashboard' (routes, session-restore, TabPane ids);
    // what a person reads is Home, everywhere, always.
    ['dashboard', 'Home'],
    // The pane id and the scope atom both stay 'Fleet' (routes, TabPane ids,
    // scope_levels.surfaces); what a person reads is Agents.
    ['fleet', 'Agents'],
    ['work', 'Work'],
    ['org', 'Org'],
  ].filter(([id]) => allowed.has(PANE_SURFACE[id]))
  return tabs.length ? tabs : [['work', 'Work']]
}

// The pane a tab value resolves to. Falls back to the first tab the seat
// allows, so a stale persisted tab (or 'team', the pane this replaced) lands
// somewhere real instead of on a blank screen.
export function resolveTab(tab, { surfaces = null } = {}) {
  const tabs = visibleTabs(surfaces)
  const ids = tabs.map(([id]) => id)
  // 'team' was the old business-only directory; Org is where people live now.
  const wanted = tab === 'team' ? 'org' : tab
  if (ids.includes(wanted)) return wanted
  return ids.includes('work') ? 'work' : ids[0]
}

// Overlays (agent detail, add agent, settings, cost, work feed, workflows)
// replace the main content as they always have: while one is open every tab
// pane is hidden, but all of them stay mounted underneath, so closing the
// overlay returns to a pane that already has its data.
export function isPaneVisible(paneId, { tab, surfaces = null, overlayOpen = false } = {}) {
  if (overlayOpen) return false
  return resolveTab(tab, { surfaces }) === paneId
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
