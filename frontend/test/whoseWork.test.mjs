// Whose work, client side.
//
// This control chooses which slice of the one Work truth is on screen. It is
// NOT a permission check — the server intersects every request with the seat
// — so these tests are about honesty rather than safety: never offer an
// option that comes back empty, never offer one the server would refuse, and
// never let a choice reach the desk.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import {
  WHOSE_DEFAULT,
  reconcileWhose,
  whoseEmptyCopy,
  whoseOptions,
  whoseParams,
} from '../src/whoseWork.js'

const workTab = readFileSync(new URL('../src/WorkTab.jsx', import.meta.url), 'utf8')
const api = readFileSync(new URL('../src/api.js', import.meta.url), 'utf8')
const app = readFileSync(new URL('../src/App.jsx', import.meta.url), 'utf8')

const MANAGER = { breadth: 'subtree', subtree_user_ids: [11, 12] }
const IC = { breadth: 'self', subtree_user_ids: [] }
const EXEC = { breadth: 'company', subtree_user_ids: [11, 12, 13] }
const PEOPLE = [
  { id: 11, name: 'Ira Chen', email: 'ira@acme.test' },
  { id: 12, name: null, email: 'bo@acme.test' },
  { id: 99, name: 'Someone Else', email: 'else@acme.test' },
]

test('no reports means no control at all, not a control that does nothing', () => {
  // "Me" and "Everyone I can see" are the same list for an IC. A picker
  // whose options all produce the same table is worse than no picker.
  assert.deepEqual(whoseOptions(IC), [])
  assert.deepEqual(whoseOptions(null), [])
  assert.deepEqual(whoseOptions({ subtree_user_ids: [] }, PEOPLE), [])
  assert.ok(whoseOptions(MANAGER).length >= 3)
})

test('the widest option says what it actually means for this seat', () => {
  const label = (seat) => whoseOptions(seat, PEOPLE)[0].label
  // A subtree manager's "Everyone" is not everyone, and saying so would be a
  // lie the table then appears to contradict.
  assert.equal(label(MANAGER), 'Everyone I can see')
  assert.equal(label(EXEC), 'Everyone')
})

test('only people actually under you are offered', () => {
  const values = whoseOptions(MANAGER, PEOPLE).map((o) => o.value)
  assert.ok(values.includes('person:11'))
  assert.ok(values.includes('person:12'))
  // 99 is in the org but not in this manager's line — the server would 403
  // it, so the control must never show it.
  assert.ok(!values.includes('person:99'))
})

test('a person with no name is listed by email, never as a blank row', () => {
  const opt = whoseOptions(MANAGER, PEOPLE).find((o) => o.value === 'person:12')
  assert.equal(opt.label, 'bo@acme.test')
})

test('the default is the seat itself — arriving needs no choice made', () => {
  assert.equal(WHOSE_DEFAULT, 'everyone')
  assert.deepEqual(whoseParams(undefined), { whose: 'everyone', personId: null })
  assert.deepEqual(whoseParams('me'), { whose: 'me', personId: null })
  assert.deepEqual(whoseParams('team'), { whose: 'team', personId: null })
  assert.deepEqual(whoseParams('person:11'), { whose: 'person', personId: 11 })
})

test('a malformed person value falls back rather than sending nonsense', () => {
  assert.deepEqual(whoseParams('person:abc'), { whose: 'everyone', personId: null })
  assert.deepEqual(whoseParams('person:-1'), { whose: 'everyone', personId: null })
})

test('a selection the seat no longer supports is reconciled, not left to 403', () => {
  // A reorg moves the ground: a manager who lost their reports is holding a
  // "My team" that now means nothing, and a person filter for someone no
  // longer under them is a refusal they did not cause.
  assert.equal(reconcileWhose('person:11', MANAGER), 'person:11')
  assert.equal(reconcileWhose('person:99', MANAGER), WHOSE_DEFAULT)
  assert.equal(reconcileWhose('team', MANAGER), 'team')
  assert.equal(reconcileWhose('team', IC), WHOSE_DEFAULT)
  assert.equal(reconcileWhose('me', IC), WHOSE_DEFAULT)
  assert.equal(reconcileWhose(undefined, MANAGER), WHOSE_DEFAULT)
})

test('an empty table caused by the filter says so, and does not sell a fix', () => {
  // "Connect an agent" is the wrong answer when the agents are connected and
  // simply belong to someone else.
  assert.equal(whoseEmptyCopy('everyone'), null)
  assert.match(whoseEmptyCopy('me'), /No work of your own/)
  assert.match(whoseEmptyCopy('team'), /team/)
  assert.match(whoseEmptyCopy('person:11'), /this person/)
  assert.match(workTab, /whoseEmptyCopy\(whose\) && \(/)
  assert.match(workTab, /Widen Whose work above/)
})

// --- what the wire carries -------------------------------------------------

test('the default sends no filter at all — the seat already is the filter', () => {
  assert.match(api, /if \(whose && whose !== 'everyone'\) q\.set\('whose', whose\)/)
})

test('every page of a filtered list asks the same question', () => {
  // The seat filter runs before the cursor server-side, so page two without
  // the selection would be a different query and would double-count or skip.
  const loadMore = workTab.slice(workTab.indexOf('cursor: nextCursor'))
  assert.match(loadMore.slice(0, 400), /whoseParams\(whoseRef\.current\)/)
  assert.equal((workTab.match(/whoseParams\(whoseRef\.current\)/g) || []).length, 3)
})

test('changing the selection refetches instead of filtering rows already held', () => {
  assert.match(workTab, /}, \[whose, loadOverview, loadItems\]\)/)
  // ...but not on mount: the first load already used the default.
  assert.match(workTab, /firstWhose\.current/)
})

test('the overview is asked the same question as the table', () => {
  // A strip reading "12 open" above a table of 3 is worse than either
  // number alone.
  assert.match(workTab, /getWorkOverview\(whoseParams\(whoseRef\.current\)\)/)
})

// --- the desk contract -----------------------------------------------------

test('nothing about Whose work reaches the desk', () => {
  // Home's desk is `waiting_on_you`, resolved server-side against the
  // session identity. Home does not own this control and must not send it.
  const dash = readFileSync(new URL('../src/Dashboard.jsx', import.meta.url), 'utf8')
  const home = readFileSync(new URL('../src/home.js', import.meta.url), 'utf8')
  for (const [name, src] of [['Dashboard.jsx', dash], ['home.js', home]]) {
    assert.doesNotMatch(src, /whoseParams|whose:/, `${name} sends a Whose-work filter`)
  }
  // And the control lives on Work, which is where the choice belongs.
  assert.match(workTab, /className="work-whose"/)
})

test('the person picker is only fetched for a seat that has reports', () => {
  // An IC has no picker to fill and the chart is not free.
  assert.match(app, /const hasSubtree = \(me\?\.seat\?\.subtree_user_ids \|\| \[\]\)\.length > 0/)
  assert.match(app, /if \(!hasSubtree\) \{/)
  // And it fails soft — losing it costs the person picker, not the control.
  assert.match(app, /\.catch\(\(\) => \{\s*\n\s*if \(alive\) setOrgPeople\(\[\]\)/)
})
