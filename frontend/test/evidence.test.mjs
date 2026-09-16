// Work Evidence in a manager's words: reported is not observed, observed is
// not verified, a name is not a vendor, and missing stays missing.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import {
  EVIDENCE_SPAN_LIMIT,
  actionName,
  correlationLabel,
  costProvenance,
  externalStateHeadline,
  observations,
  provenanceLine,
  sourceOf,
  sources,
  truncationNote,
} from '../src/evidence.js'

const src = (f) => readFileSync(new URL(`../src/${f}`, import.meta.url), 'utf8')
const strip = (s) => s.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')

const T = '2026-09-16T10:06:00+00:00'
const rec = (over = {}) => ({
  id: 'x', item_id: 1, evidence_type: 'execution', observed_at: T, source_type: 'agent',
  source_connector_id: 'grok-bot', source_label: 'refunds-agent:main', correlation_method: 'explicit_key',
  event_id: null, span_id: null, trace_id: null, external_object_id: null, external_event_id: null,
  details: {}, ...over,
})

test('a source is the WORKER, with its connector as context — never the connector alone', () => {
  // Worker from source_label (the stored service:agent actor, ":main" dropped),
  // connector from the registry as the quiet line under it.
  const bot = sourceOf(rec())
  assert.equal(bot.name, 'refunds-agent')
  assert.equal(bot.hint, 'Grok Bot')
  assert.equal(bot.key, 'agent:grok-bot:refunds-agent:main')
  const grok = sourceOf(rec({ source_connector_id: 'grok' }))
  assert.equal(grok.hint, 'Grok (xAI SDK)')
  assert.notEqual(grok.key, bot.key, 'Grok ≠ Grok Bot even for the same worker')
  // A sub-agent keeps its full stored identity.
  assert.equal(sourceOf(rec({ source_label: 'refunds-agent:reviewer' })).name, 'refunds-agent:reviewer')
  // Generic OTEL: all Trovis knows is the worker's own name; say that.
  const otel = sourceOf(rec({ source_connector_id: 'custom-otel', source_label: 'claude-refund-helper:main' }))
  assert.equal(otel.name, 'claude-refund-helper')
  assert.equal(otel.hint, 'OpenTelemetry')
  assert.doesNotMatch(otel.name + otel.hint, /Claude Agents/)
  // No worker label → the connector's name, and nothing invented.
  const nameless = sourceOf(rec({ source_label: '' }))
  assert.equal(nameless.name, 'Grok Bot')
  assert.equal(nameless.hint, null)
  // A person by resolved name; the product by its own name.
  assert.equal(sourceOf(rec({ source_type: 'human', source_connector_id: null, source_label: 'Ada Lovelace' })).name, 'Ada Lovelace')
  assert.equal(sourceOf(rec({ source_type: 'system', source_connector_id: 'stripe', source_label: 'Stripe' })).name, 'Stripe')
  assert.equal(sourceOf(rec({ source_type: 'system', source_connector_id: null, source_label: 'Trovis' })).name, 'Trovis')
  // No recorded connector: the worker is still named, its source is not guessed.
  const legacy = sourceOf(rec({ source_connector_id: null, source_label: 'claude-refund-helper:main' }))
  assert.equal(legacy.name, 'claude-refund-helper')
  assert.equal(legacy.hint, 'source not recorded')
  assert.equal(sourceOf(rec({ source_connector_id: null, source_label: '' })).name, 'Source not recorded')
})

