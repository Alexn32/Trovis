// Execution helpers: structure from the endpoint's parent ids only, words
// that never upgrade a report into an outcome, and unknown never shown as 0.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import {
  EXPAND_ALL_MAX_NODES, TYPE_LABELS, boundedNote, buildTree, chronologyRows, defaultExpanded,
  depthOf, diagnosticsNote, executionSummary, fmtTokens, inspectorSections, nodeCostLabel,
  nodeErrorLine, nodeMeta, nodeSubtitle, nodeTitle, rootGroups, subtreeErrors, technicalDetails,
  toolSystem, useChronologyFallback, workerDisplay,
} from '../src/execution.js'

const src = (f) => readFileSync(new URL(`../src/${f}`, import.meta.url), 'utf8')
const strip = (s) => s.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')

const T = '2026-09-18T10:03:14+00:00'
const later = (s) => new Date(Date.parse(T) + s * 1000).toISOString()
const prov = (over = {}) => ({
  record: 'span', span_id: 'aaaaaaaaaaaaaaaa', trace_id: 'a'.repeat(32), parent_span_id: null,
  parent_status: 'none', event_id: null, evidence_kind: 'execution', correlation: 'explicit_key',
  loop_link: 'key_batch', classification_basis: 'event_type', span_kind: 'internal', event_type: null, ...over,
})
const node = (over = {}) => ({
  id: 'span:x', type: 'other', parent_id: null, started_at: T, ended_at: later(1), duration_ms: 1000,
  label: 'x', worker: { label: 'returns-bot:main', service_name: 'returns-bot', agent_id: 'main' },
  connector: { id: 'grok-bot', method: 'mcp' }, status: 'unset', error: null, model: null, tool: null,
  usage: null, cost: null, event: null, provenance: prov(), ...over,
})

/** The run the brief sketches: one worker root, models and tools as siblings. */
function body(over = {}) {
  const root = node({ id: 'span:root', type: 'worker', label: 'agent_run', duration_ms: 12400, ended_at: later(12.4),
    provenance: prov({ span_id: 'r', event_type: 'agent_run' }) })
  const kid = (o) => node({ parent_id: 'span:root', provenance: prov({ parent_span_id: 'r', parent_status: 'attached' }), ...o })
  const nodes = [
    root,
    kid({ id: 'span:m1', type: 'model', label: 'model_call', started_at: later(0.1), duration_ms: 1800,
      model: { name: 'grok-4', provider: 'xai' }, usage: { input_tokens: 1000, output_tokens: 284, total_tokens: 1284, cache_creation_input_tokens: null, cache_read_input_tokens: null },
      cost: { known: true, source: 'estimated', amount_usd: 0.02 } }),
    kid({ id: 'span:t1', type: 'tool', label: 'tool_call', started_at: later(2), duration_ms: 382,
      tool: { name: 'mcp__shopify__lookup_order', identifier: 'mcp__shopify__lookup_order', display_name: 'lookup_order', mcp_server: 'shopify', call_id: 'c1', reported_success: true },
      provenance: prov({ parent_span_id: 'r', parent_status: 'attached', evidence_kind: 'action_reported' }) }),
    kid({ id: 'span:m2', type: 'model', label: 'model_call', started_at: later(3), duration_ms: 2100,
      model: { name: 'grok-4', provider: 'xai' }, usage: { input_tokens: 0, output_tokens: 0, total_tokens: 0, cache_creation_input_tokens: null, cache_read_input_tokens: null },
      cost: { known: true, source: 'covered', amount_usd: null } }),
    kid({ id: 'span:t2', type: 'tool', label: 'tool_call', started_at: later(5), duration_ms: 843, status: 'error', error: 'Card declined',
      tool: { name: 'stripe.refunds.create', identifier: 'stripe.refunds.create', display_name: 'stripe.refunds.create', mcp_server: null, call_id: 'c2', reported_success: false },
      provenance: prov({ parent_span_id: 'r', parent_status: 'attached', evidence_kind: 'action_reported' }) }),
    kid({ id: 'span:t3', type: 'tool', label: 'tool_call', started_at: later(6), duration_ms: 611,
      tool: { name: 'stripe.refunds.create', identifier: 'stripe.refunds.create', display_name: 'stripe.refunds.create', mcp_server: null, call_id: 'c3', reported_success: true },
      provenance: prov({ parent_span_id: 'r', parent_status: 'attached', evidence_kind: 'action_reported' }) }),
    node({ id: 'span:h', type: 'other', label: 'HTTP GET', parent_id: 'span:t3', started_at: later(6.1), duration_ms: 200,
      provenance: prov({ parent_span_id: 't3', parent_status: 'attached' }) }),
    kid({ id: 'span:done', type: 'worker', label: 'agent_run_complete', started_at: later(12), duration_ms: 5,
      usage: { input_tokens: 10, output_tokens: 2, total_tokens: 12, cache_creation_input_tokens: null, cache_read_input_tokens: null },
      cost: { known: true, source: 'reported', amount_usd: 0.03 }, provenance: prov({ parent_span_id: 'r', parent_status: 'attached', event_type: 'agent_run_complete' }) }),
    node({ id: 'event:9', type: 'completion', label: 'loop_closed', started_at: later(12.5), ended_at: null, duration_ms: null, status: 'recorded',
      worker: null, connector: null, event: { type: 'loop_closed', direction: null, actor: { type: 'agent', label: 'returns-bot:main' }, reason: 'completed_by_agent' },
      provenance: prov({ record: 'loop_event', span_id: null, trace_id: null, event_id: 9, evidence_kind: 'completion', correlation: 'direct', loop_link: null, span_kind: null }) }),
  ]
  return {
    item_id: 41, generated_at: T, trace_ids: ['a'.repeat(32)], nodes,
    roots: ['span:root', 'event:9'], chronology: nodes.map((n) => n.id),
    bounded: false, span_limit: 2000, spans_read: 8, events_read: 1, duplicate_spans_dropped: 0, cycles_broken: 0,
    summary: {}, ...over,
  }
}

