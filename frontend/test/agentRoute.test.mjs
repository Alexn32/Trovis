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
import { agentRoute } from '../src/agentRoute.js'


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

test('a missing row does not throw on the way to a click handler', () => {
  assert.deepEqual(agentRoute(undefined), [undefined, 'main'])
  assert.deepEqual(agentRoute(null), [undefined, 'main'])
})

// --- the call sites ---------------------------------------------------------

