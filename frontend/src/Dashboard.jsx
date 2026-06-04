import { useEffect, useState } from 'react'
import { api } from './api.js'
import { AskVisualRenderer } from './AskVisuals.jsx'
// Sub-dollar costs render in cents (e.g. "2.6¢"); shared with Fleet so they match.
import { formatCost as fmtMoney } from './utils.js'
import {
  SparkleIcon,
  ChevronDownIcon,
  ChevronRightIcon,
  SendIcon,
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


export default function Dashboard({ onOpenAgent, onGoFleet, onOpenCost, userName }) {
  return (
    <div className="dash">
      <Greeting userName={userName} />
      <BriefingCard />
      <div className="dash-grid-2">
        <AttentionCard />
        <CostCard onOpenCost={onOpenCost} />
      </div>
      <WorkFeedCard onGoFleet={onGoFleet} />
      <FleetGrid onOpenAgent={onOpenAgent} onGoFleet={onGoFleet} />
      <AskPill />
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

function BriefingCard() {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let alive = true
    api
      .getBriefing()
      .then((d) => alive && setData(d))
      .catch(
        () =>
          alive &&
          setData({
            summary: '',
            tasks_yesterday: 0,
            tasks_last_week: 0,
            tasks_delta: '—',
          }),
      )
      .finally(() => alive && setLoading(false))
    return () => {
      alive = false
    }
  }, [])

  return (
    <div className="dash-card dash-briefing">
      <div className="dash-card-head">
        <span className="dash-sq">
          <SparkleIcon size={10} />
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

function AttentionCard() {
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
  }, [])

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

function CostCard({ onOpenCost }) {
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
  }, [])

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

function WorkFeedCard({ onGoFleet }) {
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
  }, [])

  return (
    <section className="dash-section">
      <div className="dash-section-head">
        <span className="dash-section-title">Work Feed</span>
        <button type="button" className="dash-link" onClick={onGoFleet}>
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

function FleetGrid({ onOpenAgent, onGoFleet }) {
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
  }, [])

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

// --- 6. Floating Ask pill + slide-up panel ---------------------------------

const BASE_SUGGESTIONS = [
  'Which agent is costing me the most per task?',
  'Show me error rates across all agents',
  'Which agents are idle?',
]

function AskPill() {
  const [open, setOpen] = useState(false)
  const [messages, setMessages] = useState([])
  const [pending, setPending] = useState(false)
  const [input, setInput] = useState('')
  const [suggestions, setSuggestions] = useState(BASE_SUGGESTIONS)

  // ⌘K / Ctrl+K toggles; Escape closes.
  useEffect(() => {
    function onKey(e) {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        setOpen((o) => !o)
      } else if (e.key === 'Escape') {
        setOpen(false)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  // Derive a couple of suggestions from current fleet state.
  useEffect(() => {
    let alive = true
    api
      .listAgents()
      .then((agents) => {
        if (!alive || !Array.isArray(agents) || agents.length === 0) return
        const worst = [...agents]
          .map((a) => ({
            name: a.display_name || a.service_name,
            rate: a.total_spans
              ? (a.total_errors || 0) / a.total_spans
              : 0,
          }))
          .sort((x, y) => y.rate - x.rate)[0]
        const extra = []
        if (worst && worst.rate > 0.02) extra.push(`Why is ${worst.name} failing?`)
        setSuggestions([...extra, ...BASE_SUGGESTIONS].slice(0, 5))
      })
      .catch(() => {})
    return () => {
      alive = false
    }
  }, [])

  async function send(text) {
    const q = (text ?? input).trim()
    if (!q || pending) return
    const next = [...messages, { role: 'user', content: q }]
    setMessages(next)
    setInput('')
    setPending(true)
    try {
      const r = await api.askDashboard(next)
      setMessages([...next, { role: 'assistant', content: r.answer, visual: r.visual || null }])
    } catch (e) {
      const msg = String(e?.message || '')
      setMessages([
        ...next,
        {
          role: 'assistant',
          content: msg.includes('503')
            ? 'AI is unavailable right now — the backend needs an ANTHROPIC_API_KEY.'
            : 'Something went wrong answering that. Please try again.',
        },
      ])
    } finally {
      setPending(false)
    }
  }

  if (!open) {
    return (
      <button type="button" className="dash-ask-pill" onClick={() => setOpen(true)}>
        <span className="dash-sq">
          <SparkleIcon size={10} />
        </span>
        <span className="dash-ask-pill-text">Ask about your fleet</span>
        <kbd className="dash-kbd">⌘K</kbd>
      </button>
    )
  }

  return (
    <div className="dash-ask-overlay" onClick={() => setOpen(false)}>
      <div className="dash-ask-panel" onClick={(e) => e.stopPropagation()}>
        <div className="dash-ask-head">
          <span className="dash-ask-title">
            <span className="dash-sq">
              <SparkleIcon size={10} />
            </span>
            Ask about your fleet
          </span>
          <button
            type="button"
            className="dash-ask-close"
            onClick={() => setOpen(false)}
            aria-label="Close"
          >
            ×
          </button>
        </div>

        <div className="dash-ask-body">
          {messages.length === 0 ? (
            <div className="dash-ask-empty">
              <p className="dash-ask-help">
                Ask anything about your agents, costs, errors, or performance.
              </p>
              <div className="dash-ask-suggest">
                {suggestions.map((s) => (
                  <button
                    key={s}
                    type="button"
                    className="dash-suggest-pill"
                    onClick={() => send(s)}
                  >
                    {s}
                  </button>
                ))}
              </div>
            </div>
          ) : (
            messages.map((m, i) => <Bubble key={i} m={m} />)
          )}
          {pending && (
            <div className="dash-ask-loading" aria-label="Thinking">
              <span />
              <span />
              <span />
            </div>
          )}
        </div>

        <form
          className="dash-ask-input-row"
          onSubmit={(e) => {
            e.preventDefault()
            send()
          }}
        >
          <div className="dash-ask-input-wrap">
            <input
              className="dash-ask-input"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder="Ask about your agents..."
              autoFocus
            />
            <button
              type="submit"
              className="dash-ask-send"
              disabled={!input.trim() || pending}
              aria-label="Send"
            >
              <SendIcon size={14} />
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}

function Bubble({ m }) {
  if (m.role === 'user') {
    return (
      <div className="dash-msg user">
        <div className="dash-bubble">{m.content}</div>
      </div>
    )
  }
  return (
    <div className="dash-msg ai">
      <div className="dash-bubble">
        <div className="dash-bubble-head">
          <span className="dash-sq sm">
            <SparkleIcon size={9} />
          </span>
          OVERSEE
        </div>
        {m.visual && <AskVisualRenderer visual={m.visual} />}
        <div className="dash-bubble-text">{m.content}</div>
      </div>
    </div>
  )
}
