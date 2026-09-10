// A job you ENTER, and a story told as actions.
//
// Two changes with one theme: from Work, looking into a job is not a peek at
// a card, it is going somewhere — so the job takes the screen and Back
// returns you to exactly the view you left. And the middle of that page
// answers "what did this job actually do", which the lifecycle timeline
// structurally cannot: its events are passes ("Started", "Waiting on
// someone"), not moves.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { jobActions, shortHistory, shortenOperation } from '../src/jobDetail.js'

const src = (f) => readFileSync(new URL(`../src/${f}`, import.meta.url), 'utf8')
const strip = (t) => t.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')
const work = strip(src('WorkTab.jsx'))
const jobRaw = src('JobDetail.jsx')
const job = strip(jobRaw)
const dash = strip(src('Dashboard.jsx'))

function run(o = {}) {
  return {
    name: 'tool_call', agent: 'refunds-agent', service_name: 'refunds-agent',
    agent_id: null, at: '2026-03-10T12:00:00Z', errored: false,
    duration_ms: null, cost_usd: null, tool: null, error: null, ...o,
  }
}

// --- 1. navigation: enter the task ------------------------------------------

test('a run is a page at its own URL, not an overlay', () => {
  // Which page is showing is now the URL's answer, not local state — so a
  // pasted link, Back, and a click all arrive the same way.
  assert.match(work, /if \(route\.run\) \{/)
  assert.match(work, /<JobDetail\s+variant="page"/)
  // No slide-over left in Work: no scrim, no local `open` racing the route.
  assert.doesNotMatch(work, /const \[open, setOpen\] = useState\(null\)/)
  assert.doesNotMatch(work, /const \[jobView, setJobView\]/)
})

test('every way into a run goes through the route', () => {
  // A table row and a past run are doors to one destination, and none of
  // them may open a run any other way.
  assert.match(work, /onOpenItem=\{\(it\) => onRoute\(\{ job: route\.job, run: it\.id \}\)\}/)
  assert.doesNotMatch(work, /setJobView\(/)
})

test('leaving a run returns to the job it was opened from', () => {
  // Closing a run clears only the run: the job stays in the route, so Back
  // lands on that job's page and not the board. The filter is untouched.
  assert.match(work, /onClose=\{\(\) => onRoute\(\{ job: route\.job, run: null \}\)\}/)
  assert.match(work, /backLabel=\{openJob \? `← \$\{openJob\.name\}` : '← Work'\}/)
  const runBlock = work.slice(work.indexOf('if (route.run) {'), work.indexOf('return (\n    route.job'))
  assert.doesNotMatch(runBlock, /setFilter/,
                      'opening or leaving a run must not disturb the page under it')
})

test('Home desk keeps the slide-over — act and be done', () => {
  // Home is where you clear things; losing your place there to approve one
  // row would be the wrong trade. Work is where you go to look INTO a job.
  assert.match(dash, /<JobDetail\s+item=\{openItem\}/)
  assert.doesNotMatch(dash, /variant="page"/)
  assert.match(job, /variant = 'panel'/)
  assert.match(job, /const isPage = variant === 'page'/)
  // The panel still has its scrim; the page has none.
  assert.match(job, /<div className="bpanel-scrim" onClick=\{onClose\} \/>/)
  assert.match(job, /<div className="view job-page"/)
})

// --- 2. the action list -----------------------------------------------------

test('two tool calls are two moves, not one Tool node', () => {
  // The kind path collapses these into a single Tool hand on purpose. This
  // list must not: "it called Stripe twice" is the thing you came here for.
  const acts = jobActions([
    run({ name: 'tool_call', tool: 'stripe', at: '2026-03-10T12:00:00Z' }),
    run({ name: 'tool_call', tool: 'stripe', at: '2026-03-10T12:01:00Z' }),
  ])
  assert.equal(acts.length, 2)
  assert.deepEqual(acts.map((a) => a.system), ['stripe', 'stripe'])
})

test('a move is what · who · system · result · time', () => {
  const [a] = jobActions([
    run({ name: 'tool_call', tool: 'stripe', agent: 'refunds-agent',
          errored: true, error: 'Card declined', duration_ms: 320 }),
  ])
  assert.equal(a.what, 'Tool call')
  assert.deepEqual(a.who, { kind: 'agent', name: 'refunds-agent' })
  assert.equal(a.system, 'stripe')
  assert.equal(a.result, 'Error')
  assert.equal(a.at, '2026-03-10T12:00:00Z')
  assert.equal(a.duration, '320ms')
  // #160's rule, unchanged: one line, tool then message.
  assert.equal(a.reason, 'stripe — Card declined')
})

test('the operation is shortened, never translated', () => {
  // "Refunded the customer" would be a claim the record cannot support.
  assert.equal(shortenOperation('agent_run_complete'), 'Agent run complete')
  assert.equal(shortenOperation('model_call'), 'Model call')
  assert.equal(shortenOperation('http.client.request'), 'Http client request')
  assert.equal(shortenOperation(''), 'Ran')
  assert.equal(shortenOperation(null), 'Ran')
})

test('moves read oldest first, as a sequence', () => {
  const acts = jobActions([
    run({ name: 'third', at: '2026-03-10T12:02:00Z' }),
    run({ name: 'first', at: '2026-03-10T12:00:00Z' }),
    run({ name: 'second', at: '2026-03-10T12:01:00Z' }),
  ])
  assert.deepEqual(acts.map((a) => a.what), ['First', 'Second', 'Third'])
})

test('the current move is marked only when the job is actually moving', () => {
  const rows = [
    run({ name: 'a', at: '2026-03-10T12:00:00Z' }),
    run({ name: 'b', at: '2026-03-10T12:01:00Z' }),
  ]
  assert.deepEqual(jobActions(rows, { status: 'moving' }).map((a) => a.isCurrent),
                   [false, true])
  // On a job sitting with a person, no ACTION is current — the wait is, and
  // the block above the list already says so. Marking a finished move
  // "current" there would be false.
  for (const status of ['waiting_on_you', 'waiting_on_other', 'stuck', 'done']) {
    assert.ok(jobActions(rows, { status }).every((a) => !a.isCurrent), status)
  }
})

test('no runs means no invented list', () => {
  assert.deepEqual(jobActions([]), [])
  assert.deepEqual(jobActions(null), [])
  // ...and the page falls back to the spine rather than an empty frame.
  const fn = job.slice(job.indexOf('function ActionList'), job.indexOf('function holderSentence'))
  assert.match(fn, /actions\.length === 0/)
  assert.match(fn, /<ol className="jobd-steps">/)
})

test('the runs load once on enter, not once per action', () => {
  const fn = job.slice(job.indexOf('export default function JobDetail'), job.indexOf('function jobTotals'))
  assert.match(fn, /if \(!isPage\) return undefined/)
  assert.match(fn, /include: 'runs'/)
  // One fetch effect for the page, keyed on the item — not on the actions.
  assert.match(fn, /\}, \[isPage, item\.id, runsReload\]\)/)
})

