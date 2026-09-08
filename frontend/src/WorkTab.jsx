import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from './api.js'
import JobDetail from './JobDetail.jsx'
import { WorkLoadFailed } from './ui.jsx'
import {
  holderLabel,
  sortWorkItems,
  workItemStatusLabel,
  workUpdatedLabel,
} from './board.js'
import { partitionLookAt } from './home.js'

// Work home — UX Architecture v1.1 / Design visual-pass-v1.1.
// Overview + suggestions + Monday MAIN TABLE. Not kanban landing.
// Must NOT call /work/summary or /work/board on this path (those starve
// the replica). Board.jsx stays in the repo unused until F4 reopens it.
//
// Status wire value waiting_on_other → label "Waiting on someone".
// Fail-soft AbortSignal (#119): first-load timeout stays on Retry, no
// auto-poll back into Loading.
// Suggest approve/edit/decline never invent a named item. Approve only
// creates via POST /work/suggestions/{id}/approve. Never auto-create a
// named item on load or poll.

const POLL_START_MS = 30000
const POLL_MAX_MS = 120000

const OVERVIEW_PILLS = [
  { key: 'needs_you', label: 'Needs you', tone: 'waiting' },
  { key: 'needs_attention', label: 'Needs attention', tone: 'stuck' },
  { key: 'open', label: 'Open', tone: null },
  { key: 'completed_week', label: 'Done this week', tone: 'quiet' },
]

// Filters Home's cards navigate in with. Kept in the same vocabulary the Home
// tiles use; 'attention' mirrors home.js's rule (stuck + aging waits) so the
// two surfaces show the same rows.
const WORK_FILTER_LABELS = {
  attention: 'Needs attention',
  moving: 'Moving',
  waiting: 'Waiting',
  stuck: 'Stuck',
  done: 'Done',
}

function matchesWorkFilter(row, filter) {
  switch (filter) {
    case 'moving':
      return row.status === 'moving'
    case 'waiting':
      return row.status === 'waiting_on_you' || row.status === 'waiting_on_other'
    case 'stuck':
      return row.status === 'stuck'
    case 'done':
      return row.status === 'done'
    case 'attention': {
      const { needsYou, needsAttention } = partitionLookAt([row])
      return needsYou.length + needsAttention.length > 0
    }
    default:
      return true
  }
}

function rowClass(status) {
  if (status === 'waiting_on_you') return 'work-row is-waiting-you'
  if (status === 'stuck') return 'work-row is-stuck'
  return 'work-row'
}

// Render the contract as the API sent it. Do not re-filter rows to "fix"
// overview totals if /work/items still includes flood until a hotfix.

