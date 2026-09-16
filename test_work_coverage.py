"""Work Coverage — can Trovis see this run, dimension by dimension, without
pretending to know what the workflow should have contained?

Run:
  TROVIS_DISABLE_PRICING_SYNC=1 python3 test_work_coverage.py
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
import work_coverage
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


USAGE = {"gen_ai.request.model": "no-such-model-xyz", "gen_ai.usage.input_tokens": "120", "gen_ai.usage.output_tokens": "30"}

print("shape and vocabulary")
src = inspect.getsource(work_coverage)
check("33. no model client in the coverage module", not any(
    w in src for w in ("anthropic", "import asker", "import describer", "investigator")))
check("34. no global Work summary / board read", not any(
    w in src for w in ("get_work_board", "get_work_summary", "get_work_items(", "list_loops")))
check("dimensions are the five audited ones", work_coverage.DIMENSIONS == (
    "execution", "actions", "external_outcomes", "handoffs", "cost"))
check("states: observed / partial / not_observed / unknown — no not_applicable, no score",
      work_coverage.STATES == ("observed", "partial", "not_observed", "unknown"))
for banned in ("score", "percent", "grade", "confidence", "verified", "healthy", "success"):
    check(f"vocabulary never says '{banned}'", banned not in " ".join(work_coverage.REASONS + work_coverage.STATES))

with TestClient(main.app) as c:
    a = c.post("/auth/signup", json={
        "email": "ada@cov.test", "password": "supersecret123",
        "name": "Ada Lovelace", "account_type": "business", "org_name": "Cov Co",
    }).json()
    b = c.post("/auth/signup", json={
        "email": "bob@cov.test", "password": "supersecret123",
        "name": "Bob", "account_type": "business", "org_name": "Other Co",
    }).json()
    KA, TA = a["api_key"], a["token"]
    KB, TB = b["api_key"], b["token"]
    HA = {"Authorization": f"Bearer {TA}"}
    HB = {"Authorization": f"Bearer {TB}"}
    aid = database.resolve_session(TA)["account_id"]

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

    def coverage(h, item_id):
        return c.get(f"/work/items/{item_id}/coverage", headers=h)

    def dims(h, item_id):
        r = coverage(h, item_id)
        assert r.status_code == 200, r.text
        body = r.json()
        return body, {d["id"]: d for d in body["dimensions"]}

    GROK = {"trovis.sdk.platform": "xai"}

    # ---- the refund run: two Grok workers, a Grok Bot, actions, errors, mixed cost
    print("\n1/4/5/13. a run with attributable execution, actions and mixed cost")
    s1, t1 = sp("message_received", 900, {"trovis.loop.title": "Refund order #4471",
                                          "trovis.loop.external_id": "order-4471", **USAGE,
                                          "trovis.run.cost_usd": "0.0042"})
    s2, t2 = sp("tool_call", 880, {"trovis.loop.external_id": "order-4471",
                                   "trovis.tool.name": "refund_customer", **USAGE})
    s3, t3 = sp("tool_call", 870, {"trovis.loop.external_id": "order-4471",
                                   "trovis.tool.name": "lookup_order"}, status=2, msg="Card declined")
    post(KA, "refund-agent", [s1, s2, s3], GROK)
    # A second worker of the same service over the same connector: the
    # sub-agent id rides the span (that is where ingest reads it).
    post(KA, "refund-agent", [sp("assist", 865, {"trovis.loop.external_id": "order-4471",
                                                 "trovis.agent.id": "reviewer"})[0]], GROK)
    item = item_by_title(HA, "Refund order #4471")
    body, d = dims(HA, item["id"])
    check("response carries the five dimensions in order",
          [x["id"] for x in body["dimensions"]] == list(work_coverage.DIMENSIONS))
    check("1. execution observed", d["execution"]["state"] == "observed" and d["execution"]["reason"] == "execution_evidence")
    check("4. two workers on one connector both remain as sources", sorted(
        s["source_label"] for s in d["execution"]["sources"]) == ["refund-agent:main", "refund-agent:reviewer"])
    check("execution sources are attributed by stamp, not name", all(
        s["source_connector_id"] == "grok" for s in d["execution"]["sources"]))
    check("5. actions observed, two reports", d["actions"]["state"] == "observed" and d["actions"]["evidence_count"] == 2)
    check("17. an errored action still counts as observed (failure is not coverage)",
          d["actions"]["state"] == "observed" and d["actions"]["evidence_count"] == 2)
    check("6/10. no external observation → external_outcomes UNKNOWN, not 'not_observed', not 'missing'",
          d["external_outcomes"]["state"] == "unknown"
          and d["external_outcomes"]["reason"] == "no_external_observations"
          and d["external_outcomes"]["evidence_count"] == 0)
    check("12. no handoff record → handoffs unknown, not deficient",
          d["handoffs"]["state"] == "unknown" and d["handoffs"]["reason"] == "no_handoff_records")
    cost = d["cost"]
    check("13. cost is PARTIAL: two usage spans, one with a reported cost, one on an unknown model",
          cost["state"] == "partial" and cost["reason"] == "some_usage_cost_unknown"
          and cost["details"]["usage_spans"] == 2 and cost["details"]["cost_known_spans"] == 1
          and cost["details"]["cost_unknown_spans"] == 1 and cost["details"]["cost_covered_spans"] == 0)
    check("15. the amount is the priced part only, never zero-filled", cost["details"]["amount_usd"] == 0.0042)
    check("24. last_observed_at is the newest supporting observation's own time",
          d["actions"]["last_observed_at"] == database._ns_to_iso(t3)
          and d["execution"]["last_observed_at"] == database._ns_to_iso(NOW - 865 * NS))
    check("20. explicit-key correlation auditable", d["execution"]["correlation_methods"] == ["explicit_key"])
    check("bounded flag false on a small run", body["evidence_bounded"] is False
          and d["execution"]["from_bounded_evidence"] is False)

    # ---- 3: Grok Bot on the same run stays a distinct source
    print("\n3. Grok and Grok Bot remain distinct sources")
    post(KA, "refund-agent", [sp("report", 860, {"trovis.loop.external_id": "order-4471",
                                                 "trovis.tool.name": "refund_customer"})[0]],
         {"trovis.platform": "cursor-grok-bot"})
    _, d = dims(HA, item["id"])
    check("execution sources include grok and grok-bot separately", sorted({
        s["source_connector_id"] for s in d["execution"]["sources"]}) == ["grok", "grok-bot"])
    check("action sources keep the two connectors apart", sorted({
        s["source_connector_id"] for s in d["actions"]["sources"]}) == ["grok", "grok-bot"])

    # ---- 7/8/9: external state, with and without action reports
    print("\n7/8/9. external observations")
    tw = NOW - 850 * NS
    res = database.apply_saas_loop_effect(
        aid, item["id"], provider="stripe", effect="wait", object_id="pi_4471", target_id="Stripe",
        event_id="evt_1", event_type="payment_intent.processing", event_time_unix=tw,
    )
    check("wait applied", res["status"] == "applied")
    _, d = dims(HA, item["id"])
    ext = d["external_outcomes"]
    check("7. external_outcomes observed from Stripe", ext["state"] == "observed"
          and ext["reason"] == "external_observations"
          and ext["sources"] == [{"source_type": "system", "source_connector_id": "stripe", "source_label": "Stripe"}])
    check("24. its time is the provider event time", ext["last_observed_at"] == database._ns_to_iso(tw))
    check("9. actions and external outcomes are independent dimensions — no pairing claim",
          d["actions"]["state"] == "observed" and ext["state"] == "observed"
          and "pair" not in str(ext) and "correspond" not in str(ext))
    check("external outcomes is never 'partial' (no known denominator)", ext["state"] != "partial")
    # The evidence model files a SaaS wait as an external observation (the
    # provider reported its object's state), not as a handoff record, so the
    # handoffs dimension is untouched by it.
    check("a SaaS wait is external state, not a handoff record",
          d["handoffs"]["state"] == "unknown" and d["handoffs"]["evidence_count"] == 0)

    # 8: an item with ONLY external state (no action report) — a SaaS wait on a
    # keyed loop whose spans never named a tool.
    g1, _ = sp("run", 700, {"trovis.loop.title": "Reconcile invoice #9", "trovis.loop.external_id": "inv-9"})
    post(KA, "ledger-agent", [g1])
    inv = item_by_title(HA, "Reconcile invoice #9")
    database.apply_saas_loop_effect(
        aid, inv["id"], provider="hubspot", effect="wait", object_id="deal_9", target_id="HubSpot",
        event_id="evt_h1", event_type="deal.propertyChange", event_time_unix=NOW - 690 * NS,
    )
    _, d = dims(HA, inv["id"])
    check("8. external observation without any action report is still observed",
          d["external_outcomes"]["state"] == "observed" and d["actions"]["state"] == "unknown")
    check("2. generic OTEL execution counts, and its source stays custom-otel",
          d["execution"]["state"] == "observed"
          and d["execution"]["sources"][0]["source_connector_id"] == "custom-otel")
    check("14. execution with no model usage and no cost → cost UNKNOWN, no_model_usage_observed",
          d["cost"]["state"] == "unknown" and d["cost"]["reason"] == "no_model_usage_observed"
          and d["cost"]["details"]["amount_usd"] is None)

    # ---- 14: usage observed but none of it priced
    print("\n14. model usage with no price is NOT observed cost")
    u1, _ = sp("model_call", 600, {"trovis.loop.title": "Draft summary", "trovis.loop.external_id": "sum-1", **USAGE})
    u2, _ = sp("model_call", 590, {"trovis.loop.external_id": "sum-1", **USAGE})
    post(KA, "writer-agent", [u1, u2], {"trovis.sdk.platform": "anthropic"})
    summ = item_by_title(HA, "Draft summary")
    _, d = dims(HA, summ["id"])
    check("cost not_observed / usage_cost_unknown with the denominator exposed",
          d["cost"]["state"] == "not_observed" and d["cost"]["reason"] == "usage_cost_unknown"
          and d["cost"]["details"] == {"usage_spans": 2, "cost_known_spans": 0, "cost_covered_spans": 0,
                                        "cost_unknown_spans": 2, "amount_usd": None, "basis": None})
    check("15. missing cost is None, not 0", d["cost"]["details"]["amount_usd"] is None)
    check("execution observed from claude", d["execution"]["sources"][0]["source_connector_id"] == "claude")

    # all usage priced by a reported run cost → observed
    p1, _ = sp("model_call", 500, {"trovis.loop.title": "Price check", "trovis.loop.external_id": "pc-1",
                                   "trovis.run.id": "pc-1", **USAGE, "trovis.run.cost_usd": "0.01"})
    p2, _ = sp("model_call", 490, {"trovis.loop.external_id": "pc-1", "trovis.run.id": "pc-1", **USAGE})
    post(KA, "pricing-agent", [p1, p2], GROK)
    pc = item_by_title(HA, "Price check")
    _, d = dims(HA, pc["id"])
    check("13. every usage span has a recorded cost (one reported, one covered) → observed / usage_cost_known",
          d["cost"]["state"] == "observed" and d["cost"]["reason"] == "usage_cost_known"
          and d["cost"]["details"]["cost_known_spans"] == 2 and d["cost"]["details"]["cost_covered_spans"] == 1
          and d["cost"]["details"]["amount_usd"] == 0.01 and d["cost"]["details"]["basis"] == "reported")

    # ---- 11: human handoff
    print("\n11. human handoff")
    h1, th = sp("ask_human", 400, {"trovis.loop.title": "Approve wire #9", "trovis.loop.external_id": "wire-9",
                                   "trovis.handoff.direction": "to_human",
                                   "trovis.handoff.target_id": "ada@cov.test"})
    post(KA, "treasury-agent", [h1], GROK)
    wire = item_by_title(HA, "Approve wire #9")
    detail = c.get(f"/work/items/{wire['id']}", headers=HA).json()
    check("30. possession chain unchanged: a person holds it", detail["holder"]["kind"] == "human")
    r = c.post(f"/loops/{wire['id']}/handoffs/{detail['awaiting_handoff_event_id']}/complete", headers=HA)
    check("human completes", r.status_code == 200)
    _, d = dims(HA, wire["id"])
    hd = d["handoffs"]
    check("handoffs observed with both the agent's offer and the person's decision",
          hd["state"] == "observed" and hd["evidence_count"] == 2
          and sorted(s["source_type"] for s in hd["sources"]) == ["agent", "human"])
    check("the person appears by resolved name", any(s["source_label"] == "Ada Lovelace" for s in hd["sources"]))
    check("21/22. correlation methods auditable: explicit_key (span) and direct (person)",
          hd["correlation_methods"] == ["direct", "explicit_key"])

    # ---- 16/18: completion is not outcome coverage; closing earns nothing
    print("\n16/18. completion does not become coverage")
    post(KA, "writer-agent", [sp("done", 380, {"trovis.loop.external_id": "sum-1", "trovis.loop.close": "done"})[0]],
         {"trovis.sdk.platform": "anthropic"})
    check("the item reads done", c.get(f"/work/items/{summ['id']}", headers=HA).json()["status"] == "done")
    _, d = dims(HA, summ["id"])
    check("16. external_outcomes still unknown after the record closed",
          d["external_outcomes"]["state"] == "unknown" and d["external_outcomes"]["evidence_count"] == 0)
    check("18. a closed run with sparse evidence gets no positive verdict: actions unknown, cost not_observed",
          d["actions"]["state"] == "unknown" and d["cost"]["state"] == "not_observed")
    check("completion evidence is counted in no dimension", all(
        "completion" not in x["evidence_types"] for x in d.values()))

    # ---- 22: origin / time-adjacency auditable
    print("\n21/22. keyless placement is auditable, not upgraded")
    k1, _ = sp("tool_call", 300, {"trovis.loop.title": "Ad hoc cleanup", "trovis.tool.name": "sweep"})
    post(KA, "adhoc-agent", [k1])
    post(KA, "adhoc-agent", [sp("tool_call", 290, {"trovis.tool.name": "sweep"})[0]])
    ad = item_by_title(HA, "Ad hoc cleanup")
    _, d = dims(HA, ad["id"])
    check("actions carry origin and time_adjacency exactly as recorded",
          d["actions"]["correlation_methods"] == ["origin", "time_adjacency"])
    check("execution sums the same set", d["execution"]["correlation_methods"] == ["origin", "time_adjacency"])

    # ---- 19: historical evidence with unknown correlation still counts
    print("\n19. unrecorded correlation is counted, not upgraded")
    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute("UPDATE spans SET loop_link = NULL WHERE span_id = ?", (k1["spanId"],))
    _, d = dims(HA, ad["id"])
    check("the observation still supports 'observed'", d["actions"]["state"] == "observed")
    check("its correlation reads 'unrecorded', nothing stronger",
          "unrecorded" in d["actions"]["correlation_methods"] and "origin" not in d["actions"]["correlation_methods"])

    # ---- 23: truncation is a bound, not missing coverage
    print("\n23. bounded evidence")
    saved = database._WORK_EVIDENCE_SPAN_LIMIT
    database._WORK_EVIDENCE_SPAN_LIMIT = 1
    try:
        body, d = dims(HA, item["id"])
    finally:
        database._WORK_EVIDENCE_SPAN_LIMIT = saved
    check("evidence_bounded is true and the span-derived dimensions say so", body["evidence_bounded"] is True
          and d["execution"]["from_bounded_evidence"] and d["actions"]["from_bounded_evidence"]
          and d["cost"]["from_bounded_evidence"])
    check("event-derived dimensions are not bounded", not d["external_outcomes"]["from_bounded_evidence"]
          and not d["handoffs"]["from_bounded_evidence"])
    check("a bound does not turn observed into partial", d["execution"]["state"] == "observed"
          and d["external_outcomes"]["state"] == "observed")

    # ---- 27/28: SaaS never invents work or coverage
    print("\n27/28. an uncorrelated SaaS event creates nothing")
    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute("SELECT COUNT(*) AS n FROM loops WHERE account_id = ?", (aid,))
        n_before = cur.fetchone()["n"]
    check("no open loop for the orphan key", database.find_open_loop_by_external_id(aid, "order-nope") is None)
    database.claim_saas_event(aid, "stripe", "evt_orphan", "payment_intent.succeeded")
    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute("SELECT COUNT(*) AS n FROM loops WHERE account_id = ?", (aid,))
        n_after = cur.fetchone()["n"]
    check("no loop invented", n_after == n_before)
    _, d = dims(HA, inv["id"])
    check("no coverage change on an unrelated run", d["external_outcomes"]["evidence_count"] == 1)

    # ---- the cost denominator, against the actual ingest path
    print("\nD. the cost denominator is what ingest recorded, span by span")

    def span_row(span_id):
        with database._connect() as conn, database._cursor(conn) as cur:
            cur.execute("SELECT total_tokens, estimated_cost_usd, cost_source FROM spans WHERE span_id = ?", (span_id,))
            return dict(cur.fetchone())

    PRICED_MODEL = "claude-sonnet-5"  # in _PRICING_SEED

    # D1. a non-model span (a tool call, no usage attributes) is NOT a usage span
    n1, _ = sp("tool_call", 250, {"trovis.loop.title": "Denominator run", "trovis.loop.external_id": "den-1",
                                  "trovis.tool.name": "lookup"})
    # D2. a model span with real usage on a priced model → usage, cost known (own estimate)
    n2, _ = sp("model_call", 249, {"trovis.loop.external_id": "den-1", "gen_ai.request.model": PRICED_MODEL,
                                   "gen_ai.usage.input_tokens": "1000", "gen_ai.usage.output_tokens": "100"})
    # D3. a zero-token usage span on a priced model → usage (total 0, not NULL), cost known (0.0)
    n3, _ = sp("model_call", 248, {"trovis.loop.external_id": "den-1", "gen_ai.request.model": PRICED_MODEL,
                                   "gen_ai.usage.input_tokens": "0", "gen_ai.usage.output_tokens": "0"})
    # D4. a model name but NO usage attributes → not a usage span (no default fills it in)
    n4, _ = sp("model_call", 247, {"trovis.loop.external_id": "den-1", "gen_ai.request.model": PRICED_MODEL})
    # D5. cache-only usage → still a usage span (cache counts are billed usage)
    n5, _ = sp("model_call", 246, {"trovis.loop.external_id": "den-1", "gen_ai.request.model": PRICED_MODEL,
                                   "gen_ai.usage.cache_read_input_tokens": "500"})
    post(KA, "den-agent", [n1, n2, n3, n4, n5], {"trovis.sdk.platform": "anthropic"})
    r1, r2, r3, r4, r5 = (span_row(x["spanId"]) for x in (n1, n2, n3, n4, n5))
    check("D1. no usage attributes → total_tokens NULL and cost NULL", r1["total_tokens"] is None and r1["estimated_cost_usd"] is None)
    check("D2. real usage → total_tokens set and a cost estimated", r2["total_tokens"] == 1100 and r2["estimated_cost_usd"] > 0)
    check("D3. zero-token usage → total_tokens 0 (not NULL) and cost 0.0 (not NULL)",
          r3["total_tokens"] == 0 and r3["estimated_cost_usd"] == 0.0)
    check("D4. a model name alone gives no usage — no default value applies", r4["total_tokens"] is None)
    check("D5. cache-only usage is usage", r5["total_tokens"] == 500 and r5["estimated_cost_usd"] is not None)
    den = item_by_title(HA, "Denominator run")
    _, d = dims(HA, den["id"])
    check("D. denominator counts exactly the three usage spans, cost known for all three → observed",
          d["cost"]["state"] == "observed" and d["cost"]["reason"] == "usage_cost_known"
          and d["cost"]["details"]["usage_spans"] == 3 and d["cost"]["details"]["cost_known_spans"] == 3
          and d["cost"]["details"]["cost_covered_spans"] == 0)
    check("D. the tool span and the usage-less model span are not in the denominator",
          d["cost"]["details"]["usage_spans"] == 3)
    check("D11. amount is the estimated sum, and only that", d["cost"]["details"]["amount_usd"] == round(
        float(r2["estimated_cost_usd"]) + float(r5["estimated_cost_usd"]), 6))

    # D6. covered: a run total reported on one span, per-turn usage on others → covered, cost known, subsumed
    c1, _ = sp("agent_run", 240, {"trovis.loop.title": "Covered run", "trovis.loop.external_id": "cov-1",
                                  "trovis.run.id": "cov-1", "trovis.run.cost_usd": "0.02"})
    c2, _ = sp("llm_output", 239, {"trovis.loop.external_id": "cov-1", "trovis.run.id": "cov-1",
                                   "gen_ai.request.model": PRICED_MODEL, "gen_ai.usage.input_tokens": "800",
                                   "gen_ai.usage.output_tokens": "80"})
    c3, _ = sp("llm_output", 238, {"trovis.loop.external_id": "cov-1", "trovis.run.id": "cov-1",
                                   "gen_ai.request.model": "no-such-model-xyz", "gen_ai.usage.input_tokens": "10",
                                   "gen_ai.usage.output_tokens": "1"})
    post(KA, "cov-agent", [c1, c2, c3], {"trovis.sdk.platform": "claude-agent-sdk"})
    rc1, rc2, rc3 = (span_row(x["spanId"]) for x in (c1, c2, c3))
    check("D6. the run span reports cost with no usage of its own (total_tokens NULL, source reported)",
          rc1["total_tokens"] is None and rc1["cost_source"] == "reported" and rc1["estimated_cost_usd"] == 0.02)
    check("D6. per-turn usage inside a reported run is covered: cost 0 stored, source covered — even on an unknown model",
          rc2["cost_source"] == "covered" and rc2["estimated_cost_usd"] == 0.0
          and rc3["cost_source"] == "covered" and rc3["estimated_cost_usd"] == 0.0)
    cov = item_by_title(HA, "Covered run")
    _, d = dims(HA, cov["id"])
    check("D6. covered spans are cost-known (subsumed) and counted as such, never as priced alone",
          d["cost"]["state"] == "observed" and d["cost"]["reason"] == "usage_cost_known"
          and d["cost"]["details"] == {"usage_spans": 2, "cost_known_spans": 2, "cost_covered_spans": 2,
                                        "cost_unknown_spans": 0, "amount_usd": 0.02, "basis": "reported"})

    # D9. parent and child both carrying usage: the unit is the span as exported, and it is named so
    p_parent, _ = sp("agent_turn", 230, {"trovis.loop.title": "Nested usage", "trovis.loop.external_id": "nest-1",
                                         "gen_ai.request.model": PRICED_MODEL, "gen_ai.usage.input_tokens": "50",
                                         "gen_ai.usage.output_tokens": "5"})
    p_child, _ = sp("model_call", 229, {"trovis.loop.external_id": "nest-1",
                                        "gen_ai.request.model": PRICED_MODEL, "gen_ai.usage.input_tokens": "50",
                                        "gen_ai.usage.output_tokens": "5"})
    p_child["parentSpanId"] = p_parent["spanId"]
    post(KA, "nest-agent", [p_parent, p_child], {"trovis.sdk.platform": "anthropic"})
    nest = item_by_title(HA, "Nested usage")
    _, d = dims(HA, nest["id"])
    check("D9. two usage-bearing spans are two usage SPANS — the API never calls them model calls",
          d["cost"]["details"]["usage_spans"] == 2 and "model_call" not in str(d["cost"]["details"])
          and "model_calls" not in str(d["cost"]))
    check("D9. no field in the response claims a count of independent model invocations",
          not any("call" in k for k in d["cost"]["details"].keys()))

    # D7/D8 are the earlier "Draft summary" (all unknown → not_observed) and "Refund order" (mixed → partial)
    # D10. historical rows: usage with no recorded cost reads cost-unknown; NULL usage acquires nothing
    with database._connect() as conn, database._cursor(conn) as cur:
        # A pre-pricing row: tokens recorded, cost never computed.
        cur.execute("UPDATE spans SET estimated_cost_usd = NULL, cost_source = NULL WHERE span_id = ?", (n2["spanId"],))
    _, d = dims(HA, den["id"])
    check("D10. a usage span whose cost was never recorded is cost-unknown → the run reads partial (2 of 3 known)",
          d["cost"]["state"] == "partial" and d["cost"]["details"]["cost_unknown_spans"] == 1
          and d["cost"]["details"]["cost_known_spans"] == 2)
    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute("UPDATE spans SET total_tokens = NULL, estimated_cost_usd = NULL, cost_source = NULL "
                    "WHERE span_id IN (?, ?, ?)", (n2["spanId"], n3["spanId"], n5["spanId"]))
    _, d = dims(HA, den["id"])
    check("D10. rows with NULL usage acquire no applicability: unknown / no_model_usage_observed, amount None",
          d["cost"]["state"] == "unknown" and d["cost"]["reason"] == "no_model_usage_observed"
          and d["cost"]["details"]["usage_spans"] == 0 and d["cost"]["details"]["amount_usd"] is None)
    check("D12. existing cost totals unchanged: the Work runs fold still shows the estimate on the covered run's report",
          any(x["cost_usd"] == 0.02 for x in c.get(f"/work/items/{cov['id']}?include=runs", headers=HA).json()["runs"]))

    # ---- 25/26: isolation and not-found
    print("\n25/26. account isolation and not-found")
    check("account B gets 404 for A's item", coverage(HB, item["id"]).status_code == 404)
    check("nonexistent item → 404 like Work / Evidence", coverage(HA, 999999).status_code == 404
          and c.get("/work/items/999999/evidence", headers=HA).status_code == 404)
    check("no credential → rejected", c.get(f"/work/items/{item['id']}/coverage").status_code in (401, 403))
    check("the read model returns None across accounts",
          work_coverage.build_work_item_coverage(database.resolve_session(TB)["account_id"], item["id"]) is None)

    # ---- 29/31/32: neighbours unchanged
    print("\n29/31/32. neighbouring models unchanged")
    ev = c.get(f"/work/items/{item['id']}/evidence", headers=HA).json()
    check("32. evidence response keeps its contract", set(ev.keys()) == {"item_id", "generated_at", "spans_truncated", "evidence"}
          and set(ev["evidence"][0].keys()) == {"id", "item_id", "evidence_type", "observed_at", "source_type",
                                                "source_connector_id", "source_label", "correlation_method",
                                                "event_id", "span_id", "trace_id", "external_object_id",
                                                "external_event_id", "details"})
    runs = c.get(f"/work/items/{item['id']}?include=runs", headers=HA).json()["runs"]
    check("31. runs still price only what was priced", any(x["cost_usd"] == 0.0042 for x in runs)
          and all(x["cost_usd"] != 0 for x in runs))
    h = c.get("/connect/health", headers=HA).json()
    grok = next(x for x in h["connectors"] if x["connector_id"] == "grok")
    check("29. connection health still observes grok, independently of coverage", grok["observed"] is True)
    check("no field anywhere reads like a score or verdict", not any(
        w in str(body).lower() for w in ("score", "percent", "grade", "confidence", "verified", "healthy", "success")))

print()
if failures:
    print(f"{len(failures)} FAILED:")
    for f in failures:
        print("  -", f)
    raise SystemExit(1)
print("all work-coverage checks passed")
