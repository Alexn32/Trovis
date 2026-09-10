import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from './api.js'
import { startAbortable } from './abortable.js'
import JobDetail from './JobDetail.jsx'
import { WorkLoadFailed } from './ui.jsx'
import {
  holderLabel,
  hottestOpen,
  kindPath,
  matchesKind,
  pastRuns,
  sortWorkItems,
  workItemStatusLabel,
  workUpdatedLabel,
} from './board.js'
import { partitionLookAt } from './home.js'
import {
  COLUMNS, applyScope, boardTotals, ageLabel, groupByJob,
  healthBadge, observedPerDay, quietLine, runCardLead,
} from './workBoard.js'
import { QuietBrand } from './BrandMarks.jsx'

// Work — three levels: the board (grouped by job), a job page, a run page.
//
// The board's counts are DERIVED from the run rows it draws, not fetched
// separately: two numbers on one screen disagreeing is the failure this
// product sells against, and /work/overview was a second source for the same
// facts. It is no longer called from here.
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

// Filters Home's cards navigate in with. Kept in the same vocabulary the Home
// tiles use; 'attention' mirrors home.js's rule (stuck + aging waits) so the
// two surfaces show the same rows.
const WORK_FILTER_LABELS = {
  attention: 'Needs attention',
  // Home's desk is only YOUR waits, so its "Open in Work" has to land on the
  // same set — 'waiting' is everyone's, which would be a wider list than the
  // one you just tapped away from.
  mine: 'Waiting on you',
  moving: 'Moving',
  waiting: 'Waiting',
  stuck: 'Stuck',
  done: 'Done',
}

