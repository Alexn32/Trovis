import { useEffect, useState } from 'react'
import { api } from './api.js'
import { relativeTime, statusFromLastSeen, formatDuration } from './utils.js'
import { Spinner, Stat } from './ui.jsx'

// Landing view: one card per agent reporting telemetry.
export default function AgentRegistry({ onSelect, onAddAgent }) {
  const [agents, setAgents] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  // service_name of the agent currently being described, or null
  const [generating, setGenerating] = useState(null)

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

  async function handleGenerate(serviceName) {
    setGenerating(serviceName)
    try {
      const result = await api.describeAgent(serviceName)
      // Splice the new description into the existing agent row rather than
      // refetching the list — keeps the rest of the stats stable while
      // only the description visibly updates.
      setAgents((prev) =>
        prev.map((a) =>
          a.service_name === serviceName
            ? { ...a, description: result.description }
            : a,
        ),
      )
    } catch (e) {
      alert(`Failed to generate description: ${e.message}`)
    } finally {
      setGenerating(null)
    }
  }

  // Registry-local toolbar — sits below the global app header and above the
  // list. Lives here (rather than in App.jsx) so the button only renders on
  // the registry view, never on the detail or add-agent views.
  const toolbar = (
    <div className="registry-header">
      <h2 className="registry-heading">
        Agents{!loading && !error && ` (${agents.length})`}
      </h2>
      <button type="button" className="add-agent-btn" onClick={onAddAgent}>
        <span className="plus">+</span> Add Agent
      </button>
    </div>
  )

  if (loading) {
    return (
      <>
        {toolbar}
        <div className="state">Loading agents…</div>
      </>
    )
  }
  if (error) {
    return (
      <>
        {toolbar}
        <div className="state error">Error: {error}</div>
      </>
    )
  }
  if (agents.length === 0) {
    return (
      <>
        {toolbar}
        <div className="state">
          <h2>No agents yet</h2>
          <p>
            Click <strong>+ Add Agent</strong> above to connect your first agent,
            or send OpenTelemetry traces directly to{' '}
            <code>POST /v1/traces</code>.
          </p>
        </div>
      </>
    )
  }

  return (
    <>
      {toolbar}
      <div className="registry">
        {agents.map((agent) => (
          <AgentCard
            key={agent.service_name}
            agent={agent}
            generating={generating === agent.service_name}
            onGenerate={() => handleGenerate(agent.service_name)}
            onSelect={() => onSelect(agent.service_name)}
          />
        ))}
      </div>
    </>
  )
}

function AgentCard({ agent, generating, onGenerate, onSelect }) {
  const status = statusFromLastSeen(agent.last_seen)

  function handleButton(e) {
    // Don't bubble to the card click handler that navigates to detail.
    e.stopPropagation()
    onGenerate()
  }

  function handleKey(e) {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault()
      onSelect()
    }
  }

  return (
    <div
      className="card"
      onClick={onSelect}
      onKeyDown={handleKey}
      role="button"
      tabIndex={0}
    >
      <div className="card-head">
        <div className="title-group">
          <span
            className={`status-dot status-${status}`}
            aria-label={`Status: ${status}`}
          />
          <h2 className="service-name">{agent.service_name}</h2>
        </div>
        <button
          className="btn btn-primary"
          onClick={handleButton}
          disabled={generating}
        >
          {generating ? (
            <>
              <Spinner /> Generating…
            </>
          ) : agent.description ? (
            'Regenerate'
          ) : (
            'Generate Description'
          )}
        </button>
      </div>

      <p
        className={`description ${agent.description ? '' : 'description-empty'}`}
      >
        {agent.description || 'No description generated yet'}
      </p>

      <div className="stats">
        <Stat label="Spans" value={agent.span_count.toLocaleString()} />
        <Stat
          label="Errors"
          value={agent.error_count}
          bad={agent.error_count > 0}
        />
        <Stat label="Avg duration" value={formatDuration(agent.avg_duration_ms)} />
        <Stat label="Last seen" value={relativeTime(agent.last_seen)} />
      </div>
    </div>
  )
}
