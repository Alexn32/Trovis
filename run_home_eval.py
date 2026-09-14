"""Run the Home findings evaluation against the real investigation pipeline.

    snapshot  ->  queued analysis  ->  retrieval tools  ->  validation
              ->  publication      ->  the Home read path

Nothing is short-circuited: the scenarios in `home_eval_scenarios.py` are
seeded as ordinary work records, the read enqueues, the real worker drains the
queue, and what the reader would see is read back through `GET /home/findings`.

**It does not call a model unless you say so twice.** The default mode makes no
network request at all. `--mode live` additionally requires
`--yes-spend-money`, because this costs real money against a real key and a
flag that can be set by accident is not a decision.

    # what would run, and under what limits — no calls
    python3 run_home_eval.py

    # plumbing check: the whole pipeline, with a SCRIPTED model.
    # Proves the harness works. Says NOTHING about model quality.
    python3 run_home_eval.py --mode stub

    # the real thing
    ANTHROPIC_API_KEY=sk-... python3 run_home_eval.py \\
        --mode live --yes-spend-money --scenarios B,F,I --repeat 2 \\
        --max-usd 5

**Every repetition is a separate investigation.** `--repeat 2` seeds two
equivalent-but-distinct fixture accounts and runs each one, because a second
read of the SAME account correctly returns the cached analysis — the product
declining to re-analyse an unchanged audience is right, and reading that cache
again measures nothing. No production debounce or freshness behaviour is
bypassed; the repetition is a different audience, not a forced refresh.

Every run writes a JSON transcript carrying the worker's own report —
candidates, rejections, abstentions, coverage limitations, completion gaps and
per-attempt usage — so a reviewer can tell a missed pattern from unreachable
evidence from a blocked draft.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile
import time
from datetime import datetime, timezone

DEFAULTS = {
    "TROVIS_INVESTIGATION_WALL_S": "240",
    "TROVIS_INVESTIGATION_TIMEOUT_S": "60",
}


class BudgetExceeded(RuntimeError):
    """A request was not sent because it could have taken spend past the cap."""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["plan", "stub", "live"], default="plan")
    ap.add_argument("--yes-spend-money", action="store_true",
                    help="Required with --mode live. There is no default that spends.")
    ap.add_argument("--scenarios", default="", help="Comma-separated keys (A-I).")
    ap.add_argument("--repeat", type=int, default=1,
                    help="Independent runs per scenario, each on its own fixture.")
    ap.add_argument("--max-calls", type=int, default=60,
                    help="Hard ceiling on model calls for the whole run.")
    ap.add_argument("--max-usd", type=float, default=5.0,
                    help="Estimated-cost ceiling. Checked BEFORE each request.")
    ap.add_argument("--wall-s", type=float, default=240.0)
    ap.add_argument("--out", default="eval_out")
    args = ap.parse_args()

    if args.mode == "live" and not args.yes_spend_money:
        print("refusing: --mode live needs --yes-spend-money as well.")
        return 2
    for name, value in (("--max-calls", args.max_calls), ("--max-usd", args.max_usd),
                        ("--repeat", args.repeat), ("--wall-s", args.wall_s)):
        if not _finite_positive(value):
            print(f"refusing: {name}={value!r} is not a finite positive number.")
            return 2

    os.environ.setdefault("TROVIS_DISABLE_PRICING_SYNC", "1")
    os.environ.setdefault("OVERSEE_DISABLE_PRICING_SYNC", "1")
    os.environ["TROVIS_DISABLE_ALERTS"] = "1"
    os.environ["TROVIS_DISABLE_LOOP_SWEEP"] = "1"
    os.environ["TROVIS_LOOP_TITLES"] = "off"
    # The in-process worker is off; this script drains the queue itself so a
    # run is deterministic and every job is attributable to an attempt.
    os.environ["TROVIS_DISABLE_ANALYSIS"] = "1"
    os.environ["TROVIS_INVESTIGATION_WALL_S"] = str(args.wall_s)
    for k, v in DEFAULTS.items():
        os.environ.setdefault(k, v)
    os.environ.pop("DATABASE_URL", None)  # never a shared or production database

    key = os.environ.get("ANTHROPIC_API_KEY") or ""
    if args.mode == "plan":
        os.environ.pop("ANTHROPIC_API_KEY", None)
    elif args.mode == "stub":
        # `investigate` refuses to run without a key — correctly, since "we
        # cannot analyse" and "we analysed and found nothing" are different
        # answers. The stub replaces `_client` outright, so this placeholder is
        # never read and never leaves the process.
        os.environ["ANTHROPIC_API_KEY"] = "stub-not-a-real-key"

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    import database
    database.SQLITE_PATH = tmp.name

    import analysis_jobs
    import investigator
    import main as app_main
    from fastapi.testclient import TestClient
    import home_eval_scenarios as S

    app_main._auto_describe = lambda *a, **k: False

    keys = [k.strip().upper() for k in args.scenarios.split(",") if k.strip()] or \
        [s["key"] for s in S.SCENARIOS]

    plan = {
        "mode": args.mode,
        "scenarios": keys,
        "repeat": args.repeat,
        "fixture_schema": S.FIXTURE_SCHEMA,
        "fixture_version": S.fixture_version(),
        "model": investigator.MODEL,
        "prompt_version": investigator.PROMPT_VERSION,
        "limits": {
            "max_model_calls": args.max_calls,
            "max_estimated_usd": args.max_usd,
            "wall_clock_s_per_investigation": args.wall_s,
            "call_timeout_s": investigator.call_timeout_s(),
            "max_candidates": investigator.MAX_CANDIDATES,
            "max_investigation_turns": investigator.MAX_INVESTIGATION_TURNS,
            "max_findings_published": investigator.MAX_FINDINGS_PUBLISHED,
            "tool_budget": investigation_budget(),
            "provider_retries": 0,
        },
        "database": tmp.name,
        "api_key_present": bool(key),
        "python": sys.version.split()[0],
    }
    print(json.dumps(plan, indent=2))

    if args.mode == "plan":
        print("\nplan only — no model was called and no records were seeded.")
        if not key:
            print("NOTE: ANTHROPIC_API_KEY is not set, so --mode live would fail.")
        return 0

    if args.mode == "live" and not key:
        print("\nrefusing: --mode live needs ANTHROPIC_API_KEY. "
              "Nothing was run and nothing was spent.")
        return 2

    meter = Meter(max_calls=args.max_calls, max_usd=args.max_usd,
                  database=database, model=investigator.MODEL,
                  priced=(args.mode == "live"))
    if args.mode == "live" and not meter.pricing_known():
        print(f"\nrefusing: no price is known for {investigator.MODEL}, so a "
              "dollar budget cannot be enforced. Nothing was run and nothing "
              "was spent. Set a price in the pricing table, or run with "
              "--mode stub.")
        return 2

    if args.mode == "stub":
        import eval_stub_model
        investigator._client = lambda: meter.wrap(eval_stub_model.client())
    else:
        investigator._client = lambda: meter.wrap(_live_client(investigator))

    # Test-only: capture what each investigation actually delivered.
    import investigation_tools
    recorder = DeliveryRecorder(investigation_tools)
    recorder.install()
    ledger = JobLedger(analysis_jobs, meter=meter, recorder=recorder,
                       database=database)
    ledger.install()

    started = time.time()
    results = []
    with TestClient(app_main.app) as c:
        for key_ in keys:
            for attempt in range(1, args.repeat + 1):
                before = meter.snapshot()
                if meter.spent_out():
                    results.append(skipped_result(
                        key_, attempt, S, reason=meter.stop_reason()))
                    print(f"  {key_}#{attempt}: SKIPPED — {meter.stop_reason()}")
                    continue
                # A fresh, equivalent fixture per attempt. This is what makes
                # the repetition an independent experiment.
                ctx = S.build_instance(c, key_, instance=attempt)
                if args.mode == "stub":
                    import eval_stub_model
                    eval_stub_model.reset()
                results.append(run_one_scenario(
                    c, S, ctx, analysis_jobs, investigator,
                    attempt=attempt, meter=meter, usage_before=before,
                    database=database, recorder=recorder, ledger=ledger))

    ledger.uninstall()
    recorder.uninstall()
    attempt_totals = _sum_usage([r.get("usage_attempt_total") or {}
                                 for r in results])
    usage = meter.report()
    executions = ledger.executions
    by_execution = _sum_usage([ex["usage"] for ex in executions])
    unowned = [ex for ex in executions if ex["ownership"] == "unknown"]
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "plan": plan,
        "usage": usage,
        # Every model call must land in exactly one attempt. A non-zero
        # `unattributed` is a harness defect, not a rounding detail.
        "usage_reconciliation": _reconcile_usage(
            usage, attempt_totals, by_execution, executions, unowned),
        "job_executions": [_execution_summary(ex) for ex in executions],
        "elapsed_s": round(time.time() - started, 1),
        "results": results,
    }
    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, f"home-eval-{int(started)}.json")
    with open(path, "w") as fh:
        json.dump(report, fh, indent=2, default=str)
    print_summary(report, args.mode)
    print(f"\ntranscript: {path}")
    return 0


def _finite_positive(value) -> bool:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(v) and v > 0


def _live_client(investigator):
    """The real client, with provider retries OFF.

    An SDK that silently retries turns one counted call into three uncounted
    requests, and a ceiling that does not bound requests is not a ceiling.
    """
    import anthropic
    return anthropic.Anthropic(
        api_key=investigator._api_key(),
        timeout=investigator.call_timeout_s(),
        max_retries=0,
    )


def investigation_budget() -> dict[str, int]:
    import investigation_tools
    b = investigation_tools.ToolBudget()
    return {"max_calls": b.max_calls, "max_rows": b.max_rows,
            "max_events": b.max_events}


def skipped_result(key: str, attempt: int, S, *, reason: str) -> dict:
    """An explicit non-result. Never a copy of an earlier attempt's findings."""
    spec = S.scenario(key)
    return {
        "key": key, "name": spec["name"], "attempt": attempt,
        "execution": "skipped", "skipped": reason,
        "fixture": None, "analysis": None, "worker_reports": [],
        "findings": [], "evidence": [], "usage": None,
        "assessment": {"key": key, "name": spec["name"], "published": 0,
                       "status": {"execution": "skipped"},
                       "discovery": "not_applicable",
                       "discovery_note": f"not run: {reason}",
                       "automated_verdict": "not_run"},
    }


