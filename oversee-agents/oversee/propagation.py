"""Cross-process trace propagation for agent-to-agent calls.

When agent A (one process) calls agent B (another process), Oversee can only
draw the A → B connection if both agents' spans land in the **same trace**.
These helpers carry the trace context across the wire so that happens:

  - On the *calling* side, `inject()` writes a W3C `traceparent` into your
    outbound request headers.
  - On the *receiving* side, `continue_trace()` re-attaches that context so
    the receiver's spans become children of the caller's span — same
    `trace_id`, and B's first span points back at A's span as its parent.

That shared trace + parent link is exactly what the Oversee backend's
connection detector uses to surface "Agent A feeds into Agent B".

W3C Trace Context is the OpenTelemetry default wire format; these are thin
wrappers over `opentelemetry.propagate` so callers don't touch the OTEL API.
Both processes must have called `oversee.init()`.

Example
-------
Caller (agent A), inside a tool/run so a span is active::

    import httpx, oversee
    resp = httpx.post(url, headers=oversee.inject(), json=payload)

Receiver (agent B)::

    with oversee.continue_trace(request.headers):
        result = await Runner.run(agent_b, payload)
"""

from __future__ import annotations

import contextlib
from typing import Any, Iterator, Optional

from opentelemetry import context as _otel_context
from opentelemetry import propagate, trace


def inject(carrier: Optional[dict] = None) -> dict:
    """Inject the current trace context into a carrier dict (typically the
    headers of an outbound agent-to-agent request) and return it.

    Call this while a span is active (inside a tool call / agent run) so
    there's a real parent to link to; otherwise nothing is injected.
    """
    carrier = {} if carrier is None else carrier
    propagate.inject(carrier)
    return carrier


def extract(carrier: Optional[dict]) -> Any:
    """Extract a remote trace context from an incoming carrier (request
    headers). Returns an OpenTelemetry Context. Most callers want
    `continue_trace()`, which attaches it for you."""
    return propagate.extract(carrier or {})


@contextlib.contextmanager
def continue_trace(
    carrier: Optional[dict], span_name: str = "agent.handoff"
) -> Iterator[Any]:
    """Continue the caller's trace on the receiving side.

    Wrap the work an incoming agent-to-agent request triggers. Spans created
    inside the block (including the agent framework's own spans) become
    children of the remote parent and share its `trace_id`, so Oversee links
    the two agents. Yields the linking span.

    No-op-friendly: with no incoming context it simply starts a normal span.
    """
    token = _otel_context.attach(extract(carrier))
    tracer = trace.get_tracer("oversee")
    try:
        with tracer.start_as_current_span(span_name) as span:
            yield span
    finally:
        _otel_context.detach(token)
