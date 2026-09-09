// Home as the worker's opening: what is waiting on ME, is anything stuck, is
// the rest of the day moving.
//
// These cover the rules that decide what a person actually reads on first
// paint — whose work lands on the desk, when a section vanishes, and the exact
// shape of the one sentence at the top. All pure: no renderer.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import {
  askChips,
  clockLabel,
  deskEmptyCopy,
  deskItems,
  fleetPulse,
  noticedLines,
  proofCounts,
  stuckItems,
  stuckNotice,
  stuckSince,
} from '../src/home.js'

const NOW = Date.parse('2026-03-10T16:30:00Z')
const agoS = (s) => new Date(NOW - s * 1000).toISOString()

function item(over = {}) {
  return {
    id: over.id ?? 1,
    title: over.title ?? 'Approve refund #4821',
    status: over.status ?? 'moving',
    holder: over.holder ?? { kind: 'agent', name: 'Support Bot' },
    whats_next: over.whats_next ?? 'In progress',
    updated_at: over.updated_at ?? agoS(60),
    ...over,
  }
}

// --- 3. your desk is YOURS --------------------------------------------------

test('the desk is only what is waiting on the signed-in user', () => {
  // waiting_on_you is resolved server-side against the session identity, so
  // this filter IS "mine". Everything else on this list belongs to someone
  // else's desk or to Trovis noticed.
  const items = [
    item({ id: 1, status: 'waiting_on_you' }),
    item({ id: 2, status: 'waiting_on_other' }), // a teammate's wait
    item({ id: 3, status: 'stuck' }), // org-wide stuck
    item({ id: 4, status: 'moving' }),
    item({ id: 5, status: 'done' }),
  ]
  assert.deepEqual(deskItems(items).map((i) => i.id), [1])
})

test('a stale wait on someone else never drifts onto your desk', () => {
  // "Needs a human" is not the same claim as "needs YOU". An eight-hour wait
  // on a teammate is Trovis-noticed material at most, never your desk.
  const items = [item({ id: 7, status: 'waiting_on_other', updated_at: agoS(8 * 3600) })]
  assert.deepEqual(deskItems(items), [])
})

test('the desk puts the newest wait first', () => {
  const items = [
    item({ id: 1, status: 'waiting_on_you', updated_at: agoS(7200) }),
    item({ id: 2, status: 'waiting_on_you', updated_at: agoS(60) }),
  ]
  assert.deepEqual(deskItems(items).map((i) => i.id), [2, 1])
})

test('shell and id-shaped titles never reach the desk', () => {
  const items = [
    item({ id: 1, status: 'waiting_on_you', title: 'run_4821' }),
    item({ id: 2, status: 'waiting_on_you', title: 'Approve refund #4821' }),
  ]
  assert.deepEqual(deskItems(items).map((i) => i.id), [2])
})

test('an empty desk is empty, not a placeholder row', () => {
  assert.deepEqual(deskItems([]), [])
  assert.deepEqual(deskItems(null), [])
  assert.deepEqual(deskItems([item({ status: 'moving' })]), [])
})

// --- 2. what the desk says when it is clear --------------------------------

test('a clear desk states one fact and infers nothing about the rest', () => {
  // The bug this replaced: "Nothing is waiting on you. Work is moving." was
  // rendered above a strip reading 0 moving. The second clause was derived
  // from the ABSENCE of stuck work, not from any count of moving work.
  const clear = deskEmptyCopy({ connected: true })
  assert.equal(clear.lead, 'Nothing is waiting on you.')
  assert.equal(clear.sub, 'No tasks on your plate.')
  for (const s of [clear.lead, clear.sub]) {
    assert.doesNotMatch(s, /moving|in progress|on track/i, `infers the rest of the day: ${s}`)
  }
})

test('nothing connected is a different fact from a clear desk', () => {
  const fresh = deskEmptyCopy({ connected: false })
  assert.match(fresh.lead, /Nothing is connected yet/)
  assert.notEqual(fresh.lead, deskEmptyCopy({ connected: true }).lead)
})

test('desk copy carries no digits and no money', () => {
  // Counts live in the strip, and nowhere else.
  for (const connected of [true, false]) {
    const { lead, sub } = deskEmptyCopy({ connected })
    for (const s of [lead, sub]) {
      assert.doesNotMatch(s, /\d/, `desk copy has a digit: ${s}`)
      assert.doesNotMatch(s, /[$€£]/, `desk copy has money: ${s}`)
    }
  }
})

