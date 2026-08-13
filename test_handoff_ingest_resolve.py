"""Agent-declared handoff resolution via the trovis.handoff.resolve attribute.

The AGENT half of the seam whose HUMAN half shipped in
test_handoff_resolution.py. Before this, `trovis.handoff.direction` could
OPEN a handoff over the wire but nothing could close one — the ingest
docstring promised a correlation id "for a later handoff_accepted/
completed/declined" that no attribute could ever send.

The load-bearing property, asserted by diffing the two stored events: an
agent-emitted resolution and a human-emitted one are IDENTICAL apart from
actor_type and actor. Same event type, same payload, same correlation. The
event record must not be able to tell you which surface wrote it.

Covers:
  - uuid correlation when the initiating handoff carried one
  - fallback correlation (no uuid -> most recent unresolved handoff)
  - unknown resolve value: logged, dropped, batch still 200
  - terminal loop: appends nothing, never reopens (the freeze the 409 respects)
  - nothing open to resolve: dropped, not guessed at
  - all three kinds (accepted / completed / declined)
  - dual-read: the legacy oversee.* prefix works identically

Run:
  OVERSEE_DISABLE_PRICING_SYNC=1 python3 test_handoff_ingest_resolve.py
(isolated temp SQLite DB; never touches the dev/prod DB)
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
        "resource": {"attributes": otlp_attrs({"service.name": service})},
        "scopeSpans": [{"spans": spans}],
    }]}
    return client.post("/v1/traces", json=payload, headers={"X-Trovis-Api-Key": key})


def loop_of(client, hdrs, service):
    for loop in client.get("/loops?limit=100", headers=hdrs).json():
        if loop["service_name"] == service:
            return loop
    return None


def events_of(client, hdrs, loop_id):
    return client.get(f"/loops/{loop_id}", headers=hdrs).json()["events"]


def lifecycle(client, hdrs, service):
    """The loop's non-activity events, in stream order."""
    loop = loop_of(client, hdrs, service)
    return [e for e in events_of(client, hdrs, loop["id"]) if e["type"] != "activity"]


NS = 1_000_000_000
HOUR = 3600 * NS
NOW = time.time_ns()

