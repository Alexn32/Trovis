// "Set up with AI" snippets: the guide's copy-paste blocks must carry the
// org's real key + endpoint, while the thread posted to /connect/ask keeps the
// placeholders. Both properties were browser-verified once; these lock them.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import {
  ENDPOINT_PLACEHOLDER,
  KEY_FALLBACK,
  MCP_URL_PLACEHOLDER,
  KEY_PLACEHOLDER,
  flattenAssistant,
  substitute,
} from '../src/connectSnippets.js'

// Obvious fakes — never a real credential in a fixture.
const KEY = 'ov_sk_fake000000000000000000test'
const ENDPOINT = 'https://api.example.test/v1/traces'
const MCP_URL = 'https://api.example.test/mcp/grok'

test('value positions get the real key and endpoint', () => {
  const snippet =
    'from trovis import init\n' +
    `init(api_key="${KEY_PLACEHOLDER}", endpoint="${ENDPOINT_PLACEHOLDER}", agent_name="my-agent")`
  const out = substitute(snippet, KEY, ENDPOINT)
  assert.equal(
    out,
    'from trovis import init\n' +
      `init(api_key="${KEY}", endpoint="${ENDPOINT}", agent_name="my-agent")`,
  )
  // Nothing left for the user to fill in by hand.
  assert.doesNotMatch(out, /TROVIS_API_KEY|TROVIS_ENDPOINT/)
})

test('a placeholder used as an env-var NAME stays a name', () => {
  // Only the value is substituted, so Copy yields a runnable line instead of
  // `export ov_sk_…=ov_sk_…`.
  const out = substitute(
    `export ${KEY_PLACEHOLDER}=${KEY_PLACEHOLDER}`,
    KEY,
    ENDPOINT,
  )
  assert.equal(out, `export ${KEY_PLACEHOLDER}=${KEY}`)
  assert.equal(
    substitute(`${ENDPOINT_PLACEHOLDER}=${ENDPOINT_PLACEHOLDER}`, KEY, ENDPOINT),
    `${ENDPOINT_PLACEHOLDER}=${ENDPOINT}`,
  )
  // A name followed by whitespace before the `=` is still a name.
  assert.equal(
    substitute(`${KEY_PLACEHOLDER} = x`, KEY, ENDPOINT),
    `${KEY_PLACEHOLDER} = x`,
  )
})

test('the generic OTEL recipe substitutes every value, including the header', () => {
  const out = substitute(
    [
      'OTEL_SERVICE_NAME=my-agent',
      `OTEL_EXPORTER_OTLP_ENDPOINT=${ENDPOINT_PLACEHOLDER}`,
      'OTEL_EXPORTER_OTLP_PROTOCOL=http/json',
      `OTEL_EXPORTER_OTLP_HEADERS=X-Trovis-Api-Key=${KEY_PLACEHOLDER}`,
    ].join('\n'),
    KEY,
    ENDPOINT,
  )
  assert.match(out, new RegExp(`OTEL_EXPORTER_OTLP_ENDPOINT=${ENDPOINT}`))
  assert.match(out, new RegExp(`X-Trovis-Api-Key=${KEY}`))
})

test('every occurrence is replaced, not just the first', () => {
  const out = substitute(
    `${KEY_PLACEHOLDER} ${KEY_PLACEHOLDER} ${ENDPOINT_PLACEHOLDER} ${ENDPOINT_PLACEHOLDER}`,
    KEY,
    ENDPOINT,
  )
  assert.equal(out, `${KEY} ${KEY} ${ENDPOINT} ${ENDPOINT}`)
})

test('no key in the session falls back to the visible placeholder value', () => {
  // ConnectGuide pairs this with a "replace ov_sk_… with your key" note.
  const out = substitute(`init(api_key="${KEY_PLACEHOLDER}")`, null, ENDPOINT)
  assert.equal(out, `init(api_key="${KEY_FALLBACK}")`)
})

