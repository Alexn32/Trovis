"""Hermes Agent support for Oversee.

Hermes is a Python-based agent platform with its own plugin loader.
Plugins are discovered either by being dropped into `~/.hermes/plugins/`
or — what this module enables — via Python entry points under the
group `hermes_agent.plugins`. We register `oversee.hermes_plugin` in
pyproject.toml; Hermes calls `register(ctx)` here on every gateway
start.

The integration mirrors the OpenClaw plugin's shape — same span
vocabulary (`agent_registration`, `tool_call`), same identity files
(SOUL.md, memory.md), same `/oversee` slash command surface — so an
operator running both shows up in one Fleet view without doing
anything special on the dashboard side.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger("oversee.hermes")


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
    endpoint = os.environ.get(
        "OVERSEE_ENDPOINT",
        "https://web-production-e6bc4.up.railway.app/v1/traces",
    )
    api_key = os.environ.get("OVERSEE_API_KEY", "")
    agent_name = os.environ.get("OVERSEE_AGENT_NAME", "hermes-agent")
    capture_outputs = (
        os.environ.get("OVERSEE_CAPTURE_OUTPUTS", "").lower() == "true"
    )

    if not endpoint:
        print(
            "[Oversee] No endpoint configured. Set OVERSEE_ENDPOINT or use "
            "/oversee connect."
        )
        return

    # OTEL pipeline. _setup_otel is shared with the OpenAI/Anthropic
    # entry points so the resource shape stays consistent.
    from oversee.core import _setup_otel, _state

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
    _try(
        lambda: ctx.register_command(
            "oversee",
            _handle_oversee_command,
            "Oversee agent monitoring",
        )
    )
    _try(
        lambda: ctx.register_cli_command(
            "oversee",
            "Oversee agent monitoring commands",
            None,
            _handle_cli,
        )
    )

    print(
        f"[Oversee] Connected. Sending telemetry to {endpoint} "
        f"as '{agent_name}'"
    )
    if capture_outputs:
        print("[Oversee] Output capture: enabled")


def _try(fn: Any) -> None:
    """Run a registration call; log + swallow on failure so a missing
    `ctx.register_*` method doesn't abort the rest of register()."""
    try:
        fn()
    except (AttributeError, TypeError) as e:
        logger.debug(f"[Oversee] Hermes ctx method unsupported: {e}")


# ---------------------------------------------------------------------------
# Hook handlers
# ---------------------------------------------------------------------------


def _on_tool_call(tool_name: Any, params: Any, result: Any) -> None:
    """post_tool_call hook — one span per tool invocation.

    Same attribute vocabulary as the OpenClaw plugin so the dashboard's
    span aggregation, ownership, and Ask features work uniformly:
    `oversee.tool.name`, `oversee.tool.param_keys`, and (under opt-in
    capture) `oversee.tool.result`.
    """
    from oversee.core import _state

    tracer = _state.get("tracer")
    if not tracer:
        return

    capture = _state.get("capture_outputs", False)
    agent_name = _state.get("agent_name", "hermes-agent")

    with tracer.start_as_current_span("tool_call") as span:
        span.set_attribute("oversee.event.type", "tool_call")
        span.set_attribute("oversee.agent.id", agent_name)
        if tool_name:
            span.set_attribute("oversee.tool.name", str(tool_name))
        # Param KEYS only by default — values can carry user data.
        if isinstance(params, dict):
            span.set_attribute(
                "oversee.tool.param_keys",
                json.dumps(list(params.keys())),
            )
        if capture and result is not None:
            span.set_attribute(
                "oversee.tool.result",
                _truncate(str(result), 10_000),
            )


# ---------------------------------------------------------------------------
# /oversee chat command
# ---------------------------------------------------------------------------


def _handle_oversee_command(args: str = "", **_kwargs: Any) -> str:
    """Handle `/oversee …` inside the Hermes chat. Returns a JSON
    string per Hermes' command protocol (we follow the same shape
    the OpenClaw plugin's command returns)."""
    from oversee.core import _state

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

    # Default — bare /oversee, unknown subcommand, or `help`.
    return json.dumps(
        {
            "success": True,
            "message": (
                "Oversee commands:\n"
                "/oversee connect <url>     — set the OTLP endpoint\n"
                "/oversee apikey <key>      — set the API key\n"
                "/oversee capture on|off    — toggle content capture\n"
                "/oversee status            — show connection state"
            ),
        }
    )


def _handle_cli(*args: Any, **kwargs: Any) -> Any:
    """Hermes CLI handler — currently just delegates to the same
    chat-command logic so `hermes oversee status` behaves like
    `/oversee status`. The first positional, when present, is the
    subcommand string."""
    arg_str = " ".join(str(a) for a in args) if args else ""
    return _handle_oversee_command(arg_str, **kwargs)


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

    Same vocabulary as the OpenClaw plugin (`oversee.agent.id`,
    `oversee.agent.soul`, `oversee.agent.identity`, `oversee.agent.model`)
    so the dashboard's auto-describe pipeline + Claude prompts can
    treat Hermes agents identically.
    """
    from oversee.core import _state

    tracer = _state.get("tracer")
    if not tracer:
        return

    with tracer.start_as_current_span("agent_registration") as span:
        span.set_attribute("oversee.event.type", "agent_registration")
        span.set_attribute("oversee.agent.id", agent_name)
        if soul:
            span.set_attribute("oversee.agent.soul", soul)
        span.set_attribute("oversee.agent.identity", agent_name)
        span.set_attribute("oversee.agent.platform", "hermes")
        # Workspace path included for schema parity with OpenClaw —
        # Hermes' equivalent is the `~/.hermes` config dir.
        span.set_attribute(
            "oversee.agent.workspace_path",
            str(Path.home() / ".hermes"),
        )
        if memory:
            span.set_attribute("oversee.agent.memory", memory)


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
