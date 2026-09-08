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

// Fail-soft empty state when Work L1 (`/work/summary`) or L2 (`/work/board`)
// times out or the network dies. Copy lives here so Board.jsx doesn't grow
// new user-facing strings (its jargon sweep reads that file).
export function WorkLoadFailed({ onRetry }) {
  return (
    <div className="board-empty" role="alert">
      <p className="board-empty-lead">Can't load this work</p>
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
