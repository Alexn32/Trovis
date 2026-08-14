"""Claude-powered description generator for agents.

Given a service_name, pull recent telemetry and ask Claude to write a plain-
English description of what the agent does. This is the feature that makes
Trovis useful on day one: a non-technical operator can read the description
and immediately understand each agent's job.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

import anthropic

import database
# Station/hint vocabularies live in loops.py — the workflow draft below shapes
# its output to them rather than keeping a second copy that could drift. Safe
# at module level: loops imports describer lazily, inside a function.
import loops

MODEL = "claude-sonnet-5"
MAX_TOKENS = 1024

# Thinking is explicit, not omitted — and must stay that way.
#
# On Sonnet 5 (and the Opus 5 line) omitting `thinking` means the model DOES
# think, and max_tokens caps thinking + response text together. Every call in
# this module is a short structured extraction on a tight budget — the loop
# title and record summary run on 40 tokens — so a default-on thinking pass
# would eat the whole budget and return an empty string. Nothing here needs
# multi-step reasoning, and none of these calls use tools (the one failure
# mode that makes disabled thinking risky), so we turn it off deliberately.
# Do not "clean this up" by dropping the parameter.
THINKING = {"type": "disabled"}

SYSTEM_PROMPT = (
    "You are an AI analyst for Trovis, an agent management system. "
    "Given telemetry data from an AI agent, write a clear, concise description "
    "of what this agent does in plain English. Include: what its job appears "
    "to be, what tools or APIs it uses, how often it runs, and any notable "
    "patterns. Write for a non-technical operations manager. Keep it to one "
    "paragraph, 3-5 sentences max. Do not hedge or use phrases like 'it "
    "appears to' — be direct and confident."
)

REGISTRATION_SYSTEM_PROMPT = (
    "You are an AI analyst for Trovis, an agent management system. You have "
    "been given the agent's own configuration files that define its purpose, "
    "personality, and operating rules. Use these as the primary source of "
    "truth for describing what this agent does. Supplement with telemetry "
    "data for operational details like frequency, performance, and error "
    "rates. Write a clear, confident description for a non-technical "
    "operations manager. One paragraph, 3-5 sentences."
)

# Two-field description contract (the redesigned Agent Detail header shows the
# short line, with the long form behind a "More" toggle). Appended to whichever
# system prompt is used so the model returns structured JSON instead of prose.
_DESC_JSON_RULES = (
    "\n\nReturn ONLY a JSON object, nothing else:\n"
    '{"short": "...", "long": "..."}\n'
    "- short: ONE declarative sentence, max 20 words, present tense, describing "
    "what the agent does. No hedging words (appears, seems, likely, may, "
    "probably). Never mention telemetry, spans, span counts, runs, tokens, cost, "
    "or data volume.\n"
    "- long: 2-3 sentences of additional context about how it works and its "
    "role. Same rules — present tense, declarative, no hedging, no telemetry "
    "references."
)

# One-line, past-tense summary of a single interaction for the Work Feed.
RECORD_SUMMARY_SYSTEM_PROMPT = (
    "You summarize one interaction an AI agent had, for a non-technical reader. "
    "Given the user's message and the agent's response, write ONE sentence, "
    "max 12 words, past tense, starting with a verb — e.g. 'Answered a question "
    "about pricing', 'Drafted a reply about refunds', 'Rejected an off-brand "
    "post'. Never include IDs, span names, token counts, or quotes. Return ONLY "
    "the sentence, no quotes, no JSON, no trailing period required."
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
        system_prompt = REGISTRATION_SYSTEM_PROMPT + _DESC_JSON_RULES
        user_prompt = _build_registration_prompt(summary, registration, outputs)
        source = "registration"
    else:
        system_prompt = SYSTEM_PROMPT + _DESC_JSON_RULES
        user_prompt = _build_prompt(summary, spans, outputs)
        source = "telemetry_only"

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=MODEL,
        thinking=THINKING,
        max_tokens=MAX_TOKENS,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    raw = "".join(
        block.text for block in response.content if getattr(block, "type", None) == "text"
    ).strip()
    short, long = _parse_two_field_description(raw)

    return {
        "service_name": service_name,
        # `description` stays the canonical field (= short) so every existing
        # reader keeps working; `description_long` carries the extended context.
        "description": short,
        "description_long": long,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "span_count_analyzed": len(spans),
        "source": source,
    }


def _parse_two_field_description(raw: str) -> tuple[str, str]:
    """Parse the model's `{"short","long"}` reply, tolerant of ``` fences and
    plain prose. On any failure, fall back to treating the whole reply as the
    short field with an empty long. Never raises."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1] if "\n" in text else ""
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
        text = text.strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict) and isinstance(parsed.get("short"), str):
            short = parsed["short"].strip()
            long = parsed.get("long")
            return short, (long.strip() if isinstance(long, str) else "")
    except (ValueError, TypeError):
        pass
    return (raw or "").strip(), ""


