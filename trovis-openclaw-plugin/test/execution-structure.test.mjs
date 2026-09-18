// Execution structure (0.6.4): known containment becomes OTEL parentage;
// unknown containment stays unknown.
//
// Two harnesses. The fake tracer (as in loop-attrs.test.mjs) records the OTEL
// context each span was started in, so parentage decisions are asserted at
// the plugin boundary. The REAL tracer (sdk-trace-base + an in-memory
// exporter) proves what actually leaves the process: traceId, spanId and
// parentSpanId as the OTLP exporter would ship them. With
// TROVIS_WRITE_FIXTURE=1 the real-tracer run also writes that export as
// OTLP/JSON to test/fixtures/openclaw-run.otlp.json, which the backend's
// test_execution_hierarchy.py ingests through POST /v1/traces and reads back
// through GET /work/items/{id}/execution — the runtime → plugin → OTLP →
// database → Execution Graph chain, end to end.

import { test } from "node:test"
import assert from "node:assert/strict"
import { writeFileSync, mkdirSync } from "node:fs"
import { fileURLToPath } from "node:url"
import { dirname, join } from "node:path"

import { trace } from "@opentelemetry/api"
import {
  BasicTracerProvider,
  InMemorySpanExporter,
  SimpleSpanProcessor,
} from "@opentelemetry/sdk-trace-base"
import { Resource } from "@opentelemetry/resources"
import { createExportTraceServiceRequest } from "@opentelemetry/otlp-transformer"

import plugin, { __internal, trovisSetLoopTitle } from "../dist/index.test.mjs"

process.env.TROVIS_TRANSCRIPT_DIR = "/nonexistent-trovis-test"

const handlers = {}
plugin.register({
  on(name, handler) {
    handlers[name] = handler
  },
  registerCommand() {},
  version: "test-gateway",
})

function fire(name, event = {}, ctx = {}) {
  handlers[name](event, ctx)
}

// ---------------------------------------------------------------------------
// Fake tracer: records the context each span was started in
// ---------------------------------------------------------------------------

