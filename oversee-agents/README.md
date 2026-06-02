# oversee-agents

Connect your AI agents to Oversee in two lines of code. Supports
three agent platforms today — the OpenAI Agents SDK, Anthropic
Claude Agents, and Hermes. Extras pick which dependencies install.

## Install

```bash
# OpenAI Agents SDK
pip install oversee-agents[openai]

# Anthropic Claude Managed Agents (client.beta.agents API)
pip install oversee-agents[anthropic]

# Claude Agent SDK (query() + ClaudeSDKClient)
pip install oversee-agents[claude-agent-sdk]

# Hermes Agent (no extra deps — Hermes provides the runtime)
pip install oversee-agents[hermes]

# All Python-SDK platforms
pip install oversee-agents[all]
```

> **Two different Claude products.** `[anthropic]` instruments the
> **Managed Agents API** (`client.beta.agents.create()` /
> `sessions.stream()`). `[claude-agent-sdk]` instruments the **Claude
> Agent SDK** (`query()` + `ClaudeSDKClient`, the Claude Code engine).
> They share a name but are wholly different entry points — pick the
> one your code actually calls.

## OpenAI Agents SDK

```python
from agents import Agent, Runner
from oversee import init

init(api_key="ov_sk_your_key", agent_name="my-agent")

# Your existing code — no changes needed
agent = Agent(name="Support", instructions="You handle customer tickets...")
result = await Runner.run(agent, "Help me with my order")
# Agent appears in your Oversee dashboard automatically
```

## Claude Managed Agents

```python
import anthropic
from oversee import init

init(api_key="ov_sk_your_key", platform="anthropic")

# Your existing code — no changes needed
client = anthropic.Anthropic()
agent = client.beta.agents.create(
    name="Coding Assistant",
    model={"id": "claude-opus-4-7"},
    system="You are a helpful coding assistant.",
    tools=[{"type": "agent_toolset_20260401"}],
)
session = client.beta.sessions.create(agent=agent.id, environment_id=env_id)

# Send a message and stream events — both flow into Oversee automatically.
client.beta.sessions.events.create(session.id, events=[{
    "type": "user.message",
    "content": [{"type": "text", "text": "Hello"}],
}])
for event in client.beta.sessions.stream(session.id):
    ...
```

`platform="auto"` (the default) detects which SDK(s) are installed and
hooks into both when present — useful if your codebase mixes platforms.

### Advanced: per-client instrumentation

When monkey-patching at module load is undesirable (multi-tenant
hosts, different telemetry per client), use `monitor()` to wrap one
client at a time:

```python
from oversee import init, monitor

init(api_key="ov_sk_...", platform="anthropic")
client = monitor(anthropic.Anthropic())
# Only this client emits Oversee spans.
```

Or use `track_session()` as a context manager to scope the
agent-name mapping to a block:

```python
from oversee import track_session

with track_session(session_id=session.id, agent_name="coding-assistant"):
    for event in client.beta.sessions.stream(session.id):
        ...
```

## Claude Agent SDK

For the `claude-agent-sdk` package (`query()` + the Claude Code
engine) — distinct from the Managed Agents API above.

```python
from claude_agent_sdk import query, ClaudeAgentOptions
from oversee import init

# Call init() BEFORE importing/using query so the patch is in place.
init(api_key="ov_sk_your_key", agent_name="my-agent", platform="claude-agent-sdk")

async for message in query(
    prompt="Refactor the auth module",
    options=ClaudeAgentOptions(system_prompt="You are a senior engineer."),
):
    ...  # your existing handling — spans flow into Oversee automatically
```

Each run becomes a set of Oversee spans: an `agent_registration`
(from `options.system_prompt`), `message_received` / `llm_output` /
`tool_call` per message, and an `agent_run_complete` carrying the
run's token usage + cost (from the SDK's `ResultMessage`).

`ClaudeSDKClient`'s streaming (`receive_response`) is instrumented the
same way.

## Hermes Agent

Hermes discovers plugins via Python entry points, so installing
this package is enough — no separate plugin scaffold to copy.

```bash
pip install oversee-agents[hermes]
hermes plugins enable oversee
```

If you'd rather drop the plugin in by hand:

