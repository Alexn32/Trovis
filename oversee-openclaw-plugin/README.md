# @oversee/openclaw-plugin

Oversee plugin for OpenClaw — automatic agent telemetry and management.

## What it does

This plugin runs inside the OpenClaw gateway and forwards every agent interaction (inbound messages, outbound responses, tool calls, LLM requests, run completion) to your Oversee instance as OpenTelemetry traces. No code changes to your agents are required — install the plugin, add a config block, restart the gateway, and your agents appear in the Oversee dashboard within seconds.

All hooks are observation-only: the plugin never blocks, rewrites, or gates the message pipeline. Event names, payloads, and context fields are taken from [docs.openclaw.ai/plugins/plugin-hooks](https://docs.openclaw.ai/plugins/plugin-hooks).

## Installation

### Future (when published to ClawHub)

```bash
openclaw plugins install clawhub:@oversee/openclaw-plugin
```

### Local install (for now)

From the directory containing your `openclaw.json`:

```bash
npm install /path/to/oversee-openclaw-plugin
```

Then restart your OpenClaw gateway.

## Configuration

Add an `oversee` entry under `plugins.entries` in your `openclaw.json`:

```json
{
  "plugins": {
    "entries": {
      "oversee": {
        "endpoint": "http://localhost:8080/v1/traces",
        "agentName": "my-openclaw-instance",
        "enabled": true,
        "hooks": {
          "allowConversationAccess": true
        }
      }
    }
  }
}
```

| Field       | Type    | Required | Default          | Description                                                                                |
| ----------- | ------- | -------- | ---------------- | ------------------------------------------------------------------------------------------ |
| `endpoint`  | string  | yes      | —                | Your Oversee OTLP/HTTP traces endpoint. In production, swap `localhost` for your hostname. |
| `agentName` | string  | no       | `openclaw-agent` | Identifies this gateway in the Oversee dashboard.                                          |
| `enabled`   | boolean | no       | `true`           | Set to `false` to load the plugin without emitting telemetry.                              |

You can also override the endpoint and agent name via environment variables (`OVERSEE_ENDPOINT`, `OVERSEE_AGENT_NAME`) — useful for containerized deployments.

### `hooks.allowConversationAccess` is required

This plugin registers handlers for `model_call_started`, `model_call_ended`, and `agent_end` — OpenClaw's conversation-adjacent hooks. Per the plugin SDK, conversation-adjacent hooks require an explicit opt-in via `hooks.allowConversationAccess: true`. Without this flag, those hooks won't fire and you'll only get telemetry for `message_received`, `message_sent`, `before_tool_call`, and `after_tool_call`. The plugin is still safe to use without it — you just lose LLM call and run-completion visibility.

Note that even with `allowConversationAccess: true`, this plugin never reads message content, tool parameter values, prompts, or LLM responses (see the [privacy section](#what-data-is-captured) below). The flag only governs whether the conversation-adjacent hooks fire.

## What data is captured

Oversee only captures **metadata**. The full list of attributes emitted on each span:

**`message_received`**

- `oversee.event.type` — `"message_received"`
- `oversee.session.key` — opaque session identifier (from `ctx.sessionKey`)
- `oversee.message.sender_id` — opaque sender identifier
- `oversee.message.thread_id` — opaque thread identifier
- `oversee.message.content_length` — character count of the message body
- `oversee.trace.id` / `oversee.trace.span_id` / `oversee.trace.parent_span_id` — OpenClaw's own trace context

**`message_sent`**

- `oversee.event.type` — `"message_sent"`
- `oversee.session.key` — opaque session identifier
- `oversee.delivery.success` — boolean

**`tool_call`** (combined from `before_tool_call` + `after_tool_call`)

- `oversee.event.type` — `"tool_call"`
- `oversee.tool.name` — name of the tool that was invoked
- `oversee.tool.call_id` — the tool call's correlation id
- `oversee.tool.param_keys` — JSON array of parameter names (keys only, **not values**)
- `oversee.tool.success` — boolean
- `oversee.tool.duration_ms` — wall-clock duration
- `oversee.agent.id` — the agent that invoked the tool
- `oversee.run.id` — the run the tool call belongs to

**`model_call`** (combined from `model_call_started` + `model_call_ended`)

- `oversee.event.type` — `"model_call"`
- `gen_ai.system` — provider (anthropic, openai, etc.) — OTEL GenAI semantic convention
- `gen_ai.request.model` — model name
- `oversee.model.call_id` — call correlation id
- `oversee.model.duration_ms` — wall-clock duration
- `oversee.model.outcome` — outcome string reported by the platform
- `oversee.run.id` — the run the model call belongs to

**`agent_run_complete`** (from `agent_end`)

- `oversee.event.type` — `"agent_run_complete"`
- `oversee.run.id` — run identifier
- `oversee.run.success` — boolean
- `oversee.run.message_provider` — e.g. `"discord"`, `"telegram"`
- `oversee.run.channel_id` — opaque channel identifier
- `oversee.run.job_id` — set only on cron-triggered runs

### What is NOT captured

The following are **never** read or transmitted by this plugin:

- Message content (only its length)
- Tool parameter values (only the names of the parameters)
- LLM prompt or response text
- Upstream provider request IDs, headers, or bodies — the platform's
  `model_call_*` hooks explicitly do not expose these
- User identifiers beyond opaque session/sender IDs
- API keys, credentials, or any environment values

If you need different privacy boundaries, fork the plugin and adjust `wireEvents()` in `index.ts`. The privacy model is enforced in code, not by policy.

## Reliability

Spans are exported via the OpenTelemetry `BatchSpanProcessor`, which batches in memory and retries on transient failures. If your Oversee endpoint is briefly unreachable, the plugin will buffer and retry; if it is unreachable for an extended period the buffer drops the oldest spans. The plugin never blocks the OpenClaw message pipeline — telemetry is fire-and-forget.

## Links

- Oversee → https://oversee.dev
- OpenTelemetry → https://opentelemetry.io