test('two workers on one connector are two sources; one worker is one source', () => {
  const ev = [
    rec({ id: 'e1', source_connector_id: 'grok', source_label: 'refund-agent:main', details: { span_count: 2 } }),
    rec({ id: 'a1', evidence_type: 'action_reported', source_connector_id: 'grok', source_label: 'refund-agent:main', details: { tool: 'refund', errored: false } }),
    rec({ id: 'a2', evidence_type: 'action_reported', source_connector_id: 'grok', source_label: 'refund-agent:main', details: { tool: 'lookup', errored: false } }),
    rec({ id: 'a3', evidence_type: 'action_reported', source_connector_id: 'grok', source_label: 'refund-agent:main', details: { tool: 'notify', errored: true } }),
    rec({ id: 'c1', evidence_type: 'completion', source_connector_id: 'grok', source_label: 'refund-agent:main', details: { reason: 'completed_by_agent' } }),
    rec({ id: 'e2', source_connector_id: 'grok', source_label: 'support-agent:main', details: { span_count: 1 } }),
    rec({ id: 'a4', evidence_type: 'action_reported', source_connector_id: 'grok', source_label: 'support-agent:main', details: { tool: 'reply', errored: false } }),
    rec({ id: 'e3', source_connector_id: 'claude', source_label: 'triage-agent:main', details: { span_count: 1 } }),
    rec({ id: 'e4', source_connector_id: 'claude', source_label: 'drafting-agent:main', details: { span_count: 1 } }),
    rec({ id: 'e5', source_connector_id: 'grok-bot', source_label: 'refund-agent:main', details: { span_count: 1 } }),
    rec({ id: 'e6', source_connector_id: 'custom-otel', source_label: 'svc-a:main', details: { span_count: 1 } }),
    rec({ id: 'e7', source_connector_id: 'custom-otel', source_label: 'svc-b:main', details: { span_count: 1 } }),
    rec({ id: 'e8', source_connector_id: null, source_label: '', evidence_type: 'handoff', details: { event: 'handoff_initiated' } }),
    rec({ id: 'h1', evidence_type: 'handoff', source_type: 'human', source_connector_id: null, source_label: 'Ada Lovelace', details: { event: 'handoff_completed' } }),
    rec({ id: 'x1', evidence_type: 'external_state', source_type: 'system', source_connector_id: 'stripe', source_label: 'Stripe', details: { provider_event_type: 'refund.updated' } }),
  ]
  const s = sources(ev)
  const byName = Object.fromEntries(s.map((x) => [x.name + '|' + (x.hint || ''), x]))
  // 1. two Grok workers → two rows, with the counts on the right worker
  assert.deepEqual(byName['refund-agent|Grok (xAI SDK)'].lines, ['Execution observed', '3 actions reported · 1 with errors', 'Record closed'])
  assert.deepEqual(byName['support-agent|Grok (xAI SDK)'].lines, ['Execution observed', '1 action reported'])
  assert.ok(!s.some((x) => x.name === 'Grok (xAI SDK)'), 'no combined connector row')
  // 2. two Claude workers stay distinct
  assert.ok(byName['triage-agent|Claude Agents'] && byName['drafting-agent|Claude Agents'])
  // 3/4. one worker's execution + actions + completion is ONE row (asserted above)
  assert.equal(s.filter((x) => x.name === 'refund-agent' && x.hint === 'Grok (xAI SDK)').length, 1)
  // 5. Grok vs Grok Bot: the same worker over two connectors is two sources
  assert.ok(byName['refund-agent|Grok Bot'])
  // 6. two custom OTEL services stay distinct and generic
  assert.ok(byName['svc-a|OpenTelemetry'] && byName['svc-b|OpenTelemetry'])
  // 7. no worker identity → honest fallback, nothing invented
  assert.ok(s.some((x) => x.name === 'Source not recorded' && x.kind === 'unknown'))
  // 8. human and system grouping unchanged
  assert.deepEqual(byName['Ada Lovelace|'].lines, ['1 decision recorded'])
  assert.deepEqual(byName['Stripe|'].lines, ['1 state observation'])
})

test('reported vs observed vs recorded — and never verified', () => {
  assert.equal(provenanceLine(rec({ evidence_type: 'action_reported', details: { tool: 'refund_customer', errored: false } })), 'Reported by refunds-agent · Grok Bot')
  assert.equal(provenanceLine(rec({ evidence_type: 'action_reported', details: { tool: 'refund_customer', errored: true } })), 'Reported error by refunds-agent · Grok Bot')
  assert.equal(provenanceLine(rec()), 'Observed from refunds-agent · Grok Bot')
  // The worker is named, not just the connector; without a label, the connector.
  assert.equal(provenanceLine(rec({ evidence_type: 'action_reported', source_label: '', details: { tool: 't' } })), 'Reported by Grok Bot')
  assert.equal(provenanceLine(rec({ evidence_type: 'external_state', source_type: 'system', source_connector_id: 'stripe' })), 'Observed from Stripe')
  assert.equal(provenanceLine(rec({ evidence_type: 'handoff', source_type: 'human', source_connector_id: null, source_label: 'Ada Lovelace' })), 'Recorded by Ada Lovelace')
  assert.equal(provenanceLine(rec({ evidence_type: 'completion', source_type: 'system', source_connector_id: null })), 'Recorded by Trovis')
  assert.equal(provenanceLine(rec({ evidence_type: 'completion' })), 'Reported by refunds-agent · Grok Bot')
  // A worker whose connector was never recorded is named without one.
  assert.equal(provenanceLine(rec({ evidence_type: 'action_reported', source_connector_id: null, details: { tool: 't' } })), 'Reported by refunds-agent')
  assert.equal(provenanceLine(rec({ evidence_type: 'action_reported', source_connector_id: null, source_label: '' })), 'Source not recorded')
  for (const f of ['evidence.js', 'JobDetail.jsx']) {
    assert.doesNotMatch(strip(src(f)), /Verified by|verified by/i, `${f} never says verified by`)
  }
})

