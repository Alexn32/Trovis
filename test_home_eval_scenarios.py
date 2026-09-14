"""The evaluation fixtures, checked without a model anywhere near them.

Two things are verified here, and neither of them is "the AI is good":

1. **GROUND TRUTH.** Each scenario's records really do establish what its
   rubric claims they establish. A rubric is only worth something if the
   fixture underneath it is what the rubric says it is — otherwise the
   evaluation grades the model against a fiction.

2. **DISCOVERABILITY.** The evidence each scenario turns on is actually
   reachable through the real retrieval allowlist, inside the real budget.
   This is the precondition for discovery: if the tools cannot surface the
   pattern, no prompt can find it, and that is a retrieval defect rather than
   a model one.

What this file deliberately does NOT do is run a model or score one. It is
safe for CI: no network, no key, no spend. The live evaluation lives in
`run_home_eval.py` and is opt-in.

Run:
  TROVIS_DISABLE_PRICING_SYNC=1 python3 test_home_eval_scenarios.py
"""
import os
import tempfile

os.environ.update({
    "OVERSEE_DISABLE_PRICING_SYNC": "1",
    "TROVIS_DISABLE_PRICING_SYNC": "1",
    "TROVIS_DISABLE_ALERTS": "1",
    "TROVIS_DISABLE_LOOP_SWEEP": "1",
    "TROVIS_DISABLE_ANALYSIS": "1",
    "TROVIS_LOOP_TITLES": "off",
})
os.environ.pop("DATABASE_URL", None)
os.environ.pop("ANTHROPIC_API_KEY", None)

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()

import database
database.SQLITE_PATH = _tmp.name

import main
from fastapi.testclient import TestClient

import home_eval_scenarios as S
import investigation_tools

main._auto_describe = lambda *a, **k: False

failures: list[str] = []


def check(label, cond):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        failures.append(label)


def outcomes(runs):
    return [r.get("outcome") for r in runs]


