"""Plan limit counts INSTANCES (service_name), not sub-agents.

A single instance with many sub-agents consumes ONE plan slot; its sub-agents
are free and inherit the instance's lock. This guards the monetization invariant
against a regression back to per-sub-agent counting (which produced confusing
"12 of 5" states for multi-agent instances).
"""

import os
import tempfile
import time
import uuid

# Isolate the DB before importing database (never touch the dev/prod DB).
os.environ["OVERSEE_DISABLE_PRICING_SYNC"] = "1"
os.environ.pop("DATABASE_URL", None)
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()

import database as db  # noqa: E402

db.SQLITE_PATH = _tmp.name

_failures = []


def check(label, cond):
    print(f"  {'PASS' if cond else 'FAIL'} {label}")
    if not cond:
        _failures.append(label)


def _span(svc, agent_id, i, now):
    return {
        "trace_id": uuid.uuid4().hex,
        "span_id": uuid.uuid4().hex[:16],
        "parent_span_id": None,
        "service_name": svc,
        "span_name": "op",
        "kind": 0,
        "start_time_unix": now - i * 1_000_000_000,
        "end_time_unix": now - i * 1_000_000_000 + 1000,
        "status_code": 0,
        "status_message": "",
        "attributes": {"trovis.agent.id": agent_id, "oversee.agent.id": agent_id},
        "resource_attributes": {"service.name": svc},
    }


def main():
    db.init_db()
    acct = db.create_account("owner@test.com", account_type="business", name="Test Co")
    aid = acct["id"]  # free plan → limit 5 instances

    now = int(time.time() * 1e9)
    # 6 instances, 11 sub-agents total. Oldest 5 stay; the newest instance locks.
    # inst-0 is inserted with the most-recent timestamps (i=0..), so it's newest.
    subcounts = {"inst-0": 4, "inst-1": 3, "inst-2": 1, "inst-3": 1, "inst-4": 1, "inst-5": 1}
    rows, i = [], 0
    for svc, n in subcounts.items():
        for a in range(n):
            rows.append(_span(svc, f"agent-{a}", i, now))
            i += 1
    db.insert_spans(rows, account_id=aid)

    lock = db.get_locked_state(aid)
    check("agent_count = 6 instances (not 11 sub-agents)", lock["agent_count"] == 6)
    check("limit = 5", lock["limit"] == 5)
    check("locked_count = 1 instance beyond limit", lock["locked_count"] == 1)
    check("locked keys are service_names (strings)", all(isinstance(x, str) for x in lock["locked"]))

    groups = {g["service_name"]: g for g in db.get_agents(account_id=aid)}
    locked_sn = next(iter(lock["locked"]))
    lg = groups[locked_sn]
    check("locked instance → group locked", lg["locked"] is True)
    check("locked instance → all sub-agents inherit lock", all(a["locked"] for a in lg["agents"]))

    # A multi-sub-agent UNLOCKED instance keeps every sub-agent visible, counts once.
    open_multi = next(
        g for sn, g in groups.items() if sn not in lock["locked"] and len(g["agents"]) >= 2
    )
    check("open multi-instance not locked", open_multi["locked"] is False)
    check("open multi-instance: no sub-agent locked", not any(a["locked"] for a in open_multi["agents"]))
    check("open multi-instance kept all its sub-agents", len(open_multi["agents"]) >= 2)

    print("\n" + ("ALL CHECKS PASSED" if not _failures else f"{len(_failures)} FAILED"))
    try:
        os.remove(_tmp.name)
    except OSError:
        pass
    raise SystemExit(1 if _failures else 0)


if __name__ == "__main__":
    main()
