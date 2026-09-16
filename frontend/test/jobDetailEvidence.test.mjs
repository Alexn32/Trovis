// The Run page with evidence, mounted: the record first, evidence under it,
// and every failure mode honest.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { deferred, installDom, mount } from './mount.mjs'

installDom()

const React = await import('react')
const { api } = await import('../src/api.js')
const JobDetail = (await import('../src/JobDetail.jsx')).default

const NOW = Date.now()
const ago = (m) => new Date(NOW - m * 60000).toISOString()

const DETAIL = {
  id: 41, title: 'Refund order #4471', status: 'done', holder: { kind: 'agent', name: 'refunds-agent' },
  whats_next: null, updated_at: ago(3), awaiting_handoff_event_id: null, workflow_id: 7, workflow_name: 'Refunds',
  whats_happening: null, process: null,
  timeline: [
    { at: ago(30), text: 'Started', actor: { kind: 'agent', name: 'refunds-agent' } },
    { at: ago(20), text: 'Waiting on Ada Lovelace', actor: { kind: 'human', name: 'Ada Lovelace' } },
    { at: ago(10), text: 'Finished', actor: { kind: 'agent', name: 'refunds-agent' } },
  ],
  provenance: { source: 'telemetry', suggestion_id: null },
}
const RUNS = [
  { name: 'tool_call', agent: 'refunds-agent', service_name: 'refunds-agent', agent_id: 'main', at: ago(25), errored: false, duration_ms: 1400, cost_usd: 0.0042, tool: 'refund_customer', error: null },
]
const r = (over) => ({
  id: over.id, item_id: 41, evidence_type: over.evidence_type, observed_at: over.observed_at || ago(25),
  source_type: 'agent', source_connector_id: 'grok-bot', source_label: 'refunds-agent:main',
  correlation_method: 'explicit_key', event_id: null, span_id: null, trace_id: null,
  external_object_id: null, external_event_id: null, details: {}, ...over,
})
const EVIDENCE = {
  item_id: 41, generated_at: new Date(NOW).toISOString(), spans_truncated: false,
  evidence: [
    r({ id: 'exec:grok-bot', evidence_type: 'execution', observed_at: ago(30), details: { span_count: 3, error_count: 0, last_observed_at: ago(10), trace_ids: [], correlation_methods: ['explicit_key'] } }),
    r({ id: 'exec:grok', evidence_type: 'execution', source_connector_id: 'grok', source_label: 'pricing-agent:main', observed_at: ago(28), details: { span_count: 1, last_observed_at: ago(28), correlation_methods: ['explicit_key'] } }),
    r({ id: 'exec:otel', evidence_type: 'execution', source_connector_id: 'custom-otel', source_label: 'claude-refund-helper:main', observed_at: ago(27), details: { span_count: 2, last_observed_at: ago(27), correlation_methods: ['time_adjacency'] } }),
    r({ id: 'span:s2', evidence_type: 'action_reported', span_id: '0000000000000002', trace_id: '00000000000000000000000000000002', details: { tool: 'refund_customer', span_name: 'tool_call', errored: false, error: null, proves: 'reported', assignment: 'key_batch' } }),
    r({ id: 'span:s3', evidence_type: 'action_reported', observed_at: ago(24), span_id: '0000000000000003', details: { tool: 'lookup_order', span_name: 'tool_call', errored: true, error: 'Card declined', proves: 'reported' } }),
    r({ id: 'event:9', evidence_type: 'external_state', observed_at: ago(22), source_type: 'system', source_connector_id: 'stripe', source_label: 'Stripe', event_id: 9, external_object_id: 'pi_4471', external_event_id: 'evt_ok_2', details: { provider_event_type: 'payment_intent.succeeded', effect: 'clear', event: 'handoff_completed', provider_ids_recorded: true } }),
    r({ id: 'event:10', evidence_type: 'handoff', observed_at: ago(20), source_type: 'human', source_connector_id: null, source_label: 'Ada Lovelace', correlation_method: 'direct', event_id: 10, details: { event: 'handoff_completed' } }),
    r({ id: 'event:11', evidence_type: 'handoff', observed_at: ago(19), source_connector_id: null, correlation_method: null, event_id: 11, details: { event: 'handoff_initiated', direction: 'to_agent' } }),
    r({ id: 'event:12', evidence_type: 'completion', observed_at: ago(10), event_id: 12, span_id: '0000000000000009', details: { reason: 'completed_by_agent', proves: 'recorded_close' } }),
    r({ id: 'cost:grok-bot', evidence_type: 'cost', observed_at: ago(25), details: { amount_usd: 0.0042, basis: 'reported', span_count: 1, span_ids: ['0000000000000002'] } }),
  ],
}

