import { useEffect, useState } from 'react'
import { api } from './api.js'
import {
  bucketSpansForSparkline,
  formatCost,
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
  LockIcon,
  TrashIcon,
} from './Icons.jsx'

// Fleet view. The /agents response is now nested:
//   AgentGroup { service_name, agents: AgentInstance[], total_spans, ... }
// When a group has a single 'main' sub-agent we render a flat card (the
// pre-multi-agent UX). When it has multiple — or any non-'main' agent —
// we render a group card with an expandable sub-agent list. onSelectAgent
// is called with (serviceName, agentId?) so AgentDetail can scope its
// fetches via the ?agent_id= query param.

export default function Fleet({ onSelectAgent, onAddAgent, onUpgrade }) {
  const [groups, setGroups] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [usage, setUsage] = useState(null) // {plan, agent_count, agent_limit, locked_count}

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
    api
      .getAccountUsage()
      .then((u) => !cancelled && setUsage(u))
      .catch(() => {})
    return () => {
      cancelled = true
    }
  }, [])

  // Optimistic local update on delete — drops the sub-agent from
  // its group, and if the group becomes empty (its last sub-agent
  // is gone) drops the whole group. Avoids a full /agents refetch
  // for a snappier interaction; the next mount sees the fresh
  // server state anyway.
  async function handleDeleteSubAgent(serviceName, agentId) {
    await api.deleteAgent(serviceName, agentId)
    setGroups((prev) =>
      prev
        .map((g) =>
          g.service_name === serviceName
            ? {
                ...g,
                agents: g.agents.filter((a) => a.agent_id !== agentId),
              }
            : g,
        )
        .filter((g) => g.agents.length > 0),
    )
  }

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
  const fleetCostToday = groups.reduce((a, g) => a + (g.cost_today || 0), 0)

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
            costToday: fleetCostToday,
          }}
          usage={usage}
          onUpgrade={onUpgrade}
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
            onDeleteSubAgent={handleDeleteSubAgent}
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

// Small cost line on a Fleet card. Prefers "today" when there's spend
// today; otherwise falls back to the 7-day figure so a card that ran
// yesterday still shows something. Renders nothing when the agent has
// no cost data at all (keeps cost-free agents visually clean).
function CardCostLine({ group }) {
  const today = group.cost_today || 0
  const week = group.cost_7d || 0
  if (today <= 0 && week <= 0) return null
  return (
    <div className="agent-cost-line">
      {today > 0
        ? `${formatCost(today)} today`
        : `${formatCost(week)} this week`}
    </div>
  )
}

function FleetSummary({ counts, usage, onUpgrade }) {
  // Plan usage: "used of limit" agents, with a calm upgrade nudge when some are
  // locked. Unlimited plans show just the count.
  const agentsValue =
    usage && usage.agent_limit != null
      ? `${usage.agent_count} of ${usage.agent_limit}`
      : counts.subAgents > counts.total
        ? `${counts.total} (${counts.subAgents} sub)`
        : counts.total
  const overLimit = usage && usage.locked_count > 0
  return (
    <div className="fleet-summary">
      <Stat
        label="Agents"
        value={agentsValue}
        tone={overLimit ? 'warn' : undefined}
        sub={
          overLimit ? (
            <button type="button" className="fleet-upgrade-link" onClick={onUpgrade}>
              {usage.locked_count} recording · upgrade to view
            </button>
          ) : undefined
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
      <Stat label="Fleet cost today" value={formatCost(counts.costToday || 0)} />
    </div>
  )
}

function AgentList({ groups, loading, error, onSelectAgent, onAddAgent, onDeleteSubAgent }) {
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
            onDeleteSubAgent={(agentId) =>
              onDeleteSubAgent(g.service_name, agentId)
            }
          />
        ),
      )}
    </div>
  )
}

