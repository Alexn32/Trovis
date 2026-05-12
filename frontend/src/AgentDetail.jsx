import { useEffect, useState } from 'react'
import { api } from './api.js'
import {
  relativeTime,
  formatDuration,
  formatNsTimestamp,
  nsToMs,
} from './utils.js'
import { Stat } from './ui.jsx'

// Drill-in view for one agent. Loads summary + recent spans in parallel.
export default function AgentDetail({ serviceName, onBack }) {
  const [summary, setSummary] = useState(null)
  const [spans, setSpans] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    Promise.all([
      api.getAgentSummary(serviceName),
      api.getAgentSpans(serviceName, 50),
    ])
      .then(([s, sp]) => {
        if (!cancelled) {
          setSummary(s)
          setSpans(sp)
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
  }, [serviceName])

  return (
    <div className="detail">
      <button className="back" onClick={onBack}>
        ← Back to all agents
      </button>

      {loading && <div className="state">Loading…</div>}
      {error && <div className="state error">Error: {error}</div>}

      {summary && (
        <>
          <h2 className="service-name detail-name">{summary.service_name}</h2>
          <p
            className={`description ${summary.description ? '' : 'description-empty'}`}
          >
            {summary.description || 'No description generated yet'}
          </p>

          <div className="stats stats-detail">
            <Stat label="Spans" value={summary.span_count.toLocaleString()} />
            <Stat
              label="Errors"
              value={summary.error_count}
              bad={summary.error_count > 0}
            />
            <Stat
              label="Avg duration"
              value={formatDuration(summary.avg_duration_ms)}
            />
            <Stat label="First seen" value={relativeTime(summary.first_seen)} />
            <Stat label="Last seen" value={relativeTime(summary.last_seen)} />
          </div>

          <h3 className="section-title">
            Recent spans <span className="muted">({spans.length})</span>
          </h3>
          <table className="spans-table">
            <thead>
              <tr>
                <th className="col-caret" />
                <th>Span</th>
                <th>Duration</th>
                <th>Status</th>
                <th>Started</th>
              </tr>
            </thead>
            <tbody>
              {spans.map((span) => (
                <SpanRow key={span.id} span={span} />
              ))}
            </tbody>
          </table>
        </>
      )}
    </div>
  )
}

function SpanRow({ span }) {
  const [expanded, setExpanded] = useState(false)
  const durationMs = nsToMs(span.end_time_unix - span.start_time_unix)
  const isError = span.status_code === 2

  // Status code 0 = unset (no signal either way), 1 = ok, 2 = error.
  // We render checkmark for ok/unset and X for error — the convention real
  // OTEL backends use.

  return (
    <>
      <tr className="span-row" onClick={() => setExpanded((e) => !e)}>
        <td className="caret">{expanded ? '▾' : '▸'}</td>
        <td className="span-name">{span.span_name}</td>
        <td>{formatDuration(durationMs)}</td>
        <td>
          {isError ? (
            <span className="status-error" aria-label="error">
              ✕
            </span>
          ) : (
            <span className="status-ok" aria-label="ok">
              ✓
            </span>
          )}
        </td>
        <td className="muted">{formatNsTimestamp(span.start_time_unix)}</td>
      </tr>
      {expanded && (
        <tr className="span-detail-row">
          <td colSpan={5}>
            <pre className="attrs-json">
              {JSON.stringify(
                {
                  trace_id: span.trace_id,
                  span_id: span.span_id,
                  parent_span_id: span.parent_span_id,
                  status_code: span.status_code,
                  status_message: span.status_message,
                  attributes: span.attributes,
                  resource_attributes: span.resource_attributes,
                },
                null,
                2,
              )}
            </pre>
          </td>
        </tr>
      )}
    </>
  )
}