// --- fleet pulse ------------------------------------------------------------

test('the pulse counts agents from the honest total, and lists only the flagged', () => {
  const { count, needLook } = fleetPulse({
    agentCount: 12,
    attention: [
      { agent: 'Support Bot', service_name: 'support', severity: 'critical', title: 'x' },
      { agent: 'Ops Bot', service_name: 'ops', severity: 'warning', title: 'y' },
    ],
  })
  assert.equal(count, 12)
  assert.deepEqual(needLook.map((a) => a.agent), ['Support Bot', 'Ops Bot'])
})

test('the pulse says nothing rather than guessing the count', () => {
  // /dashboard/cost has not landed yet: null is "we do not know", which is
  // not zero, and the caller renders no number at all.
  assert.equal(fleetPulse({ agentCount: null, attention: [] }).count, null)
  assert.equal(fleetPulse({ agentCount: undefined, attention: [] }).count, null)
})

test('the same agent flagged twice is one entry', () => {
  const { needLook } = fleetPulse({
    agentCount: 3,
    attention: [
      { agent: 'Support Bot', service_name: 'support', agent_id: 'main', title: 'a' },
      { agent: 'Support Bot', service_name: 'support', agent_id: 'main', title: 'b' },
      { agent: 'Support Bot', service_name: 'support', agent_id: 'researcher', title: 'c' },
    ],
  })
  // Same service AND sub-agent collapses; a different sub-agent does not.
  assert.equal(needLook.length, 2)
})

test('a clean pulse is a count and nothing else', () => {
  const { count, needLook } = fleetPulse({ agentCount: 4, attention: [] })
  assert.equal(count, 4)
  assert.deepEqual(needLook, [])
})

// --- 4. Trovis noticed ------------------------------------------------------

test('a stuck line appears exactly when something is stuck', () => {
  const none = noticedLines({ items: [item({ status: 'moving' })], attention: [], nowMs: NOW })
  assert.equal(none.filter((l) => l.kind === 'stuck').length, 0)

  const some = noticedLines({
    items: [item({ id: 9, status: 'stuck', updated_at: agoS(3600) })],
    attention: [],
    nowMs: NOW,
  })
  assert.equal(some.filter((l) => l.kind === 'stuck').length, 1)
})