def record_summary(
    user_text: str | None, agent_text: str | None
) -> str:
    """One-sentence, past-tense, verb-first summary of a single interaction for
    the Work Feed. Returns "" when nothing usable / no API key (the caller then
    falls back to a generic label). Records are immutable, so the caller caches
    this permanently by record id and never regenerates."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return ""
    u = (user_text or "").strip()
    a = (agent_text or "").strip()
    if not u and not a:
        return ""
    user_prompt = f"USER: {u[:1500]}\n\nAGENT: {a[:1500]}"
    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=MODEL,
            thinking=THINKING,
            max_tokens=40,
            system=RECORD_SUMMARY_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        text = "".join(
            b.text for b in response.content if getattr(b, "type", None) == "text"
        ).strip()
    except Exception:  # noqa: BLE001 — summary is best-effort, never break the feed
        return ""
    # Strip stray wrapping quotes / trailing period the model sometimes adds.
    return text.strip().strip('"').rstrip(".").strip()


# ---------------------------------------------------------------------------
# Weekly summary + capability map (cached by main.py's endpoints)
# ---------------------------------------------------------------------------


WEEKLY_SYSTEM_PROMPT = (
    "You are an AI analyst for Trovis. Given week-over-week stats for an "
    "AI agent, write a 2-3 sentence plain-English summary of the week for "
    "a non-technical operations manager. Lead with what the agent did, "
    "then the most notable trend, then any concern or highlight. Be "
    "direct and confident — no hedging, no 'it appears to'. Reference "
    "concrete numbers when meaningful."
)


def _format_weekly_prompt(
    service_name: str,
    agent_id: str | None,
    this_week: dict[str, Any],
    last_week: dict[str, Any] | None,
    registration: dict[str, Any] | None,
    outputs: list[dict[str, Any]] | None,
) -> str:
    lines: list[str] = [
        f"Agent: {service_name}" + (f" / {agent_id}" if agent_id else ""),
        "",
        "## This week",
        f"- runs: {this_week['runs']}",
        f"- errors: {this_week['errors']}",
        f"- success_rate: {this_week['success_rate']:.1f}%",
        f"- avg_duration_ms: {this_week['avg_duration_ms']:.0f}",
    ]
    if this_week.get("tools_used"):
        lines.append(f"- tools_used: {', '.join(this_week['tools_used'])}")
    if this_week.get("operations"):
        lines.append(f"- operations: {', '.join(this_week['operations'])}")

    if last_week:
        lines.extend(
            [
                "",
                "## Previous week (days 8-14)",
                f"- runs: {last_week['runs']}",
                f"- errors: {last_week['errors']}",
                f"- success_rate: {last_week['success_rate']:.1f}%",
                f"- avg_duration_ms: {last_week['avg_duration_ms']:.0f}",
            ]
        )
    else:
        lines.extend(["", "Previous week: no data (new agent)."])

    if registration:
        soul = registration.get("soul") or registration.get("identity") or ""
        if soul:
            lines.extend(["", "## Identity (truncated)", soul[:600]])

    if outputs:
        lines.extend(["", "## Recent captured outputs"])
        for o in outputs[:3]:
            content = (o.get("content") or "").replace("\n", " ")
            lines.append(f"- [{o.get('content_type')}] {content[:200]}")

    return "\n".join(lines)


def weekly_summary(
    service_name: str,
    agent_id: str | None,
    this_week: dict[str, Any],
    last_week: dict[str, Any] | None,
    registration: dict[str, Any] | None,
    outputs: list[dict[str, Any]] | None,
) -> str:
    """Generate the 2-3 sentence weekly summary for one agent.

    Raises APIKeyMissingError when ANTHROPIC_API_KEY is unset so the
    caller can return a typed error to the client.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise APIKeyMissingError(
            "ANTHROPIC_API_KEY is not set. Export it before generating summaries."
        )
    user_prompt = _format_weekly_prompt(
        service_name, agent_id, this_week, last_week, registration, outputs
    )
    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=MODEL,
        thinking=THINKING,
        max_tokens=300,
        system=WEEKLY_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )
    return "".join(
        block.text for block in response.content if getattr(block, "type", None) == "text"
    ).strip()


