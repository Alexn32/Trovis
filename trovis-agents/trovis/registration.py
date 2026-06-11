"""Agent identity capture + content-capture flag.

Mirrors the OpenClaw plugin's `agent_registration` span. Every time the
host code constructs an `agents.Agent(...)` instance we emit one
registration span carrying:
  - trovis.agent.id          — Agent.name
  - trovis.agent.soul        — Agent.instructions (the system prompt;
                                 this IS the agent's identity)
  - trovis.agent.identity    — name + handoff_description when present
  - trovis.agent.model       — Agent.model
  - trovis.agent.workspace_path — empty (no filesystem workspace for
                                    SDK agents; kept for schema parity
                                    with the OpenClaw plugin)

We dedup by (name, instructions) hash so re-constructing the same
Agent across requests doesn't spam the dashboard. Different
instructions for the same name re-register so configuration changes
are visible.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from opentelemetry import trace

logger = logging.getLogger("trovis")

# 32 KB matches the OpenClaw plugin's truncation budget — comfortably
# under OTLP's per-attribute size limits and large enough for almost
# every realistic system prompt.
_ATTR_BYTE_LIMIT = 32 * 1024

# Deduper. Cleared on process restart, which is when we'd want to
# re-register anyway.
_REGISTERED: set[str] = set()
_PATCHED = False
_CAPTURE_OUTPUTS = False


def set_capture_outputs(enabled: bool) -> None:
    """Module-level flag read by the capture processor on every event."""
    global _CAPTURE_OUTPUTS
    _CAPTURE_OUTPUTS = bool(enabled)


def is_capture_enabled() -> bool:
    return _CAPTURE_OUTPUTS


def _truncate(s: str, limit: int = _ATTR_BYTE_LIMIT) -> str:
    """Truncate a string to fit within OTLP attribute byte limits.
    Splits at the byte boundary and decodes safely so we don't produce
    invalid UTF-8 mid-multibyte-sequence."""
    if not s:
        return ""
    encoded = s.encode("utf-8")
    if len(encoded) <= limit:
        return s
    return encoded[:limit].decode("utf-8", errors="ignore") + "…[truncated]"


def _agent_signature(name: str, instructions: str) -> str:
    """Stable per-(name, instructions) identity. Hash both so that
    a config change (different system prompt for the same name)
    re-registers, while repeated construction with the same
    parameters is idempotent."""
    h = hashlib.sha256()
    h.update((name or "").encode("utf-8"))
    h.update(b"|")
    h.update((instructions or "").encode("utf-8"))
    return h.hexdigest()[:16]


def _resolve_instructions(value: Any) -> str:
    """Agent.instructions can be a string OR a callable (dynamic
    instructions resolved at run time). We only register the static
    case — for callables we'd have to call them with a phony context,
    which has side effects."""
    if isinstance(value, str):
        return value
    return ""


def _resolve_model(value: Any) -> str:
    """`Agent.model` may be a string OR a model settings object.
    Stringify it and trim — empty string falls back to 'default'."""
    if value is None:
        return "default"
    text = str(value).strip()
    return text or "default"


def emit_registration_span(
    name: str,
    instructions: str,
    model: str,
    description: str = "",
) -> None:
    """Emit one `agent_registration` span. Safe to call without an
    active span context — the tracer creates a root span."""
    tracer = trace.get_tracer("trovis.registration")
    span = tracer.start_span("agent_registration")
    try:
        span.set_attribute("trovis.event.type", "agent_registration")
        span.set_attribute("trovis.agent.id", name or "main")
        if instructions:
            span.set_attribute("trovis.agent.soul", _truncate(instructions))
        identity = "\n".join(p for p in (name, description) if p).strip()
        if identity:
            span.set_attribute("trovis.agent.identity", _truncate(identity))
        span.set_attribute("trovis.agent.model", model or "default")
        span.set_attribute("trovis.agent.workspace_path", "")
    finally:
        span.end()


def patch_agent_for_registration() -> None:
    """Monkey-patch `agents.Agent.__init__` so we emit a registration
    span the first time each unique agent is constructed.

    Idempotent — calling init() twice (or three times) won't double-
    wrap the method. Silent no-op if the OpenAI Agents SDK isn't
    installed.
    """
    global _PATCHED
    if _PATCHED:
        return

    try:
        from agents import Agent
    except ImportError:
        logger.warning(
            "[Trovis] OpenAI Agents SDK not found. Telemetry will still "
            "flow for manually-traced spans, but agent registration is "
            "skipped. Install with: pip install openai-agents"
        )
        return

    original_init = Agent.__init__

    def wrapped_init(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        # Best effort. Registration failure must never break the agent.
        try:
            name = getattr(self, "name", "") or ""
            instructions = _resolve_instructions(getattr(self, "instructions", ""))
            model = _resolve_model(getattr(self, "model", None))
            description = getattr(self, "handoff_description", "") or ""

            sig = _agent_signature(name, instructions)
            if sig in _REGISTERED:
                return
            _REGISTERED.add(sig)

            emit_registration_span(name, instructions, model, description)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[Trovis] Skipped registration for agent: {e}")

    Agent.__init__ = wrapped_init  # type: ignore[method-assign]
    _PATCHED = True


# ---------------------------------------------------------------------------
# Output capture — observe agent run events and add Trovis-named
# attributes / spans so the dashboard can show real conversation
# content when the operator has opted in.
# ---------------------------------------------------------------------------


# Matches the OpenClaw plugin's per-content truncation budget.
_CONTENT_BYTE_LIMIT = 10_000


def _json_safe(value: Any) -> str:
    """Best-effort stringification for tool inputs/outputs. Strings
    pass through; everything else gets JSON-serialized. Falls back to
    repr() on non-JSON-serializable objects so we always return
    *something* rather than failing the span."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        try:
            return repr(value)
        except Exception:  # noqa: BLE001
            return ""


