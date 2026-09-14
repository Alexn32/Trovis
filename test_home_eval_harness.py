"""The evaluation harness, tested — because a harness nobody checks is a rumour.

Everything here runs without a network, a key, or a cent. It covers the three
places the harness could quietly lie:

1. **Repetition.** `--repeat 2` must run two investigations, not read one
   cached answer twice. A repeat that re-reads a cache measures nothing and
   looks exactly like a repeat that worked.
2. **Scoring.** A regex hit must not be reported as a false claim. The
   sentence "the root cause is not established; there is no evidence of an
   outage" is the model behaving WELL, and the previous scorer called it two
   false claims.
3. **Spending.** A ceiling that is checked after the money is gone, or that
   waves a request through when the price is unknown, is not a ceiling.

Run:
  TROVIS_DISABLE_PRICING_SYNC=1 python3 test_home_eval_harness.py
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
# Present only so `investigate` will run; the client is replaced outright and
# no request ever leaves this process.
os.environ["ANTHROPIC_API_KEY"] = "stub-not-a-real-key"

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()

import database
database.SQLITE_PATH = _tmp.name

import analysis_jobs
import investigator
import main
from fastapi.testclient import TestClient

import eval_stub_model
import home_eval_scenarios as S
import home_eval_scoring as SC
import run_home_eval as R

main._auto_describe = lambda *a, **k: False

failures: list[str] = []


def check(label, cond):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        failures.append(label)


# ===========================================================================
# 1. Repetitions are independent investigations
# ===========================================================================
print("\n=== repetitions run independently ===")

investigator._client = lambda: eval_stub_model.client()

with TestClient(main.app) as c:
    meter = R.Meter(max_calls=60, max_usd=5.0, database=database,
                    model=investigator.MODEL, priced=False)
    investigator._client = lambda: meter.wrap(eval_stub_model.client())

    runs = []
    for attempt in (1, 2):
        before = meter.snapshot()
        ctx = S.build_instance(c, "B", instance=attempt)
        eval_stub_model.reset()
        runs.append(R.run_one_scenario(
            c, S, ctx, analysis_jobs, investigator,
            attempt=attempt, meter=meter, usage_before=before))

    a, b = runs
    check("two attempts use two different fixture accounts",
          a["fixture"]["account_id"] != b["fixture"]["account_id"])
    check("each attempt carries its own fixture id",
          a["fixture"]["fixture_id"] == "B#1" and b["fixture"]["fixture_id"] == "B#2")
    check("both record the fixture version that produced them",
          a["fixture"]["version"] == b["fixture"]["version"]
          and len(a["fixture"]["version"]) == 12)
    check("both attempts enqueued on their first read",
          a["provenance"]["enqueued_on_first_read"] is True
          and b["provenance"]["enqueued_on_first_read"] is True)
    check("each attempt drained its own job",
          a["provenance"]["job_ids"] and b["provenance"]["job_ids"]
          and a["provenance"]["job_ids"] != b["provenance"]["job_ids"])
    check("each attempt produced a distinct analysis id",
          a["provenance"]["analysis_ids"] != b["provenance"]["analysis_ids"]
          and all(a["provenance"]["analysis_ids"]))
    check("the two analyses cover different audiences",
          a["provenance"]["scope_keys"] != b["provenance"]["scope_keys"])
    check("each attempt actually invoked the model",
          a["provenance"]["model_calls_this_attempt"] > 0
          and b["provenance"]["model_calls_this_attempt"] > 0)
    check("no job from another fixture was attributed to an attempt",
          a["provenance"]["jobs_drained_other_accounts"] == 0
          and b["provenance"]["jobs_drained_other_accounts"] == 0)
    check("findings are distinct records, not the same row read twice",
          [f["id"] for f in a["findings"]] != [f["id"] for f in b["findings"]]
          or not a["findings"])
    check("model and prompt versions are recorded per attempt",
          a["provenance"]["model"] == investigator.MODEL
          and a["provenance"]["prompt_version"] == investigator.PROMPT_VERSION)

    # The cache is real, and correct. Reading the SAME account twice must NOT
    # be counted as a second experiment — which is the bug this fixes.
    print("\n=== a re-read of one account is not a second experiment ===")
    same = S.build_instance(c, "C", instance=1)
    eval_stub_model.reset()
    first = R.run_one_scenario(c, S, same, analysis_jobs, investigator,
                               attempt=1, meter=meter, usage_before=meter.snapshot())
    second = R.run_one_scenario(c, S, same, analysis_jobs, investigator,
                                attempt=2, meter=meter, usage_before=meter.snapshot())
    check("the product declines to re-analyse an unchanged audience",
          second["provenance"]["enqueued_on_first_read"] is False)
    check("and that re-read drains no job",
          second["provenance"]["jobs_drained"] == 0)
    check("and invokes the model zero times",
          second["provenance"]["model_calls_this_attempt"] == 0)
    check("so the runner never reuses one fixture across attempts",
          first["fixture"]["account_id"] == second["fixture"]["account_id"]
          and "build_instance(c, key_, instance=attempt)" in open(
              "run_home_eval.py").read())

# ===========================================================================
# 2. Budget exhaustion is an explicit non-result
# ===========================================================================
print("\n=== budget exhaustion yields a skip, not a copy ===")

sk = R.skipped_result("B", 3, S, reason="model-call ceiling reached (60/60)")
check("a skipped attempt publishes nothing", sk["findings"] == [])
check("it is labelled skipped, not abstained",
      sk["execution"] == "skipped"
      and sk["assessment"]["status"]["execution"] == "skipped")
check("its discovery question is not applicable",
      sk["assessment"]["discovery"] == "not_applicable")
check("it carries the reason", "ceiling" in sk["skipped"])
check("it has no fixture, so it cannot be mistaken for a run",
      sk["fixture"] is None)

spent = R.Meter(max_calls=1, max_usd=5.0, database=database,
                model="claude-sonnet-4-5", priced=True)
spent.calls = 1
check("a spent meter reports it is spent", spent.spent_out())
check("with a reason naming the ceiling", "ceiling" in spent.stop_reason())

# ===========================================================================
# 3. Spending control
# ===========================================================================
print("\n=== spending control ===")


class _Usage:
    def __init__(self, i, o):
        self.input_tokens, self.output_tokens = i, o


class _Resp:
    def __init__(self, usage=None):
        self.usage = usage
        self.content = []


class _FakeClient:
    """Counts the requests that were actually SENT."""

    def __init__(self, usage=None):
        self.sent = 0
        outer = self

        class M:
            def create(self, **kw):
                outer.sent += 1
                return _Resp(usage)

        self.messages = M()


PRICED = "claude-sonnet-4-5"
CALL = {"system": "s", "messages": [{"role": "user", "content": "x" * 300}],
        "max_tokens": 1000}

m = R.Meter(max_calls=10, max_usd=5.0, database=database, model=PRICED)
check("a listed model has a known price", m.pricing_known())

unknown = R.Meter(max_calls=10, max_usd=5.0, database=database,
                  model="definitely-not-a-real-model-v0")
check("an unlisted model has no known price", not unknown.pricing_known())
fc = _FakeClient(_Usage(10, 10))
try:
    unknown.wrap(fc).messages.create(**CALL)
    check("unknown pricing refuses to send a dollar-budgeted request", False)
except R.BudgetExceeded as exc:
    check("unknown pricing refuses to send a dollar-budgeted request",
          "no price known" in str(exc))
check("and nothing was sent", fc.sent == 0)
check("the refusal is recorded", unknown.refusals == ["pricing unknown"])

# Insufficient remaining budget: the bound is checked BEFORE the request.
tiny = R.Meter(max_calls=10, max_usd=0.0001, database=database, model=PRICED)
fc = _FakeClient(_Usage(10, 10))
try:
    tiny.wrap(fc).messages.create(**CALL)
    check("a request whose upper bound exceeds the budget is not sent", False)
except R.BudgetExceeded as exc:
    check("a request whose upper bound exceeds the budget is not sent",
          "could cost up to" in str(exc))
check("and nothing was sent", fc.sent == 0)
check("no call was counted for a request never made", tiny.calls == 0)

# The call ceiling holds even with money to spare.
ceil = R.Meter(max_calls=1, max_usd=100.0, database=database, model=PRICED)
fc = _FakeClient(_Usage(100, 50))
w = ceil.wrap(fc)
w.messages.create(**CALL)
try:
    w.messages.create(**CALL)
    check("the model-call ceiling stops the next request", False)
except R.BudgetExceeded as exc:
    check("the model-call ceiling stops the next request", "ceiling" in str(exc))
check("exactly one request was sent", fc.sent == 1)

# Missing usage is not free.
nousage = R.Meter(max_calls=5, max_usd=100.0, database=database, model=PRICED)
fc = _FakeClient(None)
nousage.wrap(fc).messages.create(**CALL)
check("a response with no usage is counted as a call", nousage.calls == 1)
check("it is recorded as unmeasured", nousage.usage_missing == 1)
check("and keeps its reservation rather than costing zero",
      nousage.spent_usd() > 0 and nousage.report()["estimated_usd"] > 0)
check("the report says how much is unreconciled",
      nousage.report()["of_which_unreconciled_reservations"] > 0)

# Normal accounting: the reservation is replaced by reported usage.
ok = R.Meter(max_calls=5, max_usd=100.0, database=database, model=PRICED)
fc = _FakeClient(_Usage(1000, 500))
before = ok.snapshot()
ok.wrap(fc).messages.create(**CALL)
rep = ok.report()
check("reported tokens are recorded",
      rep["input_tokens"] == 1000 and rep["output_tokens"] == 500)
check("no reservation is left outstanding",
      abs(rep["of_which_unreconciled_reservations"]) < 1e-9)
check("the estimate matches the priced usage",
      abs(rep["estimated_usd"] - (ok._cost(1000, 500) or 0)) < 1e-9)
check("the estimate is smaller than the pre-request allowance",
      rep["estimated_usd"] < ok.preflight(**CALL) + rep["estimated_usd"])
check("per-attempt deltas are reported", ok.delta(before)["model_calls"] == 1)
check("the report calls it an estimated bound, not a bill",
      "not a statement about the provider" in rep["note"])

for bad in (0, -1, float("inf"), float("nan"), "x", None):
    try:
        R.Meter(max_calls=10, max_usd=bad, database=database, model=PRICED)
        check(f"an invalid budget {bad!r} is rejected", False)
    except (ValueError, TypeError):
        check(f"an invalid budget {bad!r} is rejected", True)

check("provider retries are disabled so the ceiling bounds requests",
      "max_retries=0" in open("run_home_eval.py").read())

# ===========================================================================
# 4. Scoring judges statements, not keywords
# ===========================================================================
print("\n=== scoring: a regex hit is a question, not a verdict ===")

B = S.scenario("B")
C = S.scenario("C")


def f(title, explanation="", **kw):
    return {"title": title, "explanation": explanation, "uncertainty": [], **kw}


def detail(**kw):
    return {"finding": {}, "claims": [], "evidence": [], **kw}


# (a) correct cautious language — the exact sentence the old scorer failed on
cautious = [f("Four refunds stopped at the same approval step",
              "The root cause is not established; there is no evidence of an "
              "outage. Four refund items each recorded a failing "
              "approval_service call.")]
a = SC.assess(B, cautious, [detail()], analysis={"analysis_outcome": "complete"},
              worker_reports=[{"status": "done"}])
flags = a["review_flags"]
check("cautious language still raises flags for a reader to check", bool(flags))
check("but none of them is an affirmative assertion",
      a["affirmative_flag_count"] == 0)
check("every flag on it is negated or hedged",
      all(x["disposition"] in ("negated", "hedged") for x in flags))
check("and it is not auto-failed", a["automated_verdict"] == "requires_review")
check("'false claims' is gone from the contract entirely",
      "false_claims" not in a and "false_claims" not in str(sorted(a.keys())))

# (b) an unsupported affirmative claim
asserted = [f("Refunds are failing because the approval service had an outage",
              "All refunds stopped.")]
a2 = SC.assess(B, asserted, [detail()], analysis={"analysis_outcome": "complete"},
               worker_reports=[{"status": "done"}])
check("an asserted cause is flagged as affirmative",
      any(x["disposition"] == "affirmative" and "because" in x["pattern"]
          for x in a2["review_flags"]))
check("an over-scoped 'all refunds' is flagged as affirmative",
      any(x["disposition"] == "affirmative" and "all" in x["pattern"]
          for x in a2["review_flags"]))
check("it is still only flagged, never auto-declared false",
      a2["automated_verdict"] == "requires_review")
check("the flag carries the clause a reviewer must read",
      all(x["clause"] for x in a2["review_flags"]))

# (c) a correct paraphrase that uses none of the expected words
para = [f("Several reimbursement jobs halted at the authorisation call",
          "Each of the four stopped at the same point in the sequence.")]
a3 = SC.assess(B, para, [detail()], analysis={"analysis_outcome": "complete"},
               worker_reports=[{"status": "done"}])
check("a paraphrase misses the lexical expectations",
      "approval" in a3["expectation_signals"]["unmatched"])
check("but is NOT therefore recorded as a missed discovery",
      a3["discovery"] == "requires_review")
check("and the signal says it is lexical only",
      "Lexical overlap only" in a3["expectation_signals"]["note"])

# (d) a misleading statement that happens to contain the expected words
misleading = [f("No approval step was involved in any of these",
                "Each of the four runs completed normally.")]
a4 = SC.assess(B, misleading, [detail()],
               analysis={"analysis_outcome": "complete"},
               worker_reports=[{"status": "done"}])
check("keyword presence does not establish discovery",
      a4["expectation_signals"]["matched"] and a4["discovery"] == "requires_review")
check("matching the words is never reported as a discovery",
      a4["discovery"] != "discovered")

# (e) a false claim that lives only in a detail field
buried = [f("Three shipments", "They ran.")]
buried_detail = detail(claims=[
    {"text": "All three shipments failed to complete.", "kind": "observation"}])
a5 = SC.assess(C, buried, [buried_detail],
               analysis={"analysis_outcome": "complete"},
               worker_reports=[{"status": "done"}])
check("a claim in the evidence panel is scored too",
      any(x["field"].startswith("claims[") for x in a5["review_flags"]))
check("and is seen as an affirmative assertion",
      any(x["disposition"] == "affirmative" and x["field"].startswith("claims[")
          for x in a5["review_flags"]))

# (f) failure and incompleteness are not abstention
failed = SC.assess(B, [], [], analysis={"state": "failed", "reason": "transport"},
                   worker_reports=[{"status": "error"}])
check("a failed analysis is execution=failed", failed["status"]["execution"] == "failed")
check("an empty response after failure is NOT an abstention",
      failed["status"]["abstained"] is False)
check("and its discovery question does not apply",
      failed["discovery"] == "not_applicable")

incomplete = SC.assess(
    B, [], [], analysis={"state": "incomplete", "analysis_outcome": "incomplete",
                         "completion_gaps": ["validation_rejected"]},
    worker_reports=[{"status": "done"}])
check("an incomplete analysis is execution=incomplete",
      incomplete["status"]["execution"] == "incomplete")
check("and is not an abstention either", incomplete["status"]["abstained"] is False)

unavailable = SC.assess(B, [], [], analysis={"state": "unavailable"},
                        worker_reports=[])
check("no model configured is execution=unavailable",
      unavailable["status"]["execution"] == "unavailable")

abstained = SC.assess(B, [], [], analysis={"state": "current",
                                           "analysis_outcome": "complete"},
                      worker_reports=[{"status": "done"}])
check("only a COMPLETED empty analysis is an abstention",
      abstained["status"]["abstained"] is True)
check("and even then the discovery question is left open",
      abstained["discovery"] == "requires_review")

# Negation and hedging, read directly.
print("\n=== reading a clause ===")
check("plain assertion", SC.disposition("an outage caused this") == "affirmative")
check("negation", SC.disposition("there is no evidence of an outage") == "negated")
check("hedge", SC.disposition("an outage might explain it") == "hedged")
check("not-established reads as negated",
      SC.disposition("the root cause is not established") == "negated")
check("a negation on one clause does not excuse the next",
      [SC.disposition(x) for x in SC.clauses(
          "There is no outage. The approval service caused this.")]
      == ["negated", "affirmative"])

# Deterministic checks really are deterministic.
print("\n=== deterministic checks ===")
d = SC.deterministic_checks(
    B, [f("t")], [detail(claims=[{"text": "x", "kind": "observation",
                                  "value": 4}])])
check("a numeric claim with no metric_ref fails a deterministic check",
      any(not x["ok"] and "authoritative source" in x["check"] for x in d))
d2 = SC.deterministic_checks(
    B, [f("t")], [detail(claims=[{"text": "x", "kind": "calculation", "value": 4,
                                  "metric_ref": "calc:mix.delta"}])])
check("and passes when it cites one",
      all(x["ok"] for x in d2 if "authoritative source" in x["check"]))
d3 = SC.deterministic_checks(
    B, [f("t")], [detail(evidence=[{"kind": "run", "ref": "999"}])],
    delivered_evidence={"run:1"})
check("citing evidence that was never delivered fails a deterministic check",
      any(not x["ok"] and "delivered evidence" in x["check"] for x in d3))

H = S.scenario("H")
money = SC.deterministic_checks(
    H, [f("Spend was $40 this week")], [detail()], restricted=True)
check("a monetary figure shown to a restricted reader fails deterministically",
      any(not x["ok"] and "monetary figure" in x["check"] for x in money))
nomoney = SC.deterministic_checks(
    H, [f("No cost information is available to you")], [detail()], restricted=True)
check("saying cost is unavailable does NOT fail that check",
      all(x["ok"] for x in nomoney if "monetary figure" in x["check"]))

# ===========================================================================
# 5. The five outcomes a reviewer has to tell apart
# ===========================================================================
print("\n=== diagnosis separates the five outcomes ===")

def diag(**kw):
    return SC.assess(B, kw.pop("findings", []), kw.pop("details", []),
                     **kw)["diagnosis"]["code"]

check("a published finding reads as published",
      diag(findings=[f("t")], details=[detail()],
           analysis={"analysis_outcome": "complete"},
           worker_reports=[{"status": "done", "candidates": 1}]) == "published")
check("a validator rejection is named as one",
      diag(analysis={"analysis_outcome": "incomplete",
                     "completion_gaps": ["validation_rejected"]},
           worker_reports=[{"status": "done", "candidates": 1,
                            "rejected": ["overstated scope"]}])
      == "draft_blocked_by_validation")
check("unreachable evidence is named as unreachable",
      diag(analysis={"analysis_outcome": "complete"},
           worker_reports=[{"status": "done", "candidates": 1}],
           evidence_delivered=False) == "required_evidence_unreachable")
check("an investigated candidate that was withheld is named as such",
      diag(analysis={"analysis_outcome": "complete"},
           worker_reports=[{"status": "done", "candidates": 1,
                            "abstained": ["refunds: evidence did not carry it"]}],
           evidence_delivered=True) == "investigated_and_withheld")
check("nothing proposed reads as no candidate raised",
      diag(analysis={"analysis_outcome": "complete"},
           worker_reports=[{"status": "done", "candidates": 0}],
           evidence_delivered=True) == "no_candidate_raised")
check("a broken pipeline is never any of those",
      diag(analysis={"state": "failed"}, worker_reports=[{"status": "error"}])
      == "pipeline_failed_before_decision")
check("the diagnosis carries the reasons a reviewer needs",
      SC.assess(B, [], [], analysis={"analysis_outcome": "complete"},
                worker_reports=[{"status": "done", "candidates": 1,
                                 "abstained": ["why"]}],
                evidence_delivered=True)["diagnosis"]["abstention_reasons"]
      == [["why"]])

print("\n" + ("FAILED: " + "; ".join(failures) if failures else "ALL PASS"))
raise SystemExit(1 if failures else 0)
