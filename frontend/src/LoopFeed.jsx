import { useState } from 'react'
import { api } from './api.js'
import { UserIcon, ChevronDownIcon, ChevronRightIcon } from './Icons.jsx'
import {
  lifecycleSentence,
  loopCostLabel,
  loopStateMeta,
  loopStuckHeadline,
  loopTitle,
  parseTs,
  showMarkDone,
  splitActor,
  workflowGroupMeta,
} from './loops.js'

// Workloop rows for the Work Feed and the Stuck view. A loop is one unit of
// work; clicking a row expands it inline (the app's list-detail pattern —
// see Dashboard's AttentionRow / AgentDetail's FeedItem) to show the merged
// event stream from GET /loops/{id}: lifecycle events as compact system
// lines, span activity via the same row markup as the flat feed.

const TYPE_LABEL = { message: 'message', response: 'response', tool_result: 'tool result' }

export function fmtRel(iso) {
  const ms = Date.now() - parseTs(iso)
  if (Number.isNaN(ms)) return ''
  const m = Math.floor(ms / 60000)
  if (m < 1) return 'just now'
  if (m < 60) return `${m}m ago`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ago`
  return `${Math.floor(h / 24)}d ago`
}

/**
 * One action (span) row — the flat feed's row markup, extracted so loop
 * expansions and the "Ungrouped activity" section render actions identically.
 * `item`: {time, status, service_name, agent_id, agent, operation, tool,
 * content, content_type}.
 */
export function ActivityRow({ item, onOpenAgent }) {
  return (
    <div className="wfp-act-row">
      <span className={`wfp-dot ${item.status === 'error' ? 'err' : ''}`} aria-hidden="true" />
      <div className="wfp-act-body">
        <div className="wfp-act-top">
          <button
            type="button"
            className="wfp-agent"
            onClick={() => onOpenAgent && onOpenAgent(item.service_name, item.agent_id || 'main')}
          >
            {item.agent}
          </button>
          <span className="dash-dot-sep">·</span>
          <span className="wfp-op">{item.operation}</span>
          {item.tool && <span className="wfp-tag">{item.tool}</span>}
          {item.status === 'error' && <span className="wfp-tag err">error</span>}
          <span className="wfp-time">{fmtRel(item.time)}</span>
        </div>
        {item.content && (
          <div className="wfp-snippet">
            {item.content_type && TYPE_LABEL[item.content_type] && (
              <span className="wfp-snippet-type">{TYPE_LABEL[item.content_type]}</span>
            )}
            {item.content}
          </div>
        )}
      </div>
    </div>
  )
}

// A loop-detail 'activity' event (payload: span_name/trace_id/…) mapped to
// the ActivityRow item shape.
function activityEventToItem(ev) {
  const { service, agent } = splitActor(ev.actor)
  return {
    time: new Date(Math.floor((ev.ts || 0) / 1e6)).toISOString(),
    status: 'ok',
    service_name: service,
    agent_id: agent,
    agent: agent !== 'main' ? `${service} · ${agent}` : service,
    operation: ev.payload?.span_name || 'action',
    tool: null,
    content: null,
    content_type: null,
  }
}

function ParticipantPill({ p }) {
  const isHuman = p.participant_type === 'human'
  const label = isHuman ? 'human' : splitActor(p.participant).agent !== 'main'
    ? splitActor(p.participant).agent
    : splitActor(p.participant).service
  return (
    <span className={`loop-part ${isHuman ? 'is-human' : ''}`}>
      {isHuman && <UserIcon size={11} />}
      {label}
      <span className="loop-part-role">{p.role}</span>
    </span>
  )
}

function LoopStateChip({ meta }) {
  return (
    <span className={`loop-state tone-${meta.tone}`}>
      {meta.tone === 'live' && <span className="loop-live-dot" aria-hidden="true" />}
      {meta.label}
    </span>
  )
}

/**
 * One loop row. Click to expand inline. `headline` switches line 2 to the
 * Stuck view's age-first phrasing. `sessionUser` gates the Mark-done button
 * (backend 403s api-key auth). `onChanged` refires the parent's load after
 * a close so the list reflects the new state.
 */
export function LoopRow({ loop, onOpenAgent, sessionUser, headline = false, onChanged }) {
  const [open, setOpen] = useState(false)
  const [detail, setDetail] = useState(null)
  const [detailErr, setDetailErr] = useState(null)
  const [closing, setClosing] = useState(false)

  const meta = loopStateMeta(loop)
  const cost = loopCostLabel(loop.total_cost_usd)
  const actions = loop.event_count || 0

  async function toggle() {
    const next = !open
    setOpen(next)
    if (next && !detail) {
      try {
        setDetail(await api.getLoop(loop.id))
        setDetailErr(null)
      } catch (e) {
        setDetailErr(e?.message || 'Could not load this loop')
      }
    }
  }

  async function markDone(e) {
    e.stopPropagation()
    if (closing) return
    setClosing(true)
    try {
      const updated = await api.closeLoop(loop.id)
      setDetail(updated)
      onChanged && onChanged()
    } catch (err) {
      setDetailErr(err?.message || 'Could not mark this loop done')
    } finally {
      setClosing(false)
    }
  }

  // After a close the fresh detail is the truth for state/button rendering.
  const current = detail && detail.id === loop.id ? detail : loop
  const currentMeta = detail ? loopStateMeta(current) : meta
  const canClose = showMarkDone(current, sessionUser)

  return (
    <div className={`loop-row ${currentMeta.attention ? 'attn' : ''} ${open ? 'open' : ''}`}>
      <button type="button" className="loop-row-main" onClick={toggle} aria-expanded={open}>
        <span className="loop-chevron" aria-hidden="true">
          {open ? <ChevronDownIcon size={13} /> : <ChevronRightIcon size={13} />}
        </span>
        <span className="loop-body">
          <span className="loop-title">{loopTitle(loop)}</span>
          <span className="loop-meta">
            {headline && (
              <span className="loop-headline">{loopStuckHeadline(current)}</span>
            )}
            {headline && <span className="dash-dot-sep">·</span>}
            <span>
              {loop.service_name}
              {loop.agent_id && loop.agent_id !== 'main' ? ` · ${loop.agent_id}` : ''}
            </span>
            <span className="dash-dot-sep">·</span>
            <span>{actions === 1 ? '1 action' : `${actions} actions`}</span>
            {cost && (
              <>
                <span className="dash-dot-sep">·</span>
                <span>{cost}</span>
              </>
            )}
            {!headline && (
              <>
                <span className="dash-dot-sep">·</span>
                <LoopStateChip meta={currentMeta} />
              </>
            )}
          </span>
        </span>
        {headline && <LoopStateChip meta={currentMeta} />}
      </button>

      {open && (
        <div className="loop-detail">
          {detailErr && <div className="loop-detail-err">{detailErr}</div>}
          {!detail && !detailErr && <div className="loop-detail-loading">Loading…</div>}
          {detail && (
            <>
              <div className="loop-detail-head">
                {detail.participants?.length > 0 && (
                  <span className="loop-parts">
                    {detail.participants.map((p, i) => (
                      <ParticipantPill key={i} p={p} />
                    ))}
                  </span>
                )}
                {canClose && (
                  <button
                    type="button"
                    className="btn btn-secondary btn-sm"
                    onClick={markDone}
                    disabled={closing}
                  >
                    {closing ? 'Marking…' : 'Mark done'}
                  </button>
                )}
              </div>
              <div className="loop-stream">
                {(detail.events || []).map((ev, i) =>
                  ev.type === 'activity' ? (
                    <ActivityRow key={i} item={activityEventToItem(ev)} onOpenAgent={onOpenAgent} />
                  ) : (
                    <div key={i} className="loop-sys-line">
                      <span className="loop-sys-mark" aria-hidden="true" />
                      {lifecycleSentence(ev)}
                    </div>
                  ),
                )}
              </div>
            </>
          )}
        </div>
      )}
    </div>
  )
}

/**
 * One workflow rollup row (the Work tab's "By workflow" view): an agent
 * identity with its run count, total cost, and a warning-toned state
 * summary when any member loop needs a human. Same visual grammar as loop
 * rows; expanding shows the group's loops via LoopList.
 */
export function WorkflowRollupRow({ group, onOpenAgent, sessionUser, onChanged }) {
  const [open, setOpen] = useState(false)
  const meta = workflowGroupMeta(group)
  return (
    <div className={`loop-row ${meta.attention ? 'attn' : ''} ${open ? 'open' : ''}`}>
      <button
        type="button"
        className="loop-row-main"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
      >
        <span className="loop-chevron" aria-hidden="true">
          {open ? <ChevronDownIcon size={13} /> : <ChevronRightIcon size={13} />}
        </span>
        <span className="loop-body">
          <span className="loop-title">{group.label}</span>
          <span className="loop-meta">
            <span>{meta.runLabel}</span>
            {meta.cost && (
              <>
                <span className="dash-dot-sep">·</span>
                <span>{meta.cost}</span>
              </>
            )}
            {meta.stateLabel && (
              <>
                <span className="dash-dot-sep">·</span>
                <span className="loop-state tone-warning">{meta.stateLabel}</span>
              </>
            )}
          </span>
        </span>
      </button>
      {open && (
        <div className="loop-rollup-loops">
          <LoopList
            loops={group.loops}
            onOpenAgent={onOpenAgent}
            sessionUser={sessionUser}
            onChanged={onChanged}
          />
        </div>
      )}
    </div>
  )
}

export function LoopList({ loops, onOpenAgent, sessionUser, headline = false, onChanged }) {
  return (
    <div className="loop-list">
      {loops.map((l) => (
        <LoopRow
          key={l.id}
          loop={l}
          onOpenAgent={onOpenAgent}
          sessionUser={sessionUser}
          headline={headline}
          onChanged={onChanged}
        />
      ))}
    </div>
  )
}
