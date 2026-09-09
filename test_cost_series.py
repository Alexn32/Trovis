"""Tests for the cost page's interactive trend: GET /cost/overview?days=.

The chart plots real days, so the series must be DATED, zero-filled (a quiet
day is a gap in spend, not a gap in the axis), ascending, and exactly `days`
long. Month-to-date must NOT move with the chart window — a 7-day chart still
reports the full month — and the budget writes echo the caller's window so the
page doesn't snap back to 30 days after a save.

Run:
  OVERSEE_DISABLE_PRICING_SYNC=1 python3 test_cost_series.py
(isolated temp SQLite DB; never touches the dev/prod DB)
"""
import os
import tempfile
import time
from datetime import datetime, timedelta, timezone

os.environ["OVERSEE_DISABLE_PRICING_SYNC"] = "1"
os.environ.pop("DATABASE_URL", None)
os.environ.pop("ANTHROPIC_API_KEY", None)

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
import database

database.SQLITE_PATH = _tmp.name

import main
from fastapi.testclient import TestClient

main._auto_describe = lambda *a, **k: False


def otlp_attrs(d):
    out = []
    for k, v in d.items():
        if isinstance(v, int):
            out.append({"key": k, "value": {"intValue": str(v)}})
        else:
            out.append({"key": k, "value": {"stringValue": str(v)}})
    return out


def post_spans(client, key, service, spans):
    payload = {
        "resourceSpans": [
            {
                "resource": {"attributes": otlp_attrs({"service.name": service})},
                "scopeSpans": [{"spans": spans}],
            }
        ]
    }
    return client.post("/v1/traces", json=payload, headers={"X-Trovis-Api-Key": key})


failures = []


def check(label, cond):
    print(("  PASS " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


with TestClient(main.app) as c:
    r = c.post(
        "/auth/signup",
        json={
            "email": "cost@test.com",
            "password": "supersecret123",
            "name": "Cost Tester",
            "account_type": "individual",
            "org_name": "Cost Co",
        },
    )
    assert r.status_code == 201, r.text
    key = r.json()["api_key"]
    H = {"X-Trovis-Api-Key": key}

    now_ns = int(time.time() * 1_000_000_000)
    day_ns = 86_400 * 1_000_000_000
    # Spend today and three days ago; the days between must zero-fill.
    for i, offset in enumerate((0, 3)):
        t = now_ns - offset * day_ns - 3_600_000_000_000
        trace = (f"{i:02d}".encode().hex() * 16)[:32]
        r = post_spans(
            c,
            key,
            "cost-agent",
            [
                {
                    "traceId": trace,
                    "spanId": "b" * 16,
                    "name": "chat",
                    "kind": 1,
                    "startTimeUnixNano": str(t),
                    "endTimeUnixNano": str(t + 5_000_000),
                    "status": {"code": 1},
                    "attributes": otlp_attrs(
                        {
                            "gen_ai.request.model": "claude-haiku-4-5",
                            "gen_ai.usage.input_tokens": 1000,
                            "gen_ai.usage.output_tokens": 500,
                        }
                    ),
                }
            ],
        )
        assert r.status_code == 200, r.text

    for days in (7, 30, 90):
        d = c.get(f"/cost/overview?days={days}", headers=H).json()
        s = d["series"]
        check(f"days={days}: series has {days} points", len(s) == days)
        check(f"days={days}: echoes days", d["days"] == days)
        today = datetime.now(timezone.utc).date()
        check(f"days={days}: last point is today", s[-1]["date"] == today.strftime("%Y-%m-%d"))
        first = (today - timedelta(days=days - 1)).strftime("%Y-%m-%d")
        check(f"days={days}: first point is {days-1} days back", s[0]["date"] == first)
        check(f"days={days}: dates ascend", [p["date"] for p in s] == sorted(p["date"] for p in s))
        check(f"days={days}: daily[] still 30 long", len(d["daily"]) == 30)
        check(f"days={days}: tokens on the busy day", s[-1]["tokens"] == 1500)
        # MTD must not depend on the chart window.
        check(f"days={days}: month_total > 0", d["month_total"] > 0)

    base = c.get("/cost/overview?days=7", headers=H).json()["month_total"]
    wide = c.get("/cost/overview?days=90", headers=H).json()["month_total"]
    check("month_total identical across windows", abs(base - wide) < 1e-9)

    r = c.get("/cost/overview?days=5", headers=H)
    check("days below 7 rejected", r.status_code == 422)
    r = c.get("/cost/overview?days=400", headers=H)
    check("days above 90 rejected", r.status_code == 422)

    r = c.put("/cost/budget?days=7", json={"monthly_budget": 500}, headers=H)
    check("budget write echoes the 7-day window", r.status_code == 200 and len(r.json()["series"]) == 7)
    r = c.put(
        "/cost/agent-budget?days=90",
        json={"service_name": "cost-agent", "agent_id": "main", "monthly_cap": 50},
        headers=H,
    )
    check("agent-cap write echoes the 90-day window", r.status_code == 200 and len(r.json()["series"]) == 90)

print()
print("FAILURES:", failures if failures else "none")
raise SystemExit(1 if failures else 0)
