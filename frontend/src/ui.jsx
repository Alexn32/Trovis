// Tiny shared UI primitives used in both views. Kept here rather than
// per-file so styling stays consistent without a component library.

export function Spinner() {
  return <span className="spinner" aria-label="Loading" />
}

export function Stat({ label, value, bad }) {
  return (
    <div className="stat">
      <span className="stat-label">{label}</span>
      <span className={`stat-value ${bad ? 'stat-bad' : ''}`}>{value}</span>
    </div>
  )
}
