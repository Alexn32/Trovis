import { useCallback, useEffect, useState } from 'react'
import { api } from './api.js'
import { ArrowLeftIcon } from './Icons.jsx'
import { ActivityRow, LoopList, fmtRel } from './LoopFeed.jsx'
import { parseTs, sortLoopsAttentionFirst } from './loops.js'

// Dedicated Work Feed page (overlay opened from the dashboard's "View all →").
// Three layers: the per-agent AI rollup up top (what each agent has been
// doing — reuses /dashboard/work-feed), then WORKLOOPS as the primary unit
// (units of work from /loops, attention-first: anything stalled or waiting
// on you floats above the newest-first rest — a deliberate break from the
// old feed's pure-chronological order), then "Ungrouped activity": actions
// that don't belong to any loop (pre-loop history and keyless ingestion),
// preserved as the old flat stream. Mirrors CostPage.

const REFRESH_MS = 30000

// "Today" / "Yesterday" / weekday-date divider for an ISO timestamp.
function dayLabel(iso) {
  const d = new Date(parseTs(iso))
  if (Number.isNaN(d.getTime())) return 'Earlier'
  const startOf = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime()
  const days = Math.round((startOf(new Date()) - startOf(d)) / 86_400_000)
  if (days <= 0) return 'Today'
  if (days === 1) return 'Yesterday'
  if (days < 7) return d.toLocaleDateString(undefined, { weekday: 'long' })
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

export default function WorkFeedPage({ onBack, onOpenAgent, sessionUser }) {
  const [summaries, setSummaries] = useState(null) // per-agent AI rollup
  const [loops, setLoops] = useState(null) // workloops, the feed's primary unit
  const [activity, setActivity] = useState(null) // chronological events
  const [error, setError] = useState(null)

  const load = useCallback(async () => {
    try {
      const [lps, acts, sums] = await Promise.all([
        api.getLoops().catch(() => []), // older backends have no /loops
        api.getActivity(),
        api.getWorkFeed().catch(() => []), // rollup is best-effort (needs Claude)
      ])
      setLoops(Array.isArray(lps) ? lps : [])
      setActivity(Array.isArray(acts) ? acts : [])
      setSummaries(Array.isArray(sums) ? sums : [])
      setError(null)
    } catch (e) {
      setError(e?.message || 'Could not load the work feed')
      setLoops((l) => l ?? [])
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

  const orderedLoops = sortLoopsAttentionFirst(loops || [])
  // Actions that never landed in a loop (pre-loop history, keyless
  // ingestion). loop_id is undefined on older backends → everything shows
  // here, which is exactly the old behavior.
  const ungrouped = activity.filter((it) => it.loop_id == null)
  const hasSummaries = summaries && summaries.length > 0
  const empty = orderedLoops.length === 0 && activity.length === 0 && !hasSummaries

  return (
    <div className="dash wfp">
      <button type="button" className="wf2-back" onClick={onBack}>
        <ArrowLeftIcon size={15} /> Dashboard
      </button>
      <div className="wfp-titlerow">
        <h1 className="dash-hello" style={{ margin: 0 }}>
          Work Feed
        </h1>
        <span className="wfp-sub">
          {orderedLoops.length === 1 ? '1 loop' : `${orderedLoops.length} loops`} · last 24
          hours
        </span>
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

          {orderedLoops.length > 0 && (
            <section className="dash-section">
              <div className="dash-section-head">
                <span className="dash-section-title">Loops</span>
              </div>
              <div className="dash-card">
                <LoopList
                  loops={orderedLoops}
                  onOpenAgent={onOpenAgent}
                  sessionUser={sessionUser}
                  onChanged={load}
                />
              </div>
            </section>
          )}

          {ungrouped.length > 0 && (
            <section className="dash-section">
              <div className="dash-section-head">
                <span className="dash-section-title">Ungrouped activity</span>
              </div>
              <div className="dash-card wfp-activity">
                {(() => {
                  let lastDay = null
                  const out = []
                  ungrouped.forEach((it, i) => {
                    const day = dayLabel(it.time)
                    if (day !== lastDay) {
                      lastDay = day
                      out.push(
                        <div key={`d-${i}`} className="wfp-divider">
                          {day}
                        </div>,
                      )
                    }
                    out.push(<ActivityRow key={`a-${i}`} item={it} onOpenAgent={onOpenAgent} />)
                  })
                  return out
                })()}
              </div>
            </section>
          )}
        </>
      )}
    </div>
  )
}
