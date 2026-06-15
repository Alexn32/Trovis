"""Tests for the agent-limit gate. Cardinal rule: ingestion is NEVER gated —
every agent's telemetry is recorded; only the VIEW is locked by plan.

Run:
  OVERSEE_DISABLE_PRICING_SYNC=1 python3 test_agent_gate.py
(isolated temp SQLite DB; never touches the dev/prod DB)
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
from fastapi.testclient import TestClient

main._auto_describe = lambda *a, **k: False

failures = []
def check(label, cond):
    print(("  PASS " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


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


B = 1_900_000_000_000_000_000  # well past the first-seen floor (2025-01-01)
AGENTS = [f"agent-{i:02d}" for i in range(1, 7)]  # 6 agents, agent-01 oldest

with TestClient(main.app) as c:
    r = c.post("/auth/signup", json={
        "email": "gate@test.com", "password": "supersecret123",
        "name": "Gate Tester", "account_type": "individual", "org_name": "Gate Co",
    })
    assert r.status_code == 201, r.text
    key = r.json()["api_key"]
    H = {"X-Trovis-Api-Key": key}

    # Connect 6 agents with ascending first_seen (agent-01 earliest → agent-06
    # newest). On the free plan (limit 5), agent-06 should land LOCKED. Every
    # agent gets a registration span + one interaction record.
    for i, svc in enumerate(AGENTS, start=1):
        t = B + i * 10_000_000_000  # 10s apart, ascending
        trace_reg = (f"{i:02d}".encode().hex() * 16)[:32]
        trace_int = (f"{i:02d}ff".encode().hex() * 16)[:32]
        resp = post(c, key, svc, [
            span(trace_reg, "b" * 16, "agent_registration", t, {
                "trovis.event.type": "agent_registration", "trovis.agent.id": "main",
                "trovis.agent.identity": f"{svc} — worker",
            }),
            span(trace_int, "c" * 16, "llm_output", t + 1_000_000_000, {
                "trovis.event.type": "llm_output",
                "trovis.message.content": f"ping {svc}",
                "trovis.response.content": f"pong from {svc}",
            }),
        ])
        assert resp.status_code == 200, resp.text
        assert resp.json()["spans_received"] == 2

    # 1. INGESTION NEVER GATED — all 6 agents recorded.
    groups = {g["service_name"]: g for g in c.get("/agents", headers=H).json()}
    check("all 6 agents ingested + visible in /agents", len(groups) == 6)

    # 2. Free plan: agents 1-5 unlocked, agent-06 (newest) locked.
    check("agent-01..05 unlocked", all(not groups[a]["locked"] for a in AGENTS[:5]))
    check("agent-06 locked", groups["agent-06"]["locked"] is True)

    # 3. /account/usage
    u = c.get("/account/usage", headers=H).json()
    check("usage: free / 6 agents / limit 5 / 1 locked",
          u == {"plan": "free", "agent_count": 6, "agent_limit": 5, "locked_count": 1})

    # 4. Locked agent records withheld (count + recording_since only).
    r6 = c.get("/agents/agent-06/records", headers=H).json()
    check("locked agent: records withheld but proof returned",
          r6["locked"] is True and r6["records"] == []
          and (r6["records_count"] or 0) >= 1 and bool(r6["recording_since"]))

    # 5. Unlocked agent records flow normally.
    r1 = c.get("/agents/agent-01/records", headers=H).json()
    check("unlocked agent: records returned, not locked",
          r1.get("locked") is False and len(r1["records"]) >= 1)

    # 6. Locked agent summary carries the lock fields.
    s6 = c.get("/agents/agent-06/summary", headers=H).json()
    check("locked agent summary: locked + count + recording_since",
          s6["locked"] is True and (s6.get("records_count") or 0) >= 1
          and bool(s6.get("recording_since")))
    s1 = c.get("/agents/agent-01/summary", headers=H).json()
    check("unlocked agent summary: not locked", s1["locked"] is False)

    # 7. UPGRADE free → starter (limit 15): agent-06 unlocks with full history.
    account_id = c.get("/auth/me", headers=H).json()["org"]["id"]
    database.set_account_plan(account_id, "starter")

    u2 = c.get("/account/usage", headers=H).json()
    check("after upgrade: starter / limit 15 / 0 locked",
          u2["plan"] == "starter" and u2["agent_limit"] == 15 and u2["locked_count"] == 0)
    g6 = {g["service_name"]: g for g in c.get("/agents", headers=H).json()}["agent-06"]
    check("after upgrade: agent-06 no longer locked", g6["locked"] is False)
    r6b = c.get("/agents/agent-06/records", headers=H).json()
    check("after upgrade: agent-06 records now visible (history intact)",
          r6b.get("locked") is False and len(r6b["records"]) >= 1)

print()
if failures:
    print(f"FAILED: {len(failures)} check(s):")
    for f in failures:
        print("  - " + f)
    raise SystemExit(1)
print("ALL CHECKS PASSED")
