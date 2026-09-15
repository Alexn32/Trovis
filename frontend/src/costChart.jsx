/**
 * The shared pieces of Trovis's cost visuals.
 *
 * The Cost page owns the full daily-spend trend. Home now shows a compact one,
 * and the two must not drift: the same dollar axis, the same day labels, the
 * same `--dash-spark` line over the same gradient fill, the same crosshair and
 * the same arrow-key inspection. So the primitives live here once and both
 * screens import them, rather than Home growing a second chart that looks
 * nearly right.
 *
 * Nothing here fetches or derives money. Every value rendered is one the
 * server computed from stored span cost and handed over; this file positions
 * and labels it.
 */
import { useEffect, useRef, useState } from 'react'
import { formatCost as fmtMoney } from './utils.js'

/** "2026-09-08" or an ISO instant → "Mon, Sep 8", read in `tz`. */
export function fmtDay(iso, { withYear = false, tz = 'UTC' } = {}) {
  if (!iso) return ''
  const d = new Date(iso.length <= 10 ? `${iso}T00:00:00Z` : iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleDateString(undefined, {
    timeZone: tz,
    weekday: withYear ? undefined : 'short',
    month: 'short',
    day: 'numeric',
    year: withYear ? 'numeric' : undefined,
  })
}

/** The same date, short: "Sep 8". */
export function fmtDayShort(iso, tz = 'UTC') {
  if (!iso) return ''
  const d = new Date(iso.length <= 10 ? `${iso}T00:00:00Z` : iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleDateString(undefined, {
    timeZone: tz, month: 'short', day: 'numeric',
  })
}

export function fmtTokens(n) {
  const v = Number(n) || 0
  if (v >= 1_000_000) return `${(v / 1_000_000).toFixed(1)}M`
  if (v >= 1_000) return `${(v / 1_000).toFixed(1)}K`
  return String(v)
}

// Axis ceiling: the next round number above the peak, so gridline labels land
// on whole dollars. The steps are deliberately fine (a 1/2/5 ladder would put a
// $64 peak on a $100 axis and squash the whole series into the bottom half).
const CEIL_STEPS = [1, 1.25, 1.5, 2, 2.5, 3, 4, 5, 6, 7, 8, 10]
export function niceCeil(v) {
  if (!(v > 0)) return 1
  const base = 10 ** Math.floor(Math.log10(v))
  const n = v / base
  return (CEIL_STEPS.find((s) => n <= s) ?? 10) * base
}

// Live element width, so the chart draws in real pixels (no viewBox stretching
// of stroke widths and type) and reflows with the layout around it.
export function useElementWidth() {
  const ref = useRef(null)
  const [width, setWidth] = useState(0)
  useEffect(() => {
    const el = ref.current
    if (!el) return undefined
    setWidth(el.getBoundingClientRect().width)
    if (typeof ResizeObserver === 'undefined') return undefined
    const ro = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect?.width
      if (w) setWidth(w)
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [])
  return [ref, width]
}

/**
 * Straight-line month-end projection from month-to-date burn.
 *
 * One definition, used by the Cost page's "Projected month" box and by Home's
 * budget line. It is an EXTRAPOLATION and both callers label it as one: it
 * assumes the rest of the month looks like the part already recorded, which is
 * the assumption, not a forecast.
 *
 * Returns null when the month-to-date figure is missing, so a caller cannot
 * render "$0.00 projected" for an unknown.
 */
export function projectMonth(monthToDate, now = new Date()) {
  if (typeof monthToDate !== 'number' || !Number.isFinite(monthToDate)) return null
  const dayOfMonth = now.getUTCDate()
  const daysInMonth = new Date(
    Date.UTC(now.getUTCFullYear(), now.getUTCMonth() + 1, 0),
  ).getUTCDate()
  if (!dayOfMonth) return null
  return {
    projected: monthToDate * (daysInMonth / dayOfMonth),
    dayOfMonth,
    daysInMonth,
  }
}

/**
 * What one bucket is, for drawing purposes.
 *
 *   'known'    every cost-bearing call that day carries a stored price, so the
 *              recorded figure is the day's cost.
 *   'partial'  money was recorded AND some calls carry no price. The recorded
 *              amount is real and is drawn; it is a FLOOR, and says so.
 *   'unknown'  nothing priced, but calls were made. The day's cost is unknown,
 *              NOT zero, and drawing it at zero is the lie this exists to stop.
 */
export function bucketKind(p) {
  const unpriced = p?.unpriced_token_spans || 0
  const spend = p?.spend_usd || 0
  if (unpriced > 0 && spend <= 0) return 'unknown'
  if (unpriced > 0) return 'partial'
  return 'known'
}

/** Contiguous runs of drawable days, split at every unknown day. */
function drawableSegments(kinds, n) {
  const out = []
  let run = []
  for (let i = 0; i < n; i += 1) {
    if (kinds[i] === 'unknown') {
      if (run.length) out.push(run)
      run = []
    } else {
      run.push(i)
    }
  }
  if (run.length) out.push(run)
  return out
}

/**
 * A compact daily-spend chart: the Cost page's trend, sized for a summary card.
 *
 * Deliberately NOT a sparkline. A bare squiggle answers "is it going up" and
 * nothing else, so this keeps a real dollar axis, the first and last day, and
 * the peak and average read out beside it — enough to understand the shape
 * without opening a tooltip, which is the point of putting it on Home at all.
 *
 * `points` are the server's own buckets: `{ bucket_start, spend_usd,
 * unpriced_token_spans }`. Days the server did not send are not invented here
 * — a gap in coverage is not a day that cost nothing.
 *
 * **A day whose calls carry no stored price is a GAP, not a zero.** Drawing
 * $10 · $0 · $10 as a V says the middle day was free; it says nothing of the
 * kind. The line and the fill break at such a day, a hatched band marks it,
 * and every read-out — tooltip, legend, accessible name — says "recorded"
 * rather than implying the total is complete. The recorded total itself is
 * untouched: nothing here estimates the missing money.
 */
export function DailySpendChart({
  points,
  timezone = 'UTC',
  height = 128,
  label,
  emptyNote = 'No spend recorded in this period.',
}) {
  const [wrapRef, width] = useElementWidth()
  const [hover, setHover] = useState(null)

  const pts = Array.isArray(points) ? points : []
  const n = pts.length
  if (n < 2) return <p className="hv-cost-chart-none">{emptyNote}</p>

  const kinds = pts.map(bucketKind)
  const unknownDays = kinds.filter((k) => k === 'unknown').length
  const partialDays = kinds.filter((k) => k === 'partial').length
  // Peak and average are over the days we actually recorded. Averaging an
  // unknown day in as zero would drag the figure down with a number nobody
  // measured.
  const recorded = pts.filter((p, i) => kinds[i] !== 'unknown')
  let total = 0
  let peak = recorded[0] || pts[0]
  for (const p of recorded) {
    total += p.spend_usd || 0
    if ((p.spend_usd || 0) > (peak.spend_usd || 0)) peak = p
  }
  // A flat line at zero is noise, not a chart. Say the window is empty — and
  // say it differently when the zero is unpriced calls rather than no calls.
  if (total <= 0) {
    return (
      <p className="hv-cost-chart-none">
        {unknownDays > 0 || partialDays > 0
          ? 'No priced spend recorded in this period — some calls carry no stored price, so this period’s cost is unknown rather than zero.'
          : emptyNote}
      </p>
    )
  }

  const padL = 46
  const padR = 10
  const padT = 10
  const padB = 20
  const w = Math.max(width || 0, 260)
  const plotW = Math.max(w - padL - padR, 40)
  const plotH = height - padT - padB

  const top = niceCeil(Math.max(...pts.map((p) => p.spend_usd || 0), 0) || 1)
  const xFor = (i) => padL + (i / (n - 1)) * plotW
  const yFor = (v) => padT + plotH - (Math.max(v, 0) / top) * plotH

  // One path per run of drawable days, so the stroke never crosses an unknown
  // day. A single-day run gets no stroke of its own — it is carried by its
  // marker below, since a zero-length path draws nothing.
  const segments = drawableSegments(kinds, n).map((run) => {
    const d = run
      .map((i, k) => `${k === 0 ? 'M' : 'L'}${xFor(i).toFixed(1)},${yFor(pts[i].spend_usd).toFixed(1)}`)
      .join(' ')
    const first = run[0]
    const last = run[run.length - 1]
    return {
      key: `${first}-${last}`,
      line: d,
      area: `${d} L${xFor(last).toFixed(1)},${padT + plotH} L${xFor(first).toFixed(1)},${padT + plotH} Z`,
      single: run.length === 1,
      x: xFor(first),
      y: yFor(pts[first].spend_usd),
    }
  })
  const unknownIdx = kinds
    .map((k, i) => (k === 'unknown' ? i : -1))
    .filter((i) => i >= 0)
  const partialIdx = kinds
    .map((k, i) => (k === 'partial' ? i : -1))
    .filter((i) => i >= 0)
  // Half a day either side, clamped to the plot, so a band at the first or
  // last bucket does not hang outside the axis.
  const halfStep = n > 1 ? plotW / (n - 1) / 2 : plotW / 2
  const bandFor = (i) => {
    const x0 = Math.max(padL, xFor(i) - halfStep)
    const x1 = Math.min(padL + plotW, xFor(i) + halfStep)
    return { x: x0, w: Math.max(x1 - x0, 2) }
  }

  const step = plotW / (n - 1)
  function indexFromEvent(e) {
    const rect = e.currentTarget.getBoundingClientRect()
    const x = e.clientX - rect.left - padL
    return Math.max(0, Math.min(n - 1, Math.round(x / step)))
  }
  function onKeyDown(e) {
    if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
      e.preventDefault()
      const dir = e.key === 'ArrowRight' ? 1 : -1
      setHover((h) => {
        const base = h == null ? (dir > 0 ? -1 : n) : h
        return Math.max(0, Math.min(n - 1, base + dir))
      })
    } else if (e.key === 'Home') {
      e.preventDefault(); setHover(0)
    } else if (e.key === 'End') {
      e.preventDefault(); setHover(n - 1)
    } else if (e.key === 'Escape') {
      setHover(null)
    }
  }

  const active = hover != null && hover < n ? pts[hover] : null
  const activeKind = hover != null && hover < n ? kinds[hover] : null
  const flip = active && xFor(hover) > padL + plotW / 2
  const uid = `${n}-${Math.round(top * 1000)}`
  const gid = `hvCostFill-${uid}`
  const hid = `hvCostGap-${uid}`

  // Every read-out says RECORDED. With an unknown day in the window the total
  // beneath this chart is a floor, and a legend reading "$12.50 total" beside
  // a gap would quietly contradict it.
  const incomplete = unknownDays > 0 || partialDays > 0
  const avgWord = incomplete ? 'a day across recorded days' : 'a day on average'
  const gapNote =
    unknownDays > 0
      ? `${unknownDays} ${unknownDays === 1 ? 'day has' : 'days have'} no priced calls — cost unknown, not zero.`
      : partialDays > 0
        ? `${partialDays} ${partialDays === 1 ? 'day is' : 'days are'} partly unpriced — those amounts are a floor.`
        : null

  return (
    <div className="hv-cost-chart">
      <div
        className="hv-cost-plot"
        ref={wrapRef}
        tabIndex={0}
        role="img"
        aria-label={[
          `${label || 'Daily spend'}: ${n} days.`,
          `${fmtMoney(total)} recorded across ${recorded.length} ${
            recorded.length === 1 ? 'day' : 'days'
          }, peak ${fmtMoney(peak.spend_usd)} on ${fmtDay(peak.bucket_start, { tz: timezone })}.`,
          unknownDays > 0
            ? `${unknownDays} ${unknownDays === 1 ? 'day has' : 'days have'} no priced calls, so ${
                unknownDays === 1 ? 'its' : 'their'
              } cost is unknown rather than zero and the line is broken there.`
            : '',
          partialDays > 0
            ? `${partialDays} ${partialDays === 1 ? 'day is' : 'days are'} only partly priced, so ${
                partialDays === 1 ? 'its' : 'their'
              } recorded amount is a floor.`
            : '',
          'Use the arrow keys to read a day.',
        ].filter(Boolean).join(' ')}
        onKeyDown={onKeyDown}
        onBlur={() => setHover(null)}
      >
        <svg
          className="hv-cost-svg"
          width={w}
          height={height}
          viewBox={`0 0 ${w} ${height}`}
          onPointerMove={(e) => setHover(indexFromEvent(e))}
          onPointerLeave={() => setHover(null)}
        >
          <defs>
            <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="var(--dash-spark)" stopOpacity="0.22" />
              <stop offset="100%" stopColor="var(--dash-spark)" stopOpacity="0" />
            </linearGradient>
            {/* Hatching, not a tint: the unknown band must read as unknown in
                a screenshot, in a printout and to anyone who cannot separate
                it from the fill by color. */}
            <pattern id={hid} width="6" height="6" patternUnits="userSpaceOnUse"
                     patternTransform="rotate(45)">
              <rect width="6" height="6" fill="transparent" />
              <line x1="0" y1="0" x2="0" y2="6" className="hv-cost-gap-hatch" />
            </pattern>
          </defs>

          {[0, top].map((v) => (
            <g key={v}>
              <line className="hv-cost-grid" x1={padL} x2={padL + plotW}
                    y1={yFor(v)} y2={yFor(v)} />
              <text className="hv-cost-axis" x={padL - 8} y={yFor(v) + 3.5}
                    textAnchor="end">{fmtMoney(v)}</text>
            </g>
          ))}

          {/* Unknown days, behind the series: a hatched band the full height of
              the plot. Nothing is drawn AT a value for these days, because
              there is no value. */}
          {unknownIdx.map((i) => {
            const b = bandFor(i)
            return (
              <g key={`gap-${i}`}>
                <rect className="hv-cost-gap" data-day={i} x={b.x} y={padT}
                      width={b.w} height={plotH} fill={`url(#${hid})`} />
                <text className="hv-cost-gap-mark" x={xFor(i)} y={padT + plotH / 2}
                      textAnchor="middle" dominantBaseline="middle">?</text>
              </g>
            )
          })}

          {segments.map((s) => (
            <g key={s.key}>
              {!s.single && <path d={s.area} fill={`url(#${gid})`} />}
              {!s.single ? (
                <path className="hv-cost-line" d={s.line} fill="none"
                      stroke="var(--dash-spark)" strokeWidth="2"
                      strokeLinejoin="round" strokeLinecap="round" />
              ) : (
                // A run of one has no line to draw; mark the value so an
                // island day between two gaps does not vanish.
                <circle className="hv-cost-lone" cx={s.x} cy={s.y} r="3"
                        fill="var(--dash-spark)" />
              )}
            </g>
          ))}

          {/* Partly-priced days keep their recorded value on the line and are
              ringed, so "this is a floor" is visible without a tooltip. */}
          {partialIdx.map((i) => (
            <circle key={`part-${i}`} className="hv-cost-partial" data-day={i}
                    cx={xFor(i)} cy={yFor(pts[i].spend_usd)} r="3.5"
                    fill="var(--bg-elevated)" stroke="var(--dash-warn)"
                    strokeWidth="2" />
          ))}

          {[0, n - 1].map((i) => (
            <text key={i} className="hv-cost-axis" x={xFor(i)} y={height - 6}
                  textAnchor={i === 0 ? 'start' : 'end'}>
              {fmtDayShort(pts[i].bucket_start, timezone)}
            </text>
          ))}

          {active && (
            <g>
              <line className="hv-cost-crosshair" x1={xFor(hover)} x2={xFor(hover)}
                    y1={padT} y2={padT + plotH} />
              {/* No dot on an unknown day: a dot sits at a value, and there
                  isn't one. */}
              {activeKind !== 'unknown' && (
                <circle cx={xFor(hover)} cy={yFor(active.spend_usd)} r="3.5"
                        fill="var(--dash-spark)" stroke="var(--bg-elevated)"
                        strokeWidth="2" />
              )}
            </g>
          )}
        </svg>

        {active && (
          <div
            className={`hv-cost-tip ${activeKind === 'unknown' ? 'unknown' : ''}`}
            style={{
              left: `${xFor(hover)}px`,
              transform: `translateX(${flip ? 'calc(-100% - 10px)' : '10px'})`,
            }}
          >
            <span className="hv-cost-tip-val">
              {activeKind === 'unknown'
                ? 'Unknown'
                : activeKind === 'partial'
                  ? `${fmtMoney(active.spend_usd)}+`
                  : fmtMoney(active.spend_usd)}
            </span>
            <span className="hv-cost-tip-day">
              {fmtDay(active.bucket_start, { tz: timezone })}
            </span>
            {activeKind === 'unknown' ? (
              <span className="hv-cost-tip-sub">
                {active.unpriced_token_spans} call
                {active.unpriced_token_spans === 1 ? '' : 's'} carry no stored
                price — not $0
              </span>
            ) : activeKind === 'partial' ? (
              <span className="hv-cost-tip-sub">
                recorded; {active.unpriced_token_spans} more unpriced
              </span>
            ) : null}
          </div>
        )}
      </div>
      {/* Read the shape without hovering: a chart on a summary card that only
          speaks through a tooltip is decoration on a touch screen. */}
      <p className="hv-cost-chart-legend">
        Peak recorded <b>{fmtMoney(peak.spend_usd)}</b> on{' '}
        {fmtDayShort(peak.bucket_start, timezone)}
        {' · '}
        <b>{fmtMoney(total / Math.max(recorded.length, 1))}</b> {avgWord}
      </p>
      {gapNote ? (
        <p className="hv-cost-chart-gapnote">
          <span className="hv-cost-gap-key" aria-hidden="true" />
          {gapNote}
        </p>
      ) : null}
    </div>
  )
}
