// Agent Flow vs Connections — vocabulary lock.
//
// ConnectionsMap.jsx draws relationships/handoffs BETWEEN agents. That is
// "Agent Flow" in product copy. "Connections" now means the external systems
// Trovis connects to (connectors.js). The component and the backend
// /connections endpoints keep their historical names; only the copy moved,
// and this pins it so the two concepts do not drift back together.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { getConnector } from '../src/connectors.js'

const src = readFileSync(new URL('../src/ConnectionsMap.jsx', import.meta.url), 'utf8')
const copy = src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')

test('the agent-to-agent map is titled Agent Flow and talks about handoffs', () => {
  assert.match(copy, /className="section-label">Agent Flow</)
  assert.match(copy, /Handoffs are observed in\s+shared traces/)
  assert.doesNotMatch(copy, /Connections map/)
  assert.doesNotMatch(copy, /feed into each other/)
  // The draw-your-own affordance is a handoff, not a "connection".
  assert.match(copy, /'Add handoff'/)
  assert.doesNotMatch(copy, /'Add connection'/)
})

test('the map still uses the agent-to-agent /connections API — no backend rename', () => {
  assert.match(src, /api\.getConnections\(\)/)
  assert.match(src, /api\.detectConnections\(\)/)
  assert.match(src, /api\.addConnection\(/)
  assert.match(src, /api\.updateConnection\(/)
  assert.match(src, /api\.deleteConnection\(/)
})

test('AddAgent tiles read their marks from the connector registry', () => {
  const addAgent = readFileSync(new URL('../src/AddAgent.jsx', import.meta.url), 'utf8')
  assert.match(addAgent, /import \{ brandIdForConnector \} from '\.\/connectors\.js'/)
  // The Grok Bot tile keeps the Cursor mark, the SDK tile the xAI mark.
  assert.equal(getConnector('grok-bot').brandId, 'cursor')
  assert.equal(getConnector('grok').brandId, 'grok')
  assert.equal(getConnector('openai-agents').brandId, 'chatgpt')
})
