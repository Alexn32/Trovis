"""Claude-powered Q&A over agent telemetry and the live work record.

Two entry points:
  - ask_about_fleet(account_id, messages, viewer_user_id=...) — context =
    every agent's summary, plus read-only tools over telemetry AND the
    work board / loops
  - ask_about_agent(service_name, account_id, messages, viewer_user_id=...)
    — context = one agent's full payload (summary + description +
    registration files + last ~30 spans)

`viewer_user_id` is the signed-in session user. It is required for
"waiting on me" / is_yours. API-key auth has no person, so those tools
return an explicit sign-in message rather than faking identity.

Stateless on the backend. The caller passes the full chat thread on every
request so we can support multi-turn conversations without server-side
session state.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any

import anthropic

import database
import loops

# The Ask assistant is a primary product surface (global ⌘K pill) — use the
# most capable model. Setup walkthroughs need more room than quick answers.
MODEL = "claude-opus-5"

# Thinking stays ON here, unlike describer.py — this module is the one that
# calls tools. On the Opus 5 line, disabling thinking can make the model write
# a tool call into its visible text instead of emitting a tool_use block: the
# turn succeeds, the call silently never runs, and _agentic_answer returns
# that text as the answer. Thinking on avoids it.
#
# The cost of thinking-on is paid two ways. `effort: low` keeps the ⌘K pill
# responsive (low/medium are strong on this model), and the token budgets
# below carry headroom because max_tokens now caps thinking + response text
# together — the pre-Opus-5 values (1500 / 2200) left no room for both.
THINKING = {"type": "adaptive"}
OUTPUT_CONFIG = {"effort": "low"}
MAX_TOKENS = 6000

# How many recent spans to include in the per-agent context. 30 covers the
# most common "why did this fail" question without bloating prompts.
RECENT_SPAN_LIMIT = 30


class AskApiKeyMissingError(RuntimeError):
    """ANTHROPIC_API_KEY is not set in the environment."""


class AgentNotFoundError(LookupError):
    """No spans have been ingested for this service_name (under this account)."""


SYSTEM_FLEET = (
    "You are an analyst for Trovis, an agent management system. You have "
    "read access to summaries for every AI agent the user is running, and "
    "(via tools) the live work record — tasks, who holds them, and what is "
    "waiting on the signed-in user. Answer using that data; never invent "
    "work-record claims from agent summaries.\n"
    "- Be direct and specific. Refer to agents by their service_name and "
    "tasks by their title.\n"
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
    "Anthropic Managed Agents), and a custom GPT built in ChatGPT.\n"
    "- Hermes Agent is NOT currently supported. If asked, say so plainly: the "
    "integration is unverified and not a supported path today. Do not give "
    "Hermes setup steps.\n"
    "- A custom GPT (ChatGPT) connects with NO code: add Trovis as an Action "
    "on the GPT via OAuth. The GPT can both report its own activity "
    "(connectAgent/logActivity/reportComplete) AND ask about the fleet "
    "(askFleet — e.g. 'what was the last agent that ran?'). There's no "
    "token-level cost for GPT-Action agents themselves (they run on OpenAI's "
    "side).\n"
    "- Python platforms use the trovis-agents pip package with an extra per "
    "platform: pip install trovis-agents[openai], [anthropic], or "
    "[claude-agent-sdk]. Then two lines BEFORE the agent "
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
    "- Agents appear on the dashboard automatically within seconds of their "
    "first telemetry — no pre-registration. Message/output content is only "
    "captured when the user opts in (capture on / TROVIS_CAPTURE_OUTPUTS=true) "
    "— recommend enabling it, since without it you can't read what an agent "
    "actually said or asked about its outputs (metadata only). If a user asks "
    "why they can't see an agent's last output/message, the usual cause is "
    "that capture is off for that agent.\n"
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
    "fleet, their live work record (tasks on the Work board), and their guide "
    "to the product. You have read access to telemetry summaries for every "
    "agent the user runs (provided below), and tools for the work record. "
    "You know how Trovis works end to end.\n"
    "- Ground every claim about the user's agents in the telemetry data. "
    "Ground every claim about tasks, what's waiting, or why something is "
    "stuck in the work-record tools — never guess from agent summaries. Use "
    "specific numbers and refer to agents and tasks by name. Never invent data.\n"
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
    "  \"CLAUDE_CODE_ENHANCED_TELEMETRY_BETA\": \"1\"\n"
    "  \"OTEL_TRACES_EXPORTER\": \"otlp\"\n"
    "  \"OTEL_EXPORTER_OTLP_PROTOCOL\": \"http/json\"\n"
    "  \"OTEL_EXPORTER_OTLP_ENDPOINT\": \"TROVIS_ENDPOINT\"\n"
    "  \"OTEL_EXPORTER_OTLP_HEADERS\": \"X-Trovis-Api-Key=TROVIS_API_KEY\"\n"
    "- OpenClaw: openclaw plugins install clawhub:@trovis/openclaw-plugin, "
    "then in any chat: /trovis connect TROVIS_ENDPOINT then /trovis apikey "
    "TROVIS_API_KEY then /trovis capture on then /trovis status. STRONGLY "
    "recommend /trovis capture on as part of setup (not optional) — without it "
    "Trovis only records metadata (what ran, when, cost), NOT the actual "
    "messages/responses/tool results, so the user can't read what the agent "
    "said or ask about its outputs. It sends that content to Trovis; only skip "
    "it if the user raises a privacy concern. Terminal alternative: openclaw "
    "config set plugins.entries.trovis.config.endpoint TROVIS_ENDPOINT and "
    "...config.apiKey TROVIS_API_KEY and ...config.captureOutputs true.\n"
    "- ChatGPT custom GPT (NO code, NO pip, NO API key in the config — OAuth "
    "handles auth). Walk them through the GPT builder: (1) open their GPT → "
    "Configure → Create new action; (2) import the schema by URL: "
    "https://api.trovisai.com/actions/openapi.json ; (3) set Authentication "
    "to OAuth with Client ID oversee-chatgpt, their OAUTH_CLIENT_SECRET, "
    "Authorization URL https://api.trovisai.com/oauth/authorize, Token URL "
    "https://api.trovisai.com/oauth/token, scope blank, token exchange "
    "Default (POST); (4) paste GPT Instructions telling it to call "
    "connectAgent at the start, logActivity per step, reportComplete when "
    "done, and askFleet whenever the user asks about their agents; (5) save "
    "and run — it authorizes once, then appears in the fleet. "
    "For ChatGPT, write these LITERAL api.trovisai.com URLs in code snippets "
    "— do NOT use the TROVIS_ENDPOINT/TROVIS_API_KEY placeholders (there's no "
    "API key in this flow). Never print the client secret yourself.\n"
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
    viewer_user_id: int | None = None,
) -> dict[str, Any]:
    """Answer a question about the whole fleet. Returns {answer, visual} —
    `visual` is a {type, props} dict (Dashboard pill, concise prompt) or None.
    When `concise` is set, use the short plain-prose + generative-UI prompt.
    `viewer_user_id` is the signed-in session user (needed for is_yours)."""
    api_key = _require_api_key()
    agents = database.get_agents(account_id=account_id)
    seed = _format_fleet_context(agents)
    system = SYSTEM_FLEET_CONCISE if concise else SYSTEM_FLEET
    raw = _agentic_answer(
        api_key, system, account_id, messages, seed,
        viewer_user_id=viewer_user_id,
    )
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
    viewer_user_id: int | None = None,
) -> str:
    """Answer a question scoped to one instance, or one sub-agent when
    `agent_id` is set. `viewer_user_id` threads through to work-record tools."""
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
    focus = (
        f"You are focused on the agent '{service_name}'"
        + (f" (sub-agent '{agent_id}')" if agent_id else "")
        + ". Default every tool call to this agent (service_name="
        + repr(service_name)
        + (f", agent_id={agent_id!r}" if agent_id else "")
        + ") unless the user explicitly asks about a different agent or the "
        "wider fleet.\n\n"
    )
    seed = focus + _format_agent_context(summary, spans, registration)
    return _agentic_answer(
        api_key, SYSTEM_AGENT, account_id, messages, seed,
        viewer_user_id=viewer_user_id,
    )


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
            status = "ERROR" if database.is_error_span(s) else "OK"
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


# ---------------------------------------------------------------------------
# Agentic Ask — read-only tools + a bounded retrieval loop
# ---------------------------------------------------------------------------
# The assistant gets tools to pull live telemetry AND the live work record
# on demand — captured message/response/tool content, spans, costs, the
# fleet list, and board/loop state — and loops until it has what it needs
# to answer. `account_id` and `viewer_user_id` are bound server-side per
# call and NEVER exposed to the model, so a tool can only ever read the
# caller's own data. is_yours is only true when viewer_user_id is set.

_ASK_MAX_ITERS = 6
_ASK_TOOL_TOKENS = 8000      # per-turn cap while looping (incl. thinking)
_TOOL_CONTENT_CAP = 1600     # max chars of captured content per item
_TOOL_ITEMS_CAP = 25         # max items any single tool returns
_TOOL_EVENTS_CAP = 12        # last N narrated loop events in get_task_story
_TOOL_SEGMENTS_CAP = 8

# Same engine-state → board-column map as main._STATE_TO_COLUMN. Display
# only — is_yours still comes from get_work_board's awaiting_is_you.
_STATE_TO_COLUMN = {
    "open": "working",
    "working": "working",
    "awaiting_agent": "working",
    "awaiting_system": "working",
    "awaiting_human": "waiting_person",
    "stalled": "stuck",
    "done": "done",
    "abandoned": "done",
}

_SIGN_IN_FOR_ME = (
    "Sign in to see what's waiting on you. An API key cannot identify a person, "
    "so Trovis will not guess which tasks are yours."
)


def _cap(s: Any, n: int) -> str | None:
    if s is None:
        return None
    t = str(s)
    return t if len(t) <= n else t[: n - 1] + "…"


def _normalize_messages(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    """Drop empties + non-user/assistant turns; require a trailing user turn."""
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
    return cleaned


_ASK_TOOLS = [
    {
        "name": "list_agents",
        "description": "List every agent in the user's fleet with status, description, recent activity, error counts, and cost. Use for fleet-wide or cross-agent questions, or to find which agent the user means.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_agent_details",
        "description": "One agent's summary (span/error counts, error rate, avg duration, cost, tokens, status + reason, first/last seen) plus its declared identity (role, system prompt/SOUL, operating manual). Use to learn what an agent is supposed to do.",
        "input_schema": {
            "type": "object",
            "properties": {
                "service_name": {"type": "string"},
                "agent_id": {"type": "string", "description": "Optional sub-agent id."},
            },
            "required": ["service_name"],
        },
    },
    {
        "name": "get_recent_exchanges",
        "description": "An agent's ACTUAL captured content, newest first: user messages, agent responses, and tool results (the real text). Call this whenever the user asks what an agent said, replied, produced, wrote, or received. Empty when output capture is off for the agent.",
        "input_schema": {
            "type": "object",
            "properties": {
                "service_name": {"type": "string"},
                "agent_id": {"type": "string"},
                "limit": {"type": "integer", "description": "Max items (default 12, max 25)."},
            },
            "required": ["service_name"],
        },
    },
    {
        "name": "get_recent_spans",
        "description": "An agent's recent raw spans (operations) newest first, including status and error messages. Use to diagnose failures or see the structural sequence of what ran. Set errors_only to focus on failures.",
        "input_schema": {
            "type": "object",
            "properties": {
                "service_name": {"type": "string"},
                "agent_id": {"type": "string"},
                "limit": {"type": "integer", "description": "Max spans (default 20, max 40)."},
                "errors_only": {"type": "boolean"},
            },
            "required": ["service_name"],
        },
    },
    {
        "name": "get_costs",
        "description": "An agent's cost + token breakdown over the last N days (default 7), including the per-day series. Use for spend questions.",
        "input_schema": {
            "type": "object",
            "properties": {
                "service_name": {"type": "string"},
                "agent_id": {"type": "string"},
                "days": {"type": "integer"},
            },
            "required": ["service_name"],
        },
    },
    {
        "name": "get_waiting_on_me",
        "description": (
            "Tasks currently waiting on the signed-in user (the Work tab "
            "'yours' strip: is_yours). Returns id, title, age_seconds, holder, "
            "workflow, handoff_event_id. Use for 'what's waiting on me', 'my "
            "desk', 'assigned to me'. If the caller is not signed in, this "
            "returns a sign-in message — tell them to sign in; never invent "
            "is_yours from agent summaries."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_work_overview",
        "description": (
            "Work-record rollups like the Work tab summary: counts by kind "
            "(workflow) for in_motion / waiting_person / stuck / done_today / "
            "ongoing, plus the Other work catch-all. Does not claim any task "
            "is 'yours' unless a signed-in viewer is present. Use for 'how "
            "much is stuck', 'what's in flight', fleet-of-work shape."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "find_tasks",
        "description": (
            "Search the live work board by title or id substring. Optional "
            "workflow_id filter. Returns up to 25 tasks with column, state, "
            "stuck_reason, waiting_on, holder. Use to locate a task before "
            "calling get_task_story."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Title or id substring (case-insensitive). Empty = all board tasks, capped.",
                },
                "workflow_id": {
                    "type": "integer",
                    "description": "Optional workflow filter.",
                },
            },
        },
    },
    {
        "name": "get_task_story",
        "description": (
            "Live story for one task (loop): current state, holder, "
            "waiting_on / awaiting_human_name, age, stuck_reason, recent "
            "events and possession segments. Cite this for 'why is X stuck' "
            "/ 'what's blocking this' — never guess from spans or agent "
            "summaries."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "loop_id": {
                    "type": "integer",
                    "description": "Task id (the board card / loop id).",
                },
            },
            "required": ["loop_id"],
        },
    },
]

_AGENTIC_INSTRUCTIONS = (
    "\n\n---\n\nTOOLS — you can fetch live telemetry AND the live work "
    "record on demand:\n"
    "Telemetry: list_agents, get_agent_details, get_recent_exchanges (the "
    "ACTUAL message/response/tool text), get_recent_spans (incl. errors + "
    "status messages), get_costs.\n"
    "Work record: get_waiting_on_me, get_work_overview, find_tasks, "
    "get_task_story.\n"
    "Ground every answer in real data:\n"
    "- Asked what an agent said/did/produced/received → call "
    "get_recent_exchanges and quote it.\n"
    "- Asked why an agent failed / what's wrong with an agent → call "
    "get_recent_spans with errors_only:true and read the status messages.\n"
    "- Asked about spend → call get_costs. Fleet/cross-agent → list_agents.\n"
    "- Asked what's waiting on me / my desk / assigned to me → call "
    "get_waiting_on_me. If it says to sign in, tell the user to sign in. "
    "NEVER invent is_yours from agent summaries.\n"
    "- Asked about work shape / how much is stuck or waiting → "
    "get_work_overview.\n"
    "- Asked why a task is stuck / what's blocking it / the story of a "
    "task → find_tasks then get_task_story. Cite holder, waiting_on, "
    "stuck_reason, and recent events from the story. Do not guess from "
    "fleet or agent summaries.\n"
    "Prefer fetching over guessing; make a few targeted calls, not many. Be "
    "decisive and specific — cite exact numbers and quote real content. If the "
    "data genuinely isn't there (capture off, no runs, every call errored, "
    "not signed in for 'me'), say so plainly and say how to get it. NEVER "
    "invent content you didn't retrieve."
)


def _as_int(v: Any) -> int | None:
    try:
        if v is None or v is False:
            return None
        return int(v)
    except (TypeError, ValueError):
        return None


def _humanize_age(s: int | None) -> str | None:
    if s is None:
        return None
    s = max(0, int(s))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def _work_title(row: dict[str, Any]) -> str:
    title = (row.get("title") or "").strip()
    if title:
        return title
    who = row.get("holder_name") or row.get("service_name") or "An agent"
    return f"Task from {who}"


def _work_age_seconds(row: dict[str, Any], now_ns: int) -> int | None:
    since = row.get("state_since_unix") or row.get("last_event_unix")
    if not since:
        return None
    return max(0, int((now_ns - int(since)) // 1_000_000_000))


def _work_stuck_reason(row: dict[str, Any], column: str, age_s: int | None) -> str | None:
    """Same posture as main._board_stuck_reason — display only."""
    if column != "stuck":
        return None
    if row.get("holder_type") == "human":
        return None
    age_bit = f", {_humanize_age(age_s)}" if age_s else ""
    if row.get("waiting_on"):
        return f"waiting on {row['waiting_on']}{age_bit}"
    if age_s:
        return f"no activity for {_humanize_age(age_s)}"
    return None


def _work_card(row: dict[str, Any], now_ns: int) -> dict[str, Any]:
    """Project a get_work_board row the same way /work/summary cards do.

    is_yours is awaiting_is_you from the board helper — we do not classify
    'yours' here. Without a viewer, get_work_board leaves awaiting_is_you False.
    """
    column = _STATE_TO_COLUMN.get(row.get("cached_state") or "", "working")
    age_s = _work_age_seconds(row, now_ns)
    return {
        "id": row["id"],
        "title": _work_title(row),
        "column": column,
        "state": row.get("cached_state") or "",
        "holder": row.get("holder_name") or row.get("service_name") or "an agent",
        "holder_type": row.get("holder_type") or "agent",
        "waiting_on": row.get("waiting_on"),
        "age_seconds": age_s,
        "is_yours": bool(row.get("awaiting_is_you")),
        "handoff_event_id": row.get("awaiting_handoff_event_id"),
        "workflow_id": row.get("workflow_id"),
        "workflow": row.get("workflow_name"),
        "stuck_reason": _work_stuck_reason(row, column, age_s),
        "standing": bool(row.get("standing")),
        "awaiting_human_name": row.get("awaiting_human_name"),
    }


def _fetch_work_cards(
    account_id: int | None,
    viewer_user_id: int | None,
    workflow_id: int | None = None,
) -> list[dict[str, Any]]:
    """Same fetch as GET /work/summary / GET /work/board — one unfiltered
    (or workflow-filtered) get_work_board, then the card projection."""
    now_ns = time.time_ns()
    rows = database.get_work_board(
        account_id,
        viewer_user_id=viewer_user_id,
        workflow_id=workflow_id,
        now_ns=now_ns,
    )
    return [_work_card(r, now_ns) for r in rows]


def _kind_rollups(cards: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Kind/stuck/waiting rollups matching GET /work/summary grouping."""
    kinds: dict[int | None, dict[str, Any]] = {}
    for c in cards:
        key = c.get("workflow_id")
        k = kinds.setdefault(
            key,
            {
                "workflow_id": key,
                "name": c.get("workflow") or "Other work",
                "in_motion": 0, "waiting_person": 0, "stuck": 0,
                "done_today": 0, "ongoing": 0,
            },
        )
        if c.get("standing"):
            field = "ongoing"
        else:
            field = {
                "working": "in_motion",
                "waiting_person": "waiting_person",
                "stuck": "stuck",
                "done": "done_today",
            }.get(c.get("column"), "in_motion")
        k[field] += 1

    def _card(d: dict[str, Any], is_other: bool) -> dict[str, Any]:
        return {
            "workflow_id": d["workflow_id"],
            "name": d["name"],
            "in_motion": d["in_motion"],
            "waiting_person": d["waiting_person"],
            "stuck": d["stuck"],
            "done_today": d["done_today"],
            "ongoing": d["ongoing"],
            "is_other": is_other,
        }

    declared = [_card(d, False) for wid, d in kinds.items() if wid is not None]
    other = _card(kinds[None], True) if None in kinds else None
    declared.sort(
        key=lambda k: (
            -(k["waiting_person"] + k["stuck"]),
            -(k["in_motion"] + k["done_today"]),
            k["name"].lower(),
        )
    )
    return declared, other


