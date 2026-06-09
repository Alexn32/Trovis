import { useEffect, useRef, useState } from 'react'
import { api } from './api.js'
import {
  bucketSpansByDay,
  errorRatePercent,
  formatCost,
  formatDuration,
  formatNsTimestamp,
  formatTokens,
  nsToMs,
  relativeTime,
  statusFor,
} from './utils.js'
import { Spinner, Stat } from './ui.jsx'
import {
  ArrowLeftIcon,
  ChevronDownIcon,
  ChevronRightIcon,
  PencilIcon,
  SendIcon,
  TrovisMark,
  TrashIcon,
} from './Icons.jsx'

// Detail view for one agent. The `agentId` prop is optional — when set,
// every fetch is scoped to that sub-agent within a multi-agent instance
// (via `?agent_id=` on the backend), and the header surfaces which
// sub-agent we're looking at. When omitted, we get the instance
// aggregate (the only mode pre-multi-agent).

export default function AgentDetail({ serviceName, agentId, account, onBack }) {
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
      api.getAgentSummary(serviceName, agentId),
      api.getAgentSpans(serviceName, 50, agentId),
      api.getAgentRegistration(serviceName, agentId),
      // Outputs endpoint returns [] when nothing's been captured (plugin
      // captureOutputs flag is off) — so this is always safe to call,
      // it just means the section renders its "not enabled" callout.
      api.getAgentOutputs(serviceName, 10, agentId).catch(() => []),
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
  }, [serviceName, agentId])

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
          <DetailHead
            summary={summary}
            registration={registration}
            agentId={agentId}
            account={account}
          />
          <WeeklySection
            serviceName={serviceName}
            agentId={agentId}
          />
          <CostSection
            serviceName={serviceName}
            agentId={agentId}
          />
          <CapabilitiesSection
            serviceName={serviceName}
            agentId={agentId}
          />
          <DetailStats summary={summary} />
          <ActivityChart spans={spans} />
          {registration && (
            <RegistrationBlock registration={registration} />
          )}
          <SpansTable spans={spans} />
          <RecentOutputs outputs={outputs} />
          <AskAboutAgent summary={summary} agentId={agentId} />
          <DangerZone
            summary={summary}
            agentId={agentId}
            onDeleted={onBack}
          />
        </>
      )}
    </div>
  )
}

