"""Three ways the layer still mistook a failure for an answer.

All three are the same error in different places: something did not work, and
the system recorded the *absence* of a result as a *result*.

  1. A REJECTED REPLACEMENT RETIRED WHAT IT FAILED TO REPLACE. The
     `FindingRejected` handler noted the rejection and registered no gap, so
     the run reported `analysis_outcome: complete`, `coverage_gaps: []`,
     `retire_previous: true` — and superseded a valid standing finding on the
     strength of a draft the validator had just refused.
  2. AN UNFINISHED RUN REPORTED `current`. Keeping the old finding was only
     half of it: both terminal paths returned `ANALYSIS_COMPLETE`
     unconditionally, so a run with `coverage_gaps: ["wording_withheld"]` and
     `retire_previous: false` still read as a completed investigation.
  3. UNDELIVERED CONTENT REPLACED DELIVERED EVIDENCE. `_record` wrote straight
     into the ledger, so a re-fetched run overwrote the payload the model had
     been shown the moment it was *retrieved*. Keeping the key through
     trimming did not keep the title, the outcome, or the digest.

Run:
  OVERSEE_DISABLE_PRICING_SYNC=1 python3 test_home_findings_round3.py
(isolated temp SQLite DB; every model call scripted; no network, no live model)
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
    """Reset findings AND job history, so no case inherits another's state."""
    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute(f"DELETE FROM findings WHERE account_id = {database.PH}", (acct,))
        cur.execute(f"DELETE FROM analysis_jobs WHERE account_id = {database.PH}", (acct,))


def finding_rows(acct):
    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute(
            "SELECT id, finding_key, state, title, analysis_id, confidence "
            f"FROM findings WHERE account_id = {database.PH} ORDER BY id",
            (acct,))
        return [dict(r) for r in cur.fetchall()]


