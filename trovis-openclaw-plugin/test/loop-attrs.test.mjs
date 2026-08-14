// Workloop attribute tests. Run with `npm test` (builds dist/index.test.mjs
// with the plugin-entry stub aliased in, then runs node's built-in runner —
// no test framework dependency).
//
// The suite drives the plugin exactly the way the gateway does: register()
// with a fake api that captures hook handlers, then fire hook events. A fake
// tracer (injected via the private __internal test surface) records every
// span and its attributes — no OTEL SDK, no network.

import { test } from "node:test"
import assert from "node:assert/strict"

import plugin, {
  trovisHandoff,
  trovisCloseLoop,
  __internal,
} from "../dist/index.test.mjs"

// Point transcript scanning at a nonexistent dir so tests never walk the
// developer's real ~/.openclaw tree.
process.env.TROVIS_TRANSCRIPT_DIR = "/nonexistent-trovis-test"

// ---------------------------------------------------------------------------
// Harness
// ---------------------------------------------------------------------------

const handlers = {}
plugin.register({
  on(name, handler) {
    handlers[name] = handler
  },
  registerCommand() {},
  version: "test-gateway",
})

function makeFakeTracer(spans) {
  return {
    startSpan(name) {
      const span = {
        name,
        attributes: {},
        ended: false,
        status: null,
        setAttribute(k, v) {
          this.attributes[k] = v
          return this
        },
        setStatus(s) {
          this.status = s
          return this
        },
        end() {
          this.ended = true
        },
      }
      spans.push(span)
      return span
    },
  }
}

/** Reset plugin state with a fresh span sink; returns the sink. */
function prime({ captureOutputs = false, handoffTools = "" } = {}) {
  const spans = []
  __internal.state.initialized = true
  __internal.state.disabled = false
  __internal.state.tracer = makeFakeTracer(spans)
  __internal.state.captureOutputs = captureOutputs
  __internal.state.handoffTools = __internal.parseHandoffTools(handoffTools)
  // All cross-hook bookkeeping (pending handoffs/closes, per-run
  // suppression, conversation state) is module-level and survives between
  // tests. Reset it so each case starts from a known state and the suite is
  // order-independent.
  __internal.resetForTest()
  return spans
}

let seq = 0
function fire(name, event = {}, ctx = {}) {
  seq += 1
  handlers[name](event, ctx)
}

/** Simulate one full execution unit (message -> tool -> output -> end). */
function simulateRun(runId, { sessionKey = `agent:main:s-${runId}` } = {}) {
  const ctx = { runId, sessionKey }
  const toolCallId = `tc-${runId}-${++seq}`
  fire("message_received", { content: `do the thing ${runId}` }, ctx)
  fire(
    "before_tool_call",
    { toolName: "read_file", toolCallId, runId },
    ctx,
  )
  fire("after_tool_call", { toolCallId }, ctx)
  fire("llm_output", { assistantTexts: ["done"], runId }, ctx)
  fire("agent_end", { runId, success: true }, ctx)
}

// ---------------------------------------------------------------------------
// 1. Run identity
// ---------------------------------------------------------------------------

test("all spans within one execution unit share one trovis.run.id; a second unit gets a different id", () => {
  const spans = prime()
  simulateRun("run-A")
  const idsA = new Set(spans.map((s) => s.attributes["trovis.run.id"]))
  assert.deepEqual([...idsA], ["run-A"], "every span of unit A carries run-A")
  assert.ok(spans.length >= 4, "message, tool, output, end spans all emitted")

  const before = spans.length
  simulateRun("run-B")
  const idsB = new Set(
    spans.slice(before).map((s) => s.attributes["trovis.run.id"]),
  )
  assert.deepEqual([...idsB], ["run-B"], "unit B gets its own id")
})