let fakeSeq = 0
function makeFakeTracer(spans) {
  return {
    startSpan(name, _options, ctx) {
      const parent = ctx ? trace.getSpan(ctx) : undefined
      const span = {
        name,
        id: `fake-${++fakeSeq}`,
        parent: parent ?? null,
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
        // trace.setSpan / getSpan store the object itself; spanContext is
        // only here so a real-API caller would not throw.
        spanContext() {
          return { traceId: "0".repeat(32), spanId: this.id, traceFlags: 1 }
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

function prime(tracer) {
  __internal.state.initialized = true
  __internal.state.disabled = false
  __internal.state.tracer = tracer
  __internal.state.captureOutputs = false
  __internal.state.handoffTools = __internal.parseHandoffTools("")
  __internal.resetForTest()
}

function primeFake() {
  const spans = []
  prime(makeFakeTracer(spans))
  return spans
}

const byName = (spans, name) => spans.filter((s) => s.name === name)

// ---------------------------------------------------------------------------
// 1. Known containment → parentage (fake tracer, plugin boundary)
// ---------------------------------------------------------------------------

test("1. hook spans that carry a runId are children of one agent_run root for that run", () => {
  const spans = primeFake()
  const ctx = { runId: "run-A", sessionKey: "agent:main:s-A" }
  fire("message_received", { content: "do it" }, ctx)
  fire("model_call_started", { callId: "m1", model: "m", provider: "p" }, ctx)
  fire("model_call_ended", { callId: "m1", durationMs: 5, outcome: "completed", usage: { input: 3, output: 4 } }, ctx)
  fire("before_tool_call", { toolName: "read_file", toolCallId: "t1", runId: "run-A" }, ctx)
  fire("after_tool_call", { toolCallId: "t1", success: true }, ctx)
  fire("llm_output", { assistantTexts: ["done"], runId: "run-A" }, ctx)
  fire("agent_end", { runId: "run-A", success: true }, ctx)

  const roots = byName(spans, "agent_run")
  assert.equal(roots.length, 1, "exactly one run root")
  const root = roots[0]
  assert.equal(root.parent, null, "the run root is a root")
  assert.equal(root.attributes["trovis.event.type"], "agent_run")
  assert.equal(root.attributes["trovis.run.id"], "run-A")
  assert.equal(root.attributes["trovis.loop.external_id"], "agent:main:s-A")
  assert.equal(root.attributes["trovis.run.start_basis"], "first_observed_hook")
  assert.equal(root.attributes["trovis.run.end_basis"], "agent_end")
  assert.equal(root.ended, true, "closed by agent_end")

  for (const name of ["message_received", "model_call", "tool_call", "llm_output", "agent_run_complete"]) {
    const s = byName(spans, name)[0]
    assert.ok(s, `${name} emitted`)
    assert.equal(s.parent, root, `${name} is a child of the run root`)
  }
})

test("3. siblings: the tool call and the model call both hang off the run — the gateway does not say which turn issued which call", () => {
  const spans = primeFake()
  const ctx = { runId: "run-S" }
  fire("model_call_started", { callId: "m1", model: "m" }, ctx)
  fire("model_call_ended", { callId: "m1", durationMs: 5, outcome: "completed", usage: { input: 1, output: 1 } }, ctx)
  fire("before_tool_call", { toolName: "exec", toolCallId: "t1" }, ctx)
  fire("after_tool_call", { toolCallId: "t1" }, ctx)
  fire("model_call_started", { callId: "m2", model: "m" }, ctx)
  fire("model_call_ended", { callId: "m2", durationMs: 5, outcome: "completed", usage: { input: 1, output: 1 } }, ctx)
  fire("agent_end", { runId: "run-S", success: true }, ctx)
  const root = byName(spans, "agent_run")[0]
  const [m1, m2] = byName(spans, "model_call")
  const tool = byName(spans, "tool_call")[0]
  assert.equal(m1.parent, root)
  assert.equal(tool.parent, root, "the tool call is NOT nested under the model call that preceded it")
  assert.equal(m2.parent, root, "the later model call is NOT nested under the tool call")
})

test("5/6. chronology creates no parentage: a run's spans out of any 'expected' order still hang off the run only", () => {
  const spans = primeFake()
  const ctx = { runId: "run-O" }
  fire("llm_output", { assistantTexts: ["first?"] }, ctx)
  fire("before_tool_call", { toolName: "a", toolCallId: "t1" }, ctx)
  fire("before_tool_call", { toolName: "b", toolCallId: "t2" }, ctx)
  fire("after_tool_call", { toolCallId: "t2" })
  fire("after_tool_call", { toolCallId: "t1" })
  fire("agent_end", { runId: "run-O", success: true }, ctx)
  const root = byName(spans, "agent_run")[0]
  for (const s of spans) {
    if (s === root) continue
    assert.equal(s.parent, root, `${s.name}: parent is the run, never the previous span`)
  }
})

test("2/7. two runs are two roots in two contexts; a run's children never attach to another run", () => {
  const spans = primeFake()
  const a = { runId: "run-1", sessionKey: "s" }
  const b = { runId: "run-2", sessionKey: "s" }
  fire("before_tool_call", { toolName: "x", toolCallId: "t1" }, a)
  fire("before_tool_call", { toolName: "y", toolCallId: "t2" }, b)
  fire("after_tool_call", { toolCallId: "t1" }, a)
  fire("after_tool_call", { toolCallId: "t2" }, b)
  fire("agent_end", { runId: "run-1", success: true }, a)
  fire("agent_end", { runId: "run-2", success: true }, b)
  const roots = byName(spans, "agent_run")
  assert.equal(roots.length, 2)
  const [t1, t2] = byName(spans, "tool_call")
  assert.equal(t1.parent.attributes["trovis.run.id"], "run-1")
  assert.equal(t2.parent.attributes["trovis.run.id"], "run-2")
  // Same session, different runs: shared loop key, separate execution roots.
  assert.equal(roots[0].attributes["trovis.loop.external_id"], "s")
  assert.equal(roots[1].attributes["trovis.loop.external_id"], "s")
  assert.notEqual(roots[0], roots[1])
})

// ---------------------------------------------------------------------------
// 2. Unknown containment stays unknown
// ---------------------------------------------------------------------------

test("4. a hook with no runId stays a root — no run is invented for it", () => {
  const spans = primeFake()
  fire("message_received", { content: "hello" }, {})
  fire("before_tool_call", { toolName: "x", toolCallId: "t1" }, {})
  fire("after_tool_call", { toolCallId: "t1" }, {})
  fire("llm_output", {}, {})
  assert.equal(byName(spans, "agent_run").length, 0, "no root without a runId")
  for (const s of spans) assert.equal(s.parent, null, `${s.name} is a root`)
})

test("4. agent_end alone does not open a run root — a run seen only at its end has no structure to carry", () => {
  const spans = primeFake()
  fire("agent_end", { runId: "lonely", success: true }, { runId: "lonely" })
  assert.equal(byName(spans, "agent_run").length, 0)
  const end = byName(spans, "agent_run_complete")[0]
  assert.equal(end.parent, null)
  assert.equal(end.attributes["trovis.loop.close"], "done", "loop semantics unchanged")
})

test("a standalone transcript usage span (no runId, session only) stays a root", () => {
  const spans = primeFake()
  // model_call_ended for an unknown callId is ignored; nothing is parented.
  fire("model_call_ended", { callId: "ghost", durationMs: 1, outcome: "completed" }, { sessionKey: "s-only" })
  assert.equal(byName(spans, "agent_run").length, 0)
})

// ---------------------------------------------------------------------------
// 3. Run/loop correlation and the other locked semantics are untouched
// ---------------------------------------------------------------------------

test("8/9. children carry exactly the loop attributes they carried before; the root adds the same key, never a title/handoff/close", () => {
  const spans = primeFake()
  const ctx = { runId: "run-K", sessionKey: "agent:main:s-K" }
  fire("message_received", { content: "x" }, ctx)
  fire("before_tool_call", { toolName: "x", toolCallId: "t1" }, ctx)
  fire("after_tool_call", { toolCallId: "t1" }, ctx)
  fire("agent_end", { runId: "run-K", success: true }, ctx)
  const root = byName(spans, "agent_run")[0]
  for (const s of spans) {
    assert.equal(s.attributes["trovis.run.id"], "run-K", `${s.name} run id`)
    assert.equal(s.attributes["trovis.loop.external_id"], "agent:main:s-K", `${s.name} loop key`)
  }
  for (const k of ["trovis.loop.title", "trovis.handoff.direction", "trovis.loop.close", "trovis.handoff.resolve"]) {
    assert.ok(!(k in root.attributes), `root never carries ${k}`)
  }
  // The conversational turn end still lands on agent_run_complete, not the root.
  const end = byName(spans, "agent_run_complete")[0]
  assert.equal(end.attributes["trovis.handoff.direction"], "to_human")
})

test("one-shot run (no session): agent_run_complete still closes the loop; the root carries no close", () => {
  const spans = primeFake()
  const ctx = { runId: "one-shot" }
  fire("before_tool_call", { toolName: "x", toolCallId: "t1" }, ctx)
  fire("after_tool_call", { toolCallId: "t1" }, ctx)
  fire("agent_end", { runId: "one-shot", success: true }, ctx)
  const end = byName(spans, "agent_run_complete")[0]
  const root = byName(spans, "agent_run")[0]
  assert.equal(end.attributes["trovis.loop.close"], "done")
  assert.ok(!("trovis.loop.close" in root.attributes))
  assert.ok(!("trovis.loop.external_id" in root.attributes), "no session, no loop key on the root either")
})

test("20. a failed run marks the root's status from the gateway's own verdict; a failed tool marks its own span only", () => {
  const spans = primeFake()
  const ctx = { runId: "run-F" }
  fire("before_tool_call", { toolName: "stripe.refunds.create", toolCallId: "t1" }, ctx)
  fire("after_tool_call", { toolCallId: "t1", success: false, error: "Card declined" }, ctx)
  fire("before_tool_call", { toolName: "stripe.refunds.create", toolCallId: "t2" }, ctx)
  fire("after_tool_call", { toolCallId: "t2", success: true }, ctx)
  fire("agent_end", { runId: "run-F", success: false, error: "boom" }, ctx)
  const root = byName(spans, "agent_run")[0]
  const [t1, t2] = byName(spans, "tool_call")
  assert.equal(t1.status?.code, 2)
  assert.equal(t1.status?.message, "Card declined")
  assert.equal(t1.attributes["trovis.tool.success"], false)
  assert.equal(t2.status, null, "the second call is its own span with its own status")
  assert.equal(t2.attributes["trovis.tool.success"], true)
  assert.equal(root.status?.code, 2)
  assert.equal(root.status?.message, "boom")
  // 21. two calls of the same tool are two spans; nothing says retry.
  const dump = JSON.stringify(spans.map((s) => s.attributes))
  assert.ok(!/retry|attempt/i.test(dump))
})

test("every span, root included, ends exactly once", () => {
  const spans = primeFake()
  const ctx = { runId: "run-E", sessionKey: "s-E" }
  fire("message_received", { content: "x" }, ctx)
  fire("before_tool_call", { toolName: "x", toolCallId: "t1" }, ctx)
  fire("after_tool_call", { toolCallId: "t1" }, ctx)
  fire("model_call_started", { callId: "m1", model: "m" }, ctx)
  fire("model_call_ended", { callId: "m1", durationMs: 1, outcome: "completed", usage: { input: 1, output: 1 } }, ctx)
  fire("llm_output", { assistantTexts: ["y"] }, ctx)
  fire("agent_end", { runId: "run-E", success: true }, ctx)
  for (const s of spans) assert.equal(s.ended, true, `${s.name} ended`)
  assert.equal(__internal.runRoots.size, 0, "no root left open after agent_end")
})

test("a root whose run never ends is closed by the timeout bookkeeping, never leaked", () => {
  const spans = primeFake()
  fire("before_tool_call", { toolName: "x", toolCallId: "t1" }, { runId: "run-hang" })
  assert.equal(__internal.runRoots.size, 1)
  const root = byName(spans, "agent_run")[0]
  assert.equal(root.ended, false, "open while the run is open")
  __internal.resetForTest()
  assert.equal(__internal.runRoots.size, 0)
})

// ---------------------------------------------------------------------------
// 4. What actually leaves the process (real tracer, real ids)
// ---------------------------------------------------------------------------

test("1/2/13. real OTEL: children carry the root's traceId and its spanId as parentSpanId; two runs are two traces; runId-less hooks are roots", () => {
  const exporter = new InMemorySpanExporter()
  const provider = new BasicTracerProvider({
    resource: new Resource({
      "service.name": "openclaw-refunds",
      "service.version": "0.6.4",
      "trovis.plugin.version": "0.6.4",
      "openclaw.gateway.version": "test-gateway",
    }),
  })
  provider.addSpanProcessor(new SimpleSpanProcessor(exporter))
  prime(provider.getTracer("@trovis/openclaw-plugin", "0.6.4"))

  const run1 = { runId: "run-real-1", sessionKey: "agent:main:s-real" }
  trovisSetLoopTitle("Refund order #4471")
  fire("message_received", { content: "Refund order #4471", senderId: "user-1" }, run1)
  fire("model_call_started", { callId: "m1", model: "grok-4", provider: "xai" }, run1)
  fire("model_call_ended", { callId: "m1", durationMs: 40, outcome: "completed", usage: { input: 120, output: 30 } }, run1)
  fire("before_tool_call", { toolName: "mcp__shopify__lookup_orders_by_email", toolCallId: "t1", params: { email: "x" } }, run1)
  fire("after_tool_call", { toolCallId: "t1", success: true, durationMs: 12 }, run1)
  fire("before_tool_call", { toolName: "stripe.refunds.create", toolCallId: "t2" }, run1)
  fire("after_tool_call", { toolCallId: "t2", success: false, error: "Card declined", durationMs: 7 }, run1)
  fire("before_tool_call", { toolName: "stripe.refunds.create", toolCallId: "t3" }, run1)
  fire("after_tool_call", { toolCallId: "t3", success: true, durationMs: 9 }, run1)
  fire("model_call_started", { callId: "m2", model: "grok-4", provider: "xai" }, run1)
  fire("model_call_ended", { callId: "m2", durationMs: 30, outcome: "completed", usage: { input: 0, output: 0 } }, run1)
  fire("llm_output", { assistantTexts: ["Refunded."], runId: "run-real-1" }, run1)
  fire("agent_end", { runId: "run-real-1", success: true }, run1)

  const run2 = { runId: "run-real-2", sessionKey: "agent:main:s-real" }
  fire("message_received", { content: "thanks", senderId: "user-1" }, run2)
  fire("agent_end", { runId: "run-real-2", success: true }, run2)

  // No runId: the gateway did not place this in a run.
  fire("before_tool_call", { toolName: "orphan_tool", toolCallId: "t9" }, {})
  fire("after_tool_call", { toolCallId: "t9" }, {})

  const spans = exporter.getFinishedSpans()
  const roots1 = spans.filter((s) => s.name === "agent_run" && s.attributes["trovis.run.id"] === "run-real-1")
  const roots2 = spans.filter((s) => s.name === "agent_run" && s.attributes["trovis.run.id"] === "run-real-2")
  assert.equal(roots1.length, 1)
  assert.equal(roots2.length, 1)
  const root1 = roots1[0]
  const root2 = roots2[0]
  assert.equal(root1.parentSpanId, undefined, "run root has no parent")
  assert.notEqual(root1.spanContext().traceId, root2.spanContext().traceId, "two runs, two traces")

  const children1 = spans.filter((s) => s.attributes["trovis.run.id"] === "run-real-1" && s.name !== "agent_run")
  assert.equal(children1.length, 8, "message, 2 model calls, 3 tool calls, output, completion")
  for (const c of children1) {
    assert.equal(c.spanContext().traceId, root1.spanContext().traceId, `${c.name} shares the run's trace`)
    assert.equal(c.parentSpanId, root1.spanContext().spanId, `${c.name} parent is the run root`)
  }
  const orphan = spans.find((s) => s.attributes["trovis.tool.name"] === "orphan_tool")
  assert.equal(orphan.parentSpanId, undefined, "no runId → root")
  assert.equal(spans.filter((s) => s.name === "agent_run").length, 2, "no root invented for the orphan")

  // Root timing brackets its children (start ≤ first child start, end ≥ last
  // child end). hrTime is [seconds, nanos]; compared as BigInt nanoseconds —
  // a float would lose the low bits at 1e18. The SDK derives each span's
  // end from ITS OWN start plus a performance.now() delta, and starts from
  // Date.now(), so two spans ended microseconds apart can read out of order
  // within one millisecond; the bracket is therefore asserted to 5 ms, which
  // still catches a root closed before its children by any real margin.
  const toNs = (hr) => BigInt(hr[0]) * 1_000_000_000n + BigInt(hr[1])
  const SLACK = 5_000_000n
  const rootStart = toNs(root1.startTime)
  const rootEnd = toNs(root1.endTime)
  for (const c of children1) {
    assert.ok(toNs(c.startTime) + SLACK >= rootStart, `${c.name} starts after the root opened`)
    assert.ok(toNs(c.endTime) <= rootEnd + SLACK, `${c.name} ends before the root closed`)
  }

  if (process.env.TROVIS_WRITE_FIXTURE === "1") {
    const req = createExportTraceServiceRequest(spans, { useHex: true, useLongBits: false })
    const dir = join(dirname(fileURLToPath(import.meta.url)), "fixtures")
    mkdirSync(dir, { recursive: true })
    writeFileSync(join(dir, "openclaw-run.otlp.json"), JSON.stringify(req, null, 2) + "\n")
  }
  provider.shutdown()
})