with TestClient(main.app) as c:
    a = c.post("/auth/signup", json={
        "email": "ceo@round3.test", "password": "correct horse battery",
        "name": "Cleo", "account_type": "business", "org_name": "Acme"}).json()
    ACCT, CEO = a["org"]["id"], a["token"]
    ceo_id = a["user"]["id"]

    job = c.post("/workflows", headers=auth(CEO), json={
        "name": "Refunds", "description": "d",
        "steps": [{"step_type": "agent", "label": "run"}]}).json()

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

    DISCOVER = _text({"candidates": [{
        "topic": "refund-approval-stall", "question": "why did three runs stop?",
        "hypothesis": "they stopped at the same approval step",
        "category": "attention", "why_this_reader": "owner",
        "evidence_needed": ["the runs"]}]})

    def investigation(kw, o):
        """Retrieve on the first turn, decide once a tool result is present."""
        if len(kw.get("messages") or []) <= 1:
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

    # A draft that is schema-valid and cites real, retrieved evidence — and
    # whose NUMBER does not match the server's. The deterministic validator
    # refuses it; no model opinion is involved.
    BAD_NUMBER = {
        **SUPPORTED,
        "title": "Nine refund runs stopped at the approval step",
        "explanation": "Nine refund runs stopped after the approval call failed.",
        "claims": [{"text": "Nine runs stopped after the approval call failed.",
                    "kind": "calculation", "value": 9,
                    "metric_ref": "snapshot:period.abandoned",
                    "evidence": [f"run:{r}" for r in RUNS]}],
    }

    PUBLISH = _text({"decision": "publish", "reason": "ok",
                     "claim_kind": "observation", "confidence": "supported"})
    RANK1 = _text({"order": [{"index": 0, "score": 1.0, "reason": "x"}]})
    GOOD = {"discovery": DISCOVER, "investigation": investigation,
            "composition": _text(SUPPORTED), "assessment": PUBLISH,
            "ranking": RANK1}

    def read(days=7, tz="UTC"):
        return c.get(f"/home/findings?days={days}&tz={tz}", headers=auth(CEO)).json()

    def nudge(tag):
        """Move the evidence on, so the next read is a genuinely new job."""
        database.ingest_spans_with_loops(
            [span("noise-agent", 0, {"trovis.loop.external_id": f"n-{tag}"})],
            account_id=ACCT)

    def publish_baseline():
        """A clean slate plus one real, published finding, through the queue."""
        clear(ACCT)
        install(GOOD)
        read()
        analysis_jobs.drain(4)
        out = read()
        assert len(out["findings"]) == 1, out["analysis"]
        assert out["analysis"]["state"] == "current", out["analysis"]
        return out

    def refresh(script, tag):
        """One isolated refresh on top of a published baseline."""
        nudge(tag)
        install(script)
        read()
        drained = analysis_jobs.drain(4)
        return drained, read()

    # =====================================================================
    print("\n=== 1. a rejected replacement cannot retire what it replaced ===")
    # =====================================================================
    before = publish_baseline()
    standing = finding_rows(ACCT)[0]
    check("a valid finding is standing",
          standing["state"] == "open" and len(before["findings"]) == 1)

    drained, after = refresh({**GOOD, "composition": _text(BAD_NUMBER)},
                             "reject")
    report = drained[0]
    check("the real validator refused the draft",
          any("says 9" in str(r["reason"]) or "numeric claim" in str(r["reason"])
              for r in report.get("rejected") or []))
    check("the run publishes nothing", report["published"] == 0)
    check("it registers the rejection as a completion gap",
          report["completion_gaps"] == ["validation_rejected"])
    check("so it does not report itself complete",
          report["analysis_outcome"] == "incomplete")
    check("and it is not eligible to retire",
          report["retire_previous"] is False
          and "validation_rejected" in report["coverage_gaps"])
    check("nothing was retired", report.get("retired") == 0)

    rows = finding_rows(ACCT)
    check("PERSISTED: the standing finding is still open",
          len(rows) == 1 and rows[0]["id"] == standing["id"]
          and rows[0]["state"] == "open")
    check("PERSISTED: it still belongs to the analysis that published it",
          rows[0]["analysis_id"] == standing["analysis_id"])
    check("GET /home/findings still serves it",
          len(after["findings"]) == 1
          and after["findings"][0]["title"] == standing["title"])
    check("and the read does not call the refresh current",
          after["analysis"]["state"] == "incomplete"
          and after["analysis"]["analysis_outcome"] == "incomplete")

    # --- a MIXED refresh: one candidate validates, one is rejected --------
    print("\n--- one valid candidate, one rejected ---")
    publish_baseline()
    standing = finding_rows(ACCT)[0]

    OTHER_TOPIC = {
        "topic": "wait-pileup", "question": "what is holding work?",
        "hypothesis": "one step holds everything",
        "category": "attention", "why_this_reader": "owner",
        "evidence_needed": ["waits"]}
    TWO = _text({"candidates": [
        json.loads(DISCOVER.content[0].text)["candidates"][0], OTHER_TOPIC]})

    SECOND_GOOD = {
        **SUPPORTED,
        "title": "Refund 0 was closed without finishing",
        "explanation": "Refund 0 stopped after its approval call failed.",
        "claims": [{"text": "It stopped after the approval call failed.",
                    "kind": "observation", "evidence": [f"run:{RUNS[0]}"]}],
        "entities": [{"kind": "run", "id": RUNS[0], "label": "Refund 0"}],
        "evidence": [{"kind": "run", "ref": str(RUNS[0]), "note": "stopped"}],
    }

    def mixed_composition(kw, o):
        """First candidate composes a bad number; second composes cleanly."""
        k = o.counts.get("compose", 0)
        o.counts["compose"] = k + 1
        return _text(BAD_NUMBER if k == 0 else SECOND_GOOD)

    drained, mixed = refresh(
        {**GOOD, "discovery": TWO, "composition": mixed_composition,
         "ranking": _text({"order": [{"index": 0, "score": 1.0, "reason": "x"}]})},
        "mixed")
    rep = drained[0]
    check("the valid candidate published", rep["published"] == 1)
    check("the rejected one is still recorded as a gap",
          rep["completion_gaps"] == ["validation_rejected"])
    check("a partly-successful run is not a complete one",
          rep["analysis_outcome"] == "incomplete"
          and rep["retire_previous"] is False)
    check("and it retired nothing", rep.get("retired") == 0)

    rows = finding_rows(ACCT)
    keys = {r["finding_key"] for r in rows}
    check("PERSISTED: the new finding was published",
          len(rows) == 2 and standing["finding_key"] in keys)
    check("PERSISTED: the standing finding was NOT erased",
          next(r for r in rows if r["finding_key"] == standing["finding_key"]
               )["state"] == "open")
    check("GET serves both", len(mixed["findings"]) == 2)
    check("and reports the mix honestly, not as one fresh answer",
          mixed["analysis"]["state"] == "incomplete"
          and mixed["analysis"]["published_this_analysis"] == 1
          and mixed["analysis"]["findings_from_previous_analysis"] is True)

    # =====================================================================
    print("\n=== 2. an unfinished analysis never reports current ===")
    # =====================================================================
    # Each case starts from a fresh publication and a clean job history, so no
    # earlier failure or debounce floor can make the next one pass by accident.
    def case(label, script, *, expect_outcome, expect_state, expect_gap,
             expect_published=0):
        publish_baseline()
        keep = finding_rows(ACCT)[0]
        drained, out = refresh(script, label.replace(" ", "-"))
        rep = drained[0] if drained else {}
        if rep.get("status") == "requeued":
            # A transport-level failure is retried (bounded). Drain the
            # attempts out so the job settles, then judge from the read and
            # the persisted rows — the requeue receipt is not the report.
            for _ in range(database.ANALYSIS_JOB_MAX_ATTEMPTS + 1):
                more = analysis_jobs.drain(2)
                if not more or more[0].get("status") != "requeued":
                    rep = more[0] if more else rep
                    break
            out = read()
        status = out["analysis"]
        check(f"{label}: outcome is {expect_outcome!r}",
              rep.get("analysis_outcome") == expect_outcome
              or status.get("analysis_outcome") == expect_outcome)
        check(f"{label}: read state is {expect_state!r} (got {status['state']!r})",
              status["state"] == expect_state)
        if expect_gap is not None and "coverage_gaps" in rep:
            check(f"{label}: the gap is named ({expect_gap})",
                  expect_gap in rep["coverage_gaps"])
        check(f"{label}: published {expect_published}",
              rep.get("published", 0) == expect_published)
        if "retire_previous" in rep:
            check(f"{label}: nothing retired",
                  rep["retire_previous"] is False)
        check(f"{label}: nothing was superseded in the database",
              all(r["state"] != "superseded" for r in finding_rows(ACCT)))
        rows = finding_rows(ACCT)
        check(f"{label}: the standing finding survives",
              any(r["finding_key"] == keep["finding_key"] and r["state"] == "open"
                  for r in rows))
        check(f"{label}: and is still served",
              any(f["title"] == keep["title"] for f in out["findings"]))
        return out

    # An unusable assessment: the exact reproduction from the report.
    case("unusable assessment", {**GOOD, "assessment": _raw("???")},
         expect_outcome="incomplete", expect_state="incomplete",
         expect_gap="wording_withheld")

    # No verdict from the investigation loop.
    case("missing verdict", {**GOOD, "investigation": _raw("no verdict here")},
         expect_outcome="incomplete", expect_state="incomplete",
         expect_gap="candidate_undecided")

    # Composition produced nothing usable.
    case("uncomposable draft", {**GOOD, "composition": _raw("not json")},
         expect_outcome="incomplete", expect_state="incomplete",
         expect_gap="candidate_uncomposed")

    # A narrowing verdict whose rewrite withdraws.
    case("rewrite withdrawn",
         {**GOOD,
          "assessment": _text({"decision": "narrow", "reason": "overstated",
                               "overstated_phrases": ["all"],
                               "claim_kind": "observation",
                               "confidence": "qualified"}),
          "revision": _text({"withdraw": True})},
         expect_outcome="incomplete", expect_state="incomplete",
         expect_gap="wording_withheld")

    # The validator refusal again, through the same isolated harness.
    case("validation rejected", {**GOOD, "composition": _text(BAD_NUMBER)},
         expect_outcome="incomplete", expect_state="incomplete",
         expect_gap="validation_rejected")

    # Discovery that cannot be read is its OWN outcome, not `incomplete`.
    case("unreadable discovery", {**GOOD, "discovery": _raw("{{{")},
         expect_outcome="discovery_unusable", expect_state="incomplete",
         expect_gap="discovery_unusable")

    # --- and the legitimate results stay legitimate ----------------------
    print("\n--- successful results are not swept up in this ---")
    publish_baseline()
    drained, quiet = refresh({**GOOD, "discovery": _text({"candidates": []})},
                             "abstain")
    check("a genuine abstention is COMPLETE",
          drained[0]["analysis_outcome"] == "complete"
          and drained[0]["completion_gaps"] == [])
    check("it may retire, and does",
          drained[0]["retire_previous"] is True and drained[0]["retired"] == 1)
    check("and it reads as current",
          quiet["analysis"]["state"] == "current"
          and quiet["findings"] == [])

    publish_baseline()
    keep = finding_rows(ACCT)[0]
    REFUTED = _text({
        "verdict": "refuted", "summary": "the runs are unrelated",
        "for": [], "against": [f"run:{RUNS[0]}"],
        "alternatives_considered": ["coincidence"], "unknown": [],
        "sample": {"observed": 3, "comparable": 3}})

    def refute(kw, o):
        if len(kw.get("messages") or []) <= 1:
            return _tool("list_comparable_runs", {"job_id": job["id"]}, "1")
        return REFUTED

    drained, refuted = refresh({**GOOD, "investigation": refute}, "refute")
    check("a supported refutation is COMPLETE, not a gap",
          drained[0]["analysis_outcome"] == "complete"
          and drained[0]["completion_gaps"] == [])
    check("and it reads as current",
          refuted["analysis"]["state"] == "current")

    # A partial SEARCH still publishes and still reads current — the retrieval
    # bound is a coverage fact, not an unanswered question.
    publish_baseline()
    for i in range(40):
        database.ingest_spans_with_loops([
            span("bulk-agent", 3000 - i, {
                "trovis.loop.title": ("A deliberately long recorded title " * 4) + str(i),
                "trovis.loop.external_id": f"bulk{i}"})], account_id=ACCT)

    def bulky(kw, o):
        if len(kw.get("messages") or []) <= 1:
            return _tool("list_comparable_runs",
                         {"agent": "bulk-agent", "limit": 50}, "1")
        return _text({
            "verdict": "supported", "summary": "one bulk run stalled",
            "for": [], "against": [], "alternatives_considered": [],
            "unknown": ["the rest of the list"], "sample": {"observed": 1}})

    bulk_ids = []
    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute(
            "SELECT id FROM loops WHERE external_id LIKE 'bulk%' ORDER BY id DESC "
            "LIMIT 1")
        bulk_ids.append(int(cur.fetchone()["id"]))

    PARTIAL_DRAFT = {
        "title": "A bulk run is still open",
        "explanation": "One bulk run has not been closed.",
        "consequence": None,
        "claims": [{"text": "This run is still open.", "kind": "observation",
                    "evidence": [f"run:{bulk_ids[0]}"]}],
        "entities": [{"kind": "run", "id": bulk_ids[0], "label": "bulk"}],
        "evidence": [{"kind": "run", "ref": str(bulk_ids[0]), "note": "open"}],
        "uncertainty": ["the rest of the list"],
        "next_step": {"kind": "review_runs", "text": "Open it."},
        "graphic": {"kind": "none"},
    }
    drained, partial = refresh(
        {**GOOD, "investigation": bulky, "composition": _text(PARTIAL_DRAFT)},
        "partial")
    rep = drained[0]
    check("a bounded search still publishes its exact observation",
          rep["published"] == 1)
    check("it answered what it raised, so the outcome is COMPLETE",
          rep["analysis_outcome"] == "complete"
          and rep["completion_gaps"] == [])
    check("but the partial retrieval still blocks retirement",
          rep["retire_previous"] is False
          and "retrieval_incomplete" in rep["coverage_gaps"])
    check("the read reports it current",
          partial["analysis"]["state"] == "current")
    check("and the finding carries the coverage, qualified",
          partial["findings"][0]["confidence"] == "qualified"
          and partial["findings"][0]["coverage"]["retrieval_complete"] is False)

    # --- a later successful refresh restores `current` -------------------
    print("\n--- recovery ---")
    publish_baseline()
    keep = finding_rows(ACCT)[0]
    _, broken = refresh({**GOOD, "assessment": _raw("???")}, "recover-a")
    check("the broken refresh reads incomplete",
          broken["analysis"]["state"] == "incomplete")
    drained, fixed = refresh(GOOD, "recover-b")
    check("a later successful refresh restores current",
          fixed["analysis"]["state"] == "current"
          and fixed["analysis"]["analysis_outcome"] == "complete")
    check("and it republished the condition rather than losing it",
          len(fixed["findings"]) == 1
          and fixed["analysis"]["published_this_analysis"] == 1
          and fixed["analysis"]["findings_from_previous_analysis"] is False)

    # =====================================================================
    print("\n=== 3. delivered evidence keeps its delivered contents ===")
    # =====================================================================
    ORIGINAL_TITLE = "Refund 0"

    def ledger(sess, key):
        return sess.evidence.get(key)

    def digest_of(sess, key):
        return findings_mod.evidence_digest_for(sess.evidence.get(key))

    def mutate_run(run_id, title, close_it):
        with database._connect() as conn, database._cursor(conn) as cur:
            cur.execute(
                f"UPDATE loops SET title = {database.PH} WHERE id = {database.PH}",
                (title, run_id))
            if close_it:
                cur.execute(
                    f"UPDATE loops SET cached_state = 'done' WHERE id = {database.PH}",
                    (run_id,))

    # -- A delivered, then an UPDATED A trimmed out of a later response ---
    sess = investigation_tools.InvestigationSession(
        account_id=ACCT, only_user_ids=None, financial_visible=False)
    a_sent = sess.retrieve("list_comparable_runs", {"job_id": job["id"]})
    key = f"run:{RUNS[0]}"
    check("A is delivered", key in sess.evidence and key in sess.delivered)
    first_payload = json.loads(json.dumps(ledger(sess, key)))
    first_digest = digest_of(sess, key)
    check("with its original title and outcome",
          first_payload["title"] == ORIGINAL_TITLE
          and first_payload["outcome"] == "abandoned")

    # The record moves on: the run is renamed and marked done.
    mutate_run(RUNS[0], "RENAMED — should not appear", close_it=True)

    # A later, oversized response re-fetches it and is trimmed before sending.
    for i in range(40):
        database.ingest_spans_with_loops([
            span("refunds-agent", 2000 - i, {
                "trovis.loop.title": ("padding title that is quite long " * 5) + str(i),
                "trovis.loop.external_id": f"pad{i}"})], account_id=ACCT)
    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute(
            f"UPDATE loops SET workflow_id = {database.PH} "
            "WHERE external_id LIKE 'pad%'", (job["id"],))
    # Same job filter, so the list contains the changed row AND enough padding
    # to overflow the size budget. Rows come back newest-first, so the original
    # run sits at the end and is exactly what trimming drops.
    big = sess.run("list_comparable_runs", {"job_id": job["id"], "limit": 50})
    refetched = next((r for r in big["runs"] if r["run_id"] == RUNS[0]), None)
    check("the later response really did re-fetch the CHANGED row",
          refetched is not None and refetched["title"].startswith("RENAMED"))
    sent, _ = sess.fit(big)
    trimmed_out = all(r["run_id"] != RUNS[0] for r in sent.get("runs", []))
    check("and the changed row was trimmed out before sending", trimmed_out)

    check("the ledger still holds the DELIVERED title",
          ledger(sess, key)["title"] == ORIGINAL_TITLE)
    check("and the DELIVERED outcome",
          ledger(sess, key)["outcome"] == "abandoned")
    check("the digest is unchanged, so a stored finding stays consistent",
          digest_of(sess, key) == first_digest)
    check("the whole payload is byte-identical to what was delivered",
          ledger(sess, key) == first_payload)

    # -- A delivered, then an UPDATED A successfully delivered ------------
    sess2 = investigation_tools.InvestigationSession(
        account_id=ACCT, only_user_ids=None, financial_visible=False)
    sess2.retrieve("inspect_run", {"run_id": RUNS[0], "event_limit": 5})
    check("sess2 starts from the changed record",
          sess2.evidence[key]["title"].startswith("RENAMED"))
    mutate_run(RUNS[0], "RENAMED AGAIN", close_it=False)
    d2 = sess2.retrieve("inspect_run", {"run_id": RUNS[0], "event_limit": 5})
    check("a delivered update DOES become the citable version",
          sess2.evidence[key]["title"] == "RENAMED AGAIN")
    check("and the digest moves with it",
          digest_of(sess2, key)
          == findings_mod.evidence_digest_for(sess2.evidence[key]))

    # -- a whole response dropped: updated A and new B ---------------------
    sess3 = investigation_tools.InvestigationSession(
        account_id=ACCT, only_user_ids=None, financial_visible=False)
    sess3.retrieve("inspect_run", {"run_id": RUNS[0], "event_limit": 5})
    a3 = json.loads(json.dumps(sess3.evidence[key]))
    a3_digest = digest_of(sess3, key)
    mutate_run(RUNS[0], "DROPPED RESPONSE TITLE", close_it=False)
    real_budget = investigation_tools.MAX_TOOL_RESULT_CHARS
    investigation_tools.MAX_TOOL_RESULT_CHARS = 20
    try:
        huge = sess3.run("list_comparable_runs", {"days_back": 90, "limit": 50})
        huge_ids = {str(r["run_id"]) for r in huge["runs"]}
        sent3, _ = sess3.fit(huge)
    finally:
        investigation_tools.MAX_TOOL_RESULT_CHARS = real_budget
    check("the whole response was dropped",
          sent3.get("tool_result_dropped") is True)
    check("A keeps its delivered payload",
          sess3.evidence[key] == a3
          and sess3.evidence[key]["title"] != "DROPPED RESPONSE TITLE")
    check("A keeps its delivered digest", digest_of(sess3, key) == a3_digest)
    new_ids = huge_ids - {str(r) for r in RUNS}
    check("B — never delivered — is not citable",
          new_ids and not any(f"run:{i}" in sess3.evidence for i in new_ids))

    # -- contrary evidence, and events / failed spans ----------------------
    sess4 = investigation_tools.InvestigationSession(
        account_id=ACCT, only_user_ids=None, financial_visible=False)
    detail = sess4.retrieve("inspect_run", {"run_id": RUNS[1], "event_limit": 40})
    ev_keys = [f"run_event:{e['event_id']}" for e in detail.get("events", [])]
    span_keys = [f"failed_span:{s['ref']}" for s in detail.get("failed_spans", [])]
    check("events and failing spans are delivered",
          ev_keys and span_keys
          and all(k in sess4.evidence for k in ev_keys + span_keys))
    ev_payload = json.loads(json.dumps(sess4.evidence[ev_keys[0]]))
    span_payload = json.loads(json.dumps(sess4.evidence[span_keys[0]]))

    # Re-fetch the same run with a tiny event cap so most events are trimmed.
    investigation_tools.MAX_TOOL_RESULT_CHARS = 20
    try:
        again = sess4.run("inspect_run", {"run_id": RUNS[1], "event_limit": 40})
        sess4.fit(again)
    finally:
        investigation_tools.MAX_TOOL_RESULT_CHARS = real_budget
    check("a dropped re-fetch does not disturb delivered events",
          sess4.evidence[ev_keys[0]] == ev_payload)
    check("or delivered failing spans",
          sess4.evidence[span_keys[0]] == span_payload)

    # -- overlapping responses fitted OUT OF ORDER -------------------------
    sess5 = investigation_tools.InvestigationSession(
        account_id=ACCT, only_user_ids=None, financial_visible=False)
    mutate_run(RUNS[2], "ORDER TEST ORIGINAL", close_it=False)
    first = sess5.run("inspect_run", {"run_id": RUNS[2], "event_limit": 5})
    mutate_run(RUNS[2], "ORDER TEST UPDATED", close_it=False)
    second = sess5.run("inspect_run", {"run_id": RUNS[2], "event_limit": 5})
    # Deliver the SECOND response first, then the first. Provenance is keyed on
    # the payload, so each settles against its own rows rather than the most
    # recent call's.
    sess5.fit(second)
    k2 = f"run:{RUNS[2]}"
    check("out-of-order delivery promotes the response that was sent",
          sess5.evidence[k2]["title"] == "ORDER TEST UPDATED")
    sess5.fit(first)
    check("and the later-delivered older response then stands",
          sess5.evidence[k2]["title"] == "ORDER TEST ORIGINAL")

    # -- aliasing: a later retrieval must not mutate delivered evidence ----
    sess6 = investigation_tools.InvestigationSession(
        account_id=ACCT, only_user_ids=None, financial_visible=False)
    payload6 = sess6.retrieve("inspect_run", {"run_id": RUNS[0], "event_limit": 5})
    snapshot6 = json.loads(json.dumps(sess6.evidence[key]))
    payload6["title"] = "MUTATED IN PLACE"
    payload6["outcome"] = "completed"
    for ev in payload6.get("events", []):
        ev["sentence"] = "MUTATED IN PLACE"
    for sp in payload6.get("failed_spans", []):
        sp["message"] = "MUTATED IN PLACE"
    check("mutating the returned payload cannot reach the ledger",
          sess6.evidence[key] == snapshot6
          and sess6.evidence[key]["title"] != "MUTATED IN PLACE")
    check("nor reach a delivered event or failing span",
          all("MUTATED IN PLACE" not in json.dumps(v, default=str)
              for k, v in sess6.evidence.items()
              if k.startswith(("run_event:", "failed_span:"))))

    # -- and validation uses the delivered version -------------------------
    period = home_snapshot.resolve_period(7, "UTC")
    seat = database.resolve_seat(ACCT, ceo_id)
    snap = home_snapshot.build_snapshot(
        account_id=ACCT, viewer_user_id=ceo_id,
        selection={"choice": "everyone", "person_id": None, "user_ids": None,
                   "clamped": False, "unreadable": False,
                   "requested": "everyone", "seat": seat},
        period=period)
    draft = {
        "title": "A refund run was given up on",
        "explanation": "Refund 0 was closed without finishing.",
        "category": "attention", "claim_kind": "observation",
        "confidence": "supported",
        "claims": [{"text": "It was closed without finishing.",
                    "kind": "observation", "evidence": [f"run:{RUNS[0]}"]}],
        "entities": [{"kind": "run", "id": RUNS[0], "label": "Refund 0"}],
        "evidence": [{"kind": "run", "ref": str(RUNS[0]), "note": "abandoned"}],
        "next_step": {"kind": "review_runs", "text": "Open it."},
    }
    validated = findings_mod.validate_finding(
        draft, snapshot=snap, evidence_index=sess.evidence,
        calculations=sess.calculations, financial_visible=False,
        retrieval=sess.retrieval_report())
    check("the stored digest is the delivered version's",
          validated["evidence"][0]["digest"] == first_digest)
    check("and the outcome check ran against the DELIVERED outcome",
          # The live row now says 'done'; the delivered row said 'abandoned',
          # and that is what the contradiction guard must use.
          validated["confidence"] in ("supported", "qualified"))
    try:
        findings_mod.validate_finding(
            {**draft,
             "category": "positive_change",
             "claims": [{"text": "It completed successfully.",
                         "kind": "observation",
                         "evidence": [f"run:{RUNS[0]}"]}]},
            snapshot=snap, evidence_index=sess.evidence,
            calculations=sess.calculations, financial_visible=False,
            retrieval=sess.retrieval_report())
        contradiction = []
    except findings_mod.FindingRejected as exc:
        contradiction = exc.reasons
    check("a win claimed over the delivered 'abandoned' row is still refused",
          any("positive finding cites no run" in r or "says work was completed" in r
              for r in contradiction))


print()
if failures:
    print("FAILED: " + "; ".join(failures))
    raise SystemExit(1)
print("ALL PASS")
