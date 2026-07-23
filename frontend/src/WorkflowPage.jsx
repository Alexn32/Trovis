import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from './api.js'
import { ArrowLeftIcon } from './Icons.jsx'
import { BoardRow } from './LoopBoard.jsx'
import {
  WORKFLOW_STRINGS as WS,
  doneTodayLabel,
  mapDotLabel,
  mapDots,
  moreDotsLabel,
  sortLoopsAttentionFirst,
  waitingStations,
  workflowHeaderLine,
} from './loops.js'

// The workflow page: the declared process (stations left to right) with
// live work positioned under its current station, and the loops list
// below. Off-path loops carry no dot — they appear only as ordinary rows
// in the list (that's the approved cut: no off-path lane, no conformance
// UI). Dots and track are divs; refresh on the board's cadence.

const REFRESH_MS = 30000
const DOT_CAP = 3

function StationBox({ station, warm }) {
  return (
    <div className={`wfmap-station ${warm ? 'is-warm' : ''}`}>
      <div className="wfmap-station-name">
        {station.holder || (station.holder_type === 'human' ? 'a human' : station.holder_type)}
      </div>
      {station.label && <div className="wfmap-station-label">{station.label}</div>}
      {station.tools?.length > 0 && (
        <div className="wfmap-station-tools">
          {station.tools.map((t, i) => (
            <span key={i} className="story-chip">{t}</span>
          ))}
        </div>
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
  const listRef = useRef(null)

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
  const mapLoops = map?.loops || []
  const dots = mapDots(mapLoops, DOT_CAP)
  const warm = waitingStations(mapLoops)
  const ordered = sortLoopsAttentionFirst(loops || [])

  return (
    <div className="dash wfpage">
      <button type="button" className="wf2-back" onClick={onBack}>
        <ArrowLeftIcon size={15} /> Work
      </button>

      <div className="wfp-titlerow">
        <span className="wfpage-head">
          <span className="board-group-tick" aria-hidden="true" />
          <h1 className="dash-hello" style={{ margin: 0 }}>{wf.name}</h1>
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
      <div className="story-sub">{workflowHeaderLine(wf)}</div>

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

      <div className="dash-card wfmap-card">
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
          <div className="wfmap-scroll">
            <div className="wfmap-stations">
              {stations.map((s, i) => (
                <span className="wfmap-cell" key={i}>
                  {i > 0 && <span className="wfmap-arrow" aria-hidden="true">→</span>}
                  <StationBox station={s} warm={warm.has(i)} />
                </span>
              ))}
            </div>
            <div className="wfmap-track">
              <span className="wfmap-line" aria-hidden="true" />
              <div className="wfmap-slots">
                {stations.map((s, i) => {
                  const stack = dots.get(i)
                  return (
                    <div className="wfmap-slot" key={i}>
                      {stack &&
                        stack.dots.map((l) => {
                          const waiting = l.cached_state !== 'working' && l.cached_state !== 'open'
                          // Title ellipsizes; the waiting age must never
                          // truncate away — it's the label's whole point.
                          const title = l.title || `${l.service_name}`
                          return (
                            <div key={l.id} className={`wfmap-dotrow ${waiting ? 'is-wait' : ''}`}>
                              <span className={`wfmap-dot ${waiting ? 'is-wait' : 'is-work'}`} />
                              <span className="wfmap-dotlabel" title={mapDotLabel(l)}>{title}</span>
                              {waiting && (
                                <span className="wfmap-dotage">{mapDotLabel(l).split(' · ').pop()}</span>
                              )}
                            </div>
                          )
                        })}
                      {stack && stack.overflow > 0 && (
                        <button
                          type="button"
                          className="wfmap-more"
                          onClick={() => listRef.current?.scrollIntoView({ behavior: 'smooth' })}
                        >
                          {moreDotsLabel(stack.overflow)}
                        </button>
                      )}
                    </div>
                  )
                })}
              </div>
              <div className="wfmap-done">
                <span className="wfmap-dot is-done" aria-hidden="true" />
                {doneTodayLabel(map?.done_today ?? 0)}
              </div>
            </div>
          </div>
        )}
      </div>

      <section className="dash-section" ref={listRef}>
        <div className="dash-section-head">
          <span className="dash-section-title">{WS.loopList}</span>
        </div>
        <div className="dash-card">
          {ordered.length === 0 ? (
            <div className="dash-empty pad">No loops yet.</div>
          ) : (
            ordered.map((l) => (
              <BoardRow
                key={l.id}
                loop={l}
                sessionUser={sessionUser}
                onOpenAgent={onOpenAgent}
                onChanged={load}
              />
            ))
          )}
        </div>
      </section>
    </div>
  )
}
