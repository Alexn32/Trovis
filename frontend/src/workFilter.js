// The Work list's filter vocabulary, in one place.
//
// Extracted so it is testable on its own, and so a caller navigating INTO Work
// can be checked against the values Work actually understands. That was a real
// bug: Home sent `waiting_on_you` — a row STATUS, not a filter name — and
// `matchesWorkFilter` fell through to `default: return true`, so the "Open your
// work" button landed on an unfiltered list while claiming to be personal.
// A value this table does not know is not a filter.

import { partitionLookAt } from './home.js'

export const WORK_FILTER_LABELS = {
  attention: 'Needs attention',
  // Home's desk is only YOUR waits, so its "Open in Work" has to land on the
  // same set — 'waiting' is everyone's, which would be a wider list than the
  // one you just tapped away from.
  mine: 'Waiting on you',
  moving: 'Moving',
  waiting: 'Waiting',
  stuck: 'Stuck',
  done: 'Done',
}

export function matchesWorkFilter(row, filter) {
  switch (filter) {
    case 'moving':
      return row.status === 'moving'
    case 'mine':
      return row.status === 'waiting_on_you'
    case 'waiting':
      return row.status === 'waiting_on_you' || row.status === 'waiting_on_other'
    case 'stuck':
      return row.status === 'stuck'
    case 'done':
      return row.status === 'done'
    case 'attention': {
      const { needsYou, needsAttention } = partitionLookAt([row])
      return needsYou.length + needsAttention.length > 0
    }
    default:
      return true
  }
}
