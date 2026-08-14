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
