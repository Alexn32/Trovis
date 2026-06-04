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


# ---------------------------------------------------------------------------
# Weekly summary + capability map (cached by main.py's endpoints)
# ---------------------------------------------------------------------------


WEEKLY_SYSTEM_PROMPT = (
    "You are an AI analyst for Oversee. Given week-over-week stats for an "
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
        max_tokens=300,
        system=WEEKLY_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )
    return "".join(
        block.text for block in response.content if getattr(block, "type", None) == "text"
    ).strip()


CAPABILITIES_SYSTEM_PROMPT = (
    "You are an AI analyst for Oversee. Based on an agent's "
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
# Workflow generation — infer a step-by-step process from telemetry + identity
# ---------------------------------------------------------------------------


WORKFLOW_SYSTEM_PROMPT = (
    "You are a process analyst for Oversee, an agent management system. You "
    "reconstruct the end-to-end process an AI agent participates in — "
    "including the HUMAN steps around it — from its telemetry and identity "
    "files. Agent steps come from observed tool calls. Human steps are "
    "inferred from long time-gaps after outbound operations (Slack/email/"
    "webhook sends, approval requests) and from identity files that describe "
    "review, approval, handoff, or escalation. Be concrete: use the actual "
    "tool names. Return ONLY valid JSON — no markdown, no prose."
)

_VALID_STEP_TYPES = {"trigger", "agent", "human", "decision", "output"}
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


def _build_workflow_prompt(
    summary: dict[str, Any],
    registration: dict[str, Any] | None,
    analysis: dict[str, Any],
) -> str:
    reg = registration or {}
    identity_block = (
        f"SOUL.md (personality and purpose):\n{(reg.get('soul') or '(none)')[:2000]}\n\n"
        f"IDENTITY.md (role definition):\n{(reg.get('identity') or '(none)')[:2000]}\n\n"
        f"AGENTS.md (operating manual):\n{(reg.get('operating_manual') or '(none)')[:2000]}"
    )

    if analysis["operations"]:
        ops_lines = "\n".join(
            f"- {o['operation']}: {o['calls']} call(s), avg {o['avg_ms']:.0f}ms"
            for o in analysis["operations"]
        )
    else:
        ops_lines = "(no operations observed)"
    seq_block = (
        "\nTypical sequence(s) of operations across recent runs:\n"
        + "\n".join(f"- {s}" for s in analysis["sequences"])
        if analysis["sequences"]
        else ""
    )
    gaps_block = (
        "\n".join(f"- {g}" for g in analysis["gaps"])
        if analysis["gaps"]
        else "(no notable gaps > 30s observed)"
    )

    return (
        f"This agent ({summary['service_name']}) has the following identity and "
        f"configuration:\n{identity_block}\n\n"
        f"Its telemetry shows these operations:\n{ops_lines}{seq_block}\n\n"
        f"Time gaps between consecutive operations:\n{gaps_block}\n\n"
        "Generate a complete workflow showing every step this agent's process "
        "involves — both automated AND human steps. Analyze:\n"
        "1. Tool calls that send to external channels (Slack, email, webhooks) "
        "followed by gaps — these indicate human review or approval\n"
        "2. Identity files that mention human approval, review, handoff, or "
        "escalation processes\n"
        "3. Operations like wait_for_approval, get_response, check_status that "
        "imply external input\n"
        "4. Long gaps (>30s) between fast operations — something external "
        "happened in between\n"
        "5. The overall pattern: what triggers this agent, what sequence does "
        "it follow, where does output go\n\n"
        "Return ONLY valid JSON, no markdown:\n"
        "{\n"
        '  "steps": [\n'
        "    {\n"
        '      "step_type": "trigger|agent|human|decision|output",\n'
        '      "label": "Short title for this step",\n'
        '      "description": "What happens in this step",\n'
        '      "operation": "tool_name if agent step, null otherwise",\n'
        '      "duration_estimate_ms": 2000,\n'
        '      "inferred_from": "telemetry|identity|gap_analysis"\n'
        "    }\n"
        "  ]\n"
        "}\n\n"
        "Be specific. Use actual tool names from the telemetry. Infer human "
        "steps from gaps and identity files. Mark each step with how you "
        "inferred it."
    )


def generate_workflow(
    service_name: str,
    account_id: int | None = None,
    agent_id: str | None = None,
) -> list[dict[str, Any]]:
    """Analyze an agent's telemetry + identity and ask Claude to reconstruct
    its full process as an ordered list of steps (agent + inferred human).

    Returns a list of step dicts ({step_type, label, description, operation,
    duration_estimate_ms, inferred_from}). Raises AgentNotFoundError when the
    agent has no telemetry, APIKeyMissingError when the key is unset.
    """
    summary = database.get_agent_summary(
        service_name, account_id=account_id, agent_id=agent_id
    )
    if summary is None:
        raise AgentNotFoundError(service_name)

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise APIKeyMissingError(
            "ANTHROPIC_API_KEY is not set. Export it before generating workflows."
        )

    spans = database.get_agent_spans(
        service_name, limit=200, account_id=account_id, agent_id=agent_id
    )
    registration = database.get_latest_registration(
        service_name, account_id=account_id, agent_id=agent_id
    )
    analysis = _analyze_telemetry(spans)
    user_prompt = _build_workflow_prompt(summary, registration, analysis)

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        system=WORKFLOW_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )
    raw = "".join(
        block.text for block in response.content if getattr(block, "type", None) == "text"
    ).strip()

    # Tolerate ```json fences.
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    import json as _json

    try:
        parsed = _json.loads(raw)
    except (TypeError, ValueError):
        parsed = {}

    raw_steps = parsed.get("steps") if isinstance(parsed, dict) else None
    if not isinstance(raw_steps, list):
        raw_steps = []

    steps: list[dict[str, Any]] = []
    for s in raw_steps:
        if not isinstance(s, dict):
            continue
        step_type = str(s.get("step_type") or "agent").strip().lower()
        if step_type not in _VALID_STEP_TYPES:
            step_type = "agent"
        label = str(s.get("label") or "").strip()
        if not label:
            continue
        dur = s.get("duration_estimate_ms")
        try:
            dur = int(dur) if dur is not None else None
        except (TypeError, ValueError):
            dur = None
        inferred = str(s.get("inferred_from") or "telemetry").strip().lower()
        if inferred not in {"telemetry", "identity", "gap_analysis", "manual"}:
            inferred = "telemetry"
        steps.append(
            {
                "step_type": step_type,
                "label": label[:200],
                "description": (str(s["description"]) if s.get("description") else None),
                "operation": (str(s["operation"]) if s.get("operation") else None),
                "duration_estimate_ms": dur,
                "inferred_from": inferred,
                # Carry the agent identity onto agent steps so the UI can pill it.
                "agent_service_name": service_name if step_type == "agent" else None,
                "agent_id": (agent_id or "main") if step_type == "agent" else None,
            }
        )
    return steps


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