def _slim_event(ev: dict[str, Any]) -> dict[str, Any]:
    payload = ev.get("payload") or {}
    slim_payload = {
        k: payload.get(k)
        for k in (
            "direction", "target_id", "target_name", "handoff_id",
            "span_name", "tool", "failed", "error", "reason",
        )
        if payload.get(k) not in (None, "", False)
    }
    out = {
        "type": ev.get("type"),
        "sentence": _cap(ev.get("sentence"), 240),
        "actor_type": ev.get("actor_type"),
        "actor": ev.get("actor"),
        "ts": ev.get("ts"),
    }
    if slim_payload:
        out["payload"] = slim_payload
    return out


def _slim_segment(seg: dict[str, Any]) -> dict[str, Any]:
    return {
        "holder_type": seg.get("holder_type"),
        "holder": seg.get("holder"),
        "waiting": bool(seg.get("waiting")),
        "start_ns": seg.get("start_ns"),
        "end_ns": seg.get("end_ns"),
        "event_count": seg.get("event_count") or 0,
    }


def _yours_strip(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Same filter/sort as GET /work/summary `yours`."""
    yours = [c for c in cards if c.get("is_yours")]
    yours.sort(key=lambda c: -(c.get("age_seconds") or 0))
    return [
        {
            "id": c["id"],
            "title": c["title"],
            "age_seconds": c.get("age_seconds"),
            "holder": c.get("holder"),
            "workflow": c.get("workflow"),
            "handoff_event_id": c.get("handoff_event_id"),
        }
        for c in yours
    ]


def _find_tasks(
    cards: list[dict[str, Any]],
    query: str,
    include_yours: bool,
) -> list[dict[str, Any]]:
    q = (query or "").strip().lower()
    matched = cards
    if q:
        matched = [
            c for c in cards
            if q in (c.get("title") or "").lower() or q in str(c.get("id"))
        ]
    out: list[dict[str, Any]] = []
    for c in matched[:_TOOL_ITEMS_CAP]:
        item = {
            "id": c["id"],
            "title": c["title"],
            "column": c.get("column"),
            "state": c.get("state"),
            "stuck_reason": c.get("stuck_reason"),
            "waiting_on": c.get("waiting_on"),
            "holder": c.get("holder"),
            "age_seconds": c.get("age_seconds"),
            "workflow": c.get("workflow"),
            "workflow_id": c.get("workflow_id"),
        }
        if include_yours:
            item["is_yours"] = bool(c.get("is_yours"))
        out.append(item)
    return out


def _task_story(
    loop_id: int,
    account_id: int | None,
    viewer_user_id: int | None,
) -> dict[str, Any]:
    loop = database.get_loop(loop_id, account_id, viewer_user_id=viewer_user_id)
    if loop is None:
        return {"error": "task not found"}
    stream = database.get_loop_stream(loop_id, account_id) or []
    narrated = loops.narrate_events(stream)
    segments = loops.compute_loop_segments(stream)
    now_ns = time.time_ns()
    # Prefer the board row when the task is on today's board so holder /
    # waiting_on / stuck_reason match WorkTab. Fall back to loop + segments.
    board_cards = _fetch_work_cards(account_id, viewer_user_id)
    card = next((c for c in board_cards if c["id"] == loop_id), None)
    last_seg = segments[-1] if segments else None
    column = (card or {}).get("column") or _STATE_TO_COLUMN.get(
        loop.get("cached_state") or "", "working"
    )
    age_s = (card or {}).get("age_seconds")
    if age_s is None:
        last = loop.get("last_event_unix")
        age_s = (
            max(0, int((now_ns - int(last)) // 1_000_000_000)) if last else None
        )
    holder = (
        (card or {}).get("holder")
        or loop.get("awaiting_human_name")
        or (last_seg or {}).get("holder")
        or loop.get("service_name")
        or "an agent"
    )
    waiting_on = (card or {}).get("waiting_on") or loop.get("awaiting_human_name")
    out = {
        "id": loop["id"],
        "title": _work_title({**loop, "holder_name": holder}),
        "state": loop.get("cached_state") or "",
        "column": column,
        "holder": holder,
        "holder_type": (card or {}).get("holder_type") or (last_seg or {}).get("holder_type"),
        "waiting_on": waiting_on,
        "awaiting_human_name": loop.get("awaiting_human_name"),
        "age_seconds": age_s,
        "stuck_reason": (card or {}).get("stuck_reason"),
        "workflow": loop.get("workflow_name"),
        "workflow_id": loop.get("workflow_id"),
        "handoff_event_id": loop.get("awaiting_handoff_event_id"),
        "events": [_slim_event(e) for e in narrated[-_TOOL_EVENTS_CAP:]],
        "event_count": len(narrated),
        "segments": [_slim_segment(s) for s in segments[-_TOOL_SEGMENTS_CAP:]],
    }
    if viewer_user_id is not None:
        out["is_yours"] = bool(loop.get("awaiting_is_you"))
    return out


def _run_tool(
    name: str,
    inp: dict[str, Any] | None,
    account_id: int | None,
    viewer_user_id: int | None = None,
) -> str:
    """Execute one read-only Ask tool, scoped to `account_id`. Returns a JSON
    string for the model. Never raises — failures return a readable message so
    the loop keeps going. `viewer_user_id` is required for is_yours / "me"."""
    try:
        inp = inp or {}
        svc = inp.get("service_name")
        aid = inp.get("agent_id")

        if name == "get_waiting_on_me":
            if viewer_user_id is None:
                return json.dumps({
                    "signed_in": False,
                    "message": _SIGN_IN_FOR_ME,
                    "tasks": [],
                })
            cards = _fetch_work_cards(account_id, viewer_user_id)
            tasks = _yours_strip(cards)
            return json.dumps({"signed_in": True, "count": len(tasks), "tasks": tasks})

        if name == "get_work_overview":
            cards = _fetch_work_cards(account_id, viewer_user_id)
            declared, other = _kind_rollups(cards)
            totals = {
                "in_motion": 0, "waiting_person": 0, "stuck": 0,
                "done_today": 0, "ongoing": 0,
            }
            for k in declared + ([other] if other else []):
                for field in totals:
                    totals[field] += k[field]
            out: dict[str, Any] = {
                "total": len(cards),
                "kinds": declared,
                "other": other,
                **totals,
            }
            if viewer_user_id is None:
                out["signed_in"] = False
                out["note"] = (
                    "Sign in to see which tasks are waiting on you. "
                    "Rollups above are account-wide, not personal."
                )
            else:
                yours = _yours_strip(cards)
                out["signed_in"] = True
                out["yours_count"] = len(yours)
                out["yours"] = yours
            return json.dumps(out)

        if name == "find_tasks":
            workflow_id = _as_int(inp.get("workflow_id"))
            cards = _fetch_work_cards(
                account_id, viewer_user_id, workflow_id=workflow_id,
            )
            query = inp.get("query")
            if query is None:
                query = inp.get("title") or ""
            tasks = _find_tasks(
                cards, str(query), include_yours=viewer_user_id is not None,
            )
            return json.dumps({"count": len(tasks), "tasks": tasks})

        if name == "get_task_story":
            loop_id = _as_int(inp.get("loop_id"))
            if loop_id is None:
                loop_id = _as_int(inp.get("id") or inp.get("task_id"))
            if loop_id is None:
                return json.dumps({"error": "loop_id is required"})
            return json.dumps(
                _task_story(loop_id, account_id, viewer_user_id), default=str,
            )

        if name == "list_agents":
            groups = database.get_agents(account_id=account_id)
            out = [
                {
                    "name": g.get("display_name") or g.get("service_name"),
                    "service_name": g.get("service_name"),
                    "sub_agents": [a.get("agent_id") for a in (g.get("agents") or [])],
                    "description": _cap(g.get("description"), 300),
                    "platform": g.get("platform"),
                    "spans": g.get("total_spans"),
                    "errors": g.get("total_errors"),
                    "cost_today": round(float(g.get("cost_today") or 0), 4),
                    "cost_7d": round(float(g.get("cost_7d") or 0), 4),
                    "last_seen": g.get("last_seen"),
                    "locked": g.get("locked"),
                }
                for g in groups[:_TOOL_ITEMS_CAP]
            ]
            return json.dumps({"count": len(groups), "agents": out})

        if name == "get_agent_details":
            if not svc:
                return json.dumps({"error": "service_name is required"})
            summary = database.get_agent_summary(svc, account_id=account_id, agent_id=aid)
            if not summary:
                return json.dumps({"error": f"agent {svc!r} not found"})
            reg = database.get_latest_registration(svc, account_id=account_id, agent_id=aid) or {}
            return json.dumps({
                "summary": {
                    k: summary.get(k)
                    for k in (
                        "service_name", "agent_id", "description", "span_count",
                        "error_count", "avg_duration_ms", "first_seen", "last_seen",
                        "estimated_cost_usd", "total_tokens", "status",
                        "status_reason", "has_registration",
                    )
                },
                "identity": {
                    "role": reg.get("identity"),
                    "system_prompt": _cap(reg.get("soul"), 2000),
                    "operating_manual": _cap(reg.get("operating_manual"), 1500),
                },
            }, default=str)

        if name == "get_recent_exchanges":
            if not svc:
                return json.dumps({"error": "service_name is required"})
            limit = max(1, min(_TOOL_ITEMS_CAP, int(inp.get("limit") or 12)))
            rows = database.get_agent_outputs(svc, account_id=account_id, agent_id=aid, limit=limit)
            items = [
                {
                    "type": r.get("content_type"),
                    "operation": r.get("operation"),
                    "time": r.get("timestamp"),
                    "content": _cap(r.get("content"), _TOOL_CONTENT_CAP),
                }
                for r in rows
            ]
            note = None if items else (
                "No captured content for this agent — output capture may be off "
                "(enable with '/trovis capture on'), or it has produced no "
                "message/response/tool content yet."
            )
            return json.dumps({"count": len(items), "exchanges": items, "note": note})

        if name == "get_recent_spans":
            if not svc:
                return json.dumps({"error": "service_name is required"})
            limit = max(1, min(40, int(inp.get("limit") or 20)))
            errors_only = bool(inp.get("errors_only"))
            spans = database.get_agent_spans(svc, limit=limit, account_id=account_id, agent_id=aid)
            out = []
            for s in spans:
                status = "error" if database.is_error_span(s) else "ok"
                if errors_only and status != "error":
                    continue
                dur = (int(s.get("end_time_unix") or 0) - int(s.get("start_time_unix") or 0)) / 1e6
                out.append({
                    "operation": s.get("span_name"),
                    "status": status,
                    "status_message": _cap(s.get("status_message"), 300),
                    "duration_ms": round(dur, 1),
                })
            return json.dumps({"count": len(out), "spans": out[:_TOOL_ITEMS_CAP]})

        if name == "get_costs":
            if not svc:
                return json.dumps({"error": "service_name is required"})
            days = max(1, min(90, int(inp.get("days") or 7)))
            costs = database.get_agent_costs(svc, account_id=account_id, agent_id=aid, days=days)
            return json.dumps(costs, default=str)[:4000]

        return json.dumps({"error": f"unknown tool {name!r}"})
    except Exception as e:  # noqa: BLE001 — a tool failure must not kill the loop
        return json.dumps({"error": f"tool {name} failed: {e}"})


def _agentic_answer(
    api_key: str,
    system_prompt: str,
    account_id: int | None,
    messages: list[dict[str, str]],
    seed_context: str,
    viewer_user_id: int | None = None,
) -> str:
    """Run the tool-use loop: hand Claude the seed context + tools, execute any
    tool calls against the account's data, and repeat until it answers (or the
    iteration cap forces a wrap-up). Returns the final text.
    `viewer_user_id` is threaded into every tool so is_yours is never guessed."""
    convo = list(_normalize_messages(messages))
    system = (
        system_prompt
        + _AGENTIC_INSTRUCTIONS
        + "\n\n---\n\nSTARTING CONTEXT (you may fetch more with tools):\n\n"
        + seed_context
    )
    client = anthropic.Anthropic(api_key=api_key)

    def _text(resp: Any) -> str:
        return "".join(
            b.text for b in resp.content if getattr(b, "type", None) == "text"
        ).strip()

    for _ in range(_ASK_MAX_ITERS):
        resp = client.messages.create(
            model=MODEL,
            thinking=THINKING,
            output_config=OUTPUT_CONFIG,
            max_tokens=_ASK_TOOL_TOKENS,
            system=system,
            tools=_ASK_TOOLS,
            messages=convo,
        )
        if resp.stop_reason != "tool_use":
            return _text(resp)
        convo.append({"role": "assistant", "content": resp.content})
        results = [
            {
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": _run_tool(
                    block.name, block.input, account_id,
                    viewer_user_id=viewer_user_id,
                ),
            }
            for block in resp.content
            if getattr(block, "type", None) == "tool_use"
        ]
        convo.append({"role": "user", "content": results})

    # Iteration cap hit — force a final answer from what's been gathered.
    resp = client.messages.create(
        model=MODEL,
        thinking=THINKING,
        output_config=OUTPUT_CONFIG,
        max_tokens=MAX_TOKENS,
        system=system + "\n\nWrap up NOW: give your best answer from what you've gathered.",
        messages=convo,
    )
    return _text(resp)


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
        thinking=THINKING,
        output_config=OUTPUT_CONFIG,
        max_tokens=MAX_TOKENS,
        system=full_system,
        messages=cleaned,
    )
    return "".join(
        block.text
        for block in response.content
        if getattr(block, "type", None) == "text"
    ).strip()
