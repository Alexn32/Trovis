# Changelog — trovis-agents

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
