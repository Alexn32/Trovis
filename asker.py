"""Claude-powered Q&A over agent telemetry.

Two entry points:
  - ask_about_fleet(account_id, messages) — context = every agent's summary
  - ask_about_agent(service_name, account_id, messages) — context = one
    agent's full payload (summary + description + registration files + last
    ~30 spans)

Stateless on the backend. The caller passes the full chat thread on every
request so we can support multi-turn conversations without server-side
session state.
"""

from __future__ import annotations

import os
from typing import Any

import anthropic

import database

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 1024

# How many recent spans to include in the per-agent context. 30 covers the
# most common "why did this fail" question without bloating prompts.
RECENT_SPAN_LIMIT = 30


class AskApiKeyMissingError(RuntimeError):
    """ANTHROPIC_API_KEY is not set in the environment."""


class AgentNotFoundError(LookupError):
    """No spans have been ingested for this service_name (under this account)."""


SYSTEM_FLEET = (
    "You are an analyst for Oversee, an agent management system. You have "
    "read access to summaries for every AI agent the user is running. "
    "Answer the user's question using only the telemetry data provided below.\n"
    "- Be direct and specific. Refer to agents by their service_name.\n"
    "- Prefer concrete numbers (\"85% error rate on lead-scorer\") over "
    "vague qualifications.\n"
    "- If the data doesn't support a confident answer, say so plainly.\n"
    "- Keep responses concise — a paragraph or short list, not an essay."
)

SYSTEM_AGENT = (
    "You are an analyst for Oversee, an agent management system. You have "
    "read access to one specific agent's telemetry: its summary stats, "
    "AI-generated description, any identity/configuration files it has "
    "published, and its most recent spans. Answer the user's question "
    "using only this data.\n"
    "- Be direct and specific. Quote span names, tool names, or model "
    "names when relevant.\n"
    "- If the data doesn't support a confident answer, say so plainly.\n"
    "- Keep responses concise — a paragraph or short list, not an essay."
)


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def ask_about_fleet(
    account_id: int | None, messages: list[dict[str, str]]
) -> str:
    """Answer a question about the whole fleet."""
    api_key = _require_api_key()
    agents = database.get_agents(account_id=account_id)
    context = _format_fleet_context(agents)
    return _call_claude(api_key, SYSTEM_FLEET, context, messages)


def ask_about_agent(
    service_name: str,
    account_id: int | None,
    messages: list[dict[str, str]],
    agent_id: str | None = None,
) -> str:
    """Answer a question scoped to one instance, or one sub-agent when
    `agent_id` is set."""
    api_key = _require_api_key()
    summary = database.get_agent_summary(
        service_name, account_id=account_id, agent_id=agent_id
    )
    if summary is None:
        raise AgentNotFoundError(service_name)

    spans = database.get_agent_spans(
        service_name,
        limit=RECENT_SPAN_LIMIT,
        account_id=account_id,
        agent_id=agent_id,
    )
    registration = database.get_latest_registration(
        service_name, account_id=account_id, agent_id=agent_id
    )
    context = _format_agent_context(summary, spans, registration)
    return _call_claude(api_key, SYSTEM_AGENT, context, messages)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _require_api_key() -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise AskApiKeyMissingError(
            "ANTHROPIC_API_KEY is not set. Export it before using /ask."
        )
    return api_key


def _format_fleet_context(agents: list[dict[str, Any]]) -> str:
    if not agents:
        return "The user's fleet is currently empty — no agents have reported telemetry."

    lines: list[str] = [f"Total instances: {len(agents)}", ""]
    for a in agents:
        total_spans = a.get("total_spans") or 0
        total_errors = a.get("total_errors") or 0
        rate = (total_errors / total_spans * 100) if total_spans else 0.0
        lines.append(f"## {a['service_name']}")
        lines.append(
            f"- spans={total_spans} errors={total_errors} "
            f"error_rate={rate:.1f}% avg_duration_ms={a['avg_duration_ms']:.0f}"
        )
        lines.append(
            f"- first_seen={a.get('first_seen')} last_seen={a.get('last_seen')}"
        )
        top_ops = a.get("top_operations") or []
        if top_ops:
            lines.append(f"- top_operations: {', '.join(top_ops)}")
        sub_agents = a.get("agents") or []
        # Only spell out the per-agent breakdown when there's actually a
        # multi-agent layout — the single-'main' case adds noise without
        # any new info.
        non_main = [ag for ag in sub_agents if ag.get("agent_id") != "main"]
        if non_main or len(sub_agents) > 1:
            lines.append(
                f"- agents ({len(sub_agents)}): "
                + ", ".join(
                    f"{ag['agent_id']} (spans={ag['span_count']}, "
                    f"errors={ag['error_count']})"
                    for ag in sub_agents
                )
            )
        if a.get("description"):
            lines.append(f"- description: {a['description']}")
        lines.append("")
    return "\n".join(lines)