def _coerce_int(v: Any) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _usage_field(usage: Any, *names: str) -> int | None:
    """First present integer field across `names`, reading either a dict or an
    attribute-style object (the Agents SDK uses both shapes)."""
    if usage is None:
        return None
    for n in names:
        v = usage.get(n) if isinstance(usage, dict) else getattr(usage, n, None)
        iv = _coerce_int(v)
        if iv is not None:
            return iv
    return None


def _extract_response_usage(sd: Any) -> tuple[int | None, int | None, int | None, str | None]:
    """Pull token usage + model id off an OpenAI Agents SDK model span.

    ResponseSpanData carries a Responses-API `Response` on `.response`
    (`.response.usage` → input_tokens / output_tokens / total_tokens, and
    `.response.model`); GenerationSpanData (chat-completions style) carries
    `.usage` + `.model` directly, where usage keys may be input/output_tokens
    or prompt/completion_tokens. Returns (input, output, total, model).

    Note on caching: the Responses API folds cached prompt tokens INTO
    `input_tokens` (unlike Anthropic, which reports them separately), so we do
    NOT emit a cache_read attribute — that would double-count. Cached input is
    therefore priced at the full input rate (slightly conservative).
    """
    resp = getattr(sd, "response", None)
    if resp is not None:
        usage = getattr(resp, "usage", None)
        model = getattr(resp, "model", None)
    else:
        usage = getattr(sd, "usage", None)
        model = getattr(sd, "model", None)

    inp = _usage_field(usage, "input_tokens", "prompt_tokens")
    out = _usage_field(usage, "output_tokens", "completion_tokens")
    tot = _usage_field(usage, "total_tokens")
    if tot is None and (inp is not None or out is not None):
        tot = (inp or 0) + (out or 0)
    return (inp, out, tot, str(model) if model else None)


