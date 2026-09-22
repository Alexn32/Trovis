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
import { ChevronRightIcon } from './Icons.jsx'
import {
  holderLabel,
  sortWorkItems,
  workItemStatusLabel,
  workUpdatedLabel,
} from './board.js'
// home.js is the vocabulary Home's tiles arrive with; the Work filters read
// the same partition so a tile there and a tile here land on the same rows.
import { viewerClock } from './home.js'
import { WORK_FILTER_LABELS, matchesWorkFilter } from './workFilter.js'
import {
  groupByJob, healthBadge, idleJobLine, jobSubline, numOrNull, tableJobMeta, touchedToday,
} from './workBoard.js'
import {
  DENSITY_KEY, WORK_VIEWS, calmLine, compactJobLine, countsLine, exceptionLine, exceptionRows,
  resolveDensity, resolveView, scopeLine, situationTiles, stateSegments, tileTarget,
} from './workPage.js'
import {
  computedFrom, declaredSteps, healthRows, jobPath, jobStats, recentRuns, settingsRows,
} from './jobPage.js'
import { CompletionChart, JobBreakdown } from './HomeSections.jsx'
import { readComparison, readJobs, readSeries, relTime } from './homeView.js'
import { QuietBrand } from './BrandMarks.jsx'

// IA: Work home is three views of the same open work — By job (the
// landing: a job row per declared job, runs that need a person listed under
// it), All open (one urgency-sorted table) and Completed (the recorded
// completions, charted from /home/snapshot, then the closed runs). Above all
// three sits the situation strip: four server counts, each a door.
// Must NOT call /work/summary or /work/board on this path (those starve
// the replica). Board.jsx stays in the repo unused until F4 reopens it.
//
// Doors:
//   A job name → the job (/work/jobs/:id). A run row or line → the run
//   (/work/runs/:id). In the All open table the row is the job door when it
//   has one and the nested task title is the run door; unmatched rows have
//   only the run, so the row opens it. Kind page rows stay run doors.
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
function rowClass(status) {
  if (status === 'waiting_on_you') return 'work-row is-waiting-you'
  if (status === 'stuck') return 'work-row is-stuck'
  return 'work-row'
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
        <button type="button" className="btn btn-secondary btn-sm work-more" onClick={onLoadMore}>
          Load more
        </button>
      )}
    </div>
  )
}

// --- the job page: a PATTERN, not an instance ----------------------------
//
// Every number here is an aggregate. The provenance line under the diagram
// and the empty comparison column in health are what keep this page from
// reading like the run page one click away, which states facts.

/** The typical route, and the one branch worth naming. */
function JobPathBand({ path, provenance }) {
  if (!path) return null
  return (
    <section className="kind-band" aria-label="How this job usually runs">
      <h2 className="dash-caps">How this job usually runs</h2>
      <ol className="kind-path">
        {path.map((n, i) => (
          // The connector is a sibling of the node, not a pseudo-element on
          // it: rendered inside, the arrow lands within the pill's border and
          // reads as part of the label rather than as the step between two.
          <li key={`${n.kind}-${i}`} className="kind-step">
            {i > 0 && <span className="kind-arrow" aria-hidden="true">→</span>}
            <span
              className={`kind-node kind-${n.kind}${n.isDivergence ? ' is-divergence' : ''}`}
            >
              <span className="kind-node-label">{n.label}</span>
              {/* Gated on the MEASUREMENT, not on the branch: a measured 0% is
                  still a fact the reader is owed, it just is not highlighted.
                  Gating on isDivergence hid it. */}
              {n.pct !== null && <span className="kind-node-pct">{n.pct}% of runs</span>}
            </span>
          </li>
        ))}
      </ol>
      {/* Never optional. An average with no stated basis is an assertion,
          and this page sits one click from a page of recorded facts. */}
      {provenance && <p className="jobp-computed">{provenance}</p>}
    </section>
  )
}

/** Four metrics, each naming the number it is read against — or nothing. */
function HealthBand({ rows, declared }) {
  return (
    <section className="kind-band" aria-label="Health">
      <h2 className="dash-caps">Health</h2>
      {!declared && (
        <p className="jobp-nodecl">
          No expectation set. These are the observed numbers; nothing is being
          graded against them.
        </p>
      )}
      <dl className="jobp-health">
        {rows.map((r) => (
          <div
            key={r.key}
            className={`jobp-metric${r.over ? ' is-over' : ''}${r.noData ? ' is-nodata' : ''}`}
          >
            <dt>{r.label}</dt>
            {/* Rule 6: the words, not a dash. A dash beside three rows of
                numbers reads as a small value rather than as no value. */}
            <dd className="jobp-observed">{r.observed}</dd>
            {/* Empty, not a dash pretending to be a verdict. */}
            <dd className="jobp-expected">
              {r.noData && r.expected ? `${r.expected} \u00b7 not checked` : r.expected || ''}
            </dd>
          </div>
        ))}
      </dl>
    </section>
  )
}

/** Recent runs — every state, so a repeated failure reads as a cluster.
 *  `bare` renders the list alone; the job page owns the section around it. */
