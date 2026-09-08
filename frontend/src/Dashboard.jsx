import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from './api.js'
import { startAbortable } from './abortable.js'
import { TaskPanel } from './Board.jsx'
import { WorkLoadFailed } from './ui.jsx'
import { itemToCard, workUpdatedLabel } from './board.js'
// Costs always render in dollars (e.g. "$0.68"); shared with Fleet so they match.
import { formatCost as fmtMoney } from './utils.js'
import {
  asOfLabel,
  briefingBullets,
  briefingLead,
  isFirstRun,
  lookAtRows,
  showCostPulse,
} from './home.js'
import { TrovisMark } from './Icons.jsx'

// ---------------------------------------------------------------------------
// Home v2 — hybrid work, served judgment.
//
// Sections, top to bottom:
//   1. Daily Briefing   — today's state in one card, readable in <30s
//   2. What to look at  — needs you + needs attention, hidden when empty
//   3. Work feed        — the ambient chronological story
//   4. Cost pulse       — a whisper; hidden at $0
//
// NO Fleet on Home. Fleet is tab 2, and Home must not first-paint GET /agents:
// that request is what made Home expensive, and the fleet grid was the only
// thing needing it. Home must also never call /work/board or /work/summary —
// both loop-scan the whole board and starve the single replica. Home reads the
// lean pair (/work/overview + /work/items), the same truth the Work tab shows.
//
// Every section fetches independently and fails independently: one dead
// section renders its own Retry and the rest of Home still paints. Each
// fetch carries an AbortSignal so unmount / tab switch / logout drops it
// instead of waiting out the timeout (the briefing runs to 120s).
//
// Ask is NOT here — the global AskPill (⌘K, App.jsx) covers every page.
// ---------------------------------------------------------------------------

// One page of named work is enough to fill a briefing (3+3+2) and a 7-row
// queue. Home never paginates; "+N more" sends you to the Work tab.
const WORK_PAGE = 50