// --- structure ------------------------------------------------------------------

test('36. buildTree uses parent_id and nothing else: children under their recorded parent, roots as the endpoint lists them', () => {
  const t = buildTree(body())
  assert.deepEqual(t.children.get('span:root'), ['span:m1', 'span:t1', 'span:m2', 'span:t2', 'span:t3', 'span:done'])
  assert.deepEqual(t.children.get('span:t3'), ['span:h'])
  assert.deepEqual(t.roots, ['span:root', 'event:9'])
  assert.equal(depthOf(t, 'span:h'), 2)
})

test('15/17. a root with parent_status outside_read_set stays a root — no parent is fabricated, no timing consulted', () => {
  const b = body()
  b.nodes.push(node({ id: 'span:orphan', parent_id: null, started_at: later(0.5),
    provenance: prov({ parent_span_id: 'ffffffffffffffff', parent_status: 'outside_read_set' }) }))
  b.roots.push('span:orphan')
  const t = buildTree(b)
  assert.ok(t.roots.includes('span:orphan'))
  assert.ok(!t.children.get('span:root')?.includes('span:orphan'), 'not slotted under the root it happened during')
})

test('a parent_id naming a node absent from the body is not honoured — the node becomes a root, nothing is invented', () => {
  const b = body({ nodes: [node({ id: 'span:a', parent_id: 'span:ghost' })], roots: [], chronology: ['span:a'] })
  const t = buildTree(b)
  assert.deepEqual(t.roots, ['span:a'])
})

