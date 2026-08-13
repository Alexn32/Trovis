# trovis-agents

Connect your AI agents to Trovis in two lines of code. Supports
the OpenAI Agents SDK, Anthropic Claude Managed Agents, and the
Claude Agent SDK. Extras pick which dependencies install.

## Install

```bash
# OpenAI Agents SDK
pip install trovis-agents[openai]

# Anthropic Claude Managed Agents (client.beta.agents API)
pip install trovis-agents[anthropic]

# Claude Agent SDK (query() + ClaudeSDKClient)
pip install trovis-agents[claude-agent-sdk]

# All Python-SDK platforms
pip install trovis-agents[all]
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
from trovis import init

init(api_key="ov_sk_your_key", agent_name="my-agent")

# Your existing code — no changes needed
agent = Agent(name="Support", instructions="You handle customer tickets...")
result = await Runner.run(agent, "Help me with my order")
# Agent appears in your Trovis dashboard automatically
```

## Claude Managed Agents

```python
import anthropic
from trovis import init

init(api_key="ov_sk_your_key", agent_name="my-agent", platform="anthropic")

# Your existing code — no changes needed
client = anthropic.Anthropic()
agent = client.beta.agents.create(
    name="Coding Assistant",
    model={"id": "claude-opus-4-7"},
    system="You are a helpful coding assistant.",
    tools=[{"type": "agent_toolset_20260401"}],
)
session = client.beta.sessions.create(agent=agent.id, environment_id=env_id)

# Send a message and stream events — both flow into Trovis automatically.
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
from trovis import init, monitor

init(api_key="ov_sk_...", agent_name="my-agent", platform="anthropic")
client = monitor(anthropic.Anthropic())
# Only this client emits Trovis spans.
```

Or use `track_session()` as a context manager to scope the
agent-name mapping to a block:

```python
from trovis import track_session

with track_session(session_id=session.id, agent_name="coding-assistant"):
    for event in client.beta.sessions.stream(session.id):
        ...
```

## Claude Agent SDK

For the `claude-agent-sdk` package (`query()` + the Claude Code
engine) — distinct from the Managed Agents API above.

```python
from claude_agent_sdk import query, ClaudeAgentOptions
from trovis import init

# Call init() BEFORE importing/using query so the patch is in place.
init(api_key="ov_sk_your_key", agent_name="my-agent", platform="claude-agent-sdk")

async for message in query(
    prompt="Refactor the auth module",
    options=ClaudeAgentOptions(system_prompt="You are a senior engineer."),
):
    ...  # your existing handling — spans flow into Trovis automatically
```

Each run becomes a set of Trovis spans: an `agent_registration`
(from `options.system_prompt`), `message_received` / `llm_output` /
`tool_call` per message, and an `agent_run_complete` carrying the
run's token usage + cost (from the SDK's `ResultMessage`).

`ClaudeSDKClient`'s streaming (`receive_response`) is instrumented the
same way.

## Hermes Agent — not currently supported

The `trovis.hermes` adapter in this package is **unverified and not a
supported integration**. It was written against an assumed Hermes plugin
API that does not match the real one: its `post_tool_call` handler raises
`TypeError` on every invocation (Hermes passes `function_name` /
`function_args` as keyword arguments; the handler takes positional
`tool_name` / `params`), and it registers a `post_model_call` hook that
does not exist — the real hook is `post_llm_call`, which carries no token
usage. No span has ever reached Trovis through it.

The code is retained for a future verified return, but do not install it
expecting telemetry. Use raw OTLP against `POST /v1/traces` if you need to
instrument a Hermes agent today.

## Connecting agents across processes

When one agent calls another that runs in a **separate process or service**,
carry the trace context across the call so Trovis can draw the
agent-to-agent connection on your dashboard. (Agents that hand off *within*
one process already share a trace automatically.)

Both processes must have called `init()`. On the calling side, attach the
context to your outbound request; on the receiving side, continue it:

```python
import httpx, trovis

# --- Agent A (caller), inside a tool call / run ---
resp = httpx.post(url, headers=trovis.inject(), json=payload)

# --- Agent B (receiver) ---
with trovis.continue_trace(request.headers):
    result = await Runner.run(agent_b, payload)
```

`inject()` writes a W3C `traceparent` header; `continue_trace()` re-attaches
it so Agent B's spans share Agent A's trace and link back to the calling
span. Trovis then surfaces "Agent A → Agent B" automatically. There's also
`trovis.extract(headers)` if you need the raw OpenTelemetry context.

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
| `TROVIS_API_KEY`          | Your Trovis API key. Sent as the `X-Trovis-Api-Key` header.            |
| `TROVIS_ENDPOINT`         | Custom OTLP/HTTP endpoint. Defaults to `https://api.trovisai.com/v1/traces`. |
| `TROVIS_AGENT_NAME`       | **Required** `service.name` for spans, unless you pass `agent_name=` to `init()`. There is no default — `init()` raises if neither is set. |
| `TROVIS_CAPTURE_OUTPUTS`  | Set to `true` (case-insensitive) to enable content capture.              |

Explicit arguments to `init()` always win over environment variables.

## How it works

`init()` does three things:

1. **OpenTelemetry pipeline** — creates a `TracerProvider` with an OTLP/HTTP exporter pointed at the Trovis endpoint, authenticated via the `X-Trovis-Api-Key` header.
2. **OpenAI Agents SDK bridge** — registers the `openai-agents-opentelemetry` processor so all agent runs (LLM calls, tools, handoffs, guardrails) flow into the OTEL pipeline.
3. **Identity capture** — monkey-patches `Agent.__init__` so each unique `(name, instructions)` pair emits one `agent_registration` span. This is what makes Trovis's Claude-generated descriptions accurate from day one.

If the OpenAI Agents SDK or its OTEL adapter isn't installed, `init()` logs a warning and degrades to OTEL-only mode (manual spans still ship).

## Privacy

The default configuration sends **only metadata** to Trovis — agent name, system prompt (as part of registration), LLM model name, tool names, span durations. No user messages, no model responses, no tool inputs/outputs.

Setting `capture_outputs=True` enables the `CaptureProcessor`, which adds:

- `trovis.message.content` on user-prompt spans.
- `trovis.response.content` on model-response spans.
- `trovis.tool.result` on tool-call spans.

Each is truncated to 10 000 characters. Same attribute names and truncation budget as the Trovis OpenClaw plugin.

## License

MIT