function RecentRunsBand({ rows, onOpenItem, bare = false }) {
  if (rows.length === 0) {
    const quiet = <p className="kind-quiet">No runs on the record yet.</p>
    return bare ? quiet : (
      <section className="kind-band" aria-label="Recent runs">
        <h2 className="dash-caps">Recent runs</h2>
        {quiet}
      </section>
    )
  }
  const list = (
      <ul className="jobp-runs">
        {rows.map((r) => (
          <li key={r.id}>
            <button type="button" className={`jobp-run is-${r.status}`} onClick={() => onOpenItem(r)}>
              <span className="jobp-run-what">{r.title}</span>
              <span className={`work-status-pill ${r.status || ''}`}>
                {workItemStatusLabel(r.status)}
              </span>
              <span className="jobp-run-age">{workUpdatedLabel(r.updated_at)}</span>
            </button>
          </li>
        ))}
      </ul>
  )
  return bare ? list : (
    <section className="kind-band" aria-label="Recent runs">
      <h2 className="dash-caps">Recent runs</h2>
      {list}
    </section>
  )
}

/** The technical block, below the fold, and only what is declared. */
function SettingsBand({ rows, onOpenAgent }) {
  if (rows.length === 0) return null
  return (
    <section className="kind-band jobp-settings" aria-label="Settings">
      <h2 className="dash-caps">Settings</h2>
      <dl>
        {rows.map((r) => (
          <div key={`${r.label}-${r.value}`}>
            <dt>{r.label}</dt>
            <dd>
              {r.route && onOpenAgent ? (
                <button
                  type="button"
                  className="jobd-run-agent"
                  onClick={() => onOpenAgent(r.route[0], r.route[1])}
                >
                  {r.value}
                </button>
              ) : (
                r.value
              )}
            </dd>
          </div>
        ))}
      </dl>
    </section>
  )
}

/**
 * The job page: /work/jobs/:id.
 *
 * One subject, and it is a PATTERN — how this job usually runs, what it
 * declared about itself, how the record compares. Individual runs appear
 * only as a recent sample that links out; the instance-level story lives on
 * the run page.
 */
function JobPage({
  job, jobErr, name, runs: jobRuns, runsLoading, runsCursor, onLoadMoreRuns,
  filter, onFilter, onClearFilter, onBack, onOpenItem, onOpenAgent, onEditJob,
}) {
  const now = Date.now()
  // This job's own runs, id-filtered by the server. The aggregates come from
  // the job itself, precomputed — the client does no arithmetic on them, so
  // it cannot disagree with the board.
  const mine = jobRuns || []
  const shown = filter ? mine.filter((r) => matchesWorkFilter(r, filter)) : mine
  const path = jobPath(job, mine)
  const stats = jobStats(job, { now })
  const health = healthRows(job)
  const settings = settingsRows(job)
  const provenance = computedFrom(job, mine)
  const runs = recentRuns(shown)
  const filteredOut = runs.length === 0 && mine.length > 0
  const steps = declaredSteps(job)
  const versions = Array.isArray(job?.versions) ? job.versions : []

  return (
    <div className="view work-kind-page">
      <header className="work-home-head">
        <button type="button" className="wf2-back" onClick={onBack}>
          ← All work
        </button>
        <h1>{job?.name || name}</h1>
        {job?.derived && <span className="wk-derived-tag">not yet described</span>}
        {/* The one edit door. On a derived job it reads as what it is —
            the promotion; on a declared job, a new version of its
            definition. Both land in the same editor. */}
        {onEditJob && job && (
          <button
            type="button"
            className="btn btn-secondary btn-sm jobp-edit"
            onClick={() => onEditJob(job.id)}
          >
            {job.derived ? 'Describe this job' : 'Edit job'}
          </button>
        )}
      </header>

      {jobErr && !job && (
        <p className="kind-quiet" role="alert">
          Couldn&apos;t load this job&apos;s definition. The runs below are still real.
        </p>
      )}
      {/* The operator's own words. Absent until somebody writes them — this
          page will not summarise a job it has only counted. */}
      {job?.definition && <p className="jobp-definition">{job.definition}</p>}
      {/* A derived job has no operator's words yet — this is where it says
          so, and offers the one step that changes it. */}
      {job?.derived && (
        <div className="jobp-derived">
          <span className="wk-derived-tag">not yet described</span>
          <p>
            Trovis created this job from {job.derived_from}&apos;s activity because nobody had described one yet.
            It is observed, not graded: no expectation, no verdict.
          </p>
          {onEditJob && (
            <button type="button" className="btn btn-secondary btn-sm" onClick={() => onEditJob(job.id)}>
              Describe this job
            </button>
          )}
        </div>
      )}

      <div className="jobp-stats">
        {stats.map((st) => (
          <div key={st.key} className={`jobp-stat${st.value === null ? ' is-nodata' : ''}`}>
            <span className="jobp-stat-label">{st.label}</span>
            {/* Rule 6, said the same way the health section says it. An em
                dash here beside "No data" two inches below was the same fact
                rendered two ways on one screen \u2014 browser-caught. */}
            <span className="jobp-stat-value">{st.value ?? 'No data'}</span>
            {/* The denominator, when it is not the whole job. */}
            {st.sub && st.value != null && <span className="jobp-stat-sub">{st.sub}</span>}
          </div>
        ))}
      </div>

      <JobPathBand path={path} provenance={provenance} />
      <DeclaredStepsBand steps={steps} />
      <HealthBand rows={health} declared={Boolean(job?.has_expectation)} />

      <section className="kind-band" aria-label="Recent runs">
        <div className="jobp-runs-head">
          <h2 className="dash-caps">Recent runs</h2>
          {/* The same status vocabulary as the board, applied to the runs
              this page has loaded. The server pages by recency; the chips
              narrow what is on screen, and Load more fetches the next page
              of the job's history whichever chip is on. */}
          {onFilter && (
            <div className="jb-filters jobp-runs-filters">
              {[...STATUS_CHIPS, 'done'].map((f) => (
                <button
                  key={f}
                  type="button"
                  className={`jb-pill${filter === f ? ' is-on' : ''}`}
                  aria-pressed={filter === f}
                  onClick={() => onFilter(filter === f ? null : f)}
                >
                  {WORK_FILTER_LABELS[f]}
                </button>
              ))}
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
            </div>
          )}
        </div>
        {runsLoading && mine.length === 0 ? (
          <div className="dash-skel"><span style={{ width: '60%' }} /></div>
        ) : filteredOut ? (
          <div className="board-empty">
            <p className="board-empty-lead">Nothing matches these filters.</p>
            <p className="board-empty-sub">
              Clear the filter, or load more of this job&apos;s history below.
            </p>
          </div>
        ) : (
          <RecentRunsBand rows={runs} onOpenItem={onOpenItem} bare />
        )}
        {/* How much of the history is on screen, and the way to more of it. */}
        <p className="jobp-runs-scope">
          {mine.length} {mine.length === 1 ? 'run' : 'runs'} loaded{runsCursor ? ', more on record' : ''}
          {filter ? ` · ${runs.length} shown` : ''}
        </p>
        {runsCursor && onLoadMoreRuns && (
          <button type="button" className="btn btn-secondary btn-sm work-more" onClick={onLoadMoreRuns}>
            Load more
          </button>
        )}
      </section>

      <SettingsBand rows={settings} onOpenAgent={onOpenAgent} />
      <HistoryBand versions={versions} />
    </div>
  )
}

