// Which agents have nobody's name on them.
//
// Ownership is what Whose work on Work is built out of: "my team's work" means
// work run by agents my reports own. But agents are *derived* from telemetry —
// they arrive the moment a span does, owned by no one — and ownership only
// became assignable recently. So the ordinary state of a fresh fleet is that
// every agent is unowned, and the ordinary symptom is that Whose work looks
// broken: a manager picks "My team" and sees nothing.
//
// The roster is the only page that can answer that, because it is the only
// page that knows which agents exist. This file decides what counts as
// unowned; Fleet.jsx renders it.
//
// Two rules that are easy to get wrong:
//
//   1. Ownership is assigned per SUB-AGENT, not per instance. A gateway with
//      six sub-agents can have five owners and a gap. So the count is over
//      sub-agents, and a group is flagged when any one of its sub-agents is
//      unowned — not only when all of them are.
//
//   2. A LOCKED agent is excluded. Locked means past the plan's agent limit:
//      the card won't open, so there is no picker to reach and no way to act.
//      Counting it would print a number nobody can drive to zero.

// An owner the viewer can see is an owner with a NAME. The server drops an
// assignment whose person is gone from both `users` and the legacy directory
// (see `_fleet_sidecar`), so a missing name is genuinely "nobody is on this",
// not "owned by someone we didn't fetch".
function hasOwner(record) {
  return Boolean(record && record.owner_name)
}

// The sub-agents of a group, normalized. An older or partial payload may carry
// no `agents` array at all; treat the group itself as its one agent so the
// roster still reports honestly rather than silently counting zero.
function subAgents(group) {
  if (!group) return []
  const list = Array.isArray(group.agents) ? group.agents : null
  if (list && list.length) return list
  return [
    {
      agent_id: 'main',
      owner_name: group.owner_name,
      owner_role: group.owner_role,
      locked: group.locked,
    },
  ]
}

// Every sub-agent that needs an owner, flattened across the roster. Returns
// {service_name, agent_id} so a caller can name them, not just count them.
export function unownedAgents(groups) {
  const out = []
  for (const group of Array.isArray(groups) ? groups : []) {
    // A wholly locked instance is locked at the group level too; skip it
    // before looking inside, or its sub-agents each read as actionable.
    if (group?.locked) continue
    for (const sa of subAgents(group)) {
      if (sa?.locked) continue
      if (hasOwner(sa)) continue
      out.push({
        service_name: group.service_name,
        agent_id: sa?.agent_id || 'main',
      })
    }
  }
  return out
}

export function unownedCount(groups) {
  return unownedAgents(groups).length
}

// Does this card deserve the marker? True when at least one of its reachable
// sub-agents is unowned.
export function groupNeedsOwner(group) {
  if (!group || group.locked) return false
  return subAgents(group).some((sa) => !sa?.locked && !hasOwner(sa))
}

// The roster filtered to cards with a gap. Groups are kept WHOLE — a gateway
// with one unowned sub-agent still shows its owned siblings, because hiding
// them would misrepresent the instance the user is about to click into.
export function groupsNeedingOwner(groups) {
  return (Array.isArray(groups) ? groups : []).filter(groupNeedsOwner)
}

// The toggle's label. Count is sub-agents, which is the unit ownership is
// assigned in, so the number matches how many times you'll use the picker.
export function unownedLabel(count) {
  if (!count) return null
  return count === 1 ? '1 agent needs an owner' : `${count} agents need an owner`
}

// What the filtered view says when it is empty — reachable two ways: every
// agent already has an owner (worth saying out loud), or the roster is empty.
export function unownedEmptyCopy(totalGroups) {
  return totalGroups
    ? {
        title: 'Every agent has an owner',
        body: 'Whose work on Work can attribute all of them.',
      }
    : {
        title: 'No agents yet',
        body: 'Connect an agent and it will show up here.',
      }
}
