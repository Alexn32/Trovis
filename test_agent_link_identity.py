"""A row's LABEL is not its ROUTE.

Reported bug: clicking a work-feed row (and the agent lines that used to sit
in the dashboard's suggestion ribbon) opened a blank screen. Cause: those
payloads put the display name in `agent`, and the UI navigated with it —
GET /agents/Support%20Bot/summary 404s, and the agent page rendered a
back-link over one line of error text, which reads as blank.

So: every agent-shaped row must carry a routable identity next to the human
label, and it has to survive the rebuild steps in the middle (the non-AI
fallback and the Claude-enrichment merge both construct fresh dicts and
silently drop anything they don't copy — which is how this happened).

Run:
  OVERSEE_DISABLE_PRICING_SYNC=1 python3 test_agent_link_identity.py
"""
import os
import tempfile
import time

os.environ.update({
    "OVERSEE_DISABLE_PRICING_SYNC": "1", "TROVIS_DISABLE_PRICING_SYNC": "1",
    "TROVIS_DISABLE_ALERTS": "1", "TROVIS_DISABLE_LOOP_SWEEP": "1",
    "TROVIS_LOOP_TITLES": "off",
})
os.environ.pop("DATABASE_URL", None)
os.environ.pop("ANTHROPIC_API_KEY", None)   # exercises the non-AI fallbacks

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


def kv(d):
    return [{"key": k, "value": {"stringValue": str(v)}} for k, v in d.items()]


NS = 10**9
NOW = time.time_ns()
_n = [0]


def sp(name, off_s, code=1):
    _n[0] += 1
    return {
        "traceId": f"{_n[0]:032d}", "spanId": f"{_n[0]:016d}", "name": name, "kind": 1,
        "startTimeUnixNano": str(NOW - off_s * NS),
        "endTimeUnixNano": str(NOW - off_s * NS + 10**6),
        "status": {"code": code}, "attributes": kv({"gen_ai.request.model": "claude-sonnet-4-6"}),
    }


DISPLAY = "Support Bot"      # what a person reads
SERVICE = "support-agent"    # what a URL needs

with TestClient(main.app) as c:
    r = c.post("/auth/signup", json={
        "email": "link@test.com", "password": "supersecret123",
        "name": "Alex", "account_type": "business", "org_name": "Link Co",
    }).json()
    KEY, TOK = r["api_key"], r["token"]
    H = {"Authorization": f"Bearer {TOK}"}

    # Recent activity (feeds the work feed) plus enough errors to trip the
    # attention classifier.
    spans = [sp("model_call", 120 + i * 30, 2 if i < 8 else 1) for i in range(10)]
    assert c.post("/v1/traces", json={"resourceSpans": [{
        "resource": {"attributes": kv({"service.name": SERVICE})},
        "scopeSpans": [{"spans": spans}]}]},
        headers={"X-Trovis-Api-Key": KEY}).status_code == 200

    # The operator renames it. This is the whole bug: from here on the label
    # and the route are different strings.
    assert c.put(f"/agents/{SERVICE}/display-name", headers=H,
                 json={"agent_id": "main", "display_name": DISPLAY}).status_code == 204

    print("\n--- work feed ---")
    feed = c.get("/dashboard/work-feed", headers=H).json()
    row = next((f for f in feed if f.get("service_name") == SERVICE), None)
    check("the renamed agent is in the feed", row is not None)
    if row:
        check("label is the display name (what a person reads)", row["agent"] == DISPLAY)
        check("route is the service name (what a URL needs)", row["service_name"] == SERVICE)
        check("label and route genuinely differ here", row["agent"] != row["service_name"])
        check("agent_id rides along", row.get("agent_id") == "main")

    print("\n--- attention ---")
    att = c.get("/dashboard/attention", headers=H).json()
    arow = next((a for a in att if a.get("service_name") == SERVICE), None)
    check("the renamed agent is flagged", arow is not None)
    if arow:
        check("label is the display name", arow["agent"] == DISPLAY)
        check("route survives the fallback rebuild", arow["service_name"] == SERVICE)
        check("agent_id rides along", arow.get("agent_id") == "main")

    print("\n--- the route actually resolves (the 404 that caused the blank page) ---")
    ok = c.get(f"/agents/{SERVICE}/summary", headers=H)
    bad = c.get(f"/agents/{DISPLAY}/summary", headers=H)
    check("GET by service_name -> 200", ok.status_code == 200)
    check("GET by display label -> 404 (so the UI must not use it)",
          bad.status_code == 404)
    if row:
        followed = c.get(f"/agents/{row['service_name']}/summary", headers=H)
        check("following the feed row's OWN route works", followed.status_code == 200)
    if arow:
        followed = c.get(f"/agents/{arow['service_name']}/summary", headers=H)
        check("following the attention row's OWN route works", followed.status_code == 200)

    print("\n--- the Claude-enrichment merge keeps identity too ---")
    # attention_items() rebuilds every row around Claude's prose. Identity is
    # ours, never the model's, so it must come back untouched.
    import describer
    flagged = [{
        "agent": DISPLAY, "service_name": SERVICE, "agent_id": "main",
        "severity": "critical", "error_rate_pct": 80.0, "span_count": 10,
        "error_count": 8, "days_since_seen": 0.0, "description": "",
        "last_seen": None,
    }]
    describer._claude_json = lambda *a, **k: {"items": [
        {"agent": DISPLAY, "title": "Elevated error rate", "detail": "d",
         "recommendation": "r", "impact": "i"}
    ]}
    merged = describer.attention_items(flagged)
    check("enriched row keeps its route", merged and merged[0].get("service_name") == SERVICE)
    check("enriched row keeps its agent_id", merged and merged[0].get("agent_id") == "main")
    check("enriched row still reads as the display name", merged and merged[0]["agent"] == DISPLAY)

print("\n" + (f"FAILURES: {failures}" if failures else "All agent-link identity checks passed."))
raise SystemExit(1 if failures else 0)
