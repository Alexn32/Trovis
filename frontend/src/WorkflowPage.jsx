import { useCallback, useEffect, useState } from 'react'
import { api } from './api.js'
import { ArrowLeftIcon } from './Icons.jsx'
import StoryView from './StoryView.jsx'
import {
  ATTENTION_STATES,
  TERMINAL_STATES,
  WORKFLOW_STRINGS as WS,
  boardTitle,
  chainGlyph,
  doneTodayLabel,
  fmtAge,
  fmtDurationShort,
  heatLabel,
  inMotionLine,
  isDoneToday,
  loopAgeSeconds,
  loopDurationSeconds,
  moreItemsLabel,
  showMarkDone,
  sortLoopsAttentionFirst,
  stationHeat,
  stationName,
  workflowHeaderLine,
} from './loops.js'

// The workflow page, level 2: the drawing IS the page. The declared path
// renders as generous station boxes left to right; the only live-data
// element on the drawing is heat — a warm fill + "{n} here · {age}" on
// stations currently holding work. Under the drawing, the work in it,
// simply: Needs you, In motion, Done today. Every item expands the loop's
// story inline (the app's list-detail pattern).

const REFRESH_MS = 30000
const DONE_SHOWN = 5

function Station({ station, heat }) {
  return (
    <div className={`wfd-station ${heat ? 'is-warm' : ''}`}>
      <div className="wfd-station-who">
        {station.holder_type === 'agent' && (
          <span className="wfd-agent-dot" aria-hidden="true" />
        )}
        {stationName(station)}
      </div>
      {station.label && <div className="wfd-station-what">{station.label}</div>}
      {heat && <div className="wfd-station-heat">{heatLabel(heat)}</div>}
    </div>
  )
}

function Arrow({ carrier }) {
  return (
    <span className="wfd-arrow-cell">
      {carrier && <span className="wfd-carrier">{carrier}</span>}
      <span className="wfd-arrow" aria-hidden="true">→</span>
    </span>
  )
}

// One work item under the drawing. Clicking anywhere on the row expands
// the loop's story inline; `meta` is the muted right-hand phrase.
function WorkItem({ loop, meta, warm, sessionUser, onOpenAgent, onChanged, action }) {
  const [open, setOpen] = useState(false)
  return (
    <div className={`wfd-item-wrap ${warm ? 'is-warm' : ''}`}>
      <div className="wfd-item-row">
        <button
          type="button"
          className="wfd-item"
          onClick={() => setOpen(!open)}
          aria-expanded={open}
        >
          <span className="wfd-item-title">{boardTitle(loop)}</span>
          {meta && <span className="wfd-item-meta">{meta}</span>}
        </button>
        {action}
      </div>
      {open && (
        <StoryView
          loop={loop}
          sessionUser={sessionUser}
          onOpenAgent={onOpenAgent}
          onChanged={onChanged}
        />
      )}
    </div>
  )
}

