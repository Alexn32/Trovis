import { useEffect, useState } from 'react'
import { api } from './api.js'
// Costs always render in dollars (e.g. "$0.68"); shared with Fleet so they match.
import { formatCost as fmtMoney } from './utils.js'
import {
  TrovisMark,
  ChevronDownIcon,
  ChevronRightIcon,
} from './Icons.jsx'

// ---------------------------------------------------------------------------
// Dashboard — the insight-driven daily briefing. Default landing page.
//
// Six sections, each fetching independently so a slow/failed Claude call on
// one card never blocks the page: greeting, AI briefing, attention + cost,
// work feed, fleet grid, and a floating ⌘K "Ask about your fleet" pill.
// All visuals key off CSS variables so the page works in light and dark.
// ---------------------------------------------------------------------------

// Status thresholds MUST mirror main.py's _agent_status: offline when no
// telemetry in 24h, degraded when error rate > 2%, else healthy.
function lastSeenAgeDays(iso) {
  if (!iso) return null
  const t = Date.parse(iso)
  if (Number.isNaN(t)) return null
  return (Date.now() - t) / 86400000
}

function deriveStatus(a) {
  const age = lastSeenAgeDays(a.last_seen)
  if (age === null || age > 1) return 'offline'
  const spans = a.total_spans || 0
  const errs = a.total_errors || 0
  const rate = spans ? (errs / spans) * 100 : 0
  return rate > 2 ? 'degraded' : 'healthy'
}