test('the stuck line names the work when there is one, counts it when there are many', () => {
  const one = stuckNotice(
    [item({ status: 'stuck', title: 'Approve refund #4821', updated_at: agoS(3600) })],
    NOW,
    'en-US',
  )
  assert.match(one, /^Approve refund #4821 is stuck since /)

  const many = stuckNotice(
    [
      item({ id: 1, status: 'stuck', updated_at: agoS(3600) }),
      item({ id: 2, status: 'stuck', updated_at: agoS(7200) }),
    ],
    NOW,
    'en-US',
  )
  assert.match(many, /^2 tasks stuck since /)
})

test('the stuck line is timed from the LONGEST-stuck thing', () => {
  // "since 4pm" has to mean the beginning of the problem, not the newest
  // arrival, or the line understates how long this has been going on.
  const items = [
    item({ id: 1, status: 'stuck', updated_at: agoS(3600) }),
    item({ id: 2, status: 'stuck', updated_at: agoS(4 * 3600) }),
  ]
  assert.equal(stuckItems(items)[0].id, 2)
  assert.equal(
    stuckNotice(items, NOW, 'en-US'),
    `2 tasks stuck ${stuckSince(items[1].updated_at, NOW, 'en-US')}`,
  )
})

test('a clock time is only used while it still means today', () => {
  assert.match(stuckSince(agoS(3600), NOW, 'en-US'), /^since \d/)
  // Past a day, "since 4pm" is misleading — say how long instead.
  assert.equal(stuckSince(agoS(3 * 86400), NOW), 'for 3d')
  // Unusable timestamp → no time claim at all.
  assert.equal(stuckSince(null, NOW), '')
  assert.equal(stuckSince('not a date', NOW), '')
})

test('clock labels drop a zero minute and read like a person wrote them', () => {
  assert.equal(clockLabel('2026-03-10T16:00:00Z', 'en-US'), '4pm')
  assert.equal(clockLabel('2026-03-10T16:30:00Z', 'en-US'), '4:30pm')
  assert.equal(clockLabel(null), '')
})

test('agent-health lines ride along, capped, stuck first', () => {
  const lines = noticedLines({
    items: [item({ status: 'stuck', updated_at: agoS(3600) })],
    attention: [
      { agent: 'Support Bot', service_name: 'support-agent', severity: 'critical', title: 'Elevated error rate' },
      { agent: 'Billing Bot', service_name: 'billing', severity: 'warning', title: 'Quiet for 3 hours' },
      { agent: 'Ops Bot', service_name: 'ops', severity: 'warning', title: 'Behavior drifted' },
      { agent: 'Extra Bot', service_name: 'extra', severity: 'warning', title: 'Should not fit' },
    ],
    nowMs: NOW,
  })
  assert.equal(lines.length, 3, 'capped at three')
  assert.equal(lines[0].kind, 'stuck', 'stuck always leads')
  assert.match(lines[1].text, /^Support Bot — /)
})

test('every noticed line carries an action, not just a worry', () => {
  const lines = noticedLines({
    items: [item({ id: 1, status: 'stuck', updated_at: agoS(3600) })],
    attention: [{ agent: 'Support Bot', service_name: 'support-agent', severity: 'warning', title: 'Quiet' }],
    nowMs: NOW,
  })
  assert.ok(lines.length >= 2)
  for (const l of lines) {
    assert.ok(l.action && l.action.trim(), `no action on: ${l.text}`)
    assert.ok(l.target && l.target.to, `no target on: ${l.text}`)
  }
  assert.equal(lines[0].action, 'Open it')
  assert.equal(lines[1].action, 'Review agent')
})

test('several stuck things send you to the filtered list, not one of them', () => {
  const lines = noticedLines({
    items: [
      item({ id: 1, status: 'stuck', updated_at: agoS(3600) }),
      item({ id: 2, status: 'stuck', updated_at: agoS(60) }),
    ],
    attention: [],
    nowMs: NOW,
  })
  assert.equal(lines[0].action, 'See stuck work')
})

test('every noticed line knows where it goes', () => {
  const lines = noticedLines({
    items: [
      item({ id: 1, status: 'stuck', updated_at: agoS(3600) }),
      item({ id: 2, status: 'stuck', updated_at: agoS(60) }),
    ],
    attention: [{ agent: 'Support Bot', service_name: 'support-agent', severity: 'warning', title: 'Quiet' }],
    nowMs: NOW,
  })
  // Several stuck things go to Work already filtered; one goes to itself.
  assert.deepEqual(lines[0].target, { to: 'work', filter: 'stuck' })
  assert.equal(lines[1].target.to, 'agent')
  const single = noticedLines({
    items: [item({ id: 5, status: 'stuck', updated_at: agoS(60) })],
    attention: [],
    nowMs: NOW,
  })
  assert.equal(single[0].target.to, 'work-item')
  assert.equal(single[0].target.item.id, 5)
})

test('an attention row with no title is dropped, not rendered blank', () => {
  const lines = noticedLines({
    items: [],
    attention: [{ agent: 'Support Bot', severity: 'warning', title: '' }, { agent: '', title: 'x' }],
    nowMs: NOW,
  })
  assert.deepEqual(lines, [])
})

// --- 5. the proof strip -----------------------------------------------------

test('the strip counts what the sections show, so the page cannot disagree', () => {
  const items = [
    item({ id: 1, status: 'waiting_on_you' }),
    item({ id: 2, status: 'waiting_on_other' }),
    item({ id: 3, status: 'stuck' }),
    item({ id: 4, status: 'moving' }),
    item({ id: 5, status: 'moving' }),
  ]
  const c = proofCounts(items, NOW)
  assert.equal(c.moving, 2)
  // Waiting in the strip is EVERYONE's waits — yours plus your teammates'.
  assert.equal(c.waiting, 2)
  assert.equal(c.stuck, 1)
  // ...and the desk is the narrower cut of the same rows.
  assert.equal(deskItems(items).length, 1)
})

test('done means done TODAY, not done this week', () => {
  const items = [
    item({ id: 1, status: 'done', updated_at: agoS(3600) }), // today
    item({ id: 2, status: 'done', updated_at: agoS(3 * 86400) }), // this week
  ]
  assert.equal(proofCounts(items, NOW).done, 1)
})

test('strip counts survive missing and junk rows', () => {
  assert.deepEqual(proofCounts(null), { moving: 0, waiting: 0, stuck: 0, done: 0 })
  assert.equal(proofCounts([item({ title: 'run_4821', status: 'moving' })]).moving, 0)
  assert.equal(proofCounts([item({ status: 'done', updated_at: null })]).done, 0)
})

// --- language ---------------------------------------------------------------

const FORBIDDEN = /\b(loops?|workloops?|possession|segments?|stations?|handoffs?)\b/i

test('nothing home.js produces can ship Trovis jargon', () => {
  const strings = [
    deskEmptyCopy({ connected: true }).lead,
    deskEmptyCopy({ connected: true }).sub,
    deskEmptyCopy({ connected: false }).lead,
    deskEmptyCopy({ connected: false }).sub,
    stuckNotice([item({ status: 'stuck', updated_at: agoS(60) })], NOW),
    stuckSince(agoS(60), NOW),
    ...noticedLines({
      items: [item({ status: 'stuck', updated_at: agoS(60) })],
      attention: [{ agent: 'Bot', service_name: 's', title: 'Quiet' }],
      nowMs: NOW,
    }).flatMap((l) => [l.text, l.action]),
    ...askChips({
      desk: [item({ status: 'waiting_on_you' })],
      counts: { waiting: 2, stuck: 1 },
      attention: [{ agent: 'Bot' }],
      costToday: 1.5,
    }).flatMap((c) => [c.label, c.query]),
  ]
  for (const s of strings) {
    assert.ok(!FORBIDDEN.test(s), `home.js ships jargon: ${JSON.stringify(s)}`)
  }
})

test('the desk actions never say "handoff" to a person', () => {
  // The write path is a handoff resolution; the words on the buttons are not.
  const src = readFileSync(new URL('../src/Dashboard.jsx', import.meta.url), 'utf8')
  const code = src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')
  const actions = code.slice(code.indexOf('const DESK_ACTIONS'), code.indexOf('function DeskRow'))
  for (const m of actions.matchAll(/(?:label|busy):\s*["'`]([^"'`]+)["'`]/g)) {
    assert.ok(!FORBIDDEN.test(m[1]), `desk action ships jargon: ${JSON.stringify(m[1])}`)
  }
  assert.match(actions, /label: 'Done'/)
  assert.match(actions, /label: "I've got this"/)
  assert.match(actions, /label: 'Not mine'/)
})

// --- Ask presets ------------------------------------------------------------

test('a chip is only offered when its answer exists', () => {
  // "What's stuck?" on a day with nothing stuck asks Trovis to describe an
  // empty set — a worse first impression than no chip at all.
  const none = askChips({ desk: [], counts: { waiting: 0, stuck: 0 }, attention: [], costToday: 0 })
  assert.deepEqual(none, [])
})

test('each chip appears exactly when its own condition holds', () => {
  const keyFor = (o) => askChips(o).map((c) => c.key)
  assert.deepEqual(keyFor({ desk: [item()], counts: {}, attention: [], costToday: 0 }), ['mine'])
  assert.deepEqual(keyFor({ desk: [], counts: { waiting: 3 }, attention: [], costToday: 0 }), ['others'])
  assert.deepEqual(keyFor({ desk: [], counts: { stuck: 1 }, attention: [], costToday: 0 }), ['stuck'])
  assert.deepEqual(
    keyFor({ desk: [], counts: {}, attention: [{ agent: 'Support Bot' }], costToday: 0 }),
    ['agent'],
  )
  assert.deepEqual(keyFor({ desk: [], counts: {}, attention: [], costToday: 0.42 }), ['cost'])
})

test('a sub-cent day gets no cost chip', () => {
  // "What cost $0.00 today?" is not a question anyone has.
  assert.deepEqual(askChips({ desk: [], counts: {}, attention: [], costToday: 0.004 }), [])
  assert.equal(askChips({ desk: [], counts: {}, attention: [], costToday: 0.01 }).length, 1)
})

test('the cost chip quotes the strip figure rather than inventing one', () => {
  const [chip] = askChips({ desk: [], counts: {}, attention: [], costToday: 29.815 })
  assert.match(chip.label, /\$29\.82/)
})

test('the chip row is capped', () => {
  const chips = askChips({
    desk: [item()],
    counts: { waiting: 2, stuck: 1 },
    attention: [{ agent: 'Support Bot' }],
    costToday: 5,
  })
  assert.ok(chips.length <= 4, `chip row is ${chips.length} wide`)
})

test('the agent chip names the agent it is about', () => {
  const [chip] = askChips({
    desk: [], counts: {}, attention: [{ agent: 'Billing Bot' }], costToday: 0,
  })
  assert.match(chip.label, /Billing Bot/)
  assert.match(chip.query, /Billing Bot/)
})
