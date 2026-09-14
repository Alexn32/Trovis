// Home, MOUNTED and driven.
//
// Every bug this file covers shipped with 572 green tests. A regex can confirm
// a string exists; it cannot confirm that narrowing the scope removes what is
// on screen, that "Open this work item" opens that item, or that a failed
// "Mark seen" tells anybody. So these render the real components into jsdom
// with the API stubbed at the module boundary, and drive them.
//
// The API is stubbed by replacing methods on the exported `api` singleton —
// no loader hooks, no new runner. Responses are deferred where ordering is
// the point, so out-of-order delivery is deterministic rather than a race.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  ME, deferred, findingFixture, findingsFixture, installDom, mount,
  seatFixture, snapshotFixture,
} from './mount.mjs'

installDom()

const React = await import('react')
const { api } = await import('../src/api.js')
const HomeView = (await import('../src/HomeView.jsx')).default
const { homeContextKey } = await import('../src/HomeView.jsx')

const h = React.createElement

/** Record every call and answer from a queue the test controls. */
function stubApi({ snapshot, findings, finding, setState } = {}) {
  const calls = { snapshot: [], findings: [], finding: [], setState: [] }
  api.getHomeSnapshot = (q) => {
    calls.snapshot.push(q)
    return snapshot ? snapshot(q, calls.snapshot.length - 1) : Promise.resolve(snapshotFixture())
  }
  api.getHomeFindings = (q) => {
    calls.findings.push(q)
    return findings ? findings(q, calls.findings.length - 1) : Promise.resolve(findingsFixture())
  }
  api.getHomeFinding = (id, q) => {
    calls.finding.push({ id, q })
    return finding ? finding(id, q) : Promise.resolve({
      finding: findingFixture(), claims: [], evidence: [], uncertainty: [],
      stale_evidence: [], navigation: { targets: [] },
    })
  }
  api.setFindingState = (id, state, q) => {
    calls.setState.push({ id, state, q })
    return setState ? setState(id, state, q) : Promise.resolve(findingFixture({ state }))
  }
  return calls
}

function home(props = {}) {
  return h(HomeView, {
    seat: seatFixture(), me: ME, people: [], active: true,
    onGoWork: () => {}, onOpenCost: () => {}, onOpenAgent: () => {},
    onOpenJob: () => {}, onOpenRun: () => {}, onConnectAgent: () => {},
    ...props,
  })
}


/** The headline completions figure, read from its own element. */
function completions(m) {
  return m.$('.hv-big')?.textContent ?? null
}

/** Change one of Home's controls by its visible label. */
async function setControl(m, label, value) {
  const { act } = await import('react')
  const lab = m.$$('label.hv-control').find((l) => l.textContent.includes(label))
  assert.ok(lab, `the ${label} control is rendered`)
  const sel = lab.querySelector('select')
  sel.value = String(value)
  await act(async () => {
    sel.dispatchEvent(new window.Event('change', { bubbles: true }))
  })
  return sel
}

// ---------------------------------------------------------------------------
// 1. Context invalidation
// ---------------------------------------------------------------------------

test('company data disappears the moment the scope narrows', async () => {
  const held = deferred()
  stubApi({
    snapshot: (q, i) =>
      i === 0
        ? Promise.resolve(snapshotFixture({ period: { ...snapshotFixture().period, completed: 27 } }))
        // The replacement is held open: the point is what is on screen WHILE
        // it is in flight.
        : held.promise,
  })
  const seat = seatFixture({ subtree_user_ids: [2, 3] })
  const m = await mount(home({ seat }))
  await m.settle()
  assert.equal(completions(m), '27', 'company data is on screen first')

  // Narrow the scope with the real control. The old body must be gone BEFORE
  // the replacement resolves.
  await setControl(m, 'Work scope', 'me')

  assert.notEqual(completions(m), '27',
    'the previous scope’s number is gone before the replacement arrives')
  held.resolve(snapshotFixture({ period: { ...snapshotFixture().period, completed: 4 } }))
  await m.settle()
  assert.equal(completions(m), '4', 'the new scope’s number arrives')
  m.unmount()
})

test('losing the Cost surface removes financial findings and the cost section at once', async () => {
  const financial = findingFixture({
    id: 9, title: 'Spend on refunds rose', requires_financial: true,
    explanation: 'The refund agent cost more this period.',
  })
  const held = deferred()
  stubApi({
    snapshot: (q, i) => (i === 0 ? Promise.resolve(snapshotFixture()) : held.promise),
    findings: (q, i) => (i === 0 ? Promise.resolve(findingsFixture([financial])) : held.promise),
  })
  const m = await mount(home({ seat: seatFixture() }))
  await m.settle()
  assert.match(m.text(), /Spend on refunds rose/)
  assert.match(m.text(), /recorded spend/)

  // The seat loses Cost. Nothing may wait for the replacement request.
  const narrowed = seatFixture({ surfaces: ['Home', 'Work', 'Fleet', 'Ask', 'Connect'] })
  await m.render(home({ seat: narrowed }))
  assert.doesNotMatch(m.text(), /Spend on refunds rose/,
    'a financial finding is gone immediately')
  assert.doesNotMatch(m.text(), /recorded spend/, 'and so is the cost section')
  m.unmount()
})

