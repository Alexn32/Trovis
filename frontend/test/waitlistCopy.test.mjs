// The public waitlist must stay a Work-desk founding list — not the old
// observability / pricing landing, and not a claim that Connect is live.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const landing = readFileSync(new URL('../src/TrovisLanding.jsx', import.meta.url), 'utf8')
const html = readFileSync(new URL('../../static/waitlist.html', import.meta.url), 'utf8')
const index = readFileSync(new URL('../index.html', import.meta.url), 'utf8')

function visible(src) {
  return src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')
}

for (const [name, src] of [
  ['TrovisLanding.jsx', visible(landing)],
  ['static/waitlist.html', html],
]) {
  test(`${name} is a founding Work-desk waitlist`, () => {
    assert.match(src, /founding/i)
    assert.match(src, /Work desk/)
    assert.match(src, /handoff/i)
    assert.match(src, /waiting/i)
    assert.match(src, /Join the founding list/)
    assert.match(src, /name=["']email["']|id=["']wl-email["']/)
    assert.match(src, /name=["']company["']|id=["']wl-company["']/)
    assert.match(src, /name=["']role["']|id=["']wl-role["']/)
    assert.match(src, /name=["']tools["']|id=["']wl-tools["']/)
  })

  test(`${name} does not sell the old landing`, () => {
    const low = src.toLowerCase()
    assert.ok(!low.includes('create your account'))
    assert.ok(!low.includes('cost tracking'))
    assert.ok(!low.includes('connect is live'))
    assert.ok(!low.includes('monday of'))
    assert.doesNotMatch(src, /\bfleet\b/i)
  })
}

test('index.html SEO describes the waitlist', () => {
  assert.match(index, /Founding waitlist for the Work desk/)
  assert.match(index, /operating layer for hybrid work/)
  assert.doesNotMatch(index, /See what your agents are actually doing/)
})
