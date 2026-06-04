"""Claude Agent SDK support for Oversee.

The Claude Agent SDK (`claude-agent-sdk`) drives the Claude Code engine
via `query()` and `ClaudeSDKClient` — this is NOT the
`anthropic.beta.agents` Managed Agents API (that's `oversee/anthropic.py`).
The two are easy to confuse; they're different products with different
entry points.

This adapter wraps `claude_agent_sdk.query()` so each run's message
stream becomes Oversee spans, using the same vocabulary as every other
platform:

    options.system_prompt        → agent_registration (once per agent)
    UserMessage                  → message_received
    AssistantMessage (text)      → llm_output
    AssistantMessage ToolUseBlock→ tool_call
    ResultMessage                → agent_run_complete + token usage/cost

`query()` returns an async iterator, so we wrap it in our own async
generator that yields each message through untouched while emitting a
span on the side. The caller's `async for` loop is unaffected.

Activation: `init(platform="claude-agent-sdk")` (or "auto"). Like the
other monkey-patch adapters, init() must run BEFORE the user imports
`query` — `from claude_agent_sdk import query` binds the name at import
time, so patching the module afterward wouldn't reach an already-bound
reference.
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Optional

from opentelemetry import trace
from opentelemetry.trace import StatusCode

from oversee.registration import is_capture_enabled

logger = logging.getLogger("oversee.claude_agent_sdk")

_PATCHED = False
_ORIGINALS: dict[str, Any] = {}
_REGISTERED: set[str] = set()

_CONTENT_BYTE_LIMIT = 10_000
_SOUL_BYTE_LIMIT = 32 * 1024


# ---------------------------------------------------------------------------
# Activation
# ---------------------------------------------------------------------------


def setup_claude_agent_sdk() -> bool:
    """Monkey-patch the Claude Agent SDK to emit Oversee spans. Idempotent.

    Patches `claude_agent_sdk.query` (the primary entry point) and, when
    present, `ClaudeSDKClient.receive_response` (the streaming entry
    point) — both yield the same message types, so one handler covers
    them. Returns True when at least one patch landed; False (with a
    warning) when the SDK isn't installed.
    """
    global _PATCHED
    if _PATCHED:
        return True

    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError:
        logger.warning(
            "[Oversee] claude-agent-sdk not installed — Claude Agent SDK "
            "instrumentation is disabled. Install with: "
            "pip install claude-agent-sdk"
        )
        return False

    patched_any = _patch_query() | _patch_client_stream()
    if not patched_any:
        logger.warning(
            "[Oversee] claude-agent-sdk present but neither query() nor "
            "ClaudeSDKClient.receive_response could be patched — the SDK's "
            "shape is unexpected."
        )
        return False

    _PATCHED = True
    return True


def _patch_query() -> bool:
    import claude_agent_sdk

    original = getattr(claude_agent_sdk, "query", None)
    if original is None:
        return False
    _ORIGINALS["query"] = original

    def patched_query(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        # Calling the original async-gen function returns the (lazy)
        # async iterator. We wrap it so each message is observed.
        inner = original(*args, **kwargs)
        return _instrumented_stream(inner, kwargs)

    claude_agent_sdk.query = patched_query  # type: ignore[attr-defined]
    return True


def _patch_client_stream() -> bool:
    """Best-effort: wrap ClaudeSDKClient.receive_response so streaming
    sessions are instrumented too. Skipped silently if the class or
    method isn't there."""
    try:
        import claude_agent_sdk

        cls = getattr(claude_agent_sdk, "ClaudeSDKClient", None)
        if cls is None or not hasattr(cls, "receive_response"):
            return False
        original = cls.receive_response
        _ORIGINALS["receive_response"] = original

        def patched(self: Any, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
            inner = original(self, *args, **kwargs)
            return _instrumented_stream(inner, {})

        cls.receive_response = patched  # type: ignore[method-assign]
        return True
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[Oversee] could not patch ClaudeSDKClient: {e}")
        return False


# ---------------------------------------------------------------------------
# Stream instrumentation
# ---------------------------------------------------------------------------


async def _instrumented_stream(
    inner: AsyncIterator[Any], call_kwargs: dict[str, Any]
) -> AsyncIterator[Any]:
    """Yield every message from the underlying iterator while emitting a
    span per message. Span failures are swallowed — the user's loop
    keeps receiving messages no matter what.

    `last_model` is threaded across messages so the ResultMessage (which
    carries the run's token totals but not the model) can be tagged with
    the model seen on the AssistantMessages.
    """
    _maybe_register(call_kwargs)
    agent_name = _agent_name(call_kwargs)
    state: dict[str, Any] = {"last_model": None, "session_id": None}

    async for message in inner:
        try:
            _emit_for_message(message, agent_name, state)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[Oversee] message span emit failed: {e}")
        yield message


def _emit_for_message(
    message: Any, agent_name: str, state: dict[str, Any]
) -> None:
    kind = type(message).__name__
    tracer = trace.get_tracer("oversee.claude_agent_sdk")

    if kind == "SystemMessage":
        # init system message carries session_id + model + tools.
        data = _get(message, "data") or {}
        if isinstance(data, dict):
            sid = data.get("session_id")
            if sid:
                state["session_id"] = sid
            model = data.get("model")
            if model:
                state["last_model"] = model
        return

    run_id = state.get("session_id") or ""

    if kind == "UserMessage":
        text = _extract_text(_get(message, "content"))
        with tracer.start_as_current_span("message_received") as span:
            span.set_attribute("oversee.event.type", "message_received")
            span.set_attribute("oversee.agent.id", agent_name)
            if run_id:
                span.set_attribute("oversee.run.id", run_id)
            if text:
                span.set_attribute("oversee.message.content_length", len(text))
                if is_capture_enabled():
                    span.set_attribute(
                        "oversee.message.content",
                        _truncate(text, _CONTENT_BYTE_LIMIT),
                    )

    elif kind == "AssistantMessage":
        model = _get(message, "model")
        if model:
            state["last_model"] = model
        content = _get(message, "content") or []
        text_parts: list[str] = []
        for block in content if isinstance(content, list) else []:
            btype = type(block).__name__
            if btype == "TextBlock":
                t = _get(block, "text")
                if isinstance(t, str):
                    text_parts.append(t)
            elif btype == "ToolUseBlock":
                _emit_tool_use(tracer, block, agent_name, run_id)
        text = "\n".join(text_parts)
        if text:
            with tracer.start_as_current_span("llm_output") as span:
                span.set_attribute("oversee.event.type", "llm_output")
                span.set_attribute("oversee.agent.id", agent_name)
                if run_id:
                    span.set_attribute("oversee.run.id", run_id)
                if model:
                    span.set_attribute("gen_ai.request.model", str(model))
                span.set_attribute("oversee.response.content_length", len(text))
                if is_capture_enabled():
                    span.set_attribute(
                        "oversee.response.content",
                        _truncate(text, _CONTENT_BYTE_LIMIT),
                    )

    elif kind == "ResultMessage":
        _emit_result(tracer, message, agent_name, state)


def _emit_tool_use(
    tracer: Any, block: Any, agent_name: str, run_id: str
) -> None:
    with tracer.start_as_current_span("tool_call") as span:
        span.set_attribute("oversee.event.type", "tool_call")
        span.set_attribute("oversee.agent.id", agent_name)
        if run_id:
            span.set_attribute("oversee.run.id", run_id)
        name = _get(block, "name")
        if name:
            span.set_attribute("oversee.tool.name", str(name))
        bid = _get(block, "id")
        if bid:
            span.set_attribute("oversee.tool.call_id", str(bid))
        # Param KEYS only by default (values may carry user data).
        tool_input = _get(block, "input")
        if isinstance(tool_input, dict):
            import json as _json

            span.set_attribute(
                "oversee.tool.param_keys", _json.dumps(list(tool_input.keys()))
            )


def _emit_result(
    tracer: Any, message: Any, agent_name: str, state: dict[str, Any]
) -> None:
    sid = _get(message, "session_id") or state.get("session_id") or ""
    if sid:
        state["session_id"] = sid
    is_error = bool(_get(message, "is_error"))

    with tracer.start_as_current_span("agent_run_complete") as span:
        span.set_attribute("oversee.event.type", "agent_run_complete")
        span.set_attribute("oversee.agent.id", agent_name)
        if sid:
            span.set_attribute("oversee.run.id", sid)
        span.set_attribute("oversee.run.success", not is_error)
        if is_error:
            span.set_status(StatusCode.ERROR)

        # Token usage → cost. The model came off the AssistantMessages.
        # Cached input tokens are billed at different rates than plain input,
        # so surface them separately for accurate cost.
        inp, out, tot, cache_create, cache_read = _extract_usage(
            _get(message, "usage")
        )
        if tot is not None:
            model = state.get("last_model")
            if model:
                span.set_attribute("gen_ai.request.model", str(model))
            if inp is not None:
                span.set_attribute("gen_ai.usage.input_tokens", inp)
            if out is not None:
                span.set_attribute("gen_ai.usage.output_tokens", out)
            span.set_attribute("gen_ai.usage.total_tokens", tot)
            if cache_create is not None:
                span.set_attribute(
                    "gen_ai.usage.cache_creation_input_tokens", cache_create
                )
            if cache_read is not None:
                span.set_attribute(
                    "gen_ai.usage.cache_read_input_tokens", cache_read
                )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def _maybe_register(call_kwargs: dict[str, Any]) -> None:
    """Emit one agent_registration span per unique (agent_name,
    system_prompt). The system prompt is the agent's identity, read off
    the ClaudeAgentOptions passed to query()."""
    agent_name = _agent_name(call_kwargs)
    soul = _system_prompt(call_kwargs)
    model = _options_model(call_kwargs)

    import hashlib

    sig = hashlib.sha256(
        f"{agent_name}|{soul}".encode("utf-8")
    ).hexdigest()[:16]
    if sig in _REGISTERED:
        return
    _REGISTERED.add(sig)

    tracer = trace.get_tracer("oversee.claude_agent_sdk")
    span = tracer.start_span("agent_registration")
    try:
        span.set_attribute("oversee.event.type", "agent_registration")
        span.set_attribute("oversee.agent.id", agent_name)
        if soul:
            span.set_attribute("oversee.agent.soul", _truncate(soul, _SOUL_BYTE_LIMIT))
        span.set_attribute("oversee.agent.identity", agent_name)
        span.set_attribute("oversee.agent.platform", "claude-agent-sdk")
        if model:
            span.set_attribute("oversee.agent.model", str(model))
        span.set_attribute("oversee.agent.workspace_path", "")
    finally:
        span.end()


def _agent_name(call_kwargs: dict[str, Any]) -> str:
    """The per-span agent id. Claude Agent SDK runs are single-agent in
    practice, so default to 'main' — the OTEL resource service.name
    (set from init's agent_name) is what distinguishes instances."""
    return "main"


def _system_prompt(call_kwargs: dict[str, Any]) -> str:
    opts = call_kwargs.get("options")
    sp = _get(opts, "system_prompt")
    if isinstance(sp, str):
        return sp
    # system_prompt can be a preset dict: {"type":"preset","preset":...,
    # "append":"..."}. Capture the append text, which is the
    # user-authored part.
    if isinstance(sp, dict):
        return str(sp.get("append") or "")
    return ""


def _options_model(call_kwargs: dict[str, Any]) -> str:
    opts = call_kwargs.get("options")
    model = _get(opts, "model")
    return str(model) if model else ""


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------


def _get(obj: Any, key: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _extract_usage(
    usage: Any,
) -> tuple[int | None, int | None, int | None, int | None, int | None]:
    """Normalize a Claude Agent SDK usage payload to (input, output, total,
    cache_creation, cache_read). ResultMessage.usage is a dict with
    input_tokens / output_tokens plus cache_creation_input_tokens /
    cache_read_input_tokens (billed at cache multipliers, and NOT included in
    input_tokens). `total` counts all four billed token kinds."""
    if usage is None:
        return (None, None, None, None, None)

    def _int(v: Any) -> int | None:
        try:
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    inp = _int(_get(usage, "input_tokens"))
    out = _int(_get(usage, "output_tokens"))
    tot = _int(_get(usage, "total_tokens"))
    cache_create = _int(_get(usage, "cache_creation_input_tokens"))
    cache_read = _int(_get(usage, "cache_read_input_tokens"))
    if tot is None and (
        inp is not None or out is not None or cache_create is not None or cache_read is not None
    ):
        tot = (inp or 0) + (out or 0) + (cache_create or 0) + (cache_read or 0)
    return (inp, out, tot, cache_create, cache_read)


def _extract_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if type(block).__name__ == "TextBlock":
                t = _get(block, "text")
                if isinstance(t, str):
                    parts.append(t)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
        return "\n".join(parts)
    return ""


def _truncate(s: str, limit: int) -> str:
    if not s:
        return ""
    encoded = s.encode("utf-8")
    if len(encoded) <= limit:
        return s
    return encoded[:limit].decode("utf-8", errors="ignore") + "…[truncated]"


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _is_patched() -> bool:
    return _PATCHED


def _reset_for_tests() -> None:
    global _PATCHED
    try:
        import claude_agent_sdk

        if "query" in _ORIGINALS:
            claude_agent_sdk.query = _ORIGINALS["query"]
        if "receive_response" in _ORIGINALS:
            cls = getattr(claude_agent_sdk, "ClaudeSDKClient", None)
            if cls is not None:
                cls.receive_response = _ORIGINALS["receive_response"]
    except Exception:  # noqa: BLE001
        pass
    _ORIGINALS.clear()
    _REGISTERED.clear()
    _PATCHED = False
