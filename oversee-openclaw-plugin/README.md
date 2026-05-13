# @oversee/openclaw-plugin

Oversee plugin for OpenClaw — automatic agent telemetry and management.

## What it does

This plugin runs inside the OpenClaw gateway and sends two kinds of data to your Oversee instance:

1. **Agent identity** (once at startup): the contents of each agent's `SOUL.md`, `IDENTITY.md`, and `AGENTS.md`. These define what the agent is and are required for accurate dashboard descriptions.
2. **Operational telemetry** (during agent activity): metadata about messages, tool calls, model calls, and run completion. Message content and tool parameter values are **never** captured.

`USER.md` and `MEMORY.md` may contain personal data and are only read if you explicitly set `readUserData: true`.

All hooks are observation-only: the plugin never blocks, rewrites, or gates the message pipeline. Event names, payloads, and context fields are taken from [docs.openclaw.ai/plugins/plugin-hooks](https://docs.openclaw.ai/plugins/plugin-hooks).

The plugin is **inert until an endpoint is explicitly configured** — there is no hardcoded fallback URL.

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
        "endpoint": "https://your-oversee-instance.example.com/v1/traces",
        "agentName": "my-openclaw-instance",
        "enabled": true,
        "readUserData": false,
        "hooks": {
          "allowConversationAccess": true
        }
      }
    }
  }
}
```

| Field          | Type    | Required | Default          | Description                                                                                                                  |
| -------------- | ------- | -------- | ---------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| `endpoint`     | string  | **yes**  | —                | Your Oversee OTLP/HTTP traces endpoint. The plugin is inert until this is set.                                               |
| `agentName`    | string  | no       | `openclaw-agent` | Identifies this gateway in the Oversee dashboard.                                                                            |
| `apiKey`       | string  | no       | —                | Sent as the `X-Oversee-Api-Key` header on every export. Used by multi-tenant Oversee deployments.                            |
| `enabled`      | boolean | no       | `true`           | Set to `false` to load the plugin without emitting telemetry.                                                                |
| `readUserData` | boolean | no       | `false`          | Include `USER.md` and `MEMORY.md` in the startup registration. These files may contain personal information — opt-in only.   |

The same values can be supplied via environment variables for containerized deployments: `OVERSEE_ENDPOINT`, `OVERSEE_AGENT_NAME`, `OVERSEE_API_KEY`, `OVERSEE_ENABLED`. Env vars are checked when `pluginConfig` doesn't supply a value; setting `OVERSEE_ENABLED=false` disables the plugin entirely at gateway start.

### `hooks.allowConversationAccess` is required

This plugin registers handlers for `model_call_started`, `model_call_ended`, and `agent_end` — OpenClaw's conversation-adjacent hooks. Per the plugin SDK, conversation-adjacent hooks require an explicit opt-in via `hooks.allowConversationAccess: true`. Without this flag, those hooks won't fire and you'll only get telemetry for `message_received`, `message_sent`, `before_tool_call`, and `after_tool_call`. The plugin is still safe to use without it — you just lose LLM call and run-completion visibility.

Note that even with `allowConversationAccess: true`, this plugin never reads message content, tool parameter values, prompts, or LLM responses (see the [privacy section](#what-data-is-captured) below). The flag only governs whether the conversation-adjacent hooks fire.

## What data is captured

The plugin sends two distinct categories of data. They have different privacy properties — please read both before deploying.

### 1. Agent identity (sent once at startup)

An `agent_registration` span is emitted at gateway start for each agent. It carries the **full contents** of the agent's identity files (truncated to 32 KB per file):

**Always sent:**

- `oversee.agent.soul` — contents of `SOUL.md` (personality and purpose)
- `oversee.agent.identity` — contents of `IDENTITY.md` (role definition)
- `oversee.agent.operating_manual` — contents of `AGENTS.md` (operating instructions)
- `oversee.agent.id` / `oversee.agent.workspace_path` / `oversee.agent.model` — agent metadata

**Sent only when `readUserData: true`:**

- `oversee.agent.user_context` — contents of `USER.md`
- `oversee.agent.memory` — contents of `MEMORY.md`

`USER.md` and `MEMORY.md` may contain personal data (user preferences, accumulated history, account-specific notes), so they require explicit opt-in. With `readUserData` unset or `false`, these files are not opened.

### 2. Operational telemetry (per agent activity)

Per-span attributes, metadata only:

**`message_received`**
- `oversee.event.type` = `"message_received"`
- `oversee.session.key`, `oversee.message.sender_id`, `oversee.message.thread_id` — opaque identifiers
- `oversee.message.content_length` — character count of the message body (**not** the content itself)
- `oversee.trace.id` / `oversee.trace.span_id` / `oversee.trace.parent_span_id` — OpenClaw's trace context

**`message_sent`**
- `oversee.event.type` = `"message_sent"`
- `oversee.session.key`, `oversee.delivery.success`

**`tool_call`** (combined from `before_tool_call` + `after_tool_call`)
- `oversee.event.type` = `"tool_call"`
- `oversee.tool.name`, `oversee.tool.call_id`
- `oversee.tool.param_keys` — JSON array of parameter **names** (never values)
- `oversee.tool.success`, `oversee.tool.duration_ms`
- `oversee.agent.id`, `oversee.run.id`

**`model_call`** (combined from `model_call_started` + `model_call_ended`)
- `oversee.event.type` = `"model_call"`
- `gen_ai.system` — provider (anthropic, openai, etc.)
- `gen_ai.request.model` — model name
- `oversee.model.call_id`, `oversee.model.duration_ms`, `oversee.model.outcome`
- `oversee.run.id`

**`agent_run_complete`** (from `agent_end`)
- `oversee.event.type` = `"agent_run_complete"`
- `oversee.run.id`, `oversee.run.success`
- `oversee.run.message_provider`, `oversee.run.channel_id`, `oversee.run.job_id`

### What is never captured

- **Message content** — only its character length
- **Tool parameter values** — only the parameter names (keys)
- **LLM prompts, responses, or chat history**
- **Upstream provider request IDs, headers, or bodies** — platform-blocked at the `model_call_*` hooks
- **API keys, credentials, or environment values**

> Message content and tool parameter values are never captured. Agent identity files (SOUL.md, IDENTITY.md, AGENTS.md) are sent on startup to enable accurate descriptions. USER.md and MEMORY.md require opt-in via `readUserData`.

The privacy model is enforced in code, not by policy. If you need different boundaries, fork the plugin and adjust `wireEvents()` and `sendAgentRegistration()` in `index.ts`.

## Reliability

Spans are exported via the OpenTelemetry `BatchSpanProcessor`, which batches in memory and retries on transient failures. If your Oversee endpoint is briefly unreachable, the plugin will buffer and retry; if it is unreachable for an extended period the buffer drops the oldest spans. The plugin never blocks the OpenClaw message pipeline — telemetry is fire-and-forget.

## Links

- Oversee → https://oversee.dev
- OpenTelemetry → https://opentelemetry.io
