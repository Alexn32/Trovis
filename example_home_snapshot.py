"""Generate the example GET /home/snapshot response in HOME_SNAPSHOT.md.

FIXTURE DATA. Every number this prints comes from a throwaway SQLite database
seeded by this script — it is not production data and not a screenshot of a
real customer. It exists so the documented example can be regenerated and
checked rather than trusted.

THE CLOCK IS PINNED BEFORE ANYTHING RUNS, not after. An earlier version
queried live time and then overwrote the timestamps in the response, which
produced a documented example no query had ever returned — the period
boundaries, the buckets and the lifecycle states all belonged to a different
instant than the one printed. Here `_FrozenClock` is installed into
`database`, `loops` and `home_snapshot` first, so the seeding, the state
machine, the period arithmetic and `generated_at` all read the same instant
and the output is byte-identical on every run.

Run:
  OVERSEE_DISABLE_PRICING_SYNC=1 python3 example_home_snapshot.py
"""
import json
import os
import tempfile
import time as _time
from datetime import datetime, timezone

os.environ.update({
    "OVERSEE_DISABLE_PRICING_SYNC": "1",
    "TROVIS_DISABLE_ALERTS": "1",
    "TROVIS_DISABLE_LOOP_SWEEP": "1",
    "TROVIS_LOOP_TITLES": "off",
})
os.environ.pop("DATABASE_URL", None)
os.environ.pop("ANTHROPIC_API_KEY", None)

# The one instant this whole script lives at.
FAKE_NOW = datetime(2026, 9, 11, 18, 0, 0, tzinfo=timezone.utc)
FAKE_NOW_NS = int(FAKE_NOW.timestamp() * 1_000_000_000)
NS = 10**9


class _FrozenClock:
    """The real `time` module with `time()` / `time_ns()` pinned."""

    def __getattr__(self, name):
        return getattr(_time, name)

    def time_ns(self):
        return FAKE_NOW_NS

    def time(self):
        return FAKE_NOW_NS / 1e9


_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()

import database
database.SQLITE_PATH = _tmp.name
database.time = _FrozenClock()
database._utcnow = lambda: FAKE_NOW

import loops
loops.time = _FrozenClock()

import home_snapshot
home_snapshot._now_utc = lambda: FAKE_NOW

import main
from fastapi.testclient import TestClient

main._auto_describe = lambda *a, **k: False

_n = [0]


def kv(d):
    return [{"key": k, "value": {"stringValue": str(v)}} for k, v in d.items()]


def sp(name, off_s, attrs):
    _n[0] += 1
    t = FAKE_NOW_NS - int(off_s) * NS
    return {
        "traceId": f"{_n[0]:032d}", "spanId": f"{_n[0]:016d}", "name": name,
        "kind": 1, "startTimeUnixNano": str(t),
        "endTimeUnixNano": str(t + 10**6), "status": {"code": 1},
        "attributes": kv(attrs),
    }


def set_times(title, created, closed=None):
    """Pin a work item's own timestamps. CURRENT_TIMESTAMP is the one clock a
    Python shim cannot reach, so the fixture states them outright."""
    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute(
            f"UPDATE loops SET created_at = {database.PH}, closed_at = {database.PH} "
            f"WHERE title = {database.PH}",
            (created, closed, title),
        )


with TestClient(main.app) as c:
    acct = c.post("/auth/signup", json={
        "email": "ceo@acme.test", "password": "correct horse battery",
        "name": "Cleo", "account_type": "business", "org_name": "Acme",
    }).json()
    KEY, TOKEN = acct["api_key"], acct["token"]
    H = {"Authorization": f"Bearer {TOKEN}"}

    def post(svc, spans):
        return c.post("/v1/traces", json={"resourceSpans": [{
            "resource": {"attributes": kv({"service.name": svc})},
            "scopeSpans": [{"spans": spans}]}]},
            headers={"X-Trovis-Api-Key": KEY})

    job = c.post("/workflows", headers=H, json={
        "name": "Refund requests", "description": "d",
        "steps": [{"step_type": "agent", "label": "run"}]}).json()

    # Five completed refunds, one per local day.
    for i in range(5):
        ext = f"d{i}"
        post("refunds-agent", [
            sp("message_received", 3600 + i, {
                "trovis.loop.title": f"Refund {i}",
                "trovis.loop.external_id": ext}),
            sp("agent_run_complete", 3500 + i, {
                "trovis.loop.external_id": ext, "trovis.loop.close": "done"})])
    # One still moving, one waiting on the viewer, one the sweep gave up on.
    post("refunds-agent", [
        sp("message_received", 300, {
            "trovis.loop.title": "Refund in flight",
            "trovis.loop.external_id": "o1"}),
        sp("tool_call", 200, {
            "trovis.loop.external_id": "o1", "trovis.tool.name": "search"})])
    post("refunds-agent", [
        sp("message_received", 200000, {
            "trovis.loop.title": "Approve exception",
            "trovis.loop.external_id": "o2"}),
        sp("agent_run_complete", 190000, {
            "trovis.loop.external_id": "o2",
            "trovis.handoff.direction": "to_human",
            "trovis.handoff.target_id": "ceo@acme.test",
            "trovis.handoff.id": "H1"})])
    post("refunds-agent", [
        sp("message_received", 400000, {
            "trovis.loop.title": "Refund nobody finished",
            "trovis.loop.external_id": "o3"})])
    # Priced and unpriced spend, so cost coverage has both halves.
    post("refunds-agent", [sp("llm_call", 120, {
        "gen_ai.request.model": "claude-sonnet-4-5",
        "gen_ai.usage.input_tokens": 12000,
        "gen_ai.usage.output_tokens": 3000})])
    post("refunds-agent", [sp("llm_call", 110, {
        "gen_ai.request.model": "unpriced-model",
        "gen_ai.usage.input_tokens": 800, "gen_ai.usage.output_tokens": 200})])

    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute(
            f"UPDATE loops SET workflow_id = {database.PH} "
            "WHERE title LIKE 'Refund %'", (job["id"],))
    for i in range(5):
        set_times(f"Refund {i}",
                  "2026-06-01 00:00:00" if i == 0 else f"2026-09-0{5 + i} 09:00:00",
                  f"2026-09-0{5 + i} 14:0{i}:00")
    set_times("Refund in flight", "2026-09-11 17:00:00")
    set_times("Approve exception", "2026-09-09 10:00:00")
    # An abandoned close: a terminal close that is NOT a completion. It must
    # appear as `period.abandoned` and never inside `period.completed`.
    abandoned = c.get("/work/items", headers=H).json()
    set_times("Refund nobody finished", "2026-09-07 06:00:00",
              "2026-09-09 06:00:00")
    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute(
            "UPDATE loops SET cached_state = 'abandoned' "
            f"WHERE title = {database.PH}", ("Refund nobody finished",))

    out = c.get("/home/snapshot?days=7&tz=America/Chicago", headers=H).json()
    print(json.dumps(out, indent=2))