def _read_for(c, S, database, analysis_jobs, *, token: str, account_id: int,
              viewer_user_id, meter, recorder, ledger, q: str) -> dict:
    """One READER's investigation: enqueue, drain, read back.

    Usage and delivery come from the JOB EXECUTIONS this reader owns, not from
    a clock window around the drain. A drain runs whatever is queued, so the
    window included other fixtures' jobs; the executions know which job they
    were, and the job record knows whose it is.
    """
    window_before = meter.snapshot()
    ledger_mark = ledger.mark() if ledger else 0
    first = c.get(f"/home/findings?{q}", headers=S.auth(token)).json()
    drained = analysis_jobs.drain(max_jobs=3)
    after = c.get(f"/home/findings?{q}", headers=S.auth(token)).json()

    findings = after.get("findings") or []
    details = [c.get(f"/home/findings/{f['id']}?{q}",
                     headers=S.auth(token)).json() for f in findings]

    executions = ledger.since(ledger_mark) if ledger else []
    mine_ex = JobLedger.owned_by(executions, account_id=account_id,
                                 viewer_user_id=viewer_user_id)
    mine_ids = {ex["job_id"] for ex in mine_ex}
    unknown_ex = [ex for ex in executions if ex["ownership"] == "unknown"]
    foreign_ex = [ex for ex in executions
                  if ex not in mine_ex and ex not in unknown_ex]

    owned = [d for d in drained if d.get("job_id") in mine_ids]
    foreign = [d for d in drained if d.get("job_id") not in mine_ids]

    window = meter.delta(window_before)
    accounted = _sum_usage([ex["usage"] for ex in executions])
    if ledger is None:
        # No per-job accounting available. Report the window and SAY SO, rather
        # than reporting zero (which under-states) or the window as though it
        # were attributed (which is the bug this replaced).
        usage = window
        attribution = "window_unattributed"
        outside = {"model_calls": 0}
    else:
        usage = _sum_usage([ex["usage"] for ex in mine_ex])
        attribution = "per_job_execution"
        outside = {k: window.get(k, 0) - accounted.get(k, 0)
                   for k in ("model_calls",)}

    return {
        "first": first, "after": after,
        "findings": findings, "details": details,
        "owned": owned, "foreign": foreign,
        "executions": mine_ex,
        "foreign_executions": [_execution_summary(ex) for ex in foreign_ex],
        "unattributed_executions": [_execution_summary(ex) for ex in unknown_ex],
        # Model calls that happened during this reader's window but inside no
        # job execution. Should be zero; reported rather than absorbed.
        "calls_outside_job_execution": outside["model_calls"],
        "usage_attribution": attribution,
        "delivery": _merge_delivery([ex.get("delivery") for ex in mine_ex]),
        "usage": usage,
        "window_usage": window,
    }


