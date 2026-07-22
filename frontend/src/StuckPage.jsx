import { useCallback, useEffect, useState } from 'react'
import { api } from './api.js'
import { LoopList } from './LoopFeed.jsx'

// The Stuck tab: every loop that needs a human — stalled, or explicitly
// waiting on you — oldest stall first (the backend orders it; the age is
// the headline fact on each row). Same loop rows as the Work Feed, same
// 30s refresh cadence.

const REFRESH_MS = 30000

// `embedded` drops the page chrome (title row) when rendered as the Work
// tab's Stuck view rather than a standalone page.
export default function StuckPage({ onOpenAgent, sessionUser, embedded = false }) {
  const [loops, setLoops] = useState(null)
  const [error, setError] = useState(null)

  const load = useCallback(async () => {
    try {
      const rows = await api.getStalledLoops()
      setLoops(Array.isArray(rows) ? rows : [])
      setError(null)
    } catch (e) {
      setError(e?.message || 'Could not load stuck loops')
      setLoops((l) => l ?? [])
    }
  }, [])

  useEffect(() => {
    load()
    const id = setInterval(load, REFRESH_MS)
    return () => clearInterval(id)
  }, [load])

  if (loops === null) {
    return (
      <div className={embedded ? '' : 'dash'}>
        <div className="dash-skel">
          <span style={{ height: 60 }} />
          <span style={{ height: 220 }} />
        </div>
      </div>
    )
  }

  return (
    <div className={embedded ? '' : 'dash'}>
      {!embedded && (
        <div className="wfp-titlerow">
          <h1 className="dash-hello" style={{ margin: 0 }}>
            Stuck
          </h1>
          {loops.length > 0 && (
            <span className="wfp-sub">
              {loops.length === 1 ? '1 loop needs you' : `${loops.length} loops need you`}
            </span>
          )}
        </div>
      )}

      {error && loops.length === 0 ? (
        <div className="dash-card">
          <div className="dash-empty pad">{error}</div>
        </div>
      ) : loops.length === 0 ? (
        <div className="dash-card">
          <div className="dash-empty pad">Nothing is stuck. All loops are moving.</div>
        </div>
      ) : (
        <div className="dash-card">
          <LoopList
            loops={loops}
            onOpenAgent={onOpenAgent}
            sessionUser={sessionUser}
            headline
            onChanged={load}
          />
        </div>
      )}
    </div>
  )
}
