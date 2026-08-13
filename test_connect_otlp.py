"""Door test: raw OTLP/HTTP JSON ingest.

The lowest-level door we claim — "anything that emits OTEL spans works out of
the box, no SDK required". This test speaks the wire format directly: a plain
OTLP/JSON ExportTraceServiceRequest POSTed to /v1/traces with an org API key,
then asserts the agent is visible in GET /agents and the spans are queryable.

No Trovis SDK, no plugin, no framework — if this fails, the OTEL claim is
false.

Run:
  TROVIS_DISABLE_PRICING_SYNC=1 python3 test_connect_otlp.py
(isolated temp SQLite DB; never touches the dev/prod DB)
"""
import os
import tempfile
import time

os.environ["OVERSEE_DISABLE_PRICING_SYNC"] = "1"
os.environ["TROVIS_DISABLE_PRICING_SYNC"] = "1"
os.environ["TROVIS_DISABLE_ALERTS"] = "1"
os.environ["TROVIS_DISABLE_LOOP_SWEEP"] = "1"
os.environ.pop("DATABASE_URL", None)
os.environ.pop("ANTHROPIC_API_KEY", None)

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()

import database
database.SQLITE_PATH = _tmp.name

import describer
import main
from fastapi.testclient import TestClient

main._auto_describe = lambda *a, **k: False

# Stub the Claude boundary. GET /agents/{name}/summary regenerates a missing
# description on read, so a test that merely READS an agent will otherwise
# make a live Anthropic call — not hermetic, and it would burn a real key.
describer.describe_agent = lambda service_name, account_id=None, agent_id=None: {
    "service_name": service_name,
    "description": "Stubbed description.",
    "description_long": "Stubbed long description for a test agent.",
    "span_count_analyzed": 1,
    "source": "telemetry_only",
}
describer.record_summary = lambda user, agent: "Stubbed record summary"

failures = []


def check(label, cond, detail=""):
    print(("  PASS " if cond else "  FAIL ") + label)
    if detail:
        print(f"        {detail}")
    if not cond:
        failures.append(label)


NS = 1_000_000_000
T0 = time.time_ns() - 3600 * NS


def kv(d):
    """OTLP's attribute encoding: a list of {key, value:{<type>Value}}."""
    out = []
    for k, v in d.items():
        if isinstance(v, bool):
            out.append({"key": k, "value": {"boolValue": v}})
        elif isinstance(v, int):
            out.append({"key": k, "value": {"intValue": str(v)}})
        else:
            out.append({"key": k, "value": {"stringValue": str(v)}})
    return out


# A hand-built OTLP/JSON payload — exactly what a generic OTEL exporter emits.
# Two spans under one resource, one of them an error, plus GenAI token-usage
# attributes so the cost path is exercised too.
PAYLOAD = {
    "resourceSpans": [
        {
            "resource": {
                "attributes": kv({
                    "service.name": "raw-otlp-agent",
                    "service.version": "1.2.3",
                })
            },
            "scopeSpans": [
                {
                    "scope": {"name": "manual-instrumentation", "version": "1.0.0"},
                    "spans": [
                        {
                            "traceId": "1" * 32,
                            "spanId": "1" * 16,
                            "name": "fetch_invoice",
                            "kind": 3,
                            "startTimeUnixNano": str(T0),
                            "endTimeUnixNano": str(T0 + 250_000_000),
                            "status": {"code": 1},
                            "attributes": kv({
                                "trovis.event.type": "tool_call",
                                "trovis.tool.name": "fetch_invoice",
                            }),
                        },
                        {
                            "traceId": "1" * 32,
                            "spanId": "2" * 16,
                            "parentSpanId": "1" * 16,
                            "name": "summarize",
                            "kind": 3,
                            "startTimeUnixNano": str(T0 + 300_000_000),
                            "endTimeUnixNano": str(T0 + 900_000_000),
                            "status": {"code": 2, "message": "model timeout"},
                            "attributes": kv({
                                "trovis.event.type": "model_call",
                                "gen_ai.request.model": "claude-sonnet-5",
                                "gen_ai.usage.input_tokens": 1200,
                                "gen_ai.usage.output_tokens": 340,
                            }),
                        },
                    ],
                }
            ],
        }
    ]
}


