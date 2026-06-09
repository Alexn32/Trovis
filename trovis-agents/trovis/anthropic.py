"""Anthropic Claude Managed Agents support for Trovis.

The Anthropic SDK exposes `client.beta.agents.create(...)`,
`client.beta.sessions.create(...)`, and `client.beta.sessions.stream(...)`.
This module hooks each one to emit Trovis-named OTEL spans matching
the rest of the dashboard's vocabulary:

  - agents.create()        → agent_registration span (soul, model, tools)
  - sessions.create()      → records session_id → agent_name mapping
  - sessions.stream()      → wraps the event iterator, one span per
                             event (message_received, message_sent,
                             tool_call, agent_run_complete)

Three ways to activate, in increasing invasiveness:

  1. `init(platform="anthropic")` (or `"auto"`) — calls
     `setup_anthropic()`, which monkey-patches the Anthropic SDK's
     resource classes. Every `anthropic.Anthropic()` client built
     after that gets instrumented automatically. Most convenient.
  2. `monitor(client)` — applies instrumentation to a single client
     instance without touching the class. Use when monkey-patching
     fails (e.g. the SDK version shipped a renamed resource path).
  3. `track_session(session_id, agent_name)` — context manager that
     just registers a session_id → agent_name mapping; requires one
     of the above to actually emit per-event spans.

All paths fail soft: if `anthropic` isn't installed, or the SDK ships
under different class paths than expected, we log a warning and the
rest of Trovis keeps working.
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from opentelemetry import trace
from opentelemetry.trace import StatusCode

from trovis.registration import is_capture_enabled

logger = logging.getLogger("trovis.anthropic")

# Module state. setup_anthropic() is idempotent — these guard the
# patches from being applied twice.
_PATCHED = False
_ORIGINAL_METHODS: dict[str, Any] = {}

# session_id → agent_name. Populated by sessions.create(). Read by
# the stream wrapper so each event span knows which agent emitted it.
# We also stash agent.id → agent.name from agents.create() so we can
# resolve sessions that pass `agent=agent.id` rather than a name.
_SESSION_TO_AGENT: dict[str, str] = {}
_AGENT_ID_TO_NAME: dict[str, str] = {}

# Model tracking, so token-usage spans can carry gen_ai.request.model
# (the backend needs both usage + model to compute cost). agents.create
# records agent.id → model; sessions.create resolves session → model.
_AGENT_ID_TO_MODEL: dict[str, str] = {}
_SESSION_TO_MODEL: dict[str, str] = {}

# Span-content truncation. Same budget as the OpenClaw plugin.
_CONTENT_BYTE_LIMIT = 10_000


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def setup_anthropic() -> bool:
    """Monkey-patch the anthropic SDK to emit Trovis spans. Idempotent.

    Returns True when at least one patch was applied. Returns False —
    with a warning logged — when anthropic isn't installed or the
    expected resource classes aren't where we look. Callers can treat
    the False return as "fall back to monitor()" rather than abort.
    """
    global _PATCHED
    if _PATCHED:
        return True

    try:
        import anthropic  # noqa: F401
    except ImportError:
        logger.warning(
            "[Trovis] anthropic SDK not installed — Claude Managed "
            "Agents instrumentation is disabled. Install with: "
            "pip install anthropic"
        )
        return False

    # Patch the three resource classes. Each helper is independent so
    # a missing class for one resource doesn't disable the others.
    a = _patch_agents_create()
    s = _patch_sessions_create()
    t = _patch_sessions_stream()

    if not (a or s or t):
        logger.warning(
            "[Trovis] Could not locate any anthropic.beta resource "
            "classes to patch. Falling back to monitor(client) per "
            "instance is recommended."
        )
        return False

    _PATCHED = True
    return True


def monitor(client: Any) -> Any:
    """Apply Trovis instrumentation to a single Anthropic client.

    Useful when class-level monkey-patching is undesirable (multiple
    clients with different telemetry needs) or fragile (SDK version
    moved the resource classes). Mutates the client in place and
    returns it so callers can chain: `client = monitor(anthropic.Anthropic())`.
    """
    try:
        beta = client.beta
        agents = beta.agents
        sessions = beta.sessions
    except AttributeError as e:
        logger.warning(
            f"[Trovis] monitor(): client.beta.{{agents,sessions}} not "
            f"found — anthropic SDK shape unexpected ({e}). Returning "
            f"client unchanged."
        )
        return client

    # We bind the method on the instance — this shadows the class
    # method just for this client.
    _wrap_instance_method(agents, "create", _agent_create_wrapper)
    _wrap_instance_method(sessions, "create", _session_create_wrapper)
    _wrap_instance_method(sessions, "stream", _session_stream_wrapper)

    return client


@contextmanager
def track_session(session_id: str, agent_name: str = "main") -> Iterator[None]:
    """Register a session_id → agent_name mapping for the duration of
    the block. Spans emitted by the stream wrapper inside this block
    are tagged with `trovis.agent.id = agent_name`.

    Requires `setup_anthropic()` or `monitor(client)` to have been
    called — track_session alone does not emit per-event spans.
    """
    previous = _SESSION_TO_AGENT.get(session_id)
    _SESSION_TO_AGENT[session_id] = agent_name
    try:
        yield
    finally:
        if previous is None:
            _SESSION_TO_AGENT.pop(session_id, None)
        else:
            _SESSION_TO_AGENT[session_id] = previous


# ---------------------------------------------------------------------------
# Class-level monkey-patching helpers
# ---------------------------------------------------------------------------


def _patch_agents_create() -> bool:
    cls = _resolve_class(
        "anthropic.resources.beta.agents",
        "Agents",
        fallback_modules=("anthropic.resources.beta.agents.agents",),
    )
    if cls is None or not hasattr(cls, "create"):
        return False
    original = cls.create
    _ORIGINAL_METHODS[("Agents", "create")] = original

    def patched(self: Any, *args: Any, **kwargs: Any) -> Any:
        result = original(self, *args, **kwargs)
        _safe(_record_agent, result, kwargs)
        return result

    cls.create = patched  # type: ignore[method-assign]
    return True


def _patch_sessions_create() -> bool:
    cls = _resolve_class(
        "anthropic.resources.beta.sessions",
        "Sessions",
        fallback_modules=("anthropic.resources.beta.sessions.sessions",),
    )
    if cls is None or not hasattr(cls, "create"):
        return False
    original = cls.create
    _ORIGINAL_METHODS[("Sessions", "create")] = original

    def patched(self: Any, *args: Any, **kwargs: Any) -> Any:
        result = original(self, *args, **kwargs)
        _safe(_record_session, result, kwargs, args)
        return result

    cls.create = patched  # type: ignore[method-assign]
    return True


def _patch_sessions_stream() -> bool:
    cls = _resolve_class(
        "anthropic.resources.beta.sessions",
        "Sessions",
        fallback_modules=("anthropic.resources.beta.sessions.sessions",),
    )
    if cls is None or not hasattr(cls, "stream"):
        return False
    original = cls.stream
    _ORIGINAL_METHODS[("Sessions", "stream")] = original

    def patched(self: Any, *args: Any, **kwargs: Any) -> Any:
        session_id = _resolve_session_id(args, kwargs)
        iterator = original(self, *args, **kwargs)
        return _instrumented_iterator(iterator, session_id)

    cls.stream = patched  # type: ignore[method-assign]
    return True


def _wrap_instance_method(obj: Any, name: str, builder: Any) -> None:
    """Wrap a single bound method on an instance. The wrapper closes
    over the original so we can still call it from inside."""
    try:
        original = getattr(obj, name)
    except AttributeError:
        logger.debug(f"[Trovis] {type(obj).__name__}.{name} missing — skipping")
        return
    setattr(obj, name, builder(original))


def _agent_create_wrapper(original: Any) -> Any:
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        _safe(_record_agent, result, kwargs)
        return result

    return wrapper


def _session_create_wrapper(original: Any) -> Any:
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        _safe(_record_session, result, kwargs, args)
        return result

    return wrapper


def _session_stream_wrapper(original: Any) -> Any:
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        session_id = _resolve_session_id(args, kwargs)
        iterator = original(*args, **kwargs)
        return _instrumented_iterator(iterator, session_id)

    return wrapper


def _resolve_class(
    module: str,
    class_name: str,
    *,
    fallback_modules: tuple[str, ...] = (),
) -> Optional[type]:
    """Import a class by module + name, tolerating SDK reorganizations
    by checking `fallback_modules` if the primary path misses."""
    for path in (module, *fallback_modules):
        try:
            mod = __import__(path, fromlist=[class_name])
            cls = getattr(mod, class_name, None)
            if cls is not None:
                return cls
        except ImportError:
            continue
    logger.debug(f"[Trovis] couldn't resolve {module}.{class_name}")
    return None


# ---------------------------------------------------------------------------
# Span emission
# ---------------------------------------------------------------------------


def _safe(fn: Any, *args: Any, **kwargs: Any) -> None:
    """Run a recording function; swallow + log any error so user code
    never breaks because of Trovis."""
    try:
        fn(*args, **kwargs)
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[Trovis] {fn.__name__} failed: {e}")


def _record_agent(agent_obj: Any, kwargs: dict[str, Any]) -> None:
    """Pull identity off the Agent response + the create() call kwargs
    and emit one agent_registration span."""
    name = _get(agent_obj, "name") or kwargs.get("name") or "main"
    agent_id = _get(agent_obj, "id")
    system = _get(agent_obj, "system") or kwargs.get("system") or ""
    model_id = _resolve_model(_get(agent_obj, "model") or kwargs.get("model"))
    if agent_id:
        _AGENT_ID_TO_NAME[agent_id] = name
        _AGENT_ID_TO_MODEL[agent_id] = model_id

    tools = _get(agent_obj, "tools") or kwargs.get("tools") or []
    tool_types = _serialize_tool_types(tools)

    tracer = trace.get_tracer("trovis.anthropic")
    span = tracer.start_span("agent_registration")
    try:
        span.set_attribute("trovis.event.type", "agent_registration")
        span.set_attribute("trovis.agent.id", name)
        if system:
            span.set_attribute("trovis.agent.soul", _truncate_attr(system))
        span.set_attribute("trovis.agent.identity", name)
        span.set_attribute("trovis.agent.model", model_id)
        if tool_types:
            span.set_attribute("trovis.agent.tools", tool_types)
        # Schema parity with the OpenClaw plugin / OpenAI registration.
        span.set_attribute("trovis.agent.workspace_path", "")
    finally:
        span.end()


def _record_session(
    session_obj: Any,
    kwargs: dict[str, Any],
    args: tuple[Any, ...],
) -> None:
    session_id = _get(session_obj, "id")
    if not session_id:
        return
    # The `agent` param can be passed positionally OR as a kwarg, and
    # the value is the agent.id string (per the SDK example).
    agent_ref = kwargs.get("agent")
    if agent_ref is None and args:
        agent_ref = args[0]
    if not agent_ref:
        return
    agent_name = _AGENT_ID_TO_NAME.get(str(agent_ref), str(agent_ref))
    _SESSION_TO_AGENT[session_id] = agent_name
    model = _AGENT_ID_TO_MODEL.get(str(agent_ref))
    if model:
        _SESSION_TO_MODEL[session_id] = model


def _instrumented_iterator(
    iterator: Iterator[Any], session_id: Optional[str]
) -> Iterator[Any]:
    """Yield events from the underlying iterator while emitting one
    OTEL span per event. Span emission failure never breaks the
    iteration — user code keeps getting events."""
    for event in iterator:
        try:
            _emit_event_span(event, session_id)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[Trovis] event span emit failed: {e}")
        yield event


def _emit_event_span(event: Any, session_id: Optional[str]) -> None:
    event_type = _get(event, "type") or ""
    agent_name = (
        _SESSION_TO_AGENT.get(session_id, "main")
        if session_id
        else "main"
    )
    run_id = session_id or ""

    tracer = trace.get_tracer("trovis.anthropic")

    if event_type == "user.message":
        text = _extract_text(_get(event, "content"))
        with tracer.start_as_current_span("message_received") as span:
            span.set_attribute("trovis.event.type", "message_received")
            span.set_attribute("trovis.agent.id", agent_name)
            if run_id:
                span.set_attribute("trovis.run.id", run_id)
            if text:
                span.set_attribute("trovis.message.content_length", len(text))
                if is_capture_enabled():
                    span.set_attribute(
                        "trovis.message.content",
                        _truncate(text, _CONTENT_BYTE_LIMIT),
                    )

    elif event_type == "agent.message":
        text = _extract_text(_get(event, "content"))
        with tracer.start_as_current_span("message_sent") as span:
            span.set_attribute("trovis.event.type", "message_sent")
            span.set_attribute("trovis.agent.id", agent_name)
            if run_id:
                span.set_attribute("trovis.run.id", run_id)
            if text:
                span.set_attribute(
                    "trovis.response.content_length", len(text)
                )
                if is_capture_enabled():
                    span.set_attribute(
                        "trovis.response.content",
                        _truncate(text, _CONTENT_BYTE_LIMIT),
                    )
            # Token usage rides on the agent.message event. Pair it with
            # the session's model so the backend can compute cost. Cached
            # input tokens (creation/read) are billed at different rates than
            # plain input, so we surface them separately for accurate cost.
            inp, out, tot, cache_create, cache_read = _extract_usage(
                _get(event, "usage")
            )
            if tot is not None:
                model = _SESSION_TO_MODEL.get(session_id or "")
                if model:
                    span.set_attribute("gen_ai.request.model", model)
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

    elif event_type == "agent.tool_use":
        with tracer.start_as_current_span("tool_call") as span:
            span.set_attribute("trovis.event.type", "tool_call")
            span.set_attribute("trovis.agent.id", agent_name)
            if run_id:
                span.set_attribute("trovis.run.id", run_id)
            tool_name = _get(event, "name")
            if tool_name:
                span.set_attribute("trovis.tool.name", str(tool_name))
            tool_id = _get(event, "id")
            if tool_id:
                span.set_attribute("trovis.tool.call_id", str(tool_id))

    elif event_type == "session.status_idle":
        with tracer.start_as_current_span("agent_run_complete") as span:
            span.set_attribute("trovis.event.type", "agent_run_complete")
            span.set_attribute("trovis.agent.id", agent_name)
            if run_id:
                span.set_attribute("trovis.run.id", run_id)
            span.set_attribute("trovis.run.success", True)

    elif event_type and event_type.startswith("agent.error"):
        with tracer.start_as_current_span("agent_error") as span:
            span.set_attribute("trovis.event.type", "agent_error")
            span.set_attribute("trovis.agent.id", agent_name)
            if run_id:
                span.set_attribute("trovis.run.id", run_id)
            span.set_status(StatusCode.ERROR)


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------


def _get(obj: Any, key: str) -> Any:
    """Read a value off either a Pydantic / SDK model OR a plain dict.
    Anthropic responses tend to be Pydantic models with dict-like
    fallback; user-constructed events in tests may be plain dicts."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _extract_usage(
    usage: Any,
) -> tuple[int | None, int | None, int | None, int | None, int | None]:
    """Normalize an Anthropic usage object to (input, output, total,
    cache_creation, cache_read) tokens. Anthropic reports input_tokens /
    output_tokens plus cache_creation_input_tokens / cache_read_input_tokens
    (these are NOT included in input_tokens). `total` includes all four so
    it reflects every billed token. Returns all-None when no usage present."""
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


