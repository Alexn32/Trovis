/**
 * @oversee/openclaw-plugin
 * ============================================================================
 * Automatic agent telemetry for Oversee. Captures messages, tool calls, LLM
 * calls, and run completion, and forwards them to the configured Oversee
 * endpoint as OpenTelemetry traces. Also reads agent identity files
 * (SOUL.md, IDENTITY.md, AGENTS.md, USER.md, MEMORY.md) at startup and
 * sends them as an `agent_registration` span so the dashboard knows what
 * each agent is supposed to be doing.
 *
 * All event names, payload shapes, and context fields are from confirmed
 * OpenClaw plugin-hooks docs.
 *
 * Privacy:
 *   - Conversation telemetry captures metadata only: message content
 *     lengths (not content), tool names (not parameter values), model
 *     provider/name, durations, success/failure. Never content or values.
 *   - SOUL.md / IDENTITY.md / AGENTS.md are sent in full on startup
 *     (truncated to 32 KB each) because they define what the agent is and
 *     are needed for accurate descriptions.
 *   - USER.md and MEMORY.md may contain personal data. They are NOT read
 *     by default — operator must opt in via config.readUserData=true.
 *   - The plugin is inert until an endpoint is explicitly configured.
 *     There is no hardcoded fallback URL.
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
import { execFile } from "node:child_process"
import * as fs from "node:fs"
import * as path from "node:path"
import * as os from "node:os"

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const PLUGIN_VERSION = "0.2.0"
// No hardcoded default endpoint — the plugin is inert until the operator
// explicitly configures where telemetry should go.
const DEFAULT_AGENT_NAME = "openclaw-agent"
const LOG = "[Oversee]"
const OBSERVATION_PRIORITY = 0
// OTLP backends typically cap individual attribute values; 32 KB keeps us
// well inside Oversee's TEXT column and most other backends' limits.
const ATTR_BYTE_LIMIT = 32 * 1024

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface PluginConfig {
  endpoint?: string
  agentName?: string
  apiKey?: string
  enabled?: boolean
  readUserData?: boolean
  captureOutputs?: boolean
}

interface OpenClawContext {
  pluginConfig?: PluginConfig

  // Common request / conversation context.
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

  // gateway_start-specific context.
  workspaceDir?: string
  config?: {
    agents?: {
      defaults?: {
        workspace?: string
        model?: { primary?: string }
      }
      list?: Array<{
        id?: string
        workspace?: string
        model?: { primary?: string }
      }>
    }
  }
}

interface BaseEvent {
  context: OpenClawContext
}

type GatewayStartEvent = BaseEvent

interface MessageReceivedEvent extends BaseEvent {
  content?: string
  threadId?: string
  messageId?: string
  senderId?: string
  metadata?: Record<string, unknown>
}

interface MessageSentEvent extends BaseEvent {
  success?: boolean
  error?: unknown
  // Optional outbound text. Captured into a span attribute only when
  // state.captureOutputs is true.
  content?: string
}

interface BeforeToolCallEvent extends BaseEvent {
  toolName: string
  toolCallId: string
  params?: Record<string, unknown>
  runId?: string
}

interface AfterToolCallEvent extends BaseEvent {
  toolCallId: string
  toolName?: string
  success?: boolean
  error?: unknown
  durationMs?: number
  // Optional tool return value. Captured into a span attribute only
  // when state.captureOutputs is true. May be a string, object, or
  // anything else JSON-serializable.
  result?: unknown
}

interface ModelCallStartedEvent extends BaseEvent {
  callId: string
  runId?: string
  provider?: string
  model?: string
}

interface ModelCallEndedEvent extends BaseEvent {
  callId: string
  runId?: string
  provider?: string
  model?: string
  durationMs: number
  outcome: string
}

interface AgentEndEvent extends BaseEvent {
  runId?: string
  success?: boolean
  error?: unknown
}

interface CommandResult {
  content: Array<{ type: string; text: string }>
}

interface PluginCommand {
  name: string
  aliases?: string[]
  description: string
  execute(args: string[], context?: unknown): Promise<CommandResult>
}

interface OpenClawApi {
  on<E>(
    name: string,
    handler: (event: E) => void | Promise<void>,
    opts?: { priority?: number; timeoutMs?: number },
  ): void
  // Optional — older gateways may not support slash commands. wireCommands()
  // feature-detects before calling.
  registerCommand?(cmd: PluginCommand): void
  version?: string
  gateway?: { version?: string }
}

// ---------------------------------------------------------------------------
// Module state
// ---------------------------------------------------------------------------
//
// We init OTEL lazily on the first hook that exposes pluginConfig. In
// practice this is gateway_start, but other hooks act as a safety net if
// for any reason gateway_start doesn't provide pluginConfig.

const state: {
  initialized: boolean
  disabled: boolean
  tracer: Tracer | null
  sdk: NodeSDK | null
  endpoint: string
  agentName: string
  apiKey: string | undefined
  readUserData: boolean
  captureOutputs: boolean
  gatewayVersion: string
} = {
  initialized: false,
  disabled: false,
  tracer: null,
  sdk: null,
  endpoint: "",
  agentName: DEFAULT_AGENT_NAME,
  apiKey: undefined,
  readUserData: false,
  captureOutputs: false,
  gatewayVersion: "unknown",
}

// ---------------------------------------------------------------------------
// OpenTelemetry bootstrap
// ---------------------------------------------------------------------------

function initTelemetry(
  endpoint: string,
  agentName: string,
  apiKey: string | undefined,
  gatewayVersion: string,
): Tracer {
  // Resource attributes are EXPLICITLY set — no auto-detectors. This
  // prevents OTEL's default resource detectors from reading host
  // identifiers (`/etc/hostid`, hostname, MAC-derived UUIDs) and
  // process metadata (PID, runtime version) and shipping them in every
  // span's resource attributes.
  //
  // Kept beyond just service.name: version metadata that's deliberately
  // emitted by THIS plugin (not auto-detected). The dashboard's
  // platform-detection logic relies on `openclaw.gateway.version` to
  // identify OpenClaw agents — dropping it would make every OpenClaw
  // install register as a generic "OpenTelemetry Agent" in the UI.
  // These four fields are the entire resource attribute footprint.
  const resource = new Resource({
    "service.name": agentName,
    "service.version": PLUGIN_VERSION,
    "oversee.plugin.version": PLUGIN_VERSION,
    "openclaw.gateway.version": gatewayVersion,
  })

  const exporter = new OTLPTraceExporter({
    url: endpoint,
    headers: apiKey ? { "X-Oversee-Api-Key": apiKey } : undefined,
  })

  const sdk = new NodeSDK({
    resource,
    traceExporter: exporter,
    // The actual fix for the hostid concern: NodeSDK normally merges
    // our resource with the output of default detectors (host, process,
    // env). autoDetectResources: false skips that merge so only the
    // attributes above are shipped.
    autoDetectResources: false,
  })
  sdk.start()
  state.sdk = sdk

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

function ensureInit(ctx: OpenClawContext | undefined): Tracer | null {
  if (state.disabled) return null
  if (state.initialized) return state.tracer

  // Defense-in-depth: register() also checks OVERSEE_ENABLED=false and
  // bails before wiring hooks, so this path is rarely reached. But if
  // for some reason hooks WERE wired and the operator set the env var
  // late, this stops any telemetry from going out.
  if (process.env.OVERSEE_ENABLED === "false") {
    state.disabled = true
    console.log(`${LOG} Plugin disabled via OVERSEE_ENABLED=false`)
    return null
  }

  const pluginConfig = ctx?.pluginConfig

  // Disabled via openclaw.json takes precedence over any other config.
  if (pluginConfig?.enabled === false) {
    state.disabled = true
    console.log(`${LOG} Plugin disabled via config.`)
    return null
  }

  // No hardcoded default — operator must opt in by configuring an endpoint.
  // If we get this far without one, mark disabled so we don't log on every
  // subsequent hook firing.
  const endpoint = pluginConfig?.endpoint ?? process.env.OVERSEE_ENDPOINT
  if (!endpoint) {
    state.disabled = true
    console.log(
      `${LOG} No endpoint configured. Set plugins.entries.oversee.config.endpoint to enable telemetry.`,
    )
    return null
  }

  state.endpoint = endpoint
  state.agentName =
    pluginConfig?.agentName ??
    process.env.OVERSEE_AGENT_NAME ??
    DEFAULT_AGENT_NAME
  state.apiKey = pluginConfig?.apiKey ?? process.env.OVERSEE_API_KEY
  // USER.md and MEMORY.md may carry personal data; the operator has to
  // explicitly opt in to having those files shipped to Oversee.
  state.readUserData = Boolean(pluginConfig?.readUserData)
  // Message content / tool outputs are opt-in for the same reason. Use
  // ?? (not ||) so an explicit `false` in pluginConfig overrides a
  // `true` env var.
  state.captureOutputs = Boolean(
    pluginConfig?.captureOutputs ??
      (process.env.OVERSEE_CAPTURE_OUTPUTS === "true"),
  )

  state.tracer = initTelemetry(
    state.endpoint,
    state.agentName,
    state.apiKey,
    state.gatewayVersion,
  )
  state.initialized = true

  console.log(
    `${LOG} Plugin initialized. Sending telemetry to ${state.endpoint} ` +
      `as service '${state.agentName}'` +
      (state.readUserData ? " (readUserData=true)" : ""),
  )
  console.log(
    `${LOG} Output capture: ${state.captureOutputs ? "enabled" : "disabled"}`,
  )

  return state.tracer
}

// ---------------------------------------------------------------------------
// Agent identity reading
// ---------------------------------------------------------------------------

function readFileOrEmpty(workspacePath: string, filename: string): string {
  try {
    return fs.readFileSync(path.join(workspacePath, filename), "utf-8")
  } catch {
    return ""
  }
}

function truncate(s: string, limit: number = ATTR_BYTE_LIMIT): string {
  if (s.length <= limit) return s
  return s.slice(0, limit) + "[truncated]"
}

function resolveDefaultWorkspace(): string {
  const dockerPath = "/data/.openclaw/workspace"
  try {
    if (fs.existsSync(dockerPath)) return dockerPath
  } catch {
    // fall through to home-dir default
  }
  return path.join(os.homedir(), ".openclaw", "workspace")
}

function sendAgentRegistration(
  tracer: Tracer,
  agentId: string,
  workspacePath: string,
  model: string,
): void {
  const span = tracer.startSpan("agent_registration", {
    kind: SpanKind.INTERNAL,
  })

  // Purpose-defining files — always read once the plugin is initialized.
  const soul = readFileOrEmpty(workspacePath, "SOUL.md")
  const identity = readFileOrEmpty(workspacePath, "IDENTITY.md")
  const operatingManual = readFileOrEmpty(workspacePath, "AGENTS.md")

  // Files that may contain personal data — only read with explicit consent
  // via config.readUserData.
  const userContext = state.readUserData
    ? readFileOrEmpty(workspacePath, "USER.md")
    : ""
  const memory = state.readUserData
    ? readFileOrEmpty(workspacePath, "MEMORY.md")
    : ""

  span.setAttribute("oversee.event.type", "agent_registration")
  span.setAttribute("oversee.agent.id", agentId)
  span.setAttribute("oversee.agent.workspace_path", workspacePath)
  span.setAttribute("oversee.agent.model", model || "unknown")
  if (soul) span.setAttribute("oversee.agent.soul", truncate(soul))
  if (identity) span.setAttribute("oversee.agent.identity", truncate(identity))
  if (operatingManual)
    span.setAttribute("oversee.agent.operating_manual", truncate(operatingManual))
  if (userContext)
    span.setAttribute("oversee.agent.user_context", truncate(userContext))
  if (memory) span.setAttribute("oversee.agent.memory", truncate(memory))

  span.end()

  console.log(
    `${LOG} Registered agent '${agentId}' from workspace ${workspacePath} ` +
      `(soul: ${soul.length}b, identity: ${identity.length}b)`,
  )
}

function registerAgents(tracer: Tracer, ctx: OpenClawContext): void {
  const defaults = ctx?.config?.agents?.defaults
  const list = ctx?.config?.agents?.list
  const defaultWorkspace =
    ctx?.workspaceDir ?? defaults?.workspace ?? resolveDefaultWorkspace()
  const defaultModel = defaults?.model?.primary ?? "unknown"

  if (Array.isArray(list) && list.length > 0) {
    for (const agent of list) {
      const ws = agent?.workspace ?? defaultWorkspace
      const id = agent?.id ?? "unknown"
      const model = agent?.model?.primary ?? defaultModel
      sendAgentRegistration(tracer, id, ws, model)
    }
  } else {
    sendAgentRegistration(tracer, "main", defaultWorkspace, defaultModel)
  }

  // Previously we called sdk.forceFlush() here for snappier dashboard
  // visibility, but NodeSDK doesn't expose forceFlush() — calling it
  // threw inside gateway_start and blocked the plugin from emitting the
  // registration span at all. The BatchSpanProcessor auto-flushes on
  // its own schedule (default 5s), which is fine for startup. The first
  // registration just lags by a few seconds rather than appearing
  // instantly.
}

// ---------------------------------------------------------------------------
// Hook registration helper
// ---------------------------------------------------------------------------

function safeOn<E>(
  api: OpenClawApi,
  name: string,
  handler: (event: E) => void,
): void {
  try {
    api.on<E>(
      name,
      (event) => {
        try {
          handler(event)
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

/**
 * Best-effort agent ID lookup. Different OpenClaw hook payloads put it in
 * different places — sometimes on the event payload directly, sometimes
 * only on the context. We try both. The backend uses this attribute to
 * split multi-agent gateways into separate dashboard entries
 * (`<service.name>-<agent_id>` when agent_id isn't 'main'), so getting
 * it on every span is what makes multi-agent OpenClaw work end-to-end.
 */
