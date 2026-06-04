import { useEffect, useState } from 'react'
import { api } from './api.js'
import { ArrowLeftIcon } from './Icons.jsx'

// Dedicated cost page (overlay opened from the dashboard Cost card). Shows
// today (rolling 24h, matching Fleet), month-to-date vs. an editable org
// budget, a 30-day trend, a per-agent breakdown with editable monthly caps,
// and an org-wide by-model breakdown.

function fmtMoney(n) {
  const v = Number(n) || 0
  if (v === 0) return '$0.00'
  if (v < 0.01) return `$${v.toFixed(4)}`
  if (v < 1) return `$${v.toFixed(3)}`
  return `$${v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

function fmtTokens(n) {
  const v = Number(n) || 0
  if (v >= 1_000_000) return `${(v / 1_000_000).toFixed(1)}M`
  if (v >= 1_000) return `${(v / 1_000).toFixed(1)}K`
  return String(v)
}

export default function CostPage({ onBack, onOpenAgent }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [budgetInput, setBudgetInput] = useState('')
  const [savingBudget, setSavingBudget] = useState(false)

  useEffect(() => {
    let alive = true
    api
      .getCostOverview()
      .then((d) => {
        if (!alive) return
        setData(d)
        setBudgetInput(d.month_budget ? String(d.month_budget) : '')
      })
      .catch((e) => alive && setError(e.message || 'Could not load cost data'))
    return () => {
      alive = false
    }
  }, [])

  async function saveBudget() {
    const raw = budgetInput.trim()
    const val = raw === '' ? null : Number(raw)
    if (val != null && (Number.isNaN(val) || val < 0)) return
    setSavingBudget(true)
    try {
      const d = await api.setBudget(val)
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
      const d = await api.setAgentBudget(serviceName, 'main', cap)
      setData(d)
    } catch (e) {
      setError(e.message || 'Could not save cap')
    }
  }

  if (error) {
    return (
      <div className="dash costp">
        <button type="button" className="wf2-back" onClick={onBack}>
          <ArrowLeftIcon size={14} /> Dashboard
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

  return (
    <div className="dash costp">
      <button type="button" className="wf2-back" onClick={onBack}>
        <ArrowLeftIcon size={15} /> Dashboard
      </button>
      <h1 className="dash-hello" style={{ marginBottom: 4 }}>
        Cost
      </h1>

      {/* Summary row */}
      <div className="costp-summary">
        <div className="costp-sum-box">
          <span className="costp-sum-label">Today</span>
          <span className="costp-bignum">{fmtMoney(data.today)}</span>
          <span className="costp-sum-sub">rolling 24 hours</span>
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
      </div>

      {/* 30-day trend */}
      <div className="dash-card">
        <div className="dash-card-head">
          <span className="dash-section-title">Last 30 days</span>
        </div>
        <AreaChart data={data.daily} />
      </div>

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

function AreaChart({ data }) {
  const w = 720
  const h = 90
  const series = Array.isArray(data) ? data : []
  if (series.length < 2) return <div className="dash-empty">Not enough data yet.</div>
  const max = Math.max(...series, 0.0001)
  const pts = series.map((v, i) => [
    (i / (series.length - 1)) * w,
    h - (v / max) * (h - 8) - 4,
  ])
  const line = pts
    .map((p, i) => `${i === 0 ? 'M' : 'L'}${p[0].toFixed(1)},${p[1].toFixed(1)}`)
    .join(' ')
  const area = `${line} L${w},${h} L0,${h} Z`
  return (
    <svg
      className="costp-chart"
      viewBox={`0 0 ${w} ${h}`}
      preserveAspectRatio="none"
      aria-hidden="true"
    >
      <defs>
        <linearGradient id="costpFill" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="var(--dash-spark)" stopOpacity="0.18" />
          <stop offset="100%" stopColor="var(--dash-spark)" stopOpacity="0" />
        </linearGradient>
      </defs>
      <path d={area} fill="url(#costpFill)" />
      <path d={line} fill="none" stroke="var(--dash-spark)" strokeWidth="1.5" strokeOpacity="0.6" />
    </svg>
  )
}
