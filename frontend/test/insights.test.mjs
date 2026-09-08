// Judgment ribbon rules.
//
// The bar these tests defend: an insight must say something the rows below it
// do not. Most of these cases are about STAYING SILENT — the ribbon's failure
// mode is clutter, not omission.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { buildInsights, MAX_INSIGHTS } from '../src/insights.js'

const NOW = Date.parse('2026-03-10T12:00:00Z')
const agoS = (s) => new Date(NOW - s * 1000).toISOString()
const HR = 3600

function item(over = {}) {
  return {
    id: over.id ?? 1,
    title: over.title ?? 'Approve refund #4821',
    status: over.status ?? 'moving',
    holder: over.holder ?? { kind: 'agent', name: 'Support Bot' },
    whats_next: over.whats_next ?? 'In progress',
    updated_at: over.updated_at ?? agoS(60),
  }
}

const person = (name) => ({ kind: 'human', name })
const tool = (name) => ({ kind: 'tool', name })

test('nothing to say → the ribbon is silent', () => {
  assert.deepEqual(buildInsights({ items: [], health: [], nowMs: NOW }), [])
  // Work that is simply moving is not an insight.
  assert.deepEqual(
    buildInsights({ items: [item({ status: 'moving' })], health: [], nowMs: NOW }),
    [],
  )
})

test('one item waiting on you is NOT an insight — the queue already says it', () => {
  const out = buildInsights({
    items: [item({ id: 1, status: 'waiting_on_you' })],
    nowMs: NOW,
  })
  assert.deepEqual(out, [])
})

test('two waiting on you earns a "start here", pointing at the oldest', () => {
  const out = buildInsights({
    items: [
      item({ id: 1, status: 'waiting_on_you', title: 'Newer thing', updated_at: agoS(HR) }),
      item({ id: 2, status: 'waiting_on_you', title: 'Older thing', updated_at: agoS(5 * HR) }),
    ],
    nowMs: NOW,
  })
  assert.equal(out.length, 1)
  assert.equal(out[0].kind, 'start-here')
  assert.match(out[0].text, /Older thing/)
  assert.equal(out[0].item.id, 2)
})

test('one person holding several things is the headline insight', () => {
  const out = buildInsights({
    items: [
      item({ id: 1, status: 'stuck', holder: person('Sarah Chen'), updated_at: agoS(2 * HR) }),
      item({ id: 2, status: 'stuck', holder: person('Sarah Chen'), updated_at: agoS(9 * HR) }),
      item({ id: 3, status: 'stuck', holder: person('Dev Patel'), updated_at: agoS(HR) }),
    ],
    nowMs: NOW,
  })
  const p = out.find((i) => i.kind === 'bottleneck-person')
  assert.ok(p, 'a person bottleneck is surfaced')
  assert.match(p.text, /Sarah Chen is holding 2 things/)
  assert.match(p.text, /9h/)
  assert.equal(p.item.id, 2, 'click target is the oldest of that person’s items')
  // Dev Patel holds only one — not a pattern, not a line.
  assert.ok(!out.some((i) => i.text.includes('Dev Patel')))
})

test('several things on one tool reads as one broken integration', () => {
  const out = buildInsights({
    items: [
      item({ id: 1, status: 'stuck', holder: tool('Stripe'), updated_at: agoS(8 * HR) }),
      item({ id: 2, status: 'stuck', holder: tool('Stripe'), updated_at: agoS(3 * HR) }),
    ],
    nowMs: NOW,
  })
  const t = out.find((i) => i.kind === 'bottleneck-tool')
  assert.ok(t)
  assert.match(t.text, /2 things are waiting on Stripe/)
  assert.equal(t.item.id, 1)
  assert.equal(t.tone, 'risk')
})

test('"Unassigned" is a gap, not a bottleneck — never named as a holder', () => {
  const out = buildInsights({
    items: [
      item({ id: 1, status: 'stuck', holder: { kind: 'human', name: 'Unassigned' } }),
      item({ id: 2, status: 'stuck', holder: { kind: 'human', name: 'Unassigned' } }),
    ],
    nowMs: NOW,
  })
  assert.ok(!out.some((i) => /Unassigned/i.test(i.text)))
})

test('a fresh wait on a person never reaches the ribbon', () => {
  // 30 minutes on someone else is normal, not attention — so no pattern.
  const fresh = agoS(30 * 60)
  const out = buildInsights({
    items: [
      item({ id: 1, status: 'waiting_on_other', holder: person('Sarah Chen'), updated_at: fresh }),
      item({ id: 2, status: 'waiting_on_other', holder: person('Sarah Chen'), updated_at: fresh }),
    ],
    nowMs: NOW,
  })
  assert.deepEqual(out, [])
})

