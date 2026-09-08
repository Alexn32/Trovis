import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from './api.js'
import { startAbortable } from './abortable.js'
import { TaskPanel } from './Board.jsx'
import { WorkLoadFailed } from './ui.jsx'
import Sparkline from './Sparkline.jsx'
import { itemToCard, workUpdatedLabel } from './board.js'
// Costs always render in dollars (e.g. "$0.68"); shared with Fleet and the
// Cost page so every surface prints the same number the same way.
import { formatCost as fmtMoney } from './utils.js'
import { asOfLabel, briefingLead, isFirstRun, partitionLookAt, workSplit } from './home.js'
import { TrovisMark, ChevronDownIcon, ChevronRightIcon } from './Icons.jsx'

// ---------------------------------------------------------------------------
// Home — snapshots of pages that already exist.
//
// Home is a hub, not a product of its own. Every card is a preview of a real
// page and every click lands on it: a row opens that work item, a header or
// tile opens Work (filtered), the cost figure opens the Cost page, the feed
// opens the Work feed, an agent opens that agent. Nothing here is a Home-only
// invention, and nothing here is a dead tap.
//
// Cards, in the order the brief numbers them:
//   1. Needs attention — omitted ENTIRELY when empty (no "all clear" prose)
//   2. Cost            — one today figure, from /dashboard/cost, + a 7d spark
//   3. Work feed       — a few ambient lines
//   4. Work            — Moving / Waiting / Stuck / Done
//   5. Fleet           — health dots, omitted when nothing needs attention
//
// What Home deliberately does NOT do on first paint: no briefing essay (it is
// behind a quiet disclosure that fetches only when opened), no judgment
// ribbon, no suggestion strips, and exactly ONE dollar figure on the page.
//
// Data: the lean pair (/work/overview + /work/items) plus the existing feed,
// cost and attention endpoints. Never /work/board, /work/summary or /agents —
// those are what starve the single replica. Each card fetches independently,
// carries an AbortSignal, and fails on its own.
// ---------------------------------------------------------------------------

// One lean page of named work is enough for a snapshot; the real table lives
// on Work, which every tile links to.
const WORK_PAGE = 50

// Rows shown in the Needs attention preview before it defers to Work.
const ATTENTION_PREVIEW = 5

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