def _execution_summary(ex: dict) -> dict:
    ident = ex.get("identity") or {}
    return {
        "job_id": ex.get("job_id"),
        "ownership": ex.get("ownership"),
        "account_id": ident.get("account_id"),
        "viewer_user_id": ident.get("viewer_user_id"),
        "scope_key": ex.get("scope_key"),
        "analysis_id": ex.get("analysis_id"),
        "status": ex.get("status"),
        "error": ex.get("error"),
        "usage": ex.get("usage"),
        "tags": ex.get("tags"),
    }


def _merge_delivery(parts: list) -> dict:
    """One reader's delivery, from its own executions only.

    No execution at all is UNAVAILABLE. An execution that delivered nothing is
    OBSERVED EMPTY. The two stay apart: the first says we cannot tell what the
    model saw, the second says it saw nothing.
    """
    usable = [p for p in parts if p and p.get("available")]
    if not usable:
        reasons = [p.get("reason") for p in parts if p and p.get("reason")]
        return {"available": False,
                "reason": reasons[0] if reasons
                else "no owned job execution captured delivery"}
    keys: set[str] = set()
    calcs: set[str] = set()
    for p in usable:
        keys |= set(p.get("keys") or [])
        calcs |= set(p.get("calculations") or [])
    return {"available": True, "executions": len(usable),
            "keys": sorted(keys), "calculations": sorted(calcs),
            "observed_empty": not keys and not calcs,
            "note": "Union over THIS reader's own job executions only."}


def actual_evidence_delivered(S, ctx, delivery: dict) -> dict:
    """Did THIS investigation receive the evidence the scenario turns on?

    Derived from captured delivery and the scenario's explicit requirements —
    never from the independent probe, which answers a different question ("could
    an optimally targeted search have reached it?").

    Three outcomes, and the third is the one that was missing:
      * True  — every requirement appears in what was delivered.
      * False — something required was not delivered by this execution. That is
                a fact about THIS RUN, not about the tools: a model may simply
                not have asked.
      * None  — the instrumentation could not establish it.

    A requirement the tools cannot satisfy at all (scenario E's repeated
    successful tool calls) is reported separately as `unretrievable`, because
    "the model did not fetch it" and "no tool exposes it" call for opposite
    responses.
    """
    spec = ctx["spec"]
    required = S.required_keys(ctx)
    unretrievable = []
    if spec.get("pattern_retrievable") is False:
        unretrievable.append({
            "requirement": "pattern:" + spec["key"],
            "reason": spec.get("pattern_note") or "no tool exposes this pattern",
        })
    if not delivery or not delivery.get("available"):
        return {
            "value": None,
            "required": required,
            "unretrievable": unretrievable,
            "reason": (delivery or {}).get("reason")
                      or "no delivery instrumentation for this investigation",
        }
    keys = set(delivery.get("keys") or [])
    calcs = set(delivery.get("calculations") or [])
    missing = []
    for need in required:
        if need.startswith("run:"):
            if need not in keys:
                missing.append(need)
        elif need == "mix" and not any(k.startswith("mix.") for k in calcs):
            missing.append(need)
        elif need == "cost" and not any(k.startswith("cost.") for k in calcs):
            missing.append(need)
        elif need == "waits" and not any(k.startswith("wait.") for k in calcs):
            missing.append(need)
        elif need == "agent" and not any(k.startswith("agent_context:")
                                         for k in keys):
            missing.append(need)
    satisfied = not missing and not unretrievable
    return {
        "value": bool(satisfied),
        "required": required,
        "delivered_keys": len(keys),
        "delivered_calculations": len(calcs),
        "missing": missing,
        "unretrievable": unretrievable,
        "reason": (
            "every requirement was delivered to this investigation" if satisfied
            else ("requirements this run did not retrieve: " + ", ".join(missing)
                  if missing else "")
            + ("; requirements no tool can satisfy: "
               + ", ".join(u["requirement"] for u in unretrievable)
               if unretrievable else "")
        ),
    }


