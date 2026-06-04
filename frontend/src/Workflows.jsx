import { useEffect, useMemo, useRef, useState } from 'react'
import { api } from './api.js'
import { Spinner } from './ui.jsx'
import { relativeTime, formatCost, formatTokens, formatDuration } from './utils.js'
import ConnectionsMap from './ConnectionsMap.jsx'
import {
  PlusIcon,
  PencilIcon,
  TrashIcon,
  RobotIcon,
  UserIcon,
  ClockIcon,
  DiamondIcon,
  CheckCircleIcon,
  GripIcon,
} from './Icons.jsx'

// Workflows — auto-generated, editable process flows for an agent. A left
// sidebar lists workflows; the main area renders the selected one as a
// vertical, Notion/Linear-style sequence of typed step cards (trigger /
// agent / human / decision / output). Agent steps come from telemetry;
// human steps are inferred by Claude from time-gaps + identity files.

const STEP_META = {
  trigger: { label: 'Trigger', Icon: ClockIcon, cls: 'wf-step-trigger' },
  agent: { label: 'Agent', Icon: RobotIcon, cls: 'wf-step-agent' },
  human: { label: 'Human', Icon: UserIcon, cls: 'wf-step-human' },
  decision: { label: 'Decision', Icon: DiamondIcon, cls: 'wf-step-decision' },
  output: { label: 'Output', Icon: CheckCircleIcon, cls: 'wf-step-output' },
}
const INSERTABLE = [
  ['agent', 'Agent action'],
  ['human', 'Human / manual task'],
  ['decision', 'Decision'],
  ['output', 'Output'],
]

function fmtDuration(ms) {
  if (ms == null || Number.isNaN(ms)) return null
  if (ms < 1000) return `~${ms}ms`
  if (ms < 60000) return `~${Math.round(ms / 1000)}s`
  return `~${Math.round(ms / 60000)} min`
}