function DetailHead({ summary, registration, agentId, account }) {
  const status = statusFor(summary)
  // Prefer the explicit scoping prop; fall back to the value the backend
  // returned on the summary (set when ?agent_id= was on the request) and
  // finally to the registration's agent_id when available.
  const shownAgentId =
    agentId || summary.agent_id || registration?.agent_id || null
  const isSubScoped = Boolean(agentId)

  // Display-name state — seeded from the summary and updated locally on
  // save so the UI flips without a re-fetch. Empty string clears the
  // override.
  const [displayName, setDisplayName] = useState(summary.display_name || '')
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(displayName)
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState(null)

  // Description state — seeded from the summary. The auto-describe
  // pipeline fires on registration / first telemetry / catchup, but
  // when none of those hits an agent (e.g. has spans pre-dating the
  // per-agent description scoping) we expose a manual trigger below.
  const [description, setDescription] = useState(summary.description || '')
  const [describing, setDescribing] = useState(false)
  const [describeError, setDescribeError] = useState(null)

  async function generateDescription() {
    setDescribing(true)
    setDescribeError(null)
    try {
      const result = await api.describeAgent(
        summary.service_name,
        shownAgentId || 'main',
      )
      setDescription(result.description || '')
    } catch (e) {
      setDescribeError(e.message || 'Could not generate description')
    } finally {
      setDescribing(false)
    }
  }

  function startEdit() {
    setDraft(displayName)
    setSaveError(null)
    setEditing(true)
  }
  function cancelEdit() {
    setEditing(false)
    setSaveError(null)
  }
  async function saveEdit() {
    setSaving(true)
    setSaveError(null)
    try {
      const trimmed = draft.trim()
      await api.setDisplayName(
        summary.service_name,
        shownAgentId || 'main',
        trimmed,
      )
      setDisplayName(trimmed)
      setEditing(false)
    } catch (e) {
      setSaveError(e.message || 'Could not save')
    } finally {
      setSaving(false)
    }
  }

  const headlineRaw = summary.service_name
  const headline = displayName || headlineRaw

  return (
    <header className="detail-head">
      <div className="detail-title-row">
        <span className={`status-dot status-${status}`} />
        {editing ? (
          <form
            className="detail-name-edit"
            onSubmit={(e) => {
              e.preventDefault()
              saveEdit()
            }}
          >
            <input
              type="text"
              className="text-input"
              placeholder={headlineRaw}
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              autoFocus
              disabled={saving}
            />
            <button
              type="submit"
              className="btn btn-primary btn-sm"
              disabled={saving}
            >
              {saving ? 'Saving…' : 'Save'}
            </button>
            <button
              type="button"
              className="btn btn-link btn-sm"
              onClick={cancelEdit}
              disabled={saving}
            >
              Cancel
            </button>
          </form>
        ) : (
          <>
            <h2 className="detail-name">
              {headline}
              {isSubScoped && (
                <span className="detail-sub-agent">
                  {' '}
                  · <span className="mono">{shownAgentId}</span>
                </span>
              )}
            </h2>
            <button
              type="button"
              className="detail-name-edit-btn"
              onClick={startEdit}
              aria-label="Edit display name"
              title="Edit display name"
            >
              <PencilIcon />
            </button>
          </>
        )}
      </div>
      {displayName && !editing && (
        <div className="detail-name-raw">{headlineRaw}</div>
      )}
      {saveError && <p className="form-error">{saveError}</p>}

      {account && account.type !== 'business' ? (
        // Individual accounts have no team — the agent is implicitly the
        // user's, so show that plainly instead of an assign-owner UI.
        <div className="owner-row">
          <span className="owner-label-text">
            Owner: <strong>{account.userName || 'You'}</strong>
            <span className="owner-role"> · you</span>
          </span>
        </div>
      ) : (
        <OwnerRow
          serviceName={summary.service_name}
          agentId={shownAgentId || 'main'}
          initialOwner={
            summary.owner_id
              ? {
                  id: summary.owner_id,
                  name: summary.owner_name,
                  role: summary.owner_role,
                }
              : null
          }
        />
      )}

      {summary.platform && (
        <div className="agent-platform">{summary.platform}</div>
      )}
      {(registration?.model || shownAgentId) && (
        <div className="tag-row">
          {registration?.model && <span className="tag">{registration.model}</span>}
          {shownAgentId && (
            <span className="tag">agent: {shownAgentId}</span>
          )}
        </div>
      )}
      {description ? (
        <p className="detail-description">{description}</p>
      ) : (
        <div className="detail-description-empty">
          <p className="detail-description empty">
            No description yet — descriptions auto-generate when an agent
            sends registration data.
          </p>
          <button
            type="button"
            className="btn btn-secondary btn-sm"
            onClick={generateDescription}
            disabled={describing}
          >
            {describing ? (
              <>
                <Spinner /> Generating…
              </>
            ) : (
              <>
                <TrovisMark size={13} /> Generate now
              </>
            )}
          </button>
          {describeError && <p className="form-error">{describeError}</p>}
        </div>
      )}
    </header>
  )
}