def run_one_scenario(c, S, ctx, analysis_jobs, investigator, *,
                     attempt: int, meter, usage_before: dict,
                     database=None, recorder=None, ledger=None) -> dict:
    """One fixture instance, through the whole real path, with its provenance."""
    key = ctx["key"]
    spec = ctx["spec"]
    token = ctx["token"]
    q = "days=7&tz=UTC"

    if ledger is not None:
        ledger.tags = {"scenario": key, "attempt": attempt,
                       "fixture_id": ctx["fixture_id"], "reader": "primary"}
    snap = c.get(f"/home/snapshot?{q}", headers=S.auth(token)).json()
    primary = _read_for(c, S, database, analysis_jobs, token=token,
                        account_id=ctx["account_id"],
                        viewer_user_id=ctx["user_id"], meter=meter,
                        recorder=recorder, ledger=ledger, q=q)
    first, after = primary["first"], primary["after"]
    findings, details = primary["findings"], primary["details"]
    mine, foreign = primary["owned"], primary["foreign"]

    analysis = after.get("analysis") or {}
    discoverability = _discoverability(S, ctx)
    # From what THIS investigation received — never from the probe, which
    # answers "could an optimally targeted search have reached it?".
    actual = actual_evidence_delivered(S, ctx, primary["delivery"])
    assessment = S.assess(
        spec, findings, details,
        analysis=analysis, worker_reports=mine,
        delivery=primary["delivery"],
        evidence_delivered=actual["value"],
        evidence_unretrievable=bool(actual["unretrievable"]),
    )

    usage = dict(primary["usage"])
    out = {
        "key": key, "name": spec["name"], "attempt": attempt,
        "fixture": {
            "fixture_id": ctx["fixture_id"],
            "schema": ctx["fixture_schema"],
            "version": ctx["fixture_version"],
            "account_id": ctx["account_id"],
            "reader_user_id": ctx["user_id"],
            "reader_email": ctx["email"],
            "seeded_ids": ctx["ids"],
        },
        "provenance": {
            "model": investigator.MODEL,
            "prompt_version": investigator.PROMPT_VERSION,
            "analysis_ids": [d.get("analysis_id") for d in mine],
            "job_ids": [d.get("job_id") for d in mine],
            "scope_keys": sorted({d.get("scope_key") for d in mine if d.get("scope_key")}),
            "enqueued_on_first_read": (first.get("analysis") or {}).get("enqueued"),
            "jobs_drained": len(mine),
            "jobs_drained_other_accounts": len(foreign),
            "foreign_job_ids": [d.get("job_id") for d in foreign],
            "model_calls_this_attempt": usage["model_calls"],
        },
        "delivery": primary["delivery"],
        "actual_evidence_delivered": actual,
        "usage_attribution": primary["usage_attribution"],
        "job_executions": [_execution_summary(ex) for ex in primary["executions"]],
        "foreign_job_executions": primary["foreign_executions"],
        "unattributed_job_executions": primary["unattributed_executions"],
        "calls_outside_job_execution": primary["calls_outside_job_execution"],
        "execution": assessment["status"]["execution"],
        "analysis": {
            "state": analysis.get("state"),
            "outcome": analysis.get("analysis_outcome"),
            "reason": analysis.get("reason"),
            "completion_gaps": analysis.get("completion_gaps"),
            "findings_from_previous_analysis":
                analysis.get("findings_from_previous_analysis"),
            "newer_evidence_available": analysis.get("newer_evidence_available"),
            "analyzed_evidence_version": analysis.get("analyzed_evidence_version"),
        },
        # FINDING 5: the worker's own report, kept rather than reduced to a
        # status line. This is what lets a reviewer separate "missed it" from
        # "could not reach the evidence" from "said it and was refused".
        "worker_reports": [_worker_report(d) for d in mine],
        "findings": [_finding_summary(f) for f in findings],
        "evidence": [_finding_detail(d) for d in details],
        "discoverability": discoverability,
        "snapshot_agreement": snapshot_agreement(snap, findings),
        "usage": usage,
        "assessment": assessment,
    }

    if key == "H":
        # The restricted reader is a DIFFERENT audience with its own scope key,
        # its own queued job and its own model calls. Those calls used to be
        # spent against the global meter and recorded nowhere, which is why the
        # global total exceeded the sum of the attempts.
        r = ctx["restricted"]
        if ledger is not None:
            ledger.tags = {"scenario": key, "attempt": attempt,
                           "fixture_id": ctx["fixture_id"], "reader": "restricted"}
        reader = _read_for(c, S, database, analysis_jobs, token=r["token"],
                           account_id=ctx["account_id"],
                           viewer_user_id=r["user_id"], meter=meter,
                           recorder=recorder, ledger=ledger, q=q)
        out["restricted_reader"] = {
            "user_id": r["user_id"],
            "published": len(reader["findings"]),
            "titles": [f.get("title") for f in reader["findings"]],
            "worker_reports": [_worker_report(d) for d in reader["owned"]],
            "job_ids": [d.get("job_id") for d in reader["owned"]],
            "delivery": reader["delivery"],
            "actual_evidence_delivered": actual_evidence_delivered(
                S, ctx, reader["delivery"]),
            "job_executions": [_execution_summary(ex)
                               for ex in reader["executions"]],
            "usage": reader["usage"],
            "assessment": S.assess(
                spec, reader["findings"], reader["details"],
                analysis=reader["after"].get("analysis") or {},
                worker_reports=reader["owned"],
                delivery=reader["delivery"], restricted=True,
                evidence_delivered=actual_evidence_delivered(
                    S, ctx, reader["delivery"])["value"]),
        }
        out["usage_readers"] = [
            {"reader": "primary", "user_id": ctx["user_id"], "usage": usage},
            {"reader": "restricted", "user_id": r["user_id"],
             "usage": reader["usage"]},
        ]

    # ATTEMPT TOTAL = the primary reader plus every other reader investigated
    # under this attempt. `usage` above is the PRIMARY SUBTOTAL only; consumers
    # summing the report must use `usage_attempt_total` and must not add the
    # subtotals to it as well.
    readers = out.get("usage_readers") or [
        {"reader": "primary", "user_id": ctx["user_id"], "usage": usage}]
    out["usage_readers"] = readers
    out["usage_attempt_total"] = _sum_usage([r["usage"] for r in readers])
    out["usage_accounting_note"] = (
        "`usage` is the primary reader's subtotal. `usage_attempt_total` is "
        "this attempt's total across all readers. Sum `usage_attempt_total` "
        "across attempts to reconcile with the report's global usage; never "
        "add the per-reader subtotals on top of it."
    )
    return out


def _reconcile_usage(usage, attempt_totals, by_execution, executions,
                     unowned) -> dict:
    """Reconcile on every dimension, not just the call count.

    Equal call totals can hide wrong ownership — which is exactly what
    happened: scenario A reported another fixture's five calls as its own and
    the arithmetic still balanced. So this compares calls, reported tokens,
    measured cost and outstanding reservations, AND reports how many executions
    could not be attributed at all.
    """
    problems = []
    dims = {}
    for field, label in (("model_calls", "model calls"),
                         ("input_tokens", "reported input tokens"),
                         ("output_tokens", "reported output tokens"),
                         ("estimated_usd", "estimated cost")):
        glob = usage.get({"estimated_usd": "estimated_usd"}.get(field, field), 0)
        if field == "estimated_usd":
            glob = usage.get("estimated_usd", 0.0)
        attempts = attempt_totals.get(field, 0)
        per_job = by_execution.get(field, 0)
        ok = abs(glob - per_job) < 1e-6 and abs(attempts - per_job) < 1e-6
        dims[field] = {"global": glob, "sum_of_attempt_totals": attempts,
                       "sum_of_job_executions": per_job, "reconciles": ok}
        if not ok:
            problems.append(
                f"{label}: global={glob} attempts={attempts} "
                f"executions={per_job} — these must agree")
    if unowned:
        problems.append(
            f"{len(unowned)} job execution(s) could not be attributed to any "
            "account; their usage is reported under `unattributed`.")
    return {
        "dimensions": dims,
        "job_executions": len(executions),
        "unattributed_executions": len(unowned),
        "unattributed_usage": _sum_usage([ex["usage"] for ex in unowned]),
        "outstanding_reservations":
            usage.get("of_which_unreconciled_reservations", 0.0),
        "reconciles": not problems,
        "problems": problems,
        "note": ("Usage is attributed per JOB EXECUTION and rolled up to the "
                 "reader that owns the job, then to the attempt. Attempt "
                 "totals already include every reader; do not add the "
                 "per-reader subtotals on top."),
    }


# The shape every usage total has, so "no executions" reads as zero rather
# than as an empty dict a consumer has to guess at.
USAGE_FIELDS = ("model_calls", "input_tokens", "output_tokens", "usage_missing",
                "failed_calls", "measured_calls", "counted_input_tokens",
                "count_requests", "estimated_usd")


def _sum_usage(parts: list[dict]) -> dict:
    total: dict[str, Any] = {k: 0 for k in USAGE_FIELDS}
    for part in parts:
        for k, v in (part or {}).items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                total[k] = round(total.get(k, 0) + v, 6)
    return total


