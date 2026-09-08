// Tab switches must not remount Dashboard / Fleet / Work: the panes are
// mounted once and only their visibility changes (App.jsx keep-alive).
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { isPaneVisible, nextRosterEpoch, resolveTab, TAB_IDS } from '../src/tabs.js'

const app = readFileSync(new URL('../src/App.jsx', import.meta.url), 'utf8')

test('exactly one pane is visible per tab, and the others only hide', () => {
  const state = { isBusiness: true, overlayOpen: false }
  for (const tab of TAB_IDS) {
    const visible = TAB_IDS.filter((id) => isPaneVisible(id, { ...state, tab }))
    assert.deepEqual(visible, [tab], `tab ${tab} shows only its own pane`)
  }
})

test('stale or unavailable tabs fall back to Fleet, as the old if/else did', () => {
  assert.equal(resolveTab('workflows'), 'fleet') // pre-consolidation persisted value
  assert.equal(resolveTab(undefined), 'fleet')
  // Individual accounts have no Team tab.
  assert.equal(resolveTab('team', { isBusiness: false }), 'fleet')
  assert.equal(resolveTab('team', { isBusiness: true }), 'team')
})

test('an open overlay hides every pane (overlays still cover main content)', () => {
  for (const tab of TAB_IDS) {
    for (const id of TAB_IDS) {
      assert.equal(isPaneVisible(id, { tab, isBusiness: true, overlayOpen: true }), false)
    }
  }
})

test('switching tabs twice cannot remount a pane: no pane sits behind a tab check', () => {
  // Each page is rendered exactly once, from a single unconditional <TabPane>.
  // If one of these ever moves back behind `tab === …`, the tab switch starts
  // unmounting again and every useEffect refetches.
  for (const page of ['Dashboard', 'Fleet', 'WorkTab']) {
    const mounts = app.match(new RegExp(`<${page}\\b`, 'g')) || []
    assert.equal(mounts.length, 1, `${page} is mounted in exactly one place`)
  }
  assert.doesNotMatch(app, /tab === 'dashboard'|tab === 'work'|tab === 'fleet'/)
  // Visibility comes from the pure helper above, not from swapping children.
  assert.match(app, /isPaneVisible\('dashboard', paneState\)/)
  assert.match(app, /isPaneVisible\('work', paneState\)/)
})

test('a hidden pane is out of layout, unfocusable and hidden from AT', () => {
  assert.match(app, /hidden=\{!visible\}/)
  assert.match(app, /aria-hidden=\{!visible\}/)
  assert.match(app, /el\.inert = !visible/)
  const css = readFileSync(new URL('../src/styles.css', import.meta.url), 'utf8')
  assert.match(css, /\.tab-pane\[hidden\]\s*\{\s*display:\s*none\s*!important;/)
})

test('an agent added or deleted elsewhere invalidates the panes that list agents', () => {
  const start = { dashboard: 0, fleet: 0 }
  // From an overlay (add agent, delete in agent detail): both reload.
  assert.deepEqual(nextRosterEpoch(start, 'overlay'), { dashboard: 1, fleet: 1 })
  // From Fleet's own optimistic delete: Fleet already shows it, Dashboard doesn't.
  assert.deepEqual(nextRosterEpoch(start, 'fleet'), { dashboard: 1, fleet: 0 })
  // A tab switch is not a roster change — nothing bumps, so nothing reloads.
  assert.deepEqual(start, { dashboard: 0, fleet: 0 })
  // The pane picks the new epoch up only while it is shown, so a hidden pane
  // doesn't refetch in the background.
  assert.match(app, /if \(dashboardVisible\) shownEpoch\.current\.dashboard = rosterEpoch\.dashboard/)
  assert.match(app, /if \(fleetVisible\) shownEpoch\.current\.fleet = rosterEpoch\.fleet/)
})

test('Home stops re-syncing on focus while its pane is hidden', () => {
  // Home v2 keeps six endpoints (briefing / attention / cost / work-feed /
  // work overview + items) and re-syncs them when the window regains focus.
  // Keep-alive leaves it mounted behind Work, so without the active gate that
  // re-sync fires for a pane nobody is looking at.
  const dash = readFileSync(new URL('../src/Dashboard.jsx', import.meta.url), 'utf8')
  assert.match(dash, /if \(document\.hidden \|\| !activeRef\.current\) return/)
  assert.match(app, /active=\{dashboardVisible\}/)
})

test('the Work poll skips its tick while the pane is hidden', () => {
  const work = readFileSync(new URL('../src/WorkTab.jsx', import.meta.url), 'utf8')
  assert.match(work, /failSoftRef\.current \|\| document\.hidden \|\| !activeRef\.current/)
  // Keep-alive must not add polling anywhere: the cadence is unchanged.
  assert.match(work, /const POLL_START_MS = 30000/)
  assert.match(work, /const POLL_MAX_MS = 120000/)
  assert.doesNotMatch(app, /setInterval/)
})