// ----------------------------------------------------------------------------
// FleetCard — the one card body used for BOTH a flat single-agent instance and
// a sub-agent inside an instance group. Driven by normalized props so a
// sub-agent (agent_id-scoped) renders identically to a standalone agent — just
// nested under its instance band. Owns its own sparkline fetch (scoped to
// service_name + optional agentId). When `onDelete` is passed (sub-agents), an
// inline delete control overlays the card corner.
// ----------------------------------------------------------------------------
function FleetCard({
  name,
  secondary,
  titleText,
  status,
  errRate,
  spans,
  avgMs,
  lastSeen,
  costToday,
  cost7d,
  description,
  platform,
  ownerName,
  ownerRole,
  locked,
  lockedLine,
  serviceName,
  agentId,
  onSelect,
  onDelete,
  deleteLabel,
}) {
  const [sparkData, setSparkData] = useState(null)

  // Hook runs unconditionally (before any conditional render) so hook order
  // stays stable if `locked` flips on a live re-render. Locked cards skip the
  // fetch. Sparkline is scoped to (service, agentId) so a sub-agent shows its
  // own trend, not the whole instance's.
  useEffect(() => {
    if (locked) return undefined
    let cancelled = false
    api
      .getAgentSpans(serviceName, 100, agentId)
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
  }, [serviceName, agentId, locked])

  const card = locked ? (
    <button type="button" className="agent-card locked" onClick={onSelect}>
      <div className="agent-card-title">
        <span className="agent-lock"><LockIcon size={13} /></span>
        <span className="agent-name" title={titleText}>{name}</span>
        {secondary && <span className="agent-name-secondary">{secondary}</span>}
      </div>
      <p className="agent-locked-line">{lockedLine || 'Recording — upgrade to view'}</p>
    </button>
  ) : (
    <button
      type="button"
      className={`agent-card status-${status}`}
      onClick={onSelect}
    >
      <div className="agent-card-top">
        <div className="agent-card-title">
          <span className={`status-dot status-${status}`} />
          <span className="agent-name" title={titleText}>{name}</span>
          {secondary && <span className="agent-name-secondary">{secondary}</span>}
        </div>
        <Sparkline
          data={sparkData ?? []}
          color={statusColor(status)}
          width={100}
          height={28}
        />
      </div>

      {platform && <div className="agent-platform">{platform}</div>}
      {ownerName && (
        <div className="owner-tag">
          Owner: <strong>{ownerName}</strong>
          {ownerRole && <span className="owner-tag-role"> · {ownerRole}</span>}
        </div>
      )}

      <p className={`agent-description ${description ? '' : 'empty'}`}>
        {description ||
          'No description yet — auto-generated when telemetry includes registration data.'}
      </p>

      <CardCostLine group={{ cost_today: costToday, cost_7d: cost7d }} />

      <div className="agent-card-stats">
        <div className="agent-stat">
          <span className="agent-stat-label">Spans</span>
          <span className="agent-stat-value">{(spans || 0).toLocaleString()}</span>
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
          <span className="agent-stat-value">{formatDuration(avgMs)}</span>
        </div>
        <div className="agent-stat">
          <span className="agent-stat-label">Last seen</span>
          <span className="agent-stat-value">{relativeTime(lastSeen)}</span>
        </div>
      </div>
    </button>
  )

  // No delete → return the plain card. With delete → wrap so the delete
  // control can overlay the corner (can't nest a <button> inside the card
  // <button>, so it's a sibling in a positioned wrapper).
  if (!onDelete) return card
  return (
    <div className="fleet-card-wrap">
      {card}
      <CardDelete label={deleteLabel} onDelete={onDelete} />
    </div>
  )
}

// Inline delete control (trash → Yes / Cancel), extracted from the old
// SubAgentRow so any card can carry it. stopPropagation everywhere so a
// click never falls through to the card's onSelect.
function CardDelete({ label, onDelete }) {
  const [confirming, setConfirming] = useState(false)
  const [deleting, setDeleting] = useState(false)

  async function handleConfirm(e) {
    e.stopPropagation()
    setDeleting(true)
    try {
      await onDelete()
      // Parent drops this card from state, unmounting us — no cleanup needed.
    } catch (err) {
      console.warn('[Fleet] delete failed:', err)
      setDeleting(false)
      setConfirming(false)
    }
  }

  if (confirming) {
    return (
      <span className="card-delete-confirm" onClick={(e) => e.stopPropagation()}>
        <button
          type="button"
          className="btn-link-inline danger"
          onClick={handleConfirm}
          disabled={deleting}
        >
          {deleting ? 'Deleting…' : 'Yes, delete'}
        </button>
        <button
          type="button"
          className="btn-link-inline"
          onClick={(e) => {
            e.stopPropagation()
            setConfirming(false)
          }}
          disabled={deleting}
        >
          Cancel
        </button>
      </span>
    )
  }
  return (
    <button
      type="button"
      className="card-delete-btn"
      onClick={(e) => {
        e.stopPropagation()
        setConfirming(true)
      }}
      aria-label={label}
      title={label}
    >
      <TrashIcon size={13} />
    </button>
  )
}

// The flat single-agent card (single 'main' sub-agent). Thin wrapper over
// FleetCard reading the group's service-level fields.
function AgentCard({ group, onSelect }) {
  const status = statusFor(groupForStatus(group))
  const errRate = errorRatePercent(groupForStatus(group))
  return (
    <FleetCard
      name={group.display_name || group.service_name}
      secondary={group.display_name ? group.service_name : null}
      titleText={group.service_name}
      status={status}
      errRate={errRate}
      spans={group.total_spans}
      avgMs={group.avg_duration_ms}
      lastSeen={group.last_seen}
      costToday={group.cost_today}
      cost7d={group.cost_7d}
      description={group.description}
      platform={group.platform}
      ownerName={group.owner_name}
      ownerRole={group.owner_role}
      locked={group.locked}
      serviceName={group.service_name}
      onSelect={onSelect}
    />
  )
}

