// Home, and the rules that stop it lying about imperfect data.
//
// Every number on Home comes from /home/snapshot and every interpretation from
// /home/findings. The interesting cases are all the ones where the contract
// says "I could not establish this" — because the easy renderer prints 0, and
// 0 is a claim.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import {
  askQuestionFor, bucketLabel, describeGaps, findingQualifier, findingTargets,
  formatMoney, groupFindings, pollDelay, readAnalysis, readAttention,
  readComparison, readCount, readFinancial, readFreshness, readJobs,
  readSeries, workEmptyCopy, workState, zeroMeansNone, createRaceGuard,
} from '../src/homeView.js'
import { homeQuery } from '../src/homeQuery.js'

const src = (f) =>
  readFileSync(new URL(`../src/${f}`, import.meta.url), 'utf8')
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/^\s*\/\/.*$/gm, '')

const hv = src('HomeView.jsx')
const sections = src('HomeSections.jsx')
const panel = src('HomeFindingPanel.jsx')
const helpers = src('homeView.js')
const api = src('api.js')

// --- counts: exact vs floor vs unknown ---------------------------------------

test('a recorded zero and an unestablished number are different answers', () => {
  const exact = readCount(0, { exact: true, qualifier: 'exact' })
  assert.equal(exact.kind, 'exact')
  assert.equal(exact.text, '0')

  const missing = readCount(null, { exact: true })
  assert.equal(missing.kind, 'unknown')
  assert.notEqual(missing.text, '0', 'an unknown must never render as zero')
  assert.equal(missing.value, null)
})

test('a bounded search yields a floor, never a total', () => {
  const floor = readCount(12, { exact: false, qualifier: 'at_least' })
  assert.equal(floor.kind, 'floor')
  assert.equal(floor.text, '12+')
  assert.match(floor.note, /at least/i)
})

test('absence_established:false means a zero cannot be read as "none"', () => {
  assert.equal(zeroMeansNone({ completeness: { absence_established: true } }), true)
  assert.equal(zeroMeansNone({ completeness: { absence_established: false } }), false)
  // Missing metadata is treated as the permissive default the contract sets.
  assert.equal(zeroMeansNone({}), true)
})

// --- the four different empties ----------------------------------------------

test('empty workspace, empty scope and unknown scope are told apart', () => {
  assert.equal(workState({ completeness: { workspace_state: 'empty' } }), 'first-run')
  assert.equal(
    workState({ completeness: { workspace_state: 'populated', scope_state: 'empty' } }),
    'scope-empty',
  )
  assert.equal(
    workState({ completeness: { workspace_state: 'populated', scope_state: 'unknown' } }),
    'scope-unknown',
  )
  assert.equal(
    workState({ completeness: { workspace_state: 'populated', scope_state: 'populated' } }),
    'populated',
  )
})

test('an unknown-empty scope never claims nothing happened', () => {
  const unknown = workEmptyCopy('scope-unknown', { effective: 'team' })
  assert.match(unknown.sub, /not the same as/i)
  const empty = workEmptyCopy('scope-empty', { effective: 'team' })
  assert.match(empty.sub, /complete/i)
  assert.notEqual(unknown.lead, empty.lead)
  // Only the first-run state offers to connect an agent.
  assert.equal(workEmptyCopy('first-run', {}).action, 'connect')
  assert.equal(empty.action, null)
})

// --- charts -------------------------------------------------------------------

test('missing history is not drawn as a flat zero line', () => {
  const none = readSeries({ completions_series: { available: false, unavailable_reason: 'x' } })
  assert.equal(none.available, false)
  assert.deepEqual(none.points, [])
  // And the renderer says so instead of drawing.
  assert.match(sections, /Trovis does not draw history it does not have/)
})

test('a chart over partial membership is marked partial even when it reconciles', () => {
  const s = readSeries({
    completions_series: {
      available: true, exact: true, qualifier: 'exact', reconciles: true,
      points: [{ bucket_start: '2026-09-01T00:00:00+00:00', completed: 2 }],
    },
    completeness: { completion_series_complete: false },
  })
  assert.equal(s.available, true)
  assert.equal(s.partial, true, 'reconciling is not the same as complete')
})

