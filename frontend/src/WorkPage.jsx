import { useCallback, useEffect, useState } from 'react'
import { api } from './api.js'
import { ActivityRow } from './LoopFeed.jsx'
import LoopBoard from './LoopBoard.jsx'
import { ATTENTION_STATES, parseTs, stuckCount } from './loops.js'

// The Work tab: the board. Loops group under workflow headers — the board
// IS the loop feed and the by-workflow view in one — and clicking a row
// opens the loop's story. Stuck is a filter toggle on the board header
// (with its live count), not a separate view. Below the board, loopless
// spans survive as "Ungrouped activity" until pre-loop data ages out.

const REFRESH_MS = 30000

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

export default function WorkPage({ view, onViewChange, onOpenAgent, sessionUser, onConnectAgent }) {
  const [loops, setLoops] = useState(null)
  const [activity, setActivity] = useState(null)
  const [error, setError] = useState(null)
  // Legacy persisted sub-views ('loops'/'workflow') both mean the board now;
  // 'stuck' arrives as the filter being on.
  const stuckOnly = view === 'stuck'

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

  const stuck = stuckCount(loops)
  const shown = stuckOnly
    ? loops.filter((l) => ATTENTION_STATES.includes(l.cached_state))
    : loops
  const ungrouped = (activity || []).filter((it) => it.loop_id == null)

  return (
    <div className="dash">
      <div className="wfp-titlerow">
        <h1 className="dash-hello" style={{ margin: 0 }}>
          Work
        </h1>
        <button
          type="button"
          className={`board-stuck-toggle ${stuckOnly ? 'is-on' : ''}`}
          aria-pressed={stuckOnly}
          onClick={() => onViewChange(stuckOnly ? 'loops' : 'stuck')}
        >
          Stuck{stuck > 0 ? ` · ${stuck}` : ''}
        </button>
      </div>

      {error && loops.length === 0 ? (
        <div className="dash-card">
          <div className="dash-empty pad">{error}</div>
        </div>
      ) : loops.length === 0 ? (
        <div className="dash-card">
          <div className="dash-empty pad">
            No loops yet.{' '}
            <button type="button" className="btn-link-inline" onClick={onConnectAgent}>
              Connect an agent to start the record →
            </button>
          </div>
        </div>
      ) : stuckOnly && shown.length === 0 ? (
        <div className="dash-card">
          <div className="dash-empty pad">Nothing is stuck. All loops are moving.</div>
        </div>
      ) : (
        <LoopBoard
          loops={shown}
          sessionUser={sessionUser}
          onOpenAgent={onOpenAgent}
          onChanged={load}
        />
      )}

      {!stuckOnly && ungrouped.length > 0 && (
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
    </div>
  )
}