test('16. rootGroups: span roots grouped by recorded trace id in first-seen order; Work record events last; one group → no heading needed', () => {
  const b = body()
  const t2 = 'b'.repeat(32)
  b.nodes.push(node({ id: 'span:r2', type: 'worker', label: 'agent_run', started_at: later(20), provenance: prov({ trace_id: t2, span_id: 'q', event_type: 'agent_run' }) }))
  b.roots.push('span:r2')
  b.trace_ids.push(t2)
  const groups = rootGroups(b)
  assert.deepEqual(groups.map((g) => [g.label, g.roots]), [
    ['Trace 1', ['span:root']], ['Trace 2', ['span:r2']], ['Work record events', ['event:9']],
  ])
  assert.equal(groups[0].traceId, 'a'.repeat(32))
  assert.equal(groups[2].traceId, null)
  const one = rootGroups(body({ nodes: body().nodes.filter((n) => n.id !== 'event:9'), roots: ['span:root'] }))
  assert.equal(one.length, 1)
  assert.doesNotMatch(JSON.stringify(one), /Run 1/, 'no run identity is invented from a trace')
})

test('20/21. THE fallback rule: chronology only when no node is attached at all (and there are at least two)', () => {
  assert.equal(useChronologyFallback(body()), false, 'a tree with attached children is a tree')
  const flat = body()
  for (const n of flat.nodes) { n.parent_id = null; n.provenance.parent_status = 'none' }
  flat.roots = flat.nodes.map((n) => n.id)
  assert.equal(useChronologyFallback(flat), true, 'all roots, nothing attached → chronology')
  // Several roots with ONE attached child anywhere keep the tree: many roots are still structure.
  const multi = body()
  for (const n of multi.nodes) if (n.id !== 'span:h') { n.parent_id = null; n.provenance.parent_status = 'none' }
  assert.equal(useChronologyFallback(multi), false)
  assert.equal(useChronologyFallback(body({ nodes: [node()], roots: ['span:x'], chronology: ['span:x'] })), false, 'one node is a one-node tree')
  assert.equal(useChronologyFallback({ nodes: [] }), false)
})

test('20. chronologyRows follows the endpoint chronology, one row per node, and carries no parent', () => {
  const b = body()
  b.chronology = [...b.chronology].reverse()
  const rows = chronologyRows(b)
  assert.deepEqual(rows.map((r) => r.id), b.chronology)
  assert.ok(rows.every((r) => !('parent' in r) && !('children' in r)))
  assert.match(rows[0].at, /^\d{2}:\d{2}:\d{2}$/)
})

test('subtreeErrors marks the ancestors of an errored node, so a collapsed branch can say it hides one', () => {
  const t = buildTree(body())
  const e = subtreeErrors(t)
  assert.ok(e.has('span:root'))
  assert.ok(!e.has('span:t3'), 'a branch with no error below it is not marked')
})

test(`defaultExpanded: everything open up to ${EXPAND_ALL_MAX_NODES} nodes, roots only above — a count, never timing`, () => {
  const t = buildTree(body())
  assert.deepEqual([...defaultExpanded(t)].sort(), ['span:root', 'span:t3'])
  const big = body()
  for (let i = 0; i < 60; i += 1) {
    big.nodes.push(node({ id: `span:k${i}`, parent_id: 'span:t3', provenance: prov({ parent_status: 'attached' }) }))
  }
  const bt = buildTree(big)
  assert.deepEqual([...defaultExpanded(bt)], ['span:root'])
})

// --- words -----------------------------------------------------------------------

test('4. a worker row is the worker label (minus :main), never the connector name', () => {
  const w = body().nodes[0]
  assert.equal(nodeTitle(w), 'returns-bot')
  assert.doesNotMatch(nodeTitle(w), /Grok Bot/)
  assert.equal(workerDisplay('returns-bot:reviewer'), 'returns-bot:reviewer')
  assert.equal(workerDisplay(''), null)
})

