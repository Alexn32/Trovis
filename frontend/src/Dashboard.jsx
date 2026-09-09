import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from './api.js'
import { startAbortable } from './abortable.js'
import JobDetail from './JobDetail.jsx'
import { WorkLoadFailed } from './ui.jsx'
import { workUpdatedLabel } from './board.js'
// Costs always render in dollars (e.g. "$0.68"); shared with Fleet and the
// Cost page so every surface prints the same number the same way.
import { formatCost as fmtMoney } from './utils.js'
import {
  asOfLabel,
  briefingLead,
  dayShape,
  deskItems,
  isFirstRun,
  noticedLines,
  proofCounts,
} from './home.js'
// A row's display label is not its route — see agentRoute.js.
import { agentRoute } from './agentRoute.js'
import { openAsk } from './askOpen.js'
import { TrovisMark, ChevronDownIcon, ChevronRightIcon } from './Icons.jsx'

// ---------------------------------------------------------------------------
// Home — the worker's opening, not a sitemap of the other tabs.
//
// First paint answers three questions, in this order:
//   what is waiting on ME  ·  is anything stuck  ·  is the rest of the day moving
//
//   1. Greeting          — who and when. No stats in the header.
//   2. Shape of the day  — one templated sentence. No counts, no money, no model.
//   3. Your desk         — ONLY work waiting on the signed-in user. Gone when empty.
//   4. Trovis noticed    — stuck first, then agent health. At most three lines.
//   5. Proof strip       — the only place on Home that prints numbers.
//   6. Ask               — the existing pill, given a real front door here.
//
// Deliberately NOT here: the work feed (its page is unchanged, just unlinked
// from Home), a Fleet roster preview, the briefing essay on first paint, and
// any chart or kind-of-work grid.
//
// Numbers rule: the sentence carries none, the strip owns them all, and the
// one dollar figure comes from /dashboard/cost — the same call the Cost page
// makes. Two sources for one number is how two surfaces start disagreeing.
//
// Data: the lean pair (/work/overview + /work/items) plus cost and attention.
// Never /work/board, /work/summary or the agent roster. Each section fetches
// independently, carries an AbortSignal, and fails on its own.
// ---------------------------------------------------------------------------

// One lean page of named work is enough for the desk and the strip; the full
// table lives on Work, which every count links to.
const WORK_PAGE = 50

// Desk rows shown before it defers to Work. Your own waits are rarely many.
const DESK_PREVIEW = 6

