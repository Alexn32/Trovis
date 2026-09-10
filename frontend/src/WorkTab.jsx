import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api } from './api.js'
import {
  WHOSE_DEFAULT,
  reconcileWhose,
  whoseEmptyCopy,
  whoseOptions,
  whoseParams,
} from './whoseWork.js'
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
import { tableJobMeta } from './workBoard.js'
import { QuietBrand } from './BrandMarks.jsx'

// IA: Work home stays Monday table (overview + Suggestions + job rows).
// #167 job-grouping/verdict numbers fold into table row data — not a
// 4-column board landing (locked v1.1 + #135).
// Must NOT call /work/summary or /work/board on this path (those starve
// the replica). Board.jsx stays in the repo unused until F4 reopens it.
//
// Dual path on the Monday table (home):
//   Primary row click → the job (/work/jobs/:id) when the row has one.
//   Nested click on the task title → that run (/work/runs/:id).
// Unmatched rows have only the run, so the row opens it. Kind page is
// already the job, so its table rows stay run doors. The job-name
// subline remains a job door so click-in cannot become undiscoverable.
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
 * genuinely are the same table — same columns, same sort — rather than
 * two that merely look alike and drift.
 *
 * Home may pass `jobMeta` / `onOpenJob` so a row can name its job and a
 * numbered verdict under Task. Those are sublines, not extra columns, and
 * they never rearrange the page into Working | Waiting | Stuck | Done.
 *
 * Click-in: `onOpen` is the primary row door (job on home, run on the
 * kind page). `onOpenRun` is the nested task-title door to the run, so
 * run detail stays secondary and the job path cannot disappear.
 */
