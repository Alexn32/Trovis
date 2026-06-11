import { useEffect, useState } from 'react'
import { api } from './api.js'
import { PlusIcon } from './Icons.jsx'
import WorkflowCreateModal from './WorkflowCreateModal.jsx'
import { assignWorkerColors, agentKey, HUMAN_COLOR } from './workerColors.js'

function fmtRel(iso) {
  if (!iso) return '—'
  const ms = Date.now() - Date.parse(iso)
  if (Number.isNaN(ms)) return '—'
  const d = Math.floor(ms / 86400000)
  if (d > 0) return `${d}d ago`
  const h = Math.floor(ms / 3600000)
  if (h > 0) return `${h}h ago`
  const m = Math.floor(ms / 60000)
  return m > 0 ? `${m}m ago` : 'just now'
}

function fmtDur(ms) {
  if (!ms || ms <= 0) return '—'
  const s = ms / 1000
  if (s < 60) return `${Math.round(s)}s`
  const m = s / 60
  if (m < 60) return `${Math.round(m)}m`
  return `${(m / 60).toFixed(1)}h`
}

export default function WorkflowList({ onSelect }) {
  const [workflows, setWorkflows] = useState(null)
  const [statsById, setStatsById] = useState({})
  const [showCreate, setShowCreate] = useState(false)
  const [error, setError] = useState(null)

  useEffect(() => {
    let alive = true
    api
      .getWorkflows()
      .then((list) => {
        if (!alive) return
        setWorkflows(list || [])
        // Fetch live stats per workflow (small N) for the card metrics.
        Promise.all(
          (list || []).map((w) =>
            api
              .getWorkflowStats(w.id)
              .then((s) => [w.id, s])
              .catch(() => [w.id, null]),
          ),
        ).then((pairs) => {
          if (!alive) return
          const map = {}
          for (const [id, s] of pairs) if (s) map[id] = s
          setStatsById(map)
        })
      })
      .catch((e) => alive && setError(e.message || 'Could not load workflows'))
    return () => {
      alive = false
    }
  }, [])

  function onCreated(wf) {
    setShowCreate(false)
    if (wf?.id) onSelect(wf.id)
  }

  return (
    <div className="wf2">
      <div className="wf2-list-head">
        <h1 className="wf2-page-title">Workflows</h1>
        <p className="wf2-page-sub">
          How your agents and team get work done together — mapped, measured, and monitored.
        </p>
      </div>

      {error && <div className="wf2-info-box">{error}</div>}

      {workflows === null ? (
        <div className="wf2-skel">
          <span />
          <span />
        </div>
      ) : (
        <div className="wf2-card-list">
          {workflows.map((w) => {
            const s = statsById[w.id]
            const success = s?.success_rate
            const colors = assignWorkerColors(w.participants || [])
            return (
              <button
                key={w.id}
                type="button"
                className="wf2-card"
                onClick={() => onSelect(w.id)}
              >
                <div className="wf2-card-top">
                  <span className="wf2-card-name">{w.name}</span>
                  <span className={`wf2-status-pill status-${w.status || 'healthy'}`}>
                    <span className="wf2-status-dot" />
                    {(w.status || 'healthy') === 'degraded' ? 'Degraded' : 'Healthy'}
                  </span>
                </div>
                {w.description && <div className="wf2-card-desc">{w.description}</div>}

                {(w.participants || []).length > 0 && (
                  <div className="wf2-card-parts">
                    {w.participants.map((p, i) =>
                      p.type === 'human' ? (
                        <span key={`h${i}`} className="wf2-part-pill human">
                          <span
                            className="wf2-part-dot is-circle"
                            style={{ background: HUMAN_COLOR }}
                          />
                          👤 {p.role_name || 'Human'}
                        </span>
                      ) : (
                        <span key={`a${i}`} className="wf2-part-pill agent">
                          <span
                            className="wf2-part-dot is-square"
                            style={{ background: colors[agentKey(p.agent_service_name, p.agent_id)] }}
                          />
                          {p.agent_service_name}
                          {p.agent_id && p.agent_id !== 'main' ? ` · ${p.agent_id}` : ''}
                        </span>
                      ),
                    )}
                  </div>
                )}

                <div className="wf2-card-stats">
                  <Stat
                    label="Steps"
                    value={
                      w.loop_count > 0 ? (
                        <span className="wf2-steps-with-loops">
                          {w.step_count}
                          <span className="wf2-loop-badge">↻ {w.loop_count} loops</span>
                        </span>
                      ) : (
                        w.step_count
                      )
                    }
                  />
                  <Stat label="Runs 24h" value={s ? s.total_runs : '—'} dim={s && !s.total_runs} />
                  <Stat
                    label="Success"
                    value={success == null ? '—' : `${Math.round(success)}%`}
                    tone={success == null ? undefined : success >= 90 ? 'good' : 'warn'}
                  />
                  <Stat label="Avg cycle" value={s ? fmtDur(s.avg_cycle_ms) : '—'} />
                  <Stat label="Last run" value={fmtRel(s?.last_run)} dim={!s?.last_run} />
                </div>
              </button>
            )
          })}

          <button type="button" className="wf2-create-card" onClick={() => setShowCreate(true)}>
            <PlusIcon size={16} />
            Create a new workflow
          </button>
        </div>
      )}

      {showCreate && (
        <WorkflowCreateModal onClose={() => setShowCreate(false)} onCreated={onCreated} />
      )}
    </div>
  )
}

function Stat({ label, value, tone, dim }) {
  return (
    <div className="wf2-stat">
      <span className="wf2-stat-label">{label}</span>
      <span className={`wf2-stat-value ${tone ? `tone-${tone}` : ''} ${dim ? 'is-dim' : ''}`}>
        {value}
      </span>
    </div>
  )
}
