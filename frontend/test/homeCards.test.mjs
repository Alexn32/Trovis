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

test('the briefing generates on render — no click, and it never blocks paint', () => {
  // The insight is the reason Home exists; it used to sit behind a "More"
  // that most people never pressed. It now fetches as soon as Home renders.
  assert.match(code, /function useBriefing\(refreshKey, active\)/)
  assert.doesNotMatch(code, /useLazyBriefing/)
  assert.doesNotMatch(code, /if \(!open\) return undefined/)
  // Independent and abortable, like every other Home section — the page
  // paints from the record with the templated lead while this is in flight.
  assert.match(code, /getBriefing\(\{[^}]*signal[^}]*\}\)/)
  assert.match(code, /startAbortable/)
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
    // The shape-of-the-day slogan: it inferred "Work is moving" from
    // "nothing is stuck" and printed it above a strip reading 0 moving.
    // The desk box's own empty state replaced it.
    /dayShape/, /home-shape/,
  ]) {
    assert.doesNotMatch(code, banned)
  }
  // The briefing survives — open, leading with the template line.
  assert.match(code, /<Briefing work=/)
})

test('the blocks render in the order the brief numbers them', () => {
  const order = [
    'Greeting',
    'FleetPulse',
    'home-cols',
    'DeskSection',
    'NoticedSection',
    'ProofStrip',
    // The insight, then the way to go deeper. Briefing above Ask.
    'Briefing',
    'AskSection',
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

test('the desk stays on the page when it is empty, and says one fact', () => {
  const fn = code.slice(code.indexOf('function DeskSection'), code.indexOf('function DeskRow'))
  assert.match(fn, /deskEmptyCopy\(\{ connected \}\)/)
  assert.match(fn, /home-desk-clear/)
  // And it must not reach for a count to describe the rest of the day.
  assert.doesNotMatch(fn, /counts\./)
  assert.doesNotMatch(fn, /moving/)
})

test('the desk and the two columns sit side by side', () => {
  assert.match(code, /<div className="home-cols">/)
  const cols = code.slice(code.indexOf('<div className="home-cols">'))
  const desk = cols.indexOf('<DeskSection')
  const noticed = cols.indexOf('<NoticedSection')
  assert.ok(desk > -1 && noticed > desk, 'desk leads, noticed sits beside it')
})

test('the pulse insight never blocks first paint', () => {
  // Everything on the pulse except one sentence comes from the record. The
  // sentence arrives afterwards, and its absence is invisible.
  const fn = code.slice(code.indexOf('function usePulseInsight'), code.indexOf('// --- 1. greeting'))
  assert.match(fn, /startAbortable/)
  assert.match(fn, /useState\(\{ insight: '', graphic: 'none' \}\)/)
  // A failure is silent: no error state, no retry, no empty frame.
  assert.match(fn, /\.catch\(\(\) => \{\}\)/)
  // And it does not ask about a packet with nothing proven in it.
  assert.match(fn, /Object\.keys\(packet\)\.length === 0/)
})

test('the pulse asks again only when the FACTS change', () => {
  // Home re-renders on every keystroke in the Ask field. Keying the packet on
  // its serialised contents is what stops that becoming a request per letter.
  assert.match(code, /function usePacket\(inputs\)/)
  assert.match(code, /const key = JSON\.stringify\(built\)/)
  const hook = code.slice(code.indexOf('function usePulseInsight'))
  assert.match(hook, /\}, \[key\]\)/)
})

test('the insight slot is never blank for a connected org', () => {
  // The generated sentence is preferred and has already passed the entailment
  // check; the templated line holds the slot until then and keeps it when the
  // model is missing, slow, or refused. Always-on is the fallback's job, not
  // a looser validator.
  assert.match(code, /const pulseLine = insight\.insight \|\| fallbackInsight\(packet\)/)
  assert.match(code, /insight=\{pulseLine\}/)
})

test('the insight line opens Ask with what it says', () => {
  // The pulse has room for a sentence; Ask is where the depth lives. Clicking
  // the line asks it rather than expanding anything on Home.
  assert.match(code, /onClick=\{\(\) => openAsk\(askSeed\(insight\)\)\}/)
  // It is a real button, not a styled div, so it is reachable by keyboard.
  const fn = code.slice(code.indexOf('function FleetPulse'), code.indexOf('function PulseGraphic'))
  assert.match(fn, /<button[^>]*className="home-pulse-insight"/s)
  // And no second chat surface was invented for it.
  assert.doesNotMatch(code, /InsightPanel|InsightsPage|useChat/)
})

test('the graphic is drawn in code, never by the model', () => {
  // The model may only NAME a series; the bars and the caption are computed.
  const fn = code.slice(code.indexOf('function PulseGraphic'))
  assert.match(fn, /graphic\.bars\.map/)
  assert.match(fn, /\{graphic\.caption\}/)
  // No pie, no gauge, no score.
  for (const banned of [/pie/i, /gauge/i, /score/i, /donut/i]) {
    assert.doesNotMatch(fn, banned)
  }
  // The graphic comes from pulseGraphic(), which returns null for a series we
  // do not have — so a missing series renders nothing at all.
  assert.match(code, /\{graphic && <PulseGraphic/)
})

test('the fleet pulse never claims health it cannot see', () => {
  // Home does not load the roster, so an agent not being flagged means nobody
  // looked — not that it is fine.
  const fn = code.slice(code.indexOf('function FleetPulse'), code.indexOf('// --- 3. your desk'))
  assert.doesNotMatch(fn, /all healthy|All healthy|healthy/)
  assert.match(fn, /Nothing flagged right now/)
  // And the count comes from agent_count, never from the truncated list.
  assert.match(code, /cost\.data\?\.agent_count/)
  assert.doesNotMatch(code, /cost\.data\?\.agents\.length/)
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
    /onOpen\(line\.target\)/, // a noticed row's action -> its work or its agent
    /onClick=\{onGoFleet\}/, // fleet pulse -> Fleet
    /onOpenAgent\(\.\.\.agentRoute\(a\)\)/, // a flagged agent -> that agent
    /openAsk\(c\.query\)/, // an Ask chip -> Ask, with that question
    /onGoWork\(graphic\.filter\)/, // the pulse graphic -> Work, filtered
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
  assert.match(app, /onGoFleet=\{\(\) => \{/)
})

test('Home has exactly one Ask affordance', () => {
  // Two Ask buttons on one screen is two answers to "where do I ask?".
  assert.match(app, /<AskPill hideLauncher=\{dashboardVisible\} \/>/)
  const pill = readFileSync(new URL('../src/AskPill.jsx', import.meta.url), 'utf8')
  assert.match(pill, /if \(hideLauncher\) return null/)
  // Hiding the launcher must never hide Ask itself: the panel still opens on
  // the keyboard and on openAsk() from anywhere.
  const guard = pill.slice(pill.indexOf('if (hideLauncher)'))
  assert.doesNotMatch(guard.slice(0, 200), /ASK_EVENT|addEventListener/)
  // And Home renders exactly one input that opens it.
  const inputs = [...code.matchAll(/className="home-ask-input"/g)]
  assert.equal(inputs.length, 1)
})

test('the briefing is open, with the generated narrative on screen', () => {
  const fn = code.slice(code.indexOf('function Briefing('))
  // The lead renders unconditionally...
  assert.match(fn, /<p className="home-brief-lead">\{lead\}<\/p>/)
  assert.doesNotMatch(fn, /if \(!showMore\) return null/)
  // ...and so does the body holding the generated prose. Nothing to expand.
  assert.doesNotMatch(fn, /showMore/)
  assert.doesNotMatch(fn, /aria-expanded/)
  assert.match(fn, /useBriefing\(refreshKey, active\)/)
  assert.match(fn, /<div className="home-brief-body">/)
  assert.match(fn, /briefing\.data\?\.summary \? \(/)
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
  // And the nav tab itself, wherever the label list lives. It moved from
  // App.jsx into tabs.js when nav became seat-driven; the rule did not move.
  const tabs = readFileSync(new URL('../src/tabs.js', import.meta.url), 'utf8')
  assert.match(tabs, /\['dashboard', 'Home'\]/)
})
