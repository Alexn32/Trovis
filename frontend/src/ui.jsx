// Tiny shared UI primitives.

export function Spinner() {
  return <span className="spinner" aria-label="Loading" />
}

export function Stat({ label, value, tone, sub }) {
  // `tone` is 'warn' | 'error' | undefined — controls the value color.
  // `sub` is optional secondary content under the value (e.g. an upgrade
  // nudge); renders nothing when omitted.
  const valueClass = `stat-box-value${tone ? ' ' + tone : ''}`
  return (
    <div className="stat-box">
      <span className="stat-box-label">{label}</span>
      <span className={valueClass}>{value}</span>
      {sub ? <span className="stat-box-sub">{sub}</span> : null}
    </div>
  )
}

// Fail-soft empty state when Work home (`/work/overview` + `/work/items`)
// or legacy L2 (`/work/board`) times out or the network dies. Copy lives
// here so Board.jsx doesn't grow new user-facing strings.
export function WorkLoadFailed({ onRetry, lead = "Can't load this work" }) {
  return (
    <div className="board-empty" role="alert">
      <p className="board-empty-lead">{lead}</p>
      <p className="board-empty-sub">
        Trovis didn't respond. Retry, or come back in a moment.
      </p>
      {onRetry && (
        <button type="button" className="btn btn-primary" onClick={onRetry}>
          Retry
        </button>
      )}
    </div>
  )
}

// Task panel / story (the per-task detail fetch). Same hang as the board:
// no deadline left "Loading…" or the browser's raw "Failed to fetch".
// Compact so it fits the slide-over; copy stays out of Board.jsx (jargon sweep).
export function StoryLoadFailed({ onRetry }) {
  return (
    <div className="bpanel-err" role="alert">
      <p className="board-empty-lead">Can't load this task</p>
      <p className="board-empty-sub">
        Trovis didn't respond. Retry, or close this panel.
      </p>
      {onRetry && (
        <button type="button" className="btn btn-primary" onClick={onRetry}>
          Retry
        </button>
      )}
    </div>
  )
}