CAPABILITIES_SYSTEM_PROMPT = (
    "You are an AI analyst for Trovis. Based on an agent's "
    "configuration and observed behavior, list its capabilities in "
    "three categories. READS FROM: what data sources it accesses. "
    "WRITES TO: what systems it changes. CAN DO: what concrete "
    "actions it performs. Be specific and use plain English (a "
    "non-technical manager should understand each entry). Return "
    "valid JSON exactly matching this schema: "
    '{"reads_from": [...], "writes_to": [...], "can_do": [...]}. '
    "Return ONLY the JSON object — no prose, no markdown fence, no "
    "explanation. Aim for 3-7 items per list. If a category is "
    "truly empty (e.g. a read-only agent with no writes), return an "
    "empty array, not null."
)


def _format_capabilities_prompt(
    service_name: str,
    agent_id: str | None,
    registration: dict[str, Any] | None,
    tools_used: list[str] | None,
    operations: list[str] | None,
) -> str:
    lines: list[str] = [
        f"Agent: {service_name}" + (f" / {agent_id}" if agent_id else ""),
    ]
    if registration:
        for field in ("soul", "identity", "operating_manual"):
            v = registration.get(field) or ""
            if v:
                lines.extend(["", f"## {field}.md", v[:2000]])
    if tools_used:
        lines.extend(["", "## Tools observed", ", ".join(tools_used)])
    if operations:
        lines.extend(["", "## Operations observed", ", ".join(operations)])
    if not registration and not tools_used and not operations:
        lines.append("(no registration or telemetry available)")
    return "\n".join(lines)


def capabilities(
    service_name: str,
    agent_id: str | None,
    registration: dict[str, Any] | None,
    tools_used: list[str] | None,
    operations: list[str] | None,
) -> dict[str, list[str]]:
    """Generate the capability map JSON.

    Robustly parses Claude's response — strips any accidental code
    fences and falls back to an empty triple when the JSON is
    unparseable so the endpoint can still return a 200 with empty
    lists rather than a 500.
    """
    import json as _json

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise APIKeyMissingError(
            "ANTHROPIC_API_KEY is not set. Export it before generating capabilities."
        )

    user_prompt = _format_capabilities_prompt(
        service_name, agent_id, registration, tools_used, operations
    )
    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=MODEL,
        thinking=THINKING,
        max_tokens=600,
        system=CAPABILITIES_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )
    raw = "".join(
        block.text for block in response.content if getattr(block, "type", None) == "text"
    ).strip()

    # Tolerate ```json fences just in case Claude ignores the "no fences"
    # instruction.
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    try:
        parsed = _json.loads(raw)
    except (TypeError, ValueError):
        parsed = {}

    def _str_list(v: Any) -> list[str]:
        if not isinstance(v, list):
            return []
        return [str(x).strip() for x in v if isinstance(x, (str, int, float)) and str(x).strip()]

    return {
        "reads_from": _str_list(parsed.get("reads_from")),
        "writes_to": _str_list(parsed.get("writes_to")),
        "can_do": _str_list(parsed.get("can_do")),
    }


# ---------------------------------------------------------------------------
# Telemetry analysis — operation stats and the long gaps that hint at a human
# ---------------------------------------------------------------------------


_GAP_THRESHOLD_S = 30.0


def _analyze_telemetry(spans: list[dict[str, Any]]) -> dict[str, Any]:
    """Mine operations (count + avg duration), per-run sequences, and the
    long time-gaps that hint at human involvement, from a span list."""
    op_count: dict[str, int] = {}
    op_dur_ms: dict[str, float] = {}
    by_trace: dict[str, list[dict[str, Any]]] = {}

    for s in spans:
        name = s.get("span_name") or "(unnamed)"
        dur = (s["end_time_unix"] - s["start_time_unix"]) / 1_000_000.0
        op_count[name] = op_count.get(name, 0) + 1
        op_dur_ms[name] = op_dur_ms.get(name, 0.0) + dur
        by_trace.setdefault(s.get("trace_id") or "", []).append(s)

    operations = [
        {
            "operation": name,
            "calls": op_count[name],
            "avg_ms": round(op_dur_ms[name] / op_count[name], 1),
        }
        for name in sorted(op_count, key=lambda n: -op_count[n])
    ]

    # Representative sequences + gaps, walking each trace in time order.
    sequences: list[str] = []
    gaps: list[str] = []
    for trace_id, tspans in by_trace.items():
        ordered = sorted(tspans, key=lambda x: x["start_time_unix"])
        seq = [s.get("span_name") or "(unnamed)" for s in ordered]
        if len(seq) > 1 and len(sequences) < 5:
            sequences.append(" → ".join(seq[:12]))
        for prev, nxt in zip(ordered, ordered[1:]):
            gap_s = (nxt["start_time_unix"] - prev["end_time_unix"]) / 1_000_000_000.0
            if gap_s > _GAP_THRESHOLD_S and len(gaps) < 8:
                gaps.append(
                    f"after '{prev.get('span_name')}' there was a "
                    f"{round(gap_s)}s gap before '{nxt.get('span_name')}'"
                )

    return {"operations": operations, "sequences": sequences, "gaps": gaps}


