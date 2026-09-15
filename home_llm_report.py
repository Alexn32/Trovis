"""What Trovis spent on Home investigations, over a bounded period.

    TROVIS_INTERNAL_OPS=1 python3 home_llm_report.py --days 7
    TROVIS_INTERNAL_OPS=1 python3 home_llm_report.py --days 30 --account 4 --json

**Server-side only, and deliberately not an endpoint.** The `/admin/*` routes
are gated by the API-key middleware alone, which authenticates *a* tenant
rather than an operator — publishing per-account internal spend there would
hand every customer a breakdown of what we spend on every other customer. This
runs where the database credentials already are, and refuses to run without an
explicit `TROVIS_INTERNAL_OPS=1`, so it cannot be reached by accident from a
web process.

It is a report, not a dashboard: it prints what the ledger holds and says what
it does not know.

## What the numbers mean

* **Estimated subtotal** — the sum of `estimated_cost_usd` over requests we
  could both measure and price. It is an ESTIMATE from list prices, not an
  invoice.
* **Unknown additional spending** — requests that were sent (so they may well
  have been billed) but which we could not price: no usage reported, or no
  stored rate for the model served, or still unresolved. These are counted and
  listed separately and are NEVER added into the subtotal as zero. The true
  total is the subtotal PLUS an unknown amount, and that is how it prints.
* **Failed, cancelled and retried requests are included.** The provider bills
  for the attempt. A report that counted only successful investigations would
  under-state exactly the spending worth looking at.

## Cost per investigation

The denominator is stated on every line, because there is more than one honest
choice and they differ a lot:

* per STARTED execution — every `(job, attempt)` pair that made at least one
  request, failures and retries included;
* per COMPLETED execution — those that finished. An execution's outcome is
  ITS OWN: a job that failed once and succeeded on retry is one failed
  execution and one completed one, because counting both as completed (the job
  did eventually finish) would hide exactly the retry spending this report
  exists to surface.

There is deliberately **no cost per user**. Home investigations are per
account and per audience scope, not per person; dividing a shared
organisation's analysis by a headcount would invent a number.

## Reconciling against the provider

This ledger counts requests WE issued and prices them from the local
`model_pricing` table. It is not the provider's billing record and cannot be:

1. A request can be billed without us recording a response — the row stays
   `in_flight` (see "unresolved" below), and the provider may still charge.
2. List prices in `model_pricing` may lag the contract actually billed.
3. SDK-internal retries below our call boundary are invisible here.

To reconcile: take the period's request count and token totals from this
report and compare against the provider's usage console for the same UTC
window and API key. Differences should be explained by the three causes
above, in that order. A persistent gap larger than the unresolved count is a
bug in this ledger, not a rounding difference.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any


def _require_operator() -> None:
    if os.environ.get("TROVIS_INTERNAL_OPS") != "1":
        sys.stderr.write(
            "refusing to run: this report exposes Trovis's own per-account "
            "inference spending and is for operators, not tenants.\n"
            "Set TROVIS_INTERNAL_OPS=1 to confirm you are running it "
            "server-side.\n"
        )
        raise SystemExit(2)


def _money(v: float | None) -> str:
    return "unknown" if v is None else f"${v:,.4f}"


def collect(rows: list[dict[str, Any]], jobs: dict[int, str]) -> dict[str, Any]:
    """Fold ledger rows into the report. Pure — takes rows, returns numbers."""
    priced = [r for r in rows if r.get("estimated_cost_usd") is not None]
    subtotal = round(sum(float(r["estimated_cost_usd"]) for r in priced), 6)

    unresolved = [r for r in rows if r.get("outcome") == "in_flight"]
    no_usage = [r for r in rows if not r.get("usage_reported")]
    unpriced = [r for r in rows
                if r.get("usage_reported") and r.get("estimated_cost_usd") is None]

    by = {"account": defaultdict(lambda: [0, 0.0, 0]),
          "model": defaultdict(lambda: [0, 0.0, 0]),
          "stage": defaultdict(lambda: [0, 0.0, 0])}
    for r in rows:
        cost = r.get("estimated_cost_usd")
        for dim, key in (
            ("account", r.get("account_id")),
            ("model", r.get("model_served") or r.get("model_requested")),
            ("stage", r.get("stage")),
        ):
            slot = by[dim][key]
            slot[0] += 1
            if cost is None:
                slot[2] += 1
            else:
                slot[1] = round(slot[1] + float(cost), 6)

    # One execution is one (job, attempt) pair that actually issued a request.
    executions = {(r.get("analysis_job_id"), r.get("job_attempt")) for r in rows}
    executions.discard((None, None))
    # An execution's outcome is ITS OWN, not its job's final one. A job that
    # failed once and succeeded on retry is one failed execution and one
    # completed execution — counting both as completed (because the job
    # eventually finished 'done') would hide exactly the retry spending this
    # report exists to surface. The last attempt of a job carries the job's
    # verdict; every earlier attempt is one that did not finish.
    last_attempt: dict[Any, Any] = {}
    for job_id, attempt in executions:
        if job_id is None:
            continue
        prev = last_attempt.get(job_id)
        if prev is None or (attempt is not None and attempt > prev):
            last_attempt[job_id] = attempt
    completed, failed, unfinished = set(), set(), set()
    for ex in executions:
        job_id, attempt = ex
        if attempt != last_attempt.get(job_id):
            failed.add(ex)          # superseded by a later attempt
        elif jobs.get(job_id) == "done":
            completed.add(ex)
        elif jobs.get(job_id) == "failed":
            failed.add(ex)
        else:
            unfinished.add(ex)      # still queued/running, or job row gone

    outcomes = defaultdict(int)
    for r in rows:
        outcomes[r.get("outcome") or "unknown"] += 1

    tokens = {}
    for field in ("input_tokens", "output_tokens",
                  "cache_creation_input_tokens", "cache_read_input_tokens"):
        seen = [r[field] for r in rows if r.get(field) is not None]
        tokens[field] = {"total": sum(seen), "reported_by": len(seen)}

    return {
        "requests": len(rows),
        "estimated_subtotal_usd": subtotal,
        "priced_requests": len(priced),
        "unknown_spend_requests": len(rows) - len(priced),
        "unresolved_requests": len(unresolved),
        "requests_without_usage": len(no_usage),
        "requests_without_price": len(unpriced),
        "usage_coverage": (round(len(rows) - len(no_usage), 6) / len(rows)
                           if rows else None),
        "pricing_coverage": (len(priced) / len(rows) if rows else None),
        "outcomes": dict(outcomes),
        "tokens": tokens,
        "by_account": {k: {"requests": v[0], "estimated_usd": v[1],
                           "unknown_cost_requests": v[2]}
                       for k, v in by["account"].items()},
        "by_model": {k: {"requests": v[0], "estimated_usd": v[1],
                         "unknown_cost_requests": v[2]}
                     for k, v in by["model"].items()},
        "by_stage": {k: {"requests": v[0], "estimated_usd": v[1],
                         "unknown_cost_requests": v[2]}
                     for k, v in by["stage"].items()},
        "executions_started": len(executions),
        "executions_completed": len(completed),
        "executions_failed": len(failed),
        "executions_unfinished": len(unfinished),
        # Both denominators, both named. Null rather than a divide-by-zero.
        "cost_per_started_execution_usd": (
            round(subtotal / len(executions), 6) if executions else None),
        "cost_per_completed_execution_usd": (
            round(subtotal / len(completed), 6) if completed else None),
        "note": (
            "Estimated subtotal covers only requests we could measure AND "
            "price. Unknown spending is reported separately and never added "
            "as zero, so the true total is the subtotal plus an unknown "
            "amount. Failed, cancelled and retried requests are included: the "
            "provider bills for the attempt."
        ),
    }


def _render(report: dict[str, Any], start, end) -> str:
    out: list[str] = []
    w = out.append
    w("=" * 68)
    w("Trovis Home inference spending — INTERNAL")
    w(f"window: {start.isoformat()}  ->  {end.isoformat()}  (UTC)")
    w("=" * 68)
    w("")
    w(f"  estimated subtotal        {_money(report['estimated_subtotal_usd'])}"
      f"   over {report['priced_requests']} priced requests")
    if report["unknown_spend_requests"]:
        w(f"  + UNKNOWN additional spend over "
          f"{report['unknown_spend_requests']} request(s) we could not price")
        w(f"      no usage reported       {report['requests_without_usage']}")
        w(f"      no price for the model  {report['requests_without_price']}")
        w(f"      still unresolved        {report['unresolved_requests']}")
        w("    The true total is the subtotal PLUS an unknown amount.")
    else:
        w("  every request in this window was measured and priced.")
    w("")
    w(f"  requests                  {report['requests']}")
    for name, value in sorted(report["outcomes"].items()):
        w(f"      {name:<20}  {value}")
    cov = report["pricing_coverage"]
    ucov = report["usage_coverage"]
    w(f"  usage coverage            "
      f"{'n/a' if ucov is None else f'{ucov * 100:.1f}%'}")
    w(f"  pricing coverage          "
      f"{'n/a' if cov is None else f'{cov * 100:.1f}%'}")
    w("")
    for title, key in (("by account", "by_account"), ("by model", "by_model"),
                       ("by stage", "by_stage")):
        w(f"  {title}")
        rows = sorted(report[key].items(),
                      key=lambda kv: kv[1]["estimated_usd"], reverse=True)
        if not rows:
            w("      (nothing recorded)")
        for k, v in rows:
            unknown = (f"   +{v['unknown_cost_requests']} unpriced"
                       if v["unknown_cost_requests"] else "")
            w(f"      {str(k):<28} {_money(v['estimated_usd']):>14}"
              f"   {v['requests']:>4} req{unknown}")
        w("")
    w(f"  investigations started    {report['executions_started']}"
      "   (one (job, attempt) pair that issued at least one request)")
    w(f"  investigations completed  {report['executions_completed']}"
      "   (its job finished 'done')")
    w(f"  investigations failed     {report['executions_failed']}"
      "   (its own attempt did not finish, retries included)")
    if report.get("executions_unfinished"):
        w(f"  investigations unfinished {report['executions_unfinished']}"
          "   (still queued or running at read time)")
    w("")
    w("  cost per investigation — the denominator is stated, not assumed:")
    w(f"      per STARTED execution     "
      f"{_money(report['cost_per_started_execution_usd'])}"
      f"   (n={report['executions_started']}, failures and retries included)")
    w(f"      per COMPLETED execution   "
      f"{_money(report['cost_per_completed_execution_usd'])}"
      f"   (n={report['executions_completed']})")
    w("      no cost-per-user is offered: Home analyses an account's audience "
      "scope,")
    w("      not a person, so a headcount denominator would invent a number.")
    w("")
    w("  reconciling against the provider: compare this window's request count")
    w("  and token totals against the provider's usage console for the same")
    w("  UTC window and API key. See this module's docstring for the three")
    w("  causes that legitimately explain a difference.")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    _require_operator()
    ap = argparse.ArgumentParser(description=__doc__ or "")
    ap.add_argument("--days", type=int, default=7,
                    help="window length in days (1-365, default 7)")
    ap.add_argument("--account", type=int, default=None,
                    help="restrict to one account id")
    ap.add_argument("--json", action="store_true", help="machine-readable")
    args = ap.parse_args(argv)
    if not 1 <= args.days <= 365:
        sys.stderr.write("--days must be between 1 and 365\n")
        return 2

    import database

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=args.days)
    rows = database.home_llm_requests_between(start, end, account_id=args.account)

    job_ids = {r.get("analysis_job_id") for r in rows if r.get("analysis_job_id")}
    jobs: dict[int, str] = {}
    if job_ids:
        with database._connect() as conn, database._cursor(conn) as cur:
            holes = ", ".join([database.PH] * len(job_ids))
            cur.execute(
                f"SELECT id, status FROM analysis_jobs WHERE id IN ({holes})",
                tuple(job_ids),
            )
            jobs = {int(dict(r)["id"]): dict(r)["status"]
                    for r in (cur.fetchall() or [])}

    report = collect(rows, jobs)
    report["window"] = {"start_utc": start.isoformat(), "end_utc": end.isoformat(),
                        "days": args.days, "account_id": args.account}
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(_render(report, start, end))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
