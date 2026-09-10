// Work page 2: one kind of work.
//
// Everything on this page is read from rows the section already loaded, plus
// ONE extra lean request for this kind's finished work. No detail fetch per
// row, no board, no summary — which is also why the path is inferred the way
// it is, and why it is sometimes absent.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { OTHER_KIND, hottestOpen, kindPath, matchesKind, pastRuns } from '../src/board.js'

const work = readFileSync(new URL('../src/WorkTab.jsx', import.meta.url), 'utf8')
const api = readFileSync(new URL('../src/api.js', import.meta.url), 'utf8')
const code = work.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')

function item(o = {}) {
  return {
    id: o.id ?? 1,
    title: o.title ?? `Approve refund #${o.id ?? 1}`,
    status: o.status ?? 'moving',
    holder: o.holder ?? { kind: 'agent', name: 'refunds-agent' },
    whats_next: o.whats_next ?? 'In progress',
    updated_at: o.updated_at ?? '2026-03-10T12:00:00Z',
    ...o,
  }
}

// --- Band A: the path -------------------------------------------------------

test('the path is the hands this kind passes through', () => {
  const path = kindPath([
    item({ id: 1, holder: { kind: 'agent', name: 'refunds-agent' } }),
    item({ id: 2, status: 'stuck', holder: { kind: 'tool', name: 'stripe' } }),
    item({ id: 3, status: 'waiting_on_you', holder: { kind: 'human', name: 'Alex' } }),
  ])
  assert.deepEqual(path.map((n) => n.kind), ['agent', 'tool', 'human'])
  // A tool worth naming is named — "Stripe" says more than "Tool".
  assert.equal(path[1].label, 'stripe')
})

test('one kind of holder is not a path', () => {
  // Drawing a one-node spine would dress a single fact up as a process.
  assert.equal(kindPath([item({ id: 1 }), item({ id: 2 })]), null)
  assert.equal(kindPath([]), null)
  assert.equal(kindPath(null), null)
})

test('the path omits Done until this kind has actually finished something', () => {
  const live = kindPath([
    item({ id: 1, holder: { kind: 'agent', name: 'a' } }),
    item({ id: 2, holder: { kind: 'human', name: 'Alex' } }),
  ])
  assert.ok(!live.some((n) => n.kind === 'done'), 'no ending the record has not seen')
  const withDone = kindPath([
    item({ id: 1, holder: { kind: 'agent', name: 'a' } }),
    item({ id: 2, holder: { kind: 'human', name: 'Alex' } }),
    item({ id: 3, status: 'done' }),
  ])
  assert.equal(withDone[withDone.length - 1].kind, 'done')
})

test('only exceptions land on a node — healthy nodes stay quiet', () => {
  const path = kindPath([
    item({ id: 1, holder: { kind: 'agent', name: 'a' } }),
    item({ id: 2, status: 'stuck', holder: { kind: 'tool', name: 'stripe' } }),
    item({ id: 3, status: 'waiting_on_you', holder: { kind: 'human', name: 'Alex' } }),
    item({ id: 4, status: 'waiting_on_other', holder: { kind: 'human', name: 'Sam' } }),
  ])
  const [agent, tool, person] = path
  assert.deepEqual([agent.waiting, agent.stuck], [0, 0], 'a quiet node carries nothing')
  assert.equal(tool.stuck, 1)
  assert.equal(person.waiting, 2, "both person-waits, yours and a teammate's")
})

