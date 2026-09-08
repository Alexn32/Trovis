import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from './api.js'
import Board, { TaskPanel } from './Board.jsx'
import { WorkLoadFailed } from './ui.jsx'
import {
  isNamedWorkTitle,
  sortWorkItems,
  workItemStatusLabel,
  workUpdatedLabel,
} from './board.js'

// Work home — UX Architecture v1.1 / Design visual-pass-v1.1.
// Overview + suggestions + Monday MAIN TABLE. Not kanban landing.
// Must NOT call /work/summary or /work/board on this path (those starve
// the replica). Board.jsx is a secondary view behind "Boards & other views".
//
// Status wire value waiting_on_other → label "Waiting on someone".
// Fail-soft AbortSignal (#119): first-load timeout stays on Retry, no
// auto-poll back into Loading.
// Suggest approve/edit/decline never auto-create a named item.

const POLL_START_MS = 30000
const POLL_MAX_MS = 120000

const OVERVIEW_PILLS = [
  { key: 'needs_you', label: 'Needs you', tone: 'waiting' },
  { key: 'needs_attention', label: 'Needs attention', tone: 'stuck' },
  { key: 'open', label: 'Open', tone: null },
  { key: 'completed_week', label: 'Done this week', tone: 'quiet' },
]

function rowClass(status) {
  if (status === 'waiting_on_you') return 'work-row is-waiting-you'
  if (status === 'stuck') return 'work-row is-stuck'
  return 'work-row'
}

function itemToCard(row) {
  const ms = row.updated_at ? Date.now() - Date.parse(row.updated_at) : NaN
  return {
    id: row.id,
    title: row.title,
    holder_name: row.holder?.name || '',
    holder_type: row.holder?.kind === 'human' ? 'human' : 'agent',
    is_yours: row.status === 'waiting_on_you',
    age_seconds: Number.isNaN(ms) ? null : Math.max(0, Math.floor(ms / 1000)),
    standing: false,
    standing_reason: null,
  }
}

function namedRows(items) {
  return sortWorkItems((items || []).filter((row) => isNamedWorkTitle(row.title)))
}

function OverviewStrip({ overview }) {
  return (
    <div className="work-overview" aria-label="Work overview">
      {OVERVIEW_PILLS.map((p) => {
        const n = overview[p.key] || 0
        const tone = p.tone && n ? p.tone : p.tone === 'quiet' ? 'quiet' : null
        return (
          <div
            key={p.key}
            className={`work-pill${tone ? ` is-${tone}` : ''}`}
          >
            <span className="work-pill-label">{p.label}</span>
            <span className="work-pill-value">{n}</span>
          </div>
        )
      })}
    </div>
  )
}

function SuggestionsStrip({ suggestions, onDismiss }) {
  const rows = (suggestions || []).filter((s) => isNamedWorkTitle(s.title))
  if (!rows.length) return null
  return (
    <section className="work-suggestions" aria-label="Suggestions">
      <h2 className="work-suggestions-title">Suggestions</h2>
      <ul className="work-suggestions-list">
        {rows.map((s) => {
          const who = s.draft_holder?.name
          const why = [s.why, who ? `with ${who}` : ''].filter(Boolean).join(' · ')
          return (
            <li key={s.id} className="work-sug-row">
              <div className="work-sug-copy">
                <p className="work-sug-title">{s.title}</p>
                {why ? <p className="work-sug-why">{why}</p> : null}
              </div>
              <div className="work-sug-actions">
                <button
                  type="button"
                  className="btn btn-primary"
                  onClick={() => onDismiss(s.id)}
                >
                  Approve
                </button>
                <button type="button" className="btn btn-ghost" disabled>
                  Edit
                </button>
                <button
                  type="button"
                  className="work-sug-decline"
                  onClick={() => onDismiss(s.id)}
                >
                  Decline
                </button>
              </div>
            </li>
          )
        })}
      </ul>
    </section>
  )
}