def job_identity(database, job_id) -> dict | None:
    """Who a drained job belongs to, from the job record itself.

    Account is not enough: scenario H has two readers with different scopes in
    the SAME account, so `viewer_user_id` — which `analysis_jobs.request`
    carries — is what separates them. A job whose row cannot be read resolves
    to None and is attributed to nobody.
    """
    if job_id is None:
        return None
    try:
        with database._connect() as conn, database._cursor(conn) as cur:
            cur.execute(
                "SELECT account_id, scope_key, request FROM analysis_jobs "
                f"WHERE id = {database.PH}", (int(job_id),))
            row = cur.fetchone()
    except Exception:
        return None
    if not row or row["account_id"] is None:
        return None
    try:
        request = json.loads(row["request"] or "{}")
    except (TypeError, ValueError):
        request = {}
    return {
        "job_id": int(job_id),
        "account_id": int(row["account_id"]),
        "viewer_user_id": request.get("viewer_user_id"),
        "scope_key": row["scope_key"] or request.get("scope_key"),
    }


def job_owner(database, job_id) -> int | None:
    """Which account a drained job belongs to, from the job record itself.

    Worker reports routinely omit `account_id`, and the previous version read a
    missing one as "ours" — so any job that happened to drain during an attempt
    was attributed to it. `analysis_jobs.account_id` is the authoritative
    answer; an id that cannot be resolved is attributed to NOBODY.
    """
    if job_id is None:
        return None
    try:
        with database._connect() as conn, database._cursor(conn) as cur:
            cur.execute(
                f"SELECT account_id FROM analysis_jobs WHERE id = {database.PH}",
                (int(job_id),),
            )
            row = cur.fetchone()
    except Exception:
        return None
    return int(row["account_id"]) if row and row["account_id"] is not None else None


class JobLedger:
    """What each JOB EXECUTION cost and was shown — test-only.

    The previous accounting took a meter delta around `drain(max_jobs=3)` and
    called the whole thing the draining reader's usage. A drain runs whatever
    is queued, so a second fixture's job executing inside that window was
    billed to the reader that happened to trigger the drain: A would correctly
    list B's job as foreign and still report B's five calls as its own. The
    totals reconciled and the ownership was wrong, which is the worst shape a
    number can have.

    So the unit of accounting is one execution of `analysis_jobs.run_one()` —
    the function that claims exactly one job and runs the investigation for it.
    Everything spent inside that call belongs to that job, including failures,
    requeues and the token-count requests the meter makes. A call that happens
    outside any execution is recorded as unattributed rather than given to
    whoever was nearby.
    """

    def __init__(self, analysis_jobs, *, meter, recorder, database):
        self._mod = analysis_jobs
        self._original = analysis_jobs.run_one
        self._meter = meter
        self._recorder = recorder
        self._db = database
        self.executions: list[dict] = []
        self.tags: dict = {}
        self.installed = False

    def install(self) -> None:
        ledger = self
        original = self._original

        def run_one():
            before = ledger._meter.snapshot()
            mark = ledger._recorder.mark() if ledger._recorder else 0
            report = None
            error = None
            try:
                report = original()
            except Exception as exc:          # a crashed job still spent money
                error = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                if report is not None or error is not None:
                    ledger._record(report, error, before, mark)
            return report

        self._mod.run_one = run_one
        self.installed = True

    def uninstall(self) -> None:
        self._mod.run_one = self._original
        self.installed = False

    def _record(self, report, error, before, mark) -> None:
        job_id = (report or {}).get("job_id")
        identity = job_identity(self._db, job_id)
        usage = self._meter.delta(before)
        delivery = (self._recorder.window(mark, identity["account_id"])
                    if self._recorder and identity
                    else (self._recorder.window(mark) if self._recorder else None))
        self.executions.append({
            "job_id": job_id,
            "identity": identity,
            "ownership": "resolved" if identity else "unknown",
            "status": (report or {}).get("status") or ("crashed" if error else None),
            "analysis_id": (report or {}).get("analysis_id"),
            "scope_key": (report or {}).get("scope_key")
                         or (identity or {}).get("scope_key"),
            "error": error or (report or {}).get("error"),
            "usage": usage,
            "delivery": delivery,
            "tags": dict(self.tags),
        })

    def mark(self) -> int:
        return len(self.executions)

    def since(self, mark: int) -> list[dict]:
        return self.executions[mark:]

    @staticmethod
    def owned_by(executions, *, account_id, viewer_user_id) -> list[dict]:
        """Executions belonging to ONE reader — account AND viewer."""
        out = []
        for ex in executions:
            ident = ex.get("identity") or {}
            if (ident.get("account_id") == account_id
                    and ident.get("viewer_user_id") == viewer_user_id):
                out.append(ex)
        return out


class DeliveryRecorder:
    """Test-only instrumentation: what each investigation actually delivered.

    `InvestigationSession.delivered` is the set of ledger keys the model was
    really shown. Nothing in the product surfaces it after the fact, and the
    two things that look like substitutes are not:

      * `publication.records[].evidence` is what the finding CITES. Comparing
        citations against it checks that the publication agrees with itself.
      * the independent probe measures what COULD be retrieved by a perfectly
        targeted search, not what this investigation did retrieve.

    So this subclasses the session to register every instance, and reads
    `delivered` off the ones created for a given account. It overrides nothing
    and changes no retrieval behaviour — the subclass body is a single
    registration after `super().__init__`.
    """

    def __init__(self, investigation_tools):
        self._mod = investigation_tools
        self._original = investigation_tools.InvestigationSession
        self.sessions: list[Any] = []
        self.installed = False

    def install(self) -> None:
        rec = self
        base = self._original

        class RecordingSession(base):  # type: ignore[valid-type,misc]
            def __init__(self, **kw):
                super().__init__(**kw)
                rec.sessions.append(self)

        self._mod.InvestigationSession = RecordingSession
        self.installed = True

    def uninstall(self) -> None:
        self._mod.InvestigationSession = self._original
        self.installed = False

    def mark(self) -> int:
        return len(self.sessions)

    def window(self, mark: int, account_id: int | None = None) -> dict[str, Any]:
        """Delivery observed since `mark`, for one account or for all of them.

        Used per JOB EXECUTION, which is the only scope where "what was
        delivered" has a single answer. Unioning across an account mixes two
        readers' investigations (scenario H) and, once a foreign job can drain
        inside a reader's window, two scenarios'.

        Calculations are captured alongside the evidence keys because a
        scenario can require a server calculation (`mix`, `cost`, `wait`) that
        never appears in the evidence ledger.
        """
        if not self.installed:
            return {"available": False,
                    "reason": "delivery instrumentation was not installed"}
        mine = [s for s in self.sessions[mark:]
                if account_id is None
                or getattr(s, "account_id", None) == account_id]
        if not mine:
            return {"available": False,
                    "reason": ("no investigation session was created"
                               + (f" for account {account_id}" if account_id
                                  else "") + " in this window")}
        keys: set[str] = set()
        calcs: set[str] = set()
        for sess in mine:
            keys |= set(getattr(sess, "delivered", ()) or ())
            calcs |= set(getattr(sess, "calculations", {}) or {})
        return {
            "available": True,
            "sessions": len(mine),
            "keys": sorted(keys),
            "calculations": sorted(calcs),
            "observed_empty": not keys and not calcs,
            "note": ("Captured from InvestigationSession.delivered — the keys "
                     "this investigation actually put in front of the model."),
        }

    def since(self, mark: int, account_id: int) -> dict[str, Any]:
        """Delivery observed for one account since `mark`.

        An OBSERVED EMPTY ledger (`available: True, keys: []`) and NO
        INSTRUMENTATION (`available: False`) are different answers and stay
        different: the first says the model was shown nothing, the second says
        we do not know what it was shown.
        """
        if not self.installed:
            return {"available": False,
                    "reason": "delivery instrumentation was not installed"}
        mine = [s for s in self.sessions[mark:]
                if getattr(s, "account_id", None) == account_id]
        if not mine:
            return {"available": False,
                    "reason": ("no investigation session was created for account "
                               f"{account_id} during this attempt")}
        keys: set[str] = set()
        for sess in mine:
            keys |= set(getattr(sess, "delivered", ()) or ())
        return {
            "available": True,
            "sessions": len(mine),
            "keys": sorted(keys),
            "observed_empty": not keys,
            "note": ("Captured from InvestigationSession.delivered — the keys "
                     "this investigation actually put in front of the model."),
        }


