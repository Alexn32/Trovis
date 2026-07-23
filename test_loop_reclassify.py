"""One-off phantom reclassification tests.

Pre-grace-window stragglers (open, model_call-only, keyed to a run whose
real loop closed just before) get a system-attributed artifact close —
payload carries the truth (ingestion_artifact + pointers), the mechanical
state is 'abandoned', and compute_loop_state can never flip it to done.
Anything off-signature is counted and left alone.

Run:
  OVERSEE_DISABLE_PRICING_SYNC=1 python3 test_loop_reclassify.py
"""
import os
import tempfile
import time

os.environ["OVERSEE_DISABLE_PRICING_SYNC"] = "1"
os.environ["TROVIS_DISABLE_ALERTS"] = "1"
os.environ["TROVIS_DISABLE_LOOP_SWEEP"] = "1"
os.environ["TROVIS_LOOP_TITLES"] = "off"  # no LLM calls from sweeps here
os.environ.pop("DATABASE_URL", None)
os.environ.pop("ANTHROPIC_API_KEY", None)

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()

import database
database.SQLITE_PATH = _tmp.name

import loops
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
def span(name, start, attrs):
    _seq[0] += 1
    return {
        "traceId": f"{_seq[0]:032d}", "spanId": f"{_seq[0]:016d}", "name": name,
        "kind": 1, "startTimeUnixNano": str(start),
        "endTimeUnixNano": str(start + 5_000_000),
        "status": {"code": 1}, "attributes": otlp_attrs(attrs),
    }


def post(client, key, service, spans):
    payload = {"resourceSpans": [{
        "resource": {"attributes": otlp_attrs({
            "service.name": service, "trovis.plugin.version": "1.0.0",
        })},
        "scopeSpans": [{"spans": spans}],
    }]}
    return client.post("/v1/traces", json=payload, headers={"X-Trovis-Api-Key": key})


NS = 1_000_000_000
NOW = time.time_ns()

with TestClient(main.app) as c:
    r = c.post("/auth/signup", json={
        "email": "phantom@test.com", "password": "supersecret123",
        "name": "Phantom Tester", "account_type": "individual", "org_name": "Phantom Co",
    })
    assert r.status_code == 201, r.text
    key = r.json()["api_key"]
    tok = c.post("/auth/login", json={
        "email": "phantom@test.com", "password": "supersecret123",
    }).json()["token"]
    account_id = c.get("/auth/me", headers={"Authorization": f"Bearer {tok}"}).json()["org"]["id"]

    def loops_for(service):
        return sorted(
            (l for l in database.get_loops(account_id, limit=100)
             if l["service_name"] == service),
            key=lambda l: l["id"],
        )

    # --- Build the prod signature: done loop + model_call-only straggler ---
    assert post(c, key, "svc-ph", [span("finish", NOW - 60 * NS, {
        "trovis.run.id": "run-ph", "trovis.loop.close": "done",
    })]).status_code == 200
    # Simulate the pre-grace era: shrink the window so the straggler re-keys
    # into a phantom exactly as production did before the fix.
    _saved = loops.CLOSE_GRACE_S
    loops.CLOSE_GRACE_S = 0
    try:
        assert post(c, key, "svc-ph", [span("model_call", NOW - 45 * NS, {
            "trovis.run.id": "run-ph",
        })]).status_code == 200
        # --- Ambiguous cousin: second loop exists but has a non-model span ---
        assert post(c, key, "svc-amb", [span("finish", NOW - 60 * NS, {
            "trovis.run.id": "run-amb", "trovis.loop.close": "done",
        })]).status_code == 200
        assert post(c, key, "svc-amb", [
            span("model_call", NOW - 45 * NS, {"trovis.run.id": "run-amb"}),
            span("tool_call", NOW - 44 * NS, {"trovis.run.id": "run-amb"}),
        ]).status_code == 200
    finally:
        loops.CLOSE_GRACE_S = _saved

    # --- A real open loop that must be untouched ---
    assert post(c, key, "svc-real", [span("work", NOW - 30 * NS, {
        "trovis.run.id": "run-real",
    })]).status_code == 200

    ph = loops_for("svc-ph")
    amb = loops_for("svc-amb")
    assert len(ph) == 2 and ph[1]["cached_state"] == "working", ph
    assert len(amb) == 2 and amb[1]["cached_state"] == "working", amb

    # --- Run the one-off ---
    result = loops.reclassify_phantom_loops(account_id)
    check("counts: 1 reclassified, 1 skipped-ambiguous, 2 examined",
          result["reclassified"] == 1 and result["skipped_ambiguous"] == 1
          and result["examined"] == 2 and result["already_ran"] == 0)

    ph_after = loops_for("svc-ph")[1]
    check("phantom is terminal (mechanically abandoned)",
          ph_after["cached_state"] == "abandoned" and ph_after["closed_at"])
    stream = database.get_loop_stream(ph_after["id"], account_id)
    close = [e for e in stream if e["type"] == "loop_closed"][0]
    check("artifact close: system-attributed with the truth in the payload",
          close["actor_type"] == "system"
          and close["payload"].get("reason") == "ingestion_artifact"
          and close["payload"].get("straggler_of_run") == "run-ph"
          and close["payload"].get("artifact_of_loop") == loops_for("svc-ph")[0]["id"])
    check("recompute keeps ingestion_artifact terminal as abandoned",
          database.recompute_loop_state_standalone(ph_after["id"], account_id)
          == "abandoned")

    check("ambiguous loop left alone (no guessing)",
          loops_for("svc-amb")[1]["cached_state"] == "working"
          and loops_for("svc-amb")[1]["closed_at"] is None)
    check("real open loop untouched",
          loops_for("svc-real")[0]["cached_state"] == "working")

    # --- Runs-once guard ---
    again = loops.reclassify_phantom_loops(account_id)
    check("runs-once guard: second invocation skips",
          again["already_ran"] == 1 and again["reclassified"] == 0)
    forced = loops.reclassify_phantom_loops(account_id, force=True)
    check("force=True re-examines but is idempotent (phantom already closed)",
          forced["already_ran"] == 0 and forced["reclassified"] == 0)

print()
if failures:
    print(f"FAILED: {len(failures)} check(s):")
    for f in failures:
        print("  - " + f)
    raise SystemExit(1)
print("ALL CHECKS PASSED")
os.unlink(_tmp.name)
