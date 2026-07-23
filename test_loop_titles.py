"""Server-side loop title generation tests.

Pipeline: plugin title always wins (never overwritten) -> LLM title (mocked
here, per the _auto_describe test precedent of stubbing the Claude boundary)
-> template fallback. Storage is the single NULL-guarded UPDATE — first
title wins forever.

Run:
  OVERSEE_DISABLE_PRICING_SYNC=1 python3 test_loop_titles.py
"""
import os
import tempfile
import time

os.environ["OVERSEE_DISABLE_PRICING_SYNC"] = "1"
os.environ["TROVIS_DISABLE_ALERTS"] = "1"
os.environ["TROVIS_DISABLE_LOOP_SWEEP"] = "1"
os.environ.pop("DATABASE_URL", None)
os.environ.pop("ANTHROPIC_API_KEY", None)  # llm path must fail soft -> template
os.environ["TROVIS_LOOP_TITLES"] = "llm"  # this test owns the flag

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()

import database
database.SQLITE_PATH = _tmp.name

import describer
import loops
import main
from fastapi.testclient import TestClient

main._auto_describe = lambda *a, **k: False
# main's load_dotenv(override=True) restores the real key from .env — pop it
# AFTER the import so the llm tier genuinely fails soft in this test.
os.environ.pop("ANTHROPIC_API_KEY", None)

