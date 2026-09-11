// The Org surface, and the seat that decides who is offered it.
//
// The rule that needs a test rather than a habit: the client is NOT an
// authority on permissions. Every affordance on this page comes from a flag
// the server put on the role (can_edit / can_add_child), and the API refuses
// anything that slips through anyway. So the tests below assert two opposite
// things and both matter —
//
//   * the page hides what the seat says no to (a manager sees no Rename on
//     their own box), and
//   * when the seat is missing or broken the page WIDENS rather than blanks,
//     because a seat arrives late, can fail, and is absent for API-key
//     sessions. Failing closed there would make a working product look
//     broken, and it would buy nothing: the server still refuses.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { ALL_SURFACES, FULL_SEAT, hasReports, hasSurface, seatOf, showsTechnicalFolds } from '../src/seat.js'
import { resolveTab, visibleTabs } from '../src/tabs.js'
import { TAB_PATHS, parsePath } from '../src/route.js'
import {
  buildForest,
  flattenForest,
  widestRow,
  emptyChartCopy,
  peopleInRole,
  personLabel,
  roleActions,
  scopeSummary,
  unplacedMembers,
} from '../src/org.js'

const org = readFileSync(new URL('../src/Org.jsx', import.meta.url), 'utf8')
const graduate = readFileSync(new URL('../src/Graduate.jsx', import.meta.url), 'utf8')
const settings = readFileSync(new URL('../src/Settings.jsx', import.meta.url), 'utf8')
const app = readFileSync(new URL('../src/App.jsx', import.meta.url), 'utf8')
const onboarding = readFileSync(new URL('../src/Onboarding.jsx', import.meta.url), 'utf8')

// --- the seat --------------------------------------------------------------

test('a missing or broken seat widens to the full product, never blanks it', () => {
  // No /auth/me yet, an API-key session, a malformed payload: all the same.
  assert.deepEqual(seatOf(undefined).surfaces, ALL_SURFACES)
  assert.deepEqual(seatOf({}).surfaces, ALL_SURFACES)
  assert.deepEqual(seatOf({ seat: null }).surfaces, ALL_SURFACES)
  assert.deepEqual(seatOf({ seat: 'nonsense' }).surfaces, ALL_SURFACES)
  // An empty surface list is a person with no product — treat it as noise.
  assert.deepEqual(seatOf({ seat: { surfaces: [] } }).surfaces, ALL_SURFACES)
  assert.equal(seatOf(undefined).breadth, 'company')
  assert.equal(seatOf(undefined).depth, 'technical')
})

test('a real seat is used as given', () => {
  const seat = seatOf({ seat: { breadth: 'self', depth: 'glance', surfaces: ['Home', 'Work'] } })
  assert.deepEqual(seat.surfaces, ['Home', 'Work'])
  assert.equal(seat.breadth, 'self')
  assert.equal(hasSurface(seat, 'Work'), true)
  assert.equal(hasSurface(seat, 'Fleet'), false)
  assert.equal(showsTechnicalFolds(seat), false)
  assert.equal(showsTechnicalFolds(FULL_SEAT), true)
})

test('the Whose-work options only exist when there are reports to list', () => {
  assert.equal(hasReports({ subtree_user_ids: [] }), false)
  assert.equal(hasReports({ subtree_user_ids: [7] }), true)
})

// --- nav from surfaces -----------------------------------------------------

test('nav shows exactly the surfaces in the seat', () => {
  const ids = (s) => visibleTabs(s).map(([id]) => id)
  assert.deepEqual(ids(ALL_SURFACES), ['dashboard', 'fleet', 'work', 'org'])
  // A glance-only manager: no Fleet door.
  assert.deepEqual(ids(['Home', 'Work', 'Org']), ['dashboard', 'work', 'org'])
  // Ask and Connect are not tabs, so a seat naming them adds nothing.
  assert.deepEqual(ids(['Work', 'Ask', 'Connect']), ['work'])
})

test('the Fleet pane reads Agents in the nav, while the atom stays Fleet', () => {
  // The label is the only thing that changed. The pane id ('fleet'), the
  // route (/fleet) and the scope atom ('Fleet', stored in
  // scope_levels.surfaces) all stay — renaming those would be a schema
  // migration for a word.
  const byId = Object.fromEntries(visibleTabs(ALL_SURFACES))
  assert.equal(byId.fleet, 'Agents')
  assert.ok(!visibleTabs(ALL_SURFACES).some(([, label]) => label === 'Fleet'))
  assert.ok(ALL_SURFACES.includes('Fleet'))
  assert.equal(TAB_PATHS.fleet, '/fleet')
  // A seat still gates it by the atom's name, not the label.
  assert.deepEqual(visibleTabs(['Home', 'Work']).map(([id]) => id), ['dashboard', 'work'])
  assert.ok(visibleTabs(['Fleet']).some(([id]) => id === 'fleet'))
})

