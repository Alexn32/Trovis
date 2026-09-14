"""Nine work scenarios, and the rubric each one is judged against.

The question this file exists to answer is NOT "does the pipeline run?" —
`test_home_findings.py` answers that with a stubbed model. It is the product
question: **does Trovis discover something useful and true, and does it hold
back when it should?**

Two rules make that measurable rather than a matter of taste:

1. **The rubric is written before the model runs.** Each scenario states what
   its records establish, what they do NOT establish, which discoveries would
   be reasonable, which claims would be false, and whether saying nothing is a
   correct answer. Nothing here is adjusted after seeing an output — that is
   the whole point of writing it down first.

2. **Discovery and truth are scored apart.** A run that publishes nothing and
   a run that publishes something false are different failures, and a
   validator that blocks a bad draft is not evidence of a good investigation.
   `score()` returns both halves and never averages them into one number.

Every scenario seeds REAL work records through the ordinary ingest path
(`database.ingest_spans_with_loops`) and then lets the real snapshot, the real
retrieval allowlist and the real validator see them. No finding is ever
hand-written here: a hand-written finding tells you about the validator, not
about the model.

Each scenario gets its own account. That keeps the rubrics clean — a claim can
be judged against one situation rather than nine overlapping ones — and it is
also the fixture set's biggest limitation, recorded in EVAL_HOME_FINDINGS.md:
a real workspace is noisy, and a pattern that is obvious in isolation may be
crowded out when nine other things are competing for the same four candidate
slots.
"""
from __future__ import annotations

import time
from typing import Any, Callable

import database
import investigation_tools

NS = 10**9
DAY = 86400

_seq = [0]


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def span(
    service: str,
    ago_s: float,
    attrs: dict[str, Any],
    *,
    name: str = "message_received",
    status: int = 0,
    msg: str = "",
    now_ns: int | None = None,
) -> dict[str, Any]:
    """One recorded span, `ago_s` seconds before now.

    The same shape the OTLP handler produces, so these rows are
    indistinguishable from ingested telemetry once they land.
    """
    _seq[0] += 1
    base = now_ns if now_ns is not None else time.time_ns()
    t = base - int(ago_s * NS)
    return {
        "trace_id": f"ev{_seq[0]:026d}",
        "span_id": f"sp{_seq[0]:012d}",
        "parent_span_id": None,
        "service_name": service,
        "agent_id": "main",
        "span_name": name,
        "kind": 1,
        "start_time_unix": t,
        "end_time_unix": t + 10**6,
        "status_code": status,
        "status_message": msg,
        "attributes": attrs,
        "resource_attributes": {},
    }


def priced(model: str = "claude-sonnet-4-5", inp: int = 1200, out: int = 300) -> dict[str, Any]:
    """Token attributes that make a span cost something."""
    return {
        "gen_ai.request.model": model,
        "gen_ai.usage.input_tokens": inp,
        "gen_ai.usage.output_tokens": out,
    }


def align_to_spans(account_id: int) -> int:
    """Move each work item's `created_at` / `closed_at` onto its own span times.

    Seeded telemetry carries real timestamps, but the ingest path does not use
    them for these two columns: `INSERT INTO loops` lets `created_at` default
    to the insert clock, and `close` stamps `closed_at` with `NOW()`. So a
    batch of a fortnight's work ingested in one second lands entirely in
    today — every period window would see all of it, and none of the
    period-comparison scenarios would mean anything.

    This is a fixture correction, not a fixture invention: it sets each item's
    start and end to the times its own spans already record. It is also the
    first finding this evaluation produced, and it is about the product rather
    than the model — see EVAL_HOME_FINDINGS.md, "ingest time is not event
    time". Real telemetry that arrives late (a buffered agent, a replayed
    export, an overnight batch) is filed into the period it ARRIVED in.
    """
    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute(
            f"SELECT id, cached_state FROM loops WHERE account_id = {database.PH}",
            (account_id,),
        )
        loops = [dict(r) for r in cur.fetchall()]
        moved = 0
        for lp in loops:
            cur.execute(
                "SELECT MIN(start_time_unix) AS first_ns, MAX(start_time_unix) AS last_ns "
                f"FROM spans WHERE loop_id = {database.PH}",
                (lp["id"],),
            )
            row = cur.fetchone()
            if not row or row["first_ns"] is None:
                continue
            started = database._ns_to_iso(int(row["first_ns"]))[:19].replace("T", " ")
            ended = database._ns_to_iso(int(row["last_ns"]))[:19].replace("T", " ")
            cur.execute(
                f"UPDATE loops SET created_at = {database.PH} WHERE id = {database.PH}",
                (started, lp["id"]),
            )
            cur.execute(
                f"UPDATE loops SET closed_at = {database.PH} "
                f"WHERE id = {database.PH} AND closed_at IS NOT NULL",
                (ended, lp["id"]),
            )
            moved += 1
    return moved


