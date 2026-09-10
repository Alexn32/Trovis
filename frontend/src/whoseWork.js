// The Whose-work control, as logic.
//
// Everyone in an org queries the same Work truth; this only chooses which
// slice of it is on screen. The SERVER decides what that slice may contain —
// it intersects any request with the seat — so nothing here is a permission
// check. What it does own is the honesty of the control: never offer an
// option that would come back empty, and never offer one the server would
// refuse.
//
// The default is the seat's own breadth, so arriving at Work shows exactly
// what the seat says you can see, with no choice to make.

import { hasReports } from './seat.js'

export const WHOSE_DEFAULT = 'everyone'

// What "everyone I can see" actually means, said in the seat's own terms.
// A company seat saying "Everyone" is true; a self seat saying it would be a
// lie, and the control hides itself there entirely (see whoseOptions).
function everyoneLabel(seat) {
  switch (seat?.breadth) {
    case 'self':
      return 'Just me'
    case 'subtree':
      return 'Everyone I can see'
    default:
      return 'Everyone'
  }
}

/**
 * The options to show, in order. Empty means: render no control at all.
 *
 * A seat with no reports has nothing to choose between — "Me" and "Everyone
 * I can see" would be the same list — so an IC gets no control rather than a
 * decorative one. That is the `hasReports` rule from seat.js, applied.
 */
export function whoseOptions(seat, people = []) {
  if (!hasReports(seat)) return []
  const subtree = new Set(seat?.subtree_user_ids || [])
  const options = [
    { value: 'everyone', label: everyoneLabel(seat) },
    { value: 'me', label: 'Me' },
    { value: 'team', label: 'My team' },
  ]
  for (const p of people || []) {
    if (!subtree.has(p.id)) continue
    options.push({
      value: `person:${p.id}`,
      label: p.name || p.email || `Member ${p.id}`,
      personId: p.id,
    })
  }
  return options
}

/** Split a control value into the query the API takes. */
export function whoseParams(value) {
  const v = value || WHOSE_DEFAULT
  if (v.startsWith('person:')) {
    const id = Number(v.slice('person:'.length))
    return Number.isFinite(id) && id > 0
      ? { whose: 'person', personId: id }
      : { whose: WHOSE_DEFAULT, personId: null }
  }
  return { whose: v, personId: null }
}

/**
 * Keep a selection honest when the seat changes underneath it.
 *
 * A manager who loses their reports (a reorg, a role move) is holding a
 * "My team" selection that no longer means anything, and the server would
 * answer a person filter for someone no longer under them with a 403. Fall
 * back rather than showing an error the person did not cause.
 */
export function reconcileWhose(value, seat) {
  const options = whoseOptions(seat)
  if (!options.length) return WHOSE_DEFAULT
  if (value && value.startsWith('person:')) {
    const id = Number(value.slice('person:'.length))
    return (seat?.subtree_user_ids || []).includes(id) ? value : WHOSE_DEFAULT
  }
  return options.some((o) => o.value === value) ? value : WHOSE_DEFAULT
}

/**
 * What the empty state should say when a filter, not an empty org, is why
 * the table is bare. Returning null means "this is the ordinary empty Work
 * screen" — the caller keeps its own copy.
 */
export function whoseEmptyCopy(value) {
  const v = value || WHOSE_DEFAULT
  if (v === WHOSE_DEFAULT) return null
  if (v === 'me') return 'No work of your own right now.'
  if (v === 'team') return "Nothing on your team's plate right now."
  return 'No work for this person right now.'
}
