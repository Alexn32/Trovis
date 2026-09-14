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

    runs = []
    for attempt in (1, 2):
        before = meter.snapshot()
        ctx = S.build_instance(c, "B", instance=attempt)
        eval_stub_model.reset()
        runs.append(R.run_one_scenario(
            c, S, ctx, analysis_jobs, investigator,
            attempt=attempt, meter=meter, usage_before=before,
            database=database, recorder=recorder))

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
                               database=database, recorder=recorder)
    second = R.run_one_scenario(c, S, same, analysis_jobs, investigator,
                                attempt=2, meter=meter, usage_before=meter.snapshot(),
                                database=database, recorder=recorder)
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
    got = []
    for scen in ("A", "H"):
        for attempt in (1, 2):
            before = m2.snapshot()
            ctx = S.build_instance(c, scen, instance=100 + attempt)
            eval_stub_model.reset()
            got.append(R.run_one_scenario(
                c, S, ctx, analysis_jobs, investigator, attempt=attempt,
                meter=m2, usage_before=before,
                database=database, recorder=rec2))
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

CLIENT.__exit__(None, None, None)

print("\n" + ("FAILED: " + "; ".join(failures) if failures else "ALL PASS"))
raise SystemExit(1 if failures else 0)
