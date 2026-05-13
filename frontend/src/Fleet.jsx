import { useEffect, useState } from 'react'
import { api } from './api.js'
import {
  bucketSpansForSparkline,
  formatDuration,
  relativeTime,
  statusFor,
  statusColor,
  errorRatePercent,
} from './utils.js'
import { Stat } from './ui.jsx'
import Sparkline from './Sparkline.jsx'
import {
  ActivityIcon,
  AlertIcon,
  ClipboardIcon,
  LightbulbIcon,
} from './Icons.jsx'

export default function Fleet({ onSelectAgent, onAddAgent }) {
  const [agents, setAgents] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  useEffect(() => {
    let cancelled = false
    api
      .listAgents()
      .then((data) => {
        if (!cancelled) {
          setAgents(data)
          setLoading(false)
        }
      })
      .catch((e) => {
        if (!cancelled) {
          setError(e.message)
          setLoading(false)
        }
      })
    return () => {
      cancelled = true
    }
  }, [])

  // Summary stats — derived from the totals the /agents endpoint already
  // returns. We don't have today-windowed counts yet, so labels say
  // "Total" rather than "Today". When the backend grows a daily-bucket
  // aggregate we can swap the labels and numbers.
  const totalAgents = agents.length
  const totalSpans = agents.reduce((a, b) => a + (b.span_count || 0), 0)
  const totalErrors = agents.reduce((a, b) => a + (b.error_count || 0), 0)
  const weightedAvgMs = (() => {
    const tot = agents.reduce((a, b) => a + (b.span_count || 0), 0)
    if (!tot) return null
    const weighted = agents.reduce(
      (a, b) => a + (b.avg_duration_ms || 0) * (b.span_count || 0),
      0,
    )
    return weighted / tot
  })()

  const statuses = agents.map(statusFor)
  const healthy = statuses.filter((s) => s === 'green').length
  const degraded = statuses.filter((s) => s === 'yellow' || s === 'red').length

  // Synthesized activity feed events — derived from existing data until we
  // have a real event log. See FleetActivityFeed for what we emit.

  return (
    <div className="view view-wide">
      <div>
        <FleetSummary
          counts={{
            total: totalAgents,
            healthy,
            degraded,
            spans: totalSpans,
            errors: totalErrors,
            avgMs: weightedAvgMs,
          }}
        />
        <section className="agents-section">
          <div className="agents-section-header">
            <h2 className="section-label">Agents · {totalAgents}</h2>
          </div>
          <AgentList
            agents={agents}
            loading={loading}
            error={error}
            onSelectAgent={onSelectAgent}
            onAddAgent={onAddAgent}
          />
        </section>
      </div>
      <FleetActivityFeed agents={agents} />
    </div>
  )
}

function FleetSummary({ counts }) {
  return (
    <div className="fleet-summary">
      <Stat label="Total agents" value={counts.total} />
      <Stat
        label="Healthy / degraded"
        value={`${counts.healthy} / ${counts.degraded}`}
        tone={counts.degraded > 0 ? 'warn' : undefined}
      />
      <Stat label="Total spans" value={counts.spans.toLocaleString()} />
      <Stat
        label="Total errors"
        value={counts.errors.toLocaleString()}
        tone={counts.errors > 0 ? 'error' : undefined}
      />
    </div>
  )
}

function AgentList({ agents, loading, error, onSelectAgent, onAddAgent }) {
  if (loading) {
    return <div className="state-card">Loading agents…</div>
  }
  if (error) {
    return (
      <div className="state-card error">
        <h2>Couldn't load agents</h2>
        <p>{error}</p>
      </div>
    )
  }
  if (agents.length === 0) {
    return (
      <div className="state-card">
        <h2>No agents yet</h2>
        <p style={{ marginBottom: 16 }}>
          Connect your first agent to start seeing telemetry here.
        </p>
        <button type="button" className="btn btn-primary" onClick={onAddAgent}>
          + Add Agent
        </button>
      </div>
    )
  }
  return (
    <div className="agents-grid">
      {agents.map((agent) => (
        <AgentCard
          key={agent.service_name}
          agent={agent}
          onSelect={() => onSelectAgent(agent.service_name)}
        />
      ))}
    </div>
  )
}

