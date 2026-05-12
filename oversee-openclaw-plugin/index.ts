/**
 * @oversee/openclaw-plugin
 * ============================================================================
 * Captures OpenClaw agent activity (messages, tool calls, LLM calls, run
 * completion) and forwards it to an Oversee endpoint as OpenTelemetry traces.
 *
 * All event names and payload fields confirmed from
 * docs.openclaw.ai/plugins/plugin-hooks.
 *
 * Hooks registered (all observation-only, priority 0):
 *   - message_received       inbound message arrival
 *   - message_sent           outbound delivery result
 *   - before_tool_call       tool invocation started
 *   - after_tool_call        tool invocation completed
 *   - model_call_started     LLM call started
 *   - model_call_ended       LLM call ended
 *   - agent_end              entire run finished
 *
 * Privacy model (enforced in code):
 *   - We capture only metadata: lengths, names, IDs, durations, counts.
 *   - We never read message content, tool parameter values, prompts, or
 *     LLM responses. The model_call_* hooks are platform-guaranteed not to
 *     expose those, but we also defensively never reach for them.
 * ============================================================================
 */

import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry"
import {
  trace,
  SpanKind,
  SpanStatusCode,
  type Span,
  type Tracer,
} from "@opentelemetry/api"
import { NodeSDK } from "@opentelemetry/sdk-node"
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-http"
import { Resource } from "@opentelemetry/resources"

const PLUGIN_VERSION = "0.1.0"
const DEFAULT_AGENT_NAME = "openclaw-agent"
const LOG = "[Oversee]"

// We're observation-only and want to run after any behavior-modifying hooks.
// Per docs, higher priority runs first, so a low value (0) keeps us last.
const OBSERVATION_PRIORITY = 0

// ----------------------------------------------------------------------------
// Types
// ----------------------------------------------------------------------------

interface OpenClawApi {
  on<E = unknown, C = OpenClawContext>(
    name: string,
    handler: (event: E, ctx: C) => void | Promise<void>,
    opts?: { priority?: number; timeoutMs?: number },
  ): void
  config?: Record<string, any>
  getPluginConfig?(pluginId: string): any
  version?: string
  gateway?: { version?: string }
}

interface OpenClawContext {
  sessionKey?: string
  sessionId?: string
  runId?: string
  jobId?: string
  messageId?: string
  senderId?: string
  agentId?: string
  traceId?: string
  spanId?: string
  parentSpanId?: string
  callDepth?: number
  messageProvider?: string
  channelId?: string
  trace?: unknown
}

interface MessageReceivedEvent {
  content?: string
  sender?: unknown
  threadId?: string
  messageId?: string
  senderId?: string
  metadata?: Record<string, unknown>
}

interface MessageSentEvent {
  success?: boolean
  error?: unknown
}

interface BeforeToolCallEvent {
  toolName: string
  params?: Record<string, unknown>
  toolCallId: string
  runId?: string
  derivedPaths?: unknown
}

interface AfterToolCallEvent {
  toolCallId: string
  toolName?: string
  success?: boolean
  error?: unknown
  durationMs?: number
}

interface ModelCallStartedEvent {
  runId?: string
  callId: string
  provider?: string
  model?: string
  api?: string
  transport?: string
}

interface ModelCallEndedEvent {
  runId?: string
  callId: string
  provider?: string
  model?: string
  durationMs: number
  outcome: string
  upstreamRequestIdHash?: string
}

interface AgentEndEvent {
  runId?: string
  success?: boolean
  error?: unknown
}

interface OverseeConfig {
  endpoint: string
  agentName: string
  enabled: boolean
}

// ----------------------------------------------------------------------------
// Config
// ----------------------------------------------------------------------------

function readConfig(api: OpenClawApi): OverseeConfig | null {
  let raw: any
  if (api?.config && typeof api.config === "object") {
    raw = api.config
  } else if (typeof api?.getPluginConfig === "function") {
    try {
      raw = api.getPluginConfig("oversee")
    } catch {
      // Fall through to env-var fallback.
    }
  }

  const endpoint = raw?.endpoint ?? process.env.OVERSEE_ENDPOINT
  if (!endpoint) {
    console.warn(
      `${LOG} No endpoint configured. Set plugins.entries.oversee.endpoint ` +
        `in openclaw.json or export OVERSEE_ENDPOINT. Plugin will not emit telemetry.`,
    )
    return null
  }

  return {
    endpoint,
    agentName:
      raw?.agentName ?? process.env.OVERSEE_AGENT_NAME ?? DEFAULT_AGENT_NAME,
    enabled: raw?.enabled ?? true,
  }
}

// ----------------------------------------------------------------------------
// OpenTelemetry bootstrap
// ----------------------------------------------------------------------------

