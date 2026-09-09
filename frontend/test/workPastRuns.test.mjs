// Past runs mean runs that ENDED.
//
// The Kind page has two headings that could each claim the same job: "Past
// runs" and "Still open". A stuck job is live — it is on the path flag, it is
// what Now names, and it is a row in Still open. Putting it under Past runs
// as well makes one of the two headings false, and a page that contradicts
// itself in one glance stops being evidence for anything else on it.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { hottestOpen, kindPath, pastRuns } from '../src/board.js'

const code = readFileSync(new URL('../src/WorkTab.jsx', import.meta.url), 'utf8')
  .replace(/\/\*[\s\S]*?\*\//g, '')
  .replace(/^\s*\/\/.*$/gm, '')

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

// The two sets the page works from: what is open (already loaded) and what
// closed (the one focused status=done request).
const OPEN_STUCK = item({
  id: 7, status: 'stuck', whats_next: 'Waiting on stripe',
  holder: { kind: 'tool', name: 'stripe' }, updated_at: '2026-03-10T11:00:00Z',
})
const OPEN_MINE = item({
  id: 8, status: 'waiting_on_you', holder: { kind: 'human', name: 'Alex' },
})
const CLOSED_DONE = item({ id: 9, status: 'done', updated_at: '2026-03-09T10:00:00Z' })

test('an open stuck job is live everywhere and a past run nowhere', () => {
  const open = [OPEN_STUCK, OPEN_MINE]

  // It IS the live item, and it IS flagged on the path...
  assert.equal(hottestOpen(open).id, 7, 'stuck is what Now names')
  const tool = kindPath(open).find((n) => n.kind === 'tool')
  assert.equal(tool.stuck, 1, 'and it is the flag on the tool node')

  // ...and it is NOT a past run, because it has not ended.
  assert.deepEqual(pastRuns([CLOSED_DONE]).map((r) => r.id), [9])
  assert.equal(
    pastRuns([CLOSED_DONE]).some((r) => r.id === 7), false,
    'a live job must never appear under a heading that means the run ended',
  )
})

test('a closed job is a past run and not still-open work', () => {
  const [run] = pastRuns([CLOSED_DONE])
  assert.equal(run.id, 9)
  assert.equal(run.result, 'Done')
  // The other side of the same rule, enforced on the page.
  const fn = code.slice(code.indexOf('function KindPage'), code.indexOf('function WorkHome'))
  assert.match(fn, /const openRows = mine\.filter\(\(r\) => r\.status !== 'done'\)/)
})

test('the band is fed the finished payload ONLY — never the open rows', () => {
  // This is the whole fix: the input set. pastRuns() cannot tell a closed row
  // from an open one (a lean row carries no closed flag), so the guarantee
  // has to be visible right here at the call.
  const fn = code.slice(code.indexOf('function KindPage'), code.indexOf('function WorkHome'))
  assert.match(fn, /const runs = pastRuns\(finished \|\| \[\]\)/)
  assert.doesNotMatch(fn, /pastRuns\([^)]*mine/, 'open rows must not reach past runs')
})

test('while the finished request is in flight, nothing is backfilled', () => {
  // `finished` is null until the one request lands. The tempting move is to
  // show open rows meanwhile so the band is not empty; that is exactly how a
  // live job ends up labelled a past run.
  assert.deepEqual(pastRuns(null), [])
  assert.deepEqual(pastRuns(undefined), [])
  assert.deepEqual(pastRuns([]), [])
  const fn = code.slice(code.indexOf('function KindPage'), code.indexOf('function WorkHome'))
  assert.doesNotMatch(fn, /finished === null \?/, 'no "while loading, show open work" branch')
})

test('Stuck is still a real result — for a row that actually closed stuck', () => {
  // Not invented, not "Sent back": if a CLOSED row comes back decorated
  // stuck, that is what it says. The ban is on open rows reaching the band,
  // not on the word.
  const closedStuck = item({ id: 10, status: 'stuck', whats_next: 'Card declined' })
  const [row] = pastRuns([closedStuck])
  assert.equal(row.result, 'Stuck')
  assert.equal(row.reason, 'Card declined')
})

test('a job the two sources both carry is still one run', () => {
  const twice = [CLOSED_DONE, { ...CLOSED_DONE }]
  assert.equal(pastRuns(twice).length, 1)
})

test('no board, no summary', () => {
  assert.doesNotMatch(code, /getWorkBoard|getWorkSummary/)
})
