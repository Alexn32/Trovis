"""Unit tests for the xAI (Grok) adapter.

No network and no xai-sdk install: the SDK is faked where detection matters,
and the span path is driven through a real OTEL TracerProvider with an
in-memory exporter. What's asserted is what a Grok agent actually depends on:

  1. platform="grok" resolves to the xai adapter (and "auto" finds it).
  2. The loop processor stamps set_loop_title() onto xAI-scoped spans only,
     once, so a Grok run lands as NAMED Work.
  3. setup_xai() refuses to claim success on the two silent-failure modes —
     a foreign global TracerProvider (xai_sdk.telemetry.Telemetry()) and
     XAI_SDK_DISABLE_TRACING.

Run:  python3 test_xai.py
"""

from __future__ import annotations

import os
import sys
import types

PKG_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PKG_DIR)

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from trovis import core, xai
from trovis.loop_attrs import mark_handoff, set_loop_title

failures = []


def check(label, cond, detail=""):
    print(("  PASS " if cond else "  FAIL ") + label)
    if detail:
        print(f"        {detail}")
    if not cond:
        failures.append(label)


def fake_xai_sdk_installed(installed: bool):
    """Make `importlib.util.find_spec("xai_sdk")` answer either way, without
    installing (or uninstalling) the real package."""
    if installed:
        mod = types.ModuleType("xai_sdk")
        mod.__spec__ = types.SimpleNamespace(name="xai_sdk")
        mod.__path__ = []
        sys.modules["xai_sdk"] = mod
    else:
        sys.modules.pop("xai_sdk", None)


def trovis_provider():
    """A provider shaped like the one `_setup_otel` builds — the resource
    attribute is how the adapter tells ours from a foreign one."""
    return TracerProvider(
        resource=Resource.create(
            {"service.name": "support-grok", "trovis.sdk.version": "test"}
        )
    )


# --- 1. platform resolution ------------------------------------------------

print("\n[1] platform=\"grok\" is the xAI adapter")
check('"grok" normalizes to "xai"', core._normalize_platform("grok") == "xai")
check('"Grok " normalizes too (case/whitespace)',
      core._normalize_platform("Grok ") == "xai")
check('"xai" passes through', core._normalize_platform("xai") == "xai")
check('unrelated values are untouched',
      core._normalize_platform("openai") == "openai")
check('empty falls back to auto', core._normalize_platform("") == "auto")
check('"xai" is a real span label, not folded to "agent"',
      core._platform_label_for_init("xai") == "xai")

fake_xai_sdk_installed(True)
check("_has_xai() detects an installed xai_sdk", core._has_xai())
check('platform="auto" labels a Grok-only install "xai"',
      core._platform_label_for_init("auto") == "xai"
      or not core._has_openai_agents(),
      "(only meaningful when no other SDK is installed)")
fake_xai_sdk_installed(False)
check("_has_xai() is False without it", not core._has_xai())


# --- 2. the loop processor -------------------------------------------------

print("\n[2] set_loop_title() reaches Grok spans — named Work, not a raw trace")
exporter = InMemorySpanExporter()
provider = trovis_provider()
provider.add_span_processor(xai.GrokLoopProcessor())
provider.add_span_processor(SimpleSpanProcessor(exporter))

grok_tracer = provider.get_tracer("xai_sdk.sync.chat")
other_tracer = provider.get_tracer("my.own.code")

set_loop_title("Triage refund for order #4821")
with grok_tracer.start_as_current_span("chat.sample grok-4.20-non-reasoning"):
    pass
# A second Grok call in the same run must not re-stamp the title: ingest
# reads it at loop creation, and repeating it would title every follow-on.
with grok_tracer.start_as_current_span("chat.sample grok-4.20-non-reasoning"):
    pass

spans = exporter.get_finished_spans()
check("both Grok spans exported", len(spans) == 2, f"got {len(spans)}")
check("the first Grok span carries the title",
      spans[0].attributes.get("trovis.loop.title") == "Triage refund for order #4821",
      f"attrs={dict(spans[0].attributes)}")
check("the second does not (one-shot, as ingest expects)",
      "trovis.loop.title" not in spans[1].attributes)

exporter.clear()
set_loop_title("Should not land on a non-xAI span")
with other_tracer.start_as_current_span("some_other_work"):
    pass
non_xai = exporter.get_finished_spans()[0]
check("a non-xAI span is left alone",
      "trovis.loop.title" not in non_xai.attributes,
      f"attrs={dict(non_xai.attributes)}")

exporter.clear()
with grok_tracer.start_as_current_span("chat.sample grok-4.20-non-reasoning"):
    pass
check("the queued title is still waiting for the next Grok span",
      exporter.get_finished_spans()[0].attributes.get("trovis.loop.title")
      == "Should not land on a non-xAI span")

exporter.clear()
mark_handoff(direction="to_human", target="ops@example.com", reason="needs approval")
with grok_tracer.start_as_current_span("chat.sample grok-4.20-non-reasoning"):
    pass
handoff = exporter.get_finished_spans()[0].attributes
check("a queued handoff rides along too",
      handoff.get("trovis.handoff.direction") == "to_human"
      and handoff.get("trovis.handoff.target_id") == "ops@example.com",
      f"attrs={dict(handoff)}")


# --- 3. the silent-failure modes -------------------------------------------

print("\n[3] setup_xai() tells the truth when telemetry can't flow")
fake_xai_sdk_installed(True)

os.environ["XAI_SDK_DISABLE_TRACING"] = "1"
check("XAI_SDK_DISABLE_TRACING is reported, not ignored", not xai.setup_xai())
os.environ.pop("XAI_SDK_DISABLE_TRACING")

check("a foreign global provider is reported (nothing is global in this test)",
      not xai.setup_xai())

fake_xai_sdk_installed(False)
check("a missing xai-sdk is reported", not xai.setup_xai())

# And the happy path: our provider is global, the SDK is present.
fake_xai_sdk_installed(True)
from opentelemetry import trace

trace.set_tracer_provider(trovis_provider())
check("with the Trovis provider global and the SDK present, setup_xai() is True",
      xai.setup_xai())
check("the loop processor is attached exactly once (idempotent)",
      xai.setup_xai() and xai._INSTALLED)

print()
if failures:
    print(f"FAILED ({len(failures)}): " + "; ".join(failures))
    raise SystemExit(1)
print("XAI (GROK) ADAPTER VERIFIED")
