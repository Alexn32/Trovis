"""Hermes Agent support for Trovis.

Hermes is a Python-based agent platform with its own plugin loader.
Plugins are discovered either by being dropped into `~/.hermes/plugins/`
or — what this module enables — via Python entry points under the
group `hermes_agent.plugins`. We register `trovis.hermes_plugin` in
pyproject.toml; Hermes calls `register(ctx)` here on every gateway
start.

The integration mirrors the OpenClaw plugin's shape — same span
vocabulary (`agent_registration`, `tool_call`), same identity files
(SOUL.md, memory.md), same `/trovis` slash command surface — so an
operator running both shows up in one Fleet view without doing
anything special on the dashboard side.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger("trovis.hermes")


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------


def register(ctx: Any) -> None:
    """Hermes plugin entry point. Called once by the Hermes plugin
    loader at gateway start.

    `ctx` exposes the hook + command registration API. We're
    permissive about its shape — register methods we recognize, log
    + skip the ones we don't, so a Hermes API revision can't crash
    the gateway.
    """
    # Read config — env vars are the only source of truth here.
    # Hermes doesn't have a built-in per-plugin config store, so
    # operators set the endpoint + key in their shell or via the
    # `requires_env` declaration in plugin.yaml (which Hermes prompts
    # for at install time).
    from trovis.core import _env  # TROVIS_* with legacy OVERSEE_* fallback

    endpoint = _env(
        "ENDPOINT",
        "https://web-production-e6bc4.up.railway.app/v1/traces",
    )
    api_key = _env("API_KEY", "")
    agent_name = _env("AGENT_NAME", "hermes-agent")
    capture_outputs = (_env("CAPTURE_OUTPUTS", "") or "").lower() == "true"

    if not endpoint:
        print(
            "[Trovis] No endpoint configured. Set TROVIS_ENDPOINT or use "
            "/trovis connect."
        )
        return

    # OTEL pipeline. _setup_otel is shared with the OpenAI/Anthropic
    # entry points so the resource shape stays consistent.
    from trovis.core import _setup_otel, _state

    _setup_otel(
        endpoint=endpoint,
        api_key=api_key,
        agent_name=agent_name,
        platform="hermes",
    )
    _state["capture_outputs"] = capture_outputs

    # Pull SOUL.md (always) + memory.md (only when capture is on; it
    # can contain personal accumulated context that operators may not
    # want to ship).
    hermes_dir = Path.home() / ".hermes"
    soul = _read_file(hermes_dir / "SOUL.md")
    memory = _read_file(hermes_dir / "memory.md") if capture_outputs else ""

    _send_registration(agent_name, soul, memory)

    # Hook + command wiring. Each helper is wrapped in try/except so
    # a Hermes API that's missing one of these methods doesn't break
    # the others.
    _try(lambda: ctx.register_hook("post_tool_call", _on_tool_call))
    # Token usage isn't carried on tool calls. If this Hermes build
    # exposes a post-model-call hook, capture usage from there; if not,
    # _try() silently no-ops and we simply don't get cost data for
    # Hermes until the gateway exposes such a hook.
    _try(lambda: ctx.register_hook("post_model_call", _on_model_call))
    _try(
        lambda: ctx.register_command(
            "trovis",
            _handle_trovis_command,
            "Trovis agent monitoring",
        )
    )
    _try(
        lambda: ctx.register_cli_command(
            "trovis",
            "Trovis agent monitoring commands",
            None,
            _handle_cli,
        )
    )

    print(
        f"[Trovis] Connected. Sending telemetry to {endpoint} "
        f"as '{agent_name}'"
    )
    if capture_outputs:
        print("[Trovis] Output capture: enabled")


def _try(fn: Any) -> None:
    """Run a registration call; log + swallow on failure so a missing
    `ctx.register_*` method doesn't abort the rest of register()."""
    try:
        fn()
    except (AttributeError, TypeError) as e:
        logger.debug(f"[Trovis] Hermes ctx method unsupported: {e}")


# ---------------------------------------------------------------------------
# Hook handlers
# ---------------------------------------------------------------------------


