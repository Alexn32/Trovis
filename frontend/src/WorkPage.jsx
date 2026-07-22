import { useCallback, useEffect, useState } from 'react'
import { api } from './api.js'
import { ActivityRow, LoopList, WorkflowRollupRow } from './LoopFeed.jsx'
import StuckPage from './StuckPage.jsx'
import { groupLoopsByWorkflow, parseTs, sortLoopsAttentionFirst, stuckCount } from './loops.js'

// The Work tab — one home for everything the agents are doing, at three
// zoom levels behind a segmented control:
//   Loops (default)  — the loop feed exactly as shipped: attention-first,
//                      inline expansion, Mark done, Ungrouped activity below.
//   By workflow      — the same loops rolled up per agent identity
//                      (client-side group-by; see loops.workflowGroupKey for
//                      the v1 heuristic). No extra fetching.
//   Stuck            — the stalled/awaiting-you filter (StuckPage embedded),
//                      with a live count on the segment label.
// This tab replaced the separate "Workflows" and "Stuck" tabs; the legacy
// workflow-mapping visualization (Workflows.jsx) is parked awaiting
// reintegration under the By-workflow view.

const REFRESH_MS = 30000

// Same divider helper as the Work Feed overlay.
function dayLabel(iso) {
  const d = new Date(parseTs(iso))
  if (Number.isNaN(d.getTime())) return 'Earlier'
  const startOf = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime()
  const days = Math.round((startOf(new Date()) - startOf(d)) / 86_400_000)
  if (days <= 0) return 'Today'
  if (days === 1) return 'Yesterday'
  if (days < 7) return d.toLocaleDateString(undefined, { weekday: 'long' })
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

const VIEWS = [
  ['loops', 'Loops'],
  ['workflow', 'By workflow'],
  ['stuck', 'Stuck'],
]

export default function WorkPage({ view, onViewChange, onOpenAgent, sessionUser }) {
  const [loops, setLoops] = useState(null)
  const [activity, setActivity] = useState(null)
  const [error, setError] = useState(null)

  const load = useCallback(async () => {
    try {
      const [lps, acts] = await Promise.all([
        api.getLoops(),
        api.getActivity().catch(() => []),
      ])
      setLoops(Array.isArray(lps) ? lps : [])
      setActivity(Array.isArray(acts) ? acts : [])
      setError(null)
    } catch (e) {
      setError(e?.message || 'Could not load work')
      setLoops((l) => l ?? [])
      setActivity((a) => a ?? [])
    }
  }, [])

  useEffect(() => {
    load()
    const id = setInterval(load, REFRESH_MS)
    return () => clearInterval(id)
  }, [load])

  const active = VIEWS.some(([id]) => id === view) ? view : 'loops'
  const stuck = stuckCount(loops || [])

  if (loops === null) {
    return (
      <div className="dash">
        <div className="dash-skel">
          <span style={{ height: 60 }} />
          <span style={{ height: 220 }} />
        </div>
      </div>
    )
  }

  const orderedLoops = sortLoopsAttentionFirst(loops)
  const groups = groupLoopsByWorkflow(orderedLoops)
  const ungrouped = (activity || []).filter((it) => it.loop_id == null)

  return (
    <div className="dash">
      <div className="wfp-titlerow">
        <h1 className="dash-hello" style={{ margin: 0 }}>
          Work
        </h1>
        <div className="wf2-view-toggle" role="tablist" aria-label="Work views">
          {VIEWS.map(([id, label]) => (
            <button
              key={id}
              type="button"
              role="tab"
              aria-selected={active === id}
              className={active === id ? 'is-on' : ''}
              onClick={() => onViewChange(id)}
            >
              {id === 'stuck' && stuck > 0 ? `${label} · ${stuck}` : label}
            </button>
          ))}
        </div>
      </div>

      {active === 'stuck' ? (
        <StuckPage onOpenAgent={onOpenAgent} sessionUser={sessionUser} embedded />
      ) : error && orderedLoops.length === 0 ? (
        <div className="dash-card">
          <div className="dash-empty pad">{error}</div>
        </div>
      ) : orderedLoops.length === 0 ? (
        <div className="dash-card">
          <div className="dash-empty pad">No loops yet. Agent activity will appear here.</div>
        </div>
      ) : active === 'workflow' ? (
        <div className="dash-card">
          <div className="loop-list">
            {groups.map((g) => (
              <WorkflowRollupRow
                key={g.key}
                group={g}
                onOpenAgent={onOpenAgent}
                sessionUser={sessionUser}
                onChanged={load}
              />
            ))}
          </div>
        </div>
      ) : (
        <>
          <section className="dash-section">
            <div className="dash-section-head">
              <span className="dash-section-title">Loops</span>
            </div>
            <div className="dash-card">
              <LoopList
                loops={orderedLoops}
                onOpenAgent={onOpenAgent}
                sessionUser={sessionUser}
                onChanged={load}
              />
            </div>
          </section>

          {ungrouped.length > 0 && (
            <section className="dash-section">
              <div className="dash-section-head">
                <span className="dash-section-title">Ungrouped activity</span>
              </div>
              <div className="dash-card wfp-activity">
                {(() => {
                  let lastDay = null
                  const out = []
                  ungrouped.forEach((it, i) => {
                    const day = dayLabel(it.time)
                    if (day !== lastDay) {
                      lastDay = day
                      out.push(
                        <div key={`d-${i}`} className="wfp-divider">
                          {day}
                        </div>,
                      )
                    }
                    out.push(<ActivityRow key={`a-${i}`} item={it} onOpenAgent={onOpenAgent} />)
                  })
                  return out
                })()}
              </div>
            </section>
          )}
        </>
      )}
    </div>
  )
}
