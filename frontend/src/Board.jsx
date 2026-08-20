import { useCallback, useEffect, useState } from 'react'
import { api } from './api.js'
import { ChevronRightIcon, UserIcon } from './Icons.jsx'
import { boardAge, boardCostLabel, boardEmpty, holderLine } from './board.js'
import { lifecycleSentence } from './loops.js'

// The Work board — the Work tab's landing surface.
//
// A board of TASKS. Every column, label and sentence here is written for
// someone who has never heard of Trovis: work, waiting, stuck, done. The
// words "loop", "possession", "station" and "handoff" do not appear, and
// must not be reintroduced.
//
// Cards move themselves. The board is a mirror of the record, so there is no
// drag: you cannot drag a task into "done" any more than you can drag a
// thermometer to change the weather. You CAN act on a card — resolving a
// handoff writes a real event — which is a different thing entirely.

const REFRESH_MS = 20000

// ---------------------------------------------------------------------------

function Card({ card, onOpen, onResolved }) {
  const [busy, setBusy] = useState(null)
  const [note, setNote] = useState(null)

  async function resolve(e, action) {
    e.stopPropagation()
    if (busy) return
    setBusy(action)
    try {
      const fn =
        action === 'accept'
          ? api.acceptHandoff
          : action === 'complete'
            ? api.completeHandoff
            : api.declineHandoff
      await fn(card.id, card.handoff_event_id)
      onResolved?.()
    } catch (err) {
      // A task that already closed is not a failure — say what happened.
      setNote(
        err?.status === 409
          ? 'This finished while you were looking at it.'
          : err?.message || 'Could not update this task.',
      )
    } finally {
      setBusy(null)
    }
  }

  const age = boardAge(card.age_seconds)
  const cost = boardCostLabel(card.cost_usd)

  return (
    <div
      className={`bc ${card.is_yours ? 'is-yours' : ''}`}
      role="button"
      tabIndex={0}
      onClick={() => onOpen(card)}
      onKeyDown={(e) => (e.key === 'Enter' ? onOpen(card) : null)}
    >
      {card.is_yours && <span className="bc-flag">Waiting on you</span>}
      <span className="bc-title">{card.title}</span>
      <span className="bc-meta">
        <span className={`bc-holder ${card.holder_type}`}>
          {card.holder_type === 'human' && <UserIcon size={11} />}
          {card.holder_name}
        </span>
        {age && (
          <>
            <span className="bc-dot">·</span>
            <span>{age}</span>
          </>
        )}
        {cost && (
          <>
            <span className="bc-dot">·</span>
            <span>{cost}</span>
          </>
        )}
      </span>
      {card.waiting_on && (
        <span className="bc-note">waiting on {card.waiting_on}</span>
      )}
      {card.stuck_reason && <span className="bc-note">{card.stuck_reason}</span>}
      {card.is_yours && card.handoff_event_id && (
        <span className="bc-actions">
          <button type="button" disabled={!!busy} onClick={(e) => resolve(e, 'complete')}>
            {busy === 'complete' ? 'Saving…' : 'Done'}
          </button>
          <button type="button" disabled={!!busy} onClick={(e) => resolve(e, 'accept')}>
            {busy === 'accept' ? 'Taking…' : 'I’ve got this'}
          </button>
          <button type="button" disabled={!!busy} onClick={(e) => resolve(e, 'decline')}>
            {busy === 'decline' ? 'Passing…' : 'Not mine'}
          </button>
        </span>
      )}
      {note && <span className="bc-note is-warn">{note}</span>}
    </div>
  )
}

// Done today gets long; show the recent ones and count the rest.
const DONE_VISIBLE = 10

function Column({ column, onOpen, onResolved }) {
  const [expanded, setExpanded] = useState(false)
  const isDone = column.key === 'done'
  const hidden = isDone && !expanded ? Math.max(0, column.cards.length - DONE_VISIBLE) : 0
  const shown = hidden ? column.cards.slice(0, DONE_VISIBLE) : column.cards

  return (
    <section className={`bcol bcol-${column.key}`}>
      <header className="bcol-head">
        <span className="bcol-label">{column.label}</span>
        <span className="bcol-count">{column.count}</span>
      </header>
      <div className="bcol-body">
        {shown.map((c) => (
          <Card key={c.id} card={c} onOpen={onOpen} onResolved={onResolved} />
        ))}
        {hidden > 0 && (
          <button type="button" className="bcol-more" onClick={() => setExpanded(true)}>
            +{hidden} more
          </button>
        )}
        {/* An empty column still renders. The shape of the board teaches what
            the board is for. */}
        {column.cards.length === 0 && <div className="bcol-empty" aria-hidden="true" />}
      </div>
    </section>
  )
}

