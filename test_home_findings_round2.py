"""Six integration defects between retrieval, validation, scheduling and publication.

Every one of these lived in a SEAM. The helpers on either side behaved; what
was wrong was what one handed the other, so each section here drives the
production path — a real tool call, a real fit, the real validator, the real
`run_one()`, the real endpoint — rather than asserting that a helper called
with a hand-built argument returns what it was told.

  1. RETRIEVAL LIMITS DID NOT REACH VALIDATION. A tool returned 30 rows, size
     fitting delivered 15, and `session.budget.report()` still said
     `complete: true` — so "no other job is affected" validated as supported
     over half a result.
  2. A BROKEN ANALYSIS LOOKED LIKE AN EMPTY ONE. An unparseable discovery reply
     became `[]`, which became "no candidate worth investigating", which
     retired a standing finding. An invalid response is not evidence that
     nothing matters.
  3. OWNERSHIP WAS CHECKED WHERE IT DID NOT MATTER. The claim was verified
     before the model work and the findings were written during it, so a
     superseded worker had already published by the time `run_one()` returned
     `superseded`.
  4. FRESHNESS WAS REPORTED FROM THE WRONG VERSION. Evidence arriving in the
     same scheduling bucket left the endpoint saying `state: current` with the
     NEW version, over an analysis that had read the old one.
  5. PENDING WORK WAS BOUNDED BY THE WRONG KEY. Two reads a bucket apart
     queued two jobs for one audience; the completed-job debounce cannot stop
     that, because it only looks at analyses that finished.
  6. TRIMMING ONE RESPONSE FORGOT ANOTHER'S EVIDENCE. `forget_beyond` dropped
     every ledger entry of a kind outside the current response, so a run
     delivered by call 1 vanished when call 3's list was trimmed.

Run:
  OVERSEE_DISABLE_PRICING_SYNC=1 python3 test_home_findings_round2.py
(isolated temp SQLite DB; every model call scripted; no network, no live model)
"""
import json
import os
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone

os.environ.update({
    "OVERSEE_DISABLE_PRICING_SYNC": "1",
    "TROVIS_DISABLE_ALERTS": "1",
    "TROVIS_DISABLE_LOOP_SWEEP": "1",
    "TROVIS_DISABLE_ANALYSIS": "1",
    "TROVIS_LOOP_TITLES": "off",
    "ANTHROPIC_API_KEY": "test-key-not-used-for-network",
    "TROVIS_ANALYSIS_DEBOUNCE_S": "0",
})
os.environ.pop("DATABASE_URL", None)

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()

import database
database.SQLITE_PATH = _tmp.name

import analysis_jobs
import findings as findings_mod
import home_snapshot
import investigation_tools
import investigator
import main
from fastapi.testclient import TestClient

main._auto_describe = lambda *a, **k: False

failures = []