# ---------------------------------------------------------------------------
# Drift detection — declared identity vs. observed behavior
# ---------------------------------------------------------------------------
#
# Trovis captures each agent's *declared* identity at registration (soul /
# operating_manual / identity / user_context). Drift detection compares that
# stated job against what the agent has actually been doing (operations, tools,
# captured outputs) and flags when behavior steps outside the declared scope.
# One Claude call per agent; callers cache the verdict (see the /drift endpoint).

_DRIFT_STATUSES = ("aligned", "minor", "drift", "unknown")
_DRIFT_SEVERITIES = ("low", "medium", "high")

DRIFT_SYSTEM_PROMPT = (
    "You are a behavioral auditor for Trovis. You receive an AI agent's DECLARED "
    "identity (its stated job, purpose, and operating manual) and a sample of its "
    "RECENT OBSERVED BEHAVIOR (operations it ran, tools it called, representative "
    "sequences, and captured messages/outputs). Judge whether the observed behavior "
    "stays within the agent's declared job.\n\n"
    "Return ONLY valid JSON (no markdown), exactly this shape:\n"
    '{"status": "aligned" | "minor" | "drift", '
    '"headline": "<one plain-English sentence a non-technical manager understands>", '
    '"findings": [{"title": "<short label>", "evidence": "<the specific observed '
    'behavior>", "severity": "low" | "medium" | "high"}]}\n\n'
    "Rules:\n"
    "- 'aligned': behavior matches the declared job. findings MUST be [].\n"
    "- 'minor': mostly aligned, but one or more low/medium concerns worth noting.\n"
    "- 'drift': the agent clearly did something OUTSIDE its declared job (at least one "
    "high-severity finding).\n"
    "- Be specific and evidence-based. Cite the actual operation, tool, or output — "
    "never vague worry. Quote/paraphrase the observed signal in 'evidence'.\n"
    "- Do NOT invent behavior that isn't in the observed sample.\n"
    "- At most 4 findings; only genuine concerns.\n"
    "- The headline must stand alone and name the agent's job in plain terms."
)


def _normalize_drift(parsed: Any) -> dict[str, Any]:
    """Coerce a model reply into a safe drift report. Never raises. An empty/
    unparseable reply becomes an honest 'unknown' verdict (so the UI never shows
    a fabricated 'no drift' when the check didn't actually run)."""
    if not isinstance(parsed, dict) or not parsed:
        return {
            "status": "unknown",
            "headline": "Couldn't assess drift this time — try again shortly.",
            "findings": [],
        }
    status = str(parsed.get("status") or "").strip().lower()
    if status not in _DRIFT_STATUSES:
        status = "unknown"
    headline = str(parsed.get("headline") or "").strip()[:400]
    findings: list[dict[str, str]] = []
    raw_findings = parsed.get("findings")
    if isinstance(raw_findings, list):
        for f in raw_findings[:4]:
            if not isinstance(f, dict):
                continue
            title = str(f.get("title") or "").strip()[:120]
            evidence = str(f.get("evidence") or "").strip()[:600]
            if not title and not evidence:
                continue
            sev = str(f.get("severity") or "").strip().lower()
            if sev not in _DRIFT_SEVERITIES:
                sev = "low"
            findings.append({"title": title or "Concern", "evidence": evidence, "severity": sev})
    # 'aligned' implies no findings; drop any the model left in by mistake.
    if status == "aligned":
        findings = []
    if not headline:
        headline = {
            "aligned": "Behaving as declared — no drift detected.",
            "minor": "Mostly on-task, with a couple of minor notes.",
            "drift": "Stepped outside its declared job.",
            "unknown": "Drift could not be assessed.",
        }[status]
    return {"status": status, "headline": headline, "findings": findings}