with TestClient(main.app) as c:
    r = c.post("/auth/signup", json={
        "email": "otlp@test.com", "password": "supersecret123",
        "name": "OTLP Tester", "account_type": "individual", "org_name": "OTLP Co",
    })
    assert r.status_code == 201, r.text
    key = r.json()["api_key"]
    H = {"X-Trovis-Api-Key": key}

    print("\n[1] The door is closed without a key")
    r = c.post("/v1/traces", json=PAYLOAD)
    check("unauthenticated ingest is rejected", r.status_code == 401, f"got {r.status_code}")
    r = c.post("/v1/traces", json=PAYLOAD, headers={"X-Trovis-Api-Key": "ov_sk_not_a_real_key"})
    check("bad API key is rejected", r.status_code == 401, f"got {r.status_code}")

    print("\n[2] Raw OTLP/JSON POST is accepted")
    r = c.post("/v1/traces", json=PAYLOAD, headers=H)
    check("POST /v1/traces returns 200", r.status_code == 200, f"got {r.status_code}: {r.text[:200]}")
    body = r.json()
    check("both spans accepted", body.get("accepted") == 2, f"body={body}")
    check("nothing dropped", body.get("dropped") == 0, f"body={body}")

    print("\n[3] The agent appears in GET /agents")
    agents = c.get("/agents", headers=H).json()
    names = [a["service_name"] for a in agents]
    check("raw-otlp-agent is in the fleet", "raw-otlp-agent" in names, f"agents={names}")

    print("\n[4] The spans actually landed and are queryable")
    s = c.get("/agents/raw-otlp-agent/summary", headers=H)
    check("summary endpoint returns 200", s.status_code == 200, f"got {s.status_code}")
    summary = s.json() if s.status_code == 200 else {}
    check("span_count == 2", summary.get("span_count") == 2, f"span_count={summary.get('span_count')}")
    check("the errored span is counted as an error",
          summary.get("error_count") == 1, f"error_count={summary.get('error_count')}")
    ops = summary.get("top_operations") or []
    check("operation names survived the round trip",
          "fetch_invoice" in ops and "summarize" in ops, f"top_operations={ops}")

    print("\n[5] Span detail survived the OTLP decode")
    detail = c.get("/agents/raw-otlp-agent/spans", headers=H)
    check("GET /agents/{name}/spans returns 200", detail.status_code == 200,
          f"got {detail.status_code}")
    rows = detail.json() if detail.status_code == 200 else []
    check("both spans are retrievable", len(rows) == 2, f"got {len(rows)} row(s)")
    by_name = {r["span_name"]: r for r in rows}
    check("both span names present", set(by_name) == {"fetch_invoice", "summarize"},
          f"names={sorted(by_name)}")
    err = by_name.get("summarize") or {}
    check("error status decoded from OTLP status.code=2 (2 == STATUS_CODE_ERROR)",
          err.get("status_code") == 2, f"status_code={err.get('status_code')!r}")
    check("all spans share the submitted trace id",
          all(r["trace_id"] == "1" * 32 for r in rows),
          f"trace_ids={[r['trace_id'] for r in rows]}")

    print("\n[6] A second batch from the same agent accumulates, not replaces")
    second = {
        "resourceSpans": [
            {
                "resource": {"attributes": kv({"service.name": "raw-otlp-agent"})},
                "scopeSpans": [{"spans": [{
                    "traceId": "3" * 32, "spanId": "3" * 16, "name": "followup",
                    "kind": 1, "startTimeUnixNano": str(T0 + NS),
                    "endTimeUnixNano": str(T0 + NS + 10_000_000),
                    "status": {"code": 1}, "attributes": kv({"trovis.event.type": "tool_call"}),
                }]}],
            }
        ]
    }
    r = c.post("/v1/traces", json=second, headers=H)
    check("second batch accepted", r.json().get("accepted") == 1, f"body={r.json()}")
    summary = c.get("/agents/raw-otlp-agent/summary", headers=H).json()
    check("span_count grew to 3", summary.get("span_count") == 3,
          f"span_count={summary.get('span_count')}")

print()
if failures:
    print(f"FAILED ({len(failures)}): " + "; ".join(failures))
    raise SystemExit(1)
print("RAW OTLP DOOR VERIFIED")