test('an action is named from its tool, readable but not translated', () => {
  assert.equal(actionName('refund_customer'), 'Refund customer')
  assert.equal(actionName('mcp__stripe__create_refund'), 'Create refund')
  assert.equal(actionName('shopify.orders.cancel'), 'Cancel')
  assert.equal(actionName('lookup-order'), 'Lookup order')
  assert.equal(actionName(''), 'Action')
  const o = observations([rec({ evidence_type: 'action_reported', details: { tool: 'refund_customer', errored: false, proves: 'reported' } })])
  assert.equal(o[0].title, 'Refund customer')
  assert.equal(o[0].provenance, 'Reported by refunds-agent · Grok Bot', 'the worker is named, the connector is context')
  assert.doesNotMatch(`${o[0].title} ${o[0].provenance} ${o[0].note}`, /complete|succeed|success/i)
})

test('an errored action reports the error, not a success, and only the message it gave', () => {
  const o = observations([rec({
    evidence_type: 'action_reported', span_id: 'abc', trace_id: 'tr1',
    details: { tool: 'refund_customer', errored: true, error: 'Card declined' },
  })])
  assert.equal(o[0].errored, true)
  assert.equal(o[0].provenance, 'Reported error by refunds-agent · Grok Bot')
  assert.equal(o[0].note, 'Card declined')
  const silent = observations([rec({ evidence_type: 'action_reported', details: { tool: 't', errored: true, error: null } })])
  assert.equal(silent[0].note, null)
})

test('external state is what the provider event is about — or plainly "State observed"', () => {
  assert.equal(externalStateHeadline(rec({ details: { provider_event_type: 'payment_intent.succeeded' } })), 'Payment state observed')
  assert.equal(externalStateHeadline(rec({ details: { provider_event_type: 'refund.updated' } })), 'Refund state observed')
  assert.equal(externalStateHeadline(rec({ details: { provider_event_type: 'fulfillments/create' } })), 'Fulfillment state observed')
  assert.equal(externalStateHeadline(rec({ details: { provider_event_type: 'charge.dispute.created' } })), 'Dispute state observed')
  assert.equal(externalStateHeadline(rec({ details: { provider_event_type: 'something.odd' } })), 'State observed')
  assert.equal(externalStateHeadline(rec({ details: {} })), 'State observed')
  const o = observations([rec({
    evidence_type: 'external_state', source_type: 'system', source_connector_id: 'stripe', source_label: 'Stripe',
    external_object_id: 'pi_4471', external_event_id: 'evt_ok_2',
    details: { provider_event_type: 'payment_intent.succeeded', effect: 'clear', reason: null },
  })])
  assert.equal(o[0].provenance, 'Observed from Stripe')
  assert.doesNotMatch(o[0].title, /succeeded|complete/i, 'the headline does not upgrade to an outcome verdict')
  const d = Object.fromEntries(o[0].details)
  assert.equal(d['External object'], 'pi_4471')
  assert.equal(d['Provider event id'], 'evt_ok_2')
  assert.equal(d['Provider event'], 'payment_intent.succeeded')
})

test('completion says the record closed, not that the outcome succeeded', () => {
  const o = observations([rec({ evidence_type: 'completion', details: { reason: 'completed_by_agent', proves: 'recorded_close' } })])
  assert.equal(o[0].title, 'Work record closed')
  assert.match(o[0].note, /does not by itself show the outcome/)
  assert.equal(o[0].provenance, 'Reported by refunds-agent · Grok Bot')
  const ab = observations([rec({ evidence_type: 'completion', source_type: 'system', source_connector_id: null, details: { reason: 'abandoned' } })])
  assert.equal(ab[0].title, 'Work record abandoned')
  assert.equal(ab[0].provenance, 'Recorded by Trovis')
})

