// Tiny sparkline. Takes an array of numbers (e.g. spans-per-bucket) and
// renders an SVG path with a gradient fill underneath. `color` is a CSS
// variable name so the sparkline matches whatever status color the parent
// card is using.

export default function Sparkline({ data, color = 'var(--text-muted)', width = 100, height = 28 }) {
  if (!data || data.length < 2) {
    // Not enough data — render a flat baseline rather than fake spikes.
    return (
      <svg width={width} height={height} className="sparkline" aria-hidden="true">
        <line
          x1="0"
          y1={height - 2}
          x2={width}
          y2={height - 2}
          stroke={color}
          strokeWidth="1.2"
          opacity="0.4"
        />
      </svg>
    )
  }

  const max = Math.max(...data, 1)
  const min = Math.min(...data, 0)
  const range = max - min || 1

  // Add 2px top/bottom padding so peaks don't clip.
  const padTop = 2
  const padBottom = 2
  const drawHeight = height - padTop - padBottom

  const points = data.map((v, i) => {
    const x = (i / (data.length - 1)) * width
    const y = padTop + drawHeight - ((v - min) / range) * drawHeight
    return [x, y]
  })

  const linePath = points
    .map(([x, y], i) => (i === 0 ? `M ${x} ${y}` : `L ${x} ${y}`))
    .join(' ')

  // Fill area: down to baseline and back to start.
  const fillPath =
    linePath +
    ` L ${width} ${height} L 0 ${height} Z`

  // Unique gradient id so multiple sparklines on a page don't collide.
  const gid = `sl-${Math.random().toString(36).slice(2, 9)}`

  return (
    <svg width={width} height={height} className="sparkline" aria-hidden="true">
      <defs>
        <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={color} stopOpacity="0.30" />
          <stop offset="100%" stopColor={color} stopOpacity="0" />
        </linearGradient>
      </defs>
      <path d={fillPath} fill={`url(#${gid})`} />
      <path
        d={linePath}
        fill="none"
        stroke={color}
        strokeWidth="1.4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}