def _on_tool_call(tool_name: Any, params: Any, result: Any) -> None:
    """post_tool_call hook — one span per tool invocation.

    Same attribute vocabulary as the OpenClaw plugin so the dashboard's
    span aggregation, ownership, and Ask features work uniformly:
    `trovis.tool.name`, `trovis.tool.param_keys`, and (under opt-in
    capture) `trovis.tool.result`.
    """
    from trovis.core import _state

    tracer = _state.get("tracer")
    if not tracer:
        return

    capture = _state.get("capture_outputs", False)
    agent_name = _state.get("agent_name", "hermes-agent")

    with tracer.start_as_current_span("tool_call") as span:
        span.set_attribute("trovis.event.type", "tool_call")
        span.set_attribute("trovis.agent.id", agent_name)
        if tool_name:
            span.set_attribute("trovis.tool.name", str(tool_name))
        # Param KEYS only by default — values can carry user data.
        if isinstance(params, dict):
            span.set_attribute(
                "trovis.tool.param_keys",
                json.dumps(list(params.keys())),
            )
        if capture and result is not None:
            span.set_attribute(
                "trovis.tool.result",
                _truncate(str(result), 10_000),
            )


def _on_model_call(*args: Any, **kwargs: Any) -> None:
    """post_model_call hook (best-effort — only fires if this Hermes
    build exposes it). Emits a model_call span with the OTEL GenAI
    usage attributes so the backend can compute cost.

    Hermes' exact signature is unknown, so we accept *args/**kwargs and
    pull what we recognize: a model name plus a usage object carrying
    input/output token counts. Anything we can't find is simply
    omitted — never raises.
    """
    from trovis.core import _state

    tracer = _state.get("tracer")
    if not tracer:
        return
    agent_name = _state.get("agent_name", "hermes-agent")

    # Merge positional + keyword sources into one lookup bag. Hermes
    # might pass a single dict, or (model, usage), or kwargs.
    bag: dict[str, Any] = {}
    for a in args:
        if isinstance(a, dict):
            bag.update(a)
    bag.update(kwargs)

    model = bag.get("model") or bag.get("model_name") or bag.get("model_id")
    usage = bag.get("usage") or bag.get("token_usage") or bag

    def _int(v: Any) -> int | None:
        try:
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    inp = _int(
        _bag_get(usage, "input_tokens", "prompt_tokens", "inputTokens")
    )
    out = _int(
        _bag_get(usage, "output_tokens", "completion_tokens", "outputTokens")
    )
    tot = _int(_bag_get(usage, "total_tokens", "totalTokens"))
    if tot is None and (inp is not None or out is not None):
        tot = (inp or 0) + (out or 0)

    if tot is None:
        return  # nothing usable — don't emit an empty model_call span

    with tracer.start_as_current_span("model_call") as span:
        span.set_attribute("trovis.event.type", "model_call")
        span.set_attribute("trovis.agent.id", agent_name)
        if model:
            span.set_attribute("gen_ai.request.model", str(model))
        if inp is not None:
            span.set_attribute("gen_ai.usage.input_tokens", inp)
        if out is not None:
            span.set_attribute("gen_ai.usage.output_tokens", out)
        span.set_attribute("gen_ai.usage.total_tokens", tot)


def _bag_get(obj: Any, *keys: str) -> Any:
    """Read the first present key from a dict or attribute bag."""
    for k in keys:
        if isinstance(obj, dict):
            if k in obj and obj[k] is not None:
                return obj[k]
        else:
            v = getattr(obj, k, None)
            if v is not None:
                return v
    return None


# ---------------------------------------------------------------------------
# /trovis chat command
# ---------------------------------------------------------------------------


