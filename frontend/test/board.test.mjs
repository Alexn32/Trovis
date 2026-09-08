// Work board presentation tests.
//
// The load-bearing one is the vocabulary sweep at the bottom: it reads the
// board's own source and fails if a single user-facing string contains
// "loop", "possession", "segment", "station" or "handoff". The board is for
// someone who has never heard of Trovis; that rule needs a test, not a habit.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { boardAge, boardCostLabel, boardEmpty, holderLine } from '../src/board.js'

test('age is specific — it is the urgency signal, never rounded to "a while"', () => {
  assert.equal(boardAge(0), 'just now')
  assert.equal(boardAge(59), 'just now')
  assert.equal(boardAge(60), '1m')
  assert.equal(boardAge(3600), '1h')
  assert.equal(boardAge(3600 * 52), '2d')
  assert.equal(boardAge(null), '')
})

test('cost renders only when there is one — never "$0.00" as noise', () => {
  assert.equal(boardCostLabel(0), '')
  assert.equal(boardCostLabel(0.004), '')
  assert.equal(boardCostLabel(0.031), '$0.03')
  assert.equal(boardCostLabel(1.5), '$1.50')
  assert.equal(boardCostLabel(null), '')
})

test('holder line: a person and an agent read with equal weight', () => {
  assert.equal(
    holderLine({ holder_type: 'human', holder_name: 'Sarah Chen', age_seconds: 7200 }),
    'With Sarah Chen for 2h',
  )
  assert.equal(
    holderLine({ holder_type: 'agent', holder_name: 'Support Bot', age_seconds: 7200 }),
    'With Support Bot for 2h',
  )
})

test('a task waiting on YOU says so first', () => {
  assert.equal(
    holderLine({ holder_type: 'human', holder_name: 'You', is_yours: true, age_seconds: 3600 }),
    'Waiting on you for 1h',
  )
})

test('a blocked agent names what it is blocked on', () => {
  assert.equal(
    holderLine({ holder_type: 'agent', holder_name: 'billing-agent', waiting_on: 'Stripe', age_seconds: 900 }),
    'billing-agent · waiting on Stripe for 15m',
  )
})

test('empty board distinguishes "nothing connected" from "nothing yet"', () => {
  const noAgents = boardEmpty({ total: 0, has_agents: false })
  assert.match(noAgents.lead, /nothing is connected/i)
  assert.ok(noAgents.cta, 'offers the way out')

  const noWork = boardEmpty({ total: 0, has_agents: true })
  assert.doesNotMatch(noWork.lead, /nothing is connected/i)
  assert.match(noWork.sub, /by itself/i, 'promises it fills itself in')
  assert.equal(noWork.cta, null, 'nothing to do — so no button')

  assert.equal(boardEmpty({ total: 3, has_agents: true }), null)
})

// ---------------------------------------------------------------------------
// THE VOCABULARY RULE
// ---------------------------------------------------------------------------
const FORBIDDEN = /\b(loops?|workloops?|possession|segments?|stations?|handoffs?)\b/i

