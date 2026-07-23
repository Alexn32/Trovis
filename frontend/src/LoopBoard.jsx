import { useState } from 'react'
import {
  agentLabel,
  barSegments,
  boardGroupSummary,
  boardGroups,
  boardTitle,
  chainDots,
  initialsOf,
  loopStateMeta,
} from './loops.js'
import StoryView from './StoryView.jsx'

// The board: what's happening to the work. Loops group under workflow
// headers (declared workflows first, agent identity for the unmatched);
// each row reads title → possession chain → status pill → story bar.
// Clicking a row opens the loop's story inline (the app's list-detail
// pattern), full-width below the row. A reading of observed truth: no
// drag, no editing, no cost on the board (cost lives in the story's
// terminal line only).

function ChainDots({ mini, label, done }) {
  if (!Array.isArray(mini) || mini.length === 0) return <span className="board-chain" />
  const { dots, collapsed } = chainDots(mini)
  const out = []
  dots.forEach((d, i) => {
    if (collapsed && i === 2) {
      out.push(
        <span key="gap" className="board-chain-gap" aria-hidden="true">
          ··
        </span>,
      )
    } else if (i > 0) {
      out.push(
        <span key={`a-${i}`} className="board-chain-arrow" aria-hidden="true">
          →
        </span>,
      )
    }
    out.push(
      <span
        key={i}
        className={[
          'board-dot',
          d.holder_type === 'agent' && !d.waiting ? 'is-agent' : 'is-wait',
          d.current ? 'is-current' : 'is-past',
        ].join(' ')}
      >
        {d.holder_type === 'agent' && !d.waiting ? initialsOf(label) : ''}
      </span>,
    )
  })
  return <span className={`board-chain ${done ? 'is-done' : ''}`}>{out}</span>
}

function StoryBar({ mini, state, done }) {
  const slices = barSegments(mini, Date.now() * 1e6, state)
  return (
    <span className={`board-bar ${done ? 'is-done' : ''}`} aria-hidden="true">
      {slices.map((s, i) => (
        <span
          key={i}
          className={`board-bar-seg kind-${s.kind}`}
          style={{ width: `${s.pct}%` }}
        />
      ))}
    </span>
  )
}

function StatusPill({ meta }) {
  return <span className={`board-pill tone-${meta.tone}`}>{meta.label}</span>
}

export function BoardRow({ loop, sessionUser, onOpenAgent, onChanged }) {
  const [open, setOpen] = useState(false)
  const meta = loopStateMeta(loop)
  const done = loop.cached_state === 'done' || loop.cached_state === 'abandoned'
  const title = boardTitle(loop)
  return (
    <div className={`board-row-wrap ${meta.attention ? 'attn' : ''}`}>
      <button
        type="button"
        className={`board-row ${done ? 'is-done' : ''}`}
        onClick={() => setOpen(!open)}
        aria-expanded={open}
      >
        <span className="board-work" title={title}>
          {title}
        </span>
        <ChainDots mini={loop.segments_mini} label={agentLabel(loop)} done={done} />
        <StatusPill meta={meta} />
        <StoryBar mini={loop.segments_mini} state={loop.cached_state} done={done} />
      </button>
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

export default function LoopBoard({ loops, sessionUser, onOpenAgent, onChanged, onOpenWorkflow }) {
  const groups = boardGroups(loops)
  return (
    <div className="board">
      {groups.map((g) => (
        <section className="board-group" key={g.key}>
          <div className="board-group-head">
            <span className="board-group-tick" aria-hidden="true" />
            {g.matched && onOpenWorkflow ? (
              // Declared workflows have a page — the header navigates there.
              <button
                type="button"
                className="board-group-name is-link"
                onClick={() => onOpenWorkflow(g.loops[0]?.workflow_id)}
              >
                {g.name} ›
              </button>
            ) : (
              <span className="board-group-name">{g.name}</span>
            )}
            <span className="board-group-summary">{boardGroupSummary(g)}</span>
          </div>
          <div className="board-card">
            {g.loops.map((l) => (
              <BoardRow
                key={l.id}
                loop={l}
                sessionUser={sessionUser}
                onOpenAgent={onOpenAgent}
                onChanged={onChanged}
              />
            ))}
          </div>
        </section>
      ))}
    </div>
  )
}
