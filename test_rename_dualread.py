"""Keystone test for the oversee→trovis rename back-compat.

Proves zero-downtime: a legacy agent (emitting `oversee.*` spans + the
`X-Oversee-Api-Key` header) and a new agent (emitting `trovis.*` + the
`X-Trovis-Api-Key` header) converge to identical dashboard state through the
backend's dual-read / dual-accept paths.

Run:
  OVERSEE_DISABLE_PRICING_SYNC=1 python3 test_rename_dualread.py
(uses an isolated temp SQLite DB; never touches the dev/prod DB)
"""
import os
import sys
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

# Registration auto-describe calls Claude; stub it to a deterministic no-op.
main._auto_describe = lambda *a, **k: False

failures = []
def check(label, cond):
    print(("  PASS " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


def otlp_attrs(d):
    return [{"key": k, "value": {"stringValue": str(v)}} for k, v in d.items()]


def payload(service, ns, now):
    """Three spans (registration, captured output, tool call) under namespace
    `ns` ('oversee' or 'trovis') for one service."""
    def span(name, attrs):
        return {
            "traceId": "a" * 32, "spanId": "b" * 16, "name": name,
            "kind": 1, "startTimeUnixNano": str(now), "endTimeUnixNano": str(now + 5_000_000),
            "status": {"code": 1}, "attributes": otlp_attrs(attrs),
        }
    spans = [
        span("agent_registration", {
            f"{ns}.event.type": "agent_registration",
            f"{ns}.agent.id": "main",
            f"{ns}.agent.soul": f"SOUL for {service}",
            f"{ns}.agent.identity": f"{service} — support bot",
            f"{ns}.agent.model": "claude-sonnet-4-6",
        }),
        span("handle_ticket", {
            f"{ns}.message.content": "customer asked for a refund",
            f"{ns}.response.content": "issued refund #4821",
        }),
        span("use_tool", {f"{ns}.tool.name": "stripe_refund"}),
    ]
    return {"resourceSpans": [{
        "resource": {"attributes": otlp_attrs({
            "service.name": service, f"{ns}.plugin.version": "1.0.0",
        })},
        "scopeSpans": [{"spans": spans}],
    }]}


with TestClient(main.app) as c:
    # Fresh org + api key.
    r = c.post("/auth/signup", json={
        "email": "dual@test.com", "password": "supersecret123",
        "name": "Dual Tester", "account_type": "individual", "org_name": "Dual Co",
    })
    assert r.status_code == 201, r.text
    body = r.json()
    api_key = body["api_key"]
    account_id = body["org"]["id"]
    now = 1_900_000_000_000_000_000  # fixed ns timestamp (recent enough)

    # Legacy agent: oversee.* + X-Oversee-Api-Key.
    r1 = c.post("/v1/traces", json=payload("legacy-bot", "oversee", now),
                headers={"X-Oversee-Api-Key": api_key})
    check("legacy ingest accepts X-Oversee-Api-Key (200)", r1.status_code == 200)
    check("legacy ingest stored 3 spans", r1.json().get("spans_received") == 3)

    # New agent: trovis.* + X-Trovis-Api-Key.
    r2 = c.post("/v1/traces", json=payload("trovis-bot", "trovis", now),
                headers={"X-Trovis-Api-Key": api_key})
    check("new ingest accepts X-Trovis-Api-Key (200)", r2.status_code == 200)
    check("new ingest stored 3 spans", r2.json().get("spans_received") == 3)

    # An invalid key on either header must still 401 (dual-accept ≠ no-auth).
    rb = c.post("/v1/traces", json=payload("x", "trovis", now),
                headers={"X-Trovis-Api-Key": "ov_sk_bogus"})
    check("bogus key rejected (401)", rb.status_code == 401)

print("\n-- DB read-path parity (legacy vs new must match) --")
agents = {a["service_name"]: a for a in database.get_agents(account_id)}
check("both agents present in fleet", "legacy-bot" in agents and "trovis-bot" in agents)

# Registration captured (soul/identity) for both namespaces.
for svc in ("legacy-bot", "trovis-bot"):
    reg = database.get_registration(svc, account_id=account_id, agent_id="main") \
        if hasattr(database, "get_registration") else None
    summ = database.get_agent_summary(svc, account_id=account_id, agent_id="main")
    check(f"{svc}: registration/summary exists", summ is not None or reg is not None)

# Captured outputs visible for both (dual-read of message/response/tool keys).
for svc in ("legacy-bot", "trovis-bot"):
    outs = database.get_agent_outputs(svc, account_id=account_id, agent_id="main")
    has_refund = any("refund" in (o.get("content") or "").lower() for o in outs)
    check(f"{svc}: captured output content returned ({len(outs)} rows)", len(outs) >= 1 and has_refund)

# Tool detected for both (get_window_aggregate dual LIKE + attr read).
for svc in ("legacy-bot", "trovis-bot"):
    agg = database.get_window_aggregate(
        svc, "main", now - 60_000_000_000, now + 60_000_000_000,
        account_id=account_id,
    )
    tools = agg.get("tools_used", []) if isinstance(agg, dict) else []
    check(f"{svc}: tool 'stripe_refund' detected", "stripe_refund" in tools)

# Platform detection treats both plugin-version namespaces identically.
import json as _json
old_pf = database._detect_platform(_json.dumps({"oversee.plugin.version": "1"}))
new_pf = database._detect_platform(_json.dumps({"trovis.plugin.version": "1"}))
check("platform detect: oversee.plugin.version recognized", old_pf == "Trovis-instrumented Agent")
check("platform detect: trovis.plugin.version recognized", new_pf == "Trovis-instrumented Agent")

os.unlink(_tmp.name)
print(f"\n{'ALL PASSED' if not failures else str(len(failures)) + ' FAILED: ' + ', '.join(failures)}")
sys.exit(1 if failures else 0)