test('a membership change invalidates with breadth and surfaces unchanged', async () => {
  const calls = stubApi({})
  const seat = seatFixture({ visible_user_ids: [1, 2, 3] })
  const m = await mount(home({ seat }))
  await m.settle()
  assert.equal(calls.snapshot.length, 1)

  // Same breadth, same surfaces — one person left the subtree.
  const reorged = seatFixture({ visible_user_ids: [1, 2] })
  assert.equal(reorged.breadth, seat.breadth)
  assert.deepEqual(reorged.surfaces, seat.surfaces)
  await m.render(home({ seat: reorged }))
  await m.settle()
  assert.equal(calls.snapshot.length, 2, 'the membership change re-asked')
  m.unmount()
})

test('the context key covers account, identity, visibility and membership', () => {
  const base = { me: ME, seat: seatFixture(), days: 7, tz: 'UTC', whose: 'everyone', personId: null }
  const k = homeContextKey(base)
  assert.notEqual(k, homeContextKey({ ...base, me: { user: { id: 2 }, org: { id: 1 } } }))
  assert.notEqual(k, homeContextKey({ ...base, me: { user: { id: 1 }, org: { id: 9 } } }))
  assert.notEqual(k, homeContextKey({ ...base, seat: seatFixture({ surfaces: ['Home'] }) }))
  assert.notEqual(k, homeContextKey({ ...base, seat: seatFixture({ breadth: 'self' }) }))
  assert.notEqual(k, homeContextKey({ ...base, seat: seatFixture({ visible_user_ids: [1] }) }))
  assert.notEqual(k, homeContextKey({ ...base, seat: seatFixture({ subtree_user_ids: [4] }) }))
  assert.notEqual(k, homeContextKey({ ...base, days: 30 }))
  // Order-insensitive, so a re-sorted list is not a new context.
  assert.equal(
    homeContextKey({ ...base, seat: seatFixture({ visible_user_ids: [3, 1] }) }),
    homeContextKey({ ...base, seat: seatFixture({ visible_user_ids: [1, 3] }) }),
  )
  // company-wide (null) and nobody ([]) are different keys, as server-side.
  assert.notEqual(
    homeContextKey({ ...base, seat: seatFixture({ visible_user_ids: null }) }),
    homeContextKey({ ...base, seat: seatFixture({ visible_user_ids: [] }) }),
  )
})

test('an out-of-order response cannot restore old content', async () => {
  const first = deferred()
  stubApi({
    snapshot: (q, i) =>
      i === 0 ? first.promise
      : Promise.resolve(snapshotFixture({ period: { ...snapshotFixture().period, completed: 4 } })),
  })
  const m = await mount(home({ seat: seatFixture() }))
  // Change the context while the first request is still open.
  await setControl(m, 'Period', 30)
  await m.settle()
  assert.equal(completions(m), '4')
  // The first scope's answer now arrives, late.
  first.resolve(snapshotFixture({ period: { ...snapshotFixture().period, completed: 27 } }))
  await m.settle()
  assert.equal(completions(m), '4', 'the stale answer did not land')
  m.unmount()
})

test('an old snapshot cannot sit beside new findings as one context', async () => {
  const slowFindings = deferred()
  stubApi({
    snapshot: (q, i) =>
      Promise.resolve(snapshotFixture({
        period: { ...snapshotFixture().period, completed: i === 0 ? 27 : 4 },
      })),
    findings: (q, i) => (i === 0 ? Promise.resolve(findingsFixture()) : slowFindings.promise),
  })
  const m = await mount(home({ seat: seatFixture() }))
  await m.settle()
  assert.equal(completions(m), '27')
  assert.match(m.text(), /Three invoice runs stopped/)

  await setControl(m, 'Period', 30)
  await m.settle()
  // The snapshot for the new context has landed; the findings have not. The
  // OLD findings must not be showing beside the new numbers.
  assert.equal(completions(m), '4')
  assert.doesNotMatch(m.text(), /Three invoice runs stopped/,
    'findings from the previous context are not rendered under the new one')
  slowFindings.resolve(findingsFixture([findingFixture({ title: 'A new finding' })]))
  await m.settle()
  assert.match(m.text(), /A new finding/)
  m.unmount()
})

