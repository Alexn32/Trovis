import { useCallback, useEffect, useState } from 'react'
import { api } from './api.js'
import { ArrowLeftIcon } from './Icons.jsx'

// Dedicated Work Feed page (overlay opened from the dashboard's "View all →").
// Two layers: the per-agent AI rollup up top (what each agent has been doing —
// reuses /dashboard/work-feed), then the chronological, fleet-wide activity
// stream below (every real work event from /dashboard/activity, newest first,
// with captured message/response/tool content when present). Mirrors CostPage.

const REFRESH_MS = 30000

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

// "Today" / "Yesterday" / weekday-date divider for an ISO timestamp.
function dayLabel(iso) {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return 'Earlier'
  const startOf = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime()
  const days = Math.round((startOf(new Date()) - startOf(d)) / 86_400_000)
  if (days <= 0) return 'Today'
  if (days === 1) return 'Yesterday'
  if (days < 7) return d.toLocaleDateString(undefined, { weekday: 'long' })
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

const TYPE_LABEL = { message: 'message', response: 'response', tool_result: 'tool result' }

export default function WorkFeedPage({ onBack, onOpenAgent }) {
  const [summaries, setSummaries] = useState(null) // per-agent AI rollup
  const [activity, setActivity] = useState(null) // chronological events
  const [error, setError] = useState(null)

  const load = useCallback(async () => {
    try {
      const [acts, sums] = await Promise.all([
        api.getActivity(),
        api.getWorkFeed().catch(() => []), // rollup is best-effort (needs Claude)
      ])
      setActivity(Array.isArray(acts) ? acts : [])
      setSummaries(Array.isArray(sums) ? sums : [])
      setError(null)
    } catch (e) {
      setError(e?.message || 'Could not load the work feed')
      setActivity((a) => a ?? [])
      setSummaries((s) => s ?? [])
    }
  }, [])

  // Initial load + light auto-refresh while the page stays open.
  useEffect(() => {
    load()
    const id = setInterval(load, REFRESH_MS)
    return () => clearInterval(id)
  }, [load])

  if (error && activity === null) {
    return (
      <div className="dash wfp">
        <button type="button" className="wf2-back" onClick={onBack}>
          <ArrowLeftIcon size={15} /> Dashboard
        </button>
        <div className="dash-empty pad">{error}</div>
      </div>
    )
  }
  if (activity === null) {
    return (
      <div className="dash wfp">
        <button type="button" className="wf2-back" onClick={onBack}>
          <ArrowLeftIcon size={15} /> Dashboard
        </button>
        <div className="dash-skel">
          <span style={{ height: 60 }} />
          <span style={{ height: 220 }} />
        </div>
      </div>
    )
  }

  const hasSummaries = summaries && summaries.length > 0
  const empty = activity.length === 0 && !hasSummaries

  return (
    <div className="dash wfp">
      <button type="button" className="wf2-back" onClick={onBack}>
        <ArrowLeftIcon size={15} /> Dashboard
      </button>
      <div className="wfp-titlerow">
        <h1 className="dash-hello" style={{ margin: 0 }}>
          Work Feed
        </h1>
        <span className="wfp-sub">Last 24 hours · {activity.length} events</span>
      </div>

      {empty ? (
        <div className="dash-card">
          <div className="dash-empty pad">No agent activity in the last 24 hours.</div>
        </div>
      ) : (
        <>
          {hasSummaries && (
            <section className="dash-section">
              <div className="dash-section-head">
                <span className="dash-section-title">What your agents have been doing</span>
              </div>
              <div className="dash-card dash-feed">
                {summaries.map((f, i) => (
                  <div key={`${f.agent}-${i}`} className="dash-feed-row">
                    <div className="dash-feed-top">
                      <span className="dash-feed-agent">{f.agent}</span>
                      <span className="dash-dot-sep">·</span>
                      <span className="dash-feed-time">{fmtRel(f.time)}</span>
                      <span className="dash-feed-tasks">{f.tasks} tasks</span>
                    </div>
                    <div className="dash-feed-summary">{f.summary}</div>
                  </div>
                ))}
              </div>
            </section>
          )}

          <section className="dash-section">
            <div className="dash-section-head">
              <span className="dash-section-title">Activity</span>
            </div>
            {activity.length === 0 ? (
              <div className="dash-card">
                <div className="dash-empty pad">
                  No individual events captured yet — summaries above reflect recent runs.
                </div>
              </div>
            ) : (
              <div className="dash-card wfp-activity">
                {(() => {
                  let lastDay = null
                  const out = []
                  activity.forEach((it, i) => {
                    const day = dayLabel(it.time)
                    if (day !== lastDay) {
                      lastDay = day
                      out.push(
                        <div key={`d-${i}`} className="wfp-divider">
                          {day}
                        </div>,
                      )
                    }
                    out.push(
                      <div key={`a-${i}`} className="wfp-act-row">
                        <span
                          className={`wfp-dot ${it.status === 'error' ? 'err' : ''}`}
                          aria-hidden="true"
                        />
                        <div className="wfp-act-body">
                          <div className="wfp-act-top">
                            <button
                              type="button"
                              className="wfp-agent"
                              onClick={() =>
                                onOpenAgent &&
                                onOpenAgent(it.service_name, it.agent_id || 'main')
                              }
                            >
                              {it.agent}
                            </button>
                            <span className="dash-dot-sep">·</span>
                            <span className="wfp-op">{it.operation}</span>
                            {it.tool && <span className="wfp-tag">{it.tool}</span>}
                            {it.status === 'error' && (
                              <span className="wfp-tag err">error</span>
                            )}
                            <span className="wfp-time">{fmtRel(it.time)}</span>
                          </div>
                          {it.content && (
                            <div className="wfp-snippet">
                              {it.content_type && TYPE_LABEL[it.content_type] && (
                                <span className="wfp-snippet-type">
                                  {TYPE_LABEL[it.content_type]}
                                </span>
                              )}
                              {it.content}
                            </div>
                          )}
                        </div>
                      </div>,
                    )
                  })
                  return out
                })()}
              </div>
            )}
          </section>
        </>
      )}
    </div>
  )
}
