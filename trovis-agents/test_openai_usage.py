"""Unit test for OpenAI Agents SDK token capture (the fix for agents that
connect but report 0 tokens / $0 cost).

Verifies CaptureProcessor now stamps gen_ai.usage.* + gen_ai.request.model on
the `llm_output` span — regardless of the content-capture flag — for both the
Responses-API (ResponseSpanData) and chat-completions (GenerationSpanData)
shapes, while content stays gated on capture.

Run:  python3 test_openai_usage.py
(no network, no real OpenAI SDK — uses duck-typed span data + a fake tracer)
"""
import importlib.util
import os
import sys
import types

# Stub the OTEL API so registration.py imports without opentelemetry installed —
# the test swaps in its own FakeTracer, so the real tracer is never exercised.
if "opentelemetry" not in sys.modules:
    _ot = types.ModuleType("opentelemetry")
    _ot_trace = types.ModuleType("opentelemetry.trace")
    _ot_trace.get_tracer = lambda *a, **k: None
    _ot.trace = _ot_trace
    sys.modules["opentelemetry"] = _ot
    sys.modules["opentelemetry.trace"] = _ot_trace

# Load registration.py standalone (its only top-level dependency is the stubbed
# opentelemetry.trace) so we don't trigger trovis/__init__.py, which pulls in
# the full OTEL SDK that isn't installed in this test env.
_path = os.path.join(os.path.dirname(__file__), "trovis", "registration.py")
_spec = importlib.util.spec_from_file_location("trovis_registration_under_test", _path)
reg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reg)

failures = []
def check(label, cond):
    print(("  PASS " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


# --- fake OTEL tracer that records what each emitted span receives ----------
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


# --- duck-typed OpenAI Agents SDK span data (class names matter) ------------
class _Usage:  # Responses-API style (attribute object)
    def __init__(self, i, o, t=None):
        self.input_tokens, self.output_tokens, self.total_tokens = i, o, t

class _Response:
    def __init__(self, usage, model):
        self.usage, self.model = usage, model

class ResponseSpanData:
    def __init__(self, response, input=None):
        self.response, self.input = response, input

class GenerationSpanData:
    def __init__(self, usage, model, input=None, output=None):
        self.usage, self.model, self.input, self.output = usage, model, input, output

class SdkSpan:
    def __init__(self, span_data):
        self.span_data = span_data


def run(span_data, capture):
    """Feed one SDK span through a fresh processor; return its emitted spans."""
    reg.is_capture_enabled = lambda: capture  # monkeypatch the gate
    proc = reg.CaptureProcessor()
    proc._tracer = FakeTracer()
    proc.on_span_end(SdkSpan(span_data))
    return proc._tracer.spans


def llm(spans):
    return next((s for s in spans if s.name == "llm_output"), None)


print("-- ResponseSpanData, capture OFF (tokens must still flow) --")
spans = run(ResponseSpanData(_Response(_Usage(100, 50, 150), "gpt-4o"), input="hi"), capture=False)
s = llm(spans)
check("llm_output span emitted", s is not None)
check("input_tokens=100", s and s.attrs.get("gen_ai.usage.input_tokens") == 100)
check("output_tokens=50", s and s.attrs.get("gen_ai.usage.output_tokens") == 50)
check("total_tokens=150", s and s.attrs.get("gen_ai.usage.total_tokens") == 150)
check("model=gpt-4o", s and s.attrs.get("gen_ai.request.model") == "gpt-4o")
check("NO response content when capture off",
      s and "trovis.response.content" not in s.attrs)
check("NO message_received span when capture off",
      not any(sp.name == "message_received" for sp in spans))

print("\n-- ResponseSpanData, capture ON (tokens + content) --")
spans = run(ResponseSpanData(_Response(_Usage(10, 20, 30), "gpt-4o-mini"), input="ping"), capture=True)
s = llm(spans)
check("usage still present", s and s.attrs.get("gen_ai.usage.input_tokens") == 10)
check("response content captured", s and "trovis.response.content" in s.attrs)
check("message_received span emitted (prompt content)",
      any(sp.name == "message_received" for sp in spans))

print("\n-- GenerationSpanData with dict usage (prompt/completion keys) --")
spans = run(GenerationSpanData(
    usage={"prompt_tokens": 200, "completion_tokens": 80},
    model="gpt-4.1", output="done"), capture=False)
s = llm(spans)
check("prompt_tokens → input_tokens=200", s and s.attrs.get("gen_ai.usage.input_tokens") == 200)
check("completion_tokens → output_tokens=80", s and s.attrs.get("gen_ai.usage.output_tokens") == 80)
check("total computed when absent = 280", s and s.attrs.get("gen_ai.usage.total_tokens") == 280)
check("model=gpt-4.1", s and s.attrs.get("gen_ai.request.model") == "gpt-4.1")

print("\n-- No-usage span, capture OFF (nothing to record → no llm_output) --")
spans = run(GenerationSpanData(usage=None, model=None, output="x"), capture=False)
check("no llm_output emitted when no usage and capture off", llm(spans) is None)

print()
if failures:
    print(f"FAILED: {len(failures)} check(s):")
    for f in failures:
        print("  - " + f)
    raise SystemExit(1)
print("ALL CHECKS PASSED")
