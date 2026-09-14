// Home's cost summary, MOUNTED and driven.
//
// The card went from a text box to a visual summary with a chart and a budget
// bar, which adds three ways to lie: a chart whose buckets disagree with the
// total above it, a monthly figure read as the period's, and a financial
// number rendered for somebody whose seat does not include Cost. Source-text
// assertions cannot catch any of those, so these render the real component.
//
// Nothing here stubs the card itself — the API is stubbed at the module
// boundary and the real HomeView, CostCard and DailySpendChart run.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  ME, deferred, findingsFixture, installDom, mount, seatFixture, snapshotFixture,
} from './mount.mjs'

installDom()

const React = await import('react')
const { api } = await import('../src/api.js')
const HomeView = (await import('../src/HomeView.jsx')).default
const { readFinancial, readCostSeries, readCostMonth } =
  await import('../src/homeView.js')
const { projectMonth } = await import('../src/costChart.jsx')

const h = React.createElement

/** A financial block with a real daily series and a real month. */
function financial(over = {}) {
  const points = [
    ['2026-09-05', 2], ['2026-09-06', 1], ['2026-09-07', 4],
    ['2026-09-08', 0.5], ['2026-09-09', 3], ['2026-09-10', 1],
    ['2026-09-11', 1],
  ].map(([d, v]) => ({
    bucket_start: `${d}T00:00:00+00:00`,
    bucket_start_utc: `${d}T00:00:00+00:00`,
    spend_usd: v,
    unpriced_token_spans: 0,
  }))
  const total = points.reduce((a, p) => a + p.spend_usd, 0)
  return {
    visible: true,
    scope: 'organization_wide',
    scope_note: 'Stored span cost is recorded per account and agent.',
    attributable_to_shown_work: false,
    currency: 'USD',
    spend_usd: total,
    period_start_utc: '2026-09-05T00:00:00+00:00',
    period_end_utc: '2026-09-11T12:00:00+00:00',
    coverage: { ratio: 1, priced_spans: 20, unpriced_token_spans: 0, denominator: 20 },
    daily: {
      available: true, bucket: 'local_day', timezone: 'UTC',
      points, total, aggregate_total: total, reconciles: true,
    },
    monthly: {
      available: true, window: 'utc_calendar_month', timezone: 'UTC',
      month_start_utc: '2026-09-01T00:00:00+00:00',
      month_to_date_usd: 40, budget_usd: 100, budget_source: 'account',
      budget_pct: 40, over_budget: false,
    },
    ...over,
  }
}

function stub(fin, over = {}) {
  api.getHomeSnapshot = () =>
    Promise.resolve(snapshotFixture({ financial: fin, ...over }))
  api.getHomeFindings = () => Promise.resolve(findingsFixture([]))
  api.getHomeFinding = () => Promise.resolve({
    finding: {}, claims: [], evidence: [], uncertainty: [], stale_evidence: [],
    navigation: { targets: [] },
  })
}

function home(props = {}) {
  return h(HomeView, {
    seat: seatFixture(), me: ME, people: [], active: true,
    onGoWork: () => {}, onOpenCost: () => {}, onOpenAgent: () => {},
    onOpenJob: () => {}, onOpenRun: () => {}, onConnectAgent: () => {},
    ...props,
  })
}

async function renderHome(fin, props = {}) {
  stub(fin)
  const m = await mount(home(props))
  await m.settle()
  return m
}

// ---------------------------------------------------------------------------
// Financial visibility
// ---------------------------------------------------------------------------

test('no financial surface means no figure from any of the new paths', async () => {
  // The series and the budget are money too. The seat gate has to cover all
  // three or the card leaks the total it was built to hide.
  const blind = seatFixture({
    surfaces: ['Home', 'Work', 'Fleet', 'Ask', 'Connect', 'Org'],
  })
  const m = await renderHome(financial(), { seat: blind })
  assert.equal(m.$('.hv-cost'), null, 'no cost card at all')
  assert.equal(m.$('.hv-cost-svg'), null, 'no chart')
  assert.equal(m.$('.hv-cost-bar'), null, 'no budget bar')
  assert.doesNotMatch(m.text(), /month to date/i)
  m.unmount()
})

test('the server withholding the block withholds every part of it', () => {
  // Server-side is the real gate; this pins that the reader agrees rather than
  // reconstructing a series from a block marked invisible.
  const fin = readFinancial({
    financial: { visible: false, unavailable_reason: 'seat_excludes_financial_surface' },
  })
  assert.equal(fin, null)
  assert.equal(readCostSeries(null), null)
  assert.equal(readCostMonth(null), null)
})

// ---------------------------------------------------------------------------
// The chart, and its reconciliation with the number above it
// ---------------------------------------------------------------------------

