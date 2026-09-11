"""The six ways the investigation layer was still wrong.

Every one is the same shape as the earlier rounds: a step that did its job and
a response that described the result as if it had done a different one.

  1. NARROWING DID NOT NARROW. An assessment that found no evidence of an
     outage or of causation returned `narrow`, and the pipeline kept the
     sentence "The approval service outage caused these refund runs to stop"
     and changed a label. A confidence word does not unsay a causal claim.
  2. COMPLETENESS WAS NEGOTIABLE. `coverage.setdefault` let a draft assert
     `counts_exact: true` over an incomplete snapshot and publish "No other job
     is affected" as supported.
  3. TRUNCATION BROKE THE JSON. `result_text` sliced a serialized payload at
     6,000 characters and the loop called `json.loads` on it.
  4. A CRASHED JOB HELD THE ONLY SLOT. `run_one` counted stale rows against
     the concurrency ceiling and returned None before reaching recovery.
  5. AN ANALYSIS WAS CURRENT FOREVER. A job finished in 2000 still reported
     `current`; freshness never reached that branch.
  6. EVERY SPAN WAS A NEW JOB. The exact counts rode inside the bucketed hash,
     so three spans in one 15-minute window produced three job keys.

Run:
  OVERSEE_DISABLE_PRICING_SYNC=1 python3 test_home_findings_review.py
(isolated temp SQLite DB; scripted model; never touches the dev/prod DB)
"""
import json
import os
import tempfile
import time
from datetime import datetime, timedelta, timezone

os.environ.update({
    "OVERSEE_DISABLE_PRICING_SYNC": "1",
    "TROVIS_DISABLE_ALERTS": "1",
    "TROVIS_DISABLE_LOOP_SWEEP": "1",
    "TROVIS_DISABLE_ANALYSIS": "1",
    "TROVIS_LOOP_TITLES": "off",
    "ANTHROPIC_API_KEY": "test-key-not-used-for-network",
    # Debounce off by default here so the scheduling tests can turn it on
    # deliberately rather than fighting it everywhere else.
    "TROVIS_ANALYSIS_DEBOUNCE_S": "0",
})
os.environ.pop("DATABASE_URL", None)

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()

import database
database.SQLITE_PATH = _tmp.name

import analysis_jobs
import findings as findings_mod
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


def _tool(name, inp, cid="c"):
    return _Resp([_Block(type="tool_use", id=cid, name=name, input=inp)], "tool_use")


class FakeMessages:
    def __init__(self, owner):
        self.owner = owner

    def create(self, **kw):
        sysx = kw.get("system") or ""
        step = (
            "discovery" if sysx.startswith("You are Trovis, examining")
            else "investigation" if sysx.startswith("You are Trovis, investigating")
            else "assessment" if sysx.startswith("You are Trovis, checking")
            else "ranking" if sysx.startswith("You are Trovis, ordering")
            else "revision" if sysx.startswith("You are Trovis, rewriting")
            else "composition"
        )
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


def tid(title):
    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute(f"SELECT id FROM loops WHERE title = {database.PH}", (title,))
        r = cur.fetchone()
        return int(r["id"]) if r else None


def clear(acct):
    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute(f"DELETE FROM findings WHERE account_id = {database.PH}", (acct,))
        cur.execute(f"DELETE FROM analysis_jobs WHERE account_id = {database.PH}", (acct,))


