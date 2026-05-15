import { useEffect, useState } from 'react'
import { api } from './api.js'
import { Spinner } from './ui.jsx'
import { ChevronDownIcon, ChevronRightIcon } from './Icons.jsx'
import { relativeTime } from './utils.js'

// Team management view. Two sections:
//   - Add form at the top (name required; email + role optional).
//   - List of existing members below with a delete button each.
// Delete also clears any agent_owners rows pointing at the deleted
// member (handled on the backend), so removing a person doesn't leave
// dangling assignments.

export default function Team({ onSelectAgent }) {
  const [members, setMembers] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  useEffect(() => {
    let cancelled = false
    api
      .getTeamMembers()
      .then((data) => {
        if (!cancelled) {
          setMembers(data || [])
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

  function handleCreated(created) {
    setMembers((prev) => [...prev, created])
  }

  async function handleDelete(id) {
    try {
      await api.deleteTeamMember(id)
      setMembers((prev) => prev.filter((m) => m.id !== id))
    } catch (e) {
      setError(e.message || 'Could not delete')
    }
  }

  return (
    <div className="view">
      <header className="team-header">
        <h2 className="section-label">Team</h2>
        <p className="team-subtitle">
          The humans behind the agents. Assign owners from each agent's
          page so you know who handles what.
        </p>
      </header>

      <AddMemberForm onCreated={handleCreated} />

      {loading && <div className="state-card">Loading team…</div>}
      {error && !loading && (
        <div className="state-card error">
          <h2>Couldn't load team</h2>
          <p>{error}</p>
        </div>
      )}
      {!loading && !error && members.length === 0 && (
        <div className="state-card">
          <h2>No team members yet</h2>
          <p>
            Use the form above to add the first one. You can assign
            them as an owner of an agent from that agent's page.
          </p>
        </div>
      )}
      {!loading && members.length > 0 && (
        <ul className="team-list">
          {members.map((m) => (
            <TeamRow
              key={m.id}
              member={m}
              onDelete={handleDelete}
              onSelectAgent={onSelectAgent}
            />
          ))}
        </ul>
      )}
    </div>
  )
}

function AddMemberForm({ onCreated }) {
  const [name, setName] = useState('')
  const [email, setEmail] = useState('')
  const [role, setRole] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState(null)

  async function handleSubmit(e) {
    e.preventDefault()
    const cleanName = name.trim()
    if (!cleanName) return
    setSubmitting(true)
    setError(null)
    try {
      const created = await api.createTeamMember({
        name: cleanName,
        email: email.trim() || null,
        role: role.trim() || null,
      })
      onCreated(created)
      setName('')
      setEmail('')
      setRole('')
    } catch (e) {
      setError(e.message || 'Could not add team member')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <form className="team-add-form" onSubmit={handleSubmit}>
      <input
        type="text"
        className="text-input"
        placeholder="Name *"
        value={name}
        onChange={(e) => setName(e.target.value)}
        required
        disabled={submitting}
      />
      <input
        type="email"
        className="text-input"
        placeholder="Email (optional)"
        value={email}
        onChange={(e) => setEmail(e.target.value)}
        disabled={submitting}
      />
      <input
        type="text"
        className="text-input"
        placeholder="Role (e.g. Sales, Content)"
        value={role}
        onChange={(e) => setRole(e.target.value)}
        disabled={submitting}
      />
      <button
        type="submit"
        className="btn btn-primary"
        disabled={submitting || !name.trim()}
      >
        {submitting ? (
          <>
            <Spinner /> Adding…
          </>
        ) : (
          'Add member'
        )}
      </button>
      {error && <p className="form-error team-add-error">{error}</p>}
    </form>
  )
}

function TeamRow({ member, onDelete, onSelectAgent }) {
  const [confirming, setConfirming] = useState(false)
  const [expanded, setExpanded] = useState(false)
  // null = not yet loaded; [] = loaded and empty.
  const [agents, setAgents] = useState(null)
  const [loadingAgents, setLoadingAgents] = useState(false)
  const [agentsError, setAgentsError] = useState(null)

  async function toggleExpanded() {
    const next = !expanded
    setExpanded(next)
    // Lazy-load on first expand. Subsequent expands reuse the cached
    // list — the team page is short-lived enough that stale data
    // between renders isn't a real concern.
    if (next && agents === null && !loadingAgents) {
      setLoadingAgents(true)
      setAgentsError(null)
      try {
        setAgents(await api.getTeamMemberAgents(member.id))
      } catch (e) {
        setAgentsError(e.message || 'Could not load assignments')
      } finally {
        setLoadingAgents(false)
      }
    }
  }

  return (
    <li className="team-row-wrapper">
      <div className="team-row">
        <button
          type="button"
          className="team-row-toggle"
          onClick={toggleExpanded}
          aria-expanded={expanded}
          aria-label={expanded ? 'Collapse assignments' : 'Show assignments'}
        >
          {expanded ? <ChevronDownIcon /> : <ChevronRightIcon />}
        </button>
        <div className="team-row-main" onClick={toggleExpanded}>
          <div className="team-row-name">{member.name}</div>
          <div className="team-row-meta">
            {member.role && (
              <span className="team-row-role">{member.role}</span>
            )}
            {member.email && (
              <span className="team-row-email mono">{member.email}</span>
            )}
          </div>
        </div>
        {confirming ? (
          <div className="team-row-confirm">
            <span>Remove?</span>
            <button
              type="button"
              className="btn btn-secondary btn-sm"
              onClick={() => onDelete(member.id)}
            >
              Yes, remove
            </button>
            <button
              type="button"
              className="btn btn-link btn-sm"
              onClick={() => setConfirming(false)}
            >
              Cancel
            </button>
          </div>
        ) : (
          <button
            type="button"
            className="btn-link-inline team-row-delete"
            onClick={() => setConfirming(true)}
          >
            Remove
          </button>
        )}
      </div>

      {expanded && (
        <div className="team-row-assignments">
          {loadingAgents && (
            <div className="team-assignments-empty">Loading…</div>
          )}
          {agentsError && !loadingAgents && (
            <div className="team-assignments-empty error">{agentsError}</div>
          )}
          {!loadingAgents && agents && agents.length === 0 && (
            <div className="team-assignments-empty">
              No agents assigned. Open any agent and use the "Assign owner"
              button to point them at this person.
            </div>
          )}
          {!loadingAgents && agents && agents.length > 0 && (
            <ul className="team-assignments-list">
              {agents.map((a) => (
                <li key={`${a.service_name}/${a.agent_id}`}>
                  <button
                    type="button"
                    className="team-assignment-row"
                    onClick={() =>
                      onSelectAgent &&
                      onSelectAgent(
                        a.service_name,
                        a.agent_id === 'main' ? undefined : a.agent_id,
                      )
                    }
                  >
                    <span className="team-assignment-name">
                      {a.display_name || a.service_name}
                      {a.agent_id && a.agent_id !== 'main' && (
                        <span className="team-assignment-sub mono">
                          {' '}
                          · {a.agent_id}
                        </span>
                      )}
                    </span>
                    <span className="team-assignment-meta">
                      {a.span_count.toLocaleString()} spans
                      {a.last_seen && (
                        <>
                          {' · '}
                          {relativeTime(a.last_seen)}
                        </>
                      )}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </li>
  )
}
