// Assigning an agent's owner — the control that closes the loop on
// Whose work.
//
// The API has supported the assignment since the ownership ship; nothing in
// the product ever called it. That was survivable while ownership was a
// label. It stopped being survivable when Whose work started attributing a
// row to a person two ways — the agent that ran it is owned by them, or the
// work is waiting on them. With no way to set an owner, the first leg is
// inert: a manager's "My team" shows only work currently waiting on a
// report, which is a fraction of it, and the feature ships correct and
// reads as broken.
//
// So these tests are about one thing: there is exactly one place to say who
// owns an agent, it tells the truth when nobody does, and it sends the
// user-shaped assignment the server now expects.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const detail = readFileSync(new URL('../src/AgentDetail.jsx', import.meta.url), 'utf8')
const api = readFileSync(new URL('../src/api.js', import.meta.url), 'utf8')
const app = readFileSync(new URL('../src/App.jsx', import.meta.url), 'utf8')

test('the product can assign an owner at all', () => {
  // The regression this whole PR exists to prevent: wrappers with no caller.
  assert.match(api, /setAgentOwner:/)
  assert.match(api, /removeAgentOwner:/)
  assert.match(detail, /api\.setAgentOwner\(/)
  assert.match(detail, /api\.removeAgentOwner\(/)
})

test('the assignment is user-shaped, matching what the server takes now', () => {
  // team_member_id is the legacy id; owners are org members.
  const setOwner = api.slice(api.indexOf('setAgentOwner:'), api.indexOf('removeAgentOwner:'))
  assert.match(setOwner, /user_id: userId/)
  assert.doesNotMatch(setOwner, /team_member_id/)
  assert.match(detail, /userId: Number\(value\)/)
})

test('an unowned agent says so instead of naming whoever is looking', () => {
  // It used to fall back to account.userName, so an agent nobody owned read
  // "Owner: you". Harmless-looking, and wrong: it hid exactly the state a
  // manager needs to find and fix before their Work list fills in.
  assert.doesNotMatch(detail, /summary\.owner_name \|\| account\?\.userName/)
  assert.match(detail, /const owner = summary\.owner_name \|\| null/)
  assert.match(detail, /\{owner \|\| 'Unassigned'\}/)
})

test('unassigning is possible, not just re-assigning', () => {
  // An owner who leaves the team is worse than no owner: their name sits on
  // work that is nobody's.
  assert.match(detail, /<option value="">Unassigned<\/option>/)
  assert.match(detail, /if \(value === ''\) \{/)
})

test('the picker lists org members, and fails soft to an empty list', () => {
  assert.match(detail, /api\s*\n?\s*\.getMembers\(\)/)
  assert.match(detail, /\.catch\(\(\) => setMembers\(\[\]\)\)/)
  // Loaded when the control is opened, not on every agent page view.
  assert.match(detail, /if \(!editing \|\| members !== null\) return/)
})

test('an API-key session is shown the owner but offered no picker', () => {
  // No person signed in means nobody to pick, and no member list to pick
  // from. It still reads the owner — that is information, not an action.
  assert.match(detail, /if \(!canAssign\) \{/)
  assert.match(detail, /canAssign=\{Boolean\(account\?\.userId\)\}/)
  assert.match(app, /userId: me\?\.user\?\.id \?\? null/)
})

test('changing the owner refreshes the page that shows it', () => {
  // The summary carries owner_name; without a reload the header would keep
  // showing the person who used to own it.
  assert.match(detail, /onOwnerChanged=\{\(\) => setReloadKey\(\(k\) => k \+ 1\)\}/)
  assert.match(detail, /if \(onChanged\) onChanged\(\)/)
})

test('a refusal is shown, not swallowed', () => {
  // Cross-tenant ids are a 400 from the server. Silently doing nothing
  // would look like the click missed.
  assert.match(detail, /setError\(e\?\.message \|\| 'Could not change the owner'\)/)
  assert.match(detail, /\{error && <span style=\{\{ color: C\.err \}\}>\{error\}<\/span>\}/)
})

test('there is still only one place people are managed', () => {
  // The picker reads the org member list; it must not grow into a second
  // way to create people.
  assert.doesNotMatch(detail, /createTeamMember|createInvite|\/org\/invites/)
})
