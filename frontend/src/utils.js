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

// Status freshness as specified: green ≤5m, yellow ≤1h, gray otherwise.
export function statusFromLastSeen(iso) {
  if (!iso) return 'gray'
  const diffSec = (Date.now() - new Date(iso).getTime()) / 1000
  if (diffSec <= 300) return 'green'
  if (diffSec <= 3600) return 'yellow'
  return 'gray'
}

export function formatDuration(ms) {
  if (ms == null || Number.isNaN(ms)) return '—'
  if (ms < 1) return `${(ms * 1000).toFixed(0)}μs`
  if (ms < 1000) return `${ms.toFixed(1)}ms`
  return `${(ms / 1000).toFixed(2)}s`
}

export function nsToMs(ns) {
  return ns / 1_000_000
}

export function formatNsTimestamp(ns) {
  return new Date(ns / 1_000_000).toLocaleString()
}
