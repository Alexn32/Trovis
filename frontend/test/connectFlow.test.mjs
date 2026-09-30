// The guided setup's lifecycle words come from the verification read, and
// only from it: no state is inferred that the endpoint did not return, and
// "cannot see" is capability language about a connector KIND, never a verdict
// about a run.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { cannotSee, lifecycle, seesList } from '../src/connectFlow.js'
import { getConnector } from '../src/connectors.js'
import { CONNECTION_PLACEHOLDER, substitute, withConnectionKey } from '../src/connectSnippets.js'

const rel = (iso) => `REL(${iso})`
const claude = getConnector('claude')

test('seesList words the observed kinds in a fixed order', () => {
  assert.deepEqual(seesList({ execution: true, actions: true, model_usage: false, named_work: true }),
    ['execution', 'tool activity', 'named runs'])
  assert.deepEqual(seesList({}), [])
  assert.deepEqual(seesList(null), [])
})

test('lifecycle: the four recorded phases plus the honest middle', () => {
  assert.equal(lifecycle(null, claude).phase, 'unknown')
  assert.deepEqual(lifecycle({ state: 'setup_started', attribution: null, sees: {} }, claude, rel), {
    phase: 'setup_started', title: 'Set up Claude Agents, then run it',
    detail: 'Trovis is listening for the first data from this setup.',
  })
  assert.equal(lifecycle({ state: 'waiting_for_data', attribution: null, sees: {} }, claude, rel).phase, 'waiting_for_data')
  const connected = lifecycle({
    state: 'connected', attribution: 'instance', last_observed_at: 'T',
    sees: { execution: true, actions: true, model_usage: false, named_work: false },
  }, claude, rel)
  assert.deepEqual(connected, {
    phase: 'connected', title: 'Claude Agents connected',
    detail: 'Last data REL(T) · Trovis can see: execution, tool activity',
  })
  // Traffic for the connector without this setup's id: said plainly, never
  // promoted to connected.
  const middle = lifecycle({
    state: 'setup_started', attribution: 'connector', services: [{ service_name: 'refund-helper' }],
    sees: { execution: true },
  }, claude, rel)
  assert.equal(middle.phase, 'connector_only')
  assert.match(middle.title, /telemetry is arriving from refund-helper/)
  assert.match(middle.detail, /does not carry this setup’s id/)
  assert.equal(lifecycle({ state: 'disconnected', sees: {} }, claude, rel).phase, 'disconnected')
})

test('cannotSee names external outcomes as a capability gap of AI connectors, and who adds it', () => {
  const gap = cannotSee(claude)
  assert.ok(gap)
  assert.match(gap.text, /cannot independently see/)
  assert.deepEqual(gap.connectors, ['stripe', 'hubspot', 'shopify'])
  assert.doesNotMatch(gap.text, /missing|not observed|should/i, 'capability, not a verdict')
  // A work system already contributes external outcomes: nothing to add.
  assert.equal(cannotSee(getConnector('stripe')), null)
  assert.equal(cannotSee(null), null)
})

// --- the connection key in snippets ------------------------------------------

test('a snippet carries the connection key when the setup has one', () => {
  const py = `init(api_key="TROVIS_API_KEY", agent_name="x", connection_id="${CONNECTION_PLACEHOLDER}")`
  assert.equal(withConnectionKey(py, 'cn_abc'), 'init(api_key="TROVIS_API_KEY", agent_name="x", connection_id="cn_abc")')
  assert.equal(substitute(py, 'ov_sk_k', 'E', null, 'cn_abc'),
    'init(api_key="ov_sk_k", agent_name="x", connection_id="cn_abc")')
})

test('and never ships the placeholder when there is none: the carrying line is dropped', () => {
  // A trailing kwarg on a one-line init()…
  assert.equal(withConnectionKey(`init(api_key="K", agent_name="x", connection_id="${CONNECTION_PLACEHOLDER}")`, null),
    'init(api_key="K", agent_name="x")')
  // …its own kwarg line in a multi-line init()…
  assert.equal(withConnectionKey(`init(\n    api_key="K",\n    connection_id="${CONNECTION_PLACEHOLDER}",\n    platform="xai",\n)`, null),
    'init(\n    api_key="K",\n    platform="xai",\n)')
  // …an env var, and a resource attribute.
  assert.equal(withConnectionKey(`A=1 \\\nOTEL_RESOURCE_ATTRIBUTES=trovis.connection.id=${CONNECTION_PLACEHOLDER} \\\nrun`, null),
    'A=1 \\\nrun')
  assert.equal(withConnectionKey(`{\n  "service.name": "x",\n  "trovis.connection.id": "${CONNECTION_PLACEHOLDER}",\n}`, null),
    '{\n  "service.name": "x",\n}')
  assert.doesNotMatch(substitute(`x ${CONNECTION_PLACEHOLDER}`, 'k', 'e'), /TROVIS_CONNECTION_ID/)
})

test('the Grok Bot MCP URL names the instance when the setup has one', () => {
  assert.equal(substitute('URL: TROVIS_MCP_URL', 'k', 'e', 'https://x/mcp/grok', 'cn_abc'),
    'URL: https://x/mcp/grok?connection=cn_abc')
  assert.equal(substitute('URL: TROVIS_MCP_URL', 'k', 'e', 'https://x/mcp/grok'), 'URL: https://x/mcp/grok')
})