export default function Dashboard({
  onOpenAgent,
  onOpenCost,
  onViewAllWorkFeed,
  onGoWork,
  onGoFleet,
  userName,
  active = true,
}) {
  // Silently re-sync when the tab regains focus (throttled to once per 30s).
  // No interval poll — #127 removed the 15s briefing re-poll and nothing here
  // may bring it back. Gated on `active` so a Home kept alive behind another
  // tab (#130) does not refetch for a pane nobody is looking at.
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

  // The lean work pair feeds both the Needs attention card and the Work card,
  // so it is fetched once here rather than twice.
  const work = useWork(refreshKey)
  const cost = useSection((signal) => api.getCost({ signal }), refreshKey)
  const feed = useSection((signal) => api.getWorkFeed({ signal }), refreshKey)
  // Agent health. Also the Fleet card's only source: it names the agents that
  // need looking at, and links to Fleet for the full roster.
  const health = useSection((signal) => api.getAttention({ signal }), refreshKey)

  const [openItem, setOpenItem] = useState(null)

  const firstRun = isFirstRun({
    overview: work.overview,
    items: work.items,
    feed: feed.data,
  })

  return (
    <div className="dash home">
      <Greeting userName={userName} />

      {firstRun ? (
        <FirstRunCard onGoWork={onGoWork} />
      ) : (
        <>
          <NeedsAttentionCard work={work} onOpenItem={setOpenItem} onGoWork={onGoWork} />
          <CostCard cost={cost} onOpenCost={onOpenCost} />
          <WorkFeedCard feed={feed} onViewAll={onViewAllWorkFeed} onOpenAgent={onOpenAgent} />
          <WorkCard work={work} onGoWork={onGoWork} />
          <FleetCard health={health} onOpenAgent={onOpenAgent} onGoFleet={onGoFleet} />
          <BriefingDisclosure work={work} refreshKey={refreshKey} />
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
 * One independent card fetch. Keeps the last good value on a refetch failure
 * (a blip must not blank a card that was already showing) and reports
 * `failed` only when there is nothing to show.
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
  const [truncated, setTruncated] = useState(false)
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
          .then((p) => {
            if (!isAlive()) return
            setItems(Array.isArray(p?.items) ? p.items : [])
            // More pages exist, so any count derived from this page is a
            // floor, not a total. The Work card says so rather than lying.
            setTruncated(Boolean(p?.next_cursor))
          })
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
    truncated,
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

/**
 * The briefing, fetched ONLY once someone opens the disclosure. It is the
 * slowest call the dashboard can make (a Claude call behind a 3s server cap),
 * and Home's first paint is now preview cards — so it must not be on it.
 */
function useLazyBriefing(open, refreshKey) {
  const [data, setData] = useState(null)
  const [err, setErr] = useState(null)
  const [nonce, setNonce] = useState(0)

  useEffect(() => {
    if (!open) return undefined
    return startAbortable(({ signal, isAlive }) => {
      api
        .getBriefing({ signal })
        .then((d) => {
          if (!isAlive()) return
          setData(d)
          setErr(null)
        })
        .catch((e) => isAlive() && setErr(e))
    })
  }, [open, refreshKey, nonce])

  return {
    data,
    err,
    failed: data === null && !!err,
    loading: open && data === null && !err,
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

/** Card shell: title, an optional link to the page this previews, children. */
function PreviewCard({ title, action, onAction, className = '', children }) {
  return (
    <section className={`dash-card home-card ${className}`} aria-label={title}>
      <div className="home-card-head">
        <span className="dash-section-title">{title}</span>
        {action && onAction && (
          <button type="button" className="dash-link" onClick={onAction}>
            {action}
          </button>
        )}
      </div>
      {children}
    </section>
  )
}

// --- 1. Needs attention ----------------------------------------------------

// Work that cannot move on its own: waiting on you, stuck, or sitting too
// long with someone else. The whole card is absent on a healthy day — no
// empty state, no "all clear" line.
function NeedsAttentionCard({ work, onOpenItem, onGoWork }) {
  if (work.failed) {
    return (
      <PreviewCard title="Needs attention">
        <WorkLoadFailed lead="Can't load what needs you" onRetry={work.retry} />
      </PreviewCard>
    )
  }
  if (work.items === null) return null // loading: stay silent, reserve nothing

  const { needsYou, needsAttention } = partitionLookAt(work.items)
  const rows = [...needsYou, ...needsAttention]
  if (rows.length === 0) return null

  const shown = rows.slice(0, ATTENTION_PREVIEW)
  const hidden = rows.length - shown.length

  return (
    <PreviewCard
      title="Needs attention"
      action="Open in Work →"
      onAction={onGoWork ? () => onGoWork('attention') : undefined}
      className="home-attention"
    >
      <ul className="home-att-list">
        {shown.map((row) => (
          <li key={row.id}>
            <button
              type="button"
              className={`home-att-row ${
                row.status === 'waiting_on_you' ? 'is-waiting-you' : 'is-stuck'
              }`}
              onClick={() => onOpenItem(row)}
            >
              <span className="home-att-title">{row.title}</span>
              <span className="home-att-next">{row.whats_next || ''}</span>
              <span className="home-att-age">{workUpdatedLabel(row.updated_at)}</span>
            </button>
          </li>
        ))}
      </ul>
      {hidden > 0 && onGoWork && (
        <button type="button" className="dash-link home-card-more" onClick={() => onGoWork('attention')}>
          {hidden} more in Work →
        </button>
      )}
    </PreviewCard>
  )
}

// --- 2. Cost ---------------------------------------------------------------

// The only dollar figure on Home, and the same one the Cost page shows — both
// read /dashboard/cost. Two sources would eventually disagree in prose.
function CostCard({ cost, onOpenCost }) {
  const c = cost.data
  // Last 7 points of the daily series the endpoint already returns.
  const week = Array.isArray(c?.daily) ? c.daily.slice(-7) : []

  return (
    <PreviewCard
      title="Cost"
      action="Open Cost →"
      onAction={onOpenCost}
      className="home-cost"
    >
      {cost.loading ? (
        <div className="dash-skel"><span style={{ width: '40%', height: 22 }} /></div>
      ) : cost.failed ? (
        <div className="dash-empty" role="alert">
          Couldn&apos;t load cost.{' '}
          <button type="button" className="dash-link" onClick={cost.retry}>Retry</button>
        </div>
      ) : (
        <button
          type="button"
          className="home-cost-body"
          onClick={onOpenCost}
          aria-label="Open the Cost page"
        >
          <span className="home-cost-figure">
            <span className="home-cost-amount">{fmtMoney(c?.today || 0)}</span>
            <span className="home-cost-label">today</span>
          </span>
          {week.length >= 2 && (
            <Sparkline data={week} color="var(--brand-accent)" width={104} height={26} />
          )}
        </button>
      )}
    </PreviewCard>
  )
}

// --- 3. Work feed ----------------------------------------------------------

function WorkFeedCard({ feed, onViewAll, onOpenAgent }) {
  const rows = (feed.data || []).slice(0, 4)
  return (
    <PreviewCard
      title="Work feed"
      action="Open feed →"
      onAction={onViewAll}
      className="home-feed"
    >
      {feed.loading ? (
        <div className="dash-skel">
          <span style={{ width: '70%' }} />
          <span style={{ width: '88%' }} />
        </div>
      ) : feed.failed ? (
        <div className="dash-empty" role="alert">
          Couldn&apos;t load recent activity.{' '}
          <button type="button" className="dash-link" onClick={feed.retry}>Retry</button>
        </div>
      ) : rows.length === 0 ? (
        <div className="dash-empty">Nothing has happened in the last day.</div>
      ) : (
        <ul className="home-feed-list">
          {rows.map((f, i) => (
            <li key={`${f.agent}-${i}`}>
              <button
                type="button"
                className="home-feed-row"
                onClick={() => onOpenAgent && onOpenAgent(f.agent, 'main')}
              >
                <span className="home-feed-summary">{f.summary}</span>
                <span className="home-feed-meta">
                  {f.agent}
                  <span className="dash-dot-sep">·</span>
                  {fmtRel(f.time)}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </PreviewCard>
  )
}

// --- 4. Work ---------------------------------------------------------------

const WORK_TILES = [
  { key: 'moving', label: 'Moving' },
  { key: 'waiting', label: 'Waiting' },
  { key: 'stuck', label: 'Stuck' },
  { key: 'done', label: 'Done' },
]

// A snapshot of the Work page's own buckets. "Waiting" is every wait, on you
// or on someone else — deliberately a different cut from Needs attention,
// which is only the work that has stopped moving.
function WorkCard({ work, onGoWork }) {
  if (work.failed) {
    return (
      <PreviewCard title="Work">
        <WorkLoadFailed lead="Can't load this work" onRetry={work.retry} />
      </PreviewCard>
    )
  }
  const counts = workSplit(work.items, work.overview)
  const pending = work.items === null

  return (
    <PreviewCard
      title="Work"
      action="Open Work →"
      onAction={onGoWork ? () => onGoWork(null) : undefined}
      className="home-work"
    >
      <div className="home-work-tiles">
        {WORK_TILES.map((t) => (
          <button
            key={t.key}
            type="button"
            className={`home-work-tile tone-${t.key}`}
            onClick={() => onGoWork && onGoWork(t.key)}
            disabled={pending}
          >
            <span className="home-work-num">
              {pending ? '—' : counts[t.key]}
              {/* A page-derived count on a truncated list is a floor. Say so
                  rather than printing a total we do not have. */}
              {!pending && work.truncated && t.key !== 'done' ? '+' : ''}
            </span>
            <span className="home-work-lbl">{t.label}</span>
          </button>
        ))}
      </div>
    </PreviewCard>
  )
}

// --- 5. Fleet --------------------------------------------------------------

// Health dots only. Home never loads the agent roster (that request is what
// made Home expensive), so this card can only speak about agents already
// flagged as needing attention — and it says exactly that. The Fleet page
// owns the full picture, and the header goes there.
//
// Absent when no agent needs attention: with no roster we cannot honestly
// claim "all healthy", so we say nothing at all.
function FleetCard({ health, onOpenAgent, onGoFleet }) {
  const rows = (health.data || []).filter((h) => h && h.agent)
  if (health.loading || health.failed || rows.length === 0) return null

  return (
    <PreviewCard
      title="Fleet"
      action="Open Fleet →"
      onAction={onGoFleet}
      className="home-fleet"
    >
      <ul className="home-fleet-list">
        {rows.slice(0, 6).map((h, i) => (
          <li key={`${h.agent}-${i}`}>
            <button
              type="button"
              className="home-fleet-row"
              onClick={() => onOpenAgent && onOpenAgent(h.agent, 'main')}
            >
              <span className={`home-fleet-dot sev-${h.severity || 'info'}`} aria-hidden="true" />
              <span className="home-fleet-name">{h.agent}</span>
            </button>
          </li>
        ))}
      </ul>
      <p className="home-fleet-note">Agents needing a look. Open Fleet for all of them.</p>
    </PreviewCard>
  )
}

// --- briefing, behind a quiet disclosure -----------------------------------

// Kept, but off the first paint: closed by default and fetched only on the
// first open. The lead line is composed from counts (always truthful, always
// available); the Claude narrative is the part that can be missing.
function BriefingDisclosure({ work, refreshKey }) {
  const [open, setOpen] = useState(false)
  const briefing = useLazyBriefing(open, refreshKey)
  const lead = briefingLead(work.overview)
  const asOf = asOfLabel(briefing.data?.generated_at)

  return (
    <div className="home-brief-disclosure">
      <button
        type="button"
        className="home-brief-toggle"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
      >
        <span className="dash-sq"><TrovisMark size={10} /></span>
        <span className="home-brief-toggle-label">Daily briefing</span>
        {open ? <ChevronDownIcon size={13} /> : <ChevronRightIcon size={13} />}
      </button>
      {open && (
        <div className="home-brief-body">
          {lead && <p className="home-brief-lead">{lead}</p>}
          {briefing.loading ? (
            <div className="dash-skel">
              <span style={{ width: '92%' }} />
              <span style={{ width: '74%' }} />
            </div>
          ) : briefing.failed ? (
            <p className="home-brief-narrative is-muted">
              Today&apos;s summary didn&apos;t load.{' '}
              <button type="button" className="dash-link" onClick={briefing.retry}>Retry</button>
            </p>
          ) : briefing.data?.summary ? (
            <p className="home-brief-narrative">{briefing.data.summary}</p>
          ) : null}
          {asOf && <div className="home-brief-foot">{asOf}</div>}
        </div>
      )}
    </div>
  )
}

// --- first run -------------------------------------------------------------

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
        <button type="button" className="btn btn-primary" onClick={() => onGoWork(null)}>
          Go to Work
        </button>
      )}
    </div>
  )
}