function initTelemetry(config: OverseeConfig, gatewayVersion: string): Tracer {
  const resource = new Resource({
    "service.name": config.agentName,
    "service.version": PLUGIN_VERSION,
    "oversee.plugin.version": PLUGIN_VERSION,
    "openclaw.gateway.version": gatewayVersion,
  })

  const exporter = new OTLPTraceExporter({ url: config.endpoint })

  const sdk = new NodeSDK({
    resource,
    traceExporter: exporter,
  })

  // NodeSDK installs a BatchSpanProcessor by default — batching, retries, and
  // back-pressure all happen off the gateway's hot path. We never block.
  sdk.start()

  // Flush buffered spans on shutdown.
  const shutdown = () => {
    sdk.shutdown().catch((err) => {
      console.warn(`${LOG} Error during OTEL shutdown:`, err)
    })
  }
  process.once("SIGINT", shutdown)
  process.once("SIGTERM", shutdown)

  return trace.getTracer("@oversee/openclaw-plugin", PLUGIN_VERSION)
}

// ----------------------------------------------------------------------------
// Hook registration helper
// ----------------------------------------------------------------------------

/**
 * Register a hook with two layers of defense: catch errors at registration
 * time, and catch any exception thrown inside the handler. The docs note
 * that the hook runner already isolates handler failures, but a try/catch
 * inside our handler guarantees we never spend the platform's error budget
 * for telemetry.
 */
function safeOn<E, C>(
  api: OpenClawApi,
  name: string,
  handler: (event: E, ctx: C) => void,
): void {
  try {
    api.on<E, C>(
      name,
      (event, ctx) => {
        try {
          handler(event, ctx)
        } catch (e) {
          console.warn(`${LOG} Handler for '${name}' threw:`, e)
        }
      },
      { priority: OBSERVATION_PRIORITY },
    )
  } catch (e) {
    console.warn(
      `${LOG} Could not register handler for '${name}': ${(e as Error).message}. Skipping.`,
    )
  }
}

function setIfPresent(span: Span, key: string, value: unknown): void {
  if (value === undefined || value === null) return
  if (typeof value === "string" && value.length === 0) return
  span.setAttribute(key, value as string | number | boolean)
}

// ----------------------------------------------------------------------------
// Hook wiring
// ----------------------------------------------------------------------------