test('6/10/11. model and tool rows: model name; Tool · <MCP server> with the technical name kept; dotted names name no system', () => {
  const b = body()
  const m = b.nodes.find((n) => n.id === 'span:m1')
  assert.equal(nodeTitle(m), 'Model · grok-4')
  assert.equal(nodeSubtitle(m), 'xai')
  const shop = b.nodes.find((n) => n.id === 'span:t1')
  assert.equal(nodeTitle(shop), 'Tool · Shopify')
  assert.equal(nodeSubtitle(shop), 'mcp__shopify__lookup_order')
  const stripe = b.nodes.find((n) => n.id === 'span:t2')
  assert.equal(nodeTitle(stripe), 'Tool', 'stripe.refunds.create names no system Trovis can vouch for')
  assert.equal(nodeSubtitle(stripe), 'stripe.refunds.create')
  assert.equal(toolSystem({ mcp_server: 'creative-toolkit' }), 'creative-toolkit', 'an unknown server is shown raw, not guessed into a brand')
  assert.equal(toolSystem({ mcp_server: null }), null)
})

test('6/7/8/9. row facts: duration, tokens and cost only when known; zero usage is 0; covered is "in run total"; unknown cost is absent', () => {
  const b = body()
  const m1 = nodeMeta(b.nodes.find((n) => n.id === 'span:m1'))
  assert.equal(m1.duration, '1.8s')
  assert.deepEqual(m1.facts, ['1,284 tokens', '$0.02'])
  const m2 = nodeMeta(b.nodes.find((n) => n.id === 'span:m2'))
  assert.deepEqual(m2.facts, ['0 tokens', 'in run total'])
  assert.equal(nodeCostLabel({ known: true, source: 'covered', amount_usd: null }), 'in run total')
  assert.equal(nodeCostLabel(null), null)
  assert.equal(nodeCostLabel({ known: true, source: 'estimated', amount_usd: 0.0042 }), '$0.0042')
  assert.equal(fmtTokens(null), null)
  assert.equal(fmtTokens(0), '0 tokens')
  assert.equal(fmtTokens(1), '1 token')
  const t = nodeMeta(b.nodes.find((n) => n.id === 'span:t1'))
  assert.deepEqual(t.facts, [], 'a tool span with no usage and no cost shows neither — not $0, not 0 tokens')
})

test('12/13/19. error line only for status=error; a reported success is never an outcome; completion is the record closing', () => {
  const b = body()
  assert.equal(nodeErrorLine(b.nodes.find((n) => n.id === 'span:t2')), 'Error · Card declined')
  assert.equal(nodeErrorLine(b.nodes.find((n) => n.id === 'span:t3')), null)
  assert.equal(nodeErrorLine(node({ status: 'error', error: null })), 'Error')
  const okTool = b.nodes.find((n) => n.id === 'span:t3')
  const words = [nodeTitle(okTool), nodeSubtitle(okTool), ...nodeMeta(okTool).facts].join(' ')
  assert.doesNotMatch(words, /succeed|success|verified|refunded|completed/i)
  const done = b.nodes.find((n) => n.id === 'event:9')
  assert.equal(nodeTitle(done), 'Work record closed')
  assert.equal(nodeSubtitle(done), 'completed by agent')
  assert.doesNotMatch(nodeTitle(done) + nodeSubtitle(done), /verified|outcome|success/i)
})

test('18. other nodes keep their raw label; handoff, wait and system nodes read from the event, not from a tool', () => {
  assert.equal(nodeTitle(node({ type: 'other', label: 'HTTP GET' })), 'HTTP GET')
  assert.equal(TYPE_LABELS.other, 'Activity')
  assert.equal(nodeTitle(node({ type: 'handoff', event: { type: 'handoff_initiated', direction: 'to_human' } })), 'Handoff · to a person')
  assert.equal(nodeTitle(node({ type: 'handoff', event: { type: 'handoff_completed' } })), 'Handoff completed')
  assert.equal(nodeTitle(node({ type: 'wait', event: { type: 'handoff_initiated', direction: 'to_system', actor: { type: 'system', label: 'Stripe' }, waiting_on: 'payment processing' } })), 'Waiting on Stripe')
  assert.equal(nodeTitle(node({ type: 'system', event: { type: 'handoff_completed', actor: { type: 'system', label: 'Stripe' }, provider: 'stripe' } })), 'Stripe · state observed')
  assert.equal(nodeSubtitle(node({ type: 'handoff', event: { type: 'handoff_initiated', direction: 'to_human', reason: 'turn_end' } })), null, 'a wire token is not prose')
})

