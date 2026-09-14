// Job detail: the route a job takes, and when it is allowed to ask you for a
// decision. The derivation is pure; the panel wiring is pinned by source
// assertions in the same style as the other Home/Work tests.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import {
  ACTOR_LABEL,
  MAX_STEPS,
  askPrompt,
  canDecide,
  processSteps,
  shortHistory,
} from '../src/jobDetail.js'

const panel = readFileSync(new URL('../src/JobDetail.jsx', import.meta.url), 'utf8')
const code = panel.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')

const ev = (text, kind, name, at = null) => ({ text, at, actor: { kind, name } })

// --- the route -------------------------------------------------------------

test('steps are the route through people, agents and tools — in order', () => {
  const { steps } = processSteps([
    ev('Started', 'agent', 'refunds-agent'),
    ev('Waiting on someone', 'human', 'Alex Nielsen'),
    ev('Picked up', 'human', 'Alex Nielsen'),
    ev('Waiting on someone', 'tool', 'Stripe'),
  ])
  assert.deepEqual(
    steps.map((s) => [s.actor.kind, s.actor.name]),
    [['agent', 'refunds-agent'], ['human', 'Alex Nielsen'], ['tool', 'Stripe']],
    'consecutive events with the same holder are ONE leg, not one each',
  )
})

test('a SaaS destination is a tool — no new enum is invented', () => {
  const { steps } = processSteps([ev('Waiting on someone', 'tool', 'HubSpot')])
  assert.equal(steps[0].actor.kind, 'tool')
  assert.equal(ACTOR_LABEL.tool, 'Tool')
  assert.deepEqual(Object.keys(ACTOR_LABEL).sort(), ['agent', 'human', 'tool'])
})

test('an unknown actor kind degrades to agent rather than rendering blank', () => {
  const { steps } = processSteps([{ text: 'Started', actor: { kind: 'saas', name: 'X' } }])
  assert.equal(steps[0].actor.kind, 'agent')
  const noActor = processSteps([{ text: 'Started' }])
  assert.equal(noActor.steps[0].actor.kind, 'agent')
})

test('the last leg is the current one, and is marked as yours when it is', () => {
  const timeline = [ev('Started', 'agent', 'refunds-agent'), ev('Waiting on someone', 'human', 'Alex')]
  const yours = processSteps(timeline, {
    holder: { kind: 'human', name: 'Alex' },
    status: 'waiting_on_you',
  })
  const last = yours.steps[yours.steps.length - 1]
  assert.equal(last.isCurrent, true)
  assert.equal(last.isYou, true)

  const theirs = processSteps(timeline, {
    holder: { kind: 'human', name: 'Alex' },
    status: 'waiting_on_other',
  })
  assert.equal(theirs.steps[theirs.steps.length - 1].isYou, false)
})

test('a finished job has no current step', () => {
  const { steps } = processSteps([ev('Started', 'agent', 'a'), ev('Done', 'agent', 'a')], {
    status: 'done',
    holder: { kind: 'agent', name: 'a' },
  })
  assert.ok(steps.every((s) => !s.isCurrent))
})

test('the holder wins when the timeline never recorded the handoff', () => {
  // An aging wait whose handoff produced no follow-up event: the route must
  // still end with the person actually holding it.
  const { steps } = processSteps([ev('Started', 'agent', 'support-agent')], {
    holder: { kind: 'human', name: 'Sarah Chen' },
    status: 'waiting_on_other',
  })
  const last = steps[steps.length - 1]
  assert.equal(last.actor.kind, 'human')
  assert.equal(last.actor.name, 'Sarah Chen')
})

test('a long route truncates from the OLDEST end and says how many are hidden', () => {
  const timeline = []
  for (let i = 0; i < 10; i++) timeline.push(ev(`Step ${i}`, 'agent', `agent-${i}`))
  const { steps, hidden } = processSteps(timeline)
  assert.equal(steps.length, MAX_STEPS)
  assert.equal(hidden, 10 - MAX_STEPS)
  // The most recent legs survive — the end of the journey is what matters.
  assert.equal(steps[steps.length - 1].actor.name, 'agent-9')
})