function fmtRel(iso) {
  const ms = Date.now() - Date.parse(iso)
  if (Number.isNaN(ms)) return ''
  const m = Math.floor(ms / 60000)
  if (m < 1) return 'just now'
  if (m < 60) return `${m}m ago`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ago`
  return `${Math.floor(h / 24)}d ago`
}


export default function Dashboard({ onOpenAgent, onGoFleet, onOpenCost, onViewAllWorkFeed, userName }) {
  // Silently re-sync every data card when the tab regains focus (throttled to
  // once per 30s). Cards keep their current data on screen while refetching —
  // no skeleton flash — so this is invisible until fresh numbers arrive. The
  // The global AskPill (App.jsx) is excluded so a conversation survives.
  const [refreshKey, setRefreshKey] = useState(0)
  useEffect(() => {
    let last = Date.now()
    function maybeRefresh() {
      if (document.hidden) return
      if (Date.now() - last > 30000) {
        last = Date.now()
        setRefreshKey((k) => k + 1)
      }
    }
    window.addEventListener('focus', maybeRefresh)
    document.addEventListener('visibilitychange', maybeRefresh)
    return () => {
      window.removeEventListener('focus', maybeRefresh)
      document.removeEventListener('visibilitychange', maybeRefresh)
    }
  }, [])

  // "Waiting for telemetry": right after onboarding the account has an agent
  // but no real activity yet (its only span is the registration). The briefing
  // counts are activity-only, so tasks_last_week === 0 + ≥1 agent means we
  // should show a warm placeholder instead of empty briefing/attention/cost/
  // work-feed cards. We fetch the briefing once here (the expensive Claude
  // call) and pass it down so BriefingCard doesn't refetch.
  const [briefing, setBriefing] = useState(null)
  const [briefingLoading, setBriefingLoading] = useState(true)
  const [hasAgents, setHasAgents] = useState(null)

  useEffect(() => {
    let alive = true
    setBriefingLoading(true)
    api
      .getBriefing()
      .then((d) => alive && setBriefing(d))
      .catch(
        () =>
          alive &&
          setBriefing({
            summary: '',
            tasks_yesterday: 0,
            tasks_last_week: 0,
            tasks_delta: '—',
          }),
      )
      .finally(() => alive && setBriefingLoading(false))
    api
      .listAgents()
      .then((d) => alive && setHasAgents(Array.isArray(d) && d.length > 0))
      .catch(() => alive && setHasAgents(false))
    return () => {
      alive = false
    }
  }, [refreshKey])

  const waiting =
    hasAgents === true &&
    briefing !== null &&
    (briefing.tasks_last_week || 0) === 0

  // While waiting, poll a little faster so the page resolves itself the moment
  // the first real spans land (reuses the shared refreshKey plumbing).
  useEffect(() => {
    if (!waiting) return
    const t = setInterval(() => setRefreshKey((k) => k + 1), 15000)
    return () => clearInterval(t)
  }, [waiting])

  return (
    <div className="dash">
      <Greeting userName={userName} />
      {waiting ? (
        <WaitingCard />
      ) : (
        <>
          <BriefingCard data={briefing} loading={briefingLoading} />
          <div className="dash-grid-2">
            <AttentionCard refreshKey={refreshKey} />
            <CostCard refreshKey={refreshKey} onOpenCost={onOpenCost} />
          </div>
          <WorkFeedCard refreshKey={refreshKey} onViewAll={onViewAllWorkFeed || onGoFleet} />
        </>
      )}
      <FleetGrid refreshKey={refreshKey} onOpenAgent={onOpenAgent} onGoFleet={onGoFleet} />
    </div>
  )
}

// Shown after onboarding while the first agent's telemetry hasn't arrived.
// Replaces the briefing/attention/cost/work-feed cards; the Fleet grid (which
// shows the connected agent) and the Ask pill stay. Auto-disappears once the
// briefing reports activity (the parent polls every 15s).
function WaitingCard() {
  return (
    <div className="dash-card dash-waiting">
      <div className="dash-waiting-pulse" aria-hidden="true">
        <span className="dash-sq">
          <TrovisMark size={11} />
        </span>
      </div>
      <h2 className="dash-waiting-title">Your first agent is connected</h2>
      <p className="dash-waiting-sub">
        Waiting for telemetry. This page fills in automatically the moment your
        agent sends its first activity — no refresh needed.
      </p>
      <ul className="dash-waiting-checklist">
        <li className="is-done">
          <span className="dash-wait-mark">✓</span> Agent connected
        </li>
        <li className="is-active">
          <span className="dash-wait-mark dot" /> First spans received
        </li>
        <li>
          <span className="dash-wait-mark dot" /> Dashboard populates
        </li>
      </ul>
    </div>
  )
}

// --- 1. Greeting -----------------------------------------------------------

function Greeting({ userName }) {
  const now = new Date()
  const h = now.getHours()
  const part = h < 12 ? 'morning' : h < 18 ? 'afternoon' : 'evening'
  const name = (userName || '').split(/[ @]/)[0] || 'there'
  const date = now.toLocaleDateString(undefined, {
    weekday: 'long',
    year: 'numeric',
    month: 'long',
    day: 'numeric',
  })
  return (
    <div className="dash-greeting">
      <h1 className="dash-hello">
        Good {part}, {name}
      </h1>
      <div className="dash-date">{date}</div>
    </div>
  )
}

// --- 2. Daily Briefing -----------------------------------------------------

function BriefingCard({ data, loading }) {
  return (
    <div className="dash-card dash-briefing">
      <div className="dash-card-head">
        <span className="dash-sq">
          <TrovisMark size={10} />
        </span>
        <span className="dash-briefing-label">Daily Briefing</span>
      </div>
      {loading ? (
        <div className="dash-skel">
          <span style={{ width: '92%' }} />
          <span style={{ width: '78%' }} />
        </div>
      ) : (
        <p className="dash-briefing-body">
          {data.summary || 'No activity to summarize yet.'}
        </p>
      )}
      {!loading && (
        <div className="dash-briefing-foot">
          <div className="dash-foot-stat">
            <span className="dash-foot-num">{data.tasks_yesterday}</span>
            <span className="dash-foot-lbl">tasks yesterday</span>
          </div>
          <div className="dash-foot-stat">
            <span className="dash-foot-num">{data.tasks_last_week}</span>
            <span className="dash-foot-lbl">this week</span>
          </div>
          <div className="dash-foot-stat">
            <span className="dash-foot-delta">{data.tasks_delta}</span>
            <span className="dash-foot-lbl">vs last week</span>
          </div>
        </div>
      )}
    </div>
  )
}

// --- 3a. Needs Attention ---------------------------------------------------

function AttentionCard({ refreshKey }) {
  const [items, setItems] = useState(null)
  const [openIdx, setOpenIdx] = useState(0)

  useEffect(() => {
    let alive = true
    api
      .getAttention()
      .then((d) => alive && setItems(Array.isArray(d) ? d : []))
      .catch(() => alive && setItems([]))
    return () => {
      alive = false
    }
  }, [refreshKey])

  const count = items?.length || 0
  return (
    <div className="dash-card dash-attention">
      <div className="dash-card-head spread">
        <span className="dash-section-title">Needs Attention</span>
        {count > 0 && <span className="dash-count-badge">{count}</span>}
      </div>
      {items === null ? (
        <div className="dash-skel">
          <span style={{ width: '100%' }} />
          <span style={{ width: '85%' }} />
        </div>
      ) : count === 0 ? (
        <div className="dash-empty">All clear — no agents need attention.</div>
      ) : (
        <div className="dash-att-list">
          {items.map((it, i) => (
            <AttentionRow
              key={`${it.agent}-${i}`}
              item={it}
              open={openIdx === i}
              onToggle={() => setOpenIdx(openIdx === i ? -1 : i)}
            />
          ))}
        </div>
      )}
    </div>
  )
}

function AttentionRow({ item, open, onToggle }) {
  return (
    <div className={`dash-att-row sev-${item.severity}`}>
      <button type="button" className="dash-att-head" onClick={onToggle}>
        <span className={`dash-sev-dot sev-${item.severity}`} />
        <span className="dash-att-agent">{item.agent}</span>
        <span className={`dash-sev-badge sev-${item.severity}`}>
          {item.severity}
        </span>
        <span className="dash-att-title">{item.title}</span>
        <span className="dash-att-chev">
          {open ? <ChevronDownIcon size={13} /> : <ChevronRightIcon size={13} />}
        </span>
      </button>
      {open && (
        <div className="dash-att-detail">
          {item.detail && <p className="dash-att-detail-text">{item.detail}</p>}
          {item.recommendation && (
            <div className="dash-rec">
              <span className="dash-rec-label">Recommendation</span>
              <span className="dash-rec-text">{item.recommendation}</span>
            </div>
          )}
          {(item.impact || item.last_seen) && (
            <div className="dash-att-meta">
              {item.impact}
              {item.impact && item.last_seen ? ' · ' : ''}
              {item.last_seen ? `last seen ${fmtRel(item.last_seen)}` : ''}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

// --- 3b. Cost Intelligence -------------------------------------------------

function CostCard({ onOpenCost, refreshKey }) {
  const [c, setC] = useState(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let alive = true
    api
      .getCost()
      .then((d) => alive && setC(d))
      .catch(() => alive && setC(null))
      .finally(() => alive && setLoading(false))
    return () => {
      alive = false
    }
  }, [refreshKey])

  const over = (c?.budget_pct || 0) > 85
  return (
    <div
      className={`dash-card dash-cost ${onOpenCost ? 'is-clickable' : ''}`}
      onClick={onOpenCost ? () => onOpenCost() : undefined}
      role={onOpenCost ? 'button' : undefined}
      tabIndex={onOpenCost ? 0 : undefined}
      onKeyDown={onOpenCost ? (e) => (e.key === 'Enter' || e.key === ' ') && onOpenCost() : undefined}
    >
      <div className="dash-card-head spread">
        <span className="dash-section-title">Cost</span>
        {onOpenCost && <span className="dash-cost-link">Details →</span>}
      </div>
      {loading ? (
        <div className="dash-skel">
          <span style={{ width: '60%', height: 24 }} />
          <span style={{ width: '90%' }} />
        </div>
      ) : !c ? (
        <div className="dash-empty">Cost data unavailable.</div>
      ) : (
        <>
          <div className="dash-cost-today">
            <span className="dash-bignum">{fmtMoney(c.today)}</span>
            <span className="dash-cost-today-lbl">today</span>
          </div>

          {c.month_budget > 0 && (
            <div className="dash-budget">
              <div className="dash-budget-row">
                <span className="dash-budget-text">
                  {fmtMoney(c.month_total)} / {fmtMoney(c.month_budget)} this month
                </span>
                <span
                  className={`dash-budget-pct ${over ? 'over' : 'ok'}`}
                >
                  {Math.round(c.budget_pct)}%
                </span>
              </div>
              <div className="dash-budget-bar">
                <div
                  className={`dash-budget-fill ${over ? 'over' : ''}`}
                  style={{ width: `${Math.min(100, c.budget_pct)}%` }}
                />
              </div>
            </div>
          )}

          <Sparkline data={c.daily} />

          {c.agents && c.agents.length > 0 && (
            <div className="dash-cost-agents">
              <div className="dash-caps">By Agent</div>
              {c.agents.map((a) => (
                <div key={a.name} className="dash-cost-agent-row">
                  <span className="dash-cost-agent-name">{a.name}</span>
                  <span className="dash-cost-agent-val">
                    {fmtMoney(a.cost)}
                    <TrendArrow trend={a.trend} />
                  </span>
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  )
}

function Sparkline({ data }) {
  const w = 180
  const h = 32
  if (!data || data.length < 2) return <svg width={w} height={h} className="dash-spark" />
  const max = Math.max(...data, 0.0001)
  const pts = data.map((v, i) => [
    (i / (data.length - 1)) * w,
    h - (v / max) * (h - 3) - 1.5,
  ])
  const line = pts
    .map((p, i) => `${i === 0 ? 'M' : 'L'}${p[0].toFixed(1)},${p[1].toFixed(1)}`)
    .join(' ')
  const area = `${line} L${w},${h} L0,${h} Z`
  return (
    <svg width={w} height={h} className="dash-spark" aria-hidden="true">
      <defs>
        <linearGradient id="dashSparkFill" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="var(--dash-spark)" stopOpacity="0.18" />
          <stop offset="100%" stopColor="var(--dash-spark)" stopOpacity="0" />
        </linearGradient>
      </defs>
      <path d={area} fill="url(#dashSparkFill)" />
      <path
        d={line}
        fill="none"
        stroke="var(--dash-spark)"
        strokeWidth="1"
        strokeOpacity="0.5"
      />
    </svg>
  )
}

function TrendArrow({ trend }) {
  if (trend === 'up')
    return (
      <svg className="dash-trend up" width="8" height="8" viewBox="0 0 8 8">
        <polygon points="4,0 8,8 0,8" />
      </svg>
    )
  if (trend === 'down')
    return (
      <svg className="dash-trend down" width="8" height="8" viewBox="0 0 8 8">
        <polygon points="0,0 8,0 4,8" />
      </svg>
    )
  return null
}

// --- 4. Work Feed ----------------------------------------------------------

function WorkFeedCard({ onViewAll, refreshKey }) {
  const [feed, setFeed] = useState(null)

  useEffect(() => {
    let alive = true
    api
      .getWorkFeed()
      .then((d) => alive && setFeed(Array.isArray(d) ? d : []))
      .catch(() => alive && setFeed([]))
    return () => {
      alive = false
    }
  }, [refreshKey])

  return (
    <section className="dash-section">
      <div className="dash-section-head">
        <span className="dash-section-title">Work Feed</span>
        <button type="button" className="dash-link" onClick={onViewAll}>
          View all →
        </button>
      </div>
      <div className="dash-card dash-feed">
        {feed === null ? (
          <div className="dash-skel pad">
            <span style={{ width: '70%' }} />
            <span style={{ width: '88%' }} />
          </div>
        ) : feed.length === 0 ? (
          <div className="dash-empty pad">
            No agent activity in the last 24 hours.
          </div>
        ) : (
          feed.map((f, i) => (
            <div key={`${f.agent}-${i}`} className="dash-feed-row">
              <div className="dash-feed-top">
                <span className="dash-feed-agent">{f.agent}</span>
                <span className="dash-dot-sep">·</span>
                <span className="dash-feed-time">{fmtRel(f.time)}</span>
                <span className="dash-feed-tasks">{f.tasks} tasks</span>
              </div>
              <div className="dash-feed-summary">{f.summary}</div>
            </div>
          ))
        )}
      </div>
    </section>
  )
}

// --- 5. Fleet Status grid --------------------------------------------------

function FleetGrid({ onOpenAgent, onGoFleet, refreshKey }) {
  const [agents, setAgents] = useState(null)

  useEffect(() => {
    let alive = true
    api
      .listAgents()
      .then((d) => alive && setAgents(Array.isArray(d) ? d : []))
      .catch(() => alive && setAgents([]))
    return () => {
      alive = false
    }
  }, [refreshKey])

  return (
    <section className="dash-section">
      <div className="dash-section-head">
        <span className="dash-section-title">Fleet</span>
        <button type="button" className="dash-link" onClick={onGoFleet}>
          Open Fleet →
        </button>
      </div>
      {agents === null ? (
        <div className="dash-skel pad">
          <span style={{ width: '100%' }} />
        </div>
      ) : agents.length === 0 ? (
        <div className="dash-card dash-empty pad">
          No agents reporting telemetry yet.
        </div>
      ) : (
        <div className="dash-fleet-grid">
          {agents.map((a) => {
            const status = deriveStatus(a)
            return (
              <button
                key={a.service_name}
                type="button"
                className={`dash-fleet-cell status-${status}`}
                onClick={() => onOpenAgent(a.service_name, 'main')}
                title={a.display_name || a.service_name}
              >
                <span className={`dash-status-dot status-${status}`} />
                <span className="dash-fleet-name">
                  {a.display_name || a.service_name}
                </span>
                <span className="dash-fleet-count">{a.total_spans || 0}</span>
              </button>
            )
          })}
        </div>
      )}
    </section>
  )
}