/**
 * What the operator DECLARED the job's steps to be — who holds the work at
 * each step. Distinct from "How this job usually runs" above it, which is
 * what the record observed; the two sit apart so a declaration is never
 * mistaken for a measurement. Absent until somebody declares steps.
 */
function DeclaredStepsBand({ steps }) {
  if (!steps.length) return null
  return (
    <section className="kind-band" aria-label="Declared steps">
      <h2 className="dash-caps">Declared steps</h2>
      <ol className="kind-path">
        {steps.map((s, i) => (
          <li key={`${s.kind}-${s.label}-${i}`} className="kind-step">
            {i > 0 && <span className="kind-arrow" aria-hidden="true">→</span>}
            <span className={`kind-node kind-${s.kind}`}>
              <span className="kind-node-label">{s.label}</span>
              {s.holder && <span className="kind-node-pct">{s.holder}</span>}
            </span>
          </li>
        ))}
      </ol>
    </section>
  )
}

/** Every version of the definition, newest first. Append-only, so a
 *  history is exactly the record and nothing is ever missing from it. */
function HistoryBand({ versions }) {
  if (!versions.length) return null
  return (
    <section className="kind-band jobp-history" aria-label="History">
      <h2 className="dash-caps">History</h2>
      <ol className="jobp-history-list">
        {versions.map((v) => (
          <li key={v.version} className="jobp-history-row">
            <span className="wfe-vchip">v{v.version}</span>
            <span className="jobp-history-date">
              {v.created_at ? new Date(String(v.created_at).replace(' ', 'T')).toLocaleDateString() : ''}
            </span>
            {v.note && <span className="jobp-history-note">{v.note}</span>}
          </li>
        ))}
      </ol>
    </section>
  )
}

// --- the Work board: the company's situation, grouped by job ---------------
//
// The home page for all active and recurring work. Three questions, in
// order: what needs a person, where the work is right now, and what got
// done. It is a live reflection of what Trovis OBSERVED, never a task board
// somebody drags cards around on. Nothing here can be moved to Done by hand;
// Done means a close was recorded.

/**
 * The situation strip. Four server counts, each saying what it counts, and
 * each one a door to the rows behind it. Counts are the server contract.
 * Do not recompute or clamp them here.
 */
/**
 * How current the picture is. A quiet job and a stopped feed look the same
 * on a board of runs; this is the one line that tells them apart. Reads the
 * overview's account-wide newest-span time — the same MAX Home's freshness
 * panel shows — and says "No data yet" rather than implying live when the
 * account has no telemetry at all.
 */
function FreshnessLine({ overview }) {
  if (!overview) return null
  const at = overview.latest_telemetry_at
  return (
    <p className="wk-fresh">
      <span className="wk-fresh-label">Newest data</span>
      <span className="wk-fresh-value">{at ? relTime(at) : 'No data yet'}</span>
    </p>
  )
}

function SituationStrip({ overview, overviewErr, onRetry, onTile, filter, view }) {
  const tiles = situationTiles(overview)
  if (!tiles) {
    if (overviewErr) {
      return (
        <div className="wk-tiles is-failed" role="alert">
          <span className="wk-tiles-failed">Can&apos;t load these counts.</span>
          <button type="button" className="btn btn-secondary btn-sm" onClick={onRetry}>
            Retry
          </button>
        </div>
      )
    }
    return (
      <div className="wk-tiles" aria-busy="true" aria-label="Loading counts">
        {[0, 1, 2, 3].map((i) => <span key={i} className="wk-tile-skel" />)}
      </div>
    )
  }
  return (
    <div className="wk-tiles" aria-label="Work right now">
      {tiles.map((t) => {
        const target = tileTarget(t)
        const on = target.filter ? filter === target.filter : (view === target.view && !filter)
        return (
          <button
            key={t.key}
            type="button"
            className={`wk-tile tone-${t.tone}${on ? ' is-on' : ''}`}
            aria-pressed={on}
            onClick={() => onTile(target)}
          >
            <span className="wk-tile-label">{t.label}</span>
            {/* Rule 6: a count we could not read is the words, not a zero. */}
            <span className={`wk-tile-value${t.value === null ? ' is-unknown' : ''}`}>
              {t.value === null ? 'No data' : t.value}
            </span>
            <span className={`wk-tile-sub${t.delta ? ` is-${t.delta.direction}` : ''}`}>{t.sub}</span>
          </button>
        )
      })}
    </div>
  )
}

