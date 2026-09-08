"""Unit tests for workloop title / handoff emission.

No network, no real agent SDK. loop_attrs is imported via file path so this
runs without the full OTEL SDK (same trick as test_openai_usage.py).
CaptureProcessor / Anthropic / Claude adapters are exercised with fake
tracers and duck-typed SDK objects.

Run:  python3 test_loop_attrs.py
"""
from __future__ import annotations

import importlib.util
import os
import sys
import types

PKG_DIR = os.path.dirname(os.path.abspath(__file__))
TROVIS_DIR = os.path.join(PKG_DIR, "trovis")
sys.path.insert(0, PKG_DIR)

# Namespace-package stub so `from trovis.loop_attrs` / registration.py don't
# run trovis/__init__.py (which pulls the full OTEL SDK).
if "trovis" not in sys.modules:
    _pkg = types.ModuleType("trovis")
    _pkg.__path__ = [TROVIS_DIR]
    sys.modules["trovis"] = _pkg

if "opentelemetry" not in sys.modules:
    _ot = types.ModuleType("opentelemetry")
    _ot_trace = types.ModuleType("opentelemetry.trace")
    _ot_trace.get_tracer = lambda *a, **k: None
    _ot_trace.StatusCode = types.SimpleNamespace(ERROR=2, OK=1)
    _ot.trace = _ot_trace
    sys.modules["opentelemetry"] = _ot
    sys.modules["opentelemetry.trace"] = _ot_trace


def _load(name, filename):
    path = os.path.join(TROVIS_DIR, filename)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


la = _load("trovis.loop_attrs", "loop_attrs.py")
sys.modules["trovis.loop_attrs"] = la
reg = _load("trovis.registration", "registration.py")
sys.modules["trovis.registration"] = reg

failures = []


def check(label, cond, detail=""):
    print(("  PASS " if cond else "  FAIL ") + label)
    if detail:
        print(f"        {detail}")
    if not cond:
        failures.append(label)


class FakeSpan:
    def __init__(self, name):
        self.name = name
        self.attrs = {}

    def set_attribute(self, k, v):
        self.attrs[k] = v


class _CM:
    def __init__(self, span):
        self.span = span

    def __enter__(self):
        return self.span

    def __exit__(self, *a):
        return False


class FakeTracer:
    def __init__(self):
        self.spans = []

    def start_as_current_span(self, name):
        s = FakeSpan(name)
        self.spans.append(s)
        return _CM(s)


# ---------------------------------------------------------------------------
# human_title / first_user_text
# ---------------------------------------------------------------------------

print("-- human_title --")
check("collapses whitespace and trims",
      la.human_title("  Fix   the\nbilling bug  ") == "Fix the billing bug")
check("caps at 80 chars",
      len(la.human_title("x" * 300) or "") == 80)
check("empty → None", la.human_title("   ") is None)
check("None → None", la.human_title(None) is None)
check("rejects generic 'Agent workflow'",
      la.human_title("Agent workflow") is None)
check("rejects generic 'Agent run'", la.human_title("Agent run") is None)
check("keeps a real workflow name",
      la.human_title("Billing triage") == "Billing triage")

print("\n-- first_user_text --")
check("bare string", la.first_user_text("Refund the order") == "Refund the order")
check("OpenAI message list",
      la.first_user_text([{"role": "user", "content": "Ship the hammock"}])
      == "Ship the hammock")
check("skips non-user roles",
      la.first_user_text([{"role": "system", "content": "secret"}]) is None)
check("nested content list",
      la.first_user_text([{"role": "user", "content": ["Do the thing"]}])
      == "Do the thing")

# ---------------------------------------------------------------------------
# set_loop_title / mark_handoff pending
# ---------------------------------------------------------------------------

print("\n-- pending helpers --")
la._reset_for_tests()
check("set_loop_title returns normalized",
      la.set_loop_title("  Name this run  ") == "Name this run")
check("pending title queued", la.peek_pending_title() == "Name this run")
check("generic set_loop_title returns None",
      la.set_loop_title("Agent workflow") is None)
check("generic set_loop_title leaves the previous pending title",
      la.peek_pending_title() == "Name this run")
la._reset_for_tests()
check("generic set_loop_title with empty pending returns None",
      la.set_loop_title("workflow") is None)

