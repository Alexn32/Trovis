"""OTLP/JSON span exporter.

The Python `opentelemetry-exporter-otlp-proto-http` package only emits
protobuf-encoded payloads. Our Trovis backend (and the OpenClaw plugin
that the dashboard is built around) speaks OTLP/JSON — protobuf would
be a 400. This module ships a minimal JSON exporter so the Python SDK
talks the same dialect as the rest of the stack.

The encoding follows the OTLP "JSON Protobuf Encoding" spec — each
attribute value is wrapped in a typed key (`stringValue`, `intValue`,
…), and IDs/timestamps are hex / decimal strings.
"""

from __future__ import annotations

import logging
from typing import Any, Sequence

import requests
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

logger = logging.getLogger("trovis.exporter")


def _attr_value(value: Any) -> dict[str, Any]:
    """Wrap a primitive into the OTLP AnyValue shape."""
    # bool MUST be checked before int — bool is a subclass of int.
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        # OTLP requires int64 as a string in JSON to survive JS-style
        # number precision loss on the wire.
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, str):
        return {"stringValue": value}
    if isinstance(value, (list, tuple)):
        return {"arrayValue": {"values": [_attr_value(x) for x in value]}}
    # Bytes, dicts, custom objects — fall back to repr so we always
    # produce something rather than silently dropping the attribute.
    return {"stringValue": str(value)}


def _attributes(attrs: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not attrs:
        return []
    return [{"key": k, "value": _attr_value(v)} for k, v in attrs.items()]


def _hex_id(value: int, byte_len: int) -> str:
    """Lowercase hex, zero-padded. Trace IDs are 16 bytes (32 hex chars),
    span IDs are 8 bytes (16 hex chars)."""
    return format(value, f"0{byte_len * 2}x")


def _kind_value(kind: Any) -> int:
    """OTLP SpanKind enum value. The SDK's SpanKind is an IntEnum, so
    int() works directly."""
    try:
        return int(kind.value) if hasattr(kind, "value") else int(kind)
    except (TypeError, ValueError):
        return 0


def _status(status: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    code = getattr(getattr(status, "status_code", None), "value", None)
    if code is not None:
        out["code"] = int(code)
    msg = getattr(status, "description", None)
    if msg:
        out["message"] = msg
    return out


def _encode(spans: Sequence[ReadableSpan]) -> dict[str, Any]:
    """Group by (resource, instrumentation scope) and produce an OTLP
    ExportTraceServiceRequest JSON body. Identical structure to what
    the JS exporter — and thus the OpenClaw plugin — emits."""
    # First bucket spans by resource (so resources share one entry),
    # then within that bucket by instrumentation scope.
    by_resource: dict[Any, dict[str, list[ReadableSpan]]] = {}
    resource_objs: dict[Any, Any] = {}

    for span in spans:
        # Resource attributes is a dict-like; freeze as items for keying.
        resource = span.resource
        key = id(resource)  # same Resource instance → same bucket
        resource_objs.setdefault(key, resource)
        scope_buckets = by_resource.setdefault(key, {})

        scope_name = ""
        scope = getattr(span, "instrumentation_scope", None)
        if scope is not None:
            scope_name = getattr(scope, "name", "") or ""
        scope_buckets.setdefault(scope_name, []).append(span)

    resource_spans = []
    for key, scope_buckets in by_resource.items():
        resource = resource_objs[key]
        resource_attrs = dict(resource.attributes or {})

        scope_spans = []
        for scope_name, scope_span_list in scope_buckets.items():
            spans_json = []
            for span in scope_span_list:
                ctx = span.get_span_context()
                parent = getattr(span, "parent", None)
                spans_json.append(
                    {
                        "traceId": _hex_id(ctx.trace_id, 16),
                        "spanId": _hex_id(ctx.span_id, 8),
                        "parentSpanId": (
                            _hex_id(parent.span_id, 8) if parent else ""
                        ),
                        "name": span.name or "",
                        "kind": _kind_value(span.kind),
                        # ns counts as strings — OTLP/JSON convention.
                        "startTimeUnixNano": str(span.start_time or 0),
                        "endTimeUnixNano": str(span.end_time or 0),
                        "attributes": _attributes(dict(span.attributes or {})),
                        "status": _status(span.status),
                    }
                )
            scope_spans.append(
                {
                    "scope": {"name": scope_name},
                    "spans": spans_json,
                }
            )

        resource_spans.append(
            {
                "resource": {"attributes": _attributes(resource_attrs)},
                "scopeSpans": scope_spans,
            }
        )

    return {"resourceSpans": resource_spans}


class OTLPJsonSpanExporter(SpanExporter):
    """OTLP/HTTP span exporter that ships JSON (not protobuf).

    Mirrors the wire format the Node.js `@opentelemetry/
    exporter-trace-otlp-http` package emits by default — which is
    what the Trovis backend already understands.
    """

    def __init__(
        self,
        endpoint: str,
        headers: dict[str, str] | None = None,
        timeout_sec: float = 10.0,
    ) -> None:
        self._endpoint = endpoint
        self._headers = {"Content-Type": "application/json"}
        if headers:
            self._headers.update(headers)
        self._timeout_sec = timeout_sec
        self._shutdown = False

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        if self._shutdown:
            return SpanExportResult.FAILURE
        if not spans:
            return SpanExportResult.SUCCESS

        try:
            payload = _encode(spans)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[Trovis] OTLP/JSON encode failed: {e}")
            return SpanExportResult.FAILURE

        try:
            resp = requests.post(
                self._endpoint,
                json=payload,
                headers=self._headers,
                timeout=self._timeout_sec,
            )
        except requests.RequestException as e:
            logger.warning(f"[Trovis] OTLP/JSON HTTP error: {e}")
            return SpanExportResult.FAILURE

        if 200 <= resp.status_code < 300:
            return SpanExportResult.SUCCESS
        logger.warning(
            f"[Trovis] OTLP/JSON export rejected: {resp.status_code} "
            f"{resp.text[:200]}"
        )
        return SpanExportResult.FAILURE

    def shutdown(self) -> None:
        self._shutdown = True

    def force_flush(self, timeout_millis: int = 30000) -> bool:  # noqa: ARG002
        # We don't buffer — every export() call ships synchronously.
        return True