def _worker_report(d: dict) -> dict:
    """Everything the worker reported that a reviewer could need.

    The product does not expose a full model transcript, and nothing here
    invents one — what is absent is absent, and `transcript_available` says so.
    """
    return {
        "job_id": d.get("job_id"),
        "analysis_id": d.get("analysis_id"),
        "scope_key": d.get("scope_key"),
        "status": d.get("status"),
        "analysis_outcome": d.get("analysis_outcome"),
        "completion_gaps": d.get("completion_gaps"),
        "coverage_gaps": d.get("coverage_gaps"),
        "deadline_hit": d.get("deadline_hit"),
        "seconds": d.get("seconds"),
        "candidates": d.get("candidates"),
        "published": d.get("published"),
        "retired": d.get("retired"),
        "retire_previous": d.get("retire_previous"),
        # The three that say WHY nothing was published.
        "rejected": d.get("rejected"),
        "abstained": d.get("abstained"),
        "error": d.get("error"),
        "retrieval": d.get("retrieval"),
        "budget": d.get("budget"),
        "evidence_version": d.get("evidence_version"),
        "evidence_cutoff": d.get("evidence_cutoff"),
        "prompt_version": d.get("prompt_version"),
        "model": d.get("model"),
        "publication_records": [
            {k: rec.get(k) for k in (
                "finding_key", "title", "explanation", "consequence", "category",
                "claim_kind", "confidence", "claims", "evidence", "entities",
                "uncertainty", "next_step", "coverage", "validation",
                "rank_score", "topic")}
            for rec in ((d.get("publication") or {}).get("records") or [])
        ],
        "transcript_available": False,
        "transcript_note": (
            "The pipeline does not persist the model transcript, so none is "
            "included. Candidate decisions, rejections and abstentions above "
            "are the worker's own report."
        ),
    }


def _finding_summary(f: dict) -> dict:
    return {k: f.get(k) for k in (
        "id", "category", "claim_kind", "confidence", "title", "explanation",
        "consequence", "next_step", "entities", "uncertainty", "state",
        "evidence_count", "requires_financial")}


def _finding_detail(d: dict) -> dict:
    return {
        "id": (d.get("finding") or {}).get("id"),
        "finding": d.get("finding"),
        "claims": d.get("claims"),
        "evidence": d.get("evidence"),
        "stale_evidence": d.get("stale_evidence"),
        "uncertainty": (d.get("finding") or {}).get("uncertainty"),
        "targets": (d.get("navigation") or {}).get("targets"),
    }


def _discoverability(S, ctx) -> dict:
    """Production-budget retrieval for this fixture, plus a labelled diagnostic."""
    prod = S.probe(ctx, budget=S.PRODUCTION_BUDGET)
    spec = ctx["spec"]
    out = {
        "production": _probe_summary(prod),
        "diagnostic": None,
        # Separate from delivery: some scenarios turn on a pattern that no tool
        # exposes, so their rows arriving proves nothing about the pattern.
        "pattern_retrievable": spec.get("pattern_retrievable", True),
        "pattern_note": spec.get("pattern_note"),
        "note": ("`production` uses the product's own ToolBudget. Only it may "
                 "support a claim about production discoverability."),
    }
    if prod["missing_required"]:
        diag = S.probe(ctx, budget=S.DIAGNOSTIC_BUDGET)
        out["diagnostic"] = _probe_summary(diag)
        out["diagnostic_note"] = (
            "Expanded budget, run only because the production budget did not "
            "deliver everything. If this succeeds where production failed, the "
            "evidence is retrievable but NOT reachable in production.")
    return out


def _probe_summary(p: dict) -> dict:
    return {
        "budget_kind": p["budget_kind"],
        "budget": p["budget"],
        "coverage": p["coverage"],
        "required_keys": p["required_keys"],
        "missing_required": p["missing_required"],
        "delivered_within_budget": p["delivered_within_budget"],
        "tool_calls_attempted": len(p["attempted"]),
        "refused_by_budget": p["refused_by_budget"],
        "tool_errors": p["tool_errors"],
        "delivered_key_count": len(p["delivered_keys"]),
    }


def snapshot_agreement(snap: dict, findings: list) -> dict:
    period = snap.get("period") or {}
    series = snap.get("completions_series") or {}
    return {
        "period_days": period.get("days"),
        "period_completed": period.get("completed"),
        "counts_exact": period.get("exact"),
        "series_total": series.get("total"),
        "series_reconciles": series.get("reconciles"),
        "scope_effective": (snap.get("scope") or {}).get("effective"),
        "findings_published": len(findings),
    }


# ---------------------------------------------------------------------------
# Spending control
# ---------------------------------------------------------------------------

