import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api } from './api.js'
import { ArrowLeftIcon } from './Icons.jsx'
import { formatCost as fmtMoney } from './utils.js'

const DAY_MS = 24 * 60 * 60 * 1000
const RANGES = [
  { days: 7, label: '7d' },
  { days: 30, label: '30d' },
  { days: 90, label: '90d' },
]

function agoLabel(ts) {
  if (!ts) return ''
  const s = Math.floor((Date.now() - ts) / 1000)
  if (s < 10) return 'just now'
  if (s < 60) return `${s}s ago`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ago`
  return `${Math.floor(m / 60)}h ago`
}

// Dedicated cost page (overlay opened from the dashboard Cost card). Shows
// today (UTC calendar day, matching the trend chart + console), month-to-date vs. an editable org
// budget, a 30-day trend, a per-agent breakdown with editable monthly caps,
// and an org-wide by-model breakdown. Costs render via the shared formatCost
// (always dollars, rounded to the nearest cent).

function fmtTokens(n) {
  const v = Number(n) || 0
  if (v >= 1_000_000) return `${(v / 1_000_000).toFixed(1)}M`
  if (v >= 1_000) return `${(v / 1_000).toFixed(1)}K`
  return String(v)
}

// "2026-09-08" → "Mon, Sep 8". Parsed as UTC so the label matches the UTC day
// the backend bucketed by, whatever the reader's timezone.
function fmtDay(iso, withYear = false) {
  if (!iso) return ''
  const d = new Date(`${iso}T00:00:00Z`)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleDateString(undefined, {
    timeZone: 'UTC',
    weekday: withYear ? undefined : 'short',
    month: 'short',
    day: 'numeric',
    year: withYear ? 'numeric' : undefined,
  })
}

function fmtDayShort(iso) {
  if (!iso) return ''
  const d = new Date(`${iso}T00:00:00Z`)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleDateString(undefined, {
    timeZone: 'UTC',
    month: 'short',
    day: 'numeric',
  })
}

// Axis ceiling: the next round number above the peak, so gridline labels land
// on whole dollars. The steps are deliberately fine (a 1/2/5 ladder would put a
// $64 peak on a $100 axis and squash the whole series into the bottom half).
const CEIL_STEPS = [1, 1.25, 1.5, 2, 2.5, 3, 4, 5, 6, 7, 8, 10]
function niceCeil(v) {
  if (!(v > 0)) return 1
  const base = 10 ** Math.floor(Math.log10(v))
  const n = v / base
  return (CEIL_STEPS.find((s) => n <= s) ?? 10) * base
}

export default function CostPage({ onBack, onOpenAgent }) {
  const [data, setData] = useState(null)
  const [audit, setAudit] = useState(null)
  const [error, setError] = useState(null)
  const [budgetInput, setBudgetInput] = useState('')
  const [savingBudget, setSavingBudget] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const [lastUpdated, setLastUpdated] = useState(null)
  const [range, setRange] = useState(30) // trend window in days (7 / 30 / 90)
  const [, forceTick] = useState(0) // keeps the "updated X ago" label live
  const rangeRef = useRef(range)
  rangeRef.current = range

  const load = useCallback(async () => {
    setRefreshing(true)
    try {
      const d = await api.getCostOverview(rangeRef.current)
      setData(d)
      setBudgetInput(d.month_budget ? String(d.month_budget) : '')
      setLastUpdated(Date.now())
      setError(null)
      // Non-blocking: flag any tokens that landed unpriced so an undercount
      // never reads as the truth. Failure here must not break the page.
      api.getCostAudit().then(setAudit).catch(() => {})
    } catch (e) {
      setError(e.message || 'Could not load cost data')
    } finally {
      setRefreshing(false)
    }
  }, [])

  // Initial load, a refetch whenever the trend window changes, and an
  // auto-refresh once a day while the page stays open.
  useEffect(() => {
    load()
    const id = setInterval(load, DAY_MS)
    return () => clearInterval(id)
  }, [load, range])

  // Re-render every 30s so the "updated X ago" label stays current.
  useEffect(() => {
    const id = setInterval(() => forceTick((t) => t + 1), 30000)
    return () => clearInterval(id)
  }, [])

  async function saveBudget() {
    const raw = budgetInput.trim()
    const val = raw === '' ? null : Number(raw)
    if (val != null && (Number.isNaN(val) || val < 0)) return
    setSavingBudget(true)
    try {
      const d = await api.setBudget(val, range)
      setData(d)
      setBudgetInput(d.month_budget ? String(d.month_budget) : '')
    } catch (e) {
      setError(e.message || 'Could not save budget')
    } finally {
      setSavingBudget(false)
    }
  }

  async function saveAgentCap(serviceName, cap) {
    try {
      const d = await api.setAgentBudget(serviceName, 'main', cap, range)
      setData(d)
    } catch (e) {
      setError(e.message || 'Could not save cap')
    }
  }

  // Only take over the page with an error before the first successful load —
  // a failed manual/daily refresh keeps the existing data on screen.
  if (error && !data) {
    return (
      <div className="dash costp">
        <button type="button" className="wf2-back" onClick={onBack}>
          <ArrowLeftIcon size={14} /> Home
        </button>
        <div className="dash-empty pad">{error}</div>
      </div>
    )
  }
  if (!data) {
    return (
      <div className="dash costp">
        <div className="dash-skel">
          <span style={{ height: 70 }} />
          <span style={{ height: 200 }} />
        </div>
      </div>
    )
  }

  const over = data.over_budget
  const pct = Math.round(data.budget_pct || 0)

  // Dated trend points. Older servers only send the bare `daily` cost array —
  // fall back to dating it backwards from today so the chart still works.
  const points = seriesFrom(data)
  // Straight-line month-end projection from the month-to-date burn.
  const now = new Date()
  const dayOfMonth = now.getUTCDate()
  const daysInMonth = new Date(
    Date.UTC(now.getUTCFullYear(), now.getUTCMonth() + 1, 0),
  ).getUTCDate()
  const projected = (data.month_total || 0) * (daysInMonth / dayOfMonth)
  const projOver = data.month_budget > 0 && projected > data.month_budget

  return (
    <div className="dash costp">
      <button type="button" className="wf2-back" onClick={onBack}>
        <ArrowLeftIcon size={15} /> Home
      </button>
      <div className="costp-titlerow">
        <h1 className="dash-hello" style={{ margin: 0 }}>
          Cost
        </h1>
        <div className="costp-refresh">
          {lastUpdated && (
            <span className="costp-updated">Updated {agoLabel(lastUpdated)}</span>
          )}
          <button
            type="button"
            className="btn btn-secondary btn-sm"
            onClick={load}
            disabled={refreshing}
          >
            {refreshing ? 'Refreshing…' : '↻ Refresh'}
          </button>
        </div>
      </div>

      {audit && audit.unpriced_token_total > 0 && (
        <div className="costp-unpriced-warn">
          <strong>Heads up — costs below are undercounted.</strong>{' '}
          {fmtTokens(audit.unpriced_token_total)} tokens from{' '}
          {audit.unpriced_models.length} model
          {audit.unpriced_models.length === 1 ? '' : 's'} aren’t priced yet
          {audit.unpriced_models[0]
            ? ` (${audit.unpriced_models
                .slice(0, 3)
                .map((m) => m.model)
                .join(', ')}${audit.unpriced_models.length > 3 ? '…' : ''})`
            : ''}
          . They’ll be priced automatically once the rate is known.
        </div>
      )}

      {/* Summary row */}
      <div className="costp-summary">
        <div className="costp-sum-box">
          <span className="costp-sum-label">Today</span>
          <span className="costp-bignum">{fmtMoney(data.today)}</span>
          <span className="costp-sum-sub">today (UTC)</span>
        </div>
        <div className="costp-sum-box">
          <span className="costp-sum-label">This month</span>
          <span className="costp-bignum">{fmtMoney(data.month_total)}</span>
          <span className={`costp-sum-sub ${over ? 'over' : ''}`}>
            {fmtMoney(data.month_budget)} budget · {pct}%{over ? ' · over budget' : ''}
          </span>
          <div className="costp-budget-bar">
            <div
              className={`costp-budget-fill ${over ? 'over' : ''}`}
              style={{ width: `${Math.min(100, data.budget_pct || 0)}%` }}
            />
          </div>
        </div>
        <div className="costp-sum-box">
          <span className="costp-sum-label">Monthly budget</span>
          <div className="costp-budget-edit">
            <span className="costp-dollar">$</span>
            <input
              className="wf2-input costp-budget-input"
              type="number"
              min="0"
              step="1"
              value={budgetInput}
              placeholder="No limit"
              onChange={(e) => setBudgetInput(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && saveBudget()}
            />
            <button
              type="button"
              className="btn btn-primary btn-sm"
              onClick={saveBudget}
              disabled={savingBudget}
            >
              Save
            </button>
          </div>
          <span className="costp-sum-sub">Drives the budget bar + over-budget warning.</span>
        </div>
        <div className="costp-sum-box">
          <span className="costp-sum-label">Projected month</span>
          <span className="costp-bignum">{fmtMoney(projected)}</span>
          <span className={`costp-sum-sub ${projOver ? 'over' : ''}`}>
            At today’s pace · day {dayOfMonth} of {daysInMonth}
            {projOver ? ' · will exceed budget' : ''}
          </span>
        </div>
      </div>

      {/* Trend */}
      <div className="dash-card costp-chart-card">
        <div className="dash-card-head spread">
          <span className="dash-section-title">
            Daily spend · last {range} days
          </span>
          <div className="costp-range" role="group" aria-label="Trend window">
            {RANGES.map((r) => (
              <button
                key={r.days}
                type="button"
                className={`costp-range-btn ${range === r.days ? 'on' : ''}`}
                aria-pressed={range === r.days}
                onClick={() => setRange(r.days)}
              >
                {r.label}
              </button>
            ))}
          </div>
        </div>
        <CostTrend points={points} dimmed={refreshing} />
      </div>

      {/* Breakdowns — side by side once there's width for them */}
      <div className="costp-tables">
      {/* By agent */}
      <div className="dash-card" style={{ padding: 0 }}>
        <div className="dash-card-head spread" style={{ padding: '14px 18px 0' }}>
          <span className="dash-section-title">By agent</span>
          <span className="dash-caps" style={{ margin: 0 }}>
            Set a monthly cap per agent
          </span>
        </div>
        <div className="costp-table">
          <div className="costp-thead">
            <span>Agent</span>
            <span className="num">Today</span>
            <span className="num">7d</span>
            <span className="num">This month</span>
            <span className="num">All-time</span>
            <span className="num">Monthly cap</span>
          </div>
          {data.agents.length === 0 && (
            <div className="dash-empty pad">No agent spend yet.</div>
          )}
          {data.agents.map((a) => (
            <AgentRow
              key={a.service_name}
              a={a}
              onSaveCap={saveAgentCap}
              onOpenAgent={onOpenAgent}
            />
          ))}
        </div>
      </div>

      {/* By model */}
      {data.by_model && data.by_model.length > 0 && (
        <div className="dash-card" style={{ padding: 0 }}>
          <div className="dash-card-head" style={{ padding: '14px 18px 0' }}>
            <span className="dash-section-title">By model</span>
          </div>
          <div className="costp-table model">
            <div className="costp-thead">
              <span>Model</span>
              <span className="num">Tokens</span>
              <span className="num">Cost (MTD)</span>
            </div>
            {data.by_model.map((m) => (
              <div key={m.model} className="costp-row model">
                <span className="costp-model mono">{m.model}</span>
                <span className="num">{fmtTokens(m.tokens)}</span>
                <span className="num strong">{fmtMoney(m.cost)}</span>
              </div>
            ))}
          </div>
        </div>
      )}
      </div>
    </div>
  )
}

function AgentRow({ a, onSaveCap, onOpenAgent }) {
  const [cap, setCap] = useState(a.monthly_cap != null ? String(a.monthly_cap) : '')

  // Re-sync if the server value changes (e.g. after another save).
  useEffect(() => {
    setCap(a.monthly_cap != null ? String(a.monthly_cap) : '')
  }, [a.monthly_cap])

  function commit() {
    const raw = cap.trim()
    const val = raw === '' ? null : Number(raw)
    if (val != null && (Number.isNaN(val) || val < 0)) return
    const current = a.monthly_cap == null ? null : a.monthly_cap
    if (val === current) return
    onSaveCap(a.service_name, val)
  }

  return (
    <div className={`costp-row ${a.over_cap ? 'over' : ''}`}>
      <span
        className="costp-agent"
        onClick={onOpenAgent ? () => onOpenAgent(a.service_name, 'main') : undefined}
        style={onOpenAgent ? { cursor: 'pointer' } : undefined}
      >
        <span className={`dash-status-dot status-${a.status}`} />
        {a.name}
        {a.over_cap && <span className="costp-over-badge">over cap</span>}
      </span>
      <span className="num">{fmtMoney(a.today)}</span>
      <span className="num">{fmtMoney(a.cost_7d)}</span>
      <span className="num strong">{fmtMoney(a.mtd)}</span>
      <span className="num">{fmtMoney(a.total)}</span>
      <span className="costp-cap-cell">
        <span className="costp-dollar">$</span>
        <input
          className="wf2-input costp-cap-input"
          type="number"
          min="0"
          step="1"
          value={cap}
          placeholder="—"
          onChange={(e) => setCap(e.target.value)}
          onBlur={commit}
          onKeyDown={(e) => e.key === 'Enter' && e.currentTarget.blur()}
        />
      </span>
    </div>
  )
}

// Dated points for the trend. Prefers the server's `series` (date + tokens);
// falls back to the bare `daily` cost array from older servers, dating it
// backwards from today in UTC.
function seriesFrom(data) {
  if (Array.isArray(data?.series) && data.series.length) return data.series
  const daily = Array.isArray(data?.daily) ? data.daily : []
  const today = new Date()
  return daily.map((cost, i) => {
    const d = new Date(
      Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), today.getUTCDate()),
    )
    d.setUTCDate(d.getUTCDate() - (daily.length - 1 - i))
    return { date: d.toISOString().slice(0, 10), cost: Number(cost) || 0, tokens: 0 }
  })
}

// Live element width, so the chart draws in real pixels (no viewBox stretching
// of stroke widths and type) and reflows with the full-width layout.
function useElementWidth() {
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

// Interactive daily-spend trend: area + line over a real dollar axis, with a
// crosshair that snaps to the nearest day on hover and on arrow-key focus, and
// a tooltip reading out that day's cost and tokens. A dashed line marks the
// window's daily average. Single series, so no legend — the card head names it.
function CostTrend({ points, dimmed }) {
  const [wrapRef, width] = useElementWidth()
  const [hover, setHover] = useState(null) // hovered/focused index

  const n = points.length
  const stats = useMemo(() => {
    if (!n) return { total: 0, avg: 0, peak: null }
    let total = 0
    let peak = points[0]
    for (const p of points) {
      total += p.cost || 0
      if ((p.cost || 0) > (peak.cost || 0)) peak = p
    }
    return { total, avg: total / n, peak }
  }, [points, n])

  if (n < 2) return <div className="dash-empty pad">Not enough data yet.</div>
  // A flat line at zero is noise, not a chart — say the window is empty.
  if (stats.total <= 0)
    return <div className="dash-empty pad">No spend recorded in this window.</div>

  const H = 232
  const padL = 56
  const padR = 18
  const padT = 16
  const padB = 28
  const w = Math.max(width || 0, 320)
  const plotW = Math.max(w - padL - padR, 40)
  const plotH = H - padT - padB

  const maxCost = Math.max(...points.map((p) => p.cost || 0), 0)
  const top = niceCeil(maxCost || 1)
  const xFor = (i) => padL + (i / (n - 1)) * plotW
  const yFor = (v) => padT + plotH - (Math.max(v, 0) / top) * plotH

  const line = points
    .map((p, i) => `${i === 0 ? 'M' : 'L'}${xFor(i).toFixed(1)},${yFor(p.cost).toFixed(1)}`)
    .join(' ')
  const area = `${line} L${xFor(n - 1).toFixed(1)},${padT + plotH} L${padL},${padT + plotH} Z`

  const gridVals = [0, top / 2, top]
  // Roughly one x label per 110px, always including the first and last day.
  const labelEvery = Math.max(1, Math.ceil(n / Math.max(2, Math.floor(plotW / 110))))
  const xLabels = points
    .map((p, i) => ({ p, i }))
    .filter(({ i }) => i === n - 1 || i % labelEvery === 0)
    .filter(({ i }) => i === n - 1 || xFor(n - 1) - xFor(i) > 46)

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
      e.preventDefault()
      setHover(0)
    } else if (e.key === 'End') {
      e.preventDefault()
      setHover(n - 1)
    } else if (e.key === 'Escape') {
      setHover(null)
    }
  }

  // Guard the index: switching the range swaps the series under a live hover.
  const active = hover != null && hover < n ? points[hover] : null
  // Keep the tooltip inside the card: it flips to the left of the crosshair
  // once the hovered day is past the halfway mark.
  const tipLeft = active ? xFor(hover) : 0
  const flip = active && tipLeft > padL + plotW / 2

  return (
    <div className="costp-trend">
      <div className="costp-trend-stats">
        <span>
          <b>{fmtMoney(stats.total)}</b> total
        </span>
        <span>
          <b>{fmtMoney(stats.avg)}</b> / day avg
        </span>
        {stats.peak && (
          <span>
            <b>{fmtMoney(stats.peak.cost)}</b> peak · {fmtDayShort(stats.peak.date)}
          </span>
        )}
        <span className="costp-trend-hint">Hover or use ← → to inspect a day</span>
      </div>

      <div
        className={`costp-plot ${dimmed ? 'dim' : ''}`}
        ref={wrapRef}
        tabIndex={0}
        role="img"
        aria-label={`Daily spend over the last ${n} days. Total ${fmtMoney(
          stats.total,
        )}, averaging ${fmtMoney(stats.avg)} per day.`}
        onKeyDown={onKeyDown}
        onBlur={() => setHover(null)}
      >
        <svg
          className="costp-chart"
          width={w}
          height={H}
          viewBox={`0 0 ${w} ${H}`}
          onPointerMove={(e) => setHover(indexFromEvent(e))}
          onPointerLeave={() => setHover(null)}
        >
          <defs>
            <linearGradient id="costpFill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="var(--dash-spark)" stopOpacity="0.22" />
              <stop offset="100%" stopColor="var(--dash-spark)" stopOpacity="0" />
            </linearGradient>
          </defs>

          {gridVals.map((v) => (
            <g key={v}>
              <line
                className="costp-grid"
                x1={padL}
                x2={padL + plotW}
                y1={yFor(v)}
                y2={yFor(v)}
              />
              <text className="costp-axis" x={padL - 10} y={yFor(v) + 3.5} textAnchor="end">
                {fmtMoney(v)}
              </text>
            </g>
          ))}

          {stats.avg > 0 && (
            <line
              className="costp-avgline"
              x1={padL}
              x2={padL + plotW}
              y1={yFor(stats.avg)}
              y2={yFor(stats.avg)}
            />
          )}

          <path d={area} fill="url(#costpFill)" />
          <path
            d={line}
            fill="none"
            stroke="var(--dash-spark)"
            strokeWidth="2"
            strokeLinejoin="round"
            strokeLinecap="round"
          />

          {xLabels.map(({ p, i }) => (
            <text
              key={p.date}
              className="costp-axis"
              x={xFor(i)}
              y={H - 8}
              textAnchor={i === 0 ? 'start' : i === n - 1 ? 'end' : 'middle'}
            >
              {fmtDayShort(p.date)}
            </text>
          ))}

          {active && (
            <g>
              <line
                className="costp-crosshair"
                x1={xFor(hover)}
                x2={xFor(hover)}
                y1={padT}
                y2={padT + plotH}
              />
              <circle
                cx={xFor(hover)}
                cy={yFor(active.cost)}
                r="4.5"
                fill="var(--dash-spark)"
                stroke="var(--bg-elevated)"
                strokeWidth="2"
              />
            </g>
          )}
        </svg>

        {active && (
          <div
            className="costp-tip"
            style={{
              left: `${tipLeft}px`,
              transform: `translateX(${flip ? 'calc(-100% - 12px)' : '12px'})`,
            }}
          >
            <span className="costp-tip-val">{fmtMoney(active.cost)}</span>
            <span className="costp-tip-day">{fmtDay(active.date)}</span>
            {active.tokens > 0 && (
              <span className="costp-tip-sub">{fmtTokens(active.tokens)} tokens</span>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
