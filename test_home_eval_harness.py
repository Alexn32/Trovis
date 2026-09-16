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
import json
import os
import sys
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
import investigation_tools
import home_eval_scenarios as S
import home_eval_scoring as SC
import run_home_eval as R

main._auto_describe = lambda *a, **k: False

# The pristine class, captured before any recorder patches it. Used to prove
# that an UNINSTRUMENTED session reports its contents as unverifiable rather
# than letting a content-dependent requirement pass on keys alone.
PRISTINE_SESSION = investigation_tools.InvestigationSession

failures: list[str] = []

# ONE client for the whole file. `main.app`'s lifespan starts a session manager
# that refuses a second run, so every section shares this one.
CLIENT = TestClient(main.app)
CLIENT.__enter__()


def check(label, cond):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        failures.append(label)


# ===========================================================================
# 1. Repetitions are independent investigations
# ===========================================================================
print("\n=== repetitions run independently ===")

investigator._client = lambda: eval_stub_model.client()

if True:
    c = CLIENT
    meter = R.Meter(max_calls=60, max_usd=5.0, database=database,
                    model=investigator.MODEL, priced=False)
    investigator._client = lambda: meter.wrap(eval_stub_model.client())
    recorder = R.DeliveryRecorder(investigation_tools)
    recorder.install()
    ledger = R.JobLedger(analysis_jobs, meter=meter, recorder=recorder,
                         database=database)
    ledger.install()

    runs = []
    for attempt in (1, 2):
        before = meter.snapshot()
        ctx = S.build_instance(c, "B", instance=attempt)
        eval_stub_model.reset()
        runs.append(R.run_one_scenario(
            c, S, ctx, analysis_jobs, investigator,
            attempt=attempt, meter=meter, usage_before=before,
            database=database, recorder=recorder, ledger=ledger))

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
                               attempt=1, meter=meter, usage_before=meter.snapshot(),
                               database=database, recorder=recorder, ledger=ledger)
    second = R.run_one_scenario(c, S, same, analysis_jobs, investigator,
                                attempt=2, meter=meter, usage_before=meter.snapshot(),
                                database=database, recorder=recorder, ledger=ledger)
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
    ledger.uninstall()

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
    """A usage block. Fields are omitted, not zeroed, when not passed."""

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class _Resp:
    def __init__(self, usage=None):
        self.usage = usage
        self.content = []


class _Count:
    def __init__(self, n):
        self.input_tokens = n


class _FakeClient:
    """Counts the requests actually SENT, and answers count_tokens."""

    def __init__(self, usage=None, *, counts=1000, raises=None,
                 can_count=True, count_raises=False):
        self.sent = 0
        self.counted = 0
        outer = self

        class M:
            def create(self, **kw):
                outer.sent += 1
                if raises:
                    raise raises
                return _Resp(usage)

            def count_tokens(self, **kw):
                outer.counted += 1
                if count_raises:
                    raise RuntimeError("counting is down")
                return _Count(counts)

        m = M()
        if not can_count:
            delattr(type(m), "count_tokens") if False else None

            class NoCount:
                def create(self, **kw):
                    outer.sent += 1
                    return _Resp(usage)

            m = NoCount()
        self.messages = m


PRICED = "claude-sonnet-4-5"
CALL = {"system": "s", "messages": [{"role": "user", "content": "x" * 300}],
        "max_tokens": 1000}


def meter(**kw):
    kw.setdefault("max_calls", 10)
    kw.setdefault("max_usd", 100.0)
    return R.Meter(database=database, model=kw.pop("model", PRICED), **kw)


m = meter()
check("a listed model has a known price", m.pricing_known())

unknown = meter(model="definitely-not-a-real-model-v0")
check("an unlisted model has no known price", not unknown.pricing_known())
fc = _FakeClient(_Usage(input_tokens=10, output_tokens=10))
try:
    unknown.wrap(fc).messages.create(**CALL)
    check("unknown pricing refuses to send a dollar-budgeted request", False)
except R.BudgetExceeded as exc:
    check("unknown pricing refuses to send a dollar-budgeted request",
          "no price known" in str(exc))
check("and nothing was sent", fc.sent == 0)

# --- the input bound comes from the provider, not from len()/3 -------------
counted = meter()
fc = _FakeClient(_Usage(input_tokens=10, output_tokens=10), counts=50_000)
counted.wrap(fc).messages.create(**CALL)
check("the provider is asked to count the input", fc.counted == 1)
check("the count is recorded", counted.report()["token_count_requests"] == 1
      and counted.report()["counted_input_tokens"] == 50_000)

# 300 characters of input that really costs 50k tokens: the old
# len(serialized)/3 + 1000 heuristic would have bounded it at ~1.3k tokens,
# under-counting by ~38x. A bound that can be beaten is not a bound.
heuristic_tokens = len(json.dumps(
    {"system": CALL["system"], "messages": CALL["messages"], "tools": None},
    default=str)) / 3 + 1000
tight = meter(max_usd=0.05)
fc = _FakeClient(_Usage(input_tokens=10, output_tokens=10), counts=50_000)
try:
    tight.wrap(fc).messages.create(**CALL)
    check("a request the old heuristic underestimated is now refused", False)
except R.BudgetExceeded as exc:
    check("a request the old heuristic underestimated is now refused",
          "could cost up to" in str(exc))
check("  (the heuristic would have counted ~%d tokens, the provider says 50000)"
      % heuristic_tokens, heuristic_tokens < 50_000)
check("and nothing was sent", fc.sent == 0)

# --- a request that cannot be bounded is refused, not guessed --------------
nocount = meter()
fc = _FakeClient(_Usage(input_tokens=1, output_tokens=1), can_count=False)
try:
    nocount.wrap(fc).messages.create(**CALL)
    check("a client that cannot count tokens is refused", False)
except R.BudgetExceeded as exc:
    check("a client that cannot count tokens is refused",
          "cannot count tokens" in str(exc))
check("and nothing was sent", fc.sent == 0)

failing = meter()
fc = _FakeClient(_Usage(input_tokens=1, output_tokens=1), count_raises=True)
try:
    failing.wrap(fc).messages.create(**CALL)
    check("a failed token count refuses the request", False)
except R.BudgetExceeded as exc:
    check("a failed token count refuses the request",
          "token counting failed" in str(exc))
check("and nothing was sent", fc.sent == 0)

nomax = meter()
fc = _FakeClient(_Usage(input_tokens=1, output_tokens=1))
try:
    nomax.wrap(fc).messages.create(system="s", messages=[], max_tokens=0)
    check("a request with no output ceiling is refused", False)
except R.BudgetExceeded as exc:
    check("a request with no output ceiling is refused", "max_tokens" in str(exc))

# --- insufficient remaining allowance --------------------------------------
tiny = meter(max_usd=0.0001)
fc = _FakeClient(_Usage(input_tokens=10, output_tokens=10))
try:
    tiny.wrap(fc).messages.create(**CALL)
    check("a request whose upper bound exceeds the budget is not sent", False)
except R.BudgetExceeded as exc:
    check("a request whose upper bound exceeds the budget is not sent",
          "could cost up to" in str(exc))
check("and nothing was sent", fc.sent == 0)
check("no call was counted for a request never made", tiny.calls == 0)

# --- the call ceiling ------------------------------------------------------
ceil = meter(max_calls=1)
fc = _FakeClient(_Usage(input_tokens=100, output_tokens=50))
w = ceil.wrap(fc)
w.messages.create(**CALL)
try:
    w.messages.create(**CALL)
    check("the model-call ceiling stops the next request", False)
except R.BudgetExceeded as exc:
    check("the model-call ceiling stops the next request", "ceiling" in str(exc))
check("exactly one request was sent", fc.sent == 1)

