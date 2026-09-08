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
    account_id = database.validate_api_key(key)["account_id"]

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

    print("\n[7] trovis.loop.title on the creating span → named Work")
    # Lean contract: only title_source=provided counts. A raw OTLP span
    # that carries trovis.loop.title at INSERT is the documented door.
    # Untitled spans above must NOT appear in /work/items.
    untitled_page = c.get("/work/items", headers=H).json()
    check("untitled OTLP spans are not named work",
          untitled_page.get("items") == [],
          f"items={untitled_page.get('items')}")

    titled = {
        "resourceSpans": [{
            "resource": {"attributes": kv({"service.name": "raw-otlp-agent"})},
            "scopeSpans": [{"spans": [{
                "traceId": "4" * 32, "spanId": "4" * 16, "name": "message_received",
                "kind": 1, "startTimeUnixNano": str(T0 + 2 * NS),
                "endTimeUnixNano": str(T0 + 2 * NS + 10_000_000),
                "status": {"code": 1},
                "attributes": kv({
                    "trovis.event.type": "message_received",
                    "trovis.loop.title": "Reconcile invoice 88",
                    "trovis.run.id": "otlp-run-88",
                }),
            }]}],
        }]
    }
    r = c.post("/v1/traces", json=titled, headers=H)
    check("titled span accepted", r.json().get("accepted") == 1, f"body={r.json()}")

    loops = [l for l in database.get_loops(account_id, limit=50)
             if l.get("external_id") == "otlp-run-88"]
    check("exactly one loop keyed to the run id",
          len(loops) == 1, f"got {len(loops)}")
    if loops:
        check("title survived ingest",
              loops[0].get("title") == "Reconcile invoice 88",
              f"title={loops[0].get('title')!r}")
        # get_loops() does not surface title_source; read the row directly.
        with database._connect() as conn, database._cursor(conn) as cur:
            cur.execute("SELECT title_source FROM loops WHERE id = ?",
                        (loops[0]["id"],))
            src = cur.fetchone()["title_source"]
        check("title_source=provided (named work, not generated)",
              src == "provided", f"title_source={src!r}")

    page = c.get("/work/items", headers=H).json()
    titles = [it.get("title") for it in page.get("items") or []]
    check("named work appears in lean GET /work/items",
          "Reconcile invoice 88" in titles, f"titles={titles}")
    check("lean gate unchanged: only the provided-title loop is listed",
          titles == ["Reconcile invoice 88"], f"titles={titles}")

    print("\n[8] untitled open loop adopts a later trovis.loop.title")
    # Creating span has the grouping key only; a later span on the same
    # run.id carries the human title. Ingest must adopt NULL→provided.
    untitled_then_title = {
        "resourceSpans": [{
            "resource": {"attributes": kv({"service.name": "raw-otlp-agent"})},
            "scopeSpans": [{"spans": [{
                "traceId": "5" * 32, "spanId": "5" * 16, "name": "start",
                "kind": 1, "startTimeUnixNano": str(T0 + 3 * NS),
                "endTimeUnixNano": str(T0 + 3 * NS + 10_000_000),
                "status": {"code": 1},
                "attributes": kv({"trovis.run.id": "otlp-adopt-1"}),
            }]}],
        }]
    }
    r = c.post("/v1/traces", json=untitled_then_title, headers=H)
    check("untitled creating span accepted",
          r.json().get("accepted") == 1, f"body={r.json()}")
    loops = [l for l in database.get_loops(account_id, limit=50)
             if l.get("external_id") == "otlp-adopt-1"]
    check("untitled loop created with no title",
          len(loops) == 1 and not (loops[0].get("title") or "").strip(),
          f"loops={loops}")

    later_title = {
        "resourceSpans": [{
            "resource": {"attributes": kv({"service.name": "raw-otlp-agent"})},
            "scopeSpans": [{"spans": [{
                "traceId": "6" * 32, "spanId": "6" * 16, "name": "named",
                "kind": 1, "startTimeUnixNano": str(T0 + 4 * NS),
                "endTimeUnixNano": str(T0 + 4 * NS + 10_000_000),
                "status": {"code": 1},
                "attributes": kv({
                    "trovis.run.id": "otlp-adopt-1",
                    "trovis.loop.title": "Close the books",
                }),
            }]}],
        }]
    }
    r = c.post("/v1/traces", json=later_title, headers=H)
    check("later titled span accepted",
          r.json().get("accepted") == 1, f"body={r.json()}")
    loops = [l for l in database.get_loops(account_id, limit=50)
             if l.get("external_id") == "otlp-adopt-1"]
    check("exactly one loop after adopt",
          len(loops) == 1, f"got {len(loops)}")
    if loops:
        check("NULL→provided adopt",
              loops[0].get("title") == "Close the books",
              f"title={loops[0].get('title')!r}")
        with database._connect() as conn, database._cursor(conn) as cur:
            cur.execute("SELECT title_source FROM loops WHERE id = ?",
                        (loops[0]["id"],))
            src = cur.fetchone()["title_source"]
        check("adopted title_source=provided",
              src == "provided", f"title_source={src!r}")

    print("\n[9] shell titles are rejected; existing provided is not overwritten")
    shell_then_human = {
        "resourceSpans": [{
            "resource": {"attributes": kv({"service.name": "raw-otlp-agent"})},
            "scopeSpans": [{"spans": [{
                "traceId": "7" * 32, "spanId": "7" * 16, "name": "shell",
                "kind": 1, "startTimeUnixNano": str(T0 + 5 * NS),
                "endTimeUnixNano": str(T0 + 5 * NS + 10_000_000),
                "status": {"code": 1},
                "attributes": kv({
                    "trovis.loop.external_id": "otlp-shell-1",
                    "trovis.loop.title": "Task from raw-otlp-agent",
                }),
            }]}],
        }]
    }
    r = c.post("/v1/traces", json=shell_then_human, headers=H)
    check("shell creating span accepted",
          r.json().get("accepted") == 1, f"body={r.json()}")
    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute(
            "SELECT title, title_source FROM loops WHERE external_id = ?",
            ("otlp-shell-1",),
        )
        shell_row = dict(cur.fetchone())
    check("shell title rejected at INSERT (not provided)",
          shell_row.get("title_source") != "provided"
          and not (shell_row.get("title") or "").strip(),
          f"row={shell_row}")

    # Template-style shell on a later span of the untitled loop: still reject.
    template_shell = {
        "resourceSpans": [{
            "resource": {"attributes": kv({"service.name": "raw-otlp-agent"})},
            "scopeSpans": [{"spans": [{
                "traceId": "8" * 32, "spanId": "8" * 16, "name": "tmpl",
                "kind": 1, "startTimeUnixNano": str(T0 + 6 * NS),
                "endTimeUnixNano": str(T0 + 6 * NS + 10_000_000),
                "status": {"code": 1},
                "attributes": kv({
                    "trovis.loop.external_id": "otlp-shell-1",
                    "trovis.loop.title": "raw-otlp-agent · exec · 3 actions",
                }),
            }]}],
        }]
    }
    r = c.post("/v1/traces", json=template_shell, headers=H)
    check("template-shell span accepted",
          r.json().get("accepted") == 1, f"body={r.json()}")
    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute(
            "SELECT title, title_source FROM loops WHERE external_id = ?",
            ("otlp-shell-1",),
        )
        shell_row = dict(cur.fetchone())
    check("template shell rejected at adopt (still untitled)",
          shell_row.get("title_source") != "provided"
          and not (shell_row.get("title") or "").strip(),
          f"row={shell_row}")

    # Existing provided title must not be overwritten by a later title.
    clobber = {
        "resourceSpans": [{
            "resource": {"attributes": kv({"service.name": "raw-otlp-agent"})},
            "scopeSpans": [{"spans": [{
                "traceId": "9" * 32, "spanId": "9" * 16, "name": "clobber",
                "kind": 1, "startTimeUnixNano": str(T0 + 7 * NS),
                "endTimeUnixNano": str(T0 + 7 * NS + 10_000_000),
                "status": {"code": 1},
                "attributes": kv({
                    "trovis.run.id": "otlp-run-88",
                    "trovis.loop.title": "Nope, a different name",
                }),
            }]}],
        }]
    }
    r = c.post("/v1/traces", json=clobber, headers=H)
    check("clobber span accepted",
          r.json().get("accepted") == 1, f"body={r.json()}")
    loops = [l for l in database.get_loops(account_id, limit=50)
             if l.get("external_id") == "otlp-run-88"]
    check("existing provided title is not overwritten",
          len(loops) == 1 and loops[0].get("title") == "Reconcile invoice 88",
          f"title={loops[0].get('title')!r}" if loops else "no loop")

    page = c.get("/work/items", headers=H).json()
    titles = [it.get("title") for it in page.get("items") or []]
    check("lean items include adopted provided title",
          "Close the books" in titles, f"titles={titles}")
    check("lean items still exclude shells and do not list the clobber",
          "Task from raw-otlp-agent" not in titles
          and "raw-otlp-agent · exec · 3 actions" not in titles
          and "Nope, a different name" not in titles,
          f"titles={titles}")
    check("lean overview still only counts provided titles",
          set(titles) == {"Reconcile invoice 88", "Close the books"},
          f"titles={titles}")

print()
if failures:
    print(f"FAILED ({len(failures)}): " + "; ".join(failures))
    raise SystemExit(1)
print("RAW OTLP DOOR VERIFIED")