function OverviewStrip({ overview }) {
  // Counts are the server contract. Do not recompute or clamp them here.
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

function suggestionWhy(s) {
  const who = s.draft_holder?.name
  return [s.why, who ? `with ${who}` : ''].filter(Boolean).join(' · ')
}

function SuggestionRow({ row, busy, onApprove, onDecline, onEdit }) {
  const [editing, setEditing] = useState(false)
  const [title, setTitle] = useState(row.title)
  const why = suggestionWhy(row)
  const blocked = !!busy

  async function saveEdit() {
    const next = title.trim()
    if (!next || next === row.title) {
      setEditing(false)
      setTitle(row.title)
      return
    }
    await onEdit(row.id, { title: next })
    setEditing(false)
  }

  return (
    <li className="work-sug-row">
      <div className="work-sug-copy">
        {editing ? (
          <input
            className="work-sug-input"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            aria-label="Suggestion name"
            disabled={blocked}
          />
        ) : (
          <p className="work-sug-title">{row.title}</p>
        )}
        {why ? <p className="work-sug-why">{why}</p> : null}
      </div>
      <div className="work-sug-actions">
        {editing ? (
          <>
            <button type="button" className="btn btn-primary" disabled={blocked} onClick={saveEdit}>
              Save
            </button>
            <button
              type="button"
              className="btn btn-ghost"
              disabled={blocked}
              onClick={() => {
                setTitle(row.title)
                setEditing(false)
              }}
            >
              Cancel
            </button>
          </>
        ) : (
          <>
            <button
              type="button"
              className="btn btn-primary"
              disabled={blocked}
              onClick={() => onApprove(row.id)}
            >
              Approve
            </button>
            <button
              type="button"
              className="btn btn-ghost"
              disabled={blocked}
              onClick={() => {
                setTitle(row.title)
                setEditing(true)
              }}
            >
              Edit
            </button>
            <button
              type="button"
              className="work-sug-decline"
              disabled={blocked}
              onClick={() => onDecline(row.id)}
            >
              Decline
            </button>
          </>
        )}
      </div>
    </li>
  )
}

function SuggestionsStrip({ suggestions, busyId, note, onApprove, onDecline, onEdit }) {
  const rows = suggestions || []
  if (!rows.length) return null
  return (
    <section className="work-suggestions" aria-label="Suggestions">
      <h2 className="work-suggestions-title">Suggestions</h2>
      <ul className="work-suggestions-list">
        {rows.map((s) => (
          <SuggestionRow
            key={s.id}
            row={s}
            busy={busyId === s.id}
            onApprove={onApprove}
            onDecline={onDecline}
            onEdit={onEdit}
          />
        ))}
      </ul>
      {note ? <p className="work-sug-note">{note}</p> : null}
    </section>
  )
}

function OverviewSkeleton() {
  return (
    <div className="work-overview" aria-busy="true" aria-label="Loading overview">
      <span className="work-skel-pill" />
      <span className="work-skel-pill" />
      <span className="work-skel-pill" />
      <span className="work-skel-pill" />
    </div>
  )
}

function TableSkeleton() {
  return (
    <div className="work-skel-table" aria-busy="true" aria-label="Loading work">
      <span />
      <span />
      <span />
      <span />
    </div>
  )
}

function WorkHome({
  onConnectAgent,
  overview,
  overviewErr,
  onRetryOverview,
  items,
  itemsErr,
  onRetryItems,
  suggestions,
  nextCursor,
  onLoadMore,
  busyId,
  suggestionNote,
  onApproveSuggestion,
  onDeclineSuggestion,
  onEditSuggestion,
  filter,
  onClearFilter,
  onItemResolved,
}) {
  const [open, setOpen] = useState(null)
  const all = sortWorkItems(items || [])
  const rows = filter ? all.filter((r) => matchesWorkFilter(r, filter)) : all
  const empty = !!items && rows.length === 0 && (!overview || (overview.open || 0) === 0)

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
        {/* Arriving from a Home card. Always dismissible — a filter you cannot
            see or clear is just a table that looks broken. */}
        {filter && (
          <button
            type="button"
            className="work-filter-chip"
            onClick={onClearFilter}
            aria-label={`Clear the ${WORK_FILTER_LABELS[filter] || filter} filter`}
          >
            {WORK_FILTER_LABELS[filter] || filter}
            <span aria-hidden="true">×</span>
          </button>
        )}
      </header>

      {overview && <OverviewStrip overview={overview} />}
      {!overview && overviewErr && (
        <div className="work-section-failed">
          <WorkLoadFailed lead="Can't load these counts" onRetry={onRetryOverview} />
        </div>
      )}
      {!overview && !overviewErr && <OverviewSkeleton />}
      <SuggestionsStrip
        suggestions={suggestions}
        busyId={busyId}
        note={suggestionNote}
        onApprove={onApproveSuggestion}
        onDecline={onDeclineSuggestion}
        onEdit={onEditSuggestion}
      />

      {itemsErr && !items && (
        <div className="work-section-failed">
          <WorkLoadFailed lead="Can't load this work" onRetry={onRetryItems} />
        </div>
      )}
      {!items && !itemsErr && <TableSkeleton />}

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

      {items && !empty && (
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
              <span className="work-td-holder">{holderLabel(row.holder, row.status)}</span>
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

      {open && (
        <JobDetail
          item={open}
          onClose={() => setOpen(null)}
          onResolved={() => {
            setOpen(null)
            // A resolved handoff changes the table underneath it.
            if (onItemResolved) onItemResolved()
          }}
        />
      )}
    </div>
  )
}

// `active` is false while the Work pane is off screen. App.jsx keeps every tab
// pane mounted (see its keep-alive note), so an inactive Work tab is hidden,
// not unmounted: the poll below skips its tick while hidden — exactly what it
// already does for a backgrounded browser tab — instead of refetching for a
// pane nobody can see. Defaults to true so other callers behave as before.
export default function WorkTab({
  onConnectAgent,
  onNewWorkflow,
  active = true,
  // { value, nonce } from a Home card. The nonce matters: keep-alive means
  // this component is never remounted, so re-clicking the SAME tile after
  // clearing the chip has to re-apply — an unchanged `value` alone would not.
  incomingFilter = null,
}) {
  const connectAgent = onConnectAgent || onNewWorkflow
  const [filter, setFilter] = useState(null)

  const filterNonce = incomingFilter?.nonce
  useEffect(() => {
    if (filterNonce === undefined) return
    setFilter(incomingFilter?.value || null)
    // Only the nonce drives this: a Home card sets it on every navigation.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filterNonce])

  const [overview, setOverview] = useState(null)
  const [items, setItems] = useState(null)
  const [suggestions, setSuggestions] = useState([])
  const [nextCursor, setNextCursor] = useState(null)
  const [overviewErr, setOverviewErr] = useState(null)
  const [itemsErr, setItemsErr] = useState(null)
  const [busyId, setBusyId] = useState(null)
  const [suggestionNote, setSuggestionNote] = useState(null)

  const overviewFailSoftRef = useRef(false)
  const itemsFailSoftRef = useRef(false)
  const failSoftRef = useRef(false)
  // Read by the poll timer, which is scheduled once — a ref so a tab switch
  // doesn't tear down and restart the interval.
  const activeRef = useRef(active)
  activeRef.current = active
  overviewFailSoftRef.current = !overview && !!overviewErr
  itemsFailSoftRef.current = !items && !!itemsErr
  failSoftRef.current = overviewFailSoftRef.current && itemsFailSoftRef.current

  const loadOverview = useCallback(async () => {
    try {
      const ov = await api.getWorkOverview()
      setOverview(ov)
      setOverviewErr(null)
    } catch (e) {
      setOverviewErr(e?.message || "Can't load these counts")
      throw e
    }
  }, [])

  const loadItems = useCallback(async () => {
    try {
      const page = await api.getWorkItems({ limit: 50 })
      setItems(Array.isArray(page?.items) ? page.items : [])
      setNextCursor(page?.next_cursor || null)
      setItemsErr(null)
    } catch (e) {
      setItemsErr(e?.message || "Can't load this work")
      throw e
    }
  }, [])

  const loadSuggestions = useCallback(() => {
    api
      .getWorkSuggestions()
      .then((s) => setSuggestions(Array.isArray(s?.suggestions) ? s.suggestions : []))
      .catch(() => {})
  }, [])

  const load = useCallback(async () => {
    const tasks = []
    if (!overviewFailSoftRef.current) tasks.push(loadOverview())
    if (!itemsFailSoftRef.current) tasks.push(loadItems())
    loadSuggestions()
    const results = await Promise.allSettled(tasks)
    if (results.some((r) => r.status === 'rejected')) {
      throw new Error('section failed')
    }
  }, [loadOverview, loadItems, loadSuggestions])

  function retryOverview() {
    setOverviewErr(null)
    loadOverview().catch(() => {})
  }

  function retryItems() {
    setItemsErr(null)
    loadItems().catch(() => {})
  }

  async function refreshNamedWork() {
    await Promise.allSettled([loadOverview(), loadItems()])
  }

  async function approveSuggestion(id) {
    // Official approve endpoint creates the named item. Never invent one here.
    setBusyId(id)
    setSuggestionNote(null)
    try {
      await api.approveWorkSuggestion(id)
      setSuggestions((prev) => prev.filter((s) => s.id !== id))
      await refreshNamedWork()
    } catch (e) {
      // 404 = mutations not shipped yet (GET stub only). Do not create a row.
      setSuggestionNote(e?.message || "Can't update this suggestion")
    } finally {
      setBusyId(null)
    }
  }

  async function declineSuggestion(id) {
    setBusyId(id)
    setSuggestionNote(null)
    try {
      await api.declineWorkSuggestion(id)
      setSuggestions((prev) => prev.filter((s) => s.id !== id))
    } catch (e) {
      if (e?.status === 404) {
        // Stub or already gone — drop from the strip. No work item.
        setSuggestions((prev) => prev.filter((s) => s.id !== id))
      } else {
        setSuggestionNote(e?.message || "Can't update this suggestion")
      }
    } finally {
      setBusyId(null)
    }
  }

  async function editSuggestion(id, patch) {
    setBusyId(id)
    setSuggestionNote(null)
    try {
      const updated = await api.editWorkSuggestion(id, patch)
      setSuggestions((prev) =>
        prev.map((s) => (s.id === id ? { ...s, ...(updated || patch) } : s)),
      )
    } catch (e) {
      if (e?.status === 404) {
        // Mutations not shipped — keep the edit in the strip only. No create.
        setSuggestions((prev) =>
          prev.map((s) => (s.id === id ? { ...s, ...patch } : s)),
        )
      } else {
        setSuggestionNote(e?.message || "Can't update this suggestion")
      }
    } finally {
      setBusyId(null)
    }
  }

  useEffect(() => {
    load().catch(() => {})
    let delay = POLL_START_MS
    let timer
    function schedule() {
      timer = setTimeout(() => {
        if (failSoftRef.current || document.hidden || !activeRef.current) {
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

  return (
    <WorkHome
      onConnectAgent={connectAgent}
      overview={overview}
      overviewErr={overviewErr}
      onRetryOverview={retryOverview}
      items={items}
      itemsErr={itemsErr}
      onRetryItems={retryItems}
      suggestions={suggestions}
      nextCursor={nextCursor}
      onLoadMore={loadMore}
      busyId={busyId}
      suggestionNote={suggestionNote}
      onApproveSuggestion={approveSuggestion}
      onDeclineSuggestion={declineSuggestion}
      onEditSuggestion={editSuggestion}
      filter={filter}
      onClearFilter={() => setFilter(null)}
      onItemResolved={refreshNamedWork}
    />
  )
}