test('the job breakdown keeps unclassified work visible', () => {
  const jobs = readJobs({
    by_job: {
      rows: [{ workflow_id: 1, name: 'Refunds', completed: 5 }],
      other_completed: 2, unclassified_completed: 3, total_job_count: 4,
    },
  })
  const kinds = jobs.rows.map((r) => r.kind)
  assert.ok(kinds.includes('unclassified'), 'work with no declared job is still shown')
  assert.ok(kinds.includes('other'))
  assert.equal(jobs.max, 5)
})

test('a comparison renders only when the contract says the windows compare', () => {
  const ok = readComparison({
    period: { comparison: { available: true, delta: 3, previous_completed: 2 } },
    completeness: { comparison_available: true },
  })
  assert.equal(ok.direction, 'up')
  // Available on the block but withheld by completeness → no comparison.
  assert.equal(
    readComparison({
      period: { comparison: { available: true, delta: 3 } },
      completeness: { comparison_available: false },
    }),
    null,
  )
  assert.equal(readComparison({ period: { comparison: { available: false } } }), null)
})

test('bucket labels do not crash on a missing or unparseable stamp', () => {
  assert.equal(bucketLabel(null), '')
  assert.equal(bucketLabel('not-a-date'), '')
})

// --- personal attention --------------------------------------------------------

test('an API-key session gets no invented personal identity', () => {
  const a = readAttention({ attention: { available: true, needs_you: null, viewer_user_id: null } })
  assert.equal(a.kind, 'unavailable')
  assert.equal(a.count, null)
  assert.match(a.text, /signed-in person/)
})

test('a truncated assignee scan is a floor, not a count', () => {
  const a = readAttention({
    attention: { available: true, needs_you: 4, viewer_user_id: 7, resolution_complete: false },
  })
  assert.equal(a.kind, 'floor')
  assert.equal(a.text, '4+')
})

test('an established zero is shown as zero', () => {
  const a = readAttention({
    attention: { available: true, needs_you: 0, viewer_user_id: 7, resolution_complete: true },
  })
  assert.equal(a.kind, 'exact')
  assert.equal(a.text, '0')
})

// --- findings ------------------------------------------------------------------