test('execution and cost roll into sources; handoffs are counted, not repeated', () => {
  const ev = [
    rec({ details: { span_count: 12, last_observed_at: '2026-09-16T10:06:00+00:00' }, observed_at: '2026-09-16T10:00:00+00:00' }),
    rec({ id: 'a1', evidence_type: 'action_reported', details: { tool: 'refund_customer', errored: false } }),
    rec({ id: 'a2', evidence_type: 'action_reported', details: { tool: 'lookup', errored: true } }),
    rec({ id: 'c', evidence_type: 'cost', details: { amount_usd: 0.42, basis: 'reported' } }),
    rec({ id: 's1', evidence_type: 'external_state', source_type: 'system', source_connector_id: 'stripe', observed_at: '2026-09-16T10:07:00+00:00', details: { provider_event_type: 'payment_intent.processing', effect: 'wait' } }),
    rec({ id: 'h', evidence_type: 'handoff', source_type: 'human', source_connector_id: null, source_label: 'Ada Lovelace', observed_at: '2026-09-16T10:08:00+00:00', details: { event: 'handoff_completed' } }),
  ]
  const s = sources(ev)
  assert.deepEqual(s.map((x) => x.name), ['Ada Lovelace', 'Stripe', 'refunds-agent'], 'most recently heard first')
  const bot = s.find((x) => x.name === 'refunds-agent')
  assert.equal(bot.hint, 'Grok Bot')
  assert.deepEqual(bot.lines, ['Execution observed', '2 actions reported · 1 with errors'])
  assert.equal(bot.lastAt, '2026-09-16T10:06:00+00:00', 'last observed comes from the execution roll-up')
  assert.deepEqual(s.find((x) => x.name === 'Stripe').lines, ['1 state observation'])
  assert.deepEqual(s.find((x) => x.name === 'Ada Lovelace').lines, ['1 decision recorded'])
  // Observations carry only actions, states and closures — no execution
  // spam, no repeated handoffs, no span count as a headline.
  assert.deepEqual(observations(ev).map((o) => o.kind), ['action_reported', 'action_reported', 'external_state'])
  assert.equal(costProvenance(ev), 'Cost as reported by the agent SDK')
  assert.equal(costProvenance([]), null)
  assert.equal(costProvenance([rec({ evidence_type: 'cost', details: { basis: 'estimated' } })]), 'Cost estimated from observed model usage')
})

test('correlation is translated, and lives only in Details', () => {
  assert.equal(correlationLabel('explicit_key'), 'Directly linked to this run')
  assert.equal(correlationLabel('time_adjacency'), 'Associated with this run by timing')
  assert.equal(correlationLabel('origin'), 'Started this run')
  assert.equal(correlationLabel('direct'), 'Recorded directly on this run')
  assert.equal(correlationLabel(null), 'Link not recorded')
  const page = strip(src('JobDetail.jsx'))
  assert.doesNotMatch(page, /time_adjacency|explicit_key|loop_link/, 'no internal vocabulary reaches the page')
  // Ids appear only inside the details disclosure.
  const section = page.slice(page.indexOf('function EvidenceSection'), page.indexOf('function EvidenceDetail'))
  assert.match(section, /<details className="jobd-ev-details">/)
  assert.doesNotMatch(section.slice(0, section.indexOf('<details')), /span_id|trace_id|external_event_id/)
})

test('truncation is stated in the backend\'s own terms, quietly', () => {
  assert.equal(truncationNote({ spans_truncated: false }), null)
  assert.equal(truncationNote(null), null)
  assert.equal(truncationNote({ spans_truncated: true }), `Showing evidence from this run's first ${EVIDENCE_SPAN_LIMIT.toLocaleString()} observations.`)
  assert.equal(EVIDENCE_SPAN_LIMIT, 2000)
  // Mirrors work_evidence's bound.
  const backend = readFileSync(new URL('../../database.py', import.meta.url), 'utf8')
  assert.match(backend, /_WORK_EVIDENCE_SPAN_LIMIT = 2000/)
})

test('no recommendations, no model call, and the API is the existing one', () => {
  // The evidence helper knows nothing of coverage. The page reads coverage
  // for its Visibility section (coverage.js, jobDetailCoverage.test.mjs),
  // but the word "coverage" is an API name there, never rendered text — the
  // mounted test checks the page's text.
  assert.doesNotMatch(strip(src('evidence.js')), /coverage/i, 'evidence.js')
  for (const f of ['evidence.js', 'JobDetail.jsx']) {
    const code = strip(src(f))
    // (Skeleton widths like '70%' are layout, not a coverage figure; the
    // mounted test checks the rendered text for percentages.)
    assert.doesNotMatch(code, /Connect Stripe|Connect Shopify|recommend|Verified/i, f)
    assert.doesNotMatch(code, /anthropic|askFleet|summariz|api\s*\.ask/i, f)
  }
  const page = strip(src('JobDetail.jsx'))
  assert.match(page, /api\s*\.getWorkItemEvidence\(item\.id, \{ signal \}\)/)
  assert.match(page, /if \(!isPage\) return undefined\s*\n\s*setEvidence\(null\)\s*\n\s*setEvidenceErr\(null\)/, 'the panel (Home desk) never fetches evidence; the page resets before each load')
  assert.doesNotMatch(src('HomeView.jsx') + src('WorkTab.jsx'), /getWorkItemEvidence/, 'Work home never fetches evidence')
})