function WorkSkeleton() {
  return (
    <div className="view work-home" aria-busy="true" aria-label="Loading work">
      <h1>Work</h1>
      <div className="work-overview">
        <span className="work-skel-pill" />
        <span className="work-skel-pill" />
        <span className="work-skel-pill" />
        <span className="work-skel-pill" />
      </div>
      <div className="work-skel-table">
        <span />
        <span />
        <span />
        <span />
      </div>
    </div>
  )
}

function WorkHome({
  onConnectAgent,
  onOpenBoards,
  overview,
  items,
  suggestions,
  nextCursor,
  onLoadMore,
  onDismissSuggestion,
}) {
  const [open, setOpen] = useState(null)
  const rows = namedRows(items)
  const empty = (overview.open || 0) === 0 && rows.length === 0

  function onRowKey(e, row) {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault()
      setOpen(row)
    }
  }

  return (
    <div className="view work-home">
      <header className="work-home-head">
        <h1>Work</h1>
      </header>

      <OverviewStrip overview={overview} />
      <SuggestionsStrip suggestions={suggestions} onDismiss={onDismissSuggestion} />

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
        <div className="work-table" role="table" aria-label="Work">
          <div className="work-table-head" role="row">
            <span role="columnheader">Task</span>
            <span role="columnheader">Status</span>
            <span role="columnheader">Holder</span>
            <span role="columnheader">What&apos;s next</span>
            <span role="columnheader">Updated</span>
          </div>
          {rows.map((row) => (
            <div
              key={row.id}
              className={rowClass(row.status)}
              role="button"
              tabIndex={0}
              onClick={() => setOpen(row)}
              onKeyDown={(e) => onRowKey(e, row)}
              aria-label={row.title}
            >
              <span className="work-td-title">{row.title}</span>
              <span className={`work-status-pill ${row.status || ''}`}>
                {workItemStatusLabel(row.status)}
              </span>
              <span className="work-td-holder">{row.holder?.name || ''}</span>
              <span className="work-td-next">{row.whats_next || ''}</span>
              <span className="work-td-updated">{workUpdatedLabel(row.updated_at)}</span>
            </div>
          ))}
          {nextCursor && (
            <button type="button" className="btn work-more" onClick={onLoadMore}>
              Load more
            </button>
          )}
        </div>
      )}

      {onOpenBoards && (
        <button type="button" className="work-other-views" onClick={onOpenBoards}>
          Boards & other views →
        </button>
      )}

      {open && (
        <TaskPanel
          card={itemToCard(open)}
          onClose={() => setOpen(null)}
          onResolved={() => setOpen(null)}
        />
      )}
    </div>
  )
}

export default function WorkTab({ onConnectAgent, onOpenWorkflow }) {
  const [surface, setSurface] = useState('home')
  const [overview, setOverview] = useState(null)
  const [items, setItems] = useState(null)
  const [suggestions, setSuggestions] = useState([])
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
    // Suggestions are a stub; never block home, never invent rows.
    api
      .getWorkSuggestions()
      .then((s) => setSuggestions(Array.isArray(s?.suggestions) ? s.suggestions : []))
      .catch(() => {})
  }, [])

  function retry() {
    setErr(null)
    load().catch(() => {})
  }

  function dismissSuggestion(id) {
    // Local dismiss only. Approve/edit/decline must not create a named item.
    setSuggestions((prev) => prev.filter((s) => s.id !== id))
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

  if (surface === 'board') {
    return (
      <Board
        onConnectAgent={onConnectAgent}
        onOpenWorkflow={onOpenWorkflow}
        onBack={() => setSurface('home')}
      />
    )
  }

  if (!overview && !items && err) {
    return (
      <div className="view work-home">
        <header className="work-home-head">
          <h1>Work</h1>
        </header>
        <WorkLoadFailed onRetry={retry} />
      </div>
    )
  }
  if (!overview || !items) {
    return <WorkSkeleton />
  }

  return (
    <WorkHome
      onConnectAgent={onConnectAgent}
      onOpenBoards={() => setSurface('board')}
      overview={overview}
      items={items}
      suggestions={suggestions}
      nextCursor={nextCursor}
      onLoadMore={loadMore}
      onDismissSuggestion={dismissSuggestion}
    />
  )
}
