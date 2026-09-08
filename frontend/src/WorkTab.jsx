import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from './api.js'
import { WorkLoadFailed } from './ui.jsx'
import { workItemStatusLabel } from './board.js'

// Work home — lean overview + named items. Must NOT call /work/summary or
// /work/board (those starve the replica). Frontend owns the Monday table
// polish; this is a minimal adapter against the locked shapes.
//
// Status wire value waiting_on_other → label "Waiting on someone".
// Fail-soft AbortSignal (#119): first-load timeout stays on Retry, no
// auto-poll back into Loading.

const POLL_START_MS = 30000
const POLL_MAX_MS = 120000

function fmtUpdated(iso) {
  if (!iso) return ''
  const ms = Date.now() - Date.parse(iso)
  if (Number.isNaN(ms)) return ''
  const m = Math.floor(ms / 60000)
  if (m < 1) return 'just now'
  if (m < 60) return `${m}m ago`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ago`
  return `${Math.floor(h / 24)}d ago`
}

function CountChip({ label, value, tone }) {
  const n = value || 0
  return (
    <div className={`work-chip${tone && n ? ` is-${tone}` : ''}`}>
      <span className="work-chip-value">{n}</span>
      <span className="work-chip-label">{label}</span>
    </div>
  )
}

export default function WorkTab({ onConnectAgent }) {
  const [overview, setOverview] = useState(null)
  const [items, setItems] = useState(null)
  const [nextCursor, setNextCursor] = useState(null)
  const [err, setErr] = useState(null)

  const failSoftRef = useRef(false)
  failSoftRef.current = !overview && !items && !!err

  const load = useCallback(async () => {
    try {
      const [ov, page] = await Promise.all([
        api.getWorkOverview(),
        api.getWorkItems({ limit: 50 }),
      ])
      setOverview(ov)
      setItems(Array.isArray(page?.items) ? page.items : [])
      setNextCursor(page?.next_cursor || null)
      setErr(null)
    } catch (e) {
      setErr(e?.message || 'Could not load your work')
      throw e
    }
  }, [])

  function retry() {
    setErr(null)
    load().catch(() => {})
  }

  useEffect(() => {
    load().catch(() => {})
    let delay = POLL_START_MS
    let timer
    function schedule() {
      timer = setTimeout(() => {
        if (failSoftRef.current || document.hidden) {
          schedule()
          return
        }
        load()
          .then(() => {
            delay = POLL_START_MS
          })
          .catch(() => {
            delay = Math.min(delay * 2, POLL_MAX_MS)
          })
          .finally(schedule)
      }, delay)
    }
    schedule()
    return () => clearTimeout(timer)
  }, [load])

  async function loadMore() {
    if (!nextCursor) return
    try {
      const page = await api.getWorkItems({ cursor: nextCursor, limit: 50 })
      setItems((prev) => [...(prev || []), ...(page?.items || [])])
      setNextCursor(page?.next_cursor || null)
    } catch {
      /* keep last-good rows */
    }
  }

  if (!overview && !items && err) {
    return (
      <div className="view board-view">
        <div className="board-head">
          <h1>Work</h1>
        </div>
        <WorkLoadFailed onRetry={retry} />
      </div>
    )
  }
  if (!overview || !items) {
    return <div className="view board-view"><div className="dash-empty pad">Loading…</div></div>
  }

  const empty = (overview.open || 0) === 0 && items.length === 0

  return (
    <div className="view board-view">
      <div className="board-head">
        <h1>Work</h1>
      </div>

      <div className="work-chips" aria-label="Work overview">
        <CountChip label="Needs you" value={overview.needs_you} tone="warn" />
        <CountChip label="Needs attention" value={overview.needs_attention} tone="stuck" />
        <CountChip label="Open" value={overview.open} />
        <CountChip label="Completed this week" value={overview.completed_week} />
      </div>

      {empty && (
        <div className="board-empty">
          <p className="board-empty-lead">No named work yet.</p>
          <p className="board-empty-sub">
            Connect an agent and titled tasks show up here on their own. You will not have to enter any of it.
          </p>
          {onConnectAgent && (
            <button type="button" className="btn btn-primary" onClick={onConnectAgent}>
              Connect an agent
            </button>
          )}
        </div>
      )}

      {!empty && (
        <div className="work-table-wrap">
          <table className="work-table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Status</th>
                <th>Holder</th>
                <th>What&apos;s next</th>
                <th>Updated</th>
              </tr>
            </thead>
            <tbody>
              {items.map((row) => (
                <tr key={row.id} className={row.status === 'waiting_on_you' ? 'is-yours' : ''}>
                  <td className="work-td-title">{row.title}</td>
                  <td>{workItemStatusLabel(row.status)}</td>
                  <td>{row.holder?.name || ''}</td>
                  <td>{row.whats_next}</td>
                  <td className="work-td-updated">{fmtUpdated(row.updated_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {nextCursor && (
            <button type="button" className="btn work-more" onClick={loadMore}>
              Load more
            </button>
          )}
        </div>
      )}
    </div>
  )
}