with TestClient(main.app) as c:
    r = c.post("/auth/signup", json={
        "email": "res@test.com", "password": "supersecret123",
        "name": "Res Tester", "account_type": "business", "org_name": "Res Co",
    })
    assert r.status_code == 201, r.text
    KEY = r.json()["api_key"]
    TOK = r.json()["token"]
    UID = r.json()["user"]["id"]
    H = {"Authorization": f"Bearer {TOK}"}

    print("\n--- uuid correlation")
    assert post(c, KEY, "uuid-bot", [
        span("open", NOW - 2 * HOUR, {
            "trovis.run.id": "u1",
            "trovis.handoff.direction": "to_human",
            "trovis.handoff.target_id": "res@test.com",
            "trovis.handoff.id": "H-AAA",
        }),
    ]).status_code == 200
    check("handoff opens the loop into awaiting_human",
          loop_of(c, H, "uuid-bot")["cached_state"] == "awaiting_human")
    assert post(c, KEY, "uuid-bot", [
        span("resolve", NOW - HOUR, {
            "trovis.run.id": "u1",
            "trovis.handoff.resolve": "completed",
            "trovis.handoff.id": "H-AAA",
        }),
    ]).status_code == 200
    evs = lifecycle(c, H, "uuid-bot")
    done = [e for e in evs if e["type"] == "handoff_completed"]
    check("resolve=completed appends handoff_completed", len(done) == 1)
    check("resolution carries the initiating uuid",
          done and done[0]["payload"].get("handoff_id") == "H-AAA")
    check("resolution is agent-attributed",
          done and done[0]["actor_type"] == "agent"
          and done[0]["actor"] == "uuid-bot:main")
    check("loop leaves awaiting_human",
          loop_of(c, H, "uuid-bot")["cached_state"] == "working")

    print("\n--- targeted correlation with two handoffs open")
    assert post(c, KEY, "two-bot", [
        span("h1", NOW - 3 * HOUR, {
            "trovis.run.id": "t1", "trovis.handoff.direction": "to_human",
            "trovis.handoff.id": "H-ONE",
        }),
        span("h2", NOW - 2 * HOUR, {
            "trovis.run.id": "t1", "trovis.handoff.direction": "to_agent",
            "trovis.handoff.id": "H-TWO",
        }),
    ]).status_code == 200
    # Resolve the OLDER one by id — the fallback would have taken the newer.
    assert post(c, KEY, "two-bot", [
        span("r", NOW - HOUR, {
            "trovis.run.id": "t1", "trovis.handoff.resolve": "accepted",
            "trovis.handoff.id": "H-ONE",
        }),
    ]).status_code == 200
    check("uuid resolves the SPECIFIC handoff, not the most recent",
          [e for e in lifecycle(c, H, "two-bot")
           if e["type"] == "handoff_accepted"][0]["payload"]["handoff_id"] == "H-ONE")
    check("the other handoff stays open -> loop still awaiting_agent",
          loop_of(c, H, "two-bot")["cached_state"] == "awaiting_agent")

    print("\n--- fallback correlation (no uuid)")
    assert post(c, KEY, "bare-bot", [
        span("open", NOW - 2 * HOUR, {
            "trovis.run.id": "b1", "trovis.handoff.direction": "to_human",
        }),
    ]).status_code == 200
    assert post(c, KEY, "bare-bot", [
        span("res", NOW - HOUR, {
            "trovis.run.id": "b1", "trovis.handoff.resolve": "completed",
        }),
    ]).status_code == 200
    bare = [e for e in lifecycle(c, H, "bare-bot") if e["type"] == "handoff_completed"]
    check("resolution without a uuid still lands", len(bare) == 1)
    check("no uuid written when the initiator had none",
          bare and "handoff_id" not in bare[0]["payload"])
    check("fallback correlation releases the loop",
          loop_of(c, H, "bare-bot")["cached_state"] == "working")

    print("\n--- all three kinds, and the legacy oversee.* prefix")
    for kind, svc in (("accepted", "k-acc"), ("declined", "k-dec")):
        assert post(c, KEY, svc, [
            span("o", NOW - 2 * HOUR, {
                "trovis.run.id": svc, "trovis.handoff.direction": "to_human",
            }),
            span("r", NOW - HOUR, {
                "trovis.run.id": svc, "trovis.handoff.resolve": kind,
            }),
        ]).status_code == 200
        check(f"resolve={kind} appends handoff_{kind}",
              any(e["type"] == f"handoff_{kind}" for e in lifecycle(c, H, svc)))
    assert post(c, KEY, "legacy-bot", [
        span("o", NOW - 2 * HOUR, {
            "oversee.run.id": "lg", "oversee.handoff.direction": "to_human",
            "oversee.handoff.id": "H-LEG",
        }),
        span("r", NOW - HOUR, {
            "oversee.run.id": "lg", "oversee.handoff.resolve": "completed",
            "oversee.handoff.id": "H-LEG",
        }),
    ]).status_code == 200
    leg = [e for e in lifecycle(c, H, "legacy-bot") if e["type"] == "handoff_completed"]
    check("legacy oversee.handoff.resolve works identically",
          len(leg) == 1 and leg[0]["payload"].get("handoff_id") == "H-LEG")

    print("\n--- unknown value: logged, dropped, batch still succeeds")
    r = post(c, KEY, "junk-bot", [
        span("o", NOW - 2 * HOUR, {
            "trovis.run.id": "j1", "trovis.handoff.direction": "to_human",
        }),
        span("r", NOW - HOUR, {
            "trovis.run.id": "j1", "trovis.handoff.resolve": "finished-ish",
        }),
    ])
    check("unknown resolve value does NOT reject the batch", r.status_code == 200)
    check("both spans still stored", r.json()["accepted"] == 2)
    check("no resolution event appended for an unknown value",
          not any(e["type"] in loops_mod._HANDOFF_RESOLUTIONS
                  for e in lifecycle(c, H, "junk-bot")))
    check("loop stays awaiting_human — the wait is still real",
          loop_of(c, H, "junk-bot")["cached_state"] == "awaiting_human")

    print("\n--- nothing open to resolve: dropped, not guessed at")
    r = post(c, KEY, "empty-bot", [
        span("work", NOW - 2 * HOUR, {"trovis.run.id": "e1"}),
        span("res", NOW - HOUR, {
            "trovis.run.id": "e1", "trovis.handoff.resolve": "completed",
        }),
    ])
    check("resolution with no open handoff does not reject the batch",
          r.status_code == 200)
    check("nothing appended when there is nothing to resolve",
          not any(e["type"] in loops_mod._HANDOFF_RESOLUTIONS
                  for e in lifecycle(c, H, "empty-bot")))
    # An id that matches no OPEN handoff is dropped too, even though the loop
    # has one open under a different id — never resolve the wrong one.
    assert post(c, KEY, "wrongid-bot", [
        span("o", NOW - 2 * HOUR, {
            "trovis.run.id": "w1", "trovis.handoff.direction": "to_human",
            "trovis.handoff.id": "H-REAL",
        }),
        span("r", NOW - HOUR, {
            "trovis.run.id": "w1", "trovis.handoff.resolve": "completed",
            "trovis.handoff.id": "H-TYPO",
        }),
    ]).status_code == 200
    check("a uuid matching no open handoff resolves NOTHING",
          not any(e["type"] in loops_mod._HANDOFF_RESOLUTIONS
                  for e in lifecycle(c, H, "wrongid-bot")))
    check("the real handoff is left open, not silently closed",
          loop_of(c, H, "wrongid-bot")["cached_state"] == "awaiting_human")

    print("\n--- terminal loops are frozen (same invariant as the 409)")
    assert post(c, KEY, "term-bot", [
        span("o", NOW - 2 * HOUR, {
            "trovis.run.id": "z1", "trovis.handoff.direction": "to_human",
            "trovis.handoff.id": "H-TERM",
        }),
    ]).status_code == 200
    term = loop_of(c, H, "term-bot")
    assert c.post(f"/loops/{term['id']}/close", headers=H).status_code == 200
    before = [e["type"] for e in lifecycle(c, H, "term-bot")]
    closed_at_before = c.get(f"/loops/{term['id']}", headers=H).json()["closed_at"]
    r = post(c, KEY, "term-bot", [
        span("late", NOW - 60 * NS, {
            "trovis.run.id": "z1", "trovis.handoff.resolve": "completed",
            "trovis.handoff.id": "H-TERM",
        }),
    ])
    check("a late resolution does not reject the batch", r.status_code == 200)
    after_types = [e["type"] for e in lifecycle(c, H, "term-bot")]
    after = c.get(f"/loops/{term['id']}", headers=H).json()
    check("the lifecycle record is byte-for-byte what it was before",
          after_types == before)
    check("specifically: no handoff_completed was appended",
          "handoff_completed" not in after_types)
    check("terminal loop is not reopened", after["cached_state"] == "done")
    check("closed_at survives untouched", after["closed_at"] == closed_at_before)

    print("\n--- THE INVARIANT: agent and human resolutions are indistinguishable")
    # Two loops, same shape, same uuid. One resolved over the wire by an
    # agent; one resolved through the API by a human.
    for svc in ("mirror-agent", "mirror-human"):
        assert post(c, KEY, svc, [
            span("o", NOW - 2 * HOUR, {
                "trovis.run.id": svc,
                "trovis.handoff.direction": "to_human",
                "trovis.handoff.target_id": "res@test.com",
                "trovis.handoff.id": "H-MIRROR",
            }),
        ]).status_code == 200
    assert post(c, KEY, "mirror-agent", [
        span("r", NOW - HOUR, {
            "trovis.run.id": "mirror-agent",
            "trovis.handoff.resolve": "completed",
            "trovis.handoff.id": "H-MIRROR",
        }),
    ]).status_code == 200
    mh = loop_of(c, H, "mirror-human")
    assert c.post(
        f"/loops/{mh['id']}/handoffs/{mh['awaiting_handoff_event_id']}/complete",
        headers=H,
    ).status_code == 200

    a_ev = [e for e in lifecycle(c, H, "mirror-agent")
            if e["type"] == "handoff_completed"][0]
    h_ev = [e for e in lifecycle(c, H, "mirror-human")
            if e["type"] == "handoff_completed"][0]
    check("same event type", a_ev["type"] == h_ev["type"] == "handoff_completed")
    check("PAYLOADS ARE BYTE-IDENTICAL", a_ev["payload"] == h_ev["payload"])
    check("both carry the same correlation uuid",
          a_ev["payload"].get("handoff_id") == h_ev["payload"].get("handoff_id")
          == "H-MIRROR")
    diff = {k for k in set(a_ev) | set(h_ev)
            if a_ev.get(k) != h_ev.get(k)} - {"ts", "sentence"}
    check("the ONLY differing fields are actor_type and actor",
          diff == {"actor_type", "actor"})
    check("agent side is agent-attributed, human side is user-attributed",
          a_ev["actor_type"] == "agent" and h_ev["actor_type"] == "human"
          and h_ev["actor"] == str(UID))
    check("both loops land in the same state",
          loop_of(c, H, "mirror-agent")["cached_state"]
          == loop_of(c, H, "mirror-human")["cached_state"] == "working")

    print("\n--- state machine untouched")
    check("thresholds unchanged (4h stall / 48h abandon)",
          loops_mod.STALL_THRESHOLD_S == 14400
          and loops_mod.ABANDON_THRESHOLD_S == 172800)
    check("resolution kinds map onto the existing event vocabulary",
          tuple(f"handoff_{k}" for k in loops_mod.HANDOFF_RESOLUTION_KINDS)
          == loops_mod._HANDOFF_RESOLUTIONS)

print()
if failures:
    print(f"FAILED ({len(failures)}):")
    for f in failures:
        print("  - " + f)
    raise SystemExit(1)
print("All handoff-ingest-resolution checks passed.")
