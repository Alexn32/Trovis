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
import * as fs from "node:fs"
import * as path from "node:path"
import * as os from "node:os"

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const PLUGIN_VERSION = "0.2.8"
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

// `message_sending` is the pre-delivery decision hook. The gateway lets
// plugins rewrite or cancel the outbound text from here, but we never
// touch it — the handler returns undefined and the original event flows
// through. This is the only hook that exposes the agent's actual
// response content; `message_sent` carries delivery status only.
interface MessageSendingEvent extends BaseEvent {
  content?: string
  threadId?: string
}

interface MessageSentEvent extends BaseEvent {
  success?: boolean
  error?: unknown
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
  // Token usage may arrive under a number of shapes depending on the
  // provider adapter — see pickTokenUsage() for the probe order.
  usage?: unknown
  tokens?: unknown
  inputTokens?: unknown
  outputTokens?: unknown
  totalTokens?: unknown
}

// `llm_output` fires after the model returns a response, regardless of
// the delivery channel (web chat, Slack, etc.). This is the only hook
// that exposes the raw model output for web chat — `message_sending`
// only fires for external channel deliveries. The response text comes
// in as `assistantTexts`, a string[] containing one or more assistant
// turns (typically a single entry but the field is plural to support
// multi-turn outputs). Requires `hooks.allowConversationAccess: true`.
interface LlmOutputEvent extends BaseEvent {
  callId?: string
  runId?: string
  provider?: string
  model?: string
  assistantTexts?: string[]
}

interface AgentEndEvent extends BaseEvent {
  runId?: string
  success?: boolean
  error?: unknown
}

// Per docs.openclaw.ai: a slash-command handler receives a ctx object
// containing the raw argument string (everything typed after the
// command name) and returns `{ text }`. We don't get a pre-parsed
// array — splitting on whitespace is the handler's responsibility.
interface CommandContext {
  args?: string
}

interface CommandResult {
  text: string
}

