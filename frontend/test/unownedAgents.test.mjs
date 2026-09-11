// The unowned-agent signal on the Agents roster.
//
// Background, because the count only makes sense against it: Whose work
// attributes a row to a person two ways — the agent that ran it is owned by
// them, or the work is waiting on them. Ownership became assignable only
// recently, and agents are derived from telemetry rather than declared, so in
// every existing org nearly every agent is unowned. A manager picks "My team"
// and sees a thin list, with nothing anywhere telling them why.
//
// The roster is the only page that knows which agents exist, so this is where
// the gap gets named. These tests pin what counts as a gap — the part that is
// easy to get subtly wrong — and that the page actually renders it.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import {
  unownedAgents,
  unownedCount,
  groupNeedsOwner,
  groupsNeedingOwner,
  unownedLabel,
  unownedEmptyCopy,
} from '../src/unowned.js'

const fleet = readFileSync(new URL('../src/Fleet.jsx', import.meta.url), 'utf8')
const css = readFileSync(new URL('../src/styles.css', import.meta.url), 'utf8')

// Shapes match /agents: a group with a nested `agents` array.
const owned = (id, name) => ({ agent_id: id, owner_name: name, owner_role: 'VP' })
const bare = (id) => ({ agent_id: id, owner_name: null })
const group = (service, agents, extra = {}) => ({
  service_name: service,
  agents,
  owner_name: agents[0]?.owner_name ?? null,
  ...extra,
})

// ---------------------------------------------------------------------------
// What counts
// ---------------------------------------------------------------------------

test('an agent with no owner is counted', () => {
  assert.equal(unownedCount([group('billing', [bare('main')])]), 1)
})

test('an agent with an owner is not', () => {
  assert.equal(unownedCount([group('billing', [owned('main', 'Ada')])]), 0)
})

test('the count is over sub-agents, not instances', () => {
  // Ownership is assigned per sub-agent, so an instance with six gaps is six
  // trips to the picker. Reporting it as "1 agent" would understate the work
  // and then fail to go down when one of them is assigned.
  const g = group('gateway', [bare('a'), bare('b'), owned('c', 'Ada')])
  assert.equal(unownedCount([g]), 2)
})

test('a partly-owned instance still needs an owner', () => {
  // The bug to avoid: flagging only instances where NOBODY owns anything,
  // which silently hides every real gap inside a large gateway.
  assert.equal(groupNeedsOwner(group('gateway', [owned('a', 'Ada'), bare('b')])), true)
})

test('an owner with no resolvable name is no owner', () => {
  // The server drops an assignment whose person is gone from both users and
  // the legacy directory, so a row arriving with owner_id but no owner_name
  // is a dangling assignment — nobody is actually on it.
  const g = group('billing', [{ agent_id: 'main', owner_id: 9, owner_name: null }])
  assert.equal(unownedCount([g]), 1)
})

test('an empty roster counts nothing', () => {
  assert.equal(unownedCount([]), 0)
  assert.equal(unownedCount(undefined), 0)
})

// ---------------------------------------------------------------------------
// Locked agents are excluded
// ---------------------------------------------------------------------------

test('a locked sub-agent is not counted', () => {
  // Locked = past the plan's agent limit. The card does not open, so the
  // picker cannot be reached: counting it prints a number nobody can drive
  // to zero, and the toggle would never disappear.
  const g = group('gateway', [bare('a'), { agent_id: 'b', owner_name: null, locked: true }])
  assert.equal(unownedCount([g]), 1)
  assert.deepEqual(unownedAgents([g]), [{ service_name: 'gateway', agent_id: 'a' }])
})

test('a wholly locked instance is skipped before its sub-agents', () => {
  const g = group('gateway', [bare('a'), bare('b')], { locked: true })
  assert.equal(unownedCount([g]), 0)
  assert.equal(groupNeedsOwner(g), false)
})

test('an instance whose only gaps are locked is not flagged', () => {
  const g = group('gateway', [
    owned('a', 'Ada'),
    { agent_id: 'b', owner_name: null, locked: true },
  ])
  assert.equal(groupNeedsOwner(g), false)
})

// ---------------------------------------------------------------------------
// A payload without the nested array
// ---------------------------------------------------------------------------

