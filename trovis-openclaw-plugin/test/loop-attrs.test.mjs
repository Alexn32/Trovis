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
