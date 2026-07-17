"""Workloop API + sweep tests.

Covers: GET /loops (ordering, state filter, pagination, aggregates incl.
total cost), GET /loops/stalled (age + routing), GET /loops/{id} detail,
POST /loops/{id}/close (session-attributed, api-key 403, idempotent),
cross-account isolation, and the sweep's abandon path (system-attributed
loop_closed past ABANDON_THRESHOLD).

Run:
  OVERSEE_DISABLE_PRICING_SYNC=1 python3 test_loops_api.py
(isolated temp SQLite DB; never touches the dev/prod DB)
"""
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

import loops as loops_mod
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
HOUR = 3600 * NS
NOW = time.time_ns()

with TestClient(main.app) as c:
    r = c.post("/auth/signup", json={
        "email": "api@test.com", "password": "supersecret123",
        "name": "API Tester", "account_type": "individual", "org_name": "API Co",
    })
    assert r.status_code == 201, r.text
    key = r.json()["api_key"]
    token = c.post("/auth/login", json={
        "email": "api@test.com", "password": "supersecret123",
    }).json()["token"]
    HK = {"X-Trovis-Api-Key": key}
    HB = {"Authorization": f"Bearer {token}"}

    # Seed loops:
    #   svc-a: working, carries a reported run cost of $1.25
    #   svc-b: handoff to_human 5h ago  -> stalled (past the 4h threshold)
    #   svc-c: handoff to_human 1h ago  -> awaiting_human
    #   svc-d: idle 49h                 -> stalled now, abandoned by the sweep
    #   svc-e: agent-closed             -> done
    assert post(c, key, "svc-a", [
        span("w", NOW - HOUR, {"trovis.run.id": "ra",
                               "trovis.run.cost_usd": "1.25",
                               "gen_ai.usage.input_tokens": "100",
                               "gen_ai.usage.output_tokens": "50"}),
        span("w2", NOW - HOUR + NS, {"trovis.run.id": "ra"}),
    ]).status_code == 200
    assert post(c, key, "svc-b", [
        span("h", NOW - 5 * HOUR, {"trovis.run.id": "rb",
                                   "trovis.handoff.direction": "to_human"}),
    ]).status_code == 200
    assert post(c, key, "svc-c", [
        span("h", NOW - HOUR, {"trovis.run.id": "rc",
                               "trovis.handoff.direction": "to_human"}),
    ]).status_code == 200
    assert post(c, key, "svc-d", [
        span("old", NOW - 49 * HOUR, {"trovis.run.id": "rd"}),
    ]).status_code == 200
    assert post(c, key, "svc-e", [
        span("fin", NOW - HOUR, {"trovis.run.id": "re", "trovis.loop.close": "done"}),
    ]).status_code == 200

    # --- GET /loops
    all_loops = c.get("/loops", headers=HB).json()
    by_svc = {l["service_name"]: l for l in all_loops}
    check("five loops listed", len(all_loops) == 5)
    ids = [l["id"] for l in all_loops]
    check("newest first (id tiebreak within same second)", ids == sorted(ids, reverse=True))
    check("aggregates: svc-a has 2 spans, 3 events, 1 participant pair",
          by_svc["svc-a"]["span_count"] == 2 and by_svc["svc-a"]["event_count"] == 3
          and by_svc["svc-a"]["participant_count"] >= 2)
    check("total cost = live SUM of span costs (reported $1.25)",
          abs(by_svc["svc-a"]["total_cost_usd"] - 1.25) < 1e-6)
    check("states derived correctly at ingest",
          by_svc["svc-a"]["cached_state"] == "working"
          and by_svc["svc-b"]["cached_state"] == "stalled"
          and by_svc["svc-c"]["cached_state"] == "awaiting_human"
          and by_svc["svc-d"]["cached_state"] == "stalled"
          and by_svc["svc-e"]["cached_state"] == "done")

    done_only = c.get("/loops?state=done", headers=HB).json()
    check("state filter", len(done_only) == 1 and done_only[0]["service_name"] == "svc-e")
    check("unknown state -> 400", c.get("/loops?state=bogus", headers=HB).status_code == 400)
    p1 = c.get("/loops?limit=2&offset=0", headers=HB).json()
    p2 = c.get("/loops?limit=2&offset=2", headers=HB).json()
    check("pagination: disjoint pages",
          len(p1) == 2 and len(p2) == 2
          and not {l["id"] for l in p1} & {l["id"] for l in p2})

    # --- GET /loops/stalled
    stalled = c.get("/loops/stalled", headers=HB).json()
    svc_order = [l["service_name"] for l in stalled]
    check("stalled includes stalled + awaiting_human, oldest first",
          set(svc_order) == {"svc-b", "svc-c", "svc-d"} and svc_order[0] == "svc-d")
    ages = {l["service_name"]: l["stalled_for_s"] for l in stalled}
    check("stall age = now - last event (svc-b ~5h, svc-d ~49h)",
          4 * 3600 < ages["svc-b"] < 6 * 3600 and 48 * 3600 < ages["svc-d"] < 50 * 3600)
    check("/loops/stalled routes (not swallowed by /loops/{id})",
          c.get("/loops/stalled", headers=HB).status_code == 200
          and c.get("/loops/999999", headers=HB).status_code == 404)

    # --- GET /loops/{id} detail
    d = c.get(f"/loops/{by_svc['svc-a']['id']}", headers=HB).json()
    check("detail: participants + full ordered event stream",
          len(d["participants"]) >= 2 and len(d["events"]) == 3
          and d["events"][0]["type"] == "loop_opened"
          and [e["ts"] for e in d["events"]] == sorted(e["ts"] for e in d["events"]))

    # --- Sweep: svc-d (49h idle) gets a system-attributed abandoned close
    me = c.get("/auth/me", headers=HB).json()
    account_id = me["org"]["id"]
    summary = loops_mod.run_sweep_for_account(account_id)
    check("sweep reports the abandon", summary["abandoned"] == 1)
    d_loop = c.get(f"/loops/{by_svc['svc-d']['id']}", headers=HB).json()
    closes = [e for e in d_loop["events"] if e["type"] == "loop_closed"]
    check("abandoned: state + closed_at + system-attributed loop_closed",
          d_loop["cached_state"] == "abandoned" and d_loop["closed_at"]
          and len(closes) == 1 and closes[0]["actor_type"] == "system"
          and closes[0]["payload"].get("reason") == "abandoned")
    check("sweep is idempotent (no second close event)",
          loops_mod.run_sweep_for_account(account_id)["abandoned"] == 0)

    # --- POST /loops/{id}/close
    b_id = by_svc["svc-b"]["id"]
    check("close with api key only -> 403 (no user to attribute)",
          c.post(f"/loops/{b_id}/close", headers=HK).status_code == 403)
    r = c.post(f"/loops/{b_id}/close", headers=HB)
    closes = [e for e in r.json()["events"] if e["type"] == "loop_closed"]
    check("close: done + closed_at + user-attributed loop_closed event",
          r.status_code == 200 and r.json()["cached_state"] == "done"
          and r.json()["closed_at"] and len(closes) == 1
          and closes[0]["actor_type"] == "human"
          and closes[0]["actor"] == str(me["user"]["id"])
          and closes[0]["payload"].get("reason") == "closed_by_user")
    r2 = c.post(f"/loops/{b_id}/close", headers=HB)
    closes2 = [e for e in r2.json()["events"] if e["type"] == "loop_closed"]
    check("double close idempotent (still done, single close event)",
          r2.status_code == 200 and r2.json()["cached_state"] == "done"
          and len(closes2) == 1)

    # --- Cross-account isolation
    r = c.post("/auth/signup", json={
        "email": "other@test.com", "password": "supersecret123",
        "name": "Other", "account_type": "individual", "org_name": "Other Co",
    })
    assert r.status_code == 201, r.text
    tok_b = c.post("/auth/login", json={
        "email": "other@test.com", "password": "supersecret123",
    }).json()["token"]
    HB2 = {"Authorization": f"Bearer {tok_b}"}
    a_id = by_svc["svc-a"]["id"]
    check("account B sees no loops", c.get("/loops", headers=HB2).json() == [])
    check("account B cannot read account A's loop",
          c.get(f"/loops/{a_id}", headers=HB2).status_code == 404)
    check("account B cannot close account A's loop",
          c.post(f"/loops/{a_id}/close", headers=HB2).status_code == 404)
    check("account A's loop untouched",
          c.get(f"/loops/{a_id}", headers=HB).json()["cached_state"] == "working")

print()
if failures:
    print(f"FAILED: {len(failures)} check(s):")
    for f in failures:
        print("  - " + f)
    raise SystemExit(1)
print("ALL CHECKS PASSED")
os.unlink(_tmp.name)
