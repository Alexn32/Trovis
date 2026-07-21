/**
 * @trovis/openclaw-plugin
 * ============================================================================
 * Automatic agent telemetry for Trovis. Captures messages, tool calls, LLM
 * calls, and run completion, and forwards them to the configured Trovis
 * endpoint as OpenTelemetry traces. Also reads agent identity files
 * (SOUL.md, IDENTITY.md, AGENTS.md, USER.md, MEMORY.md) at startup and
 * sends them as an `agent_registration` span so the dashboard knows what
 * each agent is supposed to be doing.
 *
 * All event names, payload shapes, and context fields are from confirmed
 * OpenClaw plugin-hooks docs.
 *
 * Workloops (see "Workloop signals" section): every span carries OpenClaw's
 * runId verbatim as `trovis.run.id` so the backend groups one run into one
 * loop; agent_end auto-closes the loop as done; handoffs are declared via
 * the exported trovisHandoff() helper or the handoffTools config mapping.
 * Attribute-only — span structure and the export path are unchanged, and
 * older backends simply ignore the extra attributes.
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
import { randomUUID } from "node:crypto"

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const PLUGIN_VERSION = "0.5.1"
// No hardcoded default endpoint — the plugin is inert until the operator
// explicitly configures where telemetry should go.
const DEFAULT_AGENT_NAME = "openclaw-agent"
const LOG = "[Trovis]"
const OBSERVATION_PRIORITY = 0
// OTLP backends typically cap individual attribute values; 32 KB keeps us
// well inside Trovis's TEXT column and most other backends' limits.
const ATTR_BYTE_LIMIT = 32 * 1024

// ---------------------------------------------------------------------------
// Transcript usage sourcing
// ---------------------------------------------------------------------------
//
// OpenClaw's plugin hooks do NOT carry token usage — `model_call_ended`
// exposes model/provider/duration/outcome only (the request to add usage,
// openclaw#21184, was closed as not planned). The real per-response usage
// (input/output/total/cache tokens + OpenClaw's own cost) is persisted to
// per-session transcript JSONL files on disk. We read those and attach the
// token counts to the matching `model_call` span so the backend can price
// it. Privacy posture is unchanged: we read ONLY the usage object (token
// counts + cost), never prompt/response content from the transcript.
//
// The directory layout and per-entry schema are NOT published in the
// OpenClaw docs, so the two gateway-specific knobs below are isolated and
// overridable via env. Confirm them against a live gateway (see README →
// "Token usage"): `find ~/.openclaw -name '*.jsonl'` then inspect one entry.

// Roots to scan for session transcripts. `TROVIS_TRANSCRIPT_DIR` (a dir or a
// single file) overrides everything. We DON'T hardcode the sub-path because
// OpenClaw's layout isn't documented and varies — instead we walk these
// roots and content-sniff for transcript files (see findTranscriptFile).
function transcriptRoots(): string[] {
  const out: string[] = []
  const env =
    process.env.TROVIS_TRANSCRIPT_DIR ?? process.env.OVERSEE_TRANSCRIPT_DIR
  if (env && env.length > 0) out.push(env)
  out.push("/data/.openclaw")
  out.push(path.join(os.homedir(), ".openclaw"))
  return out
}

// Bounds for the recursive transcript scan — keeps startup cheap and avoids
// pathological directory trees. Dirs unlikely to hold transcripts are skipped.
const TRANSCRIPT_SCAN_MAX_DEPTH = 5
const TRANSCRIPT_SCAN_MAX_FILES = 2000
const TRANSCRIPT_SKIP_DIRS = new Set([
  "node_modules",
  ".git",
  "cache",
  ".cache",
  "tmp",
  "plugins",
])
// Substrings that mark a JSONL file as a usage-bearing transcript (used to
// tell real transcripts apart from config-audit / other logs).
const USAGE_MARKER = /"usage"|_tokens|inputTokens|outputTokens|totalTokens|responseUsage/

// How long (ms) a `model_call` span is held open waiting for its usage
// entry to land in the transcript before we give up and export it without
// tokens. A run normally completes (agent_end) well within this window.
const USAGE_DRAIN_TIMEOUT_MS = 10_000

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
  // Comma-separated `tool_name:direction` pairs (direction: to_human |
  // to_agent). Calling a listed tool marks that tool-call span as a
  // workloop handoff. Empty by default — the plugin never guesses which
  // tools hand work off. Same format as TROVIS_HANDOFF_TOOLS.
  handoffTools?: string
  // Conversation-adjacent hooks (model_call_*, llm_output, agent_end) only
  // fire when OpenClaw is told they're allowed. Tokens + per-call model
  // depend on these — we read the flag purely to give a precise warning in
  // `/trovis status` when it's off; OpenClaw itself enforces the gating.
  hooks?: { allowConversationAccess?: boolean }
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
  // True once any conversation-adjacent hook (model_call_*, llm_output,
  // agent_end) has fired. If telemetry is flowing but this stays false, the
  // operator almost certainly hasn't set hooks.allowConversationAccess and
  // is getting no tokens/cost/model — surfaced in `/trovis status`.
  sawConversationHook: boolean
  // Most recently resolved transcript file, for `/trovis settings` display.
  transcriptFileHint: string | null
  // Human-readable summary of the last usage entry we parsed, e.g.
  // "in:4 out:151 total:155 @claude-sonnet-4-6", for chat-based verification.
  lastUsageSeen: string | null
  // The flag as configured, for `/trovis status` reporting only.
  allowConversationAccess: boolean | undefined
  // tool name -> handoff direction, parsed from config.handoffTools /
  // TROVIS_HANDOFF_TOOLS. Calling a listed tool emits workloop handoff
  // attributes on its tool_call span. Empty by default.
  handoffTools: Map<string, "to_human" | "to_agent">
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
  sawConversationHook: false,
  transcriptFileHint: null,
  lastUsageSeen: null,
  allowConversationAccess: undefined,
  handoffTools: new Map(),
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
    "trovis.plugin.version": PLUGIN_VERSION,
    "openclaw.gateway.version": gatewayVersion,
  })

  const exporter = new OTLPTraceExporter({
    url: endpoint,
    headers: apiKey ? { "X-Trovis-Api-Key": apiKey } : undefined,
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

  return trace.getTracer("@trovis/openclaw-plugin", PLUGIN_VERSION)
}

function ensureInit(ctx: OpenClawContext | undefined): Tracer | null {
  if (state.disabled) return null
  if (state.initialized) return state.tracer

  // Defense-in-depth: register() also checks TROVIS_ENABLED=false and
  // bails before wiring hooks, so this path is rarely reached. But if
  // for some reason hooks WERE wired and the operator set the env var
  // late, this stops any telemetry from going out.
  if ((process.env.TROVIS_ENABLED ?? process.env.OVERSEE_ENABLED) === "false") {
    state.disabled = true
    console.log(`${LOG} Plugin disabled via TROVIS_ENABLED=false`)
    return null
  }

  const pluginConfig = ctx?.pluginConfig ?? readPluginConfigFromDisk()

  // Disabled via openclaw.json takes precedence over any other config.
  if (pluginConfig?.enabled === false) {
    state.disabled = true
    console.log(`${LOG} Plugin disabled via config.`)
    return null
  }

  // No hardcoded default — operator must opt in by configuring an endpoint.
  // If we get this far without one, mark disabled so we don't log on every
  // subsequent hook firing.
  const endpoint = pluginConfig?.endpoint ?? (process.env.TROVIS_ENDPOINT ?? process.env.OVERSEE_ENDPOINT)
  if (!endpoint) {
    state.disabled = true
    console.log(
      `${LOG} No endpoint configured. Set plugins.entries.trovis.config.endpoint to enable telemetry.`,
    )
    return null
  }

  state.endpoint = endpoint
  state.agentName =
    pluginConfig?.agentName ??
    (process.env.TROVIS_AGENT_NAME ?? process.env.OVERSEE_AGENT_NAME) ??
    DEFAULT_AGENT_NAME
  state.apiKey = pluginConfig?.apiKey ?? (process.env.TROVIS_API_KEY ?? process.env.OVERSEE_API_KEY)
  // USER.md and MEMORY.md may carry personal data; the operator has to
  // explicitly opt in to having those files shipped to Trovis.
  state.readUserData = Boolean(pluginConfig?.readUserData)
  // Message content / tool outputs are opt-in for the same reason. Use
  // ?? (not ||) so an explicit `false` in pluginConfig overrides a
  // `true` env var.
  state.captureOutputs = Boolean(
    pluginConfig?.captureOutputs ??
      ((process.env.TROVIS_CAPTURE_OUTPUTS ?? process.env.OVERSEE_CAPTURE_OUTPUTS) === "true"),
  )
  // Recorded for `/trovis status` diagnostics only — OpenClaw enforces the
  // actual gating. undefined = not present in config (treated as off).
  state.allowConversationAccess = pluginConfig?.hooks?.allowConversationAccess
  // Workloop handoff-tool mapping. Ships empty — the operator opts in per
  // tool; the plugin never guesses which tools constitute a handoff.
  state.handoffTools = parseHandoffTools(
    pluginConfig?.handoffTools ??
      (process.env.TROVIS_HANDOFF_TOOLS ?? process.env.OVERSEE_HANDOFF_TOOLS),
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

  span.setAttribute("trovis.event.type", "agent_registration")
  span.setAttribute("trovis.agent.id", agentId)
  span.setAttribute("trovis.agent.workspace_path", workspacePath)
  span.setAttribute("trovis.agent.model", model || "unknown")
  if (soul) span.setAttribute("trovis.agent.soul", truncate(soul))
  if (identity) span.setAttribute("trovis.agent.identity", truncate(identity))
  if (operatingManual)
    span.setAttribute("trovis.agent.operating_manual", truncate(operatingManual))
  if (userContext)
    span.setAttribute("trovis.agent.user_context", truncate(userContext))
  if (memory) span.setAttribute("trovis.agent.memory", truncate(memory))

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

// The OpenClaw gateway doesn't populate ctx.pluginConfig in hook contexts
// for community / non-bundled plugins — it's always undefined there. So we
// fall back to reading openclaw.json from disk and pulling out our own
// config slice, mirroring how registerAgents() falls back to
// loadConfigFromDisk() for ctx.config. Without this, endpoint/apiKey/etc.
// stored in openclaw.json are never read and telemetry never fires.
function readPluginConfigFromDisk(): PluginConfig | null {
  const parsed = loadConfigFromDisk()
  return (
    (parsed as { plugins?: { entries?: { trovis?: { config?: PluginConfig } } } })
      ?.plugins?.entries?.trovis?.config ?? null
  )
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

// ---------------------------------------------------------------------------
// Workloop signals
// ---------------------------------------------------------------------------
//
// The Trovis backend groups spans into "workloops" (units of work with
// derived state: working / awaiting_human / done / …) using span attributes,
// read dual-prefix (trovis.* preferred, legacy oversee.* accepted):
//
//   trovis.run.id             groups all spans of one run into one loop
//   trovis.loop.title         plain-English loop title (creation only)
//   trovis.handoff.*          declares a handoff to a human/agent
//   trovis.loop.close         closes the loop as done
//
// Run identity: OpenClaw already has a natural unit of execution — the RUN
// (one message-handling cycle, `runId` on hook events/context, terminated by
// agent_end). We forward it VERBATIM as trovis.run.id on every span that has
// it. When a hook fires without a runId (e.g. message_received on some
// gateways), the attribute is omitted entirely and the backend's 30-min
// gap rule groups the span instead — we never invent a competing id that
// could split one run across two loops. A session (`sessionKey`) was
// deliberately NOT chosen as the unit: sessions live for days, so a
// session-scoped loop would never reach a terminal state.

// One-shot signals set by the public helpers (trovisHandoff /
// trovisCloseLoop, exported below) and consumed by the next span the plugin
// emits. Module-scoped single-flight: agent code runs inside the gateway
// process, so "the next span" is the helper's own run in practice.
let pendingHandoff: {
  direction: "to_human" | "to_agent"
  target?: string
  reason?: string
  id: string
} | null = null
let pendingClose: string | null = null

// Run-keys (runId, else sessionKey) that emitted a handoff / an explicit
// close during the current run. agent_end consults these so it (a) never
// auto-closes a loop that's awaiting a human/agent — the close would
// overwrite awaiting_* with done — and (b) never double-closes after
// trovisCloseLoop(). Entries are removed on agent_end; the size guard
// below covers runs that never reach agent_end (gateway crash mid-run).
const handoffActiveRuns = new Set<string>()
const closedRuns = new Set<string>()
const RUN_TRACKING_MAX = 5000

function trackRun(set: Set<string>, key: string): void {
  if (set.size >= RUN_TRACKING_MAX) set.clear() // bound memory on long-lived gateways
  set.add(key)
}

/** The run id OpenClaw assigned to this hook's execution unit, verbatim.
 * undefined (attribute omitted) when the gateway didn't provide one. */
