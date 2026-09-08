// Home is a hub of page previews. These pin the rules a reviewer would
// otherwise have to re-check by hand on every change: what Home is allowed to
// fetch on first paint, that the healthy day is genuinely empty, that there is
// exactly one dollar figure, and that nothing on the page is a dead tap.
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
  // And the ones it is allowed.
  for (const call of [
    /getWorkOverview\(\{\s*signal\s*\}\)/,
    /getWorkItems\(\{[^}]*signal[^}]*\}\)/,
    /getCost\(\{\s*signal\s*\}\)/,
    /getWorkFeed\(\{\s*signal\s*\}\)/,
    /getAttention\(\{\s*signal\s*\}\)/,
  ]) {
    assert.match(code, call)
  }
})

test('the briefing is NOT on first paint — it is fetched only when opened', () => {
  // It is the slowest call the dashboard can make. The disclosure is closed by
  // default and the hook bails until it is opened.
  assert.match(code, /function useLazyBriefing\(open, refreshKey\)/)
  assert.match(code, /if \(!open\) return undefined/)
  assert.match(code, /useState\(false\)/)
  // Still aborts like every other Home fetch once it does run.
  assert.match(code, /getBriefing\(\{\s*signal\s*\}\)/)
})

test('the essay hero and the judgment ribbon are gone', () => {
  for (const banned of [/JudgmentRibbon/, /buildInsights/, /home-ribbon/, /BriefGroup/]) {
    assert.doesNotMatch(code, banned)
  }
  // The briefing survives only as a collapsed disclosure, never as the hero.
  assert.doesNotMatch(code, /home-hero/)
  assert.match(code, /BriefingDisclosure/)
})

test('a healthy day removes the Needs attention card entirely', () => {
  const fn = code.slice(code.indexOf('function NeedsAttentionCard'))
  // No rows -> no card. Not an empty state, not an "all clear" line.
  assert.match(fn, /if \(rows\.length === 0\) return null/)
  // Loading is silent too, so the card never flashes in and out.
  assert.match(fn, /if \(work\.items === null\) return null/)
  assert.doesNotMatch(fn.slice(0, fn.indexOf('return (')), /All clear|all clear/)
})

test('the Fleet card is absent when no agent needs attention', () => {
  // Home never loads the roster, so it cannot claim "all healthy" — it says
  // nothing instead.
  const fn = code.slice(code.indexOf('function FleetCard'))
  assert.match(fn, /if \(health\.loading \|\| health\.failed \|\| rows\.length === 0\) return null/)
})

test('exactly one dollar figure on Home, from the one cost endpoint', () => {
  // Two sources eventually disagree in prose; the brief calls that out
  // specifically. fmtMoney is rendered in one place only.
  const money = [...code.matchAll(/fmtMoney\(/g)]
  assert.equal(money.length, 1, 'one money render on Home')
  assert.match(code, /getCost\(\{\s*signal\s*\}\)/)
  assert.doesNotMatch(code, /getCostOverview/)
})

test('every Home control has a real destination — no dead taps', () => {
  // Each card's header/body wires to a navigation prop rather than a no-op.
  for (const dest of [
    /onOpenItem\(row\)/, // attention row -> work item detail
    /onAction=\{onOpenCost\}/, // cost header -> Cost page
    /onClick=\{onOpenCost\}/, // cost body -> Cost page
    /onViewAll=\{onViewAllWorkFeed\}/, // feed card is handed the overlay opener
    /onAction=\{onViewAll\}/, // ...and its header fires it
    /onOpenAgent\(f\.agent, 'main'\)/, // feed row -> agent
    /onGoWork\(t\.key\)/, // work tile -> Work, filtered
    /onGoWork\('attention'\)/, // attention header -> Work, filtered
    /onAction=\{onGoFleet\}/, // fleet header -> Fleet tab
    /onOpenAgent\(h\.agent, 'main'\)/, // fleet dot -> that agent
  ]) {
    assert.match(code, dest, `missing destination: ${dest}`)
  }
})

test('App wires every destination Home expects', () => {
  assert.match(app, /onGoFleet=\{\(\) => \{/)
  assert.match(app, /onGoWork=\{\(filter = null\) => \{/)
  assert.match(app, /setWorkFilter\(\{ value: filter, nonce: Date\.now\(\) \}\)/)
  assert.match(app, /incomingFilter=\{workFilter\}/)
  assert.match(app, /onOpenCost=\{\(\) => setOverlay\(\{ kind: 'cost' \}\)\}/)
  assert.match(app, /onViewAllWorkFeed=\{\(\) => setOverlay\(\{ kind: 'workfeed' \}\)\}/)
})

test('a Work filter arriving from Home is visible and clearable', () => {
  // A filter you cannot see or clear is a table that looks broken.
  assert.match(work, /work-filter-chip/)
  assert.match(work, /onClick=\{onClearFilter\}/)
  assert.match(work, /aria-label=\{`Clear the \$\{WORK_FILTER_LABELS\[filter\] \|\| filter\} filter`\}/)
  // Re-clicking the same tile must re-apply it: the pane is kept alive, so an
  // unchanged value alone would be a no-op.
  assert.match(work, /\[filterNonce\]/)
})

test('Home and Work agree on what "needs attention" means', () => {
  // Both go through the same helper rather than each carrying a rule.
  assert.match(work, /import \{ partitionLookAt \} from '\.\/home\.js'/)
  assert.match(code, /partitionLookAt/)
})
