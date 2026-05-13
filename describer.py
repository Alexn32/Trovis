"""Claude-powered description generator for agents.

Given a service_name, pull recent telemetry and ask Claude to write a plain-
English description of what the agent does. This is the feature that makes
Oversee useful on day one: a non-technical operator can read the description
and immediately understand each agent's job.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

import anthropic

import database

MODEL = "claude-sonnet-4-20250514"
MAX_TOKENS = 1024

SYSTEM_PROMPT = (
    "You are an AI analyst for Oversee, an agent management system. "
    "Given telemetry data from an AI agent, write a clear, concise description "
    "of what this agent does in plain English. Include: what its job appears "
    "to be, what tools or APIs it uses, how often it runs, and any notable "
    "patterns. Write for a non-technical operations manager. Keep it to one "
    "paragraph, 3-5 sentences max. Do not hedge or use phrases like 'it "
    "appears to' — be direct and confident."
)

REGISTRATION_SYSTEM_PROMPT = (
    "You are an AI analyst for Oversee, an agent management system. You have "
    "been given the agent's own configuration files that define its purpose, "
    "personality, and operating rules. Use these as the primary source of "
    "truth for describing what this agent does. Supplement with telemetry "
    "data for operational details like frequency, performance, and error "
    "rates. Write a clear, confident description for a non-technical "
    "operations manager. One paragraph, 3-5 sentences."
)


class APIKeyMissingError(RuntimeError):
    """ANTHROPIC_API_KEY is not set in the environment."""


class AgentNotFoundError(LookupError):
    """No spans have been ingested for this service_name."""


# ---------------------------------------------------------------------------
# Attribute mining
# ---------------------------------------------------------------------------
#
# OTEL semantic conventions for GenAI are still settling, so different SDKs
# emit slightly different keys. We use generous substring matches rather
# than an exact allowlist so we pick up tool/model signals across CrewAI,
# LangChain, OpenAI Agents SDK, Claude Cowork, etc.


def _mine_signals(spans: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """Pull distinct tool names and model names out of span attributes."""
    tools: set[str] = set()
    models: set[str] = set()

    for s in spans:
        attrs = s.get("attributes") or {}
        for key, value in attrs.items():
            if not isinstance(value, (str, int, float)):
                continue
            sval = str(value)
            klow = key.lower()
            if "model" in klow:
                models.add(sval)
            elif "tool" in klow or "function.name" in klow:
                tools.add(sval)

    return sorted(tools), sorted(models)


def _format_outputs_block(outputs: list[dict[str, Any]]) -> str:
    """Render captured outputs (when the operator opted in via the plugin's
    captureOutputs flag) as a prompt section. Returns "" when empty so
    callers can drop the section entirely. Each content snippet is
    truncated to 500 chars so a chatty agent can't blow up the prompt."""
    if not outputs:
        return ""
    lines = ["Recent outputs from this agent (most recent first):"]
    for o in outputs:
        snippet = (o.get("content") or "").strip().replace("\n", " ")
        if len(snippet) > 500:
            snippet = snippet[:500] + "[...]"
        lines.append(
            f"- [{o.get('content_type')}] {o.get('operation')} "
            f"@ {o.get('timestamp')}: {snippet}"
        )
    return "\n".join(lines) + "\n\n"


def _build_prompt(
    summary: dict[str, Any],
    spans: list[dict[str, Any]],
    outputs: list[dict[str, Any]] | None = None,
) -> str:
    """Format the telemetry snapshot into a prompt Claude can reason over."""
    import json

    tools, models = _mine_signals(spans)

    top_ops = ", ".join(summary.get("top_operations") or []) or "(none)"

    # Most recent 20 spans, just the bits that describe behavior.
    recent_sample = [
        {
            "span_name": s["span_name"],
            "duration_ms": (s["end_time_unix"] - s["start_time_unix"]) / 1_000_000.0,
            "status_code": s["status_code"],
            "attributes": s["attributes"],
        }
        for s in spans[:20]
    ]

    return (
        f"Agent service.name: {summary['service_name']}\n"
        f"Total spans observed: {summary['span_count']}\n"
        f"Errors observed: {summary['error_count']}\n"
        f"Average span duration: {summary['avg_duration_ms']:.1f} ms\n"
        f"First seen: {summary.get('first_seen')}\n"
        f"Last seen: {summary.get('last_seen')}\n"
        f"\n"
        f"Top operations (by frequency): {top_ops}\n"
        f"Detected tools: {', '.join(tools) if tools else '(none detected)'}\n"
        f"Detected models: {', '.join(models) if models else '(none detected)'}\n"
        f"\n"
        f"{_format_outputs_block(outputs or [])}"
        f"Recent span sample (up to 20 most recent):\n"
        f"{json.dumps(recent_sample, indent=2, default=str)}\n"
        f"\n"
        f"Write the description now."
    )