/** One run that needs a person, as a line. Opens the run record. */
function ExceptionRow({ row, now, onOpen }) {
  const { lead, sub, tone } = exceptionLine(row, { now })
  return (
    <li>
      <button type="button" className={`wk-exc is-${tone}`} onClick={() => onOpen(row)}>
        <span className="wk-exc-dot" aria-hidden="true" />
        <span className="wk-exc-body">
          <span className="wk-exc-lead">{lead}</span>
          <span className="wk-exc-sub">{sub}</span>
        </span>
        <span className="wk-exc-go" aria-hidden="true"><ChevronRightIcon size={15} /></span>
      </button>
    </li>
  )
}

/** The proportional bar: a job's runs by state. The counts are printed
 *  beside it because a bar cannot say how many it is a bar of. */
function StateBar({ grouped }) {
  const { total, segments } = stateSegments(grouped)
  if (total === 0) return null
  return (
    <span className="wk-bar" role="img" aria-label={countsLine(grouped)}>
      {segments.map((s) => (
        <span key={s.key} className={`wk-bar-seg tone-${s.tone}`} style={{ width: `${s.pct}%` }} />
      ))}
    </span>
  )
}

/**
 * One job: the row IS the job. Name and verdict on the left, the shape of
 * its work on the right, and underneath only the runs that need a person.
 * Routine runs are a count; the fold opens them as a table without a fetch.
 */
function JobRow({ grouped, now, onOpenJob, onOpenItem, onEditJob, expanded, onToggle }) {
  const idle = idleJobLine(grouped, { now })
  const sub = jobSubline(grouped)
  const badge = grouped.isUnmatched ? null : healthBadge(grouped, { now })
  const loud = badge && (badge.tone === 'error' || badge.tone === 'warning')
  const openable = !grouped.isUnmatched && grouped.job?.id != null
  // Trovis filed this agent's runs here because nobody declared a job for
  // them. Said plainly, and the row offers the one step that changes it.
  const derived = Boolean(grouped.job?.derived)
  const exc = exceptionRows(grouped, { now })
  const openRows = [...grouped.columns.stuck, ...grouped.columns.waiting, ...grouped.columns.working]
  const name = openable ? (
    <button type="button" className="jb-name" onClick={() => onOpenJob(grouped.job.id)}>
      {grouped.name}
    </button>
  ) : (
    <span className="jb-name is-unmatched">{grouped.name}</span>
  )

  return (
    <article className={`wk-job${idle ? ' is-idle' : ''}${exc.total ? ' has-exceptions' : ''}`}>
      <div className="wk-job-head">
        <div className="wk-job-id">
          <div className="wk-job-title">
            {name}
            {derived && <span className="wk-derived-tag">not yet described</span>}
            {/* One badge, and it always carries the number behind it. */}
            {loud && <span className={`jb-badge tone-${badge.tone}`}>{badge.label}</span>}
          </div>
          {sub.length > 0 && <p className="jb-meta">{sub.join(' · ')}</p>}
        </div>
        <div className="wk-job-shape">
          {idle ? (
            // Two facts, never a conclusion: what was observed, what was
            // declared. The record cannot tell a stopped job from a job
            // whose telemetry stopped, so the reader draws the inference.
            <p className="jb-idle-line">
              {idle.observed}
              {idle.expected ? <span className="jb-idle-exp"> · {idle.expected}</span> : null}
            </p>
          ) : (
            <>
              <StateBar grouped={grouped} />
              <p className="wk-counts">{calmLine(grouped)}</p>
            </>
          )}
        </div>
      </div>

      {exc.total > 0 && (
        <ul className="wk-exc-list" aria-label={`Needs a person: ${grouped.name}`}>
          {exc.rows.map((r) => (
            <ExceptionRow key={r.id} row={r} now={now} onOpen={onOpenItem} />
          ))}
        </ul>
      )}

      {(openRows.length > 0 || exc.more > 0) && (
        <div className="wk-job-foot">
          <button
            type="button"
            className="wk-fold"
            aria-expanded={expanded}
            onClick={onToggle}
          >
            {expanded ? 'Hide open runs' : `Show all ${openRows.length} open`}
            {exc.more > 0 && !expanded ? ` · ${exc.more} more need a person` : ''}
          </button>
          <span className="wk-job-foot-right">
            {derived && onEditJob && (
              <button type="button" className="wk-fold" onClick={() => onEditJob(grouped.job.id)}>
                Describe this job
              </button>
            )}
            {openable && (
              <button type="button" className="wk-fold is-quiet" onClick={() => onOpenJob(grouped.job.id)}>
                Job page <ChevronRightIcon size={13} />
              </button>
            )}
          </span>
        </div>
      )}

      {expanded && openRows.length > 0 && (
        <div className="wk-job-table">
          <WorkTable rows={sortWorkItems(openRows)} onOpen={onOpenItem} />
        </div>
      )}
    </article>
  )
}

