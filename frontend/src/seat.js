// The seat, client-side.
//
// A seat says what a person sees and how far each row unfolds. The SERVER
// resolves it (scope level → role → person) and returns it on /auth/me; this
// module only reads what came back. Nothing here is a permission check — the
// API re-derives every one of these answers on its own. Hiding a tab the seat
// doesn't include is a courtesy, not a lock, and a page that forgets to hide
// something is a cosmetic bug, never a hole.
//
// The one rule that matters: when the seat is missing or malformed, fall back
// to the WIDEST view, never the narrowest. A seat arrives late (the /auth/me
// round-trip), can fail, and is absent entirely for API-key sessions. Failing
// closed would blank a working dashboard on a slow network and make Trovis
// look broken; failing open shows the same product the account had before
// seats existed, and the server still refuses anything the person may not do.

export const ALL_SURFACES = ['Home', 'Work', 'Fleet', 'Ask', 'Cost', 'Connect', 'Org']

// What we assume before /auth/me answers, and whenever it answers without a
// seat. Matches the server's own fallback for an unplaced person.
export const FULL_SEAT = {
  breadth: 'company',
  depth: 'technical',
  surfaces: ALL_SURFACES,
  org_builder: false,
  role_id: null,
  role_title: null,
  subtree_user_ids: [],
  visible_user_ids: null,
  can_edit_chart: false,
}

export function seatOf(me) {
  const seat = me?.seat
  if (!seat || typeof seat !== 'object') return FULL_SEAT
  const surfaces = Array.isArray(seat.surfaces) ? seat.surfaces : null
  return {
    ...FULL_SEAT,
    ...seat,
    // An empty surface list means "the server told us nothing useful" far
    // more often than "this person may see no pages at all" — a person with
    // no surfaces has no product. Treat it as the full set.
    surfaces: surfaces && surfaces.length ? surfaces : ALL_SURFACES,
  }
}

export function hasSurface(seat, surface) {
  return (seat?.surfaces || ALL_SURFACES).includes(surface)
}

// 'technical' unfolds the folds — Kind past-runs, Job collapsed agent runs,
// and the Fleet doors out of them. 'glance' hides them. Pages ask this
// instead of reading seat.depth, so the meaning of the atom lives in one file.
export function showsTechnicalFolds(seat) {
  return (seat?.depth || 'technical') === 'technical'
}

// Whether the Whose-work control should offer team/person options at all.
// No reports means those options would list nobody.
export function hasReports(seat) {
  return (seat?.subtree_user_ids || []).length > 0
}