interface PluginCommand {
  name: string
  description: string
  acceptsArgs?: boolean
  handler(ctx: CommandContext): Promise<CommandResult>
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

/**
 * Read `openclaw.json` directly from disk. Used as a fallback when the
 * `gateway_start` ctx doesn't carry a populated `config.agents.list`
 * (which is the case in non-bundled-plugin setups — every gateway we've
 * observed so far ships an empty config object to the plugin hook).
 *
 * Returns `null` if no readable config is found at any candidate path.
 * Tries an env override first, then the Docker volume location, then
 * the home-dir default.
 */
function loadConfigFromDisk(): unknown {
  const candidates: string[] = []
  const envPath = process.env.OPENCLAW_CONFIG_PATH
  if (envPath && envPath.length > 0) candidates.push(envPath)
  candidates.push("/data/.openclaw/openclaw.json")
  candidates.push(path.join(os.homedir(), ".openclaw", "openclaw.json"))

  for (const p of candidates) {
    try {
      const raw = fs.readFileSync(p, "utf-8")
      const parsed = JSON.parse(raw) as unknown
      return parsed
    } catch {
      // path missing or unreadable — try the next one.
    }
  }
  return null
}

function registerAgents(tracer: Tracer, ctx: OpenClawContext): void {
  // If the hook ctx didn't carry config (the common case in real
  // gateways), fall back to reading openclaw.json from disk. The
  // file-based shape mirrors what we'd expect on `ctx.config`.
  let cfg: unknown = ctx?.config
  if (!cfg) {
    cfg = loadConfigFromDisk()
  }
  const agentsCfg = (cfg as { agents?: unknown })?.agents as
    | { defaults?: { workspace?: string; model?: { primary?: string } }; list?: Array<{ id?: string; workspace?: string; model?: { primary?: string } }> }
    | undefined

  const defaults = agentsCfg?.defaults
  const list = agentsCfg?.list
  const defaultWorkspace =
    ctx?.workspaceDir ?? defaults?.workspace ?? resolveDefaultWorkspace()
  const defaultModel = defaults?.model?.primary ?? "unknown"

  if (Array.isArray(list) && list.length > 0) {
    for (const agent of list) {
      // Each agent in the config can override the default workspace.
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
  handler: (event: E, ctx?: OpenClawContext) => void,
): void {
  try {
    // OpenClaw passes (event, ctx) as two separate arguments to hook
    // handlers — the second is required for multi-agent context since
    // event.context is empty in practice. We capture both via rest args
    // and forward to the typed handler.
    api.on<E>(
      name,
      ((...args: unknown[]) => {
        try {
          handler(args[0] as E, args[1] as OpenClawContext | undefined)
        } catch (e) {
          console.warn(`${LOG} Handler for '${name}' threw:`, e)
        }
      }) as unknown as (event: E) => void,
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
/**
 * Resolve the agent ID for a hook event. OpenClaw doesn't expose
 * `agentId` directly on the event or context; instead it encodes the
 * agent in `sessionKey`, formatted as `agent:<agentId>:<sessionId>`.
 * Parse that first. Fall through to a couple of direct field reads in
 * case a future gateway version exposes it differently. Defaults to
 * `'main'` so single-agent gateways still get a stable group.
 */
function pickAgentId(event: unknown, ctx?: unknown): string {
  const ev = (event ?? {}) as {
    sessionKey?: unknown
    agentId?: unknown
    context?: { agentId?: unknown }
  }
  const c = (ctx ?? {}) as { sessionKey?: unknown; agentId?: unknown }

  const sessionKey =
    (typeof ev.sessionKey === "string" && ev.sessionKey) ||
    (typeof c.sessionKey === "string" && c.sessionKey) ||
    ""
  if (typeof sessionKey === "string" && sessionKey.startsWith("agent:")) {
    const parts = sessionKey.split(":")
    if (parts.length >= 2 && parts[1]) return parts[1]
  }

  const direct =
    (typeof ev.context?.agentId === "string" && ev.context.agentId) ||
    (typeof c.agentId === "string" && c.agentId) ||
    (typeof ev.agentId === "string" && ev.agentId) ||
    ""
  return direct || "main"
}

interface TokenUsage {
  input?: number
  output?: number
  total?: number
  cacheCreation?: number
  cacheRead?: number
}

/**
 * Best-effort token-usage extraction from a model_call_ended event.
 * Provider adapters disagree on the shape, so we probe the common
 * ones and normalize to {input, output, total}. Returns an empty
 * object when nothing usable is present.
 *
 * Shapes probed (in order):
 *   event.usage.{input_tokens, output_tokens, total_tokens}   (OTEL / Anthropic)
 *   event.usage.{prompt_tokens, completion_tokens, total_tokens}  (OpenAI)
 *   event.usage.{inputTokens, outputTokens, totalTokens}      (camelCase)
 *   event.tokens.{...}  (same keys, alternate container)
 *   event.{inputTokens, outputTokens, totalTokens}            (flat)
 */
function pickTokenUsage(event: unknown): TokenUsage {
  const ev = (event ?? {}) as Record<string, unknown>
  const num = (v: unknown): number | undefined => {
    const n = typeof v === "string" ? Number(v) : v
    return typeof n === "number" && Number.isFinite(n) ? n : undefined
  }

  // Candidate containers that might hold the usage object.
  const containers: Record<string, unknown>[] = []
  if (ev.usage && typeof ev.usage === "object")
    containers.push(ev.usage as Record<string, unknown>)
  if (ev.tokens && typeof ev.tokens === "object")
    containers.push(ev.tokens as Record<string, unknown>)
  containers.push(ev) // flat fields on the event itself

  for (const c of containers) {
    const input = num(
      c.input_tokens ?? c.prompt_tokens ?? c.inputTokens ?? c.promptTokens,
    )
    const output = num(
      c.output_tokens ??
        c.completion_tokens ??
        c.outputTokens ??
        c.completionTokens,
    )
    // Anthropic prompt-caching tokens — billed separately (creation 1.25x,
    // read 0.1x of base input) and NOT included in input_tokens.
    const cacheCreation = num(
      c.cache_creation_input_tokens ?? c.cacheCreationInputTokens,
    )
    const cacheRead = num(c.cache_read_input_tokens ?? c.cacheReadInputTokens)
    let total = num(c.total_tokens ?? c.totalTokens)
    if (
      total === undefined &&
      (input !== undefined ||
        output !== undefined ||
        cacheCreation !== undefined ||
        cacheRead !== undefined)
    ) {
      total = (input ?? 0) + (output ?? 0) + (cacheCreation ?? 0) + (cacheRead ?? 0)
    }
    if (
      input !== undefined ||
      output !== undefined ||
      total !== undefined ||
      cacheCreation !== undefined ||
      cacheRead !== undefined
    ) {
      return { input, output, total, cacheCreation, cacheRead }
    }
  }
  return {}
}

// ---------------------------------------------------------------------------
// Hook wiring
// ---------------------------------------------------------------------------

function wireEvents(api: OpenClawApi): void {
  // Correlation maps for start/end event pairs.
  const toolSpans = new Map<string, { span: Span; startedAt: number }>()
  const modelSpans = new Map<string, { span: Span; startedAt: number }>()

  // -- Gateway start: init OTEL + read agent identity files --
  safeOn<GatewayStartEvent>(api, "gateway_start", (event, hookCtx) => {
    const tracer = ensureInit(hookCtx ?? event?.context)
    if (!tracer) return
    registerAgents(tracer, ((hookCtx ?? event?.context) as OpenClawContext | undefined) ?? ({} as OpenClawContext))
  })

  // -- Inbound messages --
  safeOn<MessageReceivedEvent>(api, "message_received", (event, hookCtx) => {
    const tracer = ensureInit(hookCtx ?? event?.context)
    if (!tracer) return
    const ctx = ((hookCtx ?? event?.context) as OpenClawContext | undefined) ?? ({} as OpenClawContext)
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

  // -- Outbound response content (pre-delivery decision hook) --
  // `message_sending` is the only hook that carries the agent's actual
  // outbound text. We register it purely to observe — never rewrite or
  // cancel — and the handler returns undefined so the original event
  // flows through unchanged. Without this hook, we'd only see delivery
  // metadata (via message_sent) and never the response content itself.
  safeOn<MessageSendingEvent>(api, "message_sending", (event, hookCtx) => {
    const tracer = ensureInit(hookCtx ?? event?.context)
    if (!tracer) return
    const ctx = ((hookCtx ?? event?.context) as OpenClawContext | undefined) ?? ({} as OpenClawContext)
    const span = tracer.startSpan("message_sending", { kind: SpanKind.CLIENT })
    span.setAttribute("oversee.event.type", "message_sending")
    setIfPresent(span, "oversee.session.key", ctx.sessionKey)
    setIfPresent(span, "oversee.agent.id", pickAgentId(event, ctx))
    setIfPresent(span, "oversee.message.thread_id", event?.threadId)
    setIfPresent(
      span,
      "oversee.response.content_length",
      typeof event?.content === "string" ? event.content.length : undefined,
    )
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

  // -- Outbound delivery status (post-delivery) --
  // `message_sent` fires after the gateway has handed the message off.
  // It carries success/error metadata, NOT content (that lived on
  // `message_sending` above). Kept as a separate span so the dashboard
  // can show "tried to deliver, succeeded/failed" independently from
  // what the agent said.
  safeOn<MessageSentEvent>(api, "message_sent", (event, hookCtx) => {
    const tracer = ensureInit(hookCtx ?? event?.context)
    if (!tracer) return
    const ctx = ((hookCtx ?? event?.context) as OpenClawContext | undefined) ?? ({} as OpenClawContext)
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
    span.end()
  })

  // -- Tool calls (before / after pair) --
  safeOn<BeforeToolCallEvent>(api, "before_tool_call", (event, hookCtx) => {
    const tracer = ensureInit(hookCtx ?? event?.context)
    if (!tracer) return
    const ctx = ((hookCtx ?? event?.context) as OpenClawContext | undefined) ?? ({} as OpenClawContext)
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

  safeOn<AfterToolCallEvent>(api, "after_tool_call", (event, hookCtx) => {
    ensureInit(hookCtx ?? event?.context)
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
  safeOn<ModelCallStartedEvent>(api, "model_call_started", (event, hookCtx) => {
    const tracer = ensureInit(hookCtx ?? event?.context)
    if (!tracer) return
    const ctx = ((hookCtx ?? event?.context) as OpenClawContext | undefined) ?? ({} as OpenClawContext)
    const span = tracer.startSpan("model_call", { kind: SpanKind.CLIENT })
    span.setAttribute("oversee.event.type", "model_call")
    setIfPresent(span, "gen_ai.system", event?.provider)
    setIfPresent(span, "gen_ai.request.model", event?.model)
    span.setAttribute("oversee.model.call_id", event.callId)
    setIfPresent(span, "oversee.agent.id", pickAgentId(event, ctx))
    setIfPresent(span, "oversee.run.id", event?.runId ?? ctx.runId)

    modelSpans.set(event.callId, { span, startedAt: Date.now() })
  })

  safeOn<ModelCallEndedEvent>(api, "model_call_ended", (event, hookCtx) => {
    ensureInit(hookCtx ?? event?.context)
    const entry = modelSpans.get(event.callId)
    if (!entry) return
    modelSpans.delete(event.callId)

    entry.span.setAttribute("oversee.model.duration_ms", event.durationMs)
    setIfPresent(entry.span, "oversee.model.outcome", event.outcome)
    // Token usage → OTEL GenAI semantic conventions. The backend reads
    // these attribute names to compute cost. Only set what we actually
    // found; absent fields stay off the span so the backend records NULL.
    const usage = pickTokenUsage(event)
    setIfPresent(entry.span, "gen_ai.usage.input_tokens", usage.input)
    setIfPresent(entry.span, "gen_ai.usage.output_tokens", usage.output)
    setIfPresent(entry.span, "gen_ai.usage.total_tokens", usage.total)
    setIfPresent(
      entry.span,
      "gen_ai.usage.cache_creation_input_tokens",
      usage.cacheCreation,
    )
    setIfPresent(
      entry.span,
      "gen_ai.usage.cache_read_input_tokens",
      usage.cacheRead,
    )
    if (
      typeof event.outcome === "string" &&
      event.outcome !== "ok" &&
      event.outcome !== "success"
    ) {
      entry.span.setStatus({ code: SpanStatusCode.ERROR, message: event.outcome })
    }
    entry.span.end()
  })

  // -- Raw LLM output (channel-agnostic) --
  // Fires after the model produces a response on EVERY channel,
  // including web chat (where `message_sending` never fires — that one
  // only triggers for external-channel deliveries like Slack/Discord).
  // Observation-only: handler returns undefined so the gateway's normal
  // output path is undisturbed.
  safeOn<LlmOutputEvent>(api, "llm_output", (event, hookCtx) => {
    const tracer = ensureInit(hookCtx ?? event?.context)
    if (!tracer) return
    const ctx = ((hookCtx ?? event?.context) as OpenClawContext | undefined) ?? ({} as OpenClawContext)
    const span = tracer.startSpan("llm_output", { kind: SpanKind.INTERNAL })
    span.setAttribute("oversee.event.type", "llm_output")
    setIfPresent(span, "oversee.session.key", ctx.sessionKey)
    setIfPresent(span, "oversee.agent.id", pickAgentId(event, ctx))
    setIfPresent(span, "oversee.run.id", event?.runId ?? ctx.runId)
    setIfPresent(span, "oversee.model.call_id", event?.callId)
    setIfPresent(span, "gen_ai.system", event?.provider)
    setIfPresent(span, "gen_ai.request.model", event?.model)

    // The model's response text arrives as `assistantTexts`, a string[]
    // of one or more assistant turns. We join with newlines so a
    // multi-turn response collapses into a single readable blob.
    const responseText = event?.assistantTexts?.join("\n") ?? ""
    setIfPresent(
      span,
      "oversee.response.content_length",
      responseText.length > 0 ? responseText.length : undefined,
    )
    if (state.captureOutputs && responseText.length > 0) {
      span.setAttribute(
        "oversee.response.content",
        truncate(responseText, 10_000),
      )
    }
    span.end()
  })

  // -- Agent run completion --
  safeOn<AgentEndEvent>(api, "agent_end", (event, hookCtx) => {
    const tracer = ensureInit(hookCtx ?? event?.context)
    if (!tracer) return
    const ctx = ((hookCtx ?? event?.context) as OpenClawContext | undefined) ?? ({} as OpenClawContext)
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
 * Build the "to make this permanent, run …" hint shown after every
 * setting command. We never shell out to the gateway CLI ourselves —
 * the plugin is a telemetry tool, not a config writer. The user
 * (or their automation) is responsible for persisting the change.
 *
 * For booleans we render `on`/`off` to match the chat verbs the user
 * just typed, even though the underlying config field is a JSON bool —
 * the gateway's `config set` accepts both.
 */
function persistHint(key: string, value: string | boolean): string {
  const display =
    typeof value === "boolean" ? (value ? "true" : "false") : value
  return (
    `Setting applied for this session. To make permanent, run:\n` +
    `\`openclaw config set plugins.entries.oversee.config.${key} ${display}\``
  )
}

function wireCommands(api: OpenClawApi): void {
  if (typeof api?.registerCommand !== "function") {
    // Older gateways without command support — skip silently.
    return
  }

  const command: PluginCommand = {
    name: "oversee",
    description: "Connect to Oversee agent monitoring",
    acceptsArgs: true,
    async handler(ctx: CommandContext): Promise<CommandResult> {
      // ctx.args is the raw text after the command name — split it
      // ourselves. Multiple spaces collapse via /\s+/. Empty args means
      // the user typed bare `/oversee` (the help screen).
      const raw = (ctx?.args ?? "").trim()
      const parts = raw.length > 0 ? raw.split(/\s+/) : []
      const sub = parts[0]?.toLowerCase() ?? ""
      const arg1 = parts[1] ?? ""
      const reply = (text: string): CommandResult => ({ text })

      // --- connect <url> -------------------------------------------------
      if (sub === "connect" && arg1) {
        const endpoint = arg1
        state.endpoint = endpoint
        return reply(
          `✅ Oversee endpoint set: \`${endpoint}\`\n\n` +
            persistHint("endpoint", endpoint) +
            `\n\nThe OTLP exporter was constructed at gateway start, so ` +
            `**restart the gateway** after persisting for spans to ` +
            `actually go to this URL.`,
        )
      }

      // --- apikey <key> --------------------------------------------------
      if (sub === "apikey" && arg1) {
        const key = arg1
        state.apiKey = key
        return reply(
          `✅ Oversee API key set: \`${maskKey(key)}\`\n\n` +
            persistHint("apiKey", key) +
            `\n\nThe auth header is set on the exporter at gateway ` +
            `start, so **restart the gateway** after persisting for the ` +
            `new key to be sent.`,
        )
      }

      // --- capture on|off ------------------------------------------------
      if (sub === "capture" && (arg1 === "on" || arg1 === "off")) {
        const enable = arg1 === "on"
        state.captureOutputs = enable
        return reply(
          `✅ Output capture **${enable ? "enabled" : "disabled"}**.\n\n` +
            (enable
              ? `Message content and tool results will now appear on ` +
                `spans as \`oversee.message.content\`, ` +
                `\`oversee.response.content\`, and \`oversee.tool.result\` ` +
                `(each truncated to 10 000 chars).\n\n`
              : `Message content and tool results will no longer be ` +
                `captured. Existing spans aren't modified.\n\n`) +
            persistHint("captureOutputs", enable),
        )
      }

      // --- userdata on|off -----------------------------------------------
      if (sub === "userdata" && (arg1 === "on" || arg1 === "off")) {
        const enable = arg1 === "on"
        state.readUserData = enable
        return reply(
          `✅ User data ingestion **${enable ? "enabled" : "disabled"}**.\n\n` +
            persistHint("readUserData", enable) +
            `\n\nUSER.md and MEMORY.md are read at gateway start during ` +
            `agent registration, so **restart the gateway** after ` +
            `persisting for this to take effect on the registration spans.`,
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
          `Setting commands update in-memory state immediately. To make ` +
          `permanent, run \`openclaw config set plugins.entries.oversee.config.<key> <value>\`. ` +
          `\`connect\` and \`apikey\` need a gateway restart to re-init the OTLP exporter.`,
      )
    },
  }

  // Defensive try/catch — if the gateway's registerCommand contract
  // drifts again, command-registration failure shouldn't take down the
  // rest of the plugin (telemetry hooks are what matters).
  try {
    api.registerCommand(command)
    console.log(`${LOG} /oversee command registered.`)
  } catch (e) {
    console.warn(
      `${LOG} Failed to register /oversee command: ${(e as Error).message}. ` +
        `Telemetry will continue to work; command-based setup is unavailable.`,
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