# --- partial, absent and invalid usage all keep the reservation ------------
print("\n  -- usage shapes --")
for label, usage in [
    ("only input reported", _Usage(input_tokens=100)),
    ("only output reported", _Usage(output_tokens=100)),
    ("no usage block at all", None),
    ("usage present but empty", _Usage()),
    ("invalid usage values", _Usage(input_tokens="oops", output_tokens=None)),
    ("negative usage", _Usage(input_tokens=-5, output_tokens=10)),
]:
    mm = meter()
    fc = _FakeClient(usage)
    reservation = None
    try:
        w = mm.wrap(fc)
        reservation = mm.preflight(fc, **CALL)
        mm.counted_input_tokens -= 1000  # preflight above was a dry run
        mm.count_requests -= 1
        w.messages.create(**CALL)
    except Exception as exc:
        check(f"{label}: handled without raising", False)
        continue
    rep = mm.report()
    ok = (rep["responses_without_usage"] == 1
          and rep["of_which_unreconciled_reservations"] > 0
          and rep["of_which_measured"] == 0
          and abs(rep["estimated_usd"] - reservation) < 1e-9)
    check(f"{label}: reservation kept, not turned into zero", ok)
    check(f"{label}: recorded as unmeasured",
          rep["partial_usage_responses"] and rep["calls_with_measured_usage"] == 0)

# The exact reviewer reproduction.
repro = meter()
fc = _FakeClient(_Usage(input_tokens=100))
res = repro.preflight(fc, **CALL)
repro.count_requests -= 1
repro.counted_input_tokens -= 1000
repro.wrap(fc).messages.create(**CALL)
rep = repro.report()
check("reviewer case: input_tokens=100 with output missing keeps the "
      f"reservation (${res:.6f}), not ${0.0001:.4f}",
      abs(rep["estimated_usd"] - res) < 1e-9 and rep["estimated_usd"] > 0.001)
check("reviewer case: usage_missing is no longer zero",
      rep["responses_without_usage"] == 1)

# --- an exception after the reservation ------------------------------------
boom = meter()
fc = _FakeClient(_Usage(input_tokens=1, output_tokens=1),
                 raises=RuntimeError("connection reset"))
try:
    boom.wrap(fc).messages.create(**CALL)
    check("a failed request propagates", False)
except RuntimeError:
    check("a failed request propagates", True)
check("the request was actually attempted", fc.sent == 1)
check("a failed call is still counted against the ceiling", boom.calls == 1)
check("and keeps its reservation — a failed request is not free",
      boom.spent_usd() > 0)
check("and is reported as a failed call", boom.report()["failed_calls"] == 1)

# --- fully reported usage reconciles ---------------------------------------
ok_m = meter()
fc = _FakeClient(_Usage(input_tokens=1000, output_tokens=500))
before = ok_m.snapshot()
ok_m.wrap(fc).messages.create(**CALL)
rep = ok_m.report()
check("reported tokens are recorded",
      rep["input_tokens"] == 1000 and rep["output_tokens"] == 500)
check("no reservation is left outstanding",
      abs(rep["of_which_unreconciled_reservations"]) < 1e-9)
check("the measured figure matches the priced usage",
      abs(rep["of_which_measured"] - (ok_m._cost(1000, 500) or 0)) < 1e-9)
check("the call is counted as measured", rep["calls_with_measured_usage"] == 1)
check("per-attempt deltas are reported", ok_m.delta(before)["model_calls"] == 1)
check("the report calls it an estimated-cost limit, not a bill",
      "not a guarantee about the provider" in rep["note"])

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
                                  "value": 4}])],
    delivery={"available": True, "keys": []})
check("a numeric claim with no metric_ref fails a deterministic check",
      any(x["status"] == "fail" and "authoritative source" in x["check"]
          for x in d))
d2 = SC.deterministic_checks(
    B, [f("t")], [detail(claims=[{"text": "x", "kind": "calculation", "value": 4,
                                  "metric_ref": "calc:mix.delta"}])],
    delivery={"available": True, "keys": []})
check("and passes when it cites one",
      all(x["status"] == "pass" for x in d2
          if "authoritative source" in x["check"]))
d3 = SC.deterministic_checks(
    B, [f("t")], [detail(evidence=[{"kind": "run", "ref": "999"}])],
    delivery={"available": True, "keys": ["run:1"]})
check("citing evidence this investigation never delivered fails",
      any(x["status"] == "fail" and "delivered" in x["check"] for x in d3))

H = S.scenario("H")
money = SC.deterministic_checks(
    H, [f("Spend was $40 this week")], [detail()], restricted=True,
    delivery={"available": True, "keys": []})
check("a monetary figure shown to a restricted reader fails deterministically",
      any(x["status"] == "fail" and "monetary figure" in x["check"] for x in money))
nomoney = SC.deterministic_checks(
    H, [f("No cost information is available to you")], [detail()],
    restricted=True, delivery={"available": True, "keys": []})
check("saying cost is unavailable does NOT fail that check",
      all(x["status"] == "pass" for x in nomoney
          if "monetary figure" in x["check"]))

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
check("evidence this run did not retrieve is named as not delivered",
      diag(analysis={"analysis_outcome": "complete"},
           worker_reports=[{"status": "done", "candidates": 1}],
           evidence_delivered=False) == "required_evidence_not_delivered")
check("and evidence no tool exposes is named as unretrievable",
      diag(analysis={"analysis_outcome": "complete"},
           worker_reports=[{"status": "done", "candidates": 1}],
           evidence_delivered=False,
           evidence_unretrievable=True) == "required_evidence_unretrievable")
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

# ===========================================================================
# 6. Evidence delivery is verified independently of publication
# ===========================================================================
print("\n=== delivery verification is independent of publication ===")

cited = detail(evidence=[{"kind": "run", "ref": "41"}])

# (a) a citation the publication carries but the investigation never delivered
a6 = SC.assess(B, [f("t")], [cited],
               analysis={"analysis_outcome": "complete"},
               worker_reports=[{"status": "done", "candidates": 1,
                                "publication": {"records": [
                                    {"evidence": [{"kind": "run", "ref": "41"}]}]}}],
               delivery={"available": True, "keys": ["run:7"]})
dc = a6["deterministic"]
check("a citation absent from ACTUAL delivery fails verification",
      any(x["status"] == "fail" and "delivered" in x["check"] for x in dc["checks"]))
check("it is a deterministic failure, so the scenario fails",
      a6["automated_verdict"] == "fail")
check("the publication agreeing with itself does not rescue it",
      dc["failures"] and "run:41" in dc["failures"][0]["detail"])

# (b) a successful independent probe is not evidence of delivery
a6b = SC.assess(B, [f("t")], [cited],
                analysis={"analysis_outcome": "complete"},
                worker_reports=[{"status": "done", "candidates": 1}],
                evidence_delivered=True)  # the PROBE succeeded
check("a successful probe leaves delivery verification unavailable",
      any(x["status"] == "unavailable" and "delivered" in x["check"]
          for x in a6b["deterministic"]["checks"]))
check("and is never counted as a passed check",
      a6b["deterministic"]["passed"] < len(a6b["deterministic"]["checks"]))
check("the probe result stays in its own field",
      a6b["diagnosis"]["required_evidence_delivered"] is True)

# (c) missing instrumentation reports unavailable, with a reason
a6c = SC.assess(B, [f("t")], [cited],
                analysis={"analysis_outcome": "complete"},
                worker_reports=[{"status": "done", "candidates": 1}],
                delivery=None)
un = [x for x in a6c["deterministic"]["checks"] if x["status"] == "unavailable"]
check("no instrumentation reports unavailable", bool(un))
check("with a stated reason", un and "instrumentation" in un[0]["detail"])
check("and no deterministic failure is invented",
      not a6c["deterministic"]["failures"])

# (d) an OBSERVED EMPTY ledger is not the same as unavailable
a6d = SC.assess(B, [f("t")], [cited],
                analysis={"analysis_outcome": "complete"},
                worker_reports=[{"status": "done", "candidates": 1}],
                delivery={"available": True, "keys": [], "observed_empty": True})
check("an observed-empty ledger is a real observation, and fails a citation",
      any(x["status"] == "fail" and "delivered" in x["check"]
          for x in a6d["deterministic"]["checks"]))
check("and says the ledger was observed empty",
      any("observed EMPTY" in (x["detail"] or "")
          for x in a6d["deterministic"]["checks"]))
