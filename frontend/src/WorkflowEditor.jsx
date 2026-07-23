import { useEffect, useState } from 'react'
import { api } from './api.js'
import { ArrowLeftIcon } from './Icons.jsx'
import { WORKFLOW_STRINGS as WS, buildWorkflowPayload, saveVersionLabel } from './loops.js'

// Stations-first workflow editor. Create ("New workflow" on the board) and
// edit ("Edit stations" on the workflow page — every save is a NEW VERSION,
// hence "Save as v{n+1}"; definitions are never mutated). Session-auth only,
// like all workflow writes.

const HOLDER_TYPES = [
  ['agent', 'Agent'],
  ['human', 'Human'],
  ['system', 'System'],
]
const HINT_FIELDS = ['service_name', 'agent_id', 'title']
const HINT_OPS = ['equals', 'contains', 'prefix']

function emptyStation() {
  return { holder_type: 'agent', holder: '', label: '', tools: '' }
}
function emptyHint() {
  return { field: 'service_name', op: 'equals', value: '' }
}

export default function WorkflowEditor({ workflow, onBack, onSaved }) {
  const editing = Boolean(workflow)
  const [name, setName] = useState(workflow?.name || '')
  const [stations, setStations] = useState(
    (workflow?.stations || []).map((s) => ({
      holder_type: s.holder_type || 'agent',
      holder: s.holder || '',
      label: s.label || '',
      tools: (s.tools || []).join(', '),
    })),
  )
  const [hints, setHints] = useState(
    (workflow?.match_hints || []).map((h) => ({ ...h })),
  )
  const [hintsOpen, setHintsOpen] = useState(false)
  const [note, setNote] = useState('')
  const [agentNames, setAgentNames] = useState([])
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState(null)

  // Known agent identities for the holder autocomplete (agents only).
  useEffect(() => {
    api
      .listAgents()
      .then((list) => {
        if (!Array.isArray(list)) return
        const names = new Set()
        for (const g of list) {
          names.add(g.service_name)
          for (const a of g.agents || []) {
            if (a.agent_id && a.agent_id !== 'main') names.add(a.agent_id)
          }
        }
        setAgentNames([...names].sort())
      })
      .catch(() => {})
  }, [])

  function updateStation(i, patch) {
    setStations((s) => s.map((st, j) => (j === i ? { ...st, ...patch } : st)))
  }
  function move(i, dir) {
    setStations((s) => {
      const next = [...s]
      const j = i + dir
      if (j < 0 || j >= next.length) return s
      ;[next[i], next[j]] = [next[j], next[i]]
      return next
    })
  }

  async function save() {
    const payload = buildWorkflowPayload(name, stations, hints, note)
    if (!payload.name) {
      setError('A workflow needs a name.')
      return
    }
    setSaving(true)
    setError(null)
    try {
      if (editing) {
        await api.createWorkflowVersion(workflow.id, payload)
        onSaved(workflow.id)
      } else {
        const created = await api.createWorkflow(payload)
        onSaved(created.id)
      }
    } catch (e) {
      setError(e?.message || 'Could not save the workflow')
      setSaving(false)
    }
  }

  return (
    <div className="dash wfe">
      <button type="button" className="wf2-back" onClick={onBack}>
        <ArrowLeftIcon size={15} /> Back
      </button>
      <div className="wfp-titlerow">
        <h1 className="dash-hello" style={{ margin: 0 }}>
          {editing ? `${workflow.name}` : WS.newWorkflow}
        </h1>
        {editing && <span className="wfe-vchip">v{workflow.current_version}</span>}
      </div>

      <div className="dash-card wfe-card">
        <label className="wfe-label" htmlFor="wfe-name">
          {WS.nameLabel}
        </label>
        <input
          id="wfe-name"
          className="wfe-input"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="Signup triage"
          disabled={editing}
        />

        <div className="wfe-label" style={{ marginTop: 18 }}>
          {WS.stationsLabel}
        </div>
        {stations.length === 0 && <div className="wfe-nudge">{WS.stationsEmptyNudge}</div>}
        {stations.map((s, i) => (
          <div className="wfe-station" key={i}>
            <div className="wfe-station-row">
              <div className="wf2-view-toggle" role="tablist">
                {HOLDER_TYPES.map(([id, label]) => (
                  <button
                    key={id}
                    type="button"
                    className={s.holder_type === id ? 'is-on' : ''}
                    onClick={() => updateStation(i, { holder_type: id })}
                  >
                    {label}
                  </button>
                ))}
              </div>
              <input
                className="wfe-input"
                list={s.holder_type === 'agent' ? 'wfe-agents' : undefined}
                value={s.holder}
                onChange={(e) => updateStation(i, { holder: e.target.value })}
                placeholder={
                  s.holder_type === 'agent'
                    ? 'agent name'
                    : s.holder_type === 'human'
                      ? 'who (optional)'
                      : 'system name'
                }
              />
              <div className="wfe-station-btns">
                <button type="button" className="btn-icon-sm" onClick={() => move(i, -1)} aria-label="Move up">↑</button>
                <button type="button" className="btn-icon-sm" onClick={() => move(i, 1)} aria-label="Move down">↓</button>
                <button
                  type="button"
                  className="btn-icon-sm"
                  onClick={() => setStations((st) => st.filter((_, j) => j !== i))}
                  aria-label="Remove station"
                >
                  ×
                </button>
              </div>
            </div>
            <div className="wfe-station-row">
              <input
                className="wfe-input"
                value={s.label}
                onChange={(e) => updateStation(i, { label: e.target.value })}
                placeholder="what happens here — “scores the signup”"
              />
              <input
                className="wfe-input"
                value={s.tools}
                onChange={(e) => updateStation(i, { tools: e.target.value })}
                placeholder="tools, comma-separated (optional)"
              />
            </div>
          </div>
        ))}
        <datalist id="wfe-agents">
          {agentNames.map((n) => (
            <option key={n} value={n} />
          ))}
        </datalist>
        <button
          type="button"
          className="btn btn-secondary btn-sm"
          onClick={() => setStations((s) => [...s, emptyStation()])}
        >
          + Add station
        </button>

        <button
          type="button"
          className="wfe-hints-toggle"
          onClick={() => setHintsOpen(!hintsOpen)}
          aria-expanded={hintsOpen}
        >
          {hintsOpen ? '▾' : '▸'} {WS.hintsTitle}
        </button>
        {hintsOpen && (
          <div className="wfe-hints">
            <div className="wfe-nudge">{WS.hintsExplainer}</div>
            {hints.map((h, i) => (
              <div className="wfe-hint-row" key={i}>
                <select
                  className="wfe-input"
                  value={h.field}
                  onChange={(e) =>
                    setHints((hs) => hs.map((x, j) => (j === i ? { ...x, field: e.target.value } : x)))
                  }
                >
                  {HINT_FIELDS.map((f) => (
                    <option key={f}>{f}</option>
                  ))}
                </select>
                <select
                  className="wfe-input"
                  value={h.op}
                  onChange={(e) =>
                    setHints((hs) => hs.map((x, j) => (j === i ? { ...x, op: e.target.value } : x)))
                  }
                >
                  {HINT_OPS.map((o) => (
                    <option key={o}>{o}</option>
                  ))}
                </select>
                <input
                  className="wfe-input"
                  value={h.value}
                  onChange={(e) =>
                    setHints((hs) => hs.map((x, j) => (j === i ? { ...x, value: e.target.value } : x)))
                  }
                  placeholder="value"
                />
                <button
                  type="button"
                  className="btn-icon-sm"
                  onClick={() => setHints((hs) => hs.filter((_, j) => j !== i))}
                  aria-label="Remove rule"
                >
                  ×
                </button>
              </div>
            ))}
            <button
              type="button"
              className="btn btn-secondary btn-sm"
              onClick={() => setHints((h) => [...h, emptyHint()])}
            >
              + Add rule
            </button>
          </div>
        )}

        {editing && (
          <>
            <label className="wfe-label" htmlFor="wfe-note" style={{ marginTop: 16 }}>
              {WS.noteLabel}
            </label>
            <input
              id="wfe-note"
              className="wfe-input"
              value={note}
              onChange={(e) => setNote(e.target.value)}
              placeholder="tightened the approval step"
            />
          </>
        )}

        {error && <div className="wfe-error">{error}</div>}
        <div className="wfe-actions">
          <button type="button" className="btn btn-primary" onClick={save} disabled={saving}>
            {saving ? 'Saving…' : saveVersionLabel(editing ? workflow.current_version : 0)}
          </button>
        </div>
      </div>
    </div>
  )
}