function pickRunId(event: unknown, ctx?: OpenClawContext): string | undefined {
  const ev = (event ?? {}) as { runId?: unknown }
  return pickStr(ev.runId) ?? pickStr(ctx?.runId)
}

/** Stable key for per-run bookkeeping (handoff/close suppression). Falls
 * back to the session when the gateway gave no runId. */
function runKey(event: unknown, ctx?: OpenClawContext): string {
  return pickRunId(event, ctx) ?? pickStr(ctx?.sessionKey) ?? "(no-run)"
}

/** Parse `tool:direction,tool2:direction` (config.handoffTools /
 * TROVIS_HANDOFF_TOOLS). Unknown directions are skipped with a warning —
 * never guessed. */
function parseHandoffTools(
  raw: string | undefined | null,
): Map<string, "to_human" | "to_agent"> {
  const map = new Map<string, "to_human" | "to_agent">()
  if (!raw) return map
  for (const part of raw.split(",")) {
    const idx = part.indexOf(":")
    const name = (idx >= 0 ? part.slice(0, idx) : part).trim()
    const dir = idx >= 0 ? part.slice(idx + 1).trim() : ""
    if (!name) continue
    if (dir === "to_human" || dir === "to_agent") {
      map.set(name, dir)
    } else {
      console.warn(
        `${LOG} Ignoring handoff tool '${name}': direction must be ` +
          `'to_human' or 'to_agent' (got '${dir || "(none)"}')`,
      )
    }
  }
  return map
}

/**
 * Stamp the workloop attributes every span carries: the run id, plus any
 * one-shot handoff/close signal queued by the public helpers. Called from
 * every span-emitting hook handler. Attribute-only — never changes span
 * structure or the export path.
 */
function applyLoopSignals(span: Span, event: unknown, ctx?: OpenClawContext): void {
  setIfPresent(span, "trovis.run.id", pickRunId(event, ctx))
  if (pendingHandoff) {
    const h = pendingHandoff
    pendingHandoff = null
    span.setAttribute("trovis.handoff.direction", h.direction)
    setIfPresent(span, "trovis.handoff.target_id", h.target)
    setIfPresent(span, "trovis.handoff.reason", h.reason)
    span.setAttribute("trovis.handoff.id", h.id)
    trackRun(handoffActiveRuns, runKey(event, ctx))
  }
  if (pendingClose) {
    const reason = pendingClose
    pendingClose = null
    span.setAttribute("trovis.loop.close", reason)
    trackRun(closedRuns, runKey(event, ctx))
  }
}

