"""Tests for the chronological fleet Work Feed (GET /dashboard/activity +
database.get_fleet_activity).

Run:
  OVERSEE_DISABLE_PRICING_SYNC=1 python3 test_activity_feed.py
(isolated temp SQLite DB; never touches the dev/prod DB)
"""
import os
import tempfile

os.environ["OVERSEE_DISABLE_PRICING_SYNC"] = "1"
os.environ.pop("DATABASE_URL", None)          # force the SQLite branch
os.environ.pop("ANTHROPIC_API_KEY", None)     # keep ingest off the Claude path

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()

import database
database.SQLITE_PATH = _tmp.name              # isolate before init_db runs

import main
from fastapi.testclient import TestClient

main._auto_describe = lambda *a, **k: False   # registration auto-describe → no-op

failures = []
def check(label, cond):
    print(("  PASS " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


def otlp_attrs(d):
    return [{"key": k, "value": {"stringValue": str(v)}} for k, v in d.items()]


def span(name, start, attrs, code=1):
    return {
        "traceId": "a" * 32, "spanId": "b" * 16, "name": name, "kind": 1,
        "startTimeUnixNano": str(start), "endTimeUnixNano": str(start + 5_000_000),
        "status": {"code": code}, "attributes": otlp_attrs(attrs),
    }


def post_spans(client, api_key, service, spans):
    payload = {"resourceSpans": [{
        "resource": {"attributes": otlp_attrs({
            "service.name": service, "trovis.plugin.version": "1.0.0",
        })},
        "scopeSpans": [{"spans": spans}],
    }]}
    return client.post("/v1/traces", json=payload,
                       headers={"X-Trovis-Api-Key": api_key})


B = 1_900_000_000_000_000_000  # fixed ns base (far enough out to clear "now - 24h")

with TestClient(main.app) as c:
    r = c.post("/auth/signup", json={
        "email": "feed@test.com", "password": "supersecret123",
        "name": "Feed Tester", "account_type": "individual", "org_name": "Feed Co",
    })
    assert r.status_code == 201, r.text
    api_key = r.json()["api_key"]
    headers = {"X-Trovis-Api-Key": api_key}

    # alpha-bot: registration (must be excluded), an ok op, and a response-bearing op.
    post_spans(c, api_key, "alpha-bot", [
        span("agent_registration", B + 10, {
            "trovis.event.type": "agent_registration", "trovis.agent.id": "main",
            "trovis.agent.identity": "alpha — mailer",
        }),
        span("fetch_data", B + 20, {}),
        span("send_email", B + 30, {"trovis.response.content": "Sent the welcome email."}),
    ])
    # beta-bot: a failed tool call carrying a tool result + tool name (newest).
    post_spans(c, api_key, "beta-bot", [
        span("charge_card", B + 40, {
            "trovis.tool.name": "stripe_charge",
            "trovis.tool.result": "ERROR: card declined (insufficient_funds)",
        }, code=2),
    ])

    r = c.get("/dashboard/activity", headers=headers)
    check("GET /dashboard/activity → 200", r.status_code == 200)
    items = r.json() if r.status_code == 200 else []

    ops = [it["operation"] for it in items]
    check("registration span excluded", "agent_registration" not in ops)
    check("returns the 3 real work events", len(items) == 3)
    check("newest first (charge_card → send_email → fetch_data)",
          ops == ["charge_card", "send_email", "fetch_data"])

    times = [it["time"] for it in items]
    check("strictly reverse-chronological by time",
          times == sorted(times, reverse=True))

    by_op = {it["operation"]: it for it in items}
    check("response content surfaced with content_type",
          by_op.get("send_email", {}).get("content_type") == "response"
          and "welcome email" in (by_op.get("send_email", {}).get("content") or ""))
    check("error span marked status=error + tool result + tool name",
          by_op.get("charge_card", {}).get("status") == "error"
          and by_op["charge_card"].get("content_type") == "tool_result"
          and by_op["charge_card"].get("tool") == "stripe_charge"
          and "declined" in (by_op["charge_card"].get("content") or ""))
    check("ok span has no content, status ok",
          by_op.get("fetch_data", {}).get("status") == "ok"
          and by_op["fetch_data"].get("content") is None)
    check("agent label falls back to service_name",
          by_op.get("charge_card", {}).get("agent") == "beta-bot")

    r = c.get("/dashboard/activity?limit=1", headers=headers)
    lim = r.json()
    check("?limit=1 honored (1 row, the newest)",
          r.status_code == 200 and len(lim) == 1 and lim[0]["operation"] == "charge_card")

    r = c.get("/dashboard/activity", headers=headers)  # unauth check below
    # IDOR: a second account must see none of account 1's activity.
    r2 = c.post("/auth/signup", json={
        "email": "other@test.com", "password": "supersecret123",
        "name": "Other", "account_type": "individual", "org_name": "Other Co",
    })
    other_key = r2.json()["api_key"]
    r = c.get("/dashboard/activity", headers={"X-Trovis-Api-Key": other_key})
    check("account isolation — other account sees no activity",
          r.status_code == 200 and r.json() == [])

    # unauthenticated (users now exist) → 401
    r = c.get("/dashboard/activity")
    check("unauthenticated → 401", r.status_code == 401)

print()
if failures:
    print(f"FAILED: {len(failures)} check(s):")
    for f in failures:
        print("  - " + f)
    raise SystemExit(1)
print("ALL CHECKS PASSED")
