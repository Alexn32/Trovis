// The public waitlist landing: one shared page (src/landing/) behind both
// front doors, carrying the approved copy and staying inside the messaging
// boundaries — understanding first, later stages labelled as vision, the
// preview and Ask examples labelled as illustrative.
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
  assert.match(text, /Trovis uses AI to connect activity across your people, agents, and tools into a\s+living picture of how your business works\./)
  assert.match(text, /See how work moves\. Understand where it gets stuck\. Know what needs your attention\./)
  assert.match(text, /Get early-access updates and help shape Trovis\./)
})

test('"Join the waitlist" is the CTA in header, hero, and closing form', () => {
  assert.match(text, /data-cta="header">Join the waitlist</)
  assert.match(text, /data-cta="hero">Join the waitlist</)
  assert.match(text, /data-submit>Join the waitlist</)
  assert.doesNotMatch(text, /founding list|founding seat/i)
})

test('preview is the onboarding example, labelled illustrative, with the Trovis explanation', () => {
  for (const s of ['Marcus', 'Onboarding agent', '#cs-approvals', 'Janet', 'Account activation']) {
    assert.ok(text.includes(s), `missing step ${s}`)
  }
  assert.match(text, /Illustrative product preview/)
  assert.match(text, /The agent finished\. The onboarding is still waiting\./)
  assert.match(text, /No approval is\s+recorded in the connected systems, so account activation remains pending\./)
  // Every step is an inspectable tab with its own evidence panel.
  const tabs = [...text.matchAll(/role="tab" id="tl-step-(\d)" aria-controls="tl-panel-\1"/g)]
  assert.equal(tabs.length, 5)
  for (const [, n] of tabs) assert.match(text, new RegExp(`role="tabpanel" id="tl-panel-${n}"`))
  // Participant types are named in text, not colour alone.
  for (const t of ['Person', 'AI agent', 'System']) assert.ok(text.includes(`<span class="tl-type">${t}</span>`))
})

test('problem, value, ask, vision, and closing sections carry the approved headlines', () => {
  assert.match(text, /More work is happening\.<br \/>Less of it is in view\./)
  assert.match(text, /“What actually happened\?”/)
  assert.match(text, /“I thought that was being handled\.”/)
  assert.match(text, /“Where do I need to step in\?”/)
  assert.match(text, /Understand the work behind the activity\./)
  for (const h of ['Follow the work', 'Know what needs attention', 'Discover how your business operates']) {
    assert.ok(text.includes(h), `missing capability ${h}`)
  }
  assert.match(text, /Ask about the work\.<br \/>Understand the bigger picture\./)
  assert.match(text, /The delay is in the review handoff\./)
  assert.match(text, /Product concept · sample records/)
  assert.match(text, /Understand the work today\.<br \/>Make it work better tomorrow\./)
  assert.match(text, /Your workforce is evolving\.<br \/>Your visibility should too\./)
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
  assert.match(text, /early-access updates and for product research/)
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
