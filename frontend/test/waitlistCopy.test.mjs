// The public waitlist landing: one shared page (src/landing/) behind both
// front doors. It has to make the product concrete (what connects, what you
// can inspect inside a run, where the work goes next), keep its sample data
// consistent, label it as sample data, and stay inside the messaging
// boundaries.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const read = (p) => readFileSync(new URL(p, import.meta.url), 'utf8')
const markup = read('../src/landing/landing.html')
const behaviour = read('../src/landing/landing.js')
const wrapper = read('../src/TrovisLanding.jsx')
const shell = read('../../static/waitlist.html')
const index = read('../index.html')
const registry = JSON.parse(read('../src/connectors.registry.json'))

const text = markup.replace(/<!--[\s\S]*?-->/g, '').replace(/<svg[\s\S]*?<\/svg>/g, '')
const section = (id, nextId) => text.slice(text.indexOf(`aria-labelledby="${id}"`), nextId ? text.indexOf(`aria-labelledby="${nextId}"`) : undefined)

test('both front doors render the one shared landing', () => {
  assert.match(wrapper, /from "\.\/landing\/landing\.html\?raw"/)
  assert.match(wrapper, /mountLanding\(/)
  assert.match(shell, /<!-- landing:markup -->/)
  assert.match(shell, /href="\/landing\.css"/)
  assert.match(shell, /from "\/landing\.js"/)
})

test('hero: approved headline, copy, benefit line, and an email signup', () => {
  const hero = section('tl-hero-title', 'tl-problem-title')
  assert.match(hero, /AI is changing how work gets done\./)
  assert.match(hero, /Understand what’s happening\./)
  assert.match(hero, /Connect your AI agents and workflows to Trovis\. See the steps they take, the tools they use,\s+and how work passes between agents, people, and systems\. Get AI-powered explanations of what\s+happened and what needs your attention\./)
  assert.match(hero, /Inside the agent\. Across the workflow\. Throughout your business\./)
  assert.match(hero, /data-signup="hero"/)
})

test('"Join the waitlist" everywhere; signup in hero and closing; Sign in only in the footer', () => {
  assert.match(text, /data-cta="header"[^>]*>Join the waitlist</)
  assert.equal([...text.matchAll(/data-submit>Join the waitlist</g)].length, 2)
  assert.deepEqual([...text.matchAll(/data-signup="(\w+)"/g)].map((m) => m[1]), ['hero', 'join'])
  assert.doesNotMatch(text.slice(text.indexOf('<header'), text.indexOf('</header>')), /Sign in/)
  assert.match(text.slice(text.indexOf('<footer')), /data-signin>Sign in</)
  assert.match(text, /Your AI is doing real work\.<br \/>See how it all connects\./)
  assert.match(text, /Join the waitlist for early access to Trovis and help shape visibility across your agents,\s+people, and workflows\./)
})

test('preview is labelled and walks workflows → run → explanation', () => {
  const pv = text.slice(text.indexOf('id="tl-preview"'), text.indexOf('</figure>'))
  assert.match(pv, /Interactive product concept · Sample data/)
  for (const w of ['Customer support', 'Customer onboarding', 'Weekly reporting']) assert.ok(pv.includes(w), w)
  assert.match(pv, /data-wf="support" aria-pressed="true"/)
  assert.match(pv, /Handle return request · Order #1042/)
  for (const step of [
    'Request received from customer inbox', 'Support agent retrieved the order', 'Support agent checked the return policy',
    'Support agent drafted a response', 'Email tool attempted delivery', 'Delivery failed', 'Retry failed',
    'Case routed to the support team for review',
  ]) assert.ok(pv.includes(`<span class="tl-ev-label">${step}</span>`), step)
  for (const k of ['Agent action', 'Tool or system event', 'Human handoff']) assert.ok(pv.includes(k), k)
  assert.match(pv, /The response was prepared, but the customer never received it\./)
  assert.match(pv, /Both delivery attempts failed because the email connection had expired\./)
  for (const f of ['What failed', 'Email delivery', 'What it affected', 'Customer response', 'Where the work went next', 'Support review queue', 'Supporting evidence']) {
    assert.ok(pv.includes(f), f)
  }
  // The default view explains itself without a click: support's run,
  // explanation and the Retry failed details are all visible on load.
  assert.match(pv, /data-run="support">/)
  assert.match(pv, /data-expl="support" aria-label/)
  assert.match(pv, /data-detail="support-7">[\s\S]*?Authentication expired[\s\S]*?Attempts<\/dt><dd>2<[\s\S]*?Support review queue/)
  assert.doesNotMatch(pv, /reasoning:|thought process|the agent thought/i)
})

test('sample evidence is consistent: every step has one detail, every citation a step', () => {
  const steps = [...text.matchAll(/data-ev="([\w-]+)"/g)].map((m) => m[1])
  const details = [...text.matchAll(/data-detail="([\w-]+)"/g)].map((m) => m[1])
  assert.deepEqual(details, steps)
  for (const run of ['support', 'onboarding', 'reporting']) {
    const runSteps = steps.filter((s) => s.startsWith(`${run}-`))
    assert.ok(runSteps.length >= 5, run)
    // Exactly one step per run is open by default, with its detail shown.
    const open = [...text.matchAll(new RegExp(`data-ev="(${run}-\\d+)" aria-pressed="true"`, 'g'))].map((m) => m[1])
    assert.equal(open.length, 1, run)
    const shown = [...text.matchAll(new RegExp(`data-detail="(${run}-\\d+)">`, 'g'))].map((m) => m[1])
    assert.deepEqual(shown, open, run)
  }
  for (const m of text.matchAll(/data-goto="([\w-]+)"/g)) assert.ok(steps.includes(m[1]), `citation ${m[1]} has no step`)
})

test('How Trovis fits names only connection methods the registry marks available', () => {
  const fit = section('tl-fit-title', 'tl-levels-title')
  assert.match(fit, /Connect the work\.<br \/>See the whole journey\./)
  assert.match(fit, /Bring activity from your agents, workflows, and business tools into Trovis\./)
  assert.match(fit, /Follow recorded steps, tool calls, outcomes, and handoffs in one connected view\./)
  assert.match(fit, /Get explanations of what happened, where work stopped, and what needs your attention\./)
  const available = new Set(registry.filter((c) => c.availability === 'available').map((c) => c.name))
  const comingSoon = registry.filter((c) => c.availability !== 'available').map((c) => c.name)
  for (const name of ['OpenAI Agents SDK', 'OpenClaw', 'Stripe', 'HubSpot', 'Shopify']) {
    assert.ok(fit.includes(name), name)
    assert.ok(available.has(name), `${name} is not marked available`)
  }
  assert.ok(registry.some((c) => c.id === 'claude' && c.availability === 'available'))
  assert.ok(registry.some((c) => c.id === 'custom-otel' && c.availability === 'available'))
  for (const name of comingSoon) assert.ok(!fit.includes(name), `${name} is coming soon, not available`)
  assert.match(fit, /Not every platform is supported yet\./)
  assert.doesNotMatch(fit, /one-click|automatic(ally)? discover|any platform|all platforms|supports every/i)
  assert.doesNotMatch(fit, /<img/)
})

test('three levels of visibility, problem, pattern, and vision sections', () => {
  const levels = section('tl-levels-title', 'tl-pattern-title')
  assert.match(levels, /See inside the AI\.<br \/>Understand the work around it\./)
  for (const h of ['Across your AI work', 'Inside each run', 'Beyond the agent']) assert.ok(levels.includes(h), h)
  const problem = section('tl-problem-title', 'tl-fit-title')
  assert.match(problem, /More work is happening\.<br \/>Less of it is in view\./)
  assert.match(problem, /An agent completing its step doesn’t mean/)
  const pattern = section('tl-pattern-title', 'tl-vision-title')
  assert.match(pattern, /“Which AI work keeps coming back to people\?”/)
  assert.match(pattern, /Illustrative finding · Sample data/)
  assert.doesNotMatch(pattern, /because the agent|caused by/i)
  const stages = section('tl-vision-title', 'tl-join-title')
  assert.match(stages, /Initial focus<\/span>\s*<h3 class="tl-stage-title">Understand/)
  assert.match(stages, /Longer-term vision<\/span>\s*<h3 class="tl-stage-title">Improve/)
  assert.match(stages, /Longer-term vision<\/span>\s*<h3 class="tl-stage-title">Act/)
})

test('stays inside the messaging boundaries', () => {
  const low = text.toLowerCase()
  for (const claim of ['complete visibility', 'full visibility', 'every error', 'guarantee', 'autonomous', 'testimonial', 'trusted by', 'customers love']) {
    assert.ok(!low.includes(claim), `overclaims: ${claim}`)
  }
})

test('signup asks only for email first, optional questions after, with disclosure', () => {
  for (const where of ['hero', 'join']) {
    const block = text.slice(text.indexOf(`data-signup="${where}"`))
    const join = block.slice(0, block.indexOf('data-success'))
    assert.match(join, /name="email"[^>]*required/)
    assert.doesNotMatch(join, /name="company"|name="ai_use"/)
    assert.match(join, /name="website"/) // honeypot
    assert.match(join, /early-access updates and for product research/)
  }
  assert.match(text, /What work is your team using AI for\?/)
  assert.match(text, /You’re on the list\. We’ll reach out with early-access updates\./)
})

test('success is shown only after the server confirms the save', () => {
  const submit = behaviour.slice(behaviour.indexOf("on(joinForm, 'submit'"), behaviour.indexOf('if (!followForm) return'))
  const awaitAt = submit.indexOf('await api.joinWaitlist')
  const showAt = submit.indexOf('b.success.hidden = false')
  assert.ok(awaitAt > 0 && showAt > awaitAt, 'success must come after the awaited save')
  assert.ok(submit.indexOf("'signup_success'") > awaitAt)
  for (const m of behaviour.matchAll(/track\(([^)]*)\)/g)) assert.doesNotMatch(m[1], /email/i)
})

test('SEO and social metadata describe the landing', () => {
  for (const src of [index, shell]) {
    assert.match(src, /<title>Trovis — Understand how work gets done/)
    assert.match(src, /og:title" content="AI is changing how work gets done/)
    assert.match(src, /twitter:card" content="summary_large_image"/)
  }
})