def detect_drift(
    registration: dict[str, Any] | None,
    spans: list[dict[str, Any]],
    outputs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compare an agent's declared identity against its observed behavior.

    Returns {status, headline, findings:[{title, evidence, severity}]}. Raises
    APIKeyMissingError when ANTHROPIC_API_KEY is unset. Never raises on a bad
    model reply — falls back to an honest 'unknown' verdict.

    When there's no declared identity on record, returns 'unknown' WITHOUT a
    Claude call (drift is meaningless with nothing to compare against)."""
    reg = registration or {}
    soul = (reg.get("soul") or reg.get("identity") or "").strip()
    manual = (reg.get("operating_manual") or "").strip()
    context = (reg.get("user_context") or "").strip()

    if not (soul or manual or context):
        return {
            "status": "unknown",
            "headline": "No declared identity on record — connect an agent that "
            "registers its job to enable drift detection.",
            "findings": [],
        }

    identity_parts: list[str] = []
    if soul:
        identity_parts.append("## Declared identity / purpose\n" + soul[:1500])
    if manual:
        identity_parts.append("## Operating manual\n" + manual[:1500])
    if context:
        identity_parts.append("## User context\n" + context[:800])
    identity_block = "\n\n".join(identity_parts)

    # _analyze_telemetry subtracts start/end timings directly, so drop spans
    # with missing/non-numeric timestamps (real fleets have some — synthesized
    # action/MCP spans, legacy rows) before analyzing.
    timed = [
        s for s in (spans or [])
        if isinstance(s.get("start_time_unix"), (int, float))
        and isinstance(s.get("end_time_unix"), (int, float))
    ]
    analysis = _analyze_telemetry(timed)
    ops = analysis.get("operations") or []
    ops_block = (
        ", ".join(f"{o['operation']} (×{o['calls']})" for o in ops[:20])
        or "(no operations observed)"
    )
    tools, _models = _mine_signals(spans or [])
    tools_block = ", ".join(tools[:30]) or "(none)"
    sequences = analysis.get("sequences") or []
    seq_block = "\n".join(f"- {s}" for s in sequences[:5]) or "(none captured)"
    outputs_block = _format_outputs_block((outputs or [])[:12])

    user_prompt = (
        f"DECLARED IDENTITY:\n{identity_block}\n\n"
        "OBSERVED BEHAVIOR (recent):\n"
        f"Operations: {ops_block}\n"
        f"Tools used: {tools_block}\n"
        f"Representative sequences:\n{seq_block}\n\n"
        f"{outputs_block}"
        "Assess whether the observed behavior stays within the declared job, "
        "following the rules exactly."
    )
    parsed = _claude_json(DRIFT_SYSTEM_PROMPT, user_prompt, max_tokens=1200)
    return _normalize_drift(parsed)


# ---------------------------------------------------------------------------
# AI builder — create workflows & connections from a plain-English description
# ---------------------------------------------------------------------------


def _claude_json(system_prompt: str, user_prompt: str, max_tokens: int = 2000) -> Any:
    """Call Claude and parse a JSON object from the reply, tolerating ``` fences.
    Returns {} on parse failure. Raises APIKeyMissingError when unset."""
    import json as _json

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise APIKeyMissingError("ANTHROPIC_API_KEY is not set.")
    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=MODEL,
        thinking=THINKING,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )
    raw = "".join(
        b.text for b in response.content if getattr(b, "type", None) == "text"
    ).strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        return _json.loads(raw)
    except (TypeError, ValueError):
        return {}


WORKFLOW_DRAFT_SYSTEM_PROMPT = (
    "You are a process analyst for Trovis. An operator describes, in plain "
    "English, a recurring process their AI agents and people run together. "
    "Turn it into a DECLARED WORKFLOW: an ordered list of stations (who "
    "holds the work at each step) plus match hints (how Trovis recognizes a "
    "unit of work as an instance of this process).\n\n"
    "Stations are the drawing — one per step, in the order work actually "
    "moves. holder_type is 'agent' (an AI agent), 'human' (a person or "
    "role), or 'system' (an external service the work waits on). holder is "
    "who that is; label is what happens there, as a short verb phrase "
    "(\"scores the signup\"). carrier is how work travels to the NEXT "
    "station (\"Slack\", \"email\") — omit it unless the description says.\n\n"
    "Match hints are ANDed: a loop must satisfy EVERY hint to match, so "
    "prefer one or two precise hints over a long list. service_name and "
    "agent_id hints MUST use an exact name from the known-agents list — a "
    "hint naming an agent that does not exist means the workflow silently "
    "never matches anything. When no known agent fits, emit a single "
    "'title contains' hint instead. Return ONLY valid JSON, no markdown."
)


def workflow_draft_from_description(
    description: str, known_agents: list[str] | None = None
) -> dict[str, Any]:
    """Draft a declared workflow — name, stations, match hints — from a
    plain-English description of the process.

    Returns {"name": str, "stations": [...], "match_hints": [...]} shaped to
    the vocabularies in loops.py. Fail-soft throughout: anything the model
    returns off-schema is dropped rather than raised, so a bad reply yields a
    thin draft the operator can finish by hand — never a 500. Raises
    APIKeyMissingError when the key is unset.
    """
    known = [a for a in (known_agents or []) if a]
    agents_line = (
        "Known agents (use these EXACT names in agent stations and in "
        "service_name/agent_id hints): " + ", ".join(known)
        if known
        else "No agents have reported telemetry yet — describe stations "
        "generically and use a single 'title contains' hint."
    )
    user_prompt = (
        f"{agents_line}\n\n"
        f"Process description:\n{(description or '').strip()}\n\n"
        "Produce the workflow as JSON:\n"
        "{\n"
        '  "name": "short name for the process, 2-4 words",\n'
        '  "stations": [\n'
        f'    {{"holder_type": "{"|".join(loops.STATION_HOLDER_TYPES)}", '
        '"holder": "who", "label": "what happens here", '
        '"tools": ["tool names, omit if unknown"], '
        '"carrier": "how it travels to the next station, omit if unsaid"}\n'
        "  ],\n"
        '  "match_hints": [\n'
        f'    {{"field": "{"|".join(loops.MATCH_FIELDS)}", '
        f'"op": "{"|".join(loops.MATCH_OPS)}", "value": "..."}}\n'
        "  ]\n"
        "}\n"
        "Return ONLY the JSON."
    )
    parsed = _claude_json(WORKFLOW_DRAFT_SYSTEM_PROMPT, user_prompt)
    if not isinstance(parsed, dict):
        parsed = {}

    stations: list[dict[str, Any]] = []
    for s in parsed.get("stations") or []:
        if not isinstance(s, dict):
            continue
        holder_type = str(s.get("holder_type") or "agent").strip().lower()
        if holder_type not in loops.STATION_HOLDER_TYPES:
            holder_type = "agent"
        station: dict[str, Any] = {"holder_type": holder_type}
        for key, cap in (("holder", 120), ("label", 200), ("carrier", 60)):
            val = s.get(key)
            if isinstance(val, str) and val.strip():
                station[key] = val.strip()[:cap]
        tools = [
            t.strip()[:80]
            for t in (s.get("tools") or [])
            if isinstance(t, str) and t.strip()
        ]
        if tools:
            station["tools"] = tools
        # A station with no holder AND no label is an empty box — drop it.
        if station.get("holder") or station.get("label"):
            stations.append(station)

    hints: list[dict[str, Any]] = []
    for h in parsed.get("match_hints") or []:
        if not isinstance(h, dict):
            continue
        field = str(h.get("field") or "").strip()
        op = str(h.get("op") or "").strip()
        value = h.get("value")
        if field not in loops.MATCH_FIELDS or op not in loops.MATCH_OPS:
            continue
        if not isinstance(value, str) or not value.strip():
            continue
        value = value.strip()[:200]
        # An identity hint naming an agent that doesn't exist matches nothing
        # and the workflow just sits empty — the exact failure the operator
        # can't debug. Drop it and let them add one by hand.
        if field in ("service_name", "agent_id") and known and value not in known:
            continue
        hints.append({"field": field, "op": op, "value": value})

    name = parsed.get("name")
    name = name.strip()[:120] if isinstance(name, str) else ""
    return {"name": name, "stations": stations, "match_hints": hints}


CONNECTIONS_DESC_SYSTEM_PROMPT = (
    "You map a described data flow to directed agent-to-agent connections. "
    "Only use agent names from the provided list. Return ONLY valid JSON."
)


def connections_from_description(
    description: str, known_agents: list[str]
) -> list[dict[str, str]]:
    """Propose directed (source → target) connections among known agents from
    a description. Filters to real agent names; dedupes. Raises
    APIKeyMissingError when the key is unset."""
    if not known_agents:
        return []
    user_prompt = (
        f"Agents (use ONLY these names): {', '.join(known_agents)}\n\n"
        f"Description:\n{(description or '').strip()}\n\n"
        'Return directed connections as JSON: '
        '{"connections": [{"source": "<agent>", "target": "<agent>"}]}. '
        "source feeds into target. Use ONLY names from the list. Return ONLY JSON."
    )
    parsed = _claude_json(CONNECTIONS_DESC_SYSTEM_PROMPT, user_prompt, max_tokens=800)
    raw = parsed.get("connections") if isinstance(parsed, dict) else None
    if not isinstance(raw, list):
        raw = []
    known = set(known_agents)
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, str]] = []
    for c in raw:
        if not isinstance(c, dict):
            continue
        s, t = c.get("source"), c.get("target")
        if s in known and t in known and s != t and (s, t) not in seen:
            seen.add((s, t))
            out.append({"source": s, "target": t})
    return out


# ---------------------------------------------------------------------------
# Dashboard — daily briefing, needs-attention enrichment, work-feed summaries
# ---------------------------------------------------------------------------


DASHBOARD_BRIEFING_SYSTEM_PROMPT = (
    "You are the operations lead writing a short daily briefing for someone who "
    "manages a fleet of AI agents. Write 2-3 sentences in plain, human prose — "
    "the way a sharp manager would open a standup. Lead with what matters most: "
    "notable changes, problems, or wins. Use the specific numbers you're given. "
    "No bullet points, no headers, no jargon, no markdown. Return ONLY valid JSON."
)


def fleet_briefing(stats: dict[str, Any]) -> dict[str, str]:
    """Generate a 2-3 sentence daily briefing from a fleet snapshot. Returns
    {"summary": str} ("" when Claude gives nothing). Raises APIKeyMissingError
    when ANTHROPIC_API_KEY is unset."""
    import json as _json

    user_prompt = (
        "Fleet snapshot (JSON):\n"
        f"{_json.dumps(stats, default=str)}\n\n"
        'Return JSON: {"summary": "2-3 sentence briefing"}. Return ONLY the JSON.'
    )
    parsed = _claude_json(DASHBOARD_BRIEFING_SYSTEM_PROMPT, user_prompt, max_tokens=400)
    summary = ""
    if isinstance(parsed, dict):
        summary = str(parsed.get("summary") or "").strip()
    return {"summary": summary}


DASHBOARD_ATTENTION_SYSTEM_PROMPT = (
    "You are an SRE-minded analyst for Trovis. For each flagged agent, write a "
    "short title, a one-sentence detail explaining the likely problem, a concrete "
    "recommendation, and a brief impact estimate. Be specific and use the numbers "
    "provided. Plain prose, no markdown. Return ONLY valid JSON.\n"
    "\n"
    "You are writing for a manager, not an engineer reading a database:\n"
    "- NEVER echo a field name from the input. Write 'last active 3 days ago', "
    "never 'days_since_seen of 3'. If a phrase would only make sense to someone "
    "looking at the JSON, rewrite it.\n"
    "- Distinguish TOOL FRICTION from AGENT FAILURE. A handful of failed "
    "operations inside runs that finished is friction — expected, low stakes, "
    "worth a mention at most. An agent that cannot complete its work is a "
    "failure. Say which one this is, and do not describe friction in the "
    "language of an outage.\n"
    "- Respect the severity you are given. It already accounts for how long the "
    "agent has been silent, so do not escalate an 'info' item with urgent "
    "language. An agent nobody has run in months is a housekeeping question, "
    "not an incident.\n"
    "- When an agent has been silent a long time, lead with that fact — it is "
    "usually the real story, not the ratio."
)


def _humanize_days(days: float | None) -> str:
    """Days-since-last-activity as a phrase, so nothing numeric-looking or
    field-shaped can be pasted into manager-facing prose. None -> 'unknown'."""
    if days is None:
        return "unknown"
    if days < 1:
        return "today"
    if days < 2:
        return "yesterday"
    if days < 14:
        return f"{int(round(days))} days ago"
    if days < 60:
        return f"{int(round(days / 7))} weeks ago"
    return f"{int(round(days / 30))} months ago"


def attention_items(flagged: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Enrich flagged agents with title/detail/recommendation/impact in ONE
    Claude call. Returns a list aligned to `flagged` (same order; severity,
    agent and last_seen are preserved from our own classification, never
    Claude's). Enrichment fields fall back to "" when Claude omits them.
    Raises APIKeyMissingError when the key is unset."""
    if not flagged:
        return []
    import json as _json

    # Prose-safe keys. These land verbatim in a manager-facing sentence when
    # the model echoes a field name, and it does: "days_since_seen of 0" was
    # shipping to users. Every key here reads as English if it leaks, and the
    # system prompt forbids echoing them at all. Values are pre-formatted so
    # there is no raw ratio or ISO stamp to paste either.
    payload = [
        {
            "agent": f["agent"],
            "severity": f["severity"],
            "error rate": (
                f"{f.get('error_rate_pct')}%"
                if f.get("error_rate_pct") is not None
                else "unknown"
            ),
            "operations recorded": f.get("span_count"),
            "failed operations": f.get("error_count"),
            "last active": _humanize_days(f.get("days_since_seen")),
            "what it does": f.get("description"),
        }
        for f in flagged
    ]
    user_prompt = (
        "Flagged agents (JSON):\n"
        f"{_json.dumps(payload, default=str)}\n\n"
        'Return JSON: {"items": [{"agent": "<name>", "title": "...", '
        '"detail": "...", "recommendation": "...", "impact": "..."}]}. '
        "One object per flagged agent, echoing its exact agent name. Return ONLY JSON."
    )
    parsed = _claude_json(DASHBOARD_ATTENTION_SYSTEM_PROMPT, user_prompt, max_tokens=1200)
    enriched = parsed.get("items") if isinstance(parsed, dict) else None
    by_agent: dict[str, dict[str, Any]] = {}
    if isinstance(enriched, list):
        for e in enriched:
            if isinstance(e, dict) and e.get("agent"):
                by_agent[str(e["agent"])] = e
    out: list[dict[str, Any]] = []
    for f in flagged:
        e = by_agent.get(f["agent"], {})
        out.append(
            {
                "severity": f["severity"],
                "agent": f["agent"],
                "title": str(e.get("title") or "Needs attention").strip(),
                "detail": str(e.get("detail") or "").strip(),
                "recommendation": str(e.get("recommendation") or "").strip(),
                "impact": str(e.get("impact") or "").strip(),
                "last_seen": f.get("last_seen"),
            }
        )
    return out


DASHBOARD_WORKFEED_SYSTEM_PROMPT = (
    "You summarize what an AI agent recently did, for a non-technical manager. "
    "Write ONE or TWO sentences in plain English describing the actual work — "
    "e.g. 'Triaged 47 support emails and routed 12 to the billing team.' Use the "
    "operations and any captured content as evidence; never just restate span "
    "counts or error rates. No markdown, no jargon. Return ONLY valid JSON."
)


def work_feed_summary(agent_label: str, activity: dict[str, Any]) -> str:
    """One-to-two sentence plain-English summary of an agent's recent work.
    `activity` carries task_count, top operations and captured content samples.
    Returns "" when Claude gives nothing. Raises APIKeyMissingError when unset."""
    import json as _json

    user_prompt = (
        f"Agent: {agent_label}\n"
        "Recent activity (JSON):\n"
        f"{_json.dumps(activity, default=str)}\n\n"
        'Return JSON: {"summary": "1-2 sentence plain-English summary"}. Return ONLY JSON.'
    )
    parsed = _claude_json(DASHBOARD_WORKFEED_SYSTEM_PROMPT, user_prompt, max_tokens=300)
    if isinstance(parsed, dict):
        return str(parsed.get("summary") or "").strip()
    return ""


def loop_title(shape: dict[str, Any]) -> str | None:
    """One short plain-English title for a workloop, from its SHAPE only.

    The shape (see database.get_loop_title_shape) is metadata: agent
    identity, tool names in order, handoff target/direction, duration,
    close reason. Span attribute VALUES are deliberately never included —
    they may carry user content. Returns None on any failure (missing key,
    API error, empty response); the caller falls back to the template
    title. Same client + fail-soft posture as describe_agent.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    tools = shape.get("tools") or []
    tool_line = " -> ".join(tools[:15]) if tools else "(no tools)"
    handoff = shape.get("handoff") or {}
    handoff_line = (
        f"handed to {handoff.get('target') or handoff.get('direction') or 'someone'}"
        + (f" ({handoff['reason']})" if handoff.get("reason") else "")
        if handoff
        else "no handoff"
    )
    dur = shape.get("duration_s")
    dur_line = f"{dur}s" if dur is not None else "still open"
    prompt = (
        "Name this AI-agent work session in ONE short title (max 8 words, "
        "no quotes, no trailing period). Plain English, specific to what "
        "the shape suggests the agent was doing; never say 'session', "
        "'loop', 'telemetry', or 'spans'.\n"
        f"Agent: {shape.get('agent')}\n"
        f"Tools used, in order: {tool_line}\n"
        f"Actions: {shape.get('action_count')}\n"
        f"Handoff: {handoff_line}\n"
        f"Duration: {dur_line}\n"
        f"Outcome: {shape.get('close_reason') or shape.get('state')}\n"
    )
    try:
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=MODEL,
            thinking=THINKING,
            max_tokens=40,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(
            b.text for b in resp.content if getattr(b, "type", "") == "text"
        ).strip().strip('"').strip()
        return text[:120] or None
    except Exception:  # noqa: BLE001 — fail-soft: template fallback covers it
        return None