function fmtRel(iso) {
  const ms = Date.now() - Date.parse(iso)
  if (Number.isNaN(ms)) return ''
  const m = Math.floor(ms / 60000)
  if (m < 1) return 'just now'
  if (m < 60) return `${m}m ago`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ago`
  return `${Math.floor(h / 24)}d ago`
}

// `active` is false while the Home pane is off screen. App.jsx keeps every tab
// pane mounted (#130 keep-alive), so a hidden Home is still listening for
// focus — without this it would re-sync all six of its endpoints, the Claude
// briefing among them, for a pane nobody is looking at. Same rule the Work
// pane applies to its poll.
export default function Dashboard({
  onOpenAgent,
  onOpenCost,
  onViewAllWorkFeed,
  onGoWork,
  userName,
  active = true,
}) {
  // Silently re-sync when the tab regains focus (throttled to once per 30s).
  // Cards keep their current data on screen while refetching — no skeleton
  // flash. There is no interval poll: #127 removed the 15s briefing re-poll,
  // and nothing here may bring it back.
  const [refreshKey, setRefreshKey] = useState(0)
  const activeRef = useRef(active)
  activeRef.current = active
  useEffect(() => {
    let last = Date.now()
    function maybeRefresh() {
      if (document.hidden || !activeRef.current) return
      if (Date.now() - last > 30000) {
        last = Date.now()
        setRefreshKey((k) => k + 1)
      }
    }
    window.addEventListener('focus', maybeRefresh)
    document.addEventListener('visibilitychange', maybeRefresh)
    return () => {
      window.removeEventListener('focus', maybeRefresh)
      document.removeEventListener('visibilitychange', maybeRefresh)
    }
  }, [])

  // The work pair powers BOTH the briefing bullets and "What to look at", so
  // it is fetched once here rather than twice. Its failure is its own: the
  // briefing still renders its narrative, the feed and cost are untouched.
  const work = useWork(refreshKey)
  const briefing = useBriefing(refreshKey)
  const feed = useSection((signal) => api.getWorkFeed({ signal }), refreshKey)
  const cost = useSection((signal) => api.getCost({ signal }), refreshKey)
  // Agent-health attention (an agent erroring or gone quiet) is not named
  // work, so it never enters the work queue — but it belongs in a briefing.
  const health = useSection((signal) => api.getAttention({ signal }), refreshKey)

  const [openItem, setOpenItem] = useState(null)

  const firstRun = isFirstRun({
    overview: work.overview,
    items: work.items,
    feed: feed.data,
  })

  return (
    <div className="dash">
      <Greeting userName={userName} />

      {firstRun ? (
        <FirstRunCard onGoWork={onGoWork} />
      ) : (
        <>
          <DailyBriefing
            briefing={briefing}
            work={work}
            health={health.data}
            onOpenItem={setOpenItem}
            onOpenAgent={onOpenAgent}
            onGoWork={onGoWork}
          />
          <WhatToLookAt work={work} onOpenItem={setOpenItem} onGoWork={onGoWork} />
          <WorkFeedSection
            feed={feed}
            onViewAll={onViewAllWorkFeed}
            onOpenAgent={onOpenAgent}
          />
          <CostPulse cost={cost.data} onOpenCost={onOpenCost} />
        </>
      )}

      {openItem && (
        <TaskPanel
          card={itemToCard(openItem)}
          onClose={() => setOpenItem(null)}
          onResolved={() => setOpenItem(null)}
        />
      )}
    </div>
  )
}

// --- data hooks ------------------------------------------------------------

/**
 * One independent section fetch. Keeps the last good value on a refetch
 * failure (a blip must not blank a section that was already showing) and
 * exposes `failed` only when we have nothing to show.
 */
function useSection(fetcher, refreshKey) {
  const [data, setData] = useState(null)
  const [err, setErr] = useState(null)
  const [nonce, setNonce] = useState(0)

  // Call sites pass an inline arrow, so `fetcher` is a new function every
  // render. Hold it in a ref and keep it OUT of the effect deps — in the deps
  // it would re-fire (and re-abort) the request on every render.
  const fetchRef = useRef(fetcher)
  fetchRef.current = fetcher

  useEffect(
    () =>
      startAbortable(({ signal, isAlive }) => {
        fetchRef
          .current(signal)
          .then((d) => {
            if (!isAlive()) return
            setData(Array.isArray(d) || d ? d : null)
            setErr(null)
          })
          .catch((e) => {
            if (!isAlive()) return
            setErr(e)
          })
      }),
    [refreshKey, nonce],
  )

  return {
    data,
    err,
    failed: data === null && !!err,
    loading: data === null && !err,
    retry: useCallback(() => {
      setErr(null)
      setNonce((n) => n + 1)
    }, []),
  }
}

/** Counts + named items, settled independently so one can fail alone. */
function useWork(refreshKey) {
  const [overview, setOverview] = useState(null)
  const [items, setItems] = useState(null)
  const [err, setErr] = useState(null)
  const [nonce, setNonce] = useState(0)

  useEffect(
    () =>
      startAbortable(({ signal, isAlive }) => {
        const ov = api
          .getWorkOverview({ signal })
          .then((d) => isAlive() && setOverview(d || null))
        const it = api
          .getWorkItems({ limit: WORK_PAGE, signal })
          .then(
            (p) => isAlive() && setItems(Array.isArray(p?.items) ? p.items : []),
          )
        Promise.allSettled([ov, it]).then((rs) => {
          if (!isAlive()) return
          const bad = rs.find((r) => r.status === 'rejected')
          setErr(bad ? bad.reason : null)
        })
      }),
    [refreshKey, nonce],
  )

  return {
    overview,
    items,
    err,
    // Only a hard failure — one of the two landing is enough to render.
    failed: overview === null && items === null && !!err,
    loading: overview === null && items === null && !err,
    retry: useCallback(() => {
      setErr(null)
      setNonce((n) => n + 1)
    }, []),
  }
}

/** The Claude briefing. Cheap on the server (cached, 3s cap) but still the
 *  slowest call on the page, so it never gates anything else. */
function useBriefing(refreshKey) {
  const [data, setData] = useState(null)
  const [err, setErr] = useState(null)
  const [nonce, setNonce] = useState(0)

  useEffect(
    () =>
      startAbortable(({ signal, isAlive }) => {
        api
          .getBriefing({ signal })
          .then((d) => {
            if (!isAlive()) return
            setData(d)
            setErr(null)
          })
          .catch((e) => isAlive() && setErr(e))
      }),
    [refreshKey, nonce],
  )

  return {
    data,
    err,
    // Last-known wins: a failed refetch keeps yesterday's line on screen.
    failed: data === null && !!err,
    loading: data === null && !err,
    retry: useCallback(() => {
      setErr(null)
      setNonce((n) => n + 1)
    }, []),
  }
}

// --- greeting --------------------------------------------------------------

function Greeting({ userName }) {
  const now = new Date()
  const h = now.getHours()
  const part = h < 12 ? 'morning' : h < 18 ? 'afternoon' : 'evening'
  const name = (userName || '').split(/[ @]/)[0] || 'there'
  const date = now.toLocaleDateString(undefined, {
    weekday: 'long',
    year: 'numeric',
    month: 'long',
    day: 'numeric',
  })
  return (
    <div className="dash-greeting">
      <h1 className="dash-hello">
        Good {part}, {name}
      </h1>
      <div className="dash-date">{date}</div>
    </div>
  )
}

// --- 1. Daily Briefing -----------------------------------------------------

// Calm primary narrative: large plain type, no alarm chrome. The only alarm
// on this card is the text itself when the text is the alert.
function DailyBriefing({ briefing, work, health, onOpenItem, onOpenAgent, onGoWork }) {
  const data = briefing.data
  const counts = work.overview
  const lead = briefingLead(counts)
  const bullets = briefingBullets(work.items || [])
  const asOf = asOfLabel(data?.generated_at)

  // Agent-health rows only fill the space work items left — a named, clickable
  // work item always outranks an agent-level observation.
  const healthLines = (health || [])
    .filter((h) => h && h.severity !== 'info' && (h.title || h.detail))
    .slice(0, Math.max(0, 3 - bullets.stuck.length))

  const allClear =
    !!counts &&
    bullets.needsYou.length === 0 &&
    bullets.stuck.length === 0 &&
    healthLines.length === 0

  return (
    <section className="dash-card home-brief" aria-label="Daily briefing">
      <div className="dash-card-head">
        <span className="dash-sq">
          <TrovisMark size={10} />
        </span>
        <span className="dash-briefing-label">Daily Briefing</span>
      </div>

      {briefing.loading && !counts ? (
        <div className="dash-skel">
          <span style={{ width: '92%' }} />
          <span style={{ width: '78%' }} />
        </div>
      ) : (
        <>
          {lead && <p className="home-brief-lead">{lead}</p>}

          {/* Claude's narrative is colour, not the headline — and it is the
              one part that can be missing without costing the reader the
              state of today. */}
          {data?.summary ? (
            <p className="home-brief-narrative">{data.summary}</p>
          ) : briefing.failed ? (
            <p className="home-brief-narrative is-muted">
              Today&apos;s summary didn&apos;t load.{' '}
              <button type="button" className="dash-link" onClick={briefing.retry}>
                Retry
              </button>
            </p>
          ) : null}

          {work.failed ? (
            <p className="home-brief-narrative is-muted">
              Couldn&apos;t load what&apos;s on your plate.{' '}
              <button type="button" className="dash-link" onClick={work.retry}>
                Retry
              </button>
            </p>
          ) : (
            <>
              <BriefGroup
                label="Needs you"
                tone="warn"
                items={bullets.needsYou}
                emptyText={allClear ? null : 'None'}
                onOpenItem={onOpenItem}
              />
              <BriefGroup
                label="Needs attention"
                tone="error"
                items={bullets.stuck}
                extra={healthLines}
                onOpenAgent={onOpenAgent}
                onOpenItem={onOpenItem}
              />
              <BriefGroup
                label="Moving"
                tone="quiet"
                items={bullets.moving}
                onOpenItem={onOpenItem}
              />
              {allClear && (
                <p className="home-brief-clear">
                  Nothing is waiting on a person right now.
                </p>
              )}
            </>
          )}
        </>
      )}

      <div className="home-brief-foot">
        <span>Today</span>
        {asOf && (
          <>
            <span className="dash-dot-sep">·</span>
            <span>{asOf}</span>
          </>
        )}
        {onGoWork && (
          <>
            <span className="dash-dot-sep">·</span>
            <button type="button" className="dash-link" onClick={onGoWork}>
              Open Work
            </button>
          </>
        )}
      </div>
    </section>
  )
}

/**
 * One briefing group. Omitted entirely when it has nothing AND no explicit
 * "None" to print — an empty "Moving" heading is noise, an empty "Needs you"
 * is worth stating.
 */
function BriefGroup({ label, tone, items, extra = [], emptyText, onOpenItem, onOpenAgent }) {
  const has = items.length > 0 || extra.length > 0
  if (!has && !emptyText) return null
  return (
    <div className={`home-brief-group tone-${tone}`}>
      <span className="home-brief-group-label">{label}</span>
      {!has ? (
        <span className="home-brief-none">{emptyText}</span>
      ) : (
        <ul className="home-brief-list">
          {items.map((it) => (
            <li key={`i${it.id}`}>
              <button
                type="button"
                className="home-brief-item"
                onClick={() => onOpenItem && onOpenItem(it)}
              >
                <span className="home-brief-title">{it.title}</span>
                {it.whats_next && (
                  <span className="home-brief-why">{it.whats_next}</span>
                )}
              </button>
            </li>
          ))}
          {extra.map((h, i) => (
            <li key={`h${h.agent}-${i}`}>
              <button
                type="button"
                className="home-brief-item"
                onClick={() => onOpenAgent && onOpenAgent(h.agent, 'main')}
              >
                <span className="home-brief-title">{h.title || h.agent}</span>
                {(h.detail || h.recommendation) && (
                  <span className="home-brief-why">
                    {h.detail || h.recommendation}
                  </span>
                )}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

// --- 2. What to look at ----------------------------------------------------

// The loudest thing on Home when it has rows — and gone entirely when it
// doesn't. Healthy silence: no "all clear" card, no empty alarm chrome.
function WhatToLookAt({ work, onOpenItem, onGoWork }) {
  if (work.failed) {
    return (
      <section className="dash-section" aria-label="What to look at">
        <div className="dash-section-head">
          <span className="dash-section-title">What to look at</span>
        </div>
        <WorkLoadFailed lead="Can't load what needs you" onRetry={work.retry} />
      </section>
    )
  }
  if (work.items === null) return null // loading: stay silent, don't reserve alarm space

  const { rows, hidden } = lookAtRows(work.items)
  if (rows.length === 0) return null

  return (
    <section className="dash-section home-lookat" aria-label="What to look at">
      <div className="dash-section-head">
        <span className="dash-section-title">What to look at</span>
        {onGoWork && (
          <button type="button" className="dash-link" onClick={onGoWork}>
            Open Work →
          </button>
        )}
      </div>
      <div className="home-lookat-list">
        {rows.map((row) => (
          <button
            key={row.id}
            type="button"
            className={`home-lookat-row ${
              row.status === 'waiting_on_you' ? 'is-waiting-you' : 'is-stuck'
            }`}
            onClick={() => onOpenItem(row)}
          >
            <span className="home-lookat-title">{row.title}</span>
            {/* whats_next already names the holder ("Waiting on Sarah Chen"),
                so a holder column here would just say it twice. */}
            <span className="home-lookat-next">{row.whats_next || ''}</span>
            <span className="home-lookat-age">{workUpdatedLabel(row.updated_at)}</span>
          </button>
        ))}
      </div>
      {hidden > 0 && onGoWork && (
        <button type="button" className="dash-link home-lookat-more" onClick={onGoWork}>
          {hidden} more in Work →
        </button>
      )}
    </section>
  )
}

// --- 3. Work feed ----------------------------------------------------------

// Ambient: hairline rows, muted type, no per-row alarm wash. This is the
// story of what happened, not a queue of what to do.
function WorkFeedSection({ feed, onViewAll, onOpenAgent }) {
  const rows = feed.data || []
  return (
    <section className="dash-section" aria-label="Work feed">
      <div className="dash-section-head">
        <span className="dash-section-title">Work feed</span>
        {onViewAll && (
          <button type="button" className="dash-link" onClick={onViewAll}>
            View all →
          </button>
        )}
      </div>
      <div className="dash-card dash-feed home-feed">
        {feed.loading ? (
          <div className="dash-skel pad">
            <span style={{ width: '70%' }} />
            <span style={{ width: '88%' }} />
          </div>
        ) : feed.failed ? (
          <div className="dash-empty pad" role="alert">
            Couldn&apos;t load recent activity.{' '}
            <button type="button" className="dash-link" onClick={feed.retry}>
              Retry
            </button>
          </div>
        ) : rows.length === 0 ? (
          <div className="dash-empty pad">Nothing has happened in the last day.</div>
        ) : (
          rows.map((f, i) => (
            <button
              key={`${f.agent}-${i}`}
              type="button"
              className="dash-feed-row home-feed-row"
              onClick={() => onOpenAgent && onOpenAgent(f.agent, 'main')}
            >
              <span className="dash-feed-top">
                <span className="dash-feed-agent">{f.agent}</span>
                <span className="dash-dot-sep">·</span>
                <span className="dash-feed-time">{fmtRel(f.time)}</span>
              </span>
              <span className="dash-feed-summary">{f.summary}</span>
            </button>
          ))
        )}
      </div>
    </section>
  )
}

// --- 4. Cost pulse ---------------------------------------------------------

// The quietest thing on the page, and absent entirely at $0 — a demo account
// should not be told it spent nothing.
function CostPulse({ cost, onOpenCost }) {
  if (!showCostPulse(cost)) return null
  return (
    <button type="button" className="home-cost-pulse" onClick={onOpenCost}>
      About {fmtMoney(cost.today)} today
      <span className="home-cost-link">Cost →</span>
    </button>
  )
}

// --- first run -------------------------------------------------------------

// Minimal first-run gate: everything loaded and everything is empty. No org
// chart, no wizard — an individual gets the same calm card and full access to
// Work and Ask.
function FirstRunCard({ onGoWork }) {
  return (
    <div className="dash-card dash-waiting">
      <div className="dash-waiting-pulse" aria-hidden="true">
        <span className="dash-sq">
          <TrovisMark size={11} />
        </span>
      </div>
      <h2 className="dash-waiting-title">Nothing to show yet</h2>
      <p className="dash-waiting-sub">
        Once your agents and teammates start working, today&apos;s state shows up
        here — what needs you, what&apos;s stuck, and what moved.
      </p>
      {onGoWork && (
        <button type="button" className="btn btn-primary" onClick={onGoWork}>
          Go to Work
        </button>
      )}
    </div>
  )
}