test("runId is taken verbatim from the event, falling back to ctx", () => {
  const spans = prime()
  fire("llm_output", { runId: "ev-id" }, { runId: "ctx-id" })
  assert.equal(spans[0].attributes["trovis.run.id"], "ev-id")
  fire("llm_output", {}, { runId: "ctx-id" })
  assert.equal(spans[1].attributes["trovis.run.id"], "ctx-id")
})

// ---------------------------------------------------------------------------
// 2. Loop title
// ---------------------------------------------------------------------------

test("loop title comes from the inbound message, collapsed and capped at 80 chars — only when captureOutputs is on", () => {
  const spans = prime({ captureOutputs: true })
  fire(
    "message_received",
    { content: "  Fix   the\nbilling bug  " },
    { runId: "run-title" },
  )
  assert.equal(spans[0].attributes["trovis.loop.title"], "Fix the billing bug")

  fire("message_received", { content: "x".repeat(300) }, { runId: "run-long" })
  assert.equal(spans[1].attributes["trovis.loop.title"].length, 80)

  // Content capture off -> content-derived title is never sent.
  const spans2 = prime({ captureOutputs: false })
  fire("message_received", { content: "secret task" }, { runId: "run-priv" })
  assert.ok(!("trovis.loop.title" in spans2[0].attributes))
})

// ---------------------------------------------------------------------------
// 3a. trovisHandoff() helper
// ---------------------------------------------------------------------------

test("trovisHandoff() sets direction/target/reason/id on the next span, one-shot", () => {
  const spans = prime()
  const id = trovisHandoff("to_human", "ops-team", "needs approval")
  assert.ok(typeof id === "string" && id.length > 10, "returns the handoff id")
  fire("llm_output", { runId: "run-h1" }, {})
  const a = spans[0].attributes
  assert.equal(a["trovis.handoff.direction"], "to_human")
  assert.equal(a["trovis.handoff.target_id"], "ops-team")
  assert.equal(a["trovis.handoff.reason"], "needs approval")
  assert.equal(a["trovis.handoff.id"], id)

  fire("llm_output", { runId: "run-h1" }, {})
  assert.ok(
    !("trovis.handoff.direction" in spans[1].attributes),
    "signal consumed by the first span only",
  )
})

test("trovisHandoff() rejects invalid directions as a warn-and-no-op", () => {
  const spans = prime()
  assert.equal(trovisHandoff("sideways"), null)
  fire("llm_output", { runId: "run-h2" }, {})
  assert.ok(!("trovis.handoff.direction" in spans[0].attributes))
})

// ---------------------------------------------------------------------------
// 3b. Config-mapped handoff tools
// ---------------------------------------------------------------------------

test("a tool listed in TROVIS_HANDOFF_TOOLS format produces the handoff attributes; unlisted tools don't", () => {
  const spans = prime({
    handoffTools: "send_email:to_human, delegate_task:to_agent",
  })
  fire(
    "before_tool_call",
    { toolName: "send_email", toolCallId: "t1", runId: "run-t1" },
    {},
  )
  const a = spans[0].attributes
  assert.equal(a["trovis.handoff.direction"], "to_human")
  assert.equal(a["trovis.handoff.reason"], "tool:send_email")
  assert.ok(typeof a["trovis.handoff.id"] === "string" && a["trovis.handoff.id"].length > 10)

  fire(
    "before_tool_call",
    { toolName: "delegate_task", toolCallId: "t2", runId: "run-t1" },
    {},
  )
  assert.equal(spans[1].attributes["trovis.handoff.direction"], "to_agent")

  fire(
    "before_tool_call",
    { toolName: "read_file", toolCallId: "t3", runId: "run-t1" },
    {},
  )
  assert.ok(!("trovis.handoff.direction" in spans[2].attributes))
})

test("parseHandoffTools skips malformed entries and never guesses directions", () => {
  const map = __internal.parseHandoffTools(
    "good:to_human,bad:somewhere,also_bad,:to_agent,",
  )
  assert.deepEqual([...map.entries()], [["good", "to_human"]])
  assert.equal(__internal.parseHandoffTools(undefined).size, 0)
  assert.equal(__internal.parseHandoffTools("").size, 0)
})

