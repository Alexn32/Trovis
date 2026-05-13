# @alexn32/openclaw-plugin

Oversee plugin for OpenClaw — automatic agent telemetry and management.

## What it does

This plugin runs inside the OpenClaw gateway and sends two kinds of data to your Oversee instance:

1. **Agent identity** (once at startup): the contents of each agent's `SOUL.md`, `IDENTITY.md`, and `AGENTS.md`. These define what the agent is and are required for accurate dashboard descriptions. On startup, the plugin reads these files from each agent's workspace and sends them to Oversee. **Review these files for secrets or sensitive instructions before enabling the plugin** — they leave your machine.
2. **Operational telemetry** (during agent activity): by default, metadata only — message lengths, tool names, durations, success flags, opaque IDs. **Message content and tool outputs are NOT captured by default.** If you enable `captureOutputs` (see [Output capture](#output-capture)), the plugin will also send the text of messages, responses, and tool results.

`USER.md` and `MEMORY.md` are **not** read by default. If you enable `readUserData`, they are also sent on startup. These files may contain personal preferences and accumulated history. Only enable if you trust the configured Oversee endpoint.

The plugin is **inert until an endpoint is explicitly configured** — there is no hardcoded fallback URL.

All hooks are observation-only: the plugin never blocks, rewrites, or gates the message pipeline. Event names, payloads, and context fields are taken from [docs.openclaw.ai/plugins/plugin-hooks](https://docs.openclaw.ai/plugins/plugin-hooks).

## Activation

The plugin activates automatically with the gateway. To disable:

- Set `enabled: false` in your plugin config (`plugins.entries.oversee.enabled` in `openclaw.json`), **or**
- Set `OVERSEE_ENABLED=false` in the environment before starting the gateway.

Either takes effect at the next gateway start. The env var also fires defensively on the first hook invocation, so a late-set env var stops telemetry too.

## Installation

```bash
openclaw plugins install clawhub:@alexn32/openclaw-plugin
```

Then restart your OpenClaw gateway.

## Configuration

Add an `oversee` entry under `plugins.entries` in your `openclaw.json`:

```json
{
  "plugins": {
    "entries": {
      "oversee": {
        "endpoint": "https://your-oversee-instance.example.com/v1/traces",
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
| `endpoint`       | string  | **yes**  | —                | Your Oversee OTLP/HTTP traces endpoint. The plugin is inert until this is set.                                                              |
| `apiKey`         | string  | no       | —                | Sent as the `X-Oversee-Api-Key` header on every export. Required by hosted Oversee deployments.                                              |
| `agentName`      | string  | no       | `openclaw-agent` | Identifies this gateway in the Oversee dashboard.                                                                                            |
| `enabled`        | boolean | no       | `true`           | Set to `false` to load the plugin without emitting telemetry.                                                                                |
| `captureOutputs` | boolean | no       | `false`          | When `true`, include message text and tool results in spans (see [Output capture](#output-capture)). Off by default.                          |
| `readUserData`   | boolean | no       | `false`          | When `true`, include `USER.md` and `MEMORY.md` in the startup registration. May contain personal data — opt-in only.                          |

Each field can also be supplied via environment variables for containerized deployments: `OVERSEE_ENDPOINT`, `OVERSEE_AGENT_NAME`, `OVERSEE_API_KEY`, `OVERSEE_ENABLED`, `OVERSEE_CAPTURE_OUTPUTS`.

### API key handling

Store your Oversee API key as a secret — in your secret manager, an environment variable injected by your deployment system, or your platform's encrypted-config feature. Scope it to telemetry use only (one key per gateway is a reasonable default). **Rotate immediately if exposed.** The plugin masks the key in `/oversee settings` output (`ov_sk_AB…YZ12`) but the full key is in the `X-Oversee-Api-Key` header on every export — anyone who can intercept your egress traffic can read it.

### `hooks.allowConversationAccess` is required

This plugin registers handlers for `model_call_started`, `model_call_ended`, and `agent_end` — OpenClaw's conversation-adjacent hooks. Per the plugin SDK, conversation-adjacent hooks require an explicit opt-in via `hooks.allowConversationAccess: true`. Without this flag, those hooks won't fire and you'll only get telemetry for `message_received`, `message_sent`, `before_tool_call`, and `after_tool_call`. The plugin is still safe to use without it — you just lose LLM call and run-completion visibility.

## What data is captured

Two categories with different privacy properties. Please read both before deploying.

### 1. Agent identity (once at startup)

An `agent_registration` span is emitted at gateway start for each agent. It carries the **full contents** of the agent's identity files (truncated to 32 KB per file):

**Always sent** (when the plugin is enabled and an endpoint is configured):

- `oversee.agent.soul` — contents of `SOUL.md`
- `oversee.agent.identity` — contents of `IDENTITY.md`
- `oversee.agent.operating_manual` — contents of `AGENTS.md`
- `oversee.agent.id` / `oversee.agent.workspace_path` / `oversee.agent.model` — agent metadata

**Sent only when `readUserData: true`:**

- `oversee.agent.user_context` — contents of `USER.md`
- `oversee.agent.memory` — contents of `MEMORY.md`

Review your agents' identity files (`SOUL.md`, `IDENTITY.md`, `AGENTS.md`) before enabling the plugin. The entire file contents leave your machine on the first startup. If they contain secrets, redact them first.

### 2. Operational telemetry (per agent activity)

Per-span attributes. The defaults are **metadata only** — no message bodies, no tool inputs, no tool outputs.

**`message_received`** — `oversee.event.type`, `oversee.session.key`, `oversee.message.sender_id`, `oversee.message.thread_id`, `oversee.message.content_length` (count, **not content**), `oversee.trace.*` (OpenClaw trace context).

**`message_sent`** — `oversee.event.type`, `oversee.session.key`, `oversee.delivery.success`.

**`tool_call`** (combined from `before_tool_call` + `after_tool_call`) — `oversee.event.type`, `oversee.tool.name`, `oversee.tool.call_id`, `oversee.tool.param_keys` (JSON array of parameter **names**, never values), `oversee.tool.success`, `oversee.tool.duration_ms`, `oversee.agent.id`, `oversee.run.id`.

**`model_call`** (combined from `model_call_started` + `model_call_ended`) — `oversee.event.type`, `gen_ai.system`, `gen_ai.request.model`, `oversee.model.call_id`, `oversee.model.duration_ms`, `oversee.model.outcome`, `oversee.run.id`.

**`agent_run_complete`** (from `agent_end`) — `oversee.event.type`, `oversee.run.id`, `oversee.run.success`, `oversee.run.message_provider`, `oversee.run.channel_id`, `oversee.run.job_id`.

### Output capture

`captureOutputs: false` is the default. When you set it to `true`, three additional span attributes are populated with **actual content** (each truncated to 10 000 characters):

- `oversee.message.content` — inbound message text (on `message_received`)
- `oversee.response.content` — outbound response text (on `message_sent`)
- `oversee.tool.result` — tool return values, `JSON.stringify`'d when not already strings (on `after_tool_call`)

> **These fields may contain private data, secrets, or business information.** Only enable `captureOutputs` if you trust the configured Oversee endpoint and its operators. Off by default for a reason.

You can flip the flag at runtime in any agent's chat with `/oversee capture on` (or `off`). The change takes effect on the next event — already-emitted spans aren't modified retroactively.

### Resource attributes

The plugin sets **only four** resource attributes on every span, all explicitly:

- `service.name` — your configured `agentName`
- `service.version` — the plugin version
- `oversee.plugin.version` — also the plugin version (a stable marker the dashboard uses to identify Oversee-instrumented agents)
- `openclaw.gateway.version` — the OpenClaw gateway version when available

OpenTelemetry may by default collect standard resource attributes including **host identifiers** (`host.id`, `host.name`, process metadata). To prevent that, the plugin initializes `NodeSDK` with `autoDetectResources: false` — the default detectors are skipped entirely. Only the four attributes listed above leave your machine in the resource section.

### What is never captured

Under any configuration:

- **LLM prompts, full chat history, and full LLM response bodies** — the `model_call_*` hooks are platform-blocked from exposing these.
- **Upstream provider request IDs, headers, or bodies** — same hook contract.
- **API keys, credentials, or environment values** — the plugin never reads `process.env.*` except for its own `OVERSEE_*` config vars.
- **Host identifiers** — see "Resource attributes" above.

## In-chat commands

The plugin registers an `/oversee` slash command that any agent in the gateway can run.

```
/oversee                          → help with all commands
/oversee connect <url>            → set the endpoint, persist to openclaw.json
/oversee apikey <key>             → set the API key, persist (shown masked thereafter)
/oversee capture on | off         → toggle captureOutputs, persist
/oversee userdata on | off        → toggle readUserData, persist
/oversee settings                 → show all current config (key masked)
/oversee status                   → connection state + telemetry-flowing check
```

`/oversee connect` and `/oversee apikey` need a gateway restart to fully take effect (the OTLP exporter is constructed at startup with the URL and auth header baked in). The other toggles apply to the next event live.

### How chat commands persist settings

When a setting command runs (`connect`, `apikey`, `capture`, `userdata`), the plugin updates its in-memory state immediately and best-effort-persists to `openclaw.json` by shelling out to the gateway's own CLI:

```sh
openclaw config set plugins.entries.oversee.config.<key> <value>
```

The shell-out uses `execFile` (not shell `exec`). **Arguments are fixed strings** — no user input is interpolated into commands, so user-supplied URLs or keys can't inject extra shell commands. If the `openclaw config set` subcommand doesn't exist or fails for any reason, the in-memory state still updates (the change is good for the current session) and a warning is logged.

## Reliability

Spans are exported via the OpenTelemetry `BatchSpanProcessor`, which batches in memory and retries on transient failures. If your Oversee endpoint is briefly unreachable, the plugin will buffer and retry; if it is unreachable for an extended period the buffer drops the oldest spans. The plugin never blocks the OpenClaw message pipeline — telemetry is fire-and-forget.

## Links

- Oversee → https://oversee.dev
- OpenTelemetry → https://opentelemetry.io
