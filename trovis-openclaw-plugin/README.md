# @trovis/openclaw-plugin

Trovis plugin for OpenClaw — automatic agent telemetry and management.

## What it does

This plugin runs inside the OpenClaw gateway and sends two kinds of data to your Trovis instance:

1. **Agent identity** (once at startup): the contents of each agent's `SOUL.md`, `IDENTITY.md`, and `AGENTS.md`. These define what the agent is and are required for accurate dashboard descriptions. On startup, the plugin reads these files from each agent's workspace and sends them to Trovis. **Review these files for secrets or sensitive instructions before enabling the plugin** — they leave your machine.
2. **Operational telemetry** (during agent activity): by default, metadata only — message lengths, tool names, durations, success flags, opaque IDs. **Message content and tool outputs are NOT captured by default.** If you enable `captureOutputs` (see [Output capture](#output-capture)), the plugin will also send the text of messages, responses, and tool results.

`USER.md` and `MEMORY.md` are **not** read by default. If you enable `readUserData`, they are also sent on startup. These files may contain personal preferences and accumulated history. Only enable if you trust the configured Trovis endpoint.

The plugin is **inert until an endpoint is explicitly configured** — there is no hardcoded fallback URL.

All hooks are observation-only: the plugin never blocks, rewrites, or gates the message pipeline. Event names, payloads, and context fields are taken from [docs.openclaw.ai/plugins/plugin-hooks](https://docs.openclaw.ai/plugins/plugin-hooks).

## Activation

The plugin activates automatically with the gateway. To disable:

- Set `enabled: false` in your plugin config (`plugins.entries.trovis.enabled` in `openclaw.json`), **or**
- Set `TROVIS_ENABLED=false` in the environment before starting the gateway.

Either takes effect at the next gateway start. The env var also fires defensively on the first hook invocation, so a late-set env var stops telemetry too.

## Installation

```bash
openclaw plugins install clawhub:@trovis/openclaw-plugin
```

Then restart your OpenClaw gateway.

## Configuration

Add an `trovis` entry under `plugins.entries` in your `openclaw.json`:

```json
{
  "plugins": {
    "entries": {
      "trovis": {
        "endpoint": "https://your-trovis-instance.example.com/v1/traces",
        "apiKey": "ov_sk_…",
        "agentName": "my-openclaw-instance",
        "enabled": true,
        "captureOutputs": false,
        "readUserData": false,
        "hooks": {
          "allowConversationAccess": true
        }
      }
    }
  }
}
```

| Field            | Type    | Required | Default          | Description                                                                                                                                  |
| ---------------- | ------- | -------- | ---------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| `endpoint`       | string  | **yes**  | —                | Your Trovis OTLP/HTTP traces endpoint. The plugin is inert until this is set.                                                              |
| `apiKey`         | string  | no       | —                | Sent as the `X-Trovis-Api-Key` header on every export. Required by hosted Trovis deployments.                                              |
| `agentName`      | string  | no       | `openclaw-agent` | Identifies this gateway in the Trovis dashboard.                                                                                            |
| `enabled`        | boolean | no       | `true`           | Set to `false` to load the plugin without emitting telemetry.                                                                                |
| `captureOutputs` | boolean | no       | `false`          | When `true`, include message text and tool results in spans (see [Output capture](#output-capture)). Off by default.                          |
| `readUserData`   | boolean | no       | `false`          | When `true`, include `USER.md` and `MEMORY.md` in the startup registration. May contain personal data — opt-in only.                          |
| `handoffTools`   | string  | no       | *(empty)*        | Comma-separated `tool:direction` pairs marking tools as workloop handoffs (see [Workloops](#workloops)). Empty by default — never guessed.     |

Each field can also be supplied via environment variables for containerized deployments: `TROVIS_ENDPOINT`, `TROVIS_AGENT_NAME`, `TROVIS_API_KEY`, `TROVIS_ENABLED`, `TROVIS_CAPTURE_OUTPUTS`, `TROVIS_HANDOFF_TOOLS`, `TROVIS_TRANSCRIPT_DIR` (override the auto-detected session-transcript directory).

### API key handling

Store your Trovis API key as a secret — in your secret manager, an environment variable injected by your deployment system, or your platform's encrypted-config feature. Scope it to telemetry use only (one key per gateway is a reasonable default). **Rotate immediately if exposed.** The plugin masks the key in `/trovis settings` output (`ov_sk_AB…YZ12`) but the full key is in the `X-Trovis-Api-Key` header on every export — anyone who can intercept your egress traffic can read it.

### `hooks.allowConversationAccess` is required for tokens, cost & model

This plugin registers handlers for `model_call_started`, `model_call_ended`, `llm_output`, and `agent_end` — OpenClaw's conversation-adjacent hooks. Per the plugin SDK, conversation-adjacent hooks require an explicit opt-in via `hooks.allowConversationAccess: true`. Without this flag, those hooks won't fire and you'll only get telemetry for `message_received`, `message_sent`, `before_tool_call`, and `after_tool_call` — **no token counts, no cost, and no per-call model**. The plugin is still safe to use without it; you just lose LLM-call visibility. Run `/trovis status` — it warns when LLM-call telemetry hasn't been seen.

### Token usage comes from the session transcript, not the hooks

OpenClaw's `model_call_ended` hook carries the model, provider, duration, and outcome — but **not token usage** (the request to add it, [openclaw#21184](https://github.com/openclaw/openclaw/issues/21184), was closed as not planned). OpenClaw does persist per-response usage (input/output/total/cache tokens, and its own cost) to per-session **transcript JSONL files**. This plugin reads those files and attaches the token counts to the matching `model_call` span. It reads **only the usage object** (token counts) — never prompt or response content. Cost is then computed by Trovis from tokens + model (its pricing table), so the cost basis is consistent across every agent regardless of platform.

The transcript directory is auto-detected (`~/.openclaw/sessions`, `~/.openclaw/transcripts`, and the `/data` equivalents). If your gateway stores them elsewhere, set `TROVIS_TRANSCRIPT_DIR` to the directory. `/trovis settings` shows the directory the plugin resolved. If tokens still read zero with conversation access enabled, confirm the directory and that transcript entries carry a `usage` object — file an issue with a sample entry (token fields only) and we'll add the field aliases.

## What data is captured

Two categories with different privacy properties. Please read both before deploying.

### 1. Agent identity (once at startup)

An `agent_registration` span is emitted at gateway start for each agent. It carries the **full contents** of the agent's identity files (truncated to 32 KB per file):

**Always sent** (when the plugin is enabled and an endpoint is configured):

- `trovis.agent.soul` — contents of `SOUL.md`
- `trovis.agent.identity` — contents of `IDENTITY.md`
- `trovis.agent.operating_manual` — contents of `AGENTS.md`
- `trovis.agent.id` / `trovis.agent.workspace_path` / `trovis.agent.model` — agent metadata

**Sent only when `readUserData: true`:**

- `trovis.agent.user_context` — contents of `USER.md`
- `trovis.agent.memory` — contents of `MEMORY.md`

Review your agents' identity files (`SOUL.md`, `IDENTITY.md`, `AGENTS.md`) before enabling the plugin. The entire file contents leave your machine on the first startup. If they contain secrets, redact them first.

### 2. Operational telemetry (per agent activity)

Per-span attributes. The defaults are **metadata only** — no message bodies, no tool inputs, no tool outputs.

**`message_received`** — `trovis.event.type`, `trovis.session.key`, `trovis.message.sender_id`, `trovis.message.thread_id`, `trovis.message.content_length` (count, **not content**), `trovis.trace.*` (OpenClaw trace context).

**`message_sent`** — `trovis.event.type`, `trovis.session.key`, `trovis.delivery.success`.

**`tool_call`** (combined from `before_tool_call` + `after_tool_call`) — `trovis.event.type`, `trovis.tool.name`, `trovis.tool.call_id`, `trovis.tool.param_keys` (JSON array of parameter **names**, never values), `trovis.tool.success`, `trovis.tool.duration_ms`, `trovis.agent.id`, `trovis.run.id`.

**`model_call`** (combined from `model_call_started` + `model_call_ended`, enriched from the transcript) — `trovis.event.type`, `gen_ai.system`, `gen_ai.request.model`, `trovis.model.call_id`, `trovis.model.duration_ms`, `trovis.model.outcome`, `trovis.run.id`, and the token counts `gen_ai.usage.input_tokens` / `output_tokens` / `total_tokens` / `cache_creation_input_tokens` / `cache_read_input_tokens` (token **counts** only — read from the session transcript, never prompt/response content). `trovis.model.usage_source=transcript` marks spans whose tokens came from the transcript.

**`agent_run_complete`** (from `agent_end`) — `trovis.event.type`, `trovis.run.id`, `trovis.run.success`, `trovis.run.message_provider`, `trovis.run.channel_id`, `trovis.run.job_id`.

### Output capture

`captureOutputs: false` is the default. When you set it to `true`, three additional span attributes are populated with **actual content** (each truncated to 10 000 characters):

- `trovis.message.content` — inbound message text (on `message_received`)
- `trovis.response.content` — outbound response text (on `message_sent`)
- `trovis.tool.result` — tool return values, `JSON.stringify`'d when not already strings (on `after_tool_call`)

> **These fields may contain private data, secrets, or business information.** Only enable `captureOutputs` if you trust the configured Trovis endpoint and its operators. Off by default for a reason.

You can flip the flag at runtime in any agent's chat with `/trovis capture on` (or `off`). The change takes effect on the next event — already-emitted spans aren't modified retroactively.

### Resource attributes

The plugin sets **only four** resource attributes on every span, all explicitly:

- `service.name` — your configured `agentName`
- `service.version` — the plugin version
- `trovis.plugin.version` — also the plugin version (a stable marker the dashboard uses to identify Trovis-instrumented agents)
- `openclaw.gateway.version` — the OpenClaw gateway version when available

OpenTelemetry may by default collect standard resource attributes including **host identifiers** (`host.id`, `host.name`, process metadata). To prevent that, the plugin initializes `NodeSDK` with `autoDetectResources: false` — the default detectors are skipped entirely. Only the four attributes listed above leave your machine in the resource section.

### What is never captured

Under any configuration:

- **LLM prompts, full chat history, and full LLM response bodies** — the `model_call_*` hooks are platform-blocked from exposing these.
- **Upstream provider request IDs, headers, or bodies** — same hook contract.
- **API keys, credentials, or environment values** — the plugin never reads `process.env.*` except for its own `TROVIS_*` config vars.
- **Host identifiers** — see "Resource attributes" above.

## In-chat commands

The plugin registers an `/trovis` slash command that any agent in the gateway can run.

```
/trovis                          → help with all commands
/trovis connect <url>            → set the endpoint, persist to openclaw.json
/trovis apikey <key>             → set the API key, persist (shown masked thereafter)
/trovis capture on | off         → toggle captureOutputs, persist
/trovis userdata on | off        → toggle readUserData, persist
/trovis settings                 → show all current config (key masked)
/trovis status                   → connection state + telemetry-flowing check
```

`/trovis connect` and `/trovis apikey` need a gateway restart to fully take effect (the OTLP exporter is constructed at startup with the URL and auth header baked in). The other toggles apply to the next event live.

### How chat commands persist settings

When a setting command runs (`connect`, `apikey`, `capture`, `userdata`), the plugin updates its in-memory state immediately and best-effort-persists to `openclaw.json` by shelling out to the gateway's own CLI:

```sh
openclaw config set plugins.entries.trovis.config.<key> <value>
```

The shell-out uses `execFile` (not shell `exec`). **Arguments are fixed strings** — no user input is interpolated into commands, so user-supplied URLs or keys can't inject extra shell commands. If the `openclaw config set` subcommand doesn't exist or fails for any reason, the in-memory state still updates (the change is good for the current session) and a warning is logged.

## Workloops

Trovis groups an agent's spans into **workloops** — units of work with a
derived state (`working`, `awaiting_human`, `done`, …) shown on the
dashboard. The plugin declares loop boundaries automatically and gives you
two small hooks for the parts only your agent knows.

**Automatic (no setup):**

- Every span carries OpenClaw's own run id as `trovis.run.id`, so one
  message-handling cycle = one loop. Spans without a run id fall back to
  the backend's 30-minute gap rule.
- When `captureOutputs` is on, the inbound message (collapsed, first 80
  chars) becomes the loop's title. With capture off no title is sent —
  titles are content-derived and follow the same opt-in.
- When a run ends without error (`agent_end`), the loop is closed as
  `done` — unless the run declared a handoff (the loop stays
  `awaiting_human` / `awaiting_agent`) or was already closed explicitly.
  Failed runs are never closed; the backend's sweep handles genuinely
  abandoned loops.

**Declared handoffs — helper:**

```ts
import { trovisHandoff, trovisCloseLoop } from "@trovis/openclaw-plugin"

trovisHandoff("to_human", "sarah@acme.com", "needs approval")  // loop -> awaiting_human
trovisCloseLoop("done")                                        // close the loop early
```

`trovisHandoff(direction, target?, reason?)` marks the current unit of work
as waiting on a human (`"to_human"`) or another agent (`"to_agent"`); the
attributes land on the next span the plugin emits, and the run's automatic
`done` close is suppressed. It returns the generated handoff id for later
correlation. For `"to_human"`, pass the teammate's **email** (or their
Trovis user id) as `target` — the Trovis loop record resolves it to their
name ("Handed to Sarah"); anything unresolvable displays as "a human". `trovisCloseLoop(reason = "done")` closes the loop from agent
code — useful when work completes mid-run.

**Declared handoffs — config-mapped tools:**

```json
"handoffTools": "send_slack_message:to_human,request_approval:to_human,delegate_task:to_agent"
```

(or `TROVIS_HANDOFF_TOOLS` with the same format). When the agent calls a
listed tool, that tool-call span is marked as a handoff automatically, with
`reason: "tool:<name>"`. Ships **empty** — the plugin never guesses which
tools hand work off.

All loop signals are plain span attributes: older Trovis backends (and any
other OTLP backend) simply ignore them.

## Reliability

Spans are exported via the OpenTelemetry `BatchSpanProcessor`, which batches in memory and retries on transient failures. If your Trovis endpoint is briefly unreachable, the plugin will buffer and retry; if it is unreachable for an extended period the buffer drops the oldest spans. The plugin never blocks the OpenClaw message pipeline — telemetry is fire-and-forget.

## Links

- Trovis → https://oversee.dev
- OpenTelemetry → https://opentelemetry.io
