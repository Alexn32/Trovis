"""The briefing card must agree with itself.

The prose is a cached LLM rendering of the task counts as they were when it
was written; the stat underneath was recomputed live on every request. Both
read `tasks_yesterday` — a ROLLING 24-hour window, which FALLS as old spans
age out — so the same card routinely contradicted itself: 23,607 tasks in
the sentence, 23,605 in the number directly beneath it.

A cached summary now carries the counts it was generated from, and the two
are served together. Fresh counts are only paired with freshly generated
prose.

Separate file because the MCP session manager can only be started once per
process, so a second TestClient context in an existing suite fails.

Run:
  OVERSEE_DISABLE_PRICING_SYNC=1 python3 test_briefing_snapshot.py
"""
import os
import tempfile
import time

os.environ["OVERSEE_DISABLE_PRICING_SYNC"] = "1"
os.environ["TROVIS_DISABLE_ALERTS"] = "1"
os.environ["TROVIS_DISABLE_LOOP_SWEEP"] = "1"
os.environ["TROVIS_LOOP_TITLES"] = "off"
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

failures = []
def check(label, cond):
    print(("  PASS " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


def otlp_attrs(d):
    return [{"key": k, "value": {"stringValue": str(v)}} for k, v in d.items()]


_seq = [0]
def span(start):
    _seq[0] += 1
    return {
        "traceId": f"{_seq[0]:032d}", "spanId": f"{_seq[0]:016d}", "name": "op",
        "kind": 1, "startTimeUnixNano": str(start),
        "endTimeUnixNano": str(start + 1_000_000),
        "status": {"code": 1},
        "attributes": otlp_attrs({"trovis.run.id": f"r{_seq[0]}"}),
    }


NS = 1_000_000_000
NOW = time.time_ns()

# Stub the LLM so the prose provably echoes the counts it was handed — that is
# what makes drift between prose and stat detectable at all.
describer.fleet_briefing = lambda stats: {
    "summary": f"Ran {stats['tasks_last_24h']} tasks."
}

with TestClient(main.app) as c:
    r = c.post("/auth/signup", json={
        "email": "brief@test.com", "password": "supersecret123",
        "name": "B", "account_type": "individual", "org_name": "B Co",
    }).json()
    KEY, TOK = r["api_key"], r["token"]
    H = {"Authorization": f"Bearer {TOK}"}

    def post(spans):
        return c.post("/v1/traces", json={"resourceSpans": [{
            "resource": {"attributes": otlp_attrs({"service.name": "brief-bot"})},
            "scopeSpans": [{"spans": spans}],
        }]}, headers={"X-Trovis-Api-Key": KEY})

    assert post([span(NOW - 60 * NS) for _ in range(3)]).status_code == 200

    print("\n--- first read: prose and stat are generated together ---")
    first = c.get("/dashboard/briefing", headers=H).json()
    check("the stat matches what the prose says",
          first["summary"] == f"Ran {first['tasks_yesterday']} tasks.")
    check("and it reflects the spans just ingested",
          first["tasks_yesterday"] == 3)

    print("\n--- more work lands; the prose is still cached ---")
    assert post([span(NOW - 30 * NS) for _ in range(5)]).status_code == 200
    second = c.get("/dashboard/briefing", headers=H).json()
    check("the cached prose is served, not regenerated",
          second["summary"] == first["summary"])
    check("THE CARD AGREES WITH ITSELF — stat still matches its prose",
          second["summary"] == f"Ran {second['tasks_yesterday']} tasks.")
    check("the served counts are the snapshot the prose was written from",
          second["tasks_yesterday"] == first["tasks_yesterday"])
    check("the live count really did move (the drift was real)",
          database.count_fleet_spans(
              database.get_user_by_email("brief@test.com")["account_id"],
              NOW - 24 * 3600 * NS, activity_only=True) == 8)

    print("\n--- a legacy cache entry with no counts still renders ---")
    acct = database.get_user_by_email("brief@test.com")["account_id"]
    database.save_insight(
        account_id=acct, service_name=main._DASHBOARD_SENTINEL, agent_id="main",
        kind="briefing", data={"summary": "Legacy entry with no counts."},
    )
    legacy = c.get("/dashboard/briefing", headers=H).json()
    check("falls back to live counts rather than erroring or showing zero",
          legacy["summary"] == "Legacy entry with no counts."
          and legacy["tasks_yesterday"] == 8)

print()
if failures:
    print(f"FAILED ({len(failures)}):")
    for f in failures:
        print("  - " + f)
    raise SystemExit(1)
print("All briefing-snapshot checks passed.")
os.unlink(_tmp.name)