def _build_registration_prompt(
    summary: dict[str, Any],
    registration: dict[str, Any],
    outputs: list[dict[str, Any]] | None = None,
) -> str:
    """Format the agent's own identity files plus telemetry into a prompt.

    The identity files are the primary source of truth; telemetry only
    contributes operational stats (cadence, errors, latency). USER.md and
    MEMORY.md are stored in the registration but deliberately not surfaced
    here — they're user-private context the operator doesn't need.
    """
    top_ops = ", ".join(summary.get("top_operations") or []) or "(none)"
    return (
        f"Agent: {summary['service_name']}\n"
        f"Agent ID: {registration.get('agent_id') or 'main'}\n"
        f"Model: {registration.get('model') or 'unknown'}\n"
        f"\n"
        f"SOUL.md (personality and purpose):\n"
        f"{registration.get('soul') or '(empty)'}\n"
        f"\n"
        f"IDENTITY.md (role definition):\n"
        f"{registration.get('identity') or '(empty)'}\n"
        f"\n"
        f"AGENTS.md (operating manual):\n"
        f"{registration.get('operating_manual') or '(empty)'}\n"
        f"\n"
        f"Telemetry summary:\n"
        f"- Total spans observed: {summary['span_count']}\n"
        f"- Errors observed: {summary['error_count']}\n"
        f"- Average span duration: {summary['avg_duration_ms']:.1f} ms\n"
        f"- Top operations: {top_ops}\n"
        f"\n"
        f"{_format_outputs_block(outputs or [])}"
        f"Based on the configuration files above and the telemetry data, "
        f"describe what this agent does."
    )


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def describe_agent(
    service_name: str,
    account_id: int | None = None,
    agent_id: str | None = None,
) -> dict[str, Any]:
    """Generate a plain-English description of an agent.

    If the agent has sent its identity files via an agent_registration span,
    those are used as the primary source — far more accurate than inferring
    from telemetry alone. Otherwise we fall back to inferring purely from
    observed span behavior.

    `account_id` scopes every database read so a user can only describe
    agents they own. Pass None for legacy / unauthenticated paths.

    `agent_id` optionally scopes the prompt's telemetry sample to one
    sub-agent within a multi-agent instance. The saved description is
    still indexed per `service_name` regardless of the scope.

    Raises:
        AgentNotFoundError: no spans exist for service_name.
        APIKeyMissingError: ANTHROPIC_API_KEY is not configured.
    """
    summary = database.get_agent_summary(
        service_name, account_id=account_id, agent_id=agent_id
    )
    if summary is None:
        raise AgentNotFoundError(service_name)

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise APIKeyMissingError(
            "ANTHROPIC_API_KEY is not set. Export it before generating descriptions."
        )

    spans = database.get_agent_spans(
        service_name, limit=100, account_id=account_id, agent_id=agent_id
    )
    registration = database.get_latest_registration(
        service_name, account_id=account_id, agent_id=agent_id
    )
    # Captured outputs (gated by the plugin's captureOutputs flag at
    # emit time). Empty list when nothing's been captured. Concrete
    # examples of what the agent says/returns are by far the most
    # useful signal for Claude — when present they should dominate
    # telemetry-only descriptions.
    outputs = database.get_agent_outputs(
        service_name, account_id=account_id, limit=5, agent_id=agent_id
    )

    # The registration must carry meaningful identity content — an empty
    # row would be worse than telemetry-only because Claude would invent
    # filler instead of describing real behavior.
    has_registration_content = bool(
        registration
        and (
            registration.get("soul")
            or registration.get("identity")
            or registration.get("operating_manual")
        )
    )

    if has_registration_content:
        system_prompt = REGISTRATION_SYSTEM_PROMPT
        user_prompt = _build_registration_prompt(summary, registration, outputs)
        source = "registration"
    else:
        system_prompt = SYSTEM_PROMPT
        user_prompt = _build_prompt(summary, spans, outputs)
        source = "telemetry_only"

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    description = "".join(
        block.text for block in response.content if getattr(block, "type", None) == "text"
    ).strip()

    return {
        "service_name": service_name,
        "description": description,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "span_count_analyzed": len(spans),
        "source": source,
    }
