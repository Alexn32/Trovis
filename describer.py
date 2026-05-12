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


def _build_prompt(summary: dict[str, Any], spans: list[dict[str, Any]]) -> str:
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
        f"Recent span sample (up to 20 most recent):\n"
        f"{json.dumps(recent_sample, indent=2, default=str)}\n"
        f"\n"
        f"Write the description now."
    )


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def describe_agent(service_name: str) -> dict[str, Any]:
    """Generate a plain-English description of an agent from its telemetry.

    Raises:
        AgentNotFoundError: no spans exist for service_name.
        APIKeyMissingError: ANTHROPIC_API_KEY is not configured.
    """
    summary = database.get_agent_summary(service_name)
    if summary is None:
        raise AgentNotFoundError(service_name)

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise APIKeyMissingError(
            "ANTHROPIC_API_KEY is not set. Export it before generating descriptions."
        )

    spans = database.get_agent_spans(service_name, limit=100)
    prompt = _build_prompt(summary, spans)

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )

    # Concatenate all text blocks in the response (usually just one).
    description = "".join(
        block.text for block in response.content if getattr(block, "type", None) == "text"
    ).strip()

    return {
        "service_name": service_name,
        "description": description,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "span_count_analyzed": len(spans),
    }