test('the path is built from list rows only — never a detail per row', () => {
  // A per-row detail fetch is the thing this page must not do. The band is
  // fed the rows the section already has.
  assert.match(code, /const path = kindPath\(mine\)/)
  const fn = code.slice(code.indexOf('function KindPage'), code.indexOf('function HealthBadge'))
  assert.doesNotMatch(fn, /getWorkItem\(/)
})

// --- Band C: past runs ------------------------------------------------------

test('past runs are the finished and the stuck, newest first', () => {
  const runs = pastRuns([
    item({ id: 1, status: 'done', updated_at: '2026-03-10T09:00:00Z' }),
    item({ id: 2, status: 'stuck', updated_at: '2026-03-10T11:00:00Z', whats_next: 'Waiting on stripe' }),
    item({ id: 3, status: 'moving', updated_at: '2026-03-10T12:00:00Z' }),
  ])
  assert.deepEqual(runs.map((r) => r.id), [2, 1], 'open, moving work is not a past run')
  assert.deepEqual(runs.map((r) => r.result), ['Stuck', 'Done'])
})

test('a stuck reason is work language already on the row, never a trace', () => {
  const [run] = pastRuns([item({ id: 1, status: 'stuck', whats_next: 'Waiting on stripe' })])
  assert.equal(run.reason, 'Waiting on stripe')
  // Done needs no reason.
  const [done] = pastRuns([item({ id: 2, status: 'done' })])
  assert.equal(done.reason, '')
})

test('"Sent back" is not claimed from a list row', () => {
  // It lives in an item's own history. Naming an outcome we did not observe
  // is worse than naming the two we did.
  const results = pastRuns([
    item({ id: 1, status: 'done' }),
    item({ id: 2, status: 'stuck' }),
  ]).map((r) => r.result)
  assert.deepEqual([...new Set(results)].sort(), ['Done', 'Stuck'])
})

test('a job the two sources both carry is one run, not two', () => {
  // The band is fed the rows the section already holds PLUS the focused
  // request for finished work, and those overlap. Listing a job twice reads
  // as two runs — the page would be inventing history.
  const mine = [
    item({ id: 1, status: 'done', updated_at: '2026-03-10T09:00:00Z' }),
    item({ id: 2, status: 'stuck', updated_at: '2026-03-10T11:00:00Z' }),
  ]
  const fetched = [item({ id: 1, status: 'done', updated_at: '2026-03-10T09:00:00Z' })]
  assert.deepEqual(pastRuns([...mine, ...fetched]).map((r) => r.id), [2, 1])
})

test('the band disappears when the record has no finished or stuck work', () => {
  assert.deepEqual(pastRuns([item({ id: 1, status: 'moving' })]), [])
  const fn = code.slice(code.indexOf('function PastRunsBand'))
  assert.match(fn, /if \(runs\.length === 0\) return null/)
})

test('past runs load with a lean filter, never the board', () => {
  assert.match(code, /workflowId: kindWorkflowId === null \? 'none' : kindWorkflowId/)
  assert.match(code, /status: 'done'/)
  assert.doesNotMatch(code, /getWorkBoard/)
  assert.doesNotMatch(code, /getWorkSummary/)
  // The filter is a real query param, not a client-side sieve over everything.
  assert.match(api, /q\.set\('workflow_id', String\(workflowId\)\)/)
  assert.match(api, /q\.set\('status', status\)/)
})

test('the extra request happens only on a kind page, and is abortable', () => {
  assert.match(code, /if \(!kindOpen\) \{[\s\S]*?setFinished\(null\)/)
  assert.match(code, /return startAbortable\(\(\{ signal, isAlive \}\) =>/)
})

// --- Band B: now ------------------------------------------------------------

test('the hottest open item is the one furthest from moving, oldest first', () => {
  const hot = hottestOpen([
    item({ id: 1, status: 'moving', updated_at: '2026-03-10T09:00:00Z' }),
    item({ id: 2, status: 'waiting_on_you', updated_at: '2026-03-10T11:00:00Z' }),
    item({ id: 3, status: 'stuck', updated_at: '2026-03-10T12:00:00Z' }),
  ])
  assert.equal(hot.id, 3, 'stuck outranks waiting outranks moving')
  const older = hottestOpen([
    item({ id: 4, status: 'stuck', updated_at: '2026-03-10T12:00:00Z' }),
    item({ id: 5, status: 'stuck', updated_at: '2026-03-09T12:00:00Z' }),
  ])
  assert.equal(older.id, 5, 'age is the tiebreak')
})

test('done work is never the live item', () => {
  assert.equal(hottestOpen([item({ id: 1, status: 'done' })]), null)
})

test('no cost anywhere on the page', () => {
  // Per-kind cost only exists on the board's span aggregate. Absent beats fake.
  const fn = code.slice(code.indexOf('function PathBand'), code.indexOf('function HealthBadge'))
  assert.doesNotMatch(fn, /cost/i)
})

// --- routing ----------------------------------------------------------------

test('a job row opens that job page, by id, at its own URL', () => {
  // The board's rows ARE the jobs now, so a row opens /work/jobs/:id — no
  // name lookup, no local view state to fall out of step with the URL.
  assert.match(code, /onOpenJob=\{\(row\) => onRoute\(\{ job: Number\(row\.key\), run: null \}\)\}/)
  assert.match(code, /const kindWorkflowId = route\.job \?\? null/)
  const rows = [item({ id: 1, workflow_name: 'Refunds' }), item({ id: 2, workflow_name: null })]
  assert.deepEqual(rows.filter((r) => matchesKind(r, OTHER_KIND)).map((r) => r.id), [2])
})

test('All work returns to the board', () => {
  assert.match(code, /onBack=\{\(\) => onRoute\(\{ job: null, run: null \}\)\}/)
  assert.match(code, /← All work/)
})

test('the page shows only this kind', () => {
  assert.match(code, /const mine = items\.filter\(\(r\) => matchesKind\(r, kindName\)\)/)
})

test('"Still open" holds no finished work', () => {
  // The lean list carries done rows incidentally, so the table has to drop
  // them itself. A heading that says open over a row that says Done is the
  // page contradicting itself in one glance.
  const fn = code.slice(code.indexOf('function KindPage'), code.indexOf('function HealthBadge'))
  assert.match(fn, /const openRows = mine\.filter\(\(r\) => r\.status !== 'done'\)/)
  assert.match(fn, /filter \? openRows\.filter\(\(r\) => matchesWorkFilter\(r, filter\)\) : openRows/)
  // ...and "nothing matches" is measured against what the table can show.
  assert.match(fn, /filteredOut = rows\.length === 0 && openRows\.length > 0/)
})

test('a Home filter still applies here and stays dismissible', () => {
  const fn = code.slice(code.indexOf('function KindPage'), code.indexOf('function HealthBadge'))
  assert.match(fn, /filter \? openRows\.filter\(\(r\) => matchesWorkFilter\(r, filter\)\) : openRows/)
  assert.match(fn, /onClick=\{onClearFilter\}/)
})

test('filtered to nothing says so, instead of asking for an agent', () => {
  const fn = code.slice(code.indexOf('function KindPage'), code.indexOf('function HealthBadge'))
  assert.match(fn, /Nothing matches these filters/)
  assert.doesNotMatch(fn, /Connect an agent/)
  // A kind with genuinely nothing open is quiet, not first-run theatre.
  assert.match(fn, /Nothing open here/)
})

test('no jargon on the kind page', () => {
  const FORBIDDEN = /\b(loops?|workloops?|possession|segments?|stations?|handoffs?)\b/i
  const fn = code.slice(code.indexOf('function PathBand'), code.indexOf('function HealthBadge'))
  for (const m of fn.matchAll(/>([^<>{}]{3,})</g)) {
    assert.ok(!FORBIDDEN.test(m[1]), `kind page ships jargon: ${JSON.stringify(m[1])}`)
  }
  for (const s of ['Agent', 'Tool', 'Person', 'Done', 'Stuck']) {
    assert.ok(!FORBIDDEN.test(s))
  }
})

test('no second Ask on the kind page', () => {
  const fn = code.slice(code.indexOf('function KindPage'), code.indexOf('function HealthBadge'))
  assert.doesNotMatch(fn, /openAsk|AskPill|home-ask/)
})
