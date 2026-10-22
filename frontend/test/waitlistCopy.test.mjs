// The public waitlist landing: one shared page (src/landing/) behind both
// front doors, carrying the approved copy and staying inside the messaging
// boundaries — understanding first, later stages labelled as vision, the
// preview and discoveries labelled as illustrative.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const read = (p) => readFileSync(new URL(p, import.meta.url), 'utf8')
const markup = read('../src/landing/landing.html')
const behaviour = read('../src/landing/landing.js')
const wrapper = read('../src/TrovisLanding.jsx')
const shell = read('../../static/waitlist.html')
const index = read('../index.html')

const text = markup.replace(/<!--[\s\S]*?-->/g, '').replace(/<svg[\s\S]*?<\/svg>/g, '')

test('both front doors render the one shared landing', () => {
  assert.match(wrapper, /from "\.\/landing\/landing\.html\?raw"/)
  assert.match(wrapper, /mountLanding\(/)
  assert.match(wrapper, /\.\/landing\/landing\.css/)
  assert.match(shell, /<!-- landing:markup -->/)
  assert.match(shell, /href="\/landing\.css"/)
  assert.match(shell, /from "\/landing\.js"/)
})

test('hero carries the approved copy and CTA', () => {
  assert.match(text, /AI is changing how work gets done\./)
  assert.match(text, /Understand what’s happening\./)
  assert.match(text, /Trovis uses AI to understand how work moves across your people, agents, and tools,\s+explain what’s happening, and surface what needs your attention\./)
})

test('"Join the waitlist" is the CTA in header, hero, and closing form; Sign in lives in the footer', () => {
  assert.match(text, /data-cta="header">Join the waitlist</)
  assert.match(text, /data-cta="hero">Join the waitlist</)
  assert.match(text, /data-submit>Join the waitlist</)
  assert.doesNotMatch(text, /founding list|founding seat|help shape Trovis/i)
  const header = text.slice(text.indexOf('<header'), text.indexOf('</header>'))
  assert.doesNotMatch(header, /Sign in/)
  const footer = text.slice(text.indexOf('<footer'))
  assert.match(footer, /data-signin>Sign in</)
})

test('preview leads with the explanation; records open only on demand', () => {
  const preview = text.slice(text.indexOf('id="tl-preview"'), text.indexOf('aria-labelledby="tl-problem-title"'))
  assert.match(preview, /Illustrative example/)
  const insightAt = preview.indexOf('The agent finished. The onboarding is still waiting.')
  assert.ok(insightAt > 0 && insightAt < preview.indexOf('class="tl-flow"'), 'explanation comes before the steps')
  assert.match(preview, /No approval is\s+recorded in the connected systems, so account activation remains pending\./)
  const steps = [...preview.matchAll(/class="tl-step"[^>]*aria-pressed="false"[^>]*data-step="(\d)"/g)].map((m) => m[1])
  assert.deepEqual(steps, ['1', '2', '3', '4', '5'])
  for (const n of steps) assert.match(preview, new RegExp(`data-panel="${n}" hidden`))
  for (const t of ['Person', 'AI agent', 'System']) assert.ok(preview.includes(`<span class="tl-type">${t}</span>`))
})

test('three different discoveries, each question → explanation → workflow → records', () => {
  const d = text.slice(text.indexOf('aria-labelledby="tl-discover-title"'), text.indexOf('aria-labelledby="tl-vision-title"'))
  assert.match(d, /Illustrative examples/)
  assert.match(d, /Ask about the work\.<br \/>Understand the bigger picture\./)
  const panels = d.split('role="tabpanel"').slice(1)
  assert.equal(panels.length, 3)
  for (const p of panels) {
    assert.match(p, /class="tl-answer-title"/)
    assert.match(p, /class="tl-(mini|change|loop)"/)
    assert.match(p, /class="tl-cites"/)
  }
  assert.match(d, /Stalled handoff/)
  assert.match(d, /Slower workflow/)
  assert.match(d, /Repeated corrections/)
  // Janet's approval is the hero's example, not the whole page's.
  assert.doesNotMatch(d, /Janet|Northwind/)
  // The old separate capability + Ask sections are merged into this one.
  assert.doesNotMatch(text, /Understand the work behind the activity/)
})

test('problem, vision, and closing sections carry the approved copy', () => {
  assert.match(text, /More work is happening\.<br \/>Less of it is in view\./)
  assert.match(text, /“What actually happened\?”/)
  assert.match(text, /Understand the work today\.<br \/>Make it work better tomorrow\./)
  assert.match(text, /Your workforce is evolving\.<br \/>Your visibility should too\./)
  assert.match(text, /Be among the first teams to see their work connected\. Join the waitlist for early-access\s+updates and opportunities to test Trovis with your team\./)
})

test('vision labels understanding as the focus and the rest as longer-term', () => {
  const stages = text.slice(text.indexOf('tl-stages'))
  assert.match(stages, /Initial focus<\/span>\s*<h3 class="tl-stage-title">Understand/)
  assert.match(stages, /Longer-term vision<\/span>\s*<h3 class="tl-stage-title">Improve/)
  assert.match(stages, /Longer-term vision<\/span>\s*<h3 class="tl-stage-title">Act/)
})

test('stays inside the messaging boundaries', () => {
  const low = text.toLowerCase()
  for (const claim of ['complete visibility', 'full visibility', 'every error', 'guarantee', 'autonomous', 'testimonial', 'trusted by']) {
    assert.ok(!low.includes(claim), `overclaims: ${claim}`)
  }
})

test('signup asks only for email first, optional questions after, with disclosure', () => {
  const join = text.slice(text.indexOf('data-form="join"'), text.indexOf('data-success'))
  assert.match(join, /name="email"[^>]*required/)
  assert.doesNotMatch(join, /name="company"|name="ai_use"/)
  assert.match(text, /What work is your team using AI for\?/)
  assert.match(text, /You’re on the list\. We’ll reach out with early-access updates\./)
  assert.match(text, /early-access updates and invitations to test,\s+and for product research/)
  assert.match(text, /name="website"/) // honeypot
})

test('success is shown only after the server confirms the save', () => {
  const submit = behaviour.slice(behaviour.indexOf("on(joinForm, 'submit'"), behaviour.indexOf('const finishFollowup'))
  const awaitAt = submit.indexOf('await api.joinWaitlist')
  const showAt = submit.indexOf('successBox.hidden = false')
  assert.ok(awaitAt > 0 && showAt > awaitAt, 'success must come after the awaited save')
  assert.ok(submit.indexOf("track(status === 'joined' ? 'signup_success'") > awaitAt)
  // Analytics never carry the email.
  for (const m of behaviour.matchAll(/track\(([^)]*)\)/g)) assert.doesNotMatch(m[1], /email/i)
})

test('SEO and social metadata describe the new landing', () => {
  for (const src of [index, shell]) {
    assert.match(src, /<title>Trovis — Understand how work gets done/)
    assert.match(src, /name="description"[\s\S]{0,40}content="Trovis uses AI to connect activity/)
    assert.match(src, /og:title" content="AI is changing how work gets done/)
    assert.match(src, /twitter:card" content="summary_large_image"/)
  }
})