function matchesWorkFilter(row, filter) {
  switch (filter) {
    case 'moving':
      return row.status === 'moving'
    case 'mine':
      return row.status === 'waiting_on_you'
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

/**
 * Band A — the path.
 *
 * A spine of the hands this kind's work passes through. The ORDER is
 * canonical (agent → tool → person → done), not observed: a list row says
 * where a job sits now, never the sequence it took, and learning the real
 * sequence means one detail fetch per row. Absent when it cannot be drawn
 * honestly — see kindPath.
 */
function PathBand({ path }) {
  if (!path) return null
  return (
    <section className="kind-band" aria-label="Path">
      <h2 className="dash-caps">How this work moves</h2>
      <ol className="kind-path">
        {path.map((n, i) => (
          <li key={`${n.kind}-${i}`} className={`kind-node kind-${n.kind}`}>
            <span className="kind-node-label">{n.label}</span>
            {/* Exceptions only. A node with nothing waiting and nothing stuck
                stays quiet. */}
            {n.stuck > 0 && <span className="kind-node-flag is-stuck">{n.stuck} stuck</span>}
            {n.waiting > 0 && (
              <span className="kind-node-flag is-waiting">{n.waiting} waiting on a person</span>
            )}
          </li>
        ))}
      </ol>
    </section>
  )
}

/** Band B — what is live for this kind, as one block, not one per row. */
function LiveBand({ kindName, open, hottest, onOpenItem }) {
  if (open.length === 0) {
    return (
      <section className="kind-band" aria-label="Now">
        <h2 className="dash-caps">Now</h2>
        <p className="kind-quiet">No {kindName.toLowerCase()} running.</p>
      </section>
    )
  }
  return (
    <section className="kind-band" aria-label="Now">
      <h2 className="dash-caps">Now</h2>
      <p className="kind-live-lead">
        {open.length} open
        {hottest && (
          <>
            <span className="dash-dot-sep">·</span>
            {workItemStatusLabel(hottest.status).toLowerCase()} with{' '}
            {holderLabel(hottest.holder, hottest.status) || 'an agent'}
          </>
        )}
      </p>
      {hottest && (
        <button type="button" className="kind-live-row" onClick={() => onOpenItem(hottest)}>
          <span className="kind-live-title">{hottest.title}</span>
          <span className="kind-live-meta">
            {hottest.whats_next}
            {hottest.updated_at && (
              <>
                <span className="dash-dot-sep">·</span>
                {workUpdatedLabel(hottest.updated_at)}
              </>
            )}
          </span>
        </button>
      )}
    </section>
  )
}

/**
 * Band C — past runs. The technical door.
 *
 * Absent when the record has no finished or stuck work for this kind, rather
 * than an empty frame. "Sent back" is not offered: it lives in an item's own
 * history, and naming an outcome we did not observe is worse than naming the
 * two we did.
 */
function PastRunsBand({ runs, loading, onOpenItem }) {
  if (loading) {
    return (
      <section className="kind-band" aria-label="Past runs">
        <h2 className="dash-caps">Past runs</h2>
        <div className="dash-skel"><span style={{ width: '60%' }} /></div>
      </section>
    )
  }
  if (runs.length === 0) return null
  return (
    <section className="kind-band" aria-label="Past runs">
      <h2 className="dash-caps">Past runs</h2>
      <ul className="kind-runs">
        {runs.map((r) => (
          <li key={r.id}>
            <button type="button" className="kind-run" onClick={() => onOpenItem(r.item)}>
              <span className="kind-run-when">{r.at ? workUpdatedLabel(r.at) : ''}</span>
              <span className={`kind-run-result is-${r.status}`}>{r.result}</span>
              <span className="kind-run-what">{r.title}</span>
              {r.reason && <span className="kind-run-why">{r.reason}</span>}
            </button>
          </li>
        ))}
      </ul>
    </section>
  )
}


/**
 * The inventory table. Shared by Work home and the Kind page so the two
 * genuinely are the same table — same columns, same sort, same row-opens-
 * JobDetail — rather than two that merely look alike and drift.
 */
function WorkTable({ rows, onOpen, nextCursor, onLoadMore }) {
  function onRowKey(e, row) {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault()
      onOpen(row)
    }
  }
  return (
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
          onClick={() => onOpen(row)}
          onKeyDown={(e) => onRowKey(e, row)}
          aria-label={row.title}
        >
          <span className="work-td-title">{row.title}</span>
          <span className={`work-status-pill ${row.status || ''}`}>
            {workItemStatusLabel(row.status)}
          </span>
          <span className="work-td-holder">
            <QuietBrand texts={[row.holder?.name]} size={12} />
            {holderLabel(row.holder, row.status)}
          </span>
          <span className="work-td-next">{row.whats_next || ''}</span>
          <span className="work-td-updated">{workUpdatedLabel(row.updated_at)}</span>
        </div>
      ))}
      {nextCursor && onLoadMore && (
        <button type="button" className="btn work-more" onClick={onLoadMore}>
          Load more
        </button>
      )}
    </div>
  )
}

/**
 * The Kind page. One subject: this kind of work.
 *
 * `items` is the page Work home already loaded, filtered to this kind — no
 * refetch for the bands. `finished` is the ONE extra lean request: this
 * kind's closed work, which the main page only carries incidentally.
 */