/**
 * Declare a handoff from agent code: the current unit of work is now
 * waiting on a human (`to_human`) or another agent (`to_agent`). Sets the
 * trovis.handoff.* attributes on the next span the plugin emits and
 * suppresses the run's automatic `done` close, so the loop stays in
 * awaiting_human / awaiting_agent on the dashboard until it's resolved.
 * Returns the generated handoff id (uuid) for later correlation, or null
 * when the direction is invalid (warned, no-op — agent code never throws).
 */
export function trovisHandoff(
  direction: "to_human" | "to_agent" = "to_human",
  target?: string,
  reason?: string,
): string | null {
  if (direction !== "to_human" && direction !== "to_agent") {
    console.warn(
      `${LOG} trovisHandoff: direction must be 'to_human' or 'to_agent' ` +
        `(got '${String(direction)}') — ignored.`,
    )
    return null
  }
  const id = randomUUID()
  pendingHandoff = { direction, target, reason, id }
  return id
}

/**
 * Close the current unit of work from agent code. Sets trovis.loop.close
 * on the next span the plugin emits; the backend closes the loop as done,
 * agent-attributed (a non-"done" reason string is preserved as detail).
 * Usually unnecessary — the plugin auto-closes on agent_end — but useful
 * when work completes mid-run or a handoff was resolved by the agent
 * itself. Empty reasons are coerced to "done" (the backend treats empty
 * strings as absent).
 */
export function trovisCloseLoop(reason: string = "done"): void {
  const r = typeof reason === "string" && reason.trim().length > 0 ? reason : "done"
  pendingClose = r
}

interface TokenUsage {
  input?: number
  output?: number
  total?: number
  cacheCreation?: number
  cacheRead?: number
  // OpenClaw's own computed cost when present. We do NOT forward it to the
  // backend (Trovis recomputes cost from tokens+model for a single
  // cost basis across all agents) — kept only for logging/diagnostics.
  cost?: number
}

/**
 * Normalize a single usage-bearing object into {input, output, total,
 * cache*, cost}. Provider adapters and OpenClaw transcripts disagree on
 * field names, so every common alias is probed. Returns an empty object
 * when nothing usable is present.
 *
 * Aliases probed:
 *   input  : input_tokens | prompt_tokens | inputTokens | promptTokens | input | inputTokenCount | promptTokenCount | tokensIn
 *   output : output_tokens | completion_tokens | outputTokens | completionTokens | output | outputTokenCount | completionTokenCount | candidatesTokenCount | tokensOut
 *   total  : total_tokens | totalTokens  (derived from the parts if absent)
 *   cacheCreation : cache_creation_input_tokens | cacheCreationInputTokens | cacheWrite
 *   cacheRead     : cache_read_input_tokens | cacheReadInputTokens | cacheRead
 *   cost   : cost | cost_usd | costUsd | usd
 */
function normalizeUsageObject(container: unknown): TokenUsage {
  const c = (container ?? {}) as Record<string, unknown>
  const num = (v: unknown): number | undefined => {
    const n = typeof v === "string" ? Number(v) : v
    return typeof n === "number" && Number.isFinite(n) ? n : undefined
  }

  const input = num(
    c.input_tokens ??
      c.prompt_tokens ??
      c.inputTokens ??
      c.promptTokens ??
      // OpenClaw trajectory usage uses bare `input`/`output` alongside
      // `totalTokens`/`cacheRead`; cover those plus common count variants.
      c.input ??
      c.inputTokenCount ??
      c.promptTokenCount ??
      c.tokensIn,
  )
  const output = num(
    c.output_tokens ??
      c.completion_tokens ??
      c.outputTokens ??
      c.completionTokens ??
      c.output ??
      c.outputTokenCount ??
      c.completionTokenCount ??
      c.candidatesTokenCount ??
      c.tokensOut,
  )
  // Anthropic prompt-caching tokens — billed separately (creation 1.25x,
  // read 0.1x of base input) and NOT included in input_tokens.
  const cacheCreation = num(
    c.cache_creation_input_tokens ?? c.cacheCreationInputTokens ?? c.cacheWrite,
  )
  const cacheRead = num(
    c.cache_read_input_tokens ?? c.cacheReadInputTokens ?? c.cacheRead,
  )
  const cost = num(c.cost ?? c.cost_usd ?? c.costUsd ?? c.usd)
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
    return { input, output, total, cacheCreation, cacheRead, cost }
  }
  return {}
}

/**
 * Best-effort token-usage extraction from an event payload. Kept for the
 * (unlikely) case a future gateway DOES put usage on model_call_ended; in
 * practice OpenClaw hooks carry no usage, which is why we also read
 * transcripts. Probes event.usage, then event.tokens, then flat fields.
 */
function pickTokenUsage(event: unknown): TokenUsage {
  const ev = (event ?? {}) as Record<string, unknown>
  const containers: unknown[] = []
  if (ev.usage && typeof ev.usage === "object") containers.push(ev.usage)
  if (ev.tokens && typeof ev.tokens === "object") containers.push(ev.tokens)
  containers.push(ev) // flat fields on the event itself
  for (const c of containers) {
    const u = normalizeUsageObject(c)
    if (Object.keys(u).some((k) => u[k as keyof TokenUsage] !== undefined)) {
      return u
    }
  }
  return {}
}

// ---------------------------------------------------------------------------
// Transcript usage reader
// ---------------------------------------------------------------------------
//
// OpenClaw hooks carry no tokens, but the per-session transcript does. We
// tail each session's transcript, parse the per-response usage object, and
// attach the token counts to the matching (still-open) `model_call` span —
// see the design note at the top of this file.

interface OpenModelSpan {
  span: Span
  startedAtMs: number
  endedAtMs?: number // wall-clock when model_call_ended fired; used as the
  // explicit span end time so deferring the end() doesn't inflate duration
  sessionKey?: string
  done: boolean
}

interface TranscriptUsageEntry {
  usage: TokenUsage
  model?: string
  provider?: string
  callId?: string
}

// callId -> open model_call span awaiting its usage entry.
const openModelSpans = new Map<string, OpenModelSpan>()
// sessionKey -> callIds in start order, for FIFO matching when the
// transcript entry doesn't carry a callId.
const sessionOpenOrder = new Map<string, string[]>()
// transcript file path -> bytes already consumed. First sight seeks to EOF
// so we never replay history; only appended bytes are processed thereafter.
const transcriptOffsets = new Map<string, number>()

function pickStr(v: unknown): string | undefined {
  return typeof v === "string" && v.length > 0 ? v : undefined
}

/** Session id, preferred from ctx, else parsed from `agent:<id>:<session>`. */
function pickSessionId(ctx: OpenClawContext | undefined): string | undefined {
  if (ctx?.sessionId) return ctx.sessionId
  const sk = ctx?.sessionKey
  if (typeof sk === "string" && sk.startsWith("agent:")) {
    const parts = sk.split(":")
    if (parts.length >= 3 && parts[2]) return parts[2]
  }
  return undefined
}

/** Recursively collect `*.jsonl` paths under `root`, bounded by depth and
 * count, skipping noise directories. Iterative to avoid deep recursion. */
