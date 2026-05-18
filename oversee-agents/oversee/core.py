"""Oversee SDK — init() entrypoint for OpenAI Agents.

Wires three things in order:
  1. OpenTelemetry: TracerProvider with an OTLPSpanExporter pointed
     at the Oversee /v1/traces endpoint, authenticated via the
     X-Oversee-Api-Key header.
  2. OpenAI Agents SDK tracing: best-effort registration of the
     openai-agents-opentelemetry processor so SDK-internal spans
     (LLM calls, tool calls, handoffs, guardrails, run completion)
     flow into the same OTEL pipeline. Plus a CaptureProcessor that
     adds Oversee-named content spans when capture_outputs is on.
  3. Agent identity registration: monkey-patches Agent.__init__ so
     each unique agent registers itself once on first construction.

The whole thing is idempotent and never raises on misconfiguration —
worst case is a warning log and partial telemetry.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from oversee.exporter import OTLPJsonSpanExporter
from oversee.registration import (
    CaptureProcessor,
    patch_agent_for_registration,
    set_capture_outputs,
)
from oversee.version import __version__

DEFAULT_ENDPOINT = "https://web-production-e6bc4.up.railway.app/v1/traces"

logger = logging.getLogger("oversee")

# Module-level state. The init() call is safe to repeat — we only set
# up the tracer provider on the first call.
_INITIALIZED = False
_CAPTURE_PROCESSOR: Optional[CaptureProcessor] = None


def init(
    api_key: Optional[str] = None,
    agent_name: Optional[str] = None,
    endpoint: Optional[str] = None,
    capture_outputs: bool = False,
) -> None:
    """Connect this process's OpenAI Agents to Oversee.

    Call once at startup, before constructing any Agent. Idempotent —
    re-calling updates the capture flag but doesn't rebuild the OTEL
    pipeline (would lose buffered spans).

    Args:
        api_key: Oversee API key. Falls back to OVERSEE_API_KEY env var.
            None is allowed for local dev against an unsecured backend.
        agent_name: service.name resource attribute. Falls back to
            OVERSEE_AGENT_NAME env var, then "openai-agent".
        endpoint: OTLP/HTTP traces endpoint. Falls back to
            OVERSEE_ENDPOINT env var, then the Oversee cloud default.
        capture_outputs: When True, the CaptureProcessor emits
            additional Oversee-named spans with the actual message /
            response / tool-result content (truncated to 10 000 chars).
            Off by default for privacy. Falls back to
            OVERSEE_CAPTURE_OUTPUTS env var (case-insensitive "true").
    """
    global _INITIALIZED, _CAPTURE_PROCESSOR

    # Priority: explicit arg → env var → default.
    resolved_endpoint = (
        endpoint
        or os.environ.get("OVERSEE_ENDPOINT")
        or DEFAULT_ENDPOINT
    )
    resolved_api_key = api_key or os.environ.get("OVERSEE_API_KEY")
    resolved_agent_name = (
        agent_name
        or os.environ.get("OVERSEE_AGENT_NAME")
        or "openai-agent"
    )
    resolved_capture = bool(
        capture_outputs
        or os.environ.get("OVERSEE_CAPTURE_OUTPUTS", "").lower() == "true"
    )

    # Setting the capture flag must happen on every call, even when
    # we skip the rest of init (re-init case).
    set_capture_outputs(resolved_capture)

    if _INITIALIZED:
        logger.debug("[Oversee] init() called again — capture flag updated only")
        return

    # 1. OpenTelemetry pipeline.
    resource = Resource.create(
        {
            "service.name": resolved_agent_name,
            "service.version": __version__,
            "oversee.sdk.version": __version__,
            "oversee.sdk.platform": "openai-agents",
        }
    )
    headers = {}
    if resolved_api_key:
        # The Oversee backend reads this header on every authenticated
        # request. Plain string — no special encoding needed.
        headers["X-Oversee-Api-Key"] = resolved_api_key

    exporter = OTLPJsonSpanExporter(
        endpoint=resolved_endpoint,
        headers=headers,
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    # 2. Wire the OpenAI Agents SDK's tracing into our OTEL pipeline.
    _CAPTURE_PROCESSOR = CaptureProcessor()
    _wire_agents_tracing(_CAPTURE_PROCESSOR)

    # 3. Catch each Agent's identity on construction.
    patch_agent_for_registration()

    _INITIALIZED = True

    print(
        f"[Oversee] Connected. Sending telemetry to {resolved_endpoint} "
        f"as '{resolved_agent_name}'"
    )
    if resolved_capture:
        print("[Oversee] Output capture: enabled")


def _wire_agents_tracing(capture_processor: CaptureProcessor) -> None:
    """Best-effort: register the OTEL bridge + our content-capture
    processor with the OpenAI Agents SDK's tracing system.

    Both registrations are independent and fail soft — the OTEL pipe
    is functional even when one (or both) of the agents SDK / its
    OTEL adapter aren't installed. Manual spans still ship.
    """
    # Locate the SDK's processor-registration function. The API name
    # has churned across releases ("add_trace_processor",
    # "add_trace_processors", "set_trace_processors"), so we probe.
    register = _resolve_agents_register()

    # 2a. The official OTEL adapter — emits one OTEL span per
    # internal Agents-SDK span. The package name has settled but we
    # tolerate both common variants in case it ships under a slightly
    # different namespace in some envs.
    otel_processor = _build_official_otel_processor()
    if otel_processor is not None and register is not None:
        try:
            register(otel_processor)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[Oversee] Could not attach OTEL processor: {e}")

    # 2b. Our capture processor — listens to the same SDK events and
    # emits Oversee-named content spans when the flag is on. Always
    # registered; the flag is checked per-event so flipping
    # OVERSEE_CAPTURE_OUTPUTS at runtime takes effect on the next
    # event.
    if register is not None:
        try:
            register(capture_processor)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[Oversee] Could not attach capture processor: {e}")


def _resolve_agents_register():
    """Return the agents-SDK function that registers a tracing
    processor — or None when the SDK isn't installed / exposes neither
    known shape."""
    try:
        from agents import add_trace_processor  # type: ignore[attr-defined]
        return add_trace_processor
    except ImportError:
        pass
    try:
        from agents import add_trace_processors  # type: ignore[attr-defined]
        # add_trace_processors takes a list; adapt to one-at-a-time.
        return lambda p: add_trace_processors([p])
    except ImportError:
        pass
    try:
        from agents import set_trace_processors  # type: ignore[attr-defined]
        # set_trace_processors REPLACES the list, so we accumulate
        # via a module-local store.
        store: list = []

        def _register(p):
            store.append(p)
            set_trace_processors(store)

        return _register
    except ImportError:
        pass

    logger.warning(
        "[Oversee] OpenAI Agents SDK not detected. Agent SDK events will "
        "not produce OTEL spans automatically — only manually-traced spans "
        "and agent registrations will ship. Install with: "
        "pip install openai-agents"
    )
    return None


def _build_official_otel_processor():
    """Try to construct the openai-agents-opentelemetry adapter.
    Returns None when the package isn't installed."""
    for module_name, class_name in (
        ("openai_agents_opentelemetry", "OpenTelemetryTracingProcessor"),
        ("openai_agents_otel", "OpenTelemetryTracingProcessor"),
    ):
        try:
            mod = __import__(module_name, fromlist=[class_name])
            cls = getattr(mod, class_name)
            return cls()
        except (ImportError, AttributeError):
            continue
    logger.warning(
        "[Oversee] openai-agents-opentelemetry not installed. LLM and "
        "tool spans from the Agents SDK won't ship — only Oversee's "
        "agent registration + capture spans. Install with: "
        "pip install openai-agents-opentelemetry"
    )
    return None