function pickAgentId(event: unknown, ctx: OpenClawContext): string | undefined {
  const fromEvent = (event as { agentId?: unknown })?.agentId
  if (typeof fromEvent === "string" && fromEvent.length > 0) return fromEvent
  if (typeof ctx?.agentId === "string" && ctx.agentId.length > 0) {
    return ctx.agentId
  }
  return undefined
}

// ---------------------------------------------------------------------------
// Hook wiring
// ---------------------------------------------------------------------------

function wireEvents(api: OpenClawApi): void {
  // Correlation maps for start/end event pairs.
  const toolSpans = new Map<string, { span: Span; startedAt: number }>()
  const modelSpans = new Map<string, { span: Span; startedAt: number }>()

  // -- Gateway start: init OTEL + read agent identity files --
  safeOn<GatewayStartEvent>(api, "gateway_start", (event) => {
    const tracer = ensureInit(event?.context)
    if (!tracer) return
    registerAgents(tracer, event?.context ?? ({} as OpenClawContext))
  })

  // -- Inbound messages --
  safeOn<MessageReceivedEvent>(api, "message_received", (event) => {
    const tracer = ensureInit(event?.context)
    if (!tracer) return
    const ctx = event?.context ?? ({} as OpenClawContext)
    const span = tracer.startSpan("message_received", { kind: SpanKind.SERVER })
    span.setAttribute("oversee.event.type", "message_received")
    setIfPresent(span, "oversee.session.key", ctx.sessionKey)
    setIfPresent(
      span,
      "oversee.message.sender_id",
      event?.senderId ?? ctx.senderId,
    )
    setIfPresent(span, "oversee.message.thread_id", event?.threadId)
    setIfPresent(
      span,
      "oversee.message.content_length",
      typeof event?.content === "string" ? event.content.length : undefined,
    )
    setIfPresent(span, "oversee.trace.id", ctx.traceId)
    setIfPresent(span, "oversee.trace.span_id", ctx.spanId)
    setIfPresent(span, "oversee.trace.parent_span_id", ctx.parentSpanId)
    // Multi-agent gateways: the backend uses this to split spans into
    // per-agent virtual service names (`<service>-<agent_id>`).
    setIfPresent(span, "oversee.agent.id", pickAgentId(event, ctx))
    // Capture inbound message text when the operator opted in.
    if (
      state.captureOutputs &&
      typeof event?.content === "string" &&
      event.content.length > 0
    ) {
      span.setAttribute(
        "oversee.message.content",
        truncate(event.content, 10_000),
      )
    }
    span.end()
  })

  // -- Outbound delivery --
  safeOn<MessageSentEvent>(api, "message_sent", (event) => {
    const tracer = ensureInit(event?.context)
    if (!tracer) return
    const ctx = event?.context ?? ({} as OpenClawContext)
    const span = tracer.startSpan("message_sent", { kind: SpanKind.CLIENT })
    const success = event?.success ?? !event?.error
    span.setAttribute("oversee.event.type", "message_sent")
    setIfPresent(span, "oversee.session.key", ctx.sessionKey)
    setIfPresent(span, "oversee.agent.id", pickAgentId(event, ctx))
    span.setAttribute("oversee.delivery.success", Boolean(success))
    if (!success) {
      span.setStatus({
        code: SpanStatusCode.ERROR,
        message:
          typeof event?.error === "string" ? event.error : "delivery failed",
      })
    }
    // Capture outbound response body when opted in.
    if (
      state.captureOutputs &&
      typeof event?.content === "string" &&
      event.content.length > 0
    ) {
      span.setAttribute(
        "oversee.response.content",
        truncate(event.content, 10_000),
      )
    }
    span.end()
  })

  // -- Tool calls (before / after pair) --
  safeOn<BeforeToolCallEvent>(api, "before_tool_call", (event) => {
    const tracer = ensureInit(event?.context)
    if (!tracer) return
    const ctx = event?.context ?? ({} as OpenClawContext)
    const span = tracer.startSpan("tool_call", { kind: SpanKind.INTERNAL })
    span.setAttribute("oversee.event.type", "tool_call")
    span.setAttribute("oversee.tool.name", event.toolName)
    span.setAttribute("oversee.tool.call_id", event.toolCallId)
    // PRIVACY: parameter KEYS only — values are never read.
    span.setAttribute(
      "oversee.tool.param_keys",
      JSON.stringify(Object.keys(event?.params ?? {})),
    )
    setIfPresent(span, "oversee.agent.id", pickAgentId(event, ctx))
    setIfPresent(span, "oversee.run.id", event?.runId ?? ctx.runId)

    toolSpans.set(event.toolCallId, { span, startedAt: Date.now() })
  })

  safeOn<AfterToolCallEvent>(api, "after_tool_call", (event) => {
    ensureInit(event?.context)
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
        message:
          typeof event?.error === "string" ? event.error : "tool failed",
      })
    }
    // Capture the tool's return value when opted in. Strings pass through
    // as-is; anything else gets JSON.stringify'd. Truncated to 10 000
    // chars to stay safely inside OTLP attribute limits.
    if (
      state.captureOutputs &&
      event?.result !== undefined &&
      event.result !== null
    ) {
      let raw: string | null = null
      if (typeof event.result === "string") {
        raw = event.result
      } else {
        try {
          raw = JSON.stringify(event.result)
        } catch {
          raw = null
        }
      }
      if (typeof raw === "string" && raw.length > 0) {
        entry.span.setAttribute("oversee.tool.result", truncate(raw, 10_000))
      }
    }
    entry.span.end()
  })

  // -- Model calls (started / ended pair) --
  safeOn<ModelCallStartedEvent>(api, "model_call_started", (event) => {
    const tracer = ensureInit(event?.context)
    if (!tracer) return
    const ctx = event?.context ?? ({} as OpenClawContext)
    const span = tracer.startSpan("model_call", { kind: SpanKind.CLIENT })
    span.setAttribute("oversee.event.type", "model_call")
    setIfPresent(span, "gen_ai.system", event?.provider)
    setIfPresent(span, "gen_ai.request.model", event?.model)
    span.setAttribute("oversee.model.call_id", event.callId)
    setIfPresent(span, "oversee.agent.id", pickAgentId(event, ctx))
    setIfPresent(span, "oversee.run.id", event?.runId ?? ctx.runId)

    modelSpans.set(event.callId, { span, startedAt: Date.now() })
  })

  safeOn<ModelCallEndedEvent>(api, "model_call_ended", (event) => {
    ensureInit(event?.context)
    const entry = modelSpans.get(event.callId)
    if (!entry) return
    modelSpans.delete(event.callId)

    entry.span.setAttribute("oversee.model.duration_ms", event.durationMs)
    setIfPresent(entry.span, "oversee.model.outcome", event.outcome)
    if (
      typeof event.outcome === "string" &&
      event.outcome !== "ok" &&
      event.outcome !== "success"
    ) {
      entry.span.setStatus({ code: SpanStatusCode.ERROR, message: event.outcome })
    }
    entry.span.end()
  })

  // -- Agent run completion --
  safeOn<AgentEndEvent>(api, "agent_end", (event) => {
    const tracer = ensureInit(event?.context)
    if (!tracer) return
    const ctx = event?.context ?? ({} as OpenClawContext)
    const span = tracer.startSpan("agent_run_complete", {
      kind: SpanKind.INTERNAL,
    })
    span.setAttribute("oversee.event.type", "agent_run_complete")
    setIfPresent(span, "oversee.agent.id", pickAgentId(event, ctx))
    setIfPresent(span, "oversee.run.id", event?.runId ?? ctx.runId)

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
    setIfPresent(span, "oversee.run.message_provider", ctx.messageProvider)
    setIfPresent(span, "oversee.run.channel_id", ctx.channelId)
    setIfPresent(span, "oversee.run.job_id", ctx.jobId)
    span.end()
  })
}

