"""Home's desk: whose work it shows, and what the three answers actually do.

Home puts Done / I've got this / Not mine directly on a desk row, so two
things have to hold before that is safe to ship:

  1. The desk is MINE. `waiting_on_you` is resolved by the server against the
     session identity, so a teammate's wait must never be able to appear on my
     desk — and org-wide stuck work is not a desk item at all.

  2. "Not mine" must not orphan the work. Declining hands possession back to
     whoever passed it over; work held by nobody silently becomes Stuck, which
     is the exact failure the button would be introducing. The refusal is also
     recorded on the work's own history, so the agent that sent it can see the
     answer on its next turn.

Also covers the reason the row can offer those buttons at all: /work/items
carries awaiting_handoff_event_id, so the desk knows whether a decision is
real without a detail fetch per row.

Run:
  OVERSEE_DISABLE_PRICING_SYNC=1 python3 test_home_desk.py
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


def attrs(d):
    return [{"key": k, "value": {"stringValue": str(v)}} for k, v in d.items()]


_seq = [0]
NS = 1_000_000_000
HOUR = 3600 * NS
NOW = time.time_ns()


def span(name, start, d):
    _seq[0] += 1
    return {
        "traceId": f"{_seq[0]:032d}", "spanId": f"{_seq[0]:016d}", "name": name,
        "kind": 1, "startTimeUnixNano": str(start),
        "endTimeUnixNano": str(start + 5_000_000),
        "status": {"code": 1}, "attributes": attrs(d),
    }


def post(c, key, service, spans):
    return c.post("/v1/traces", json={"resourceSpans": [{
        "resource": {"attributes": attrs({"service.name": service})},
        "scopeSpans": [{"spans": spans}],
    }]}, headers={"X-Trovis-Api-Key": key})


def items(c, hdrs):
    return {i["title"]: i for i in c.get("/work/items?limit=50", headers=hdrs).json()["items"]}


with TestClient(main.app) as c:
    r = c.post("/auth/signup", json={
        "email": "ann@test.com", "password": "supersecret123",
        "name": "Ann", "account_type": "business", "org_name": "Ann Co",
    }).json()
    KEY, TOK = r["api_key"], r["token"]
    H = {"Authorization": f"Bearer {TOK}"}

    MINE = "Approve refund for order #4821"
    THEIRS = "Countersign the vendor contract"

    # Waiting on ME.
    assert post(c, KEY, "support-agent", [span("review", NOW - HOUR, {
        "trovis.run.id": "r-mine",
        "trovis.loop.title": MINE,
        "trovis.loop.external_id": "job-mine",
        "trovis.handoff.direction": "to_human",
        "trovis.handoff.target_id": "ann@test.com",
        "trovis.handoff.id": "uuid-mine",
    })]).status_code == 200

    # Waiting on a TEAMMATE — same shape, different person.
    assert post(c, KEY, "legal-agent", [span("review", NOW - HOUR, {
        "trovis.run.id": "r-theirs",
        "trovis.loop.title": THEIRS,
        "trovis.loop.external_id": "job-theirs",
        "trovis.handoff.direction": "to_human",
        "trovis.handoff.target_id": "someone.else@test.com",
        "trovis.handoff.id": "uuid-theirs",
    })]).status_code == 200

    print("\n--- the desk is mine, and only mine ---")
    rows = items(c, H)
    mine, theirs = rows.get(MINE), rows.get(THEIRS)
    check("my wait is on the list", mine is not None)
    check("the teammate's wait is on the list too", theirs is not None)
    if mine and theirs:
        check("mine reads as waiting on me", mine["status"] == "waiting_on_you")
        check("theirs does NOT read as waiting on me",
              theirs["status"] == "waiting_on_other")
        # This is the whole desk filter, server-resolved. A client-side guess
        # at "who is you" is what this replaces.
        desk = [i for i in rows.values() if i["status"] == "waiting_on_you"]
        check("exactly one item is on my desk", [d["title"] for d in desk] == [MINE])

    print("\n--- the row can offer a decision without a detail fetch ---")
    check("my row carries the open decision's id",
          bool(mine and mine.get("awaiting_handoff_event_id")))
    check("so does the teammate's (their desk, same mechanism)",
          bool(theirs and theirs.get("awaiting_handoff_event_id")))
    moving = [i for i in rows.values() if i["status"] == "moving"]
    check("a row with nothing to decide carries no id — so no button is offered",
          all(i.get("awaiting_handoff_event_id") is None for i in moving))
    # The detail endpoint must keep agreeing with the list, or the desk and the
    # panel would offer different buttons for the same work.
    if mine:
        d = c.get(f"/work/items/{mine['id']}", headers=H).json()
        check("the detail agrees with the list on which decision is open",
              d.get("awaiting_handoff_event_id") == mine["awaiting_handoff_event_id"])

    print("\n--- 'Not mine' hands the work back; it never orphans it ---")
    before = c.get(f"/loops/{mine['id']}", headers=H).json()
    check("before: the work is waiting on a human",
          before["cached_state"] == "awaiting_human")

    resp = c.post(
        f"/loops/{mine['id']}/handoffs/{mine['awaiting_handoff_event_id']}/decline",
        headers=H, json={"reason": "not my area"},
    )
    check("decline succeeds", resp.status_code == 200)

    after = items(c, H).get(MINE)
    check("the work is off my desk", after and after["status"] != "waiting_on_you")
    # The orphan test. Held by nobody reads as Stuck to every other surface,
    # so these three are the button's whole safety case.
    check("it is NOT stuck", after and after["status"] != "stuck")
    check("it is NOT held by nobody", after and after["holder"]["kind"] != "unassigned")
    check("possession went back to the agent that handed it over",
          after and after["holder"]["kind"] == "agent"
          and after["holder"]["name"] == "support-agent")
    check("it is moving again", after and after["status"] == "moving")

    detail = c.get(f"/loops/{mine['id']}", headers=H).json()
    check("the work left the awaiting-a-human state",
          detail["cached_state"] not in ("awaiting_human", "stalled"))
    kinds = [e.get("type") for e in detail["events"]]
    check("the refusal is on the record, so the agent can see it next turn",
          "handoff_declined" in kinds)
    declined = next((e for e in detail["events"] if e.get("type") == "handoff_declined"), None)
    check("attributed to the person who declined, not to the agent",
          declined and declined.get("actor_type") == "human")
    check("nothing is left awaiting a decision",
          items(c, H)[MINE].get("awaiting_handoff_event_id") is None)

    print("\n--- 'Done' finishes it, 'I've got this' takes it ---")
    DONE_T = "Send the renewal quote"
    assert post(c, KEY, "sales-agent", [span("draft", NOW - HOUR, {
        "trovis.run.id": "r-done", "trovis.loop.title": DONE_T,
        "trovis.loop.external_id": "job-done",
        "trovis.handoff.direction": "to_human",
        "trovis.handoff.target_id": "ann@test.com",
    })]).status_code == 200
    row = items(c, H)[DONE_T]
    check("Done resolves the decision", c.post(
        f"/loops/{row['id']}/handoffs/{row['awaiting_handoff_event_id']}/complete",
        headers=H).status_code == 200)
    done_row = items(c, H)[DONE_T]
    check("Done ends the wait", done_row["status"] != "waiting_on_you")
    check("Done leaves nothing orphaned", done_row["holder"]["kind"] != "unassigned")

    MINE_T = "Reconcile the March invoices"
    assert post(c, KEY, "finance-agent", [span("prep", NOW - HOUR, {
        "trovis.run.id": "r-take", "trovis.loop.title": MINE_T,
        "trovis.loop.external_id": "job-take",
        "trovis.handoff.direction": "to_human",
        "trovis.handoff.target_id": "ann@test.com",
    })]).status_code == 200
    row = items(c, H)[MINE_T]
    check("I've got this resolves the decision", c.post(
        f"/loops/{row['id']}/handoffs/{row['awaiting_handoff_event_id']}/accept",
        headers=H).status_code == 200)
    took = items(c, H)[MINE_T]
    check("taking it ends the wait", took["status"] != "waiting_on_you")
    check("taking it leaves nothing orphaned", took["holder"]["kind"] != "unassigned")

    print("\n--- a stale decision cannot be acted on twice ---")
    # The desk can be a few seconds out of date; a second press must be a
    # no-op, not a second event on the record.
    again = c.post(
        f"/loops/{mine['id']}/handoffs/{mine['awaiting_handoff_event_id']}/decline",
        headers=H, json={})
    check("pressing Not mine twice is idempotent", again.status_code == 200)
    kinds2 = [e.get("type") for e in c.get(f"/loops/{mine['id']}", headers=H).json()["events"]]
    check("and appends nothing the second time",
          kinds2.count("handoff_declined") == 1)

    print("\n--- the fleet pulse can count agents without loading the roster ---")
    # Home says "N agents" out loud. The only agent data it may read is
    # /dashboard/cost, whose `agents` list is capped at the top 8 spenders —
    # counting THAT would print "8 agents" for a fleet of 11.
    for i in range(11):
        assert post(c, KEY, f"pulse-agent-{i}", [span("run", NOW - HOUR, {
            "gen_ai.request.model": "claude-sonnet-4-6"})]).status_code == 200
    cost = c.get("/dashboard/cost", headers=H).json()
    seen = {a["service_name"] for a in c.get("/agents", headers=H).json()}
    check("agent_count is the real total, not the capped list",
          cost["agent_count"] == len(seen))
    check("...and the fleet is bigger than the cap, so the two genuinely differ",
          len(seen) > len(cost["agents"]))
    check("the capped list is still capped", len(cost["agents"]) <= 8)
    # First-run detection reads the same field: a non-zero count must never
    # look like "nothing is connected".
    check("a populated account never reads as a first run", cost["agent_count"] > 0)

print("\n" + (f"FAILURES: {failures}" if failures else "All Home desk checks passed."))
raise SystemExit(1 if failures else 0)