test('aging waits on the same person DO make a pattern', () => {
  const old = agoS(6 * HR)
  const out = buildInsights({
    items: [
      item({ id: 1, status: 'waiting_on_other', holder: person('Sarah Chen'), updated_at: old }),
      item({ id: 2, status: 'waiting_on_other', holder: person('Sarah Chen'), updated_at: old }),
    ],
    nowMs: NOW,
  })
  assert.equal(out.length, 1)
  assert.match(out[0].text, /Sarah Chen is holding 2 things/)
})

test('agent health surfaces here because it cannot surface anywhere else', () => {
  const out = buildInsights({
    items: [],
    health: [{ severity: 'critical', agent: 'billing-agent', title: "billing-agent hasn't run in 3 days" }],
    nowMs: NOW,
  })
  assert.equal(out.length, 1)
  assert.equal(out[0].kind, 'agent-risk')
  assert.equal(out[0].agent, 'billing-agent')
  assert.match(out[0].text, /\.$/, 'reads as a sentence')
  assert.equal(out[0].item, undefined, 'agent lines click to the agent, not a work item')
})

test('info-severity agent noise is not worth a line', () => {
  const out = buildInsights({
    items: [],
    health: [{ severity: 'info', agent: 'ops-agent', title: 'ops-agent is idle' }],
    nowMs: NOW,
  })
  assert.deepEqual(out, [])
})

test('only one agent line, however many agents are unhappy', () => {
  const out = buildInsights({
    items: [],
    health: [
      { severity: 'critical', agent: 'a', title: 'a is down' },
      { severity: 'critical', agent: 'b', title: 'b is down' },
      { severity: 'warning', agent: 'c', title: 'c is slow' },
    ],
    nowMs: NOW,
  })
  assert.equal(out.filter((i) => i.kind === 'agent-risk').length, 1)
})

test('never more than three, however much is wrong', () => {
  const out = buildInsights({
    items: [
      item({ id: 1, status: 'stuck', holder: person('Sarah Chen'), updated_at: agoS(9 * HR) }),
      item({ id: 2, status: 'stuck', holder: person('Sarah Chen'), updated_at: agoS(8 * HR) }),
      item({ id: 3, status: 'stuck', holder: tool('Stripe'), updated_at: agoS(7 * HR) }),
      item({ id: 4, status: 'stuck', holder: tool('Stripe'), updated_at: agoS(6 * HR) }),
      item({ id: 5, status: 'waiting_on_you', updated_at: agoS(5 * HR) }),
      item({ id: 6, status: 'waiting_on_you', updated_at: agoS(4 * HR) }),
    ],
    health: [{ severity: 'critical', agent: 'x', title: 'x is down' }],
    nowMs: NOW,
  })
  assert.equal(out.length, MAX_INSIGHTS)
  assert.equal(MAX_INSIGHTS, 3)
  // The pattern insights outrank "start here" and the agent line.
  assert.deepEqual(out.map((i) => i.kind), [
    'bottleneck-person',
    'bottleneck-tool',
    'start-here',
  ])
})

test('every insight is clickable — an observation you cannot act on is noise', () => {
  const out = buildInsights({
    items: [
      item({ id: 1, status: 'stuck', holder: tool('Stripe'), updated_at: agoS(8 * HR) }),
      item({ id: 2, status: 'stuck', holder: tool('Stripe'), updated_at: agoS(3 * HR) }),
    ],
    health: [{ severity: 'warning', agent: 'ops-agent', title: 'ops-agent is erroring' }],
    nowMs: NOW,
  })
  assert.ok(out.length > 0)
  for (const i of out) {
    assert.ok(i.item || i.agent, `${i.kind} has somewhere to go`)
    assert.ok(i.id && i.tone && i.text)
  }
})

test('the ribbon stays silent while work is still loading, and on failure', () => {
  const dash = readFileSync(new URL('../src/Dashboard.jsx', import.meta.url), 'utf8')
  const code = dash.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')
  const fn = code.slice(code.indexOf('function JudgmentRibbon'))
  assert.match(fn, /if \(work\.items === null\) return null/)
  assert.match(fn, /if \(insights\.length === 0\) return null/)
})

test('the ribbon costs nothing on first paint — it fetches nothing', () => {
  const src = readFileSync(new URL('../src/insights.js', import.meta.url), 'utf8')
  const code = src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')
  assert.doesNotMatch(code, /\bfetch\(/)
  assert.doesNotMatch(code, /from '\.\/api\.js'/)
})

test('no Trovis jargon in ribbon copy', () => {
  const src = readFileSync(new URL('../src/insights.js', import.meta.url), 'utf8')
  const code = src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')
  const FORBIDDEN = /\b(loops?|possession|segments?|stations?|handoffs?|spans?|otel|telemetry)\b/i
  for (const m of code.matchAll(/`([^`]*)`/g)) {
    assert.ok(!FORBIDDEN.test(m[1]), `ribbon ships jargon: ${JSON.stringify(m[1])}`)
  }
})
