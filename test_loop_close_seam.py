"""Regression: the close-ordering seam, replayed from REAL production data.

fixtures_close_seam.json is a captured prod run (2026-07-22, loops 4467 +
phantom 4468): the OpenClaw plugin closes the loop on agent_run_complete,
but defers ending model_call spans up to ~10s waiting for transcript token
usage — so one model_call exported in a LATER batch. Under the v1 rule
("closed loops never accept spans") that straggler re-keyed into a phantom
1-span 'working' loop; production accumulated 1,170 of them, one per
completed run, which is why the Feed showed everything perpetually running.

This test replays the exact prod batches through /v1/traces and asserts the
v2 rule: the loop closes, and the late span attaches to the closed loop
within the grace window — one loop, done, no phantom, state/clock frozen.

Run:
  OVERSEE_DISABLE_PRICING_SYNC=1 python3 test_loop_close_seam.py
(isolated temp SQLite DB; never touches the dev/prod DB)
"""
import json
import os
import tempfile
import time

os.environ["OVERSEE_DISABLE_PRICING_SYNC"] = "1"
os.environ["TROVIS_DISABLE_ALERTS"] = "1"
os.environ["TROVIS_DISABLE_LOOP_SWEEP"] = "1"
os.environ.pop("DATABASE_URL", None)
os.environ.pop("ANTHROPIC_API_KEY", None)

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()

import database
database.SQLITE_PATH = _tmp.name

import main
from fastapi.testclient import TestClient

main._auto_describe = lambda *a, **k: False

failures = []
def check(label, cond):
    print(("  PASS " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


FIX = json.load(open(os.path.join(os.path.dirname(__file__), "fixtures_close_seam.json")))
NS = 1_000_000_000

# Re-time the captured spans to "just now" so the clamp and grace window see
# them the way prod ingestion did (fresh spans, wall-clock-adjacent). The
# relative gaps between spans are preserved exactly.
_base = min(int(s["start_time_unix"]) for s in FIX["spans"])
_shift = (time.time_ns() - 120 * NS) - _base


def otlp_value(v):
    if isinstance(v, bool):
        return {"boolValue": v}
    if isinstance(v, (int, float)):
        return {"doubleValue" if isinstance(v, float) else "intValue": v}
    return {"stringValue": str(v)}


def to_otlp(rows):
    spans = []
    for s in rows:
        attrs = json.loads(s["attributes"])
        spans.append({
            "traceId": s["trace_id"], "spanId": s["span_id"],
            "name": s["span_name"], "kind": s["kind"] or 1,
            "startTimeUnixNano": str(int(s["start_time_unix"]) + _shift),
            "endTimeUnixNano": str(int(s["end_time_unix"]) + _shift),
            "status": {"code": s["status_code"] or 0},
            "attributes": [{"key": k, "value": otlp_value(v)} for k, v in attrs.items()],
        })
    return {"resourceSpans": [{
        "resource": {"attributes": [
            {"key": "service.name", "value": {"stringValue": FIX["pair"]["service_name"]}},
            {"key": "trovis.plugin.version", "value": {"stringValue": "0.5.5"}},
        ]},
        "scopeSpans": [{"spans": spans}],
    }]}


# The prod batches: everything that landed in the done loop (incl. the close
# on agent_run_complete AND the llm_output that arrived after it), then the
# straggler model_call that landed in the phantom.
main_rows = [s for s in FIX["spans"] if s["loop_id"] == FIX["pair"]["done_id"]]
late_rows = [s for s in FIX["spans"] if s["loop_id"] == FIX["pair"]["phantom_id"]]
assert main_rows and late_rows, "fixture must contain both batches"
assert any("trovis.loop.close" in s["attributes"] for s in main_rows)

with TestClient(main.app) as c:
    r = c.post("/auth/signup", json={
        "email": "seam@test.com", "password": "supersecret123",
        "name": "Seam Tester", "account_type": "individual", "org_name": "Seam Co",
    })
    assert r.status_code == 201, r.text
    key = r.json()["api_key"]
    H = {"X-Trovis-Api-Key": key}
    account_id = None  # resolved below via database (single-account test DB)

    # Batch 1: the run's main export — including llm_output AFTER the
    # close-carrying agent_run_complete, exactly as prod ordered it.
    r1 = c.post("/v1/traces", json=to_otlp(main_rows), headers=H)
    check("batch 1 (real prod payload) ingests", r1.status_code == 200
          and r1.json()["spans_received"] == len(main_rows))

    loops_now = database.get_loops(None, limit=50)
    check("one loop, closed done — close honored with llm_output after it",
          len(loops_now) == 1 and loops_now[0]["cached_state"] == "done"
          and loops_now[0]["closed_at"] is not None)
    frozen_last_event = loops_now[0]["last_event_unix"]

    # Batch 2: the straggler model_call (deferred token-usage end, exported
    # ~13s after close in prod). v2 rule: attaches, never reopens.
    r2 = c.post("/v1/traces", json=to_otlp(late_rows), headers=H)
    check("batch 2 (late model_call) ingests", r2.status_code == 200)

    loops_after = database.get_loops(None, limit=50)
    check("NO phantom loop — still exactly one loop", len(loops_after) == 1)
    check("loop stays done (never reopened)",
          loops_after[0]["cached_state"] == "done"
          and loops_after[0]["closed_at"] is not None)
    check("late span attached to the closed loop (record complete)",
          loops_after[0]["span_count"] == len(main_rows) + len(late_rows))
    check("frozen: activity clock unchanged by the late attach",
          loops_after[0]["last_event_unix"] == frozen_last_event)
    stream = database.get_loop_stream(loops_after[0]["id"], None)
    check("exactly one loop_closed event (close never duplicated)",
          sum(1 for e in stream if e["type"] == "loop_closed") == 1)

print()
if failures:
    print(f"FAILED: {len(failures)} check(s):")
    for f in failures:
        print("  - " + f)
    raise SystemExit(1)
print("ALL CHECKS PASSED")
os.unlink(_tmp.name)
