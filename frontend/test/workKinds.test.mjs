// Work home: the kinds map above the inventory table.
//
// The kind is `workflow_name` off /work/items — the declared workflow the
// matcher claimed the loop for. It is never guessed from a title: a regex over
// titles would invent kinds nobody declared and that no other surface agrees
// with. And it never costs a second request — the cards are grouped from the
// rows the table is already showing, not from GET /work/summary, which reuses
// the whole loop-scanning board to read the same two fields.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { OTHER_KIND, kindSegments, matchesKind, workKinds } from '../src/board.js'

const work = readFileSync(new URL('../src/WorkTab.jsx', import.meta.url), 'utf8')
const code = work.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')

function item(over = {}) {
  return {
    id: over.id ?? 1,
    title: over.title ?? 'Approve refund for order #4821',
    status: over.status ?? 'moving',
    holder: { kind: 'agent', name: 'support-agent' },
    whats_next: 'In progress',
    updated_at: '2026-03-10T12:00:00Z',
    ...over,
  }
}

// --- kinds come from a real field, or not at all ----------------------------

test('kinds group by the declared workflow, not by anything guessed', () => {
  const kinds = workKinds([
    item({ id: 1, workflow_name: 'Refunds', workflow_id: 2, status: 'waiting_on_you' }),
    item({ id: 2, workflow_name: 'Refunds', workflow_id: 2, status: 'moving' }),
    item({ id: 3, workflow_name: 'Onboarding', workflow_id: 5, status: 'stuck' }),
  ])
  assert.deepEqual(kinds.map((k) => k.name), ['Onboarding', 'Refunds'])
  const refunds = kinds.find((k) => k.name === 'Refunds')
  assert.equal(refunds.workflowId, 2)
  assert.equal(refunds.total, 2)
})

test('work with no declared kind lands in Other work, never nowhere', () => {
  const kinds = workKinds([
    item({ id: 1, workflow_name: 'Refunds', workflow_id: 2 }),
    item({ id: 2, workflow_name: null, workflow_id: null }),
    item({ id: 3 }), // no keys at all
  ])
  const other = kinds.find((k) => k.name === OTHER_KIND)
  assert.ok(other, 'unmatched work is grouped, not dropped')
  assert.equal(other.total, 2)
  assert.equal(other.isOther, true)
  // Every row is accounted for somewhere.
  assert.equal(kinds.reduce((n, k) => n + k.total, 0), 3)
})

test('no kind field anywhere means no kind map', () => {
  // One card reading "Other work" over the whole table is a heading, not a
  // map — the strip renders nothing in that case.
  const kinds = workKinds([item({ id: 1 }), item({ id: 2 })])
  assert.equal(kinds.length, 1)
  assert.equal(kinds[0].isOther, true)
  assert.match(code, /if \(kinds\.length === 1 && kinds\[0\]\.isOther\) return null/)
  assert.match(code, /if \(kinds\.length === 0\) return null/)
})

test('kinds obey the same showable gate as the table', () => {
  // A shell title is not on the table, so it must not be in a card's count —
  // otherwise the numbers do not add up to rows anyone can click.
  const kinds = workKinds([
    item({ id: 1, title: 'run_4821', workflow_name: 'Refunds' }),
    item({ id: 2, title: 'Approve refund #1', workflow_name: 'Refunds' }),
    item({ id: 3, title: 'Real title', workflow_name: 'Refunds', id: undefined }),
  ])
  assert.equal(kinds[0].total, 1)
})

test('a card counts what the table calls things', () => {
  const [k] = workKinds([
    item({ id: 1, status: 'moving', workflow_name: 'R' }),
    item({ id: 2, status: 'waiting_on_you', workflow_name: 'R' }),
    item({ id: 3, status: 'waiting_on_other', workflow_name: 'R' }),
    item({ id: 4, status: 'stuck', workflow_name: 'R' }),
    item({ id: 5, status: 'done', workflow_name: 'R' }),
  ])
  assert.equal(k.moving, 1)
  // Both person-waits, yours and a teammate's.
  assert.equal(k.waiting, 2)
  assert.equal(k.stuck, 1)
  // Done is deliberately absent: a kind card is about what is live.
  assert.ok(!('done' in k))
})

test('the loudest kind leads, and Other work sinks', () => {
  const kinds = workKinds([
    item({ id: 1, workflow_name: null, status: 'stuck' }),
    item({ id: 2, workflow_name: 'Quiet', status: 'moving' }),
    item({ id: 3, workflow_name: 'Hot', status: 'stuck' }),
  ])
  assert.deepEqual(kinds.map((k) => k.name), ['Hot', 'Quiet', OTHER_KIND])
})