function KindPage({
  kindName, workflowId, items, finished, finishedLoading,
  filter, onClearFilter, onBack, onOpenItem, onOpenAgent,
}) {
  const mine = items.filter((r) => matchesKind(r, kindName))
  // "Still open" means still open. Finished work is Past runs' subject, and
  // listing it under both headings would make one of the two a lie.
  const openRows = mine.filter((r) => r.status !== 'done')
  const rows = sortWorkItems(
    filter ? openRows.filter((r) => matchesWorkFilter(r, filter)) : openRows,
  )
  const path = kindPath(mine)
  // Past runs are the CLOSED rows and nothing else — only the focused
  // request's payload feeds it. A stuck job is live: it belongs on the path
  // flag, in Now, and in Still open, but listing it under a heading that
  // means the run ended is the page telling two stories about one job.
  // While that request is in flight there are no past runs to show yet;
  // backfilling from open rows would put live work under that heading again.
  const runs = pastRuns(finished || [])
  const filteredOut = rows.length === 0 && openRows.length > 0

  return (
    <div className="view work-kind-page">
      <header className="work-home-head">
        <button type="button" className="wf2-back" onClick={onBack}>
          ← All work
        </button>
        <h1>{kindName}</h1>
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

      <PathBand path={path} />
      <LiveBand
        kindName={kindName}
        open={openRows}
        hottest={hottestOpen(mine)}
        onOpenItem={onOpenItem}
      />
      <PastRunsBand runs={runs} loading={finishedLoading} onOpenItem={onOpenItem} />

      <section className="kind-band" aria-label="Open work">
        <h2 className="dash-caps">Still open</h2>
        {filteredOut ? (
          <div className="board-empty">
            <p className="board-empty-lead">Nothing matches these filters.</p>
            <p className="board-empty-sub">Clear the filter above to see the rest of this work.</p>
          </div>
        ) : rows.length === 0 ? (
          <p className="kind-quiet">Nothing open here.</p>
        ) : (
          <WorkTable rows={rows} onOpen={onOpenItem} />
        )}
      </section>

    </div>
  )
}

// --- the board, grouped by job -------------------------------------------
//
// The job is the object. A run only earns a card when it needs a person;
// everything else is a count, because a wall of equal cards buries the two
// rows that matter and hides the job that stopped running entirely.

function HealthBadge({ badge }) {
  return <span className={`jb-badge tone-${badge.tone}`}>{badge.label}</span>
}

function RunCard({ row, onOpen, now }) {
  const { lead, sub } = runCardLead(row, { now })
  return (
    <button
      type="button"
      className={`jb-card is-${row.status}`}
      onClick={() => onOpen(row)}
      title={row.title || undefined}
    >
      <span className="jb-card-lead">{lead}</span>
      {sub && <span className="jb-card-sub">{sub}</span>}
    </button>
  )
}

/** The counts strip, aligned to the four columns. Every run is in here. */
function CountStrip({ counts }) {
  return (
    <div className="jb-counts" aria-label="Runs by state">
      {COLUMNS.map((c) => (
        <span key={c.key} className={`jb-count tone-${c.key}`}>
          <b>{counts[c.key]}</b> {c.label.toLowerCase()}
        </span>
      ))}
    </div>
  )
}

