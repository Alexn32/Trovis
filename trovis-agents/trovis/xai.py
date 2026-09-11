"""xAI (Grok) support for Trovis — connecting Grok bots.

The xAI Python SDK (`xai-sdk`) is already OpenTelemetry-instrumented: every
`chat.sample()` / `chat.stream()` / image / tokenizer call opens a span via
`xai_sdk.telemetry.get_tracer`, which resolves the GLOBAL TracerProvider
lazily. `init()` sets that global provider — pointed at Trovis, speaking
OTLP/JSON, authenticated with the org's key — so a Grok bot's spans reach
Trovis with no wrapping at all.

So this adapter does the three things that are NOT automatic:

  1. Confirms the SDK is installed, and that its own kill switch
     (`XAI_SDK_DISABLE_TRACING`) isn't silently swallowing every span.
  2. Warns when something else got to the global provider first — usually
     `xai_sdk.telemetry.Telemetry()`, which installs its OWN provider with
     `service.name="xai-sdk"`. That collapses every Grok bot in an org into
     one indistinguishable agent AND exports protobuf, which Trovis ingest
     (OTLP/JSON) rejects. OTEL refuses to override an already-set global
     provider, so the only fix is ordering: `init()` first.
  3. Stamps workloop attrs onto the first xAI span of a run, so a Grok bot
     lands as *named* Work (`trovis.loop.title`) instead of an untitled
     trace. Use `trovis.set_loop_title("Triage refund #4821")` before the
     call; handoffs queued with `trovis.mark_handoff()` ride along too.

Activation: `init(platform="xai")` (or "grok", or "auto" when `xai-sdk` is
installed). Unlike the monkey-patching adapters, import order inside the
user's own module doesn't matter — `xai_sdk` binds a ProxyTracer at import
time, which resolves to whatever provider is global when the first span
starts.
"""

from __future__ import annotations

import logging
import os

from opentelemetry import trace
from opentelemetry.sdk.trace import SpanProcessor

from trovis.loop_attrs import apply_loop_attrs

logger = logging.getLogger("trovis.xai")

# Instrumentation scopes the xAI SDK opens spans under — every tracer is
# created as `get_tracer(__name__)` from a module inside the `xai_sdk`
# package (xai_sdk.sync.chat, xai_sdk.aio.chat, xai_sdk.sync.image, …).
_XAI_SCOPE_PREFIX = "xai_sdk"

_INSTALLED = False


class GrokLoopProcessor(SpanProcessor):
    """Stamps queued workloop attrs onto xAI SDK spans as they start.

    `set_loop_title()` / `mark_handoff()` queue one-shot attrs; every other
    platform adapter applies them on the span it creates itself. Nobody
    creates the xAI spans but the xAI SDK, so we apply them here instead —
    on the first Grok span after the queue was filled. `apply_loop_attrs`
    consumes what it applies, so later spans in the same run are untouched
    and the title is stamped once, where ingest reads it.
    """

    def on_start(self, span, parent_context=None):  # noqa: ARG002 — OTEL API
        try:
            scope = getattr(span, "instrumentation_scope", None)
            name = getattr(scope, "name", "") or ""
            if not name.startswith(_XAI_SCOPE_PREFIX):
                return
            apply_loop_attrs(span)
        except Exception as e:  # noqa: BLE001 — telemetry never breaks the bot
            logger.debug("[Trovis] could not stamp loop attrs on a Grok span: %s", e)

    def on_end(self, span):  # noqa: ARG002 — nothing to do; export is elsewhere
        return

    def shutdown(self) -> None:
        return

    def force_flush(self, timeout_millis: int = 30000) -> bool:  # noqa: ARG002
        return True


def _has_xai_sdk() -> bool:
    """Detect the xAI SDK without forcing an import."""
    import importlib.util as _u

    return _u.find_spec("xai_sdk") is not None


def _tracing_disabled() -> bool:
    """The SDK's own kill switch. When on, `get_tracer` hands back a
    NoOpTracer and no Grok call ever produces a span — worth saying out
    loud, since from Trovis's side it looks identical to 'agent never ran'."""
    return os.getenv("XAI_SDK_DISABLE_TRACING", "0").lower() in ("1", "true")


def setup_xai() -> bool:
    """Wire xAI SDK (Grok) telemetry into the Trovis pipeline. Idempotent.

    Returns True when Grok spans will flow to Trovis, False when something
    is in the way (SDK missing, tracing disabled, or another provider owns
    the global). Never raises — a telemetry problem must not take down the
    bot.
    """
    global _INSTALLED

    ok = True

    if not _has_xai_sdk():
        logger.warning(
            "[Trovis] xai-sdk not detected. Grok calls won't produce spans "
            "until it's installed: pip install trovis-agents[xai]"
        )
        ok = False

    if _tracing_disabled():
        logger.warning(
            "[Trovis] XAI_SDK_DISABLE_TRACING is set — the xAI SDK is "
            "emitting no spans at all, so this Grok bot will never appear "
            "in Trovis. Unset it to connect."
        )
        ok = False

    provider = trace.get_tracer_provider()
    if not _is_trovis_provider(provider):
        logger.warning(
            "[Trovis] Another OpenTelemetry TracerProvider is already global "
            "(commonly xai_sdk.telemetry.Telemetry(), which names every bot "
            "'xai-sdk' and exports protobuf). OTEL will not let Trovis "
            "replace it, so Grok spans may not reach Trovis. Call "
            "trovis.init() BEFORE creating a Telemetry() — and don't create "
            "one at all; init() does that job."
        )
        ok = False
    elif not _INSTALLED:
        try:
            provider.add_span_processor(GrokLoopProcessor())
            _INSTALLED = True
        except Exception as e:  # noqa: BLE001
            logger.warning("[Trovis] Could not attach the Grok loop processor: %s", e)

    return ok


def _is_trovis_provider(provider) -> bool:
    """True when the global provider is the one `_setup_otel` built — i.e.
    it carries our resource attributes. A ProxyTracerProvider (nothing set
    yet) and a foreign SDK provider both come back False."""
    resource = getattr(provider, "resource", None)
    attrs = getattr(resource, "attributes", None) or {}
    return "trovis.sdk.version" in attrs
