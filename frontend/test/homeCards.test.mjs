// Home is the worker's opening, not a sitemap of the other tabs. These pin the
// rules a reviewer would otherwise have to re-check by hand on every change:
// what Home is allowed to fetch on first paint, which blocks it renders and in
// what order, that the numbers can't disagree, and that nothing is a dead tap.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const dash = readFileSync(new URL('../src/Dashboard.jsx', import.meta.url), 'utf8')
const app = readFileSync(new URL('../src/App.jsx', import.meta.url), 'utf8')
const work = readFileSync(new URL('../src/WorkTab.jsx', import.meta.url), 'utf8')
// Comments describe the rules; strip them so the assertions read real code.
const code = dash.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')

test('Home first paint fetches only the lean, allowed endpoints', () => {
  // The three that starve the single replica.
  assert.doesNotMatch(code, /getWorkBoard/)
  assert.doesNotMatch(code, /getWorkSummary/)
  assert.doesNotMatch(code, /listAgents/)
  // The feed card is gone from Home, so its fetch must be gone too — a
  // request whose only reader was deleted is pure cost.
  assert.doesNotMatch(code, /getWorkFeed/)
  // And the ones it is allowed.
  for (const call of [
    /getWorkOverview\(\{\s*signal\s*\}\)/,
    /getWorkItems\(\{[^}]*signal[^}]*\}\)/,
    /getCost\(\{\s*signal\s*\}\)/,
    /getAttention\(\{\s*signal\s*\}\)/,
  ]) {
    assert.match(code, call)
  }
})

test('the briefing is NOT on first paint — it is fetched only when opened', () => {
  // It is the slowest call Home can make. The disclosure is closed by default
  // and the hook bails until it is opened.
  assert.match(code, /function useLazyBriefing\(open, refreshKey\)/)
  assert.match(code, /if \(!open\) return undefined/)
  assert.match(code, /useState\(false\)/)
  // Still aborts like every other Home fetch once it does run.
  assert.match(code, /getBriefing\(\{\s*signal\s*\}\)/)
})

test('the cut blocks stay cut', () => {
  // Each of these was on Home and is deliberately not any more. A reviewer
  // adding one back should have to delete a line here first.
  for (const banned of [
    /JudgmentRibbon/, /buildInsights/, /home-ribbon/, /BriefGroup/, // judgment ribbon
    /home-hero/, // briefing essay hero
    /function WorkFeedCard/, /home-feed-row/, // work feed card
    /function FleetCard/, /home-fleet-row/, // fleet roster preview
    /function WorkCard/, /home-work-tile/, // kind-of-work grid
    /Sparkline/, // no charts on Home
  ]) {
    assert.doesNotMatch(code, banned)
  }
  // The briefing survives only as a collapsed disclosure, never as the hero.
  assert.match(code, /BriefingDisclosure/)
})

test('the blocks render in the order the brief numbers them', () => {
  const order = [
    'Greeting',
    'home-shape',
    'DeskSection',
    'NoticedSection',
    'ProofStrip',
    'AskEntry',
  ]
  // Read the render body only, so the definition order below cannot mask a
  // block rendered out of sequence.
  const body = code.slice(code.indexOf('return (\n    <div className="dash home">'))
  let last = -1
  for (const name of order) {
    const at = body.indexOf(name)
    assert.ok(at > last, `${name} is out of order on Home`)
    last = at
  }
})

test('an empty desk removes the section entirely', () => {
  const fn = code.slice(code.indexOf('function DeskSection'))
  // No rows -> no section. Not an empty state, not an "all clear" line.
  assert.match(fn, /if \(desk\.length === 0\) return null/)
  // Loading is silent too, so the section never flashes in and out.
  assert.match(fn, /if \(work\.items === null\) return null/)
  assert.doesNotMatch(fn.slice(0, fn.indexOf('return (')), /All clear|all clear/)
})

test('Trovis noticed disappears when there is nothing to notice', () => {
  const fn = code.slice(code.indexOf('function NoticedSection'))
  assert.match(fn, /if \(lines\.length === 0\) return null/)
})

test('exactly one dollar figure on Home, from the one cost endpoint', () => {
  // Two sources eventually disagree; the brief calls that out specifically.
  const money = [...code.matchAll(/fmtMoney\(/g)]
  assert.equal(money.length, 1, 'one money render on Home')
  assert.match(code, /getCost\(\{\s*signal\s*\}\)/)
  assert.doesNotMatch(code, /getCostOverview/)
})

test('the counts live in the strip and nowhere else', () => {
  // The sentence is templated from counts but prints none of them, and no
  // other block renders a bare number — that is what stops Home from
  // contradicting itself.
  const strip = code.slice(code.indexOf('function ProofStrip'), code.indexOf('function AskEntry'))
  assert.match(strip, /counts\[c\.key\]/)
  const elsewhere = code.slice(0, code.indexOf('function ProofStrip'))
  assert.doesNotMatch(elsewhere, /\{counts\.(moving|waiting|stuck|done)\}/)
})

test('every Home control has a real destination — no dead taps', () => {
  for (const dest of [
    /onClick=\{onOpen\}/, // desk row -> that job's detail
    /onGoWork\('mine'\)/, // desk header -> Work, filtered to YOUR waits
    /onOpen\(line\.target\)/, // a noticed line -> its work or its agent
    /onGoWork && onGoWork\(c\.filter\)/, // strip count -> Work, filtered
    /onOpen=\{onOpenCost\}/, // strip dollar -> Cost page
    /onClick=\{failed \? onRetry : onOpen\}/, // ...and a failed cell -> retry
    /openAsk\(text\)/, // Ask -> the real Ask panel
    /onClick=\{onConnectAgent\}/, // first run -> Add agent
  ]) {
    assert.match(code, dest, `missing destination: ${dest}`)
  }
})

test('every noticed target resolves to a real navigation', () => {
  // noticedLines emits three target shapes; openTarget must handle all three
  // or a line becomes a dead tap.
  const fn = code.slice(code.indexOf('function openTarget'), code.indexOf('return (\n    <div'))
  for (const to of ['work-item', 'work', 'agent']) {
    assert.match(fn, new RegExp(`target\\.to === '${to}'`), `openTarget ignores ${to}`)
  }
})

test('App wires every destination Home expects', () => {
  assert.match(app, /onGoWork=\{\(filter = null\) => \{/)
  assert.match(app, /setWorkFilter\(\{ value: filter, nonce: Date\.now\(\) \}\)/)
  assert.match(app, /incomingFilter=\{workFilter\}/)
  assert.match(app, /onOpenCost=\{\(\) => setOverlay\(\{ kind: 'cost' \}\)\}/)
  assert.match(app, /onConnectAgent=\{openAddAgent\}/)
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
  for (const f of ['mine', 'moving', 'waiting', 'stuck', 'done']) {
    assert.match(work, new RegExp(`case '${f}':`), `Work cannot filter by ${f}`)
  }
})

// --- the name --------------------------------------------------------------

test('a person never reads the word Dashboard', () => {
  // The pane id, the file and the API route keep the old name; every string a
  // person sees says Home.
  const surfaces = ['App.jsx', 'Dashboard.jsx', 'CostPage.jsx', 'WorkFeedPage.jsx']
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
  // And the nav tab itself.
  assert.match(app, /\['dashboard', 'Home'\]/)
})