export default function WorkflowPage({ workflowId, onBack, onEdit, onOpenAgent, sessionUser }) {
  const [wf, setWf] = useState(null)
  const [map, setMap] = useState(null)
  const [loops, setLoops] = useState(null)
  const [error, setError] = useState(null)
  const [historyOpen, setHistoryOpen] = useState(false)
  const [doneExpanded, setDoneExpanded] = useState(false)
  const [closingId, setClosingId] = useState(null)

  const load = useCallback(async () => {
    try {
      const [w, m, ls] = await Promise.all([
        api.getWorkflow(workflowId),
        api.getWorkflowMap(workflowId),
        api.getWorkflowLoops(workflowId),
      ])
      setWf(w)
      setMap(m)
      setLoops(Array.isArray(ls) ? ls : [])
      setError(null)
    } catch (e) {
      setError(e?.message || 'Could not load this workflow')
    }
  }, [workflowId])

  useEffect(() => {
    load()
    const id = setInterval(load, REFRESH_MS)
    return () => clearInterval(id)
  }, [load])

  async function markDone(loop) {
    if (closingId) return
    setClosingId(loop.id)
    try {
      await api.closeLoop(loop.id)
      await load()
    } catch (e) {
      setError(e?.message || 'Could not mark this loop done')
    } finally {
      setClosingId(null)
    }
  }

  if (error && !wf) {
    return (
      <div className="dash">
        <button type="button" className="wf2-back" onClick={onBack}>
          <ArrowLeftIcon size={15} /> Work
        </button>
        <div className="dash-card"><div className="dash-empty pad">{error}</div></div>
      </div>
    )
  }
  if (!wf) {
    return (
      <div className="dash">
        <div className="dash-skel"><span style={{ height: 120 }} /><span style={{ height: 220 }} /></div>
      </div>
    )
  }

  const stations = map?.stations || []
  const heat = stationHeat(map?.loops || [])
  // Station position per loop id, for the In motion lines.
  const positionById = new Map()
  for (const l of map?.loops || []) {
    if (l?.position?.status === 'on_path' && l.position.station_index != null) {
      positionById.set(l.id, stations[l.position.station_index])
    }
  }

  const all = loops || []
  const needsYou = sortLoopsAttentionFirst(
    all.filter((l) => ATTENTION_STATES.includes(l.cached_state)),
  )
  const inMotion = all.filter(
    (l) =>
      !ATTENTION_STATES.includes(l.cached_state) &&
      !TERMINAL_STATES.includes(l.cached_state),
  )
  const doneToday = all.filter((l) => isDoneToday(l))
  const doneShown = doneExpanded ? doneToday : doneToday.slice(0, DONE_SHOWN)

  return (
    <div className="dash wfpage">
      <button type="button" className="wf2-back" onClick={onBack}>
        <ArrowLeftIcon size={15} /> Work
      </button>

      <div className="wfp-titlerow">
        <span className="wfpage-head">
          <h1 className="wfd-title">{wf.name}</h1>
          <button
            type="button"
            className="wfe-vchip is-btn"
            onClick={() => setHistoryOpen(!historyOpen)}
            aria-expanded={historyOpen}
          >
            v{wf.current_version} · {WS.historyLabel}
          </button>
        </span>
        {sessionUser && (
          <button type="button" className="btn btn-secondary btn-sm" onClick={() => onEdit(wf)}>
            {WS.editStations}
          </button>
        )}
      </div>
      <div className="wfd-summary">{workflowHeaderLine(wf)}</div>

      {historyOpen && (
        <div className="dash-card wfpage-history">
          {(wf.versions || []).map((v) => (
            <div className="wfpage-history-row" key={v.version}>
              <span className="wfe-vchip">v{v.version}</span>
              <span className="wfpage-history-date">
                {v.created_at ? new Date(v.created_at.replace(' ', 'T')).toLocaleDateString() : ''}
              </span>
              {v.created_by && <span className="wfpage-history-by">by {v.created_by}</span>}
              {v.note && <span className="wfpage-history-note">{v.note}</span>}
            </div>
          ))}
        </div>
      )}

      <div className="wfd-drawing">
        {stations.length === 0 ? (
          <div className="dash-empty pad">
            {WS.mapEmpty}
            {sessionUser && (
              <>
                {' '}
                <button type="button" className="btn-link-inline" onClick={() => onEdit(wf)}>
                  {WS.editStations} →
                </button>
              </>
            )}
          </div>
        ) : (
          <div className="wfd-path">
            {stations.map((s, i) => (
              <span className="wfd-cell" key={i}>
                {i > 0 && <Arrow carrier={stations[i - 1].carrier} />}
                <Station station={s} heat={heat.get(i)} />
              </span>
            ))}
          </div>
        )}
      </div>

      {needsYou.length > 0 && (
        <section className="dash-section">
          <div className="dash-section-head">
            <span className="wfd-section-title is-warm">{WS.needsYou}</span>
          </div>
          <div className="dash-card">
            {needsYou.map((l) => (
              <WorkItem
                key={l.id}
                loop={l}
                warm
                meta={
                  loopAgeSeconds(l) != null
                    ? `${WS.waitingWord} ${fmtAge(loopAgeSeconds(l))}`
                    : null
                }
                sessionUser={sessionUser}
                onOpenAgent={onOpenAgent}
                onChanged={load}
                action={
                  showMarkDone(l, sessionUser) ? (
                    <button
                      type="button"
                      className="btn btn-secondary btn-sm"
                      onClick={() => markDone(l)}
                      disabled={closingId === l.id}
                    >
                      {closingId === l.id ? 'Marking…' : 'Mark done'}
                    </button>
                  ) : null
                }
              />
            ))}
          </div>
        </section>
      )}

      {inMotion.length > 0 && (
        <section className="dash-section">
          <div className="dash-section-head">
            <span className="wfd-section-title">{WS.inMotion}</span>
          </div>
          <div className="dash-card">
            {inMotion.map((l) => (
              <WorkItem
                key={l.id}
                loop={l}
                meta={inMotionLine(l, positionById.get(l.id))}
                sessionUser={sessionUser}
                onOpenAgent={onOpenAgent}
                onChanged={load}
              />
            ))}
          </div>
        </section>
      )}

      <section className="dash-section">
        <div className="dash-section-head">
          <span className="wfd-section-title is-muted">{doneTodayLabel(doneToday.length)}</span>
        </div>
        {doneToday.length > 0 && (
          <div className="dash-card">
            {doneShown.map((l) => {
              const dur = fmtDurationShort(loopDurationSeconds(l))
              const glyph = chainGlyph(l)
              const bits = [glyph, dur]
                .filter(Boolean)
                .join(' · ')
              return (
                <WorkItem
                  key={l.id}
                  loop={l}
                  meta={bits || null}
                  sessionUser={sessionUser}
                  onOpenAgent={onOpenAgent}
                  onChanged={load}
                />
              )
            })}
            {!doneExpanded && doneToday.length > DONE_SHOWN && (
              <button
                type="button"
                className="wfd-more"
                onClick={() => setDoneExpanded(true)}
              >
                {moreItemsLabel(doneToday.length - DONE_SHOWN)}
              </button>
            )}
          </div>
        )}
      </section>
    </div>
  )
}