def check(label, cond):
    print(("  PASS " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


# --- scripted model --------------------------------------------------------
class _Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Resp:
    def __init__(self, content, stop_reason="end_turn"):
        self.content = content
        self.stop_reason = stop_reason


def _text(payload):
    return _Resp([_Block(type="text", text=json.dumps(payload))])


def _raw(text):
    return _Resp([_Block(type="text", text=text)])


def _tool(name, inp, cid="c"):
    return _Resp([_Block(type="tool_use", id=cid, name=name, input=inp)], "tool_use")


_STEPS = (
    ("You are Trovis, examining", "discovery"),
    ("You are Trovis, investigating", "investigation"),
    ("You are Trovis, checking", "assessment"),
    ("You are Trovis, ordering", "ranking"),
    ("You are Trovis, rewriting", "revision"),
)


class FakeMessages:
    def __init__(self, owner):
        self.owner = owner

    def create(self, **kw):
        sysx = kw.get("system") or ""
        step = next((n for p, n in _STEPS if sysx.startswith(p)), "composition")
        self.owner.seen.append(step)
        script = self.owner.script.get(step)
        if callable(script):
            return script(kw, self.owner)
        return script if script is not None else _text({})


class FakeAnthropic:
    def __init__(self, script):
        self.script = script
        self.messages = FakeMessages(self)
        self.seen = []
        self.counts = {}


def install(script):
    client = FakeAnthropic(script)
    investigator._client = lambda: client
    return client


NS = 10**9
NOW = time.time_ns()
_n = [0]


def auth(tok):
    return {"Authorization": f"Bearer {tok}"}


def span(svc, off, attrs, name="message_received", status=0, msg=""):
    _n[0] += 1
    t = NOW - int(off) * NS
    return {
        "trace_id": f"t{_n[0]:028d}", "span_id": f"s{_n[0]:014d}",
        "parent_span_id": None, "service_name": svc, "agent_id": "main",
        "span_name": name, "kind": 1, "start_time_unix": t,
        "end_time_unix": t + 10**6, "status_code": status, "status_message": msg,
        "attributes": attrs, "resource_attributes": {},
    }


def tid(external_id):
    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute(
            f"SELECT id FROM loops WHERE external_id = {database.PH}", (external_id,))
        r = cur.fetchone()
        return int(r["id"]) if r else None


def clear(acct):
    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute(f"DELETE FROM findings WHERE account_id = {database.PH}", (acct,))
        cur.execute(f"DELETE FROM analysis_jobs WHERE account_id = {database.PH}", (acct,))


def finding_rows(acct):
    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute(
            "SELECT finding_key, state, title, analysis_id, evidence_version "
            f"FROM findings WHERE account_id = {database.PH} ORDER BY id",
            (acct,))
        return [dict(r) for r in cur.fetchall()]


def pending_count(acct, scope):
    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute(
            f"SELECT COUNT(*) AS n FROM analysis_jobs WHERE account_id = {database.PH} "
            f"AND scope_key = {database.PH} AND status IN ('queued', 'running')",
            (acct, scope))
        return int(cur.fetchone()["n"])


def make_stale(job_id, seconds=None):
    old = (datetime.now(timezone.utc)
           - timedelta(seconds=seconds or database.ANALYSIS_JOB_STALE_S + 60)
           ).strftime("%Y-%m-%d %H:%M:%S")
    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute(
            f"UPDATE analysis_jobs SET heartbeat_at = {database.PH}, "
            f"started_at = {database.PH} WHERE id = {database.PH}",
            (old, old, job_id))


with TestClient(main.app) as c:
    a = c.post("/auth/signup", json={
        "email": "ceo@round2.test", "password": "correct horse battery",
        "name": "Cleo", "account_type": "business", "org_name": "Acme"}).json()
    ACCT, CEO = a["org"]["id"], a["token"]
    ceo_id = a["user"]["id"]

    job = c.post("/workflows", headers=auth(CEO), json={
        "name": "Refunds", "description": "d",
        "steps": [{"step_type": "agent", "label": "run"}]}).json()

    # Three refund runs that stopped at the same approval call.
    for i in range(3):
        e = f"r{i}"
        database.ingest_spans_with_loops([
            span("refunds-agent", 5000 - i, {
                "trovis.loop.title": f"Refund {i}", "trovis.loop.external_id": e}),
            span("refunds-agent", 4900 - i, {
                "trovis.loop.external_id": e, "trovis.tool.name": "approval"},
                name="tool_call", status=2, msg="approval service timed out"),
        ], account_id=ACCT)
    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute(
            f"UPDATE loops SET workflow_id = {database.PH} "
            f"WHERE external_id IN ('r0','r1','r2')", (job["id"],))
    RUNS = [tid(f"r{i}") for i in range(3)]
    for r in RUNS:
        database.abandon_loop(r, ACCT)

    def snapshot_now(days=7, tz="UTC"):
        period = home_snapshot.resolve_period(days, tz)
        seat = database.resolve_seat(ACCT, ceo_id)
        return home_snapshot.build_snapshot(
            account_id=ACCT, viewer_user_id=ceo_id,
            selection={"choice": "everyone", "person_id": None, "user_ids": None,
                       "clamped": False, "unreadable": False,
                       "requested": "everyone", "seat": seat},
            period=period)

    # =====================================================================
    print("\n=== 1. real retrieval limits reach validation ===")
    # =====================================================================
    # 40 runs behind one agent, so a genuine `list_comparable_runs` call both
    # hits the query's own row cap AND overflows the size budget. Nothing
    # about the partial result is hand-built: the session's coverage comes
    # from the retrieval that actually happened.
    for i in range(40):
        database.ingest_spans_with_loops([
            span("bulk-agent", 3000 - i, {
                "trovis.loop.title": ("Bulk item with a deliberately long "
                                      "title so the payload overruns ") * 3 + str(i),
                "trovis.loop.external_id": f"bulk{i}"}),
        ], account_id=ACCT)

    sess = investigation_tools.InvestigationSession(
        account_id=ACCT, only_user_ids=None, financial_visible=False)
    payload = sess.run("list_comparable_runs", {"agent": "bulk-agent", "limit": 50})
    retrieved = len(payload["runs"])
    sent, blob = sess.fit(payload)
    delivered = len(sent["runs"])

    check("the tool retrieved more rows than the size budget could deliver",
          retrieved > delivered > 0)
    check("the budget alone still calls itself complete (it only knows exhaustion)",
          sess.budget.report()["budget_complete"] is True)
    report = sess.retrieval_report()
    check("the SESSION knows the search was partial",
          report["complete"] is False)
    check("and names why, rather than implying it",
          "size_trimmed" in report["limitation_kinds"])
    # The QUERY's own cap is a separate source, with no size budget involved.
    cap_sess = investigation_tools.InvestigationSession(
        account_id=ACCT, only_user_ids=None, financial_visible=False)
    capped = cap_sess.run("list_comparable_runs", {"agent": "bulk-agent", "limit": 20})
    check("a capped query reports its own truncation", capped["truncated"] is True)
    check("the query's own row cap is carried too",
          "query_row_cap" in cap_sess.retrieval_report()["limitation_kinds"]
          and cap_sess.retrieval_report()["complete"] is False)

    snap = snapshot_now()
    live_run = sent["runs"][0]["run_id"]
    base_claim = {
        "kind": "observation",
        "evidence": [f"run:{live_run}"],
    }

    def draft(text, title="Bulk work stalled", **over):
        d = {
            "title": title,
            "explanation": "Something about the bulk agent's recent work.",
            "category": "attention", "claim_kind": "observation",
            "confidence": "supported",
            "claims": [{"text": text, **base_claim}],
            "entities": [{"kind": "run", "id": live_run, "label": "one run"}],
            "evidence": [{"kind": "run", "ref": str(live_run), "note": "read"}],
            "next_step": {"kind": "review_runs", "text": "Open it."},
        }
        d.update(over)
        return d

    def validate(d, retrieval):
        try:
            return findings_mod.validate_finding(
                d, snapshot=snap, evidence_index=sess.evidence,
                calculations=sess.calculations, financial_visible=False,
                retrieval=retrieval), None
        except findings_mod.FindingRejected as exc:
            return None, exc.reasons

    ok, why = validate(
        draft("No other job is affected by this."), report)
    check("an exhaustive claim CANNOT pass over the real partial result",
          ok is None and any("incomplete search" in r for r in (why or [])))
    ok, why = validate(draft("Only 40% of these runs finished."), report)
    check("a percentage over the same partial result is refused too",
          ok is None)
    ok, why = validate(
        draft("This run stopped after the approval call failed."), report)
    check("an exact observation about a record we DID read survives",
          ok is not None)
    check("and it is published qualified, carrying the limitation",
          ok and ok["confidence"] == "qualified"
          and ok["coverage"]["retrieval_complete"] is False
          and "size_trimmed" in ok["coverage"]["retrieval_limitations"])

    # A graphic drawn from the same partial result is stamped, and an
    # exhaustive label on it is refused.
    calc_sess = investigation_tools.InvestigationSession(
        account_id=ACCT, only_user_ids=None, financial_visible=False)
    mix = calc_sess.run("compare_outcome_mix", {"days": 7, "job_id": job["id"]})
    calc_sess.fit(mix)
    calc_id = mix["calculation_ids"]["current_abandoned"]
    graphic_draft = draft(
        "Three refund runs were given up on.",
        claims=[{"text": "Three runs were abandoned.", "kind": "calculation",
                 "value": float(mix["current"]["abandoned"]),
                 "metric_ref": f"calc:{calc_id}",
                 "evidence": [f"run:{live_run}"]}],
    )
    graphic_draft["graphic"] = {
        "kind": "run_outcome_split",
        "series": [{"label": "Total abandoned", "metric_ref": f"calc:{calc_id}"}]}
    merged = dict(sess.calculations)
    merged.update(calc_sess.calculations)
    try:
        findings_mod.validate_finding(
            graphic_draft, snapshot=snap, evidence_index=sess.evidence,
            calculations=merged, financial_visible=False, retrieval=report)
        gfx_rejected = []
    except findings_mod.FindingRejected as exc:
        gfx_rejected = exc.reasons
    check("a chart label claiming a whole is refused over a partial search",
          any("implies a complete picture" in r for r in gfx_rejected))

    graphic_draft["graphic"]["series"][0]["label"] = "Given up on"
    try:
        validated_gfx = findings_mod.validate_finding(
            graphic_draft, snapshot=snap, evidence_index=sess.evidence,
            calculations=merged, financial_visible=False, retrieval=report)
    except findings_mod.FindingRejected as exc:
        validated_gfx = None
        print("     (unexpected rejection:", exc.reasons, ")")
    check("a neutral label passes, and the point is marked partial",
          validated_gfx
          and validated_gfx["graphic"]["series"][0]["partial"] is True)
    check("the calculation derived from it is marked a floor",
          validated_gfx and validated_gfx["claims"][0]["partial"] is True
          and validated_gfx["claims"][0]["qualifier"] == "at_least")

    # A failed tool leaves a question open — that is incomplete coverage too,
    # with no budget or size involvement at all.
    fail_sess = investigation_tools.InvestigationSession(
        account_id=ACCT, only_user_ids=None, financial_visible=False)
    fail_sess.run("inspect_run", {"run_id": 99999999})
    check("a retrieval that returned nothing usable counts as incomplete",
          fail_sess.retrieval_report()["complete"] is False
          and "retrieval_failed" in fail_sess.retrieval_report()["limitation_kinds"])

    # And the snapshot's own incompleteness still governs, independently.
    partial_snap = json.loads(json.dumps(snap, default=str))
    partial_snap["completeness"]["counts_exact"] = False
    clean = investigation_tools.InvestigationSession(
        account_id=ACCT, only_user_ids=None, financial_visible=False)
    clean.fit(clean.run("list_comparable_runs", {"job_id": job["id"]}))
    cov = findings_mod.derive_coverage(
        partial_snap, retrieval=clean.retrieval_report())
    check("an incomplete SCOPE blocks exhaustive claims even on a whole retrieval",
          clean.retrieval_report()["complete"] is True
          and cov["supports_exhaustive_claims"] is False)
    check("a retrieval report with no verdict fails CLOSED",
          findings_mod.derive_coverage(
              snap, retrieval={"tool_calls": 1})["retrieval_complete"] is False)

    # =====================================================================
    print("\n=== 2. a failed analysis is not an empty one ===")
    # =====================================================================
    DISCOVER = _text({"candidates": [{
        "topic": "refund-approval-stall", "question": "why did three runs stop?",
        "hypothesis": "they stopped at the same approval step",
        "category": "attention", "why_this_reader": "owner",
        "evidence_needed": ["the runs"]}]})

    def investigation(kw, o):
        k = o.counts.get("t", 0)
        o.counts["t"] = k + 1
        if k == 0:
            return _tool("list_comparable_runs", {"job_id": job["id"]}, "1")
        return _text({
            "verdict": "supported",
            "summary": "Three refund runs stopped at the same approval step.",
            "for": [f"run:{r}" for r in RUNS], "against": [],
            "alternatives_considered": ["a one-off outage"],
            "unknown": ["whether the approval service was down"],
            "sample": {"observed": 3, "comparable": 3}})

    SUPPORTED = {
        "title": "Three refund runs stopped at the same approval step",
        "explanation": ("Refund 0, 1 and 2 each stopped after the approval call "
                        "failed and were closed without finishing."),
        "consequence": "Three refunds were given up on.",
        "claims": [{"text": "Three runs stopped after the approval call failed.",
                    "kind": "observation",
                    "evidence": [f"run:{r}" for r in RUNS]}],
        "entities": [{"kind": "run", "id": r, "label": f"Refund {i}"}
                     for i, r in enumerate(RUNS)],
        "evidence": [{"kind": "run", "ref": str(r), "note": "stopped at approval"}
                     for r in RUNS],
        "uncertainty": ["whether the approval service was down"],
        "next_step": {"kind": "review_runs", "text": "Open the three refunds."},
        "graphic": {"kind": "none"},
    }
    GOOD = {
        "discovery": DISCOVER, "investigation": investigation,
        "composition": _text(SUPPORTED),
        "assessment": _text({"decision": "publish", "reason": "ok",
                             "claim_kind": "observation",
                             "confidence": "supported"}),
        "ranking": _text({"order": [{"index": 0, "score": 1.0, "reason": "x"}]}),
    }

    def read(days=7, tz="UTC"):
        return c.get(f"/home/findings?days={days}&tz={tz}", headers=auth(CEO)).json()

    def publish_one():
        """Drive a real analysis to publication through the queue."""
        install(GOOD)
        read()
        analysis_jobs.drain(4)
        return read()

    clear(ACCT)
    first = publish_one()
    check("a finding is published through the queue",
          len(first["findings"]) == 1)
    check("and the analysis reports itself complete",
          first["analysis"]["state"] == "current"
          and first["analysis"]["analysis_outcome"] == "complete")
    published_key = finding_rows(ACCT)[0]["finding_key"]
    published_analysis = finding_rows(ACCT)[0]["analysis_id"]

    def refresh_with(script, label, expect_outcome):
        """Force a fresh job for this audience, run it, and report."""
        # Move the evidence on so a new job key exists, then run the queue
        # with the failing script.
        database.ingest_spans_with_loops(
            [span("noise-agent", 0, {"trovis.loop.external_id": f"n-{label}"})],
            account_id=ACCT)
        install(script)
        before = read()
        analysis_jobs.drain(4)
        after = read()
        rows = finding_rows(ACCT)
        check(f"{label}: the standing finding is NOT retired",
              any(r["finding_key"] == published_key and r["state"] == "open"
                  for r in rows))
        check(f"{label}: it is still served",
              any(f["id"] for f in after["findings"]))
        check(f"{label}: the state is not reported as a current analysis",
              after["analysis"]["state"] != "current")
        check(f"{label}: and the findings are flagged as an earlier analysis's",
              after["analysis"]["findings_from_previous_analysis"] is True)
        return after

    # (a) MALFORMED DISCOVERY — the original reproduction.
    refresh_with({**GOOD, "discovery": _raw("not json at all")},
                 "malformed discovery", "discovery_unusable")

    # (b) DISCOVERY WITH A MISSING candidates KEY — "no candidates" must be
    #     said, not inferred from a reply we could not read.
    refresh_with({**GOOD, "discovery": _text({"note": "hmm"})},
                 "discovery missing candidates", "discovery_unusable")

    # (c) DISCOVERY WITH UNUSABLE ENTRIES.
    refresh_with({**GOOD, "discovery": _text({"candidates": [{"nope": 1}]})},
                 "discovery entries unusable", "discovery_unusable")

    # (d) MISSING / INVALID ASSESSMENT.
    refresh_with({**GOOD, "assessment": _raw("???")},
                 "unusable assessment", "complete")

    # (e) FAILED RETRIEVAL that leaves the verdict unusable.
    refresh_with({**GOOD, "investigation": _raw("no verdict here")},
                 "no usable verdict", "complete")

    # (f) DEADLINE EXHAUSTION.
    os.environ["TROVIS_INVESTIGATION_WALL_S"] = "0.001"
    try:
        refresh_with(GOOD, "deadline exhausted", "deadline")
    finally:
        os.environ.pop("TROVIS_INVESTIGATION_WALL_S", None)

    # (g) INTERRUPTED — the worker raises partway through.
    def boom(kw, o):
        raise RuntimeError("model transport died")

    refresh_with({**GOOD, "investigation": boom}, "worker raised", "interrupted")

    # A LEGITIMATE ABSTENTION, tested separately, DOES retire. Isolated from
    # the failure paths above, which deliberately leave retries in the queue.
    clear(ACCT)
    publish_one()
    database.ingest_spans_with_loops(
        [span("noise-agent", 0, {"trovis.loop.external_id": "n-abstain"})],
        account_id=ACCT)
    install({**GOOD, "discovery": _text({"candidates": []})})
    read()
    analysis_jobs.drain(4)
    quiet = read()
    rows = finding_rows(ACCT)
    check("a real abstention DOES retire what it did not republish",
          all(r["state"] == "superseded" for r in rows))
    check("and it reads as a completed analysis, not a failure",
          quiet["analysis"]["state"] == "current"
          and quiet["analysis"]["analysis_outcome"] == "complete"
          and quiet["findings"] == [])

    # A bounded run that skipped a candidate does not retire either.
    clear(ACCT)
    publish_one()
    key_before = finding_rows(ACCT)[0]["finding_key"]
    database.ingest_spans_with_loops(
        [span("noise-agent", 0, {"trovis.loop.external_id": "n-withheld"})],
        account_id=ACCT)
    install({**GOOD,
             "assessment": _text({"decision": "narrow", "reason": "overstated",
                                  "overstated_phrases": ["all"],
                                  "claim_kind": "observation",
                                  "confidence": "qualified"}),
             "revision": _text({"withdraw": True})})
    read()
    analysis_jobs.drain(4)
    check("a rewrite that could not be narrowed does not retire the condition",
          any(r["finding_key"] == key_before and r["state"] == "open"
              for r in finding_rows(ACCT)))

    # =====================================================================
    print("\n=== 3. ownership is enforced at the writes ===")
    # =====================================================================
    clear(ACCT)
    publish_one()
    standing = finding_rows(ACCT)
    check("a finding stands before the race", len(standing) == 1
          and standing[0]["state"] == "open")
    standing_key = standing[0]["finding_key"]
    standing_analysis = standing[0]["analysis_id"]

    stolen = {"job_id": None, "new_token": None}

    def steal_mid_investigation(kw, o):
        """Transfer ownership WHILE the old worker is inside a model call."""
        k = o.counts.get("t", 0)
        o.counts["t"] = k + 1
        if k == 0 and stolen["job_id"] is None:
            with database._connect() as conn, database._cursor(conn) as cur:
                cur.execute(
                    f"SELECT id FROM analysis_jobs WHERE account_id = {database.PH} "
                    "AND status = 'running' ORDER BY id DESC LIMIT 1", (ACCT,))
                row = cur.fetchone()
            if row is not None:
                stolen["job_id"] = int(row["id"])
                make_stale(stolen["job_id"])
                taken = database.claim_analysis_job()
                stolen["new_token"] = taken["claim_token"] if taken else None
        return investigation(kw, o)

    database.ingest_spans_with_loops(
        [span("noise-agent", 0, {"trovis.loop.external_id": "n-steal"})],
        account_id=ACCT)
    install({**GOOD, "investigation": steal_mid_investigation})
    read()
    outcome = analysis_jobs.run_one()

    check("the claim really was transferred mid-investigation",
          stolen["job_id"] is not None and stolen["new_token"] is not None)
    check("the old worker reports superseded through run_one()",
          outcome is not None and outcome["status"] == "superseded")
    after_rows = finding_rows(ACCT)
    check("it published NOTHING (no new finding row)",
          len(after_rows) == 1 and after_rows[0]["finding_key"] == standing_key)
    check("it wrote nothing over the standing finding",
          after_rows[0]["analysis_id"] == standing_analysis)
    check("it retired nothing", after_rows[0]["state"] == "open")

    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute(
            f"SELECT status, attempts, result FROM analysis_jobs WHERE id = {database.PH}",
            (stolen["job_id"],))
        jrow = dict(cur.fetchone())
    check("it did not finish the replacement's job",
          jrow["status"] == "running" and jrow["result"] is None)
    check("and it did not requeue it either",
          int(jrow["attempts"]) == stolen["new_token"])

    # The same, but the old worker RAISES instead of returning.
    raised = {"job_id": None, "new_token": None}

    def steal_then_raise(kw, o):
        k = o.counts.get("t", 0)
        o.counts["t"] = k + 1
        if k == 0 and raised["job_id"] is None:
            with database._connect() as conn, database._cursor(conn) as cur:
                cur.execute(
                    f"SELECT id FROM analysis_jobs WHERE account_id = {database.PH} "
                    "AND status = 'running' ORDER BY id DESC LIMIT 1", (ACCT,))
                row = cur.fetchone()
            if row is not None:
                raised["job_id"] = int(row["id"])
                make_stale(raised["job_id"])
                taken = database.claim_analysis_job()
                raised["new_token"] = taken["claim_token"] if taken else None
        raise RuntimeError("transport died after the takeover")

    # Reset the stolen job so the queue has one claimable row again.
    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute(f"UPDATE analysis_jobs SET status = 'done' WHERE account_id = {database.PH}",
                    (ACCT,))
    database.ingest_spans_with_loops(
        [span("noise-agent", 0, {"trovis.loop.external_id": "n-raise"})],
        account_id=ACCT)
    install({**GOOD, "investigation": steal_then_raise})
    read()
    outcome2 = analysis_jobs.run_one()
    check("a superseded worker that RAISES is also fenced out",
          raised["job_id"] is not None and outcome2 is not None
          and outcome2["status"] == "superseded")
    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute(
            f"SELECT status, attempts FROM analysis_jobs WHERE id = {database.PH}",
            (raised["job_id"],))
        jrow2 = dict(cur.fetchone())
    check("its crash did not requeue the replacement's live job",
          jrow2["status"] == "running"
          and int(jrow2["attempts"]) == raised["new_token"])
    check("and still nothing was published or retired",
          [r["state"] for r in finding_rows(ACCT)] == ["open"])

    # The commit helper itself, directly: a stale token writes nothing.
    fenced = database.enqueue_analysis_job(
        ACCT, "fence-direct", {"scope_key": "fence-scope"},
        scope_key="fence-scope", evidence_version="v1")
    claim = database.claim_analysis_job()
    victim = dict(finding_rows(ACCT)[0])
    stale_commit = database.commit_analysis_publication(
        fenced["id"], (claim["claim_token"] or 0) + 99, ACCT, "fence-scope",
        "bogus-analysis", {"records": [], "keep_keys": [], "retire": True},
        status="done")
    check("a stale claim token commits nothing at all",
          stale_commit["committed"] is False
          and stale_commit["published"] == 0 and stale_commit["retired"] == 0)
    check("and the job it does not own is untouched",
          database.analysis_job_status(ACCT, "fence-direct")["status"] == "running")
    good_commit = database.commit_analysis_publication(
        fenced["id"], claim["claim_token"], ACCT, "fence-scope", "real-analysis",
        {"records": [], "keep_keys": [], "retire": False}, status="done",
        analyzed_evidence_version="v1")
    check("the current claim commits", good_commit["committed"] is True)
    check("a second commit on the same token is refused (the job is closed)",
          database.commit_analysis_publication(
              fenced["id"], claim["claim_token"], ACCT, "fence-scope", "again",
              {"records": [], "keep_keys": [], "retire": True},
              status="done")["committed"] is False)
    check("the unrelated audience's finding survived all of it",
          finding_rows(ACCT)[0]["state"] == victim["state"])

    # =====================================================================
    print("\n=== 4. freshness is reported from what was analysed ===")
    # =====================================================================
    clear(ACCT)
    publish_one()
    live = read()
    check("right after an analysis, analysed and observed agree",
          live["analysis"]["state"] == "current"
          and live["analysis"]["analyzed_evidence_version"]
          == live["analysis"]["evidence_version"])

    # SAME BUCKET. The scheduling key does not move; the records did.
    before_v = live["analysis"]["evidence_version"]
    for i in range(3):
        database.ingest_spans_with_loops(
            [span("burst-agent", 0, {"trovis.loop.external_id": f"sb{i}"})],
            account_id=ACCT)
    same = read()
    check("same-bucket evidence changes the exact version",
          same["analysis"]["evidence_version"] != before_v)
    check("the endpoint does NOT call that current",
          same["analysis"]["state"] != "current")
    check("it reports what was analysed, not what was observed",
          same["analysis"]["analyzed_evidence_version"] == before_v)
    check("and says newer evidence is waiting",
          same["analysis"]["newer_evidence_available"] is True)

    # CHANGES WHILE QUEUED.
    queued_state = read()
    check("a refresh is queued for the change",
          queued_state["analysis"]["state"] in ("queued", "running"))
    queued_v = queued_state["analysis"]["evidence_version"]
    database.ingest_spans_with_loops(
        [span("burst-agent", 0, {"trovis.loop.external_id": "while-queued"})],
        account_id=ACCT)
    while_queued = read()
    check("evidence arriving while queued does not stamp the pending job",
          while_queued["analysis"]["state"] in ("queued", "running")
          and while_queued["analysis"]["evidence_version"] != queued_v)
    check("and the findings on screen are still flagged as an earlier analysis's",
          while_queued["analysis"]["findings_from_previous_analysis"] is True)

    # CHANGES DURING EXECUTION — inserted while the worker is inside a model
    # call, not after it has finished.
    during = {"version_at_start": None, "inserted": False}

    def ingest_mid_investigation(kw, o):
        k = o.counts.get("t", 0)
        o.counts["t"] = k + 1
        if k == 0 and not during["inserted"]:
            during["version_at_start"] = database.evidence_version(ACCT)["version"]
            database.ingest_spans_with_loops(
                [span("burst-agent", 0,
                      {"trovis.loop.external_id": "mid-execution"})],
                account_id=ACCT)
            during["inserted"] = True
        return investigation(kw, o)

    install({**GOOD, "investigation": ingest_mid_investigation})
    analysis_jobs.drain(4)
    mid = read()
    check("evidence really did arrive during execution",
          during["inserted"] and during["version_at_start"] is not None
          and database.evidence_version(ACCT)["version"]
          != during["version_at_start"])
    check("the analysis is not reported as covering what arrived after it read",
          mid["analysis"]["state"] != "current"
          or mid["analysis"]["analyzed_evidence_version"]
          == mid["analysis"]["evidence_version"])
    check("a later read eventually queues the follow-up",
          read()["analysis"]["state"] in ("queued", "running", "current"))
    analysis_jobs.drain(4)
    settled = read()
    check("and once it runs, analysed matches observed again",
          settled["analysis"]["analyzed_evidence_version"]
          == settled["analysis"]["evidence_version"]
          and settled["analysis"]["state"] == "current"
          and settled["analysis"]["newer_evidence_available"] is False)

    # An analysis does not stamp its version onto findings it did not write.
    rows = finding_rows(ACCT)
    check("findings carry the version of the analysis that wrote them",
          all(r["evidence_version"] for r in rows))

    # =====================================================================
    print("\n=== 5. pending work is bounded by audience ===")
    # =====================================================================
    clear(ACCT)
    install(GOOD)
    ctx_req = analysis_jobs.analysis_request(
        account_id=ACCT, viewer_user_id=ceo_id,
        seat=database.resolve_seat(ACCT, ceo_id),
        selection={"choice": "everyone", "person_id": None},
        period=home_snapshot.resolve_period(7, "UTC"))
    SCOPE = ctx_req["scope_key"]

    read()
    check("one read queues one analysis", pending_count(ACCT, SCOPE) == 1)
    first_key = database.pending_analysis_for_scope(ACCT, SCOPE)["job_key"]

    # Move evidence into a LATER scheduling bucket, which changes the job key.
    # This is the exact reproduction: two reads, two keys, one audience.
    later = NOW + 4 * database.EVIDENCE_COALESCE_WINDOW_S * NS
    _n[0] += 1
    database.ingest_spans_with_loops([{
        "trace_id": f"t{_n[0]:028d}", "span_id": f"s{_n[0]:014d}",
        "parent_span_id": None, "service_name": "bucket-agent",
        "agent_id": "main", "span_name": "message_received", "kind": 1,
        "start_time_unix": later, "end_time_unix": later + 10**6,
        "status_code": 0, "status_message": "",
        "attributes": {"trovis.loop.external_id": "bucket-move"},
        "resource_attributes": {}}], account_id=ACCT)

    moved_req = analysis_jobs.analysis_request(
        account_id=ACCT, viewer_user_id=ceo_id,
        seat=database.resolve_seat(ACCT, ceo_id),
        selection={"choice": "everyone", "person_id": None},
        period=home_snapshot.resolve_period(7, "UTC"))
    check("the scheduling key really moved",
          moved_req["job_key"] != first_key
          and moved_req["scope_key"] == SCOPE)
    moved = read()
    check("a new bucket does NOT queue a second job for the same audience",
          pending_count(ACCT, SCOPE) == 1)
    check("the read says it joined the pending analysis",
          moved["analysis"]["joined_pending_analysis"] is True
          and moved["analysis"]["enqueued"] is False)
    pend = database.pending_analysis_for_scope(ACCT, SCOPE)
    check("the arriving evidence is recorded as still needing coverage",
          pend["pending_evidence_version"]
          == moved["analysis"]["evidence_version"]
          and pend["pending_evidence_version"] != pend["evidence_version"])

    # A moved PERIOD SLOT while work is pending: same answer.
    real_slot = analysis_jobs._period_slot
    analysis_jobs._period_slot = lambda period: str(int(real_slot(period)) + 5)
    try:
        slot_moved = read()
        check("a moved period slot does not queue a second job either",
              pending_count(ACCT, SCOPE) == 1
              and slot_moved["analysis"]["joined_pending_analysis"] is True)
    finally:
        analysis_jobs._period_slot = real_slot

    # CONCURRENT reads.
    errors = []
    barrier = threading.Barrier(6)

    def hammer():
        try:
            barrier.wait(timeout=20)
            c.get("/home/findings?days=7&tz=UTC", headers=auth(CEO))
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    threads = [threading.Thread(target=hammer) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    check("six concurrent reads raise nothing", not errors)
    check("and still leave exactly one pending analysis for the audience",
          pending_count(ACCT, SCOPE) == 1)

    # Isolation: a different audience gets its own pending job.
    other_req = analysis_jobs.analysis_request(
        account_id=ACCT, viewer_user_id=ceo_id,
        seat=database.resolve_seat(ACCT, ceo_id),
        selection={"choice": "everyone", "person_id": None},
        period=home_snapshot.resolve_period(30, "UTC"))
    check("a different period is a different audience", other_req["scope_key"] != SCOPE)
    c.get("/home/findings?days=30&tz=UTC", headers=auth(CEO))
    check("which queues its own analysis, unaffected by the first",
          pending_count(ACCT, other_req["scope_key"]) == 1
          and pending_count(ACCT, SCOPE) == 1)

    b = c.post("/auth/signup", json={
        "email": "other@round2b.test", "password": "correct horse battery",
        "name": "Ola", "account_type": "business", "org_name": "Beta"}).json()
    c.get("/home/findings?days=7&tz=UTC", headers=auth(b["token"]))
    check("and another account's pending work is entirely separate",
          pending_count(ACCT, SCOPE) == 1)

    # The backlog does not chain: run it, and the follow-up is READ-TRIGGERED.
    analysis_jobs.drain(6)
    check("no queued analysis is left behind after draining",
          pending_count(ACCT, SCOPE) == 0)
    after_drain = read()
    check("the next read covers what arrived while it was pending",
          after_drain["analysis"]["state"] in ("queued", "running", "current"))
    if after_drain["analysis"]["state"] != "current":
        analysis_jobs.drain(4)
        final = read()
        check("and that follow-up ends up current",
              final["analysis"]["state"] == "current"
              and final["analysis"]["newer_evidence_available"] is False)
    else:
        check("and that follow-up ends up current",
              after_drain["analysis"]["newer_evidence_available"] is False)

    # =====================================================================
    print("\n=== 6. trimming one response keeps another's evidence ===")
    # =====================================================================
    seq = investigation_tools.InvestigationSession(
        account_id=ACCT, only_user_ids=None, financial_visible=False)

    # A: a small, fully delivered result naming the three refund runs.
    a_payload = seq.run("list_comparable_runs", {"job_id": job["id"]})
    a_sent, _ = seq.fit(a_payload)
    a_ids = {str(r["run_id"]) for r in a_sent["runs"]}
    check("A is delivered whole", len(a_sent["runs"]) == len(a_payload["runs"]))
    check("A's runs are citable", all(f"run:{i}" in seq.evidence for i in a_ids))

    # A2: contrary evidence — one run's events and failing spans.
    detail = seq.run("inspect_run", {"run_id": RUNS[0], "event_limit": 40})
    d_sent, _ = seq.fit(detail)
    a_events = {f"run_event:{e['event_id']}" for e in d_sent.get("events", [])}
    a_spans = {f"failed_span:{s['ref']}" for s in d_sent.get("failed_spans", [])}
    check("A2 delivers run events and failing spans",
          a_events and a_spans
          and all(k in seq.evidence for k in a_events | a_spans))

    # B: an oversized result that OVERLAPS nothing of A (a different agent),
    # trimmed hard.
    b_payload = seq.run("list_comparable_runs", {"agent": "bulk-agent", "limit": 50})
    b_all = {str(r["run_id"]) for r in b_payload["runs"]}
    b_sent, _ = seq.fit(b_payload)
    b_kept = {str(r["run_id"]) for r in b_sent["runs"]}
    b_lost = b_all - b_kept
    check("B really was trimmed", len(b_lost) > 0)
    check("A's evidence SURVIVES B's trimming",
          all(f"run:{i}" in seq.evidence for i in a_ids))
    check("A2's events and failing spans survive it too",
          all(k in seq.evidence for k in a_events | a_spans))
    check("B's unseen rows are not citable",
          not any(f"run:{i}" in seq.evidence for i in b_lost))
    check("B's delivered rows are citable",
          all(f"run:{i}" in seq.evidence for i in b_kept))

    # OVERLAP: re-retrieve A's runs inside an oversized result and trim them.
    # They were already delivered, so they must stay.
    overlap_payload = seq.run("list_comparable_runs", {"days_back": 90, "limit": 50})
    overlap_sent, _ = seq.fit(overlap_payload)
    overlap_kept = {str(r["run_id"]) for r in overlap_sent["runs"]}
    dropped_but_seen = a_ids - overlap_kept
    check("the overlapping result did drop some already-delivered runs",
          len(dropped_but_seen) > 0)
    check("an already-delivered run is NOT forgotten when a later result drops it",
          all(f"run:{i}" in seq.evidence for i in dropped_but_seen))

    # A WHOLE RESULT DROPPED: its unseen content never becomes evidence.
    drop_sess = investigation_tools.InvestigationSession(
        account_id=ACCT, only_user_ids=None, financial_visible=False)
    seen_first = drop_sess.run("list_comparable_runs", {"job_id": job["id"]})
    drop_sess.fit(seen_first)
    kept_first = {f"run:{r}" for r in RUNS}
    real_budget = investigation_tools.MAX_TOOL_RESULT_CHARS
    investigation_tools.MAX_TOOL_RESULT_CHARS = 20
    try:
        huge = drop_sess.run("list_comparable_runs", {"agent": "bulk-agent", "limit": 50})
        huge_ids = {str(r["run_id"]) for r in huge["runs"]}
        sent_huge, _ = drop_sess.fit(huge)
    finally:
        investigation_tools.MAX_TOOL_RESULT_CHARS = real_budget
    check("an undeliverable result is replaced, not truncated",
          sent_huge.get("tool_result_dropped") is True)
    check("none of its content became citable evidence",
          not any(f"run:{i}" in drop_sess.evidence for i in huge_ids))
    check("evidence delivered before it is untouched",
          all(k in drop_sess.evidence for k in kept_first))
    check("and the dropped result is recorded as a coverage limitation",
          "result_dropped" in drop_sess.retrieval_report()["limitation_kinds"])

    # Composition, assessment and validation all see the same delivered set.
    check("the ledger never holds a record the model was not shown",
          set(seq.evidence) <= seq.delivered | {
              k for k in seq.evidence if k.split(":", 1)[0]
              in ("job", "agent", "agent_context", "financial_window")})

    ok, why = findings_mod.validate_finding, None
    fake = {
        "title": "Citing something never delivered", "explanation": "x y z.",
        "category": "attention", "claim_kind": "observation",
        "confidence": "supported",
        "claims": [{"text": "This run stalled.", "kind": "observation",
                    "evidence": [f"run:{sorted(b_lost)[0]}"]}],
        "entities": [{"kind": "run", "id": int(sorted(b_lost)[0]), "label": "x"}],
        "evidence": [{"kind": "run", "ref": sorted(b_lost)[0], "note": "n"}],
    }
    try:
        findings_mod.validate_finding(
            fake, snapshot=snap, evidence_index=seq.evidence,
            calculations=seq.calculations, financial_visible=False,
            retrieval=seq.retrieval_report())
        rejected = []
    except findings_mod.FindingRejected as exc:
        rejected = exc.reasons
    check("validation rejects a citation of a trimmed-away record",
          any("never retrieved" in r for r in rejected))

    good = {
        "title": "A delivered run stopped at approval",
        "explanation": "One refund run stopped after the approval call failed.",
        "category": "attention", "claim_kind": "observation",
        "confidence": "supported",
        "claims": [{"text": "It stopped after the approval call failed.",
                    "kind": "observation", "evidence": [f"run:{RUNS[0]}"]}],
        "entities": [{"kind": "run", "id": RUNS[0], "label": "Refund 0"}],
        "evidence": [{"kind": "run", "ref": str(RUNS[0]), "note": "n"},
                     {"kind": "failed_span", "ref": sorted(a_spans)[0].split(":", 1)[1],
                      "note": "the failing approval call"}],
        "next_step": {"kind": "review_runs", "text": "Open it."},
    }
    try:
        validated_good = findings_mod.validate_finding(
            good, snapshot=snap, evidence_index=seq.evidence,
            calculations=seq.calculations, financial_visible=False,
            retrieval=seq.retrieval_report())
    except findings_mod.FindingRejected as exc:
        validated_good = None
        print("     (unexpected rejection:", exc.reasons, ")")
    check("and accepts one resting on evidence delivered earlier in the session",
          validated_good is not None)


print()
if failures:
    print("FAILED: " + "; ".join(failures))
    raise SystemExit(1)
print("ALL PASS")