```bash
cp -r $(python -c "import oversee.hermes_plugin, os; \
    print(os.path.dirname(oversee.hermes_plugin.__file__))") \
    ~/.hermes/plugins/oversee
```

Configure via environment variables (Hermes will prompt for these
on `plugins enable` thanks to `plugin.yaml`'s `requires_env`):

```bash
export OVERSEE_API_KEY="ov_sk_your_key"
export OVERSEE_ENDPOINT="https://your-oversee/v1/traces"  # optional
```

Or from chat after the first start:

```
/oversee connect https://your-oversee/v1/traces
/oversee apikey ov_sk_your_key
/oversee capture on
/oversee status
```

### What gets captured on Hermes

- **Agent identity** — `~/.hermes/SOUL.md`, plus `memory.md` when
  `capture_outputs` is on. Sent once on gateway start.
- **Every `post_tool_call` hook** — tool name, parameter keys (not
  values, unless capture is on), and the tool's result (capture-only).
- **`/oversee status`** in chat to verify telemetry is flowing.

## Connecting agents across processes

When one agent calls another that runs in a **separate process or service**,
carry the trace context across the call so Oversee can draw the
agent-to-agent connection on your dashboard. (Agents that hand off *within*
one process already share a trace automatically.)

Both processes must have called `init()`. On the calling side, attach the
context to your outbound request; on the receiving side, continue it:

```python
import httpx, oversee

# --- Agent A (caller), inside a tool call / run ---
resp = httpx.post(url, headers=oversee.inject(), json=payload)

# --- Agent B (receiver) ---
with oversee.continue_trace(request.headers):
    result = await Runner.run(agent_b, payload)
```

`inject()` writes a W3C `traceparent` header; `continue_trace()` re-attaches
it so Agent B's spans share Agent A's trace and link back to the calling
span. Oversee then surfaces "Agent A → Agent B" automatically. There's also
`oversee.extract(headers)` if you need the raw OpenTelemetry context.

## What gets captured

- **Agent identity** (name, instructions/system prompt) — sent once when each unique agent is first constructed.
- **Every LLM call** (model, duration, token usage).
- **Every tool call** (name, duration, success/failure).
- **Agent handoffs.**
- **Guardrail checks.**
- **Run completion.**

By default, **message content is NOT captured** — only metadata. Enable with `init(capture_outputs=True)` for full visibility.

## Environment variables

| Variable                   | Purpose                                                                 |
| -------------------------- | ----------------------------------------------------------------------- |
| `OVERSEE_API_KEY`          | Your Oversee API key. Sent as the `X-Oversee-Api-Key` header.            |
| `OVERSEE_ENDPOINT`         | Custom OTLP/HTTP endpoint. Defaults to the Oversee cloud.                |
| `OVERSEE_AGENT_NAME`       | Default `service.name` for spans. Defaults to `openai-agent`.            |
| `OVERSEE_CAPTURE_OUTPUTS`  | Set to `true` (case-insensitive) to enable content capture.              |

Explicit arguments to `init()` always win over environment variables.

## How it works

`init()` does three things:

1. **OpenTelemetry pipeline** — creates a `TracerProvider` with an OTLP/HTTP exporter pointed at the Oversee endpoint, authenticated via the `X-Oversee-Api-Key` header.
2. **OpenAI Agents SDK bridge** — registers the `openai-agents-opentelemetry` processor so all agent runs (LLM calls, tools, handoffs, guardrails) flow into the OTEL pipeline.
3. **Identity capture** — monkey-patches `Agent.__init__` so each unique `(name, instructions)` pair emits one `agent_registration` span. This is what makes Oversee's Claude-generated descriptions accurate from day one.

If the OpenAI Agents SDK or its OTEL adapter isn't installed, `init()` logs a warning and degrades to OTEL-only mode (manual spans still ship).

## Privacy

The default configuration sends **only metadata** to Oversee — agent name, system prompt (as part of registration), LLM model name, tool names, span durations. No user messages, no model responses, no tool inputs/outputs.

Setting `capture_outputs=True` enables the `CaptureProcessor`, which adds:

- `oversee.message.content` on user-prompt spans.
- `oversee.response.content` on model-response spans.
- `oversee.tool.result` on tool-call spans.

Each is truncated to 10 000 characters. Same attribute names and truncation budget as the Oversee OpenClaw plugin.

## License

MIT
