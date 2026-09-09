// Job detail — turning a work item's event timeline into the two things a
// person actually wants to see. Pure, so both are testable without a renderer.
//
//   steps    the ROUTE the job takes: Agent → You → Stripe. Consecutive events
//            with the same actor collapse into one step, because "the agent did
//            four things in a row" is one leg of the journey, not four.
//   history  the LOG: the last few timestamped events, newest last.
//
// They are deliberately different cuts. Printing the event list twice under
// two headings would be a table pretending to be a process.

/** Human-readable label for an actor kind. `tool` covers SaaS destinations
 *  (Stripe, HubSpot) — the wire has no `saas` kind and this PR does not add one. */
export const ACTOR_LABEL = { human: 'Person', agent: 'Agent', tool: 'Tool' }

/** Steps shown before the route is truncated from the front (oldest dropped). */
export const MAX_STEPS = 6

/** Events in the short history. */
export const MAX_HISTORY = 5

function actorOf(entry) {
  const a = entry?.actor || {}
  const kind = a.kind === 'human' || a.kind === 'tool' ? a.kind : 'agent'
  return { kind, name: String(a.name || '').trim() }
}

/** Two steps are the same leg when the same party still holds the work. */
function sameActor(a, b) {
  return a.kind === b.kind && a.name.toLowerCase() === b.name.toLowerCase()
}

/**
 * The route: consecutive same-actor events collapsed, oldest first.
 *
 * `isYou` marks the step the signed-in person is holding, so the pane can
 * highlight it rather than making the reader match names.
 *
 * Returns `{ steps, hidden }` — `hidden` is how many older legs were dropped,
 * so the pane can say so instead of silently starting mid-journey.
 */
export function processSteps(timeline, { holder = null, status = null, max = MAX_STEPS } = {}) {
  const legs = []
  for (const entry of timeline || []) {
    if (!entry || !entry.text) continue
    const actor = actorOf(entry)
    const last = legs[legs.length - 1]
    if (last && sameActor(last.actor, actor)) {
      last.text = entry.text
      last.at = entry.at || last.at
      continue
    }
    legs.push({ actor, text: entry.text, at: entry.at || null })
  }

  // The party holding it right now is the last leg. When the item is still
  // open and the holder disagrees with the timeline (an aging wait whose
  // handoff never produced another event), trust the holder.
  if (holder?.name && status && status !== 'done') {
    const current = actorOf({ actor: holder })
    const last = legs[legs.length - 1]
    if (!last || !sameActor(last.actor, current)) {
      legs.push({ actor: current, text: null, at: null })
    }
  }

  const hidden = Math.max(0, legs.length - max)
  const steps = legs.slice(hidden).map((leg, i, arr) => ({
    ...leg,
    isCurrent: i === arr.length - 1 && status !== 'done',
    isYou: i === arr.length - 1 && status === 'waiting_on_you',
  }))
  return { steps, hidden }
}

/** The log: newest-last, capped. Entries without text are dropped. */
export function shortHistory(timeline, { max = MAX_HISTORY } = {}) {
  const rows = (timeline || []).filter((e) => e && e.text)
  return rows.slice(Math.max(0, rows.length - max))
}

/**
 * Whether this item is awaiting a decision from the signed-in person, and can
 * therefore offer judgment CTAs.
 *
 * Both halves are required: the status says it is on you, and the server gave
 * us the handoff row to resolve against. Without the id, Approve and Send back
 * have nothing to call — so we show no buttons rather than dead ones.
 */
export function canDecide(detail) {
  return Boolean(detail && detail.status === 'waiting_on_you' && detail.awaiting_handoff_event_id)
}

// --- the technical fold ----------------------------------------------------
//
// The last section of the pane is the door out of Work: someone who needs the
// machine, not the process, reads these lines and then leaves for the agent's
// own page. So each line is English, each is optional, and every one of them
// is dropped when the record does not have it. An absent duration is absent;
// a $0.00 is not a cost; a failure with no message says only that it failed.

/** "820ms" / "1.4s" / "2m 05s" — null when the run carried no duration. */
export function runDuration(ms) {
  // Guard the coercion, not just the result: Number(null) and Number('') are
  // both 0, so a missing duration would otherwise print as a measured "0ms".
  if (ms === null || ms === undefined || ms === '') return null
  const n = Number(ms)
  if (!Number.isFinite(n) || n <= 0) return null
  if (n < 1000) return `${Math.round(n)}ms`
  const s = n / 1000
  if (s < 60) return `${s < 10 ? s.toFixed(1) : Math.round(s)}s`
  const m = Math.floor(s / 60)
  return `${m}m ${String(Math.round(s - m * 60)).padStart(2, '0')}s`
}

/**
 * "$0.004" / "$1.20" — null when there is nothing to report.
 *
 * Zero is null on purpose. A cost column that reads $0.00 on every row looks
 * like a measured zero when it is really "we were not told", and the two mean
 * very different things to someone deciding whether an agent is expensive.
 */
export function runCost(usd) {
  const n = Number(usd)
  if (!Number.isFinite(n) || n <= 0) return null
  return n < 0.01 ? `$${n.toFixed(4)}` : `$${n.toFixed(2)}`
}

/**
 * The one line a failed run gets: "stripe — Card declined".
 *
 * Only for runs that actually failed, and only when the run said something.
 * Returns null otherwise — a failure with no message is reported by the
 * Error tag alone, because writing a reason we were never given is the
 * failure mode this whole pane is built to avoid.
 */
export function runErrorLine(run) {
  if (!run || !run.errored) return null
  const msg = String(run.error || '').trim()
  if (!msg) return null
  const tool = String(run.tool || '').trim()
  return tool ? `${tool} — ${msg}` : msg
}

/**
 * Where a run's agent name points, or null when it points nowhere.
 *
 * Same rule as everywhere else: `service_name` is the route and `agent`
 * is the label. Without a route the name stays text — a button that opens
 * a 404 is worse than a name you cannot click.
 */
export function runAgentRoute(run) {
  const service = String(run?.service_name || '').trim()
  if (!service) return null
  return [service, String(run?.agent_id || '').trim() || 'main']
}

/** The question the Ask pill opens with for this job. */
export function askPrompt(detail) {
  const title = String(detail?.title || '').trim()
  if (!title) return "What's waiting on me?"
  if (detail?.status === 'stuck') return `Why is "${title}" stuck?`
  return `What do I need to know about "${title}"?`
}