test('the action list IS the runs, so the page does not fold them again', () => {
  // Depth means a layer that adds something. The same eight rows under a
  // disclosure triangle is the same eight rows.
  assert.match(job, /\{!isPage && <AgentRuns itemId=\{item\.id\} onOpenAgent=\{onOpenAgent\} \/>\}/)
})

test('an agent name in a move still opens Fleet, and still only with a route', () => {
  const fn = job.slice(job.indexOf('function ActionList'), job.indexOf('function holderSentence'))
  assert.match(fn, /onOpenAgent && a\.route/)
  assert.match(fn, /onClick=\{\(\) => onOpenAgent\(a\.route\[0\], a\.route\[1\]\)\}/)
  assert.match(fn, /<span className="jobd-run-agent is-plain">\{a\.who\.name\}<\/span>/)
})

// --- 3. recent passes -------------------------------------------------------

test('"Recent handoffs" is gone from the UI', () => {
  assert.doesNotMatch(jobRaw, /Recent handoffs/)
  assert.match(job, /<h3 className="dash-caps">Recent passes<\/h3>/)
})

test('five identical passes in a row are one pass, with a count', () => {
  // "Waiting on someone · 19d ago" five times reads as five passes when it
  // is one agent re-announcing the same wait.
  const t = (text, at) => ({ text, at, actor: { kind: 'agent', name: 'a' } })
  const rows = shortHistory([
    t('Started', '2026-03-01T00:00:00Z'),
    t('Waiting on someone', '2026-03-02T00:00:00Z'),
    t('Waiting on someone', '2026-03-03T00:00:00Z'),
    t('Waiting on someone', '2026-03-04T00:00:00Z'),
  ])
  assert.deepEqual(rows.map((r) => r.text), ['Started', 'Waiting on someone'])
  assert.equal(rows[1].count, 3, 'the count says the row stands for three')
  assert.equal(rows[1].at, '2026-03-04T00:00:00Z', 'and carries the latest of them')
})