// ---------------------------------------------------------------------------
// 4. Completion
// ---------------------------------------------------------------------------

test("agent_end closes the loop as done on success", () => {
  const spans = prime()
  fire("agent_end", { runId: "run-c1", success: true }, { runId: "run-c1" })
  const end = spans.find((s) => s.name === "agent_run_complete")
  assert.equal(end.attributes["trovis.loop.close"], "done")
  assert.equal(end.attributes["trovis.run.id"], "run-c1")
})

test("a failed run is NOT closed as done", () => {
  const spans = prime()
  fire("agent_end", { runId: "run-c2", success: false }, { runId: "run-c2" })
  const end = spans.find((s) => s.name === "agent_run_complete")
  assert.ok(!("trovis.loop.close" in end.attributes))
})

test("a run that declared a handoff is NOT auto-closed — the loop stays awaiting", () => {
  const spans = prime({ handoffTools: "request_approval:to_human" })
  const ctx = { runId: "run-c3" }
  fire(
    "before_tool_call",
    { toolName: "request_approval", toolCallId: "t9", runId: "run-c3" },
    ctx,
  )
  fire("after_tool_call", { toolCallId: "t9" }, ctx)
  fire("agent_end", { runId: "run-c3", success: true }, ctx)
  const end = spans.find((s) => s.name === "agent_run_complete")
  assert.ok(
    !("trovis.loop.close" in end.attributes),
    "auto-close suppressed after a handoff",
  )
})

test("trovisCloseLoop(reason) closes with the reason and suppresses the auto-done", () => {
  const spans = prime()
  trovisCloseLoop("blocked on credentials")
  fire("agent_end", { runId: "run-c4", success: true }, { runId: "run-c4" })
  const end = spans.find((s) => s.name === "agent_run_complete")
  assert.equal(end.attributes["trovis.loop.close"], "blocked on credentials")
})

test("handoff suppression is per-run: the next run auto-closes normally", () => {
  const spans = prime()
  trovisHandoff("to_agent")
  fire("llm_output", { runId: "run-c5" }, {})
  fire("agent_end", { runId: "run-c5", success: true }, { runId: "run-c5" })
  fire("agent_end", { runId: "run-c6", success: true }, { runId: "run-c6" })
  const ends = spans.filter((s) => s.name === "agent_run_complete")
  assert.ok(!("trovis.loop.close" in ends[0].attributes))
  assert.equal(ends[1].attributes["trovis.loop.close"], "done")
})

// ---------------------------------------------------------------------------
// 5. Absent when unknown — never empty strings
// ---------------------------------------------------------------------------

test("no runId -> trovis.run.id omitted entirely; empty strings are never emitted", () => {
  const spans = prime()
  fire("message_received", { content: "hello" }, {})
  fire("llm_output", {}, {})
  fire("agent_end", {}, {})
  for (const s of spans) {
    assert.ok(
      !("trovis.run.id" in s.attributes),
      `${s.name}: run.id omitted when unknown`,
    )
    for (const [k, v] of Object.entries(s.attributes)) {
      assert.notEqual(v, "", `${s.name}: attribute ${k} must never be ""`)
    }
  }
  // Empty runId strings are treated as absent, not forwarded.
  fire("llm_output", { runId: "" }, { runId: "" })
  assert.ok(!("trovis.run.id" in spans.at(-1).attributes))
})

test("every span still ends exactly once (zero behavior change to span lifecycle)", () => {
  const spans = prime()
  simulateRun("run-z")
  for (const s of spans) {
    assert.equal(s.ended, true, `${s.name} ended`)
  }
})

// ---------------------------------------------------------------------------
// 7. Structural turn-end handoffs (0.6.0)
// ---------------------------------------------------------------------------
// agent_end used to assert `done` on every successful run. AgentEndEvent
// carries only {runId, success, error} — nothing that distinguishes "the task
// finished" from "my turn finished". The discriminator is session continuity,
// on the context: with a session, the honest signal is a handoff to the human.