test('a context change closes a stale finding detail', async () => {
  stubApi({})
  const m = await mount(home({ seat: seatFixture() }))
  await m.settle()
  const open = m.$('.hv-finding-open')
  assert.ok(open, 'a finding card is on screen')
  await m.click(open)
  await m.settle()
  assert.ok(m.$('.hv-panel'), 'the detail panel opened')

  await setControl(m, 'Period', 30)
  assert.equal(m.$('.hv-panel'), null, 'the panel is gone as soon as the context moves')
  m.unmount()
})

test('a mutation landing after the context moved changes nothing', async () => {
  const held = deferred()
  const calls = stubApi({ setState: () => held.promise })
  const m = await mount(home({ seat: seatFixture() }))
  await m.settle()
  const ack = m.$$('button').find((b) => b.textContent.trim() === 'Mark seen')
  assert.ok(ack)
  await m.click(ack)
  const findingsBefore = calls.findings.length

  await setControl(m, 'Period', 30)
  await m.settle()
  const afterContextChange = calls.findings.length
  held.resolve(findingFixture({ state: 'acknowledged' }))
  await m.settle()
  assert.equal(calls.findings.length, afterContextChange,
    'the stale mutation did not trigger a reload in the new context')
  assert.ok(afterContextChange > findingsBefore, 'the new context did fetch')
  m.unmount()
})

test('snapshot and findings load and fail independently', async () => {
  stubApi({
    snapshot: () => Promise.reject(Object.assign(new Error('boom'), { status: 500 })),
    findings: () => Promise.resolve(findingsFixture()),
  })
  const m = await mount(home())
  await m.settle()
  assert.match(m.text(), /numbers here are unavailable/, 'the snapshot reports its own failure')
  assert.match(m.text(), /Three invoice runs stopped/, 'findings still render')
  m.unmount()

  stubApi({
    snapshot: () => Promise.resolve(snapshotFixture()),
    findings: () => Promise.reject(Object.assign(new Error('boom'), { status: 500 })),
  })
  const m2 = await mount(home())
  await m2.settle()
  assert.equal(completions(m2), '27', 'the numbers still render')
  assert.match(m2.text(), /numbers above are unaffected/, 'findings report their own failure')
  m2.unmount()
})

// ---------------------------------------------------------------------------
// 2 + 4. Navigation
// ---------------------------------------------------------------------------

test('personal attention navigates with the canonical filter and unnarrowed', async () => {
  stubApi({})
  const seen = []
  const m = await mount(home({ onGoWork: (f, scope) => seen.push([f, scope]) }))
  await m.settle()
  const btn = m.$$('button').find((b) => b.textContent.trim() === 'Open your work')
  assert.ok(btn, 'the personal link is rendered')
  await m.click(btn)
  assert.deepEqual(seen[0][0], 'mine',
    "Work's canonical personal filter, not the unsupported 'waiting_on_you'")
  assert.equal(seen[0][1].whose, 'everyone',
    'the personal population is not narrowed by the work scope')
  assert.equal(seen[0][1].personId, null)
  m.unmount()
})

test("Work's filter table actually understands the value Home sends", async () => {
  // The bug was a value Work did not know falling through to "everything".
  const { matchesWorkFilter } = await import('../src/workFilter.js')
  assert.equal(matchesWorkFilter({ status: 'waiting_on_you' }, 'mine'), true)
  assert.equal(matchesWorkFilter({ status: 'moving' }, 'mine'), false)
  // The old value is not a filter at all — every row matches.
  assert.equal(matchesWorkFilter({ status: 'moving' }, 'waiting_on_you'), true)
})

test('general work links carry the selected scope', async () => {
  stubApi({})
  const seen = []
  const seat = seatFixture({ subtree_user_ids: [2, 3] })
  const m = await mount(home({ seat, onGoWork: (f, scope) => seen.push([f, scope]) }))
  await m.settle()
  await setControl(m, 'Work scope', 'team')
  await m.settle()
  const openWork = m.$$('button').find((b) => b.textContent.trim() === 'Open Work')
  assert.ok(openWork)
  await m.click(openWork)
  assert.equal(seen.at(-1)[1].whose, 'team', 'the destination arrives scoped')

  // A current-state count carries it too.
  const nowCell = m.$('.hv-now-cell')
  await m.click(nowCell)
  assert.equal(seen.at(-1)[1].whose, 'team')
  m.unmount()
})

// ---------------------------------------------------------------------------
// 3. Run navigation
// ---------------------------------------------------------------------------

test('"Open this work item" opens THAT item', async () => {
  const opened = []
  stubApi({
    finding: () => Promise.resolve({
      finding: findingFixture(),
      claims: [], evidence: [], uncertainty: [], stale_evidence: [],
      navigation: { targets: [{ kind: 'run', id: 4471, exact: true }] },
    }),
  })
  const m = await mount(home({ onOpenRun: (id) => opened.push(id) }))
  await m.settle()
  await m.click(m.$('.hv-finding-open'))
  await m.settle()
  const go = m.$$('button').find((b) => b.textContent.trim() === 'Open this work item')
  assert.ok(go, 'the run target is offered')
  await m.click(go)
  assert.deepEqual(opened, [4471], 'the actual run id, not a general list')
  assert.equal(m.$('.hv-panel'), null, 'and the panel closed behind it')
  m.unmount()
})