function WorkTable({ rows, onOpen, onOpenJob, onOpenRun, jobMeta, nextCursor, onLoadMore }) {
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
      {rows.map((row) => {
        const meta = jobMeta
          ? (jobMeta.get(row.id)
            || (row.workflow_name ? { name: row.workflow_name, badge: null } : null))
          : null
        const runNested = onOpenRun && row.workflow_id != null
        return (
          <div
            key={row.id}
            className={rowClass(row.status)}
            role="row"
            tabIndex={0}
            onClick={() => onOpen(row)}
            onKeyDown={(e) => onRowKey(e, row)}
            aria-label={row.title}
          >
            <span className="work-td-title">
              {runNested ? (
                <button
                  type="button"
                  className="work-td-task"
                  onClick={(e) => {
                    e.stopPropagation()
                    onOpenRun(row)
                  }}
                >
                  {row.title}
                </button>
              ) : (
                <span className="work-td-task">{row.title}</span>
              )}
              {meta?.name && (
                <span className="work-td-job">
                  {onOpenJob && row.workflow_id != null ? (
                    <button
                      type="button"
                      className="work-td-job-name"
                      onClick={(e) => {
                        e.stopPropagation()
                        onOpenJob(row.workflow_id)
                      }}
                    >
                      {meta.name}
                    </button>
                  ) : (
                    <span className="work-td-job-name is-plain">{meta.name}</span>
                  )}
                  {meta.badge && (
                    <span className={`jb-badge tone-${meta.badge.tone}`}>{meta.badge.label}</span>
                  )}
                </span>
              )}
            </span>
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
        )
      })}
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

function WorkHome({
  onConnectAgent,
  overview,
  overviewErr,
  onRetryOverview,
  items,
  itemsErr,
  onRetryItems,
  jobs,
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
  onOpenItem,
  onOpenJob,
  whose,
  onWhoseChange,
  whoseChoices = [],
}) {
  const all = sortWorkItems(items || [])
  const rows = all.filter((r) => (filter ? matchesWorkFilter(r, filter) : true))
  const empty = !!items && rows.length === 0 && (!overview || (overview.open || 0) === 0)
  // Filtered down to nothing is a different situation from having no work:
  // the answer is to clear a chip, not to connect an agent.
  const filteredOut = !!items && rows.length === 0 && all.length > 0
  const jobMeta = useMemo(
    () => tableJobMeta(jobs || [], items || [], rows),
    [jobs, items, rows],
  )

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
        {/* Whose work. Absent entirely for a seat with no reports — "Me" and
            "Everyone I can see" would be the same list, and a control whose
            options all do the same thing is worse than no control. */}
        {whoseChoices.length > 0 && (
          <label className="work-whose">
            <span className="work-whose-label">Whose work</span>
            <select
              className="work-whose-select"
              value={whose}
              onChange={(e) => onWhoseChange(e.target.value)}
            >
              {whoseChoices.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </label>
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

      {filteredOut && !empty && (
        <div className="board-empty">
          <p className="board-empty-lead">Nothing matches these filters.</p>
          <p className="board-empty-sub">Clear a filter above to see the rest of the work.</p>
        </div>
      )}

      {/* Empty because of the Whose-work choice, not because the org has
          no work. Offering "Connect an agent" here would be answering a
          question nobody asked — the agents are connected, they just belong
          to someone else. */}
      {empty && whoseEmptyCopy(whose) && (
        <div className="board-empty">
          <p className="board-empty-lead">{whoseEmptyCopy(whose)}</p>
          <p className="board-empty-sub">
            Widen Whose work above to see the rest.
          </p>
        </div>
      )}

      {empty && !whoseEmptyCopy(whose) && (
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

      {items && !empty && !filteredOut && (
        <WorkTable
          rows={rows}
          onOpen={(row) => {
            if (row.workflow_id != null) onOpenJob(row.workflow_id)
            else onOpenItem(row)
          }}
          onOpenRun={onOpenItem}
          onOpenJob={onOpenJob}
          jobMeta={jobMeta}
          nextCursor={nextCursor}
          onLoadMore={onLoadMore}
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
  // The resolved seat from /auth/me. Decides the Whose-work options and the
  // default; the SERVER decides what each one may contain.
  seat = null,
  // People this seat may name — the chart members already loaded for Org.
  // Empty is fine: the control still offers Me / My team.
  people = [],
}) {
  const connectAgent = onConnectAgent || onNewWorkflow
  const [filter, setFilter] = useState(null)
  // Whose work is on screen. Default is the seat's own breadth, so arriving
  // at Work needs no choice made.
  const [whose, setWhose] = useState(WHOSE_DEFAULT)
  // A reorg can move the ground under a selection — a manager who lost their
  // reports is holding a "My team" the server would now answer differently.
  const reconciled = reconcileWhose(whose, seat)
  useEffect(() => {
    if (reconciled !== whose) setWhose(reconciled)
  }, [reconciled, whose])
  // The loaders are stable useCallbacks (they feed the poll and the retry
  // paths). A ref keeps them reading the live selection without rebuilding
  // them — and without a stale closure sending last minute's filter.
  const whoseRef = useRef(whose)
  whoseRef.current = whose

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

  // Declared jobs, for table-row enrichment (name + numbered verdict).
  // Not a second count source — overview pills stay authoritative.
  const [jobs, setJobs] = useState(null)
  const [refreshKeyForJobs, setRefreshKeyForJobs] = useState(0)

  const [overview, setOverview] = useState(null)
  const [items, setItems] = useState(null)
  const [suggestions, setSuggestions] = useState([])
  const [nextCursor, setNextCursor] = useState(null)
  const [overviewErr, setOverviewErr] = useState(null)
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
      const ov = await api.getWorkOverview(whoseParams(whoseRef.current))
      setOverview(ov)
      setOverviewErr(null)
    } catch (e) {
      setOverviewErr(e?.message || "Can't load these counts")
      throw e
    }
  }, [])

  const loadItems = useCallback(async () => {
    try {
      const page = await api.getWorkItems({
        limit: 50,
        ...whoseParams(whoseRef.current),
      })
      setItems(Array.isArray(page?.items) ? page.items : [])
      setNextCursor(page?.next_cursor || null)
      setItemsErr(null)
    } catch (e) {
      setItemsErr(e?.message || "Can't load this work")
      throw e
    }
  }, [])

  // Changing Whose work is a new question for the server, not a client-side
  // narrowing of rows already fetched: the seat filter runs before the
  // cursor, so page one for "My team" is a different page one.
  const firstWhose = useRef(true)
  useEffect(() => {
    if (firstWhose.current) {
      firstWhose.current = false
      return
    }
    loadOverview().catch(() => {})
    loadItems().catch(() => {})
  }, [whose, loadOverview, loadItems])

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

  // Declarations only — not polled. A job name and its expectation do not
  // change between ticks the way live runs do.
  useEffect(
    () =>
      startAbortable(({ signal, isAlive }) => {
        api.getWorkflows({ signal })
          .then((r) => isAlive() && setJobs(Array.isArray(r) ? r : []))
          .catch(() => isAlive() && setJobs([]))
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
      const page = await api.getWorkItems({
        cursor: nextCursor,
        limit: 50,
        // The same slice, or page two would be a different question.
        ...whoseParams(whoseRef.current),
      })
      setItems((prev) => [...(prev || []), ...(page?.items || [])])
      setNextCursor(page?.next_cursor || null)
    } catch {
      /* keep last-good rows */
    }
  }

  // WHICH PAGE is the URL's job now, not local state. Back, Forward and a
  // pasted link all go through the same path, so they cannot diverge.
  const openJob = route.job ? (jobs || []).find((j) => j.id === route.job) || null : null
  const kindName = openJob?.name
    || (items || []).find((r) => r.workflow_id === route.job)?.workflow_name
    || ''

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
        kindName={kindName}
        workflowId={route.job}
        items={items || []}
        finished={finished}
        finishedLoading={finishedLoading}
        filter={filter}
        onClearFilter={() => setFilter(null)}
        onBack={() => onRoute({ job: null, run: null })}
        onOpenItem={(it) => onRoute({ job: it.workflow_id ?? route.job, run: it.id })}
        onOpenAgent={onOpenAgent}
      />
    ) : (
    <WorkHome
      onConnectAgent={connectAgent}
      overview={overview}
      overviewErr={overviewErr}
      onRetryOverview={retryOverview}
      items={items}
      itemsErr={itemsErr}
      onRetryItems={retryItems}
      jobs={jobs}
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
      onOpenItem={(it) => onRoute({ job: it.workflow_id ?? route.job, run: it.id })}
      onOpenJob={(id) => onRoute({ job: Number(id), run: null })}
      whose={whose}
      onWhoseChange={setWhose}
      whoseChoices={whoseOptions(seat, people)}
    />
    )
  )
}