const convo = (sessionKey, runId, senderId) => ({ sessionKey, runId, senderId })

test("agent_end emits a turn_end handoff to the human, targeted at the sender", () => {
  const spans = prime()
  const ctx = convo("s-1", "run-t1", "user-42")
  fire("message_received", { content: "hi", senderId: "user-42" }, ctx)
  fire("agent_end", { runId: "run-t1", success: true }, ctx)
  const end = spans.find((s) => s.name === "agent_run_complete")
  assert.equal(end.attributes["trovis.handoff.direction"], "to_human")
  assert.equal(end.attributes["trovis.handoff.reason"], "turn_end")
  assert.equal(end.attributes["trovis.handoff.target_id"], "user-42")
  assert.match(end.attributes["trovis.handoff.id"], /^[0-9a-f-]{36}$/)
})

test("agent_end no longer closes the loop in the conversational path", () => {
  const spans = prime()
  const ctx = convo("s-2", "run-t2", "user-1")
  fire("message_received", { content: "hi", senderId: "user-1" }, ctx)
  fire("agent_end", { runId: "run-t2", success: true }, ctx)
  const end = spans.find((s) => s.name === "agent_run_complete")
  assert.ok(
    !("trovis.loop.close" in end.attributes),
    "a finished TURN is not a finished unit of work",
  )
})

test("the human's reply resolves the pending handoff by its uuid", () => {
  const spans = prime()
  const ctx = convo("s-3", "run-t3", "user-7")
  fire("message_received", { content: "first", senderId: "user-7" }, ctx)
  fire("agent_end", { runId: "run-t3", success: true }, ctx)
  const uuid = spans.find((s) => s.name === "agent_run_complete")
    .attributes["trovis.handoff.id"]

  // Next turn: same session, DIFFERENT run — the whole point.
  const ctx2 = convo("s-3", "run-t4", "user-7")
  fire("message_received", { content: "second", senderId: "user-7" }, ctx2)
  const reply = spans.filter((s) => s.name === "message_received").at(-1)
  assert.equal(reply.attributes["trovis.handoff.resolve"], "completed")
  assert.equal(
    reply.attributes["trovis.handoff.id"], uuid,
    "resolves THAT handoff by id, never uuid-less",
  )
})

test("a conversation is one loop: every turn shares one loop.external_id", () => {
  const spans = prime()
  fire("message_received", { content: "a", senderId: "u" }, convo("s-4", "r1", "u"))
  fire("agent_end", { runId: "r1", success: true }, convo("s-4", "r1", "u"))
  fire("message_received", { content: "b", senderId: "u" }, convo("s-4", "r2", "u"))
  fire("agent_end", { runId: "r2", success: true }, convo("s-4", "r2", "u"))
  const keys = new Set(spans.map((s) => s.attributes["trovis.loop.external_id"]))
  assert.deepEqual([...keys], ["s-4"], "one loop key across both runs")
  const runIds = new Set(spans.map((s) => s.attributes["trovis.run.id"]))
  assert.deepEqual([...runIds].sort(), ["r1", "r2"], "run ids still distinct")
})

test("no session continuity: still closes as done, no handoff invented", () => {
  const spans = prime()
  fire("agent_end", { runId: "one-shot", success: true }, { runId: "one-shot" })
  const end = spans.find((s) => s.name === "agent_run_complete")
  assert.equal(end.attributes["trovis.loop.close"], "done")
  assert.ok(!("trovis.handoff.direction" in end.attributes))
  assert.ok(!("trovis.loop.external_id" in end.attributes))
})

