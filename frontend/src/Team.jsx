import { useEffect, useState } from 'react'
import { api } from './api.js'
import { Spinner } from './ui.jsx'

// Team management view. Two sections:
//   - Add form at the top (name required; email + role optional).
//   - List of existing members below with a delete button each.
// Delete also clears any agent_owners rows pointing at the deleted
// member (handled on the backend), so removing a person doesn't leave
// dangling assignments.

export default function Team() {
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
            <TeamRow key={m.id} member={m} onDelete={handleDelete} />
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

function TeamRow({ member, onDelete }) {
  const [confirming, setConfirming] = useState(false)
  return (
    <li className="team-row">
      <div className="team-row-main">
        <div className="team-row-name">{member.name}</div>
        <div className="team-row-meta">
          {member.role && <span className="team-row-role">{member.role}</span>}
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
    </li>
  )
}
