// Home is the worker's opening, not a sitemap of the other tabs. These pin the
// rules a reviewer would otherwise have to re-check by hand on every change:
// what Home is allowed to fetch on first paint, which blocks it renders and in
// what order, that the numbers can't disagree, and that nothing is a dead tap.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const app = readFileSync(new URL('../src/App.jsx', import.meta.url), 'utf8')
const work = readFileSync(new URL('../src/WorkTab.jsx', import.meta.url), 'utf8')

test('App wires every destination Home expects', () => {
  assert.match(app, /onGoWork=\{\(filter = null, scope = null\) => \{/)
  // The navigation carries Home's scope, and clears any job/run left open
  // from a previous visit — otherwise that detail renders instead of the
  // list this navigation asked for.
  assert.match(app, /whose: scope\?\.whose \?\? null/)
  assert.match(app, /personId: scope\?\.personId \?\? null/)
  assert.match(app, /setWorkRoute\(\{ job: null, run: null \}\)/)
  assert.match(app, /incomingFilter=\{workFilter\}/)
  // A finding's run target opens that exact run through Work's run route.
  assert.match(app, /onOpenRun=\{\(id\) => \{/)
  assert.match(app, /setWorkRoute\(\{ job: null, run: Number\(id\) \}\)/)
  assert.match(app, /onOpenCost=\{\(\) => setOverlay\(\{ kind: 'cost' \}\)\}/)
  assert.match(app, /onConnectAgent=\{openAddAgent\}/)
  // The by-job bars open the job pane App already owns.
  assert.match(app, /onOpenJob=\{\(id\) => id && setOverlay\(\{ kind: 'workflow', id \}\)\}/)
  // Home never invents a URL route; every destination is App state.
  assert.doesNotMatch(readFileSync(new URL('../src/HomeView.jsx', import.meta.url), 'utf8'), /window\.location|history\.push/)
})

test('a Work filter arriving from Home is visible and clearable', () => {
  // A filter you cannot see or clear is a table that looks broken.
  assert.match(work, /work-filter-chip/)
  assert.match(work, /onClick=\{onClearFilter\}/)
  assert.match(work, /aria-label=\{`Clear the \$\{WORK_FILTER_LABELS\[filter\] \|\| filter\} filter`\}/)
  // Re-clicking the same count must re-apply it: the pane is kept alive, so an
  // unchanged value alone would be a no-op.
  assert.match(work, /\[filterNonce\]/)
})

test('every filter Home navigates with is one Work understands', () => {
  // A tap that lands on an unfiltered table is a lie about where it went.
  const table = readFileSync(new URL('../src/workFilter.js', import.meta.url), 'utf8')
  for (const f of ['mine', 'moving', 'waiting', 'stuck', 'done']) {
    assert.match(table, new RegExp(`case '${f}':`), `Work cannot filter by ${f}`)
  }
  // And the value Home's personal link sends is in that table. It used to
  // send 'waiting_on_you' — a row status, not a filter — which fell through
  // to an unfiltered list.
  const hvSrc = readFileSync(new URL('../src/HomeView.jsx', import.meta.url), 'utf8')
  assert.match(hvSrc, /onGoWork\('mine', \{ whose: 'everyone', personId: null \}\)/)
  assert.doesNotMatch(hvSrc, /onGoWork\('waiting_on_you'/)
})

// --- the name --------------------------------------------------------------

test('a person never reads the word Dashboard', () => {
  // The pane id, the file and the API route keep the old name; every string a
  // person sees says Home.
  const surfaces = ['App.jsx', 'HomeView.jsx', 'HomeSections.jsx', 'HomeFindingPanel.jsx', 'CostPage.jsx', 'WorkFeedPage.jsx']
  for (const f of surfaces) {
    const src = readFileSync(new URL(`../src/${f}`, import.meta.url), 'utf8')
      .replace(/\/\*[\s\S]*?\*\//g, '')
      .replace(/^\s*\/\/.*$/gm, '')
    // JSX text nodes.
    for (const m of src.matchAll(/>([^<>{}]+)</g)) {
      assert.doesNotMatch(m[1], /Dashboard/, `${f} shows "Dashboard" to a person`)
    }
    // Labels, titles, placeholders, aria.
    for (const m of src.matchAll(/(?:aria-label|title|placeholder|label)=["']([^"']+)["']/g)) {
      assert.doesNotMatch(m[1], /Dashboard/, `${f} labels something "Dashboard"`)
    }
  }
  // And the nav tab itself, wherever the label list lives. It moved from
  // App.jsx into tabs.js when nav became seat-driven; the rule did not move.
  const tabs = readFileSync(new URL('../src/tabs.js', import.meta.url), 'utf8')
  assert.match(tabs, /\['dashboard', 'Home'\]/)
})
