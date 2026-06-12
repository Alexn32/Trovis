"""Tests for the Agent Detail redesign backend: the Work Feed records endpoint,
trace grouping, status-with-reason, description v2, and the first_seen fix.

Run:
  OVERSEE_DISABLE_PRICING_SYNC=1 python3 test_agent_records.py
(isolated temp SQLite DB; never touches the dev/prod DB)
"""
import os
import tempfile
import time

os.environ["OVERSEE_DISABLE_PRICING_SYNC"] = "1"
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

# Stub Claude: record summaries + the two-field description (no network).
_summary_calls = {"n": 0}
def _fake_record_summary(user, agent):
    _summary_calls["n"] += 1
    return "Answered a test question"
describer.record_summary = _fake_record_summary
describer.describe_agent = lambda service_name, account_id=None, agent_id=None: {
    "service_name": service_name,
    "description": "Writes marketing content.",
    "description_long": "Routes content requests and enforces brand voice across posts and replies.",
    "span_count_analyzed": 1,
    "source": "telemetry_only",
}

failures = []
def check(label, cond):
    print(("  PASS " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)

def otlp_attrs(d):
    return [{"key": k, "value": {"stringValue": str(v)}} for k, v in d.items()]

def span(trace, sid, name, start_ns, attrs, code=1):
    return {
        "traceId": trace, "spanId": sid, "name": name, "kind": 1,
        "startTimeUnixNano": str(start_ns), "endTimeUnixNano": str(start_ns + 2_000_000_000),
        "status": {"code": code}, "attributes": otlp_attrs(attrs),
    }

def post(client, key, spans):
    payload = {"resourceSpans": [{
        "resource": {"attributes": otlp_attrs({"service.name": "mkt", "trovis.plugin.version": "1.0.0"})},
        "scopeSpans": [{"spans": spans}],
    }]}
    return client.post("/v1/traces", json=payload, headers={"X-Trovis-Api-Key": key})

NOW = int(time.time() * 1_000_000_000)
S = 1_000_000_000

with TestClient(main.app) as c:
    r = c.post("/auth/signup", json={
        "email": "rec@test.com", "password": "supersecret123",
        "name": "Rec", "account_type": "individual", "org_name": "Rec Co",
    })
    assert r.status_code == 201, r.text
    key = r.json()["api_key"]
    H = {"X-Trovis-Api-Key": key}

    # trace A — registration (system record), oldest.
    post(c, key, [span("a" * 32, "a1" + "0" * 14, "agent_registration", NOW - 3000 * S,
                       {"trovis.event.type": "agent_registration", "trovis.agent.id": "main"})])
    # trace B — a clean interaction (ok).
    post(c, key, [
        span("b" * 32, "b1" + "0" * 14, "message_received", NOW - 2000 * S,
             {"trovis.message.content": "what is my x strategy"}),
        span("b" * 32, "b2" + "0" * 14, "llm_output", NOW - 2000 * S + S,
             {"trovis.response.content": "Your X strategy is 70% replies, 30% posts."}),
        span("b" * 32, "b3" + "0" * 14, "gen_ai.response", NOW - 2000 * S + 2 * S, {}),
    ])
    # trace C — an errored interaction, NEWEST (drives status=error).
    post(c, key, [
        span("c" * 32, "c1" + "0" * 14, "message_received", NOW - 100 * S,
             {"trovis.message.content": "charge the card"}),
        span("c" * 32, "c2" + "0" * 14, "charge_card", NOW - 100 * S + S,
             {"trovis.tool.result": "ERROR: declined"}, code=2),
    ])
    # A bogus 2023-dated span (bad clock) — must NOT become first_seen.
    BOGUS_2023 = 1_699_920_000 * S  # ~2023-11-14
    post(c, key, [span("d" * 32, "d1" + "0" * 14, "message_received", BOGUS_2023,
                       {"trovis.message.content": "old"})])

    print("-- /agents/mkt/records --")
    r = c.get("/agents/mkt/records", headers=H)
    check("records → 200", r.status_code == 200)
    body = r.json() if r.status_code == 200 else {}
    recs = body.get("records", [])
    by_summary = {x["summary"]: x for x in recs}

    check("one record per trace (4 traces seeded)", len(recs) == 4)
    # newest first → errored trace C first
    check("newest first (errored interaction leads)",
          recs and recs[0]["error"] is True and recs[0]["kind"] == "interaction")
    reg = next((x for x in recs if x["kind"] == "system"), None)
    check("registration → system record, fixed summary, no exchange",
          reg is not None
          and reg["summary"] == "Registered with the fleet and declared its identity"
          and reg["exchange"] is None)
    okrec = next((x for x in recs if not x["error"] and x["kind"] == "interaction"), None)
    check("interaction → cleaned exchange (user + agent)",
          okrec is not None
          and "x strategy" in (okrec["exchange"]["user"] or "")
          and "70%" in (okrec["exchange"]["agent"] or ""))
    check("interaction summary came from Claude stub",
          okrec is not None and okrec["summary"] == "Answered a test question")
    check("records carry a spans list", okrec is not None and len(okrec["spans"]) >= 2)

    # Summary caching: a second fetch must NOT re-invoke record_summary.
    calls_before = _summary_calls["n"]
    c.get("/agents/mkt/records", headers=H)
    check("record summaries cached (no regen on 2nd fetch)",
          _summary_calls["n"] == calls_before)

    print("-- pagination --")
    r = c.get("/agents/mkt/records?limit=1", headers=H)
    p1 = r.json()
    check("limit=1 → 1 record + next_cursor",
          len(p1["records"]) == 1 and p1["next_cursor"])
    r2 = c.get(f"/agents/mkt/records?limit=1&cursor={p1['next_cursor']}", headers=H)
    p2 = r2.json()
    check("cursor advances to an older, different record",
          len(p2["records"]) == 1 and p2["records"][0]["id"] != p1["records"][0]["id"])

    print("-- /agents/mkt/summary (status + first_seen + description v2) --")
    r = c.get("/agents/mkt/summary", headers=H)
    s = r.json()
    check("summary → 200", r.status_code == 200)
    check("status=error (latest record errored) with a reason",
          s["status"] == "error" and "failed" in s["status_reason"].lower())
    check("status_reason names the errored operation",
          "charge_card" in s["status_reason"])
    check("first_seen ignores the bogus 2023 span (>= 2025)",
          (s.get("first_seen") or "").startswith("202") and s["first_seen"][:4] >= "2025")
    check("description v2 regenerated (short + long)",
          s.get("description") == "Writes marketing content."
          and "brand voice" in (s.get("description_long") or ""))

print()
if failures:
    print(f"FAILED: {len(failures)}")
    for f in failures:
        print("  - " + f)
    raise SystemExit(1)
print("ALL CHECKS PASSED")