check("empty and unavailable are distinguishable",
      a6d["deterministic"]["failures"] and not a6c["deterministic"]["failures"])

# (e) properly delivered evidence passes
a6e = SC.assess(B, [f("t")], [cited],
                analysis={"analysis_outcome": "complete"},
                worker_reports=[{"status": "done", "candidates": 1}],
                delivery={"available": True, "keys": ["run:41", "run:7"]})
check("evidence this investigation really delivered passes",
      any(x["status"] == "pass" and "delivered" in x["check"]
          for x in a6e["deterministic"]["checks"]))
check("and the scenario is not auto-failed",
      a6e["automated_verdict"] == "requires_review")

# (f) E's delivered rows must not imply its pattern was delivered
espec = S.scenario("E")
check("scenario E declares its pattern unretrievable",
      espec.get("pattern_retrievable") is False and espec.get("pattern_note"))
check("and says so in words a report can carry",
      "does NOT mean the repetition" in espec["pattern_note"])

# ===========================================================================
# 7. Usage reconciles, and jobs are attributed authoritatively
# ===========================================================================
print("\n=== usage reconciles across attempts and readers ===")

if True:
    c = CLIENT
    m2 = R.Meter(max_calls=200, max_usd=5.0, database=database,
                 model=investigator.MODEL, priced=False)
    investigator._client = lambda: m2.wrap(eval_stub_model.client())
    rec2 = R.DeliveryRecorder(investigation_tools)
    rec2.install()
    led2 = R.JobLedger(analysis_jobs, meter=m2, recorder=rec2, database=database)
    led2.install()
    got = []
    for scen in ("A", "H"):
        for attempt in (1, 2):
            before = m2.snapshot()
            ctx = S.build_instance(c, scen, instance=100 + attempt)
            eval_stub_model.reset()
            got.append(R.run_one_scenario(
                c, S, ctx, analysis_jobs, investigator, attempt=attempt,
                meter=m2, usage_before=before,
                database=database, recorder=rec2, ledger=led2))
    led2.uninstall()
    rec2.uninstall()

    total = sum(r["usage_attempt_total"]["model_calls"] for r in got)
    check(f"A,H x2: global calls ({m2.calls}) equal the sum of attempt "
          f"totals ({total})", m2.calls == total)
    h_runs = [r for r in got if r["key"] == "H"]
    check("H records a restricted-reader subtotal",
          all((r.get("restricted_reader") or {}).get("usage") is not None
              for r in h_runs))
    check("the restricted reader actually spent calls",
          all(r["restricted_reader"]["usage"]["model_calls"] > 0 for r in h_runs))
    check("H's attempt total is the sum of its two readers, counted once",
          all(r["usage_attempt_total"]["model_calls"]
              == r["usage"]["model_calls"]
              + r["restricted_reader"]["usage"]["model_calls"]
              for r in h_runs))
    check("every attempt lists its readers so nothing is double counted",
          all(len(r["usage_readers"]) >= 1 for r in got)
          and all(len(r["usage_readers"]) == 2 for r in h_runs))
    check("the accounting note warns against double counting",
          all("never" in r["usage_accounting_note"] for r in got))
    check("A records exactly one reader",
          all(len(r["usage_readers"]) == 1 for r in got if r["key"] == "A"))
    check("delivery was captured for each attempt",
          all((r.get("delivery") or {}).get("available") for r in got))

print("\n=== job attribution is authoritative ===")
check("ownership comes from the job record, not a missing field",
      "SELECT account_id FROM analysis_jobs" in open("run_home_eval.py").read())
check("a job id that resolves to nothing is attributed to nobody",
      R.job_owner(database, 10**9) is None)
check("a missing job id is attributed to nobody",
      R.job_owner(database, None) is None)
