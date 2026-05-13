import { useEffect, useRef, useState } from 'react'
import { api } from './api.js'
import {
  bucketSpansByDay,
  errorRatePercent,
  formatDuration,
  formatNsTimestamp,
  nsToMs,
  relativeTime,
  statusFor,
} from './utils.js'
import { Spinner, Stat } from './ui.jsx'
import {
  ArrowLeftIcon,
  ChevronDownIcon,
  ChevronRightIcon,
  SendIcon,
  SparkleIcon,
} from './Icons.jsx'

// Detail view for a single agent. Loads summary + spans in parallel, plus
// /registration (which 404s gracefully when the agent hasn't provided
// identity data). Renders 14-day activity bars, a recent-spans table with
// expandable attributes, and suggested questions that hand off to Ask.

export default function AgentDetail({ serviceName, onBack }) {
  const [summary, setSummary] = useState(null)
  const [spans, setSpans] = useState([])
  const [registration, setRegistration] = useState(null)
  const [outputs, setOutputs] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    Promise.all([
      api.getAgentSummary(serviceName),
      api.getAgentSpans(serviceName, 50),
      api.getAgentRegistration(serviceName),
      // Outputs endpoint returns [] when nothing's been captured (plugin
      // captureOutputs flag is off) — so this is always safe to call,
      // it just means the section renders its "not enabled" callout.
      api.getAgentOutputs(serviceName, 10).catch(() => []),
    ])
      .then(([s, sp, reg, outs]) => {
        if (cancelled) return
        setSummary(s)
        setSpans(sp)
        setRegistration(reg)
        setOutputs(outs || [])
        setLoading(false)
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
    <div className="view">
      <button type="button" className="detail-back" onClick={onBack}>
        <ArrowLeftIcon /> Back to fleet
      </button>

      {loading && <div className="state-card">Loading…</div>}
      {error && (
        <div className="state-card error">
          <h2>Couldn't load this agent</h2>
          <p>{error}</p>
        </div>
      )}

      {summary && (
        <>
          <DetailHead summary={summary} registration={registration} />
          <DetailStats summary={summary} />
          <ActivityChart spans={spans} />
          {registration && (
            <RegistrationBlock registration={registration} />
          )}
          <SpansTable spans={spans} />
          <RecentOutputs outputs={outputs} />
          <AskAboutAgent summary={summary} />
        </>
      )}
    </div>
  )
}

function DetailHead({ summary, registration }) {
  const status = statusFor(summary)
  return (
    <header className="detail-head">
      <div className="detail-title-row">
        <span className={`status-dot status-${status}`} />
        <h2 className="detail-name">{summary.service_name}</h2>
      </div>
      {summary.platform && (
        <div className="agent-platform">{summary.platform}</div>
      )}
      {(registration?.model || (summary.top_operations || []).length > 0) && (
        <div className="tag-row">
          {registration?.model && <span className="tag">{registration.model}</span>}
          {registration?.agent_id && (
            <span className="tag">agent: {registration.agent_id}</span>
          )}
        </div>
      )}
      <p className={`detail-description ${summary.description ? '' : 'empty'}`}>
        {summary.description ||
          'No description yet — descriptions auto-generate when an agent sends registration data.'}
      </p>
    </header>
  )
}

function DetailStats({ summary }) {
  const rate = errorRatePercent(summary)
  return (
    <div className="detail-stats">
      <Stat label="Total spans" value={summary.span_count.toLocaleString()} />
      <Stat
        label="Error rate"
        value={`${rate.toFixed(1)}%`}
        tone={rate > 20 ? 'error' : rate > 5 ? 'warn' : undefined}
      />
      <Stat label="Avg duration" value={formatDuration(summary.avg_duration_ms)} />
      <Stat label="First seen" value={relativeTime(summary.first_seen)} />
      <Stat label="Last seen" value={relativeTime(summary.last_seen)} />
    </div>
  )
}

function ActivityChart({ spans }) {
  const data = bucketSpansByDay(spans, 14)
  const max = Math.max(...data, 1)
  return (
    <section className="section-block">
      <div className="section-block-header">
        <h3 className="section-label">Activity · last 14 days</h3>
      </div>
      <div className="activity-chart">
        <div className="activity-bars" role="img" aria-label="Activity bars">
          {data.map((v, i) => {
            const height = v === 0 ? 0 : Math.max(4, (v / max) * 100)
            return (
              <div
                key={i}
                className={`activity-bar ${v > 0 ? 'bar-green' : ''}`}
                style={{ height: `${height}%` }}
                title={`${v} span${v === 1 ? '' : 's'}`}
              />
            )
          })}
        </div>
        <div className="activity-chart-labels">
          <span>14d ago</span>
          <span>today</span>
        </div>
      </div>
    </section>
  )
}

function RegistrationBlock({ registration }) {
  const [open, setOpen] = useState(true)
  return (
    <section className="section-block">
      <div className="registration-block">
        <button
          type="button"
          className="registration-toggle"
          onClick={() => setOpen((o) => !o)}
          aria-expanded={open}
        >
          <span>
            Agent identity{' '}
            <span className="registration-source">
              · {registration.workspace_path || 'from registration span'}
            </span>
          </span>
          {open ? <ChevronDownIcon /> : <ChevronRightIcon />}
        </button>
        {open && (
          <div className="registration-body">
            {registration.soul && (
              <Field label="Soul" body={registration.soul} />
            )}
            {registration.identity && (
              <Field label="Identity" body={registration.identity} />
            )}
            {registration.operating_manual && (
              <Field label="Operating manual" body={registration.operating_manual} />
            )}
            {registration.user_context && (
              <Field label="User context" body={registration.user_context} />
            )}
            {registration.memory && (
              <Field label="Memory" body={registration.memory} />
            )}
          </div>
        )}
      </div>
    </section>
  )
}

function Field({ label, body }) {
  return (
    <div>
      <h4 className="registration-field-label">{label}</h4>
      <div className="registration-field-body">{body}</div>
    </div>
  )
}

function SpansTable({ spans }) {
  return (
    <section className="section-block">
      <div className="section-block-header">
        <h3 className="section-label">
          Recent spans <span style={{ color: 'var(--text-dim)' }}>· {spans.length}</span>
        </h3>
      </div>
      <table className="spans-table">
        <thead>
          <tr>
            <th style={{ width: 28 }}></th>
            <th>Operation</th>
            <th>Duration</th>
            <th>Status</th>
            <th>Started</th>
          </tr>
        </thead>
        <tbody>
          {spans.map((s) => (
            <SpanRow key={s.id} span={s} />
          ))}
        </tbody>
      </table>
    </section>
  )
}

function SpanRow({ span }) {
  const [expanded, setExpanded] = useState(false)
  const durationMs = nsToMs(span.end_time_unix - span.start_time_unix)
  const isError = span.status_code === 2
  return (
    <>
      <tr className="span-row" onClick={() => setExpanded((e) => !e)}>
        <td style={{ color: 'var(--text-dim)' }}>
          {expanded ? <ChevronDownIcon /> : <ChevronRightIcon />}
        </td>
        <td className="span-name">{span.span_name}</td>
        <td>{formatDuration(durationMs)}</td>
        <td>
          {isError ? (
            <span className="status-error">✕</span>
          ) : (
            <span className="status-ok">✓</span>
          )}
        </td>
        <td style={{ color: 'var(--text-muted)' }}>
          {formatNsTimestamp(span.start_time_unix)}
        </td>
      </tr>
      {expanded && (
        <tr className="attrs-block">
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

function RecentOutputs({ outputs }) {
  return (
    <section className="section-block">
      <div className="section-block-header">
        <h3 className="section-label">
          Recent outputs{' '}
          <span style={{ color: 'var(--text-dim)' }}>· {outputs.length}</span>
        </h3>
      </div>
      {outputs.length === 0 ? (
        <div className="callout callout-info">
          Output capture is not enabled for this agent. To see what your
          agents produce, run <code>/oversee capture on</code> in your
          agent's chat, or add <code>captureOutputs: true</code> to your
          plugin config.
        </div>
      ) : (
        <div className="outputs-list">
          {outputs.map((o, i) => (
            <OutputItem key={i} output={o} />
          ))}
        </div>
      )}
    </section>
  )
}

function OutputItem({ output }) {
  const [expanded, setExpanded] = useState(false)
  const content = output.content || ''
  const truncated = content.length > 200
  const displayed = !expanded && truncated ? content.slice(0, 200) + '…' : content
  // Pretty-print the type label: 'tool_result' → 'tool result'.
  const typeLabel =
    output.content_type === 'tool_result' ? 'tool result' : output.content_type
  return (
    <div className="output-item">
      <div className="output-meta">
        <span className={`output-badge output-badge-${output.content_type}`}>
          {typeLabel}
        </span>
        <span className="output-timestamp">{relativeTime(output.timestamp)}</span>
        <span className="output-operation mono">{output.operation}</span>
      </div>
      <div className="output-content">{displayed}</div>
      {truncated && (
        <button
          type="button"
          className="output-toggle"
          onClick={() => setExpanded((e) => !e)}
        >
          {expanded ? 'Show less' : 'Show more'}
        </button>
      )}
    </div>
  )
}

function AskAboutAgent({ summary }) {
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const [pending, setPending] = useState(false)
  const threadRef = useRef(null)

  // Auto-scroll the thread when new messages arrive.
  useEffect(() => {
    if (threadRef.current) {
      threadRef.current.scrollTop = threadRef.current.scrollHeight
    }
  }, [messages, pending])

  const rate = errorRatePercent(summary)
  const suggestions = [
    `Why does ${summary.service_name} have a ${rate.toFixed(1)}% error rate?`,
    `What did ${summary.service_name} do today?`,
    `Is ${summary.service_name} behaving as configured?`,
    `How can I improve ${summary.service_name}'s performance?`,
  ]

  async function submit(text) {
    const q = (text ?? input).trim()
    if (!q) return
    const next = [...messages, { role: 'user', content: q }]
    setMessages(next)
    setInput('')
    setPending(true)
    try {
      const res = await api.askAboutAgent(
        summary.service_name,
        next.map((m) => ({ role: m.role, content: m.content })),
      )
      setMessages((m) => [...m, { role: 'assistant', content: res.answer }])
    } catch (e) {
      setMessages((m) => [
        ...m,
        { role: 'assistant', content: `Couldn't answer: ${e.message}` },
      ])
    } finally {
      setPending(false)
    }
  }

  function onSubmit(e) {
    e.preventDefault()
    submit()
  }

  return (
    <section className="section-block">
      <div className="section-block-header">
        <h3 className="section-label">
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
            <SparkleIcon size={13} /> Ask about this agent
          </span>
        </h3>
      </div>

      {messages.length === 0 ? (
        <div className="suggested-pills" style={{ marginBottom: 12 }}>
          {suggestions.map((s) => (
            <button
              key={s}
              type="button"
              className="suggested-pill"
              onClick={() => submit(s)}
              disabled={pending}
            >
              {s}
            </button>
          ))}
        </div>
      ) : (
        <div
          ref={threadRef}
          className="ask-thread"
          style={{ maxHeight: 420, marginBottom: 12 }}
        >
          {messages.map((m, i) => (
            <div key={i} className={`ask-message ${m.role}`}>
              <div className="ask-bubble">{m.content}</div>
            </div>
          ))}
          {pending && (
            <div className="ask-message assistant">
              <div className="ask-bubble">
                <Spinner /> Thinking…
              </div>
            </div>
          )}
        </div>
      )}

      <form
        className="ask-input-row"
        onSubmit={onSubmit}
        style={{ borderTop: 'none', padding: 0 }}
      >
        <input
          type="text"
          className="ask-input"
          placeholder={`Ask anything about ${summary.service_name}…`}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          disabled={pending}
        />
        <button
          type="submit"
          className="ask-send"
          disabled={pending || !input.trim()}
          aria-label="Send"
        >
          <SendIcon size={16} />
        </button>
      </form>
    </section>
  )
}

