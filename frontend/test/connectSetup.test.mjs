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
  // Recognition-only logos. Stripe flipped to a live Settings door; if another
  // of these ever becomes a door it should be a deliberate edit here.
  for (const id of ['slack', 'github', 'hubspot', 'intercom', 'shopify']) {
    assert.equal(BRANDS[id].role, 'coming', `${id} is recognition-only`)
    assert.ok(COMING_BRAND_IDS.includes(id), `${id} sits in the Coming row`)
  }
  assert.equal(BRANDS.stripe.role, 'live', 'stripe is a live SaaS door')
  assert.ok(LIVE_BRAND_IDS.includes('stripe'), 'stripe sits in the Works-with row')
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
  for (const id of [...COMING_BRAND_IDS, 'stripe']) {
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
  ]) {
    const start = addAgent.indexOf(`function ${fn}`)
    assert.ok(start > 0, `${fn} exists`)
    const body = addAgent.slice(start, start + 4000)
    assert.match(body, /loop\.title|set_loop_title|NamedWorkGuidance/, fn)
  }
  // OpenClaw teaches it inside its setup tabs.
  assert.match(addAgent, /Work stays untitled/)
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