function userFacingStrings(src) {
  // Quoted literals of 4+ chars containing a space — i.e. prose, not
  // identifiers, css classes or api paths.
  const out = []
  const re = /(['"])((?:(?!\1)[^\\\n]|\\.){4,200})\1/g
  let m
  while ((m = re.exec(src))) {
    const t = m[2]
    if (!t.includes(' ')) continue
    if (t.startsWith('/') || t.includes('${') === false && t.startsWith('.')) continue
    out.push(t)
  }
  return out
}

test('no Trovis jargon reaches the user anywhere on the board', () => {
  for (const file of ['../src/Board.jsx', '../src/board.js']) {
    const src = readFileSync(new URL(file, import.meta.url), 'utf8')
    // Strip comments — engineers may say "loop"; users may not.
    const code = src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')
    for (const s of userFacingStrings(code)) {
      assert.ok(
        !FORBIDDEN.test(s),
        `${file} ships jargon to the user: ${JSON.stringify(s)}`,
      )
    }
  }
})

test('the columns are the four the board promises, in order', () => {
  const src = readFileSync(new URL('../src/Board.jsx', import.meta.url), 'utf8')
  // Column labels come from the server; assert the board renders whatever it
  // is given rather than hardcoding a fifth.
  assert.match(src, /column\.label/, 'labels come from the server')
  assert.doesNotMatch(src, /draggable|onDragStart|onDrop/i,
    'the board reflects reality — it never accepts a drag')
})

// A structural turn-end handoff carries reason "turn_end" on the wire. That is
// a machine token, and "Handed to Sarah — turn_end" is exactly the leak the
// board's language rule exists to stop.
test('machine tokens never reach a step sentence', async () => {
  const { LIFECYCLE_SENTENCES } = await import('../src/loops.js')
  const h = LIFECYCLE_SENTENCES.handoff_initiated
  assert.equal(h({ direction: 'to_human', target_name: 'Sarah Chen', reason: 'turn_end' }),
    'Handed to Sarah Chen')
  assert.equal(h({ direction: 'to_human', target_name: 'Sarah Chen', reason: 'some_wire_value' }),
    'Handed to Sarah Chen', 'anything snake_case-shaped is a token, not prose')
  // A real, human-written reason still shows.
  assert.equal(h({ direction: 'to_human', target_name: 'Sarah Chen', reason: 'needs approval' }),
    'Handed to Sarah Chen — needs approval')
  assert.equal(h({ direction: 'to_system' }), 'Handed to a system')
})

// ---------------------------------------------------------------------------
// Level 1 — kind-of-work cards
// ---------------------------------------------------------------------------
import { kindRollup, kindNeedsAttention, otherNudge, workScreenEmpty } from '../src/board.js'

test('rollup shows all four counts; a calm card stays muted', () => {
  const parts = kindRollup({ in_motion: 2, waiting_person: 0, stuck: 0, done_today: 5, cost_today: 0 })
  const byLabel = Object.fromEntries(parts.map((p) => [p.label, p]))
  assert.equal(byLabel['in motion'].value, 2)
  assert.equal(byLabel['done today'].value, 5)
  // zero waiting/stuck are present but muted — quiet, not colored
  assert.equal(byLabel['waiting on a person'].tone, 'muted')
  assert.equal(byLabel['stuck'].tone, 'muted')
  // no cost segment when zero
  assert.ok(!parts.some((p) => /today/.test(p.label) && /\$/.test(p.label)))
})

test('semantic color appears ONLY on nonzero waiting/stuck', () => {
  const parts = kindRollup({ in_motion: 1, waiting_person: 3, stuck: 1, done_today: 0, cost_today: 1.2 })
  const byLabel = Object.fromEntries(parts.map((p) => [p.label, p]))
  assert.equal(byLabel['waiting on a person'].tone, 'warn')
  assert.equal(byLabel['stuck'].tone, 'stuck')
  assert.ok(parts.some((p) => p.label === '$1.20 today'), 'cost shown when nonzero')
})

test('a kind needs attention only when someone is waiting or stuck', () => {
  assert.equal(kindNeedsAttention({ waiting_person: 0, stuck: 0 }), false)
  assert.equal(kindNeedsAttention({ waiting_person: 1, stuck: 0 }), true)
  assert.equal(kindNeedsAttention({ waiting_person: 0, stuck: 2 }), true)
})

test('the Other-work nudge is an offer, and only when warranted', () => {
  assert.equal(otherNudge({ suggest_declare: true }), 'A lot of work here — declare a workflow to organize it')
  assert.equal(otherNudge({ suggest_declare: false }), '')
  assert.equal(otherNudge({}), '')
})

test('Level-1 empty state distinguishes "nothing connected" from "nothing yet"', () => {
  const noAgents = workScreenEmpty({ total: 0, has_agents: false })
  assert.match(noAgents.lead, /nothing is connected/i)
  assert.ok(noAgents.cta)
  const noWork = workScreenEmpty({ total: 0, has_agents: true })
  assert.doesNotMatch(noWork.lead, /nothing is connected/i)
  assert.equal(noWork.cta, null)
  assert.equal(workScreenEmpty({ total: 4, has_agents: true }), null)
})

test('no Trovis jargon in the Level-1 helpers', () => {
  const strings = [
    ...kindRollup({ in_motion: 1, waiting_person: 1, stuck: 1, done_today: 1, cost_today: 1 }).map((p) => p.label),
    otherNudge({ suggest_declare: true }),
    workScreenEmpty({ total: 0, has_agents: false }).lead,
    workScreenEmpty({ total: 0, has_agents: false }).sub,
    workScreenEmpty({ total: 0, has_agents: true }).sub,
  ]
  for (const s of strings) {
    assert.ok(!/\b(loops?|possession|segments?|stations?|handoffs?)\b/i.test(s), `jargon: ${s}`)
  }
})

// ---------------------------------------------------------------------------
// Standing (always-on) work — Level 1 rollup + the ongoing line
// ---------------------------------------------------------------------------
import { ongoingLine } from '../src/board.js'

test('ongoing shows as a quiet count on a kind card, never colored', () => {
  const parts = kindRollup({ in_motion: 2, waiting_person: 0, stuck: 0, done_today: 1, ongoing: 3 })
  const seg = parts.find((p) => p.label === 'ongoing')
  assert.ok(seg, 'ongoing segment present when >0')
  assert.equal(seg.value, 3)
  assert.equal(seg.tone, 'muted', 'ongoing is never an alarm color')
})

test('no ongoing segment when there is no standing work', () => {
  const parts = kindRollup({ in_motion: 1, waiting_person: 0, stuck: 0, done_today: 0, ongoing: 0 })
  assert.ok(!parts.some((p) => p.label === 'ongoing'))
})

test('ongoing does not count toward a kind needing attention', () => {
  // Always-on work is not, by itself, something a person must look at.
  assert.equal(kindNeedsAttention({ waiting_person: 0, stuck: 0, ongoing: 9 }), false)
})

test('the ongoing line is human, and empty when there is none', () => {
  assert.equal(ongoingLine(3), '3 ongoing, running normally')
  assert.equal(ongoingLine(1), '1 ongoing, running normally')
  assert.equal(ongoingLine(0), '')
  assert.equal(ongoingLine(), '')
})

test('work item status: waiting_on_other is labeled Waiting on someone', async () => {
  const { workItemStatusLabel } = await import('../src/board.js')
  assert.equal(workItemStatusLabel('waiting_on_other'), 'Waiting on someone')
  assert.equal(workItemStatusLabel('waiting_on_you'), 'Waiting on you')
  assert.equal(workItemStatusLabel('stuck'), 'Stuck')
  assert.equal(workItemStatusLabel('moving'), 'Moving')
  assert.equal(workItemStatusLabel('done'), 'Done')
})

test('no Trovis jargon in the standing-work copy', () => {
  const strings = [ongoingLine(3), 'ongoing']
  for (const s of strings) {
    assert.ok(!/\b(loops?|possession|segments?|stations?|handoffs?)\b/i.test(s), `jargon: ${s}`)
  }
})

test('Work home and Ask do not call fat /work/board or /work/summary', () => {
  const work = readFileSync(new URL('../src/WorkTab.jsx', import.meta.url), 'utf8')
  const ask = readFileSync(new URL('../src/AskPill.jsx', import.meta.url), 'utf8')
  // Home load path: overview + items + suggestions. getWorkBoard lives in
  // Board.jsx and must not be invoked from WorkTab's load().
  assert.doesNotMatch(work, /getWorkSummary/)
  assert.doesNotMatch(work, /getWorkBoard/)
  assert.doesNotMatch(ask, /getWorkBoard/, 'Ask must not prefetch the fat board')
  assert.match(work, /getWorkOverview/)
  assert.match(work, /getWorkItems/)
  assert.match(work, /getWorkSuggestions/)
})

test('Work home is the Monday table — Task column, no Priority, no KindCard landing', () => {
  const work = readFileSync(new URL('../src/WorkTab.jsx', import.meta.url), 'utf8')
  assert.match(work, />Task</)
  assert.match(work, /Status/)
  assert.match(work, /Holder/)
  assert.match(work, /What&apos;s next/)
  assert.match(work, /Updated/)
  assert.doesNotMatch(work, /Priority/)
  assert.doesNotMatch(work, /KindCard/)
  assert.match(work, /work-suggestions/)
  assert.match(work, /TaskPanel/)
  assert.match(work, /holderLabel/)
  assert.match(work, /Other views →/)
  assert.doesNotMatch(work, /Boards & other views/)
})

test('Work home never auto-creates from suggestion approve/edit/decline', () => {
  const work = readFileSync(new URL('../src/WorkTab.jsx', import.meta.url), 'utf8')
  assert.doesNotMatch(work, /insert_spans|createWork|postWork|\/work\/items['"`].*POST/i)
  assert.match(work, /approveWorkSuggestion/)
  assert.match(work, /editWorkSuggestion/)
  assert.match(work, /declineWorkSuggestion/)
  assert.doesNotMatch(work, /Local dismiss only|onDismissSuggestion/)
  assert.match(work, /refreshHome|getWorkOverview/)
  assert.match(work, /getWorkItems/)
  assert.match(work, /isNamedWorkTitle/)
})

test('sortWorkItems is Needs you → Stuck → waiting on someone → Moving → Done', async () => {
  const { sortWorkItems } = await import('../src/board.js')
  const rows = [
    { id: 1, status: 'done', updated_at: '2026-09-08T12:00:00Z' },
    { id: 2, status: 'moving', updated_at: '2026-09-08T12:00:00Z' },
    { id: 3, status: 'waiting_on_other', updated_at: '2026-09-08T12:00:00Z' },
    { id: 4, status: 'stuck', updated_at: '2026-09-08T12:00:00Z' },
    { id: 5, status: 'waiting_on_you', updated_at: '2026-09-08T12:00:00Z' },
  ]
  assert.deepEqual(sortWorkItems(rows).map((r) => r.status), [
    'waiting_on_you',
    'stuck',
    'waiting_on_other',
    'moving',
    'done',
  ])
})

test('named-work title gate hides ids, UUIDs, snake_case, and jargon', async () => {
  const { isNamedWorkTitle } = await import('../src/board.js')
  assert.equal(isNamedWorkTitle('Approve refund #4821'), true)
  assert.equal(isNamedWorkTitle('loop_42'), false)
  assert.equal(isNamedWorkTitle('550e8400-e29b-41d4-a716-446655440000'), false)
  assert.equal(isNamedWorkTitle(''), false)
})

test('holderLabel prefixes kind when lean fields have it, else name-only', async () => {
  const { holderLabel } = await import('../src/board.js')
  assert.equal(holderLabel({ kind: 'human', name: 'Alex' }, 'waiting_on_you'), 'You Alex')
  assert.equal(holderLabel({ kind: 'human', name: 'You' }, 'waiting_on_you'), 'You')
  assert.equal(holderLabel({ kind: 'human', name: 'Sarah Chen' }, 'waiting_on_other'), 'Person Sarah Chen')
  assert.equal(holderLabel({ kind: 'agent', name: 'Support Bot' }, 'moving'), 'Agent Support Bot')
  assert.equal(holderLabel({ kind: 'tool', name: 'Stripe' }, 'stuck'), 'Tool Stripe')
  assert.equal(holderLabel({ kind: 'unassigned', name: 'Unassigned' }, 'moving'), 'Unassigned')
  assert.equal(holderLabel({ name: 'Alex' }, 'waiting_on_you'), 'Alex')
  assert.equal(holderLabel(null, 'moving'), '')
})

test('workUpdatedLabel is compact like boardAge, not "2h ago"', async () => {
  const { workUpdatedLabel } = await import('../src/board.js')
  const twoHoursAgo = new Date(Date.now() - 2 * 3600 * 1000).toISOString()
  assert.equal(workUpdatedLabel(twoHoursAgo), '2h')
  assert.equal(workUpdatedLabel(null), '')
})

test('Design locks: color + 3px rail only on waiting_on_you / stuck', () => {
  const css = readFileSync(new URL('../src/styles.css', import.meta.url), 'utf8')
  assert.match(css, /\.work-row\.is-waiting-you[\s\S]{0,180}inset 3px 0 0 var\(--warning\)/)
  assert.match(css, /\.work-row\.is-stuck[\s\S]{0,180}inset 3px 0 0 var\(--error\)/)
  assert.match(css, /\.work-row\.is-waiting-you[\s\S]{0,120}var\(--warm-fill\)/)
  assert.match(css, /\.work-row\.is-stuck[\s\S]{0,120}var\(--error-faint\)/)
  assert.match(css, /\.work-status-pill\.waiting_on_you[\s\S]{0,60}var\(--warning\)/)
  assert.match(css, /\.work-status-pill\.stuck[\s\S]{0,60}var\(--error\)/)
  assert.match(css, /\.work-status-pill\.moving[\s\S]{0,80}var\(--text-muted\)/)
})

test('no Trovis jargon on Work home', () => {
  const src = readFileSync(new URL('../src/WorkTab.jsx', import.meta.url), 'utf8')
  const code = src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')
  for (const s of userFacingStrings(code)) {
    assert.ok(!FORBIDDEN.test(s), `Work home ships jargon: ${JSON.stringify(s)}`)
  }
})
