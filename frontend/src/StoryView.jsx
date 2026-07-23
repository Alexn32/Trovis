import { useEffect, useState } from 'react'
import { api } from './api.js'
import {
  agentLabel,
  fmtAgeLong,
  initialsOf,
  loopCostLabel,
  loopDurationSeconds,
  loopStateMeta,
  parseTs,
  showMarkDone,
  splitActor,
} from './loops.js'

// The loop's story: possession blocks flowing down, each holding the
// narrated events (server `sentence` strings — no client narration).
// The texture change at every handoff — solid teal spine for agent
// possessions, dashed warm spine for waits — is the design's key move.

const SENTENCE_COLLAPSE_AT = 8

function fmtRel(iso) {
  const ms = Date.now() - parseTs(iso)
  if (Number.isNaN(ms)) return ''
  const m = Math.floor(ms / 60000)
  if (m < 1) return 'just now'
  if (m < 60) return `${m}m ago`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ago`
  return `${Math.floor(h / 24)}d ago`
}

function segDurationS(seg, nowNs) {
  const end = seg.end_ns == null ? nowNs : seg.end_ns
  return Math.max(0, Math.floor((end - seg.start_ns) / 1e9))
}

function holderName(seg) {
  if (seg.holder_type === 'agent') {
    const { service, agent } = splitActor(seg.holder)
    return agent !== 'main' ? agent : service || 'agent'
  }
  return seg.holder || (seg.holder_type === 'system' ? 'the system' : 'a human')
}

function PossessionBlock({ seg, events, live, canClose, closing, onMarkDone }) {
  const [expanded, setExpanded] = useState(false)
  const nowNs = Date.now() * 1e6
  const dur = fmtAgeLong(segDurationS(seg, nowNs))
  const name = holderName(seg)
  const isAgent = seg.holder_type === 'agent' && !seg.waiting

  if (seg.waiting) {
    return (
      <div className="story-block is-wait">
        <div className="story-gutter">
          <span className="story-dot is-wait" aria-hidden="true" />
          <span className="story-spine is-wait" aria-hidden="true" />
        </div>
        <div className="story-waitbox">
          {live ? (
            <>
              <div className="story-wait-head">Waiting {dur}</div>
              <div className="story-wait-sub">
                The work is with {name}. Nothing moves until they act.
              </div>
              {canClose && (
                <button
                  type="button"
                  className="btn btn-secondary btn-sm"
                  onClick={onMarkDone}
                  disabled={closing}
                >
                  {closing ? 'Marking…' : 'Mark done'}
                </button>
              )}
            </>
          ) : (
            <div className="story-wait-sub">
              Waited {dur} on {name}
            </div>
          )}
        </div>
      </div>
    )
  }

  const sentences = events.map((e) => e.sentence).filter(Boolean)
  const shown =
    sentences.length > SENTENCE_COLLAPSE_AT && !expanded
      ? sentences.slice(0, 2)
      : sentences
  const hidden = sentences.length - shown.length
  return (
    <div className="story-block">
      <div className="story-gutter">
        <span className={`story-dot ${isAgent ? 'is-agent' : 'is-wait'}`} aria-hidden="true">
          {isAgent ? initialsOf(name) : ''}
        </span>
        <span className="story-spine" aria-hidden="true" />
      </div>
      <div className="story-body">
        <div className="story-holder">
          {name} <span className="story-dur">· {dur}</span>
        </div>
        {shown.map((s, i) => (
          <div key={i} className="story-line">
            {s}
          </div>
        ))}
        {hidden > 0 && (
          <button
            type="button"
            className="story-more"
            onClick={() => setExpanded(true)}
          >
            · {hidden} more actions
          </button>
        )}
        {seg.touches?.length > 0 && (
          <div className="story-chips">
            {seg.touches.map((t, i) => (
              <span key={i} className="story-chip">
                {t.name}
                {t.count > 1 ? ` ×${t.count}` : ''}
              </span>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

export default function StoryView({ loop, sessionUser, onChanged }) {
  const [detail, setDetail] = useState(null)
  const [err, setErr] = useState(null)
  const [closing, setClosing] = useState(false)

  useEffect(() => {
    let alive = true
    api
      .getLoop(loop.id)
      .then((d) => alive && setDetail(d))
      .catch((e) => alive && setErr(e?.message || 'Could not load this loop'))
    return () => {
      alive = false
    }
  }, [loop.id])

  async function markDone(e) {
    e.stopPropagation()
    if (closing) return
    setClosing(true)
    try {
      setDetail(await api.closeLoop(loop.id))
      onChanged && onChanged()
    } catch (error) {
      setErr(error?.message || 'Could not mark this loop done')
    } finally {
      setClosing(false)
    }
  }

  if (err) return <div className="story is-err">{err}</div>
  if (!detail) return <div className="story is-loading">Loading the story…</div>

  const meta = loopStateMeta(detail)
  const segments = detail.segments || []
  const events = detail.events || []
  const canClose = showMarkDone(detail, sessionUser)
  const closed = Boolean(detail.closed_at)
  const durS = loopDurationSeconds(detail)
  const cost = loopCostLabel(detail.total_cost_usd)

  // Assign narrated events to the possession covering their timestamp.
  // Boundary events (ts exactly at a segment seam — e.g. the handoff
  // sentence, stamped at the wait's start) belong to the EARLIER block:
  // "Handed to Sarah" is the agent's last act, not the wait's first.
  // loop_closed is excluded — the terminal line below tells that ending.
  const perSegment = segments.map(() => [])
  for (const e of events) {
    if (e.type === 'loop_closed' || segments.length === 0) continue
    let idx = segments.findIndex(
      (seg) => e.ts >= seg.start_ns && (seg.end_ns == null || e.ts < seg.end_ns),
    )
    if (idx === -1) idx = e.ts < segments[0].start_ns ? 0 : segments.length - 1
    if (idx > 0 && e.ts === segments[idx].start_ns) idx -= 1
    perSegment[idx].push(e)
  }

  return (
    <div className="story">
      <div className="story-head">
        <span className="story-title">{detail.title || agentLabel(detail)}</span>
        <span className={`board-pill tone-${meta.tone}`}>{meta.label}</span>
      </div>
      <div className="story-sub">
        {detail.workflow_name || agentLabel(detail)} · started {fmtRel(detail.created_at)}
      </div>
      <div className="story-blocks">
        {segments.map((seg, i) => (
          <PossessionBlock
            key={i}
            seg={seg}
            events={perSegment[i]}
            live={seg.end_ns == null && !closed}
            canClose={canClose}
            closing={closing}
            onMarkDone={markDone}
          />
        ))}
      </div>
      {closed && (
        <div className="story-terminal">
          {detail.cached_state === 'done' ? 'Done' : 'Abandoned'}
          {durS != null && <> · {fmtAgeLong(durS)}</>}
          {cost && (
            <>
              {' · '}
              <span className="story-cost">{cost}</span>
            </>
          )}
        </div>
      )}
    </div>
  )
}
