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
  askChips,
  briefingLead,
  deskEmptyCopy,
  deskItems,
  fleetPulse,
  isFirstRun,
  noticedLines,
  proofCounts,
  chooseGraphic,
  pulseGraphic,
  pulsePacket,
} from './home.js'
// A row's display label is not its route — see agentRoute.js.
import { agentRoute } from './agentRoute.js'
import { openAsk } from './askOpen.js'
import { TrovisMark, ChevronDownIcon, ChevronRightIcon } from './Icons.jsx'

// ---------------------------------------------------------------------------
// Home — the worker's opening, across the full width of the screen.
//
// First paint answers three questions, in this order:
//   what is waiting on ME  ·  is anything stuck  ·  is the rest of the day moving
//
//   1. Greeting      — who and when. No stats in the header.
//   2. Fleet pulse   — full width. How many agents, and which need a look.
//   3. Two columns   — your desk (left) · Trovis noticed (right)
//   4. Proof strip   — full width. The only place on Home that prints numbers.
//   5. Ask           — one field, plus chips built from what is on the page.
//   6. Briefing      — open, leading with a templated line.
//
// Deliberately NOT here: the work feed, a Fleet roster, any chart or
// kind-of-work grid.
//
// THE NUMBERS RULE. The strip owns every count. No other block states one in
// prose — not the desk box, not the briefing lead. This is not fussiness: the
// old shape-of-the-day line inferred "Work is moving" from "nothing is stuck"
// and printed it above a strip reading 0 moving. Prose that reasons about
// counts it does not print is how a page contradicts itself.
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

// Agents named in the pulse before it defers to Fleet.
const PULSE_PREVIEW = 5

