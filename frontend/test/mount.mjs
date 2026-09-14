// A minimal mounted-component harness.
//
// The suite here is mostly pure functions and source-text assertions, and that
// is exactly how seven integration bugs shipped with 572 tests green: a regex
// can confirm a string is present, and cannot confirm that clicking the button
// it belongs to does anything. So this renders the real components into jsdom,
// with the API stubbed at the module boundary, and drives them.
//
// It is deliberately small: jsdom + react-dom/client, no testing-library, no
// new runner. `node --test` stays the runner.

import { JSDOM } from 'jsdom'

let installed = false

/** Install a DOM. Call once per test file, before importing any component. */
export function installDom() {
  if (installed) return
  const dom = new JSDOM('<!doctype html><html><body></body></html>', {
    url: 'http://localhost/',
    pretendToBeVisual: true,
  })
  const g = globalThis
  g.window = dom.window
  g.document = dom.window.document
  // Node 22 defines `navigator` as a getter-only global, so assigning it
  // throws. Redefine instead — jsdom's is what React and the components read.
  Object.defineProperty(g, 'navigator', {
    value: dom.window.navigator, configurable: true, writable: true,
  })
  g.localStorage = dom.window.localStorage
  g.sessionStorage = dom.window.sessionStorage
  g.HTMLElement = dom.window.HTMLElement
  g.Node = dom.window.Node
  g.Event = dom.window.Event
  g.CustomEvent = dom.window.CustomEvent
  g.MutationObserver = dom.window.MutationObserver
  g.getComputedStyle = dom.window.getComputedStyle
  g.requestAnimationFrame = (cb) => setTimeout(() => cb(Date.now()), 0)
  g.cancelAnimationFrame = (id) => clearTimeout(id)
  g.IS_REACT_ACT_ENVIRONMENT = true
  // Vite's `import.meta.env`, which api.js reads at module scope.
  if (!g.__viteEnvPatched) {
    g.__viteEnvPatched = true
  }
  installed = true
}

/**
 * Mount an element and return handles for driving it.
 *
 * `act` is React's own, so state updates and effects flush the way they do in
 * the browser rather than whenever a timer happens to fire.
 */
export async function mount(element) {
  const { createRoot } = await import('react-dom/client')
  const { act } = await import('react')
  const container = document.createElement('div')
  document.body.appendChild(container)
  const root = createRoot(container)
  await act(async () => {
    root.render(element)
  })
  return {
    container,
    /** Re-render with new props. */
    async render(next) {
      await act(async () => {
        root.render(next)
      })
    },
    /** Let pending promises and effects settle. */
    async settle(ms = 0) {
      await act(async () => {
        await new Promise((r) => setTimeout(r, ms))
      })
    },
    async click(el) {
      await act(async () => {
        el.dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
      })
    },
    text() {
      return container.textContent || ''
    },
    $(sel) {
      return container.querySelector(sel)
    },
    $$(sel) {
      return [...container.querySelectorAll(sel)]
    },
    unmount() {
      act(() => root.unmount())
      container.remove()
    },
  }
}

/**
 * A deferred promise, for holding a response open until the test decides.
 *
 * Out-of-order delivery is the whole point of several cases below, and you
 * cannot write those with `setTimeout` races without making them flaky.
 */
export function deferred() {
  let resolve, reject
  const promise = new Promise((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

/** A snapshot body with sane defaults, overridable per test. */
export function snapshotFixture(over = {}) {
  return {
    generated_at: '2026-09-11T12:00:00+00:00',
    scope: { requested: 'everyone', effective: 'everyone', choices: ['everyone', 'me'],
             breadth: 'company', membership_complete: true, account_id: 1,
             viewer_user_id: 1 },
    period: {
      start: '2026-09-05T00:00:00+00:00', end: '2026-09-11T12:00:00+00:00',
      start_utc: '2026-09-05T00:00:00+00:00', end_utc: '2026-09-11T12:00:00+00:00',
      timezone: 'UTC', days: 7, completed: 27, abandoned: 1,
      exact: true, qualifier: 'exact',
      comparison: { available: false },
    },
    current_state: { as_of: 'now', open: 4, moving: 3, waiting_on_person: 1,
                     blocked: 0, exact: true, qualifier: 'exact' },
    completions_series: {
      available: true, bucket: 'local_day', timezone: 'UTC',
      points: [{ bucket_start: '2026-09-05T00:00:00+00:00', completed: 4 }],
      total: 27, aggregate_total: 27, reconciles: true, exact: true,
      qualifier: 'exact',
    },
    by_job: { rows: [{ workflow_id: 3, name: 'Refunds', completed: 7 }],
              row_limit: 12, truncated: false, other_completed: 0,
              unclassified_completed: 0, total_job_count: 1,
              aggregate_total: 7, reconciles: true, exact: true },
    attention: { available: true, needs_you: 2, needs_you_at_least: 2,
                 viewer_user_id: 1, scoped_to: 'session_identity',
                 resolution_complete: true },
    financial: { visible: true, scope: 'organization_wide', currency: 'USD',
                 spend_usd: 12.5, period_start_utc: '2026-09-05T00:00:00+00:00',
                 period_end_utc: '2026-09-11T12:00:00+00:00',
                 attributable_to_shown_work: false,
                 coverage: { ratio: 1, priced_spans: 4, unpriced_token_spans: 0,
                             denominator: 4 } },
    freshness: { latest_work_activity_at: '2026-09-11T11:00:00+00:00',
                 absence_established: true },
    completeness: { has_any_recorded_work: true, workspace_state: 'populated',
                    scope_state: 'populated', absence_established: true,
                    completion_series_complete: true, job_breakdown_complete: true,
                    comparison_available: false, financial_available: true,
                    scope_membership_complete: true, counts_exact: true,
                    unavailable: [] },
    navigation: { account_id: 1, carry_query: {}, work_items_path: '/work/items' },
    ...over,
  }
}

export function findingFixture(over = {}) {
  return {
    id: 1, category: 'attention', claim_kind: 'observation',
    confidence: 'supported', title: 'Three invoice runs stopped',
    explanation: 'They each stopped at the same matching step.',
    entities: [{ kind: 'run', id: 41, label: 'Invoice 88213' }],
    next_step: { kind: 'review_runs', text: 'Open the invoices' },
    graphic: { kind: 'none' }, coverage: {}, state: 'open',
    evidence_count: 2, requires_financial: false,
    ...over,
  }
}

export function findingsFixture(findings = [findingFixture()], analysis = {}) {
  return {
    findings,
    analysis: { state: 'current', reason: null, analysis_outcome: 'complete',
                completion_gaps: [], enqueued: false,
                findings_from_previous_analysis: false,
                newer_evidence_available: false, ...analysis },
    scope: { requested: 'everyone', effective: 'everyone' },
    generated_at: '2026-09-11T12:00:00+00:00',
  }
}

/** The seat shape `seatOf` produces. */
export function seatFixture(over = {}) {
  return {
    breadth: 'company', depth: 'technical',
    surfaces: ['Home', 'Work', 'Fleet', 'Ask', 'Cost', 'Connect', 'Org'],
    org_builder: false, role_id: null, role_title: null,
    subtree_user_ids: [], visible_user_ids: null, can_edit_chart: false,
    ...over,
  }
}

export const ME = { user: { id: 1, name: 'Alex' }, org: { id: 1, name: 'Fixture Co' } }
