"""What an evaluation may conclude, and what it may only flag.

The previous scorer matched regexes against a finding's text and called every
hit a false claim. That is not a truth judgement, and it got the easiest case
exactly backwards: the sentence

    "The root cause is not established; there is no evidence of an outage."

is *exemplary* — it is the model declining to assert two things — and it was
scored as two false claims, because it contains the words "root cause" and
"outage". A rubric that punishes honesty rewards silence, and silence is the
one failure mode this product cannot afford.

So the contract here is three-way, and the three are never mixed:

  DETERMINISTIC  A fact about the output that needs no interpretation: how
                 many findings published, whether the analysis completed or
                 failed, whether every cited evidence id was actually
                 delivered, whether a numeric claim carries the metric
                 reference the contract requires. These can fail a scenario.

  REVIEW FLAG    A regex hit, in context. It says "a human should look at
                 this sentence", and it says nothing about whether the
                 sentence is true. Each flag carries the clause it matched
                 and whether that clause ASSERTS the thing, DENIES it, or
                 HEDGES it — which is the difference between the example
                 above and an actual false claim.

  REQUIRES       Everything else, including "did it discover the pattern?".
  REVIEW         No automated semantic judgement is made, because none is
                 implemented and a judge-model call is out of budget. The
                 result carries the rubric and the evidence a reviewer needs
                 and is labelled unresolved rather than guessed.

Two symmetric rules follow, and both were broken before:

  * The ABSENCE of a review flag is not proof of truth. A finding can be
    entirely wrong in words no regex anticipated.
  * The PRESENCE of an expected keyword is not proof of discovery. "approval"
    appears in "no approval step was involved" too.

Neither `discovered: True` nor `false_claims` exists here any more.
"""
from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# Reading a sentence in context
# ---------------------------------------------------------------------------

# Cues that the clause DENIES the thing it mentions.
_NEGATION = re.compile(
    r"\b(no|not|never|cannot|can't|couldn't|isn't|aren't|wasn't|weren't|don't|"
    r"doesn't|didn't|nothing|none|neither|nor|without|lacks?|lacking|absent|"
    r"un(?:established|proven|supported|confirmed|clear|known))\b",
    re.I,
)
# Cues that the clause does not commit to it.
_HEDGE = re.compile(
    r"\b(may|might|could|appears?|seems?|possibly|perhaps|unclear|unknown|"
    r"whether|suggests?|suggesting|consistent with|remains?|cannot be|"
    r"not established|would need|worth checking|investigate|check)\b",
    re.I,
)

_CLAUSE_SPLIT = re.compile(r"(?<=[.!?;:])\s+|\s+(?:—|--)\s+|,\s+(?=(?:and|but|though|although|while)\b)", re.I)


def clauses(text: str) -> list[str]:
    """Split into clause-sized pieces, so a negation on one side of a sentence
    does not silently excuse an assertion on the other."""
    parts = [p.strip() for p in _CLAUSE_SPLIT.split(text or "") if p and p.strip()]
    return parts or ([text.strip()] if (text or "").strip() else [])


def disposition(clause: str) -> str:
    """How the clause stands towards what it mentions.

    `negated` beats `hedged` beats `affirmative`. Only `affirmative` is a
    candidate for being an unsupported assertion — and even then this returns
    a flag, never a verdict.
    """
    if _NEGATION.search(clause):
        return "negated"
    if _HEDGE.search(clause):
        return "hedged"
    return "affirmative"


# ---------------------------------------------------------------------------
# The text a finding actually puts in front of a reader
# ---------------------------------------------------------------------------