export default function Dashboard({
  onOpenAgent,
  onOpenCost,
  onGoWork,
  onGoFleet,
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

  // The lean work pair feeds the desk, the stuck line, the strip and the
  // briefing lead, so it is fetched once here rather than four times.
  const work = useWork(refreshKey)
  const cost = useSection((signal) => api.getCost({ signal }), refreshKey)
  // Agent health — the fleet pulse and Trovis noticed both read this, and it
  // is the only agent data Home is allowed to load.
  const health = useSection((signal) => api.getAttention({ signal }), refreshKey)

  const [openItem, setOpenItem] = useState(null)
  const refresh = useCallback(() => setRefreshKey((k) => k + 1), [])

  // "Is anything connected" comes from /dashboard/cost, which Home fetches for
  // the strip anyway — no extra request, and honest in a way "no work yet" is
  // not. `agent_count` is the real total; `agents` is a truncated top-spender
  // list and must never be counted.
  const agentCount = Number.isFinite(cost.data?.agent_count) ? cost.data.agent_count : null
  const firstRun = isFirstRun({
    overview: work.overview,
    items: work.items,
    agentCount,
  })

  const desk = deskItems(work.items)
  const counts = proofCounts(work.items)
  const pulse = fleetPulse({ agentCount, attention: health.data })
  // Everything the insight is allowed to reason over, built from what is
  // already on the page. Memoised on its own contents so a re-render does not
  // look like a new packet and re-ask.
  const packet = usePacket({
    overview: work.overview,
    items: work.items,
    truncated: work.truncated,
    cost: cost.data,
    attention: health.data,
    agentCount,
  })
  const insight = usePulseInsight(packet)
  // Chosen from the packet in this browser, so the chart is on screen with the
  // rest of the pulse. The model's pick only overrides it once it arrives, and
  // only if it names a series we can draw.
  const graphic = pulseGraphic(chooseGraphic(packet, insight.graphic), packet)
  const chips = askChips({
    desk,
    counts,
    attention: health.data,
    costToday: cost.data?.today,
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

      {firstRun ? (
        // Nothing connected: the desk box carries the whole message and the
        // one thing to do. No fleet pulse (there is no fleet), and no strip —
        // five theatrical zeros and $0.00 is a product pretending to have data.
        <FirstRun work={work} onConnectAgent={onConnectAgent} />
      ) : (
        <>
          <FleetPulse
            pulse={pulse}
            health={health}
            insight={insight.insight}
            graphic={graphic}
            onOpenAgent={onOpenAgent}
            onGoFleet={onGoFleet}
            onGoWork={onGoWork}
          />

          <div className="home-cols">
            <DeskSection
              work={work}
              desk={desk}
              connected
              onOpenItem={setOpenItem}
              onGoWork={onGoWork}
              onResolved={refresh}
            />
            <NoticedSection work={work} health={health} onOpen={openTarget} />
          </div>

          <ProofStrip
            work={work}
            counts={counts}
            cost={cost}
            onGoWork={onGoWork}
            onOpenCost={onOpenCost}
          />
          <AskSection chips={chips} />
          <Briefing work={work} refreshKey={refreshKey} />
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
 * The Claude narrative, fetched ONLY once someone asks for it.
 *
 * The briefing section is open by default, but its body leads with the
 * templated line — which is instant and cannot contradict the strip. The
 * generated prose stays behind "More" for two reasons: it is the slowest call
 * Home can make, and it is the one thing on the page whose wording we do not
 * control, so it must not be able to state a number the strip disagrees with
 * on first paint.
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

/**
 * The DATA packet, rebuilt only when its CONTENTS change.
 *
 * Home re-renders on every keystroke in the Ask field; a fresh object each
 * time would look like a fresh packet and re-ask the model on every one of
 * them. Keying on the serialised packet makes the identity follow the facts.
 */
function usePacket(inputs) {
  const built = pulsePacket(inputs)
  const key = JSON.stringify(built)
  const ref = useRef({ key, value: built })
  if (ref.current.key !== key) ref.current = { key, value: built }
  return ref.current.value
}

/**
 * The generated sentence — strictly after first paint, and never required.
 *
 * The pulse renders its workforce, graphic and caption from the record
 * immediately; this fills in one line if and when a valid one arrives. A
 * timeout, an outage, a model that says something the packet does not entail
 * — all land the same way: `insight` stays empty and the pulse is unchanged.
 * `graphic` always comes back usable because the server falls back to the
 * deterministic chooser.
 */
function usePulseInsight(packet) {
  const [state, setState] = useState({ insight: '', graphic: 'none' })
  const key = JSON.stringify(packet)

  useEffect(() => {
    // Nothing proven yet — do not ask about an empty packet.
    if (!packet || Object.keys(packet).length === 0) return undefined
    return startAbortable(({ signal, isAlive }) => {
      api
        .getPulseInsight(packet, { signal })
        .then((d) => {
          if (!isAlive()) return
          setState({
            insight: String(d?.insight || ''),
            graphic: String(d?.graphic || 'none'),
          })
        })
        // No insight is a normal outcome, so a failure is silent: the pulse
        // was already complete without it.
        .catch(() => {})
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key])

  return state
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

// --- 2. fleet pulse --------------------------------------------------------

/**
 * The highest-level thing Home says about the agents, and the only thing it
 * can say without loading the roster: how many are reporting, and which ones
 * are already flagged.
 *
 * It never claims "all healthy". Home does not see the roster, so an agent
 * not being flagged means nobody looked — not that it is fine. When nothing
 * is flagged the row states the count and stops.
 */
function FleetPulse({ pulse, health, insight, graphic, onOpenAgent, onGoFleet, onGoWork }) {
  const { count, needLook } = pulse
  // Nothing truthful to say yet.
  if (count === null && needLook.length === 0 && !graphic) return null
  const shown = needLook.slice(0, PULSE_PREVIEW)

  return (
    <section className="home-pulse" aria-label="Fleet">
      <div className="home-pulse-workforce">
        <div className="home-pulse-row">
          {count !== null && (
            <button
              type="button"
              className="home-pulse-count"
              onClick={onGoFleet}
              disabled={!onGoFleet}
            >
              <span className="home-pulse-num">{count}</span>
              <span className="home-pulse-lbl">{count === 1 ? 'agent' : 'agents'}</span>
            </button>
          )}

          {needLook.length > 0 ? (
          <div className="home-pulse-look">
            <button
              type="button"
              className="home-pulse-need"
              onClick={onGoFleet}
              disabled={!onGoFleet}
            >
              {needLook.length} need{needLook.length === 1 ? 's' : ''} a look
            </button>
            <ul className="home-pulse-list">
              {shown.map((a, i) => (
                <li key={`${a.service_name || a.agent}-${a.agent_id || 'main'}-${i}`}>
                  <button
                    type="button"
                    className={`home-pulse-agent sev-${a.severity || 'info'}`}
                    onClick={() => onOpenAgent && onOpenAgent(...agentRoute(a))}
                  >
                    <span className="home-pulse-dot" aria-hidden="true" />
                    {a.agent}
                  </button>
                </li>
              ))}
              {needLook.length > shown.length && (
                <li>
                  <button type="button" className="dash-link" onClick={onGoFleet}>
                    {needLook.length - shown.length} more →
                  </button>
                </li>
              )}
            </ul>
          </div>
          ) : (
            // Not "all healthy" — we only know that nothing was flagged.
            !health.loading && !health.failed && count !== null && (
              <span className="home-pulse-quiet">Nothing flagged right now.</span>
            )
          )}
        </div>

        {/* The generated line, when there is a valid one. It arrives after
            first paint and its absence is invisible — everything above and
            beside it came from the record. */}
        {insight && <p className="home-pulse-insight">{insight}</p>}
      </div>

      {graphic && <PulseGraphic graphic={graphic} onGoWork={onGoWork} />}
    </section>
  )
}

/**
 * Two bars and a caption, drawn in code from the packet — never by the model.
 *
 * The caption quotes the raw numbers, so the picture and the words come from
 * one source and cannot drift. No pie, no gauge, no health score: this proves
 * one comparison and says which one it is.
 */
function PulseGraphic({ graphic, onGoWork }) {
  const max = Math.max(1, ...graphic.bars.map((b) => Number(b.value) || 0))
  return (
    <button
      type="button"
      className="home-pulse-graphic"
      onClick={() => onGoWork && onGoWork(graphic.filter)}
      disabled={!onGoWork}
      aria-label={graphic.caption}
    >
      <span className="home-pulse-bars" aria-hidden="true">
        {graphic.bars.map((b) => (
          <span key={b.label} className="home-pulse-bar-wrap">
            <span
              className={`home-pulse-bar${b.label === 'this week' ? ' is-now' : ''}`}
              style={{ height: `${Math.round(((Number(b.value) || 0) / max) * 100)}%` }}
            />
          </span>
        ))}
      </span>
      <span className="home-pulse-caption">{graphic.caption}</span>
    </button>
  )
}

// --- 3. your desk ----------------------------------------------------------

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

/**
 * Only what is waiting on YOU. Not "needs a human", not a teammate's wait,
 * not org-wide stuck work — those belong to their owner or to Trovis noticed.
 *
 * Unlike every other block, this one stays on the page when it is empty: a
 * clear desk is the answer to the question Home exists to ask, so it gets a
 * designed empty state rather than silence.
 */
function DeskSection({ work, desk, connected, onOpenItem, onGoWork, onResolved, onConnectAgent }) {
  if (work.failed) {
    return (
      <HomeSection title="Your desk" className="home-desk">
        <WorkLoadFailed lead="Can't load what's waiting on you" onRetry={work.retry} />
      </HomeSection>
    )
  }
  if (connected && work.items === null) {
    return (
      <HomeSection title="Your desk" className="home-desk">
        <div className="dash-skel">
          <span style={{ width: '70%' }} />
          <span style={{ width: '52%' }} />
        </div>
      </HomeSection>
    )
  }

  if (desk.length === 0) {
    // One fact, and nothing inferred about the rest of the day. The strip
    // below says what is moving; this box does not guess at it.
    const { lead, sub } = deskEmptyCopy({ connected })
    return (
      <HomeSection title="Your desk" className="home-desk is-clear">
        <p className="home-desk-clear">{lead}</p>
        <p className="home-desk-clear-sub">{sub}</p>
        {!connected && onConnectAgent && (
          <button type="button" className="btn btn-primary" onClick={onConnectAgent}>
            Connect an agent
          </button>
        )}
      </HomeSection>
    )
  }

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
// line first, then agent health. Every row carries the one thing to do about
// it — a notice you cannot act on is just worry.
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
          <li key={line.key} className={`home-noticed-row tone-${line.kind}`}>
            <span className="home-noticed-dot" aria-hidden="true" />
            <span className="home-noticed-text">{line.text}</span>
            <button
              type="button"
              className="btn btn-secondary home-noticed-act"
              onClick={() => onOpen(line.target)}
            >
              {line.action}
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

/**
 * The one Ask affordance on Home. The floating pill is suppressed while this
 * pane is on screen (see App.jsx) so there is a single target, and both open
 * the same panel — this is a front door to the existing Ask, not a second
 * chat.
 *
 * The chips are built from what is actually on the page right now, so a chip
 * never asks about an empty set.
 */
function AskSection({ chips }) {
  const [q, setQ] = useState('')

  function submit(e) {
    e.preventDefault()
    const text = q.trim()
    setQ('')
    openAsk(text)
  }

  return (
    <section className="home-ask-block" aria-label="Ask Trovis">
      <form className="home-ask" onSubmit={submit}>
        <span className="dash-sq" aria-hidden="true">
          <TrovisMark size={11} />
        </span>
        <input
          className="home-ask-input"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Ask Trovis about the work…"
          aria-label="Ask Trovis about the work"
        />
        <span className="home-ask-key" aria-hidden="true">⌘K</span>
      </form>
      {chips.length > 0 && (
        <div className="home-ask-chips">
          {chips.map((c) => (
            <button
              key={c.key}
              type="button"
              className="dash-suggest-pill"
              onClick={() => openAsk(c.query)}
            >
              {c.label}
            </button>
          ))}
        </div>
      )}
    </section>
  )
}

// --- 7. daily briefing -----------------------------------------------------

/**
 * Open on first paint, leading with the templated line.
 *
 * The lead is composed from counts and states no figures — always available,
 * always true, and it cannot disagree with the strip. The Claude narrative is
 * the one piece of text on Home whose wording we do not control, so it sits
 * behind "More" and is fetched only when asked for: that keeps the slowest
 * call Home can make off the first paint AND guarantees nothing on the opening
 * screen can contradict the numbers underneath it.
 */
function Briefing({ work, refreshKey }) {
  const [showMore, setShowMore] = useState(false)
  const briefing = useLazyBriefing(showMore, refreshKey)
  const lead = briefingLead(work.overview)
  const asOf = asOfLabel(briefing.data?.generated_at)

  if (!lead && work.overview === null) return null

  return (
    <section className="home-brief" aria-label="Daily briefing">
      <div className="home-card-head">
        <span className="dash-section-title">
          <span className="dash-sq"><TrovisMark size={10} /></span>
          Daily briefing
        </span>
      </div>
      <p className="home-brief-lead">{lead}</p>

      <button
        type="button"
        className="home-brief-more"
        onClick={() => setShowMore((o) => !o)}
        aria-expanded={showMore}
      >
        {showMore ? <ChevronDownIcon size={13} /> : <ChevronRightIcon size={13} />}
        <span>{showMore ? 'Less' : 'More'}</span>
      </button>

      {showMore && (
        <div className="home-brief-body">
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
    </section>
  )
}

// --- first run -------------------------------------------------------------

// Nothing is connected. The desk box says so and offers the one action; every
// other block would be inventing data it does not have.
function FirstRun({ work, onConnectAgent }) {
  return (
    <DeskSection work={work} desk={[]} connected={false} onConnectAgent={onConnectAgent} />
  )
}