test('the same text by a different party is a different pass', () => {
  const rows = shortHistory([
    { text: 'Picked up', at: '1', actor: { kind: 'human', name: 'Alex' } },
    { text: 'Picked up', at: '2', actor: { kind: 'human', name: 'Sam' } },
  ])
  assert.equal(rows.length, 2)
})

test('non-consecutive repeats are not merged', () => {
  const a = { kind: 'agent', name: 'a' }
  const rows = shortHistory([
    { text: 'Waiting on someone', at: '1', actor: a },
    { text: 'Picked up', at: '2', actor: a },
    { text: 'Waiting on someone', at: '3', actor: a },
  ])
  assert.equal(rows.length, 3, 'a wait, a pickup, then another wait is three things')
})

test('a burst of repeats cannot crowd real history out of the list', () => {
  // This is why collapsing has to happen BEFORE the cap, not after. Cap
  // first and the last five rows are all the same wait, so every earlier
  // pass is gone — the list would be five copies of one fact and no history
  // at all.
  const a = { kind: 'agent', name: 'a' }
  const rows = shortHistory([
    { text: 'Started', at: '2026-03-01T00:00:00Z', actor: a },
    { text: 'Picked up', at: '2026-03-02T00:00:00Z', actor: a },
    { text: 'Moved forward', at: '2026-03-03T00:00:00Z', actor: a },
    { text: 'Sent back', at: '2026-03-04T00:00:00Z', actor: a },
    ...Array.from({ length: 10 }, (_, i) => ({
      text: 'Waiting on someone', at: `2026-03-${10 + i}T00:00:00Z`, actor: a,
    })),
  ])
  assert.deepEqual(rows.map((r) => r.text),
                   ['Started', 'Picked up', 'Moved forward', 'Sent back', 'Waiting on someone'])
  assert.equal(rows[4].count, 10)
})

// --- 4. #160 still holds ----------------------------------------------------

test('cost still only when there is one, on a move as on a run', () => {
  assert.equal(jobActions([run({ cost_usd: 0 })])[0].cost, null)
  assert.equal(jobActions([run({ cost_usd: null })])[0].cost, null)
  assert.equal(jobActions([run({ cost_usd: 0.0042 })])[0].cost, '$0.0042')
  // ...and the header total obeys the same rule.
  const fn = job.slice(job.indexOf('function jobTotals'), job.indexOf('function ActionList'))
  assert.match(fn, /const cost = runCost\(/)
  assert.match(fn, /if \(cost\) out\.push\(cost\)/)
})

test('a failure with no message still gets no invented reason', () => {
  assert.equal(jobActions([run({ errored: true, error: null })])[0].reason, null)
  assert.equal(jobActions([run({ errored: true, error: 'boom' })])[0].reason, 'boom')
})

// --- 5. house rules ---------------------------------------------------------

test('no jargon in the job page chrome', () => {
  const FORBIDDEN = /\b(loops?|workloops?|possession|segments?|stations?|handoffs?)\b/i
  for (const m of job.matchAll(/>([^<>{}]{2,})</g)) {
    assert.ok(!FORBIDDEN.test(m[1]), `job pane ships jargon: ${JSON.stringify(m[1])}`)
  }
  for (const s of ['How this job ran', 'Recent passes', '← All work', 'now']) {
    assert.ok(!FORBIDDEN.test(s))
  }
})

test('still no fat endpoints', () => {
  for (const f of ['WorkTab.jsx', 'JobDetail.jsx', 'Dashboard.jsx']) {
    assert.doesNotMatch(src(f), /getWorkBoard|getWorkSummary/, f)
  }
})

test('one Ask on the panel, one on the page', () => {
  assert.equal((job.match(/openAsk\(/g) || []).length, 1)
})