function AgentCard({ agent, onSelect }) {
  const [sparkData, setSparkData] = useState(null)
  const status = statusFor(agent)
  const errRate = errorRatePercent(agent)

  // Pull recent spans for this agent and bucket their timestamps so the
  // card sparkline is real data, not a placeholder. Fires per-card on
  // mount; fleet of N agents → N parallel calls, fine for the sizes we
  // care about. Falls back to a flat baseline if no spans / failure.
  useEffect(() => {
    let cancelled = false
    api
      .getAgentSpans(agent.service_name, 100)
      .then((spans) => {
        if (cancelled) return
        setSparkData(bucketSpansForSparkline(spans, 12))
      })
      .catch(() => {
        if (!cancelled) setSparkData([])
      })
    return () => {
      cancelled = true
    }
    // service_name is stable; refetch only on change.
  }, [agent.service_name])

  const platformTag = derivePlatform(agent)
  const modelTag = deriveModel(agent)

  return (
    <button
      type="button"
      className={`agent-card status-${status}`}
      onClick={onSelect}
    >
      <div className="agent-card-top">
        <div className="agent-card-title">
          <span className={`status-dot status-${status}`} />
          <span className="agent-name" title={agent.service_name}>
            {agent.service_name}
          </span>
        </div>
        <Sparkline
          data={sparkData ?? []}
          color={statusColor(status)}
          width={100}
          height={28}
        />
      </div>

      {(platformTag || modelTag) && (
        <div className="tag-row">
          {platformTag && <span className="tag">{platformTag}</span>}
          {modelTag && <span className="tag">{modelTag}</span>}
        </div>
      )}

      <p className={`agent-description ${agent.description ? '' : 'empty'}`}>
        {agent.description || 'No description yet — auto-generated when telemetry includes registration data.'}
      </p>

      <div className="agent-card-stats">
        <div className="agent-stat">
          <span className="agent-stat-label">Spans</span>
          <span className="agent-stat-value">
            {agent.span_count.toLocaleString()}
          </span>
        </div>
        <div className="agent-stat">
          <span className="agent-stat-label">Error rate</span>
          <span
            className={`agent-stat-value ${
              errRate > 20 ? 'error' : errRate > 5 ? 'warn' : ''
            }`}
          >
            {errRate.toFixed(1)}%
          </span>
        </div>
        <div className="agent-stat">
          <span className="agent-stat-label">Avg duration</span>
          <span className="agent-stat-value">
            {formatDuration(agent.avg_duration_ms)}
          </span>
        </div>
        <div className="agent-stat">
          <span className="agent-stat-label">Last seen</span>
          <span className="agent-stat-value">{relativeTime(agent.last_seen)}</span>
        </div>
      </div>
    </button>
  )
}

// Best-effort platform inference from top_operations. If we see span names
// that look like LLM-framework operations, tag them; otherwise omit. The
// goal is to NEVER hardcode an "OpenClaw-specific" assumption — we only
// recognize patterns that arrive in the data.
function derivePlatform(agent) {
  const ops = agent.top_operations || []
  if (ops.some((o) => o?.startsWith('agent_'))) return 'agent_*'
  if (ops.some((o) => o === 'agent_registration')) return 'registered'
  return ''
}

function deriveModel() {
  // No reliable model attribute on the /agents response shape. The
  // AgentDetail view pulls from /registration where it exists.
  return ''
}

// ============================================================================
// Activity feed
// ----------------------------------------------------------------------------
// We don't have a real event log yet, so we synthesize a feed from the
// agent data we already have:
//   - "registered"   : agents with has_registration=true (sorted by last_seen)
//   - "described"    : agents with a non-empty description
//   - "first_seen"   : the most recent first_seen across the fleet
//   - "alert"        : agents with error_rate > 20%
// Each event is plain English, agent-name in mono, and a relative time.
// ============================================================================

function FleetActivityFeed({ agents }) {
  const events = synthesizeFeed(agents)
  return (
    <aside className="activity-feed">
      <header className="activity-feed-header">
        <h3 className="section-label">Activity</h3>
      </header>
      {events.length === 0 ? (
        <div className="activity-empty">
          No activity yet. Send some telemetry to populate this feed.
        </div>
      ) : (
        <div>
          {events.map((e, i) => (
            <ActivityItem key={i} event={e} />
          ))}
        </div>
      )}
    </aside>
  )
}

function ActivityItem({ event }) {
  const Icon =
    event.type === 'alert'
      ? AlertIcon
      : event.type === 'insight'
        ? LightbulbIcon
        : event.type === 'registration'
          ? ClipboardIcon
          : ActivityIcon
  return (
    <div className="activity-item">
      <div className={`activity-icon ${event.type}`}>
        <Icon size={13} />
      </div>
      <div className="activity-body">
        <div className="activity-headline">
          <span className="activity-agent">{event.agent}</span>
          <span className="activity-time">{relativeTime(event.at)}</span>
        </div>
        <p className="activity-message">{event.message}</p>
      </div>
    </div>
  )
}

function synthesizeFeed(agents) {
  const events = []

  for (const a of agents) {
    const rate = errorRatePercent(a)
    if (rate > 20) {
      events.push({
        type: 'alert',
        agent: a.service_name,
        at: a.last_seen,
        message: `Elevated error rate at ${rate.toFixed(1)}% across ${a.span_count.toLocaleString()} spans.`,
      })
    }
    if (a.has_registration && a.description) {
      events.push({
        type: 'insight',
        agent: a.service_name,
        at: a.last_seen,
        message: 'Description generated from agent identity files.',
      })
    } else if (a.has_registration) {
      events.push({
        type: 'registration',
        agent: a.service_name,
        at: a.last_seen,
        message: 'Agent registration received.',
      })
    } else if (a.first_seen) {
      events.push({
        type: 'activity',
        agent: a.service_name,
        at: a.first_seen,
        message: 'First telemetry received from this agent.',
      })
    }
  }

  // Most recent first.
  events.sort((x, y) => new Date(y.at).getTime() - new Date(x.at).getTime())
  return events.slice(0, 12)
}