// ---------------------------------------------------------------------------
// The task panel — the story, as steps. Slides over; never the entry point.

function TaskPanel({ card, onClose, onResolved }) {
  const [detail, setDetail] = useState(null)
  const [err, setErr] = useState(null)

  useEffect(() => {
    let live = true
    api
      .getLoop(card.id)
      .then((d) => live && setDetail(d))
      .catch((e) => live && setErr(e?.message || 'Could not load this task'))
    return () => {
      live = false
    }
  }, [card.id])

  const steps = (detail?.events || []).filter((e) => e.sentence)

  return (
    <>
      <div className="bpanel-scrim" onClick={onClose} />
      <aside className="bpanel" role="dialog" aria-label={card.title}>
        <header className="bpanel-head">
          <button type="button" className="bpanel-close" onClick={onClose}>
            Close
          </button>
          <h2>{card.title}</h2>
          <p className="bpanel-sub">{holderLine(card)}</p>
        </header>
        {err && <div className="bpanel-err">{err}</div>}
        {!detail && !err && <div className="bpanel-loading">Loading…</div>}
        {detail && (
          <ol className="bpanel-steps">
            {steps.map((e, i) => (
              <li key={i}>
                <span className="bps-text">{e.sentence || lifecycleSentence(e)}</span>
              </li>
            ))}
            {steps.length === 0 && <li className="bps-none">No steps recorded yet.</li>}
          </ol>
        )}
      </aside>
    </>
  )
}

// ---------------------------------------------------------------------------

export default function Board({ onConnectAgent, onOpenWorkflow }) {
  const [board, setBoard] = useState(null)
  const [err, setErr] = useState(null)
  const [workflowId, setWorkflowId] = useState('')
  const [open, setOpen] = useState(null)

  const load = useCallback(async () => {
    try {
      setBoard(await api.getWorkBoard(workflowId || null))
      setErr(null)
    } catch (e) {
      setErr(e?.message || 'Could not load the board')
    }
  }, [workflowId])

  useEffect(() => {
    load()
    const t = setInterval(load, REFRESH_MS)
    return () => clearInterval(t)
  }, [load])

  if (err) return <div className="view board-view"><div className="dash-empty pad">{err}</div></div>
  if (!board) return <div className="view board-view"><div className="dash-empty pad">Loading…</div></div>

  const empty = boardEmpty(board)

  return (
    <div className="view board-view">
      <div className="board-head">
        <h1>Work</h1>
        {board.workflows.length > 0 && (
          <div className="board-filter">
            <select
              value={workflowId}
              onChange={(e) => setWorkflowId(e.target.value)}
              aria-label="Filter by workflow"
            >
              <option value="">All work</option>
              {board.workflows.map((w) => (
                <option key={w.id} value={w.id}>
                  {w.name} ({w.count})
                </option>
              ))}
            </select>
            {workflowId && (
              <button
                type="button"
                className="board-map-link"
                onClick={() => onOpenWorkflow?.(Number(workflowId))}
              >
                See the steps <ChevronRightIcon size={13} />
              </button>
            )}
          </div>
        )}
      </div>

      {empty && (
        <div className="board-empty">
          <p className="board-empty-lead">{empty.lead}</p>
          <p className="board-empty-sub">{empty.sub}</p>
          {empty.cta && (
            <button type="button" className="btn btn-primary" onClick={onConnectAgent}>
              {empty.cta}
            </button>
          )}
        </div>
      )}

      <div className="board">
        {board.columns.map((c) => (
          <Column key={c.key} column={c} onOpen={setOpen} onResolved={load} />
        ))}
      </div>

      {open && (
        <TaskPanel
          card={open}
          onClose={() => setOpen(null)}
          onResolved={() => {
            setOpen(null)
            load()
          }}
        />
      )}
    </div>
  )
}