test('history is the log: newest last, capped, and text-only entries', () => {
  const timeline = []
  for (let i = 0; i < 9; i++) timeline.push(ev(`E${i}`, 'agent', 'a', `2026-03-1${i % 9}T00:00:00Z`))
  timeline.push({ at: null, text: '' }) // no text -> not an event
  const rows = shortHistory(timeline)
  assert.equal(rows.length, 5)
  assert.equal(rows[rows.length - 1].text, 'E8')
})

test('empty timeline yields no steps and no history, never a crash', () => {
  assert.deepEqual(processSteps(null), { steps: [], hidden: 0 })
  assert.deepEqual(shortHistory(undefined), [])
})

// --- when the pane may ask for a decision ----------------------------------

test('CTAs need BOTH waiting_on_you and a handoff id to resolve against', () => {
  assert.equal(canDecide({ status: 'waiting_on_you', awaiting_handoff_event_id: 12 }), true)
  // On you, but the server gave us nothing to call — show no buttons rather
  // than dead ones.
  assert.equal(canDecide({ status: 'waiting_on_you', awaiting_handoff_event_id: null }), false)
  assert.equal(canDecide({ status: 'stuck', awaiting_handoff_event_id: 12 }), false)
  assert.equal(canDecide({ status: 'waiting_on_other', awaiting_handoff_event_id: 12 }), false)
  assert.equal(canDecide(null), false)
})

test('Ask opens with a question about this job', () => {
  assert.match(askPrompt({ title: 'Countersign the Acme renewal' }), /Acme renewal/)
  assert.match(askPrompt({ title: 'Reconcile March', status: 'stuck' }), /^Why is "Reconcile March" stuck\?$/)
  assert.match(askPrompt({}), /waiting on me/)
})

// --- panel wiring ----------------------------------------------------------

test('judgment CTAs render only when the job is waiting on you', () => {
  // The whole action block sits behind waitingOnYou, and Approve/Send back sit
  // behind `decidable` inside it.
  assert.match(code, /const waitingOnYou = view\.status === 'waiting_on_you'/)
  assert.match(code, /\{waitingOnYou && \(/)
  assert.match(code, /\{decidable && \(/)
  for (const label of [/>\s*\{busy === 'approve' \? 'Approving…' : 'Approve'\}/, /'Sending back…' : 'Send back'/, /Ask\s*<\/button>/]) {
    assert.match(code, label)
  }
})

test('the CTAs call real endpoints with the handoff id', () => {
  assert.match(code, /api\.completeHandoff\(item\.id, handoffId\)/)
  assert.match(code, /api\.declineHandoff\(item\.id, handoffId\)/)
  assert.match(code, /if \(!handoffId \|\| busy\) return/)
})

test('open and close: scrim, Back button and Escape all leave', () => {
  assert.match(code, /className="bpanel-scrim" onClick=\{onClose\}/)
  assert.match(code, /className="jobd-close" onClick=\{onClose\}/)
  assert.match(code, /if \(e\.key === 'Escape'\) onClose\(\)/)
})

test('the pane reads the LEAN detail, never the fat loop or board', () => {
  assert.match(code, /\.getWorkItem\(item\.id, \{ signal \}\)/)
  assert.doesNotMatch(code, /getLoop\(/)
  assert.doesNotMatch(code, /getWorkBoard/)
  assert.doesNotMatch(code, /getWorkSummary/)
})

test('runs are collapsed and cost nothing until opened', () => {
  const runs = code.slice(code.indexOf('function AgentRuns'))
  assert.match(runs, /useState\(false\)/)
  assert.match(runs, /if \(!open \|\| asked\.current\) return undefined/)
  assert.match(runs, /getWorkItem\(itemId, \{ include: 'runs', signal \}\)/)
})

test('Work opens this pane, not the old loop panel', () => {
  // Home used to open work items from its desk. It now sends people INTO
  // Work instead of reproducing the item pane, so this rule is Work's.
  for (const f of ['WorkTab.jsx']) {
    const src = readFileSync(new URL(`../src/${f}`, import.meta.url), 'utf8')
    assert.match(src, /import JobDetail from '\.\/JobDetail\.jsx'/, f)
    assert.match(src, /<JobDetail/, f)
    assert.doesNotMatch(src, /TaskPanel/, f)
  }
})

test('the detail API forwards include + signal', () => {
  const api = readFileSync(new URL('../src/api.js', import.meta.url), 'utf8')
  assert.match(api, /getWorkItem: \(id, \{ include = null, signal = undefined \} = \{\}\)/)
})
