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
                    attempt=attempt, meter=meter, usage_before=before))

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "plan": plan,
        "usage": meter.report(),
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


def run_one_scenario(c, S, ctx, analysis_jobs, investigator, *,
                     attempt: int, meter, usage_before: dict) -> dict:
    """One fixture instance, through the whole real path, with its provenance."""
    key = ctx["key"]
    spec = ctx["spec"]
    token = ctx["token"]
    q = "days=7&tz=UTC"

    snap = c.get(f"/home/snapshot?{q}", headers=S.auth(token)).json()
    # The read enqueues; it never runs a model itself.
    first = c.get(f"/home/findings?{q}", headers=S.auth(token)).json()
    drained = analysis_jobs.drain(max_jobs=3)
    after = c.get(f"/home/findings?{q}", headers=S.auth(token)).json()

    findings = after.get("findings") or []
    details = []
    for f in findings:
        details.append(c.get(f"/home/findings/{f['id']}?{q}",
                             headers=S.auth(token)).json())

    # Only jobs belonging to THIS fixture's account count as this attempt's.
    mine = [d for d in drained if _job_account(d, ctx) is not False]
    delivered = _delivered_from(mine)

    analysis = after.get("analysis") or {}
    discoverability = _discoverability(S, ctx)
    assessment = S.assess(
        spec, findings, details,
        analysis=analysis, worker_reports=mine,
        delivered_evidence=delivered or None,
        evidence_delivered=discoverability["production"]["delivered_within_budget"],
    )

    usage = meter.delta(usage_before)
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
            "jobs_drained_other_accounts": len(drained) - len(mine),
            "model_calls_this_attempt": usage["model_calls"],
        },
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
        r = ctx["restricted"]
        c.get(f"/home/findings?{q}", headers=S.auth(r["token"]))
        r_drained = analysis_jobs.drain(max_jobs=3)
        r_after = c.get(f"/home/findings?{q}", headers=S.auth(r["token"])).json()
        r_findings = r_after.get("findings") or []
        r_details = [c.get(f"/home/findings/{f['id']}?{q}",
                           headers=S.auth(r["token"])).json() for f in r_findings]
        out["restricted_reader"] = {
            "user_id": r["user_id"],
            "published": len(r_findings),
            "titles": [f.get("title") for f in r_findings],
            "worker_reports": [_worker_report(d) for d in r_drained],
            "assessment": S.assess(
                spec, r_findings, r_details,
                analysis=r_after.get("analysis") or {},
                worker_reports=r_drained, restricted=True),
        }
    return out


def _job_account(report: dict, ctx: dict):
    """Is this drained job this fixture's? Unknown counts as ours rather than
    silently dropping evidence; the scope key is recorded either way."""
    acct = report.get("account_id")
    if acct is None:
        return None
    return acct == ctx["account_id"]


def _delivered_from(reports: list[dict]) -> set[str]:
    """Ledger keys the worker says it delivered, if it says."""
    keys: set[str] = set()
    for r in reports:
        for rec in ((r.get("publication") or {}).get("records") or []):
            for ev in (rec.get("evidence") or []):
                if ev.get("kind") and ev.get("ref") is not None:
                    keys.add(f"{ev['kind']}:{ev['ref']}")
    return keys


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
    out = {
        "production": _probe_summary(prod),
        "diagnostic": None,
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

    # Conservative: ~3 characters per token under-counts tokens, so dividing by
    # 3 over-counts them, which is the direction an upper bound must err in.
    CHARS_PER_TOKEN = 3.0

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

    def preflight(self, **kw) -> float:
        """The most this request could cost, by this repository's prices."""
        blob = json.dumps({"system": kw.get("system"),
                           "messages": kw.get("messages"),
                           "tools": kw.get("tools")}, default=str)
        est_in = int(len(blob) / self.CHARS_PER_TOKEN) + 1000  # headroom
        est_out = int(kw.get("max_tokens") or 4096)
        cost = self._cost(est_in, est_out)
        return 0.0 if cost is None else cost

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
                allowance = meter.preflight(**kw)
                if meter._enforce_dollars:
                    if not meter.pricing_known():
                        meter.refusals.append("pricing unknown")
                        raise BudgetExceeded(
                            f"no price known for {meter.model}; refusing to "
                            "send a request a dollar budget cannot bound")
                    if allowance > meter.remaining_usd():
                        meter.refusals.append("would exceed remaining budget")
                        raise BudgetExceeded(
                            f"request could cost up to ${allowance:.4f}, only "
                            f"${meter.remaining_usd():.4f} remains of "
                            f"${meter.max_usd:.2f}")
                meter.calls += 1
                meter.reserved_usd += allowance
                resp = client.messages.create(**kw)
                meter._reconcile(resp, allowance)
                return resp

        class Wrapped:
            messages = Messages()

        return Wrapped()

    def _reconcile(self, resp, allowance: float) -> None:
        """Swap the reservation for what the provider says it actually used.

        A response with no usage block keeps the reservation. Treating it as
        zero would make an unmeasurable call free, which is the one reading
        that can never be justified.
        """
        usage = getattr(resp, "usage", None)
        inp = getattr(usage, "input_tokens", None) if usage is not None else None
        out = getattr(usage, "output_tokens", None) if usage is not None else None
        if inp is None and out is None:
            self.usage_missing += 1
            return  # reservation stands
        inp, out = int(inp or 0), int(out or 0)
        self.input_tokens += inp
        self.output_tokens += out
        actual = self._cost(inp, out)
        self.reserved_usd -= allowance
        self.reconciled_usd += 0.0 if actual is None else actual

    # -- reporting -----------------------------------------------------
    def snapshot(self) -> dict:
        return {"model_calls": self.calls, "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "usage_missing": self.usage_missing,
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
            "estimated_usd": round(self.spent_usd(), 6),
            "of_which_unreconciled_reservations": round(self.reserved_usd, 6),
            "usd_limit": self.max_usd,
            "pricing_known": self.pricing_known(),
            "dollar_budget_enforced": self._enforce_dollars,
            "refusals": self.refusals,
            "note": ("Token counts are the provider's own where it reported "
                     "them. The dollar figure is an ESTIMATED BOUND from this "
                     "repository's pricing table — not a statement about the "
                     "provider's final bill. Calls with no usage block keep "
                     "their pre-request reservation rather than counting zero."),
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
    print("\nusage: " + json.dumps(report["usage"]))


if __name__ == "__main__":
    sys.exit(main())
