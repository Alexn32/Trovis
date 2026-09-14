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
  assert.match(legend.textContent, /Peak recorded \$4\.00 on Sep 7/)
  assert.match(legend.textContent, /a day on average/)
  // And an accessible name that states the period, recorded total and peak.
  const plot = m.$('.hv-cost-plot')
  assert.equal(plot.getAttribute('role'), 'img')
  assert.match(plot.getAttribute('aria-label'), /7 days\./)
  assert.match(plot.getAttribute('aria-label'),
               /\$12\.50 recorded across 7 days, peak \$4\.00/)
  // A fully priced window carries no gap note and no unknown band.
  assert.equal(m.$('.hv-cost-chart-gapnote'), null)
  assert.equal(m.$('.hv-cost-gap'), null)
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
// Mixed priced / unpriced days: an unknown day is a gap, not a zero
// ---------------------------------------------------------------------------

/** $10 · nothing priced · $10 — the reviewer's reproduction. */
function mixedFinancial() {
  const fin = financial()
  const day = (d, spend, unpriced) => ({
    bucket_start: `2026-09-0${d}T00:00:00+00:00`,
    bucket_start_utc: `2026-09-0${d}T00:00:00+00:00`,
    spend_usd: spend,
    unpriced_token_spans: unpriced,
  })
  const points = [day(5, 10, 0), day(6, 0, 5), day(7, 10, 0)]
  fin.spend_usd = 20
  fin.coverage = { ratio: 0.8, priced_spans: 20, unpriced_token_spans: 5,
                   denominator: 25 }
  fin.daily = {
    available: true, bucket: 'local_day', timezone: 'UTC',
    points, total: 20, aggregate_total: 20, reconciles: true,
  }
  return fin
}

test('an unpriced-only day breaks the line instead of drawing a free day', async () => {
  // $10, nothing priced, $10. Drawing that as a V says the middle day was
  // free. It says nothing of the kind, and this asserts on the RENDERED chart
  // rather than on caveat text.
  const m = await renderHome(mixedFinancial())

  // Nothing is drawn ACROSS the gap. Both surviving days are isolated, so
  // there is no stroke at all — and critically no V down to zero.
  assert.equal(m.$$('path.hv-cost-line').length, 0,
               'no line is drawn through the unknown day')
  assert.equal(m.$$('path[d*="L"]').filter((p) =>
    !p.classList.contains('hv-cost-grid')).length, 0,
               'no path interpolates between the two known days')

  const gap = m.$('rect.hv-cost-gap')
  assert.ok(gap, 'the unknown day is marked with an explicit band')
  assert.equal(gap.getAttribute('data-day'), '1', 'and it is the middle day')
  assert.ok(Number(gap.getAttribute('height')) > 0, 'the band spans the plot')

  // The band carries a text marker, not colour alone.
  assert.ok(m.$('text.hv-cost-gap-mark'), 'the gap is marked in text too')

  // Both real days are still drawn at their recorded value.
  const lone = m.$$('circle.hv-cost-lone')
  assert.equal(lone.length, 2, 'each island day keeps its recorded value')

  m.unmount()
})

test('a longer window draws each run of known days as its own segment', async () => {
  // The three-day reproduction leaves two isolated days. This is the same rule
  // where real multi-day runs exist: two strokes, neither crossing the gap.
  const fin = financial()
  const day = (d, spend, unpriced) => ({
    bucket_start: `2026-09-0${d}T00:00:00+00:00`,
    bucket_start_utc: `2026-09-0${d}T00:00:00+00:00`,
    spend_usd: spend,
    unpriced_token_spans: unpriced,
  })
  const points = [day(5, 10, 0), day(6, 8, 0), day(7, 0, 5),
                  day(8, 10, 0), day(9, 9, 0)]
  fin.spend_usd = 37
  fin.daily = { available: true, bucket: 'local_day', timezone: 'UTC',
                points, total: 37, aggregate_total: 37, reconciles: true }
  const m = await renderHome(fin)
  const lines = m.$$('path.hv-cost-line')
  assert.equal(lines.length, 2, 'two segments, split at the unknown day')
  // Each segment joins exactly two points — so neither spans the gap.
  for (const p of lines) {
    assert.equal(p.getAttribute('d').split('L').length, 2,
                 `a segment spans the gap: ${p.getAttribute('d')}`)
  }
  assert.equal(m.$$('rect.hv-cost-gap').length, 1)
  m.unmount()
})

