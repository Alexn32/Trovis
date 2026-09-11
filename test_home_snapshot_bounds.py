"""GET /home/snapshot: what the bounds do when they bite.

Three follow-up defects, all about a bound quietly turning into a claim:

  1. COMPLETENESS METADATA CONTRADICTED ITSELF. A truncated scope correctly set
     `scope_membership_complete: false` while still reporting
     `completion_series_complete: true`, `job_breakdown_complete: true`,
     `has_any_recorded_work: false` and `workspace_state: "empty"`. Finding no
     rows in an incomplete scope does not establish that no rows exist, and a
     chart that reconciles with an incomplete aggregate is not complete.
  2. IDENTITY RESOLUTION STILL FANNED OUT. Batching candidates fixed the event
     queries but `_target_user_id` still ran once per DISTINCT target, so 501
     real users with 501 distinct numeric targets cost ~504 statements.
  3. EVENT VOLUME WAS UNBOUNDED. Capping candidate WORK ITEMS at 500 does not
     cap their EVENTS: one long-running item can carry thousands of handoffs,
     and `fetchall()` took all of them.

The third is the dangerous one. A truncated event history is not a weaker
answer, it is a potentially INVERTED one — the row that would have resolved a
pending handoff is exactly the row that got cut — so a clipped loop must
contribute nothing rather than a guess.

Run:
  OVERSEE_DISABLE_PRICING_SYNC=1 python3 test_home_snapshot_bounds.py
(isolated temp SQLite DB; never touches the dev/prod DB)
"""
import contextlib
import os
import tempfile
import time

os.environ.update({
    "OVERSEE_DISABLE_PRICING_SYNC": "1",
    "TROVIS_DISABLE_ALERTS": "1",
    "TROVIS_DISABLE_LOOP_SWEEP": "1",
    "TROVIS_LOOP_TITLES": "off",
})
os.environ.pop("DATABASE_URL", None)
os.environ.pop("ANTHROPIC_API_KEY", None)

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()

import database
database.SQLITE_PATH = _tmp.name

import asker
import describer
import loops as loops_mod
import main
from fastapi.testclient import TestClient

main._auto_describe = lambda *a, **k: False

failures = []


