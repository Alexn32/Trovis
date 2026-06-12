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
import re
from typing import Any

import anthropic

import database

# The Ask assistant is a primary product surface (global ⌘K pill) — use the
# most capable model. Setup walkthroughs need more room than quick answers.
MODEL = "claude-opus-4-7"
MAX_TOKENS = 1500

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
# What the assistant knows about setting Trovis up — kept factually in sync
# with the Add-Agent wizard and the trovis-agents SDK. This is what lets the
# pill walk a user through connecting an agent, not just read telemetry.
_SETUP_KNOWLEDGE = (
    "\n\nYou are also the Trovis setup expert. Facts you know:\n"
    "- Connecting an agent: the Add Agent button (top right) has guided steps "
    "for OpenClaw, OpenAI Agents SDK, Claude Agents (Claude Agent SDK or "
    "Anthropic Managed Agents), and Hermes Agent.\n"
    "- Python platforms use the trovis-agents pip package with an extra per "
    "platform: pip install trovis-agents[openai], [anthropic], "
    "[claude-agent-sdk], or [hermes]. Then two lines BEFORE the agent "
    "framework is imported: from trovis import init; "
    "init(api_key=\"ov_sk_...\", agent_name=\"my-agent\"). platform=\"auto\" "
    "detects the installed framework.\n"
    "- Config env vars: TROVIS_API_KEY, TROVIS_ENDPOINT, TROVIS_AGENT_NAME, "
    "TROVIS_CAPTURE_OUTPUTS (legacy OVERSEE_* names still work).\n"
    "- The org's API key (ov_sk_...) lives in Settings; it can be revealed "
    "again after re-entering the password.\n"
    "- OpenClaw: install the trovis plugin, then in chat /trovis connect "
    "<endpoint>, /trovis apikey <key>, /trovis capture on, /trovis status — "
    "or via CLI: openclaw config set plugins.entries.trovis.config.endpoint/"
    "apiKey.\n"
    "- Hermes: pip install trovis-agents[hermes], then hermes plugins enable "
    "trovis.\n"
    "- Agents appear on the dashboard automatically within seconds of their "
    "first telemetry — no pre-registration. Message/output content is only "
    "captured when the user opts in (capture on / TROVIS_CAPTURE_OUTPUTS=true).\n"
    "- Costs are tracked automatically from token usage across all major "
    "model providers; Claude Agent SDK runs use the SDK's own reported cost "
    "(exact). 'Today' is the UTC calendar day. Monthly budget and per-agent "
    "caps are set on the Cost page (open via the dashboard Cost card).\n"
    "- Troubleshooting: agent not appearing → check the API key, that init() "
    "runs before the framework import, and that the agent actually ran. "
    "Cost showing $0 → the agent's telemetry must include token usage "
    "(gen_ai.usage.* attributes); the platform integrations above emit this "
    "automatically.\n"
)

SYSTEM_FLEET_CONCISE = (
    "You are the Trovis assistant — an expert analyst for the user's AI agent "
    "fleet and their guide to the product. You have read access to telemetry "
    "summaries for every agent the user runs (provided below), and you know "
    "how Trovis works end to end.\n"
    "- Ground every claim about the user's agents in the telemetry data. Use "
    "specific numbers and refer to agents by name. Never invent data.\n"
    "- Default to 2-4 sentences. For how-do-I/setup questions, give complete "
    "step-by-step instructions instead — exact commands and code on their own "
    "lines (use \\n line breaks; the UI renders plain text, so no markdown "
    "headers, bullets, or code fences).\n"
    "- If relevant, suggest one concrete action.\n"
    "- If the data doesn't support a confident answer, say so plainly.\n"
    "- Be genuinely smart: connect cause and effect across agents, costs, and "
    "errors; anticipate the user's next question when it's obvious."
    + _SETUP_KNOWLEDGE
    + _VISUAL_CATALOG
)