with TestClient(main.app) as c:
    CTX = S.build_all(c)

    # -- A ---------------------------------------------------------------
    print("\n=== A · ordinary successful work ===")
    p = S.probe(CTX["A"])
    runs = p["seen"]["runs"]["runs"]
    check("six items recorded", len(runs) == 6)
    check("all six completed", all(o == "completed" for o in outcomes(runs)))
    check("no failing step on any of them",
          all(not r.get("error_span_count") for r in runs))
    check("the evidence is reachable inside budget", p["budget"]["budget_complete"])

    # -- B ---------------------------------------------------------------
    print("\n=== B · repeated unsuccessful work at a shared step ===")
    p = S.probe(CTX["B"])
    runs = p["seen"]["runs"]["runs"]
    stalled = CTX["B"]["ids"]["stalled"]
    check("six refunds recorded", len(runs) == 6)
    check("four abandoned", sum(1 for o in outcomes(runs) if o == "abandoned") == 4)
    check("two completed — so 'every refund' would be false",
          sum(1 for o in outcomes(runs) if o == "completed") == 2)
    shared = []
    for rid in stalled:
        detail = p["seen"]["inspected"][rid]
        text = str(detail)
        shared.append("approval_service" in text)
    check("each abandoned run's failing step is retrievable and is the same one",
          all(shared) and len(shared) == 4)
    check("the recurrence is visible in one job listing",
          p["seen"]["runs"]["returned"] == 6)

    # -- C ---------------------------------------------------------------
    print("\n=== C · recovered errors ===")
    p = S.probe(CTX["C"])
    runs = p["seen"]["runs"]["runs"]
    check("three shipments recorded", len(runs) == 3)
    check("all three completed", all(o == "completed" for o in outcomes(runs)))
    check("each still carries a failing step — the error is not hidden",
          all(r.get("error_span_count") for r in runs))
    detail = p["seen"]["inspected"][CTX["C"]["ids"]["runs"][0]]
    check("inspect_run shows both the failure and the completion",
          "carrier_api" in str(detail) and "done" in str(detail).lower())

    # -- D ---------------------------------------------------------------
    print("\n=== D · work waiting on a person ===")
    p = S.probe(CTX["D"])
    waits = p["seen"]["waits"]
    check("three contracts are waiting", waits["returned"] == 3)
    holders = waits.get("by_holder") or {}
    check("the record says who each one waits on", bool(holders), )
    check("waiting is retrievable as present-tense state only",
          "trend" in (waits.get("note") or "").lower()
          or "yesterday" in (waits.get("note") or "").lower())
    snap = c.get("/home/snapshot?days=7&tz=UTC",
                 headers=S.auth(CTX["D"]["token"])).json()
    att = snap.get("attention") or {}
    check("personal attention counts only what waits on the reader",
          att.get("needs_you") == 2, )

    # -- E ---------------------------------------------------------------
    print("\n=== E · a plausible optimization, with evidence both ways ===")
    p = S.probe(CTX["E"])
    heavy = p["seen"]["inspected"][CTX["E"]["ids"]["heavy"][0]]
    light = p["seen"]["inspected"][CTX["E"]["ids"]["light"]]
    # FINDING (retrieval, not model): tool calls are NOT in inspect_run's
    # output. `events` carries lifecycle only and `failed_spans` carries
    # failures only, so a SUCCESSFUL repeated tool call is invisible to the
    # investigation. All it can see is that one run has more spans than
    # another, with no way to learn what they were. Recorded in
    # EVAL_HOME_FINDINGS.md; asserted here so the gap cannot close silently.
    check("repeated tool calls are NOT retrievable through inspect_run",
          "web_search" not in str(heavy))
    check("only the span count distinguishes a heavy run from a light one",
          heavy["span_count"] > light["span_count"])
    check("both completed — repetition is not failure",
          all(o == "completed" for o in outcomes(p["seen"]["runs"]["runs"])))
    check("no tool records what a search returned, so redundancy is unknowable",
          "result" not in str(light).lower())
    check("cost evidence is available to a permitted reader",
          bool(p["seen"].get("cost")))

    # -- F ---------------------------------------------------------------
    print("\n=== F · a supported positive change ===")
    p = S.probe(CTX["F"])
    mix = p["seen"]["mix"]
    check("both windows started the same number",
          mix["current"]["started"] == mix["previous"]["started"] == 9)
    check("completions rose 4 -> 8",
          mix["previous"]["completed"] == 4 and mix["current"]["completed"] == 8)
    check("the numbers come with calculation ids a claim must cite",
          bool(mix.get("calculation_ids", {}).get("delta_completed")))
    check("windows are equal length and non-overlapping",
          mix.get("windows_equal_length") is True)

    # -- G ---------------------------------------------------------------
    print("\n=== G · incomplete visibility ===")
    p = S.probe(CTX["G"])
    snap = c.get("/home/snapshot?days=7&tz=UTC",
                 headers=S.auth(CTX["G"]["token"])).json()
    comp = snap.get("completeness") or {}
    check("the period reports no usable comparison",
          (snap.get("period", {}).get("comparison") or {}).get("available") is False
          or comp.get("comparison_available") is False)
    mix = p["seen"]["mix"]
    check("the previous window is genuinely empty, not zero-by-measurement",
          mix["previous"]["started"] == 0)
    ctxa = p["seen"]["agent"]
    check("the quiet agent's last-seen time is retrievable",
          any(k for k in ctxa if "last" in k.lower()))
    check("silence is a gap in the record, not a recorded failure",
          not ctxa.get("error_span_count"))

    # -- H ---------------------------------------------------------------
    print("\n=== H · cost, coverage and permission ===")
    p = S.probe(CTX["H"])
    cost = p["seen"]["cost"]
    check("organization-wide spend is retrievable", "spend_usd" in str(cost))
    check("pricing coverage is reported as partial",
          (cost.get("unpriced_token_spans") or 0) > 0
          and (cost.get("coverage_ratio") or 1) < 1)
    check("unpriced spend is named as unknown, not treated as zero",
          "unknown cost, not zero" in (cost.get("note") or ""))
    owner_snap = c.get("/home/snapshot?days=7&tz=UTC",
                       headers=S.auth(CTX["H"]["token"])).json()
    check("the permitted reader sees a financial block",
          (owner_snap.get("financial") or {}).get("visible") is True)
    check("and it is labelled organization-wide",
          (owner_snap.get("financial") or {}).get("scope") == "organization_wide")

    restricted = CTX["H"]["restricted"]
    r_snap = c.get("/home/snapshot?days=7&tz=UTC",
                   headers=S.auth(restricted["token"])).json()
    check("the restricted reader gets no financial figures",
          not (r_snap.get("financial") or {}).get("visible"))
    check("and is told why, rather than shown an empty number",
          (r_snap.get("financial") or {}).get("unavailable_reason")
          == "seat_excludes_financial_surface")
    check("every financial field is absent, not zero",
          (r_snap.get("financial") or {}).get("spend_usd") is None)
    # Without this the gate proves nothing: a reader with no work in scope
    # cannot leak money either, and an empty page would pass.
    check("that reader DOES see work, so the gate is actually under test",
          ((r_snap.get("completeness") or {}).get("scope_state") == "populated"))
    r_probe = S.probe(CTX["H"], financial_visible=False)
    check("and cost_evidence is not even offered to that investigation",
          "cost" not in r_probe["seen"])

    # -- I ---------------------------------------------------------------
    print("\n=== I · a counterexample ===")
    p = S.probe(CTX["I"])
    mix = p["seen"]["mix"]
    check("the recent window has three abandoned",
          mix["current"]["abandoned"] == 3)
    check("so did the previous one — nothing is newly wrong",
          mix["previous"]["abandoned"] == 3)
    check("out of a comparable number started",
          mix["current"]["started"] == mix["previous"]["started"] == 10)
    steps = set()
    for rid in CTX["I"]["ids"]["recent_abandoned"]:
        body = str(p["seen"]["inspected"][rid])
        for name in ("card_network", "fraud_check", "ledger_post"):
            if name in body:
                steps.add(name)
    check("the three recent failures are at three different steps",
          len(steps) == 3)

    # -- the rubric itself ----------------------------------------------
    print("\n=== the rubric is complete and self-consistent ===")
    for spec in S.SCENARIOS:
        k = spec["key"]
        check(f"{k}: states what the records establish", bool(spec["establishes"]))
        check(f"{k}: states what they do not establish", bool(spec["not_established"]))
        check(f"{k}: names claims that would be false", bool(spec["wrong"]))
        check(f"{k}: names the retrieval paths that could find it", bool(spec["paths"]))
        check(f"{k}: a scenario with no acceptable discovery allows abstention",
              bool(spec["acceptable"]) or spec["abstention_ok"])

    # Scoring semantics live in test_home_eval_harness.py, which covers
    # cautious language, paraphrase, misleading keywords, detail-field claims
    # and the failed/incomplete/abstained distinction. What belongs HERE is the
    # measurement this file is for: was the evidence delivered under the budget
    # the product actually gives an investigation?
    print("\n=== production-budget discoverability, per scenario ===")
    verdicts = {}
    for spec in S.SCENARIOS:
        k = spec["key"]
        pr = S.probe(CTX[k], budget=S.PRODUCTION_BUDGET)
        verdicts[k] = pr
        b = pr["budget"]
        label = (f"{k}: required evidence delivered within the production "
                 f"budget ({b['tool_calls']}/{b['tool_call_limit']} calls, "
                 f"{b['rows_retrieved']}/{b['row_limit']} rows, "
                 f"{b['events_retrieved']}/{b['event_limit']} events)")
        if k == "E":
            # E is the known retrieval gap: every RETRIEVABLE requirement is
            # delivered, and the PATTERN (repeated successful tool calls) is
            # not retrievable at all — so the verdict is False, by way of
            # `unretrievable` rather than anything this run failed to fetch.
            # Recorded as finding 2 in EVAL_HOME_FINDINGS.md.
            failed = [c for c in pr["requirement_checks"]
                      if c["status"] == "not_delivered"
                      and c["requirement"]["kind"] != "unretrievable"]
            unretrievable = [c for c in pr["requirement_checks"]
                             if c["requirement"]["kind"] == "unretrievable"]
            check(f"{k}: every retrievable requirement IS delivered", not failed)
            check(f"{k}: but the repetition is unretrievable, so the verdict "
                  f"is not true (finding 2)",
                  pr["delivered_within_budget"] is False and unretrievable)
            check(f"{k}: and no tool exposes the repeated calls",
                  "web_search" not in str(pr["seen"].get("inspected")))
            continue
        check(label, pr["delivered_within_budget"] is True)
        check(f"{k}: nothing was refused for want of budget",
              not pr["refused_by_budget"])
        check(f"{k}: no tool errored", not pr["tool_errors"])

    check("the production probe uses the product's own ToolBudget, not a "
          "larger one",
          verdicts["B"]["budget"]["tool_call_limit"]
          == investigation_tools.ToolBudget().max_calls
          and verdicts["B"]["budget"]["row_limit"]
          == investigation_tools.ToolBudget().max_rows)

    # And the budget must genuinely be able to BITE, or "it fitted" means
    # nothing. Squeeze it until the evidence cannot get through, and check the
    # probe reports that honestly rather than reporting an empty result as an
    # absence in the records.
    print("\n=== the budget can actually prevent delivery ===")
    original_budget = S._budget
    S._budget = lambda kind: investigation_tools.ToolBudget(
        max_calls=2, max_rows=1, max_events=1)
    try:
        blocked = S.probe(CTX["I"], budget=S.PRODUCTION_BUDGET)
    finally:
        S._budget = original_budget
    check("a squeezed budget refuses retrievals", bool(blocked["refused_by_budget"]))
    check("and the scenario's required evidence does not arrive",
          not blocked["delivered_within_budget"])
    check("which is reported as missing, not as absent from the records",
          bool(blocked["missing_required"]))
    check("the refusal names the budget as the reason",
          all("budget" in (a.get("why") or "")
              for a in blocked["refused_by_budget"]))
    check("and the unsqueezed probe still delivers, so the difference is the "
          "budget and not the fixture",
          S.probe(CTX["I"], budget=S.PRODUCTION_BUDGET)["delivered_within_budget"])

print("\n" + ("FAILED: " + "; ".join(failures) if failures else "ALL PASS"))
raise SystemExit(1 if failures else 0)
