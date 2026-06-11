import { useEffect, useMemo, useState } from 'react'
import { api } from './api.js'
import { agentKey } from './workerColors.js'

// Right-docked panel for adding / editing one workflow step. Owns its form
// state and performs the API mutations itself (incl. edge rewiring for a
// splice and participant auto-add), then calls onSaved() so the canvas
// refetches once. `placement` describes the operation:
//   { mode:'edit', step }                       — edit an existing step
//   { mode:'add', afterStepId }                 — append after a step (or null → first)
//   { mode:'add', splice:{ fromStepId, toStepId, edge } } — insert between A and B

const TYPES = [
  { id: 'trigger', label: 'Trigger' },
  { id: 'agent', label: 'Agent' },
  { id: 'human', label: 'Human' },
  { id: 'decision', label: 'Decision' },
  { id: 'output', label: 'Output' },
]

export default function WorkflowStepEditor({ workflowId, placement, participants, onSaved, onClose }) {
  const editing = placement.mode === 'edit'
  const step = editing ? placement.step : null

  const [stepType, setStepType] = useState(step?.step_type || 'agent')
  const [label, setLabel] = useState(step?.label || '')
  const [operation, setOperation] = useState(step?.operation || '')
  const [description, setDescription] = useState(step?.description || '')
  const [svc, setSvc] = useState(step?.agent_service_name || '')
  const [aid, setAid] = useState(step?.agent_id || 'main')
  const [roleName, setRoleName] = useState((step?.config && step.config.role_name) || '')
  const [teamMemberId, setTeamMemberId] = useState(step?.team_member_id || '')
  const [humanMode, setHumanMode] = useState(step?.team_member_id ? 'member' : 'role')
  const [agents, setAgents] = useState([])
  const [team, setTeam] = useState([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  useEffect(() => {
    api.listAgents().then((l) => setAgents(l || [])).catch(() => {})
    api.getTeamMembers().then((l) => setTeam(l || [])).catch(() => {})
  }, [])

  // Agents already on the roster, shown first in the picker.
  const rosterAgents = useMemo(
    () => (participants || []).filter((p) => p.type === 'agent'),
    [participants],
  )
  const colorKeys = useMemo(
    () => new Set((participants || []).filter((p) => p.type === 'agent').map((p) => agentKey(p.agent_service_name, p.agent_id))),
    [participants],
  )

  const canSave =
    !busy && label.trim() && (stepType !== 'agent' || svc) &&
    (stepType !== 'human' || humanMode !== 'role' || roleName.trim())

  function buildPayload() {
    const p = {
      step_type: stepType,
      label: label.trim(),
      operation: operation.trim() || null,
      description: description.trim() || null,
      agent_service_name: null,
      agent_id: null,
      team_member_id: null,
      config: null,
    }
    if (stepType === 'agent') {
      p.agent_service_name = svc
      p.agent_id = aid || 'main'
    } else if (stepType === 'human') {
      if (humanMode === 'member' && teamMemberId) {
        p.team_member_id = Number(teamMemberId)
        const m = team.find((t) => String(t.id) === String(teamMemberId))
        if (m) p.config = { role_name: m.role || m.name }
      } else {
        p.config = { role_name: roleName.trim() }
      }
    }
    return p
  }

  // Ensure the step's worker shows on the roster (so it gets a color + chip).
  async function ensureParticipant(payload) {
    try {
      if (payload.step_type === 'agent' && payload.agent_service_name) {
        if (!colorKeys.has(agentKey(payload.agent_service_name, payload.agent_id))) {
          await api.addWorkflowParticipant(workflowId, {
            type: 'agent',
            agent_service_name: payload.agent_service_name,
            agent_id: payload.agent_id || 'main',
          })
        }
      } else if (payload.step_type === 'human') {
        const role = payload.config && payload.config.role_name
        if (role) {
          await api.addWorkflowParticipant(workflowId, { type: 'human', role_name: role })
        }
      }
    } catch {
      /* 409 (already a participant) is fine */
    }
  }

  async function save() {
    if (!canSave) return
    setBusy(true)
    setError(null)
    const payload = buildPayload()
    try {
      if (editing) {
        await api.updateWorkflowStep(workflowId, step.id, payload)
        await ensureParticipant(payload)
      } else if (placement.splice) {
        const { fromStepId, toStepId, edge } = placement.splice
        const created = await api.addWorkflowStep(workflowId, payload)
        await ensureParticipant(payload)
        if (edge && edge.id != null) await api.deleteWorkflowEdge(workflowId, edge.id)
        await api.addWorkflowEdge(workflowId, { from_step_id: fromStepId, to_step_id: created.id })
        await api.addWorkflowEdge(workflowId, {
          from_step_id: created.id,
          to_step_id: toStepId,
          label: edge ? edge.label : null,
          is_branch: edge ? edge.is_branch : false,
        })
      } else {
        // Append (after a step, or standalone).
        const created = await api.addWorkflowStep(workflowId, payload)
        await ensureParticipant(payload)
        if (placement.afterStepId != null) {
          await api.addWorkflowEdge(workflowId, {
            from_step_id: placement.afterStepId,
            to_step_id: created.id,
          })
        }
      }
      onSaved()
    } catch (e) {
      setError(e?.message || 'Could not save the step.')
      setBusy(false)
    }
  }

  return (
    <div className="wf2-editor-panel" onClick={(e) => e.stopPropagation()}>
      <div className="wf2-editor-head">
        <span>{editing ? 'Edit step' : 'Add step'}</span>
        <button type="button" className="wf2-editor-close" onClick={onClose} aria-label="Close">
          ×
        </button>
      </div>
      <div className="wf2-editor-body">
        <div className="wf2-field">
          <span>Type</span>
          <div className="wf2-type-grid">
            {TYPES.map((t) => (
              <button
                key={t.id}
                type="button"
                className={`wf2-agent-pill ${stepType === t.id ? 'is-on' : ''}`}
                onClick={() => setStepType(t.id)}
              >
                {t.label}
              </button>
            ))}
          </div>
        </div>

        <label className="wf2-field">
          <span>Name</span>
          <input
            className="wf2-input"
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            placeholder="e.g. Draft reply"
            autoFocus
          />
        </label>

        {stepType === 'agent' && (
          <div className="wf2-field">
            <span>Agent</span>
            <div className="wf2-agent-pills">
              {rosterAgents.map((p) => {
                const on = svc === p.agent_service_name && (aid || 'main') === (p.agent_id || 'main')
                return (
                  <button
                    key={agentKey(p.agent_service_name, p.agent_id)}
                    type="button"
                    className={`wf2-agent-pill ${on ? 'is-on' : ''}`}
                    onClick={() => {
                      setSvc(p.agent_service_name)
                      setAid(p.agent_id || 'main')
                    }}
                  >
                    {p.agent_service_name}
                  </button>
                )
              })}
            </div>
            <select
              className="wf2-input"
              value={rosterAgents.some((p) => p.agent_service_name === svc) ? '' : svc}
              onChange={(e) => {
                if (e.target.value) {
                  setSvc(e.target.value)
                  setAid('main')
                }
              }}
              style={{ marginTop: 6 }}
            >
              <option value="">+ other agent…</option>
              {agents
                .filter((g) => !rosterAgents.some((p) => p.agent_service_name === g.service_name))
                .map((g) => (
                  <option key={g.service_name} value={g.service_name}>
                    {g.display_name || g.service_name}
                  </option>
                ))}
            </select>
            {svc && <span className="wf2-hint">Worker: {svc}</span>}
          </div>
        )}

        {stepType === 'human' && (
          <div className="wf2-field">
            <span>Assignee</span>
            <div className="wf2-type-grid">
              <button
                type="button"
                className={`wf2-agent-pill ${humanMode === 'role' ? 'is-on' : ''}`}
                onClick={() => setHumanMode('role')}
              >
                Role name
              </button>
              <button
                type="button"
                className={`wf2-agent-pill ${humanMode === 'member' ? 'is-on' : ''}`}
                onClick={() => setHumanMode('member')}
              >
                Team member
              </button>
            </div>
            {humanMode === 'role' ? (
              <input
                className="wf2-input"
                value={roleName}
                onChange={(e) => setRoleName(e.target.value)}
                placeholder="e.g. Support Manager"
                style={{ marginTop: 6 }}
              />
            ) : (
              <select
                className="wf2-input"
                value={teamMemberId}
                onChange={(e) => setTeamMemberId(e.target.value)}
                style={{ marginTop: 6 }}
              >
                <option value="">Select a team member…</option>
                {team.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.name}
                    {m.role ? ` · ${m.role}` : ''}
                  </option>
                ))}
              </select>
            )}
          </div>
        )}

        {(stepType === 'agent' || stepType === 'decision') && (
          <label className="wf2-field">
            <span>Operation {stepType === 'decision' ? '' : '(tool name)'}</span>
            <input
              className="wf2-input"
              value={operation}
              onChange={(e) => setOperation(e.target.value)}
              placeholder="e.g. triage"
            />
          </label>
        )}

        <label className="wf2-field">
          <span>Description</span>
          <textarea
            className="wf2-input"
            rows={2}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="One sentence (optional)"
          />
        </label>

        {error && <p className="wf2-error">{error}</p>}
      </div>
      <div className="wf2-editor-actions">
        <button type="button" className="btn btn-secondary" onClick={onClose} disabled={busy}>
          Cancel
        </button>
        <button type="button" className="btn btn-primary" onClick={save} disabled={!canSave}>
          {busy ? 'Saving…' : editing ? 'Save' : 'Add step'}
        </button>
      </div>
    </div>
  )
}