test('a group with no agents array falls back to the group itself', () => {
  // Better to read the group's own owner fields than to count zero and
  // quietly report a clean fleet.
  assert.equal(unownedCount([{ service_name: 'billing' }]), 1)
  assert.equal(unownedCount([{ service_name: 'billing', owner_name: 'Ada' }]), 0)
})

// ---------------------------------------------------------------------------
// Filtering
// ---------------------------------------------------------------------------

test('the filter keeps only instances with a gap', () => {
  const a = group('billing', [bare('main')])
  const b = group('payroll', [owned('main', 'Ada')])
  assert.deepEqual(
    groupsNeedingOwner([a, b]).map((g) => g.service_name),
    ['billing'],
  )
})

test('a filtered instance is kept whole', () => {
  // Stripping the owned siblings would misrepresent the instance the user is
  // about to click into — and change its span and cost totals with it.
  const g = group('gateway', [owned('a', 'Ada'), bare('b')])
  const [only] = groupsNeedingOwner([g])
  assert.equal(only.agents.length, 2)
  assert.equal(only, g)
})

test('the filter never invents rows', () => {
  assert.deepEqual(groupsNeedingOwner([]), [])
  assert.deepEqual(groupsNeedingOwner(null), [])
})

// ---------------------------------------------------------------------------
// Copy
// ---------------------------------------------------------------------------

test('the label agrees with itself on one', () => {
  assert.equal(unownedLabel(1), '1 agent needs an owner')
  assert.equal(unownedLabel(4), '4 agents need an owner')
})

test('no gap, no label', () => {
  assert.equal(unownedLabel(0), null)
})

test('an empty filtered view says the fleet is covered, not that it is empty', () => {
  assert.match(unownedEmptyCopy(3).title, /has an owner/)
  assert.match(unownedEmptyCopy(0).title, /No agents/)
})

// ---------------------------------------------------------------------------
// The page renders it
// ---------------------------------------------------------------------------

test('Fleet uses the shared logic rather than re-deriving it', () => {
  assert.match(fleet, /from '\.\/unowned\.js'/)
  assert.match(fleet, /unownedCount\(groups\)/)
  assert.match(fleet, /groupsNeedingOwner\(groups\)/)
})

test('a card with no owner says so where the owner line would be', () => {
  assert.match(fleet, /owner-tag is-unowned/)
  assert.match(fleet, /No owner yet/)
})

test('the toggle is a real pressed-state control, not a link', () => {
  assert.match(fleet, /aria-pressed=\{onlyUnowned\}/)
  assert.match(fleet, /setOnlyUnowned/)
})

test('the toggle hides itself when there is no gap', () => {
  // A control whose only possible reading is "0" is noise on every healthy
  // fleet, which is most of them once this lands.
  assert.match(fleet, /needOwner > 0 &&/)
})

test('the filtered list is what reaches the grid', () => {
  // The regression: filtering the summary counts too, so the headline
  // "Total spans" changes when you toggle a view filter.
  assert.match(fleet, /groups=\{shown\}/)
  assert.match(fleet, /total: totalInstances/)
})

test('an empty filtered list does not offer + Add Agent', () => {
  assert.match(fleet, /emptyCopy=\{onlyUnowned \? unownedEmptyCopy\(groups\.length\) : null\}/)
})

test('a collapsed instance still shows its gap', () => {
  // Expanded, each sub-agent card says it; collapsed, the band is the only
  // thing on screen.
  assert.match(fleet, /!expanded && unownedHere > 0/)
  assert.match(fleet, /instance-band-unowned/)
})

test('the marker is styled in theme variables, not hardcoded color', () => {
  assert.match(css, /\.owner-tag\.is-unowned\s*\{[^}]*var\(--warning\)/)
  assert.match(css, /\.unowned-toggle\s*\{/)
  assert.match(css, /\.instance-band-unowned\s*\{[^}]*var\(--warning\)/)
})

test('the header counts what is on screen while filtered', () => {
  // Leaving it at the fleet total prints a number the eye disproves by
  // scrolling: "Agents · 5" over four cards.
  assert.match(fleet, /Agents · \{shown\.length\} of \{totalInstances\}/)
})