test("a failed conversational run emits neither a close nor a handoff", () => {
  const spans = prime()
  const ctx = convo("s-5", "run-f", "user-9")
  fire("message_received", { content: "go", senderId: "user-9" }, ctx)
  fire("agent_end", { runId: "run-f", success: false, error: "boom" }, ctx)
  const end = spans.find((s) => s.name === "agent_run_complete")
  assert.ok(!("trovis.loop.close" in end.attributes), "a crash is not done")
  assert.ok(
    !("trovis.handoff.direction" in end.attributes),
    "a crash hands work to nobody — no phantom item in a person's queue",
  )
  assert.equal(end.status?.code, 2, "but the failure IS recorded on the span")
})

test("an explicit handoff wins — no structural handoff stacked on top", () => {
  const spans = prime({ handoffTools: "request_approval:to_human" })
  const ctx = convo("s-6", "run-h", "user-3")
  fire("message_received", { content: "x", senderId: "user-3" }, ctx)
  fire("before_tool_call", { toolName: "request_approval", toolCallId: "t1" }, ctx)
  fire("after_tool_call", { toolCallId: "t1" }, ctx)
  fire("agent_end", { runId: "run-h", success: true }, ctx)
  const end = spans.find((s) => s.name === "agent_run_complete")
  assert.ok(!("trovis.handoff.direction" in end.attributes))
  assert.ok(!("trovis.loop.close" in end.attributes))
})

test("mapped tool handoffs stay target-less; only the structural one has a target", () => {
  const spans = prime({ handoffTools: "request_approval:to_human" })
  const ctx = convo("s-7", "run-m", "user-5")
  fire("message_received", { content: "x", senderId: "user-5" }, ctx)
  fire("before_tool_call", { toolName: "request_approval", toolCallId: "t2" }, ctx)
  const tool = spans.find((s) => s.name === "tool_call")
  assert.equal(tool.attributes["trovis.handoff.direction"], "to_human")
  assert.equal(tool.attributes["trovis.handoff.reason"], "tool:request_approval")
  assert.ok(
    !("trovis.handoff.target_id" in tool.attributes),
    "the recipient lives in parameter VALUES, which we never read",
  )
})

test("unconfigured tools never emit a handoff — the allowlist ships empty", () => {
  const spans = prime()
  const ctx = convo("s-8", "run-n", "user-2")
  fire("before_tool_call", { toolName: "send_slack_message", toolCallId: "t3" }, ctx)
  const tool = spans.find((s) => s.name === "tool_call")
  assert.ok(
    !("trovis.handoff.direction" in tool.attributes),
    "no default handoffTools — the plugin still never guesses",
  )
})

test("an unmatched reply emits no resolve (the restart limitation, made explicit)", () => {
  const spans = prime()
  // No preceding agent_end for this session — as after a gateway restart.
  fire("message_received", { content: "hello?", senderId: "u" }, convo("s-9", "r", "u"))
  const msg = spans.find((s) => s.name === "message_received")
  assert.ok(
    !("trovis.handoff.resolve" in msg.attributes),
    "silent rather than resolving whatever happens to be open",
  )
})

// ---------------------------------------------------------------------------
// 8. Agent identity (0.6.0)
// ---------------------------------------------------------------------------

test("agentName derives from a configured agent id, else the workspace name", () => {
  const { deriveAgentName } = __internal
  assert.equal(
    deriveAgentName({ config: { agents: { list: [{ id: "billing-bot" }] } } }),
    "billing-bot",
  )
  assert.equal(deriveAgentName({ workspaceDir: "/home/me/agents/support" }), "support")
  assert.equal(deriveAgentName({ workspaceDir: "/home/me/agents/support/" }), "support")
  assert.equal(
    deriveAgentName({ config: { agents: { defaults: { workspace: "/srv/triage" } } } }),
    "triage",
  )
})

test("agentName has NO shared fallback — undefined rather than 'openclaw-agent'", () => {
  const { deriveAgentName } = __internal
  assert.equal(deriveAgentName({}), undefined)
  assert.equal(deriveAgentName(undefined), undefined)
  // The old constant collapsed every unconfigured install into one agent.
  assert.ok(
    !JSON.stringify(deriveAgentName({}) ?? "").includes("openclaw-agent"),
  )
})
