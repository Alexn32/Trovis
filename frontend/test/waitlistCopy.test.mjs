// The public waitlist must stay the locked Home/Work founding pitch —
// not observability, not Fleet/Cost, and not a claim that Connect is live.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const landing = readFileSync(new URL('../src/TrovisLanding.jsx', import.meta.url), 'utf8')
const html = readFileSync(new URL('../../static/waitlist.html', import.meta.url), 'utf8')
const index = readFileSync(new URL('../index.html', import.meta.url), 'utf8')

function visible(src) {
  return src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')
}

const LOCKED_BULLETS = [
  'Home desk: what needs you, not another dashboard',
  'Work as jobs — people, agents, and the tools already in the loop — one place to see and judge.',
  'See what your agents are actually doing — and who’s waiting / what’s stuck — without drowning in traces',
  'Built for eng/founder teams with agents already in production',
]

for (const [name, src] of [
  ['TrovisLanding.jsx', visible(landing)],
  ['static/waitlist.html', html],
]) {
  test(`${name} ships the locked founding copy`, () => {
    assert.match(src, /See the Work/)
    assert.match(src, /across humans, agents, and tools/)
    assert.match(src, /operating layer for hybrid work/)
    assert.match(src, /what’s waiting on you/)
    assert.match(src, /what’s blocked/)
    assert.match(src, /where handoffs get stuck/)
    assert.match(src, /Founding seats for\s+teams already running agents/)
    for (const bullet of LOCKED_BULLETS) {
      assert.ok(src.includes(bullet), `missing locked bullet: ${bullet}`)
    }
    assert.match(src, /Join the founding list/)
    assert.match(src, /Desk and Work visibility first/)
    assert.match(src, /we’re not pitching them as live yet/)
    assert.match(src, /name=["']name["']|id=["']wl-name["']/)
    assert.match(src, /name=["']email["']|id=["']wl-email["']/)
    assert.match(src, /name=["']company["']|id=["']wl-company["']/)
    assert.match(src, /name=["']role["']|id=["']wl-role["']/)
    assert.match(src, /name=["']tools["']|id=["']wl-tools["']/)
    assert.match(src, /What agents\/tools are already in your loop\?/)
  })

  test(`${name} keeps name optional and before email`, () => {
    const nameAt = src.search(/name=["']name["']|id=["']wl-name["']/)
    const emailAt = src.search(/name=["']email["']|id=["']wl-email["']/)
    assert.ok(nameAt >= 0 && emailAt > nameAt, 'name field should precede email')
    const nameBlock = src.slice(nameAt, emailAt)
    assert.doesNotMatch(nameBlock, /\srequired(\s|>|\/)/)
    assert.match(src, /Name[\s\S]{0,160}\(optional\)/)
  })

  test(`${name} does not sell the old landing`, () => {
    const low = src.toLowerCase()
    assert.ok(!low.includes('create your account'))
    assert.ok(!low.includes('cost tracking'))
    assert.ok(!low.includes('connect is live'))
    assert.ok(!low.includes('saas in one loop'))
    assert.ok(!low.includes('record themselves'))
    assert.ok(!low.includes('trace dump'))
    assert.ok(!low.includes('handoffs die'))
    assert.doesNotMatch(src, /\bfleet\b/i)
  })
}

test('index.html SEO describes the waitlist', () => {
  assert.match(index, /Founding waitlist/)
  assert.match(index, /operating layer for hybrid work/)
  assert.match(index, /where handoffs get stuck/)
  assert.match(index, /what your agents are actually doing/)
})
