// A render crash must not blank the app.
//
// Before this, nothing in the tree caught a render error, so React unmounted
// everything and the user got a white screen with no header and no way back —
// indistinguishable from the routing bug in agentRoute.test.mjs, and the
// reason "blank screen" was ambiguous to debug.
//
// There is no DOM or test renderer in this project, so these read the source
// the way the other suites here do. Behaviour was verified in the browser by
// throwing from a pane and confirming the crash pane rendered with the header
// and the other tabs still working.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const read = (f) => readFileSync(new URL(`../src/${f}`, import.meta.url), 'utf8')
const strip = (s) => s.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')

const boundary = strip(read('ErrorBoundary.jsx'))
const app = strip(read('App.jsx'))
const css = read('styles.css')

test('the boundary implements both halves of the React contract', () => {
  // getDerivedStateFromError renders the fallback; componentDidCatch is what
  // puts the stack somewhere a developer can find it. One without the other
  // either swallows the bug or fails to recover from it.
  assert.match(boundary, /static getDerivedStateFromError\(error\)/)
  assert.match(boundary, /componentDidCatch\(error, info\)/)
  assert.match(boundary, /console\.error\(/)
})

test('a healthy boundary is invisible', () => {
  // No error means children render untouched — no wrapper markup, no layout
  // shift on every page in the app.
  assert.match(boundary, /if \(!error\) return this\.props\.children/)
})

test('the fallback gives a way out, not just an apology', () => {
  // A dead-end error screen is the blank screen with extra steps.
  assert.match(boundary, /Try again/)
  assert.match(boundary, /window\.location\.reload\(\)/)
  assert.match(boundary, /role="alert"/)
  // "Try again" clears the error so the subtree can re-render in place.
  assert.match(boundary, /this\.setState\(\{ error: null \}\)/)
})

test('every tab pane is wrapped, including tabs added later', () => {
  // Wrapping inside TabPane rather than at each call site is the point: a new
  // tab cannot forget to opt in.
  const pane = app.slice(app.indexOf('function TabPane'))
  assert.match(pane, /<ErrorBoundary label=\{PANE_LABELS\[id\] \|\| id\}>\{children\}<\/ErrorBoundary>/)
  // And the boundary sits INSIDE the pane div, so the header and tab bar
  // survive a crash and the user can leave the broken tab.
  assert.ok(
    pane.indexOf('<ErrorBoundary') > pane.indexOf('<div'),
    'boundary must be inside the pane, not around it',
  )
})

test('the overlay has its own boundary, reset per overlay', () => {
  // A crashed agent page must not poison the next one, and closing it must
  // land on a working page rather than the same fallback.
  const main = app.slice(app.indexOf('<main className="app-main">'))
  assert.match(main, /<ErrorBoundary\s+key=\{`\$\{overlay\?\.kind/)
  assert.match(main, /onReset=\{closeOverlay\}/)
})

test('a crash in one tab leaves the others mounted', () => {
  // The panes render outside the overlay's boundary, so an overlay crash
  // cannot unmount them — and vice versa.
  const main = app.slice(app.indexOf('<main className="app-main">'), app.indexOf('</main>'))
  const closed = main.lastIndexOf('</ErrorBoundary>')
  assert.ok(closed > 0 && main.indexOf('{panes}') > closed, '{panes} sits outside the overlay boundary')
})

test('the crash pane is styled from existing tokens', () => {
  // No new palette — a failure state is not the place to invent colours.
  const block = css.slice(css.indexOf('.crash-pane'))
  assert.match(block, /var\(--bg-surface\)/)
  assert.match(block, /var\(--border-default\)/)
  assert.doesNotMatch(block.slice(0, block.indexOf('.crash-detail')), /#[0-9a-f]{3,6}\b/i)
})