test('nothing a person reads calls the surface Fleet any more', () => {
  for (const f of ['App.jsx', 'Dashboard.jsx', 'Fleet.jsx', 'TrovisLanding.jsx']) {
    const src = readFileSync(new URL(`../src/${f}`, import.meta.url), 'utf8')
      .replace(/\/\*[\s\S]*?\*\//g, '')
      .replace(/^\s*\/\/.*$/gm, '')
    for (const m of src.matchAll(/>([^<>{}]+)</g)) {
      assert.doesNotMatch(m[1], /\bfleet\b/i, `${f} shows "${m[1].trim()}"`)
    }
    for (const m of src.matchAll(/(?:aria-label|title|placeholder|label)=["']([^"']+)["']/g)) {
      assert.doesNotMatch(m[1], /\bfleet\b/i, `${f} labels something "${m[1]}"`)
    }
  }
})

test('Org is a nav item now, and Team is gone', () => {
  assert.ok(visibleTabs(ALL_SURFACES).some(([, label]) => label === 'Org'))
  assert.ok(!visibleTabs(ALL_SURFACES).some(([, label]) => label === 'Team'))
  // Nav is built from the seat, not from the account type — a Business flag
  // decided this before, and that is the thing this ship replaced.
  assert.match(app, /visibleTabs\(seatOf\(me\)\.surfaces\)/)
  assert.doesNotMatch(app, /isBusiness/)
})

test('a seat that names nothing still leaves a way to navigate', () => {
  assert.deepEqual(visibleTabs([]).map(([id]) => id), ['dashboard', 'fleet', 'work', 'org'])
  assert.ok(visibleTabs(['Nonsense']).length >= 1)
  assert.equal(resolveTab('anything', { surfaces: ['Nonsense'] }), 'work')
})

// --- the chart -------------------------------------------------------------

const ROLES = [
  { id: 1, title: 'CEO', parent_role_id: null, user_ids: [10], can_edit: false, can_add_child: false },
  { id: 2, title: 'Support Manager', parent_role_id: 1, user_ids: [11], can_edit: false, can_add_child: true },
  { id: 3, title: 'Support IC', parent_role_id: 2, user_ids: [12], can_edit: true, can_add_child: true },
]
const MEMBERS = [
  { id: 10, name: 'Ada', email: 'ada@acme.test' },
  { id: 11, name: 'Bo', email: 'bo@acme.test' },
  { id: 12, name: null, email: 'cy@acme.test' },
  { id: 13, name: 'Dee', email: 'dee@acme.test' },
]

test('the chart is a real hierarchy, not a flat list with indents', () => {
  // Nested nodes, because the page draws connectors between a parent and
  // its children — an indented list cannot express a reporting line, which
  // is the one thing an org chart exists to show.
  const forest = buildForest(ROLES)
  assert.equal(forest.length, 1)
  assert.equal(forest[0].role.title, 'CEO')
  assert.equal(forest[0].children[0].role.title, 'Support Manager')
  assert.equal(forest[0].children[0].children[0].role.title, 'Support IC')
  assert.deepEqual(forest[0].children[0].children[0].children, [])
  assert.deepEqual(
    flattenForest(forest).map(({ role, depth }) => [role.title, depth]),
    [['CEO', 0], ['Support Manager', 1], ['Support IC', 2]],
  )
})

test('siblings are ordered by title, so boxes do not move between reloads', () => {
  const roles = [
    { id: 1, title: 'Root', parent_role_id: null, user_ids: [] },
    { id: 2, title: 'Zeta', parent_role_id: 1, user_ids: [] },
    { id: 3, title: 'Alpha', parent_role_id: 1, user_ids: [] },
  ]
  assert.deepEqual(
    buildForest(roles)[0].children.map((c) => c.role.title),
    ['Alpha', 'Zeta'],
  )
})

test('the widest row is known, so the chart can scroll instead of squeezing', () => {
  assert.equal(widestRow(buildForest(ROLES)), 1)
  const wide = [
    { id: 1, title: 'Root', parent_role_id: null, user_ids: [] },
    { id: 2, title: 'A', parent_role_id: 1, user_ids: [] },
    { id: 3, title: 'B', parent_role_id: 1, user_ids: [] },
    { id: 4, title: 'C', parent_role_id: 1, user_ids: [] },
  ]
  assert.equal(widestRow(buildForest(wide)), 3)
  assert.equal(widestRow([]), 0)
})

test('a role whose parent is not visible still renders, at the top', () => {
  // An IC sees their box and the chain above it; a manager sees their
  // subtree. Neither necessarily sees a role's parent — and a role that
  // silently vanished because its parent was filtered out would be worse
  // than one shown at the wrong indent.
  const subtreeOnly = ROLES.slice(1) // Support Manager's parent (CEO) missing
  const forest = buildForest(subtreeOnly)
  assert.equal(forest.length, 1)
  assert.equal(forest[0].role.title, 'Support Manager')
  assert.deepEqual(
    flattenForest(forest).map(({ role, depth }) => [role.title, depth]),
    [['Support Manager', 0], ['Support IC', 1]],
  )
})

test('a cycle in the data does not hang the page', () => {
  const cyclic = [
    { id: 1, title: 'A', parent_role_id: 2, user_ids: [] },
    { id: 2, title: 'B', parent_role_id: 1, user_ids: [] },
  ]
  const out = flattenForest(buildForest(cyclic))
  assert.ok(out.length <= 2)
})

test('empty and unknown inputs render nothing rather than throwing', () => {
  assert.deepEqual(buildForest(undefined), [])
  assert.deepEqual(buildForest([]), [])
  assert.deepEqual(peopleInRole(null, MEMBERS), [])
  // An id with no member row is dropped, not drawn as a blank chip.
  assert.deepEqual(
    peopleInRole({ user_ids: [10, 999] }, MEMBERS).map((m) => m.id),
    [10],
  )
  assert.equal(personLabel(MEMBERS[2]), 'cy@acme.test') // falls back to email
  assert.equal(personLabel(null), '')
})

test('people not on the chart are surfaced — they are the ones seeing everything', () => {
  assert.deepEqual(unplacedMembers(ROLES, MEMBERS).map((m) => m.id), [13])
  assert.deepEqual(unplacedMembers([], MEMBERS).length, 4)
  // And the page says so, because an unplaced person falls back to the full
  // seat: that is a fact about access, not a cosmetic gap.
  assert.match(org, /not\s*\n?\s*on the chart yet, so they see everything/)
})

test('a role with no scope level says it grants full access, not "none"', () => {
  assert.match(scopeSummary(null), /full access/i)
  assert.equal(scopeSummary({ breadth: 'subtree', depth: 'technical' }), 'Their team · technical detail')
  assert.equal(scopeSummary({ breadth: 'self', depth: 'glance' }), 'Own work · at a glance')
  assert.equal(scopeSummary({ breadth: 'company', depth: 'glance' }), 'Whole company · at a glance')
})

// --- affordances follow the server's two rungs -----------------------------

test('edit and add-child are separate rungs — a manager adds under a box they cannot edit', () => {
  // This is the whole reason roleActions exists. Collapsing the two flags
  // into one would either hide the Add button a manager needs on their own
  // box, or show a Rename that 403s.
  const ownBox = roleActions(ROLES[1]) // can_edit false, can_add_child true
  assert.equal(ownBox.canRename, false)
  assert.equal(ownBox.canDelete, false)
  assert.equal(ownBox.canSetScope, false)
  assert.equal(ownBox.canInvite, false)
  assert.equal(ownBox.canAddChild, true)

  const below = roleActions(ROLES[2])
  assert.equal(below.canRename, true)
  assert.equal(below.canAssignPeople, true)
  assert.equal(below.canInvite, true)
})

test('an IC is offered nothing on a chart they can only read', () => {
  const readOnly = roleActions({ id: 9, title: 'X' })
  assert.deepEqual(Object.values(readOnly), [false, false, false, false, false, false])
})

test('the empty chart tells a non-builder the truth instead of a dead button', () => {
  assert.match(emptyChartCopy(true).body, /Add the first role/)
  assert.match(emptyChartCopy(false).body, /org builder can set it up/)
})

test('Org never computes permissions itself — it renders what the server said', () => {
  // roleActions reads can_edit / can_add_child and nothing else. If this
  // page ever starts deriving rights from breadth, role titles or the member
  // list, the client becomes a second authority and the two will drift.
  const orgLogic = readFileSync(new URL('../src/org.js', import.meta.url), 'utf8')
  assert.match(orgLogic, /can_edit/)
  assert.match(orgLogic, /can_add_child/)
  assert.doesNotMatch(orgLogic, /org_builder\s*(&&|\|\||\?)/)
  // The builder-only control on the page is gated by the server's own flag
  // from /org/chart, not by anything inferred here.
  assert.match(org, /orgBuilder && \(/)
})

// --- Path A vs Path B ------------------------------------------------------

test('Path A gets no chart step; Path B gets one', () => {
  assert.match(
    onboarding,
    /isBusiness\s*\n?\s*\?\s*\['name', 'chart', 'connect', 'invite', 'done'\]\s*\n?\s*:\s*\['name', 'connect', 'done'\]/,
  )
})

test('every onboarding step can be skipped — a wizard must not trap anyone', () => {
  // Including the new chart step: an org that skips it lands with no roles,
  // which is the same state every existing org is in.
  assert.match(onboarding, /Skip — I’ll map it later/)
  assert.match(onboarding, /Skip setup/)
})

test('Path B invites carry the role, so the invitee arrives already seated', () => {
  assert.match(onboarding, /role_id: inviteRole === '' \? null : Number\(inviteRole\)/)
})

test('Path A is offered graduation, and it is an invitation rather than a wall', () => {
  assert.match(graduate, /Invite your company/)
  assert.match(graduate, /Not now/)
  // It must say the work survives — that is the actual worry someone has
  // before clicking, and it is true (account_id never changes).
  assert.match(graduate, /agents, work and connections all stay/i)
  assert.match(graduate, /api\.graduateOrg/)
})

test('graduation refreshes the WHOLE identity, not just the org', () => {
  // account_type, the new root role and the founder's seat all change at
  // once. Merging only the returned org left nav rendering a stale seat
  // until the next reload.
  assert.match(app, /const payload = await api\.validateSession\(\)/)
  assert.match(app, /onGraduated=\{refreshMe\}/)
  assert.doesNotMatch(app, /org: updated \|\| prev\.org/)
  assert.match(graduate, /await onGraduated\(res\)/)
})

// --- one invite truth ------------------------------------------------------

test('there is exactly one place to invite someone into this company', () => {
  const settings = readFileSync(new URL('../src/Settings.jsx', import.meta.url), 'utf8')
  // Settings used to carry its own invite form that knew nothing about roles.
  assert.doesNotMatch(settings, /api\.createInvite/)
  assert.doesNotMatch(settings, /InviteForm/)
  assert.match(settings, /Org page/)
  // And nothing in the product creates the old parallel directory rows.
  for (const f of ['Org.jsx', 'Onboarding.jsx', 'Settings.jsx', 'App.jsx']) {
    const src = readFileSync(new URL(`../src/${f}`, import.meta.url), 'utf8')
    assert.doesNotMatch(src, /createTeamMember/, `${f} still writes team_members`)
  }
})

test('the Org page speaks Work language, not internal vocabulary', () => {
  const strings = [
    ...[...org.matchAll(/>([^<>{}]+)</g)].map((m) => m[1]),
    ...[...org.matchAll(/(?:aria-label|title|placeholder)=["']([^"']+)["']/g)].map((m) => m[1]),
  ]
  for (const s of strings) {
    assert.doesNotMatch(s, /\bloop\b|\bhandoff\b|\bstation\b|\bsubtree\b|\bbreadth\b/i, `Org shows "${s.trim()}"`)
  }
})

// --- the hotfix: graduation has to be REACHABLE ----------------------------
//
// The first version put the graduation CTA only on the Org page. That is the
// one surface a solo user has no reason to open — the whole premise of Path A
// is that they never built a chart — so the invitation to grow was, in
// practice, invisible. QA found it on a real Individual workspace. These
// tests pin the reachability, not the wording.

test('an Individual is offered graduation without going to Org', () => {
  // On Home, above the day's work.
  assert.match(app, /showGraduate && \(/)
  assert.match(app, /<GraduateCard\s+variant="banner"/)
  // And in Settings, for someone who went looking there instead.
  assert.match(settings, /<GraduateCard/)
  assert.match(settings, /!isBusiness && user/)
})

test('both entry points are the same component calling the same API', () => {
  // Two CTAs that each did their own thing would be two features to keep in
  // step, which is how the Org-only version drifted out of reach in the
  // first place.
  for (const src of [app, settings, org]) {
    assert.match(src, /from '\.\/Graduate\.jsx'/)
  }
  assert.equal((graduate.match(/api\.graduateOrg/g) || []).length, 1)
})

test('the offer is only for Individual workspaces, and only for a person', () => {
  assert.match(app, /me\?\.org\?\.account_type === 'individual'/)
  // An API-key session has no person to invite anyone.
  assert.match(app, /Boolean\(me\?\.user\)/)
})

test('declining sticks — an offer that reappears after "Not now" is a nag', () => {
  assert.match(app, /trovis_graduate_dismissed/)
  assert.match(app, /onDismiss=\{dismissGraduate\}/)
  // localStorage can throw in private mode; dismissing must not crash Home.
  assert.match(app, /catch \{\s*\/\* private mode/)
})

test('every tab in the nav has a URL — a tab that rewrites to / is not linkable', () => {
  // Org was added to the nav but never to route.js, so clicking it pushed
  // "/" and the address bar disagreed with the screen.
  for (const [id] of visibleTabs(ALL_SURFACES)) {
    assert.equal(
      typeof TAB_PATHS[id],
      'string',
      `nav tab "${id}" has no path in route.js`,
    )
    assert.equal(parsePath(TAB_PATHS[id]).tab, id, `"${id}" does not round-trip`)
  }
})

test('a person reads Home, never Dashboard — on every surface, not just the shell', () => {
  // The original rule only swept App/Dashboard/CostPage/WorkFeedPage, which
  // is how "Go to dashboard" and "Continue to dashboard" survived in the two
  // screens a new user sees first.
  for (const [name, src] of [
    ['Onboarding.jsx', onboarding],
    ['Login.jsx', readFileSync(new URL('../src/Login.jsx', import.meta.url), 'utf8')],
    ['Settings.jsx', settings],
    ['Org.jsx', org],
    ['Graduate.jsx', graduate],
  ]) {
    const stripped = src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')
    for (const m of stripped.matchAll(/>([^<>{}]+)</g)) {
      assert.doesNotMatch(m[1], /\bdashboard\b/i, `${name} shows "${m[1].trim()}"`)
    }
  }
})

// --- naming someone who has not signed in ---------------------------------

test('the invite form can name a person, not just address them', () => {
  // Work gets handed to people before they have a login. A named invite is
  // what makes that handoff read as "Sarah Chen" rather than "a human", so
  // the name field has to be on the one invite form the product has.
  assert.match(org, /placeholder="Their name \(optional\)"/)
  assert.match(org, /name: name\.trim\(\) \|\| null/)
  // Optional, and honestly labelled: an invite with no name still works.
  assert.match(org, /\(optional\)/)
})

test('a pending invite is listed by name once it has one', () => {
  assert.match(org, /\{i\.display_name \|\| i\.email\}/)
})

test('nothing in the product writes the old directory', () => {
  for (const f of ['Org.jsx', 'Onboarding.jsx', 'Settings.jsx', 'App.jsx', 'api.js']) {
    const src = readFileSync(new URL(`../src/${f}`, import.meta.url), 'utf8')
    assert.doesNotMatch(src, /createTeamMember|post\(.\/team/, `${f} writes team_members`)
  }
})

// --- the chart is drawn, not indented ---------------------------------------

test('reporting lines are drawn, and the old indented list is gone', () => {
  // An indented list cannot show a reporting line. The chart is nested
  // <ul>/<li> with connector pseudo-elements — no layout library, because a
  // diagram people mostly read does not need a canvas engine.
  assert.match(org, /className="oc-level is-root"/)
  assert.match(org, /<ChartNode/)
  assert.doesNotMatch(org, /paddingLeft: `\$\{12 \+ depth \* 18\}px`/)
  const css = readFileSync(new URL('../src/styles.css', import.meta.url), 'utf8')
  for (const rule of ['.oc-level::before', '.oc-node::before', '.oc-cell::before']) {
    assert.ok(css.includes(rule), `missing connector rule ${rule}`)
  }
})

test('a long tail of leaves runs down the page instead of off the side', () => {
  // One rule: children fan out horizontally UNLESS every one of them is a
  // leaf. Without it a manager with twelve reports makes the chart wider
  // than any screen, and the shape of the org is what the chart is for.
  assert.match(org, /children\.every\(\(c\) => c\.children\.length === 0\)/)
  assert.match(org, /is-stacked/)
})

test('the chart gets the page width, with the role detail under it', () => {
  const css = readFileSync(new URL('../src/styles.css', import.meta.url), 'utf8')
  // Side by side, the chart was squeezed into a column narrow enough to
  // clip whole branches.
  assert.doesNotMatch(css, /\.org-body \{[^}]*grid-template-columns/s)
  assert.match(css, /\.org-body \{[^}]*flex-direction: column/s)
  // And it scrolls sideways rather than shrinking boxes past readability.
  assert.match(css, /\.org-chart-wrap \{[^}]*overflow-x: auto/s)
})

test('a role nobody is in says so', () => {
  // A vacancy is a real state. Rendering nothing would read as a bug.
  assert.match(org, /people\.length === 0 && <span className="oc-person is-vacant">Open<\/span>/)
})
