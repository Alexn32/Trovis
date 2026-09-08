import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import {
  TITLE_MAX,
  WAITING_ON_ME,
  WHATS_STUCK,
  FALLBACK_CHIPS,
  looksInternal,
  clipTitle,
  stuckChip,
  askSuggestions,
} from '../src/askChips.js'

test('chip 1 is the #117 string — waiting on me, never on you', () => {
  assert.equal(WAITING_ON_ME, "What's waiting on me?")
  assert.doesNotMatch(WAITING_ON_ME, /on you/)
  assert.equal(FALLBACK_CHIPS[0].query, WAITING_ON_ME)
  assert.equal(FALLBACK_CHIPS[1].query, WHATS_STUCK)
})

test('clipTitle stays in the 40–48 window and ellipsizes', () => {
  assert.equal(TITLE_MAX, 44)
  assert.equal(clipTitle('Refund review'), 'Refund review')
  const long =
    'Quarterly vendor invoice dispute for the Acme renewal that finance has not touched'
  const clipped = clipTitle(long)
  assert.ok(clipped.endsWith('…'))
  assert.ok(clipped.length <= TITLE_MAX)
  assert.ok(!clipped.includes(long.slice(-10)), 'does not keep the tail')
})

test('internal ids and snake_case never become the stuck chip', () => {
  assert.equal(looksInternal(''), true)
  assert.equal(looksInternal('1842'), true)
  assert.equal(looksInternal('loop_42'), true)
  assert.equal(looksInternal('awaiting_handoff'), true)
  assert.equal(looksInternal('Refund review'), false)
  assert.equal(stuckChip({ title: 'loop_1842' }).label, WHATS_STUCK)
  assert.equal(stuckChip({ title: '42' }).label, WHATS_STUCK)
  assert.equal(stuckChip(null).label, WHATS_STUCK)
})

test('stuck chip truncates the label, keeps the full title on hover and in the query', () => {
  const title =
    'Quarterly vendor invoice dispute for the Acme renewal that finance has not touched'
  const chip = stuckChip({ title, stuck_reason: 'waiting on Stripe' })
  assert.match(chip.label, /^Why is .+ stuck\?$/)
  assert.ok(chip.label.includes('…'))
  assert.equal(chip.query, `Why is ${title} stuck?`)
  assert.equal(chip.title, `${title} — waiting on Stripe`)
  assert.ok(!/loop|handoff|station/i.test(chip.label))
})

test('askSuggestions is waiting, then stuck — no extra recap chip', () => {
  const board = {
    columns: [
      { key: 'stuck', cards: [{ title: 'Refund review', stuck_reason: 'no activity for 2 days' }] },
      { key: 'done', cards: [{ title: 'Shipped order 9' }] },
    ],
  }
  const chips = askSuggestions(board)
  assert.equal(chips.length, 2)
  assert.equal(chips[0].query, WAITING_ON_ME)
  assert.equal(chips[1].query, 'Why is Refund review stuck?')
  assert.equal(chips[1].label, 'Why is Refund review stuck?')
  assert.ok(!chips.some((c) => /finished today/i.test(c.label)))
})

test('Ask chrome copy is work-aware — not fleet/agents', () => {
  const src = readFileSync(new URL('../src/AskPill.jsx', import.meta.url), 'utf8')
  const code = src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')
  assert.match(code, /placeholder="Ask what's waiting or why something's stuck…"/)
  assert.match(code, />Ask</)
  assert.match(code, /Answers from your work record/)
  assert.doesNotMatch(code, /Ask about your fleet/)
  assert.doesNotMatch(code, /Ask about your agents/)
  assert.doesNotMatch(code, /waiting on you/i)
})