def finding_fields(summary: dict[str, Any],
                   detail: dict[str, Any] | None = None) -> list[tuple[str, str]]:
    """Every (field, text) pair a reader can see, card AND evidence panel.

    Scoring only the card summary was its own hole: a claim's `text`, an
    evidence `note` and a next step live in the detail response, and a false
    assertion in any of them is published just the same.
    """
    out: list[tuple[str, str]] = []

    def add(field: str, value: Any) -> None:
        if isinstance(value, str) and value.strip():
            out.append((field, value))

    body = dict(summary or {})
    if detail:
        inner = detail.get("finding") or {}
        body = {**body, **{k: v for k, v in inner.items() if v is not None}}

    add("title", body.get("title"))
    add("explanation", body.get("explanation"))
    add("consequence", body.get("consequence"))
    for i, u in enumerate(body.get("uncertainty") or []):
        add(f"uncertainty[{i}]", u)
    step = body.get("next_step") or {}
    add("next_step.text", step.get("text"))

    source = detail or body
    for i, cl in enumerate(source.get("claims") or []):
        add(f"claims[{i}].text", (cl or {}).get("text"))
    for i, ev in enumerate(source.get("evidence") or []):
        add(f"evidence[{i}].note", (ev or {}).get("note"))
    return out


# ---------------------------------------------------------------------------
# Execution status — failure, incompleteness and abstention are not the same
# ---------------------------------------------------------------------------

EXECUTIONS = (
    "completed",     # an analysis ran and finished its candidates
    "incomplete",    # it ran but could not finish something it started
    "failed",        # it did not reach a decision at all
    "unavailable",   # it never ran (no model configured, scope changed)
    "skipped",       # the harness declined to run it (budget)
    "unknown",
)


def classify_execution(*, analysis: dict[str, Any] | None,
                       worker_reports: list[dict[str, Any]] | None) -> dict[str, Any]:
    """What happened to the pipeline, from the product's own reporting.

    Deliberately NOT inferred from "were there findings". An empty response
    after a failure is not an abstention, and counting it as one would let a
    broken pipeline score as admirable restraint.
    """
    analysis = analysis or {}
    reports = worker_reports or []
    state = analysis.get("state")
    outcome = analysis.get("analysis_outcome")
    gaps = list(analysis.get("completion_gaps") or [])
    statuses = [r.get("status") for r in reports]

    if state == "unavailable":
        execution = "unavailable"
    elif any(s in ("error", "failed") for s in statuses) or state == "failed":
        execution = "failed"
    elif state in ("queued", "running"):
        execution = "unknown"
    elif outcome == "complete" and not gaps:
        execution = "completed"
    elif outcome is None and not reports:
        execution = "unknown"
    elif gaps or outcome not in (None, "complete"):
        execution = "incomplete"
    else:
        execution = "completed"

    return {
        "execution": execution,
        "analysis_state": state,
        "analysis_outcome": outcome,
        "completion_gaps": gaps,
        "reason": analysis.get("reason"),
        "worker_statuses": statuses,
        # Abstention is only meaningful when the run actually finished.
        "abstained": execution == "completed" and not analysis.get("_published"),
    }


# ---------------------------------------------------------------------------
# Deterministic checks
# ---------------------------------------------------------------------------

def deterministic_checks(spec: dict[str, Any], findings: list[dict[str, Any]],
                         details: list[dict[str, Any]], *,
                         delivered_evidence: set[str] | None = None,
                         restricted: bool = False) -> list[dict[str, Any]]:
    """Facts about the output that need no interpretation.

    These are the only things here allowed to FAIL a scenario on their own.
    """
    out: list[dict[str, Any]] = []

    def record(name: str, ok: bool, detail: str = "") -> None:
        out.append({"check": name, "ok": bool(ok), "detail": detail})

    for i, f in enumerate(findings):
        d = details[i] if i < len(details) else None
        body = (d or {}).get("finding") or f

        kinds = [(c or {}).get("kind") for c in ((d or {}).get("claims") or [])]
        record(f"finding[{i}] every claim declares its kind",
               all(k in ("observation", "calculation", "hypothesis") for k in kinds)
               if kinds else True,
               f"kinds={kinds}")

        numeric = [c for c in ((d or {}).get("claims") or [])
                   if (c or {}).get("value") is not None]
        record(f"finding[{i}] numeric claims cite an authoritative source",
               all(str((c.get("metric_ref") or "")).startswith(("calc:", "snapshot:"))
                   for c in numeric),
               f"{len(numeric)} numeric claim(s)")

        if delivered_evidence is not None:
            refs = {f"{(e or {}).get('kind')}:{(e or {}).get('ref')}"
                    for e in ((d or {}).get("evidence") or [])}
            unknown = sorted(r for r in refs if r not in delivered_evidence)
            record(f"finding[{i}] cites only delivered evidence",
                   not unknown, f"unknown={unknown}")

        if restricted:
            # A monetary FIGURE shown to a reader whose seat excludes the
            # financial surface is a policy breach whatever the sentence around
            # it says. The money-flavoured WORDS are a review flag instead,
            # because "no cost information is available to you" is correct.
            money = [(field, text) for field, text in finding_fields(f, d)
                     if re.search(r"[$£€]\s?\d|\b\d+(?:\.\d+)?\s?(usd|dollars?)\b",
                                  text, re.I)]
            record(f"finding[{i}] shows no monetary figure to a restricted reader",
                   not money, f"{money[:2]}")

    if restricted:
        record("no financial finding reached the restricted reader at all",
               all(not (((details[i] or {}).get("finding") or f).get("requires_financial"))
                   for i, f in enumerate(findings)))
    return out


