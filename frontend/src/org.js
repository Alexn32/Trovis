// Org chart presentation logic, kept out of the component so it can be
// tested without a DOM.
//
// The server sends a flat list of roles, already filtered to what the caller
// may see. Two consequences shape everything here:
//
//   1. A visible role's parent may be missing. An IC sees their own box and
//      the chain above it; a manager sees their subtree. Neither necessarily
//      sees a role's parent — so any role whose parent isn't in the list must
//      still render, at the top, or it vanishes from the page entirely.
//   2. can_edit / can_add_child come from the server per role. This file
//      never computes them. Greying out a button the seat says no to is a
//      courtesy; the API refuses it regardless.

// Build a real hierarchy from the flat role list: nested nodes, so the page
// can draw a chart with connectors rather than an indented list.
//
// Roots are roles with no parent AND roles whose parent isn't visible to
// this caller (see 2 above). Children are ordered by title so the chart is
// stable across reloads — a box that moves between refreshes is a box people
// stop trusting.
export function buildForest(roles) {
  const list = Array.isArray(roles) ? roles : []
  const byId = new Map(list.map((r) => [r.id, r]))
  const children = new Map()
  const roots = []
  for (const role of list) {
    const parent = role.parent_role_id
    if (parent == null || !byId.has(parent)) {
      roots.push(role)
    } else {
      if (!children.has(parent)) children.set(parent, [])
      children.get(parent).push(role)
    }
  }
  const byTitle = (a, b) => (a.title || '').localeCompare(b.title || '')
  const seen = new Set()
  const build = (role, depth) => {
    // A cycle would recurse forever. The server rejects them on write, but a
    // page must not hang on data it merely received.
    if (seen.has(role.id)) return null
    seen.add(role.id)
    const kids = (children.get(role.id) || [])
      .sort(byTitle)
      .map((k) => build(k, depth + 1))
      .filter(Boolean)
    return { role, depth, children: kids }
  }
  return roots.sort(byTitle).map((r) => build(r, 0)).filter(Boolean)
}

// The same hierarchy flattened, parents before their children. Useful for
// counting, for keyboard order, and for tests that care about shape rather
// than markup.
export function flattenForest(forest) {
  const out = []
  const walk = (nodes) => {
    for (const n of nodes || []) {
      out.push({ role: n.role, depth: n.depth })
      walk(n.children)
    }
  }
  walk(forest)
  return out
}

// How wide the chart is at its widest point. The page uses it to decide
// whether the chart needs to scroll sideways rather than guessing from a
// breakpoint.
export function widestRow(forest) {
  const perDepth = new Map()
  for (const { depth } of flattenForest(forest)) {
    perDepth.set(depth, (perDepth.get(depth) || 0) + 1)
  }
  return perDepth.size ? Math.max(...perDepth.values()) : 0
}

// Everyone standing in a role, resolved against the member list the chart
// came with. An id with no member row is dropped rather than rendered as a
// blank chip.
export function peopleInRole(role, members) {
  const byId = new Map((members || []).map((m) => [m.id, m]))
  return (role?.user_ids || []).map((id) => byId.get(id)).filter(Boolean)
}

export function personLabel(member) {
  if (!member) return ''
  return member.name || member.email || `Member ${member.id}`
}

// People with a login who are not standing in any box yet. These are the org
// members the chart hasn't placed — and an unplaced person falls back to the
// full seat, so the page surfaces them rather than letting them sit unseen.
export function unplacedMembers(roles, members) {
  const placed = new Set()
  for (const r of roles || []) for (const id of r.user_ids || []) placed.add(id)
  return (members || []).filter((m) => !placed.has(m.id))
}

// One-line summary of what a scope level grants, for the role detail panel.
export function scopeSummary(level) {
  if (!level) return 'No seat assigned — falls back to full access'
  const breadth = {
    self: 'Own work',
    subtree: 'Their team',
    company: 'Whole company',
  }[level.breadth] || level.breadth
  const depth = level.depth === 'technical' ? 'technical detail' : 'at a glance'
  return `${breadth} · ${depth}`
}

// Which actions the page offers on a role. Purely a rendering decision — the
// server re-checks all of it. `canEdit` is "change or remove THIS box";
// `canAddChild` is "hang a new box under it". They are different rungs: a
// manager may add under their own box but not edit it, so a page that
// collapsed them into one flag would either hide the add button they need or
// show an edit button that 403s.
export function roleActions(role) {
  return {
    canRename: Boolean(role?.can_edit),
    canDelete: Boolean(role?.can_edit),
    canSetScope: Boolean(role?.can_edit),
    canAssignPeople: Boolean(role?.can_edit),
    canInvite: Boolean(role?.can_edit),
    canAddChild: Boolean(role?.can_add_child),
  }
}

// What the empty chart should say, given who is looking. A builder gets a
// call to action; anyone else gets the truth, because they cannot fix it.
export function emptyChartCopy(canEditChart) {
  return canEditChart
    ? {
        title: 'No chart yet',
        body: 'Add the first role to start mapping who reports to whom.',
      }
    : {
        title: 'No chart yet',
        body: 'Nobody has mapped this company yet. An org builder can set it up.',
      }
}