WORKFLOW_DESC_SYSTEM_PROMPT = (
    "You are a process analyst for Oversee. Turn a plain-English description "
    "of how work flows into an ordered list of workflow steps — both "
    "automated (agent) and human steps. Reference the provided agent names "
    "when relevant. Mark review/approval/handoff steps as 'human'. Return "
    "ONLY valid JSON, no markdown."
)


def workflow_from_description(
    description: str, known_agents: list[str] | None = None
) -> list[dict[str, Any]]:
    """Draft workflow steps from a natural-language description. Raises
    APIKeyMissingError when the key is unset."""
    agents_line = (
        "Known agents you can reference: " + ", ".join(known_agents)
        if known_agents
        else "No known agents — describe steps generically."
    )
    user_prompt = (
        f"{agents_line}\n\n"
        f"Process description:\n{(description or '').strip()}\n\n"
        "Produce the workflow as JSON, ordered logically (a trigger first and "
        "an output last when sensible):\n"
        "{\n"
        '  "steps": [\n'
        '    {"step_type": "trigger|agent|human|decision|output", '
        '"label": "short title", "description": "what happens", '
        '"operation": "tool name if an agent step, else null", '
        '"duration_estimate_ms": 2000}\n'
        "  ]\n"
        "}\n"
        "Return ONLY the JSON."
    )
    parsed = _claude_json(WORKFLOW_DESC_SYSTEM_PROMPT, user_prompt)
    raw_steps = parsed.get("steps") if isinstance(parsed, dict) else None
    if not isinstance(raw_steps, list):
        raw_steps = []

    steps: list[dict[str, Any]] = []
    for s in raw_steps:
        if not isinstance(s, dict):
            continue
        step_type = str(s.get("step_type") or "agent").strip().lower()
        if step_type not in _VALID_STEP_TYPES:
            step_type = "agent"
        label = str(s.get("label") or "").strip()
        if not label:
            continue
        dur = s.get("duration_estimate_ms")
        try:
            dur = int(dur) if dur is not None else None
        except (TypeError, ValueError):
            dur = None
        steps.append(
            {
                "step_type": step_type,
                "label": label[:200],
                "description": (str(s["description"]) if s.get("description") else None),
                "operation": (str(s["operation"]) if s.get("operation") else None),
                "duration_estimate_ms": dur,
                "inferred_from": "manual",  # operator-described, not telemetry
            }
        )
    return steps


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
    "You are an SRE-minded analyst for Oversee. For each flagged agent, write a "
    "short title, a one-sentence detail explaining the likely problem, a concrete "
    "recommendation, and a brief impact estimate. Be specific and use the numbers "
    "provided. Plain prose, no markdown. Return ONLY valid JSON."
)


def attention_items(flagged: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Enrich flagged agents with title/detail/recommendation/impact in ONE
    Claude call. Returns a list aligned to `flagged` (same order; severity,
    agent and last_seen are preserved from our own classification, never
    Claude's). Enrichment fields fall back to "" when Claude omits them.
    Raises APIKeyMissingError when the key is unset."""
    if not flagged:
        return []
    import json as _json

    payload = [
        {
            "agent": f["agent"],
            "severity": f["severity"],
            "error_rate_pct": f.get("error_rate_pct"),
            "span_count": f.get("span_count"),
            "error_count": f.get("error_count"),
            "days_since_seen": f.get("days_since_seen"),
            "description": f.get("description"),
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
