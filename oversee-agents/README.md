# oversee-agents

Connect your AI agents to Oversee in two lines of code. Supports the
**OpenAI Agents SDK** and **Anthropic Claude Managed Agents** today;
extras pick which dependencies to install.

## Install

```bash
# OpenAI Agents SDK
pip install oversee-agents[openai]

# Anthropic Claude Managed Agents
pip install oversee-agents[anthropic]

# Both
pip install oversee-agents[all]
```

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