// ---------------------------------------------------------------------------
// Command wiring
// ---------------------------------------------------------------------------
//
// /oversee is the user-facing setup command. Same UX pattern as channel
// plugins like /telegram or /whatsapp: the plugin knows what it needs, the
// command tells the user how to provide it, and `/oversee status` reflects
// the live connection state.

/** Mask an API key for display in chat — first 6 chars + "…" + last 4. */
function maskKey(key: string | undefined): string {
  if (!key) return "(not set)"
  if (key.length <= 10) return key
  return `${key.slice(0, 6)}…${key.slice(-4)}`
}

/**
 * Best-effort persist of a single config field to openclaw.json by
 * shelling out to the gateway's own CLI. Uses execFile (not exec) so a
 * user-supplied URL or key can't inject shell metacharacters. If the
 * `openclaw config set` subcommand doesn't exist or fails, we log and
 * fall back to the in-memory state change only.
 */
function persistConfig(key: string, value: string | boolean): void {
  execFile(
    "openclaw",
    [
      "config",
      "set",
      `plugins.entries.oversee.config.${key}`,
      String(value),
    ],
    (err) => {
      if (err) {
        console.log(
          `${LOG} Could not persist ${key} via 'openclaw config set' — ` +
            `applied for this session only.`,
        )
      }
    },
  )
}