test('the unknown day is described as unknown, never as zero', async () => {
  const m = await renderHome(mixedFinancial())

  // The legend says so without hovering.
  const note = m.$('.hv-cost-chart-gapnote')
  assert.ok(note, 'the gap is explained beside the chart')
  assert.match(note.textContent, /1 day has no priced calls/)
  assert.match(note.textContent, /unknown, not zero/)

  // The accessible description says so too.
  const plot = m.$('.hv-cost-plot')
  assert.match(plot.getAttribute('aria-label'), /recorded across 2 days/)
  assert.match(plot.getAttribute('aria-label'),
               /1 day has no priced calls, so its cost is unknown rather than zero/)
  assert.match(plot.getAttribute('aria-label'), /the line is broken there/)

  // Peak and average are over RECORDED days: averaging the unknown day in as
  // zero would report a number nobody measured ($6.67 rather than $10.00).
  const legend = m.$('.hv-cost-chart-legend')
  assert.match(legend.textContent, /Peak recorded \$10\.00/)
  assert.match(legend.textContent, /\$10\.00 a day across recorded days/)
  assert.doesNotMatch(legend.textContent, /\$6\.6/)

  // And the authoritative recorded total above is untouched — no estimate of
  // the missing money anywhere.
  assert.match(m.$('.hv-cost-value').textContent, /\$20\.00/)
  m.unmount()
})

test('the tooltip on an unknown day says unknown, not $0.00', async () => {
  const m = await renderHome(mixedFinancial())
  const plot = m.$('.hv-cost-plot')
  const { act } = await import('react')
  // Arrow to the middle day.
  for (const key of ['Home', 'ArrowRight']) {
    await act(async () => {
      plot.dispatchEvent(new window.window.KeyboardEvent('keydown', {
        key, bubbles: true,
      }))
    })
  }
  const tip = m.$('.hv-cost-tip')
  assert.ok(tip, 'the day is read out')
  assert.match(tip.textContent, /Unknown/)
  assert.match(tip.textContent, /5 calls carry no stored price/)
  assert.match(tip.textContent, /not \$0/)
  assert.doesNotMatch(tip.textContent, /^\$0\.00/)
  m.unmount()
})

test('a partly priced day keeps its recorded amount, qualified as a floor', async () => {
  // Recorded money on a day that also has unpriced calls is real and is drawn.
  // It is a FLOOR, and the chart says so without hiding the value.
  const fin = financial()
  fin.daily = {
    ...fin.daily,
    points: fin.daily.points.map((p, i) =>
      (i === 2 ? { ...p, unpriced_token_spans: 4 } : p)),
  }
  const m = await renderHome(fin)
  const ring = m.$('circle.hv-cost-partial')
  assert.ok(ring, 'the partly priced day is marked on the line')
  assert.equal(ring.getAttribute('data-day'), '2')
  // The line is NOT broken — the value is known, just not the whole of it.
  assert.equal(m.$$('path.hv-cost-line').length, 1)
  assert.equal(m.$('rect.hv-cost-gap'), null)

  const note = m.$('.hv-cost-chart-gapnote')
  assert.match(note.textContent, /1 day is partly unpriced/)
  assert.match(note.textContent, /a floor/)

  const { act } = await import('react')
  const plot = m.$('.hv-cost-plot')
  for (const key of ['Home', 'ArrowRight', 'ArrowRight']) {
    await act(async () => {
      plot.dispatchEvent(new window.window.KeyboardEvent('keydown', {
        key, bubbles: true,
      }))
    })
  }
  const tip = m.$('.hv-cost-tip')
  assert.match(tip.textContent, /\$4\.00\+/, 'the recorded value, marked as a floor')
  assert.match(tip.textContent, /4 more unpriced/)
  m.unmount()
})

// ---------------------------------------------------------------------------
// Partial pricing coverage
// ---------------------------------------------------------------------------

test('a rounded percentage cannot hide a missing price', async () => {
  // 999 priced, 1 unpriced: the ratio is 0.999, which rounds to 100. Deciding
  // the warning from that rounded number made the warning disappear exactly
  // where it was most likely to be missed.
  const nearly = financial({
    coverage: { ratio: 999 / 1000, priced_spans: 999, unpriced_token_spans: 1,
                denominator: 1000 },
  })
  const m = await renderHome(nearly)
  const cov = m.$('.hv-cost-cov')
  assert.ok(cov, 'the warning survives rounding')
  assert.match(cov.textContent, /Some calls are unpriced/)
  // Rounding is for display only, and never to a figure that contradicts it.
  assert.match(cov.textContent, />99% priced/)
  assert.doesNotMatch(cov.textContent, /100% priced/)
  m.unmount()
})

test('coverage the server could not establish is not read as complete', async () => {
  const unknown = financial({
    coverage: { ratio: null, priced_spans: 0, unpriced_token_spans: 0,
                denominator: 0, unavailable_reason: 'something_else' },
  })
  const m = await renderHome(unknown)
  assert.match(m.$('.hv-cost-cov').textContent, /coverage not established/)
  m.unmount()
})

test('a window with no cost-bearing calls at all is not a warning', async () => {
  // Nothing was spent and nothing is missing. "Some calls are unpriced" there
  // would be a warning about zero calls.
  const quiet = financial({
    coverage: { ratio: null, priced_spans: 0, unpriced_token_spans: 0,
                denominator: 0,
                unavailable_reason: 'no_cost_bearing_spans_in_period' },
  })
  const m = await renderHome(quiet)
  assert.equal(m.$('.hv-cost-cov'), null)
  m.unmount()
})

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