test('empty and missing snippet text do not throw', () => {
  assert.equal(substitute('', KEY, ENDPOINT), '')
  assert.equal(substitute(undefined, KEY, ENDPOINT), '')
  assert.equal(substitute(null, KEY, ENDPOINT), '')
})

test('the wire history keeps placeholders — the key never leaves the browser', () => {
  const turn = {
    role: 'assistant',
    content: 'Add these two lines at the top of your entry file.',
    code: [
      { title: 'Install', language: 'bash', content: 'pip install trovis-agents[openai]' },
      {
        title: 'Initialize',
        language: 'python',
        content: `init(api_key="${KEY_PLACEHOLDER}", endpoint="${ENDPOINT_PLACEHOLDER}")`,
      },
    ],
  }
  // Rendering substitutes (display only)...
  assert.match(substitute(turn.code[1].content, KEY, ENDPOINT), new RegExp(KEY))
  // ...but what we post back to /connect/ask must not carry the credential.
  const wire = flattenAssistant(turn)
  assert.ok(!wire.includes(KEY), 'flattened history must not contain the key')
  assert.match(wire, new RegExp(KEY_PLACEHOLDER))
  assert.match(wire, new RegExp(ENDPOINT_PLACEHOLDER))
  // The model still sees its own answer and both snippets.
  assert.match(wire, /Add these two lines/)
  assert.match(wire, /pip install trovis-agents\[openai\]/)
})

test('an assistant turn with no code flattens to just its answer', () => {
  assert.equal(
    flattenAssistant({ content: "What's your agent built with?", code: [] }),
    "What's your agent built with?",
  )
  assert.equal(flattenAssistant({ content: 'No code key at all.' }), 'No code key at all.')
})

test('a Grok Bot snippet gets the MCP URL, never the traces endpoint', () => {
  // A Bot is pointed at the MCP server. Filling the OTLP ingest URL in there
  // would have someone paste a URL that speaks no MCP and see nothing.
  const snippet =
    `{"mcpServers":{"trovis":{"url":"${MCP_URL_PLACEHOLDER}",` +
    `"headers":{"Authorization":"Bearer ${KEY_PLACEHOLDER}"}}}}`
  const out = substitute(snippet, KEY, ENDPOINT, MCP_URL)
  assert.match(out, new RegExp(MCP_URL.replace(/[/.]/g, '\\$&')))
  assert.match(out, new RegExp(KEY))
  assert.doesNotMatch(out, /TROVIS_MCP_URL|TROVIS_API_KEY/)
  // The two URLs must not be confusable with each other.
  assert.doesNotMatch(out, /v1\/traces/)
})

test('the MCP placeholder as an env-var NAME stays a name, like the others', () => {
  assert.equal(
    substitute(`${MCP_URL_PLACEHOLDER}=${MCP_URL_PLACEHOLDER}`, KEY, ENDPOINT, MCP_URL),
    `${MCP_URL_PLACEHOLDER}=${MCP_URL}`,
  )
})

test('the guide passes the real MCP URL through to snippets', () => {
  // substitute() can only fill what ConnectGuide hands it — a missing 4th
  // argument would silently blank every Grok Bot URL.
  const guide = readFileSync(new URL('../src/ConnectGuide.jsx', import.meta.url), 'utf8')
  assert.match(guide, /computeGrokMcpUrl\(\)/)
  assert.match(guide, /substitute\(c\.content, orgKey, endpoint, mcpUrl\)/)
})

test('ConnectGuide uses these helpers instead of its own copy', () => {
  // A re-inlined private substitute() would drift out from under this test.
  const guide = readFileSync(new URL('../src/ConnectGuide.jsx', import.meta.url), 'utf8')
  assert.match(guide, /from '\.\/connectSnippets\.js'/)
  assert.doesNotMatch(guide, /function substitute\s*\(/)
  assert.doesNotMatch(guide, /function flattenAssistant\s*\(/)
})