// The multi-agent instance group: a clickable instance BAND on top (opens the
// instance aggregate view + holds the collapse toggle) over a stack of full
// FleetCards — one per sub-agent. Each sub-agent is a first-class agent card;
// the band + shared container are what say "these live in the same instance."
// The outer container is a <div> (not a button) because we can't nest
// interactive elements; each clickable region is its own <button>.
function GroupCard({ group, onSelectInstance, onSelectSubAgent, onDeleteSubAgent }) {
  const [expanded, setExpanded] = useState(true)
  const [sparkData, setSparkData] = useState(null)
  const status = statusFor(groupForStatus(group))
  const errRate = errorRatePercent(groupForStatus(group))

  // Hooks before any early return — locked skips the fetch but still calls the
  // hook, keeping hook order stable if `locked` flips on a live re-render.
  useEffect(() => {
    if (group.locked) return undefined
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
  }, [group.service_name, group.locked])

  // Fully-locked instance (every sub-agent beyond the plan limit): muted card,
  // still clickable into its locked detail. Telemetry is recorded regardless.
  if (group.locked) {
    return (
      <button type="button" className="agent-card locked" onClick={onSelectInstance}>
        <div className="agent-card-title">
          <span className="agent-lock"><LockIcon size={13} /></span>
          <span className="agent-name" title={group.service_name}>
            {group.display_name || group.service_name}
          </span>
        </div>
        <p className="agent-locked-line">
          Recording {group.agents.length} agents — upgrade to view
        </p>
      </button>
    )
  }

  const lockedCount = group.agents.filter((a) => a.locked).length
  const costLabel =
    group.cost_today > 0
      ? `${formatCost(group.cost_today)} today`
      : group.cost_7d > 0
        ? `${formatCost(group.cost_7d)} wk`
        : null

  return (
    <div className={`instance-group status-${status}`}>
      <div className="instance-band">
        <button
          type="button"
          className="instance-band-main"
          onClick={onSelectInstance}
        >
          <span className={`status-dot status-${status}`} />
          <span className="instance-band-name" title={group.service_name}>
            {group.display_name || group.service_name}
          </span>
          {group.display_name && (
            <span className="agent-name-secondary">{group.service_name}</span>
          )}
          {group.platform && (
            <span className="instance-band-platform">{group.platform}</span>
          )}
          <span className="agent-sub-count">· {group.agents.length} agents</span>
          {lockedCount > 0 && (
            <span className="instance-band-locked">{lockedCount} locked</span>
          )}
          <span className="instance-band-stats">
            <span>{(group.total_spans || 0).toLocaleString()} spans</span>
            <span className={errRate > 20 ? 'error' : errRate > 5 ? 'warn' : ''}>
              {errRate.toFixed(1)}% err
            </span>
            {costLabel && <span>{costLabel}</span>}
            <span>{relativeTime(group.last_seen)}</span>
          </span>
          <Sparkline
            data={sparkData ?? []}
            color={statusColor(status)}
            width={72}
            height={22}
          />
        </button>
        <button
          type="button"
          className="instance-band-toggle"
          onClick={(e) => {
            e.stopPropagation()
            setExpanded((v) => !v)
          }}
          aria-expanded={expanded}
          aria-label={expanded ? 'Collapse agents' : 'Expand agents'}
        >
          {expanded ? <ChevronDownIcon /> : <ChevronRightIcon />}
        </button>
      </div>

      {expanded && (
        <div className="instance-group-grid">
          {group.agents.map((sa) => {
            const saCompat = {
              span_count: sa.span_count,
              error_count: sa.error_count,
              last_seen: sa.last_seen,
            }
            return (
              <FleetCard
                key={sa.agent_id}
                name={sa.display_name || sa.agent_id}
                secondary={sa.display_name ? sa.agent_id : null}
                titleText={sa.agent_id}
                status={statusFor(saCompat)}
                errRate={errorRatePercent(saCompat)}
                spans={sa.span_count}
                avgMs={sa.avg_duration_ms}
                lastSeen={sa.last_seen}
                costToday={sa.cost_today}
                cost7d={sa.cost_7d}
                description={sa.description}
                ownerName={sa.owner_name}
                ownerRole={sa.owner_role}
                locked={sa.locked}
                serviceName={group.service_name}
                agentId={sa.agent_id}
                onSelect={() => onSelectSubAgent(sa.agent_id)}
                onDelete={() => onDeleteSubAgent(sa.agent_id)}
                deleteLabel={`Delete ${sa.agent_id}`}
              />
            )
          })}
        </div>
      )}
    </div>
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