function collectJsonl(root: string): string[] {
  const found: string[] = []
  const stack: Array<{ dir: string; depth: number }> = [{ dir: root, depth: 0 }]
  while (stack.length > 0 && found.length < TRANSCRIPT_SCAN_MAX_FILES) {
    const { dir, depth } = stack.pop() as { dir: string; depth: number }
    let entries: fs.Dirent[]
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true })
    } catch {
      continue
    }
    for (const e of entries) {
      const full = path.join(dir, e.name)
      if (e.isDirectory()) {
        if (depth < TRANSCRIPT_SCAN_MAX_DEPTH && !TRANSCRIPT_SKIP_DIRS.has(e.name)) {
          stack.push({ dir: full, depth: depth + 1 })
        }
      } else if (e.isFile() && e.name.endsWith(".jsonl")) {
        found.push(full)
      }
    }
  }
  return found
}

/** True if the file's tail contains a usage marker — i.e. it's a real
 * transcript, not a config-audit or other log. Reads only the last 16 KB. */
function looksLikeTranscript(filePath: string): boolean {
  try {
    const size = fs.statSync(filePath).size
    const start = Math.max(0, size - 16_384)
    const fd = fs.openSync(filePath, "r")
    try {
      const len = size - start
      const buf = Buffer.alloc(len)
      fs.readSync(fd, buf, 0, len, start)
      return USAGE_MARKER.test(buf.toString("utf-8"))
    } finally {
      fs.closeSync(fd)
    }
  } catch {
    return false
  }
}

// Per-session resolved transcript path — found once, then reused.
const sessionFileCache = new Map<string, string>()

/**
 * Auto-locate the transcript file for a session. We do NOT assume a fixed
 * directory (OpenClaw's layout is undocumented and varies): scan the roots
 * for every `*.jsonl`, then choose:
 *  1. a file whose name contains the sessionId (the normal per-session case);
 *  2. otherwise, among files that actually contain usage data, the most
 *     recently modified one (single-session gateways, opaque filenames).
 * Returns null when the sessionId is known but several usage-bearing files
 * exist and none matches — we never guess and risk cross-session attribution.
 */
function findTranscriptFile(sessionId: string | undefined): string | null {
  if (sessionId) {
    const cached = sessionFileCache.get(sessionId)
    if (cached) return cached
  }

  const all: string[] = []
  for (const root of transcriptRoots()) {
    // A root may itself be a single file (TROVIS_TRANSCRIPT_DIR=/path/x.jsonl).
    try {
      const st = fs.statSync(root)
      if (st.isFile() && root.endsWith(".jsonl")) {
        all.push(root)
        continue
      }
    } catch {
      continue
    }
    for (const f of collectJsonl(root)) all.push(f)
  }
  if (all.length === 0) return null

  if (sessionId) {
    const named = all.find((f) => path.basename(f).includes(sessionId))
    if (named) {
      sessionFileCache.set(sessionId, named)
      state.transcriptFileHint = named
      return named
    }
  }

  // Fall back to usage-bearing files, newest first.
  const transcripts = all.filter(looksLikeTranscript)
  if (transcripts.length === 0) return null
  if (sessionId && transcripts.length > 1) return null // ambiguous — don't guess

  let newest: { f: string; mtime: number } | null = null
  for (const f of transcripts) {
    try {
      const m = fs.statSync(f).mtimeMs
      if (!newest || m > newest.mtime) newest = { f, mtime: m }
    } catch {
      // skip
    }
  }
  if (!newest) return null
  if (sessionId) sessionFileCache.set(sessionId, newest.f)
  state.transcriptFileHint = newest.f
  return newest.f
}

/**
 * Anchor the read offset for a session's transcript at the file's current
 * size, called when a tracked model call STARTS — i.e. before that call's
 * usage line is appended. This is what lets the first call of a session get
 * its tokens: without it, the first drain would "seek to EOF" and skip the
 * line that was just written. No-op if we've already anchored this file or
 * the file doesn't exist yet (a brand-new session's first call falls back to
 * the EOF-seek in readNewUsageEntries — one call's tokens may be missed, but
 * history is never replayed).
 */
function noteSessionBaseline(ctx: OpenClawContext | undefined): void {
  const file = findTranscriptFile(pickSessionId(ctx))
  if (!file || transcriptOffsets.has(file)) return
  try {
    transcriptOffsets.set(file, fs.statSync(file).size)
  } catch {
    // unreadable — let readNewUsageEntries handle first-sight later
  }
}


/** Richness score — prefer usage with an input/output split (needed for
 * cost) over a total-only object. input+output > either alone > total-only. */
function usageScore(u: TokenUsage): number {
  return (
    (u.input !== undefined ? 2 : 0) +
    (u.output !== undefined ? 2 : 0) +
    (u.total !== undefined ? 1 : 0) +
    (u.cacheRead !== undefined || u.cacheCreation !== undefined ? 1 : 0)
  )
}

/**
 * Schema-agnostic: walk a parsed entry (bounded breadth/depth) and return
 * the RICHEST nested object that normalizes to token usage. "Richest" so we
 * don't stop at a total-only summary object when a sibling has the
 * input/output split we need to price the call. Lets us capture usage even
 * when OpenClaw nests it under an unexpected key.
 */
function deepFindUsage(root: unknown): TokenUsage {
  const queue: Array<{ v: unknown; depth: number }> = [{ v: root, depth: 0 }]
  let visited = 0
  let best: TokenUsage = {}
  let bestScore = 0
  while (queue.length > 0 && visited < 400) {
    const { v, depth } = queue.shift() as { v: unknown; depth: number }
    visited++
    if (!v || typeof v !== "object") continue
    const u = normalizeUsageObject(v)
    const s = usageScore(u)
    if (s > bestScore) {
      best = u
      bestScore = s
    }
    // input+output is as rich as it gets — stop early.
    if (u.input !== undefined && u.output !== undefined) break
    if (depth < 5) {
      for (const val of Object.values(v as Record<string, unknown>)) {
        if (val && typeof val === "object") queue.push({ v: val, depth: depth + 1 })
      }
    }
  }
  return best
}

/** Schema-agnostic string lookup: first string value found under any of the
 * given keys, searching nested objects (bounded). */
function deepFindStr(root: unknown, keys: string[]): string | undefined {
  const queue: Array<{ v: unknown; depth: number }> = [{ v: root, depth: 0 }]
  let visited = 0
  while (queue.length > 0 && visited < 200) {
    const { v, depth } = queue.shift() as { v: unknown; depth: number }
    visited++
    if (!v || typeof v !== "object") continue
    const obj = v as Record<string, unknown>
    for (const k of keys) {
      const s = pickStr(obj[k])
      if (s) return s
    }
    if (depth < 5) {
      for (const val of Object.values(obj)) {
        if (val && typeof val === "object") queue.push({ v: val, depth: depth + 1 })
      }
    }
  }
  return undefined
}

// Token-ish field-name matcher for the debug dumper below.
const TOKEN_FIELD_RE = /token|cache|input|output|prompt|completion|usage|cost/i

/**
 * Diagnostic: walk a parsed entry and collect NUMERIC leaf fields whose key
 * looks token-related, as `path=value` strings. Numbers only — never strings
 * — so this can't leak prompt/response content. Powers `/trovis debug` when
 * the input/output split can't be found, revealing OpenClaw's real field
 * names so we can map them without another guess.
 */