if True:
    c = CLIENT
    other = S.build_instance(c, "C", instance=200)
    c.get("/home/findings?days=7&tz=UTC", headers=S.auth(other["token"]))
    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute("SELECT id, account_id FROM analysis_jobs "
                    "ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
    check("a queued job resolves to its own account",
          R.job_owner(database, row["id"]) == other["account_id"])
    check("and not to some other fixture",
          R.job_owner(database, row["id"]) != other["account_id"] + 1)

# ===========================================================================
# 8. Zero-candidate runs are diagnosed correctly
# ===========================================================================
print("\n=== a run that raised no candidate is not a withheld candidate ===")

# The REAL report shape: investigator.py sets candidates=0 and appends
# "no candidate worth investigating" to abstained. Both fields present.
real_zero = [{"status": "done", "analysis_outcome": "complete",
              "candidates": 0, "published": 0, "rejected": [],
              "abstained": ["no candidate worth investigating"]}]
zero = SC.assess(B, [], [], analysis={"state": "current",
                                      "analysis_outcome": "complete"},
                 worker_reports=real_zero, evidence_delivered=True)
check("candidates: 0 with an abstention message reads as no_candidate_raised",
      zero["diagnosis"]["code"] == "no_candidate_raised")
check("the abstention message is still preserved for the reviewer",
      zero["diagnosis"]["abstention_reasons"]
      == [["no candidate worth investigating"]])
check("and the candidate count is reported",
      zero["diagnosis"]["candidates_raised"] == 0
      and zero["diagnosis"]["candidate_count_reported"] is True)

withheld = SC.assess(B, [], [], analysis={"state": "current",
                                          "analysis_outcome": "complete"},
                     worker_reports=[{"status": "done", "candidates": 2,
                                      "abstained": ["refunds: evidence did "
                                                    "not carry it"]}],
                     evidence_delivered=True)
check("a candidate that WAS investigated and held back still reads that way",
      withheld["diagnosis"]["code"] == "investigated_and_withheld")

blocked = SC.assess(B, [], [], analysis={"analysis_outcome": "incomplete",
                                         "completion_gaps": ["validation_rejected"]},
                    worker_reports=[{"status": "done", "candidates": 0,
                                     "rejected": ["overstated scope"],
                                     "abstained": ["no candidate worth investigating"]}])
check("a validator rejection outranks a zero candidate count",
      blocked["diagnosis"]["code"] == "draft_blocked_by_validation")

missing = SC.assess(B, [], [], analysis={"analysis_outcome": "complete"},
                    worker_reports=[{"status": "done"}], evidence_delivered=True)
check("a report with no candidate count says so rather than guessing",
      missing["diagnosis"]["code"] == "outcome_not_reported"
      and missing["diagnosis"]["candidate_count_reported"] is False)

inc = SC.assess(B, [], [], analysis={"state": "incomplete",
                                     "analysis_outcome": "incomplete",
                                     "completion_gaps": ["candidates_not_examined"]},
                worker_reports=[{"status": "done", "candidates": 2}],
                evidence_delivered=True)
check("an incomplete empty run is diagnosed as incomplete",
      inc["diagnosis"]["code"] == "incomplete_execution")
check("and is NOT described as a completed abstention",
      "completed analysis" not in inc["discovery_note"]
      and inc["discovery"] == "not_applicable")
check("the note names the gaps instead",
      "candidates_not_examined" in inc["discovery_note"])
check("the status still says it is not an abstention",
      inc["status"]["abstained"] is False)

# ===========================================================================
# 9. Usage follows the JOB that incurred it, not the reader that drained
# ===========================================================================
print("\n=== a foreign job draining inside a reader's window ===")

if True:
    c = CLIENT
    m3 = R.Meter(max_calls=200, max_usd=5.0, database=database,
                 model=investigator.MODEL, priced=False)
    investigator._client = lambda: m3.wrap(eval_stub_model.client())
    rec3 = R.DeliveryRecorder(investigation_tools)
    rec3.install()
    led3 = R.JobLedger(analysis_jobs, meter=m3, recorder=rec3, database=database)
    led3.install()

    # Queue B's job and leave it queued. Then run A — whose drain executes it.
    bctx = S.build_instance(c, "B", instance=900)
    c.get("/home/findings?days=7&tz=UTC", headers=S.auth(bctx["token"]))
    actx = S.build_instance(c, "A", instance=900)
    eval_stub_model.reset()
    before = m3.snapshot()
    ares = R.run_one_scenario(c, S, actx, analysis_jobs, investigator,
                              attempt=1, meter=m3, usage_before=before,
                              database=database, recorder=rec3, ledger=led3)

    own = ares["job_executions"]
    foreign = ares["foreign_job_executions"]
    total_window = m3.delta(before)["model_calls"]
    check("A drained a job that is not its own", bool(foreign))
    b_jobs = [e for e in foreign if e["account_id"] == bctx["account_id"]]
    check("B's job is attributed to B's account, not A's",
          b_jobs and all(e["account_id"] != actx["account_id"] for e in b_jobs))
    check("A's own execution is attributed to A's account",
          own and all(e["account_id"] == actx["account_id"] for e in own))
    a_calls = ares["usage"]["model_calls"]
    b_calls = sum(e["usage"]["model_calls"] for e in b_jobs)
    check(f"A reports only its own calls ({a_calls}), not the window's "
          f"({total_window})", a_calls < total_window and b_calls > 0)
    check("every call in the window belongs to exactly one job execution",
          sum(e["usage"]["model_calls"]
              for e in ares["job_executions"] + foreign) == total_window)
    check("the foreign job keeps its usage under its own identity",
          all(e["usage"]["model_calls"] > 0 for e in foreign))
    check("no call happened outside a job execution",
          ares["calls_outside_job_execution"] == 0)
    check("the attempt total does not absorb the foreign job",
          ares["usage_attempt_total"]["model_calls"] == a_calls)
    check("and the accounting says it is per-job, not a window",
          ares["usage_attribution"] == "per_job_execution")
    check("A's delivery does not borrow B's evidence",
          not (set(ares["delivery"].get("keys") or [])
               & set((foreign and rec3 and []) or [])) )

    # Two readers in the same account keep separate attribution.
    print("\n=== two readers, one account ===")
    hctx = S.build_instance(c, "H", instance=900)
    eval_stub_model.reset()
    hres = R.run_one_scenario(c, S, hctx, analysis_jobs, investigator,
                              attempt=1, meter=m3, usage_before=m3.snapshot(),
                              database=database, recorder=rec3, ledger=led3)
    rr = hres["restricted_reader"]
    check("both readers ran in the same account",
          hres["fixture"]["account_id"] == hctx["account_id"])
    check("the primary reader's executions carry its own viewer id",
          all(e["viewer_user_id"] == hctx["user_id"]
              for e in hres["job_executions"]))
    check("the restricted reader's executions carry a different viewer id",
          rr["job_executions"]
          and all(e["viewer_user_id"] == rr["user_id"]
                  for e in rr["job_executions"]))
    check("account alone would have merged them — viewer id keeps them apart",
          hctx["user_id"] != rr["user_id"])
    check("neither reader's subtotal contains the other's job",
          set(e["job_id"] for e in hres["job_executions"]).isdisjoint(
              e["job_id"] for e in rr["job_executions"]))
    check("and the attempt total is exactly their sum",
          hres["usage_attempt_total"]["model_calls"]
          == hres["usage"]["model_calls"] + rr["usage"]["model_calls"])

    led3.uninstall()
    rec3.uninstall()

print("\n=== failures and unknown ownership stay put ===")

class _Boom(Exception):
    pass


class _FakeJobs:
    """Stands in for analysis_jobs so run_one's outcome can be chosen."""

    def __init__(self, behaviour):
        self.behaviour = list(behaviour)

    meter = None

    def run_one(self):
        if not self.behaviour:
            return None
        kind, payload, spend = self.behaviour.pop(0)
        # Spend INSIDE the execution, which is what the ledger measures.
        self.meter.calls += spend
        if kind == "raise":
            raise _Boom(payload)
        return payload


mfail = R.Meter(max_calls=50, max_usd=5.0, database=database,
                model=investigator.MODEL, priced=False)
fake_jobs = _FakeJobs([
    ("ok", {"job_id": 424242, "status": "failed", "error": "transport"}, 3),
    ("raise", "kaboom", 2),
])
fake_jobs.meter = mfail
led4 = R.JobLedger(fake_jobs, meter=mfail, recorder=None, database=database)
led4.install()
fake_jobs.run_one()
try:
    fake_jobs.run_one()
except _Boom:
    pass
led4.uninstall()

check("a FAILED job still records an execution", len(led4.executions) == 2)
check("its usage is retained", led4.executions[0]["usage"]["model_calls"] == 3)
check("its failure status is retained",
      led4.executions[0]["status"] == "failed")
check("a CRASHED job records an execution too",
      led4.executions[1]["status"] == "crashed"
      and led4.executions[1]["usage"]["model_calls"] == 2)
check("a job id that resolves to nothing is explicitly unattributed",
      all(e["ownership"] == "unknown" for e in led4.executions))
check("and carries no borrowed account",
      all(e["identity"] is None for e in led4.executions))
check("unknown-ownership usage is not assigned to any reader",
      R.JobLedger.owned_by(led4.executions, account_id=1,
                           viewer_user_id=1) == [])

# ===========================================================================
# 10. Delivered contents, not retrieved ids
#
# Both reviewer reproductions, driven through the REAL session retrieval and
# fitting paths. No hand-built "delivered" dictionary appears here: a dict you
# write yourself tests your own typing, not the instrumentation.
# ===========================================================================
print("\n=== reviewer case 1: run summaries are not failing-step evidence ===")

import home_eval_delivery as HD
SESSION = HD.recording_session_class(investigation_tools.InvestigationSession)

if True:
    c = CLIENT
    bfix = S.build_instance(c, "B", instance=700)
    job, stalled = bfix["ids"]["job"], bfix["ids"]["stalled"]

    # Summaries + comparison, NO inspect_run — exactly the reviewer's steps.
    s_sum = SESSION(account_id=bfix["account_id"], only_user_ids=None,
                    financial_visible=True)
    s_sum.retrieve("list_comparable_runs", {"job_id": job, "limit": 50})
    s_sum.retrieve("compare_outcome_mix", {"days": 7, "job_id": job})
    d_sum = HD.delivery_report([s_sum])
    v_sum = R.actual_evidence_delivered(S, bfix, d_sum)

    check("the run summaries really were delivered",
          all(f"run:{r}" in d_sum["keys"] for r in stalled))
    check("but no failed_span was",
          not any(k.startswith("failed_span") for k in d_sum["keys"]))
    check("and approval_service appears in no delivered payload",
          "approval_service" not in json.dumps(d_sum["payloads"], default=str))
    check("so the failing-step requirement is NOT satisfied",
          v_sum["value"] is False)
    check("and the reason names the failing step and the runs",
          "approval_service" in v_sum["reason"]
          and all(str(r) in v_sum["reason"] for r in stalled))
    check("run ids arriving no longer reads as 'every requirement delivered'",
          "every requirement" not in v_sum["reason"])

    print("\n=== the same scenario WITH the failing-step details delivered ===")
    s_full = SESSION(account_id=bfix["account_id"], only_user_ids=None,
                     financial_visible=True)
    s_full.retrieve("list_comparable_runs", {"job_id": job, "limit": 50})
    s_full.retrieve("compare_outcome_mix", {"days": 7, "job_id": job})
    for rid in stalled:
        s_full.retrieve("inspect_run", {"run_id": rid})
    d_full = HD.delivery_report([s_full])
    v_full = R.actual_evidence_delivered(S, bfix, d_full)
    check("failed_span rows are now delivered",
          any(k.startswith("failed_span") for k in d_full["keys"]))
    check("and the requirement is satisfied", v_full["value"] is True)
    check("with every requirement accounted for",
          len(v_full["satisfied"]) == len(v_full["requirements"]))

    print("\n=== reviewer case 2: a registered calculation is not a delivered one ===")
    ffix = S.build_instance(c, "F", instance=700)
    fjob = ffix["ids"]["job"]

    s_run = SESSION(account_id=ffix["account_id"], only_user_ids=None,
                    financial_visible=True)
    s_run.run("compare_outcome_mix", {"days": 7, "job_id": fjob})   # no fit
    d_run = HD.delivery_report([s_run])
    v_run = R.actual_evidence_delivered(S, ffix, d_run)
    check("the calculation registry is populated by retrieval alone",
          len(s_run.calculations) >= 12)
    check("but nothing was delivered", d_run["keys"] == []
          and d_run["calculations"] == [])
    check("so the comparison requirement is NOT satisfied", v_run["value"] is False)
    check("and the report no longer counts 12 delivered calculations",
          v_run["delivered_calculations"] == 0)

    print("\n=== the same comparison, actually delivered ===")
    s_fit = SESSION(account_id=ffix["account_id"], only_user_ids=None,
                    financial_visible=True)
    s_fit.retrieve("compare_outcome_mix", {"days": 7, "job_id": fjob})
    d_fit = HD.delivery_report([s_fit])
    v_fit = R.actual_evidence_delivered(S, ffix, d_fit)
    check("delivered calculations are now counted",
          v_fit["delivered_calculations"] > 0)
    check("and the requirement is satisfied", v_fit["value"] is True)

    print("\n=== a dropped or trimmed response delivers nothing ===")
    s_drop = SESSION(account_id=ffix["account_id"], only_user_ids=None,
                     financial_visible=True)
    raw = s_drop.run("compare_outcome_mix", {"days": 7, "job_id": fjob})
    # Fit, then settle as though the whole result had been dropped — the real
    # path the product takes when a response cannot be sent.
    sent, _text = s_drop.fit(raw)
    s_drop2 = SESSION(account_id=ffix["account_id"], only_user_ids=None,
                      financial_visible=True)
    raw2 = s_drop2.run("compare_outcome_mix", {"days": 7, "job_id": fjob})
    resp2 = s_drop2._responses.get(id(raw2), (None, None))[1]
    s_drop2._settle_delivery(resp2, {}, whole_result_dropped=True)
    d_drop = HD.delivery_report([s_drop2])
    v_drop = R.actual_evidence_delivered(S, ffix, d_drop)
    check("a dropped response promotes no evidence", d_drop["keys"] == [])
    check("and no calculation", d_drop["calculations"] == [])
    check("and records the drop", bool(d_drop.get("dropped_responses")))
    check("so it cannot satisfy the requirement", v_drop["value"] is False)

    print("\n=== a calculation whose supporting values were removed ===")
    s_trim = SESSION(account_id=ffix["account_id"], only_user_ids=None,
                     financial_visible=True)
    raw3 = s_trim.run("compare_outcome_mix", {"days": 7, "job_id": fjob})
    resp3 = s_trim._responses.get(id(raw3), (None, None))[1]
    # The ids survive; the numbers they refer to do not.
    stripped = {k: v for k, v in raw3.items() if k not in ("current", "previous")}
    s_trim._settle_delivery(resp3, stripped, whole_result_dropped=False)
    d_trim = HD.delivery_report([s_trim])
    check("the calculation ids are still in the sent payload",
          "calculation_ids" in json.dumps(d_trim["payloads"], default=str))
    check("but none is counted as delivered, because its value is gone",
          d_trim["calculations"] == [])
    check("so the comparison requirement fails",
          R.actual_evidence_delivered(S, ffix, d_trim)["value"] is False)

    print("\n=== a comparison for the wrong job cannot satisfy this one ===")
    s_wrong = SESSION(account_id=ffix["account_id"], only_user_ids=None,
                      financial_visible=True)
    s_wrong.retrieve("compare_outcome_mix", {"days": 7})   # account-wide, no job
    v_wrong = R.actual_evidence_delivered(S, ffix, HD.delivery_report([s_wrong]))
    check("a comparison over the wrong population does not count",
          v_wrong["value"] is False)
    check("and the reason names the job and period it needed",
          str(fjob) in v_wrong["reason"] and "7d" in v_wrong["reason"])

    print("\n=== unknown vs observed-empty vs unavailable ===")
    s_empty = SESSION(account_id=ffix["account_id"], only_user_ids=None,
                      financial_visible=True)
    d_empty = HD.delivery_report([s_empty])
    check("a session that delivered nothing is observed empty, not unavailable",
          d_empty["available"] is True and d_empty["observed_empty"] is True)
    check("and yields a definite False",
          R.actual_evidence_delivered(S, ffix, d_empty)["value"] is False)
    d_none = HD.delivery_report([])
    check("no session at all is unavailable", d_none["available"] is False)
    check("and yields unknown, not False",
          R.actual_evidence_delivered(S, ffix, d_none)["value"] is None)
    check("those are different answers",
          R.actual_evidence_delivered(S, ffix, d_empty)["value"]
          is not R.actual_evidence_delivered(S, ffix, d_none)["value"])

    print("\n=== an uninstrumented session cannot establish contents ===")
    plain = PRISTINE_SESSION(
        account_id=bfix["account_id"], only_user_ids=None, financial_visible=True)
    plain.retrieve("list_comparable_runs", {"job_id": job, "limit": 50})
    d_plain = HD.delivery_report([plain])
    v_plain = R.actual_evidence_delivered(S, bfix, d_plain)
    check("evidence keys are still trusted", bool(d_plain["keys"]))
    check("but payload capture is reported absent",
          d_plain["payload_capture"] is False)
    check("and the content-dependent requirement is unknown, never satisfied",
          v_plain["value"] is None and v_plain["unknown"])

    print("\n=== an unsupported requirement kind never passes ===")
    made_up = HD.check_requirements([{"kind": "telepathy"}], d_full)
    check("an unrecognised requirement is unknown",
          made_up["value"] is None and made_up["unknown"])
    check("and says so", "unsupported requirement kind" in made_up["reason"])

    print("\n=== scenario E keeps its unavailable-pattern distinction ===")
    efix = S.build_instance(c, "E", instance=700)
    s_e = SESSION(account_id=efix["account_id"], only_user_ids=None,
                  financial_visible=True)
    s_e.retrieve("list_comparable_runs", {"job_id": efix["ids"]["job"], "limit": 50})
    s_e.retrieve("cost_evidence", {"days": 7})
    for rid in efix["ids"]["heavy"]:
        s_e.retrieve("inspect_run", {"run_id": rid})
    v_e = R.actual_evidence_delivered(S, efix, HD.delivery_report([s_e]))
    check("E's run rows and cost evidence are delivered",
          not v_e["missing"])
    check("yet the requirement is still not satisfied", v_e["value"] is False)
    check("because its pattern is unretrievable",
          v_e["unretrievable"]
          and "repeated successful tool calls"
          in v_e["unretrievable"][0]["requirement"])
    check("and that is reported as 'no tool can satisfy', not 'did not fetch'",
          "no tool can satisfy" in v_e["reason"])
    check("the diagnosis code reflects that",
          SC.diagnose(status={"execution": "completed"},
                      worker_reports=[{"status": "done", "candidates": 1}],
                      published=0, evidence_delivered=False,
                      evidence_unretrievable=True)["code"]
          == "required_evidence_unretrievable")

    print("\n=== another execution's delivery cannot satisfy this one ===")
    v_cross = R.actual_evidence_delivered(S, bfix, HD.delivery_report([s_fit]))
    check("F's delivered comparison satisfies none of B's requirements",
          v_cross["value"] is False and v_cross["missing"])

# ===========================================================================
# 11. The captured contents reach the assessment — end to end
# ===========================================================================
# Section 10 proves the instrumentation records the right thing. It did, and
# the assessment still never saw it: `_merge_delivery` reduced every execution
# to its evidence keys and calculation ids before handing it on, so
# `--scenarios B,F,H` reported `payload_capture: missing` and
# `actual_evidence_delivered.value: null` for all three. These go through
# `run_one_scenario` — the real enqueue/drain/read path — because a direct call
# to a delivery helper cannot see a runner that drops the helper's output.
print("\n=== captured payloads reach assessment through the runner ===")


class _ScriptedClient:
    """A model that makes exactly the tool calls it is told to.

    `eval_stub_model` deliberately opens with one `list_comparable_runs` and
    stops, which exercises the pipeline but can never deliver a scenario's full
    evidence. These regressions need both directions, so the tool script is an
    argument.
    """

    def __init__(self, script):
        self._script = list(script)
        self.turns = 0
        self.run_ref = None
        outer = self

        class M:
            def create(self, **kw):
                return outer._create(**kw)

        self.messages = M()

    def _create(self, **kw):
        import eval_stub_model as SM
        system = kw.get("system") or ""
        seen = SM._first_run_ref(kw.get("messages") or [])
        if seen and not self.run_ref:
            self.run_ref = seen
        if system.startswith("You are Trovis, examining"):
            return SM._text({"candidates": [{
                "topic": "recorded-outcomes",
                "question": "What do the recorded outcomes of this job show?",
                "hypothesis": "Some items did not finish.",
                "category": "attention",
                "why_this_reader": "They are responsible for this work.",
                "evidence_needed": ["the job's runs and their outcomes"],
            }]})
        if system.startswith("You are Trovis, investigating"):
            if self.turns < len(self._script):
                name, args = self._script[self.turns]
                self.turns += 1
                return SM._tool(name, args, call_id=f"s{self.turns}")
            self.turns += 1
            ref = self.run_ref
            return SM._text({
                "verdict": "qualified",
                "summary": "The retrieved runs carry the outcomes shown.",
                "for": [f"run:{ref}"] if ref else [],
                "against": [],
                "alternatives_considered": ["a different population"],
                "unknown": ["why any individual item ended as it did"],
                "sample": {"observed": 1, "comparable": 1},
            })
        if system.startswith("You are Trovis, checking"):
            return SM._text({"decision": "publish", "reason": "observation only",
                             "claim_kind": "observation",
                             "confidence": "qualified",
                             "overstated_phrases": []})
        if system.startswith("You are Trovis, ordering"):
            return SM._text({"order": [{"index": 0, "score": 0.5,
                                        "reason": "only one"}],
                             "merge": [], "drop": []})
        ref = self.run_ref
        if not ref:
            return SM._text({"withdraw": True})
        return SM._text({
            "title": "One work item's recorded outcome",
            "explanation": "The record shows this item's outcome as retrieved.",
            "consequence": None,
            "claims": [{"text": "This item has the outcome the record shows.",
                        "kind": "observation", "evidence": [f"run:{ref}"]}],
            "entities": [{"kind": "run", "id": int(ref)}],
            "evidence": [{"kind": "run", "ref": ref, "note": "the retrieved row"}],
            "uncertainty": ["this is one item, not a pattern"],
            "next_step": {"kind": "review_runs", "text": "Open the item."},
            "graphic": {"kind": "none"},
        })


def _e2e(key, script_for, *, instance, recorder=None):
    """Run ONE scenario end to end with a scripted tool sequence.

    Returns the runner's own result dict, so the assertions read exactly the
    fields `--mode stub` writes into the transcript.
    """
    meter = R.Meter(max_calls=60, max_usd=5.0, database=database,
                    model=investigator.MODEL, priced=False)
    rec = recorder if recorder is not None else R.DeliveryRecorder(
        investigation_tools)
    own_recorder = recorder is None
    if own_recorder:
        rec.install()
    ledger = R.JobLedger(analysis_jobs, meter=meter, recorder=rec,
                         database=database)
    ledger.install()
    try:
        ctx = S.build_instance(CLIENT, key, instance=instance)
        client = _ScriptedClient(script_for(ctx))
        investigator._client = lambda: meter.wrap(client)
        res = R.run_one_scenario(
            CLIENT, S, ctx, analysis_jobs, investigator,
            attempt=1, meter=meter, usage_before=meter.snapshot(),
            database=database, recorder=rec, ledger=ledger)
        return ctx, res
    finally:
        ledger.uninstall()
        if own_recorder:
            rec.uninstall()


# --- the evidence the scenario turns on IS delivered -----------------------
b_full_ctx, b_full = _e2e(
    "B",
    lambda ctx: (
        [("list_comparable_runs", {"job_id": ctx["ids"]["job"], "limit": 25})]
        + [("inspect_run", {"run_id": r}) for r in ctx["ids"]["stalled"]]
        + [("compare_outcome_mix", {"days": 7, "job_id": ctx["ids"]["job"]})]
    ), instance=11)
bd, ba = b_full["delivery"], b_full["actual_evidence_delivered"]
check("the runner's delivery report carries payload capture",
      bd.get("payload_capture") is True)
check("and carries the captured payloads themselves",
      bool(bd.get("payloads")))
check("and keeps them per job execution rather than flattened away",
      isinstance(bd.get("per_execution"), list) and bd["per_execution"])
check("B with the full retrieval is assessed as delivered",
      ba["value"] is True, )
check("with no requirement unknown, so nothing passed by default",
      ba["unknown"] == [] and ba["missing"] == [])
check("and the supporting calculations are counted as delivered",
      ba["delivered_calculations"] > 0)

# --- summary-only retrieval does NOT satisfy the failing step --------------
b_sum_ctx, b_sum = _e2e(
    "B",
    lambda ctx: [
        ("list_comparable_runs", {"job_id": ctx["ids"]["job"], "limit": 25}),
        ("compare_outcome_mix", {"days": 7, "job_id": ctx["ids"]["job"]}),
    ], instance=12)
bs = b_sum["actual_evidence_delivered"]
check("summary-only B reaches a definite answer, not 'unavailable'",
      bs["value"] is False)
check("and it is the failing step that is named missing",
      any(r.get("kind") == "failing_step" for r in bs["missing"]))
check("the run summaries themselves still count as delivered",
      b_sum["delivery"].get("keys")
      and any(k.startswith("run:") for k in b_sum["delivery"]["keys"]))
check("and payload capture is reported present, not missing",
      b_sum["delivery"].get("payload_capture") is True)

# --- a calculation that was generated but never fitted ---------------------
# The runner's own path always fits, so the unfitted case is produced by
# settling a real response as wholly dropped inside a real execution's session,
# then letting the runner report it.
print("\n=== an unfitted / dropped calculation is not a delivered one ===")
f_ctx, f_res = _e2e(
    "F",
    lambda ctx: [("compare_outcome_mix", {"days": 7, "job_id": ctx["ids"]["job"]})],
    instance=13)
fa = f_res["actual_evidence_delivered"]
check("F with a fitted comparison is assessed as delivered",
      fa["value"] is True and fa["delivered_calculations"] > 0)

sess_drop = HD.recording_session_class(PRISTINE_SESSION)(
    account_id=f_ctx["account_id"], only_user_ids=None, financial_visible=True)
raw_drop = sess_drop.run("compare_outcome_mix",
                         {"days": 7, "job_id": f_ctx["ids"]["job"]})
resp_drop = sess_drop._responses.get(id(raw_drop), (None, None))[1]
sess_drop._settle_delivery(resp_drop, {}, whole_result_dropped=True)
drop_report = HD.delivery_report([sess_drop])
merged_drop = R._merge_delivery([drop_report])
v_drop = R.actual_evidence_delivered(S, f_ctx, merged_drop)
check("a registered calculation with a dropped response is not delivered",
      v_drop["value"] is False and v_drop["delivered_calculations"] == 0)
check("even though the session registered the calculation ids",
      len(getattr(sess_drop, "calculations", {}) or {}) > 0)
check("and the merge reports the dropped response rather than hiding it",
      bool(merged_drop.get("dropped_responses")))
check("the raw tool result did carry the calculation ids",
      bool((raw_drop or {}).get("calculation_ids")))

# --- missing instrumentation stays explicitly unavailable ------------------
print("\n=== missing instrumentation stays unavailable, never satisfied ===")
plain_e2e = PRISTINE_SESSION(
    account_id=f_ctx["account_id"], only_user_ids=None, financial_visible=True)
plain_e2e.retrieve("compare_outcome_mix",
                   {"days": 7, "job_id": f_ctx["ids"]["job"]})
merged_plain = R._merge_delivery([HD.delivery_report([plain_e2e])])
v_plain = R.actual_evidence_delivered(S, f_ctx, merged_plain)
check("an uninstrumented execution reports payload capture absent",
      merged_plain.get("payload_capture") is False)
check("and its content requirement is unknown, never true",
      v_plain["value"] is None and v_plain["unknown"])
check("no owned execution at all is unavailable, not observed-empty",
      R._merge_delivery([])["available"] is False
      and "observed_empty" not in R._merge_delivery([]))

# --- partial capture is not full capture -----------------------------------
mixed = R._merge_delivery([HD.delivery_report([plain_e2e]),
                           HD.delivery_report([sess_drop])])
check("a reader with one uninstrumented execution is not fully observed",
      mixed.get("payload_capture") is False
      and mixed.get("payload_capture_partial") is True)
check("and the count of uncaptured executions is reported",
      mixed.get("executions_without_payload_capture") == 1)
v_mixed = R.actual_evidence_delivered(S, f_ctx, mixed)
check("the partial state is carried into the assessment's reason",
      "payload capture was unavailable" in v_mixed["reason"])

# --- two executions are never pooled ---------------------------------------
print("\n=== unrelated executions are not pooled into sufficiency ===")
# A fresh B fixture. It no longer has to be seeded last: `_classify` is scoped
# to its own account, so a later B cannot re-point this one's runs.
pool_ctx = S.build_instance(CLIENT, "B", instance=14)
half_a = HD.recording_session_class(PRISTINE_SESSION)(
    account_id=pool_ctx["account_id"], only_user_ids=None,
    financial_visible=True)
for r in pool_ctx["ids"]["stalled"]:
    half_a.retrieve("inspect_run", {"run_id": r})
half_b = HD.recording_session_class(PRISTINE_SESSION)(
    account_id=pool_ctx["account_id"], only_user_ids=None,
    financial_visible=True)
half_b.retrieve("list_comparable_runs",
                {"job_id": pool_ctx["ids"]["job"], "limit": 25})
half_b.retrieve("compare_outcome_mix",
                {"days": 7, "job_id": pool_ctx["ids"]["job"]})
split = R._merge_delivery([HD.delivery_report([half_a]),
                           HD.delivery_report([half_b])])
v_split = R.actual_evidence_delivered(S, pool_ctx, split)
check("two executions each holding half the evidence do not add up to enough",
      v_split["value"] is False)
check("although the top-level union does contain both halves",
      len(split["keys"]) > len(HD.delivery_report([half_b])["keys"]))
together = HD.delivery_report([half_a, half_b])
check("the same two sessions inside ONE execution do satisfy it",
      R.actual_evidence_delivered(
          S, pool_ctx, R._merge_delivery([together]))["value"] is True)

# ===========================================================================
# 12. A fixture cannot rewrite another fixture
# ===========================================================================
# `home_eval_scenarios._classify` filed seeded work with
# `UPDATE loops SET workflow_id = ? WHERE title LIKE ?` and no account filter,
# so it reached across the whole database. Seeding scenario B twice re-pointed
# the FIRST fixture's six runs at the SECOND fixture's job, and the harness's
# own regressions had to be ordered around it. These prove the first fixture is
# untouched — its job assignments, what a query returns, and what retrieval
# actually delivers.
print("\n=== a second fixture leaves the first one alone ===")


def _raises(fn):
    """Did `fn` refuse? Used where writing the wrong row is the bug."""
    try:
        fn()
    except AssertionError:
        return True
    return False


def _job_of(run_ids):
    """The job each run is currently filed under, straight from the record."""
    with database._connect() as conn, database._cursor(conn) as cur:
        out = {}
        for rid in run_ids:
            cur.execute(
                f"SELECT workflow_id, account_id FROM loops WHERE id = {database.PH}",
                (rid,),
            )
            row = cur.fetchone()
            out[rid] = dict(row) if row else None
        return out


first = S.build_instance(CLIENT, "B", instance=21)
first_runs = list(first["ids"]["stalled"]) + list(first["ids"]["finished"])
before_jobs = _job_of(first_runs)

# What the first fixture's own job returns, and what an investigation is shown,
# BEFORE the second fixture exists.
probe_before = HD.recording_session_class(PRISTINE_SESSION)(
    account_id=first["account_id"], only_user_ids=None, financial_visible=True)
listed_before = probe_before.retrieve(
    "list_comparable_runs", {"job_id": first["ids"]["job"], "limit": 50})
keys_before = sorted(probe_before.delivered)

# Now seed an equivalent second fixture: same scenario, same titles, its own
# account and its own job.
second = S.build_instance(CLIENT, "B", instance=22)

after_jobs = _job_of(first_runs)
probe_after = HD.recording_session_class(PRISTINE_SESSION)(
    account_id=first["account_id"], only_user_ids=None, financial_visible=True)
listed_after = probe_after.retrieve(
    "list_comparable_runs", {"job_id": first["ids"]["job"], "limit": 50})
keys_after = sorted(probe_after.delivered)

check("the two fixtures got different accounts and different jobs",
      first["account_id"] != second["account_id"]
      and first["ids"]["job"] != second["ids"]["job"])
check("the two fixtures seeded different runs",
      not (set(first_runs) & set(second["ids"]["stalled"])))
check("the first fixture's job assignments are unchanged",
      after_jobs == before_jobs)
check("and every one of its runs is still filed under ITS job",
      all(r and r["workflow_id"] == first["ids"]["job"]
          for r in after_jobs.values()))
check("the first fixture's query returns the same rows it did before",
      [x["run_id"] for x in (listed_after.get("runs") or [])]
      == [x["run_id"] for x in (listed_before.get("runs") or [])])
check("and the same six of them",
      len(listed_after.get("runs") or []) == 6)
check("the evidence delivered from it is unchanged",
      keys_after == keys_before and keys_before)
check("the second fixture's runs never appear in the first one's results",
      not (set(second["ids"]["stalled"])
           & {x["run_id"] for x in (listed_after.get("runs") or [])}))
check("the second fixture's own job holds only its own runs",
      all(r and r["workflow_id"] == second["ids"]["job"]
          for r in _job_of(second["ids"]["stalled"]).values()))
# The scenario's requirements still resolve against the first fixture after the
# second exists — the point of the whole exercise.
first_full = HD.recording_session_class(PRISTINE_SESSION)(
    account_id=first["account_id"], only_user_ids=None, financial_visible=True)
first_full.retrieve("list_comparable_runs",
                    {"job_id": first["ids"]["job"], "limit": 50})
for r in first["ids"]["stalled"]:
    first_full.retrieve("inspect_run", {"run_id": r})
first_full.retrieve("compare_outcome_mix",
                    {"days": 7, "job_id": first["ids"]["job"]})
check("and the first fixture still satisfies its own requirements",
      R.actual_evidence_delivered(
          S, first, R._merge_delivery([HD.delivery_report([first_full])])
      )["value"] is True)

print("\n=== classification refuses to cross an account boundary ===")
try:
    S._classify(second["ids"]["job"], "Refund %", first["account_id"])
    crossed = True
except AssertionError as exc:
    crossed = False
    cross_msg = str(exc)
check("filing one account's work under another's job is refused",
      crossed is False)
check("and the refusal names both accounts", "belongs to account" in cross_msg)
check("the refusal wrote nothing", _job_of(first_runs) == before_jobs)
check("a non-existent job is refused too",
      _raises(lambda: S._classify(10**9, "Refund %", first["account_id"])))

# ===========================================================================
# 13. An execution we could not look at still counts
# ===========================================================================
# `_merge_delivery` filtered the `available: false` reports out BEFORE
# measuring capture completeness, so a reader with one observed execution and
# one unobservable one reported `payload_capture: true`, `partial: false`,
# `executions_without_payload_capture: 0` — a clean bill written by leaving the
# unknown out of the denominator.
print("\n=== an unavailable execution is not left out of the count ===")

UNAVAILABLE = {"available": False, "reason": "capture missing"}

obs_ctx = S.build_instance(CLIENT, "F", instance=23)
observed_sess = HD.recording_session_class(PRISTINE_SESSION)(
    account_id=obs_ctx["account_id"], only_user_ids=None, financial_visible=True)
observed_sess.retrieve("compare_outcome_mix",
                       {"days": 7, "job_id": obs_ctx["ids"]["job"]})
observed = HD.delivery_report([observed_sess])

# --- fully observed --------------------------------------------------------
full = R._merge_delivery([observed])
check("one observed execution is fully captured",
      full["payload_capture"] is True
      and full["payload_capture_partial"] is False
      and full["executions_without_payload_capture"] == 0)
check("and it reports its own totals",
      full["executions"] == 1 and full["executions_total"] == 1
      and full["executions_unavailable"] == 0)
check("a fully observed reader is assessed from what it delivered",
      R.actual_evidence_delivered(S, obs_ctx, full)["value"] is True)

# --- the reproduction: one observed, one unavailable -----------------------
mixed_av = R._merge_delivery([observed, dict(UNAVAILABLE)])
check("a reader with an unavailable execution is NOT fully captured",
      mixed_av["payload_capture"] is False)
check("it is reported as partial",
      mixed_av["payload_capture_partial"] is True)
check("and the uncaptured execution is counted, not dropped",
      mixed_av["executions_without_payload_capture"] == 1)
check("both executions are retained",
      mixed_av["executions_total"] == 2 and len(mixed_av["per_execution"]) == 2)
check("with the unavailable one's reason kept",
      mixed_av["unavailable_reasons"] == ["capture missing"])
v_mixed_av = R.actual_evidence_delivered(S, obs_ctx, mixed_av)
check("the observed execution's verified result survives",
      v_mixed_av["value"] is True and v_mixed_av["verified_by_execution"] is True)
check("but the reader's capture is NOT reported complete",
      v_mixed_av["payload_capture"] is False
      and v_mixed_av.get("payload_capture_partial") is True)
check("and the reason says which executions could not be read",
      "payload capture was unavailable for 1" in v_mixed_av["reason"])

# --- an observed failure beside an unknown is not a reader-wide absence ----
print("\n=== one failure plus one unknown is not a definite absence ===")
short_sess = HD.recording_session_class(PRISTINE_SESSION)(
    account_id=obs_ctx["account_id"], only_user_ids=None, financial_visible=True)
short_sess.retrieve("list_comparable_runs",
                    {"job_id": obs_ctx["ids"]["job"], "limit": 25})
short = HD.delivery_report([short_sess])
check("on its own, that execution definitively missed the requirement",
      R.actual_evidence_delivered(
          S, obs_ctx, R._merge_delivery([short]))["value"] is False)
v_gap = R.actual_evidence_delivered(
    S, obs_ctx, R._merge_delivery([short, dict(UNAVAILABLE)]))
check("beside an unreadable execution it is NOT a definite absence",
      v_gap["value"] is None)
check("the unavailable execution is assessed, not skipped",
      v_gap["executions_assessed"] == 2 and v_gap["executions_observed"] == 1
      and v_gap["executions_unavailable"] == 1)
check("and no execution verified it either",
      v_gap["verified_by_execution"] is False)

# --- fully unavailable -----------------------------------------------------
print("\n=== every execution unavailable stays unavailable ===")
none_av = R._merge_delivery([dict(UNAVAILABLE), dict(UNAVAILABLE)])
check("no usable execution is unavailable, not observed-empty",
      none_av["available"] is False and "observed_empty" not in none_av)
check("it counts the executions it could not read",
      none_av["executions_total"] == 2
      and none_av["executions_unavailable"] == 2
      and none_av["executions_without_payload_capture"] == 2)
check("and never claims capture",
      none_av["payload_capture"] is False
      and none_av["payload_capture_partial"] is False)
check("its reason is the executions' own",
      none_av["reason"] == "capture missing")
check("and the assessment is unknown, never False",
      R.actual_evidence_delivered(S, obs_ctx, none_av)["value"] is None)

# --- still no pooling ------------------------------------------------------
check("an unavailable execution cannot complete another one's evidence",
      R.actual_evidence_delivered(
          S, b_full_ctx,
          R._merge_delivery([HD.delivery_report([half_a]), dict(UNAVAILABLE)])
      )["value"] is None)

# --- through the runner ----------------------------------------------------
print("\n=== the runner's own handoff keeps the unavailable execution ===")
_, e2e_res = _e2e(
    "F",
    lambda ctx: [("compare_outcome_mix", {"days": 7, "job_id": ctx["ids"]["job"]})],
    instance=24)
runner_delivery = e2e_res["delivery"]
check("a real run reports its execution total",
      runner_delivery.get("executions_total") == runner_delivery.get("executions")
      and runner_delivery.get("executions_unavailable") == 0)
check("and is fully captured with nothing uncounted",
      runner_delivery.get("payload_capture") is True
      and runner_delivery.get("executions_without_payload_capture") == 0)
check("and reaches a verified assessment",
      e2e_res["actual_evidence_delivered"]["value"] is True)
# The same runner report with one unreadable execution added behaves as above.
runner_gap = R._merge_delivery(
    list(runner_delivery["per_execution"]) + [dict(UNAVAILABLE)])
check("adding an unreadable execution to a real run makes it partial",
      runner_gap["payload_capture"] is False
      and runner_gap["payload_capture_partial"] is True
      and runner_gap["executions_without_payload_capture"] == 1)

# ===========================================================================
# The plan's retry policy is the one a spend estimate turns on
# ===========================================================================
print("\n=== the pre-spend plan describes the retries that actually happen ===")

# Trovis moved retrying out of `messages.create()` and into its own recorded
# boundary. The plan kept reporting a flat `provider_retries: 0`, which reads
# as "one logical call, one request" — so a reader approving a $5 run was
# shown a request count that a single retried call could exceed threefold.
import home_llm_usage

class _Boom(Exception):
    status_code = 500


class _AlwaysFails:
    class messages:
        @staticmethod
        def create(**kw):
            raise _Boom("upstream")


retry_meter = R.Meter(max_calls=60, max_usd=5.0, database=database,
                      model=investigator.MODEL, priced=False)
wrapped = retry_meter.wrap(_AlwaysFails())
try:
    home_llm_usage.call(
        home_llm_usage.STAGE_PROPOSING,
        lambda: wrapped.messages.create(
            model=investigator.MODEL, max_tokens=16,
            messages=[{"role": "user", "content": "hi"}]),
        model=investigator.MODEL, sleep=lambda _s: None)
except _Boom:
    pass

check("one logical call can spend more than one metered request",
      retry_meter.calls == home_llm_usage.MAX_RETRIES + 1)
check("and every failed attempt is counted, not released",
      retry_meter.failed_calls == retry_meter.calls)

# The real plan, from the real entry point — a hand-built dict here would pass
# even with the fix reverted.
import subprocess

plan_proc = subprocess.run(
    [sys.executable, "run_home_eval.py", "--mode", "plan",
     "--scenarios", "A", "--max-usd", "5"],
    cwd=os.path.dirname(os.path.abspath(__file__)),
    capture_output=True, text=True, timeout=300,
    env={**os.environ, "OVERSEE_DISABLE_PRICING_SYNC": "1"},
)
out = plan_proc.stdout
plan = json.loads(out[out.index("{"):out.rindex("}") + 1])
plan_limits = plan["limits"]

check("the plan no longer claims a flat zero retries",
      "provider_retries" not in plan_limits)
check("it reports the SDK's own retries and Trovis's separately",
      plan_limits["retry_policy"]["sdk_retries"] == 0
      and plan_limits["retry_policy"]["trovis_boundary_retries"]
      == home_llm_usage.MAX_RETRIES)
check("and the requests-per-call figure matches what the meter observed",
      plan_limits["retry_policy"]["max_requests_per_call"] == retry_meter.calls)
check("the plan still states the model, the ceilings and the tool budget",
      plan["model"] == investigator.MODEL
      and plan_limits["max_model_calls"] > 0
      and plan_limits["max_estimated_usd"] == 5.0
      and plan_limits["tool_budget"]["max_calls"] > 0)

CLIENT.__exit__(None, None, None)

print("\n" + ("FAILED: " + "; ".join(failures) if failures else "ALL PASS"))
raise SystemExit(1 if failures else 0)
