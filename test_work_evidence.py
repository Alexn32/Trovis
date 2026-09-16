"""Work Evidence — can Trovis show the observation behind each claim, and
say what it does and does not prove?

Run:
  TROVIS_DISABLE_PRICING_SYNC=1 python3 test_work_evidence.py
"""
from __future__ import annotations

import inspect
import os
import tempfile
import time
import uuid

os.environ.update({
    "OVERSEE_DISABLE_PRICING_SYNC": "1",
    "TROVIS_DISABLE_PRICING_SYNC": "1",
    "TROVIS_DISABLE_ALERTS": "1",
    "TROVIS_DISABLE_LOOP_SWEEP": "1",
    "TROVIS_LOOP_TITLES": "off",
})
os.environ.pop("DATABASE_URL", None)
os.environ.pop("ANTHROPIC_API_KEY", None)

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()

import database
database.SQLITE_PATH = _tmp.name

import main
import work_evidence
from fastapi.testclient import TestClient

main._auto_describe = lambda *a, **k: False

failures = []


def check(label, cond):
    print(("  PASS " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


def kv(d):
    return [{"key": k, "value": {"stringValue": str(v)}} for k, v in d.items()]


NS = 1_000_000_000
NOW = time.time_ns()
_n = [0]


def sp(name, off_s, attrs, *, status=1, msg=None, dur_ns=5_000_000):
    _n[0] += 1
    start = NOW - off_s * NS
    s = {
        "traceId": f"{_n[0]:032d}", "spanId": f"{_n[0]:016d}", "name": name, "kind": 1,
        "startTimeUnixNano": str(start), "endTimeUnixNano": str(start + dur_ns),
        "status": {"code": status}, "attributes": kv(attrs),
    }
    if msg is not None:
        s["status"]["message"] = msg
    return s, start


print("no model is involved")
src = inspect.getsource(work_evidence)
check("work_evidence imports no LLM client", not any(
    w in src for w in ("anthropic", "import asker", "import describer", "investigator")))
check("the evidence types are the six documented ones", work_evidence.EVIDENCE_TYPES == (
    "execution", "action_reported", "external_state", "handoff", "completion", "cost"))
check("no 'inferred' correlation exists", "inferred" not in work_evidence.CORRELATION_METHODS)

with TestClient(main.app) as c:
    a = c.post("/auth/signup", json={
        "email": "ada@evid.test", "password": "supersecret123",
        "name": "Ada Lovelace", "account_type": "business", "org_name": "Evid Co",
    }).json()
    b = c.post("/auth/signup", json={
        "email": "bob@evid.test", "password": "supersecret123",
        "name": "Bob", "account_type": "business", "org_name": "Other Co",
    }).json()
    KA, TA = a["api_key"], a["token"]
    KB, TB = b["api_key"], b["token"]
    HA = {"Authorization": f"Bearer {TA}"}
    HB = {"Authorization": f"Bearer {TB}"}
    aid = database.resolve_session(TA)["account_id"]
    user_a = database.resolve_session(TA)["user_id"] if "user_id" in database.resolve_session(TA) else None

    def post(key, svc, spans, resource=None):
        res = {"service.name": svc}
        res.update(resource or {})
        r = c.post("/v1/traces", json={"resourceSpans": [{
            "resource": {"attributes": kv(res)},
            "scopeSpans": [{"spans": spans}],
        }]}, headers={"X-Trovis-Api-Key": key})
        assert r.status_code == 200, r.text
        return r

    def item_by_title(h, title):
        items = c.get("/work/items?limit=50", headers=h).json()["items"]
        return next(i for i in items if i["title"] == title)

    def evidence(h, item_id):
        r = c.get(f"/work/items/{item_id}/evidence", headers=h)
        return r

    def of_type(ev, t):
        return [e for e in ev if e["evidence_type"] == t]

    # ---- 1 / 3 / 7: a Grok (xAI SDK) worker runs and reports a refund tool call
    print("\n1. agent execution + 3. tool action, no external outcome yet")
    GROK = {"trovis.sdk.platform": "xai"}
    s1, t1 = sp("message_received", 900, {"trovis.loop.title": "Refund order #4471",
                                          "trovis.loop.external_id": "order-4471"})
    s2, t2 = sp("tool_call", 880, {"trovis.loop.external_id": "order-4471",
                                   "trovis.tool.name": "refund_customer",
                                   "trovis.run.cost_usd": "0.0042"}, dur_ns=1_400_000_000)
    post(KA, "refunds-agent", [s1, s2], GROK)
    item = item_by_title(HA, "Refund order #4471")
    r = evidence(HA, item["id"])
    check("evidence endpoint answers", r.status_code == 200)
    body = r.json()
    ev = body["evidence"]
    ex = of_type(ev, "execution")
    check("one execution record for the Grok worker", len(ex) == 1)
    check("execution is attributed to grok (xAI SDK), by stamp", ex[0]["source_connector_id"] == "grok")
    check("execution source_type is agent, label is the route",
          ex[0]["source_type"] == "agent" and ex[0]["source_label"] == "refunds-agent:main")
    check("execution correlated by explicit key", ex[0]["correlation_method"] == "explicit_key")
    check("execution observed_at is the first span's own time, not now",
          ex[0]["observed_at"] == database._ns_to_iso(t1))
    check("execution counts both spans", ex[0]["details"]["span_count"] == 2)

    act = of_type(ev, "action_reported")
    check("one action_reported record", len(act) == 1)
    check("it names the tool exactly", act[0]["details"]["tool"] == "refund_customer")
    check("it points at the exact span", act[0]["span_id"] == s2["spanId"] and act[0]["trace_id"] == s2["traceId"])
    check("it says what it proves: the worker REPORTED it", act[0]["details"]["proves"] == "reported")
    check("it did not error", act[0]["details"]["errored"] is False)
    check("its time is the tool span's time", act[0]["observed_at"] == database._ns_to_iso(t2))
    check("7. no external outcome evidence appears out of nowhere", of_type(ev, "external_state") == [])
    check("no completion evidence before a close", of_type(ev, "completion") == [])

    cost = of_type(ev, "cost")
    check("one cost record, pointing at its span",
          len(cost) == 1 and cost[0]["details"]["span_ids"] == [s2["spanId"]])
    check("cost basis is 'reported' (SDK-authoritative), amount exact",
          cost[0]["details"]["basis"] == "reported" and cost[0]["details"]["amount_usd"] == 0.0042)
    check("nothing says verified", "verified" not in str(body).lower())

    # ---- 2: generic OTEL does not become a brand
    print("\n2. generic OTEL is custom-otel, even when the service is named after Claude")
    g1, _ = sp("run", 700, {"trovis.loop.title": "Triage inbox", "trovis.loop.external_id": "tri-1"})
    post(KA, "claude-refund-helper", [g1])
    gi = item_by_title(HA, "Triage inbox")
    gev = evidence(HA, gi["id"]).json()["evidence"]
    gex = of_type(gev, "execution")
    check("generic service → custom-otel", len(gex) == 1 and gex[0]["source_connector_id"] == "custom-otel")

    # ---- 8: Grok and Grok Bot on the same item stay distinct sources
    print("\n8. Grok and Grok Bot remain distinct")
    b1, tb = sp("report", 870, {"trovis.loop.external_id": "order-4471",
                                "trovis.tool.name": "refund_customer"})
    post(KA, "refunds-agent", [b1], {"trovis.platform": "cursor-grok-bot", "trovis.connector.id": "grok-bot"})
    ev = evidence(HA, item["id"]).json()["evidence"]
    ex_ids = sorted(e["source_connector_id"] for e in of_type(ev, "execution"))
    check("two execution sources: grok and grok-bot", ex_ids == ["grok", "grok-bot"])
    acts = of_type(ev, "action_reported")
    check("two action reports with different sources", sorted(a_["source_connector_id"] for a_ in acts) == ["grok", "grok-bot"])
    check("9. multiple sources support one run", len({e["source_connector_id"] for e in ev}) >= 2)

    # ---- 4 / 6: Stripe reports the outcome, correlated by the explicit key
    print("\n4. SaaS correlated outcome is external_state with exact provider ids")
    loop_id = item["id"]
    tw = NOW - 860 * NS
    res = database.apply_saas_loop_effect(
        aid, loop_id, provider="stripe", effect="wait", object_id="pi_4471",
        target_id="Stripe", waiting_on="payment processing",
        event_id="evt_proc_1", event_type="payment_intent.processing", event_time_unix=tw,
    )
    check("wait applied", res["status"] == "applied")
    tc = NOW - 850 * NS
    res = database.apply_saas_loop_effect(
        aid, loop_id, provider="stripe", effect="clear", object_id="pi_4471",
        target_id="Stripe", event_id="evt_ok_2", event_type="payment_intent.succeeded",
        event_time_unix=tc,
    )
    check("clear applied", res["status"] == "applied")
    ev = evidence(HA, item["id"]).json()["evidence"]
    ext = of_type(ev, "external_state")
    check("two external_state records (processing, then succeeded)", len(ext) == 2)
    check("both sourced from stripe as a system", all(
        e["source_connector_id"] == "stripe" and e["source_type"] == "system" for e in ext))
    check("both correlated by explicit key", all(e["correlation_method"] == "explicit_key" for e in ext))
    check("11. provider ids exact on the wait",
          ext[0]["external_event_id"] == "evt_proc_1" and ext[0]["external_object_id"] == "pi_4471")
    check("11. provider ids exact on the CLEAR (previously lost)",
          ext[1]["external_event_id"] == "evt_ok_2" and ext[1]["external_object_id"] == "pi_4471"
          and ext[1]["details"]["provider_event_type"] == "payment_intent.succeeded"
          and ext[1]["details"]["provider_ids_recorded"] is True)
    check("10. observed_at is the provider event time",
          ext[0]["observed_at"] == database._ns_to_iso(tw) and ext[1]["observed_at"] == database._ns_to_iso(tc))
    check("effects read wait then clear", ext[0]["details"]["effect"] == "wait" and ext[1]["details"]["effect"] == "clear")
    check("6. the agent's report and Stripe's outcome are separate records with separate sources",
          {a_["source_connector_id"] for a_ in of_type(ev, "action_reported")} == {"grok", "grok-bot"}
          and all(e["source_connector_id"] == "stripe" for e in ext))
    check("6. the tool report still only claims 'reported'",
          all(a_["details"]["proves"] == "reported" for a_ in of_type(ev, "action_reported")))

    # ---- 5: an uncorrelated SaaS event invents nothing and attaches to nothing
    print("\n5. SaaS uncorrelated event → no run, no evidence for unrelated work")
    before_loops = database.list_loops(aid) if hasattr(database, "list_loops") else None
    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute("SELECT COUNT(*) AS n FROM loops WHERE account_id = ?", (aid,))
        n_loops_before = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) AS n FROM loop_events WHERE account_id = ?", (aid,))
        n_events_before = cur.fetchone()["n"]
    check("no open loop for an unknown key", database.find_open_loop_by_external_id(aid, "order-nope") is None)
    check("the event is still claimed for idempotency / connection health",
          database.claim_saas_event(aid, "stripe", "evt_orphan", "payment_intent.succeeded"))
    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute("SELECT COUNT(*) AS n FROM loops WHERE account_id = ?", (aid,))
        cur2 = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) AS n FROM loop_events WHERE account_id = ?", (aid,))
        ev2 = cur.fetchone()["n"]
    check("no loop was invented", cur2 == n_loops_before)
    check("no loop event was written", ev2 == n_events_before)
    ev = evidence(HA, item["id"]).json()["evidence"]
    check("the orphan is not evidence for the refund run",
          not any(e.get("external_event_id") == "evt_orphan" for e in ev))

    # ---- 12: time-adjacency is named as such
    print("\n12. correlation is truthful: keyless spans read time_adjacency")
    k1, _ = sp("run", 600, {"trovis.loop.title": "Ad hoc cleanup"})
    k2, _ = sp("run", 590, {})
    post(KA, "adhoc-agent", [k1, k2])
    ki = item_by_title(HA, "Ad hoc cleanup")
    kev = evidence(HA, ki["id"]).json()["evidence"]
    kex = of_type(kev, "execution")
    check("keyless loop's execution is time_adjacency, not explicit_key",
          len(kex) == 1 and kex[0]["correlation_method"] == "time_adjacency")

    # ---- human handoff: possession chain preserved, human evidence is direct
    print("\nhuman / holder evidence")
    h1, th = sp("ask_human", 500, {"trovis.loop.title": "Approve wire #9",
                                   "trovis.loop.external_id": "wire-9",
                                   "trovis.handoff.direction": "to_human",
                                   "trovis.handoff.target_id": "ada@evid.test",
                                   "trovis.handoff.reason": "over limit"})
    post(KA, "treasury-agent", [h1], GROK)
    hi = item_by_title(HA, "Approve wire #9")
    detail = c.get(f"/work/items/{hi['id']}", headers=HA).json()
    check("17. possession chain unchanged: a person holds it",
          detail["holder"]["kind"] == "human" and detail["awaiting_handoff_event_id"])
    hev = evidence(HA, hi["id"]).json()["evidence"]
    hand = of_type(hev, "handoff")
    check("handoff_initiated evidence from the agent span",
          len(hand) == 1 and hand[0]["details"]["event"] == "handoff_initiated"
          and hand[0]["source_type"] == "agent" and hand[0]["span_id"] == h1["spanId"])
    check("its connector comes from the span behind the event", hand[0]["source_connector_id"] == "grok")
    check("its time is the span's time", hand[0]["observed_at"] == database._ns_to_iso(th))
    r = c.post(f"/loops/{hi['id']}/handoffs/{detail['awaiting_handoff_event_id']}/complete", headers=HA)
    check("human completes the handoff", r.status_code == 200, )
    hev = evidence(HA, hi["id"]).json()["evidence"]
    hand = of_type(hev, "handoff")
    done = [e for e in hand if e["details"]["event"] == "handoff_completed"]
    check("handoff_completed evidence exists", len(done) == 1)
    check("14. its source is a person, resolved by name, with no connector",
          done[0]["source_type"] == "human" and done[0]["source_label"] == "Ada Lovelace"
          and done[0]["source_connector_id"] is None)
    check("a person acting in the product is 'direct' correlation", done[0]["correlation_method"] == "direct")
    check("17. holder is back with the agent",
          c.get(f"/work/items/{hi['id']}", headers=HA).json()["holder"]["kind"] == "agent")

    # ---- completion
    print("\ncompletion evidence")
    d1, td = sp("done", 400, {"trovis.loop.external_id": "order-4471", "trovis.loop.close": "done"})
    post(KA, "refunds-agent", [d1], GROK)
    ev = evidence(HA, item["id"]).json()["evidence"]
    comp = of_type(ev, "completion")
    check("one completion record", len(comp) == 1)
    check("it says the record closed, by the agent, with the reason",
          comp[0]["details"]["proves"] == "recorded_close" and comp[0]["source_type"] == "agent"
          and comp[0]["details"]["reason"] == "completed_by_agent")
    check("it points at the closing span, so the connector is known",
          comp[0]["span_id"] == d1["spanId"] and comp[0]["source_connector_id"] == "grok")
    check("15. the item reads done through the existing API",
          c.get(f"/work/items/{item['id']}", headers=HA).json()["status"] == "done")

    # ---- legacy event without span link → missing stays missing
    print("\n14. missing source information stays missing")
    with database._connect() as conn, database._cursor(conn) as cur:
        legacy_id = database.append_loop_event(
            cur, gi["id"], "handoff_initiated", "agent", "claude-refund-helper:main",
            payload={"direction": "to_agent", "target_id": "other"}, account_id=aid,
            event_time_unix=NOW - 650 * NS,
        )
    gev = evidence(HA, gi["id"]).json()["evidence"]
    legacy = next(e for e in gev if e["event_id"] == legacy_id)
    check("no span link → connector None, correlation None, label falls back to the actor",
          legacy["source_connector_id"] is None and legacy["correlation_method"] is None
          and legacy["source_label"] == "claude-refund-helper:main")

    # ---- 13: account isolation
    print("\n13. account isolation")
    r = evidence(HB, item["id"])
    check("account B gets 404 for A's item, never 403", r.status_code == 404)
    r = c.get(f"/work/items/{item['id']}/evidence")
    check("no credential → rejected", r.status_code in (401, 403))
    check("the read model itself returns None across accounts",
          work_evidence.build_work_item_evidence(
              database.resolve_session(TB)["account_id"], item["id"]) is None)

    # ---- 16 / 18 / 19: surrounding behaviour unchanged
    print("\n16/18/19. surrounding behaviour unchanged")
    r = c.get(f"/work/items/{item['id']}?include=runs", headers=HA).json()
    check("18. runs still carry cost as before", any(x.get("cost_usd") == 0.0042 for x in r["runs"]))
    check("18. runs still carry no span ids (unchanged contract)", all("span_id" not in x for x in r["runs"]))
    r = c.get("/connect/health", headers=HA).json()
    grok = next(x for x in r["connectors"] if x["connector_id"] == "grok")
    stripe = next(x for x in r["connectors"] if x["connector_id"] == "stripe")
    check("19. connection health still reads grok as observed", grok["observed"] is True)
    check("19. stripe observed via saas_events, not authorized (no OAuth row)",
          stripe["observed"] is True and stripe["state"] == "not_connected")
    ordered = [e["observed_at"] for e in ev if e["observed_at"]]
    check("records are ordered by observation time", ordered == sorted(ordered))
    check("spans_truncated is false for a small item",
          evidence(HA, item["id"]).json()["spans_truncated"] is False)

print()
if failures:
    print(f"{len(failures)} FAILED:")
    for f in failures:
        print("  -", f)
    raise SystemExit(1)
print("all work-evidence checks passed")