// --- header ------------------------------------------------------------------------

test('summary: counts, observed span, tokens and cost from the record; covered spans are not added to the cost; no verdict', () => {
  const s = executionSummary(body())
  assert.equal(s.nodes, 9)
  assert.equal(s.traces, 1)
  assert.equal(s.observed, '12s', 'first recorded start to last recorded end among spans (the root ends at 12.4s)')
  assert.equal(s.tokens, '1,296 tokens')
  assert.equal(s.cost, '$0.05', 'estimated 0.02 + reported 0.03; the covered span adds nothing')
  assert.equal(s.errors, 1)
  assert.ok(!('score' in s) && !('health' in s) && !('grade' in s))
})

test('7. summary cost is null (not $0) when no node carries a priced cost; tokens null when none carry usage', () => {
  const b = body()
  for (const n of b.nodes) { n.cost = null; n.usage = null }
  const s = executionSummary(b)
  assert.equal(s.cost, null)
  assert.equal(s.tokens, null)
  const covered = body()
  for (const n of covered.nodes) n.cost = n.cost ? { known: true, source: 'covered', amount_usd: null } : null
  assert.equal(executionSummary(covered).cost, null, 'only covered costs → no total is claimed')
})

test('24. bounded and diagnostics notes are quiet sentences, present only when the endpoint says so', () => {
  assert.equal(boundedNote(body()), null)
  assert.match(boundedNote(body({ bounded: true, span_limit: 2000 })), /bounded: the first 2,000 spans were read and later spans were not/)
  assert.doesNotMatch(boundedNote(body({ bounded: true })), /broken|incomplete data|error/i)
  assert.equal(diagnosticsNote(body()), null)
  assert.equal(diagnosticsNote(body({ duplicate_spans_dropped: 1, cycles_broken: 2 })), '1 duplicate span dropped · 2 recorded parent cycles broken')
})

// --- inspector -----------------------------------------------------------------------

test('5/29. inspector: worker and connector are two rows; ids live in technical details', () => {
  const secs = inspectorSections(body().nodes[0])
  const identity = secs.find((s) => s.title === 'Identity')
  assert.deepEqual(identity.rows.map((r) => [r.label, r.value]), [
    ['Type', 'Worker'], ['Label', 'agent_run'], ['Worker', 'returns-bot'], ['Connector', 'Grok Bot'],
  ])
  const tech = technicalDetails(body().nodes[0])
  assert.deepEqual(tech.map((r) => r.label), ['Trace id', 'Span id', 'Loop link', 'Span kind', 'Event type', 'Node id'])
  assert.equal(tech[0].value, 'a'.repeat(32))
  assert.equal(tech.find((r) => r.label === 'Event type').value, 'agent_run')
})

test('12/30. inspector: the tool section says "Reported by worker · Call completed/failed" — never an outcome — and omits absent fields', () => {
  const b = body()
  const ok = inspectorSections(b.nodes.find((n) => n.id === 'span:t3')).find((s) => s.title === 'Tool')
  assert.deepEqual(ok.rows.map((r) => [r.label, r.value]), [
    ['Technical name', 'stripe.refunds.create'], ['Call id', 'c3'], ['Reported by worker', 'Call completed'],
  ])
  const failed = inspectorSections(b.nodes.find((n) => n.id === 'span:t2'))
  assert.equal(failed.find((s) => s.title === 'Tool').rows.at(-1).value, 'Call failed')
  assert.deepEqual(failed.find((s) => s.title === 'Status').rows.map((r) => r.value), ['Error', 'Card declined'])
  const shop = inspectorSections(b.nodes.find((n) => n.id === 'span:t1')).find((s) => s.title === 'Tool')
  assert.deepEqual(shop.rows.map((r) => r.label), ['Name', 'Technical name', 'MCP server', 'Call id', 'Reported by worker'])
  const all = JSON.stringify(inspectorSections(b.nodes.find((n) => n.id === 'span:t2')))
  assert.doesNotMatch(all, /null|undefined|None|verified|business/)
})