# ---------------------------------------------------------------------------
# Review flags
# ---------------------------------------------------------------------------

def review_flags(spec: dict[str, Any], findings: list[dict[str, Any]],
                 details: list[dict[str, Any]], *,
                 restricted: bool = False) -> list[dict[str, Any]]:
    """Regex hits, each carried with the clause and how that clause stands.

    A flag is an invitation to read a sentence. It is not a finding of
    falsity, and `disposition` is the reason: the same pattern in
    "an outage caused this" and "there is no evidence of an outage" means
    opposite things, and only the first is worth a reviewer's alarm.
    """
    rules = spec.get("restricted_reader") if restricted else spec
    patterns = (rules or {}).get("wrong") or []
    flags: list[dict[str, Any]] = []
    for i, f in enumerate(findings):
        d = details[i] if i < len(details) else None
        for field, text in finding_fields(f, d):
            for pat in patterns:
                for cl in clauses(text):
                    if re.search(pat, cl, re.I):
                        flags.append({
                            "finding_index": i,
                            "finding_title": f.get("title"),
                            "field": field,
                            "pattern": pat,
                            "clause": cl.strip()[:240],
                            "disposition": disposition(cl),
                        })
    return flags


def expectation_signals(spec: dict[str, Any], findings: list[dict[str, Any]],
                        details: list[dict[str, Any]]) -> dict[str, Any]:
    """Which rubric expectations appear somewhere in the output.

    A SIGNAL, not a verdict. The word "approval" turning up proves that the
    word turned up; whether the finding states the pattern the records
    establish is a reading task, and it is left to one.
    """
    expected = spec.get("acceptable") or []
    hits, misses = [], []
    blob = " ".join(text for i, f in enumerate(findings)
                    for _, text in finding_fields(
                        f, details[i] if i < len(details) else None))
    for pat in expected:
        (hits if re.search(pat, blob, re.I) else misses).append(pat)
    return {"expected": expected, "matched": hits, "unmatched": misses,
            "note": "Lexical overlap only. Neither a hit nor a miss decides "
                    "whether the pattern was discovered."}


# ---------------------------------------------------------------------------
# Putting it together
# ---------------------------------------------------------------------------

DISCOVERY_RUBRIC = (
    "Read the published findings against this scenario's `establishes` and "
    "`not_established` lists. Discovery means the finding states a pattern the "
    "records support and that a reader could not already see in the snapshot "
    "totals. It is NOT satisfied by containing the expected words, and it is "
    "NOT refuted by paraphrasing them. Where the scenario prefers abstention, "
    "publishing is acceptable only if the finding adds something beyond the "
    "visible totals."
)


DIAGNOSES = {
    "published": "Findings reached the reader; judge them against the rubric.",
    "no_candidate_raised": "Discovery proposed nothing to investigate.",
    "investigated_and_withheld": "A candidate was investigated and not published; "
                                 "the abstention reasons say why.",
    "draft_blocked_by_validation": "A draft existed and the deterministic "
                                   "validator refused it. Read `rejected`.",
    "required_evidence_unreachable": "The evidence this scenario turns on did "
                                     "not reach the investigation.",
    "pipeline_failed_before_decision": "No decision was reached at all. This is "
                                       "not an abstention.",
    "not_run": "The harness declined to run this attempt.",
    "completed_without_publication": "It finished and published nothing, with "
                                     "no candidate, rejection or abstention "
                                     "recorded to explain it.",
}