test('the chart renders from the server buckets and reads out its shape', async () => {
  const m = await renderHome(financial())
  assert.ok(m.$('.hv-cost-svg'), 'the chart is drawn')
  // Enough context to read without hovering: a summary card whose chart only
  // speaks through a tooltip is decoration on a touch screen.
  const legend = m.$('.hv-cost-chart-legend')
  assert.ok(legend, 'the legend is rendered')
  assert.match(legend.textContent, /Peak \$4\.00 on Sep 7/)
  assert.match(legend.textContent, /a day on average/)
  // And an accessible name that states the period, total and peak.
  const plot = m.$('.hv-cost-plot')
  assert.equal(plot.getAttribute('role'), 'img')
  assert.match(plot.getAttribute('aria-label'), /7 days, \$12\.50 total, peak \$4\.00/)
  m.unmount()
})

test('buckets that disagree with the total drop the chart, not the total', async () => {
  // The server asserts this rather than assuming it. When it reports a
  // mismatch, showing a chart that contradicts the headline is the worse of
  // the two failures, so the headline wins and the chart goes.
  const bad = financial()
  bad.daily = { ...bad.daily, reconciles: false }
  const m = await renderHome(bad)
  assert.ok(m.$('.hv-cost'), 'the card still renders')
  assert.match(m.$('.hv-cost-value').textContent, /\$12\.50/)
  assert.equal(m.$('.hv-cost-svg'), null, 'but no chart')
  m.unmount()
})

test('an unavailable series is simply absent — no invented zero days', async () => {
  const none = financial()
  none.daily = {
    available: false, unavailable_reason: 'too_many_cost_spans_to_bucket',
    points: [], total: null, aggregate_total: 12.5, reconciles: null,
  }
  const m = await renderHome(none)
  assert.equal(m.$('.hv-cost-svg'), null)
  assert.match(m.$('.hv-cost-value').textContent, /\$12\.50/)
  m.unmount()
})

test('arrow keys inspect a day without a pointer', async () => {
  const m = await renderHome(financial())
  const plot = m.$('.hv-cost-plot')
  const { act } = await import('react')
  await act(async () => {
    plot.dispatchEvent(new window.window.KeyboardEvent('keydown', {
      key: 'End', bubbles: true,
    }))
  })
  const tip = m.$('.hv-cost-tip')
  assert.ok(tip, 'a day is read out')
  assert.match(tip.textContent, /Sep 11/)
  m.unmount()
})

// ---------------------------------------------------------------------------
// Partial pricing coverage
// ---------------------------------------------------------------------------

test('material incompleteness stays visible in the reading flow', async () => {
  const partial = financial({
    coverage: { ratio: 0.62, priced_spans: 62, unpriced_token_spans: 38,
                denominator: 100 },
  })
  const m = await renderHome(partial)
  const cov = m.$('.hv-cost-cov')
  assert.ok(cov, 'the caveat is on the card, not only in a disclosure')
  assert.match(cov.textContent, /Some calls are unpriced/)
  assert.match(cov.textContent, /62% priced/)
  // The long methodology sits in a disclosure instead of the main flow.
  const about = m.$('.hv-cost-about')
  assert.ok(about, 'the explanation is a disclosure')
  assert.equal(about.tagName, 'DETAILS')
  assert.match(about.textContent, /unknown, not zero/)
  m.unmount()
})

test('fully priced spend carries no unpriced warning', async () => {
  const m = await renderHome(financial())
  assert.equal(m.$('.hv-cost-cov'), null)
  m.unmount()
})

test('a day of only unpriced calls is not drawn as a free day', async () => {
  const unpriced = financial()
  unpriced.spend_usd = 0
  unpriced.daily = {
    ...unpriced.daily,
    points: unpriced.daily.points.map((p) => ({
      ...p, spend_usd: 0, unpriced_token_spans: 3,
    })),
    total: 0, aggregate_total: 0,
  }
  const m = await renderHome(unpriced)
  assert.match(m.text(), /No priced spend recorded in this period/)
  assert.equal(m.$('.hv-cost-svg'), null, 'a flat zero line is not a chart')
  m.unmount()
})

// ---------------------------------------------------------------------------
// Budget context stays monthly
// ---------------------------------------------------------------------------

test('the budget bar is labelled month-to-date, never the selected period', async () => {
  const m = await renderHome(financial())
  const month = m.$('.hv-cost-month')
  assert.ok(month)
  assert.match(month.textContent, /month to date/)
  assert.match(month.textContent, /of \$100\.00 budget/)
  assert.match(month.textContent, /UTC calendar month, not the period above/)
  // The period figure above it is the 7-day one, and is labelled as such.
  assert.match(m.$('.hv-cost-label').textContent, /recorded spend · last 7 days/)
  m.unmount()
})

test('an over-budget month is shown with the existing warning treatment', async () => {
  const over = financial()
  over.monthly = { ...over.monthly, month_to_date_usd: 140, budget_pct: 140,
                   over_budget: true }
  const m = await renderHome(over)
  assert.ok(m.$('.hv-cost-month.over'), 'the over state is on the block')
  assert.ok(m.$('.hv-cost-bar-fill.over'), 'and on the bar')
  // Never color alone.
  assert.match(m.text(), /over budget/)
  // The bar is clamped, so a 140% fill cannot overflow its track.
  assert.equal(m.$('.hv-cost-bar-fill').style.width, '100%')
  m.unmount()
})

