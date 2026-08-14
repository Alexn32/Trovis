# Changelog

## 0.6.0

**This release changes what your loops look like.** It is a behavior change
to the permanent record, not a bug fix. Read the operator note below before
upgrading.

### What you'll see after upgrading

Two things change on your dashboard, and both are the record becoming more
accurate rather than anything breaking.

**Your conversations will start showing up in Stuck.** Until now, the plugin
closed a loop as "done" every time your agent finished replying. That was an
assertion it couldn't actually make — the agent had finished its *turn*, not
the *work*, and it had no idea whether you were going to reply. So a
conversation you left overnight was recorded as a series of completed tasks
when really it was one piece of work waiting on a person. Now, when your
agent finishes a turn, the loop is marked as waiting on the human it's
talking to. If nobody replies within four hours it appears in your Stuck
view. That is not a malfunction: it is real work, genuinely waiting on a
real person, and it was always waiting — you just couldn't see it. If you
chat with an agent in the evening and pick it up the next morning, expect to
find it in Stuck when you sit down.

**Your loop counts will drop sharply, and that's expected.** A conversation
is now one loop instead of one loop per reply. A ten-message exchange that
used to show as ten completed loops now shows as one loop that moved back
and forth between your agent and you ten times. Anywhere you were reading
loop counts as a volume number — "runs today", workflow activity — those
figures will fall by roughly the number of turns in a typical conversation.
Nothing is lost and nothing is broken: the unit changed, from "reply" to
"conversation", because a conversation is the thing that actually has a
beginning, an end, and a person waiting in the middle of it.

**Loops that existed before you upgrade keep the old grain permanently.**
They were recorded per-reply and they stay that way — the event record is
append-only, so we don't rewrite history. You'll see a seam: older loops are
small and numerous, newer ones are fewer and longer. Loops left open from
before the upgrade are never absorbed into a new conversation loop; they age
out through the normal sweep exactly as they would have.

### Changed

- **Loop grain is now the conversation.** Every span carries the session key
  as `trovis.loop.external_id`, so all turns of a conversation group into
  one loop. Spans with no session continuity fall back to the run id and
  then to the backend's 30-minute gap rule, unchanged.
- **`agent_end` no longer closes the loop as `done` on a conversational
  turn.** It emits a `to_human` handoff (`reason: "turn_end"`) targeted at
  the sender last heard from in that session. `message_received` resolves it
  by id when the person replies, returning possession to the agent. The loop
  story now reads agent → human → agent.
- **`agent_end` on a run with no session continuity still closes as `done`.**
  One-shot and cron runs are unaffected by this release.
- **A failed run emits neither a close nor a handoff.** A crash hands work to
  nobody; inventing a handoff would put a phantom item in a person's queue,
  and a fake `done` would be wrong data in a permanent record. The sweep owns
  the lifecycle, and the run's error status is now rendered in the loop story
  as "The run failed" instead of the story going silent.
- **`agentName` is no longer silently defaulted to `openclaw-agent`.** It is
  derived from your gateway's configured agent id, else the workspace
  directory name. If neither is available the plugin goes inert with an
  explanatory message, the same posture `endpoint` already had. The old
  shared default meant every unconfigured install reported under one
  `service.name`, and because an agent in Trovis is derived from
  `service.name`, they all collapsed into a single agent — merged spans,
  overwritten descriptions, summed costs, one slot against the plan limit.
  `/trovis status` now shows whether the name was configured or derived.

### Fixed

- `openclaw.plugin.json` and the README pointed at `oversee.dev`, a domain
  that has not been ours since the rename. Now `trovisai.com`.

### Unchanged, deliberately

- **`handoffTools` still ships empty.** The plugin still never guesses which
  tools constitute a handoff. It remains available as operator config, and is
  now explicitly the secondary mechanism — the structural turn-end signal is
  the primary one, and it needs no configuration.
- Config-mapped tool handoffs remain target-less. The recipient of a tool
  call lives in that call's parameter *values*, which this plugin does not
  read. Only the structural turn-end handoff carries a target, sourced from
  the message sender's identity field.

### Known limitations

- **A gateway restart between the agent's turn and the human's reply loses
  the pending handoff.** The map of outstanding turn-end handoffs is held in
  memory. If the gateway restarts in between, no resolution is emitted and
  that loop stays `awaiting_human` until the sweep. This is deliberate: the
  alternative — resolving whatever handoff happens to be open when an
  unmatched reply arrives — risks closing a real, unrelated handoff, and the
  event record has no deletion path. A loop stuck this way can be resolved
  by a person from the Trovis dashboard; a wrongly-resolved one cannot be
  undone.
