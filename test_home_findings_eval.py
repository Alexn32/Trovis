"""The evaluation set: is a finding TRUE and USEFUL, not just well-formed?

This is a different question from `test_home_findings.py`. That file proves
the machinery works. This one is a repeatable rubric for the OUTPUT: a fixed
set of fixtures, each with claims that must appear, claims that must never
appear, and cases where the only right answer is to say nothing.

**No live model runs here.** Every case scores a candidate finding — a canned
one, standing in for what a model might produce — against the fixture's
record. What that measures is the GUARD: whether the validator and the
evidence requirements let a bad finding through. It does not and cannot tell
you the real model is insightful.

To measure the model itself you would run `investigator.investigate` against
these same fixtures with a real API key and score the output with
`score_case`. That is deliberately not wired into CI: it costs money, it is
non-deterministic, and a green tick from it would be read as a guarantee it
cannot give. `EVAL_CASES` is written so that run is a small script, not a
rewrite.

Metrics reported: support, relevance, specificity, actionability, false
positives, missed meaningful findings.

Run:
  OVERSEE_DISABLE_PRICING_SYNC=1 python3 test_home_findings_eval.py
"""
import json
import os
import re
import tempfile
import time

os.environ.update({
    "OVERSEE_DISABLE_PRICING_SYNC": "1",
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

import findings as findings_mod
import investigation_tools
import main
from fastapi.testclient import TestClient

main._auto_describe = lambda *a, **k: False

failures = []


def check(label, cond):
    print(("  PASS " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


NS = 10**9
NOW = time.time_ns()
_n = [0]


def auth(tok):
    return {"Authorization": f"Bearer {tok}"}


def span(service, off_s, attrs, name="message_received", status=0, msg=""):
    _n[0] += 1
    t = NOW - int(off_s) * NS
    return {
        "trace_id": f"t{_n[0]:028d}", "span_id": f"s{_n[0]:014d}",
        "parent_span_id": None, "service_name": service, "agent_id": "main",
        "span_name": name, "kind": 1,
        "start_time_unix": t, "end_time_unix": t + 10**6,
        "status_code": status, "status_message": msg,
        "attributes": attrs, "resource_attributes": {},
    }


def title_id(title):
    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute(f"SELECT id FROM loops WHERE title = {database.PH}", (title,))
        row = cur.fetchone()
        return int(row["id"]) if row else None


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def score_case(case, finding, *, snapshot, session, financial_visible):
    """Score one candidate finding against one fixture's rubric.

    `support` is the only one with teeth: it runs the real deterministic
    validator, so a finding that cannot be published scores zero on it
    regardless of how well it reads. The rest are text rubrics and are
    reported as such.
    """
    result = {
        "case": case["name"], "support": 0.0, "relevance": 0.0,
        "specificity": 0.0, "actionability": 0.0,
        "false_positive": False, "missed": False, "rejected_because": None,
    }

    if finding is None:
        # Abstention. Correct exactly when the case expects it.
        result["support"] = 1.0 if case["expect_abstain"] else 0.0
        result["missed"] = not case["expect_abstain"]
        result["relevance"] = 1.0 if case["expect_abstain"] else 0.0
        return result

    if case["expect_abstain"]:
        # Publishing anything here is a false positive by definition.
        result["false_positive"] = True

    try:
        validated = findings_mod.validate_finding(
            finding, snapshot=snapshot,
            evidence_index=session.evidence,
            calculations=session.calculations,
            financial_visible=financial_visible,
        )
        result["support"] = 1.0
    except findings_mod.FindingRejected as exc:
        result["rejected_because"] = exc.reasons
        # A rejected finding never reaches a reader, so a case that expected
        # abstention is satisfied by the rejection.
        if case["expect_abstain"]:
            result["false_positive"] = False
            result["support"] = 1.0
        return result

    text = " ".join([
        validated["title"], validated["explanation"],
        json.dumps(validated.get("claims") or []),
    ]).lower()

    required = case.get("must_claim") or []
    hit = sum(1 for pat in required if re.search(pat, text, re.I))
    result["relevance"] = hit / len(required) if required else 1.0
    result["missed"] = bool(required) and hit < len(required)

    forbidden = case.get("must_not_claim") or []
    if any(re.search(pat, text, re.I) for pat in forbidden):
        result["false_positive"] = True

    # Specific = names a real entity rather than gesturing at the fleet.
    result["specificity"] = 1.0 if validated.get("entities") else 0.0
    # Actionable = proposes something a person does, or honestly says nothing.
    step = (validated.get("next_step") or {}).get("kind")
    result["actionability"] = 1.0 if step in findings_mod.NEXT_STEP_KINDS else 0.0
    return result


with TestClient(main.app) as c:
    acct = c.post("/auth/signup", json={
        "email": "ceo@eval.test", "password": "correct horse battery",
        "name": "Cleo", "account_type": "business", "org_name": "Eval",
    }).json()
    ACCT, TOKEN = acct["org"]["id"], acct["token"]

    job = c.post("/workflows", headers=auth(TOKEN), json={
        "name": "Refunds", "description": "d",
        "steps": [{"step_type": "agent", "label": "run"}]}).json()

    # --- fixture A: a genuine shared blocker across several runs ---------
    for i in range(3):
        ext = f"blocked{i}"
        database.ingest_spans_with_loops([
            span("refunds-agent", 5000 - i, {
                "trovis.loop.title": f"Blocked refund {i}",
                "trovis.loop.external_id": ext}),
            span("refunds-agent", 4900 - i, {
                "trovis.loop.external_id": ext, "trovis.tool.name": "approval"},
                name="tool_call", status=2, msg="approval service timed out"),
        ], account_id=ACCT)
    BLOCKED = [title_id(f"Blocked refund {i}") for i in range(3)]

    # --- fixture B: recovered tool errors (friction, not failure) --------
    database.ingest_spans_with_loops([
        span("refunds-agent", 4000, {
            "trovis.loop.title": "Recovered refund", "trovis.loop.external_id": "rec1"}),
        span("refunds-agent", 3950, {
            "trovis.loop.external_id": "rec1", "trovis.tool.name": "approval"},
            name="tool_call", status=2, msg="approval service timed out"),
        span("refunds-agent", 3900, {
            "trovis.loop.external_id": "rec1", "trovis.loop.close": "done"},
            name="agent_run_complete"),
    ], account_id=ACCT)
    RECOVERED = title_id("Recovered refund")

    # --- fixture C: necessary repetition ---------------------------------
    lookups = [span("search-agent", 3000, {
        "trovis.loop.title": "Research report", "trovis.loop.external_id": "rep1"})]
    for i in range(9):
        lookups.append(span("search-agent", 2900 - i, {
            "trovis.loop.external_id": "rep1", "trovis.tool.name": "web_search"},
            name="tool_call"))
    lookups.append(span("search-agent", 2800, {
        "trovis.loop.external_id": "rep1", "trovis.loop.close": "done"},
        name="agent_run_complete"))
    database.ingest_spans_with_loops(lookups, account_id=ACCT)
    REPEATED = title_id("Research report")

    # --- fixture D: a completed run, for positive findings ---------------
    database.ingest_spans_with_loops([
        span("refunds-agent", 2000, {
            "trovis.loop.title": "Clean refund", "trovis.loop.external_id": "clean1"}),
        span("refunds-agent", 1900, {
            "trovis.loop.external_id": "clean1", "trovis.loop.close": "done"},
            name="agent_run_complete"),
    ], account_id=ACCT)
    CLEAN = title_id("Clean refund")

    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute(
            f"UPDATE loops SET workflow_id = {database.PH} WHERE title LIKE '%refund%'",
            (job["id"],))
    for lid in BLOCKED:
        database.abandon_loop(lid, ACCT)

    # A session that has retrieved everything the cases may cite.
    session = investigation_tools.InvestigationSession(
        account_id=ACCT, only_user_ids=None, financial_visible=True,
        budget=investigation_tools.ToolBudget(max_calls=40, max_rows=4000),
    )
    session.run("list_comparable_runs", {"job_id": job["id"], "limit": 50})
    session.run("list_comparable_runs", {"agent": "search-agent", "limit": 50})
    for rid in (*BLOCKED, RECOVERED, REPEATED, CLEAN):
        session.run("inspect_run", {"run_id": rid})
    mix = session.run("compare_outcome_mix", {"days": 7, "job_id": job["id"]})
    session.run("cost_evidence", {"days": 7})

    SNAPSHOT = c.get("/home/snapshot?days=7&tz=UTC", headers=auth(TOKEN)).json()

    def ev(kind, ref):
        return {"kind": kind, "ref": str(ref)}

    # -----------------------------------------------------------------
    # The evaluation set
    # -----------------------------------------------------------------
    EVAL_CASES = [
        {
            "name": "shared blocker across runs",
            "expect_abstain": False,
            "must_claim": [r"approval", r"three|3"],
            "must_not_claim": [r"\bcaused by\b.*\boutage\b", r"\d+%"],
            "candidate": {
                "category": "attention", "claim_kind": "observation",
                "confidence": "supported",
                "title": "Three refunds stopped at the same approval step",
                "explanation": (
                    "Three refund runs each failed at the approval call and "
                    "were closed without finishing."),
                "claims": [{
                    "text": "Three runs of this job stopped at the approval call.",
                    "kind": "observation",
                    "evidence": [f"run:{r}" for r in BLOCKED]}],
                "entities": [{"kind": "run", "id": BLOCKED[0]},
                             {"kind": "job", "id": job["id"]}],
                "evidence": [ev("run", r) for r in BLOCKED],
                "uncertainty": ["whether the approval service has recovered"],
                "next_step": {"kind": "review_runs", "text": "Open the three refunds."},
            },
        },
        {
            "name": "recovered tool error is friction, not failed work",
            "expect_abstain": True,
            "must_not_claim": [r"failed to complete", r"work was lost"],
            "candidate": {
                "category": "attention", "claim_kind": "observation",
                "confidence": "supported",
                "title": "A refund failed",
                "explanation": "The refund run hit an approval error and failed.",
                "claims": [{"text": "This run failed and did not complete.",
                            "kind": "observation",
                            "evidence": [f"run:{RECOVERED}"]}],
                "entities": [{"kind": "run", "id": RECOVERED}],
                "evidence": [ev("run", RECOVERED)],
                "uncertainty": [],
                "next_step": {"kind": "review_runs"},
            },
        },
        {
            "name": "abandonment reframed as a win",
            "expect_abstain": True,
            "must_not_claim": [r"improved", r"completed"],
            "candidate": {
                "category": "positive_change", "claim_kind": "observation",
                "confidence": "supported",
                "title": "Refund throughput improved",
                "explanation": "Three refunds finished this week.",
                "claims": [{"text": "Three refunds completed.",
                            "kind": "observation",
                            "evidence": [f"run:{r}" for r in BLOCKED]}],
                "entities": [{"kind": "run", "id": BLOCKED[0]}],
                "evidence": [ev("run", r) for r in BLOCKED],
                "uncertainty": [], "next_step": {"kind": "no_action"},
            },
        },
        {
            "name": "necessary repetition is not waste",
            "expect_abstain": True,
            "must_not_claim": [r"wasteful", r"redundant"],
            "candidate": {
                "category": "opportunity", "claim_kind": "hypothesis",
                "confidence": "supported",
                "title": "Search agent is wasting calls",
                "explanation": "It made nine lookups for one report, which is redundant.",
                "claims": [{"text": "Nine repeated lookups are wasteful.",
                            "kind": "observation",
                            "evidence": [f"run:{REPEATED}"]}],
                "entities": [{"kind": "run", "id": REPEATED}],
                "evidence": [ev("run", REPEATED)],
                "uncertainty": [], "next_step": {"kind": "review_agent"},
            },
        },
        {
            "name": "positive change supported by a completed run",
            "expect_abstain": False,
            "must_claim": [r"refund"],
            "must_not_claim": [r"\d+%"],
            "candidate": {
                "category": "positive_change", "claim_kind": "observation",
                "confidence": "supported",
                "title": "A refund ran clean end to end",
                "explanation": (
                    "The most recent refund run finished without a failing step, "
                    "unlike the three before it."),
                "claims": [{"text": "This refund run completed with no failing step.",
                            "kind": "observation", "evidence": [f"run:{CLEAN}"]}],
                "entities": [{"kind": "run", "id": CLEAN}],
                "evidence": [ev("run", CLEAN)],
                "uncertainty": ["one run is a small sample"],
                "next_step": {"kind": "no_action"},
            },
        },
        {
            "name": "credible optimization tied to a mechanism",
            "expect_abstain": False,
            "must_claim": [r"approval"],
            "must_not_claim": [r"save \$", r"cheaper model"],
            "candidate": {
                "category": "opportunity", "claim_kind": "hypothesis",
                "confidence": "qualified",
                "title": "Refunds could retry the approval call before giving up",
                "explanation": (
                    "One run recovered from the same approval error the three "
                    "abandoned runs hit, so a retry may be the difference."),
                "claims": [
                    {"text": "One run recovered from the same error and finished.",
                     "kind": "observation", "evidence": [f"run:{RECOVERED}"]},
                    {"text": "Three runs with that error were abandoned.",
                     "kind": "observation",
                     "evidence": [f"run:{BLOCKED[0]}", f"run:{BLOCKED[1]}"]},
                ],
                "entities": [{"kind": "job", "id": job["id"]}],
                "evidence": [ev("run", RECOVERED), ev("run", BLOCKED[0]),
                             ev("run", BLOCKED[1])],
                "uncertainty": ["why one recovered and three did not"],
                "next_step": {"kind": "review_agent",
                              "text": "Check whether the approval step retries."},
            },
        },
        {
            "name": "higher spend explained by volume",
            "expect_abstain": True,
            "must_not_claim": [r"less efficient", r"efficiency"],
            "candidate": {
                "category": "attention", "claim_kind": "hypothesis",
                "confidence": "supported",
                "title": "Refunds are getting less efficient",
                "explanation": "Spend rose this week, so the agent is less efficient.",
                "claims": [{"text": "Cost per refund is rising.",
                            "kind": "calculation", "value": 1.5,
                            "evidence": [f"run:{CLEAN}"]}],
                "entities": [{"kind": "job", "id": job["id"]}],
                "evidence": [ev("run", CLEAN)],
                "uncertainty": [], "next_step": {"kind": "investigate_cost"},
            },
        },
        {
            "name": "fabricated evidence id",
            "expect_abstain": True,
            "candidate": {
                "category": "attention", "claim_kind": "observation",
                "confidence": "supported",
                "title": "Four runs failed at the same step",
                "explanation": "Four refund runs stopped at the approval call.",
                "claims": [{"text": "Four runs stopped there.", "kind": "observation",
                            "evidence": ["run:987654"]}],
                "entities": [{"kind": "run", "id": 987654}],
                "evidence": [ev("run", 987654)],
                "uncertainty": [], "next_step": {"kind": "review_runs"},
            },
        },
        {
            "name": "quiet account, nothing to say",
            "expect_abstain": True,
            "candidate": None,
        },
    ]

    print("\n=== evaluation set (canned candidates, no live model) ===")
    results = [
        score_case(case, case["candidate"], snapshot=SNAPSHOT,
                   session=session, financial_visible=True)
        for case in EVAL_CASES
    ]

    for case, r in zip(EVAL_CASES, results):
        verdict = "abstained/blocked" if r["rejected_because"] or case["candidate"] is None \
            else "published"
        detail = ""
        if r["rejected_because"]:
            detail = f" — {r['rejected_because'][0][:70]}"
        ok = (
            (r["support"] == 1.0)
            and not r["false_positive"]
            and not (r["missed"] and not case["expect_abstain"])
        )
        check(f"{case['name']}: {verdict}{detail}", ok)

    n = len(results)
    summary = {
        "cases": n,
        "support": round(sum(r["support"] for r in results) / n, 2),
        "relevance": round(sum(r["relevance"] for r in results) / n, 2),
        "specificity": round(
            sum(r["specificity"] for r in results if not r["rejected_because"])
            / max(1, sum(1 for r in results if not r["rejected_because"])), 2),
        "actionability": round(
            sum(r["actionability"] for r in results if not r["rejected_because"])
            / max(1, sum(1 for r in results if not r["rejected_because"])), 2),
        "false_positives": sum(1 for r in results if r["false_positive"]),
        "missed_meaningful": sum(
            1 for c, r in zip(EVAL_CASES, results)
            if not c["expect_abstain"] and r["missed"]),
    }
    print("\n  metrics: " + json.dumps(summary))
    check("no false positives across the evaluation set",
          summary["false_positives"] == 0)
    check("no meaningful finding was missed",
          summary["missed_meaningful"] == 0)
    check("every published case is fully supported", summary["support"] == 1.0)

    print(
        "\n  NOTE: these scores measure the GUARD (validation + evidence "
        "requirements)\n  against canned candidates. No live model ran. They "
        "are not evidence that\n  the real model produces insightful findings."
    )

print("\n" + ("FAILED: " + "; ".join(failures) if failures else "ALL PASS"))
raise SystemExit(1 if failures else 0)
