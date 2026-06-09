import { useEffect, useState } from 'react'
import { api } from './api.js'
import { TrovisMark, GearIcon, PlusIcon } from './Icons.jsx'

// Creation modal: Describe it (AI full graph) · From agents (multi-agent
// telemetry inference) · Start blank. Mirrors the spec's 3-method selector.

const METHODS = [
  { id: 'describe', Icon: TrovisMark, title: 'Describe it', sub: 'AI builds the flow' },
  { id: 'agents', Icon: GearIcon, title: 'From agents', sub: 'Pick agents, infer flow' },
  { id: 'blank', Icon: PlusIcon, title: 'Start blank', sub: 'Build from scratch' },
]

export default function WorkflowCreateModal({ onClose, onCreated }) {
  const [method, setMethod] = useState('describe')
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [agents, setAgents] = useState(null)
  const [picked, setPicked] = useState([]) // service_names
  const [rolesText, setRolesText] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  useEffect(() => {
    api
      .listAgents()
      .then((l) => setAgents(l || []))
      .catch(() => setAgents([]))
  }, [])

  function toggleAgent(svc) {
    setPicked((p) => (p.includes(svc) ? p.filter((x) => x !== svc) : [...p, svc]))
  }

  const canSubmit =
    name.trim() &&
    !busy &&
    (method === 'blank' ||
      (method === 'describe' && description.trim()) ||
      (method === 'agents' && picked.length > 0))

  async function submit() {
    if (!canSubmit) return
    setBusy(true)
    setError(null)
    try {
      let wf
      if (method === 'describe') {
        wf = await api.describeWorkflow({ name: name.trim(), description: description.trim() })
      } else if (method === 'agents') {
        const roles = rolesText
          .split(',')
          .map((r) => r.trim())
          .filter(Boolean)
        wf = await api.generateWorkflow({
          name: name.trim(),
          method: 'agents',
          agents: picked.map((s) => ({ service_name: s, agent_id: 'main' })),
          human_roles: roles,
        })
      } else {
        wf = await api.createWorkflow({ name: name.trim() })
      }
      onCreated(wf)
    } catch (e) {
      const msg = String(e?.message || '')
      setError(
        msg.includes('503')
          ? 'AI is unavailable — the backend needs an ANTHROPIC_API_KEY.'
          : msg || 'Could not create the workflow.',
      )
      setBusy(false)
    }
  }

  const primaryLabel =
    method === 'describe'
      ? 'Build with AI'
      : method === 'agents'
        ? 'Generate workflow'
        : 'Create blank workflow'

  return (
    <div className="wf2-modal-backdrop" onClick={busy ? undefined : onClose}>
      <div className="wf2-modal" onClick={(e) => e.stopPropagation()}>
        <div className="wf2-modal-head">
          <span className="wf2-modal-title">New Workflow</span>
          {!busy && (
            <button type="button" className="wf2-modal-close" onClick={onClose} aria-label="Close">
              ×
            </button>
          )}
        </div>

        {busy ? (
          <div className="wf2-modal-loading">
            <div className="wf2-dots">
              <span />
              <span />
              <span />
            </div>
            <p>
              {method === 'blank'
                ? 'Creating your workflow…'
                : 'Analyzing and drafting your workflow…'}
            </p>
          </div>
        ) : (
          <div className="wf2-modal-body">
            <div className="wf2-method-grid">
              {METHODS.map((m) => (
                <button
                  key={m.id}
                  type="button"
                  className={`wf2-method ${method === m.id ? 'is-active' : ''}`}
                  onClick={() => setMethod(m.id)}
                >
                  <m.Icon size={15} />
                  <strong>{m.title}</strong>
                  <span>{m.sub}</span>
                </button>
              ))}
            </div>

            <label className="wf2-field">
              <span>Workflow name</span>
              <input
                className="wf2-input"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="e.g. Customer Service"
                autoFocus
              />
            </label>

            {method === 'describe' && (
              <label className="wf2-field">
                <span>Describe the workflow in plain English</span>
                <textarea
                  className="wf2-input"
                  rows={5}
                  value={description}
                  onChange={(e) => setDescription(e.target.value)}
                  placeholder="e.g. When a customer emails us, the CS Agent triages it. If it's about shipping, the Shipping Tracker provides tracking info. If the issue is complex, a Support Manager reviews the response before it's sent."
                />
                <span className="wf2-hint">
                  Mention specific agents and human roles. The more detail, the better the
                  generated flow.
                </span>
              </label>
            )}

            {method === 'agents' && (
              <div className="wf2-field">
                <span>Which agents are involved?</span>
                {agents === null ? (
                  <div className="wf2-hint">Loading agents…</div>
                ) : agents.length === 0 ? (
                  <div className="wf2-hint">No agents reporting telemetry yet.</div>
                ) : (
                  <div className="wf2-agent-pills">
                    {agents.map((g) => (
                      <button
                        key={g.service_name}
                        type="button"
                        className={`wf2-agent-pill ${picked.includes(g.service_name) ? 'is-on' : ''}`}
                        onClick={() => toggleAgent(g.service_name)}
                      >
                        {g.display_name || g.service_name}
                      </button>
                    ))}
                  </div>
                )}
                <label className="wf2-field" style={{ marginTop: 10 }}>
                  <span>Human roles (optional)</span>
                  <input
                    className="wf2-input"
                    value={rolesText}
                    onChange={(e) => setRolesText(e.target.value)}
                    placeholder="e.g. Support Manager, Returns Lead"
                  />
                </label>
                <span className="wf2-hint">
                  We'll analyze their telemetry and identity files to infer how they work
                  together.
                </span>
              </div>
            )}

            {method === 'blank' && (
              <div className="wf2-info-box">
                You'll start with an empty canvas. Regenerate from agents or describe it later
                to fill in the flow.
              </div>
            )}

            {error && <p className="wf2-error">{error}</p>}

            <div className="wf2-modal-actions">
              <button type="button" className="btn btn-secondary" onClick={onClose}>
                Cancel
              </button>
              <button
                type="button"
                className="btn btn-primary"
                onClick={submit}
                disabled={!canSubmit}
              >
                {method !== 'blank' && <TrovisMark size={13} />} {primaryLabel}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