function dumpNumericTokenFields(root: unknown, limit = 24): string[] {
  const out: string[] = []
  const queue: Array<{ v: unknown; path: string; depth: number }> = [
    { v: root, path: "", depth: 0 },
  ]
  let visited = 0
  while (queue.length > 0 && visited < 400 && out.length < limit) {
    const { v, path: p, depth } = queue.shift() as {
      v: unknown
      path: string
      depth: number
    }
    visited++
    if (!v || typeof v !== "object") continue
    for (const [k, val] of Object.entries(v as Record<string, unknown>)) {
      const childPath = p ? `${p}.${k}` : k
      const n = typeof val === "string" ? Number(val) : val
      if (typeof n === "number" && Number.isFinite(n)) {
        if (TOKEN_FIELD_RE.test(k)) out.push(`${childPath}=${n}`)
      } else if (val && typeof val === "object" && depth < 5) {
        queue.push({ v: val, path: childPath, depth: depth + 1 })
      }
    }
  }
  return out
}

/**
 * Read newly-appended JSONL lines and return parsed usage entries. Advances
 * the per-file offset, keeping any trailing partial line for the next read.
 * Returns [] on first sight of a file (seeks to EOF to avoid replaying
 * history) and on any read/parse trouble.
 */
function readNewUsageEntries(filePath: string): TranscriptUsageEntry[] {
  let size: number
  try {
    size = fs.statSync(filePath).size
  } catch {
    return []
  }
  const prev = transcriptOffsets.get(filePath)
  if (prev === undefined) {
    transcriptOffsets.set(filePath, size) // first sight — tail from EOF
    return []
  }
  if (size <= prev) {
    if (size < prev) transcriptOffsets.set(filePath, size) // truncated/rotated
    return []
  }

  let chunk = ""
  try {
    const fd = fs.openSync(filePath, "r")
    try {
      const buf = Buffer.alloc(size - prev)
      fs.readSync(fd, buf, 0, buf.length, prev)
      chunk = buf.toString("utf-8")
    } finally {
      fs.closeSync(fd)
    }
  } catch {
    return []
  }

  const lastNl = chunk.lastIndexOf("\n")
  if (lastNl === -1) return [] // no complete line yet — wait for more
  // Advance only past whole lines (byte-accurate for multi-byte UTF-8).
  transcriptOffsets.set(
    filePath,
    prev + Buffer.byteLength(chunk.slice(0, lastNl + 1), "utf-8"),
  )

  const out: TranscriptUsageEntry[] = []
  for (const line of chunk.slice(0, lastNl).split("\n")) {
    const t = line.trim()
    if (!t) continue
    let entry: Record<string, unknown>
    try {
      entry = JSON.parse(t) as Record<string, unknown>
    } catch {
      continue
    }
    const msg = entry.message as Record<string, unknown> | undefined
    // Usage may live at entry.usage, entry.responseUsage, or entry.message.usage.
    // A response often carries BOTH a per-call usage (with input/output) and a
    // running session total (total-only) — pick the richest so we keep the
    // input/output split needed to price the call, not just a bare total.
    const candidates = [
      normalizeUsageObject(entry.usage),
      normalizeUsageObject(entry.responseUsage),
      normalizeUsageObject(msg?.usage),
    ]
    let usage: TokenUsage = {}
    for (const c of candidates) if (usageScore(c) > usageScore(usage)) usage = c
    // If no known path had an input/output split, deep-search for a richer one.
    if (usage.input === undefined || usage.output === undefined) {
      const deep = deepFindUsage(entry)
      if (usageScore(deep) > usageScore(usage)) usage = deep
    }
    if (
      usage.input === undefined &&
      usage.output === undefined &&
      usage.total === undefined
    ) {
      continue // not a usage-bearing entry
    }
    out.push({
      usage,
      model:
        pickStr(entry.model) ??
        pickStr(msg?.model) ??
        deepFindStr(entry, ["model", "modelId", "model_id"]),
      provider:
        pickStr(entry.provider) ??
        pickStr(msg?.provider) ??
        deepFindStr(entry, ["provider", "system"]),
      callId: pickStr(entry.callId ?? entry.call_id),
    })
  }
  return out
}

/** Remember the latest usage we parsed, as a one-line summary surfaced by
 * `/trovis status` so an operator can confirm capture from chat alone. */
function recordUsageSeen(usage: TokenUsage, model?: string): void {
  const parts: string[] = []
  if (usage.input !== undefined) parts.push(`in:${usage.input}`)
  if (usage.output !== undefined) parts.push(`out:${usage.output}`)
  if (usage.total !== undefined) parts.push(`total:${usage.total}`)
  if (model) parts.push(`@${model}`)
  if (parts.length > 0) state.lastUsageSeen = parts.join(" ")
}

/** Attach token counts to an open model_call span and end it (preserving
 * the original call duration via the recorded end time). */
function applyUsageAndEnd(
  e: OpenModelSpan,
  usage: TokenUsage,
  model?: string,
  provider?: string,
): void {
  if (e.done) return
  setIfPresent(e.span, "gen_ai.usage.input_tokens", usage.input)
  setIfPresent(e.span, "gen_ai.usage.output_tokens", usage.output)
  setIfPresent(e.span, "gen_ai.usage.total_tokens", usage.total)
  setIfPresent(
    e.span,
    "gen_ai.usage.cache_creation_input_tokens",
    usage.cacheCreation,
  )
  setIfPresent(e.span, "gen_ai.usage.cache_read_input_tokens", usage.cacheRead)
  // Transcript may carry a more precise model id / provider than the start
  // hook; setIfPresent leaves the start value in place when absent.
  setIfPresent(e.span, "gen_ai.request.model", model)
  setIfPresent(e.span, "gen_ai.system", provider)
  e.span.setAttribute("trovis.model.usage_source", "transcript")
  recordUsageSeen(usage, model)
  e.done = true
  e.span.end(e.endedAtMs)
}

/** Forget an open span (already ended elsewhere, or timed out). */
function forgetOpenSpan(callId: string, sessionKey?: string): void {
  openModelSpans.delete(callId)
  if (sessionKey) {
    const order = sessionOpenOrder.get(sessionKey)
    if (order) {
      const i = order.indexOf(callId)
      if (i >= 0) order.splice(i, 1)
    }
  }
}

/** Standalone usage span for a transcript entry with no open span to
 * attach to (e.g. conversation hooks disabled, or correlation drift). */
function emitStandaloneUsageSpan(
  tracer: Tracer,
  entry: TranscriptUsageEntry,
  sessionKey: string | undefined,
): void {
  const span = tracer.startSpan("model_call", { kind: SpanKind.CLIENT })
  span.setAttribute("trovis.event.type", "model_call")
  span.setAttribute("trovis.model.usage_source", "transcript")
  setIfPresent(span, "gen_ai.request.model", entry.model)
  setIfPresent(span, "gen_ai.system", entry.provider)
  setIfPresent(span, "gen_ai.usage.input_tokens", entry.usage.input)
  setIfPresent(span, "gen_ai.usage.output_tokens", entry.usage.output)
  setIfPresent(span, "gen_ai.usage.total_tokens", entry.usage.total)
  setIfPresent(
    span,
    "gen_ai.usage.cache_creation_input_tokens",
    entry.usage.cacheCreation,
  )
  setIfPresent(
    span,
    "gen_ai.usage.cache_read_input_tokens",
    entry.usage.cacheRead,
  )
  if (sessionKey) span.setAttribute("trovis.agent.id", pickAgentId({ sessionKey }))
  recordUsageSeen(entry.usage, entry.model)
  span.end()
}

/**
 * Read any newly-written usage entries for a session and reconcile them
 * against that session's open model_call spans. Matches by callId when the
 * transcript provides one, else FIFO (calls complete in order within a
 * session). Idempotent: offset tracking means repeat calls are cheap and
 * never double-count.
 */
