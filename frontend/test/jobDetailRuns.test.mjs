// The technical fold: the door out of Work.
//
// Everything in a run row is optional, and the rule for every field is the
// same — show it when the record has it, drop it when it does not. The
// failure mode this guards against is a fold that pads itself out with
// zeroes and invented reasons until it reads like a measurement, which is
// exactly what someone opening it is trying to escape.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { runAgentRoute, runCost, runDuration, runErrorLine } from '../src/jobDetail.js'

const code = readFileSync(new URL('../src/JobDetail.jsx', import.meta.url), 'utf8')
const bare = code.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')
const runRow = bare.slice(bare.indexOf('function RunRow'), bare.indexOf('function AgentRuns'))

function run(o = {}) {
  return {
    name: 'agent_run_complete',
    agent: 'refunds-agent',
    service_name: 'refunds-agent',
    agent_id: null,
    at: '2026-03-10T12:00:00Z',
    errored: false,
    duration_ms: null,
    cost_usd: null,
    tool: null,
    error: null,
    ...o,
  }
}

// --- the failure line -------------------------------------------------------

test('a failed run gets ONE work-language line: tool then message', () => {
  assert.equal(
    runErrorLine(run({ errored: true, tool: 'stripe', error: 'Card declined' })),
    'stripe — Card declined',
  )
})

test('no tool means the message stands alone', () => {
  assert.equal(runErrorLine(run({ errored: true, error: 'Timed out after 30s' })),
               'Timed out after 30s')
})

test('a failure with no message invents nothing', () => {
  // The Error tag already says it failed. A sentence we were never given
  // would be the pane making something up, which is the one thing it cannot
  // do and still be worth reading.
  assert.equal(runErrorLine(run({ errored: true, error: null })), null)
  assert.equal(runErrorLine(run({ errored: true, error: '   ' })), null)
  assert.equal(runErrorLine(run({ errored: true, tool: 'stripe', error: '' })), null)
})

test('a run that succeeded gets no reason line, message or not', () => {
  assert.equal(runErrorLine(run({ errored: false, error: 'ignored' })), null)
  assert.equal(runErrorLine(null), null)
})

test('the reason renders once, and no waterfall or token dump comes with it', () => {
  assert.match(runRow, /\{reason && <p className="jobd-run-why">\{reason\}<\/p>\}/)
  // Depth that belongs on the agent's page, not in this fold. (`<span>` is a
  // tag, so match the payload fields a dump would actually reach for.)
  assert.doesNotMatch(runRow, /\b(tokens?|input_tokens|total_tokens|attributes|spans|waterfall)\b/i)
})

// --- cost -------------------------------------------------------------------

test('cost shows only when there is one', () => {
  assert.equal(runCost(0.0042), '$0.0042')
  assert.equal(runCost(1.2), '$1.20')
})

test('$0.00 is not a cost', () => {
  // "We were not told" and "it was free" are different claims, and a column
  // of $0.00 makes the first look like the second.
  for (const v of [0, -1, null, undefined, '', NaN, 'abc']) {
    assert.equal(runCost(v), null, `${JSON.stringify(v)} must not print as a cost`)
  }
})

// --- duration ---------------------------------------------------------------

test('duration reads in the unit a person would say it in', () => {
  assert.equal(runDuration(820), '820ms')
  assert.equal(runDuration(1400), '1.4s')
  assert.equal(runDuration(45000), '45s')
  assert.equal(runDuration(125000), '2m 05s')
})

test('an absent duration is absent, not zero', () => {
  // Number(null) and Number('') are both 0, so these have to be rejected
  // before coercion or a missing duration prints as a measured "0ms".
  for (const v of [null, undefined, '', 0, -5, NaN, 'soon']) {
    assert.equal(runDuration(v), null, `${JSON.stringify(v)} must not print as a duration`)
  }
})

// --- the agent name as a door ----------------------------------------------

test('the route is service_name + agent_id, never the display label', () => {
  // The blank-screen bug (#146) was a display label used as a URL. Same rule
  // holds here: `agent` is for reading, `service_name` is for routing.
  assert.deepEqual(
    runAgentRoute(run({ agent: 'Support Bot', service_name: 'support-svc', agent_id: 'researcher' })),
    ['support-svc', 'researcher'],
  )
  assert.deepEqual(runAgentRoute(run({ service_name: 'refunds-agent' })), ['refunds-agent', 'main'])
})

test('no route means no button — a name that opens a 404 is worse than plain text', () => {
  assert.equal(runAgentRoute(run({ service_name: '', agent: 'Support Bot' })), null)
  assert.equal(runAgentRoute(null), null)
})

test('the agent name invokes onOpenAgent when there is somewhere to send it', () => {
  assert.match(runRow, /const canOpen = Boolean\(onOpenAgent && route\)/)
  assert.match(runRow, /onClick=\{\(\) => onOpenAgent\(route\[0\], route\[1\]\)\}/)
  // ...and stays text otherwise, in the same row, rather than disappearing.
  assert.match(runRow, /<span className="jobd-run-agent is-plain">\{run\.agent\}<\/span>/)
})

test('the callback is threaded from the app, not invented in the pane', () => {
  assert.match(code, /export default function JobDetail\(\{ item, onClose, onResolved, onOpenAgent \}\)/)
  assert.match(bare, /<AgentRuns itemId=\{item\.id\} onOpenAgent=\{onOpenAgent\} \/>/)
  for (const [file, label] of [['WorkTab.jsx', 'Work'], ['Dashboard.jsx', 'Home']]) {
    const src = readFileSync(new URL(`../src/${file}`, import.meta.url), 'utf8')
    assert.match(src, /onOpenAgent=\{onOpenAgent\}/, `${label} must pass it down`)
  }
  const app = readFileSync(new URL('../src/App.jsx', import.meta.url), 'utf8')
  const work = app.slice(app.indexOf('<WorkTab'), app.indexOf('</TabPane>', app.indexOf('<WorkTab')))
  assert.match(work, /onOpenAgent=\{openDetail\}/, 'Work must be given the real Fleet opener')
})

// --- the fold stays folded --------------------------------------------------

test('runs still load only when the fold is opened', () => {
  assert.match(bare, /if \(!open \|\| asked\.current\) return undefined/)
  assert.match(bare, /include: 'runs'/)
})

test('no second Ask on the panel', () => {
  // The wait block owns the one Ask. A second entry point on the same screen
  // makes neither of them the obvious one.
  assert.equal((bare.match(/openAsk\(/g) || []).length, 1)
  assert.doesNotMatch(runRow, /openAsk|Ask/)
})

test('no jargon in the fold', () => {
  const FORBIDDEN = /\b(loops?|workloops?|possession|segments?|stations?|handoffs?)\b/i
  for (const m of runRow.matchAll(/>([^<>{}]{2,})</g)) {
    assert.ok(!FORBIDDEN.test(m[1]), `fold ships jargon: ${JSON.stringify(m[1])}`)
  }
  for (const s of ['Error', 'OK', 'Agent runs']) assert.ok(!FORBIDDEN.test(s))
})

test('the pane never reaches for a fat endpoint', () => {
  assert.doesNotMatch(code, /getWorkBoard|getWorkSummary/)
})
