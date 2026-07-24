import { useCallback, useEffect, useState } from 'react'
import { api } from './api.js'
import { ArrowLeftIcon, ChevronRightIcon } from './Icons.jsx'
import { LoopList } from './LoopFeed.jsx'
import {
  ATTENTION_STATES,
  WORKFLOW_STRINGS as WS,
  isCreatedToday,
  otherWorkLine,
  stuckCount,
  workflowChip,
  workflowShapeLine,
} from './loops.js'

// The Work tab, level 1: a plain list of declared workflows. Each row is
// name + one muted shape line + one status chip — the drawing lives on the
// workflow page (level 2), not here. Below the list, one muted row links
// to the plain loop list for work no workflow matched. Stuck stays a
// header toggle rendering the same plain rows.

const REFRESH_MS = 30000

function WorkflowRow({ wf, onOpen }) {
  const chip = workflowChip(wf)
  return (
    <button
      type="button"
      className={`wl-row ${chip.warm ? 'is-warm' : ''}`}
      onClick={() => onOpen(wf.id)}
    >
      <span className="wl-body">
        <span className="wl-name">{wf.name}</span>
        <span className="wl-shape">{workflowShapeLine(wf)}</span>
      </span>
      <span className={`wl-chip ${chip.warm ? 'is-warm' : ''}`}>{chip.label}</span>
      <span className="wl-chevron" aria-hidden="true">
        <ChevronRightIcon size={15} />
      </span>
    </button>
  )
}

export default function WorkPage({ view, onViewChange, onOpenAgent, sessionUser, onConnectAgent, onOpenWorkflow, onNewWorkflow }) {
  const [workflows, setWorkflows] = useState(null)
  const [loops, setLoops] = useState(null)
  const [error, setError] = useState(null)
  // Sub-views: the list (default), 'stuck' (header toggle), 'other' (the
  // plain loop list for unmatched work). Legacy persisted values
  // ('loops'/'workflow') mean the list.
  const stuckOnly = view === 'stuck'
  const otherView = view === 'other'

  const load = useCallback(async () => {
    try {
      const [wfs, lps] = await Promise.all([
        api.getWorkflows().catch(() => []),
        api.getLoops(),
      ])
      setWorkflows(Array.isArray(wfs) ? wfs : [])
      setLoops(Array.isArray(lps) ? lps : [])
      setError(null)
    } catch (e) {
      setError(e?.message || 'Could not load work')
      setWorkflows((w) => w ?? [])
      setLoops((l) => l ?? [])
    }
  }, [])

  useEffect(() => {
    load()
    const id = setInterval(load, REFRESH_MS)
    return () => clearInterval(id)
  }, [load])

  if (loops === null || workflows === null) {
    return (
      <div className="dash">
        <div className="dash-skel">
          <span style={{ height: 60 }} />
          <span style={{ height: 220 }} />
        </div>
      </div>
    )
  }

  const stuck = stuckCount(loops)
  const unmatched = loops.filter((l) => l.workflow_id == null)
  const unmatchedToday = unmatched.filter((l) => isCreatedToday(l)).length
  const active = workflows.filter((w) => !w.archived_at)

  // The plain loop list for work no declared workflow matched.
  if (otherView) {
    return (
      <div className="dash">
        <button type="button" className="wf2-back" onClick={() => onViewChange('loops')}>
          <ArrowLeftIcon size={15} /> Work
        </button>
        <div className="wfp-titlerow">
          <h1 className="dash-hello" style={{ margin: 0 }}>
            {WS.otherWork}
          </h1>
        </div>
        <div className="dash-card">
          {unmatched.length === 0 ? (
            <div className="dash-empty pad">Nothing here — every loop matched a workflow.</div>
          ) : (
            <LoopList
              loops={unmatched}
              onOpenAgent={onOpenAgent}
              sessionUser={sessionUser}
              onChanged={load}
            />
          )}
        </div>
      </div>
    )
  }

  return (
    <div className="dash">
      <div className="wfp-titlerow">
        <h1 className="dash-hello" style={{ margin: 0 }}>
          Work
        </h1>
        <span className="board-head-actions">
          {sessionUser && (
            <button type="button" className="btn btn-secondary btn-sm" onClick={onNewWorkflow}>
              {WS.newWorkflow}
            </button>
          )}
          <button
            type="button"
            className={`board-stuck-toggle ${stuckOnly ? 'is-on' : ''}`}
            aria-pressed={stuckOnly}
            onClick={() => onViewChange(stuckOnly ? 'loops' : 'stuck')}
          >
            Stuck{stuck > 0 ? ` · ${stuck}` : ''}
          </button>
        </span>
      </div>

      {stuckOnly ? (
        <div className="dash-card">
          {stuck === 0 ? (
            <div className="dash-empty pad">Nothing is stuck. All loops are moving.</div>
          ) : (
            <LoopList
              loops={loops.filter((l) => ATTENTION_STATES.includes(l.cached_state))}
              onOpenAgent={onOpenAgent}
              sessionUser={sessionUser}
              headline
              onChanged={load}
            />
          )}
        </div>
      ) : (
        <>
          {error && active.length === 0 && loops.length === 0 ? (
            <div className="dash-card">
              <div className="dash-empty pad">{error}</div>
            </div>
          ) : active.length === 0 ? (
            <div className="dash-card">
              <div className="dash-empty pad">
                {WS.listEmpty}{' '}
                {sessionUser ? (
                  <button type="button" className="btn-link-inline" onClick={onNewWorkflow}>
                    {WS.listEmptyCta}
                  </button>
                ) : loops.length === 0 ? (
                  <button type="button" className="btn-link-inline" onClick={onConnectAgent}>
                    Connect an agent to start the record →
                  </button>
                ) : null}
              </div>
            </div>
          ) : (
            <div className="dash-card wl-list">
              {active.map((wf) => (
                <WorkflowRow key={wf.id} wf={wf} onOpen={onOpenWorkflow} />
              ))}
            </div>
          )}

          {unmatched.length > 0 && (
            <button type="button" className="wl-other" onClick={() => onViewChange('other')}>
              {otherWorkLine(unmatchedToday)}
            </button>
          )}
        </>
      )}
    </div>
  )
}