function drainSessionUsage(
  tracer: Tracer | null,
  ctx: OpenClawContext | undefined,
): void {
  const file = findTranscriptFile(pickSessionId(ctx))
  if (!file) return
  const entries = readNewUsageEntries(file)
  if (entries.length === 0) return

  const sessionKey = ctx?.sessionKey
  const order = sessionKey ? sessionOpenOrder.get(sessionKey) ?? [] : []

  for (const entry of entries) {
    let callId: string | undefined
    if (entry.callId && openModelSpans.has(entry.callId)) {
      callId = entry.callId
    } else {
      while (order.length > 0 && !openModelSpans.has(order[0])) order.shift()
      callId = order[0]
    }
    if (callId && openModelSpans.has(callId)) {
      const e = openModelSpans.get(callId)!
      applyUsageAndEnd(e, entry.usage, entry.model, entry.provider)
      forgetOpenSpan(callId, e.sessionKey)
    } else if (tracer) {
      emitStandaloneUsageSpan(tracer, entry, sessionKey)
    }
  }
}

// ---------------------------------------------------------------------------
// Hook wiring
// ---------------------------------------------------------------------------

function wireEvents(api: OpenClawApi): void {
  // Tool start/end correlation. Model-call spans live in module-scope
  // `openModelSpans` because they're enriched later from transcript usage.
  const toolSpans = new Map<string, { span: Span; startedAt: number }>()

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
    span.setAttribute("trovis.event.type", "message_received")
    setIfPresent(span, "trovis.session.key", ctx.sessionKey)
    setIfPresent(
      span,
      "trovis.message.sender_id",
      event?.senderId ?? ctx.senderId,
    )
    setIfPresent(span, "trovis.message.thread_id", event?.threadId)
    setIfPresent(
      span,
      "trovis.message.content_length",
      typeof event?.content === "string" ? event.content.length : undefined,
    )
    setIfPresent(span, "trovis.trace.id", ctx.traceId)
    setIfPresent(span, "trovis.trace.span_id", ctx.spanId)
    setIfPresent(span, "trovis.trace.parent_span_id", ctx.parentSpanId)
    // Multi-agent gateways: the backend uses this to split spans into
    // per-agent virtual service names (`<service>-<agent_id>`).
    setIfPresent(span, "trovis.agent.id", pickAgentId(event, ctx))
    applyLoopSignals(span, event, ctx)
    // Capture inbound message text when the operator opted in.
    if (
      state.captureOutputs &&
      typeof event?.content === "string" &&
      event.content.length > 0
    ) {
      span.setAttribute(
        "trovis.message.content",
        truncate(event.content, 10_000),
      )
      // Workloop title: the inbound message is the best human-readable
      // label for what this run is about. Content-derived, so it follows
      // the same opt-in as content capture — with capture off, no title
      // is sent (the backend shows the loop untitled; we never send
      // placeholders). Creation-only on the backend, so re-sending on a
      // later message of the same run is harmless.
      const title = event.content.replace(/\s+/g, " ").trim().slice(0, 80)
      setIfPresent(span, "trovis.loop.title", title)
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
    span.setAttribute("trovis.event.type", "message_sending")
    setIfPresent(span, "trovis.session.key", ctx.sessionKey)
    setIfPresent(span, "trovis.agent.id", pickAgentId(event, ctx))
    applyLoopSignals(span, event, ctx)
    setIfPresent(span, "trovis.message.thread_id", event?.threadId)
    setIfPresent(
      span,
      "trovis.response.content_length",
      typeof event?.content === "string" ? event.content.length : undefined,
    )
    if (
      state.captureOutputs &&
      typeof event?.content === "string" &&
      event.content.length > 0
    ) {
      span.setAttribute(
        "trovis.response.content",
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
    span.setAttribute("trovis.event.type", "message_sent")
    setIfPresent(span, "trovis.session.key", ctx.sessionKey)
    setIfPresent(span, "trovis.agent.id", pickAgentId(event, ctx))
    applyLoopSignals(span, event, ctx)
    span.setAttribute("trovis.delivery.success", Boolean(success))
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
    span.setAttribute("trovis.event.type", "tool_call")
    span.setAttribute("trovis.tool.name", event.toolName)
    span.setAttribute("trovis.tool.call_id", event.toolCallId)
    // PRIVACY: parameter KEYS only — values are never read.
    span.setAttribute(
      "trovis.tool.param_keys",
      JSON.stringify(Object.keys(event?.params ?? {})),
    )
    setIfPresent(span, "trovis.agent.id", pickAgentId(event, ctx))
    applyLoopSignals(span, event, ctx)
    // Config-mapped handoff tools: calling a listed tool IS the handoff.
    // Declared tier only — the mapping ships empty; the operator opts in
    // per tool via config.handoffTools / TROVIS_HANDOFF_TOOLS.
    const handoffDir = state.handoffTools.get(event.toolName)
    if (handoffDir) {
      span.setAttribute("trovis.handoff.direction", handoffDir)
      span.setAttribute("trovis.handoff.reason", `tool:${event.toolName}`)
      span.setAttribute("trovis.handoff.id", randomUUID())
      trackRun(handoffActiveRuns, runKey(event, ctx))
    }

    toolSpans.set(event.toolCallId, { span, startedAt: Date.now() })
  })

  safeOn<AfterToolCallEvent>(api, "after_tool_call", (event, hookCtx) => {
    ensureInit(hookCtx ?? event?.context)
    const entry = toolSpans.get(event.toolCallId)
    if (!entry) return
    toolSpans.delete(event.toolCallId)

    const success = event?.success ?? !event?.error
    const duration = event?.durationMs ?? Date.now() - entry.startedAt
    entry.span.setAttribute("trovis.tool.success", Boolean(success))
    entry.span.setAttribute("trovis.tool.duration_ms", duration)
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
        entry.span.setAttribute("trovis.tool.result", truncate(raw, 10_000))
      }
    }
    entry.span.end()
  })

  // -- Model calls (started / ended pair) --
  // The span is NOT ended in model_call_ended: OpenClaw hooks carry no
  // tokens, so we keep it open and attach token counts once they land in
  // the transcript (drained on llm_output / agent_end). The end time is
  // recorded so deferring end() preserves the true call duration.
  safeOn<ModelCallStartedEvent>(api, "model_call_started", (event, hookCtx) => {
    const tracer = ensureInit(hookCtx ?? event?.context)
    if (!tracer) return
    state.sawConversationHook = true
    const ctx = ((hookCtx ?? event?.context) as OpenClawContext | undefined) ?? ({} as OpenClawContext)
    const span = tracer.startSpan("model_call", { kind: SpanKind.CLIENT })
    span.setAttribute("trovis.event.type", "model_call")
    setIfPresent(span, "gen_ai.system", event?.provider)
    setIfPresent(span, "gen_ai.request.model", event?.model)
    span.setAttribute("trovis.model.call_id", event.callId)
    setIfPresent(span, "trovis.agent.id", pickAgentId(event, ctx))
    applyLoopSignals(span, event, ctx)

    // Anchor the transcript read position BEFORE this call's usage line is
    // written, so the first call of a session isn't skipped on first sight.
    noteSessionBaseline(ctx)

    openModelSpans.set(event.callId, {
      span,
      startedAtMs: Date.now(),
      sessionKey: ctx.sessionKey,
      done: false,
    })
    if (ctx.sessionKey) {
      const order = sessionOpenOrder.get(ctx.sessionKey) ?? []
      order.push(event.callId)
      sessionOpenOrder.set(ctx.sessionKey, order)
    }
  })

  safeOn<ModelCallEndedEvent>(api, "model_call_ended", (event, hookCtx) => {
    const tracer = ensureInit(hookCtx ?? event?.context)
    state.sawConversationHook = true
    const ctx = ((hookCtx ?? event?.context) as OpenClawContext | undefined) ?? ({} as OpenClawContext)
    const entry = openModelSpans.get(event.callId)
    if (!entry) return

    entry.endedAtMs = Date.now()
    entry.span.setAttribute("trovis.model.duration_ms", event.durationMs)
    setIfPresent(entry.span, "trovis.model.outcome", event.outcome)
    if (
      typeof event.outcome === "string" &&
      event.outcome !== "ok" &&
      event.outcome !== "success"
    ) {
      entry.span.setStatus({ code: SpanStatusCode.ERROR, message: event.outcome })
    }

    // If a future gateway DOES carry usage on the event, use it and end now.
    const usage = pickTokenUsage(event)
    if (usage.total !== undefined || usage.input !== undefined) {
      applyUsageAndEnd(entry, usage, event?.model, event?.provider)
      forgetOpenSpan(event.callId, entry.sessionKey)
      return
    }

    // Otherwise try to drain the transcript right away (the usage entry may
    // already be written), then arm a safety timeout so the span is always
    // exported even if no later drain matches it.
    drainSessionUsage(tracer, ctx)
    if (openModelSpans.has(event.callId)) {
      const timer = setTimeout(() => {
        const e = openModelSpans.get(event.callId)
        if (e && !e.done) {
          e.span.end(e.endedAtMs) // export without tokens — better than leaking
          forgetOpenSpan(event.callId, e.sessionKey)
        }
      }, USAGE_DRAIN_TIMEOUT_MS)
      // Don't keep the gateway process alive just for this timer.
      if (typeof timer.unref === "function") timer.unref()
    }
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
    state.sawConversationHook = true
    const ctx = ((hookCtx ?? event?.context) as OpenClawContext | undefined) ?? ({} as OpenClawContext)
    // A response just landed → its usage line is likely written. Reconcile
    // open model_call spans for this session against the transcript.
    drainSessionUsage(tracer, ctx)
    const span = tracer.startSpan("llm_output", { kind: SpanKind.INTERNAL })
    span.setAttribute("trovis.event.type", "llm_output")
    setIfPresent(span, "trovis.session.key", ctx.sessionKey)
    setIfPresent(span, "trovis.agent.id", pickAgentId(event, ctx))
    applyLoopSignals(span, event, ctx)
    setIfPresent(span, "trovis.model.call_id", event?.callId)
    setIfPresent(span, "gen_ai.system", event?.provider)
    setIfPresent(span, "gen_ai.request.model", event?.model)

    // The model's response text arrives as `assistantTexts`, a string[]
    // of one or more assistant turns. We join with newlines so a
    // multi-turn response collapses into a single readable blob.
    const responseText = event?.assistantTexts?.join("\n") ?? ""
    setIfPresent(
      span,
      "trovis.response.content_length",
      responseText.length > 0 ? responseText.length : undefined,
    )
    if (state.captureOutputs && responseText.length > 0) {
      span.setAttribute(
        "trovis.response.content",
        truncate(responseText, 10_000),
      )
    }
    span.end()
  })

  // -- Agent run completion --
  safeOn<AgentEndEvent>(api, "agent_end", (event, hookCtx) => {
    const tracer = ensureInit(hookCtx ?? event?.context)
    if (!tracer) return
    state.sawConversationHook = true
    const ctx = ((hookCtx ?? event?.context) as OpenClawContext | undefined) ?? ({} as OpenClawContext)
    // Run finished → all of its usage lines are written. Final reconcile so
    // every model_call span in this session gets its tokens before export.
    drainSessionUsage(tracer, ctx)
    const span = tracer.startSpan("agent_run_complete", {
      kind: SpanKind.INTERNAL,
    })
    span.setAttribute("trovis.event.type", "agent_run_complete")
    setIfPresent(span, "trovis.agent.id", pickAgentId(event, ctx))
    // Drains any trovisCloseLoop() the agent queued late in the run, so
    // the explicit close lands here and marks the run closed BEFORE the
    // auto-close check below.
    applyLoopSignals(span, event, ctx)

    const success = event?.success ?? !event?.error
    if (typeof success === "boolean") {
      span.setAttribute("trovis.run.success", success)
      if (!success) {
        span.setStatus({
          code: SpanStatusCode.ERROR,
          message: typeof event?.error === "string" ? event.error : "run failed",
        })
      }
    }
    // Automatic workloop completion: agent_end IS OpenClaw's observable
    // "unit of work finished" signal, and this span is the run's final
    // span. Close as done UNLESS (a) the run failed — the backend's
    // stall/abandon sweep is the honest state for that, a fake `done`
    // would be wrong data in the permanent record; (b) the run declared a
    // handoff — the loop must stay awaiting_human/awaiting_agent, not
    // flip to done the moment the agent's turn ends; or (c) the run
    // already closed explicitly via trovisCloseLoop() — never two closes.
    const key = runKey(event, ctx)
    if (success !== false && !handoffActiveRuns.has(key) && !closedRuns.has(key)) {
      span.setAttribute("trovis.loop.close", "done")
    }
    handoffActiveRuns.delete(key)
    closedRuns.delete(key)
    setIfPresent(span, "trovis.run.message_provider", ctx.messageProvider)
    setIfPresent(span, "trovis.run.channel_id", ctx.channelId)
    setIfPresent(span, "trovis.run.job_id", ctx.jobId)
    span.end()
  })
}