test('6/9/30. inspector model section: tokens as counts, covered cost as "in run total" with its source, unknown cost omitted', () => {
  const b = body()
  const m2 = inspectorSections(b.nodes.find((n) => n.id === 'span:m2')).find((s) => s.title === 'Model')
  assert.deepEqual(m2.rows.map((r) => [r.label, r.value]), [
    ['Provider', 'xai'], ['Model', 'grok-4'], ['Input tokens', '0'], ['Output tokens', '0'], ['Total tokens', '0'],
    ['Cost', 'in run total'], ['Cost source', 'Covered by a reported run total'],
  ])
  const m1 = inspectorSections(b.nodes.find((n) => n.id === 'span:m1')).find((s) => s.title === 'Model')
  assert.deepEqual(m1.rows.filter((r) => /Cost/.test(r.label)).map((r) => r.value), ['$0.02', 'Estimated from tokens'])
  const noCost = inspectorSections(node({ type: 'model', model: { name: 'm', provider: null }, usage: { total_tokens: 5 } })).find((s) => s.title === 'Model')
  assert.ok(!noCost.rows.some((r) => /Cost/.test(r.label)), 'no cost row at all when cost is unknown — never $0')
})

test('inspector: provenance names evidence kind in Evidence’s words, correlation "not recorded" when null, parent status in words', () => {
  const b = body()
  const p = inspectorSections(b.nodes.find((n) => n.id === 'span:t1')).find((s) => s.title === 'Provenance')
  assert.deepEqual(p.rows.map((r) => [r.label, r.value]), [
    ['Record', 'Span'], ['Evidence kind', 'Reported by the worker'], ['Correlation', 'explicit key'],
    ['Classified by', 'event type'], ['Parent', 'Attached to its recorded parent'],
  ])
  const hist = inspectorSections(node({ provenance: prov({ correlation: null, parent_status: 'outside_read_set', evidence_kind: 'execution' }) }))
  const pr = hist.find((s) => s.title === 'Provenance')
  assert.equal(pr.rows.find((r) => r.label === 'Correlation').value, 'not recorded')
  assert.equal(pr.rows.find((r) => r.label === 'Parent').value, 'Parent recorded but not in this run’s read set')
  const ev = inspectorSections(b.nodes.find((n) => n.id === 'event:9'))
  assert.equal(ev.find((s) => s.title === 'Provenance').rows[0].value, 'Work record event')
  assert.equal(ev.find((s) => s.title === 'Event').rows.find((r) => r.label === 'Reason').value, 'completed_by_agent')
  assert.ok(!ev.some((s) => s.title === 'Model' || s.title === 'Tool'))
})

test('35. no classification, no inferred edges: the helper never reads span names into types and never assigns parents', () => {
  const code = strip(src('execution.js'))
  assert.doesNotMatch(code, /span_name|\.label\s*===\s*['"](model_call|tool_call|llm_output)/, 'types come from node.type')
  assert.doesNotMatch(code, /parent_id\s*=(?!=)/, 'parent_id is read, never written')
  assert.doesNotMatch(code, /started_at[^\n]*parent|parent[^\n]*started_at/, 'timing never meets parentage')
  assert.doesNotMatch(code, /retry|verified|healthy|score/i)
  const view = strip(src('ExecutionView.jsx'))
  assert.doesNotMatch(view, /parent_id\s*=(?!=)|retried|retries|verified|healthy|score/i)
  // The only "retry" in the view is the failed-fetch Retry button: the
  // onRetry prop, its onClick, and the label. Nothing calls a node a retry.
  assert.equal((view.match(/retry/gi) || []).length, 3)
  assert.doesNotMatch(view, /retry[^\n]*node|node[^\n]*retry/i)
})
