# Changelog — trovis-agents

## 0.5.0

**Grok bots (xAI).** `init(platform="xai")` — alias `"grok"`, and picked up
by `platform="auto"` when `xai-sdk` is installed — connects a bot built on
the xAI SDK.

- The xAI SDK traces itself through the global TracerProvider, which
  `init()` already owns, so there is no wrapping: install
  `trovis-agents[xai]`, call `init()` before creating the client, and Grok
  calls land in Trovis with token usage and cost.
- `set_loop_title()` / `mark_handoff()` now reach Grok runs: a span
  processor stamps the queued workloop attrs onto the first xAI span, so a
  Grok bot lands as *named* Work instead of an untitled trace.
- Warns on the two silent-failure modes instead of shipping nothing:
  `xai_sdk.telemetry.Telemetry()` having taken the global provider first
  (every bot named "xai-sdk", protobuf to a JSON endpoint), and
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
