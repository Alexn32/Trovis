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

test('sources are named from the registry, never from a service name', () => {
  assert.equal(sourceOf(rec()).name, 'Grok Bot')
  assert.equal(sourceOf(rec({ source_connector_id: 'grok' })).name, 'Grok (xAI SDK)')
  assert.notEqual(sourceOf(rec({ source_connector_id: 'grok' })).key, sourceOf(rec()).key, 'Grok ≠ Grok Bot')
  // Generic OTEL: all Trovis knows is the service; say that, with the hint.
  const otel = sourceOf(rec({ source_connector_id: 'custom-otel', source_label: 'claude-refund-helper:main' }))
  assert.equal(otel.name, 'claude-refund-helper')
  assert.equal(otel.hint, 'OpenTelemetry')
  assert.doesNotMatch(otel.name, /Claude Agents/)
  // A person by resolved name; the product by its own name.
  assert.equal(sourceOf(rec({ source_type: 'human', source_connector_id: null, source_label: 'Ada Lovelace' })).name, 'Ada Lovelace')
  assert.equal(sourceOf(rec({ source_type: 'system', source_connector_id: 'stripe', source_label: 'Stripe' })).name, 'Stripe')
  assert.equal(sourceOf(rec({ source_type: 'system', source_connector_id: null, source_label: 'Trovis' })).name, 'Trovis')
  // No recorded source stays unknown — not guessed from the actor string.
  const legacy = sourceOf(rec({ source_connector_id: null, source_label: 'claude-refund-helper:main' }))
  assert.equal(legacy.kind, 'unknown')
  assert.equal(legacy.hint, 'source not recorded')
})

test('reported vs observed vs recorded — and never verified', () => {
  assert.equal(provenanceLine(rec({ evidence_type: 'action_reported', details: { tool: 'refund_customer', errored: false } })), 'Reported by Grok Bot')
  assert.equal(provenanceLine(rec({ evidence_type: 'action_reported', details: { tool: 'refund_customer', errored: true } })), 'Reported error by Grok Bot')
  assert.equal(provenanceLine(rec()), 'Observed from Grok Bot')
  assert.equal(provenanceLine(rec({ evidence_type: 'external_state', source_type: 'system', source_connector_id: 'stripe' })), 'Observed from Stripe')
  assert.equal(provenanceLine(rec({ evidence_type: 'handoff', source_type: 'human', source_connector_id: null, source_label: 'Ada Lovelace' })), 'Recorded by Ada Lovelace')
  assert.equal(provenanceLine(rec({ evidence_type: 'completion', source_type: 'system', source_connector_id: null })), 'Recorded by Trovis')
  assert.equal(provenanceLine(rec({ evidence_type: 'completion' })), 'Reported by Grok Bot')
  assert.equal(provenanceLine(rec({ evidence_type: 'action_reported', source_connector_id: null })), 'Source not recorded')
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
  assert.doesNotMatch(`${o[0].title} ${o[0].provenance} ${o[0].note}`, /complete|succeed|success/i)
})

test('an errored action reports the error, not a success, and only the message it gave', () => {
  const o = observations([rec({
    evidence_type: 'action_reported', span_id: 'abc', trace_id: 'tr1',
    details: { tool: 'refund_customer', errored: true, error: 'Card declined' },
  })])
  assert.equal(o[0].errored, true)
  assert.equal(o[0].provenance, 'Reported error by Grok Bot')
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
  assert.deepEqual(s.map((x) => x.name), ['Ada Lovelace', 'Stripe', 'Grok Bot'], 'most recently heard first')
  const bot = s.find((x) => x.name === 'Grok Bot')
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

test('no coverage, no recommendations, no model call, and the API is the existing one', () => {
  for (const f of ['evidence.js', 'JobDetail.jsx']) {
    const code = strip(src(f))
    // (Skeleton widths like '70%' are layout, not a coverage figure; the
    // mounted test checks the rendered text for percentages.)
    assert.doesNotMatch(code, /coverage|Connect Stripe|Connect Shopify|recommend|Verified/i, f)
    assert.doesNotMatch(code, /anthropic|askFleet|summariz|api\s*\.ask/i, f)
  }
  const page = strip(src('JobDetail.jsx'))
  assert.match(page, /api\s*\.getWorkItemEvidence\(item\.id, \{ signal \}\)/)
  assert.match(page, /if \(!isPage\) return undefined\s*\n\s*setEvidenceErr\(null\)/, 'the panel (Home desk) never fetches evidence')
  assert.doesNotMatch(src('HomeView.jsx') + src('WorkTab.jsx'), /getWorkItemEvidence/, 'Work home never fetches evidence')
})
