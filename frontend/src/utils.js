// Small presentation-layer helpers. Pure functions — no React, no DOM.

export function relativeTime(iso) {
  if (!iso) return 'never'
  const then = new Date(iso).getTime()
  const diffSec = Math.floor((Date.now() - then) / 1000)
  if (diffSec < 0) return 'just now'
  if (diffSec < 60) return `${diffSec}s ago`
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)}m ago`
  if (diffSec < 86400) return `${Math.floor(diffSec / 3600)}h ago`
  if (diffSec < 2592000) return `${Math.floor(diffSec / 86400)}d ago`
  return new Date(iso).toLocaleDateString()
}

// Status freshness for an agent: green ≤5m, yellow ≤1h, gray otherwise.
// Bumped to yellow if the agent has a high enough error rate, regardless
// of recency.
export function statusFor(agent) {
  const errorRate = agent.span_count
    ? (agent.error_count / agent.span_count) * 100
    : 0
  if (errorRate > 20) return 'red'
  if (!agent.last_seen) return 'gray'
  const diffSec = (Date.now() - new Date(agent.last_seen).getTime()) / 1000
  if (diffSec <= 300) {
    return errorRate > 5 ? 'yellow' : 'green'
  }
  if (diffSec <= 3600 || errorRate > 5) return 'yellow'
  return 'gray'
}

export function errorRatePercent(agent) {
  if (!agent.span_count) return 0
  return (agent.error_count / agent.span_count) * 100
}

export function formatDuration(ms) {
  if (ms == null || Number.isNaN(ms)) return '—'
  if (ms < 1) return `${(ms * 1000).toFixed(0)}μs`
  if (ms < 1000) return `${ms.toFixed(0)}ms`
  return `${(ms / 1000).toFixed(2)}s`
}

export function nsToMs(ns) {
  return ns / 1_000_000
}

// Format a USD cost. Sub-cent values get more precision so a $0.0042
// agent doesn't read as "$0.00". Larger values round to cents.
export function formatCost(usd) {
  const n = Number(usd) || 0
  if (n === 0) return '$0.00'
  if (n < 0.01) return `$${n.toFixed(4)}`
  if (n < 1) return `$${n.toFixed(3)}`
  return `$${n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

// Compact token count: 48200 → "48.2K", 1_500_000 → "1.5M".
export function formatTokens(n) {
  const v = Number(n) || 0
  if (v >= 1_000_000) return `${(v / 1_000_000).toFixed(1)}M`
  if (v >= 1_000) return `${(v / 1_000).toFixed(1)}K`
  return String(v)
}

export function formatNsTimestamp(ns) {
  return new Date(ns / 1_000_000).toLocaleString()
}

// Extract a friendly model name from an agent's top operations or
// description, if any. Returns empty string if nothing identifiable.
// Used as a fallback when /registration isn't available; for agents with
// registration data we'd prefer registration.model.
export function inferModelFromAgent(agent) {
  // No reliable signal in the /agents response yet. Caller can pass an
  // overrideModel from the registration endpoint when available.
  return ''
}

// Color helpers — return a CSS variable name (not a literal color) so the
// caller can write style={{ color: cssVar(...) }} and have it theme-match.
export function statusColor(status) {
  switch (status) {
    case 'green':
      return 'var(--success)'
    case 'yellow':
      return 'var(--warning)'
    case 'red':
      return 'var(--error)'
    default:
      return 'var(--idle)'
  }
}

// Group span timestamps into N equal-width time buckets between earliest
// and latest. Returns counts per bucket. Used to feed Sparkline.
export function bucketSpansForSparkline(spans, bucketCount = 12) {
  if (!spans || spans.length === 0) return []
  // start_time_unix is a nanosecond timestamp; convert to ms for bucketing.
  const times = spans.map((s) => s.start_time_unix / 1_000_000)
  const min = Math.min(...times)
  const max = Math.max(...times)
  if (max === min) {
    // All spans at the same time — single bar.
    return [spans.length]
  }
  const width = (max - min) / bucketCount
  const buckets = new Array(bucketCount).fill(0)
  for (const t of times) {
    let idx = Math.floor((t - min) / width)
    if (idx >= bucketCount) idx = bucketCount - 1
    buckets[idx] += 1
  }
  return buckets
}

// Group span counts into the last `days` calendar days (UTC). Returns an
// array of length `days` with the oldest first. Used by AgentDetail.
export function bucketSpansByDay(spans, days = 14) {
  const now = Date.now()
  const dayMs = 24 * 60 * 60 * 1000
  const buckets = new Array(days).fill(0)
  if (!spans) return buckets
  for (const s of spans) {
    const t = s.start_time_unix / 1_000_000
    const ageMs = now - t
    const idx = days - 1 - Math.floor(ageMs / dayMs)
    if (idx >= 0 && idx < days) buckets[idx] += 1
  }
  return buckets
}