function JobRow({ row, onOpenJob, onOpenRun, now }) {
  const badge = healthBadge(row, { now })
  const job = row.job
  const perDay = observedPerDay(job)
  // Cadence sits here only when there is an expectation to read it against.
  // Without one the badge already carries the bare observed number, and the
  // same figure twice on one row is noise.
  const cadence =
    perDay !== null && job?.expected_per_day_min != null
      ? `${perDay}/day, expected ${
          job.expected_per_day_max != null
            ? `${job.expected_per_day_min}–${job.expected_per_day_max}`
            : `${job.expected_per_day_min}+`
        }`
      : null
  const meta = [
    job?.owning_service_name || null,
    cadence,
    // Only a real cost. A per-run figure of $0.00 on a job whose spans
    // carried no cost reads as a measurement of zero.
    job?.cost_per_run != null ? `${fmtCost(job.cost_per_run)}/run` : null,
  ].filter(Boolean)

  // A job that ran nothing at all collapses to its header plus when it last
  // ran. This is the row a flat board of live runs cannot draw.
  const idle = row.total === 0
  const lastRan = ageLabel(job?.last_run_at, now)

  return (
    <section className={`jb-row${idle ? ' is-idle' : ''}`} aria-label={row.name}>
      <header className="jb-head">
        {row.isUnmatched ? (
          <span className="jb-name is-unmatched">{row.name}</span>
        ) : (
          <button type="button" className="jb-name" onClick={() => onOpenJob(row)}>
            {row.name}
          </button>
        )}
        {job?.workflow_archived_at && <span className="jb-archived">archived</span>}
        {!row.isUnmatched && <HealthBadge badge={badge} />}
      </header>
      {row.isUnmatched ? (
        <p className="jb-meta">
          {row.total} {row.total === 1 ? 'run' : 'runs'} matched to no job
        </p>
      ) : (
        meta.length > 0 && <p className="jb-meta">{meta.join(' \u00b7 ')}</p>
      )}

      {idle ? (
        <p className="jb-quiet">{lastRan ? `Last ran ${lastRan} ago.` : 'Never run.'}</p>
      ) : (
        <>
          <CountStrip counts={row.counts} />
          {row.exceptions === 0 ? (
            <p className="jb-quiet">{quietLine(row)}</p>
          ) : (
            <div className="jb-grid">
              {COLUMNS.map((c) => (
                <div key={c.key} className="jb-cell">
                  {row.columns[c.key].slice(0, 2).map((r) => (
                    <RunCard key={r.id} row={r} onOpen={onOpenRun} now={now} />
                  ))}
                  {row.columns[c.key].length > 2 && !row.isUnmatched && (
                    <button
                      type="button"
                      className="jb-more"
                      onClick={() => onOpenJob(row, c.key)}
                    >
                      {row.columns[c.key].length - 2} more \u2192
                    </button>
                  )}
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </section>
  )
}

/** "$4.12" / "$0.0042" — null when there is nothing real to show. */
function fmtCost(v) {
  const n = Number(v)
  if (!Number.isFinite(n) || n <= 0) return null
  return n < 0.01 ? `$${n.toFixed(4)}` : `$${n.toFixed(2)}`
}

function BoardHome({
  jobs, items, itemsErr, onRetryItems, costToday, layout, onLayout,
  scope, onScope, filter, onClearFilter, onOpenJob, onOpenRun, onNewJob,
  suggestions, busyId, suggestionNote,
  onApproveSuggestion, onDeclineSuggestion, onEditSuggestion,
  nextCursor, onLoadMore,
}) {
  const now = Date.now()
  const all = applyScope(items || [], scope, { now })
  const shown = filter ? all.filter((r) => matchesWorkFilter(r, filter)) : all
  const rows = groupByJob(jobs || [], shown, { now })
  const totals = boardTotals(rows)
  const cost = fmtCost(costToday)

  if (itemsErr && !items) {
    return (
      <div className="view work-home">
        <WorkLoadFailed lead="Can\'t load work" onRetry={onRetryItems} />
      </div>
    )
  }

  return (
    <div className="view work-home work-board">
      <header className="work-home-head">
        <h1>Work</h1>
        <button type="button" className="btn btn-primary jb-new" onClick={onNewJob}>
          New job
        </button>
      </header>

      {/* Every number here is derived from the rows below, never queried
          separately — two counts on one screen disagreeing is the failure
          this product sells against. The cost is the exception and says so:
          it is account-wide and includes spend no job can claim. */}
      <p className="jb-totals">
        <span>{totals.jobs} {totals.jobs === 1 ? 'job' : 'jobs'}</span>
        <button type="button" onClick={() => onScope(scope === 'today' ? null : 'today')}>
          {totals.done} done today
        </button>
        <button type="button" onClick={() => onClearFilter('waiting')}>
          {totals.waiting} waiting
        </button>
        <button type="button" onClick={() => onClearFilter('stuck')}>
          {totals.stuck} stuck
        </button>
        {cost && <span className="jb-cost">{cost} in 24h</span>}
      </p>

      <div className="jb-filters" role="group" aria-label="Board view">
        <button
          type="button"
          className={`jb-pill${layout === 'grouped' ? ' is-on' : ''}`}
          onClick={() => onLayout('grouped')}
        >
          By job
        </button>
        <button
          type="button"
          className={`jb-pill${layout === 'flat' ? ' is-on' : ''}`}
          onClick={() => onLayout('flat')}
        >
          Flat
        </button>
        <button
          type="button"
          className={`jb-pill${scope === 'mine' ? ' is-on' : ''}`}
          onClick={() => onScope(scope === 'mine' ? null : 'mine')}
        >
          Mine
        </button>
        <button
          type="button"
          className={`jb-pill${scope === 'today' ? ' is-on' : ''}`}
          onClick={() => onScope(scope === 'today' ? null : 'today')}
        >
          Today
        </button>
        {filter && (
          <button type="button" className="work-filter-chip" onClick={() => onClearFilter(null)}>
            {WORK_FILTER_LABELS[filter] || filter}
            <span aria-hidden="true">\u00d7</span>
          </button>
        )}
      </div>

      <SuggestionsStrip
        suggestions={suggestions}
        busyId={busyId}
        note={suggestionNote}
        onApprove={onApproveSuggestion}
        onDecline={onDeclineSuggestion}
        onEdit={onEditSuggestion}
      />

      {layout === 'flat' ? (
        <WorkTable rows={sortWorkItems(shown)} onOpen={onOpenRun}
                   nextCursor={nextCursor} onLoadMore={onLoadMore} />
      ) : rows.length === 0 ? (
        <p className="jb-quiet">No work on the record yet.</p>
      ) : (
        <>
          {/* The column headers appear ONCE, not per job. */}
          <div className="jb-colheads" aria-hidden="true">
            {COLUMNS.map((c) => <span key={c.key}>{c.label}</span>)}
          </div>
          {rows.map((r) => (
            <JobRow key={r.key} row={r} now={now}
                    onOpenJob={onOpenJob} onOpenRun={onOpenRun} />
          ))}
        </>
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
  onOpenAgent,
  active = true,
  // Which Work page the URL is pointing at, and how to change it. The URL is
  // the single source of truth for job/run — not a mirror of local state, so
  // Back and a pasted link behave identically.
  route = { job: null, run: null },
  onRoute = () => {},
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

  // This kind's CLOSED work. The main page carries done rows only
  // incidentally (it is a mixed page of 50 ordered by recency), so the kind
  // page asks for them directly. One extra lean request, column-filtered —
  // never the board.
  const [finished, setFinished] = useState(null)
  const [finishedLoading, setFinishedLoading] = useState(false)

  // The declared jobs. Needed even when a job has no runs today — a job that
  // STOPPED running is exactly what a board of live runs cannot show, and it
  // is the main reason this page groups.
  const [jobs, setJobs] = useState(null)
  const [costToday, setCostToday] = useState(null)
  // Bumped when a write should re-read the declarations (a new job, a
  // resolved handoff). Not a poll.
  const [refreshKeyForJobs, setRefreshKeyForJobs] = useState(0)
  const [layout, setLayout] = useState('grouped')  // 'grouped' | 'flat'
  const [scope, setScope] = useState(null)         // null | 'mine' | 'today'
  const [items, setItems] = useState(null)
  const [suggestions, setSuggestions] = useState([])
  const [nextCursor, setNextCursor] = useState(null)
  const [itemsErr, setItemsErr] = useState(null)
  const [busyId, setBusyId] = useState(null)
  const [suggestionNote, setSuggestionNote] = useState(null)

  // Load this job's finished work when its page opens, and only then.
  const kindWorkflowId = route.job ?? null
  const kindOpen = route.job ? String(route.job) : null
  useEffect(() => {
    if (!kindOpen) {
      setFinished(null)
      return undefined
    }
    setFinishedLoading(true)
    return startAbortable(({ signal, isAlive }) => {
      api
        .getWorkItems({
          limit: 50,
          workflowId: kindWorkflowId === null ? 'none' : kindWorkflowId,
          status: 'done',
          signal,
        })
        .then((p) => {
          if (!isAlive()) return
          setFinished(Array.isArray(p?.items) ? p.items : [])
          setFinishedLoading(false)
        })
        // Past runs is a band, not the page. Its absence is quiet.
        .catch(() => isAlive() && setFinishedLoading(false))
    })
  }, [kindOpen, kindWorkflowId])

  const itemsFailSoftRef = useRef(false)
  const failSoftRef = useRef(false)
  // Read by the poll timer, which is scheduled once — a ref so a tab switch
  // doesn't tear down and restart the interval.
  const activeRef = useRef(active)
  activeRef.current = active
  itemsFailSoftRef.current = !items && !!itemsErr
  failSoftRef.current = itemsFailSoftRef.current

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
    if (!itemsFailSoftRef.current) tasks.push(loadItems())
    loadSuggestions()
    const results = await Promise.allSettled(tasks)
    if (results.some((r) => r.status === 'rejected')) {
      throw new Error('section failed')
    }
  }, [loadItems, loadSuggestions])

  function retryItems() {
    setItemsErr(null)
    loadItems().catch(() => {})
  }

  async function refreshNamedWork() {
    await Promise.allSettled([loadItems()])
    setRefreshKeyForJobs((n) => n + 1)
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

  // Jobs and today's spend, alongside the runs. Two more lean reads, both
  // already in use elsewhere; no new polling — these ride the same effect and
  // simply do not refetch on the interval, because a declaration and a daily
  // total do not change between ticks the way live runs do.
  useEffect(
    () =>
      startAbortable(({ signal, isAlive }) => {
        api.getWorkflows({ signal })
          .then((r) => isAlive() && setJobs(Array.isArray(r) ? r : []))
          .catch(() => isAlive() && setJobs([]))
        api.getCost({ signal })
          .then((r) => isAlive() && setCostToday(Number(r?.today) || 0))
          .catch(() => isAlive() && setCostToday(null))
      }),
    [refreshKeyForJobs],
  )

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

  // WHICH PAGE is the URL's job now, not local state. Back, Forward and a
  // pasted link all go through the same path, so they cannot diverge.
  const openJob = route.job ? (jobs || []).find((j) => j.id === route.job) || null : null

  if (route.run) {
    return (
      <JobDetail
        variant="page"
        // Only the id is in the URL; JobDetail fetches the rest and renders
        // its header from whatever it has.
        item={{ id: route.run }}
        backLabel={openJob ? `← ${openJob.name}` : '← Work'}
        onOpenAgent={onOpenAgent}
        onClose={() => onRoute({ job: route.job, run: null })}
        onResolved={() => {
          onRoute({ job: route.job, run: null })
          refreshNamedWork()
        }}
      />
    )
  }

  return (
    route.job ? (
      <KindPage
        kindName={openJob?.name || ''}
        workflowId={route.job}
        items={items || []}
        finished={finished}
        finishedLoading={finishedLoading}
        filter={filter}
        onClearFilter={() => setFilter(null)}
        onBack={() => onRoute({ job: null, run: null })}
        onOpenItem={(it) => onRoute({ job: route.job, run: it.id })}
        onOpenAgent={onOpenAgent}
      />
    ) : (
    <BoardHome
      jobs={jobs}
      items={items}
      itemsErr={itemsErr}
      onRetryItems={retryItems}
      costToday={costToday}
      layout={layout}
      onLayout={setLayout}
      scope={scope}
      onScope={setScope}
      filter={filter}
      onClearFilter={setFilter}
      suggestions={suggestions}
      busyId={busyId}
      suggestionNote={suggestionNote}
      onApproveSuggestion={approveSuggestion}
      onDeclineSuggestion={declineSuggestion}
      onEditSuggestion={editSuggestion}
      nextCursor={nextCursor}
      onLoadMore={loadMore}
      onNewJob={onNewWorkflow || connectAgent}
      onOpenRun={(it) => onRoute({ job: route.job, run: it.id })}
      onOpenJob={(row) => onRoute({ job: Number(row.key), run: null })}
    />
    )
  )
}