test('agent and job targets reach their own destinations', async () => {
  const agents = []
  const jobs = []
  stubApi({
    finding: () => Promise.resolve({
      finding: findingFixture(),
      claims: [], evidence: [], uncertainty: [], stale_evidence: [],
      navigation: { targets: [
        { kind: 'agent', id: 'refunds-agent', exact: true },
        { kind: 'job', id: 3, exact: false, note: 'Not the period.' },
      ] },
    }),
  })
  const m = await mount(home({
    onOpenAgent: (id) => agents.push(id), onOpenJob: (id) => jobs.push(id),
  }))
  await m.settle()
  await m.click(m.$('.hv-finding-open'))
  await m.settle()
  assert.match(m.text(), /Not the period/, 'the inexact target discloses why')
  await m.click(m.$$('button').find((b) => b.textContent.trim() === 'Open this agent'))
  assert.deepEqual(agents, ['refunds-agent'])
  m.unmount()
})

test('a target with no wired destination is not offered', async () => {
  stubApi({
    finding: () => Promise.resolve({
      finding: findingFixture(),
      claims: [], evidence: [], uncertainty: [], stale_evidence: [],
      navigation: { targets: [{ kind: 'run', id: 41, exact: true }] },
    }),
  })
  // No onOpenRun handler passed.
  const m = await mount(home({ onOpenRun: undefined }))
  await m.settle()
  await m.click(m.$('.hv-finding-open'))
  await m.settle()
  assert.doesNotMatch(m.text(), /Open this work item/,
    'a dead destination is not advertised')
  m.unmount()
})

// ---------------------------------------------------------------------------
// 5. Dismissed findings
// ---------------------------------------------------------------------------

test('dismissed findings are revisitable and kept out of the active groups', async () => {
  const dismissed = findingFixture({ id: 77, title: 'An old worry', state: 'dismissed' })
  const calls = stubApi({
    findings: (q) =>
      Promise.resolve(
        q.includeDismissed
          ? findingsFixture([findingFixture(), dismissed])
          : findingsFixture([findingFixture()]),
      ),
  })
  const m = await mount(home())
  await m.settle()
  assert.doesNotMatch(m.text(), /An old worry/, 'not in the active groups')

  const details = m.$('.hv-dismissed')
  assert.ok(details, 'there is a way back')
  const { act } = await import('react')
  await act(async () => {
    details.open = true
    details.dispatchEvent(new window.Event('toggle', { bubbles: false }))
  })
  await m.settle()
  assert.ok(
    calls.findings.some((q) => q.includeDismissed === true),
    'it uses the existing include_dismissed capability',
  )
  const withScope = calls.findings.find((q) => q.includeDismissed)
  assert.equal(withScope.days, 7, 'carrying the same period')
  assert.equal(withScope.whose, 'everyone', 'and the same scope')
  assert.match(m.text(), /An old worry/, 'the dismissed finding is reachable')
  assert.match(m.text(), /Dismissed/, 'and clearly marked')
  m.unmount()
})

// ---------------------------------------------------------------------------
// 6. Acknowledgement failures
// ---------------------------------------------------------------------------

test('a failed "Mark seen" is visible, retryable, and keeps the finding', async () => {
  let attempt = 0
  const calls = stubApi({
    setState: () => {
      attempt += 1
      return attempt === 1
        ? Promise.reject(new Error('Trovis did not respond'))
        : Promise.resolve(findingFixture({ state: 'acknowledged' }))
    },
  })
  const m = await mount(home())
  await m.settle()
  const ack = () => m.$$('button').find((b) => /Mark seen|Try again/.test(b.textContent))
  await m.click(ack())
  await m.settle()

  assert.match(m.text(), /Couldn’t mark this seen/, 'the failure is stated')
  assert.match(m.text(), /Trovis did not respond/, 'with the reason')
  const alert = m.$('[role="alert"].hv-finding-ack-error')
  assert.ok(alert, 'announced to assistive tech')
  assert.match(m.text(), /Three invoice runs stopped/, 'the finding survives')
  assert.doesNotMatch(m.text(), /Acknowledged/, 'and keeps its prior state')

  // And it can be retried.
  const retry = ack()
  assert.match(retry.textContent, /Try again/)
  await m.click(retry)
  await m.settle()
  assert.equal(calls.setState.length, 2)
  assert.doesNotMatch(m.text(), /Couldn’t mark this seen/, 'the error clears on success')
  m.unmount()
})