hid = la.mark_handoff("to_human", "ops@acme.com", "needs approval")
check("mark_handoff returns a uuid", isinstance(hid, str) and len(hid) > 10)
h = la.consume_pending_handoff()
check("handoff direction", h and h["direction"] == "to_human")
check("handoff target", h and h["target"] == "ops@acme.com")
check("handoff one-shot", la.consume_pending_handoff() is None)
check("invalid direction is no-op", la.mark_handoff("sideways") is None)

span = FakeSpan("x")
la.set_loop_title("Queued title")
la.mark_handoff("to_agent", "billing-bot")
la.apply_loop_attrs(span, run_id="run-1", external_id="sess-1")
check("apply stamps run.id", span.attrs.get("trovis.run.id") == "run-1")
check("apply stamps external_id",
      span.attrs.get("trovis.loop.external_id") == "sess-1")
check("apply stamps pending title",
      span.attrs.get("trovis.loop.title") == "Queued title")
check("apply stamps handoff direction",
      span.attrs.get("trovis.handoff.direction") == "to_agent")
check("pending title consumed", la.peek_pending_title() is None)

# ---------------------------------------------------------------------------
# OpenAI CaptureProcessor
# ---------------------------------------------------------------------------

print("\n-- CaptureProcessor: workflow name / set_loop_title --")
la._reset_for_tests()


class _Trace:
    def __init__(self, name, trace_id="tr-1", group_id=None):
        self.name = name
        self.trace_id = trace_id
        self.group_id = group_id


class _Usage:
    def __init__(self, i, o, t=None):
        self.input_tokens, self.output_tokens, self.total_tokens = i, o, t


class _Response:
    def __init__(self, usage, model):
        self.usage, self.model = usage, model


class ResponseSpanData:
    def __init__(self, response, input=None):
        self.response, self.input = response, input


class HandoffSpanData:
    def __init__(self, from_agent, to_agent):
        self.from_agent, self.to_agent = from_agent, to_agent


class SdkSpan:
    def __init__(self, span_data):
        self.span_data = span_data


def _proc(capture=False):
    reg.set_capture_outputs(capture)
    p = reg.CaptureProcessor()
    p._tracer = FakeTracer()
    return p


p = _proc(False)
p.on_trace_start(_Trace("Billing triage", "tr-abc", "grp-9"))
check("workflow name → creating span with title",
      any(s.attrs.get("trovis.loop.title") == "Billing triage" for s in p._tracer.spans))
check("creating span carries run.id",
      any(s.attrs.get("trovis.run.id") == "tr-abc" for s in p._tracer.spans))
check("creating span carries group_id as external_id",
      any(s.attrs.get("trovis.loop.external_id") == "grp-9" for s in p._tracer.spans))

p = _proc(False)
p.on_trace_start(_Trace("Agent workflow", "tr-gen"))
check("generic workflow name does NOT emit a title span",
      not any("trovis.loop.title" in s.attrs for s in p._tracer.spans))

la.set_loop_title("Explicit task")
p = _proc(False)
p.on_trace_start(_Trace("Agent workflow", "tr-exp"))
check("set_loop_title wins over generic workflow name",
      any(s.attrs.get("trovis.loop.title") == "Explicit task" for s in p._tracer.spans))

print("\n-- CaptureProcessor: first user task is capture-gated --")
p = _proc(False)
p.on_trace_start(_Trace("Agent workflow", "tr-priv"))
p.on_span_end(SdkSpan(ResponseSpanData(_Response(_Usage(1, 1, 2), "gpt-4o"),
                                       input="secret refund request")))
check("capture OFF → no title from user input",
      not any(s.attrs.get("trovis.loop.title") for s in p._tracer.spans))

p = _proc(True)
p.on_trace_start(_Trace("Agent workflow", "tr-pub"))
p.on_span_end(SdkSpan(ResponseSpanData(_Response(_Usage(1, 1, 2), "gpt-4o"),
                                       input="Refund order 42")))
check("capture ON → title from first user task",
      any(s.attrs.get("trovis.loop.title") == "Refund order 42" for s in p._tracer.spans))
check("message_received still emitted when capture on",
      any(s.name == "message_received" and "trovis.message.content" in s.attrs
          for s in p._tracer.spans))

print("\n-- CaptureProcessor: HandoffSpanData --")
p = _proc(False)
p.on_trace_start(_Trace("Agent workflow", "tr-h"))
p.on_span_end(SdkSpan(HandoffSpanData("Triage", "Billing")))
ho = next((s for s in p._tracer.spans if s.name == "handoff"), None)
check("handoff span emitted", ho is not None)
check("handoff direction is to_agent",
      ho and ho.attrs.get("trovis.handoff.direction") == "to_agent")