def check(label, cond):
    print(("  PASS " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


class _Exploding:
    def __init__(self, *a, **k):
        raise AssertionError("the snapshot path must never call a model")


describer.anthropic.Anthropic = _Exploding
asker.anthropic.Anthropic = _Exploding

NS = 10**9
NOW = time.time_ns()
_n = [0]


def auth(tok):
    return {"Authorization": f"Bearer {tok}"}


def span(service, off_s, attrs, name="message_received"):
    _n[0] += 1
    t = NOW - int(off_s) * NS
    return {
        "trace_id": f"t{_n[0]:028d}", "span_id": f"s{_n[0]:014d}",
        "parent_span_id": None, "service_name": service, "agent_id": "main",
        "span_name": name, "kind": 1,
        "start_time_unix": t, "end_time_unix": t + 10**6,
        "status_code": 0, "status_message": "",
        "attributes": attrs, "resource_attributes": {},
    }


# --- counting cursor -------------------------------------------------------
_stmts: list[str] = []
_real_cursor = database._cursor


class _CountingCursor:
    def __init__(self, inner):
        self._inner = inner

    def execute(self, sql, *a, **k):
        _stmts.append(sql)
        return self._inner.execute(sql, *a, **k)

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def __iter__(self):
        return iter(self._inner)


@contextlib.contextmanager
def _counting_cursor(conn):
    with _real_cursor(conn) as cur:
        yield _CountingCursor(cur)


database._cursor = _counting_cursor


@contextlib.contextmanager
def counted():
    start = len(_stmts)
    box = {}
    yield box
    box["n"] = len(_stmts) - start
    box["sql"] = _stmts[start:]


def loop_id_by_title(title):
    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute(f"SELECT id FROM loops WHERE title = {database.PH}", (title,))
        row = cur.fetchone()
        return int(row["id"]) if row else None


def add_events(loop_id, account_id, specs):
    """Append raw handoff lifecycle events, in order."""
    with database._connect() as conn, database._cursor(conn) as cur:
        for i, (etype, payload) in enumerate(specs):
            database.append_loop_event(
                cur, loop_id, etype, "agent", "svc:main",
                payload=payload, account_id=account_id,
                # After every ingested span above, so the fold sees a
                # real continuation of each loop's stream.
                event_time_unix=NOW - (1_000 - i) * NS,
            )


with TestClient(main.app) as c:
    def snap(tok, **q):
        qs = "&".join(f"{k}={v}" for k, v in q.items() if v is not None)
        return c.get(f"/home/snapshot{'?' + qs if qs else ''}", headers=auth(tok))

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
    mgr_role = c.post("/org/roles", headers=auth(CEO), json={
        "title": "Manager", "parent_role_id": ceo_role["id"],
        "scope_level_id": lv["manager"]["id"]}).json()
    ic_role = c.post("/org/roles", headers=auth(CEO), json={
        "title": "IC", "parent_role_id": mgr_role["id"],
        "scope_level_id": lv["ic"]["id"]}).json()

    def join(email, name, role_id):
        r = c.post("/org/invites", headers=auth(CEO),
                   json={"email": email, "name": name, "role_id": role_id}).json()
        tok = r["invite_url"].split("token=")[1]
        out = c.post("/auth/accept-invite", json={
            "token": tok, "name": name, "password": "correct horse battery"}).json()
        return out["token"], out["user"]["id"]

    MGR, mgr_id = join("mgr@acme.test", "Mo Reyes", mgr_role["id"])
    REP, rep_id = join("rep@acme.test", "Rae Park", ic_role["id"])

    CAP = database._ASSIGNEE_SCAN_LIMIT
    BUDGET = database._ASSIGNEE_EVENT_BUDGET
    CHUNK = database._ASSIGNEE_EVENT_CHUNK
    BATCH = database._IDENTITY_BATCH

    def handoff_pair(ext, title, target, off, svc="desk-agent"):
        return [
            span(svc, off + 10, {
                "trovis.loop.title": title, "trovis.loop.external_id": ext}),
            span(svc, off, {
                "trovis.loop.external_id": ext,
                "trovis.handoff.direction": "to_human",
                "trovis.handoff.target_id": target,
                "trovis.handoff.id": f"H-{ext}"}, name="agent_run_complete"),
        ]

    # =====================================================================
    print("\n=== 3. the handoff-event budget ===")
    # =====================================================================
    # Run this first, on a small account, so the budget can be shrunk without
    # fighting the 501-item fixtures below.
    print("\n--- a complete history below the budget ---")
    database.ingest_spans_with_loops(
        handoff_pair("norm", "Normal history", "mgr@acme.test", 5000),
        account_id=ACCT)
    norm_id = loop_id_by_title("Normal history")
    # Accepted then re-handed off: the fold must still land on the live one.
    add_events(norm_id, ACCT, [
        ("handoff_accepted", {"handoff_id": "H-norm"}),
        ("handoff_initiated", {"direction": "to_human",
                               "target_id": "rep@acme.test",
                               "handoff_id": "H-norm-2"}),
    ])
    with database._connect() as conn, database._cursor(conn) as cur:
        t, inc = database.assigned_handoff_targets(cur, ACCT, named_only=True)
    check("a complete history resolves to the LIVE handoff, not the accepted one",
          t.get(norm_id) == "rep@acme.test" and inc is False)

    # Declined and completed resolutions, still below budget.
    database.ingest_spans_with_loops(
        handoff_pair("res", "Resolved history", "mgr@acme.test", 4900),
        account_id=ACCT)
    res_id = loop_id_by_title("Resolved history")
    add_events(res_id, ACCT, [
        ("handoff_declined", {"handoff_id": "H-res"}),
        ("handoff_initiated", {"direction": "to_human",
                               "target_id": "mgr@acme.test",
                               "handoff_id": "H-res-2"}),
        ("handoff_completed", {"handoff_id": "H-res-2"}),
    ])
    with database._connect() as conn, database._cursor(conn) as cur:
        t, inc = database.assigned_handoff_targets(cur, ACCT, named_only=True)
    check("declined + completed resolutions leave nothing pending",
          res_id not in t and inc is False)

    print("\n--- one item with many events, past the budget ---")
    database.ingest_spans_with_loops(
        handoff_pair("fat", "Fat history", "mgr@acme.test", 4800),
        account_id=ACCT)
    fat_id = loop_id_by_title("Fat history")
    # 60 more handoff events on this ONE item: capping items would not have
    # capped these. The last one resolves the pending handoff, so a truncated
    # read would invent a live assignment that does not exist.
    fat_events = []
    for i in range(59):
        fat_events.append(("handoff_initiated", {
            "direction": "to_human", "target_id": "mgr@acme.test",
            "handoff_id": f"F{i}"}))
        fat_events.append(("handoff_accepted", {"handoff_id": f"F{i}"}))
    fat_events.append(("handoff_completed", {"handoff_id": "H-fat"}))
    add_events(fat_id, ACCT, fat_events)

    with database._connect() as conn, database._cursor(conn) as cur:
        full, inc_full = database.assigned_handoff_targets(cur, ACCT, named_only=True)
    check("read whole, the fat item's final resolution clears it",
          fat_id not in full and inc_full is False)

    real_budget = database._ASSIGNEE_EVENT_BUDGET
    try:
        # A budget that cuts INSIDE the fat item's history, after its opening
        # handoff but before the resolution that closes it.
        database._ASSIGNEE_EVENT_BUDGET = 5
        with database._connect() as conn, database._cursor(conn) as cur:
            clipped, inc_clip = database.assigned_handoff_targets(
                cur, ACCT, named_only=True)
        check("a clipped history yields NO assignment, not a stale pending one",
              fat_id not in clipped)
        check("exhausting the budget marks the resolution incomplete",
              inc_clip is True)
        check("only positively established matches survive the budget",
              all(lid in full or lid not in clipped for lid in clipped))

        # Multiple items exceeding the combined budget: the ones we did read
        # whole still count, the rest are simply absent.
        database._ASSIGNEE_EVENT_BUDGET = 3
        with database._connect() as conn, database._cursor(conn) as cur:
            few, inc_few = database.assigned_handoff_targets(cur, ACCT, named_only=True)
        check("multiple items over the combined budget: partial + flagged",
              inc_few is True and len(few) <= len(full))
        check("a tiny budget never invents a target",
              set(few) <= set(full) | {norm_id})

        # End to end: the budget's incompleteness reaches the response.
        database._ASSIGNEE_EVENT_BUDGET = 5
        over_budget = snap(MGR, days=7, tz="UTC").json()
        check("a budget-exhausted snapshot reports attention unavailable",
              over_budget["attention"]["available"] is False
              and over_budget["attention"]["needs_you"] is None
              and over_budget["attention"]["unavailable_reason"]
              == "assignment_resolution_incomplete"
              and over_budget["attention"]["resolution_complete"] is False)
        check("the event budget propagates to scope membership too",
              over_budget["scope"]["membership_complete"] is False
              and over_budget["completeness"]["assignment_resolution_complete"]
              is False)
    finally:
        database._ASSIGNEE_EVENT_BUDGET = real_budget

    with database._connect() as conn, database._cursor(conn) as cur:
        with counted() as box:
            database.assigned_handoff_targets(cur, ACCT, named_only=True)
    check("event retrieval is one statement per chunk, never a paging loop",
          box["n"] <= 1 + 2)
    check("the budget is a real module constant, not a magic number",
          isinstance(BUDGET, int) and BUDGET > 0
          and "LIMIT" in "".join(q for q in box["sql"] if "loop_events" in q))

    # =====================================================================
    print("\n=== 2. identity resolution is batched ===")
    # =====================================================================
    # An isolated org so the 501 users do not disturb the fixtures above.
    other = c.post("/auth/signup", json={
        "email": "boss@other.test", "password": "correct horse battery",
        "name": "Bo", "account_type": "business", "org_name": "Other",
    }).json()
    OACCT, OTHER = other["org"]["id"], other["token"]
    o_boss = other["user"]["id"]

    # 501 real users, created straight through the data layer (the invite flow
    # would be 501 HTTP round trips for no extra coverage).
    user_ids, emails = [], []
    for i in range(501):
        email = f"u{i}@other.test"
        u = database.create_user(OACCT, email, f"U{i}")
        user_ids.append(u["id"])
        emails.append(email)

    def seed_targets(prefix, targets, svc):
        spans = []
        for i, tgt in enumerate(targets):
            spans += handoff_pair(f"{prefix}{i}", f"{prefix} {i}", tgt,
                                  6000 - i, svc=svc)
        database.ingest_spans_with_loops(spans, account_id=OACCT)

    # (a) hundreds of DISTINCT numeric ids.
    seed_targets("num", [str(u) for u in user_ids[:CAP]], "num-agent")
    with database._connect() as conn, database._cursor(conn) as cur:
        with counted() as distinct_ids:
            database._work_person_filter(cur, OACCT, [user_ids[0], user_ids[1]])
    check(f"{CAP} distinct numeric ids resolve in bounded batches",
          distinct_ids["n"] <= 1 + (CAP // CHUNK + 1) + (CAP // BATCH + 1) + 2)
    check("distinct numeric ids do not cost one statement each",
          distinct_ids["n"] < 20)

    # (b) hundreds of DISTINCT emails, plus (c) mixed + aliases, (d) repeats,
    #     (e) nonexistent and cross-account targets.
    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute(f"DELETE FROM loop_events WHERE account_id = {database.PH}",
                    (OACCT,))
        cur.execute(f"DELETE FROM loops WHERE account_id = {database.PH}", (OACCT,))
    seed_targets("eml", emails[:CAP], "eml-agent")
    with database._connect() as conn, database._cursor(conn) as cur:
        with counted() as distinct_emails:
            database._work_person_filter(cur, OACCT, [user_ids[0], user_ids[1]])
    check(f"{CAP} distinct emails resolve in bounded batches",
          distinct_emails["n"] < 20)

    with database._connect() as conn, database._cursor(conn) as cur:
        # Mixed ids and emails, aliases for the same user (id + email + a
        # differently-cased email), repeats, a nonexistent id, a target with no
        # login, and a cross-account user's real id and email.
        mixed = [
            str(user_ids[0]), emails[0], emails[0].upper(),
            str(user_ids[1]), emails[1],
            str(user_ids[2]), str(user_ids[2]), str(user_ids[2]),
            "99999999", "not-a-target", "", "   ",
            str(ceo_id), "ceo@acme.test",
        ]
        with counted() as mixed_box:
            ident = database.resolve_target_user_ids(cur, mixed, OACCT)
    check("an id and both spellings of its email resolve to the same user",
          ident[str(user_ids[0])] == user_ids[0]
          and ident[emails[0]] == user_ids[0]
          and ident[emails[0].upper()] == user_ids[0])
    check("repeats are deduplicated, not re-queried",
          ident[str(user_ids[2])] == user_ids[2] and mixed_box["n"] <= 2)
    check("unknown ids, non-targets and blanks resolve to None, not an error",
          ident["99999999"] is None and ident["not-a-target"] is None
          and ident[""] is None and ident["   "] is None)
    check("cross-account targets never match, by id or by email",
          ident[str(ceo_id)] is None and ident["ceo@acme.test"] is None)

    # A target with no login at all (a team_members-style directory entry).
    with database._connect() as conn, database._cursor(conn) as cur:
        nologin = database.resolve_target_user_ids(
            cur, ["nobody@other.test", "Some Person"], OACCT)
    check("a target with no login resolves to None",
          nologin["nobody@other.test"] is None and nologin["Some Person"] is None)

    # Multiple people in the selected scope, and a full snapshot including
    # personal attention.
    with database._connect() as conn, database._cursor(conn) as cur:
        with counted() as many_people:
            database._work_person_filter(
                cur, OACCT, [o_boss, *user_ids[:25]])
    check("asking about 26 people costs the same as asking about 2",
          many_people["n"] == distinct_emails["n"])

    with counted() as full_req:
        r = snap(OTHER, days=7, tz="UTC")
    check("a full snapshot over 500 distinct email targets answers",
          r.status_code == 200)
    check("the whole request stays in double digits of statements",
          full_req["n"] < 40)
    check("scope membership and personal attention share one identity map",
          sum(1 for q in full_req["sql"] if "LOWER(email) IN" in q) <= 2)

    # =====================================================================
    print("\n=== 1. completeness metadata is consistent ===")
    # =====================================================================
    # 501 named open items with unresolved handoffs; the oldest is the
    # manager's, the rest belong to someone with no work in the manager's line.
    bulk = handoff_pair("cap-yours", "Oldest of all, yours", "mgr@acme.test", 7000)
    for i in range(CAP):
        bulk += handoff_pair(f"cap-{i}", f"Theirs {i}", "rep@acme.test", 6000 - i)
    database.ingest_spans_with_loops(bulk, account_id=ACCT)

    # Someone real with no work of their own: a scope that finds nothing while
    # the assignment scan is capped, which is the exact contradiction.
    GHOST, ghost_id = join("ghost@acme.test", "Gwen Host", ic_role["id"])

    view = snap(CEO, days=7, whose="person", person_id=ghost_id).json()
    comp = view["completeness"]
    mgr_view = snap(MGR, days=7, tz="UTC").json()
    check("the capped scan does miss the viewer's oldest item",
          mgr_view["attention"]["needs_you"] is None
          and mgr_view["attention"]["needs_you_at_least"] == 0)
    check("membership is incomplete, as before",
          comp["scope_membership_complete"] is False
          and comp["counts_exact"] is False)
    check("a chart over incomplete membership is NOT a complete chart",
          comp["completion_series_complete"] is False
          and comp["job_breakdown_complete"] is False)
    check("reconciliation and completeness are separate answers",
          view["completions_series"]["reconciles"] is True
          and comp["completion_series_complete"] is False)
    check("an empty incomplete scope is UNKNOWN, not empty",
          comp["scope_state"] == "unknown"
          and comp["has_any_work_in_scope"] is None
          and comp["absence_established"] is False)
    check("the workspace is not called empty because one view is",
          comp["workspace_state"] == "populated"
          and comp["has_any_recorded_work"] is True)
    check("freshness says its nulls are not established absences",
          view["freshness"]["absence_established"] is False
          and view["freshness"]["unavailable_reason"]
          == "scope_membership_incomplete")
    check("telemetry freshness stays exact (account-wide, membership-free)",
          view["freshness"]["latest_telemetry_at"] is not None)
    reasons = {u["field"] for u in comp["unavailable"]}
    check("every affected field is named in one place",
          {"scope.membership", "period.completed", "current_state",
           "completions_series", "by_job", "freshness",
           "completeness.scope_state"} <= reasons)
    check("per-block flags and summary flags agree",
          view["period"]["exact"] is comp["counts_exact"]
          and view["completions_series"]["exact"] is False
          and view["by_job"]["exact"] is False
          and view["scope"]["membership_complete"] is False)

    # A scope that finds rows, while still incomplete: existence IS
    # established even though absence is not.
    found = snap(CEO, days=7, whose="person", person_id=rep_id).json()
    check("a narrowed scope that finds rows reports scope_state populated",
          found["completeness"]["scope_state"] == "populated"
          and found["completeness"]["has_any_work_in_scope"] is True)

    # The rule itself, across several real scopes: populated iff rows were
    # found; empty only when the absence was actually established; unknown
    # otherwise. Never "empty" off the back of a partial search.
    coherent = True
    for who, pid in (("person", ghost_id), ("person", mgr_id),
                     ("person", rep_id), ("everyone", None)):
        cc = snap(CEO, days=7, whose=who, person_id=pid).json()["completeness"]
        found_rows = cc["has_any_work_in_scope"]
        want = ("populated" if found_rows else
                ("empty" if cc["absence_established"] else "unknown"))
        if cc["scope_state"] != want or cc["workspace_state"] != "populated":
            coherent = False
            print(f"    (scope {who}:{pid} -> {cc['scope_state']}, wanted {want})")
    check("scope_state follows found-rows and absence-established, always",
          coherent)

    # A company seat: no person filter, so everything is exact and complete.
    whole = snap(CEO, days=7, tz="UTC").json()
    wc = whole["completeness"]
    check("a company seat reports complete charts and an established absence",
          wc["scope_membership_complete"] is True
          and wc["completion_series_complete"] is True
          and wc["job_breakdown_complete"] is True
          and wc["absence_established"] is True
          and whole["freshness"]["absence_established"] is True)

    # A brand-new workspace: empty, established, and not "unknown".
    fresh = c.post("/auth/signup", json={
        "email": "new@third.test", "password": "correct horse battery",
        "name": "Nia", "account_type": "business", "org_name": "Third",
    }).json()
    nf = snap(fresh["token"], days=7, tz="UTC").json()
    check("an empty workspace is empty, not unknown",
          nf["completeness"]["workspace_state"] == "empty"
          and nf["completeness"]["scope_state"] == "empty"
          and nf["completeness"]["has_any_work_in_scope"] is False
          and nf["completeness"]["absence_established"] is True)

print("\n" + ("FAILED: " + "; ".join(failures) if failures else "ALL PASS"))
raise SystemExit(1 if failures else 0)