test('the detail panel surfaces its own mutation failure', async () => {
  stubApi({ setState: () => Promise.reject(new Error('nope')) })
  const m = await mount(home())
  await m.settle()
  await m.click(m.$('.hv-finding-open'))
  await m.settle()
  // Scoped to the panel: the card behind it has a button of the same name.
  const seen = [...m.$('.hv-panel').querySelectorAll('button')]
    .find((b) => b.textContent.trim() === 'Mark seen')
  assert.ok(seen, 'the panel offers Mark seen')
  await m.click(seen)
  await m.settle()
  assert.ok(m.$('.hv-panel-mut-error'), 'the panel says so')
  assert.ok(m.$('.hv-panel'), 'and stays open with its evidence')
  m.unmount()
})

test('rapid acknowledgement clicks do not lose the finding', async () => {
  const calls = stubApi({ setState: () => Promise.reject(new Error('nope')) })
  const m = await mount(home())
  await m.settle()
  const btn = () => m.$$('button').find((b) => /Mark seen|Saving|Try again/.test(b.textContent))
  await m.click(btn())
  await m.click(btn())
  await m.settle()
  assert.match(m.text(), /Three invoice runs stopped/)
  assert.ok(calls.setState.length >= 1)
  m.unmount()
})

// ---------------------------------------------------------------------------
// 7. Personal attention settles
// ---------------------------------------------------------------------------

test('a failed snapshot settles personal attention into an error with retry', async () => {
  let calls = 0
  stubApi({
    snapshot: () => {
      calls += 1
      return calls === 1
        ? Promise.reject(Object.assign(new Error('down'), { status: 500 }))
        : Promise.resolve(snapshotFixture())
    },
  })
  const m = await mount(home())
  await m.settle()
  const box = m.$('.hv-att-personal')
  assert.equal(box.getAttribute('data-att-state'), 'error')
  assert.equal(m.$('.hv-att-personal [role="status"]'), null, 'not a spinner')
  assert.match(box.textContent, /couldn’t load what is waiting on you/i)

  const retry = [...box.querySelectorAll('button')].find((b) => /Retry/.test(b.textContent))
  assert.ok(retry, 'there is a way back')
  await m.click(retry)
  await m.settle()
  assert.equal(m.$('.hv-att-personal').getAttribute('data-att-state'), 'exact')
  m.unmount()
})

test('attention distinguishes loading, unavailable, exact and floor', async () => {
  const cases = [
    [snapshotFixture({ attention: { available: false, viewer_user_id: null } }), 'unavailable'],
    [snapshotFixture({ attention: { available: true, needs_you: 3, viewer_user_id: 1,
                                    resolution_complete: true } }), 'exact'],
    [snapshotFixture({ attention: { available: true, needs_you: 3, viewer_user_id: 1,
                                    resolution_complete: false } }), 'floor'],
  ]
  for (const [snap, expected] of cases) {
    stubApi({ snapshot: () => Promise.resolve(snap) })
    const m = await mount(home())
    await m.settle()
    assert.equal(m.$('.hv-att-personal').getAttribute('data-att-state'), expected)
    m.unmount()
  }
  // Loading is its own state, before anything resolves.
  const held = deferred()
  stubApi({ snapshot: () => held.promise })
  const m = await mount(home())
  assert.equal(m.$('.hv-att-personal').getAttribute('data-att-state'), 'loading')
  held.resolve(snapshotFixture())
  await m.settle()
  m.unmount()
})

// ---------------------------------------------------------------------------
// Polling
// ---------------------------------------------------------------------------

test('polling stops when Home goes inactive and when it unmounts', async () => {
  const calls = stubApi({
    findings: () => Promise.resolve(findingsFixture([], { state: 'running' })),
  })
  const m = await mount(home({ active: true }))
  await m.settle()
  const afterMount = calls.findings.length

  // Off screen: the scheduled check must not fire.
  await m.render(home({ active: false }))
  await m.settle(60)
  assert.equal(calls.findings.length, afterMount, 'no poll while inactive')

  m.unmount()
  await new Promise((r) => setTimeout(r, 60))
  assert.equal(calls.findings.length, afterMount, 'and none after unmount')
})

test('a settled analysis state never polls', async () => {
  for (const state of ['current', 'debounced', 'incomplete', 'failed', 'unavailable']) {
    const calls = stubApi({
      findings: () => Promise.resolve(findingsFixture([], { state })),
    })
    const m = await mount(home())
    await m.settle()
    const n = calls.findings.length
    await m.settle(40)
    assert.equal(calls.findings.length, n, `${state} does not poll`)
    m.unmount()
  }
})

// ---------------------------------------------------------------------------
// 9. Mutation ownership
//
// A mutation is two separate things, and conflating them is the bug this
// section exists for: the SERVER OPERATION (a PATCH that may already have been
// applied, and which nothing here may pretend to undo) and the QUESTION OF
// WHETHER ITS COMPLETION MAY STILL DRIVE THIS UI. The second one expires. A
// panel that has been closed, replaced, or reopened is not the panel that
// started the save; a scope the reader has left does not own a spinner in the
// scope they are in now.
//
// Every case here holds the response open with `deferred()` and delivers it
// after the state it would wrongly touch has moved, because that ordering is
// the whole defect and a timer race would only reproduce it sometimes.
// ---------------------------------------------------------------------------

