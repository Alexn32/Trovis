// Tiny shared UI primitives.

export function Spinner() {
  return <span className="spinner" aria-label="Loading" />
}

export function Stat({ label, value, tone }) {
  // `tone` is one of 'warn' | 'error' | undefined — controls the value color.
  const valueClass = `stat-box-value${tone ? ' ' + tone : ''}`
  return (
    <div className="stat-box">
      <span className="stat-box-label">{label}</span>
      <span className={valueClass}>{value}</span>
    </div>
  )
}
