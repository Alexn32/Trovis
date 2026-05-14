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
  ChevronDownIcon,
  ChevronRightIcon,
  ClipboardIcon,
  LightbulbIcon,
} from './Icons.jsx'

// Fleet view. The /agents response is now nested:
//   AgentGroup { service_name, agents: AgentInstance[], total_spans, ... }
// When a group has a single 'main' sub-agent we render a flat card (the
// pre-multi-agent UX). When it has multiple — or any non-'main' agent —
// we render a group card with an expandable sub-agent list. onSelectAgent
// is called with (serviceName, agentId?) so AgentDetail can scope its
// fetches via the ?agent_id= query param.

export default function Fleet({ onSelectAgent, onAddAgent }) {
  const [groups, setGroups] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  useEffect(() => {
    let cancelled = false
    api
      .listAgents()
      .then((data) => {
        if (!cancelled) {
          setGroups(data)
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

  // Headline counts are at the instance level so a multi-agent gateway
  // doesn't artificially inflate the "Total agents" stat. We also count
  // the true sub-agent total for a secondary label.
  const totalInstances = groups.length
  const totalSubAgents = groups.reduce(
    (a, g) => a + (g.agents?.length || 0),
    0,
  )
  const totalSpans = groups.reduce((a, g) => a + (g.total_spans || 0), 0)
  const totalErrors = groups.reduce((a, g) => a + (g.total_errors || 0), 0)
  const weightedAvgMs = (() => {
    const tot = groups.reduce((a, g) => a + (g.total_spans || 0), 0)
    if (!tot) return null
    const weighted = groups.reduce(
      (a, g) => a + (g.avg_duration_ms || 0) * (g.total_spans || 0),
      0,
    )
    return weighted / tot
  })()

  const statuses = groups.map((g) => statusFor(groupForStatus(g)))
  const healthy = statuses.filter((s) => s === 'green').length
  const degraded = statuses.filter((s) => s === 'yellow' || s === 'red').length

  return (
    <div className="view view-wide">
      <div>
        <FleetSummary
          counts={{
            total: totalInstances,
            subAgents: totalSubAgents,
            healthy,
            degraded,
            spans: totalSpans,
            errors: totalErrors,
            avgMs: weightedAvgMs,
          }}
        />
        <section className="agents-section">
          <div className="agents-section-header">
            <h2 className="section-label">
              Agents · {totalInstances}
              {totalSubAgents > totalInstances && (
                <span style={{ color: 'var(--text-dim)', fontWeight: 400 }}>
                  {' '}
                  ({totalSubAgents} sub-agents)
                </span>
              )}
            </h2>
          </div>
          <AgentList
            groups={groups}
            loading={loading}
            error={error}
            onSelectAgent={onSelectAgent}
            onAddAgent={onAddAgent}
          />
        </section>
      </div>
      <FleetActivityFeed groups={groups} />
    </div>
  )
}

// Adapter: the status/errorRate helpers in utils.js read `.span_count` and
// `.error_count` (the old flat shape). New groups use `total_spans` /
// `total_errors`; this projects a group into the legacy shape for those
// utilities. Avoids changing helpers used by AgentDetail too.
function groupForStatus(group) {
  return {
    span_count: group.total_spans,
    error_count: group.total_errors,
    last_seen: group.last_seen,
  }
}

function isFlatGroup(group) {
  const list = group.agents || []
  return list.length <= 1 && (list[0]?.agent_id ?? 'main') === 'main'
}

function FleetSummary({ counts }) {
  return (
    <div className="fleet-summary">
      <Stat
        label="Total agents"
        value={
          counts.subAgents > counts.total
            ? `${counts.total} (${counts.subAgents} sub)`
            : counts.total
        }
      />
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

function AgentList({ groups, loading, error, onSelectAgent, onAddAgent }) {
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
  if (groups.length === 0) {
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
      {groups.map((g) =>
        isFlatGroup(g) ? (
          <AgentCard
            key={g.service_name}
            group={g}
            onSelect={() => onSelectAgent(g.service_name)}
          />
        ) : (
          <GroupCard
            key={g.service_name}
            group={g}
            onSelectInstance={() => onSelectAgent(g.service_name)}
            onSelectSubAgent={(agentId) =>
              onSelectAgent(g.service_name, agentId)
            }
          />
        ),
      )}
    </div>
  )
}

// The flat single-agent card. Looks exactly like the pre-multi-agent
// version. Reads .total_spans/.total_errors off the group instead of the
// (now-removed) flat per-service span_count/error_count.
function AgentCard({ group, onSelect }) {
  const [sparkData, setSparkData] = useState(null)
  const status = statusFor(groupForStatus(group))
  const compat = groupForStatus(group)
  const errRate = errorRatePercent(compat)

  useEffect(() => {
    let cancelled = false
    api
      .getAgentSpans(group.service_name, 100)
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
  }, [group.service_name])

  return (
    <button
      type="button"
      className={`agent-card status-${status}`}
      onClick={onSelect}
    >
      <div className="agent-card-top">
        <div className="agent-card-title">
          <span className={`status-dot status-${status}`} />
          <span className="agent-name" title={group.service_name}>
            {group.display_name || group.service_name}
          </span>
          {group.display_name && (
            <span className="agent-name-secondary">{group.service_name}</span>
          )}
        </div>
        <Sparkline
          data={sparkData ?? []}
          color={statusColor(status)}
          width={100}
          height={28}
        />
      </div>

      {group.platform && <div className="agent-platform">{group.platform}</div>}

      <p className={`agent-description ${group.description ? '' : 'empty'}`}>
        {group.description ||
          'No description yet — auto-generated when telemetry includes registration data.'}
      </p>

      <div className="agent-card-stats">
        <div className="agent-stat">
          <span className="agent-stat-label">Spans</span>
          <span className="agent-stat-value">
            {group.total_spans.toLocaleString()}
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
            {formatDuration(group.avg_duration_ms)}
          </span>
        </div>
        <div className="agent-stat">
          <span className="agent-stat-label">Last seen</span>
          <span className="agent-stat-value">{relativeTime(group.last_seen)}</span>
        </div>
      </div>
    </button>
  )
}

// The multi-agent group card — instance-level header on top, expandable
// sub-agent list underneath. Clicking the header opens the instance
// aggregate view; clicking a sub-agent row opens AgentDetail scoped to
// that agent_id. The outer container is a <div> (not a button) because we
// can't nest interactive elements; each clickable region is its own
// <button>.
function GroupCard({ group, onSelectInstance, onSelectSubAgent }) {
  const [expanded, setExpanded] = useState(true)
  const [sparkData, setSparkData] = useState(null)
  const status = statusFor(groupForStatus(group))
  const compat = groupForStatus(group)
  const errRate = errorRatePercent(compat)

  useEffect(() => {
    let cancelled = false
    api
      .getAgentSpans(group.service_name, 100)
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
  }, [group.service_name])

  return (
    <div className={`agent-card agent-card-group status-${status}`}>
      <button
        type="button"
        className="agent-card-header-btn"
        onClick={onSelectInstance}
      >
        <div className="agent-card-top">
          <div className="agent-card-title">
            <span className={`status-dot status-${status}`} />
            <span className="agent-name" title={group.service_name}>
              {group.display_name || group.service_name}
            </span>
            {group.display_name && (
              <span className="agent-name-secondary">{group.service_name}</span>
            )}
            <span className="agent-sub-count">
              · {group.agents.length} agents
            </span>
          </div>
          <Sparkline
            data={sparkData ?? []}
            color={statusColor(status)}
            width={100}
            height={28}
          />
        </div>

        {group.platform && (
          <div className="agent-platform">{group.platform}</div>
        )}

        <p
          className={`agent-description ${group.description ? '' : 'empty'}`}
        >
          {group.description ||
            'No description yet — auto-generated when telemetry includes registration data.'}
        </p>

        <div className="agent-card-stats">
          <div className="agent-stat">
            <span className="agent-stat-label">Total spans</span>
            <span className="agent-stat-value">
              {group.total_spans.toLocaleString()}
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
              {formatDuration(group.avg_duration_ms)}
            </span>
          </div>
          <div className="agent-stat">
            <span className="agent-stat-label">Last seen</span>
            <span className="agent-stat-value">
              {relativeTime(group.last_seen)}
            </span>
          </div>
        </div>
      </button>

      <div className="agent-card-subagents">
        <button
          type="button"
          className="agent-card-expand"
          onClick={(e) => {
            e.stopPropagation()
            setExpanded((v) => !v)
          }}
          aria-expanded={expanded}
        >
          {expanded ? <ChevronDownIcon /> : <ChevronRightIcon />}
          <span>
            {expanded ? 'Hide' : 'Show'} {group.agents.length} agents
          </span>
        </button>
        {expanded && (
          <ul className="subagent-list">
            {group.agents.map((sa) => (
              <SubAgentRow
                key={sa.agent_id}
                subAgent={sa}
                onSelect={() => onSelectSubAgent(sa.agent_id)}
              />
            ))}
          </ul>
        )}
      </div>
    </div>
  )
}

function SubAgentRow({ subAgent, onSelect }) {
  const compat = {
    span_count: subAgent.span_count,
    error_count: subAgent.error_count,
    last_seen: subAgent.last_seen,
  }
  const status = statusFor(compat)
  const errRate = errorRatePercent(compat)
  return (
    <li>
      <button type="button" className="subagent-row" onClick={onSelect}>
        <span className={`status-dot status-${status}`} />
        <span className="subagent-id">
          {subAgent.display_name ? (
            <>
              {subAgent.display_name}{' '}
              <span className="subagent-id-raw mono">({subAgent.agent_id})</span>
            </>
          ) : (
            <span className="mono">{subAgent.agent_id}</span>
          )}
        </span>
        <span className="subagent-stat">
          {subAgent.span_count.toLocaleString()} spans
        </span>
        <span
          className={`subagent-stat ${
            errRate > 20 ? 'error' : errRate > 5 ? 'warn' : ''
          }`}
        >
          {errRate.toFixed(1)}% err
        </span>
        <span className="subagent-stat subagent-seen">
          {relativeTime(subAgent.last_seen)}
        </span>
      </button>
    </li>
  )
}

// ============================================================================
// Activity feed
// ----------------------------------------------------------------------------
// Same synthesis logic as before, but reading the nested shape. Events are
// still keyed by instance (service_name) — sub-agent granularity would
// noise the feed without much added value at this scale.
// ============================================================================

function FleetActivityFeed({ groups }) {
  const events = synthesizeFeed(groups)
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

function synthesizeFeed(groups) {
  const events = []

  for (const g of groups) {
    const compat = groupForStatus(g)
    const rate = errorRatePercent(compat)
    if (rate > 20) {
      events.push({
        type: 'alert',
        agent: g.service_name,
        at: g.last_seen,
        message: `Elevated error rate at ${rate.toFixed(1)}% across ${g.total_spans.toLocaleString()} spans.`,
      })
    }
    if (g.has_registration && g.description) {
      events.push({
        type: 'insight',
        agent: g.service_name,
        at: g.last_seen,
        message: 'Description generated from agent identity files.',
      })
    } else if (g.has_registration) {
      events.push({
        type: 'registration',
        agent: g.service_name,
        at: g.last_seen,
        message: 'Agent registration received.',
      })
    } else if (g.first_seen) {
      events.push({
        type: 'activity',
        agent: g.service_name,
        at: g.first_seen,
        message: 'First telemetry received from this agent.',
      })
    }
  }

  events.sort((x, y) => new Date(y.at).getTime() - new Date(x.at).getTime())
  return events.slice(0, 12)
}