/** One finding card, found by its title, optionally within a region. */
function card(m, title, root = null) {
  const scope = root ? m.$$(`${root} .hv-finding-card`) : m.$$('.hv-finding-card')
  return scope.find((c) => c.textContent.includes(title))
}

/** A card's acknowledge button, whatever it currently says. */
function ackBtn(cardEl) {
  return [...cardEl.querySelectorAll('button')]
    .find((b) => /Mark seen|Saving|Try again/.test(b.textContent))
}

/** A button inside the detail panel, by exact label. */
function panelBtn(m, label) {
  const panel = m.$('.hv-panel')
  assert.ok(panel, 'the panel is open')
  return [...panel.querySelectorAll('button')].find((b) => b.textContent.trim() === label)
}

async function openDismissed(m) {
  const { act } = await import('react')
  const details = m.$('.hv-dismissed')
  assert.ok(details, 'the dismissed disclosure is rendered')
  await act(async () => {
    details.open = true
    details.dispatchEvent(new window.Event('toggle', { bubbles: false }))
  })
  await m.settle()
}

const TWO = [
  findingFixture({ id: 1, title: 'First worry' }),
  findingFixture({ id: 2, title: 'Second worry' }),
]

function twoFindings({ setState } = {}) {
  return stubApi({
    findings: () => Promise.resolve(findingsFixture(TWO)),
    finding: (id) =>
      Promise.resolve({
        finding: TWO.find((f) => f.id === id) || TWO[0],
        claims: [], evidence: [], uncertainty: [], stale_evidence: [],
        navigation: { targets: [] },
      }),
    setState,
  })
}

test('a dismissal from a panel the reader has left cannot close the panel they are in now', async () => {
  const held = deferred()
  twoFindings({ setState: () => held.promise })
  const m = await mount(home())
  await m.settle()

  await m.click(card(m, 'First worry').querySelector('.hv-finding-open'))
  await m.settle()
  assert.match(m.$('.hv-panel').textContent, /First worry/)
  await m.click(panelBtn(m, 'Dismiss'))
  await m.settle()
  assert.ok(m.$('.hv-panel'), 'still open while the save is in flight')

  // Leave the context. The panel goes with it.
  await setControl(m, 'Period', 14)
  await m.settle()
  assert.equal(m.$('.hv-panel'), null, 'the period change closed the original panel')

  // Open a different finding under the new context.
  await m.click(card(m, 'Second worry').querySelector('.hv-finding-open'))
  await m.settle()
  assert.match(m.$('.hv-panel').textContent, /Second worry/)

  // Now the original dismissal lands.
  held.resolve(findingFixture({ id: 1, state: 'dismissed' }))
  await m.settle()

  assert.ok(m.$('.hv-panel'), 'the newer panel is still open')
  assert.match(m.$('.hv-panel').textContent, /Second worry/,
    'and is still the finding the reader opened')
  assert.equal(m.$('.hv-panel-mut-error'), null,
    'the old operation does not decorate it with an error either')
  m.unmount()
})

test('closing and reopening the same finding disowns the pending mutation', async () => {
  // The read guard cannot catch this on its own: findingId and query are
  // unchanged, so its generation never moves. The panel instance is what
  // changed.
  const held = deferred()
  twoFindings({ setState: () => held.promise })
  const m = await mount(home())
  await m.settle()

  await m.click(card(m, 'First worry').querySelector('.hv-finding-open'))
  await m.settle()
  await m.click(panelBtn(m, 'Dismiss'))
  await m.settle()
  await m.click(panelBtn(m, '← Back'))
  await m.settle()
  assert.equal(m.$('.hv-panel'), null)

  await m.click(card(m, 'First worry').querySelector('.hv-finding-open'))
  await m.settle()
  assert.ok(m.$('.hv-panel'), 'reopened')

  held.resolve(findingFixture({ id: 1, state: 'dismissed' }))
  await m.settle()
  assert.ok(m.$('.hv-panel'), 'the reopened panel stays open')
  assert.equal(panelBtn(m, 'Dismiss').disabled, false,
    'and its own controls are usable, not left disabled by the old save')
  m.unmount()
})