check("handoff target is the receiving agent",
      ho and ho.attrs.get("trovis.handoff.target_id") == "Billing")
check("handoff carries run.id",
      ho and ho.attrs.get("trovis.run.id") == "tr-h")

# ---------------------------------------------------------------------------
# Anthropic + Claude adapters (fake tracer)
# ---------------------------------------------------------------------------

print("\n-- Anthropic user.message title --")
# Load anthropic.py the same standalone way. It imports registration + loop_attrs
# (already in sys.modules) and opentelemetry.trace (stubbed).
anth = _load("trovis.anthropic", "anthropic.py")
sys.modules["trovis.anthropic"] = anth
cas = _load("trovis.claude_agent_sdk", "claude_agent_sdk.py")
sys.modules["trovis.claude_agent_sdk"] = cas


def _patch_tracer(mod, name):
    ft = FakeTracer()
    orig = mod.trace.get_tracer

    def _get(n, *a, **k):
        if n == name:
            return ft
        return orig(n, *a, **k)

    mod.trace.get_tracer = _get
    return ft


reg.set_capture_outputs(True)
la._reset_for_tests()
ft = _patch_tracer(anth, "trovis.anthropic")
anth._SESSION_TO_AGENT["sess-a"] = "coder"
anth._emit_event_span(
    {"type": "user.message", "content": [{"type": "text", "text": "  Write the invoice  "}]},
    "sess-a",
)
check("anthropic capture ON → title from user.message",
      any(s.attrs.get("trovis.loop.title") == "Write the invoice" for s in ft.spans))
check("anthropic stamps run.id = session id",
      any(s.attrs.get("trovis.run.id") == "sess-a" for s in ft.spans))
check("anthropic stamps external_id = session id",
      any(s.attrs.get("trovis.loop.external_id") == "sess-a" for s in ft.spans))

reg.set_capture_outputs(False)
la._reset_for_tests()
ft = _patch_tracer(anth, "trovis.anthropic")
anth._emit_event_span(
    {"type": "user.message", "content": "private prompt"},
    "sess-b",
)
check("anthropic capture OFF → no title from user.message",
      not any("trovis.loop.title" in s.attrs for s in ft.spans))

la.set_loop_title("Named without capture")
ft = _patch_tracer(anth, "trovis.anthropic")
anth._emit_event_span(
    {"type": "user.message", "content": "private prompt"},
    "sess-c",
)
check("anthropic set_loop_title works with capture off",
      any(s.attrs.get("trovis.loop.title") == "Named without capture" for s in ft.spans))

ft = _patch_tracer(anth, "trovis.anthropic")
anth._emit_event_span(
    {"type": "agent.handoff", "direction": "to_human", "target": "sarah@acme.com",
     "reason": "approval"},
    "sess-d",
)
ho = next((s for s in ft.spans if s.name == "handoff"), None)
check("anthropic .handoff event → to_human",
      ho and ho.attrs.get("trovis.handoff.direction") == "to_human")
check("anthropic handoff target survived",
      ho and ho.attrs.get("trovis.handoff.target_id") == "sarah@acme.com")

print("\n-- Claude Agent SDK UserMessage title --")
reg.set_capture_outputs(True)
la._reset_for_tests()
ft = _patch_tracer(cas, "trovis.claude_agent_sdk")


class UserMessage:
    def __init__(self, content):
        self.content = content


cas._emit_for_message(
    UserMessage("Refactor the auth module"),
    "main",
    {"session_id": "cas-1", "prompt": "Refactor the auth module"},
)
check("claude capture ON → title from UserMessage",
      any(s.attrs.get("trovis.loop.title") == "Refactor the auth module"
          for s in ft.spans))
check("claude stamps run.id",
      any(s.attrs.get("trovis.run.id") == "cas-1" for s in ft.spans))

reg.set_capture_outputs(False)
la._reset_for_tests()
ft = _patch_tracer(cas, "trovis.claude_agent_sdk")
cas._emit_for_message(UserMessage("secret task"), "main", {"session_id": "cas-2"})
check("claude capture OFF → no title from UserMessage",
      not any("trovis.loop.title" in s.attrs for s in ft.spans))

print()
if failures:
    print(f"FAILED: {len(failures)} check(s):")
    for f in failures:
        print("  - " + f)
    raise SystemExit(1)
print("ALL CHECKS PASSED")