def run_id(title: str) -> int | None:
    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute(
            f"SELECT id FROM loops WHERE title = {database.PH} ORDER BY id DESC",
            (title,),
        )
        row = cur.fetchone()
        return int(row["id"]) if row else None


def _work(
    service: str,
    ext: str,
    title: str,
    *,
    start_ago: float,
    steps: list[dict[str, Any]] | None = None,
    close: str | None = None,
    close_ago: float | None = None,
    cost: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """One work item: an opening span, some steps, optionally a close.

    `close=None` leaves the item open, which is what "waiting" and "still
    running" look like in the record — there is no separate state to set.
    """
    spans = [span(service, start_ago, {
        "trovis.loop.title": title, "trovis.loop.external_id": ext,
        **(cost or {}),
    })]
    for st in steps or []:
        spans.append(span(
            service, st["ago"],
            {"trovis.loop.external_id": ext, **(st.get("attrs") or {})},
            name=st.get("name", "tool_call"),
            status=st.get("status", 0),
            msg=st.get("msg", ""),
        ))
    if close:
        spans.append(span(
            service, close_ago if close_ago is not None else start_ago - 60,
            {"trovis.loop.external_id": ext, "trovis.loop.close": close},
            name="agent_run_complete",
        ))
    return spans


def tool_step(ago: float, tool: str, *, status: int = 0, msg: str = "") -> dict[str, Any]:
    return {"ago": ago, "attrs": {"trovis.tool.name": tool}, "status": status, "msg": msg}


# ---------------------------------------------------------------------------
# The scenarios
#
# Each `seed` receives a live TestClient, the owner's token and the account id,
# and returns the ids its rubric refers to. Seeding goes through the HTTP API
# and the ingest path — never straight into the findings table.
# ---------------------------------------------------------------------------


def _job(c, token: str, name: str) -> int:
    return c.post("/workflows", headers=auth(token), json={
        "name": name, "description": f"{name} work",
        "steps": [{"step_type": "agent", "label": "run"}],
    }).json()["id"]


def _classify(job_id: int, like: str) -> None:
    """File seeded work under a job, the way the matcher would."""
    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute(
            f"UPDATE loops SET workflow_id = {database.PH} WHERE title LIKE {database.PH}",
            (job_id, like),
        )


# --- A ---------------------------------------------------------------------

def seed_a(c, token, acct):
    """Ordinary successful work. Nothing is wrong here."""
    job = _job(c, token, "Invoice sync")
    spans = []
    for i in range(6):
        spans += _work(
            "invoice-agent", f"a{i}", f"Invoice sync {i}",
            start_ago=(i + 1) * DAY * 0.9,
            steps=[tool_step((i + 1) * DAY * 0.9 - 30, "ledger_write")],
            close="done", close_ago=(i + 1) * DAY * 0.9 - 60,
            cost=priced(),
        )
    database.ingest_spans_with_loops(spans, account_id=acct)
    _classify(job, "Invoice sync%")
    return {"job": job, "runs": [run_id(f"Invoice sync {i}") for i in range(6)]}


# --- B ---------------------------------------------------------------------

def seed_b(c, token, acct):
    """Four runs stop at the same step. Two others of the same job finish.

    The recurrence is real and the shared step is real. The CAUSE is not in
    the record: nothing here says why the approval call failed.
    """
    job = _job(c, token, "Refund approval")
    spans = []
    for i in range(4):
        ago = (i + 1) * DAY * 0.8
        spans += _work(
            "refunds-agent", f"b{i}", f"Refund {i}",
            start_ago=ago,
            steps=[
                tool_step(ago - 20, "fetch_order"),
                tool_step(ago - 40, "approval_service", status=2,
                          msg="approval_service call did not return"),
            ],
            cost=priced(),
        )
    for i in range(4, 6):
        ago = (i + 1) * DAY * 0.8
        spans += _work(
            "refunds-agent", f"b{i}", f"Refund {i}",
            start_ago=ago,
            steps=[tool_step(ago - 20, "fetch_order"),
                   tool_step(ago - 40, "approval_service")],
            close="done", close_ago=ago - 60, cost=priced(),
        )
    database.ingest_spans_with_loops(spans, account_id=acct)
    _classify(job, "Refund %")
    stalled = [run_id(f"Refund {i}") for i in range(4)]
    for rid in stalled:
        database.abandon_loop(rid, acct)
    return {"job": job, "stalled": stalled,
            "finished": [run_id(f"Refund {i}") for i in (4, 5)]}


# --- C ---------------------------------------------------------------------

def seed_c(c, token, acct):
    """An error, a retry, and then the work completes. Friction, not failure."""
    job = _job(c, token, "Shipment booking")
    spans = []
    for i in range(3):
        ago = (i + 1) * DAY * 1.2
        spans += _work(
            "shipping-agent", f"c{i}", f"Shipment {i}",
            start_ago=ago,
            steps=[
                tool_step(ago - 15, "carrier_api", status=2, msg="carrier_api 503"),
                tool_step(ago - 25, "carrier_api"),
            ],
            close="done", close_ago=ago - 40, cost=priced(),
        )
    database.ingest_spans_with_loops(spans, account_id=acct)
    _classify(job, "Shipment %")
    return {"job": job, "runs": [run_id(f"Shipment {i}") for i in range(3)]}


# --- D ---------------------------------------------------------------------

def seed_d(c, token, acct, viewer_email: str, other_email: str):
    """Work handed to a person and still waiting.

    Two items wait on the reader, one waits on somebody else. The record has
    when each handoff was raised. It has no target response time, and nothing
    that says anyone did anything wrong.
    """
    job = _job(c, token, "Contract review")
    spans = []
    for i, (ext, who) in enumerate([("d0", viewer_email), ("d1", viewer_email),
                                    ("d2", other_email)]):
        ago = (i + 1) * DAY * 1.5
        spans += _work(
            "contracts-agent", ext, f"Contract {i}",
            start_ago=ago,
            steps=[{
                "ago": ago - 30,
                "attrs": {
                    "trovis.handoff.direction": "to_human",
                    "trovis.handoff.target_id": who,
                    "trovis.handoff.reason": "needs a signature",
                    "trovis.handoff.id": f"h-{ext}",
                },
                "name": "handoff",
            }],
            cost=priced(),
        )
    database.ingest_spans_with_loops(spans, account_id=acct)
    _classify(job, "Contract %")
    return {"job": job, "waiting_on_viewer": [run_id("Contract 0"), run_id("Contract 1")],
            "waiting_on_other": [run_id("Contract 2")]}


# --- E ---------------------------------------------------------------------

def seed_e(c, token, acct):
    """Repeated activity that LOOKS avoidable, with evidence both ways.

    Four research runs each make eight `web_search` calls. A fifth makes two
    and finishes just the same — which is the thing that makes a saving look
    plausible. But nothing records what any search returned, so whether the
    eight were redundant is exactly what the record cannot say.
    """
    job = _job(c, token, "Market research")
    spans = []
    for i in range(4):
        ago = (i + 1) * DAY * 1.1
        steps = [tool_step(ago - 5 * (k + 1), "web_search") for k in range(8)]
        spans += _work("research-agent", f"e{i}", f"Research {i}",
                       start_ago=ago, steps=steps, close="done",
                       close_ago=ago - 60, cost=priced(inp=9000, out=800))
    ago = 5 * DAY
    spans += _work("research-agent", "e4", "Research 4", start_ago=ago,
                   steps=[tool_step(ago - 5, "web_search"),
                          tool_step(ago - 10, "web_search")],
                   close="done", close_ago=ago - 30, cost=priced(inp=2000, out=400))
    database.ingest_spans_with_loops(spans, account_id=acct)
    _classify(job, "Research %")
    return {"job": job, "heavy": [run_id(f"Research {i}") for i in range(4)],
            "light": run_id("Research 4")}


# --- F ---------------------------------------------------------------------

def seed_f(c, token, acct):
    """A real improvement between two comparable periods.

    Last week: 9 started, 4 completed, 5 abandoned. This week: 9 started, 8
    completed, 1 abandoned. Same job, same agent, same volume — so the change
    is not a change in the work mix, and `compare_outcome_mix` can say so
    without anybody computing a rate.
    """
    job = _job(c, token, "Onboarding")
    spans = []
    for i in range(9):  # previous window: 8-13 days ago
        ago = 8 * DAY + i * DAY * 0.5
        done = i < 4
        spans += _work("onboarding-agent", f"fp{i}", f"Onboarding prev {i}",
                       start_ago=ago, steps=[tool_step(ago - 20, "provision")],
                       close="done" if done else None,
                       close_ago=ago - 40, cost=priced())
    for i in range(9):  # current window: 1-6 days ago
        ago = 1 * DAY + i * DAY * 0.5
        done = i < 8
        spans += _work("onboarding-agent", f"fc{i}", f"Onboarding now {i}",
                       start_ago=ago, steps=[tool_step(ago - 20, "provision")],
                       close="done" if done else None,
                       close_ago=ago - 40, cost=priced())
    database.ingest_spans_with_loops(spans, account_id=acct)
    _classify(job, "Onboarding %")
    prev_open = [run_id(f"Onboarding prev {i}") for i in range(4, 9)]
    now_open = [run_id("Onboarding now 8")]
    for rid in prev_open + now_open:
        database.abandon_loop(rid, acct)
    return {"job": job, "prev_abandoned": prev_open, "now_abandoned": now_open}


# --- G ---------------------------------------------------------------------

def seed_g(c, token, acct):
    """Incomplete visibility: the history does not cover the period.

    Every record in this account is under two days old, so five of the seven
    days in the period have nothing in them — not zero work, NO RECORD. One
    agent also stopped emitting a day ago, which is silence and not failure.
    """
    job = _job(c, token, "Payroll run")
    spans = []
    for i in range(3):
        ago = DAY * 1.5 + i * 3600
        spans += _work("payroll-agent", f"g{i}", f"Payroll {i}",
                       start_ago=ago, steps=[tool_step(ago - 20, "bank_file")],
                       close="done", close_ago=ago - 40, cost=priced())
    spans += _work("quiet-agent", "gq", "Quiet task", start_ago=DAY * 1.2,
                   steps=[tool_step(DAY * 1.2 - 10, "ping")],
                   close="done", close_ago=DAY * 1.2 - 20, cost=priced())
    database.ingest_spans_with_loops(spans, account_id=acct)
    _classify(job, "Payroll %")
    return {"job": job, "runs": [run_id(f"Payroll {i}") for i in range(3)],
            "quiet_agent": "quiet-agent"}


# --- H ---------------------------------------------------------------------

def seed_h(c, token, acct):
    """Money, and who may see it.

    Org-wide spend covers two agents; the work scope a reader selects covers
    one. Some spans carry an unpriced model, so coverage is partial and the
    total is a floor. A second reader holds a seat without the Cost surface.
    """
    job = _job(c, token, "Billing sync")
    spans = []
    for i in range(4):
        ago = (i + 1) * DAY * 1.3
        spans += _work("billing-agent", f"h{i}", f"Billing {i}", start_ago=ago,
                       steps=[tool_step(ago - 20, "invoice_api")],
                       close="done", close_ago=ago - 40,
                       cost=priced(inp=4000, out=900))
    for i in range(3):
        ago = (i + 1) * DAY * 1.7
        spans += _work("labs-agent", f"hl{i}", f"Lab task {i}", start_ago=ago,
                       steps=[tool_step(ago - 20, "experiment")],
                       close="done", close_ago=ago - 40,
                       cost=priced(model="some-unlisted-model-v9", inp=50000, out=5000))
    database.ingest_spans_with_loops(spans, account_id=acct)
    _classify(job, "Billing %")
    return {"job": job, "priced_agent": "billing-agent", "unpriced_agent": "labs-agent"}


# --- I ---------------------------------------------------------------------

def seed_i(c, token, acct):
    """A pattern that looks alarming until you read the rest of the record.

    Three payments runs were abandoned in the last two days, which looks like
    something newly broken. Two things contradict that, and both are
    retrievable: the previous window had the same number abandoned out of the
    same number started, and the three failed at THREE DIFFERENT steps, so
    there is no shared cause to point at either.
    """
    job = _job(c, token, "Payments")
    spans = []
    recent_steps = ["card_network", "fraud_check", "ledger_post"]
    for i in range(3):  # recent, abandoned, each at a different step
        ago = DAY * (1 + i * 0.3)
        spans += _work("payments-agent", f"i{i}", f"Payment {i}", start_ago=ago,
                       steps=[tool_step(ago - 20, recent_steps[i], status=2,
                                        msg=f"{recent_steps[i]} returned an error")],
                       cost=priced())
    for i in range(3, 10):  # recent, completed
        ago = DAY * (1 + i * 0.3)
        spans += _work("payments-agent", f"i{i}", f"Payment {i}", start_ago=ago,
                       steps=[tool_step(ago - 20, "card_network")],
                       close="done", close_ago=ago - 40, cost=priced())
    for i in range(10, 20):  # previous window: same 3-in-10 shape
        ago = 8 * DAY + (i - 10) * DAY * 0.4
        failed = i < 13
        spans += _work("payments-agent", f"i{i}", f"Payment {i}", start_ago=ago,
                       steps=[tool_step(ago - 20, recent_steps[(i - 10) % 3],
                                        status=2 if failed else 0)],
                       close=None if failed else "done",
                       close_ago=ago - 40, cost=priced())
    database.ingest_spans_with_loops(spans, account_id=acct)
    _classify(job, "Payment %")
    recent_bad = [run_id(f"Payment {i}") for i in range(3)]
    prev_bad = [run_id(f"Payment {i}") for i in range(10, 13)]
    for rid in recent_bad + prev_bad:
        database.abandon_loop(rid, acct)
    return {"job": job, "recent_abandoned": recent_bad, "prev_abandoned": prev_bad}


# ---------------------------------------------------------------------------
# The rubrics
#
# `acceptable` and `wrong` are regexes matched against the published title,
# explanation, consequence and claim text, lowercased. They are deliberately
# coarse: this measures whether a claim of a given KIND was made, not whether
# particular words were used. Wording is not being graded.
# ---------------------------------------------------------------------------

SCENARIOS: list[dict[str, Any]] = [
    {
        "key": "A",
        "name": "ordinary successful work",
        "seed": seed_a,
        "establishes": [
            "six invoice-sync items were recorded and all six closed as done",
            "no failing step was recorded on any of them",
        ],
        "not_established": [
            "that the invoices were correct, or that the business outcome happened",
            "that six is a good, bad or normal number for this account",
        ],
        "acceptable": [],
        "abstention_ok": True,
        "abstention_preferred": True,
        "wrong": [
            r"\b(failed|failing|broke|broken|stalled|stuck|at risk)\b",
            r"\b(saved|revenue|customers?|business impact|roi)\b",
            r"\b(healthy|all good|everything is (fine|working))\b",
            r"\b(down|outage|degrad)",
        ],
        "paths": ["list_comparable_runs(job)", "compare_outcome_mix(days=7, job)"],
        "financial": True,
    },
    {
        "key": "B",
        "name": "repeated unsuccessful work at a shared step",
        "seed": seed_b,
        "establishes": [
            "four refund items were abandoned",
            "each of the four recorded a failing approval_service step",
            "two other refunds of the same job completed",
        ],
        "not_established": [
            "why the approval call failed",
            "that the approval service was down or is at fault",
            "that every refund is affected — two completed",
        ],
        "acceptable": [r"approval", r"\b(same|shared|each)\b.*\bstep\b", r"\bfour\b|\b4\b"],
        "abstention_ok": False,
        "wrong": [
            r"\b(because|caused by|due to|root cause)\b",
            r"\boutage\b",
            r"\b(all|every) refund",
            r"\b(none|no) refunds? (completed|finished)",
            r"\d+\s?%",
        ],
        "paths": ["list_comparable_runs(job)", "inspect_run(each abandoned)",
                  "compare_outcome_mix(days=7, job)"],
        "financial": True,
    },
    {
        "key": "C",
        "name": "recovered errors are friction, not failed work",
        "seed": seed_c,
        "establishes": [
            "three shipment items each recorded a failing carrier_api step",
            "all three then closed as done",
        ],
        "not_established": [
            "that any shipment work failed or was lost",
            "how much the retry cost in time or money",
        ],
        "acceptable": [r"(recover|retr(y|ied)|despite|then (completed|finished))"],
        "abstention_ok": True,
        "wrong": [
            r"\b(failed to complete|did not complete|work was lost|incomplete)\b",
            r"three shipments? failed",
            r"\b(abandoned|unfinished)\b",
        ],
        "paths": ["inspect_run(each)", "list_comparable_runs(job)"],
        "financial": True,
    },
    {
        "key": "D",
        "name": "work waiting on a person",
        "seed": None,  # takes extra arguments; wired in build_all
        "establishes": [
            "two contract items are open with an unresolved handoff to the reader",
            "one is waiting on a different person",
            "when each handoff was raised",
        ],
        "not_established": [
            "any target or expected response time",
            "that the delay is anyone's fault, or that it caused harm",
        ],
        "acceptable": [r"wait", r"\b(you|your)\b", r"contract"],
        "abstention_ok": True,
        "wrong": [
            r"\b(sla|overdue|late|breach|missed deadline)\b",
            r"\b(ignoring|neglect|unresponsive|failed to respond)\b",
            r"\b(blocking|costing) (the|your) (business|company|team)\b",
        ],
        "paths": ["wait_concentration()", "inspect_run(each)"],
        "financial": True,
    },
    {
        "key": "E",
        "name": "a plausible optimization with evidence both ways",
        "seed": seed_e,
        "establishes": [
            "four research items each recorded eight web_search calls",
            "a fifth recorded two and also completed",
            "the heavy runs carried more input tokens",
        ],
        "not_established": [
            "that any of the eight searches was redundant — no result content is recorded",
            "how much would be saved by making fewer",
            "that the light run answered the same question as the heavy ones",
        ],
        "acceptable": [r"(search|lookup|repeat)", r"(whether|could|may|might|investigate|check)"],
        "abstention_ok": True,
        "wrong": [
            r"\b(wasteful|waste|redundant|unnecessary)\b",
            r"save \$|\$\d+ (a|per) (week|month)|would save",
            r"cheaper model",
            r"\b(should|must) (reduce|cut|switch)\b",
        ],
        "paths": ["list_comparable_runs(agent)", "inspect_run(heavy and light)",
                  "cost_evidence(days=7)"],
        "financial": True,
    },
    {
        "key": "F",
        "name": "a supported positive change",
        "seed": seed_f,
        "establishes": [
            "nine onboarding items started in each of the two windows",
            "four completed in the previous window and eight in the current one",
            "the population is the same job and the same agent in both",
        ],
        "not_established": [
            "why completion improved",
            "that the improvement will continue",
        ],
        "acceptable": [r"(onboarding|complet)", r"(more|improve|rose|up from|better)"],
        "abstention_ok": False,
        "wrong": [
            r"\b(because|caused by|due to)\b",
            r"\b(will|expect|projected|going to) (continue|keep|stay)\b",
            r"\b(doubled|halved)\b",
        ],
        "paths": ["compare_outcome_mix(days=7, job)", "list_comparable_runs(job)"],
        "financial": True,
    },
    {
        "key": "G",
        "name": "incomplete visibility",
        "seed": seed_g,
        "establishes": [
            "three payroll items completed in the last two days",
            "no record exists for the first five days of the period",
            "one agent last emitted about a day ago",
        ],
        "not_established": [
            "that nothing happened on the days with no record",
            "that completions fell, rose, or held steady — there is no baseline",
            "that the quiet agent stopped working",
        ],
        # Deliberately narrow. An earlier draft accepted the bare word
        # "record", which matched boilerplate like "the record shows" and
        # scored a discovery that had not happened.
        "acceptable": [r"payroll"],
        "abstention_ok": True,
        "abstention_preferred": True,
        "wrong": [
            r"\b(fell|dropped|declined|down from|decrease)\b",
            r"\b(no work|nothing) (happened|ran|occurred)\b",
            r"\b(stopped working|is (down|offline|broken)|has failed)\b",
            r"\b(healthy|all clear|nothing to worry)\b",
            r"\d+\s?%",
        ],
        "paths": ["list_comparable_runs(agent)", "compare_outcome_mix(days=7)",
                  "agent_context(quiet-agent)"],
        "financial": True,
    },
    {
        "key": "H",
        "name": "cost, coverage and permission",
        "seed": seed_h,
        "establishes": [
            "organization-wide spend for the period, as a stored total",
            "that pricing coverage is partial — one agent's model is unpriced",
        ],
        "not_established": [
            "what any narrower work scope cost — spend is recorded per account and agent",
            "the true total, because unpriced calls are missing from it",
        ],
        "acceptable": [r"(cost|spend)", r"(organi[sz]ation|account|whole|org-wide|partial|unpriced|coverage)"],
        "abstention_ok": True,
        "wrong": [
            r"(this|the|your) (team|scope|group|selection)('s)?\s+(cost|spend|spent|spending)",
            r"\b(cost|spend) (of|for) (this|the|your) (team|work scope|selection|group)\b",
            r"\btotal (cost|spend) (was|is) \$[\d.]+\b.*\b(exact|complete|all)\b",
        ],
        "paths": ["cost_evidence(days=7)"],
        "financial": True,
        # The same account is read a second time by a seat without Cost.
        "restricted_reader": {
            "wrong": [r"\$", r"\b(cost|spend|spending|price|pricing|dollars?)\b"],
            "note": "no financial content of any kind may reach this reader",
        },
    },
    {
        "key": "I",
        "name": "a counterexample that contradicts the obvious reading",
        "seed": seed_i,
        "establishes": [
            "three payments items were abandoned in the last two days",
            "the previous window also had three abandoned out of ten started",
            "the three recent ones failed at three DIFFERENT steps",
        ],
        "not_established": [
            "that anything is newly wrong — the rate is unchanged",
            "that the three share a cause or a step",
        ],
        "acceptable": [r"payment"],
        "abstention_ok": True,
        "abstention_preferred": True,
        "wrong": [
            r"\b(new|newly|started|began|suddenly|regress|spike|worse)\b",
            r"\b(same|shared|common) (step|cause|failure|error)\b",
            r"\b(than|vs\.?|compared with) last week\b.*\b(worse|more|higher)\b",
            r"\b(deteriorat|declin)",
        ],
        "paths": ["list_comparable_runs(job)", "inspect_run(each recent)",
                  "compare_outcome_mix(days=7, job)"],
        "financial": True,
    },
]


def scenario(key: str) -> dict[str, Any]:
    for s in SCENARIOS:
        if s["key"] == key:
            return s
    raise KeyError(key)


# ---------------------------------------------------------------------------
# Building the fixtures
# ---------------------------------------------------------------------------


def build_all(client, *, only: list[str] | None = None) -> dict[str, dict[str, Any]]:
    """Seed every scenario into its own account. Returns {key: context}."""
    out: dict[str, dict[str, Any]] = {}
    for spec in SCENARIOS:
        key = spec["key"]
        if only and key not in only:
            continue
        email = f"owner-{key.lower()}@eval.test"
        acct = client.post("/auth/signup", json={
            "email": email, "password": "correct horse battery",
            "name": f"Owner {key}", "account_type": "business",
            "org_name": f"Eval {key}",
        }).json()
        token, acct_id = acct["token"], acct["org"]["id"]
        ctx: dict[str, Any] = {
            "key": key, "account_id": acct_id, "token": token,
            "user_id": acct["user"]["id"], "email": email, "spec": spec,
        }
        if key == "D":
            other = f"other-{key.lower()}@eval.test"
            client.post("/org/invites", headers=auth(token),
                        json={"email": other, "name": "Dana Otter"})
            ctx["ids"] = seed_d(client, token, acct_id, email, other)
        else:
            ctx["ids"] = spec["seed"](client, token, acct_id)
        if key == "H":
            ctx["restricted"] = _restricted_reader(client, token, acct)
        # Abandonment is stamped last, so align after every seed has finished
        # opening, closing and abandoning its work.
        align_to_spans(acct_id)
        out[key] = ctx
    return out


def _restricted_reader(client, token, acct) -> dict[str, Any]:
    """A second person on a seat that does not carry Cost.

    Built through the real org API — roles, a scope level, an invite — because
    a hand-made seat would prove nothing about the gate that actually runs.
    """
    levels = {l["key"]: l for l in
              client.get("/org/scope-levels", headers=auth(token)).json()}
    top = client.post("/org/roles", headers=auth(token), json={
        "title": "Head", "scope_level_id": levels["exec"]["id"]}).json()
    client.post(f"/org/roles/{top['id']}/members", headers=auth(token),
                json={"user_id": acct["user"]["id"]})
    ic_role = client.post("/org/roles", headers=auth(token), json={
        "title": "Analyst", "parent_role_id": top["id"],
        "scope_level_id": levels["ic"]["id"]}).json()
    inv = client.post("/org/invites", headers=auth(token), json={
        "email": "analyst-h@eval.test", "name": "Ana Lyst",
        "role_id": ic_role["id"]}).json()
    accepted = client.post("/auth/accept-invite", json={
        "token": inv["invite_url"].split("token=")[1],
        "name": "Ana Lyst", "password": "correct horse battery"}).json()
    # Give them the billing agent, so their seat SEES work while still having
    # no Cost surface. Without this the reader's scope is empty and the money
    # gate is never really tested — an empty page leaks nothing.
    client.put("/agents/billing-agent/owner", headers=auth(token),
               json={"agent_id": "main", "user_id": accepted["user"]["id"]})
    return {"token": accepted["token"], "user_id": accepted["user"]["id"]}


# ---------------------------------------------------------------------------
# Discoverability — model-free, and a precondition for discovery
# ---------------------------------------------------------------------------

PROBES: dict[str, Callable[[Any, dict[str, Any]], dict[str, Any]]] = {}


def probe(ctx: dict[str, Any], *, financial_visible: bool = True) -> dict[str, Any]:
    """Retrieve what a model WOULD be able to read, and report what it shows.

    This runs the real allowlist against the real records with no model in the
    loop. It answers the question that has to be true before discovery is even
    possible: is the evidence reachable, within budget, through the tools the
    investigation actually has?

    A scenario that fails here is a retrieval gap, and no amount of prompting
    fixes it.
    """
    ids = ctx["ids"]
    session = investigation_tools.InvestigationSession(
        account_id=ctx["account_id"], only_user_ids=None,
        financial_visible=financial_visible,
        budget=investigation_tools.ToolBudget(max_calls=40, max_rows=4000),
    )
    seen: dict[str, Any] = {}
    job = ids.get("job")
    if job:
        seen["runs"] = session.retrieve(
            "list_comparable_runs", {"job_id": job, "limit": 50})
        seen["mix"] = session.retrieve(
            "compare_outcome_mix", {"days": 7, "job_id": job})
    for name in ("runs", "stalled", "finished", "heavy", "recent_abandoned",
                 "waiting_on_viewer", "prev_abandoned"):
        for rid in (ids.get(name) or []):
            seen.setdefault("inspected", {})[rid] = session.retrieve(
                "inspect_run", {"run_id": rid})
    if ids.get("light"):
        seen.setdefault("inspected", {})[ids["light"]] = session.retrieve(
            "inspect_run", {"run_id": ids["light"]})
    if ctx["key"] == "D":
        seen["waits"] = session.retrieve("wait_concentration", {"limit": 50})
    if ctx["key"] == "G":
        seen["agent"] = session.retrieve(
            "agent_context", {"agent": ids["quiet_agent"]})
    if financial_visible and ctx["key"] in ("E", "H"):
        seen["cost"] = session.retrieve("cost_evidence", {"days": 7})
    return {"session": session, "seen": seen,
            "coverage": session.retrieval_report(),
            "budget": session.budget.report()}


# ---------------------------------------------------------------------------
# Scoring a published finding set
# ---------------------------------------------------------------------------


def finding_text(f: dict[str, Any]) -> str:
    parts = [str(f.get("title") or ""), str(f.get("explanation") or ""),
             str(f.get("consequence") or "")]
    for c in f.get("claims") or []:
        parts.append(str(c.get("text") or ""))
    for u in f.get("uncertainty") or []:
        parts.append(str(u))
    step = f.get("next_step") or {}
    parts.append(str(step.get("text") or ""))
    return " ".join(parts).lower()


def score(spec: dict[str, Any], findings: list[dict[str, Any]], *,
          restricted: bool = False) -> dict[str, Any]:
    """Judge one scenario's published findings against its rubric.

    Discovery and truth are kept apart on purpose:

    * `false_claims` are things the records do not support. One is a failure
      however good the rest of the output is.
    * `discovered` says whether the useful pattern was found. Missing it is a
      different failure, and on a scenario where abstention is correct it is
      not a failure at all.
    """
    import re

    rules = spec.get("restricted_reader") if restricted else spec
    wrong = (rules or {}).get("wrong") or []
    acceptable = [] if restricted else (spec.get("acceptable") or [])

    result: dict[str, Any] = {
        "key": spec["key"], "name": spec["name"],
        "published": len(findings), "abstained": not findings,
        "false_claims": [], "discovered": None, "notes": [],
        "titles": [f.get("title") for f in findings],
    }

    for f in findings:
        text = finding_text(f)
        for pat in wrong:
            if re.search(pat, text, re.I):
                result["false_claims"].append(
                    {"title": f.get("title"), "matched": pat})

    if not findings:
        result["discovered"] = None if spec.get("abstention_ok") else False
        if spec.get("abstention_ok"):
            result["notes"].append("abstained, which this scenario allows")
        else:
            result["notes"].append("published nothing on a scenario with a real pattern")
        return result

    if spec.get("abstention_preferred") and not result["false_claims"]:
        result["notes"].append(
            "published where silence was the preferred answer; not a false claim, "
            "but check that it adds something beyond the visible totals")

    if acceptable:
        hits = [pat for pat in acceptable
                if any(re.search(pat, finding_text(f), re.I) for f in findings)]
        result["discovered"] = len(hits) == len(acceptable)
        result["matched_expectations"] = hits
        if not result["discovered"]:
            result["notes"].append(
                "published, but did not state the pattern the records establish: "
                + ", ".join(p for p in acceptable if p not in hits))
    return result