test('a current panel still acknowledges, dismisses, and shows its failures', async () => {
  // The guard must not cost the panel its actual job.
  let dismissed = false
  const calls = twoFindings({
    setState: (id, state) => {
      if (state === 'dismissed') dismissed = true
      return Promise.resolve(findingFixture({ id, state }))
    },
  })
  const m = await mount(home())
  await m.settle()
  await m.click(card(m, 'First worry').querySelector('.hv-finding-open'))
  await m.settle()
  await m.click(panelBtn(m, 'Mark seen'))
  await m.settle()
  assert.equal(calls.setState.at(-1).state, 'acknowledged')
  assert.ok(m.$('.hv-panel'), 'acknowledging keeps the evidence on screen')
  assert.equal(m.$('.hv-panel-mut-error'), null)

  await m.click(panelBtn(m, 'Dismiss'))
  await m.settle()
  assert.ok(dismissed, 'the dismissal reached the server')
  assert.equal(m.$('.hv-panel'), null, 'and the panel closed itself')
  m.unmount()
})

test('leaving a context mid-save never leaves a card stuck on Saving…', async () => {
  const held = deferred()
  stubApi({ setState: () => held.promise })
  const m = await mount(home())
  await m.settle()
  const btn = () => ackBtn(card(m, 'Three invoice runs stopped'))

  await m.click(btn())
  await m.settle()
  assert.match(btn().textContent, /Saving/, 'the save is pending')

  await setControl(m, 'Period', 14)
  await m.settle()
  assert.doesNotMatch(btn().textContent, /Saving/,
    'the other period never shows the pending state')

  // The original save fails while the reader is elsewhere.
  held.reject(new Error('gone'))
  await m.settle()

  await setControl(m, 'Period', 7)
  await m.settle()
  const back = btn()
  assert.doesNotMatch(back.textContent, /Saving/, 'the card is not stuck pending')
  assert.equal(back.disabled, false, 'and can be used again')
  assert.doesNotMatch(m.text(), /Couldn’t mark this seen/,
    'nor does a failure from the scope they left decorate this one')
  m.unmount()
})

test('a stale completion cannot clear a newer save’s pending state', async () => {
  const first = deferred()
  const second = deferred()
  let n = 0
  stubApi({ setState: () => (++n === 1 ? first.promise : second.promise) })
  const m = await mount(home())
  await m.settle()
  const btn = () => ackBtn(card(m, 'Three invoice runs stopped'))

  await m.click(btn())
  await m.settle()
  await setControl(m, 'Period', 14)
  await m.settle()
  await setControl(m, 'Period', 7)
  await m.settle()

  // A second, current save of the same card.
  await m.click(btn())
  await m.settle()
  assert.match(btn().textContent, /Saving/)

  // The first save, from the operation the reader abandoned, now completes.
  first.resolve(findingFixture({ state: 'acknowledged' }))
  await m.settle()
  assert.match(btn().textContent, /Saving/,
    'the older completion does not clear the newer operation')

  second.resolve(findingFixture({ state: 'acknowledged' }))
  await m.settle()
  assert.doesNotMatch(btn().textContent, /Saving/, 'its own completion does')
  m.unmount()
})

test('two cards saving at once keep their own state', async () => {
  const one = deferred()
  const two = deferred()
  twoFindings({ setState: (id) => (id === 1 ? one.promise : two.promise) })
  const m = await mount(home())
  await m.settle()

  await m.click(ackBtn(card(m, 'First worry')))
  await m.click(ackBtn(card(m, 'Second worry')))
  await m.settle()
  assert.match(ackBtn(card(m, 'First worry')).textContent, /Saving/)
  assert.match(ackBtn(card(m, 'Second worry')).textContent, /Saving/)

  one.resolve(findingFixture({ id: 1, state: 'acknowledged' }))
  await m.settle()
  assert.doesNotMatch(ackBtn(card(m, 'First worry')).textContent, /Saving/,
    'the one that finished is done')
  assert.match(ackBtn(card(m, 'Second worry')).textContent, /Saving/,
    'and it did not clear the other')

  two.resolve(findingFixture({ id: 2, state: 'acknowledged' }))
  await m.settle()
  assert.doesNotMatch(ackBtn(card(m, 'Second worry')).textContent, /Saving/)
  m.unmount()
})

test('repeated clicks on a saving card do not start a second operation', async () => {
  const held = deferred()
  const calls = stubApi({ setState: () => held.promise })
  const m = await mount(home())
  await m.settle()
  const btn = () => ackBtn(card(m, 'Three invoice runs stopped'))
  await m.click(btn())
  await m.settle()
  const b = btn()
  // Bypass the disabled attribute the way a keyboard repeat or a stale render
  // can: dispatch the click directly.
  await m.click(b)
  await m.click(b)
  await m.settle()
  assert.equal(calls.setState.length, 1, 'one card, one in-flight save')
  held.resolve(findingFixture({ state: 'acknowledged' }))
  await m.settle()
  m.unmount()
})

