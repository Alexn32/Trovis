"""Run the Home findings evaluation against the real investigation pipeline.

    snapshot  ->  queued analysis  ->  retrieval tools  ->  validation
              ->  publication      ->  the Home read path

Nothing is short-circuited: the scenarios in `home_eval_scenarios.py` are
seeded as ordinary work records, the read enqueues, the real worker drains the
queue, and what the reader would see is read back through `GET /home/findings`.

**It does not call a model unless you say so twice.** The default mode makes no
network request at all. `--live` additionally requires
`--yes-spend-money`, because this costs real money against a real key and a
flag that can be set by accident is not a decision.

    # what would run, and what it would cost — no calls
    python3 run_home_eval.py

    # plumbing check: the whole pipeline, with a SCRIPTED model.
    # Proves the harness works. Says NOTHING about model quality.
    python3 run_home_eval.py --mode stub

    # the real thing
    ANTHROPIC_API_KEY=sk-... python3 run_home_eval.py \\
        --mode live --yes-spend-money --scenarios B,F,I --repeat 2 \\
        --max-usd 5

Every run writes a JSON transcript next to a human summary, so a result can be
re-read rather than remembered.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone

# Bounds are applied before anything imports the investigator, because two of
# them are read at module scope.
DEFAULTS = {
    "TROVIS_INVESTIGATION_WALL_S": "240",
    "TROVIS_INVESTIGATION_TIMEOUT_S": "60",
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["plan", "stub", "live"], default="plan",
                    help="plan: describe the run and make no calls (default). "
                         "stub: real pipeline, scripted model, no network. "
                         "live: real pipeline, real model, real money.")
    ap.add_argument("--yes-spend-money", action="store_true",
                    help="Required with --mode live. There is no default that spends.")
    ap.add_argument("--scenarios", default="",
                    help="Comma-separated keys (A-I). Default: all.")
    ap.add_argument("--repeat", type=int, default=1,
                    help="Runs per scenario, to see whether results are stable.")
    ap.add_argument("--max-calls", type=int, default=60,
                    help="Hard ceiling on model calls for the whole run.")
    ap.add_argument("--max-usd", type=float, default=5.0,
                    help="Stop before a run that would take estimated spend past this.")
    ap.add_argument("--wall-s", type=float, default=240.0,
                    help="Per-investigation wall clock, as the product uses it.")
    ap.add_argument("--out", default="eval_out",
                    help="Directory for the JSON transcript and summary.")
    args = ap.parse_args()

    if args.mode == "live" and not args.yes_spend_money:
        print("refusing: --mode live needs --yes-spend-money as well.")
        return 2

    os.environ.setdefault("TROVIS_DISABLE_PRICING_SYNC", "1")
    os.environ.setdefault("OVERSEE_DISABLE_PRICING_SYNC", "1")
    os.environ["TROVIS_DISABLE_ALERTS"] = "1"
    os.environ["TROVIS_DISABLE_LOOP_SWEEP"] = "1"
    os.environ["TROVIS_LOOP_TITLES"] = "off"
    # The in-process worker is off; this script drains the queue itself so a
    # run is deterministic and countable.
    os.environ["TROVIS_DISABLE_ANALYSIS"] = "1"
    os.environ["TROVIS_INVESTIGATION_WALL_S"] = str(args.wall_s)
    for k, v in DEFAULTS.items():
        os.environ.setdefault(k, v)
    os.environ.pop("DATABASE_URL", None)  # never a shared or production database

    key = os.environ.get("ANTHROPIC_API_KEY") or ""
    if args.mode == "plan":
        os.environ.pop("ANTHROPIC_API_KEY", None)
    elif args.mode == "stub":
        # `investigate` refuses to run at all without a key — correctly, since
        # "we cannot analyse" and "we analysed and found nothing" are different
        # answers for a reader. The stub replaces `_client` outright, so this
        # placeholder is never read by anything and never leaves the process.
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
            "tool_calls_per_investigation": investigation_budget(),
        },
        "database": tmp.name,
        "api_key_present": bool(key),
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

    meter = Meter(args.max_calls, args.max_usd, database)
    if args.mode == "stub":
        import eval_stub_model
        investigator._client = lambda: eval_stub_model.client()
    else:
        real = investigator._client
        investigator._client = lambda: meter.wrap(real())

    started = time.time()
    results = []
    with TestClient(app_main.app) as c:
        ctxs = S.build_all(c, only=keys)
        for key_ in keys:
            ctx = ctxs[key_]
            for attempt in range(1, args.repeat + 1):
                if meter.spent_out():
                    results.append({"key": key_, "attempt": attempt,
                                    "skipped": "budget reached"})
                    continue
                if args.mode == "stub":
                    import eval_stub_model
                    eval_stub_model.reset()
                results.append(run_one_scenario(
                    c, S, ctx, analysis_jobs, attempt=attempt, meter=meter))

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


def investigation_budget() -> dict[str, int]:
    import investigation_tools
    b = investigation_tools.ToolBudget()
    return {"max_calls": b.max_calls, "max_rows": b.max_rows,
            "max_events": b.max_events}


def run_one_scenario(c, S, ctx, analysis_jobs, *, attempt: int, meter) -> dict:
    """One scenario, through the whole real path, scored against its rubric."""
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
    detail = []
    for f in findings:
        d = c.get(f"/home/findings/{f['id']}?{q}", headers=S.auth(token)).json()
        detail.append(d)

    scored = S.score(spec, findings)
    out = {
        "key": key, "name": spec["name"], "attempt": attempt,
        "analysis": {
            "state": (after.get("analysis") or {}).get("state"),
            "outcome": (after.get("analysis") or {}).get("analysis_outcome"),
            "completion_gaps": (after.get("analysis") or {}).get("completion_gaps"),
            "reason": (after.get("analysis") or {}).get("reason"),
            "enqueued_on_first_read": (first.get("analysis") or {}).get("enqueued"),
        },
        "drained": [{"status": d.get("status"), "outcome": d.get("analysis_outcome"),
                     "published": d.get("published"), "error": d.get("error")}
                    for d in drained],
        "score": scored,
        "findings": [{
            "id": f.get("id"), "category": f.get("category"),
            "claim_kind": f.get("claim_kind"), "confidence": f.get("confidence"),
            "title": f.get("title"), "explanation": f.get("explanation"),
            "consequence": f.get("consequence"),
            "next_step": f.get("next_step"), "entities": f.get("entities"),
            "uncertainty": f.get("uncertainty"),
        } for f in findings],
        "evidence": [{
            "id": (d.get("finding") or {}).get("id"),
            "claims": d.get("claims"),
            "evidence": d.get("evidence"),
            "stale_evidence": d.get("stale_evidence"),
            "targets": (d.get("navigation") or {}).get("targets"),
        } for d in detail],
        "snapshot_agreement": snapshot_agreement(snap, findings),
    }

    if key == "H":
        # The restricted reader is a DIFFERENT audience, so their read enqueues
        # its own analysis under its own scope key. Reading once only queues it.
        r = ctx["restricted"]
        c.get(f"/home/findings?{q}", headers=S.auth(r["token"]))
        analysis_jobs.drain(max_jobs=3)
        r_after = c.get(f"/home/findings?{q}", headers=S.auth(r["token"])).json()
        r_findings = r_after.get("findings") or []
        out["restricted_reader"] = {
            "published": len(r_findings),
            "score": S.score(spec, r_findings, restricted=True),
            "titles": [f.get("title") for f in r_findings],
        }
    return out


def snapshot_agreement(snap: dict, findings: list) -> dict:
    """Do the numbers a reader sees and the findings beside them describe the
    same slice? Reported, not asserted — a mismatch is a finding."""
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


class Meter:
    """Counts what a live run actually spends, and stops it going further.

    Wraps the real client rather than replacing it: every request still goes
    to the configured provider, and the numbers below come from the provider's
    own usage block, not from an estimate of what was sent.
    """

    def __init__(self, max_calls: int, max_usd: float, database):
        self.max_calls = max_calls
        self.max_usd = max_usd
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self._db = database
        self._pricing = None

    def wrap(self, client):
        meter = self

        class Messages:
            def create(self, **kw):
                if meter.spent_out():
                    raise RuntimeError("evaluation budget reached")
                meter.calls += 1
                resp = client.messages.create(**kw)
                usage = getattr(resp, "usage", None)
                if usage is not None:
                    meter.input_tokens += int(getattr(usage, "input_tokens", 0) or 0)
                    meter.output_tokens += int(getattr(usage, "output_tokens", 0) or 0)
                return resp

        class Wrapped:
            messages = Messages()

        return Wrapped()

    def estimated_usd(self) -> float | None:
        import investigator
        try:
            with self._db._connect() as conn, self._db._cursor(conn) as cur:
                pricing = self._db._load_pricing(cur)
            return self._db._compute_cost(
                investigator.MODEL, self.input_tokens, self.output_tokens, pricing)
        except Exception:
            return None

    def spent_out(self) -> bool:
        if self.calls >= self.max_calls:
            return True
        est = self.estimated_usd()
        return est is not None and est >= self.max_usd

    def report(self) -> dict:
        est = self.estimated_usd()
        return {
            "model_calls": self.calls,
            "call_limit": self.max_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "estimated_usd": est,
            "usd_limit": self.max_usd,
            "estimate_available": est is not None,
            "note": ("Token counts are the provider's own. The dollar figure is this "
                     "repository's pricing table applied to them, and is an estimate."),
        }


def print_summary(report: dict, mode: str) -> None:
    print("\n" + "=" * 72)
    if mode == "stub":
        print("SCRIPTED MODEL. This exercises the pipeline end to end and says")
        print("NOTHING about whether a real model discovers anything.")
    print("=" * 72)
    for r in report["results"]:
        if r.get("skipped"):
            print(f"\n{r['key']} (attempt {r['attempt']}): SKIPPED — {r['skipped']}")
            continue
        s = r["score"]
        a = r["analysis"]
        print(f"\n{r['key']} · {r['name']}  (attempt {r['attempt']})")
        print(f"   analysis: {a['state']} / outcome={a['outcome']} "
              f"gaps={a['completion_gaps']}")
        print(f"   published: {s['published']}   "
              f"discovered: {s['discovered']}   "
              f"false claims: {len(s['false_claims'])}")
        for t in s["titles"]:
            print(f"     · {t}")
        for fc in s["false_claims"]:
            print(f"     !! unsupported: {fc['title']} ~ /{fc['matched']}/")
        for n in s["notes"]:
            print(f"     - {n}")
        if r.get("restricted_reader"):
            rr = r["restricted_reader"]
            print(f"   restricted reader: {rr['published']} published, "
                  f"{len(rr['score']['false_claims'])} financial leaks")
    print("\nusage: " + json.dumps(report["usage"]))


if __name__ == "__main__":
    sys.exit(main())