function wireCommands(api: OpenClawApi): void {
  if (typeof api?.registerCommand !== "function") {
    // Older gateways without command support — skip silently.
    return
  }

  const command = {
    name: "oversee",
    aliases: ["ov"],
    description: "Connect to Oversee agent monitoring",
    async execute(args: string[], _context: unknown): Promise<CommandResult> {
      const sub = args[0]?.toLowerCase()
      const reply = (text: string): CommandResult => ({
        content: [{ type: "text", text }],
      })

      // --- connect <url> -------------------------------------------------
      if (sub === "connect" && args[1]) {
        const endpoint = args[1]
        state.endpoint = endpoint
        persistConfig("endpoint", endpoint)
        return reply(
          `✅ Oversee endpoint set: \`${endpoint}\`\n\n` +
            `In-memory state updated and saved to your openclaw.json ` +
            `(best-effort). The OTLP exporter was constructed at gateway ` +
            `start, so **restart the gateway** for spans to actually go to ` +
            `this URL.`,
        )
      }

      // --- apikey <key> --------------------------------------------------
      if (sub === "apikey" && args[1]) {
        const key = args[1]
        state.apiKey = key
        persistConfig("apiKey", key)
        return reply(
          `✅ Oversee API key set: \`${maskKey(key)}\`\n\n` +
            `Saved to your openclaw.json (best-effort). The auth header ` +
            `is set on the exporter at gateway start, so **restart the ` +
            `gateway** for the new key to be sent.`,
        )
      }

      // --- capture on|off ------------------------------------------------
      if (sub === "capture" && (args[1] === "on" || args[1] === "off")) {
        const enable = args[1] === "on"
        state.captureOutputs = enable
        persistConfig("captureOutputs", enable)
        return reply(
          `✅ Output capture **${enable ? "enabled" : "disabled"}**.\n\n` +
            `Takes effect immediately for new events. ` +
            (enable
              ? `Message content and tool results will now appear on ` +
                `spans as \`oversee.message.content\`, ` +
                `\`oversee.response.content\`, and \`oversee.tool.result\` ` +
                `(each truncated to 10 000 chars).`
              : `Message content and tool results will no longer be ` +
                `captured. Existing spans aren't modified.`),
        )
      }

      // --- userdata on|off -----------------------------------------------
      if (sub === "userdata" && (args[1] === "on" || args[1] === "off")) {
        const enable = args[1] === "on"
        state.readUserData = enable
        persistConfig("readUserData", enable)
        return reply(
          `✅ User data ingestion **${enable ? "enabled" : "disabled"}**.\n\n` +
            `Saved (best-effort). USER.md and MEMORY.md are read at ` +
            `gateway start during agent registration, so **restart the ` +
            `gateway** for this to take effect on the registration spans.`,
        )
      }

      // --- settings ------------------------------------------------------
      if (sub === "settings") {
        const lines = [
          `⚙️ **Oversee Settings**`,
          ``,
          `• Endpoint: \`${state.endpoint || "(not set)"}\``,
          `• API key: \`${maskKey(state.apiKey)}\``,
          `• Agent name: \`${state.agentName}\``,
          `• Capture outputs: **${state.captureOutputs ? "on" : "off"}**`,
          `• User data: **${state.readUserData ? "on" : "off"}**`,
          `• Telemetry: ${state.initialized ? "flowing" : "not initialized"}`,
        ]
        return reply(lines.join("\n"))
      }

      // --- status --------------------------------------------------------
      if (sub === "status") {
        const enabled = state.initialized
        return reply(
          enabled
            ? `✅ Oversee is active.\n\n` +
                `• Endpoint: \`${state.endpoint}\`\n` +
                `• Agent: \`${state.agentName}\`\n` +
                `• Telemetry: flowing`
            : `⚠️ Oversee is not connected.\n\n` +
                `Get your endpoint URL from your Oversee dashboard ` +
                `(Add Agent → OpenClaw), then run:\n\n` +
                `\`/oversee connect <your-endpoint-url>\``,
        )
      }

      // --- default / help ------------------------------------------------
      return reply(
        `🔍 **Oversee Agent Monitoring**\n\n` +
          `**Setup**\n` +
          `• \`/oversee connect <url>\` — set the Oversee endpoint\n` +
          `• \`/oversee apikey <key>\` — set your API key\n\n` +
          `**Capture toggles**\n` +
          `• \`/oversee capture on\` / \`off\` — message + tool output capture (default off)\n` +
          `• \`/oversee userdata on\` / \`off\` — USER.md + MEMORY.md in registration (default off)\n\n` +
          `**Inspect**\n` +
          `• \`/oversee settings\` — show all current config\n` +
          `• \`/oversee status\` — connection state + telemetry flowing or not\n\n` +
          `Setting commands update in-memory state immediately and ` +
          `best-effort-persist to \`openclaw.json\` via \`openclaw config ` +
          `set\`. \`connect\` and \`apikey\` need a gateway restart to ` +
          `re-init the OTLP exporter.`,
      )
    },
  }

  // OpenClaw's registerCommand contract may not match what we send (a
  // recent gateway error was "Command handler must be a function" — that
  // suggests it expects either a different property name or a different
  // call signature). Until we can verify against real docs, swallow the
  // error so command-registration failure doesn't take down the rest of
  // the plugin (telemetry hooks are the important part).
  try {
    api.registerCommand(command)
    console.log(`${LOG} /oversee command registered.`)
  } catch (e) {
    console.warn(
      `${LOG} Failed to register /oversee command: ${(e as Error).message}. ` +
        `Telemetry will continue to work; command-based setup is unavailable. ` +
        `Command shape sent: name="${command.name}", aliases=${JSON.stringify(command.aliases)}, ` +
        `execute=${typeof command.execute}.`,
    )
  }
}

