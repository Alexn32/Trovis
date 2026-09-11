// Set up / Add agent / guided door: is it honest, and does it teach the one
// thing a builder must do?
//
// #137 locked the brand catalog itself (see brandMarks.test.mjs). These cover
// the surfaces a first-run user actually walks through, and the guidance that
// decides whether their work arrives as named jobs or as an unnamed trace.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { BRANDS, LIVE_BRAND_IDS, RECIPE_BRAND_IDS, COMING_BRAND_IDS } from '../src/brandMarks.js'

const read = (f) => readFileSync(new URL(`../src/${f}`, import.meta.url), 'utf8')
const strip = (s) => s.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')

const addAgent = strip(read('AddAgent.jsx'))
const onboarding = strip(read('Onboarding.jsx'))
const guide = strip(read('ConnectGuide.jsx'))
const marks = strip(read('BrandMarks.jsx'))

// --- live vs coming, on every first-run surface -----------------------------

test('every first-run surface shows the works-with strip', () => {
  // Someone can arrive at setup three ways. All three have to set the same
  // expectation about what actually connects today.
  for (const [name, src] of [
    ['Onboarding', onboarding],
    ['Add agent landing', addAgent],
    ['Set up with AI (guided)', guide],
  ]) {
    // Must be RENDERED, not merely imported — an import alone still matches a
    // bare /WorksWithStrip/ and would pass with the strip deleted.
    assert.match(src, /<WorksWithStrip\b/, `${name} renders the strip`)
  }
})

