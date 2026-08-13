"""Ingest honesty tests — /v1/traces reports what it threw away.

A span with no service.name can't be attributed to an agent, so ingest
discards it. It used to do that behind a bare {"status": "ok"}: the caller
saw success, the spans never appeared in the dashboard, and nothing on the
wire said why. These tests pin the honest shape — accepted, dropped, and a
one-line reason — and that partial acceptance still stores the good spans.

Run:
  TROVIS_DISABLE_PRICING_SYNC=1 python3 test_ingest_drop_reporting.py
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


def otlp_attrs(d):
    return [{"key": k, "value": {"stringValue": str(v)}} for k, v in d.items()]


_seq = [0]


def span(name):
    _seq[0] += 1
    start = time.time_ns() - 3600 * 1_000_000_000
    return {
        "traceId": f"{_seq[0]:032d}",
        "spanId": f"{_seq[0]:016d}",
        "name": name,
        "kind": 1,
        "startTimeUnixNano": str(start),
        "endTimeUnixNano": str(start + 5_000_000),
        "status": {"code": 1},
        "attributes": otlp_attrs({"trovis.event.type": "tool_call"}),
    }


def resource_block(service_name, spans):
    """One resourceSpans entry. service_name=None omits the attribute
    entirely — the exact shape an SDK emits when it never set one."""
    attrs = {"trovis.plugin.version": "1.0.0"}
    if service_name is not None:
        attrs["service.name"] = service_name
    return {
        "resource": {"attributes": otlp_attrs(attrs)},
        "scopeSpans": [{"spans": spans}],
    }


def post(client, key, blocks):
    return client.post(
        "/v1/traces",
        json={"resourceSpans": blocks},
        headers={"X-Trovis-Api-Key": key},
    )


with TestClient(main.app) as c:
    r = c.post("/auth/signup", json={
        "email": "drops@test.com", "password": "supersecret123",
        "name": "Drop Tester", "account_type": "individual", "org_name": "Drop Co",
    })
    assert r.status_code == 201, r.text
    key = r.json()["api_key"]

    print("\n[1] Mixed batch: one good span, one nameless span")
    r = post(c, key, [
        resource_block("good-agent", [span("does_work")]),
        resource_block(None, [span("orphan")]),
    ])
    check("batch is accepted, not rejected", r.status_code == 200, f"got {r.status_code}")
    body = r.json()
    check("status is ok", body.get("status") == "ok", f"body={body}")
    check("accepted == 1", body.get("accepted") == 1, f"body={body}")
    check("dropped == 1", body.get("dropped") == 1, f"body={body}")
    check(
        "reason names the cause",
        body.get("reason") == "missing service.name",
        f"reason={body.get('reason')!r}",
    )

    print("\n[2] The good span actually landed (partial acceptance is real)")
    agents = c.get("/agents", headers={"X-Trovis-Api-Key": key}).json()
    names = [a["service_name"] for a in agents]
    check("good-agent is visible in GET /agents", "good-agent" in names, f"agents={names}")
    summary = c.get("/agents/good-agent/summary", headers={"X-Trovis-Api-Key": key})
    check("good-agent summary reports its span", summary.status_code == 200
          and summary.json().get("span_count", 0) >= 1,
          f"status={summary.status_code} span_count={summary.json().get('span_count')}")

    print("\n[3] The nameless span left no agent behind")
    check("no empty-named agent was created",
          all(n for n in names), f"agents={names}")

    print("\n[4] Clean batch omits `reason` entirely")
    r = post(c, key, [resource_block("clean-agent", [span("a"), span("b")])])
    body = r.json()
    check("accepted == 2", body.get("accepted") == 2, f"body={body}")
    check("dropped == 0", body.get("dropped") == 0, f"body={body}")
    check("reason absent when nothing dropped", "reason" not in body, f"body={body}")

    print("\n[5] dropped counts SPANS, not resource blocks")
    # service.name lives on the resource, so one nameless block can carry
    # many spans. Reporting "1" here would understate the loss.
    r = post(c, key, [
        resource_block("good-agent", [span("ok")]),
        resource_block(None, [span("x"), span("y"), span("z")]),
    ])
    body = r.json()
    check("accepted == 1", body.get("accepted") == 1, f"body={body}")
    check("dropped == 3 (all spans in the nameless block)",
          body.get("dropped") == 3, f"body={body}")

    print("\n[6] Empty-string service.name is treated as missing")
    r = post(c, key, [resource_block("", [span("blank")])])
    body = r.json()
    check("accepted == 0", body.get("accepted") == 0, f"body={body}")
    check("dropped == 1", body.get("dropped") == 1, f"body={body}")
    check("reason present", body.get("reason") == "missing service.name", f"body={body}")

    print("\n[7] All-dropped batch is still 200/ok, not an error")
    r = post(c, key, [resource_block(None, [span("lonely")])])
    check("status_code 200", r.status_code == 200, f"got {r.status_code}")
    check("status still ok", r.json().get("status") == "ok", f"body={r.json()}")

print()
if failures:
    print(f"FAILED ({len(failures)}): " + "; ".join(failures))
    raise SystemExit(1)
print("ALL INGEST DROP-REPORTING TESTS PASSED")