// ---------------------------------------------------------------------------
// Command wiring
// ---------------------------------------------------------------------------
//
// /trovis is the user-facing setup command. Same UX pattern as channel
// plugins like /telegram or /whatsapp: the plugin knows what it needs, the
// command tells the user how to provide it, and `/trovis status` reflects
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
    `\`openclaw config set plugins.entries.trovis.config.${key} ${display}\``
  )
}

function wireCommands(api: OpenClawApi): void {
  if (typeof api?.registerCommand !== "function") {
    // Older gateways without command support — skip silently.
    return
  }

  const command: PluginCommand = {
    name: "trovis",
    description: "Connect to Trovis agent monitoring",
    acceptsArgs: true,
    async handler(ctx: CommandContext): Promise<CommandResult> {
      // ctx.args is the raw text after the command name — split it
      // ourselves. Multiple spaces collapse via /\s+/. Empty args means
      // the user typed bare `/trovis` (the help screen).
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
          `✅ Trovis endpoint set: \`${endpoint}\`\n\n` +
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
          `✅ Trovis API key set: \`${maskKey(key)}\`\n\n` +
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
                `spans as \`trovis.message.content\`, ` +
                `\`trovis.response.content\`, and \`trovis.tool.result\` ` +
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
          `⚙️ **Trovis Settings**`,
          ``,
          `• Endpoint: \`${state.endpoint || "(not set)"}\``,
          `• API key: \`${maskKey(state.apiKey)}\``,
          `• Agent name: \`${state.agentName}\``,
          `• Capture outputs: **${state.captureOutputs ? "on" : "off"}**`,
          `• User data: **${state.readUserData ? "on" : "off"}**`,
          `• Conversation access: **${
            state.allowConversationAccess === true ? "on" : "off"
          }** (required for tokens, cost & model)`,
          `• Handoff tools: ${
            state.handoffTools.size > 0
              ? [...state.handoffTools]
                  .map(([t, d]) => `\`${t}:${d}\``)
                  .join(", ")
              : "(none configured)"
          }`,
          `• Transcript file: \`${state.transcriptFileHint ?? "(not located)"}\``,
          `• Last usage read: ${state.lastUsageSeen ?? "(none yet)"}`,
          `• Telemetry: ${state.initialized ? "flowing" : "not initialized"}`,
        ]
        return reply(lines.join("\n"))
      }

      // --- debug ---------------------------------------------------------
      // Scans for the transcript, parses the most recent usage entry, and
      // reports what it found — so token capture can be verified from chat
      // alone (no gateway shell access needed).
      if (sub === "debug") {
        const file = findTranscriptFile(undefined)
        if (!file) {
          return reply(
            `🔎 **Trovis token debug**\n\n` +
              `No transcript file found under the scanned roots ` +
              `(\`~/.openclaw\`, \`/data/.openclaw\`).\n\n` +
              `If OpenClaw stores session logs elsewhere, set ` +
              `\`TROVIS_TRANSCRIPT_DIR\` to that directory (or the exact ` +
              `\`.jsonl\` file) and restart the gateway.`,
          )
        }
        // Parse the last usage-bearing entry without disturbing the live
        // tail offset (use a temporary read from a generous look-back).
        let sample = "(no usage entry found in the file tail)"
        // Field-name diagnostics (key names only, no content) so we can wire
        // up input/output for cost if they're under names we don't yet map.
        let diag = ""
        try {
          const size = fs.statSync(file).size
          const start = Math.max(0, size - 65_536)
          const fd = fs.openSync(file, "r")
          let text = ""
          try {
            const buf = Buffer.alloc(size - start)
            fs.readSync(fd, buf, 0, buf.length, start)
            text = buf.toString("utf-8")
          } finally {
            fs.closeSync(fd)
          }
          const lines2 = text.split("\n").filter((l) => l.trim().length > 0)
          for (let i = lines2.length - 1; i >= 0; i--) {
            let entry: Record<string, unknown>
            try {
              entry = JSON.parse(lines2[i]) as Record<string, unknown>
            } catch {
              continue
            }
            const u = deepFindUsage(entry)
            if (u.input !== undefined || u.output !== undefined || u.total !== undefined) {
              const model = deepFindStr(entry, ["model", "modelId", "model_id"])
              sample =
                `in:${u.input ?? "?"} out:${u.output ?? "?"} ` +
                `total:${u.total ?? "?"}` +
                (u.cacheRead !== undefined ? ` cacheRead:${u.cacheRead}` : "") +
                (model ? ` @${model}` : "")
              // If we couldn't get an input/output split, surface the entry's
              // field names so we can map the right keys for cost.
              if (u.input === undefined || u.output === undefined) {
                // Dump the actual numeric token fields (names + numbers only,
                // no content) so we can map OpenClaw's exact input/output keys.
                const numeric = dumpNumericTokenFields(entry.data ?? entry)
                diag =
                  `\n• Entry keys: \`${Object.keys(entry).join(", ")}\`` +
                  `\n• Numeric token fields: \`${
                    numeric.join(", ") || "(none found)"
                  }\``
              }
              break
            }
          }
        } catch {
          sample = "(could not read the transcript file)"
        }
        return reply(
          `🔎 **Trovis token debug**\n\n` +
            `• Transcript file: \`${file}\`\n` +
            `• Parsed usage from tail: ${sample}` +
            diag +
            `\n• Last usage attached to a span: ${state.lastUsageSeen ?? "(none yet)"}\n` +
            `• Conversation access: **${
              state.allowConversationAccess === true ? "on" : "off"
            }**\n\n` +
            (sample.startsWith("in:")
              ? `✅ Token parsing works. New model calls will report these to ` +
                `Trovis (cost is computed there from tokens + model).`
              : `⚠️ Couldn't parse token counts from this file. Reply with a ` +
                `sample line (token fields only) and we'll add the field names.`),
        )
      }

      // --- status --------------------------------------------------------
      if (sub === "status") {
        if (!state.initialized) {
          return reply(
            `⚠️ Trovis is not connected.\n\n` +
              `Get your endpoint URL from your Trovis dashboard ` +
              `(Add Agent → OpenClaw), then run:\n\n` +
              `\`/trovis connect <your-endpoint-url>\``,
          )
        }
        // Initialized but no conversation hook has fired → tokens, cost and
        // per-call model can't be captured. Almost always a missing flag.
        const convoWarning = !state.sawConversationHook
          ? `\n\n⚠️ No LLM-call telemetry yet — **tokens, cost, and model ` +
            `won't be captured.** Enable conversation access:\n` +
            `\`openclaw config set ` +
            `plugins.entries.trovis.config.hooks.allowConversationAccess ` +
            `true\`\nthen restart the gateway.`
          : ``
        return reply(
          `✅ Trovis is active.\n\n` +
            `• Endpoint: \`${state.endpoint}\`\n` +
            `• Agent: \`${state.agentName}\`\n` +
            `• Telemetry: flowing\n` +
            `• LLM-call telemetry: ${
              state.sawConversationHook ? "active" : "none seen"
            }` +
            convoWarning,
        )
      }

      // --- default / help ------------------------------------------------
      return reply(
        `🔍 **Trovis Agent Monitoring**\n\n` +
          `**Setup**\n` +
          `• \`/trovis connect <url>\` — set the Trovis endpoint\n` +
          `• \`/trovis apikey <key>\` — set your API key\n\n` +
          `**Capture toggles**\n` +
          `• \`/trovis capture on\` / \`off\` — message + tool output capture (default off)\n` +
          `• \`/trovis userdata on\` / \`off\` — USER.md + MEMORY.md in registration (default off)\n\n` +
          `**Inspect**\n` +
          `• \`/trovis settings\` — show all current config\n` +
          `• \`/trovis status\` — connection state + telemetry flowing or not\n` +
          `• \`/trovis debug\` — locate the transcript & verify token parsing\n\n` +
          `Setting commands update in-memory state immediately. To make ` +
          `permanent, run \`openclaw config set plugins.entries.trovis.config.<key> <value>\`. ` +
          `\`connect\` and \`apikey\` need a gateway restart to re-init the OTLP exporter.`,
      )
    },
  }

  // Defensive try/catch — if the gateway's registerCommand contract
  // drifts again, command-registration failure shouldn't take down the
  // rest of the plugin (telemetry hooks are what matters).
  try {
    api.registerCommand(command)
    console.log(`${LOG} /trovis command registered.`)
  } catch (e) {
    console.warn(
      `${LOG} Failed to register /trovis command: ${(e as Error).message}. ` +
        `Telemetry will continue to work; command-based setup is unavailable.`,
    )
  }
}