def _handle_trovis_command(args: str = "", **_kwargs: Any) -> str:
    """Handle `/trovis …` inside the Hermes chat. Returns a JSON
    string per Hermes' command protocol (we follow the same shape
    the OpenClaw plugin's command returns)."""
    from trovis.core import _state

    parts = (args or "").strip().split()
    sub = parts[0].lower() if parts else ""
    value = " ".join(parts[1:]) if len(parts) > 1 else ""

    if sub == "connect" and value:
        _state["endpoint"] = value
        return json.dumps(
            {
                "success": True,
                "message": (
                    f"Endpoint set to {value}. Restart Hermes for the "
                    f"OTLP exporter to pick up the new URL."
                ),
            }
        )

    if sub == "apikey" and value:
        _state["api_key"] = value
        masked = _mask_key(value)
        return json.dumps(
            {
                "success": True,
                "message": (
                    f"API key set: {masked}. Restart Hermes for the "
                    f"OTLP exporter to send the new header."
                ),
            }
        )

    if sub == "capture":
        on = value.lower() in ("on", "true", "1", "yes")
        _state["capture_outputs"] = on
        return json.dumps(
            {
                "success": True,
                "message": (
                    f"Output capture {'enabled' if on else 'disabled'}. "
                    f"Takes effect on the next event."
                ),
            }
        )

    if sub == "status":
        connected = bool(_state.get("tracer"))
        return json.dumps(
            {
                "success": True,
                "message": (
                    f"{'Connected' if connected else 'Not connected'}. "
                    f"Endpoint: {_state.get('endpoint', 'none')}, "
                    f"agent: {_state.get('agent_name', 'unknown')}, "
                    f"capture: {'on' if _state.get('capture_outputs') else 'off'}"
                ),
            }
        )

    # Default — bare /trovis, unknown subcommand, or `help`.
    return json.dumps(
        {
            "success": True,
            "message": (
                "Trovis commands:\n"
                "/trovis connect <url>     — set the OTLP endpoint\n"
                "/trovis apikey <key>      — set the API key\n"
                "/trovis capture on|off    — toggle content capture\n"
                "/trovis status            — show connection state"
            ),
        }
    )


def _handle_cli(*args: Any, **kwargs: Any) -> Any:
    """Hermes CLI handler — currently just delegates to the same
    chat-command logic so `hermes trovis status` behaves like
    `/trovis status`. The first positional, when present, is the
    subcommand string."""
    arg_str = " ".join(str(a) for a in args) if args else ""
    return _handle_trovis_command(arg_str, **kwargs)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_file(path: Path) -> str:
    """Read an identity / memory file. Truncated to 32 KB to stay
    inside OTLP attribute limits — same budget as the OpenClaw plugin.
    Missing/unreadable files return empty string so they're omitted
    from the registration span."""
    try:
        return path.read_text(encoding="utf-8")[:32_000]
    except (FileNotFoundError, PermissionError, IsADirectoryError):
        return ""


def _send_registration(agent_name: str, soul: str, memory: str) -> None:
    """Emit the one-shot `agent_registration` span carrying identity.

    Same vocabulary as the OpenClaw plugin (`trovis.agent.id`,
    `trovis.agent.soul`, `trovis.agent.identity`, `trovis.agent.model`)
    so the dashboard's auto-describe pipeline + Claude prompts can
    treat Hermes agents identically.
    """
    from trovis.core import _state

    tracer = _state.get("tracer")
    if not tracer:
        return

    with tracer.start_as_current_span("agent_registration") as span:
        span.set_attribute("trovis.event.type", "agent_registration")
        span.set_attribute("trovis.agent.id", agent_name)
        if soul:
            span.set_attribute("trovis.agent.soul", soul)
        span.set_attribute("trovis.agent.identity", agent_name)
        span.set_attribute("trovis.agent.platform", "hermes")
        # Workspace path included for schema parity with OpenClaw —
        # Hermes' equivalent is the `~/.hermes` config dir.
        span.set_attribute(
            "trovis.agent.workspace_path",
            str(Path.home() / ".hermes"),
        )
        if memory:
            span.set_attribute("trovis.agent.memory", memory)


def _truncate(s: str, limit: int) -> str:
    """Byte-accurate truncation. Mirrors the OpenAI / Anthropic
    adapters."""
    if not s:
        return ""
    encoded = s.encode("utf-8")
    if len(encoded) <= limit:
        return s
    return encoded[:limit].decode("utf-8", errors="ignore") + "…[truncated]"


def _mask_key(key: str) -> str:
    """Mask an API key for display in chat output — first 6 chars +
    `…` + last 4. Same shape as the OpenClaw plugin."""
    if not key:
        return "(not set)"
    if len(key) <= 10:
        return key
    return f"{key[:6]}…{key[-4:]}"