function stub({ evidence = EVIDENCE, evidenceFail = false, runs = RUNS } = {}) {
  const calls = { item: [], evidence: [] }
  api.getWorkItem = async (id, opts = {}) => {
    calls.item.push([id, opts.include || null])
    return opts.include === 'runs' ? { ...DETAIL, runs } : DETAIL
  }
  api.getWorkItemEvidence = async (id) => {
    calls.evidence.push(id)
    if (evidenceFail) throw new Error('evidence down')
    return typeof evidence === 'function' ? evidence() : evidence
  }
  return calls
}

/** A row's text without its folded Details — what a reader sees by default. */
const visibleText = (row) =>
  [...row.children].filter((c) => c.tagName !== 'DETAILS').map((c) => c.textContent).join(' ')

const page = (over = {}) =>
  React.createElement(JobDetail, {
    variant: 'page', item: { id: 41 }, onClose: () => {}, onOpenAgent: () => {}, backLabel: '← Refunds', ...over,
  })

test('the page loads evidence once, for the viewed item only, and stays a Run page', async () => {
  const calls = stub()
  const m = await mount(page())
  await m.settle()
  assert.deepEqual(calls.evidence, [41])
  const text = m.text()
  // The record comes first: title, status, steps — then evidence.
  assert.ok(text.indexOf('Refund order #4471') < text.indexOf('Evidence'))
  assert.ok(text.indexOf('How this job ran') < text.indexOf('Evidence'))
  assert.match(text, /← Refunds/)
  m.unmount()
})

