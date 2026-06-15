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
