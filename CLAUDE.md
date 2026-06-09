# Trovis — Agent Management System (AMS)

## What we're building

Trovis is the **system of record for companies running AI agents**. As companies deploy more AI agents across their operations (CrewAI, LangChain, OpenAI Agents SDK, Claude Cowork, Claude Code, custom Python agents, etc.), nobody has centralized visibility into what those agents are doing, whether they're performing well, or how they fit into broader workflows alongside humans.

Trovis solves this by:

1. **Ingesting telemetry** from any agent platform via OpenTelemetry (OTEL) — the industry standard supported by all major agent frameworks.
2. **Using Claude** to generate plain-English descriptions of what each agent does based on its observed behavior.
3. **Showing operators and managers** a unified view of their entire AI workforce — what agents exist, what each one does, how they're performing, and where problems are.
4. **Eventually** reconstructing full hybrid (human + agent) workflows and providing conversational querying over them.

## v1 scope

Keep it simple. The v1 demo should:

- Accept OTEL traces from agents over HTTP
- Store them
- Use Claude to describe each agent in plain English from its observed behavior
- Render it all in a basic dashboard

Goal: a working demo we can show potential customers within a few weeks.

## Tech stack (v1)

- **Backend:** Python, FastAPI
- **Database:** SQLite (migrate to Postgres later)
- **Frontend:** React or Next.js (added later)
- **AI layer:** Claude API — for agent descriptions and insights. Use the latest model (Opus 4.7 / Sonnet 4.6) and prompt caching where it helps.
- **Ingestion:** OpenTelemetry HTTP receiver (OTLP/HTTP)

## Architectural principles

- **Build for the OTEL standard, not any one framework.** Anything that emits OTEL spans should work out of the box. Don't hardcode framework-specific schemas.
- **SQLite now, Postgres-shaped schemas.** Use types and patterns that port cleanly to Postgres (e.g. avoid SQLite-only quirks; prefer ISO timestamps, explicit foreign keys).
- **Agents are derived, not declared.** An "agent" in Trovis is inferred from telemetry — typically a `service.name` or similar resource attribute on incoming spans. Don't require users to pre-register agents.
- **Plain-English descriptions are a first-class product surface.** The Claude-generated description of each agent is what makes Trovis useful on day one. Treat that pipeline as core, not a nice-to-have.
- **Demo-driven.** Every decision should be evaluated against: does this get us to a working, showable demo faster?
