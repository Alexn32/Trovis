"""Handoff target -> human name resolution on the loop detail read.

The ingest contract: for to_human handoffs, target_id is the teammate's
email or Trovis user id. GET /loops/{id} resolves it to a display name
(payload.target_name), org-scoped, at READ time — the stored loop_events
row is never modified. Unresolvable targets keep target_id and gain no
name (UI shows "a human").

Run:
  OVERSEE_DISABLE_PRICING_SYNC=1 python3 test_loop_handoff_names.py
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
def handoff_span(run_id, target):
    _seq[0] += 1
    attrs = {"trovis.run.id": run_id, "trovis.handoff.direction": "to_human",
             "trovis.handoff.reason": "needs approval"}
    if target is not None:
        attrs["trovis.handoff.target_id"] = target
    start = time.time_ns() - 60_000_000_000
    return {
        "traceId": f"{_seq[0]:032d}", "spanId": f"{_seq[0]:016d}", "name": "ask",
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


def handoff_payload(client, tok, loop_id):
    d = client.get(f"/loops/{loop_id}", headers={"Authorization": f"Bearer {tok}"}).json()
    for e in d["events"]:
        if e["type"] == "handoff_initiated":
            return e["payload"]
    return {}


with TestClient(main.app) as c:
    r = c.post("/auth/signup", json={
        "email": "sarah@acme.com", "password": "supersecret123",
        "name": "Sarah Chen", "account_type": "individual", "org_name": "ACME",
    })
    assert r.status_code == 201, r.text
    key = r.json()["api_key"]
    tok = c.post("/auth/login", json={
        "email": "sarah@acme.com", "password": "supersecret123",
    }).json()["token"]
    me = c.get("/auth/me", headers={"Authorization": f"Bearer {tok}"}).json()
    account_id = me["org"]["id"]
    user_id = me["user"]["id"]

    def loop_id_for(service):
        return [l for l in database.get_loops(account_id, limit=100)
                if l["service_name"] == service][0]["id"]

    # 1. Email target -> resolved to the user's name.
    assert post(c, key, "bot-email", [handoff_span("r1", "sarah@acme.com")]).status_code == 200
    p = handoff_payload(c, tok, loop_id_for("bot-email"))
    check("email target resolves to the user's name",
          p.get("target_name") == "Sarah Chen" and p.get("target_id") == "sarah@acme.com")

    # 2. Email is case-insensitive.
    assert post(c, key, "bot-case", [handoff_span("r2", "SARAH@ACME.COM")]).status_code == 200
    check("email resolution is case-insensitive",
          handoff_payload(c, tok, loop_id_for("bot-case")).get("target_name") == "Sarah Chen")

    # 3. Numeric Trovis user id -> resolved.
    assert post(c, key, "bot-uid", [handoff_span("r3", str(user_id))]).status_code == 200
    check("numeric user id resolves",
          handoff_payload(c, tok, loop_id_for("bot-uid")).get("target_name") == "Sarah Chen")

    # 4. team_members email -> resolved (business-org seat, no login).
    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute(
            f"INSERT INTO team_members (account_id, name, email, role) "
            f"VALUES ({database.PH}, {database.PH}, {database.PH}, {database.PH})",
            (account_id, "Omar Diaz", "omar@acme.com", "ops"),
        )
    assert post(c, key, "bot-seat", [handoff_span("r4", "omar@acme.com")]).status_code == 200
    check("team_members email resolves",
          handoff_payload(c, tok, loop_id_for("bot-seat")).get("target_name") == "Omar Diaz")

    # 5. Unknown target -> target_id kept, no target_name (UI says "a human").
    assert post(c, key, "bot-unknown", [handoff_span("r5", "nobody@else.com")]).status_code == 200
    p = handoff_payload(c, tok, loop_id_for("bot-unknown"))
    check("unknown target keeps target_id, gains no name",
          "target_name" not in p and p.get("target_id") == "nobody@else.com")

    # 6. No target at all -> unchanged.
    assert post(c, key, "bot-none", [handoff_span("r6", None)]).status_code == 200
    p = handoff_payload(c, tok, loop_id_for("bot-none"))
    check("absent target unchanged", "target_name" not in p and "target_id" not in p)

    # 7. Org scoping: another org's user never resolves in this org's loops.
    r = c.post("/auth/signup", json={
        "email": "eve@other.com", "password": "supersecret123",
        "name": "Eve Other", "account_type": "individual", "org_name": "Other Co",
    })
    assert r.status_code == 201, r.text
    assert post(c, key, "bot-crossorg", [handoff_span("r7", "eve@other.com")]).status_code == 200
    p = handoff_payload(c, tok, loop_id_for("bot-crossorg"))
    check("cross-org target does NOT resolve", "target_name" not in p)

    # 8. Decoration is read-time only: the stored event payload has no name.
    import sqlite3, json as _json
    conn = sqlite3.connect(_tmp.name)
    raw = conn.execute(
        "SELECT payload FROM loop_events WHERE type='handoff_initiated' "
        "ORDER BY id LIMIT 1"
    ).fetchone()[0]
    conn.close()
    check("stored event payload is undecorated (append-only record intact)",
          "target_name" not in _json.loads(raw))

print()
if failures:
    print(f"FAILED: {len(failures)} check(s):")
    for f in failures:
        print("  - " + f)
    raise SystemExit(1)
print("ALL CHECKS PASSED")
os.unlink(_tmp.name)