export default function Workflows({ onSelectAgent }) {
  const [workflows, setWorkflows] = useState([])
  const [loadingList, setLoadingList] = useState(true)
  const [listError, setListError] = useState(null)
  const [selectedId, setSelectedId] = useState(null)
  const [detail, setDetail] = useState(null)
  const [loadingDetail, setLoadingDetail] = useState(false)
  const [showCreate, setShowCreate] = useState(false)
  const [notice, setNotice] = useState(null)
  const [busy, setBusy] = useState(false)
  const [view, setView] = useState('workflow') // 'workflow' | 'map'

  function flash(msg) {
    setNotice(msg)
    window.clearTimeout(flash._t)
    flash._t = window.setTimeout(() => setNotice(null), 3500)
  }

  async function reloadList(selectAfter) {
    try {
      const list = await api.getWorkflows()
      setWorkflows(list || [])
      if (selectAfter != null) setSelectedId(selectAfter)
      else if (list?.length && selectedId == null) setSelectedId(list[0].id)
    } catch (e) {
      setListError(e.message || 'Could not load workflows')
    } finally {
      setLoadingList(false)
    }
  }

  useEffect(() => {
    reloadList()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    if (selectedId == null) {
      setDetail(null)
      return
    }
    let cancelled = false
    setLoadingDetail(true)
    api
      .getWorkflow(selectedId)
      .then((wf) => !cancelled && setDetail(wf))
      .catch((e) => !cancelled && flash(e.message || 'Could not load workflow'))
      .finally(() => !cancelled && setLoadingDetail(false))
    return () => {
      cancelled = true
    }
  }, [selectedId])

  async function reloadDetail() {
    if (selectedId == null) return
    try {
      setDetail(await api.getWorkflow(selectedId))
    } catch (e) {
      flash(e.message || 'Could not refresh')
    }
  }

  function handleCreated(wf) {
    setShowCreate(false)
    setDetail(wf)
    setSelectedId(wf.id)
    reloadList(wf.id)
  }

  // --- workflow-level mutations ---
  async function renameWorkflow(name) {
    const wf = await api.updateWorkflow(detail.id, { name })
    setDetail(wf)
    reloadList(wf.id)
  }
  async function deleteWorkflow() {
    await api.deleteWorkflow(detail.id)
    const remaining = workflows.filter((w) => w.id !== detail.id)
    setWorkflows(remaining)
    setSelectedId(remaining[0]?.id ?? null)
  }

  // --- step-level mutations ---
  async function insertStep(index, type) {
    setBusy(true)
    try {
      const created = await api.addWorkflowStep(detail.id, {
        step_type: type,
        label: `New ${type} step`,
        inferred_from: 'manual',
      })
      // addWorkflowStep appends at the end; move it into `index`.
      const ids = detail.steps.map((s) => s.id)
      ids.splice(index, 0, created.id)
      await api.reorderWorkflowSteps(detail.id, ids)
      await reloadDetail()
      reloadList(detail.id)
    } catch (e) {
      flash(e.message || 'Could not add step')
    } finally {
      setBusy(false)
    }
  }
  async function patchStep(stepId, patch) {
    await api.updateWorkflowStep(detail.id, stepId, patch)
    // Reload so joined fields (e.g. assigned team member name) are fresh.
    await reloadDetail()
  }
  async function deleteStep(stepId) {
    await api.deleteWorkflowStep(detail.id, stepId)
    await reloadDetail()
    reloadList(detail.id)
  }
  async function commitReorder(orderedIds) {
    // optimistic
    setDetail((d) => ({
      ...d,
      steps: orderedIds.map((id) => d.steps.find((s) => s.id === id)).filter(Boolean),
    }))
    try {
      await api.reorderWorkflowSteps(detail.id, orderedIds)
      reloadList(detail.id)
    } catch (e) {
      flash(e.message || 'Could not reorder')
      reloadDetail()
    }
  }

  // --- footer / regenerate ---
  async function regenerate() {
    if (!detail?.agent_service_name) {
      flash('This workflow has no source agent to regenerate from.')
      return
    }
    if (!window.confirm('Regenerate this workflow from the latest telemetry? The current steps will be replaced.')) return
    setBusy(true)
    try {
      const fresh = await api.generateWorkflow({
        name: detail.name,
        agent_service_name: detail.agent_service_name,
        agent_id: detail.agent_id || 'main',
      })
      // Replace in place from the user's view: drop the old, keep the new.
      await api.deleteWorkflow(detail.id)
      setDetail(fresh)
      setSelectedId(fresh.id)
      reloadList(fresh.id)
      flash('Regenerated from telemetry.')
    } catch (e) {
      flash(e.message || 'Could not regenerate')
    } finally {
      setBusy(false)
    }
  }
  async function suggestMissing() {
    if (!detail?.agent_service_name) {
      flash('This workflow has no source agent to analyze.')
      return
    }
    setBusy(true)
    try {
      const probe = await api.generateWorkflow({
        name: `${detail.name} (suggestions)`,
        agent_service_name: detail.agent_service_name,
        agent_id: detail.agent_id || 'main',
      })
      const have = new Set(detail.steps.map((s) => `${s.step_type}:${(s.label || '').toLowerCase()}`))
      const missing = (probe.steps || []).filter(
        (s) => !have.has(`${s.step_type}:${(s.label || '').toLowerCase()}`),
      )
      for (const s of missing) {
        await api.addWorkflowStep(detail.id, {
          step_type: s.step_type,
          label: s.label,
          description: s.description,
          operation: s.operation,
          duration_estimate_ms: s.duration_estimate_ms,
          agent_service_name: s.agent_service_name,
          agent_id: s.agent_id,
          inferred_from: s.inferred_from || 'telemetry',
        })
      }
      await api.deleteWorkflow(probe.id) // discard the throwaway probe
      await reloadDetail()
      reloadList(detail.id)
      flash(missing.length ? `Added ${missing.length} suggested step${missing.length > 1 ? 's' : ''}.` : 'No missing steps found — your workflow looks complete.')
    } catch (e) {
      flash(e.message || 'Could not analyze')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="view workflow-view">
      <WorkflowSidebar
        workflows={workflows}
        loading={loadingList}
        error={listError}
        selectedId={view === 'map' ? null : selectedId}
        mapActive={view === 'map'}
        onShowMap={() => setView('map')}
        onSelect={(id) => {
          setView('workflow')
          setSelectedId(id)
        }}
        onCreate={() => setShowCreate(true)}
      />

      {view === 'map' ? (
        <div className="workflow-main">
          <ConnectionsMap onSelectAgent={onSelectAgent} />
        </div>
      ) : (
      <div className="workflow-main">
        {notice && <div className="workflow-toast">{notice}</div>}
        {selectedId == null && !loadingList && (
          <div className="workflow-empty-main">
            <h2>Map an agent's process</h2>
            <p>
              Pick or create a workflow on the left. Oversee analyzes an agent's
              telemetry and identity to draft every step — automated and human —
              that its work actually involves.
            </p>
            <button type="button" className="btn btn-primary" onClick={() => setShowCreate(true)}>
              <PlusIcon /> New workflow
            </button>
          </div>
        )}
        {loadingDetail && <div className="state-card">Loading workflow…</div>}
        {detail && !loadingDetail && (
          <WorkflowDetail
            workflow={detail}
            busy={busy}
            onRename={renameWorkflow}
            onDelete={deleteWorkflow}
            onSelectAgent={onSelectAgent}
            onInsert={insertStep}
            onPatchStep={patchStep}
            onDeleteStep={deleteStep}
            onReorder={commitReorder}
            onRegenerate={regenerate}
            onSuggest={suggestMissing}
          />
        )}
      </div>
      )}

      {showCreate && (
        <CreateWorkflowModal
          onClose={() => setShowCreate(false)}
          onCreated={handleCreated}
        />
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Sidebar
// ---------------------------------------------------------------------------

function WorkflowSidebar({ workflows, loading, error, selectedId, onSelect, onCreate, mapActive, onShowMap }) {
  return (
    <aside className="workflow-sidebar">
      <div className="workflow-sidebar-head">
        <h2 className="section-label">Workflows</h2>
        <button type="button" className="btn-icon" onClick={onCreate} aria-label="New workflow" title="New workflow">
          <PlusIcon />
        </button>
      </div>
      <button
        type="button"
        className={`workflow-map-btn ${mapActive ? 'is-active' : ''}`}
        onClick={onShowMap}
      >
        <span className="workflow-map-icon">⤳</span> Connections map
      </button>
      {loading && <div className="workflow-side-empty">Loading…</div>}
      {error && !loading && <div className="workflow-side-empty error">{error}</div>}
      {!loading && !error && workflows.length === 0 && (
        <div className="workflow-side-empty">No workflows yet</div>
      )}
      <ul className="workflow-list">
        {workflows.map((w) => (
          <li key={w.id}>
            <button
              type="button"
              className={`workflow-card ${w.id === selectedId ? 'is-active' : ''}`}
              onClick={() => onSelect(w.id)}
            >
              <span className="workflow-card-name">{w.name}</span>
              <span className="workflow-card-meta">
                {w.agent_service_name && (
                  <span className="workflow-card-agent">{w.agent_service_name}</span>
                )}
                <span className="workflow-card-count">{w.step_count} step{w.step_count === 1 ? '' : 's'}</span>
              </span>
              {w.updated_at && (
                <span className="workflow-card-updated">{relativeTime(w.updated_at)}</span>
              )}
            </button>
          </li>
        ))}
      </ul>
    </aside>
  )
}

// ---------------------------------------------------------------------------
// Detail (header + step flow + footer)
// ---------------------------------------------------------------------------

function WorkflowDetail({
  workflow,
  busy,
  onRename,
  onDelete,
  onSelectAgent,
  onInsert,
  onPatchStep,
  onDeleteStep,
  onReorder,
  onRegenerate,
  onSuggest,
}) {
  const [editingName, setEditingName] = useState(false)
  const [draftName, setDraftName] = useState(workflow.name)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [showTiming, setShowTiming] = useState(false)
  const [dragId, setDragId] = useState(null)
  const [overId, setOverId] = useState(null)
  const [stats, setStats] = useState(null)

  useEffect(() => {
    setDraftName(workflow.name)
    setEditingName(false)
    setConfirmDelete(false)
  }, [workflow.id, workflow.name])

  // Live agent telemetry for the stats rail. Re-fetches when the workflow
  // changes (incl. after a regenerate, which mints a new id).
  useEffect(() => {
    let cancelled = false
    setStats(null)
    api
      .getWorkflowStats(workflow.id)
      .then((s) => !cancelled && setStats(s))
      .catch(() => !cancelled && setStats(null))
    return () => {
      cancelled = true
    }
  }, [workflow.id])

  const steps = workflow.steps || []

  const timing = useMemo(() => {
    let total = 0
    const byType = {}
    for (const s of steps) {
      const ms = s.duration_estimate_ms || 0
      total += ms
      byType[s.step_type] = (byType[s.step_type] || 0) + ms
    }
    return { total, byType }
  }, [steps])

  // Composition for the "This workflow" stats block.
  const composition = useMemo(() => {
    const counts = {}
    for (const s of steps) counts[s.step_type] = (counts[s.step_type] || 0) + 1
    return counts
  }, [steps])

  function handleDrop(targetId) {
    if (dragId == null || dragId === targetId) {
      setDragId(null)
      setOverId(null)
      return
    }
    const ids = steps.map((s) => s.id)
    const from = ids.indexOf(dragId)
    const to = ids.indexOf(targetId)
    ids.splice(from, 1)
    ids.splice(to, 0, dragId)
    setDragId(null)
    setOverId(null)
    onReorder(ids)
  }

  async function saveName() {
    const next = draftName.trim()
    setEditingName(false)
    if (next && next !== workflow.name) await onRename(next)
  }

  return (
    <div className="workflow-detail">
      <div className="workflow-detail-content">
      <header className="workflow-detail-head">
        <div className="workflow-detail-title">
          {editingName ? (
            <form
              onSubmit={(e) => {
                e.preventDefault()
                saveName()
              }}
              className="workflow-name-edit"
            >
              <input
                className="text-input"
                value={draftName}
                onChange={(e) => setDraftName(e.target.value)}
                autoFocus
                onBlur={saveName}
              />
            </form>
          ) : (
            <h1 className="workflow-title" onClick={() => setEditingName(true)} title="Click to rename">
              {workflow.name}
              <PencilIcon />
            </h1>
          )}
          {workflow.agent_service_name && (
            <button
              type="button"
              className="workflow-agent-tag"
              onClick={() => onSelectAgent?.(workflow.agent_service_name)}
            >
              {workflow.agent_service_name}
              {workflow.agent_id && workflow.agent_id !== 'main' && (
                <span className="mono"> · {workflow.agent_id}</span>
              )}
            </button>
          )}
        </div>
        <div className="workflow-detail-actions">
          {confirmDelete ? (
            <>
              <span className="workflow-confirm-label">Delete this workflow?</span>
              <button type="button" className="btn btn-danger btn-sm" onClick={onDelete}>
                Yes, delete
              </button>
              <button type="button" className="btn btn-link btn-sm" onClick={() => setConfirmDelete(false)}>
                Cancel
              </button>
            </>
          ) : (
            <button type="button" className="btn btn-secondary btn-sm" onClick={() => setConfirmDelete(true)}>
              <TrashIcon /> Delete
            </button>
          )}
        </div>
      </header>

      {workflow.description && <p className="workflow-detail-desc">{workflow.description}</p>}

      <div className="workflow-steps">
        <InsertBetween disabled={busy} onInsert={(type) => onInsert(0, type)} />
        {steps.map((step, i) => (
          <div key={step.id}>
            <StepCard
              step={step}
              dragging={dragId === step.id}
              dragOver={overId === step.id}
              onDragStart={() => setDragId(step.id)}
              onDragEnter={() => setOverId(step.id)}
              onDragEnd={() => {
                setDragId(null)
                setOverId(null)
              }}
              onDrop={() => handleDrop(step.id)}
              onPatch={(patch) => onPatchStep(step.id, patch)}
              onDelete={() => onDeleteStep(step.id)}
            />
            <InsertBetween disabled={busy} onInsert={(type) => onInsert(i + 1, type)} />
          </div>
        ))}
        {steps.length === 0 && (
          <div className="workflow-steps-empty">
            No steps yet — add one below, or regenerate from telemetry.
          </div>
        )}
        <InsertBetween
          disabled={busy}
          label="Add step"
          onInsert={(type) => onInsert(steps.length, type)}
        />
      </div>

      <footer className="workflow-footer">
        <p className="workflow-footer-note">
          Agent steps are generated from telemetry. Human steps are inferred from
          gaps and identity files. Edit any step to match your actual process.
        </p>
        <div className="workflow-footer-actions">
          <button type="button" className="btn btn-secondary btn-sm" disabled={busy} onClick={onSuggest}>
            {busy ? <Spinner /> : null} Suggest missing steps
          </button>
          <button type="button" className="btn btn-secondary btn-sm" onClick={() => setShowTiming((v) => !v)}>
            Timing analysis
          </button>
          <button type="button" className="btn btn-secondary btn-sm" disabled={busy} onClick={onRegenerate}>
            {busy ? <Spinner /> : null} Regenerate from telemetry
          </button>
        </div>
        {showTiming && (
          <div className="workflow-timing">
            <strong>Estimated total: {fmtDuration(timing.total) || '—'}</strong>
            <ul>
              {Object.entries(timing.byType).map(([type, ms]) => (
                <li key={type}>
                  <span className={`wf-type-dot ${STEP_META[type]?.cls}`} /> {STEP_META[type]?.label || type}: {fmtDuration(ms) || '—'}
                </li>
              ))}
            </ul>
          </div>
        )}
      </footer>
      </div>

      <StatsRail
        stats={stats}
        composition={composition}
        stepCount={steps.length}
        estTotalMs={timing.total}
        serviceName={workflow.agent_service_name}
        agentId={workflow.agent_id}
        onSelectAgent={onSelectAgent}
      />
    </div>
  )
}

// ---------------------------------------------------------------------------
// Stats rail
// ---------------------------------------------------------------------------

function StatsRail({ stats, composition, stepCount, estTotalMs, serviceName, agentId, onSelectAgent }) {
  const order = ['trigger', 'agent', 'human', 'decision', 'output']
  const compParts = order
    .filter((t) => composition[t])
    .map((t) => `${composition[t]} ${t}`)
  return (
    <aside className="workflow-stats">
      {serviceName && (
        <ConnectionsCard
          serviceName={serviceName}
          agentId={agentId || 'main'}
          onSelectAgent={onSelectAgent}
        />
      )}
      <div className="workflow-stats-card">
        <h3 className="workflow-stats-title">Agent activity</h3>
        {stats == null ? (
          <div className="workflow-stats-loading">
            <Spinner />
          </div>
        ) : !stats.has_agent ? (
          <p className="workflow-stats-note">No source agent linked, so there's no live telemetry to show.</p>
        ) : stats.runs === 0 && stats.spans === 0 ? (
          <p className="workflow-stats-note">No telemetry yet from this agent.</p>
        ) : (
          <ul className="workflow-stat-list">
            <StatRow label="Total runs" value={stats.runs.toLocaleString()} />
            <StatRow
              label="Errors"
              value={stats.errors.toLocaleString()}
              tone={stats.errors > 0 ? 'error' : undefined}
            />
            <StatRow label="Success rate" value={`${stats.success_rate}%`} />
            <StatRow label="Avg duration" value={formatDuration(stats.avg_duration_ms)} />
            <StatRow label="Last run" value={stats.last_run ? relativeTime(stats.last_run) : 'never'} />
            <StatRow label="Tokens" value={formatTokens(stats.total_tokens)} />
            <StatRow label="Cost" value={formatCost(stats.estimated_cost_usd)} />
          </ul>
        )}
      </div>

      <div className="workflow-stats-card">
        <h3 className="workflow-stats-title">This workflow</h3>
        <ul className="workflow-stat-list">
          <StatRow label="Steps" value={String(stepCount)} />
          <StatRow label="Make-up" value={compParts.length ? compParts.join(' · ') : '—'} />
          <StatRow label="Est. cycle time" value={fmtDuration(estTotalMs) || '—'} />
        </ul>
      </div>
    </aside>
  )
}

function StatRow({ label, value, tone }) {
  return (
    <li className="workflow-stat-row">
      <span className="workflow-stat-label">{label}</span>
      <span className={`workflow-stat-value ${tone === 'error' ? 'is-error' : ''}`}>{value}</span>
    </li>
  )
}

// Agent-to-agent connections for this workflow's agent, detected from shared
// traces. Shows who it feeds into / receives from, with confirm/dismiss.
function ConnectionsCard({ serviceName, agentId, onSelectAgent }) {
  const [conns, setConns] = useState(null)

  useEffect(() => {
    let cancelled = false
    // Refresh detection on open, then read the (curated) list.
    api
      .detectConnections()
      .then((list) => !cancelled && setConns(list))
      .catch(() =>
        api.getConnections().then((l) => !cancelled && setConns(l)).catch(() => !cancelled && setConns([])),
      )
    return () => {
      cancelled = true
    }
  }, [serviceName, agentId])

  async function setStatus(id, status) {
    try {
      const updated = await api.updateConnection(id, status)
      setConns((prev) => prev.map((c) => (c.id === id ? updated : c)))
    } catch {
      /* ignore */
    }
  }

  if (conns === null) {
    return (
      <div className="workflow-stats-card">
        <h3 className="workflow-stats-title">Connected agents</h3>
        <div className="workflow-stats-loading"><Spinner /></div>
      </div>
    )
  }

  const mine = (c) => c.source_service === serviceName && (c.source_agent_id || 'main') === agentId
  const theirs = (c) => c.target_service === serviceName && (c.target_agent_id || 'main') === agentId
  const visible = (c) => c.status !== 'dismissed'
  const outgoing = conns.filter((c) => mine(c) && visible(c))
  const incoming = conns.filter((c) => theirs(c) && visible(c))

  if (outgoing.length === 0 && incoming.length === 0) {
    return (
      <div className="workflow-stats-card">
        <h3 className="workflow-stats-title">Connected agents</h3>
        <p className="workflow-stats-note">
          No agent-to-agent connections detected. They appear when this agent
          shares a trace with another (handoffs / calls).
        </p>
      </div>
    )
  }

  return (
    <div className="workflow-stats-card">
      <h3 className="workflow-stats-title">Connected agents</h3>
      {outgoing.length > 0 && (
        <div className="wf-conn-group">
          <div className="wf-conn-dir">Feeds into</div>
          {outgoing.map((c) => (
            <ConnRow
              key={c.id}
              conn={c}
              label={c.target_service}
              onOpen={() => onSelectAgent?.(c.target_service)}
              onConfirm={() => setStatus(c.id, 'confirmed')}
              onDismiss={() => setStatus(c.id, 'dismissed')}
            />
          ))}
        </div>
      )}
      {incoming.length > 0 && (
        <div className="wf-conn-group">
          <div className="wf-conn-dir">Receives from</div>
          {incoming.map((c) => (
            <ConnRow
              key={c.id}
              conn={c}
              label={c.source_service}
              onOpen={() => onSelectAgent?.(c.source_service)}
              onConfirm={() => setStatus(c.id, 'confirmed')}
              onDismiss={() => setStatus(c.id, 'dismissed')}
            />
          ))}
        </div>
      )}
    </div>
  )
}

function ConnRow({ conn, label, onOpen, onConfirm, onDismiss }) {
  return (
    <div className="wf-conn-row">
      <button type="button" className="wf-conn-name" onClick={onOpen} title={`Open ${label}`}>
        {label}
      </button>
      <span className="wf-conn-meta">
        {conn.call_count} call{conn.call_count === 1 ? '' : 's'}
      </span>
      {conn.status === 'detected' ? (
        <span className="wf-conn-actions">
          <button type="button" className="wf-conn-act confirm" title="Confirm" onClick={onConfirm}>✓</button>
          <button type="button" className="wf-conn-act dismiss" title="Dismiss" onClick={onDismiss}>×</button>
        </span>
      ) : (
        <span className={`wf-conn-status wf-conn-${conn.status}`}>{conn.status}</span>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Step card
// ---------------------------------------------------------------------------

function StepCard({ step, dragging, dragOver, onDragStart, onDragEnter, onDragEnd, onDrop, onPatch, onDelete }) {
  const [editing, setEditing] = useState(false)
  const [confirm, setConfirm] = useState(false)
  const meta = STEP_META[step.step_type] || STEP_META.agent
  const Icon = meta.Icon
  const duration = fmtDuration(step.duration_estimate_ms)

  if (editing) {
    return (
      <StepEditor
        step={step}
        onCancel={() => setEditing(false)}
        onSave={async (patch) => {
          await onPatch(patch)
          setEditing(false)
        }}
      />
    )
  }

  return (
    <div
      className={`wf-step ${meta.cls} ${dragging ? 'is-dragging' : ''} ${dragOver ? 'is-over' : ''}`}
      draggable
      onDragStart={onDragStart}
      onDragEnter={onDragEnter}
      onDragOver={(e) => e.preventDefault()}
      onDragEnd={onDragEnd}
      onDrop={onDrop}
    >
      <div className="wf-step-rail">
        <span className="wf-step-icon">
          <Icon size={15} />
        </span>
      </div>
      <div className="wf-step-body">
        <div className="wf-step-top">
          <span className="wf-step-type">{meta.label}</span>
          {step.step_type === 'agent' && step.agent_service_name && (
            <span className="wf-step-pill wf-pill-agent">{step.agent_service_name}</span>
          )}
          {step.step_type === 'human' && step.team_member_name && (
            <span className="wf-step-pill wf-pill-human">{step.team_member_name}</span>
          )}
          {step.inferred_from && step.inferred_from !== 'manual' && (
            <span className="wf-step-inferred">inferred from: {step.inferred_from}</span>
          )}
          <span className="wf-step-grip" title="Drag to reorder">
            <GripIcon size={15} />
          </span>
        </div>
        <div className="wf-step-label">{step.label}</div>
        {step.description && <div className="wf-step-desc">{step.description}</div>}
        <div className="wf-step-foot">
          {step.step_type === 'agent' && step.operation && (
            <span className="wf-op-pill mono">{step.operation}</span>
          )}
          {step.step_type === 'decision' && (
            <span className="wf-branches">
              <span className="wf-branch wf-branch-yes">Yes</span>
              <span className="wf-branch wf-branch-no">No</span>
            </span>
          )}
          {duration && <span className="wf-step-duration">{duration}</span>}
        </div>
      </div>
      <div className="wf-step-actions">
        <button type="button" className="btn-icon-sm" onClick={() => setEditing(true)} aria-label="Edit step" title="Edit">
          <PencilIcon />
        </button>
        {confirm ? (
          <button type="button" className="btn-icon-sm danger" onClick={onDelete} title="Confirm delete">
            ✓
          </button>
        ) : (
          <button type="button" className="btn-icon-sm" onClick={() => setConfirm(true)} aria-label="Delete step" title="Delete">
            <TrashIcon />
          </button>
        )}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Step inline editor
// ---------------------------------------------------------------------------

function StepEditor({ step, onCancel, onSave }) {
  const [label, setLabel] = useState(step.label || '')
  const [description, setDescription] = useState(step.description || '')
  const [operation, setOperation] = useState(step.operation || '')
  const [durationMin, setDurationMin] = useState(
    step.duration_estimate_ms ? Math.round(step.duration_estimate_ms / 1000) : '',
  )
  const [teamMemberId, setTeamMemberId] = useState(step.team_member_id || '')
  const [members, setMembers] = useState(null)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    if (step.step_type === 'human' && members === null) {
      api.getTeamMembers().then(setMembers).catch(() => setMembers([]))
    }
  }, [step.step_type, members])

  async function submit(e) {
    e.preventDefault()
    setSaving(true)
    const patch = { label: label.trim() || 'Untitled step', description: description.trim() || null }
    if (step.step_type === 'agent') patch.operation = operation.trim() || null
    if (step.step_type === 'agent' || step.step_type === 'human') {
      patch.duration_estimate_ms = durationMin === '' ? null : Math.round(Number(durationMin) * 1000)
    }
    if (step.step_type === 'human') {
      patch.team_member_id = teamMemberId === '' ? null : Number(teamMemberId)
    }
    try {
      await onSave(patch)
    } finally {
      setSaving(false)
    }
  }

  const meta = STEP_META[step.step_type] || STEP_META.agent
  return (
    <form className={`wf-step wf-step-editing ${meta.cls}`} onSubmit={submit}>
      <div className="wf-step-rail">
        <span className="wf-step-icon">
          <meta.Icon size={15} />
        </span>
      </div>
      <div className="wf-step-body wf-edit-body">
        <label className="wf-edit-field">
          <span>Title</span>
          <input className="text-input" value={label} onChange={(e) => setLabel(e.target.value)} autoFocus />
        </label>
        <label className="wf-edit-field">
          <span>Description</span>
          <textarea className="text-input" rows={2} value={description} onChange={(e) => setDescription(e.target.value)} />
        </label>
        {step.step_type === 'agent' && (
          <label className="wf-edit-field">
            <span>Operation (tool name)</span>
            <input className="text-input" value={operation} onChange={(e) => setOperation(e.target.value)} placeholder="e.g. send_email" />
          </label>
        )}
        {(step.step_type === 'agent' || step.step_type === 'human') && (
          <label className="wf-edit-field">
            <span>Duration estimate (seconds)</span>
            <input type="number" min="0" className="text-input" value={durationMin} onChange={(e) => setDurationMin(e.target.value)} />
          </label>
        )}
        {step.step_type === 'human' && (
          <label className="wf-edit-field">
            <span>Assign to</span>
            <select className="text-input" value={teamMemberId} onChange={(e) => setTeamMemberId(e.target.value)}>
              <option value="">Unassigned</option>
              {(members || []).map((m) => (
                <option key={m.id} value={m.id}>
                  {m.name}{m.role ? ` · ${m.role}` : ''}
                </option>
              ))}
            </select>
          </label>
        )}
        <div className="wf-edit-actions">
          <button type="submit" className="btn btn-primary btn-sm" disabled={saving}>
            {saving ? 'Saving…' : 'Save'}
          </button>
          <button type="button" className="btn btn-link btn-sm" onClick={onCancel} disabled={saving}>
            Cancel
          </button>
        </div>
      </div>
    </form>
  )
}

// ---------------------------------------------------------------------------
// Insert-between control
// ---------------------------------------------------------------------------

function InsertBetween({ onInsert, disabled, label }) {
  const [open, setOpen] = useState(false)
  return (
    <div className={`wf-insert ${open ? 'is-open' : ''} ${label ? 'wf-insert-labeled' : ''}`}>
      {label ? (
        <button
          type="button"
          className="wf-add-step-btn"
          disabled={disabled}
          onClick={() => setOpen((v) => !v)}
        >
          <PlusIcon size={13} /> {label}
        </button>
      ) : (
        <button
          type="button"
          className="wf-insert-btn"
          disabled={disabled}
          onClick={() => setOpen((v) => !v)}
          aria-label="Insert step"
        >
          <PlusIcon size={13} />
        </button>
      )}
      {open && (
        <div className="wf-insert-menu" onMouseLeave={() => setOpen(false)}>
          {INSERTABLE.map(([type, label]) => (
            <button
              key={type}
              type="button"
              className="wf-insert-item"
              onClick={() => {
                setOpen(false)
                onInsert(type)
              }}
            >
              {label}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Create / generate modal
// ---------------------------------------------------------------------------

function CreateWorkflowModal({ onClose, onCreated }) {
  const [name, setName] = useState('')
  const [agents, setAgents] = useState(null)
  const [serviceName, setServiceName] = useState('')
  const [agentId, setAgentId] = useState('main')
  const [mode, setMode] = useState(null) // 'generate' | 'blank' | 'describe' while busy
  const [error, setError] = useState(null)
  const [tab, setTab] = useState('agent') // 'agent' | 'describe'
  const [description, setDescription] = useState('')

  useEffect(() => {
    api
      .listAgents()
      .then((list) => {
        setAgents(list || [])
        if (list?.length) setServiceName(list[0].service_name)
      })
      .catch((e) => setError(e.message || 'Could not load agents'))
  }, [])

  const selectedGroup = useMemo(
    () => (agents || []).find((g) => g.service_name === serviceName),
    [agents, serviceName],
  )
  const subAgents = (selectedGroup?.agents || []).filter((a) => a.agent_id)
  const hasSubAgents = subAgents.length > 1 || (subAgents[0] && subAgents[0].agent_id !== 'main')

  const agentLabel =
    (selectedGroup && (selectedGroup.display_name || selectedGroup.service_name)) || 'this agent'

  async function generate() {
    setError(null)
    setMode('generate')
    try {
      const wf = await api.generateWorkflow({
        name: name.trim() || `${agentLabel} workflow`,
        agent_service_name: serviceName,
        agent_id: agentId || 'main',
      })
      onCreated(wf)
    } catch (e) {
      setError(e.message || 'Could not generate workflow')
      setMode(null)
    }
  }
  async function blank() {
    setError(null)
    setMode('blank')
    try {
      const wf = await api.createWorkflow({
        name: name.trim() || 'Untitled workflow',
        agent_service_name: serviceName || null,
        agent_id: agentId || 'main',
      })
      onCreated(wf)
    } catch (e) {
      setError(e.message || 'Could not create workflow')
      setMode(null)
    }
  }
  async function fromDescription() {
    setError(null)
    setMode('describe')
    try {
      const wf = await api.createWorkflowFromDescription({
        name: name.trim() || 'Workflow',
        description: description.trim(),
        agent_service_name: serviceName || null,
      })
      onCreated(wf)
    } catch (e) {
      setError(e.message || 'Could not build workflow')
      setMode(null)
    }
  }

  return (
    <div className="modal-backdrop" onClick={mode ? undefined : onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <h2>New workflow</h2>
          {!mode && (
            <button type="button" className="close-btn" onClick={onClose} aria-label="Close">
              ×
            </button>
          )}
        </div>

        {mode === 'generate' || mode === 'describe' ? (
          <div className="modal-loading">
            <Spinner />
            <p>{mode === 'generate' ? `Analyzing ${agentLabel}'s telemetry…` : 'Drafting your workflow…'}</p>
            <p className="modal-loading-sub">
              {mode === 'generate'
                ? 'Reading tool calls, sequences, and time-gaps to draft the steps.'
                : 'Turning your description into steps.'}
            </p>
          </div>
        ) : (
          <div className="modal-body">
            <div className="auth-type-toggle">
              <button
                type="button"
                className={`auth-type-option ${tab === 'agent' ? 'is-active' : ''}`}
                onClick={() => setTab('agent')}
              >
                <strong>From an agent</strong>
                <span>Use its telemetry</span>
              </button>
              <button
                type="button"
                className={`auth-type-option ${tab === 'describe' ? 'is-active' : ''}`}
                onClick={() => setTab('describe')}
              >
                <strong>Describe it</strong>
                <span>AI drafts the steps</span>
              </button>
            </div>

            <label className="wf-edit-field">
              <span>Name</span>
              <input
                className="text-input"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder={tab === 'agent' ? `${agentLabel} workflow` : 'My workflow'}
                autoFocus
              />
            </label>

            {tab === 'agent' ? (
              <>
                <label className="wf-edit-field">
                  <span>Which agent is this workflow for?</span>
                  {agents === null ? (
                    <div className="workflow-side-empty">Loading agents…</div>
                  ) : agents.length === 0 ? (
                    <div className="workflow-side-empty">No agents reporting telemetry yet.</div>
                  ) : (
                    <select
                      className="text-input"
                      value={serviceName}
                      onChange={(e) => {
                        setServiceName(e.target.value)
                        setAgentId('main')
                      }}
                    >
                      {agents.map((g) => (
                        <option key={g.service_name} value={g.service_name}>
                          {g.display_name || g.service_name}
                        </option>
                      ))}
                    </select>
                  )}
                </label>

                {hasSubAgents && (
                  <label className="wf-edit-field">
                    <span>Sub-agent</span>
                    <select className="text-input" value={agentId} onChange={(e) => setAgentId(e.target.value)}>
                      {subAgents.map((a) => (
                        <option key={a.agent_id} value={a.agent_id}>
                          {a.agent_id}
                        </option>
                      ))}
                    </select>
                  </label>
                )}

                {error && <p className="form-error">{error}</p>}

                <div className="modal-actions">
                  <button
                    type="button"
                    className="btn btn-primary"
                    onClick={generate}
                    disabled={!serviceName || mode != null}
                  >
                    Generate workflow
                  </button>
                  <button type="button" className="btn btn-link" onClick={blank} disabled={mode != null}>
                    or start blank
                  </button>
                </div>
              </>
            ) : (
              <>
                <label className="wf-edit-field">
                  <span>Describe the process in plain English</span>
                  <textarea
                    className="text-input"
                    rows={4}
                    value={description}
                    onChange={(e) => setDescription(e.target.value)}
                    placeholder="e.g. A support ticket comes in, the agent drafts a reply, a teammate approves it, then it's sent to the customer."
                  />
                </label>
                {error && <p className="form-error">{error}</p>}
                <div className="modal-actions">
                  <button
                    type="button"
                    className="btn btn-primary"
                    onClick={fromDescription}
                    disabled={!description.trim() || mode != null}
                  >
                    Build with AI
                  </button>
                </div>
              </>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