test('colour appears only where a person is needed', () => {
  const quiet = kindSegments({ moving: 3, waiting: 0, stuck: 0 })
  assert.deepEqual(quiet.map((s) => s.tone), ['muted', 'muted', 'muted'])
  const hot = kindSegments({ moving: 1, waiting: 2, stuck: 1 })
  assert.deepEqual(hot.map((s) => s.tone), ['muted', 'warn', 'stuck'])
})

test('no cost on a kind card', () => {
  // Per-kind cost only exists on the board's span aggregate — the scan this
  // page exists to avoid. It is absent rather than approximated.
  const [k] = workKinds([item({ id: 1, workflow_name: 'R' })])
  assert.ok(!('cost' in k) && !('cost_today' in k))
  const strip = code.slice(code.indexOf('function KindsStrip'), code.indexOf('function WorkHome'))
  assert.doesNotMatch(strip, /cost|\\$/i)
})

// --- filtering ---------------------------------------------------------------

test('a kind filter shows only that kind', () => {
  const rows = [
    item({ id: 1, workflow_name: 'Refunds' }),
    item({ id: 2, workflow_name: 'Onboarding' }),
    item({ id: 3, workflow_name: null }),
  ]
  assert.deepEqual(rows.filter((r) => matchesKind(r, 'Refunds')).map((r) => r.id), [1])
  assert.deepEqual(rows.filter((r) => matchesKind(r, OTHER_KIND)).map((r) => r.id), [3])
  // No filter restores everything.
  assert.equal(rows.filter((r) => matchesKind(r, null)).length, 3)
})

test('the kind filter composes with the one Home arrives with', () => {
  // "Stuck" from Home plus "Refunds" here is a legitimate question, so the
  // two filters chain rather than replace each other...
  assert.match(code, /\.filter\(\(r\) => \(filter \? matchesWorkFilter\(r, filter\) : true\)\)/)
  assert.match(code, /\.filter\(\(r\) => matchesKind\(r, activeKind\)\)/)
  // ...and both chips stay on screen and clearable.
  assert.match(code, /onClick=\{onClearFilter\}/)
  assert.match(code, /onClick=\{\(\) => setKind\(null\)\}/)
  assert.match(code, /aria-label=\{`Clear the \$\{activeKind\} filter`\}/)
})

test('a kind that vanishes cannot strand the table', () => {
  // Resolved away, or filtered out by Home's chip: the active kind falls back
  // to none rather than leaving an empty table with no way back.
  assert.match(code, /const activeKind = kinds\.some\(\(k\) => k\.name === kind\) \? kind : null/)
})

test('filtered-to-nothing is not the same as having no work', () => {
  // The answer to one is "clear a chip"; the answer to the other is "connect
  // an agent". Showing the connect empty state to someone who over-filtered
  // is telling them to fix the wrong thing.
  assert.match(code, /const filteredOut = !!items && rows\.length === 0 && all\.length > 0/)
  assert.match(code, /Nothing matches these filters/)
  assert.match(code, /Clear a filter above/)
})

// --- the page keeps its contract --------------------------------------------

test('Work home still uses only the lean pair', () => {
  assert.doesNotMatch(code, /getWorkBoard/)
  assert.doesNotMatch(code, /getWorkSummary/)
  assert.doesNotMatch(code, /listAgents/)
  assert.match(code, /getWorkOverview/)
  assert.match(code, /getWorkItems/)
})

test('a row still opens JobDetail', () => {
  assert.match(code, /setOpen\(row\)/)
  assert.match(code, /<JobDetail/)
})

test('the count rows do not compete', () => {
  // Once the kind cards carry moving/waiting/stuck, the overview strip drops
  // to the two cuts a kind card structurally cannot express.
  assert.match(code, /demoted=\{kinds\.length > 1\}/)
  assert.match(code, /DEMOTED_PILLS = new Set\(\['needs_you', 'completed_week'\]\)/)
})

test('no jargon on the new Work-home strings', () => {
  const FORBIDDEN = /\b(loops?|workloops?|possession|segments?|stations?|handoffs?)\b/i
  const strip = code.slice(code.indexOf('function KindsStrip'), code.indexOf('function WorkHome'))
  for (const m of strip.matchAll(/>([^<>{}]{3,})</g)) {
    assert.ok(!FORBIDDEN.test(m[1]), `KindsStrip ships jargon: ${JSON.stringify(m[1])}`)
  }
  for (const s of [OTHER_KIND, ...kindSegments({}).map((x) => x.label)]) {
    assert.ok(!FORBIDDEN.test(s), `kind copy ships jargon: ${JSON.stringify(s)}`)
  }
})