class CaptureProcessor:
    """OpenAI Agents SDK tracing processor that emits Trovis-named
    OTEL spans carrying actual content when capture-outputs is on.

    Listens to FunctionSpanData (tool calls) and ResponseSpanData /
    GenerationSpanData (LLM input/output). Emits one OTEL span per
    captured event so the existing /agents/{name}/outputs endpoint
    finds them just like it finds OpenClaw plugin output spans.
    """

    def __init__(self) -> None:
        self._tracer = trace.get_tracer("trovis.capture")

    # The OpenAI Agents SDK's TracingProcessor interface accepts these
    # four methods. We only care about `on_span_end` — that's when
    # the input/output values are fully populated.
    def on_trace_start(self, trace_obj: Any) -> None:  # noqa: ARG002
        pass

    def on_trace_end(self, trace_obj: Any) -> None:  # noqa: ARG002
        pass

    def on_span_start(self, span: Any) -> None:  # noqa: ARG002
        pass

    def on_span_end(self, span: Any) -> None:
        # NOTE: we do NOT early-return on capture-disabled here. Token usage +
        # model id are billing metadata (not content), so cost tracking must
        # work regardless of the capture-outputs flag — matching the Claude
        # Agent SDK path. Only message/response/tool *content* is gated below.
        try:
            self._handle(span)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[Trovis] capture failed: {e}")

    def shutdown(self) -> None:
        pass

    def force_flush(self) -> None:
        pass

    # ----- internal -----

    def _handle(self, sdk_span: Any) -> None:
        sd = getattr(sdk_span, "span_data", None)
        if sd is None:
            return
        kind = type(sd).__name__
        capture = is_capture_enabled()

        if kind == "FunctionSpanData":
            # Tool results are content (no tokens) — gated on capture.
            if capture:
                self._emit_tool(sd)
        elif kind in ("ResponseSpanData", "GenerationSpanData"):
            self._emit_model_io(sd, kind, capture)

    def _emit_tool(self, sd: Any) -> None:
        name = getattr(sd, "name", "") or ""
        result = getattr(sd, "output", None)
        if result is None:
            return
        content = _json_safe(result)
        if not content:
            return
        with self._tracer.start_as_current_span("tool_call") as s:
            s.set_attribute("trovis.event.type", "tool_call")
            if name:
                s.set_attribute("trovis.tool.name", name)
            s.set_attribute(
                "trovis.tool.result",
                _truncate(content, _CONTENT_BYTE_LIMIT),
            )

    def _emit_model_io(self, sd: Any, kind: str, capture: bool) -> None:
        # One model call surfaces input + output on the same SDK span. We emit
        # a `message_received` span for the prompt and an `llm_output` span for
        # the response (matching the OpenClaw plugin's separation). The
        # `llm_output` span ALWAYS carries token usage + model id (billing
        # metadata, so cost tracking works even when capture is off); the
        # message/response *content* is attached only when capture is on.
        inp, out, tot, model = _extract_response_usage(sd)
        has_usage = inp is not None or out is not None or tot is not None

        input_value = getattr(sd, "input", None)
        output_value = getattr(sd, "output", None) or getattr(sd, "response", None)

        # message_received — prompt content only (no tokens). Capture-gated.
        if capture and input_value is not None:
            text = _json_safe(input_value)
            if text:
                with self._tracer.start_as_current_span("message_received") as s:
                    s.set_attribute("trovis.event.type", "message_received")
                    s.set_attribute(
                        "trovis.message.content",
                        _truncate(text, _CONTENT_BYTE_LIMIT),
                    )
                    s.set_attribute("trovis.message.source", kind)

        # llm_output — usage + model always; response content only when on.
        out_text = ""
        if capture and output_value is not None:
            out_text = _json_safe(output_value)
        if has_usage or out_text:
            with self._tracer.start_as_current_span("llm_output") as s:
                s.set_attribute("trovis.event.type", "llm_output")
                if model:
                    s.set_attribute("gen_ai.request.model", model)
                if inp is not None:
                    s.set_attribute("gen_ai.usage.input_tokens", inp)
                if out is not None:
                    s.set_attribute("gen_ai.usage.output_tokens", out)
                if tot is not None:
                    s.set_attribute("gen_ai.usage.total_tokens", tot)
                if out_text:
                    s.set_attribute(
                        "trovis.response.content",
                        _truncate(out_text, _CONTENT_BYTE_LIMIT),
                    )
                    s.set_attribute("trovis.response.source", kind)