test('grouping preserves the backend ranking inside each group', () => {
  const input = [
    { id: 1, category: 'attention' }, { id: 2, category: 'opportunity' },
    { id: 3, category: 'attention' }, { id: 4, category: 'positive_change' },
  ]
  const g = groupFindings(input)
  assert.deepEqual(g.attention.map((f) => f.id), [1, 3], 'server order kept')
  assert.equal(g.opportunity.length, 1)
  // No client-side scoring anywhere.
  assert.doesNotMatch(helpers, /\.sort\(/, 'findings are never re-sorted client-side')
})

test('the qualification is read off coverage, never invented from a category', () => {
  assert.equal(findingQualifier({ confidence: 'supported', coverage: {} }), null)
  const q = findingQualifier({
    confidence: 'qualified', claim_kind: 'hypothesis',
    coverage: { retrieval_complete: false },
  })
  assert.match(q, /proposed explanation/i)
  assert.match(q, /whole scope/i)
  // Category alone says nothing about severity or urgency.
  assert.equal(findingQualifier({ category: 'attention', confidence: 'supported', coverage: {} }), null)
  assert.doesNotMatch(helpers, /severity|urgency|impact_score/i)
})

test('an inexact drill-through is disclosed, not implied', () => {
  const targets = findingTargets({
    navigation: {
      targets: [
        { kind: 'run', id: 4, exact: true },
        { kind: 'job', id: 9, exact: false, note: 'Filters to this job, but not to the period.' },
      ],
    },
  })
  assert.equal(targets[0].exact, true)
  assert.equal(targets[1].exact, false)
  assert.match(targets[1].note, /not to the period/)
  // And the panel renders that note rather than swallowing it.
  assert.match(panel, /!t\.exact \?/)
  assert.match(panel, /cannot filter to this finding/)
})

test('Ask is only offered with a specific, grounded question', () => {
  assert.equal(askQuestionFor({ title: '' }), null)
  const q = askQuestionFor({ title: 'Three refund runs stopped', entities: [{ label: 'Refund 0' }] })
  assert.match(q, /Three refund runs stopped/)
  assert.match(q, /Refund 0/)
})

// --- the analysis lifecycle ------------------------------------------------------

test('absent findings are never "everything is healthy"', () => {
  const r = readAnalysis({ state: 'current' }, { findingCount: 0 })
  assert.doesNotMatch(r.label, /healthy|all good|no issues/i)
  assert.match(r.label, /nothing to raise/i)
  assert.match(r.detail, /not a health verdict/i)
  // Nowhere in the UI either.
  for (const [name, s] of [['HomeView', hv], ['HomeSections', sections]]) {
    assert.doesNotMatch(s, /Everything is healthy|All clear|You're all set/i, name)
  }
})

test('only the genuinely in-flight states poll', () => {
  for (const state of ['queued', 'running']) {
    assert.equal(readAnalysis({ state }).poll, true, state)
  }
  // Settled answers, including the ones that settled badly. Polling these is
  // retry pressure against a backend that already said to wait.
  for (const state of ['current', 'debounced', 'incomplete', 'failed', 'unavailable']) {
    assert.equal(readAnalysis({ state }).poll, false, state)
  }
})

test('polling is bounded and backs off', () => {
  assert.equal(pollDelay('current', 0), null)
  const first = pollDelay('running', 0)
  const later = pollDelay('running', 3)
  assert.ok(later > first, 'it backs off')
  assert.ok(later <= 60000, 'and is capped')
  assert.equal(pollDelay('running', 8), null, 'it gives up rather than spinning forever')
  // No fixed interval anywhere on Home.
  assert.doesNotMatch(hv, /setInterval/)
})

test('an unavailable model does not sit under a spinner', () => {
  const r = readAnalysis({ state: 'unavailable', reason: 'no_model_configured' })
  assert.equal(r.poll, false)
  assert.match(r.label, /not configured/i)
  assert.match(r.detail, /do not depend on it/i)
  // The pulse only renders for an in-flight state.
  assert.match(sections, /const inFlight = read\.state === 'queued' \|\| read\.state === 'running'/)
  assert.match(sections, /\{inFlight \? <span className="hv-ai-pulse"/)
})

test('an incomplete analysis says so and keeps the earlier findings', () => {
  const r = readAnalysis({
    state: 'incomplete', findings_from_previous_analysis: true,
    completion_gaps: ['validation_rejected'],
  })
  assert.equal(r.tone, 'warn')
  assert.match(r.detail, /earlier analysis/i)
  assert.match(r.detail, /own checks/i)
  assert.match(describeGaps(['wording_withheld']), /within the evidence/)
  assert.match(describeGaps([]), /kept rather than retired/)
})

test('debounce says new evidence is waiting rather than implying coverage', () => {
  const r = readAnalysis({ state: 'debounced', newer_evidence_available: true, debounce_seconds: 300 })
  assert.match(r.label, /waiting to be analysed/i)
  assert.equal(r.poll, false)
  const quiet = readAnalysis({ state: 'debounced', newer_evidence_available: false })
  assert.match(quiet.label, /recently/i)
})

test('a failed analysis is not a successful empty one', () => {
  const r = readAnalysis({ state: 'failed', reason: 'model outage', findings_from_previous_analysis: true })
  assert.match(r.label, /failed/i)
  assert.match(r.detail, /kept and still shown/i)
  assert.equal(r.poll, false)
})

// --- freshness -------------------------------------------------------------------

test('retrieval, analysis and source freshness are three different times', () => {
  const f = readFreshness(
    {
      generated_at: '2026-09-11T10:00:00+00:00',
      freshness: {
        latest_work_activity_at: '2026-09-11T09:00:00+00:00',
        latest_telemetry_at: '2026-09-11T09:30:00+00:00',
        absence_established: false,
      },
    },
    { analysis: { completed_at: '2026-09-11T08:00:00+00:00' } },
  )
  assert.notEqual(f.retrieved, f.analysed)
  assert.notEqual(f.analysed, f.sourceActivity)
  assert.equal(f.absenceEstablished, false)
  // A null under an incomplete scope reads "not found", not "never happened".
  assert.match(sections, /“not found”, not “never happened”/)
})

// --- cost --------------------------------------------------------------------------

test('no financial visibility means no financial content at all', () => {
  assert.equal(readFinancial({ financial: { visible: false } }), null)
  // And Home does not reach for another cost endpoint to work around the gate.
  assert.doesNotMatch(hv, /getCost\b|getCostOverview|getAgentCosts|dashboard\/cost/)
})

test('organization-wide spend is labelled organization-wide', () => {
  const fin = readFinancial({
    financial: {
      visible: true, scope: 'organization_wide', currency: 'USD', spend_usd: 12.5,
      coverage: { ratio: 0.5, unpriced_token_spans: 4 },
    },
  })
  assert.equal(fin.orgWide, true)
  assert.equal(fin.attributable, false)
  assert.match(sections, /covers the whole organization regardless of the work scope/)
})

test('unpriced cost reads as unknown, never as free', () => {
  const fin = readFinancial({
    financial: { visible: true, spend_usd: 1, coverage: { unpriced_token_spans: 9 } },
  })
  assert.match(fin.unpricedNote, /unknown, not zero/)
  const none = readFinancial({
    financial: { visible: true, spend_usd: 1, coverage: { unpriced_token_spans: 0 } },
  })
  assert.equal(none.unpricedNote, null)
})

test('an empty workspace shows no cost card at all', () => {
  // $0.00 beside "connect an agent" is noise, not context.
  assert.match(hv, /if \(workState\(snap\) === 'first-run'\) return null/)
})

test('no cost-per-completion, savings or efficiency ratio is computed', () => {
  for (const [name, s] of [['homeView', helpers], ['HomeView', hv], ['HomeSections', sections]]) {
    assert.doesNotMatch(s, /costPer|cost_per|perCompletion|savings|efficiency|roi/i, name)
  }
})

test('money formats with the currency the contract supplied', () => {
  assert.match(formatMoney(12.5, 'USD'), /12\.50/)
  assert.equal(formatMoney(null, 'USD'), '—')
})

// --- the two requests ask the same question ------------------------------------------

test('snapshot and findings are built from one query helper', () => {
  assert.equal(homeQuery({ days: 7, tz: 'UTC', whose: 'team', personId: null }), '?days=7&tz=UTC&whose=team')
  // 'everyone' is omitted: the server already applies the seat's own breadth.
  assert.equal(homeQuery({ days: 7, tz: 'UTC', whose: 'everyone' }), '?days=7&tz=UTC')
  assert.match(homeQuery({ personId: 4 }), /person_id=4/)
  assert.match(homeQuery({ includeDismissed: true }), /include_dismissed=true/)
  // Both endpoints go through it, so they cannot describe different slices.
  for (const call of ['getHomeSnapshot', 'getHomeFindings', 'getHomeFinding']) {
    assert.match(api, new RegExp(`${call}:[\\s\\S]{0,220}homeQuery`), call)
  }
})

test('one query key drives both reads, and it carries the seat', () => {
  assert.match(hv, /const queryKey = \[/)
  assert.match(hv, /seat\?\.surfaces \|\| \[\]\)\.join\(','\)/, 'a permission change is a new key')
  assert.match(hv, /useHomeRead\(\s*\(signal\) => api\.getHomeSnapshot\(\{ \.\.\.query, signal \}\),\s*\[queryKey\]/)
  assert.match(hv, /useHomeRead\(\s*\(signal\) => api\.getHomeFindings\(\{ \.\.\.query, signal \}\),\s*\[queryKey\]/)
})

// --- independence and staleness -------------------------------------------------------

test('an out-of-order response for a superseded request is dropped', async () => {
  // The real shape of the bug: scope A is asked, scope B is asked, and A's
  // answer arrives LAST. Aborting alone does not cover it — a request already
  // in flight can still resolve.
  const guard = createRaceGuard()
  const landed = []
  function ask(label, ms) {
    const mine = guard.next()
    return new Promise((r) => setTimeout(r, ms)).then(() => {
      if (!guard.isCurrent(mine)) return
      landed.push(label)
    })
  }
  const slowOldScope = ask('scope-A', 30)
  const fastNewScope = ask('scope-B', 1)
  await Promise.all([slowOldScope, fastNewScope])
  assert.deepEqual(landed, ['scope-B'], 'only the newest question is answered')
})

test('rapid scope changes leave exactly the last one rendered', async () => {
  const guard = createRaceGuard()
  const landed = []
  const delays = [40, 5, 25, 1, 30] // deliberately unordered
  await Promise.all(
    delays.map((ms, i) => {
      const mine = guard.next()
      return new Promise((r) => setTimeout(r, ms)).then(() => {
        if (guard.isCurrent(mine)) landed.push(i)
      })
    }),
  )
  assert.deepEqual(landed, [4], 'the fifth request is the only one that may land')
})

test('teardown invalidates everything in flight', async () => {
  // What a permission change does: after invalidate(), no answer issued
  // before it is current, so unauthorized cached content cannot be re-shown.
  const guard = createRaceGuard()
  const mine = guard.next()
  guard.invalidate()
  assert.equal(guard.isCurrent(mine), false)
})

test('both reads and the detail panel use the same guard', () => {
  assert.match(hv, /const guard = useRef\(createRaceGuard\(\)\)\.current/)
  assert.match(hv, /if \(!guard\.isCurrent\(mine\)\) return/)
  assert.match(hv, /guard\.invalidate\(\)/)
  assert.match(hv, /controller\.abort\(\)/)
  assert.match(panel, /if \(!guard\.isCurrent\(mine\)\) return/)
  assert.match(panel, /guard\.invalidate\(\)/)
})

test('snapshot and findings fail separately', () => {
  // Two independent reads, two independent error branches, and the numbers
  // survive a findings outage.
  assert.match(hv, /if \(snapshot\.error\)/)
  assert.match(hv, /\{findings\.error \?/)
  assert.match(hv, /The numbers above are unaffected/)
  assert.match(hv, /The numbers here are unavailable/)
})

test('a scope or permission change closes the finding detail', () => {
  assert.match(hv, /setOpenFinding\(null\)[\s\S]{0,120}\}, \[queryKey\]\)/)
})

test('polling is torn down with the pane and the query', () => {
  assert.match(hv, /if \(!active \|\| !analysis\.poll\) return undefined/)
  assert.match(hv, /return \(\) => clearTimeout\(t\)/)
  assert.match(hv, /pollAttempt\.current = 0\s*\}, \[queryKey\]\)/)
})

// --- what Home is allowed to fetch -------------------------------------------------------

test('Home never calls the endpoints that starve the replica', () => {
  for (const [name, s] of [['HomeView', hv], ['HomeSections', sections], ['HomeFindingPanel', panel]]) {
    assert.doesNotMatch(s, /getWorkBoard|getWorkSummary|listAgents/, name)
  }
})

test('no model runs on the Home read path', () => {
  assert.doesNotMatch(hv, /getBriefing|getPulseInsight|askDashboard/)
})

// --- no execution controls --------------------------------------------------------------

test('Home offers no way to execute anything', () => {
  for (const [name, s] of [['HomeView', hv], ['HomeSections', sections], ['HomeFindingPanel', panel]]) {
    assert.doesNotMatch(s, /Approve fix|Retry agent|Run again|Apply fix|>\s*Fix\s*</i, name)
  }
  // What it does offer is a step a PERSON takes, and it says so.
  assert.match(panel, /A step for a person\. Trovis does not act on findings\./)
})

test('acknowledge and dismiss never claim the condition ended', () => {
  assert.match(panel, /Neither\s*\n?\s*says the condition ended/)
  assert.doesNotMatch(panel, /Resolve|Mark resolved|Mark fixed/i)
  assert.match(panel, /api\.setFindingState\(findingId, state, query\)/)
  assert.match(panel, /setState\('acknowledged'\)/)
  assert.match(panel, /setState\('dismissed'\)/)
  // A dismissed finding stays reachable.
  assert.match(panel, /include-dismissed/)
  assert.match(src('homeQuery.js'), /include_dismissed/)
})

test('a failed mutation keeps the finding on screen', () => {
  // The panel surfaces the error and leaves the body intact — no close, no
  // optimistic removal.
  assert.match(panel, /setMutationError\(err\?\.message/)
  assert.match(panel, /className="hv-panel-mut-error" role="alert"/)
  // And the list's acknowledge never drops the row on failure: the catch
  // clears the busy flag and nothing else.
  assert.match(hv, /catch \{\s*\n\s*setMutating\(null\)\s*\n\s*\}/)
})

// --- honesty of language -----------------------------------------------------------------

test('a recorded completion is never called a verified outcome', () => {
  for (const [name, s] of [['HomeView', hv], ['HomeSections', sections]]) {
    assert.doesNotMatch(s, /successful outcomes?|hours saved|business impact|time saved/i, name)
  }
  assert.match(hv, /Recorded completions/)
  assert.match(hv, /not an independently verified business outcome/)
})

test('no canned AI copy fills an empty section', () => {
  // An empty opportunities list renders nothing at all rather than a
  // client-generated "insight".
  assert.match(hv, /if \(!loading && !items\.length\) return null/)
  assert.doesNotMatch(hv, /fallbackInsight|pulsePacket|briefingLead/)
})