# Exact commands the connect guide may emit, sourced from the Add-Agent wizard
# pages (AddAgent.jsx). _SETUP_KNOWLEDGE covers the what; this covers the
# copy-paste how. Keep both in sync with the wizard when instructions change.
_CONNECT_SETUP_EXTRAS = (
    "\n\nExact setup recipes (the only commands you may give):\n"
    "- OpenAI Agents SDK: pip install trovis-agents[openai] then, before the "
    "agent framework is imported:\n"
    "  from trovis import init\n"
    "  init(api_key=\"TROVIS_API_KEY\", endpoint=\"TROVIS_ENDPOINT\", "
    "agent_name=\"<their-agent-name>\")\n"
    "- Claude Agent SDK (query()/ClaudeSDKClient): pip install "
    "trovis-agents[claude-agent-sdk]; same init() lines with "
    "platform=\"claude-agent-sdk\", and init() MUST run before importing "
    "query/ClaudeSDKClient.\n"
    "- Anthropic Managed Agents (client.beta.agents...): pip install "
    "trovis-agents[anthropic]; same init() lines with platform=\"anthropic\".\n"
    "- Claude Code (the CLI): add to the \"env\" object in "
    "~/.claude/settings.json, then restart Claude Code:\n"
    "  \"CLAUDE_CODE_ENABLE_TELEMETRY\": \"1\"\n"
    "  \"OTEL_TRACES_EXPORTER\": \"otlp\"\n"
    "  \"OTEL_EXPORTER_OTLP_PROTOCOL\": \"http/json\"\n"
    "  \"OTEL_EXPORTER_OTLP_ENDPOINT\": \"TROVIS_ENDPOINT\"\n"
    "  \"OTEL_EXPORTER_OTLP_HEADERS\": \"X-Trovis-Api-Key=TROVIS_API_KEY\"\n"
    "- OpenClaw: openclaw plugins install clawhub:@trovis/openclaw-plugin, "
    "then in any chat: /trovis connect TROVIS_ENDPOINT then /trovis apikey "
    "TROVIS_API_KEY then optionally /trovis capture on, /trovis status. "
    "Terminal alternative: openclaw config set "
    "plugins.entries.trovis.config.endpoint TROVIS_ENDPOINT and ...config."
    "apiKey TROVIS_API_KEY.\n"
    "- Hermes: pip install trovis-agents[hermes] then hermes plugins enable "
    "trovis (it prompts for the key/endpoint; or export TROVIS_API_KEY/"
    "TROVIS_ENDPOINT/TROVIS_AGENT_NAME first).\n"
    "- Anything else that can emit OpenTelemetry (custom Python, LangChain, "
    "CrewAI, Node, ...): if it's Python, pip install trovis-agents and the "
    "same two init() lines (platform=\"auto\"); otherwise point its OTLP/HTTP "
    "exporter at Trovis with env vars:\n"
    "  OTEL_SERVICE_NAME=<their-agent-name>\n"
    "  OTEL_EXPORTER_OTLP_ENDPOINT=TROVIS_ENDPOINT\n"
    "  OTEL_EXPORTER_OTLP_PROTOCOL=http/json\n"
    "  OTEL_TRACES_EXPORTER=otlp\n"
    "  OTEL_EXPORTER_OTLP_HEADERS=X-Trovis-Api-Key=TROVIS_API_KEY\n"
    "- Do NOT invent other Trovis packages, commands, or flags. For anything "
    "not listed, use the generic OTEL recipe.\n"
)