def diagnose(*, status: dict[str, Any], worker_reports: list[dict[str, Any]] | None,
             published: int, evidence_delivered: bool | None = None) -> dict[str, Any]:
    """Which of the reviewer's questions this run answers.

    "Nothing was published" has at least five different causes and they call
    for five different responses. Collapsing them into one number is how an
    evaluation stops being useful.
    """
    reports = worker_reports or []
    execution = status.get("execution")
    rejected = [r for r in reports if r.get("rejected")]
    abstained = [r for r in reports if r.get("abstained")]
    candidates = sum(int(r.get("candidates") or 0) for r in reports)

    if execution == "skipped":
        code = "not_run"
    elif execution in ("failed", "unavailable", "unknown"):
        code = "pipeline_failed_before_decision"
    elif published:
        code = "published"
    elif rejected:
        code = "draft_blocked_by_validation"
    elif evidence_delivered is False:
        code = "required_evidence_unreachable"
    elif abstained:
        code = "investigated_and_withheld"
    elif candidates == 0:
        code = "no_candidate_raised"
    else:
        code = "completed_without_publication"

    return {
        "code": code,
        "meaning": DIAGNOSES[code],
        "candidates_raised": candidates,
        "validator_rejections": [r.get("rejected") for r in rejected],
        "abstention_reasons": [r.get("abstained") for r in abstained],
        "required_evidence_delivered": evidence_delivered,
    }


def assess(spec: dict[str, Any], findings: list[dict[str, Any]],
           details: list[dict[str, Any]] | None = None, *,
           analysis: dict[str, Any] | None = None,
           worker_reports: list[dict[str, Any]] | None = None,
           delivered_evidence: set[str] | None = None,
           evidence_delivered: bool | None = None,
           restricted: bool = False) -> dict[str, Any]:
    """One scenario's result: settled facts, flags to read, and open questions."""
    details = details or []
    status = classify_execution(analysis=analysis, worker_reports=worker_reports)
    status["abstained"] = status["execution"] == "completed" and not findings

    checks = deterministic_checks(spec, findings, details,
                                  delivered_evidence=delivered_evidence,
                                  restricted=restricted)
    flags = review_flags(spec, findings, details, restricted=restricted)
    signals = expectation_signals(spec, findings, details)

    failures = [c for c in checks if not c["ok"]]
    affirmative = [f for f in flags if f["disposition"] == "affirmative"]

    # The discovery question is never answered here.
    if status["execution"] in ("failed", "unavailable", "skipped", "unknown"):
        discovery = "not_applicable"
        note = (f"No completed analysis to judge (execution={status['execution']}). "
                "This is not an abstention.")
    elif not findings:
        discovery = "requires_review"
        note = ("Nothing was published by a completed analysis. Whether that is "
                "correct restraint or a missed pattern is a reading task; the "
                "scenario's `abstention_ok` says which is expected.")
    else:
        discovery = "requires_review"
        note = "Findings were published; judge them against the rubric."

    return {
        "key": spec["key"],
        "name": spec["name"],
        "status": status,
        "diagnosis": diagnose(status=status, worker_reports=worker_reports,
                              published=len(findings),
                              evidence_delivered=evidence_delivered),
        "published": len(findings),
        "titles": [f.get("title") for f in findings],
        "deterministic": {"checks": checks, "failures": failures,
                          "passed": len(checks) - len(failures)},
        "review_flags": flags,
        "affirmative_flag_count": len(affirmative),
        "expectation_signals": signals,
        "discovery": discovery,
        "discovery_note": note,
        "discovery_rubric": DISCOVERY_RUBRIC,
        "abstention_expected": bool(spec.get("abstention_ok")),
        "abstention_preferred": bool(spec.get("abstention_preferred")),
        "establishes": spec.get("establishes"),
        "not_established": spec.get("not_established"),
        # A scenario fails automatically ONLY on a deterministic check.
        # Everything else waits for a person.
        "automated_verdict": "fail" if failures else "requires_review",
    }