export default function Dashboard({
  onOpenAgent,
  onOpenCost,
  onGoWork,
  onConnectAgent,
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

  // The lean work pair feeds the sentence, the desk, the stuck line and the
  // strip, so it is fetched once here rather than four times.
  const work = useWork(refreshKey)
  const cost = useSection((signal) => api.getCost({ signal }), refreshKey)
  // Agent health — the only other thing Trovis noticed is allowed to say.
  const health = useSection((signal) => api.getAttention({ signal }), refreshKey)

  const [openItem, setOpenItem] = useState(null)
  const refresh = useCallback(() => setRefreshKey((k) => k + 1), [])

  // "Is anything connected" comes from the agent list /dashboard/cost already
  // returns — no extra request, and honest in a way "no work yet" is not.
  const agents = Array.isArray(cost.data?.agents) ? cost.data.agents : null
  const firstRun = isFirstRun({ overview: work.overview, items: work.items, agents })

  const desk = deskItems(work.items)
  const counts = proofCounts(work.items)
  // The sentence reads the SAME rows the sections below render, so it can
  // never claim work is waiting on you while the desk sits empty.
  const sentence = dayShape({
    hasRecord: work.items !== null,
    connected: !firstRun,
    deskCount: desk.length,
    stuckCount: counts.stuck,
  })

  function openTarget(target) {
    if (!target) return
    if (target.to === 'work-item') setOpenItem(target.item)
    else if (target.to === 'work') onGoWork && onGoWork(target.filter)
    else if (target.to === 'agent' && onOpenAgent) onOpenAgent(...agentRoute(target.row))
  }

  return (
    <div className="dash home">
      <Greeting userName={userName} />

      {sentence && <p className="home-shape">{sentence}</p>}

      {firstRun ? (
        <FirstRunCta onConnectAgent={onConnectAgent} />
      ) : (
        <>
          <DeskSection
            work={work}
            desk={desk}
            onOpenItem={setOpenItem}
            onGoWork={onGoWork}
            onResolved={refresh}
          />
          <NoticedSection work={work} health={health} onOpen={openTarget} />
          <ProofStrip
            work={work}
            counts={counts}
            cost={cost}
            onGoWork={onGoWork}
            onOpenCost={onOpenCost}
          />
          <AskEntry />
          <BriefingDisclosure work={work} refreshKey={refreshKey} />
        </>
      )}

      {openItem && (
        <JobDetail
          item={openItem}
          onClose={() => setOpenItem(null)}
          onResolved={() => {
            setOpenItem(null)
            refresh()
          }}
        />
      )}
    </div>
  )
}

// --- data hooks ------------------------------------------------------------

/**
 * One independent section fetch. Keeps the last good value on a refetch
 * failure (a blip must not blank a section that was already showing) and
 * reports `failed` only when there is nothing to show.
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
            // floor, not a total. The strip says so rather than lying.
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
 * slowest call Home can make (a Claude call behind a server cap), and it is
 * not what a person opens Home to read — so it stays off the first paint.
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

// --- 1. greeting -----------------------------------------------------------

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

/** Section shell: title, an optional link onward, children. */
function HomeSection({ title, action, onAction, className = '', children }) {
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

// --- 3. your desk ----------------------------------------------------------

/**
 * Only what is waiting on YOU. Not "needs a human", not a teammate's wait,
 * not org-wide stuck work — those belong to their owner or to Trovis noticed.
 *
 * The whole section is absent when the desk is clear. There is no "all clear"
 * card: an empty desk is best said by the sentence at the top and by silence
 * here.
 */
function DeskSection({ work, desk, onOpenItem, onGoWork, onResolved }) {
  if (work.failed) {
    return (
      <HomeSection title="Your desk">
        <WorkLoadFailed lead="Can't load what's waiting on you" onRetry={work.retry} />
      </HomeSection>
    )
  }
  if (work.items === null) return null // loading: stay silent, reserve nothing
  if (desk.length === 0) return null

  const shown = desk.slice(0, DESK_PREVIEW)
  const hidden = desk.length - shown.length

  return (
    <HomeSection
      title="Your desk"
      action="Open in Work →"
      onAction={onGoWork ? () => onGoWork('mine') : undefined}
      className="home-desk"
    >
      <ul className="home-desk-list">
        {shown.map((row) => (
          <DeskRow
            key={row.id}
            row={row}
            onOpen={() => onOpenItem(row)}
            onResolved={onResolved}
          />
        ))}
      </ul>
      {hidden > 0 && onGoWork && (
        <button
          type="button"
          className="dash-link home-card-more"
          onClick={() => onGoWork('mine')}
        >
          {hidden} more in Work →
        </button>
      )}
    </HomeSection>
  )
}

// The three answers a person can give without opening anything:
//
//   Done          — I did it. Ends here.
//   I've got this — mine now, still going. Ends the wait, not the work.
//   Not mine      — pass it back to whoever sent it.
//
// All three are only offered when the server gave us the open decision's id.
// A button with nothing to call is worse than no button.
const DESK_ACTIONS = [
  { key: 'done', label: 'Done', busy: 'Marking done…', className: 'btn btn-primary' },
  { key: 'mine', label: "I've got this", busy: 'Taking it…', className: 'btn btn-secondary' },
  { key: 'not-mine', label: 'Not mine', busy: 'Passing back…', className: 'btn btn-secondary' },
]

function DeskRow({ row, onOpen, onResolved }) {
  const [busy, setBusy] = useState(null)
  const [err, setErr] = useState(null)
  const eventId = row.awaiting_handoff_event_id
  const age = workUpdatedLabel(row.updated_at)

  async function act(kind) {
    if (!eventId || busy) return
    setBusy(kind)
    setErr(null)
    try {
      if (kind === 'done') await api.completeHandoff(row.id, eventId)
      else if (kind === 'mine') await api.acceptHandoff(row.id, eventId)
      // "Not mine" hands it back to whoever passed it over — never to nobody.
      // The server records the refusal on the work's own history, so the agent
      // that sent it can see the answer on its next turn.
      else await api.declineHandoff(row.id, eventId)
      onResolved()
    } catch (e) {
      setErr(e?.message || "That didn't go through")
      setBusy(null)
    }
  }

  return (
    <li className="home-desk-item">
      <button type="button" className="home-desk-row" onClick={onOpen}>
        <span className="home-desk-title">{row.title}</span>
        <span className="home-desk-why">
          Waiting on you
          {age && (
            <>
              <span className="dash-dot-sep">·</span>
              {age}
            </>
          )}
        </span>
      </button>
      {eventId && (
        <div className="home-desk-actions">
          {DESK_ACTIONS.map((a) => (
            <button
              key={a.key}
              type="button"
              className={a.className}
              disabled={!!busy}
              onClick={() => act(a.key)}
            >
              {busy === a.key ? a.busy : a.label}
            </button>
          ))}
        </div>
      )}
      {err && (
        <p className="home-desk-err" role="alert">
          {err}
        </p>
      )}
    </li>
  )
}

// --- 4. Trovis noticed -----------------------------------------------------

// Composed from the record, never by a model on the critical path: the stuck
// line first, then agent health. Absent entirely when there is nothing to say.
function NoticedSection({ work, health, onOpen }) {
  if (work.items === null && health.loading) return null
  const lines = noticedLines({
    items: work.items,
    attention: health.data,
  })
  // Anything stuck always produces a line, so an empty list genuinely means
  // there is nothing to notice — no card, no "all clear".
  if (lines.length === 0) return null

  return (
    <HomeSection title="Trovis noticed" className="home-noticed">
      <ul className="home-noticed-list">
        {lines.map((line) => (
          <li key={line.key}>
            <button
              type="button"
              className={`home-noticed-row tone-${line.kind}`}
              onClick={() => onOpen(line.target)}
            >
              <span className="home-noticed-dot" aria-hidden="true" />
              <span className="home-noticed-text">{line.text}</span>
            </button>
          </li>
        ))}
      </ul>
    </HomeSection>
  )
}

// --- 5. proof strip --------------------------------------------------------

const STRIP_CELLS = [
  { key: 'moving', label: 'moving', filter: 'moving' },
  { key: 'waiting', label: 'waiting', filter: 'waiting' },
  { key: 'stuck', label: 'stuck', filter: 'stuck' },
  { key: 'done', label: 'done today', filter: 'done' },
]

/**
 * The only place on Home that prints numbers.
 *
 * "waiting" here is EVERYONE's waits — deliberately wider than the desk above,
 * which is only yours. The two are different questions, so they get different
 * words and never share a figure.
 *
 * The dollar comes from /dashboard/cost, which is what the Cost page reads.
 */
function ProofStrip({ work, counts, cost, onGoWork, onOpenCost }) {
  if (work.failed && cost.failed) return null
  const pending = work.items === null
  // A count off a truncated page is a floor. Say so rather than print a total
  // we do not have. Done is a same-day slice of the same page, so it floors too.
  const more = work.truncated ? '+' : ''

  return (
    <section className="home-strip" aria-label="Today by the numbers">
      {STRIP_CELLS.map((c) => (
        <StripCell
          key={c.key}
          label={c.label}
          tone={c.key}
          value={`${counts[c.key]}${more}`}
          loading={pending && !work.failed}
          failed={work.failed}
          onRetry={work.retry}
          onOpen={() => onGoWork && onGoWork(c.filter)}
        />
      ))}
      <StripCell
        label="today"
        tone="cost"
        value={fmtMoney(cost.data?.today || 0)}
        loading={cost.loading}
        failed={cost.failed}
        onRetry={cost.retry}
        onOpen={onOpenCost}
      />
    </section>
  )
}

// One cell. A failed cell is never a dead end: it keeps its place in the row,
// shows a dash instead of inventing a number, and the tap becomes Retry.
function StripCell({ label, tone, value, loading, failed, onRetry, onOpen }) {
  return (
    <button
      type="button"
      className={`home-strip-cell tone-${tone}${failed ? ' is-failed' : ''}`}
      onClick={failed ? onRetry : onOpen}
      disabled={loading}
      aria-label={failed ? `Couldn't load ${label} — retry` : undefined}
    >
      <span className="home-strip-num">{loading || failed ? '—' : value}</span>
      <span className="home-strip-lbl">{failed ? 'retry' : label}</span>
    </button>
  )
}

// --- 6. Ask ----------------------------------------------------------------

// The pill is always there on ⌘K, but a pill you have to know about is not an
// invitation. This is the same control with a front door: it opens the real
// Ask panel, and typing here sends the question straight in.
function AskEntry() {
  const [q, setQ] = useState('')

  function submit(e) {
    e.preventDefault()
    const text = q.trim()
    setQ('')
    openAsk(text)
  }

  return (
    <form className="home-ask" onSubmit={submit}>
      <span className="dash-sq" aria-hidden="true">
        <TrovisMark size={11} />
      </span>
      <input
        className="home-ask-input"
        value={q}
        onChange={(e) => setQ(e.target.value)}
        placeholder="Ask Trovis about today…"
        aria-label="Ask Trovis about today"
      />
      <span className="home-ask-key" aria-hidden="true">⌘K</span>
    </form>
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

// The sentence above already said nothing is connected. This is the one thing
// to do about it — no tiles, and no strip of zeros pretending to be a product.
function FirstRunCta({ onConnectAgent }) {
  return (
    <div className="dash-card home-firstrun">
      <p className="home-firstrun-sub">
        Connect an agent and its work shows up here on its own — what needs you,
        what&apos;s stuck, and what moved. You will not have to enter any of it.
      </p>
      {onConnectAgent && (
        <button type="button" className="btn btn-primary" onClick={onConnectAgent}>
          Connect an agent
        </button>
      )}
    </div>
  )
}
