"""The briefing is about the WORK, not the telemetry.

Before this, the prompt was handed agent counts, span counts and error rates —
so the best it could ever say was "5 agents reporting, 24 tasks in the last 24
hours". A person does not open a briefing to learn a span count. The prompt now
also gets named work: real titles, who holds them, how long they have sat.

What must stay true:
  - named work reaches the prompt, bucketed and with ages
  - the CACHE HIT path never fetches work (it is the hot path on every Home
    paint; the whole point of caching the prose is that a hit is cheap)
  - a work query that falls over degrades the prose, never the endpoint
  - /dashboard/briefing stays a sync def (event-loop survival, #129)

Run:
  OVERSEE_DISABLE_PRICING_SYNC=1 python3 test_briefing_voice.py
"""
import inspect
import os
import tempfile
import time

os.environ["OVERSEE_DISABLE_PRICING_SYNC"] = "1"
os.environ["TROVIS_DISABLE_PRICING_SYNC"] = "1"
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


def kv(d):
    return [{"key": k, "value": {"stringValue": str(v)}} for k, v in d.items()]


NS = 1_000_000_000
NOW = time.time_ns()
_seq = [0]


def sp(name, off_s, attrs):
    _seq[0] += 1
    return {
        "traceId": f"{_seq[0]:032d}", "spanId": f"{_seq[0]:016d}", "name": name,
        "kind": 1, "startTimeUnixNano": str(NOW - off_s * NS),
        "endTimeUnixNano": str(NOW - off_s * NS + 1_000_000),
        "status": {"code": 1}, "attributes": kv(attrs),
    }


# Capture whatever the prompt is handed, so we can assert on it directly.
seen = {}
describer.fleet_briefing = lambda stats: (
    seen.update(stats) or {"summary": "Briefing written from named work."}
)

HR = 3600

with TestClient(main.app) as c:
    r = c.post("/auth/signup", json={
        "email": "voice@test.com", "password": "supersecret123",
        "name": "Alex", "account_type": "business", "org_name": "Voice Co",
    }).json()
    KEY, TOK = r["api_key"], r["token"]
    H = {"Authorization": f"Bearer {TOK}"}
    c.post("/team", headers=H, json={"name": "Sarah Chen", "email": "sarah@test.com", "role": "Ops"})

    def post(svc, spans):
        return c.post("/v1/traces", json={"resourceSpans": [{
            "resource": {"attributes": kv({"service.name": svc})},
            "scopeSpans": [{"spans": spans}]}]}, headers={"X-Trovis-Api-Key": KEY})

    # Work waiting on the signed-in person.
    post("contracts-agent", [
        sp("intake", 3 * HR, {"trovis.loop.title": "Countersign the Acme renewal",
                              "trovis.loop.external_id": "v1"}),
        sp("handoff", 2 * HR, {"trovis.loop.external_id": "v1",
                               "trovis.handoff.direction": "to_human",
                               "trovis.handoff.target_id": "voice@test.com",
                               "trovis.handoff.id": "V1"}),
    ])
    # Work stuck on a tool.
    post("billing-agent", [
        sp("intake", 9 * HR, {"trovis.loop.title": "Reconcile the March invoice batch",
                              "trovis.loop.external_id": "v2"}),
        sp("handoff", 8 * HR, {"trovis.loop.external_id": "v2",
                               "trovis.handoff.direction": "to_system",
                               "trovis.handoff.target_id": "Stripe",
                               "trovis.handoff.id": "V2"}),
    ])
    # Work simply moving.
    post("ops-agent", [
        sp("intake", 20 * 60, {"trovis.loop.title": "Draft the Q2 supplier report",
                               "trovis.loop.external_id": "v3"}),
        sp("step", 10 * 60, {"trovis.loop.external_id": "v3"}),
    ])

    print("\n--- named work reaches the prompt ---")
    r1 = c.get("/dashboard/briefing", headers=H)
    check("200", r1.status_code == 200)
    nw = seen.get("named_work") or {}
    check("prompt carries a named_work block", bool(nw))

    BUCKETS = ("waiting_on_you", "stuck", "waiting_on_others", "moving")
    titles = {row["title"] for bucket in BUCKETS for row in nw.get(bucket, [])}
    check("the person's own waiting work is named",
          "Countersign the Acme renewal" in titles)
    check("stuck work is named", "Reconcile the March invoice batch" in titles)
    check("moving work is named", "Draft the Q2 supplier report" in titles)

    you = nw.get("waiting_on_you") or []
    check("waiting_on_you is bucketed separately",
          len(you) == 1 and you[0]["title"] == "Countersign the Acme renewal")
    check("rows carry how long they have sat",
          isinstance(you[0].get("waiting_hours"), float) and you[0]["waiting_hours"] >= 1.5)
    check("rows carry the plain-words status",
          bool((you[0].get("status_note") or "").strip()))
    check("counts ride along for scale",
          (nw.get("counts") or {}).get("needs_you") == 1)

    # Every item the counts describe must actually be shown. Telling the model
    # "N need attention" while handing it fewer than N titles invites it to
    # invent the rest, and hides the person-level patterns entirely.
    attention_titles = [
        r for b in ("stuck", "waiting_on_others") for r in nw.get(b, [])
    ]
    check("attention work is fully represented, not just the stuck half",
          len(attention_titles) >= (nw.get("counts") or {}).get("needs_attention", 0))
    check("rows name who is holding them, so patterns are visible",
          any(r.get("held_by") for r in attention_titles))

    print("\n--- the prose still renders ---")
    check("summary is the generated line",
          r1.json()["summary"] == "Briefing written from named work.")

    print("\n--- the CACHE HIT path does not touch work ---")
    # Every Home paint hits this. If a cache hit started running work queries,
    # caching the prose would have bought nothing.
    calls = {"n": 0}
    real_overview = database.get_work_overview
    database.get_work_overview = lambda *a, **k: (
        calls.__setitem__("n", calls["n"] + 1) or real_overview(*a, **k)
    )
    r2 = c.get("/dashboard/briefing", headers=H)
    database.get_work_overview = real_overview
    check("cached read still 200", r2.status_code == 200)
    check("served from cache (same prose)",
          r2.json()["summary"] == "Briefing written from named work.")
    check("no work query on the cache-hit path", calls["n"] == 0)

    print("\n--- a broken work query degrades prose, not the endpoint ---")
    database.delete_insight = getattr(database, "delete_insight", None)
    # Force a miss by aging the cache out, then break the work lookup.
    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute("DELETE FROM agent_insights WHERE kind = 'briefing'")
        conn.commit()
    seen.clear()

    def boom(*a, **k):
        raise RuntimeError("work store down")

    database.get_work_overview = boom
    r3 = c.get("/dashboard/briefing", headers=H)
    database.get_work_overview = real_overview
    check("briefing still 200 when work is unavailable", r3.status_code == 200)
    check("prompt told there is no named work (not fed a lie)",
          seen.get("named_work") == {})
    check("telemetry half of the snapshot survives",
          "agent_count" in seen and "tasks_last_24h" in seen)

    print("\n--- still off the event loop (#129) ---")
    check("dashboard_briefing is a sync def",
          not inspect.iscoroutinefunction(main.dashboard_briefing))

print("\n" + (f"FAILURES: {failures}" if failures else "All briefing-voice checks passed."))
raise SystemExit(1 if failures else 0)
