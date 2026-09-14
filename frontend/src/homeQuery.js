/**
 * The query string BOTH Home endpoints take, built in one place.
 *
 * `/home/snapshot`, `/home/findings` and the finding detail all resolve the
 * same scope key from `days` + `tz` + `whose` + `person_id`. Building the
 * string once is what keeps the two reads asking the same question: a
 * snapshot for one period beside findings for another is a page that
 * contradicts itself, and the scope key would not match either.
 *
 * `whose: 'everyone'` is omitted deliberately — the server already applies the
 * seat's own breadth, so sending it only makes the URL noisier. That matches
 * `getWorkItems` / `getWorkOverview`.
 */
export function homeQuery({
  days = null, tz = null, whose = null, personId = null,
  includeDismissed = false,
} = {}) {
  const q = new URLSearchParams()
  if (days) q.set('days', String(days))
  if (tz) q.set('tz', tz)
  if (whose && whose !== 'everyone') q.set('whose', whose)
  if (personId) q.set('person_id', String(personId))
  if (includeDismissed) q.set('include_dismissed', 'true')
  const s = q.toString()
  return s ? `?${s}` : ''
}