class Meter:
    """Counts model calls and bounds estimated spend BEFORE each request.

    The previous version allowed a call when pricing was unknown, checked only
    money already spent, and reserved nothing for the request it was about to
    send — so the first request over the line was always sent, and with unknown
    pricing every request was. All three are fixed here:

    * unknown pricing REFUSES a dollar-budgeted live run outright;
    * each request is priced at a conservative upper bound (a bounded estimate
      of its input plus its full `max_tokens` of output) and is not sent if
      that bound would take the run past the cap;
    * the reservation is reconciled against the provider's reported usage
      afterwards, and a response with NO usage keeps the reservation rather
      than being counted as free.

    This bounds an ESTIMATE. It is not a promise about the provider's bill:
    prices here come from this repository's table, and only the provider knows
    what it actually charged.
    """

    # Headroom applied to the provider's own count, covering what
    # `count_tokens` cannot see from the caller's arguments alone: the
    # server-side system additions and the thinking configuration the product
    # sends. 10% plus a flat 512 tokens.
    INPUT_HEADROOM_RATIO = 1.10
    INPUT_HEADROOM_FLAT = 512

    def __init__(self, *, max_calls: int, max_usd: float, database, model: str,
                 priced: bool = True):
        if not _finite_positive(max_calls) or not _finite_positive(max_usd):
            raise ValueError("max_calls and max_usd must be finite and positive")
        self.max_calls = int(max_calls)
        self.max_usd = float(max_usd)
        self.model = model
        self._db = database
        self._enforce_dollars = priced
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.usage_missing = 0
        self.counted_input_tokens = 0
        self.count_requests = 0
        self.failed_calls = 0
        self.measured_calls = 0
        self.partial_usage: list[dict] = []
        self.reserved_usd = 0.0     # for calls whose usage never came back
        self.reconciled_usd = 0.0   # for calls the provider reported
        self.refusals: list[str] = []
        self._rates = self._load_rates()

    # -- pricing -------------------------------------------------------
    def _load_rates(self):
        try:
            with self._db._connect() as conn, self._db._cursor(conn) as cur:
                pricing = self._db._load_pricing(cur)
        except Exception:
            return None
        one = self._db._compute_cost(self.model, 1_000_000, 0, pricing)
        two = self._db._compute_cost(self.model, 0, 1_000_000, pricing)
        if one is None or two is None:
            return None
        return {"in_per_token": one / 1_000_000, "out_per_token": two / 1_000_000}

    def pricing_known(self) -> bool:
        return self._rates is not None

    def _cost(self, inp: int, out: int) -> float | None:
        if self._rates is None:
            return None
        return inp * self._rates["in_per_token"] + out * self._rates["out_per_token"]

    # -- the allowance -------------------------------------------------
    def spent_usd(self) -> float:
        return self.reconciled_usd + self.reserved_usd

    def remaining_usd(self) -> float:
        return self.max_usd - self.spent_usd()

    def preflight(self, client, **kw) -> float:
        """The most this request could cost, by this repository's prices.

        The input side is counted by the PROVIDER (`messages.count_tokens`),
        not estimated from string length. `len(serialized)/3 + 1000` was a
        heuristic wearing the words "upper bound": it has no guarantee behind
        it, and it under-counts exactly where it matters — dense non-English
        text, base64, and long tool schemas.

        The output side is the request's own `max_tokens`, which the server
        enforces, so it is a true ceiling.

        If the count cannot be obtained, this raises. A request that cannot be
        bounded is refused rather than sent on the assumption that a guess was
        close enough.
        """
        counted = self.count_input_tokens(client, **kw)
        est_in = int(counted * self.INPUT_HEADROOM_RATIO) + self.INPUT_HEADROOM_FLAT
        est_out = int(kw.get("max_tokens") or 0)
        if est_out <= 0:
            raise BudgetExceeded(
                "request has no max_tokens, so its output cannot be bounded")
        cost = self._cost(est_in, est_out)
        if cost is None:
            raise BudgetExceeded(f"no price known for {self.model}")
        self.counted_input_tokens += counted
        return cost

    def count_input_tokens(self, client, **kw) -> int:
        """Ask the provider how many input tokens this request is.

        One extra request per model call. It is not a Messages call and does
        not consume output tokens, but it is a request against the same
        account — recorded here so the count appears in the report rather than
        being invisible overhead.
        """
        counter = getattr(getattr(client, "messages", None), "count_tokens", None)
        if counter is None:
            raise BudgetExceeded(
                "the client cannot count tokens, so this request cannot be "
                "bounded; refusing to send it under a dollar budget")
        args = {"model": kw.get("model") or self.model,
                "messages": kw.get("messages") or []}
        for optional in ("system", "tools"):
            if kw.get(optional) is not None:
                args[optional] = kw[optional]
        try:
            counted = counter(**args)
        except BudgetExceeded:
            raise
        except Exception as exc:
            raise BudgetExceeded(
                f"token counting failed ({type(exc).__name__}), so this request "
                "cannot be bounded; refusing to send it") from exc
        self.count_requests += 1
        value = getattr(counted, "input_tokens", None)
        if not isinstance(value, int) or value < 0:
            raise BudgetExceeded(
                "token counting returned no usable input_tokens; refusing to "
                "send an unbounded request")
        return value

    def spent_out(self) -> bool:
        if self.calls >= self.max_calls:
            return True
        return self._enforce_dollars and self.remaining_usd() <= 0

    def stop_reason(self) -> str:
        if self.calls >= self.max_calls:
            return f"model-call ceiling reached ({self.calls}/{self.max_calls})"
        if self._enforce_dollars and self.remaining_usd() <= 0:
            return (f"estimated-cost ceiling reached "
                    f"(${self.spent_usd():.4f} of ${self.max_usd:.2f})")
        return "budget reached"

    # -- the wrapper ---------------------------------------------------
    def wrap(self, client):
        meter = self

        class Messages:
            def create(self, **kw):
                if meter.calls >= meter.max_calls:
                    raise BudgetExceeded(meter.stop_reason())
                allowance = 0.0
                if meter._enforce_dollars:
                    if not meter.pricing_known():
                        meter.refusals.append("pricing unknown")
                        raise BudgetExceeded(
                            f"no price known for {meter.model}; refusing to "
                            "send a request a dollar budget cannot bound")
                    # Raises if the request cannot be bounded — which refuses
                    # it, rather than sending it on a heuristic.
                    allowance = meter.preflight(client, **kw)
                    if allowance > meter.remaining_usd():
                        meter.refusals.append("would exceed remaining budget")
                        raise BudgetExceeded(
                            f"request could cost up to ${allowance:.4f}, only "
                            f"${meter.remaining_usd():.4f} remains of "
                            f"${meter.max_usd:.2f}")
                meter.calls += 1
                meter.reserved_usd += allowance
                try:
                    resp = client.messages.create(**kw)
                except Exception:
                    # The request was made and may have been served and billed.
                    # The reservation STAYS: releasing it would make a failed
                    # request free, and a retry storm invisible.
                    meter.failed_calls += 1
                    raise
                meter._reconcile(resp, allowance)
                return resp

        class Wrapped:
            messages = Messages()

        return Wrapped()

    @staticmethod
    def _token_field(usage, name: str) -> int | None:
        """A usage field, or None when it is absent or not a usable count."""
        value = getattr(usage, name, None)
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value if value >= 0 else None

    def _reconcile(self, resp, allowance: float) -> None:
        """Swap the reservation for what the provider says it actually used —
        but ONLY when the provider reported all of it.

        The previous version released the reservation whenever EITHER field was
        present, treating the missing half as zero: a response carrying
        `input_tokens=100` and no `output_tokens` turned a $0.031 reservation
        into $0.0001 of accounted spend, and left `usage_missing` at zero so
        nothing said the number was unreliable. Partial usage is unknown
        consumption, and unknown consumption keeps its reservation.
        """
        usage = getattr(resp, "usage", None)
        inp = self._token_field(usage, "input_tokens") if usage is not None else None
        out = self._token_field(usage, "output_tokens") if usage is not None else None
        if inp is None or out is None:
            self.usage_missing += 1
            self.partial_usage.append({
                "input_tokens": inp, "output_tokens": out,
                "reservation_usd": round(allowance, 6),
            })
            return  # reservation stands
        self.input_tokens += inp
        self.output_tokens += out
        actual = self._cost(inp, out)
        if actual is None:
            return  # unpriced: the reservation is the only number we have
        self.reserved_usd -= allowance
        self.reconciled_usd += actual
        self.measured_calls += 1

    # -- reporting -----------------------------------------------------
    def snapshot(self) -> dict:
        return {"model_calls": self.calls, "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "usage_missing": self.usage_missing,
                "failed_calls": self.failed_calls,
                "measured_calls": self.measured_calls,
                "counted_input_tokens": self.counted_input_tokens,
                "count_requests": self.count_requests,
                "estimated_usd": self.spent_usd()}

    def delta(self, before: dict) -> dict:
        now = self.snapshot()
        return {k: (round(now[k] - before[k], 6) if isinstance(now[k], float)
                    else now[k] - before[k]) for k in now}

    def report(self) -> dict:
        return {
            "model_calls": self.calls,
            "call_limit": self.max_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "responses_without_usage": self.usage_missing,
            "partial_usage_responses": self.partial_usage,
            "failed_calls": self.failed_calls,
            "calls_with_measured_usage": self.measured_calls,
            "token_count_requests": self.count_requests,
            "counted_input_tokens": self.counted_input_tokens,
            "estimated_usd": round(self.spent_usd(), 6),
            "of_which_measured": round(self.reconciled_usd, 6),
            "of_which_unreconciled_reservations": round(self.reserved_usd, 6),
            "usd_limit": self.max_usd,
            "pricing_known": self.pricing_known(),
            "dollar_budget_enforced": self._enforce_dollars,
            "refusals": self.refusals,
            "note": ("An ESTIMATED-COST LIMIT based on this repository's "
                     "pricing table — not a guarantee about the provider's "
                     "bill. `of_which_measured` comes from usage the provider "
                     "fully reported; `of_which_unreconciled_reservations` is "
                     "the pre-request allowance kept for calls whose usage was "
                     "absent, partial or unusable, and for calls that raised. "
                     "Input bounds come from the provider's own count_tokens "
                     f"plus {int((Meter.INPUT_HEADROOM_RATIO - 1) * 100)}% and "
                     f"{Meter.INPUT_HEADROOM_FLAT} tokens of headroom."),
        }


