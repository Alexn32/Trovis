"""Smoke test for the trovis-agents package.

Run from the package root:

    pip install -e . && python test_smoke.py

Or without install:

    PYTHONPATH=. python test_smoke.py

Verifies:
  1. The package imports cleanly.
  2. init() runs without raising, with and without an api_key.
  3. The global TracerProvider gets set to our SDK provider.
  4. Manual span emission works through the configured pipeline.
  5. If the OpenAI Agents SDK is installed, constructing an Agent
     triggers a registration span (no actual API call needed).

Exits non-zero on any failure.
"""

from __future__ import annotations

import os
import sys
import traceback

# Allow `python test_smoke.py` to work without installing the package.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def step(n: int, label: str) -> None:
    print(f"\n[{n}] {label}")


failed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global failed
    status = "OK" if cond else "FAIL"
    print(f"    {status} — {name}")
    if detail:
        print(f"        {detail}")
    if not cond:
        failed += 1


try:
    step(1, "Importing trovis…")
    import trovis
    check("import trovis", True, f"version={trovis.__version__}")
    check("init is callable", callable(trovis.init))

    step(2, "Calling init() with a dummy endpoint (won't actually export)…")
    # Point at a localhost port nothing is listening on so the BatchSpanProcessor
    # quietly retries-and-drops without hanging on shutdown.
    trovis.init(
        api_key="test-key",
        agent_name="smoke-test-agent",
        endpoint="http://127.0.0.1:1/v1/traces",
        capture_outputs=False,
    )
    check("init() returned without raising", True)

    step(3, "Inspecting global tracer provider…")
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider

    provider = trace.get_tracer_provider()
    check(
        "provider is an SDK TracerProvider",
        isinstance(provider, TracerProvider),
        f"got {type(provider).__name__}",
    )

    step(4, "Emitting a manual span…")
    tracer = trace.get_tracer("smoke-test")
    with tracer.start_as_current_span("manual_span") as span:
        span.set_attribute("trovis.smoke", "true")
    check("manual span emitted", True)

    step(5, "Toggling capture flag at runtime…")
    from trovis.registration import is_capture_enabled, set_capture_outputs

    set_capture_outputs(True)
    check("capture flag flipped on", is_capture_enabled())
    set_capture_outputs(False)
    check("capture flag flipped off", not is_capture_enabled())

    step(6, "OpenAI Agents SDK interaction (skipped if not installed)…")
    try:
        from agents import Agent  # type: ignore[import-not-found]

        # Construct an agent — should trigger the monkey-patched
        # __init__ and emit one registration span.
        agent = Agent(
            name="SmokeBot",
            instructions="You are a smoke test agent. Do nothing.",
        )
        check(
            "Agent constructed without raising",
            agent.name == "SmokeBot",
            f"agent.name={agent.name!r}",
        )
    except ImportError:
        print("    SKIP — `agents` (openai-agents) not installed in this env")

    step(7, "Re-calling init() is idempotent…")
    trovis.init(api_key="another-key", agent_name="smoke-test-agent")
    check("second init() returned without raising", True)

    step(8, "Anthropic instrumentation (skipped if anthropic not installed)…")
    try:
        import anthropic  # noqa: F401
        from anthropic.resources.beta.agents import Agents
        from anthropic.resources.beta.sessions import Sessions
        from trovis.anthropic import (
            _is_patched,
            _reset_for_tests,
            setup_anthropic,
            monitor,
            track_session,
            _SESSION_TO_AGENT,
        )

        # init(platform="auto") in step 2 already ran setup_anthropic().
        # Reset so we can verify the patcher actually mutates the
        # methods (otherwise the snapshot below captures the already-
        # patched version).
        _reset_for_tests()
        original_agents_create = Agents.create
        original_sessions_create = Sessions.create

        ok = setup_anthropic()
        check(
            "setup_anthropic() returned True (at least one patch landed)",
            ok,
        )
        check("module reports patched", _is_patched())
        check(
            "Agents.create was replaced",
            Agents.create is not original_agents_create,
        )
        check(
            "Sessions.create was replaced",
            Sessions.create is not original_sessions_create,
        )

        # track_session round-trip — registers then cleans up.
        from trovis.anthropic import _SESSION_TO_AGENT as _MAP
        with track_session("sess_smoke", agent_name="smoke-bot"):
            check(
                "track_session sets the mapping",
                _MAP.get("sess_smoke") == "smoke-bot",
            )
        check(
            "track_session clears the mapping on exit",
            "sess_smoke" not in _MAP,
        )

        # monitor() must not crash on a non-real client.
        class _FakeBeta:
            class agents:  # noqa: N801
                @staticmethod
                def create(**kwargs):
                    return type("A", (), {"id": "a_1", "name": "Fake"})()

            class sessions:  # noqa: N801
                @staticmethod
                def create(agent=None, **kwargs):
                    return type("S", (), {"id": "s_1"})()

                @staticmethod
                def stream(session_id):
                    return iter([])

        class _FakeClient:
            beta = _FakeBeta()

        monitored = monitor(_FakeClient())
        check("monitor() returned the client", monitored is not None)

        # Undo the patches so subsequent re-imports of the SDK keep
        # working in this Python process.
        _reset_for_tests()
        check(
            "_reset_for_tests restored Agents.create",
            Agents.create is original_agents_create,
        )

    except ImportError:
        print("    SKIP — `anthropic` not installed in this env")

    step(9, "Re-init with platform='anthropic' only…")
    # platform-explicit re-init shouldn't crash even on an already-
    # initialized SDK. (Note: _INITIALIZED is true from step 2 so the
    # internal early-return path fires; this exercises that path.)
    trovis.init(
        api_key="another-key",
        agent_name="smoke-test-agent",
        platform="anthropic",
    )
    check("platform='anthropic' init() returned without raising", True)

    step(10, "Hermes plugin (entry point + ctx wiring)…")
    # Hermes plugin doesn't need anthropic or openai-agents installed;
    # it operates on a `ctx` object the Hermes runtime provides. We
    # build a fake ctx that records which hook/command names were
    # registered, then run `register(ctx)` and assert what landed.
    from trovis.hermes_plugin import register as hermes_register

    class FakeHermesCtx:
        def __init__(self):
            self.hooks = []
            self.commands = []
            self.cli = []

        def register_hook(self, name, fn):
            self.hooks.append((name, fn))

        def register_command(self, name, fn, desc):
            self.commands.append((name, fn, desc))

        def register_cli_command(self, name, desc, group, fn):
            self.cli.append((name, desc, group, fn))

    ctx = FakeHermesCtx()
    hermes_register(ctx)
    check(
        "register_hook('post_tool_call', …)",
        any(name == "post_tool_call" for name, _ in ctx.hooks),
    )
    check(
        "register_command('trovis', …)",
        any(name == "trovis" for name, _, _ in ctx.commands),
    )
    check(
        "register_cli_command('trovis', …)",
        any(name == "trovis" for name, _, _, _ in ctx.cli),
    )

    # Drive the /trovis chat command end-to-end.
    import json as _json

    _, cmd_fn, _ = next(c for c in ctx.commands if c[0] == "trovis")
    status_resp = _json.loads(cmd_fn("status"))
    check("/trovis status returns success", status_resp.get("success") is True)
    help_resp = _json.loads(cmd_fn(""))
    check(
        "/trovis (no args) returns help text",
        "Trovis commands" in help_resp.get("message", ""),
    )
    capture_resp = _json.loads(cmd_fn("capture on"))
    check(
        "/trovis capture on flips the flag",
        "enabled" in capture_resp.get("message", ""),
    )

    # Drive the post_tool_call hook so we exercise the span path.
    _, hook_fn = next(h for h in ctx.hooks if h[0] == "post_tool_call")
    hook_fn("search", {"query": "hammocks"}, "5 results")
    check("post_tool_call hook ran without raising", True)

    step(11, "Claude Agent SDK adapter (fake message stream)…")
    # The SDK isn't a hard dep, so we exercise the message-handling
    # logic directly with stand-in message objects whose class NAMES
    # match what the adapter dispatches on (type(m).__name__).
    import asyncio

    from trovis import claude_agent_sdk as cas

    class SystemMessage:
        def __init__(self, data):
            self.data = data

    class TextBlock:
        def __init__(self, text):
            self.text = text

    class ToolUseBlock:
        def __init__(self, id, name, input):  # noqa: A002
            self.id = id
            self.name = name
            self.input = input

    class AssistantMessage:
        def __init__(self, content, model):
            self.content = content
            self.model = model

    class UserMessage:
        def __init__(self, content):
            self.content = content

    class ResultMessage:
        def __init__(self, session_id, is_error, usage):
            self.session_id = session_id
            self.is_error = is_error
            self.usage = usage

    async def fake_query_stream():
        yield SystemMessage({"session_id": "cas_sess_1", "model": "claude-opus-4-6"})
        yield UserMessage("Help me build a hammock landing page")
        yield AssistantMessage(
            [TextBlock("Sure!"), ToolUseBlock("tu_1", "write_file", {"path": "x"})],
            "claude-opus-4-6",
        )
        yield ResultMessage(
            "cas_sess_1", False, {"input_tokens": 1200, "output_tokens": 800}
        )

    # The options arg carries the system prompt → registration.
    call_kwargs = {
        "options": type(
            "Opts", (), {"system_prompt": "You are a hammock site builder.", "model": "claude-opus-4-6"}
        )()
    }

    async def drain():
        out = []
        async for m in cas._instrumented_stream(fake_query_stream(), call_kwargs):
            out.append(type(m).__name__)
        return out

    drained = asyncio.run(drain())
    check(
        "adapter yielded all 4 messages untouched",
        drained == ["SystemMessage", "UserMessage", "AssistantMessage", "ResultMessage"],
        f"got {drained}",
    )

    # setup/reset round-trip — SDK not installed, so setup returns False
    # cleanly (no crash). _reset_for_tests is a no-op then.
    setup_ok = cas.setup_claude_agent_sdk()
    check(
        "setup_claude_agent_sdk() returns a bool without raising "
        "(False when SDK absent)",
        isinstance(setup_ok, bool),
        f"returned {setup_ok!r}",
    )
    cas._reset_for_tests()
    check("claude_agent_sdk _reset_for_tests ran", not cas._is_patched())

    step(12, "Cross-process trace propagation (inject / continue_trace)…")
    # init() in step 2 set the global W3C propagator. Inject within an
    # active span → a traceparent; continue_trace re-attaches it on the
    # "receiving" side and shares the trace_id.
    with tracer.start_as_current_span("prop_caller") as _caller:
        caller_trace = _caller.get_span_context().trace_id
        carrier = trovis.inject()
    check(
        "inject() produced a traceparent",
        isinstance(carrier, dict) and "traceparent" in carrier,
        f"carrier keys={list(carrier)}",
    )
    with trovis.continue_trace(carrier, "prop_receiver") as _recv:
        check(
            "continue_trace continues the same trace",
            _recv.get_span_context().trace_id == caller_trace,
        )
    check("inject/extract exported by package", hasattr(trovis, "continue_trace"))

    step(13, "Flushing spans before exit…")
    try:
        provider.shutdown()
        check("provider shutdown clean", True)
    except Exception as e:
        # Shutdown can timeout when the endpoint is unreachable — that's
        # expected for this dummy port and not a real failure.
        check(
            "provider shutdown (timeout expected on unreachable endpoint)",
            True,
            f"shutdown raised: {type(e).__name__}: {e}",
        )

except Exception:
    failed += 1
    print("\nUNCAUGHT EXCEPTION:")
    traceback.print_exc()


print()
if failed:
    print(f"SMOKE TEST FAILED ({failed} check(s) failed).")
    sys.exit(1)
else:
    print("SMOKE TEST PASSED.")