/**
 * The compact list: one line per job, for scanning many. The same jobs in
 * the same order as the rows, with the same single verdict — a different
 * density, not a different page. Empty cells stay empty (rule 6).
 */
function JobList({ grouped, now, onOpenJob }) {
  const lines = grouped.map((g) => compactJobLine(g, { now }))
  return (
    <div className="wk-list" role="table" aria-label="Jobs">
      <div className="wk-list-head" role="row">
        <span role="columnheader">Job</span>
        <span role="columnheader">Verdict</span>
        <span role="columnheader" className="is-num">Open</span>
        <span role="columnheader">Last run</span>
        <span role="columnheader">Cadence</span>
        <span role="columnheader" className="is-num">Cost / run</span>
      </div>
      {lines.map((l) => {
        const Row = l.openable ? 'button' : 'div'
        return (
          <Row
            key={l.key}
            role="row"
            className={`wk-list-row${l.openable ? ' is-link' : ''}${l.loud ? ` is-${l.badge.tone}` : ''}`}
            {...(l.openable ? { type: 'button', onClick: () => onOpenJob(l.key) } : {})}
          >
            <span className="wk-list-name">
              <span className={l.openable ? 'jb-name' : 'jb-name is-unmatched'}>{l.name}</span>
              {l.derived && <span className="wk-derived-tag">not yet described</span>}
            </span>
            <span className="wk-list-verdict">
              {l.badge && (
                <span className={`jb-badge tone-${l.badge.tone}`}>{l.badge.label}</span>
              )}
            </span>
            <span className="is-num">{l.open || ''}</span>
            <span>{l.lastRun || ''}</span>
            <span>{l.cadence || ''}</span>
            <span className="is-num">{l.costPerRun || ''}</span>
          </Row>
        )
      })}
    </div>
  )
}

// The status chips. `mine` and `attention` share Home's vocabulary, so a tile
// here and a card there land on the same rows.
const STATUS_CHIPS = ['mine', 'attention', 'moving', 'waiting', 'stuck']

/** The By job density, remembered per browser. A convenience, never state
 *  the server needs — so a blocked storage just means the default. */
function readDensity() {
  try { return resolveDensity(localStorage.getItem(DENSITY_KEY)) } catch { return 'rows' }
}
function writeDensity(v) {
  try { localStorage.setItem(DENSITY_KEY, v) } catch { /* per-viewer convenience only */ }
}

/**
 * What got done: the period's completions as a chart, by job, and then the
 * recently closed runs themselves. The chart reads /home/snapshot — the
 * LLM-free foundation Home already draws from — so the two pages cannot
 * disagree on what a completion is.
 */