test('a successful acknowledgement shows the new state', async () => {
  let acked = false
  stubApi({
    findings: () =>
      Promise.resolve(findingsFixture([findingFixture({ state: acked ? 'acknowledged' : 'open' })])),
    setState: () => {
      acked = true
      return Promise.resolve(findingFixture({ state: 'acknowledged' }))
    },
  })
  const m = await mount(home())
  await m.settle()
  assert.doesNotMatch(m.text(), /Acknowledged/)
  await m.click(ackBtn(card(m, 'Three invoice runs stopped')))
  await m.settle()
  assert.match(m.text(), /Acknowledged/, 'the refreshed card reflects the save')
  m.unmount()
})

// ---------------------------------------------------------------------------
// 10. One collection, two views
// ---------------------------------------------------------------------------

test('dismissing refreshes an already-open dismissed list', async () => {
  let gone = false
  const calls = stubApi({
    findings: (q) =>
      Promise.resolve(
        q.includeDismissed
          ? findingsFixture(
              gone ? [findingFixture({ id: 1, title: 'Live worry', state: 'dismissed' })] : [],
            )
          : findingsFixture(gone ? [] : [findingFixture({ id: 1, title: 'Live worry' })]),
      ),
    finding: () =>
      Promise.resolve({
        finding: findingFixture({ id: 1, title: 'Live worry' }),
        claims: [], evidence: [], uncertainty: [], stale_evidence: [],
        navigation: { targets: [] },
      }),
    setState: (id, state) => {
      if (state === 'dismissed') gone = true
      return Promise.resolve(findingFixture({ id, state }))
    },
  })
  const m = await mount(home())
  await m.settle()
  await openDismissed(m)
  assert.match(m.$('.hv-dismissed-body').textContent, /Nothing dismissed/,
    'the list loaded, and it is empty')
  const readsWhileClosed = calls.findings.filter((q) => q.includeDismissed).length

  await m.click(card(m, 'Live worry', '.hv-att-findings').querySelector('.hv-finding-open'))
  await m.settle()
  await m.click(panelBtn(m, 'Dismiss'))
  await m.settle()

  assert.equal(m.$('.hv-panel'), null, 'the panel closed')
  assert.equal(card(m, 'Live worry', '.hv-att-findings'), undefined,
    'the finding left the active cards')
  assert.match(m.$('.hv-dismissed-body').textContent, /Live worry/,
    'and arrived in the dismissed list that was already open')
  assert.ok(
    calls.findings.filter((q) => q.includeDismissed).length > readsWhileClosed,
    'which took a fresh read of the dismissed view',
  )
  assert.ok(card(m, 'Live worry', '.hv-dismissed').querySelector('.hv-finding-open'),
    'its evidence is still reopenable')
  m.unmount()
})

test('a closed dismissed disclosure is not read in the background, and opens fresh', async () => {
  let gone = false
  const calls = stubApi({
    findings: (q) =>
      Promise.resolve(
        q.includeDismissed
          ? findingsFixture(
              gone ? [findingFixture({ id: 1, title: 'Live worry', state: 'dismissed' })] : [],
            )
          : findingsFixture(gone ? [] : [findingFixture({ id: 1, title: 'Live worry' })]),
      ),
    setState: (id, state) => {
      if (state === 'dismissed') gone = true
      return Promise.resolve(findingFixture({ id, state }))
    },
  })
  const m = await mount(home())
  await m.settle()
  assert.equal(calls.findings.filter((q) => q.includeDismissed).length, 0,
    'nothing is read while the disclosure is closed')

  await m.click(card(m, 'Live worry', '.hv-att-findings').querySelector('.hv-finding-open'))
  await m.settle()
  await m.click(panelBtn(m, 'Dismiss'))
  await m.settle()
  assert.equal(calls.findings.filter((q) => q.includeDismissed).length, 0,
    'and the invalidation does not wake it up either')

  await openDismissed(m)
  assert.match(m.$('.hv-dismissed-body').textContent, /Live worry/,
    'opening it reads the current state of the collection')
  m.unmount()
})

test('a dismissed-list failure stays visible and retryable on its own', async () => {
  let fail = true
  stubApi({
    findings: (q) => {
      if (!q.includeDismissed) return Promise.resolve(findingsFixture())
      return fail
        ? Promise.reject(new Error('boom'))
        : Promise.resolve(
            findingsFixture([findingFixture({ id: 9, title: 'Old worry', state: 'dismissed' })]),
          )
    },
  })
  const m = await mount(home())
  await m.settle()
  await openDismissed(m)
  const body = () => m.$('.hv-dismissed-body')
  assert.match(body().textContent, /couldn't load dismissed findings/i,
    'the dismissed read fails on its own terms')
  assert.match(m.text(), /Three invoice runs stopped/,
    'without taking the active findings down with it')

  fail = false
  const retry = [...body().querySelectorAll('button')]
    .find((b) => b.textContent.trim() === 'Retry')
  assert.ok(retry, 'and offers a retry')
  await m.click(retry)
  await m.settle()
  assert.match(body().textContent, /Old worry/, 'which works')
  m.unmount()
})