# Guided add-agent chat (the "Set up with AI" flow on the Add Agent page).
# One short question or step per turn; quick-reply chips via `options`;
# copy-paste snippets via `code` with TROVIS_API_KEY / TROVIS_ENDPOINT
# placeholders that the frontend substitutes with the org's real values.
SYSTEM_CONNECT = (
    "You are the Trovis connect guide. You walk the user through connecting "
    "ONE AI agent to Trovis, step by step, in a chat. The DATA section below "
    "lists the agents already connected to this account.\n"
    "\n"
    "Always respond with raw JSON (no code fences, nothing outside the JSON):\n"
    "{\"answer\": \"...\", \"options\": [], \"code\": []}\n"
    "- answer: 1-3 short plain sentences. No markdown. Never put commands or "
    "code inline in the answer — commands go ONLY in code.\n"
    "- options: quick-reply chips, ONLY when you are asking a genuine "
    "multiple-choice question (2-5 options, each under 30 characters). Leave "
    "[] for open-ended questions.\n"
    "- code: copy-paste snippets, each {\"title\": \"...\", \"language\": "
    "\"bash|python|json\", \"content\": \"...\"}. Leave [] when there is "
    "nothing to run.\n"
    "\n"
    "CRITICAL — code must be attached, never just promised:\n"
    "- The `code` array IS what the user sees as a copy-paste block. The "
    "`answer` is only a short intro; it does NOT display your code.\n"
    "- So whenever your answer refers to a snippet, lines, a command, or says "
    "anything like 'add these two lines', 'here are the lines', 'paste this', "
    "or 'run this', the `code` array MUST be non-empty in the SAME response and "
    "contain exactly that code. Never reference code you did not put in `code`.\n"
    "- If `code` is empty, your answer must NOT mention or promise any code — "
    "ask a question or give a non-code instruction instead.\n"
    "- Example of a CORRECT step:\n"
    "  {\"answer\": \"Add these two lines at the very top of your agent's entry "
    "file, before any OpenAI Agents SDK imports. Run it, then tell me how it "
    "went.\", \"options\": [], \"code\": [{\"title\": \"Initialize Trovis\", "
    "\"language\": \"python\", \"content\": \"from trovis import init\\n"
    "init(api_key=\\\"TROVIS_API_KEY\\\", endpoint=\\\"TROVIS_ENDPOINT\\\", "
    "agent_name=\\\"my-agent\\\")\"}]}\n"
    "\n"
    "Conversation rules:\n"
    "- ONE question at a time. ONE step per turn (at most two tightly coupled "
    "steps, like install + init). End each step with a short confirmation "
    "question (\"Run that, then tell me how it went — or paste any error.\").\n"
    "- In code, write the literal placeholders TROVIS_API_KEY and "
    "TROVIS_ENDPOINT wherever the user's key or endpoint belongs — the UI "
    "fills in their real values. Never write ov_sk_ keys yourself, and never "
    "repeat a key the user pastes into the chat.\n"
    "- When you give the final \"run your agent\" step, tell them to watch "
    "this chat — a green connected banner appears here automatically within "
    "seconds of the agent's first telemetry.\n"
    "- If DATA shows their agent already arrived, congratulate them and offer "
    "next steps (rename it on the dashboard, set a budget on the Cost page, "
    "ask about it anytime with the assistant) instead of repeating setup.\n"
    "- If they are not sure what their agent is built with, ask ONE "
    "clarifying question with concrete signals as chips (e.g. does their "
    "code call query() / client.beta.agents / neither). Still unsure → use "
    "the generic OTEL recipe.\n"
    "- If they ask an unrelated question mid-flow, answer it briefly from "
    "what you know, then steer back to the current step.\n"
    "- If they paste an error, diagnose using the troubleshooting facts "
    "below; do not guess beyond them."
    + _SETUP_KNOWLEDGE
    + _CONNECT_SETUP_EXTRAS
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


def _parse_connect_response(raw_text: str) -> dict[str, Any]:
    """Parse a connect-guide reply into {answer, options, code}, tolerant of
    non-JSON. Mirrors _parse_ask_response: strip ``` fences, json.loads, and
    on any failure degrade to plain text with empty options/code. A truncated
    reply (MAX_TOKENS cutoff) is salvaged via the "answer" field when
    possible so raw JSON never lands in the chat. Never raises."""
    text = (raw_text or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1] if "\n" in text else ""
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
        text = text.strip()
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        parsed = None
    if isinstance(parsed, dict):
        answer = parsed.get("answer")
        if isinstance(answer, str) and answer.strip():
            options = [
                o.strip()
                for o in (parsed.get("options") or [])
                if isinstance(o, str) and o.strip()
            ][:6]
            code = []
            for block in (parsed.get("code") or [])[:4]:
                if not isinstance(block, dict):
                    continue
                content = block.get("content")
                if not (isinstance(content, str) and content.strip()):
                    continue
                title = block.get("title")
                language = block.get("language")
                code.append(
                    {
                        "title": title if isinstance(title, str) else None,
                        "language": language if isinstance(language, str) else None,
                        "content": content,
                    }
                )
            return {"answer": answer.strip(), "options": options, "code": code}
    # Truncated JSON: pull the answer string out so the user never sees raw
    # JSON. Matches "answer": "..." allowing escaped quotes.
    m = re.search(r'"answer"\s*:\s*"((?:[^"\\]|\\.)*)', text)
    if m:
        try:
            salvaged = json.loads(f'"{m.group(1)}"')
        except (json.JSONDecodeError, ValueError):
            salvaged = m.group(1)
        if salvaged.strip():
            return {"answer": salvaged.strip(), "options": [], "code": []}
    return {"answer": (raw_text or "").strip(), "options": [], "code": []}


def ask_connect(
    account_id: int | None,
    messages: list[dict[str, str]],
) -> dict[str, Any]:
    """Guided add-agent chat. Returns {answer, options, code}. The client
    seeds the thread with a hardcoded assistant greeting; the Anthropic API
    requires the first message to be role=user, so prepend a synthetic primer
    when the history starts with the assistant (also covers histories whose
    head was trimmed by the client's turn cap)."""
    api_key = _require_api_key()
    agents = database.get_agents(account_id=account_id)
    context = _format_fleet_context(agents)
    msgs = list(messages or [])
    if msgs and (msgs[0] or {}).get("role") == "assistant":
        msgs.insert(
            0,
            {
                "role": "user",
                "content": "(I just opened the guided agent-connect flow.)",
            },
        )
    raw = _call_claude(api_key, SYSTEM_CONNECT, context, msgs)
    return _parse_connect_response(raw)


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
        name = a["service_name"]
        display = a.get("display_name")
        lines.append(
            f"## {name}" + (f" (display name: {display})" if display else "")
        )
        if a.get("platform"):
            lines.append(f"- platform: {a['platform']}")
        lines.append(
            f"- spans={total_spans} errors={total_errors} "
            f"error_rate={rate:.1f}% avg_duration_ms={a['avg_duration_ms']:.0f}"
        )
        # Cost grounding — without this the model would have to guess at
        # "which agent costs the most" style questions.
        lines.append(
            f"- cost: today_utc=${a.get('cost_today') or 0:.4f} "
            f"last_7d=${a.get('cost_7d') or 0:.4f} "
            f"all_time=${a.get('estimated_cost_usd') or 0:.4f} "
            f"total_tokens={a.get('total_tokens') or 0}"
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