test('a budget nobody set draws no bar, and the month still reads', async () => {
  // The deployment default is not this org's decision; a bar against it would
  // read as a limit somebody chose.
  const dflt = financial()
  dflt.monthly = { ...dflt.monthly, budget_source: 'deployment_default' }
  const m = await renderHome(dflt)
  assert.equal(m.$('.hv-cost-bar'), null, 'no bar')
  assert.match(m.text(), /month to date/)
  assert.match(m.text(), /UTC calendar month/)
  m.unmount()
})

test('no monthly block at all omits the section rather than guessing', async () => {
  const none = financial()
  none.monthly = { available: false, unavailable_reason: 'not_collected' }
  const m = await renderHome(none)
  assert.equal(m.$('.hv-cost-month'), null)
  assert.ok(m.$('.hv-cost-value'), 'the period figure is unaffected')
  m.unmount()
})

test('the projection is the Cost page\'s own, and is labelled a projection', () => {
  // One definition, shared. A second copy would drift from the number on the
  // next screen.
  const at = new Date(Date.UTC(2026, 8, 10, 12))
  const p = projectMonth(40, at)
  assert.equal(p.dayOfMonth, 10)
  assert.equal(p.daysInMonth, 30)
  assert.equal(Math.round(p.projected), 120)
  // Never a number for an unknown month-to-date.
  assert.equal(projectMonth(null, at), null)
  assert.equal(projectMonth(undefined, at), null)
})

test('the projection is worded as an extrapolation on screen', async () => {
  const m = await renderHome(financial())
  assert.match(m.text(), /Projected \$[\d,.]+ by month end at this pace/)
  m.unmount()
})

// ---------------------------------------------------------------------------
// Scope honesty and navigation
// ---------------------------------------------------------------------------

test('organization-wide scope is visible, not hidden in a tooltip', async () => {
  const m = await renderHome(financial())
  const chip = m.$('.hv-cost-scope')
  assert.ok(chip, 'the scope is a rendered label')
  assert.equal(chip.textContent, 'Organization-wide')
  // And it is never described as the cost of the selected work.
  assert.doesNotMatch(m.text(), /what this selected work cost/i)
  m.unmount()
})

test('View costs navigates with the period, and says where it lands', async () => {
  const seen = []
  const m = await renderHome(financial(), { onOpenCost: (d) => seen.push(d) })
  const btn = m.$('.hv-cost-open')
  assert.ok(btn, 'the header carries the navigation')
  assert.equal(btn.textContent.trim(), 'View costs')
  await m.click(btn)
  assert.deepEqual(seen, [7], 'the selected period travels')
  assert.match(btn.getAttribute('title'), /Opens the Cost page on its 7-day window/)
  m.unmount()
})

test('a period the destination cannot show says so instead of claiming it', async () => {
  stub(financial())
  const base = snapshotFixture()
  api.getHomeSnapshot = () => Promise.resolve(snapshotFixture({
    financial: financial(),
    period: { ...base.period, days: 14 },
  }))
  const seen = []
  const m = await mount(home({ onOpenCost: (d) => seen.push(d) }))
  await m.settle()
  const btn = m.$('.hv-cost-open')
  assert.match(
    btn.getAttribute('title'),
    /shows 7, 30 or 90 UTC days, so it opens on 30 days rather than this view's 14/,
  )
  await m.click(btn)
  assert.deepEqual(seen, [14], 'Home sends its own period; the page clamps it')
  m.unmount()
})

// ---------------------------------------------------------------------------
// Period changes and stale responses
// ---------------------------------------------------------------------------

test('a slow response for an abandoned period never paints', async () => {
  // The classic financial staleness bug: the 7-day request lands after the
  // 30-day one and the reader sees last week's money under this month's label.
  const slow = deferred()
  let call = 0
  api.getHomeFindings = () => Promise.resolve(findingsFixture([]))
  api.getHomeFinding = () => Promise.resolve({
    finding: {}, claims: [], evidence: [], uncertainty: [], stale_evidence: [],
    navigation: { targets: [] },
  })
  const base = snapshotFixture()
  api.getHomeSnapshot = () => {
    call += 1
    if (call === 1) return slow.promise
    return Promise.resolve(snapshotFixture({
      financial: financial({ spend_usd: 99 }),
      period: { ...base.period, days: 30 },
    }))
  }
  const m = await mount(home())
  await m.settle()

  const { act } = await import('react')
  const lab = m.$$('label.hv-control').find((l) => l.textContent.includes('Period'))
  assert.ok(lab, 'the period control is rendered')
  const sel = lab.querySelector('select')
  sel.value = '30'
  await act(async () => {
    sel.dispatchEvent(new window.Event('change', { bubbles: true }))
  })
  await m.settle()
  assert.match(m.$('.hv-cost-value').textContent, /\$99\.00/)

  // Now the abandoned 7-day response arrives.
  await act(async () => {
    slow.resolve(snapshotFixture({ financial: financial({ spend_usd: 3 }) }))
    await slow.promise
  })
  await m.settle()
  assert.match(m.$('.hv-cost-value').textContent, /\$99\.00/,
               'the superseded figure never lands')
  m.unmount()
})