// ---------------------------------------------------------------------------
// Test surface (private)
// ---------------------------------------------------------------------------
// Exposed ONLY so the test suite can inject a fake tracer and inspect
// parsed config without standing up a gateway or the OTEL SDK. Not part of
// the public API — subject to change without a version bump.

export const __internal = { state, parseHandoffTools }

// ---------------------------------------------------------------------------
// Plugin entry
// ---------------------------------------------------------------------------

export default definePluginEntry({
  id: "trovis",
  name: "Trovis Agent Management",
  description:
    "Automatic agent monitoring and management. Captures telemetry, reads agent identity, and sends everything to your Trovis dashboard.",
  register(api: OpenClawApi) {
    state.gatewayVersion =
      api?.version ?? api?.gateway?.version ?? "unknown"

    // Env-var disable is checked at register() so we never even wire
    // hooks when an operator wants the plugin totally inert. The
    // pluginConfig.enabled flag from openclaw.json is checked later
    // inside ensureInit, on the first hook that exposes pluginConfig.
    if ((process.env.TROVIS_ENABLED ?? process.env.OVERSEE_ENABLED) === "false") {
      console.log(`${LOG} Plugin disabled via TROVIS_ENABLED=false.`)
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
    // is what people install this plugin for; the /trovis command is a
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