function wireEvents(api: OpenClawApi, tracer: Tracer): void {
  // Correlation maps: start/end event pairs are joined into a single span
  // by their canonical id (toolCallId, callId).
  const toolSpans = new Map<string, { span: Span; startedAt: number }>()
  const modelSpans = new Map<string, { span: Span; startedAt: number }>()

  // -- Inbound messages --
  safeOn<MessageReceivedEvent, OpenClawContext>(
    api,
    "message_received",
    (event, ctx) => {
      const span = tracer.startSpan("message_received", {
        kind: SpanKind.SERVER,
      })
      span.setAttribute("oversee.event.type", "message_received")
      setIfPresent(span, "oversee.session.key", ctx?.sessionKey)
      setIfPresent(
        span,
        "oversee.message.sender_id",
        event?.senderId ?? ctx?.senderId,
      )
      setIfPresent(span, "oversee.message.thread_id", event?.threadId)
      setIfPresent(
        span,
        "oversee.message.content_length",
        typeof event?.content === "string" ? event.content.length : undefined,
      )
      setIfPresent(span, "oversee.trace.id", ctx?.traceId)
      setIfPresent(span, "oversee.trace.span_id", ctx?.spanId)
      setIfPresent(span, "oversee.trace.parent_span_id", ctx?.parentSpanId)
      span.end()
    },
  )

  // -- Outbound delivery --
  safeOn<MessageSentEvent, OpenClawContext>(
    api,
    "message_sent",
    (event, ctx) => {
      const span = tracer.startSpan("message_sent", {
        kind: SpanKind.CLIENT,
      })
      const success = event?.success ?? !event?.error
      span.setAttribute("oversee.event.type", "message_sent")
      setIfPresent(span, "oversee.session.key", ctx?.sessionKey)
      span.setAttribute("oversee.delivery.success", Boolean(success))
      if (!success) {
        span.setStatus({
          code: SpanStatusCode.ERROR,
          message: typeof event?.error === "string" ? event.error : "delivery failed",
        })
      }
      span.end()
    },
  )

  // -- Tool calls (before / after pair) --
  safeOn<BeforeToolCallEvent, OpenClawContext>(
    api,
    "before_tool_call",
    (event, ctx) => {
      const span = tracer.startSpan("tool_call", {
        kind: SpanKind.INTERNAL,
      })
      span.setAttribute("oversee.event.type", "tool_call")
      span.setAttribute("oversee.tool.name", event.toolName)
      span.setAttribute("oversee.tool.call_id", event.toolCallId)
      // PRIVACY: parameter KEYS only — values are never read.
      span.setAttribute(
        "oversee.tool.param_keys",
        JSON.stringify(Object.keys(event?.params ?? {})),
      )
      setIfPresent(span, "oversee.agent.id", ctx?.agentId)
      setIfPresent(span, "oversee.run.id", event?.runId ?? ctx?.runId)

      toolSpans.set(event.toolCallId, { span, startedAt: Date.now() })
      // Returning undefined is required by the hook contract — we never
      // block, rewrite params, or gate the call.
    },
  )

  safeOn<AfterToolCallEvent, OpenClawContext>(
    api,
    "after_tool_call",
    (event) => {
      const entry = toolSpans.get(event.toolCallId)
      if (!entry) return
      toolSpans.delete(event.toolCallId)

      const success = event?.success ?? !event?.error
      const duration = event?.durationMs ?? Date.now() - entry.startedAt
      entry.span.setAttribute("oversee.tool.success", Boolean(success))
      entry.span.setAttribute("oversee.tool.duration_ms", duration)
      if (!success) {
        entry.span.setStatus({
          code: SpanStatusCode.ERROR,
          message: typeof event?.error === "string" ? event.error : "tool failed",
        })
      }
      entry.span.end()
    },
  )

  // -- Model calls (started / ended pair) --
  safeOn<ModelCallStartedEvent, OpenClawContext>(
    api,
    "model_call_started",
    (event, ctx) => {
      const span = tracer.startSpan("model_call", {
        kind: SpanKind.CLIENT,
      })
      span.setAttribute("oversee.event.type", "model_call")
      setIfPresent(span, "gen_ai.system", event?.provider)
      setIfPresent(span, "gen_ai.request.model", event?.model)
      span.setAttribute("oversee.model.call_id", event.callId)
      setIfPresent(span, "oversee.run.id", event?.runId ?? ctx?.runId)

      modelSpans.set(event.callId, { span, startedAt: Date.now() })
    },
  )

  safeOn<ModelCallEndedEvent, OpenClawContext>(
    api,
    "model_call_ended",
    (event) => {
      const entry = modelSpans.get(event.callId)
      if (!entry) return
      modelSpans.delete(event.callId)

      entry.span.setAttribute("oversee.model.duration_ms", event.durationMs)
      setIfPresent(entry.span, "oversee.model.outcome", event.outcome)
      // Outcomes other than "ok" / "success" are treated as errors so they
      // show up in the dashboard's error counts.
      if (
        typeof event.outcome === "string" &&
        event.outcome !== "ok" &&
        event.outcome !== "success"
      ) {
        entry.span.setStatus({
          code: SpanStatusCode.ERROR,
          message: event.outcome,
        })
      }
      entry.span.end()
    },
  )

  // -- Agent run completion --
  safeOn<AgentEndEvent, OpenClawContext>(api, "agent_end", (event, ctx) => {
    const span = tracer.startSpan("agent_run_complete", {
      kind: SpanKind.INTERNAL,
    })
    span.setAttribute("oversee.event.type", "agent_run_complete")
    setIfPresent(span, "oversee.run.id", event?.runId ?? ctx?.runId)

    const success = event?.success ?? !event?.error
    if (typeof success === "boolean") {
      span.setAttribute("oversee.run.success", success)
      if (!success) {
        span.setStatus({
          code: SpanStatusCode.ERROR,
          message: typeof event?.error === "string" ? event.error : "run failed",
        })
      }
    }
    setIfPresent(span, "oversee.run.message_provider", ctx?.messageProvider)
    setIfPresent(span, "oversee.run.channel_id", ctx?.channelId)
    setIfPresent(span, "oversee.run.job_id", ctx?.jobId)
    span.end()
  })
}

// ----------------------------------------------------------------------------
// Plugin entry
// ----------------------------------------------------------------------------

export default definePluginEntry({
  id: "oversee",
  name: "Oversee Agent Management",
  description:
    "Automatic agent telemetry for Oversee — the Agent Management System",
  register(api: OpenClawApi) {
    const config = readConfig(api)
    if (!config) return

    if (!config.enabled) {
      console.log(
        `${LOG} Plugin disabled via config (enabled=false). Skipping telemetry.`,
      )
      return
    }

    if (typeof api?.on !== "function") {
      console.warn(
        `${LOG} OpenClaw api.on() not available. Cannot register event ` +
          `handlers. Plugin will not emit telemetry.`,
      )
      return
    }

    const gatewayVersion =
      api?.version ?? api?.gateway?.version ?? "unknown"

    const tracer = initTelemetry(config, gatewayVersion)
    wireEvents(api, tracer)

    console.log(
      `${LOG} Plugin initialized. Sending telemetry to ${config.endpoint} ` +
        `as service '${config.agentName}'`,
    )
  },
})
