// A row's LABEL is not its ROUTE.
//
// Reported bug: clicking a row in Home's work feed opened a blank screen. The
// row's `agent` is the display name once an operator renames an agent, and the
// click navigated with it — GET /agents/Support%20Bot/summary 404s, and the
// agent page answered with a back-link over one line of error text, which
// reads as blank.
//
// These pin both halves of the fix: the helper picks the routable identity,
// and every agent click on Home actually goes through it.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { agentRoute } from '../src/agentRoute.js'

const dash = readFileSync(new URL('../src/Dashboard.jsx', import.meta.url), 'utf8')
const code = dash.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')

// --- the helper -------------------------------------------------------------

test('a renamed agent routes by service name, not by what the row reads', () => {
  // The exact shape /dashboard/work-feed returns for a renamed agent.
  const row = { agent: 'Support Bot', service_name: 'support-agent', agent_id: 'main' }
  assert.deepEqual(agentRoute(row), ['support-agent', 'main'])
  // And specifically NOT the label — that is the 404.
  assert.notEqual(agentRoute(row)[0], row.agent)
})

test('a drift row keeps its sub-agent instead of collapsing to main', () => {
  // Multi-agent services label the row "Support Bot · researcher"; the route
  // is the service plus that agent_id, and neither is derivable from the text.
  const row = { agent: 'Support Bot · researcher', service_name: 'support-agent', agent_id: 'researcher' }
  assert.deepEqual(agentRoute(row), ['support-agent', 'researcher'])
})

test('agent_id defaults to main when a row omits it', () => {
  assert.deepEqual(agentRoute({ agent: 'x', service_name: 'svc' }), ['svc', 'main'])
  assert.deepEqual(agentRoute({ agent: 'x', service_name: 'svc', agent_id: '' }), ['svc', 'main'])
})

test('a row cached before the fix still navigates somewhere', () => {
  // Attention rows carry a 1h TTL, so rows written by the old code outlive the
  // deploy. Falling back to the label reproduces the old behaviour for an hour
  // rather than navigating to undefined, which would blank the page for good.
  assert.deepEqual(agentRoute({ agent: 'support-agent' }), ['support-agent', 'main'])
})

test('a missing row does not throw on the way to a click handler', () => {
  assert.deepEqual(agentRoute(undefined), [undefined, 'main'])
  assert.deepEqual(agentRoute(null), [undefined, 'main'])
})

// --- the call sites ---------------------------------------------------------

test('every agent click on Home routes through the helper', () => {
  // Home opens an agent from one place now — a Trovis noticed line about
  // agent health — and it must spread agentRoute rather than reach for a
  // field itself. Any future call site is held to the same rule.
  const clicks = [...code.matchAll(/onOpenAgent\(([^)]*)\)/g)]
    .map((m) => m[1].trim())
    .filter((a) => a !== '')
  assert.ok(clicks.length >= 1, `expected at least one agent click, saw ${clicks.length}`)
  for (const args of clicks) {
    assert.match(args, /^\.\.\.agentRoute\(/, `onOpenAgent(${args}) bypasses agentRoute`)
  }
})

test('Home never navigates by a display label', () => {
  // The regression in one line: no click handler may pass `.agent` as a route.
  assert.doesNotMatch(code, /onOpenAgent\([^)]*\.agent\b(?!_id)/)
})

test('the helper is shared, not re-declared inside the page', () => {
  // A local copy is how the two call sites drift apart again.
  assert.match(code, /import \{ agentRoute \} from '\.\/agentRoute\.js'/)
  assert.doesNotMatch(code, /function agentRoute\b/)
})
