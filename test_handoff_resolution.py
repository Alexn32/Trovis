"""Human handoff resolution + the assignee filter.

Before this seam only an AGENT could resolve a handoff (via the /v1/traces
attribute path), so a human who did the work and had nothing reporting it
watched the loop go awaiting_human -> stalled -> force-closed 'abandoned' by
the 48h sweep. Covers:

  - POST /loops/{id}/handoffs/{event_id}/{accept,complete,decline}:
    session-attributed, api-key 403, idempotent, moves the loop off
    awaiting_human
  - uuid correlation BOTH ways: an initiating event carrying a uuid
    handoff_id produces a resolution carrying the same uuid (so a
    human-emitted resolution is indistinguishable from an agent-emitted
    one); one without produces a resolution with no handoff_id, resolved by
    the existing most-recent-unresolved fallback
  - cross-account isolation: A resolving B's handoff is 404, not 403 (never
    leak existence)
  - terminal loops are frozen: 409 with {state, closed_at, close_reason},
    nothing appended, no un-abandoning
  - GET /loops?assignee=me and the server-side awaiting_is_you /
    awaiting_human_name decoration that replaced the "waiting on you" lie

Run:
  OVERSEE_DISABLE_PRICING_SYNC=1 python3 test_handoff_resolution.py
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


def signup(client, email, org):
    r = client.post("/auth/signup", json={
        "email": email, "password": "supersecret123",
        "name": email.split("@")[0], "account_type": "business", "org_name": org,
    })
    assert r.status_code == 201, r.text
    body = r.json()
    return body["api_key"], body["token"], body["user"]["id"]


def loop_by_service(client, hdrs, service):
    for loop in client.get("/loops?limit=100", headers=hdrs).json():
        if loop["service_name"] == service:
            return loop
    return None


def events_of(client, hdrs, loop_id):
    return client.get(f"/loops/{loop_id}", headers=hdrs).json()["events"]


NS = 1_000_000_000
HOUR = 3600 * NS
NOW = time.time_ns()

with TestClient(main.app) as c:
    KEY_A, TOK_A, UID_A = signup(c, "ann@test.com", "Ann Co")
    KEY_B, TOK_B, UID_B = signup(c, "bob@test.com", "Bob Co")
    HA = {"Authorization": f"Bearer {TOK_A}"}
    HB = {"Authorization": f"Bearer {TOK_B}"}
    AK = {"X-Trovis-Api-Key": KEY_A}

    # --- Seed account A -----------------------------------------------------
    # h-uuid : handoff to ann@test.com WITH a uuid -> uuid must ride through
    # h-bare : handoff to ann@test.com WITHOUT a uuid -> fallback correlation
    # h-other: handoff to a teammate who is not the caller
    # h-anon : handoff with no target at all -> "a human", never "you"
    # h-term : handoff then user-closed -> terminal, 409
    # h-gone : handoff 49h old -> abandoned by the sweep, 409
    assert post(c, KEY_A, "h-uuid", [
        span("x", NOW - HOUR, {
            "trovis.run.id": "r-uuid",
            "trovis.handoff.direction": "to_human",
            "trovis.handoff.target_id": "ann@test.com",
            "trovis.handoff.id": "uuid-1111",
            "trovis.handoff.reason": "needs sign-off",
        }),
    ]).status_code == 200
    assert post(c, KEY_A, "h-bare", [
        span("x", NOW - HOUR, {
            "trovis.run.id": "r-bare",
            "trovis.handoff.direction": "to_human",
            "trovis.handoff.target_id": "ann@test.com",
        }),
    ]).status_code == 200
    assert post(c, KEY_A, "h-other", [
        span("x", NOW - HOUR, {
            "trovis.run.id": "r-other",
            "trovis.handoff.direction": "to_human",
            "trovis.handoff.target_id": "someone.else@test.com",
        }),
    ]).status_code == 200
    assert post(c, KEY_A, "h-anon", [
        span("x", NOW - HOUR, {
            "trovis.run.id": "r-anon",
            "trovis.handoff.direction": "to_human",
        }),
    ]).status_code == 200
    assert post(c, KEY_A, "h-term", [
        span("x", NOW - HOUR, {
            "trovis.run.id": "r-term",
            "trovis.handoff.direction": "to_human",
            "trovis.handoff.target_id": "ann@test.com",
        }),
    ]).status_code == 200
    assert post(c, KEY_A, "h-gone", [
        span("x", NOW - 49 * HOUR, {
            "trovis.run.id": "r-gone",
            "trovis.handoff.direction": "to_human",
            "trovis.handoff.target_id": "ann@test.com",
        }),
    ]).status_code == 200
    # Account B: its own handoff, for the isolation test.
    assert post(c, KEY_B, "b-loop", [
        span("x", NOW - HOUR, {
            "trovis.run.id": "r-b",
            "trovis.handoff.direction": "to_human",
            "trovis.handoff.target_id": "bob@test.com",
        }),
    ]).status_code == 200

    print("\n--- server-side 'waiting on whom' decoration")
    l_uuid = loop_by_service(c, HA, "h-uuid")
    l_other = loop_by_service(c, HA, "h-other")
    l_anon = loop_by_service(c, HA, "h-anon")
    check("seeded handoffs land in awaiting_human",
          l_uuid["cached_state"] == "awaiting_human")
    check("target resolving to the caller -> awaiting_is_you",
          l_uuid["awaiting_is_you"] is True
          and l_uuid["awaiting_human_name"] == "ann")
    check("target resolving to someone else -> named, not 'you'",
          l_other["awaiting_is_you"] is False
          and l_other["awaiting_human_name"] is None)
    check("no target at all -> neither you nor a name",
          l_anon["awaiting_is_you"] is False
          and l_anon["awaiting_human_name"] is None)
    check("addressable handoff event id is served on the summary",
          isinstance(l_uuid["awaiting_handoff_event_id"], int))
    # The same loop, viewed by the OTHER org's user, must never say "you".
    check("cross-account viewer never sees awaiting_is_you",
          all(not l["awaiting_is_you"] for l in c.get("/loops", headers=HB).json()
              if l["service_name"] != "b-loop") )

    print("\n--- GET /loops?assignee=me")
    mine = c.get("/loops?assignee=me", headers=HA).json()
    mine_svcs = {l["service_name"] for l in mine}
    # h-gone is included on purpose: it is 49h stale, so its cached_state is
    # already 'stalled' — but the handoff still targets Ann and is still
    # unresolved. A stalled handoff is MORE waiting-on-you, not less.
    check("assignee=me returns every loop targeting the caller, stalled included",
          mine_svcs == {"h-uuid", "h-bare", "h-term", "h-gone"})
    check("assignee=me spans both awaiting_human and stalled",
          {l["cached_state"] for l in mine} == {"awaiting_human", "stalled"})
    check("assignee=me excludes another person's handoff", "h-other" not in mine_svcs)
    check("assignee=me excludes target-less handoffs", "h-anon" not in mine_svcs)
    check("assignee=me is scoped to the account (B sees only its own)",
          {l["service_name"] for l in c.get("/loops?assignee=me", headers=HB).json()}
          == {"b-loop"})
    check("assignee=me with api-key auth -> 403 (no 'me' to mean)",
          c.get("/loops?assignee=me", headers=AK).status_code == 403)
    check("unknown assignee value -> 400",
          c.get("/loops?assignee=someone", headers=HA).status_code == 400)

    print("\n--- (a) human resolution moves the loop off awaiting_human")
    hid = l_uuid["awaiting_handoff_event_id"]
    r = c.post(f"/loops/{l_uuid['id']}/handoffs/{hid}/complete", headers=HA)
    check("complete -> 200", r.status_code == 200)
    body = r.json()
    check("loop left awaiting_human", body["cached_state"] != "awaiting_human")
    check("loop is now working (no unresolved handoff, not idle)",
          body["cached_state"] == "working")
    check("resolution is attributed to the human, not the agent",
          any(e["type"] == "handoff_completed" and e["actor_type"] == "human"
              and e["actor"] == str(UID_A) for e in body["events"]))
    check("resolver joins the cast as a human participant",
          any(p["participant_type"] == "human" and p["participant"] == str(UID_A)
              for p in body["participants"]))
    check("resolved loop drops out of assignee=me",
          "h-uuid" not in {l["service_name"] for l in
                           c.get("/loops?assignee=me", headers=HA).json()})

    print("\n--- (d) uuid correlation, both paths")
    done_ev = [e for e in body["events"] if e["type"] == "handoff_completed"][0]
    check("initiating uuid rides through to the resolution",
          done_ev["payload"].get("handoff_id") == "uuid-1111")
    init_ev = [e for e in body["events"] if e["type"] == "handoff_initiated"][0]
    check("resolution uuid matches the initiating event exactly",
          done_ev["payload"].get("handoff_id")
          == init_ev["payload"].get("handoff_id"))

    l_bare = loop_by_service(c, HA, "h-bare")
    r = c.post(
        f"/loops/{l_bare['id']}/handoffs/{l_bare['awaiting_handoff_event_id']}/accept",
        headers=HA,
    )
    check("accept on an id-less handoff -> 200", r.status_code == 200)
    bare_body = r.json()
    acc_ev = [e for e in bare_body["events"] if e["type"] == "handoff_accepted"][0]
    check("no uuid on the initiator -> none written on the resolution",
          "handoff_id" not in acc_ev["payload"])
    check("fallback correlation still moves the loop off awaiting_human",
          bare_body["cached_state"] != "awaiting_human")

    print("\n--- (e) idempotency")
    before = len(events_of(c, HA, l_bare["id"]))
    again = c.post(
        f"/loops/{l_bare['id']}/handoffs/{l_bare['awaiting_handoff_event_id']}/accept",
        headers=HA,
    )
    after = len(events_of(c, HA, l_bare["id"]))
    check("re-resolving is a no-op 200", again.status_code == 200)
    check("no duplicate resolution event appended", before == after)
    check("a different verb on an already-resolved handoff is also a no-op",
          c.post(
              f"/loops/{l_bare['id']}/handoffs/"
              f"{l_bare['awaiting_handoff_event_id']}/complete", headers=HA,
          ).status_code == 200
          and len(events_of(c, HA, l_bare["id"])) == after)

    print("\n--- auth posture (mirrors POST /loops/{id}/close)")
    l_term = loop_by_service(c, HA, "h-term")
    check("api-key auth -> 403, not 401/404",
          c.post(
              f"/loops/{l_term['id']}/handoffs/"
              f"{l_term['awaiting_handoff_event_id']}/accept", headers=AK,
          ).status_code == 403)

    print("\n--- (b) cross-account isolation")
    l_b = loop_by_service(c, HB, "b-loop")
    r = c.post(
        f"/loops/{l_b['id']}/handoffs/{l_b['awaiting_handoff_event_id']}/accept",
        headers=HA,
    )
    check("A resolving B's handoff -> 404 (existence never leaked)",
          r.status_code == 404)
    check("B's loop is untouched — still awaiting_human",
          c.get(f"/loops/{l_b['id']}", headers=HB).json()["cached_state"]
          == "awaiting_human")
    check("B's handoff has no resolution event",
          not any(e["type"].startswith("handoff_a")
                  or e["type"] == "handoff_completed"
                  for e in events_of(c, HB, l_b["id"])
                  if e["type"] != "handoff_initiated"))
    check("a handoff id from another loop in the SAME account -> 404",
          c.post(
              f"/loops/{l_term['id']}/handoffs/"
              f"{l_other['awaiting_handoff_event_id']}/accept", headers=HA,
          ).status_code == 404)
    check("a non-handoff event id -> 404",
          c.post(
              f"/loops/{l_term['id']}/handoffs/999999/accept", headers=HA,
          ).status_code == 404)

    print("\n--- (c) terminal loops are frozen")
    # User-closed -> done.
    term_hid = l_term["awaiting_handoff_event_id"]
    assert c.post(f"/loops/{l_term['id']}/close", headers=HA).status_code == 200
    before = len(events_of(c, HA, l_term["id"]))
    r = c.post(f"/loops/{l_term['id']}/handoffs/{term_hid}/complete", headers=HA)
    check("resolving a closed loop -> 409", r.status_code == 409)
    detail = r.json()["detail"]
    check("409 body carries state/closed_at/close_reason",
          detail["state"] == "done"
          and detail["closed_at"]
          and detail["close_reason"] == "closed_by_user")
    check("409 appended nothing", len(events_of(c, HA, l_term["id"])) == before)

    # Swept -> abandoned. This is the case the counter exists to measure.
    l_gone = loop_by_service(c, HA, "h-gone")
    gone_hid = l_gone["awaiting_handoff_event_id"]
    loops_mod.run_sweep()
    swept = c.get(f"/loops/{l_gone['id']}", headers=HA).json()
    check("49h-idle handoff is abandoned by the sweep",
          swept["cached_state"] == "abandoned")
    before = len(swept["events"])
    count_before = main._handoff_409_count
    r = c.post(f"/loops/{l_gone['id']}/handoffs/{gone_hid}/complete", headers=HA)
    check("resolving an abandoned loop -> 409", r.status_code == 409)
    detail = r.json()["detail"]
    check("409 body reports abandoned + the sweep's reason",
          detail["state"] == "abandoned" and detail["close_reason"] == "abandoned")
    check("409 did not un-abandon the loop",
          c.get(f"/loops/{l_gone['id']}", headers=HA).json()["cached_state"]
          == "abandoned")
    check("409 appended nothing",
          len(events_of(c, HA, l_gone["id"])) == before)
    check("terminal-resolution attempts are counted for later analysis",
          main._handoff_409_count == count_before + 1)

    print("\n--- decline")
    l_anon = loop_by_service(c, HA, "h-anon")
    r = c.post(
        f"/loops/{l_anon['id']}/handoffs/{l_anon['awaiting_handoff_event_id']}/decline",
        headers=HA, json={"reason": "not mine to sign"},
    )
    check("decline -> 200", r.status_code == 200)
    dec = [e for e in r.json()["events"] if e["type"] == "handoff_declined"]
    check("decline records the reason and the human actor",
          len(dec) == 1 and dec[0]["payload"].get("reason") == "not mine to sign"
          and dec[0]["actor_type"] == "human")
    check("declined loop leaves awaiting_human",
          r.json()["cached_state"] != "awaiting_human")

    print("\n--- state machine untouched")
    check("compute_loop_state consumes the human events with no change to it",
          loops_mod.compute_loop_state([
              {"type": "loop_opened", "ts": NOW - 2 * HOUR,
               "actor_type": "agent", "actor": "a:main", "payload": {}},
              {"type": "handoff_initiated", "ts": NOW - HOUR,
               "actor_type": "agent", "actor": "a:main",
               "payload": {"direction": "to_human", "handoff_id": "u1"}},
              {"type": "handoff_completed", "ts": NOW - 60 * NS,
               "actor_type": "human", "actor": "7",
               "payload": {"handoff_id": "u1"}},
          ], now_ns=NOW) == "working")
    check("thresholds unchanged (4h stall / 48h abandon)",
          loops_mod.STALL_THRESHOLD_S == 14400
          and loops_mod.ABANDON_THRESHOLD_S == 172800)

print()
if failures:
    print(f"FAILED ({len(failures)}):")
    for f in failures:
        print("  - " + f)
    raise SystemExit(1)
print("All handoff-resolution checks passed.")
