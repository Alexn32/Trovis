"""Trovis's OWN Home inference spending: does the ledger record what we spent?

These drive the REAL production path — a `/home/findings` read enqueues, the
real `analysis_jobs.drain()` claims and runs the job, the real investigator
reaches a scripted provider — and then assert on the rows that landed. A test
that called `home_llm_usage.call()` directly would prove the helper works and
say nothing about whether production is wired to it, which is the only question
that matters here.

What this file defends, in the order it would hurt to get wrong:

  1. ATTRIBUTION. Every request lands under the account, job, attempt and
     stage that incurred it, or it is not usable for anything.
  2. UNKNOWN IS NOT ZERO. A request we could not measure or price is counted
     and reported as unknown. Coalescing it to $0.00 states a number nobody
     measured, and always downward.
  3. NOTHING IS LOST. A retry is a second billable request. An investigation
     that later fails validation, or loses its lease, keeps the spending it
     already incurred.
  4. NOTHING IS DOUBLE-COUNTED. Re-persisting one request cannot bill it
     twice; re-reading Home bills nothing at all.
  5. SEPARATION. Trovis's cost of goods never reaches a customer's cost card.

Run:
  OVERSEE_DISABLE_PRICING_SYNC=1 python3 test_home_llm_usage.py
(isolated temp SQLite DB; never touches the dev/prod DB, never a network call)
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
})
os.environ.pop("DATABASE_URL", None)

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()

import database
database.SQLITE_PATH = _tmp.name

import analysis_jobs
import home_llm_report
import home_llm_usage
import investigator
import main
from fastapi.testclient import TestClient

main._auto_describe = lambda *a, **k: False

failures: list[str] = []


def check(label, cond, detail=""):
    print(("  PASS " if cond else "  FAIL ") + label)
    if not cond:
        if detail:
            print("        " + str(detail))
        failures.append(label)


def auth(tok):
    return {"Authorization": f"Bearer {tok}"}


NS = 10**9
NOW = time.time_ns()
_seq = [0]


# --- a scripted provider, with a REAL usage block ---------------------------

class _Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Usage:
    """Only the fields passed exist — absent is absent, not zero."""

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class _Resp:
    def __init__(self, content, stop_reason="end_turn", usage=None, model=None):
        self.content = content
        self.stop_reason = stop_reason
        if usage is not None:
            self.usage = usage
        if model is not None:
            self.model = model


def _text(payload, **kw):
    return _Resp([_Block(type="text", text=json.dumps(payload))], **kw)


def _tool(name, inp, call_id="t1", **kw):
    return _Resp([_Block(type="tool_use", id=call_id, name=name, input=inp)],
                 stop_reason="tool_use", **kw)


def usage(inp=1000, out=200, cache_creation=None, cache_read=None):
    fields = {"input_tokens": inp, "output_tokens": out}
    if cache_creation is not None:
        fields["cache_creation_input_tokens"] = cache_creation
    if cache_read is not None:
        fields["cache_read_input_tokens"] = cache_read
    return _Usage(**fields)


class FakeMessages:
    def __init__(self, owner):
        self.owner = owner

    def create(self, **kw):
        system = kw.get("system") or ""
        if system.startswith("You are Trovis, examining"):
            step = "discovery"
        elif system.startswith("You are Trovis, investigating"):
            step = "investigation"
        elif system.startswith("You are Trovis, checking"):
            step = "assessment"
        elif system.startswith("You are Trovis, ordering"):
            step = "ranking"
        else:
            step = "composition"
        self.owner.seen.append(step)
        script = self.owner.script.get(step)
        if callable(script):
            return script(kw, self.owner)
        if isinstance(script, list):
            idx = self.owner.counts.get(step, 0)
            self.owner.counts[step] = idx + 1
            script = script[min(idx, len(script) - 1)]
        if script is None:
            return _text({}, usage=usage())
        return script


class FakeAnthropic:
    def __init__(self, script):
        self.script = script
        self.messages = FakeMessages(self)
        self.seen: list[str] = []
        self.counts: dict[str, int] = {}


def install(script):
    client = FakeAnthropic(script)
    investigator._client = lambda: client
    return client


# --- reading the ledger back ------------------------------------------------

def ledger_rows(**where):
    sql = "SELECT * FROM home_llm_requests"
    args = []
    if where:
        sql += " WHERE " + " AND ".join(f"{k} = {database.PH}" for k in where)
        args = list(where.values())
    sql += " ORDER BY id"
    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute(sql, tuple(args))
        return [dict(r) for r in (cur.fetchall() or [])]


def span(service, ago_s, attrs, name="message_received", status=0, msg=""):
    _seq[0] += 1
    t = NOW - int(ago_s * NS)
    return {
        "trace_id": f"ev{_seq[0]:026d}", "span_id": f"sp{_seq[0]:012d}",
        "parent_span_id": None, "service_name": service, "agent_id": "main",
        "span_name": name, "kind": 1, "start_time_unix": t,
        "end_time_unix": t + 10**6, "status_code": status,
        "status_message": msg, "attributes": attrs, "resource_attributes": {},
    }


with TestClient(main.app) as c:
    acme = c.post("/auth/signup", json={
        "email": "ceo@acme.test", "password": "correct horse battery",
        "name": "Cleo", "account_type": "business", "org_name": "Acme",
    }).json()
    ACCT, CEO = acme["org"]["id"], acme["token"]
    ceo_id = acme["user"]["id"]
    lv = {l["key"]: l for l in c.get("/org/scope-levels", headers=auth(CEO)).json()}
    ceo_role = c.post("/org/roles", headers=auth(CEO), json={
        "title": "CEO", "scope_level_id": lv["exec"]["id"]}).json()
    c.post(f"/org/roles/{ceo_role['id']}/members", headers=auth(CEO),
           json={"user_id": ceo_id})

    # A price for the model the investigator actually asks for, so the happy
    # path is genuinely priced rather than accidentally unknown.
    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute(
            f"DELETE FROM model_pricing WHERE model_name = {database.PH}",
            (investigator.MODEL,))
        cur.execute(
            "INSERT INTO model_pricing (model_name, input_cost_per_1k, "
            f"output_cost_per_1k) VALUES ({database.PH}, {database.PH}, {database.PH})",
            (investigator.MODEL, 0.003, 0.015))

    # Real work to investigate.
    spans = []
    for i in range(3):
        ago = (i + 1) * 3600
        ext = f"r{i}"
        spans.append(span("refunds-agent", ago, {
            "trovis.loop.title": f"Refund {i}", "trovis.loop.external_id": ext}))
        spans.append(span("refunds-agent", ago - 20,
                          {"trovis.loop.external_id": ext,
                           "trovis.tool.name": "approval_service"},
                          name="tool_call", status=2, msg="did not return"))
    database.ingest_spans_with_loops(spans, account_id=ACCT)

    def run_analysis(**q):
        qs = "&".join(f"{k}={v}" for k, v in q.items() if v is not None)
        first = c.get(f"/home/findings{'?' + qs if qs else ''}", headers=auth(CEO))
        drained = analysis_jobs.drain(3)
        second = c.get(f"/home/findings{'?' + qs if qs else ''}", headers=auth(CEO))
        return first.json(), drained, second.json()

    GOOD_COMPOSITION = _text({
        "title": "Three refunds stopped at the approval step",
        "explanation": "Each of the three recorded a failing approval call.",
        "consequence": None,
        "claims": [{"text": "Three runs recorded a failing approval step.",
                    "kind": "observation", "evidence": []}],
        "entities": [], "evidence": [], "uncertainty": ["why it failed"],
        "next_step": {"kind": "review_runs", "text": "Open them."},
        "graphic": {"kind": "none"},
    }, usage=usage(1800, 400))

    BASE = {
        "discovery": _text({"candidates": [{
            "topic": "refund-approval-stall",
            "question": "Why did three refund runs not finish?",
            "hypothesis": "They all stopped at the same approval step.",
            "category": "attention",
            "why_this_reader": "they own the refunds agent",
            "evidence_needed": ["the abandoned runs"],
        }]}, usage=usage(1200, 300)),
        "investigation": [
            _tool("list_comparable_runs", {"limit": 10}, usage=usage(2000, 150)),
            _text({"verdict": "qualified",
                   "summary": "Three runs stopped at approval.",
                   "for": [], "against": [],
                   "alternatives_considered": ["a different population"],
                   "unknown": ["why"], "sample": {"observed": 3, "comparable": 3}},
                  usage=usage(2600, 320)),
        ],
        "composition": GOOD_COMPOSITION,
        "assessment": _text({"decision": "publish", "reason": "carried",
                             "claim_kind": "observation",
                             "confidence": "qualified",
                             "overstated_phrases": []}, usage=usage(900, 120)),
        "ranking": _text({"order": [{"index": 0, "score": 0.9, "reason": "only"}],
                          "merge": [], "drop": []}, usage=usage(700, 90)),
    }

    # =================================================================
    print("\n--- the real production path writes the ledger ---")
    # =================================================================
    install(BASE)
    first, drained, second = run_analysis(days=7, tz="UTC")
    check("the worker actually ran a job", bool(drained) and drained[0]["status"] == "done")
    rows = ledger_rows()
    check("the production run recorded provider requests", len(rows) > 0)
    check("as many rows as the provider was actually called",
          len(rows) == len(investigator._client().seen))
    check("every row is attributed to the account that incurred it",
          rows and all(r["account_id"] == ACCT for r in rows))
    job_id = drained[0]["job_id"]
    check("and to the analysis job that ran",
          all(r["analysis_job_id"] == job_id for r in rows))
    check("and to that job's attempt",
          all(r["job_attempt"] is not None for r in rows))
    check("each request is numbered within its execution",
          sorted(r["request_seq"] for r in rows) == list(range(1, len(rows) + 1)))
    stages = {r["stage"] for r in rows}
    check("every investigation stage that called the provider is named",
          {home_llm_usage.STAGE_PROPOSING, home_llm_usage.STAGE_INVESTIGATING,
           home_llm_usage.STAGE_COMPOSING, home_llm_usage.STAGE_ASSESSING}
          <= stages, f"stages={sorted(stages)}")
    check("the tool loop records one row per turn, not one per investigation",
          sum(1 for r in rows
              if r["stage"] == home_llm_usage.STAGE_INVESTIGATING) == 2)
    check("each row names the model actually asked for",
          all(r["model_requested"] == investigator.MODEL for r in rows))
    check("and every row is settled, not left in flight",
          all(r["outcome"] == home_llm_usage.OUTCOME_SUCCEEDED for r in rows))
    check("timing is recorded per request",
          all(r["started_at"] and r["finished_at"] and r["latency_ms"] is not None
              for r in rows))
    check("provider-reported tokens are stored",
          all(r["input_tokens"] and r["output_tokens"] for r in rows)
          and all(r["usage_reported"] == 1 for r in rows))
    check("and priced, with the rate captured at request time",
          all(r["estimated_cost_usd"] is not None for r in rows)
          and all(r["price_input_per_1k"] == 0.003 for r in rows)
          and all(r["pricing_captured_at"] for r in rows))
    check("the pricing match is recorded, not just the number",
          all(r["pricing_source"] == "exact" for r in rows)
          and all(r["pricing_model_key"] == investigator.MODEL for r in rows))
    one = rows[0]
    expect = round(one["input_tokens"] / 1000 * 0.003
                   + one["output_tokens"] / 1000 * 0.015, 8)
    check("the estimate is the tokens times the captured rate",
          abs(one["estimated_cost_usd"] - expect) < 1e-9)
    check("an analysis id is stamped once the run mints one",
          any(r["analysis_id"] for r in rows))
    sensitive = [k for k in one
                 if k != "request_key"
                 and any(w in k for w in ("prompt", "message", "api_key",
                                          "token_text", "header", "secret"))]
    check("the schema has no column for a prompt, response or credential",
          not sensitive, f"suspicious columns={sensitive}")
    blob = json.dumps(rows, default=str)
    check("and nothing that looks like one leaked into a column",
          "test-key-not-used-for-network" not in blob
          and "You are Trovis" not in blob)

    # =================================================================
    print("\n--- re-reading Home spends nothing ---")
    # =================================================================
    before = len(ledger_rows())
    c.get("/home/findings?days=7&tz=UTC", headers=auth(CEO))
    c.get("/home/findings?days=7&tz=UTC", headers=auth(CEO))
    published = second.get("findings") or []
    if published:
        c.get(f"/home/findings/{published[0]['id']}?days=7&tz=UTC",
              headers=auth(CEO))
    check("re-reading the list and a finding creates no usage row",
          len(ledger_rows()) == before)
    check("because no model runs on a read path",
          analysis_jobs.drain(3) == [] or len(ledger_rows()) == before)

    # =================================================================
    print("\n--- a retry is a second billable request ---")
    # =================================================================
    # The first attempt raises inside composition; the job requeues and runs
    # again. Both attempts' spending must survive, separately.
    state = {"n": 0}

    def flaky_composition(kw, owner):
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("provider blew up mid-composition")
        return GOOD_COMPOSITION

    c.post("/auth/signup", json={
        "email": "two@beta.test", "password": "correct horse battery",
        "name": "Bo", "account_type": "business", "org_name": "Beta",
    })
    install({**BASE, "composition": flaky_composition})
    before = len(ledger_rows())
    c.get("/home/findings?days=14&tz=UTC", headers=auth(CEO))
    attempts = [analysis_jobs.run_one(), analysis_jobs.run_one()]
    retry_rows = ledger_rows()[before:]
    by_attempt = {}
    for r in retry_rows:
        by_attempt.setdefault(r["job_attempt"], []).append(r)
    check("the first attempt failed and the job was retried",
          any(a and a.get("status") in ("requeued", "failed") for a in attempts),
          f"attempts={[a and a.get('status') for a in attempts]}")
    check("both attempts recorded their own requests",
          len(by_attempt) >= 2, f"attempts seen={sorted(by_attempt)}")
    check("the failed attempt's spending is kept, not discarded",
          all(len(v) > 0 for v in by_attempt.values()))
    check("the request that raised is recorded as failed, not as succeeded",
          any(r["outcome"] == home_llm_usage.OUTCOME_FAILED for r in retry_rows))
    failed_row = next(r for r in retry_rows
                      if r["outcome"] == home_llm_usage.OUTCOME_FAILED)
    check("the failure names the error type and no message",
          failed_row["error_kind"] == "RuntimeError"
          and "blew up" not in json.dumps(failed_row, default=str))
    check("a request that never returned carries no invented usage",
          failed_row["input_tokens"] is None
          and failed_row["estimated_cost_usd"] is None)
    check("but it is still attributed and still counted",
          failed_row["account_id"] == ACCT
          and failed_row["stage"] == home_llm_usage.STAGE_COMPOSING)

    # =================================================================
    print("\n--- unknown usage and unknown pricing stay unknown ---")
    # =================================================================
    no_usage = {**BASE, "discovery": _text({"candidates": []})}  # no usage block
    install(no_usage)
    before = len(ledger_rows())
    c.get("/home/findings?days=21&tz=UTC", headers=auth(CEO))
    analysis_jobs.drain(3)
    quiet = ledger_rows()[before:]
    check("a response carrying no usage block records no tokens",
          quiet and all(r["input_tokens"] is None for r in quiet))
    check("and is flagged as unreported rather than zero",
          all(r["usage_reported"] == 0 for r in quiet))
    check("so its cost is unknown, never $0.00",
          all(r["estimated_cost_usd"] is None for r in quiet))

    unpriced = database.resolve_home_llm_price("a-model-nobody-has-priced-xyz")
    check("an unknown model has no rate at all",
          unpriced["rates"] is None
          and unpriced["source"] == database._PRICE_MATCH_NONE)
    check("and never borrows another model's price",
          database.home_llm_cost(unpriced["rates"], input_tokens=5000,
                                 output_tokens=1000, cache_creation=None,
                                 cache_read=None) is None)
    dated = database.resolve_home_llm_price(f"{investigator.MODEL}-20260101")
    check("a dated variant of a priced model uses ITS OWN row, and says so",
          dated["rates"] == (0.003, 0.015)
          and dated["source"] == database._PRICE_MATCH_PREFIX
          and dated["matched_key"] == investigator.MODEL)
    check("a measured request with a zero-token response is priced 0, not unknown",
          database.home_llm_cost((0.003, 0.015), input_tokens=0, output_tokens=0,
                                 cache_creation=None, cache_read=None) == 0.0)

    # =================================================================
    print("\n--- cache tokens are counted once, at their own rates ---")
    # =================================================================
    cost = database.home_llm_cost(
        (0.010, 0.050), input_tokens=1000, output_tokens=500,
        cache_creation=2000, cache_read=4000)
    expect = round(1000 / 1000 * 0.010 + 500 / 1000 * 0.050
                   + 2000 / 1000 * 0.010 * 1.25 + 4000 / 1000 * 0.010 * 0.10, 8)
    check("cache creation bills at 1.25x input and cache read at 0.1x",
          abs(cost - expect) < 1e-9, f"{cost} != {expect}")
    check("cached tokens are not also counted as plain input",
          cost < round((1000 + 2000 + 4000) / 1000 * 0.010
                       + 500 / 1000 * 0.050, 8))
    # Put the cache counts on a stage the real path actually reaches: with a
    # single candidate the investigator never ranks, so scripting `ranking`
    # here would assert on a request that is never made.
    install({**BASE, "assessment": _text(
        {"decision": "publish", "reason": "carried",
         "claim_kind": "observation", "confidence": "qualified",
         "overstated_phrases": []},
        usage=usage(500, 60, cache_creation=1500, cache_read=3000))})
    before = len(ledger_rows())
    c.get("/home/findings?days=28&tz=UTC", headers=auth(CEO))
    analysis_jobs.drain(3)
    fresh = ledger_rows()[before:]
    cached = [r for r in fresh if r["stage"] == home_llm_usage.STAGE_ASSESSING]
    check("the real path stores the cache counts the provider reported",
          bool(cached) and cached[0]["cache_creation_input_tokens"] == 1500
          and cached[0]["cache_read_input_tokens"] == 3000,
          f"stages seen={[r['stage'] for r in fresh]}")

    # =================================================================
    print("\n--- one request cannot be billed twice ---")
    # =================================================================
    key = "dup-test-request-key"
    opened = database.open_home_llm_request({
        "request_key": key, "account_id": ACCT, "analysis_job_id": None,
        "job_attempt": None, "analysis_id": None, "scope_key": None,
        "stage": home_llm_usage.STAGE_PROPOSING, "request_seq": 1,
        "model_requested": investigator.MODEL, "started_at": "2026-09-15 00:00:00",
    })
    again = database.open_home_llm_request({
        "request_key": key, "account_id": ACCT, "analysis_job_id": None,
        "job_attempt": None, "analysis_id": None, "scope_key": None,
        "stage": home_llm_usage.STAGE_PROPOSING, "request_seq": 1,
        "model_requested": investigator.MODEL, "started_at": "2026-09-15 00:00:00",
    })
    check("opening the same request twice inserts one row",
          opened and not again and len(ledger_rows(request_key=key)) == 1)
    settle = {"model_served": investigator.MODEL, "outcome": "succeeded",
              "error_kind": None, "finished_at": "2026-09-15 00:00:01",
              "latency_ms": 900, "input_tokens": 100, "output_tokens": 10,
              "cache_creation_input_tokens": None,
              "cache_read_input_tokens": None, "usage_reported": 1,
              "estimated_cost_usd": 0.00045, "pricing_source": "exact",
              "pricing_model_key": investigator.MODEL,
              "pricing_captured_at": "2026-09-15 00:00:00",
              "price_input_per_1k": 0.003, "price_output_per_1k": 0.015}
    check("settling it once takes effect",
          database.close_home_llm_request(key, settle))
    check("settling it again changes nothing",
          not database.close_home_llm_request(key, {**settle, "input_tokens": 999}))
    dup = ledger_rows(request_key=key)[0]
    check("so the recorded usage is the first settlement, not the second",
          dup["input_tokens"] == 100 and len(ledger_rows(request_key=key)) == 1)

    # =================================================================
    print("\n--- an interrupted request stays explicitly unresolved ---")
    # =================================================================
    orphan = "orphan-request-key"
    database.open_home_llm_request({
        "request_key": orphan, "account_id": ACCT, "analysis_job_id": None,
        "job_attempt": None, "analysis_id": None, "scope_key": None,
        "stage": home_llm_usage.STAGE_INVESTIGATING, "request_seq": 1,
        "model_requested": investigator.MODEL, "started_at": "2026-09-15 00:00:00",
    })
    stranded = ledger_rows(request_key=orphan)[0]
    check("a request opened and never settled reads in_flight",
          stranded["outcome"] == home_llm_usage.OUTCOME_IN_FLIGHT)
    check("with no usage and no cost invented for it",
          stranded["input_tokens"] is None
          and stranded["estimated_cost_usd"] is None)

    # =================================================================
    print("\n--- Trovis's spending is not the customer's ---")
    # =================================================================
    snap = c.get("/home/snapshot?days=7&tz=UTC", headers=auth(CEO)).json()
    fin = snap.get("financial") or {}
    ours = sum(float(r["estimated_cost_usd"]) for r in ledger_rows()
               if r["estimated_cost_usd"] is not None)
    check("we did spend something on this account's analysis", ours > 0)
    check("and none of it appears in the customer's Home cost figure",
          abs(float(fin.get("spend_usd") or 0.0)) < 1e-9,
          f"customer spend={fin.get('spend_usd')} ours={ours}")
    check("the customer-facing snapshot carries no field from our ledger",
          "home_llm" not in json.dumps(snap, default=str))
    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute("SELECT COALESCE(SUM(estimated_cost_usd), 0) AS s FROM spans")
        span_cost = float(dict(cur.fetchone())["s"])
    check("and our requests were never written into the spans table",
          abs(span_cost) < 1e-9)

    # =================================================================
    print("\n--- the internal report ---")
    # =================================================================
    os.environ.pop("TROVIS_INTERNAL_OPS", None)
    denied = False
    try:
        home_llm_report.main(["--days", "7"])
    except SystemExit as exc:
        denied = exc.code == 2
    check("the report refuses to run without an explicit operator confirmation",
          denied)
    os.environ["TROVIS_INTERNAL_OPS"] = "1"

    all_rows = ledger_rows()
    jobs_status = {}
    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute("SELECT id, status FROM analysis_jobs")
        jobs_status = {int(dict(r)["id"]): dict(r)["status"]
                       for r in (cur.fetchall() or [])}
    rep = home_llm_report.collect(all_rows, jobs_status)
    check("it counts every recorded request", rep["requests"] == len(all_rows))
    check("the estimated subtotal covers only what we could price",
          rep["priced_requests"] + rep["unknown_spend_requests"] == rep["requests"])
    check("unknown spending is reported separately, never folded in as zero",
          rep["unknown_spend_requests"] > 0
          and rep["estimated_subtotal_usd"] == round(sum(
              float(r["estimated_cost_usd"]) for r in all_rows
              if r["estimated_cost_usd"] is not None), 6))
    check("an unresolved request is visible in the totals",
          rep["unresolved_requests"] >= 1
          and rep["outcomes"].get(home_llm_usage.OUTCOME_IN_FLIGHT, 0) >= 1)
    check("failed requests stay in the totals too",
          rep["outcomes"].get(home_llm_usage.OUTCOME_FAILED, 0) >= 1)
    check("it breaks down by account, model and stage",
          rep["by_account"] and rep["by_model"] and rep["by_stage"])
    check("each breakdown keeps its own unpriced count",
          all("unknown_cost_requests" in v for v in rep["by_stage"].values()))
    check("investigations are counted per (job, attempt), started and completed",
          rep["executions_started"] >= rep["executions_completed"] >= 1)
    check("cost per investigation names both denominators",
          rep["cost_per_started_execution_usd"] is not None
          and rep["cost_per_completed_execution_usd"] is not None)
    check("the started denominator is the larger one, so it includes retries",
          rep["cost_per_started_execution_usd"]
          <= rep["cost_per_completed_execution_usd"])
    # A job that failed once and succeeded on retry is ONE failed execution and
    # ONE completed one. Counting both as completed — because the job did
    # eventually finish — would hide the retry spending.
    check("a superseded attempt counts as failed, not as completed",
          rep["executions_failed"] >= 1
          and rep["executions_started"]
          == rep["executions_completed"] + rep["executions_failed"]
          + rep["executions_unfinished"],
          f"started={rep['executions_started']} done={rep['executions_completed']}"
          f" failed={rep['executions_failed']}"
          f" unfinished={rep['executions_unfinished']}")
    check("no cost-per-user is offered anywhere",
          not any("per_user" in k for k in rep))
    rendered = home_llm_report._render(rep, datetime.now(timezone.utc) -
                                       timedelta(days=7),
                                       datetime.now(timezone.utc))
    check("the rendered report says the total is a floor",
          "PLUS an unknown amount" in rendered)
    check("and explains how to reconcile against the provider",
          "reconcil" in rendered.lower() and "usage console" in rendered)
    check("and refuses a per-user number in words too",
          "no cost-per-user is offered" in rendered)

    # A bounded window really is bounded.
    now = datetime.now(timezone.utc)
    check("the window query is bounded and returns rows for the period",
          len(database.home_llm_requests_between(now - timedelta(days=1), now)) > 0)
    check("and excludes a window that ended before anything ran",
          database.home_llm_requests_between(
              now - timedelta(days=400), now - timedelta(days=399)) == [])
    check("and can be narrowed to one account",
          all(r["account_id"] == ACCT for r in
              database.home_llm_requests_between(
                  now - timedelta(days=1), now, account_id=ACCT)))

print("\n" + ("FAILED: " + "; ".join(failures) if failures else "ALL PASS"))
raise SystemExit(1 if failures else 0)
