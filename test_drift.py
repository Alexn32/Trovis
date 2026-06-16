"""Tests for drift detection — declared identity vs. observed behavior.

Run:
  OVERSEE_DISABLE_PRICING_SYNC=1 python3 test_drift.py
(isolated temp SQLite DB; Claude is stubbed, so no network / API key needed.)
"""
import os
import tempfile

os.environ["OVERSEE_DISABLE_PRICING_SYNC"] = "1"
os.environ.pop("DATABASE_URL", None)
os.environ.pop("ANTHROPIC_API_KEY", None)

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()

import database
database.SQLITE_PATH = _tmp.name

import main
import describer
from fastapi.testclient import TestClient

main._auto_describe = lambda *a, **k: False

failures = []
def check(label, cond):
    print(("  PASS " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


# --- stub Claude: count calls, return a controllable reply ---
_stub = {"calls": 0, "reply": {}}
def _fake_claude_json(system, user, max_tokens=2000):
    _stub["calls"] += 1
    return _stub["reply"]
describer._claude_json = _fake_claude_json


def otlp_attrs(d):
    return [{"key": k, "value": {"stringValue": str(v)}} for k, v in d.items()]

def span(trace, sid, name, start, attrs):
    return {
        "traceId": trace, "spanId": sid, "name": name, "kind": 1,
        "startTimeUnixNano": str(start), "endTimeUnixNano": str(start + 5_000_000),
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


print("\n_normalize_drift:")
n = describer._normalize_drift({})
check("empty reply -> unknown, no findings", n["status"] == "unknown" and n["findings"] == [])
n = describer._normalize_drift({"status": "aligned", "headline": "ok",
                                "findings": [{"title": "x", "evidence": "y", "severity": "low"}]})
check("aligned drops stray findings", n["status"] == "aligned" and n["findings"] == [])
n = describer._normalize_drift({
    "status": "drift", "headline": "bad",
    "findings": [{"title": "T", "evidence": "E", "severity": "weird"}, {}]
    + [{"title": str(i)} for i in range(6)],
})
check("drift caps 4 findings", n["status"] == "drift" and len(n["findings"]) <= 4)
check("bad severity -> low", n["findings"][0]["severity"] == "low")
check("blank finding dropped", all(f["title"] or f["evidence"] for f in n["findings"]))

print("\ndetect_drift:")
_before = _stub["calls"]
n = describer.detect_drift({}, [{"span_name": "x", "start_time_unix": 1, "end_time_unix": 2}], [])
check("no declared identity -> unknown WITHOUT a Claude call",
      n["status"] == "unknown" and _stub["calls"] == _before)
_stub["reply"] = {"status": "minor", "headline": "mostly ok",
                  "findings": [{"title": "t", "evidence": "e", "severity": "medium"}]}
n = describer.detect_drift({"soul": "You summarize the morning news. Never touch billing."},
                           [{"span_name": "llm_output", "start_time_unix": 1, "end_time_unix": 2}], [])
check("declared identity -> Claude verdict normalized",
      n["status"] == "minor" and len(n["findings"]) == 1)

print("\nendpoint /agents/{svc}/drift + attention:")
with TestClient(main.app) as c:
    r = c.post("/auth/signup", json={
        "email": "drift@test.com", "password": "supersecret123",
        "name": "Drift Tester", "account_type": "individual", "org_name": "Drift Co",
    })
    assert r.status_code == 201, r.text
    key = r.json()["api_key"]
    H = {"X-Trovis-Api-Key": key}

    t = 1_900_000_000_000_000_000
    resp = post(c, key, "scout", [
        span("a" * 32, "b" * 16, "agent_registration", t, {
            "trovis.event.type": "agent_registration", "trovis.agent.id": "main",
            "trovis.agent.soul": "You summarize the morning news. You never touch billing or CRM.",
            "trovis.agent.operating_manual": "Read RSS feeds, write a daily digest.",
        }),
        span("a1" * 16, "c" * 16, "llm_output", t + 1_000_000_000, {
            "trovis.event.type": "llm_output",
            "trovis.message.content": "summarize today",
            "trovis.response.content": "Here is the digest...",
        }),
    ])
    assert resp.status_code == 200, resp.text

    _stub["reply"] = {
        "status": "drift",
        "headline": "Scout queried billing — outside its news-summary job.",
        "findings": [{"title": "Out-of-scope tool", "evidence": "called billing.fetch", "severity": "high"}],
    }
    before = _stub["calls"]
    r = c.get("/agents/scout/drift", headers=H)
    check("GET /drift -> 200", r.status_code == 200)
    d = r.json()
    check("returns drift status + headline + finding",
          d["status"] == "drift" and "billing" in d["headline"] and len(d["findings"]) == 1)
    check("computed once (1 Claude call)", _stub["calls"] == before + 1)

    r2 = c.get("/agents/scout/drift", headers=H)
    check("second GET is cached (no new Claude call)",
          r2.status_code == 200 and _stub["calls"] == before + 1 and r2.json()["status"] == "drift")

    r3 = c.get("/agents/scout/drift?refresh=true", headers=H)
    check("refresh=true forces a re-check", r3.status_code == 200 and _stub["calls"] == before + 2)

    att = c.get("/dashboard/attention", headers=H).json()
    check("attention surfaces the cached drift as a warning row",
          any(it["severity"] == "warning" and "billing" in (it["title"] + it["detail"]) for it in att))

    # IDOR: a second account sees neither this agent's drift nor its attention row.
    r = c.post("/auth/signup", json={
        "email": "other@test.com", "password": "supersecret123",
        "name": "Other", "account_type": "individual", "org_name": "Other Co",
    })
    key2 = r.json()["api_key"]
    H2 = {"X-Trovis-Api-Key": key2}
    att2 = c.get("/dashboard/attention", headers=H2).json()
    check("other account sees no drift (scope isolation)",
          not any("billing" in (it.get("title", "") + it.get("detail", "")) for it in att2))

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    raise SystemExit(1)
print("ALL PASS")
os.unlink(_tmp.name)