function CompletedView({ snapshot, snapshotErr, onRetrySnapshot, done, doneErr, doneLoading,
  onRetryDone, nextCursor, onLoadMore, onOpenItem, onOpenJob, timeZone }) {
  const series = snapshot ? readSeries(snapshot) : null
  const byJob = snapshot ? readJobs(snapshot) : null
  const comparison = snapshot ? readComparison(snapshot) : null
  const total = numOrNull(snapshot?.period?.completed)
  const abandoned = numOrNull(snapshot?.period?.abandoned)
  const rows = sortWorkItems(done || [])
  // Job names only on closed rows. A verdict badge beside a completion
  // grades the job on the row that went right; the job page carries it.
  const meta = new Map()
  return (
    <div className="wk-done">
      <section className="wk-done-chart" aria-label="Completions, last 7 days">
        {snapshot ? (
          <div className="wk-done-grid">
            <div className="wk-done-headline">
              <span className={`hv-big${total === null ? ' hv-big-unknown' : ''}`}>
                {total === null ? '—' : total}
              </span>
              <span className="hv-big-label">
                recorded completions · 7 days
                {comparison && (
                  <em className={`hv-delta hv-delta-${comparison.direction}`}>{comparison.text}</em>
                )}
              </span>
              {abandoned !== null && abandoned > 0 && (
                <span className="hv-abandoned">{abandoned} closed without finishing</span>
              )}
              <p className="hv-big-note">
                A recorded completion is work the record shows as closed and done.
                It is not an independently verified business outcome.
              </p>
            </div>
            <CompletionChart series={series} timezone={snapshot?.period?.timezone || timeZone} />
          </div>
        ) : snapshotErr ? (
          <div className="work-section-failed">
            <WorkLoadFailed lead="Can't load the completion history" onRetry={onRetrySnapshot} />
          </div>
        ) : (
          <div className="wk-done-grid" aria-busy="true"><span className="wk-tile-skel" /><span className="wk-chart-skel" /></div>
        )}
        {byJob && !byJob.empty && (
          <div className="wk-done-jobs">
            <h2 className="dash-caps">By job</h2>
            <JobBreakdown jobs={byJob} onOpenJob={onOpenJob} />
          </div>
        )}
      </section>

      <section className="wk-done-list" aria-label="Recently completed">
        <h2 className="dash-caps">Recently completed</h2>
        {doneErr && !done && (
          <div className="work-section-failed">
            <WorkLoadFailed lead="Can't load completed work" onRetry={onRetryDone} />
          </div>
        )}
        {!done && !doneErr && doneLoading && <TableSkeleton />}
        {done && rows.length === 0 && (
          <div className="board-empty">
            <p className="board-empty-lead">Nothing closed in the last 7 days.</p>
            <p className="board-empty-sub">When a run is recorded as done it appears here on its own.</p>
          </div>
        )}
        {done && rows.length > 0 && (
          <WorkTable
            rows={rows}
            jobMeta={meta}
            onOpen={onOpenItem}
            onOpenJob={onOpenJob}
            nextCursor={nextCursor}
            onLoadMore={onLoadMore}
          />
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
  onFilter,
  onClearFilter,
  view,
  onView,
  onOpenItem,
  onOpenJob,
  onEditJob,
  onNewJob,
  whose,
  onWhoseChange,
  whoseChoices = [],
  completed,
}) {
  const now = Date.now()
  const [todayOnly, setTodayOnly] = useState(false)
  const [expanded, setExpanded] = useState(() => new Set())
  const [density, setDensity] = useState(readDensity)
  function changeDensity(v) {
    setDensity(v)
    writeDensity(v)
  }
  const all = sortWorkItems(items || [])
  // The board shows OPEN work; Done has its own view with its own fetch, so
  // a done row that happens to sit on the mixed page is not drawn twice.
  const openAll = all.filter((r) => r.status !== 'done')
  const scoped = todayOnly ? all.filter((r) => touchedToday(r, now)) : all
  const rows = scoped.filter((r) => (filter ? matchesWorkFilter(r, filter) : true))
  const openRows = rows.filter((r) => r.status !== 'done')
  const grouped = useMemo(
    () => groupByJob(jobs || [], rows, { now }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [jobs, rows],
  )
  const tableMeta = useMemo(
    () => tableJobMeta(jobs || [], items || [], openRows, { now }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [jobs, items, openRows],
  )
  // Rule 6: an overview we could not read has not told us the account is
  // empty. `(overview.open || 0) === 0` made an unreadable count say so.
  const openCount = numOrNull(overview?.open)
  const empty = !!items && all.length === 0 && openCount === 0
  const filteredOut = !!items && rows.length === 0 && all.length > 0
  const whoseLabel = whoseChoices.find((o) => o.value === whose)?.label
  const scope = scopeLine({ whoseLabel, shown: openAll.length, truncated: Boolean(nextCursor) })

  function toggleJob(key) {
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  function onTile(target) {
    onView(target.view)
    onFilter(target.filter)
  }

  // Done has its own view; carrying a `done` filter onto the board would
  // leave it showing only closed rows with the chip that explains it hidden.
  function changeView(next) {
    if (filter === 'done' && next !== 'done') onFilter(null)
    onView(next)
  }

  return (
    <div className="view work-home work-board">
      <header className="wk-head">
        <div>
          <h1>Work</h1>
          <p className="wk-lede">What is happening, who has it, and what got done.</p>
        </div>
        <div className="wk-head-controls">
          {whoseChoices.length > 0 && (
            <label className="work-whose">
              <span className="work-whose-label">Whose work</span>
              <select
                className="work-whose-select"
                value={whose}
                onChange={(e) => onWhoseChange(e.target.value)}
              >
                {whoseChoices.map((o) => (
                  <option key={o.value} value={o.value}>{o.label}</option>
                ))}
              </select>
            </label>
          )}
          {onNewJob && (
            <button type="button" className="btn btn-primary jb-new" onClick={onNewJob}>
              New job
            </button>
          )}
        </div>
      </header>

      <SituationStrip
        overview={overview}
        overviewErr={overviewErr}
        onRetry={onRetryOverview}
        onTile={onTile}
        filter={filter}
        view={view}
      />
      <FreshnessLine overview={overview} />

      <SuggestionsStrip
        suggestions={suggestions}
        busyId={busyId}
        note={suggestionNote}
        onApprove={onApproveSuggestion}
        onDecline={onDeclineSuggestion}
        onEdit={onEditSuggestion}
      />

      <div className="jb-bar">
        <div className="wk-views" role="tablist" aria-label="View">
          {WORK_VIEWS.map((v) => (
            <button
              key={v.key}
              type="button"
              role="tab"
              aria-selected={view === v.key}
              className={`wk-view${view === v.key ? ' is-on' : ''}`}
              onClick={() => changeView(v.key)}
            >
              {v.label}
            </button>
          ))}
        </div>
        {view === 'jobs' && (
          <div className="wk-density" role="group" aria-label="Density">
            <button
              type="button"
              className={`wk-density-btn${density === 'rows' ? ' is-on' : ''}`}
              aria-pressed={density === 'rows'}
              onClick={() => changeDensity('rows')}
            >
              Rows
            </button>
            <button
              type="button"
              className={`wk-density-btn${density === 'list' ? ' is-on' : ''}`}
              aria-pressed={density === 'list'}
              onClick={() => changeDensity('list')}
            >
              List
            </button>
          </div>
        )}
        {view !== 'done' && (
          <div className="jb-filters">
            {STATUS_CHIPS.map((f) => (
              <button
                key={f}
                type="button"
                className={`jb-pill${filter === f ? ' is-on' : ''}`}
                aria-pressed={filter === f}
                onClick={() => onFilter(filter === f ? null : f)}
              >
                {WORK_FILTER_LABELS[f]}
              </button>
            ))}
            <button
              type="button"
              className={`jb-pill is-toggle${todayOnly ? ' is-on' : ''}`}
              aria-pressed={todayOnly}
              onClick={() => setTodayOnly((v) => !v)}
            >
              Touched today
            </button>
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
          </div>
        )}
      </div>

      {view === 'done' ? (
        <CompletedView {...completed} onOpenItem={onOpenItem} onOpenJob={onOpenJob} />
      ) : (
        <>
          {/* The scope, stated: whose work, and how much of it this page
              holds. The tiles are server totals; the rows below are one
              page, so what "these rows" means has to be visible. */}
          <p className="jb-scope">
            {scope.map((s, i) => (
              <span key={s}>
                {i > 0 && <span className="dash-dot-sep">·</span>}
                {s}
              </span>
            ))}
            <span className="dash-dot-sep">·</span>
            <span>{todayOnly ? 'Touched today' : 'All open work'}</span>
          </p>

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

          {empty && whoseEmptyCopy(whose) && (
            <div className="board-empty">
              <p className="board-empty-lead">{whoseEmptyCopy(whose)}</p>
              <p className="board-empty-sub">Widen Whose work above to see the rest.</p>
            </div>
          )}

          {empty && !whoseEmptyCopy(whose) && (
            <div className="board-empty">
              <p className="board-empty-lead">No named work yet.</p>
              <p className="board-empty-sub">
                Connect an agent and its work shows up here on its own. You will not have to enter any of it.
              </p>
              {onConnectAgent && (
                <button type="button" className="btn btn-primary" onClick={onConnectAgent}>
                  Connect an agent
                </button>
              )}
            </div>
          )}

          {items && !empty && !filteredOut && view === 'open' && (
            openRows.length === 0 ? (
              <div className="board-empty">
                <p className="board-empty-lead">Nothing open right now.</p>
                <p className="board-empty-sub">Everything on this page has closed. Completed shows what got done.</p>
              </div>
            ) : (
              <WorkTable
                rows={openRows}
                jobMeta={tableMeta}
                onOpen={onOpenItem}
                onOpenJob={onOpenJob}
                onOpenRun={onOpenItem}
                nextCursor={nextCursor}
                onLoadMore={onLoadMore}
              />
            )
          )}

          {items && !empty && !filteredOut && view === 'jobs' && density === 'list' && (
            <JobList grouped={grouped} now={now} onOpenJob={onOpenJob} />
          )}

          {items && !empty && !filteredOut && view === 'jobs' && density === 'rows' && (
            <div className="wk-jobs">
              {grouped.map((g) => (
                <JobRow
                  key={g.key}
                  grouped={g}
                  now={now}
                  onOpenJob={onOpenJob}
                  onOpenItem={onOpenItem}
                  onEditJob={onEditJob}
                  expanded={expanded.has(g.key)}
                  onToggle={() => toggleJob(g.key)}
                />
              ))}
              {nextCursor && onLoadMore && (
                <button type="button" className="btn btn-secondary btn-sm work-more" onClick={onLoadMore}>
                  Load more
                </button>
              )}
            </div>
          )}
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
  // Opens the job editor on one job. On a DERIVED job that is the promotion
  // path — the person names and describes what Trovis only filed.
  onEditWorkflow = null,
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
  // Which of the three views is on screen. A tile can switch it (Done this
  // week opens Completed); the URL does not carry it, so Back returns to
  // the job or run you came from, not to a view toggle.
  const [view, setView] = useState(resolveView(null))

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
    // A Home tile lands where its rows are visible: Done on Completed, any
    // status filter on the flat table, nothing on the job board.
    const v = incomingFilter?.value
    setView(v === 'done' ? 'done' : v ? 'open' : 'jobs')
    // A navigation may carry Home's whose-work selection so the destination
    // shows the same slice the reader tapped away from. `reconcileWhose`
    // above still clamps it against the seat, so an arriving selection can
    // only ever ask. Omitted (null) leaves Work's own selection alone.
    if (incomingFilter?.whose) {
      setWhose(
        incomingFilter.personId
          ? `person:${incomingFilter.personId}`
          : incomingFilter.whose,
      )
    }
    // Only the nonce drives this: a Home card sets it on every navigation.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filterNonce])

  // This kind's CLOSED work. The main page carries done rows only
  // incidentally (it is a mixed page of 50 ordered by recency), so the kind
  // page asks for them directly. One extra lean request, column-filtered —
  // never the board.
  const [finished, setFinished] = useState(null)
  const [finishedLoading, setFinishedLoading] = useState(false)
  const [finishedCursor, setFinishedCursor] = useState(null)

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

  // The job's own definition, expectation and observed numbers. One lean
  // read, only on the job page — the board never needs this depth, and the
  // aggregates come precomputed so the client does no arithmetic on them.
  const [jobDetail, setJobDetail] = useState(null)
  const [jobDetailErr, setJobDetailErr] = useState(null)
  // Re-read when the pane comes back on screen too: the editor is an
  // overlay over this pane, so a saved version (or a promotion) returns to
  // a job page that must show it — the same page, one lean read newer.
  useEffect(() => {
    if (!route.job || !active) {
      if (!route.job) setJobDetail(null)
      return undefined
    }
    setJobDetailErr(null)
    return startAbortable(({ signal, isAlive }) => {
      api
        .getWorkflow(route.job, { signal })
        .then((d) => isAlive() && setJobDetail(d))
        .catch((e) => isAlive() && setJobDetailErr(e))
    })
  }, [route.job, active])
  // THIS job's runs, in every state — one lean column-filtered read.
  //
  // Not sliced out of the board's 50-row page: that page is ordered by
  // recency across every job, so on a busy account one job's runs are
  // whatever happens to have survived the crowd. Asking by workflow_id gets
  // this job's actual recent history, and it matches on the id rather than
  // the name, so renaming a job does not empty its own page.
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
          signal,
        })
        .then((p) => {
          if (!isAlive()) return
          setFinished(Array.isArray(p?.items) ? p.items : [])
          setFinishedCursor(p?.next_cursor || null)
          setFinishedLoading(false)
        })
        .catch(() => isAlive() && setFinishedLoading(false))
    })
  }, [kindOpen, kindWorkflowId])

  // The next page of THIS job's history — the same question, continued.
  async function loadMoreFinished() {
    if (!finishedCursor || !kindOpen) return
    try {
      const page = await api.getWorkItems({
        cursor: finishedCursor,
        limit: 50,
        workflowId: kindWorkflowId === null ? 'none' : kindWorkflowId,
      })
      setFinished((prev) => [...(prev || []), ...(page?.items || [])])
      setFinishedCursor(page?.next_cursor || null)
    } catch {
      /* keep last-good rows */
    }
  }

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
  // change between ticks the way live runs do; they change when a person
  // saves the editor, which is an overlay over this pane — so the list is
  // re-read when the pane comes back on screen, and otherwise left alone.
  useEffect(
    () => {
      if (!active) return undefined
      return startAbortable(({ signal, isAlive }) => {
        api.getWorkflows({ signal })
          .then((r) => isAlive() && setJobs(Array.isArray(r) ? r : []))
          .catch(() => isAlive() && setJobs([]))
      })
    },
    [refreshKeyForJobs, active],
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

  // The Completed view's own data, fetched only when it is opened. The
  // mixed open page carries done rows incidentally; this asks for them
  // directly, lean and column-filtered — never the board. The chart reads
  // the same LLM-free snapshot Home draws, in the viewer's own timezone.
  const [done, setDone] = useState(null)
  const [doneErr, setDoneErr] = useState(null)
  const [doneLoading, setDoneLoading] = useState(false)
  const [doneCursor, setDoneCursor] = useState(null)
  const [snapshot, setSnapshot] = useState(null)
  const [snapshotErr, setSnapshotErr] = useState(null)
  const [completedKey, setCompletedKey] = useState(0)
  const completedWanted = view === 'done' && !route.job && !route.run
  useEffect(() => {
    if (!completedWanted) return undefined
    setDoneLoading(true)
    setDoneErr(null)
    setSnapshotErr(null)
    const { timeZone } = viewerClock()
    return startAbortable(({ signal, isAlive }) => {
      api
        .getWorkItems({ limit: 50, status: 'done', ...whoseParams(whoseRef.current), signal })
        .then((p) => {
          if (!isAlive()) return
          setDone(Array.isArray(p?.items) ? p.items : [])
          setDoneCursor(p?.next_cursor || null)
        })
        .catch((e) => isAlive() && setDoneErr(e?.message || "Can't load completed work"))
        .finally(() => isAlive() && setDoneLoading(false))
      api
        .getHomeSnapshot({ days: 7, tz: timeZone, ...whoseParams(whoseRef.current), signal })
        .then((snap) => isAlive() && setSnapshot(snap))
        .catch((e) => isAlive() && setSnapshotErr(e?.message || "Can't load the completion history"))
    })
    // Re-asked when the view opens, Whose work changes, or Retry is pressed.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [completedWanted, whose, completedKey])

  async function loadMoreDone() {
    if (!doneCursor) return
    try {
      const page = await api.getWorkItems({
        cursor: doneCursor, limit: 50, status: 'done', ...whoseParams(whoseRef.current),
      })
      setDone((prev) => [...(prev || []), ...(page?.items || [])])
      setDoneCursor(page?.next_cursor || null)
    } catch {
      /* keep last-good rows */
    }
  }

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
        // The run's own job, by the id the lean detail carries — the same
        // job route a row click takes, so the header can name and open it.
        onOpenJob={(jobId) => onRoute({ job: jobId, run: null })}
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
      <JobPage
        job={jobDetail}
        jobErr={jobDetailErr}
        name={kindName}
        runs={finished}
        runsLoading={finishedLoading}
        runsCursor={finishedCursor}
        onLoadMoreRuns={loadMoreFinished}
        filter={filter}
        onFilter={setFilter}
        onClearFilter={() => setFilter(null)}
        onBack={() => onRoute({ job: null, run: null })}
        onOpenItem={(it) => onRoute({ job: it.workflow_id ?? route.job, run: it.id })}
        onOpenAgent={onOpenAgent}
        onEditJob={onEditWorkflow}
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
      onFilter={setFilter}
      onClearFilter={() => setFilter(null)}
      view={view}
      onView={setView}
      onOpenItem={(it) => onRoute({ job: it.workflow_id ?? route.job, run: it.id })}
      onOpenJob={(id) => onRoute({ job: Number(id), run: null })}
      onEditJob={onEditWorkflow}
      onNewJob={onNewWorkflow}
      whose={whose}
      onWhoseChange={setWhose}
      whoseChoices={whoseOptions(seat, people)}
      completed={{
        snapshot,
        snapshotErr,
        onRetrySnapshot: () => setCompletedKey((n) => n + 1),
        done,
        doneErr,
        doneLoading,
        onRetryDone: () => setCompletedKey((n) => n + 1),
        nextCursor: doneCursor,
        onLoadMore: loadMoreDone,
        timeZone: viewerClock().timeZone,
      }}
    />
    )
  )
}