def _resolve_session_id(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Optional[str]:
    """The session_id is the first positional arg to .stream(...) per
    the user's example, but may also arrive as a kwarg in future SDKs."""
    sid = kwargs.get("session_id") or kwargs.get("id")
    if sid is None and args:
        sid = args[0]
    return str(sid) if sid else None


def _resolve_model(model: Any) -> str:
    """`model` may be a string, a Pydantic model with `.id`, or a
    plain dict with an 'id' key. Normalize to a string."""
    if model is None:
        return "default"
    if isinstance(model, str):
        return model
    mid = _get(model, "id")
    if isinstance(mid, str) and mid:
        return mid
    return str(model)


def _serialize_tool_types(tools: Any) -> str:
    """Compact JSON list of tool `type` values for the registration
    span. Skipped entirely if no tools were declared."""
    if not tools:
        return ""
    out: list[str] = []
    for t in tools:
        ttype = _get(t, "type")
        if isinstance(ttype, str) and ttype:
            out.append(ttype)
    return json.dumps(out) if out else ""


def _extract_text(content: Any) -> str:
    """Pull the text out of a content payload. The SDK uses content
    blocks shaped like {"type": "text", "text": "…"} either as dicts
    or Pydantic models; bare strings are also tolerated."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            btype = _get(block, "type")
            if btype == "text":
                text = _get(block, "text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    # Single block.
    if _get(content, "type") == "text":
        text = _get(content, "text")
        if isinstance(text, str):
            return text
    return ""


def _truncate_attr(s: str) -> str:
    """For registration soul — 32 KB cap, same as the OpenClaw plugin
    and the OpenAI Agents SDK registration."""
    return _truncate(s, 32 * 1024)


def _truncate(s: str, limit: int) -> str:
    if not s:
        return ""
    encoded = s.encode("utf-8")
    if len(encoded) <= limit:
        return s
    return encoded[:limit].decode("utf-8", errors="ignore") + "…[truncated]"


# ---------------------------------------------------------------------------
# Test-only helpers — kept lightweight so the smoke test can verify
# patching without spinning up a real Anthropic client.
# ---------------------------------------------------------------------------


def _is_patched() -> bool:
    return _PATCHED


def _reset_for_tests() -> None:
    """Undo the class patches. Called from the smoke test; not part of
    the public API."""
    global _PATCHED
    for (cls_name, method_name), original in _ORIGINAL_METHODS.items():
        try:
            cls = _resolve_class(
                f"anthropic.resources.beta.{cls_name.lower()}",
                cls_name,
                fallback_modules=(
                    f"anthropic.resources.beta.{cls_name.lower()}.{cls_name.lower()}",
                ),
            )
            if cls is not None:
                setattr(cls, method_name, original)
        except Exception:  # noqa: BLE001
            pass
    _ORIGINAL_METHODS.clear()
    _SESSION_TO_AGENT.clear()
    _AGENT_ID_TO_NAME.clear()
    _AGENT_ID_TO_MODEL.clear()
    _SESSION_TO_MODEL.clear()
    _PATCHED = False
