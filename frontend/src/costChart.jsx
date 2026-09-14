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
 * A compact daily-spend chart: the Cost page's trend, sized for a summary card.
 *
 * Deliberately NOT a sparkline. A bare squiggle answers "is it going up" and
 * nothing else, so this keeps a real dollar axis, the first and last day, and
 * the total/peak read out beside it — enough to understand the shape without
 * opening a tooltip, which is the point of putting it on Home at all.
 *
 * `points` are the server's own buckets: `{ bucket_start, spend_usd,
 * unpriced_token_spans }`. Days the server did not send are not invented here
 * — a gap in coverage is not a day that cost nothing.
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

  let total = 0
  let peak = pts[0]
  for (const p of pts) {
    total += p.spend_usd || 0
    if ((p.spend_usd || 0) > (peak.spend_usd || 0)) peak = p
  }
  const unpricedDays = pts.filter((p) => (p.unpriced_token_spans || 0) > 0).length
  // A flat line at zero is noise, not a chart. Say the window is empty — and
  // say it differently when the zero is unpriced calls rather than no calls.
  if (total <= 0) {
    return (
      <p className="hv-cost-chart-none">
        {unpricedDays > 0
          ? 'No priced spend recorded in this period — some calls carry no stored price.'
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

  const line = pts
    .map((p, i) => `${i === 0 ? 'M' : 'L'}${xFor(i).toFixed(1)},${yFor(p.spend_usd).toFixed(1)}`)
    .join(' ')
  const area = `${line} L${xFor(n - 1).toFixed(1)},${padT + plotH} L${padL},${padT + plotH} Z`

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
  const flip = active && xFor(hover) > padL + plotW / 2
  const gid = `hvCostFill-${n}-${Math.round(top * 1000)}`

  return (
    <div className="hv-cost-chart">
      <div
        className="hv-cost-plot"
        ref={wrapRef}
        tabIndex={0}
        role="img"
        aria-label={`${label || 'Daily spend'}: ${n} days, ${fmtMoney(total)} total, peak ${fmtMoney(
          peak.spend_usd,
        )} on ${fmtDay(peak.bucket_start, { tz: timezone })}. Use the arrow keys to read a day.`}
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
          </defs>

          {[0, top].map((v) => (
            <g key={v}>
              <line className="hv-cost-grid" x1={padL} x2={padL + plotW}
                    y1={yFor(v)} y2={yFor(v)} />
              <text className="hv-cost-axis" x={padL - 8} y={yFor(v) + 3.5}
                    textAnchor="end">{fmtMoney(v)}</text>
            </g>
          ))}

          <path d={area} fill={`url(#${gid})`} />
          <path d={line} fill="none" stroke="var(--dash-spark)" strokeWidth="2"
                strokeLinejoin="round" strokeLinecap="round" />

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
              <circle cx={xFor(hover)} cy={yFor(active.spend_usd)} r="3.5"
                      fill="var(--dash-spark)" stroke="var(--bg-elevated)"
                      strokeWidth="2" />
            </g>
          )}
        </svg>

        {active && (
          <div
            className="hv-cost-tip"
            style={{
              left: `${xFor(hover)}px`,
              transform: `translateX(${flip ? 'calc(-100% - 10px)' : '10px'})`,
            }}
          >
            <span className="hv-cost-tip-val">{fmtMoney(active.spend_usd)}</span>
            <span className="hv-cost-tip-day">
              {fmtDay(active.bucket_start, { tz: timezone })}
            </span>
            {(active.unpriced_token_spans || 0) > 0 && (
              <span className="hv-cost-tip-sub">
                {active.unpriced_token_spans} unpriced
              </span>
            )}
          </div>
        )}
      </div>
      {/* Read the shape without hovering: a chart on a summary card that only
          speaks through a tooltip is decoration on a touch screen. */}
      <p className="hv-cost-chart-legend">
        Peak <b>{fmtMoney(peak.spend_usd)}</b> on {fmtDayShort(peak.bucket_start, timezone)}
        {' · '}
        <b>{fmtMoney(total / n)}</b> a day on average
      </p>
    </div>
  )
}