test('the strip labels the two groups differently — live is not "coming"', () => {
  assert.match(marks, /works-with-label">Works with</)
  assert.match(marks, /works-with-label">Coming</)
  // The coming row is marked in the DOM too, not only by its heading, so it
  // can be styled down and read differently by assistive tech.
  assert.match(marks, /works-with-row is-coming/)
  assert.match(marks, /role === 'coming' \? ' is-coming' : ''/)
})

test('the recognition brands are coming, and the doors are not', () => {
  // Recognition-only logos. Stripe / HubSpot / Shopify flipped to live
  // Settings doors; if another of these ever becomes a door it should be
  // a deliberate edit.
  for (const id of ['slack', 'github', 'intercom']) {
    assert.equal(BRANDS[id].role, 'coming', `${id} is recognition-only`)
    assert.ok(COMING_BRAND_IDS.includes(id), `${id} sits in the Coming row`)
  }
  assert.equal(BRANDS.stripe.role, 'live', 'stripe is a live SaaS door')
  assert.ok(LIVE_BRAND_IDS.includes('stripe'), 'stripe sits in the Works-with row')
  assert.equal(BRANDS.hubspot.role, 'live', 'hubspot is a live SaaS door')
  assert.ok(LIVE_BRAND_IDS.includes('hubspot'), 'hubspot sits in the Works-with row')
  assert.equal(BRANDS.shopify.role, 'live', 'shopify is a live SaaS door')
  assert.ok(LIVE_BRAND_IDS.includes('shopify'), 'shopify sits in the Works-with row')
  // And the doors that do work today are not filed as coming.
  for (const id of [...LIVE_BRAND_IDS, ...RECIPE_BRAND_IDS]) {
    assert.notEqual(BRANDS[id].role, 'coming', `${id} is a real door`)
  }
})

test('a recognition brand never becomes a clickable door in the picker', () => {
  // The picker is built from PLATFORMS / RECIPE_PLATFORMS. A SaaS logo showing
  // up there would promise an agent-setup flow that does not exist — Stripe
  // lives in Settings, not Add Agent.
  const picker = addAgent.slice(
    addAgent.indexOf('const PLATFORMS'),
    addAgent.indexOf('function '),
  )
  for (const id of [...COMING_BRAND_IDS, 'stripe', 'hubspot', 'shopify']) {
    assert.doesNotMatch(picker, new RegExp(`id: '${id}'`), `${id} is not a door`)
  }
})

// --- named work: the one rule a builder has to follow -----------------------

test('the raw OTEL recipe teaches the title as a required step, not a footnote', () => {
  assert.match(addAgent, /title="Name the job \(required for named Work\)"/)
  assert.match(addAgent, /<NamedWorkGuidance \/>/)
})

test('the OTEL snippet a builder copies already sets a human title', () => {
  // Copy-paste is the real documentation. The sample span must model the
  // right thing rather than `custom.key`.
  const block = addAgent.slice(addAgent.indexOf('function otelSetupBlock'))
  assert.match(block, /trovis\.loop\.title/)
  assert.match(block, /Approve refund for order #4821/)
  // Grouping too, or every step becomes its own job.
  assert.match(block, /trovis\.loop\.external_id/)
  assert.doesNotMatch(block.slice(0, block.indexOf('}')), /custom\.key/)
})

test('the guidance says what a good title is — and what gets filtered out', () => {
  const g = addAgent.slice(addAgent.indexOf('function NamedWorkGuidance'))
  assert.match(g, /trovis\.loop\.title/)
  // Names the failure mode concretely rather than saying "use a good title".
  assert.match(g, /run_4821/)
  assert.match(g, /UUID/)
  assert.match(g, /filtered out of Work/)
  // Points at the helpers first — hand-writing the attribute is the fallback.
  assert.match(g, /set_loop_title/)
  assert.match(g, /trovis capture on/)
})

test('every live door explains how its jobs get named', () => {
  // A door that never mentions titles ships silent unnamed work.
  for (const fn of [
    'OpenAIAgentsInstructions',
    'AnthropicAgentsInstructions',
    'ClaudeAgentSdkInstructions',
    'CursorOtelInstructions',
    'GrokSdkSetup',
    'GrokBotInstructions',
  ]) {
    const start = addAgent.indexOf(`function ${fn}`)
    assert.ok(start > 0, `${fn} exists`)
    // Bound by the next function, not a fixed character count: a door that
    // grows a section should not fail this, and a door that genuinely stops
    // explaining titles still must.
    const after = addAgent.indexOf('\nfunction ', start + 1)
    const body = addAgent.slice(start, after > 0 ? after : addAgent.length)
    assert.match(body, /loop\.title|set_loop_title|NamedWorkGuidance/, fn)
  }
  // OpenClaw teaches it inside its setup tabs.
  assert.match(addAgent, /Work stays untitled/)
})

test('the Grok door is the SDK path, not xai-sdk\'s own OTLP exporter', () => {
  // xai_sdk.telemetry.Telemetry().setup_otlp_exporter() looks like the
  // obvious recipe and fails three ways at once: protobuf against a JSON
  // ingest, no API key header, and service.name "xai-sdk" on every agent
  // in the org. The door must teach trovis-agents instead, and say why.
  const g = addAgent.slice(addAgent.indexOf('function GrokSdkSetup'))
  assert.match(g, /pip install trovis-agents\[xai\]/)
  assert.match(g, /platform="xai"/)
  assert.doesNotMatch(g.slice(0, g.indexOf('\n}')), /setup_otlp_exporter/)
  // Both silent-failure modes are named where the builder will hit them.
  assert.match(g, /Telemetry\(\)/)
  assert.match(g, /XAI_SDK_DISABLE_TRACING/)
  // And the OpenAI-compatible endpoint is pointed somewhere real.
  assert.match(g, /api\.x\.ai/)
  // The provider picker's xAI branch renders the same steps — one recipe.
  const px = addAgent.slice(addAgent.indexOf('function PythonXaiInstructions'))
  assert.match(px.slice(0, px.indexOf('\n}')), /<GrokSdkSetup/)
})

test('the two Grok doors are named apart and never blur together', () => {
  // Two different products with the same word in the name. "Grok (xAI SDK)" is
  // an app built on xai-sdk; "Grok Bot" is a desktop assistant that
  // reports over MCP. Calling either by the other's name sends a builder down
  // a path that cannot work for them.
  assert.match(addAgent, /label: 'Grok \(xAI SDK\)'/)
  assert.match(addAgent, /label: 'Grok Bot'/)
  assert.match(addAgent, /Connect Grok \(xAI SDK\)/)
  assert.match(addAgent, /Connect a Grok Bot/)
  assert.match(guide, /'Grok \(xAI SDK\)'/)
  assert.match(guide, /'Grok Bot'/)

  // The SDK door never calls its user's app a bot...
  const sdkDoor = addAgent.slice(
    addAgent.indexOf('function GrokInstructions'),
    addAgent.indexOf('function GrokBotInstructions'),
  )
  assert.doesNotMatch(sdkDoor, /\bbots?\b/i, 'the xAI SDK door calls an app a bot')

  // ...and the Bot door never sells itself as the SDK path. It says so out
  // loud, because a Grok Bot user who pip-installs trovis-agents gets nothing.
  // `addAgent` is comment-stripped, so bound the slice by the next function
  // rather than a comment banner — otherwise it runs to end of file.
  const botBody = addAgent.slice(
    addAgent.indexOf('function GrokBotInstructions'),
    addAgent.indexOf('function ChatGPTInstructions'),
  )
  assert.doesNotMatch(botBody, /pip install/, 'the Grok Bot door hands out a pip command')
  assert.match(botBody, /Not the same as/)
  assert.match(botBody, /xai-sdk/)
})

test('the Grok Bot door admits the bot has to call in', () => {
  // Grok Bots export nothing and Trovis cannot pull them. A page that implies
  // silent telemetry would have someone add an MCP server, see nothing, and
  // conclude Trovis is broken.
  const body = addAgent.slice(
    addAgent.indexOf('function GrokBotInstructions'),
    addAgent.indexOf('function ChatGPTInstructions'),
  )
  assert.match(body, /reporting, not telemetry/i)
  // Paste-to-the-bot is the path that worked first time; MCP-settings editing
  // is the fallback. If those ever swap, the page is teaching the slower one.
  assert.ok(
    body.indexOf('Paste this to your Bot') < body.indexOf("can&apos;t add its own MCP server"),
    'the manual MCP-settings path outranks paste-to-the-bot',
  )
  // The failure the first real connection actually hit.
  assert.match(body, /placeholder/i)
  assert.match(body, /remove and re-add/i)
  assert.match(body, /never report anything/i)
  assert.doesNotMatch(body, /automatic(ally)?/i)
  // The MCP URL and the auth header are the two things they must copy.
  assert.match(body, /computeGrokMcpUrl\(\)/)
  assert.match(body, /Authorization/)
  // And the four tools are named where the reader can check the Bot sees them.
  for (const tool of ['report_job_started', 'report_job_waiting',
                      'report_job_finished', 'report_job_failed']) {
    assert.match(body, new RegExp(tool), tool)
  }
})

test('the Actions door admits it cannot produce named jobs yet', () => {
  // /actions/log has no title field, so a no-code GPT lands as activity.
  // Saying so is the point: the alternative is a builder wondering why their
  // GPT never shows up on Work.
  const c = addAgent.slice(addAgent.indexOf('function ChatGPTInstructions'))
  assert.match(c, /Activity, not named jobs/)
  assert.match(c, /no field for a job title/)
  // And offers the path that does work.
  assert.match(c, /emit OpenTelemetry with/)
})

// --- copy ------------------------------------------------------------------

test('setup copy never sends anyone to the desktop table', () => {
  // Work is a stacked queue on mobile and a table on desktop. Setup copy has
  // no idea which one the reader is on, so it must not name either.
  for (const [name, src] of [
    ['AddAgent', addAgent],
    ['Onboarding', onboarding],
    ['ConnectGuide', guide],
  ]) {
    for (const m of src.matchAll(/>([^<>{}]{4,})</g)) {
      const line = m[1].trim()
      assert.ok(
        !/\b(the|your|a)\s+(Work\s+)?(table|Monday table|board)\b/i.test(line),
        `${name} points at a desktop-only surface: ${JSON.stringify(line)}`,
      )
    }
  }
})

test('success copy promises named jobs, not a telemetry dump', () => {
  const s = addAgent.slice(addAgent.indexOf('function SuccessCallout'))
  assert.match(s, /named jobs/)
  assert.doesNotMatch(s.slice(0, s.indexOf('\n}')), /span|trace|OTel/i)
})

test('the guidance frames work as hybrid — person, agent and tool', () => {
  const g = addAgent.slice(addAgent.indexOf('function NamedWorkGuidance'))
  assert.match(g, /person/i)
  assert.match(g, /agent/i)
  assert.match(g, /SaaS|tool/i)
})