// Owner row — shown right under the agent name. When unassigned, shows
// an "Assign owner" link that opens a dropdown of team members (lazily
// fetched on first open). When assigned, shows "Owner: <name> · <role>"
// with a small chevron that re-opens the dropdown to reassign or
// remove. Includes an inline "Add new team member…" form at the bottom
// of the dropdown that creates and assigns in one go.
function OwnerRow({ serviceName, agentId, initialOwner }) {
  const [owner, setOwner] = useState(initialOwner)
  const [open, setOpen] = useState(false)
  const [members, setMembers] = useState(null) // null = not loaded yet
  const [loadingMembers, setLoadingMembers] = useState(false)
  const [error, setError] = useState(null)
  const [showAddForm, setShowAddForm] = useState(false)
  const [newName, setNewName] = useState('')
  const [newEmail, setNewEmail] = useState('')
  const [newRole, setNewRole] = useState('')
  const [adding, setAdding] = useState(false)

  async function ensureMembersLoaded() {
    if (members !== null || loadingMembers) return
    setLoadingMembers(true)
    setError(null)
    try {
      setMembers(await api.getTeamMembers())
    } catch (e) {
      setError(e.message || 'Could not load team')
    } finally {
      setLoadingMembers(false)
    }
  }

  function toggle() {
    setOpen((prev) => {
      if (!prev) ensureMembersLoaded()
      return !prev
    })
    setShowAddForm(false)
  }

  async function assign(member) {
    setError(null)
    try {
      await api.setAgentOwner(serviceName, {
        agent_id: agentId,
        team_member_id: member.id,
      })
      setOwner({ id: member.id, name: member.name, role: member.role })
      setOpen(false)
    } catch (e) {
      setError(e.message || 'Could not assign owner')
    }
  }

  async function clearOwner() {
    setError(null)
    try {
      await api.removeAgentOwner(serviceName, agentId)
      setOwner(null)
      setOpen(false)
    } catch (e) {
      setError(e.message || 'Could not remove owner')
    }
  }

  async function submitNewMember(e) {
    e.preventDefault()
    const name = newName.trim()
    if (!name) return
    setAdding(true)
    setError(null)
    try {
      const created = await api.createTeamMember({
        name,
        email: newEmail.trim() || null,
        role: newRole.trim() || null,
      })
      // Locally append + assign in one go.
      setMembers((prev) => [...(prev || []), created])
      await assign(created)
      setNewName('')
      setNewEmail('')
      setNewRole('')
      setShowAddForm(false)
    } catch (e) {
      setError(e.message || 'Could not add team member')
    } finally {
      setAdding(false)
    }
  }

  return (
    <div className="owner-row">
      {owner ? (
        <div className="owner-label">
          <span className="owner-label-text">
            Owner: <strong>{owner.name}</strong>
            {owner.role && <span className="owner-role"> · {owner.role}</span>}
          </span>
          <button
            type="button"
            className="btn-link-inline"
            onClick={toggle}
            aria-expanded={open}
          >
            {open ? 'Cancel' : 'Change'}
          </button>
        </div>
      ) : (
        <button
          type="button"
          className="owner-assign-link"
          onClick={toggle}
          aria-expanded={open}
        >
          Assign owner
        </button>
      )}

      {open && (
        <div className="owner-dropdown">
          {loadingMembers && (
            <div className="owner-dropdown-empty">Loading team…</div>
          )}
          {!loadingMembers && members && members.length === 0 && !showAddForm && (
            <div className="owner-dropdown-empty">
              No team members yet.
            </div>
          )}
          {!loadingMembers && members && members.length > 0 && (
            <ul className="owner-dropdown-list">
              {members.map((m) => (
                <li key={m.id}>
                  <button
                    type="button"
                    className={`owner-dropdown-item ${owner?.id === m.id ? 'selected' : ''}`}
                    onClick={() => assign(m)}
                  >
                    <span>{m.name}</span>
                    {m.role && (
                      <span className="owner-dropdown-role">{m.role}</span>
                    )}
                  </button>
                </li>
              ))}
            </ul>
          )}

          {!showAddForm ? (
            <div className="owner-dropdown-actions">
              {owner && (
                <button
                  type="button"
                  className="btn-link-inline"
                  onClick={clearOwner}
                >
                  Remove owner
                </button>
              )}
              <button
                type="button"
                className="btn-link-inline"
                onClick={() => setShowAddForm(true)}
              >
                + Add new team member…
              </button>
            </div>
          ) : (
            <form className="owner-dropdown-form" onSubmit={submitNewMember}>
              <input
                type="text"
                className="text-input"
                placeholder="Name *"
                value={newName}
                onChange={(e) => setNewName(e.target.value)}
                autoFocus
                required
                disabled={adding}
              />
              <input
                type="email"
                className="text-input"
                placeholder="Email (optional)"
                value={newEmail}
                onChange={(e) => setNewEmail(e.target.value)}
                disabled={adding}
              />
              <input
                type="text"
                className="text-input"
                placeholder="Role (e.g. Sales, Content)"
                value={newRole}
                onChange={(e) => setNewRole(e.target.value)}
                disabled={adding}
              />
              <div className="owner-dropdown-form-actions">
                <button
                  type="submit"
                  className="btn btn-primary btn-sm"
                  disabled={adding || !newName.trim()}
                >
                  {adding ? 'Adding…' : 'Add & assign'}
                </button>
                <button
                  type="button"
                  className="btn btn-link btn-sm"
                  onClick={() => setShowAddForm(false)}
                  disabled={adding}
                >
                  Cancel
                </button>
              </div>
            </form>
          )}

          {error && <p className="form-error">{error}</p>}
        </div>
      )}
    </div>
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

function AskAboutAgent({ summary, agentId }) {
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
        agentId,
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
            <TrovisMark size={13} /> Ask about this agent
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

// =====================================================================
// SECTION: This week — weekly stats + plain-English summary
// =====================================================================

function WeeklySection({ serviceName, agentId }) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    api
      .getWeeklySummary(serviceName, agentId)
      .then((res) => {
        if (cancelled) return
        setData(res)
        setLoading(false)
      })
      .catch((e) => {
        if (cancelled) return
        setError(e.message || 'Could not load weekly summary')
        setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [serviceName, agentId])

  return (
    <section className="section-block weekly-section">
      <div className="section-block-header">
        <h3 className="section-label">This week</h3>
      </div>

      {loading && <div className="weekly-loading">Loading weekly summary…</div>}
      {error && !loading && (
        <div className="callout callout-error">{error}</div>
      )}
      {!loading && !error && data && (
        <>
          {data.summary_unavailable ? (
            <div className="callout callout-info">
              Weekly summary unavailable — the backend doesn't have an
              Anthropic API key configured. Stats below are still live.
            </div>
          ) : data.summary ? (
            <p className="weekly-summary-text">{data.summary}</p>
          ) : data.runs === 0 ? (
            <p className="weekly-summary-text empty">
              No activity in the last 7 days. Once spans start flowing,
              a plain-English summary will appear here.
            </p>
          ) : (
            <p className="weekly-summary-text empty">
              Generating summary…
            </p>
          )}

          <div className="weekly-stats">
            <WeeklyStat
              label="Runs"
              value={data.runs.toLocaleString()}
              deltaPct={data.trends.runs_delta_pct}
              betterDirection="up"
            />
            <WeeklyStat
              label="Success rate"
              value={`${data.success_rate.toFixed(1)}%`}
              deltaPct={data.trends.success_rate_delta_pct}
              betterDirection="up"
            />
            <WeeklyStat
              label="Avg response"
              value={formatDuration(data.avg_duration_ms)}
              deltaPct={data.trends.avg_duration_delta_pct}
              betterDirection="down"
            />
            <WeeklyStat
              label="Errors"
              value={data.errors.toLocaleString()}
              deltaPct={data.trends.errors_delta_pct}
              betterDirection="down"
            />
          </div>
        </>
      )}
    </section>
  )
}

// One stat box with optional trend arrow. `betterDirection` controls
// the color — for Errors and Avg response, smaller is better, so a
// negative delta gets the green arrow.
function WeeklyStat({ label, value, deltaPct, betterDirection }) {
  let trendNode = null
  if (deltaPct !== null && deltaPct !== undefined) {
    const isUp = deltaPct > 0
    const isDown = deltaPct < 0
    const better =
      (betterDirection === 'up' && isUp) ||
      (betterDirection === 'down' && isDown)
    const worse =
      (betterDirection === 'up' && isDown) ||
      (betterDirection === 'down' && isUp)
    const cls = better ? 'good' : worse ? 'bad' : 'flat'
    const arrow = isUp ? '↑' : isDown ? '↓' : '•'
    // Round to whole percent for display; show one decimal only if
    // the absolute value is below 10 (e.g. 4.2% reads better than 4%).
    const mag = Math.abs(deltaPct)
    const text =
      mag === 0
        ? 'no change'
        : mag < 10
          ? `${mag.toFixed(1)}%`
          : `${Math.round(mag)}%`
    trendNode = (
      <span className={`weekly-trend ${cls}`}>
        {arrow} {text}
      </span>
    )
  } else {
    trendNode = <span className="weekly-trend flat">—</span>
  }
  return (
    <div className="weekly-stat">
      <span className="weekly-stat-label">{label}</span>
      <span className="weekly-stat-value">{value}</span>
      {trendNode}
    </div>
  )
}

// =====================================================================
// SECTION: Cost — token usage + estimated USD spend
// =====================================================================

function CostSection({ serviceName, agentId }) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    api
      .getAgentCosts(serviceName, agentId, 7)
      .then((res) => {
        if (cancelled) return
        setData(res)
        setLoading(false)
      })
      .catch((e) => {
        if (cancelled) return
        setError(e.message || 'Could not load costs')
        setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [serviceName, agentId])

  // Derive "today" from the last day bucket (cost_by_day is sorted
  // ascending by date). "this week" is the whole-window total.
  const todayCost = (() => {
    if (!data || !data.cost_by_day.length) return 0
    const today = new Date().toISOString().slice(0, 10)
    const last = data.cost_by_day[data.cost_by_day.length - 1]
    return last.date === today ? last.cost : 0
  })()

  const hasData = data && data.total_tokens > 0
  const maxDayCost = data
    ? Math.max(...data.cost_by_day.map((d) => d.cost), 0.000001)
    : 1

  return (
    <section className="section-block cost-section">
      <div className="section-block-header">
        <h3 className="section-label">Cost</h3>
      </div>

      {loading && <div className="weekly-loading">Loading cost data…</div>}
      {error && !loading && (
        <div className="callout callout-error">{error}</div>
      )}
      {!loading && !error && !hasData && (
        <div className="callout callout-info">
          No token usage recorded yet. Cost tracking populates once this
          agent makes model calls that report token counts.
        </div>
      )}
      {!loading && !error && hasData && (
        <>
          <p className="cost-headline">
            {formatCost(todayCost)} today
            <span className="cost-headline-sep"> · </span>
            {formatCost(data.estimated_cost_usd)} this week
            <span className="cost-headline-sep"> · </span>
            {formatTokens(data.total_tokens)} tokens
          </p>

          <div className="cost-stats">
            <div className="cost-stat">
              <span className="cost-stat-label">Input tokens</span>
              <span className="cost-stat-value">
                {data.total_input_tokens.toLocaleString()}
              </span>
            </div>
            <div className="cost-stat">
              <span className="cost-stat-label">Output tokens</span>
              <span className="cost-stat-value">
                {data.total_output_tokens.toLocaleString()}
              </span>
            </div>
            <div className="cost-stat">
              <span className="cost-stat-label">7-day cost</span>
              <span className="cost-stat-value">
                {formatCost(data.estimated_cost_usd)}
              </span>
            </div>
          </div>

          <h4 className="cost-subhead">Cost by day</h4>
          <div className="cost-bars">
            {data.cost_by_day.map((d) => (
              <div key={d.date} className="cost-bar-col" title={`${d.date}: ${formatCost(d.cost)} · ${d.tokens.toLocaleString()} tokens`}>
                <div className="cost-bar-track">
                  <div
                    className="cost-bar-fill"
                    style={{
                      height: `${Math.max(2, (d.cost / maxDayCost) * 100)}%`,
                    }}
                  />
                </div>
                <span className="cost-bar-label">{d.date.slice(5)}</span>
              </div>
            ))}
          </div>

          {data.cost_by_model.length > 1 && (
            <>
              <h4 className="cost-subhead">Cost by model</h4>
              <ul className="cost-model-list">
                {data.cost_by_model.map((m) => (
                  <li key={m.model} className="cost-model-row">
                    <span className="cost-model-name mono">{m.model}</span>
                    <span className="cost-model-tokens">
                      {formatTokens(m.tokens)} tokens
                    </span>
                    <span className="cost-model-cost">
                      {formatCost(m.cost)}
                    </span>
                  </li>
                ))}
              </ul>
            </>
          )}
        </>
      )}
    </section>
  )
}

// =====================================================================
// SECTION: What it touches — capability map
// =====================================================================

function CapabilitiesSection({ serviceName, agentId }) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    api
      .getAgentCapabilities(serviceName, agentId)
      .then((res) => {
        if (cancelled) return
        setData(res)
        setLoading(false)
      })
      .catch((e) => {
        if (cancelled) return
        setError(e.message || 'Could not load capabilities')
        setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [serviceName, agentId])

  const allEmpty =
    data &&
    (data.reads_from?.length ?? 0) === 0 &&
    (data.writes_to?.length ?? 0) === 0 &&
    (data.can_do?.length ?? 0) === 0

  return (
    <section className="section-block capabilities-section">
      <div className="section-block-header">
        <h3 className="section-label">What it touches</h3>
      </div>

      {loading && (
        <div className="weekly-loading">Loading capability map…</div>
      )}
      {error && !loading && (
        <div className="callout callout-error">{error}</div>
      )}
      {!loading && !error && data?.unavailable && (
        <div className="callout callout-info">
          Capability map unavailable — the backend doesn't have an
          Anthropic API key configured.
        </div>
      )}
      {!loading && !error && data && !data.unavailable && allEmpty && (
        <div className="callout callout-info">
          Not enough signal yet to infer this agent's capabilities. The
          map fills in once it sends a registration or uses some tools.
        </div>
      )}
      {!loading && !error && data && !data.unavailable && !allEmpty && (
        <div className="capabilities-grid">
          <CapabilityColumn label="Reads from" items={data.reads_from || []} />
          <CapabilityColumn label="Writes to" items={data.writes_to || []} />
          <CapabilityColumn label="Can do" items={data.can_do || []} />
        </div>
      )}
    </section>
  )
}

function CapabilityColumn({ label, items }) {
  return (
    <div className="capability-column">
      <h4 className="capability-column-label">{label}</h4>
      {items.length === 0 ? (
        <span className="capability-empty">—</span>
      ) : (
        <ul className="capability-list">
          {items.map((item, i) => (
            <li key={i} className="capability-pill">
              {item}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

// =====================================================================
// SECTION: Danger zone — hard-delete this agent's data
// =====================================================================
//
// Two-step confirm: clicking "Delete agent" reveals an inline confirm
// pane with the exact name + a destructive-action warning. The actual
// DELETE only fires from the inner "Yes, delete" button. On success
// we call onDeleted (= AgentDetail's onBack prop), which routes the
// user back to the Fleet view.

function DangerZone({ summary, agentId, onDeleted }) {
  const [confirming, setConfirming] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [error, setError] = useState(null)

  // What we're actually deleting — used in the confirm copy. When
  // agentId is set we're targeting one sub-agent within an instance;
  // otherwise the whole service_name and every sub-agent under it.
  const isSubScoped = Boolean(agentId)
  const friendlyName =
    summary.display_name ||
    (isSubScoped ? `${summary.service_name} / ${agentId}` : summary.service_name)

  async function handleConfirm() {
    setDeleting(true)
    setError(null)
    try {
      await api.deleteAgent(summary.service_name, agentId)
      // Bounce back to Fleet. The Fleet view re-fetches /agents on
      // mount, so the deleted entry won't be there.
      onDeleted?.()
    } catch (e) {
      setError(e.message || 'Could not delete the agent')
      setDeleting(false)
    }
  }

  return (
    <section className="section-block danger-zone">
      <div className="section-block-header">
        <h3 className="section-label">Danger zone</h3>
      </div>

      {!confirming ? (
        <div className="danger-row">
          <div>
            <p className="danger-headline">Delete this agent</p>
            <p className="danger-help">
              Removes all telemetry, descriptions, owner assignment,
              and any cached insights. This cannot be undone.
            </p>
          </div>
          <button
            type="button"
            className="btn btn-danger"
            onClick={() => setConfirming(true)}
          >
            <TrashIcon size={13} /> Delete agent
          </button>
        </div>
      ) : (
        <div className="danger-confirm">
          <p className="danger-confirm-headline">
            Delete <strong>{friendlyName}</strong>?
          </p>
          <p className="danger-help">
            This removes all telemetry, descriptions, and settings for
            this {isSubScoped ? 'sub-agent' : 'instance'}. This cannot
            be undone.
          </p>
          {error && <p className="form-error">{error}</p>}
          <div className="danger-confirm-actions">
            <button
              type="button"
              className="btn btn-danger"
              onClick={handleConfirm}
              disabled={deleting}
            >
              {deleting ? (
                <>
                  <Spinner /> Deleting…
                </>
              ) : (
                <>Yes, delete</>
              )}
            </button>
            <button
              type="button"
              className="btn btn-link"
              onClick={() => {
                setConfirming(false)
                setError(null)
              }}
              disabled={deleting}
            >
              Cancel
            </button>
          </div>
        </div>
      )}
    </section>
  )
}