// ---------------------------------------------------------------------------
// Plugin entry
// ---------------------------------------------------------------------------

export default definePluginEntry({
  id: "oversee",
  name: "Oversee Agent Management",
  description:
    "Automatic agent monitoring and management. Captures telemetry, reads agent identity, and sends everything to your Oversee dashboard.",
  register(api: OpenClawApi) {
    state.gatewayVersion =
      api?.version ?? api?.gateway?.version ?? "unknown"

    // Env-var disable is checked at register() so we never even wire
    // hooks when an operator wants the plugin totally inert. The
    // pluginConfig.enabled flag from openclaw.json is checked later
    // inside ensureInit, on the first hook that exposes pluginConfig.
    if (process.env.OVERSEE_ENABLED === "false") {
      console.log(`${LOG} Plugin disabled via OVERSEE_ENABLED=false.`)
      return
    }

    if (typeof api?.on !== "function") {
      console.warn(
        `${LOG} OpenClaw api.on() not available. Plugin cannot register handlers.`,
      )
      return
    }

    // Belt-and-braces: a bad command shape thrown synchronously from
    // wireCommands must never prevent wireEvents from running. Telemetry
    // is what people install this plugin for; the /oversee command is a
    // convenience.
    try {
      wireCommands(api)
    } catch (e) {
      console.warn(
        `${LOG} wireCommands threw: ${(e as Error).message}. Continuing with telemetry only.`,
      )
    }
    wireEvents(api)
    // OTEL is initialized lazily inside the first hook (typically
    // gateway_start) that exposes pluginConfig. No init log here yet.
  },
})