test('the record is usable while evidence is still loading', async () => {
  const gate = deferred()
  stub({ evidence: () => gate.promise })
  const m = await mount(page())
  await m.settle()
  assert.match(m.text(), /Refund order #4471/)
  assert.match(m.text(), /How this job ran/)
  assert.ok(m.$('.jobd-evidence .dash-skel'), 'evidence shows a skeleton, not an error')
  assert.doesNotMatch(m.text(), /No supporting evidence/)
  gate.resolve(EVIDENCE)
  await m.settle()
  assert.match(m.text(), /Reported by refunds-agent · Grok Bot/)
  assert.ok(!m.$('.jobd-evidence .dash-skel'), 'skeleton gone once evidence arrives')
  m.unmount()
})

test('an evidence failure is local, with Retry — never "no evidence", never a dead page', async () => {
  stub({ evidenceFail: true })
  const m = await mount(page())
  await m.settle()
  assert.match(m.text(), /Refund order #4471/)
  assert.match(m.text(), /How this job ran/)
  const sec = m.$('.jobd-evidence')
  assert.match(sec.textContent, /Evidence couldn.t be loaded\./)
  assert.doesNotMatch(sec.textContent, /No supporting evidence/)
  assert.ok(sec.querySelector('button'), 'Retry offered')
  m.unmount()
})

test('an empty evidence response is an honest sentence, not "nothing happened" or "not connected"', async () => {
  stub({ evidence: { ...EVIDENCE, evidence: [] } })
  const m = await mount(page())
  await m.settle()
  const sec = m.$('.jobd-evidence')
  assert.match(sec.textContent, /No supporting evidence is available for this run\./)
  assert.doesNotMatch(sec.textContent, /Nothing happened|Not connected|couldn’t be loaded/i)
  m.unmount()
})

test('sources: the worker first, its connector under it; Grok ≠ Grok Bot; generic OTEL stays generic', async () => {
  stub()
  const m = await mount(page())
  await m.settle()
  const rows = m.$$('.jobd-ev-source')
  const names = m.$$('.jobd-ev-source-name').map((e) => e.textContent)
  assert.ok(names.includes('refunds-agent'), 'the worker is the row, not its connector')
  assert.ok(names.includes('pricing-agent'))
  assert.ok(names.includes('Stripe'))
  assert.ok(names.includes('Ada Lovelace'), 'human source by resolved name')
  assert.ok(names.includes('claude-refund-helper'), 'generic OTEL shows the worker, nothing more')
  assert.ok(!names.includes('Grok Bot') && !names.includes('Grok (xAI SDK)'), 'no connector-only rows')
  const bot = rows.find((e) => e.querySelector('.jobd-ev-source-name').textContent === 'refunds-agent' && e.textContent.includes('Grok Bot'))
  assert.ok(bot, 'refunds-agent carries its connector as context')
  assert.match(bot.textContent, /Execution observed/)
  assert.match(bot.textContent, /2 actions reported · 1 with errors/)
  const grok = rows.find((e) => e.querySelector('.jobd-ev-source-name').textContent === 'pricing-agent')
  assert.match(grok.textContent, /Grok \(xAI SDK\)/)
  assert.notEqual(bot, grok)
  const otel = rows.find((e) => e.textContent.includes('claude-refund-helper'))
  assert.match(otel.textContent, /OpenTelemetry/)
  assert.doesNotMatch(otel.textContent, /Claude Agents/)
  // The unrecorded-source handoff names the worker and says the source is not recorded.
  const unknown = rows.find((e) => e.textContent.includes('source not recorded'))
  assert.ok(unknown, 'a source that was never recorded is named as such')
  m.unmount()
})

test('two workers on the same connector stay two source rows with their own counts', async () => {
  const two = { ...EVIDENCE, evidence: [
    r({ id: 'exec:a', evidence_type: 'execution', source_connector_id: 'grok', source_label: 'refund-agent:main', observed_at: ago(30), details: { span_count: 2, last_observed_at: ago(20) } }),
    r({ id: 'act:a1', evidence_type: 'action_reported', source_connector_id: 'grok', source_label: 'refund-agent:main', observed_at: ago(25), details: { tool: 'refund_customer', errored: false } }),
    r({ id: 'act:a2', evidence_type: 'action_reported', source_connector_id: 'grok', source_label: 'refund-agent:main', observed_at: ago(24), details: { tool: 'lookup_order', errored: false } }),
    r({ id: 'act:a3', evidence_type: 'action_reported', source_connector_id: 'grok', source_label: 'refund-agent:main', observed_at: ago(23), details: { tool: 'notify', errored: false } }),
    r({ id: 'exec:b', evidence_type: 'execution', source_connector_id: 'grok', source_label: 'support-agent:main', observed_at: ago(29), details: { span_count: 1, last_observed_at: ago(21) } }),
    r({ id: 'act:b1', evidence_type: 'action_reported', source_connector_id: 'grok', source_label: 'support-agent:main', observed_at: ago(22), details: { tool: 'reply', errored: false } }),
  ] }
  stub({ evidence: two })
  const m = await mount(page())
  await m.settle()
  const rows = m.$$('.jobd-ev-source')
  assert.equal(rows.length, 2)
  const refund = rows.find((e) => e.textContent.startsWith('refund-agent'))
  const support = rows.find((e) => e.textContent.startsWith('support-agent'))
  assert.match(refund.textContent, /Grok \(xAI SDK\)/)
  assert.match(refund.textContent, /3 actions reported/)
  assert.match(support.textContent, /Grok \(xAI SDK\)/)
  assert.match(support.textContent, /1 action reported/)
  // Observation rows name the worker that reported, not just the connector.
  const notify = m.$$('.jobd-ev-row').find((e) => e.textContent.includes('Notify'))
  assert.match(notify.textContent, /Reported by refund-agent · Grok \(xAI SDK\)/)
  const reply = m.$$('.jobd-ev-row').find((e) => e.textContent.includes('Reply'))
  assert.match(reply.textContent, /Reported by support-agent · Grok \(xAI SDK\)/)
  m.unmount()
})

test('actions read as reported; an error reports the error; nothing implies success', async () => {
  stub()
  const m = await mount(page())
  await m.settle()
  const rows = m.$$('.jobd-ev-row')
  const refund = rows.find((e) => e.textContent.includes('Refund customer'))
  assert.match(refund.textContent, /Reported by refunds-agent · Grok Bot/)
  assert.doesNotMatch(visibleText(refund), /complete|succeed|success|\bOK\b/)
  const lookup = rows.find((e) => e.textContent.includes('Lookup order'))
  assert.ok(lookup.classList.contains('is-errored'))
  assert.match(lookup.textContent, /Reported error by refunds-agent · Grok Bot/)
  assert.match(lookup.textContent, /Card declined/)
  m.unmount()
})

test('external state uses narrow "observed" language and feels different from a report', async () => {
  stub()
  const m = await mount(page())
  await m.settle()
  const ext = m.$('.jobd-ev-row.kind-external_state')
  assert.match(ext.textContent, /Payment state observed/)
  assert.match(ext.textContent, /Observed from Stripe/)
  // The visible row never upgrades the headline; the raw provider event
  // type ("payment_intent.succeeded") stays inside Details.
  assert.doesNotMatch(visibleText(ext), /Reported by|Verified|succeeded|Refund completed/)
  assert.doesNotMatch(m.text(), /Verified by/)
  m.unmount()
})

test('completion says the record closed, not that the outcome succeeded', async () => {
  stub()
  const m = await mount(page())
  await m.settle()
  const done = m.$('.jobd-ev-row.kind-completion')
  assert.match(done.textContent, /Work record closed/)
  assert.match(done.textContent, /does not by itself show the outcome/)
  assert.match(done.textContent, /Reported by refunds-agent · Grok Bot/)
  m.unmount()
})

test('technical ids live behind a closed Details disclosure, exact when shown', async () => {
  stub()
  const m = await mount(page())
  await m.settle()
  const visible = [...m.container.querySelectorAll('.jobd-ev-row')]
    .map((e) => [...e.children].filter((c) => c.tagName !== 'DETAILS').map((c) => c.textContent).join(' '))
    .join(' ')
  assert.doesNotMatch(visible, /0000000000000002|pi_4471|evt_ok_2|key_batch|explicit_key/)
  const details = m.$$('.jobd-ev-details')
  assert.ok(details.length > 0)
  assert.ok(details.every((d) => !d.hasAttribute('open')), 'closed by default')
  const ext = m.$('.jobd-ev-row.kind-external_state .jobd-ev-details')
  assert.match(ext.textContent, /pi_4471/)
  assert.match(ext.textContent, /evt_ok_2/)
  assert.match(ext.textContent, /payment_intent\.succeeded/)
  assert.match(ext.textContent, /Directly linked to this run/)
  const refund = m.$$('.jobd-ev-row').find((e) => e.textContent.includes('Refund customer')).querySelector('.jobd-ev-details')
  assert.match(refund.textContent, /0000000000000002/)
  assert.doesNotMatch(m.text(), /time_adjacency|explicit_key|loop_link/)
  m.unmount()
})

test('truncation is stated plainly and cost gets its provenance; a missing cost stays absent', async () => {
  stub({ evidence: { ...EVIDENCE, spans_truncated: true } })
  const m = await mount(page())
  await m.settle()
  assert.match(m.text(), /Showing evidence from this run's first 2,000 observations\./)
  assert.match(m.$('.jobd-status').textContent, /\$0\.0042/)
  assert.match(m.$('.jobd-status').textContent, /Cost as reported by the agent SDK/)
  m.unmount()
  // No priced run → no figure and no provenance line.
  stub({ runs: [{ ...RUNS[0], cost_usd: null }] })
  const m2 = await mount(page())
  await m2.settle()
  assert.doesNotMatch(m2.$('.jobd-status').textContent, /\$|Cost/)
  m2.unmount()
})

test('the page never speaks of coverage, verification or connecting a system', async () => {
  stub()
  const m = await mount(page())
  await m.settle()
  assert.doesNotMatch(m.text(), /coverage|\d+\s?%|Verified|Connect Stripe|Connect Shopify|recommend/i)
  m.unmount()
})

test('possession chain and navigation are unchanged: steps, passes, back link, decision block', async () => {
  let closed = 0
  stub()
  const m = await mount(page({ onClose: () => { closed += 1 } }))
  await m.settle()
  assert.match(m.text(), /Recent passes/)
  assert.match(m.text(), /Waiting on Ada Lovelace/)
  assert.match(m.text(), /Finished/)
  await m.click(m.$('.jobd-close'))
  assert.equal(closed, 1)
  m.unmount()
})

test('the Home desk panel does not fetch evidence', async () => {
  const calls = stub()
  const m = await mount(React.createElement(JobDetail, { variant: 'panel', item: { id: 41, title: 'x' }, onClose: () => {} }))
  await m.settle()
  assert.deepEqual(calls.evidence, [])
  assert.ok(!m.$('.jobd-evidence'))
  m.unmount()
})
