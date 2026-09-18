# Changelog — trovis-agents

## 0.5.2

**Known execution structure is carried as OTEL parentage.** The two
adapters that wrap a run's event stream — Claude Managed Agents
(`sessions.stream()`) and the Claude Agent SDK (`query()` /
`receive_response()`) — know that every event they yield belongs to the one
call the user opened. Until now each event span was its own root in its own
trace and that containment was dropped. Each stream now opens one
`agent_run` span and starts every event span in its context, so the
exported spans share the run's trace id and name the run span as parent;
the run span ends when the stream ends (or fails, which marks it).

- Nothing deeper is encoded: neither SDK says which model turn issued which
  tool use, so `message_received` / `llm_output` / `tool_call` /
  `agent_run_complete` are siblings under the run. Nothing is parented by
  timing or name.
- Work correlation is unchanged: event spans carry exactly the
  `trovis.run.id` / `trovis.loop.external_id` they carried before, and the
  run span carries the same two (never the one-shot title / handoff
  signals), so it lands in the same loop.
- The run span is never made *current* — a generator that attached it to
  the caller's context would leak it across every `yield` — so user code
  that reads the active span sees exactly what it saw before.
- OpenAI Agents SDK: unchanged. The official OpenTelemetry adapter already
  emits the SDK's own hierarchy; `CaptureProcessor` still adds content spans
  beside it. Grok (xAI SDK): unchanged — the SDK owns its spans.

## 0.5.1

`init(agent_role="…")` — one line on what an agent is FOR, registered as its
identity. `describe_agent` reads a registration before it reads behavior, so
this is the difference between "drafts the founder's updates and chases
follow-ups" and "an agent that runs smoke tests", which is what an agent gets
called when its first telemetry happens to be a connection test.

Every adapter already registered identity from the framework — an Agent's
instructions, a SOUL.md, a system prompt. The doors with no such file to read
(xAI, and anyone emitting plain OTEL) had no way to say it at all. Also reads
`TROVIS_AGENT_ROLE`; best-effort, and never delays the agent's first work.

## 0.5.0

**Grok (xAI SDK).** `init(platform="xai")` — alias `"grok"`, and picked up
by `platform="auto"` when `xai-sdk` is installed — connects a bot built on
the xAI SDK.

- The xAI SDK traces itself through the global TracerProvider, which
  `init()` already owns, so there is no wrapping: install
  `trovis-agents[xai]`, call `init()` before creating the client, and Grok
  calls land in Trovis with token usage and cost.
- `set_loop_title()` / `mark_handoff()` now reach Grok runs: a span
  processor stamps the queued workloop attrs onto the first xAI span, so a
  Grok run lands as *named* Work instead of an untitled trace.
- Warns on the two silent-failure modes instead of shipping nothing:
  `xai_sdk.telemetry.Telemetry()` having taken the global provider first
  (every agent named "xai-sdk", protobuf to a JSON endpoint), and
  `XAI_SDK_DISABLE_TRACING` being set.

## 0.4.6

Named Work titles on the creating span (`trovis.loop.title` → ingest
`title_source=provided`). Platforms emit a short human title when one is
available, plus `trovis.run.id` / `trovis.loop.external_id` and handoff
attrs when the SDK surfaces them.

- **OpenAI Agents SDK:** `CaptureProcessor` stamps `trovis.run.id` from the
  SDK trace id. Title comes from `set_loop_title()`, a non-generic
  workflow/trace name (`RunConfig.workflow_name` / `trace("…")`), or — when
  `capture_outputs` is on — the first user task. `HandoffSpanData` emits
  `trovis.handoff.direction=to_agent` + target.
- **Anthropic Managed Agents / Claude Agent SDK:** first user message (or
  `query(prompt=…)`) becomes the title when `capture_outputs` is on.
  Session id is stamped as `trovis.run.id` and `trovis.loop.external_id`.
- **Helpers:** `trovis.set_loop_title(title)` and `trovis.mark_handoff(...)`
  queue attrs for the next span. Use `set_loop_title` to name Work without
  turning on content capture.
- Privacy unchanged: prompt-derived titles follow `capture_outputs`. Generic
  SDK defaults (`"Agent workflow"`) are never emitted.

## 0.4.5

Two silent-failure modes removed. **Both are breaking for installs that relied
on a default.** That is the point: each default was silently wrong.

- **`agent_name` is now required.** It resolves from `agent_name=` or
  `TROVIS_AGENT_NAME`; with neither, `init()` raises `ValueError` with the fix
  in the message. It previously fell back to `"openai-agent"`, which became the
  span's `service.name` — so every unconfigured install in an org collapsed
  into one indistinguishable agent, with no signal that it had happened.
  Nothing about a Python process is a reliable per-agent name, so there is
  nothing safe to derive; refusing to start beats guessing wrong.
- **`DEFAULT_ENDPOINT` is now `https://api.trovisai.com/v1/traces`**, the
  canonical public ingest host, instead of the raw
  `web-production-e6bc4.up.railway.app` platform hostname. Unconfigured
  installs were hardcoding an infra URL whose lifetime we don't control.
  `trovis/hermes.py` carried a second copy of that hostname and now shares the
  single constant.

Upgrading: pass `agent_name=` (or set `TROVIS_AGENT_NAME`) if you weren't
already, and drop any `endpoint=` override that pointed at the Railway host.

## 0.4.4

Fail loud on connectivity and export problems (#112).

- `init()` probes the ingest endpoint before printing a verdict, so a dead or
  misconfigured endpoint reports "NOT connected" with the reason instead of
  claiming success while telemetry vanished.
- The exporter prints its first export failure prominently and logs subsequent
  ones, so a dropped span batch is never fully silent.

## 0.4.3 and earlier

Not separately changelogged. See `git log -- trovis-agents/`.