failures = []
def check(label, cond):
    print(("  PASS " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


def otlp_attrs(d):
    return [{"key": k, "value": {"stringValue": str(v)}} for k, v in d.items()]


_seq = [0]
def span(name, start, attrs):
    _seq[0] += 1
    return {
        "traceId": f"{_seq[0]:032d}", "spanId": f"{_seq[0]:016d}", "name": name,
        "kind": 1, "startTimeUnixNano": str(start),
        "endTimeUnixNano": str(start + 5_000_000),
        "status": {"code": 1}, "attributes": otlp_attrs(attrs),
    }


def post(client, key, service, spans):
    payload = {"resourceSpans": [{
        "resource": {"attributes": otlp_attrs({
            "service.name": service, "trovis.plugin.version": "1.0.0",
        })},
        "scopeSpans": [{"spans": spans}],
    }]}
    return client.post("/v1/traces", json=payload, headers={"X-Trovis-Api-Key": key})


NS = 1_000_000_000
NOW = time.time_ns()

with TestClient(main.app) as c:
    r = c.post("/auth/signup", json={
        "email": "titles@test.com", "password": "supersecret123",
        "name": "Title Tester", "account_type": "individual", "org_name": "Title Co",
    })
    assert r.status_code == 201, r.text
    key = r.json()["api_key"]
    tok = c.post("/auth/login", json={
        "email": "titles@test.com", "password": "supersecret123",
    }).json()["token"]
    account_id = c.get("/auth/me", headers={"Authorization": f"Bearer {tok}"}).json()["org"]["id"]

    def loop_for(service):
        return [l for l in database.get_loops(account_id, limit=100)
                if l["service_name"] == service][0]

    # --- 1. Plugin title always wins, never regenerated ---
    assert post(c, key, "svc-plugin", [span("w", NOW - NS, {
        "trovis.run.id": "t1", "trovis.loop.title": "Plugin says so",
        "trovis.loop.close": "done",
    })]).status_code == 200
    lid = loop_for("svc-plugin")["id"]
    check("plugin title wins", loop_for("svc-plugin")["title"] == "Plugin says so")
    check("shape is None for titled loops (no LLM call would ever fire)",
          database.get_loop_title_shape(lid, account_id) is None)
    check("ensure_loop_title no-ops on titled loops",
          loops.ensure_loop_title(lid, account_id) is False)
    check("NULL-only write refuses to overwrite",
          database.set_loop_title_if_missing(lid, "clobber", account_id) is False
          and loop_for("svc-plugin")["title"] == "Plugin says so")

    # --- 2. Template fallback (no ANTHROPIC key -> llm path fails soft) ---
    assert post(c, key, "svc-tmpl", [
        span("work", NOW - 2 * NS, {"trovis.run.id": "t2",
                                    "trovis.tool.name": "exec"}),
        span("fin", NOW - NS, {"trovis.run.id": "t2", "trovis.loop.close": "done"}),
    ]).status_code == 200
    lid = loop_for("svc-tmpl")["id"]
    check("terminal loop starts untitled (sweep is the trigger)",
          loop_for("svc-tmpl")["title"] is None)
    check("ensure_loop_title writes the template fallback",
          loops.ensure_loop_title(lid, account_id) is True
          and loop_for("svc-tmpl")["title"] == "svc-tmpl · exec · 2 actions")
    check("second ensure is a no-op (one title per loop ever)",
          loops.ensure_loop_title(lid, account_id) is False)

    # --- 3. LLM path (mocked at the describer boundary) ---
    assert post(c, key, "svc-llm", [
        span("work", NOW - 2 * NS, {"trovis.run.id": "t3",
                                    "trovis.tool.name": "web_search"}),
        span("fin", NOW - NS, {"trovis.run.id": "t3", "trovis.loop.close": "done"}),
    ]).status_code == 200
    lid = loop_for("svc-llm")["id"]
    _orig = describer.loop_title
    describer.loop_title = lambda shape: "Research sweep for the weekly digest"
    try:
        check("LLM title stored when the call succeeds",
              loops.ensure_loop_title(lid, account_id) is True
              and loop_for("svc-llm")["title"] == "Research sweep for the weekly digest")
    finally:
        describer.loop_title = _orig

    # LLM raising -> template fallback, still fail-soft.
    assert post(c, key, "svc-boom", [
        span("fin", NOW - NS, {"trovis.run.id": "t4", "trovis.loop.close": "done"}),
    ]).status_code == 200
    lid = loop_for("svc-boom")["id"]
    describer.loop_title = lambda shape: (_ for _ in ()).throw(RuntimeError("api down"))
    try:
        check("LLM failure falls back to template (never raises)",
              loops.ensure_loop_title(lid, account_id) is True
              and loop_for("svc-boom")["title"] == "svc-boom · run · 1 actions")
    finally:
        describer.loop_title = _orig

    # --- 4. TROVIS_LOOP_TITLES flag kills the LLM tier, template still stored ---
    assert post(c, key, "svc-flag", [
        span("fin", NOW - NS, {"trovis.run.id": "t5", "trovis.loop.close": "done"}),
    ]).status_code == 200
    lid = loop_for("svc-flag")["id"]
    os.environ["TROVIS_LOOP_TITLES"] = "off"
    describer.loop_title = lambda shape: (_ for _ in ()).throw(
        AssertionError("LLM tier must not be called when the flag is off"))
    try:
        check("flag off: template stored without touching the LLM tier",
              loops.ensure_loop_title(lid, account_id) is True
              and loop_for("svc-flag")["title"] == "svc-flag · run · 1 actions")
    finally:
        describer.loop_title = _orig
        os.environ.pop("TROVIS_LOOP_TITLES", None)

    # --- 5. Sweep title pass drains untitled terminal loops (capped) ---
    assert post(c, key, "svc-sweep", [
        span("fin", NOW - NS, {"trovis.run.id": "t6", "trovis.loop.close": "done"}),
    ]).status_code == 200
    summary = loops.run_sweep_for_account(account_id)
    check("sweep titles untitled terminal loops",
          summary["titled"] >= 1 and loop_for("svc-sweep")["title"] is not None)
    check("sweep re-run titles nothing new (idempotent)",
          loops.run_sweep_for_account(account_id)["titled"] == 0)

    # --- 6. Ingestion trigger: first handoff titles the loop ---
    describer.loop_title = lambda shape: "Waiting on approval from ops"
    try:
        assert post(c, key, "svc-handoff", [span("ask", NOW - NS, {
            "trovis.run.id": "t7", "trovis.handoff.direction": "to_human",
        })]).status_code == 200
        check("first handoff triggers a title at ingest",
              loop_for("svc-handoff")["title"] == "Waiting on approval from ops")
    finally:
        describer.loop_title = _orig

print()
if failures:
    print(f"FAILED: {len(failures)} check(s):")
    for f in failures:
        print("  - " + f)
    raise SystemExit(1)
print("ALL CHECKS PASSED")
os.unlink(_tmp.name)