with TestClient(main.app) as c:
    a = c.post("/auth/signup", json={
        "email": "ceo@acme.test", "password": "correct horse battery",
        "name": "Cleo", "account_type": "business", "org_name": "Acme"}).json()
    ACCT, CEO = a["org"]["id"], a["token"]
    ceo_id = a["user"]["id"]

    job = c.post("/workflows", headers=auth(CEO), json={
        "name": "Refunds", "description": "d",
        "steps": [{"step_type": "agent", "label": "run"}]}).json()
    for i in range(3):
        e = f"s{i}"
        database.ingest_spans_with_loops([
            span("refunds-agent", 5000 - i, {
                "trovis.loop.title": f"Refund {i}", "trovis.loop.external_id": e}),
            span("refunds-agent", 4900 - i, {
                "trovis.loop.external_id": e, "trovis.tool.name": "approval"},
                name="tool_call", status=2, msg="approval service timed out"),
        ], account_id=ACCT)
    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute(
            f"UPDATE loops SET workflow_id = {database.PH} WHERE title LIKE 'Refund %'",
            (job["id"],))
    RUNS = [tid(f"Refund {i}") for i in range(3)]
    for r in RUNS:
        database.abandon_loop(r, ACCT)

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
            "unknown": ["whether the approval service was actually down"],
            "sample": {"observed": 3, "comparable": 3}})

    DISCOVER = _text({"candidates": [{
        "topic": "refund-approval-stall", "question": "why did three runs stop?",
        "hypothesis": "they stopped at the same approval step",
        "category": "attention", "why_this_reader": "owner",
        "evidence_needed": ["the runs"]}]})

    # The overstated draft from the report: a causal claim the evidence
    # does not carry.
    OVERSTATED = {
        "title": "The approval service outage caused these refund runs to stop",
        "explanation": (
            "An outage in the approval service caused all three refund runs to "
            "stop, and it is costing you completed refunds."),
        "consequence": "The outage is blocking refunds.",
        "claims": [{
            "text": "The approval service outage caused three runs to stop.",
            "kind": "observation", "evidence": [f"run:{RUNS[0]}"]}],
        "entities": [{"kind": "run", "id": RUNS[0]}],
        "evidence": [{"kind": "run", "ref": str(RUNS[0]), "note": "stopped"}],
        "uncertainty": [],
        "next_step": {"kind": "review_runs",
                      "text": "Escalate the approval service outage."},
    }
    SUPPORTED = {
        "title": "Three refund runs stopped at the same approval step",
        "explanation": (
            "Refund 0, 1 and 2 each stopped after the approval call failed and "
            "were closed without finishing."),
        "consequence": "Three refunds were given up on.",
        "claims": [{
            "text": "Three runs stopped after the approval call failed.",
            "kind": "observation",
            "evidence": [f"run:{RUNS[0]}", f"run:{RUNS[1]}", f"run:{RUNS[2]}"]}],
        "entities": [{"kind": "run", "id": RUNS[0]},
                     {"kind": "job", "id": job["id"]}],
        "evidence": [{"kind": "run", "ref": str(r)} for r in RUNS],
        "uncertainty": ["whether the approval service was actually down"],
        "next_step": {"kind": "review_runs", "text": "Open the three refunds."},
    }
    NARROW = _text({
        "decision": "narrow",
        "reason": "no evidence of an outage, and no evidence of causation",
        "overstated_phrases": ["outage", "caused", "costing you"],
        "claim_kind": "observation", "confidence": "qualified"})

    def run_pipeline(**q):
        qs = "&".join(f"{k}={v}" for k, v in q.items() if v is not None)
        c.get(f"/home/findings?{qs}", headers=auth(CEO))
        drained = analysis_jobs.drain(3)
        out = c.get(f"/home/findings?{qs}", headers=auth(CEO)).json()
        return drained, out

    # =====================================================================
    print("\n=== 1. narrowing actually narrows ===")
    # =====================================================================
    clear(ACCT)
    revised = {"n": 0}

    def revision(kw, o):
        revised["n"] += 1
        return _text(SUPPORTED)

    def narrow_then_publish(kw, o):
        # The first assessment narrows; the assessment of the REWRITE passes.
        k = o.counts.get("a", 0)
        o.counts["a"] = k + 1
        if k == 0:
            return NARROW
        return _text({"decision": "publish", "reason": "the rewrite is supported",
                      "claim_kind": "observation", "confidence": "supported"})

    install({"discovery": DISCOVER, "investigation": investigation,
             "composition": _text(OVERSTATED),
             "assessment": narrow_then_publish,
             "revision": revision,
             "ranking": _text({"order": [{"index": 0, "score": 0.9, "reason": "x"}]})})
    drained, out = run_pipeline(days=7, tz="UTC")
    check("a narrowing verdict triggers a rewrite", revised["n"] == 1)
    check("the finding publishes", len(out["findings"]) == 1)
    f = out["findings"][0]
    surface = json.dumps(f).lower()
    check("the unsupported causal wording is GONE from every visible field",
          "outage" not in surface and "caused" not in surface
          and "costing you" not in surface)
    check("the supported observation survives",
          "stopped at the same approval step" in f["title"].lower())
    check("a rewritten finding is qualified, never supported",
          f["confidence"] == "qualified")
    detail = c.get(f"/home/findings/{f['id']}?days=7&tz=UTC",
                   headers=auth(CEO)).json()
    check("the claims were rewritten too, not just the title",
          all("caused" not in cl["text"].lower() for cl in detail["claims"]))
    check("and the next-step wording as well",
          "outage" not in json.dumps(f["next_step"]).lower())

    print("\n--- when the rewrite cannot be supported ---")
    clear(ACCT)
    install({"discovery": DISCOVER, "investigation": investigation,
             "composition": _text(OVERSTATED), "assessment": NARROW,
             "revision": _text({"withdraw": True}),
             "ranking": _text({"order": []})})
    drained, out = run_pipeline(days=7, tz="UTC")
    check("a withdrawn rewrite publishes nothing",
          out["findings"] == [] and drained[0]["published"] == 0)
    check("and the reason names the narrowing, not a generic failure",
          any("narrow" in str(r["reason"]).lower()
              for r in drained[0]["rejected"]))

    print("\n--- when the rewrite keeps overstating ---")
    clear(ACCT)
    install({"discovery": DISCOVER, "investigation": investigation,
             "composition": _text(OVERSTATED), "assessment": NARROW,
             "revision": _text(OVERSTATED),
             "ranking": _text({"order": []})})
    drained, out = run_pipeline(days=7, tz="UTC")
    check("repeated overstatement is withheld after the revision bound",
          out["findings"] == []
          and any("could not be narrowed" in str(r["reason"])
                  for r in drained[0]["rejected"]))

    print("\n--- unusable or missing assessment ---")
    for label, script in (
        ("no decision", _text({"reason": "hmm"})),
        ("unparseable", _Resp([_Block(type="text", text="not json at all")])),
        ("unknown decision", _text({"decision": "maybe"})),
    ):
        clear(ACCT)
        install({"discovery": DISCOVER, "investigation": investigation,
                 "composition": _text(OVERSTATED), "assessment": script,
                 "ranking": _text({"order": []})})
        drained, out = run_pipeline(days=7, tz="UTC")
        if out["findings"] or drained[0]["published"]:
            check(f"an unassessed draft is withheld ({label})", False)
            break
    else:
        check("an unassessed draft is withheld (no/unparseable/unknown decision)",
              True)

    print("\n--- deadline during assessment ---")
    clear(ACCT)
    real_budget = investigator.wall_clock_budget_s
    calls = {"n": 0}

    def tiny_budget():
        # Enough for discovery + investigation + composition, then expired.
        calls["n"] += 1
        return 0.0 if calls["n"] > 1 else 30.0

    install({"discovery": DISCOVER, "investigation": investigation,
             "composition": _text(OVERSTATED), "assessment": NARROW,
             "ranking": _text({"order": []})})
    real_ok = investigator.Deadline.ok
    seq = {"n": 0}

    def flaky_ok(self):
        # True for discovery/investigation/composition, then expired at
        # assessment time.
        seq["n"] += 1
        return seq["n"] <= 3

    investigator.Deadline.ok = flaky_ok
    try:
        drained, out = run_pipeline(days=7, tz="UTC")
    finally:
        investigator.Deadline.ok = real_ok
    check("reaching the deadline does not publish an unassessed draft",
          out["findings"] == [] and drained[0]["published"] == 0)

    # =====================================================================
    print("\n=== 2. completeness is server-derived ===")
    # =====================================================================
    incomplete_snapshot = {
        "period": {"completed": 5},
        "completeness": {"counts_exact": False, "scope_membership_complete": False,
                         "absence_established": False, "scope_state": "unknown"},
        "financial": {"visible": False},
    }
    ledger = {f"run:{RUNS[0]}": {"run_id": RUNS[0], "outcome": "abandoned"}}
    complete_retrieval = {"complete": True, "exhausted": [],
                          "tool_calls": 2, "rows_retrieved": 3}

    def validate(finding, snapshot=incomplete_snapshot, retrieval=complete_retrieval,
                 financial=False):
        try:
            return findings_mod.validate_finding(
                finding, snapshot=snapshot, evidence_index=ledger,
                calculations={}, financial_visible=financial,
                retrieval=retrieval), None
        except findings_mod.FindingRejected as exc:
            return None, exc.reasons

    exhaustive = {
        "category": "attention", "claim_kind": "observation",
        "confidence": "supported",
        "title": "Only the refunds job is affected",
        "explanation": "The refunds job is the only one with stopped runs.",
        "entities": [{"kind": "run", "id": RUNS[0]}],
        "evidence": [{"kind": "run", "ref": str(RUNS[0])}],
        "claims": [{"text": "No other job is affected.", "kind": "observation",
                    "evidence": [f"run:{RUNS[0]}"]}],
        # The lie: the draft asserts a complete search.
        "coverage": {"counts_exact": True, "scope_membership_complete": True,
                     "absence_established": True},
    }
    ok, reasons = validate(exhaustive)
    check("a model-supplied `coverage` cannot upgrade an incomplete snapshot",
          ok is None and any("incomplete search" in r for r in reasons))

    omitted = {k: v for k, v in exhaustive.items() if k != "coverage"}
    ok, reasons = validate(omitted)
    check("omitting coverage does not make the search complete",
          ok is None)

    observation = {**omitted,
                   "title": "Three refund runs stopped at the same step",
                   "explanation": "Refund 0, 1 and 2 stopped after the approval call.",
                   "claims": [{"text": "These three runs stopped after the approval call.",
                               "kind": "observation", "evidence": [f"run:{RUNS[0]}"]}]}
    ok, reasons = validate(observation)
    check("an exact observation about records we READ still publishes",
          ok is not None)
    check("and it is published as qualified, carrying the server's coverage",
          ok["confidence"] == "qualified"
          and ok["coverage"]["counts_exact"] is False
          and ok["coverage"]["scope_membership_complete"] is False
          and ok["coverage"]["supports_exhaustive_claims"] is False)

    complete_snapshot = {
        "period": {"completed": 5},
        "completeness": {"counts_exact": True, "scope_membership_complete": True,
                         "absence_established": True, "scope_state": "populated"},
        "financial": {"visible": False},
    }
    partial_retrieval = {"complete": False, "exhausted": ["tool_calls"],
                         "tool_calls": 14, "rows_retrieved": 400}
    ok, reasons = validate(exhaustive, snapshot=complete_snapshot,
                           retrieval=partial_retrieval)
    check("a truncated RETRIEVAL also blocks an exhaustive claim",
          ok is None and any("retrieval_complete=False" in r for r in reasons))
    ok, _ = validate(observation, snapshot=complete_snapshot,
                     retrieval=partial_retrieval)
    check("and the retrieval limit is carried onto the published finding",
          ok is not None and ok["coverage"]["retrieval_complete"] is False
          and ok["coverage"]["retrieval_exhausted"] == ["tool_calls"]
          and ok["confidence"] == "qualified")

    chart = {**observation, "graphic": {"kind": "run_outcome_split", "series": [
        {"label": "All runs", "metric_ref": "snapshot:period.completed"}]}}
    ok, reasons = validate(chart)
    check("a chart label implying a whole is rejected over an incomplete search",
          ok is None and any("implies a complete picture" in r for r in reasons))
    chart_ok = {**observation, "graphic": {"kind": "run_outcome_split", "series": [
        {"label": "Runs we looked at", "metric_ref": "snapshot:period.completed"}]}}
    ok, _ = validate(chart_ok)
    check("an honest chart label passes and is flagged partial",
          ok is not None and ok["graphic"]["series"][0]["partial"] is True)

    # =====================================================================
    print("\n=== 3. truncation preserves valid JSON ===")
    # =====================================================================
    big_spans = []
    for i in range(30):
        big_spans.append(span("bulk-agent", 6000 - i, {
            "trovis.loop.title": "Ünïcödé — réfund « " + ("ø" * 320) + f" » {i}",
            "trovis.loop.external_id": f"big{i}"}))
    database.ingest_spans_with_loops(big_spans, account_id=ACCT)
    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute(
            f"SELECT id FROM loops WHERE external_id = {database.PH} "
            f"AND account_id = {database.PH}", ("big0", ACCT))
        fat_id = int(dict(cur.fetchone())["id"])
    with database._connect() as conn, database._cursor(conn) as cur:
        for i in range(60):
            database.append_loop_event(
                cur, fat_id, "handoff_initiated", "agent", "svc:main",
                payload={"direction": "to_human", "target_id": "x@y.test",
                         "handoff_id": f"H{i}", "reason": "z" * 300},
                account_id=ACCT, event_time_unix=NOW - (100 - i) * NS)

    sess = investigation_tools.InvestigationSession(
        account_id=ACCT, only_user_ids=None, financial_visible=False,
        budget=investigation_tools.ToolBudget(max_calls=20, max_rows=2000,
                                              max_events=2000))
    for label, payload in (
        ("oversized run list", sess.run("list_comparable_runs",
                                        {"agent": "bulk-agent", "limit": 50})),
        ("oversized run detail", sess.run("inspect_run",
                                          {"run_id": fat_id, "event_limit": 40})),
        ("wait list", sess.run("wait_concentration", {"limit": 50})),
    ):
        sent, blob = sess.fit(payload)
        parsed = None
        try:
            parsed = json.loads(blob)
        except Exception:
            parsed = None
        check(f"{label}: serialized result is valid JSON", parsed is not None)
        check(f"{label}: within the documented budget",
              len(blob) <= investigation_tools.MAX_TOOL_RESULT_CHARS)
        check(f"{label}: what is sent equals what was serialized",
              parsed == json.loads(json.dumps(sent, default=str)))

    runs_payload = sess.run("list_comparable_runs", {"agent": "bulk-agent", "limit": 50})
    sent, blob = sess.fit(runs_payload)
    check("truncation is stated, not implied",
          "size_truncated" in sent and sent["size_truncated"]["dropped"].get("runs"))
    check("identifiers survive on the rows that remain",
          all("run_id" in r and "outcome" in r for r in sent["runs"]))
    check("the evidence ledger matches what the model was shown",
          {str(r["run_id"]) for r in sent["runs"]}
          >= {k.split(":", 1)[1] for k in sess.evidence if k.startswith("run:")}
          or all(f"run:{r['run_id']}" in sess.evidence for r in sent["runs"]))
    kept_ids = {str(k["run_id"]) for k in sent["runs"]}
    dropped_id = None
    for r in runs_payload["runs"]:
        # Only a run this response was the first to retrieve. One an EARLIER
        # response already delivered stays quotable on purpose — see the
        # sequential-delivery section in test_home_findings_round2.py.
        if str(r["run_id"]) not in kept_ids and f"run:{r['run_id']}" not in sess.delivered:
            dropped_id = r["run_id"]
            break
    check("evidence dropped for size is no longer quotable",
          dropped_id is None or f"run:{dropped_id}" not in sess.evidence)

    nested = {"runs": [{"run_id": i, "deep": {"a": {"b": ["x" * 200] * 5}}}
                       for i in range(40)]}
    _, blob = sess.fit(nested)
    check("a nested payload survives trimming as valid JSON",
          json.loads(blob) is not None
          and len(blob) <= investigation_tools.MAX_TOOL_RESULT_CHARS)

    # Through the real loop: a tool result that would have been sliced.
    clear(ACCT)
    loop_ok = {"n": 0}

    def fat_investigation(kw, o):
        k = o.counts.get("t", 0)
        o.counts["t"] = k + 1
        if k == 0:
            return _tool("list_comparable_runs", {"agent": "bulk-agent", "limit": 50}, "1")
        loop_ok["n"] += 1
        return _text({"verdict": "qualified", "summary": "s", "for": [], "against": [],
                      "alternatives_considered": [], "unknown": [], "sample": {}})

    install({"discovery": DISCOVER, "investigation": fat_investigation,
             "composition": _text(SUPPORTED),
             "assessment": _text({"decision": "publish", "reason": "ok",
                                  "claim_kind": "observation",
                                  "confidence": "qualified"}),
             "ranking": _text({"order": [{"index": 0, "score": 0.5, "reason": "x"}]})})
    drained, out = run_pipeline(days=7, tz="UTC")
    check("an oversized tool result does not crash the investigation loop",
          loop_ok["n"] >= 1 and drained and drained[0]["status"] == "done")

    # =====================================================================
    print("\n=== 4. stale jobs are recovered through the worker ===")
    # =====================================================================
    clear(ACCT)
    install({"discovery": _text({"candidates": []})})

    def make_stale(job_id, seconds=None):
        seconds = seconds or database.ANALYSIS_JOB_STALE_S + 60
        old = (datetime.now(timezone.utc) - timedelta(seconds=seconds)
               ).strftime("%Y-%m-%d %H:%M:%S")
        with database._connect() as conn, database._cursor(conn) as cur:
            cur.execute(
                f"UPDATE analysis_jobs SET status = 'running', started_at = {database.PH}, "
                f"heartbeat_at = {database.PH} WHERE id = {database.PH}",
                (old, old, job_id))

    crashed = database.enqueue_analysis_job(ACCT, "crashed", {
        "scope_key": "scope-crashed", "viewer_user_id": ceo_id, "whose": "everyone",
        "person_id": None, "days": 7, "timezone": "UTC", "evidence": {}})
    make_stale(crashed["id"])
    check("a crashed job does not count against live concurrency",
          database.count_running_analysis_jobs() == 0
          and database.count_running_analysis_jobs(include_stale=True) == 1)
    recovered = analysis_jobs.run_one()
    check("run_one() RECOVERS it (the entry point, not the claim helper)",
          recovered is not None and recovered["job_id"] == crashed["id"])

    follow = database.enqueue_analysis_job(ACCT, "follow-on", {
        "scope_key": "scope-follow-on", "viewer_user_id": ceo_id, "whose": "everyone",
        "person_id": None, "days": 7, "timezone": "UTC", "evidence": {}})
    nxt = analysis_jobs.run_one()
    check("queued work behind it then progresses",
          nxt is not None and nxt["job_id"] == follow["id"])

    live = database.enqueue_analysis_job(ACCT, "live-one", {
        "scope_key": "scope-live-one", "viewer_user_id": ceo_id, "whose": "everyone",
        "person_id": None, "days": 7, "timezone": "UTC", "evidence": {}})
    database.claim_analysis_job()  # genuinely running, fresh heartbeat
    blocked = database.enqueue_analysis_job(ACCT, "blocked-by-live", {
        "scope_key": "scope-blocked-by-live", "viewer_user_id": ceo_id, "whose": "everyone",
        "person_id": None, "days": 7, "timezone": "UTC", "evidence": {}})
    check("a genuinely active job still holds the concurrency slot",
          analysis_jobs.run_one() is None)
    database.finish_analysis_job(live["id"], "done")
    check("and once it finishes the queue moves again",
          (analysis_jobs.run_one() or {}).get("job_id") == blocked["id"])

    print("\n--- competing recovery ---")
    race = database.enqueue_analysis_job(ACCT, "race-recover", {
        "scope_key": "scope-race-recover", "viewer_user_id": ceo_id, "whose": "everyone",
        "person_id": None, "days": 7, "timezone": "UTC", "evidence": {}})
    original = database.claim_analysis_job()
    make_stale(race["id"])
    first = database.claim_analysis_job()
    second = database.claim_analysis_job()
    check("two workers cannot claim the same stale job",
          first is not None and first["id"] == race["id"] and second is None)
    check("the reclaim carries a newer fence than the abandoned attempt",
          first["claim_token"] > original["claim_token"])
    database.finish_analysis_job(race["id"], "done",
                                claim_token=first["claim_token"])

    print("\n--- a superseded worker cannot publish over its replacement ---")
    supersede = database.enqueue_analysis_job(ACCT, "supersede", {
        "scope_key": "scope-supersede", "viewer_user_id": ceo_id, "whose": "everyone",
        "person_id": None, "days": 7, "timezone": "UTC", "evidence": {}})
    old_claim = database.claim_analysis_job()
    make_stale(supersede["id"])
    new_claim = database.claim_analysis_job()
    check("the stale claim is taken over",
          new_claim is not None and new_claim["id"] == supersede["id"]
          and new_claim["claim_token"] > old_claim["claim_token"])
    check("the old worker's claim is no longer current",
          database.analysis_claim_is_current(supersede["id"],
                                             old_claim["claim_token"]) is False
          and database.analysis_claim_is_current(supersede["id"],
                                                 new_claim["claim_token"]) is True)
    check("and its finish write does nothing",
          database.finish_analysis_job(
              supersede["id"], "done", {"published": 99},
              claim_token=old_claim["claim_token"]) is False)
    check("while the current worker's write lands",
          database.finish_analysis_job(
              supersede["id"], "done", {"published": 0},
              claim_token=new_claim["claim_token"]) is True)

    print("\n--- exhausted jobs retire without blocking ---")
    # Nothing live may be holding the single slot when we test the queue moving.
    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute(
            f"UPDATE analysis_jobs SET status = 'done', finished_at = "
            f"{'NOW()' if database.USE_POSTGRES else 'CURRENT_TIMESTAMP'} "
            f"WHERE account_id = {database.PH} AND status = 'running'", (ACCT,))
    doomed = database.enqueue_analysis_job(ACCT, "doomed", {})
    for _ in range(database.ANALYSIS_JOB_MAX_ATTEMPTS):
        j = database.claim_analysis_job()
        if j:
            database.requeue_analysis_job(j["id"], "boom")
    behind = database.enqueue_analysis_job(ACCT, "behind-doomed", {
        "scope_key": "scope-behind-doomed", "viewer_user_id": ceo_id, "whose": "everyone",
        "person_id": None, "days": 7, "timezone": "UTC", "evidence": {}})
    got = analysis_jobs.run_one()
    check("an exhausted job is retired and does not block the queue",
          database.analysis_job_status(ACCT, "doomed")["status"] == "failed"
          and got is not None and got["job_id"] == behind["id"])

    # =====================================================================
    print("\n=== 5. old analyses expire ===")
    # =====================================================================
    clear(ACCT)
    real_now = analysis_jobs._now
    clock = {"t": datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)}
    analysis_jobs._now = lambda: clock["t"]
    try:
        install({"discovery": _text({"candidates": []})})
        _, out = run_pipeline(days=7, tz="UTC")
        check("a fresh analysis reads as current",
              out["analysis"]["state"] == "current")

        # Pin the completion into the distant past — the reported reproduction.
        with database._connect() as conn, database._cursor(conn) as cur:
            cur.execute(
                f"UPDATE analysis_jobs SET finished_at = '2000-01-01 00:00:00' "
                f"WHERE account_id = {database.PH}", (ACCT,))
        stale_read = c.get("/home/findings?days=7&tz=UTC", headers=auth(CEO)).json()
        check("an analysis from 2000 is NOT current",
              stale_read["analysis"]["state"] != "current")
        check("and a refresh is queued for it",
              stale_read["analysis"]["state"] == "queued")

        again = c.get("/home/findings?days=7&tz=UTC", headers=auth(CEO)).json()
        check("repeated reads join the one pending refresh",
              again["analysis"]["state"] == "queued"
              and again["analysis"]["enqueued"] is False)

        print("\n--- a rolling period is a new question ---")
        clock["t"] = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)
        period_a = {"days": 7, "timezone": "UTC",
                    "end_utc": datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)}
        period_b = {"days": 7, "timezone": "UTC",
                    "end_utc": datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)
                    + timedelta(seconds=analysis_jobs.freshness_s() * 2)}
        slot_a = analysis_jobs._period_slot(period_a)
        slot_b = analysis_jobs._period_slot(period_b)
        check("the period slot advances as the window rolls", slot_a != slot_b)

        print("\n--- a failed refresh is not a successful empty analysis ---")
        clear(ACCT)
        _, out = run_pipeline(days=7, tz="UTC")
        # Publish something, then fail the next refresh.
        install({"discovery": DISCOVER, "investigation": investigation,
                 "composition": _text(SUPPORTED),
                 "assessment": _text({"decision": "publish", "reason": "ok",
                                      "claim_kind": "observation",
                                      "confidence": "supported"}),
                 "ranking": _text({"order": [{"index": 0, "score": 1.0, "reason": "x"}]})})
        clear(ACCT)
        _, published = run_pipeline(days=7, tz="UTC")
        check("a finding is published first", len(published["findings"]) == 1)
        with database._connect() as conn, database._cursor(conn) as cur:
            cur.execute(
                f"UPDATE analysis_jobs SET status = 'failed', error = 'model outage' "
                f"WHERE account_id = {database.PH}", (ACCT,))
        after_fail = c.get("/home/findings?days=7&tz=UTC", headers=auth(CEO)).json()
        check("a failed refresh reports failed, not 'found nothing'",
              after_fail["analysis"]["state"] == "failed"
              and "outage" in (after_fail["analysis"]["reason"] or ""))
        check("the previously published findings are NOT discarded",
              len(after_fail["findings"]) == 1)
        check("and they are marked as coming from an earlier analysis",
              after_fail["analysis"]["findings_from_previous_analysis"] is True)
    finally:
        analysis_jobs._now = real_now

    # =====================================================================
    print("\n=== 6. evidence change vs scheduling coalescence ===")
    # =====================================================================
    clear(ACCT)
    versions, buckets = [], []
    for i in range(3):
        database.ingest_spans_with_loops([
            span("burst-agent", 10 + i, {"trovis.loop.external_id": f"burst{i}"})
        ], account_id=ACCT)
        ev = database.evidence_version(ACCT)
        versions.append(ev["version"])
        buckets.append(ev["schedule_bucket"])
    check("each span still changes the EXACT evidence version",
          len(set(versions)) == 3)
    check("but they share one scheduling bucket",
          len(set(buckets)) == 1)

    def job_key_now(days=7, tz="UTC"):
        seat = database.resolve_seat(ACCT, ceo_id)
        period = __import__("home_snapshot").resolve_period(days, tz)
        return analysis_jobs.analysis_request(
            account_id=ACCT, viewer_user_id=ceo_id, seat=seat,
            selection={"choice": "everyone", "person_id": None},
            period=period)["job_key"]

    keys = []
    for i in range(3):
        database.ingest_spans_with_loops([
            span("burst-agent", 5 + i, {"trovis.loop.external_id": f"more{i}"})
        ], account_id=ACCT)
        keys.append(job_key_now())
    check("an ingest burst produces ONE job key, not one per span",
          len(set(keys)) == 1)

    install({"discovery": _text({"candidates": []})})
    enqueued = 0
    for i in range(4):
        database.ingest_spans_with_loops([
            span("burst-agent", 1 + i, {"trovis.loop.external_id": f"read{i}"})
        ], account_id=ACCT)
        r = c.get("/home/findings?days=7&tz=UTC", headers=auth(CEO)).json()
        if r["analysis"].get("enqueued"):
            enqueued += 1
    check("interleaved ingestion and reads enqueue bounded work",
          enqueued <= 1)
    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute(
            f"SELECT COUNT(*) AS n FROM analysis_jobs WHERE account_id = {database.PH} "
            f"AND status IN ('queued','running')", (ACCT,))
        pending = int(dict(cur.fetchone())["n"])
    check("and never build a backlog", pending <= 1)

    print("\n--- changes during execution are covered by the next run ---")
    before = database.evidence_version(ACCT)["version"]
    analysis_jobs.drain(3)
    database.ingest_spans_with_loops([
        span("burst-agent", 0, {"trovis.loop.external_id": "during"})
    ], account_id=ACCT)
    after = database.evidence_version(ACCT)["version"]
    check("evidence that arrived during execution is visible afterwards",
          before != after)
    done_job = database.analysis_job_status(ACCT, keys[0])
    check("the completed analysis is not claimed to cover it",
          done_job is None or done_job.get("status") in ("done", "queued", "running"))

    print("\n--- debounce, and that it does not ignore evidence forever ---")
    os.environ["TROVIS_ANALYSIS_DEBOUNCE_S"] = "3600"
    try:
        clear(ACCT)
        install({"discovery": DISCOVER, "investigation": investigation,
                 "composition": _text(SUPPORTED),
                 "assessment": _text({"decision": "publish", "reason": "ok",
                                      "claim_kind": "observation",
                                      "confidence": "supported"}),
                 "ranking": _text({"order": [{"index": 0, "score": 1.0, "reason": "x"}]})})
        _, first_out = run_pipeline(days=7, tz="UTC")
        check("an analysis publishes", len(first_out["findings"]) == 1)

        # Same evidence bucket: the SCHEDULING key has not moved, so no second
        # investigation is bought. But the completed analysis did not read
        # these records, and this read must not say it did — that was the
        # round-2 freshness bug. Deferred is fine; "already covered" is not.
        for i in range(3):
            database.ingest_spans_with_loops([
                span("burst-agent", 0, {"trovis.loop.external_id": f"deb{i}"})
            ], account_id=ACCT)
        same_bucket = c.get("/home/findings?days=7&tz=UTC", headers=auth(CEO)).json()
        check("evidence inside one bucket does not start a second investigation",
              same_bucket["analysis"]["enqueued"] is False
              and same_bucket["analysis"]["state"] != "current")
        check("and the read says the new evidence is not yet analysed",
              same_bucket["analysis"]["newer_evidence_available"] is True
              and same_bucket["analysis"]["analyzed_evidence_version"]
              != same_bucket["analysis"]["evidence_version"])

        # Now push evidence into a LATER bucket, so the job key really moves.
        # Without the debounce floor this would start a fresh investigation
        # seconds after the last one.
        later = int(database.EVIDENCE_COALESCE_WINDOW_S * 2)
        database.ingest_spans_with_loops([
            span("burst-agent", -later, {"trovis.loop.external_id": "next-bucket"})
        ], account_id=ACCT)
        debounced = c.get("/home/findings?days=7&tz=UTC", headers=auth(CEO)).json()
        check("a new evidence bucket inside the debounce window is held",
              debounced["analysis"]["state"] == "debounced"
              and debounced["analysis"]["enqueued"] is False)
        check("and the reader is told the evidence is not ignored",
              "next run" in (debounced["analysis"]["reason"] or ""))
        check("the existing findings stay visible while it is held",
              len(debounced["findings"]) == 1
              and debounced["analysis"]["findings_from_previous_analysis"] is True)
        with database._connect() as conn, database._cursor(conn) as cur:
            cur.execute(
                f"SELECT COUNT(*) AS n FROM analysis_jobs WHERE account_id = "
                f"{database.PH} AND status IN ('queued','running')", (ACCT,))
            held = int(dict(cur.fetchone())["n"])
        check("nothing is queued behind the floor", held == 0)

        os.environ["TROVIS_ANALYSIS_DEBOUNCE_S"] = "0"
        after_window = c.get("/home/findings?days=7&tz=UTC", headers=auth(CEO)).json()
        check("once the window passes the change is picked up",
              after_window["analysis"]["state"] == "queued")
        check("and it covers the evidence that arrived while it was held",
              after_window["analysis"]["evidence_version"]
              == database.evidence_version(ACCT)["version"])
    finally:
        os.environ["TROVIS_ANALYSIS_DEBOUNCE_S"] = "0"

    print("\n--- audiences stay isolated ---")
    other = c.post("/auth/signup", json={
        "email": "b@other.test", "password": "correct horse battery",
        "name": "Bo", "account_type": "business", "org_name": "Other"}).json()
    ok_keys = {job_key_now(days=7), job_key_now(days=30)}
    check("a different period is a different job", len(ok_keys) == 2)
    other_seat = database.resolve_seat(other["org"]["id"], other["user"]["id"])
    other_req = analysis_jobs.analysis_request(
        account_id=other["org"]["id"], viewer_user_id=other["user"]["id"],
        seat=other_seat, selection={"choice": "everyone", "person_id": None},
        period=__import__("home_snapshot").resolve_period(7, "UTC"))
    check("a different account is a different job and a different slice",
          other_req["job_key"] not in ok_keys
          and other_req["scope_key"] != job_key_now(days=7))

print("\n" + ("FAILED: " + "; ".join(failures) if failures else "ALL PASS"))
raise SystemExit(1 if failures else 0)