/** A fully priced 7-day period inside a month that also holds unpriced calls. */
function pricedWeekUnpricedMonth() {
  const fin = financial()
  // The period is spotless.
  fin.coverage = { ratio: 1, priced_spans: 20, unpriced_token_spans: 0,
                   denominator: 20 }
  // The month is not.
  fin.monthly = {
    ...fin.monthly,
    month_to_date_usd: 40,
    spend_is_recorded_only: true,
    coverage: { measure: 'priced_cost_bearing_spans', priced_spans: 80,
                unpriced_token_spans: 12, denominator: 92, ratio: 80 / 92,
                unavailable_reason: null },
    budget_pct: 40,
    budget_pct_is_floor: true,
    over_budget: false,
    as_of_utc: '2026-09-11T12:00:00+00:00',
  }
  return fin
}

test('a fully priced week inside a partly unpriced month still warns', async () => {
  // The period caveat at the foot of the card describes a DIFFERENT window.
  // Without its own coverage the month would draw an unqualified bar and read
  // as complete spend.
  const m = await renderHome(pricedWeekUnpricedMonth())

  // The period is clean, so the period caveat is correctly absent...
  assert.equal(m.$('.hv-cost-cov'), null, 'the period itself is fully priced')

  // ...and the month's own qualification is visible beside the month.
  const unknown = m.$('.hv-cost-month-unknown')
  assert.ok(unknown, 'the month carries its own incompleteness')
  assert.match(unknown.textContent, /12 calls this month carry no stored price/)
  assert.match(unknown.textContent, /actual spend is higher than recorded/)
  m.unmount()
})

test('an incomplete month reads as recorded, and its percentage as a floor', async () => {
  const m = await renderHome(pricedWeekUnpricedMonth())
  const month = m.$('.hv-cost-month')
  assert.match(month.textContent, /recorded month to date/)
  // A floor, not an actual: unknown money can only push it up.
  assert.match(m.$('.hv-cost-month-pct').textContent, /≥40%/)
  // The projection is explicitly of recorded priced spend only.
  assert.match(month.textContent, /from recorded priced spend only/)
  // And nothing anywhere says the org is safely under budget.
  assert.doesNotMatch(m.text(), /under budget/i)
  assert.doesNotMatch(m.text(), /within budget/i)
  m.unmount()
})

test('a fully priced month says nothing about recorded-only spend', async () => {
  const clean = financial()
  clean.monthly = {
    ...clean.monthly,
    coverage: { priced_spans: 92, unpriced_token_spans: 0, denominator: 92,
                ratio: 1, unavailable_reason: null },
    budget_pct_is_floor: false,
  }
  const m = await renderHome(clean)
  assert.equal(m.$('.hv-cost-month-unknown'), null)
  assert.match(m.$('.hv-cost-month-pct').textContent, /^40%/)
  assert.doesNotMatch(m.text(), /from recorded priced spend only/)
  m.unmount()
})

test('a month whose coverage is unknown is not treated as fully priced', async () => {
  const murky = financial()
  murky.monthly = { ...murky.monthly, coverage: null, budget_pct_is_floor: true }
  const m = await renderHome(murky)
  assert.match(m.$('.hv-cost-month-unknown').textContent,
               /coverage for this month could not be established/)
  m.unmount()
})

test('the projection is anchored to the snapshot, not the browser clock', async () => {
  // A page left open past the 1st would otherwise extrapolate last month's
  // burn across a month it has no data for.
  const stale = financial()
  stale.monthly = {
    ...stale.monthly,
    as_of_utc: '2026-09-10T12:00:00+00:00',   // day 10 of 30
    month_to_date_usd: 40,
  }
  const m = await renderHome(stale)
  // The inline " of $X budget" span shares this class; read them all.
  const note = m.$$('.hv-cost-month-note').map((n) => n.textContent).join(' ')
  assert.match(note, /day 10 of 30/,
               'the day comes from the snapshot, not from Date.now()')
  assert.match(note, /Projected \$120\.00 by month end/)
  m.unmount()
})

test('the progressbar value stays inside its declared range', async () => {
  const over = financial()
  over.monthly = { ...over.monthly, month_to_date_usd: 144, budget_pct: 144,
                   over_budget: true }
  const m = await renderHome(over)
  const bar = m.$('.hv-cost-bar')
  assert.equal(bar.getAttribute('aria-valuemin'), '0')
  assert.equal(bar.getAttribute('aria-valuemax'), '100')
  assert.equal(bar.getAttribute('aria-valuenow'), '100',
               'clamped — 144 against a max of 100 is an invalid state')
  // The real figure is still announced, and still visible.
  assert.match(bar.getAttribute('aria-valuetext'), /144 percent/)
  assert.match(bar.getAttribute('aria-valuetext'), /over budget/)
  assert.match(m.$('.hv-cost-month-pct').textContent, /144%/)
  m.unmount()
})

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