def _format_agent_context(
    summary: dict[str, Any],
    spans: list[dict[str, Any]],
    registration: dict[str, Any] | None,
) -> str:
    rate = (
        (summary["error_count"] / summary["span_count"] * 100)
        if summary["span_count"]
        else 0.0
    )
    top_ops = summary.get("top_operations") or []

    header = (
        f"# Agent: {summary['service_name']} "
        f"(sub-agent: {summary['agent_id']})"
        if summary.get("agent_id")
        else f"# Agent: {summary['service_name']}"
    )
    lines: list[str] = [
        header,
        "",
        "## Summary",
        f"- spans={summary['span_count']} errors={summary['error_count']} "
        f"error_rate={rate:.1f}% avg_duration_ms={summary['avg_duration_ms']:.0f}",
        f"- first_seen={summary.get('first_seen')}",
        f"- last_seen={summary.get('last_seen')}",
    ]
    if top_ops:
        lines.append(f"- top_operations: {', '.join(top_ops)}")
    if summary.get("description"):
        lines.extend(["", "## Description", summary["description"]])

    if registration:
        lines.extend(["", "## Identity files"])
        if registration.get("soul"):
            lines.extend(["", "### SOUL.md", registration["soul"]])
        if registration.get("identity"):
            lines.extend(["", "### IDENTITY.md", registration["identity"]])
        if registration.get("operating_manual"):
            lines.extend(
                ["", "### AGENTS.md", registration["operating_manual"]]
            )
        # USER.md and MEMORY.md are deliberately NOT included — they're the
        # opt-in fields on the plugin side. If you want them in the
        # context, expose a flag from the request and gate on that.

    if spans:
        lines.extend(["", f"## Recent spans (last {len(spans)}, newest first)"])
        for s in spans:
            duration_ms = (
                (s["end_time_unix"] - s["start_time_unix"]) / 1_000_000
            )
            status = "OK" if s["status_code"] != 2 else "ERROR"
            line = f"- {s['span_name']} | {duration_ms:.0f}ms | {status}"
            # Compact attribute dump: keys + short values, capped per-line
            # so a single fat attribute doesn't blow up the prompt.
            attrs = s.get("attributes") or {}
            compact: list[str] = []
            for k, v in attrs.items():
                sval = str(v)
                if len(sval) > 80:
                    sval = sval[:77] + "..."
                compact.append(f"{k}={sval}")
            if compact:
                line += " | " + " ".join(compact[:6])
            lines.append(line)

    return "\n".join(lines)


def _call_claude(
    api_key: str,
    system_prompt: str,
    context: str,
    messages: list[dict[str, str]],
) -> str:
    # Normalize the message list. We accept anything shaped like
    # {role, content}; drop empties and non-user/assistant roles. The last
    # turn must be from the user — that's how Claude's API expects it.
    cleaned: list[dict[str, str]] = []
    for m in messages or []:
        role = (m or {}).get("role")
        content = ((m or {}).get("content") or "").strip()
        if role in ("user", "assistant") and content:
            cleaned.append({"role": role, "content": content})
    if not cleaned:
        raise ValueError("messages must contain at least one user message")
    if cleaned[-1]["role"] != "user":
        raise ValueError("the last message must be from the user")

    full_system = system_prompt + "\n\n---\n\nDATA:\n\n" + context

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=full_system,
        messages=cleaned,
    )
    return "".join(
        block.text
        for block in response.content
        if getattr(block, "type", None) == "text"
    ).strip()
