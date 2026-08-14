"""Error classification: the read-model exclusion for known-mislabeled spans.

OpenClaw plugin <= 0.6.0 allowlisted the model-call outcomes "ok"/"success"
and flagged everything else. OpenClaw's success value is "completed", so
EVERY successful model call was ingested with status_code=2 and
status_message='completed'. Production audit 2026-08-14: 103,311 spans,
100% of them mislabeled, producing a fleet error rate of 43.5% against a
true 1.6% — 96.4% of every "error" in the system.

The spans are never rewritten (append-only record). The INTERPRETATION
excludes that exact signature everywhere an error is counted.

Covers:
  - a mislabeled span is not counted as an error, anywhere
  - a genuinely failed span still is, including one from the same agent
  - the exclusion is narrow: only status_code=2 AND message='completed'
  - SQL and Python paths agree (database.is_error_span vs the aggregates)
  - error RATE reflects the exclusion end to end through the API

Run:
  OVERSEE_DISABLE_PRICING_SYNC=1 python3 test_error_classification.py
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
def span(name, start, attrs, code=1, msg=""):
    _seq[0] += 1
    return {
        "traceId": f"{_seq[0]:032d}", "spanId": f"{_seq[0]:016d}", "name": name,
        "kind": 1, "startTimeUnixNano": str(start),
        "endTimeUnixNano": str(start + 5_000_000),
        "status": {"code": code, "message": msg}, "attributes": otlp_attrs(attrs),
    }


def post(client, key, service, spans):
    return client.post("/v1/traces", json={"resourceSpans": [{
        "resource": {"attributes": otlp_attrs({"service.name": service})},
        "scopeSpans": [{"spans": spans}],
    }]}, headers={"X-Trovis-Api-Key": key})


NS = 1_000_000_000
NOW = time.time_ns()
ERR = "STATUS_CODE_ERROR"

print("\n--- database.is_error_span (the row-level twin) ---")
check("a mislabeled 'completed' error is NOT an error",
      database.is_error_span(
          {"status_code": 2, "status_message": "completed"}) is False)
check("a genuine error IS an error",
      database.is_error_span(
          {"status_code": 2, "status_message": "connection refused"}) is True)
check("a genuine error with an EMPTY message is still an error",
      database.is_error_span({"status_code": 2, "status_message": ""}) is True)
check("an OK span is never an error",
      database.is_error_span(
          {"status_code": 1, "status_message": "completed"}) is False)
check("the exclusion is narrow — 'completed successfully' is NOT excluded",
      database.is_error_span(
          {"status_code": 2, "status_message": "completed successfully"}) is True)

with TestClient(main.app) as c:
    r = c.post("/auth/signup", json={
        "email": "err@test.com", "password": "supersecret123",
        "name": "Err", "account_type": "individual", "org_name": "Err Co",
    }).json()
    KEY, TOK = r["api_key"], r["token"]
    H = {"Authorization": f"Bearer {TOK}"}

    # Reproduce the production shape in miniature: 10 model calls that all
    # SUCCEEDED but were ingested as errors by the old plugin, plus 1 genuine
    # tool failure. Old behavior: 11/12 = 91.7% error rate. True: 1/12 = 8.3%.
    spans = [span("model_call", NOW - (100 - i) * NS, {"trovis.run.id": "r1"},
                  code=ERR, msg="completed") for i in range(10)]
    spans.append(span("tool_call", NOW - 5 * NS,
                      {"trovis.run.id": "r1", "trovis.tool.name": "exec"},
                      code=ERR, msg="connection refused"))
    spans.append(span("message_received", NOW - 110 * NS, {"trovis.run.id": "r1"}))
    assert post(c, KEY, "mislabel-bot", spans).status_code == 200

    print("\n--- the aggregate the whole dashboard reads from ---")
    g = [x for x in c.get("/agents", headers=H).json()
         if x["service_name"] == "mislabel-bot"][0]
    check("all 12 spans are recorded — nothing was dropped from the record",
          g["total_spans"] == 12)
    check("only the GENUINE failure counts as an error (1, not 11)",
          g["total_errors"] == 1)
    rate = 100.0 * g["total_errors"] / g["total_spans"]
    check(f"error rate is the true 8.3%, not the mislabeled 91.7% (got {rate:.1f}%)",
          abs(rate - 8.3) < 0.2)

    print("\n--- agent summary + weekly agree with the fleet aggregate ---")
    s = c.get("/agents/mislabel-bot/summary", headers=H).json()
    check("summary error_count matches the fleet aggregate",
          s.get("error_count") == 1)
    check("summary span_count matches too", s.get("span_count") == 12)

    print("\n--- the record itself is untouched ---")
    raw = c.get("/agents/mislabel-bot/spans?limit=50", headers=H).json()
    mislabeled = [x for x in raw
                  if x.get("status_code") == 2 and x.get("status_message") == "completed"]
    check("the 10 mislabeled spans are STILL in the record, unmodified",
          len(mislabeled) == 10)
    check("their status_code is still 2 — we never rewrote history",
          all(x["status_code"] == 2 for x in mislabeled))

    print("\n--- a healthy agent stays at zero ---")
    assert post(c, KEY, "clean-bot", [
        span("model_call", NOW - 50 * NS, {"trovis.run.id": "r2"},
             code=ERR, msg="completed"),
        span("model_call", NOW - 40 * NS, {"trovis.run.id": "r2"}),
    ]).status_code == 200
    g2 = [x for x in c.get("/agents", headers=H).json()
          if x["service_name"] == "clean-bot"][0]
    check("an agent whose only 'errors' were mislabels reads 0 errors",
          g2["total_errors"] == 0 and g2["total_spans"] == 2)

# ---------------------------------------------------------------------------
# Severity decay + prose-safe payload
# ---------------------------------------------------------------------------
# Severity used to be decided by error rate ALONE: the `info` branch for a
# dormant agent was unreachable whenever the agent had ANY error rate above
# 2%, so "High Error Rate on Dormant Content Agent, Last Active 91 Days Ago"
# rendered as CRITICAL. A problem nobody has been able to hit in three months
# is not an incident.
print("\n--- severity decays with staleness ---")
check("a live agent keeps critical",
      main._decay_severity("critical", 0.2) == "critical")
check("past 30 days, critical caps at warning",
      main._decay_severity("critical", 31.0) == "warning")
check("past 60 days, critical caps at info",
      main._decay_severity("critical", 91.4) == "info")
check("past 60 days, warning also caps at info",
      main._decay_severity("warning", 70.0) == "info")
check("decay NEVER raises a severity",
      main._decay_severity("info", 0.1) == "info"
      and main._decay_severity("warning", 0.1) == "warning")
check("unknown age is left alone, not silently downgraded",
      main._decay_severity("critical", None) == "critical")

print("\n--- the production case that started this ---")
flagged = main._flag_attention([{
    "service_name": "hammocks-content-agent",
    "total_spans": 264, "total_errors": 95,
    "last_seen": "2026-05-14 12:00:00", "description": "writes content",
}])
check("the 91-day-dormant agent is no longer CRITICAL",
      flagged and flagged[0]["severity"] != "critical")
check("...it is INFO", flagged and flagged[0]["severity"] == "info")

print("\n--- the LLM payload is prose-safe ---")
import describer
check("days are a phrase, never a number to paste",
      describer._humanize_days(91.4) == "3 months ago"
      and describer._humanize_days(0.2) == "today"
      and describer._humanize_days(None) == "unknown")
_keys = set()
_orig = describer._claude_json
def _capture(system, user, max_tokens=2000):
    import json as _j, re as _re
    _keys.update(_re.findall(r'"([a-z][a-z _]+)":', user))
    return {"items": []}
describer._claude_json = _capture
describer.attention_items(flagged)
describer._claude_json = _orig
leaky = {k for k in _keys if "_" in k}
check(f"no snake_case field names reach the prompt (found: {sorted(leaky)})",
      not leaky)
check("the keys that DO reach it read as English",
      {"error rate", "operations recorded", "last active"} <= _keys)
check("the prompt forbids echoing field names",
      "NEVER echo a field name" in describer.DASHBOARD_ATTENTION_SYSTEM_PROMPT)
check("the prompt separates tool friction from agent failure",
      "TOOL FRICTION" in describer.DASHBOARD_ATTENTION_SYSTEM_PROMPT
      and "AGENT FAILURE" in describer.DASHBOARD_ATTENTION_SYSTEM_PROMPT)

print()
if failures:
    print(f"FAILED ({len(failures)}):")
    for f in failures:
        print("  - " + f)
    raise SystemExit(1)
print("All error-classification checks passed.")
os.unlink(_tmp.name)
