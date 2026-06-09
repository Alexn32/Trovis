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

import json
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
    "You are an analyst for Trovis, an agent management system. You have "
    "read access to summaries for every AI agent the user is running. "
    "Answer the user's question using only the telemetry data provided below.\n"
    "- Be direct and specific. Refer to agents by their service_name.\n"
    "- Prefer concrete numbers (\"85% error rate on lead-scorer\") over "
    "vague qualifications.\n"
    "- If the data doesn't support a confident answer, say so plainly.\n"
    "- Keep responses concise — a paragraph or short list, not an essay."
)

SYSTEM_AGENT = (
    "You are an analyst for Trovis, an agent management system. You have "
    "read access to one specific agent's telemetry: its summary stats, "
    "AI-generated description, any identity/configuration files it has "
    "published, and its most recent spans. Answer the user's question "
    "using only this data.\n"
    "- Be direct and specific. Quote span names, tool names, or model "
    "names when relevant.\n"
    "- If the data doesn't support a confident answer, say so plainly.\n"
    "- Keep responses concise — a paragraph or short list, not an essay."
)

# Catalog of inline visual components the Dashboard Ask pill can render. Claude
# returns {answer, visual}; the frontend maps visual.type → a React component.
_VISUAL_CATALOG = """

You can optionally include a visual component with your response. Return your answer as JSON:
{
  "answer": "Your text response here (2-4 sentences, plain prose, no markdown)",
  "visual": null
}

If the question would benefit from a visual, set "visual" to one of these component types:

1. bar_chart — comparing a numeric value across agents (error rates, costs, spans, durations).
{"type": "bar_chart", "props": {"title": "Error Rate by Agent", "value_label": "Error rate", "value_suffix": "%", "data": [{"label": "Content Agent", "value": 33.3, "status": "degraded"}, {"label": "QA Checker", "value": 2.1, "status": "healthy"}]}}
"status" is optional ("degraded" highlights problems, "healthy" is normal). Only include agents relevant to the question.

2. metric_highlight — the answer is a single key number (today's cost, total tasks, fleet size).
{"type": "metric_highlight", "props": {"label": "Fleet cost today", "value": "$14.82", "detail": "9% below 7-day average", "trend": "down"}}
"trend" is "up", "down", or "flat".

3. agent_card — the user asks about ONE specific agent.
{"type": "agent_card", "props": {"name": "Fraud Detector", "status": "healthy", "type": "Python", "owner": "Alex", "description": "Real-time order fraud scoring.", "stats": {"spans": 2103, "error_rate": "0%", "avg_duration": "45ms", "last_seen": "Now", "cost_today": "$1.24"}}}

4. comparison_table — comparing two or more agents side by side.
{"type": "comparison_table", "props": {"title": "Fraud Detector vs Pricing Engine", "agents": [{"name": "Fraud Detector", "status": "healthy", "spans": 2103, "error_rate": "0%", "avg_duration": "45ms", "cost_today": "$1.24"}, {"name": "Pricing Engine", "status": "healthy", "spans": 1847, "error_rate": "0.2%", "avg_duration": "340ms", "cost_today": "$4.21"}]}}

5. cost_projection — "what if" cost questions ("what if I pause X", "what would I save").
{"type": "cost_projection", "props": {"title": "Impact of pausing Ad Optimizer", "current_daily": 14.82, "projected_daily": 10.95, "savings_daily": 3.87, "savings_monthly": 116.10, "note": "Also eliminates wasted spend from failed calls."}}

6. fleet_grid — filtering agents ("which agents are idle", "show me healthy agents", "who has errors").
{"type": "fleet_grid", "props": {"title": "Idle Agents", "agents": [{"name": "Content Agent", "status": "degraded", "last_seen": "20d ago", "spans": 192}]}}

7. timeline — recent events / what happened in a time period.
{"type": "timeline", "props": {"title": "Today's Activity", "events": [{"time": "9:41 AM", "agent": "Fraud Detector", "event": "Flagged 3 orders", "type": "action"}, {"time": "8:30 AM", "agent": "Shipping Tracker", "event": "2 delivery exceptions", "type": "warning"}]}}
"type" is "action", "warning", or "info".

8. workflow_summary — a specific workflow or process.
{"type": "workflow_summary", "props": {"name": "Customer Service", "status": "healthy", "steps": 9, "agents": ["CS Agent", "Shipping Tracker"], "humans": ["Support Manager"], "stats": {"runs_24h": 47, "success_rate": "98%", "avg_cycle": "~8 min", "escalation_rate": "25%"}}}

Rules:
- Only include "visual" when it genuinely helps. Conversational follow-ups, clarifications, opinions, and simple yes/no answers should have "visual": null.
- Use real numbers from the fleet data above. Never fabricate stats.
- The "answer" text MUST be a complete standalone response. The visual is additive. Do not reference the visual ("as shown above") — just state the insight.
- Always return valid JSON with both "answer" and "visual" keys."""

# Tighter variant for the Dashboard "Ask about your fleet" pill: short,
# plain-prose answers with no markdown so they read well in a chat bubble.
# Includes the generative-UI catalog so Claude can attach an inline visual.
SYSTEM_FLEET_CONCISE = (
    "You are an analyst for Trovis, an agent management system. You have "
    "read access to summaries for every AI agent the user is running. "
    "Answer the user's question using only the telemetry data provided below.\n"
    "- Be concise: 2-4 sentences max unless they explicitly ask for detail.\n"
    "- Use specific numbers from the data, and refer to agents by name.\n"
    "- If relevant, suggest one concrete action.\n"
    "- If the data doesn't support a confident answer, say so plainly.\n"
    "- Write in plain prose. Never use markdown headers, bullet points, or lists."
    + _VISUAL_CATALOG
)


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def _parse_ask_response(raw_text: str) -> dict[str, Any]:
    """Parse Claude's fleet reply into {answer, visual}, tolerant of non-JSON.

    The concise/dashboard prompt asks for `{answer, visual}` JSON, but Claude
    sometimes returns plain prose (or fences it). We strip ``` fences and try
    json.loads; on any failure we treat the whole text as the answer with no
    visual. This never raises — a malformed reply degrades to plain text."""
    text = (raw_text or "").strip()
    if text.startswith("```"):
        # Drop the opening fence line (``` or ```json) and any closing fence.
        text = text.split("\n", 1)[-1] if "\n" in text else ""
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
        text = text.strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict) and "answer" in parsed:
            answer = parsed.get("answer")
            visual = parsed.get("visual")
            if isinstance(answer, str) and answer.strip():
                # Only pass through a well-formed visual object.
                if not (isinstance(visual, dict) and visual.get("type")):
                    visual = None
                return {"answer": answer.strip(), "visual": visual}
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    return {"answer": (raw_text or "").strip(), "visual": None}


def ask_about_fleet(
    account_id: int | None,
    messages: list[dict[str, str]],
    concise: bool = False,
) -> dict[str, Any]:
    """Answer a question about the whole fleet. Returns {answer, visual} —
    `visual` is a {type, props} dict (Dashboard pill, concise prompt) or None.
    When `concise` is set, use the short plain-prose + generative-UI prompt."""
    api_key = _require_api_key()
    agents = database.get_agents(account_id=account_id)
    context = _format_fleet_context(agents)
    system = SYSTEM_FLEET_CONCISE if concise else SYSTEM_FLEET
    raw = _call_claude(api_key, system, context, messages)
    return _parse_ask_response(raw)


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