def print_summary(report: dict, mode: str) -> None:
    print("\n" + "=" * 72)
    if mode == "stub":
        print("SCRIPTED MODEL. This exercises the pipeline end to end and says")
        print("NOTHING about whether a real model discovers anything.")
    print("=" * 72)
    for r in report["results"]:
        if r.get("skipped"):
            print(f"\n{r['key']}#{r['attempt']}: SKIPPED — {r['skipped']}")
            continue
        a = r["assessment"]
        prov = r["provenance"]
        det = a["deterministic"]
        print(f"\n{r['key']}#{r['attempt']} · {r['name']}")
        print(f"   fixture {r['fixture']['fixture_id']} "
              f"account={r['fixture']['account_id']} "
              f"jobs={prov['job_ids']} analyses={prov['analysis_ids']} "
              f"calls={prov['model_calls_this_attempt']}")
        print(f"   execution: {r['execution']} "
              f"(state={r['analysis']['state']}, "
              f"outcome={r['analysis']['outcome']}, "
              f"gaps={r['analysis']['completion_gaps']})")
        disc = r["discoverability"]["production"]
        print(f"   production-budget evidence: "
              f"{'delivered' if disc['delivered_within_budget'] else 'MISSING ' + str(disc['missing_required'])}"
              f" ({disc['budget']['tool_calls']}/{disc['budget']['tool_call_limit']} calls,"
              f" {disc['budget']['rows_retrieved']}/{disc['budget']['row_limit']} rows)")
        print(f"   published: {a['published']}   "
              f"deterministic: {det['passed']} passed / {len(det['failures'])} failed   "
              f"review flags: {len(a['review_flags'])} "
              f"({a['affirmative_flag_count']} affirmative)")
        for t in a["titles"]:
            print(f"     · {t}")
        for f in det["failures"]:
            print(f"     !! FAILED CHECK: {f['check']} — {f['detail']}")
        for fl in a["review_flags"]:
            print(f"     ?  review [{fl['disposition']}] {fl['field']}: "
                  f"{fl['clause'][:90]}")
        for wr in r["worker_reports"]:
            if wr.get("rejected"):
                print(f"     ~  validator rejected: {wr['rejected']}")
            if wr.get("abstained"):
                print(f"     ~  abstained: {wr['abstained']}")
        print(f"   diagnosis: {a['diagnosis']['code']} — {a['diagnosis']['meaning']}")
        print(f"   discovery: {a['discovery']} — {a['discovery_note']}")
        if r.get("restricted_reader"):
            rr = r["restricted_reader"]
            rd = rr["assessment"]["deterministic"]
            print(f"   restricted reader: {rr['published']} published, "
                  f"{len(rd['failures'])} policy failures, "
                  f"{len(rr['assessment']['review_flags'])} review flags")
    rec = report.get("usage_reconciliation") or {}
    print("\nusage: " + json.dumps(report["usage"]))
    print("reconciliation: " + json.dumps(rec))
    for line in rec.get("problems") or []:
        print(f"  !! {line}")


if __name__ == "__main__":
    sys.exit(main())
