// Generative-UI layer for the Dashboard Ask pill. Claude may return a
// `visual` block ({type, props}); AskVisualRenderer maps it to one of these
// pre-built, theme-aware components rendered above the text answer. All
// components read props defensively — missing/odd props never crash.

function statusClass(status) {
  if (status === 'degraded') return 'degraded'
  if (status === 'offline') return 'offline'
  return 'healthy'
}

// 1. Bar chart — compare a numeric value across agents.
export function BarChart({ title, value_label, value_suffix = '', data }) {
  const rows = Array.isArray(data) ? data : []
  const max = Math.max(...rows.map((d) => Number(d.value) || 0), 0.0001)
  return (
    <div className="ask-vis">
      {title && <div className="ask-vis-title">{title}</div>}
      {value_label && <div className="ask-vis-caption">{value_label}</div>}
      <div className="ask-vis-bars">
        {rows.map((d, i) => (
          <div key={i} className="ask-vis-bar-row">
            <span className="ask-vis-bar-label">{d.label}</span>
            <span className="ask-vis-bar-track">
              <span
                className={`ask-vis-bar-fill ${statusClass(d.status)}`}
                style={{ width: `${Math.max(2, ((Number(d.value) || 0) / max) * 100)}%` }}
              />
            </span>
            <span className="ask-vis-bar-value">
              {d.value}
              {value_suffix}
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}

// 2. Metric highlight — a single key number.
export function MetricHighlight({ label, value, detail, trend }) {
  const arrow = trend === 'down' ? '▾' : trend === 'up' ? '▴' : null
  const trendClass = trend === 'down' ? 'down' : trend === 'up' ? 'up' : ''
  return (
    <div className="ask-vis ask-vis-metric">
      {label && <div className="ask-vis-metric-label">{label}</div>}
      <div className="ask-vis-metric-value">{value}</div>
      {detail && (
        <div className="ask-vis-metric-detail">
          {arrow && <span className={`ask-vis-trend ${trendClass}`}>{arrow}</span>}
          {detail}
        </div>
      )}
    </div>
  )
}

// 3. Agent card — identity + stats for one agent.
export function AgentCard({ name, status, type, owner, description, stats }) {
  const s = stats && typeof stats === 'object' ? stats : {}
  const entries = Object.entries(s)
  const LABELS = {
    spans: 'Spans',
    error_rate: 'Error rate',
    avg_duration: 'Avg duration',
    last_seen: 'Last seen',
    cost_today: 'Cost today',
  }
  return (
    <div className="ask-vis-card">
      <div className="ask-vis-card-top">
        <span className={`ask-vis-dot ${statusClass(status)}`} />
        <span className="ask-vis-card-name">{name}</span>
        {type && <span className="ask-vis-badge">{type}</span>}
        {owner && <span className="ask-vis-owner">· {owner}</span>}
      </div>
      {description && <div className="ask-vis-card-desc">{description}</div>}
      {entries.length > 0 && (
        <div className="ask-vis-stat-grid">
          {entries.map(([k, v]) => (
            <div key={k} className="ask-vis-stat-box">
              <span className="ask-vis-stat-k">{LABELS[k] || k.replace(/_/g, ' ')}</span>
              <span className="ask-vis-stat-v">{String(v)}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// 4. Comparison table — agents side by side.
export function ComparisonTable({ title, agents }) {
  const list = Array.isArray(agents) ? agents : []
  const METRICS = [
    ['spans', 'Spans'],
    ['error_rate', 'Error rate'],
    ['avg_duration', 'Avg duration'],
    ['cost_today', 'Cost today'],
  ]
  const rows = METRICS.filter(([k]) => list.some((a) => a[k] != null))
  return (
    <div className="ask-vis">
      {title && <div className="ask-vis-title">{title}</div>}
      <div
        className="ask-vis-table"
        style={{ gridTemplateColumns: `auto repeat(${list.length}, 1fr)` }}
      >
        <div className="ask-vis-th" />
        {list.map((a, i) => (
          <div key={i} className="ask-vis-th ask-vis-th-agent">
            <span className={`ask-vis-dot ${statusClass(a.status)}`} />
            {a.name}
          </div>
        ))}
        {rows.map(([k, label]) => (
          <Row key={k} k={k} label={label} list={list} />
        ))}
      </div>
    </div>
  )
}

function Row({ k, label, list }) {
  return (
    <>
      <div className="ask-vis-rk">{label}</div>
      {list.map((a, i) => (
        <div key={i} className="ask-vis-rv">
          {a[k] != null ? String(a[k]) : '—'}
        </div>
      ))}
    </>
  )
}

// 5. Cost projection — before/after for "what if" questions.
export function CostProjection({
  title,
  current_daily,
  projected_daily,
  savings_daily,
  savings_monthly,
  note,
}) {
  const fmt = (n) => (n == null ? '—' : `$${Number(n).toFixed(2)}`)
  return (
    <div className="ask-vis">
      {title && <div className="ask-vis-title">{title}</div>}
      <div className="ask-vis-proj">
        <div className="ask-vis-proj-col">
          <span className="ask-vis-proj-k">Current</span>
          <span className="ask-vis-proj-v">{fmt(current_daily)}</span>
          <span className="ask-vis-proj-unit">/day</span>
        </div>
        <span className="ask-vis-proj-arrow">→</span>
        <div className="ask-vis-proj-col">
          <span className="ask-vis-proj-k">Projected</span>
          <span className="ask-vis-proj-v">{fmt(projected_daily)}</span>
          <span className="ask-vis-proj-unit">/day</span>
        </div>
      </div>
      {(savings_daily != null || savings_monthly != null) && (
        <div className="ask-vis-savings">
          Save {fmt(savings_daily)}/day
          {savings_monthly != null ? ` · ${fmt(savings_monthly)}/month` : ''}
        </div>
      )}
      {note && <div className="ask-vis-note">{note}</div>}
    </div>
  )
}

// 6. Fleet grid — a filtered set of agents (2-col for the narrow panel).
export function FleetGrid({ title, agents }) {
  const list = Array.isArray(agents) ? agents : []
  return (
    <div className="ask-vis">
      {title && <div className="ask-vis-title">{title}</div>}
      <div className="ask-vis-fleet">
        {list.map((a, i) => (
          <div key={i} className={`ask-vis-fleet-cell status-${statusClass(a.status)}`}>
            <span className={`ask-vis-dot ${statusClass(a.status)}`} />
            <span className="ask-vis-fleet-name">{a.name}</span>
            {a.last_seen && <span className="ask-vis-fleet-seen">{a.last_seen}</span>}
          </div>
        ))}
      </div>
    </div>
  )
}

// 7. Timeline — chronological events.
export function Timeline({ title, events }) {
  const list = (Array.isArray(events) ? events : []).slice(0, 8)
  return (
    <div className="ask-vis">
      {title && <div className="ask-vis-title">{title}</div>}
      <div className="ask-vis-timeline">
        {list.map((e, i) => (
          <div key={i} className="ask-vis-tl-row">
            <span className="ask-vis-tl-time">{e.time}</span>
            <span className={`ask-vis-tl-dot type-${e.type || 'info'}`} />
            <span className="ask-vis-tl-body">
              {e.agent && <span className="ask-vis-tl-agent">{e.agent}</span>}
              <span className="ask-vis-tl-event">{e.event}</span>
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}

// 8. Workflow summary — a compact process overview.
export function WorkflowSummary({ name, status, steps, agents, humans, stats }) {
  const s = stats && typeof stats === 'object' ? stats : {}
  const ags = Array.isArray(agents) ? agents : []
  const hus = Array.isArray(humans) ? humans : []
  const STAT_LABELS = {
    runs_24h: 'Runs 24h',
    success_rate: 'Success',
    avg_cycle: 'Avg cycle',
    escalation_rate: 'Escalation',
  }
  return (
    <div className="ask-vis-card">
      <div className="ask-vis-card-top">
        <span className="ask-vis-card-name">{name}</span>
        {status && (
          <span className={`ask-vis-wf-pill status-${statusClass(status)}`}>
            <span className={`ask-vis-dot ${statusClass(status)}`} />
            {statusClass(status) === 'degraded' ? 'Degraded' : 'Healthy'}
          </span>
        )}
        {steps != null && <span className="ask-vis-owner">· {steps} steps</span>}
      </div>
      {(ags.length > 0 || hus.length > 0) && (
        <div className="ask-vis-wf-parts">
          {ags.map((a, i) => (
            <span key={`a${i}`} className="ask-vis-part agent">
              <span className="ask-vis-dot healthy" />
              {a}
            </span>
          ))}
          {hus.map((h, i) => (
            <span key={`h${i}`} className="ask-vis-part human">
              👤 {h}
            </span>
          ))}
        </div>
      )}
      {Object.keys(s).length > 0 && (
        <div className="ask-vis-wf-stats">
          {Object.entries(s).map(([k, v]) => (
            <div key={k} className="ask-vis-wf-stat">
              <span className="ask-vis-stat-k">{STAT_LABELS[k] || k.replace(/_/g, ' ')}</span>
              <span className="ask-vis-stat-v">{String(v)}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

const VISUAL_COMPONENTS = {
  bar_chart: BarChart,
  metric_highlight: MetricHighlight,
  agent_card: AgentCard,
  comparison_table: ComparisonTable,
  cost_projection: CostProjection,
  fleet_grid: FleetGrid,
  timeline: Timeline,
  workflow_summary: WorkflowSummary,
}

export function AskVisualRenderer({ visual }) {
  if (!visual || !visual.type) return null
  const Component = VISUAL_COMPONENTS[visual.type]
  if (!Component) return null
  let body
  try {
    body = <Component {...(visual.props || {})} />
  } catch {
    return null // never let a malformed visual break the chat
  }
  return <div className="ask-vis-wrap">{body}</div>
}
