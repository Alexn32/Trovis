import { useCallback, useEffect, useState } from 'react'
import { api } from './api.js'
import { UserIcon, ChevronRightIcon } from './Icons.jsx'
import Board from './Board.jsx'
import {
  boardAge,
  boardCostLabel,
  kindRollup,
  kindNeedsAttention,
  otherNudge,
  workScreenEmpty,
} from './board.js'

// The Work tab.
//   Level 1 — this screen: one card per kind of work (per workflow), plus an
//             "Other work" catch-all. Above them, a strip of tasks waiting on
//             YOU, across every kind.
//   Level 2 — the board (Board.jsx), filtered to one kind. The Yours strip
//             persists here too: your desk is never more than zero clicks away.
//   Level 3 — the task story (the board's slide-over).
//
// Task language throughout. No loops, no stations, no handoffs.

const REFRESH_MS = 20000

// ---------------------------------------------------------------------------
// The "waiting on you" strip — shown on both levels, always cross-workflow.

function YoursStrip({ yours, onOpen, onResolved }) {
  if (!yours || yours.length === 0) return null
  return (
    <section className="yours-strip" aria-label="Waiting on you">
      <div className="yours-head">Waiting on you</div>
      <div className="yours-rail">
        {yours.map((c) => (
          <YoursCard key={c.id} card={c} onOpen={onOpen} onResolved={onResolved} />
        ))}
      </div>
    </section>
  )
}

function YoursCard({ card, onOpen, onResolved }) {
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
  return (
    <div
      className="yc"
      role="button"
      tabIndex={0}
      onClick={() => onOpen(card)}
      onKeyDown={(e) => (e.key === 'Enter' ? onOpen(card) : null)}
    >
      <span className="yc-title">{card.title}</span>
      {age && <span className="yc-age">waiting {age}</span>}
      {card.handoff_event_id && (
        <span className="yc-actions">
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
      {note && <span className="yc-note">{note}</span>}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Level 1 — a card per kind of work

function KindCard({ card, onOpen, onDeclare }) {
  const rollup = kindRollup(card)
  const nudge = card.is_other ? otherNudge(card) : ''
  return (
    <div
      className={`kind ${kindNeedsAttention(card) ? 'attn' : ''} ${card.is_other ? 'is-other' : ''}`}
      role="button"
      tabIndex={0}
      onClick={() => onOpen(card)}
      onKeyDown={(e) => (e.key === 'Enter' ? onOpen(card) : null)}
    >
      <div className="kind-top">
        <span className="kind-name">{card.name}</span>
        <ChevronRightIcon size={15} />
      </div>
      <div className="kind-rollup">
        {rollup.map((p, i) => (
          <span key={i} className={`kr kr-${p.tone}`}>
            {p.value != null && <b>{p.value}</b>} {p.label}
          </span>
        ))}
      </div>
      {nudge && (
        <button
          type="button"
          className="kind-nudge"
          onClick={(e) => {
            e.stopPropagation()
            onDeclare?.()
          }}
        >
          {nudge}
        </button>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------

export default function WorkTab({ onConnectAgent, onNewWorkflow, onOpenWorkflow }) {
  const [summary, setSummary] = useState(null)
  const [err, setErr] = useState(null)
  const [selected, setSelected] = useState(null) // { id, name } | null (Level 2)

  const load = useCallback(async () => {
    try {
      setSummary(await api.getWorkSummary())
      setErr(null)
    } catch (e) {
      setErr(e?.message || 'Could not load your work')
    }
  }, [])

  useEffect(() => {
    load()
    const t = setInterval(load, REFRESH_MS)
    return () => clearInterval(t)
  }, [load])

  // A strip card's body click drills into that task's KIND on the board,
  // where the full card and its story live and the item floats at the top.
  // (The resolve actions on the strip itself need no navigation.)
  const openTask = (card) =>
    setSelected({ id: card.workflow_id || null, name: card.workflow_name || 'Other work' })

  if (err) {
    return <div className="view board-view"><div className="dash-empty pad">{err}</div></div>
  }
  if (!summary) {
    return <div className="view board-view"><div className="dash-empty pad">Loading…</div></div>
  }

  // Level 2 — the board, filtered to the chosen kind, with the strip above it.
  if (selected) {
    return (
      <div className="view board-view">
        <YoursStrip yours={summary.yours} onOpen={openTask} onResolved={load} />
        <Board
          initialWorkflowId={selected.id || ''}
          onBack={() => setSelected(null)}
          onConnectAgent={onConnectAgent}
          onOpenWorkflow={onOpenWorkflow}
        />
      </div>
    )
  }

  // Level 1 — the Work screen.
  const empty = workScreenEmpty(summary)
  const kinds = summary.kinds || []
  const other = summary.other

  return (
    <div className="view board-view">
      <div className="board-head">
        <h1>Work</h1>
      </div>

      <YoursStrip yours={summary.yours} onOpen={openTask} onResolved={load} />

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

      {!empty && (
        <div className="kinds">
          {kinds.map((k) => (
            <KindCard
              key={k.workflow_id}
              card={k}
              onOpen={() => setSelected({ id: k.workflow_id, name: k.name })}
              onDeclare={onNewWorkflow}
            />
          ))}
          {/* Other work is always last. */}
          {other && (
            <KindCard
              card={other}
              onOpen={() => setSelected({ id: null, name: other.name })}
              onDeclare={onNewWorkflow}
            />
          )}
        </div>
      )}
    </div>
  )
}
