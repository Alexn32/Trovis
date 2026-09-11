"""GET /home/snapshot: the four ways it was quietly wrong.

Review found four defects that all share one shape — the endpoint knew less
than it claimed, and said nothing:

  1. ABANDONED WORK COUNTED AS COMPLETED. `closed_at IS NOT NULL` is a
     terminal close, not a completion; the sweep giving up (`abandon_loop`)
     and a phantom reclassification (`artifact_close_loop`) both stamp it.
  2. A CAPPED SCAN REPORTED AS AN EXACT COUNT. The assignee resolver returns
     a truncation flag; the snapshot dropped it, so 501 open handoffs with
     yours oldest produced a confident "0 needs you".
  3. QUERY FAN-OUT. Scope filtering ran one candidate scan per person and one
     event query per candidate — 1,002 statements for 500 candidates and two
     people, under a docstring promising no N+1.
  4. SCOPE METADATA CONTRADICTING THE FILTER. `effective` was inferred from
     whether a selector appeared in the UI's choice list, so a company-breadth
     person with no reports asking for `team` got their own work labeled
     "everyone".

Each section below reproduces the original defect's fixture and pins the
corrected behavior.

Run:
  OVERSEE_DISABLE_PRICING_SYNC=1 python3 test_home_snapshot_integrity.py
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
import home_snapshot
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


def loop_id_by_title(title):
    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute(
            f"SELECT id FROM loops WHERE title = {database.PH}", (title,)
        )
        row = cur.fetchone()
        return int(row["id"]) if row else None


# ---------------------------------------------------------------------------
# Counting cursor: proves the query-shape claims instead of asserting them.
# ---------------------------------------------------------------------------
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
    """Statements issued inside the block."""
    start = len(_stmts)
    box = {}
    yield box
    box["n"] = len(_stmts) - start
    box["sql"] = _stmts[start:]


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

    def join(email, name, role_id):
        r = c.post("/org/invites", headers=auth(CEO),
                   json={"email": email, "name": name, "role_id": role_id}).json()
        tok = r["invite_url"].split("token=")[1]
        out = c.post("/auth/accept-invite", json={
            "token": tok, "name": name, "password": "correct horse battery"}).json()
        return out["token"], out["user"]["id"]

    # =====================================================================
    print("\n=== 1. abandoned work is not completed work ===")
    # =====================================================================
    # Four terminal shapes from ONE fixture, so every completion-related
    # field can be checked against the same ground truth.
    for title, ext in (("Finished properly", "f1"), ("Given up on", "g1"),
                       ("Phantom straggler", "p1"), ("Still going", "o1")):
        database.ingest_spans_with_loops([span("work-agent", 3600, {
            "trovis.loop.title": title, "trovis.loop.external_id": ext,
        })], account_id=ACCT)
    # A real completion: the operator close path.
    database.close_loop(loop_id_by_title("Finished properly"), ACCT, ceo_id)
    # The sweep giving up.
    abandoned_id = loop_id_by_title("Given up on")
    database.abandon_loop(abandoned_id, ACCT)
    # An ingestion artifact — straggler telemetry from a run that already
    # ended. compute_loop_state maps reason='ingestion_artifact' to abandoned.
    phantom_id = loop_id_by_title("Phantom straggler")
    database.artifact_close_loop(phantom_id, "run-1", abandoned_id, ACCT)

    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute(
            "SELECT title, cached_state, closed_at FROM loops "
            f"WHERE account_id = {database.PH} ORDER BY id", (ACCT,)
        )
        states = {r["title"]: (r["cached_state"], r["closed_at"]) for r in cur.fetchall()}
    check("fixture: three terminal closes, only one of them 'done'",
          states["Finished properly"][0] == "done"
          and states["Given up on"][0] == "abandoned"
          and states["Phantom straggler"][0] == "abandoned"
          and all(states[t][1] is not None for t in
                  ("Finished properly", "Given up on", "Phantom straggler"))
          and states["Still going"][1] is None)

    s = snap(CEO, days=7, tz="UTC").json()
    check("period.completed counts the completion only",
          s["period"]["completed"] == 1)
    check("abandonments are reported as themselves, not dropped",
          s["period"]["abandoned"] == 2)
    check("the completion series totals the completion only",
          s["completions_series"]["total"] == 1
          and s["completions_series"]["aggregate_total"] == 1
          and s["completions_series"]["reconciles"] is True)
    check("the job breakdown totals the completion only",
          s["by_job"]["aggregate_total"] == 1
          and s["by_job"]["unclassified_completed"] == 1
          and s["by_job"]["reconciles"] is True)
    check("completion freshness is the completion's close, not an abandonment's",
          s["freshness"]["latest_recorded_completion_at"] is not None
          and s["freshness"]["latest_recorded_completion_at"][:10]
          == str(states["Finished properly"][1])[:10])
    check("open work is unaffected by terminal closes",
          s["current_state"]["open"] == 1)

    # The abandoned records still exist, in their own terminal state.
    check("abandoned records are preserved, not rewritten as completions",
          database.get_loop(abandoned_id, ACCT)["cached_state"] == "abandoned"
          and database.get_loop(phantom_id, ACCT)["cached_state"] == "abandoned")

    # Affected consumers of the shared predicate.
    ov = c.get("/work/overview", headers=auth(CEO)).json()
    check("/work/overview completed_week uses the same definition",
          ov["completed_week"] == 1 and ov["open"] == 1)
    done_rows = c.get("/work/items?status=done", headers=auth(CEO)).json()["items"]
    check("/work/items?status=done lists the completion only",
          [r["title"] for r in done_rows] == ["Finished properly"])
    all_rows = {
        r["title"]: r
        for r in c.get("/work/items", headers=auth(CEO)).json()["items"]
    }
    check("an abandoned row is still listed, and does not say 'Done'",
          all_rows["Given up on"]["whats_next"] == "Closed — not completed"
          and all_rows["Finished properly"]["whats_next"] == "Done")
    check("the locked status enum is unchanged (terminal rows stay 'done')",
          all_rows["Given up on"]["status"] == "done")

    # =====================================================================
    print("\n=== 2. a capped scan is not an exact count ===")
    # =====================================================================
    LEAD, lead_id = join("lead@acme.test", "Lena Ortiz", ceo_role["id"])
    other_role = c.post("/org/roles", headers=auth(CEO), json={
        "title": "Ops lead", "parent_role_id": ceo_role["id"],
        "scope_level_id": lv["manager"]["id"]}).json()
    MGR, mgr_id = join("mgr@acme.test", "Mo Reyes", other_role["id"])
    rep_role = c.post("/org/roles", headers=auth(CEO), json={
        "title": "Rep", "parent_role_id": other_role["id"],
        "scope_level_id": lv["ic"]["id"]}).json()
    REP, rep_id = join("rep@acme.test", "Rae Park", rep_role["id"])

    cap = database._ASSIGNEE_SCAN_LIMIT
    check("the cap under test is the real one", cap == 500)

    # Under the cap first: the same shape must be EXACT and must find the
    # oldest item even though newer non-matching ones sort ahead of it.
    def handoff_pair(ext, title, target, off):
        return [
            span("desk-agent", off + 10, {
                "trovis.loop.title": title, "trovis.loop.external_id": ext}),
            span("desk-agent", off, {
                "trovis.loop.external_id": ext,
                "trovis.handoff.direction": "to_human",
                "trovis.handoff.target_id": target,
                "trovis.handoff.id": f"H-{ext}"}, name="agent_run_complete"),
        ]

    small = handoff_pair("small-yours", "Oldest, yours", "mgr@acme.test", 9000)
    for i in range(5):
        small += handoff_pair(f"small-{i}", f"Newer, theirs {i}",
                              "rep@acme.test", 8000 - i)
    database.ingest_spans_with_loops(small, account_id=ACCT)
    under = snap(MGR, days=7, tz="UTC").json()
    check("under the cap: the oldest matching item is found behind newer ones",
          under["attention"]["available"] is True
          and under["attention"]["needs_you"] == 1)
    check("under the cap: counts are exact",
          under["period"]["exact"] is True
          and under["scope"]["membership_complete"] is True
          and under["completeness"]["counts_exact"] is True)

    # Now past the cap. 501 open named items with unresolved human handoffs:
    # the OLDEST is the manager's, the 500 newest belong to someone else, so
    # the id-DESC candidate scan fills entirely with non-matching rows.
    bulk = handoff_pair("cap-yours", "Oldest of all, yours", "mgr@acme.test", 7000)
    for i in range(cap):
        bulk += handoff_pair(f"cap-{i}", f"Theirs {i}", "rep@acme.test", 6000 - i)
    database.ingest_spans_with_loops(bulk, account_id=ACCT)

    over = snap(MGR, days=7, tz="UTC").json()
    att = over["attention"]
    check("past the cap: attention is UNAVAILABLE, never a confident 0",
          att["available"] is False and att["needs_you"] is None
          and att["unavailable_reason"] == "assignment_resolution_incomplete")
    check("past the cap: what was found is offered as a labeled lower bound",
          att["needs_you_at_least"] == 0)
    check("past the cap: attention still answers to session identity",
          att["scoped_to"] == "session_identity"
          and att["viewer_user_id"] == mgr_id)

    # Scoped work membership: the manager's seat is subtree breadth, so the
    # whose-work filter runs and its waiting-on leg caps too.
    check("past the cap: scope membership is reported incomplete",
          over["scope"]["membership_complete"] is False
          and over["scope"]["membership_incomplete_reason"]
          == "assignment_resolution_incomplete")
    check("past the cap: every scoped count is labeled a lower bound",
          over["period"]["exact"] is False
          and over["period"]["qualifier"] == "at_least"
          and over["current_state"]["exact"] is False
          and over["completions_series"]["exact"] is False
          and over["by_job"]["exact"] is False)
    check("past the cap: a reconciling chart does not claim completeness",
          over["completions_series"]["reconciles"] is True
          and over["completions_series"]["exact"] is False)
    check("past the cap: the comparison is unavailable, not a delta of subsets",
          over["period"]["comparison"]["available"] is False
          and over["period"]["comparison"]["unavailable_reason"]
          == "scope_membership_incomplete")
    reasons = {u["field"]: u["reason"] for u in over["completeness"]["unavailable"]}
    check("past the cap: completeness names every affected field",
          reasons.get("scope.membership") == "assignment_resolution_incomplete"
          and reasons.get("period.completed") == "lower_bound_not_a_total"
          and reasons.get("by_job") == "lower_bound_not_a_total")

    # Personal attention stays personal, even while truncated.
    for whose in ("everyone", "me", "team"):
        a = snap(MGR, days=7, whose=whose).json()["attention"]
        if a["available"] is not False or a["viewer_user_id"] != mgr_id:
            check(f"attention stays personal under scope={whose}", False)
            break
    else:
        check("attention stays personal across scope selections while truncated",
              True)

    # A company-breadth seat applies no person filter at all, so its
    # membership is complete regardless of the assignee cap.
    ceo_over = snap(CEO, days=7, tz="UTC").json()
    check("a company seat's membership is complete (no person filter runs)",
          ceo_over["scope"]["filtered"] is False
          and ceo_over["scope"]["membership_complete"] is True
          and ceo_over["period"]["exact"] is True)
    check("the company seat's own attention is still capped and says so",
          ceo_over["attention"]["available"] is False)

    # Existing handoff semantics are untouched: resolving the manager's
    # handoff must remove it from their desk.
    with database._connect() as conn, database._cursor(conn) as cur:
        targets, truncated_flag = database.assigned_handoff_targets(
            cur, ACCT, named_only=True)
    check("the resolver reports truncation to its callers",
          truncated_flag is True and len(targets) == cap)

    # =====================================================================
    print("\n=== 3. scope filtering has no per-person / per-item fan-out ===")
    # =====================================================================
    # The exact fixture from the report: 500+ candidate handoffs, two people.
    with database._connect() as conn, database._cursor(conn) as cur:
        with counted() as one:
            database._work_person_filter(cur, ACCT, [mgr_id])
        with counted() as two:
            database._work_person_filter(cur, ACCT, [mgr_id, rep_id])
        with counted() as three:
            database._work_person_filter(cur, ACCT, [mgr_id, rep_id, lead_id])

    check("500 candidates / 2 people is a handful of statements, not 1,002",
          two["n"] < 20)
    check("adding people adds no candidate scans and no event queries",
          two["n"] == one["n"] == three["n"])
    check("the statement count is bounded by chunks, not by candidates",
          # 1 candidate scan + ceil(500/200) event chunks + <=1 identity
          # lookup per distinct target.
          one["n"] <= 1 + 3 + 4)
    check("no statement is issued once per candidate loop",
          sum(1 for q in two["sql"] if "loop_events" in q)
          <= -(-cap // database._ASSIGNEE_EVENT_CHUNK) + 1)

    # Team scope end to end — a company-wide fixture would never exercise the
    # person filter at all, which is why the original defect survived.
    with counted() as team_req:
        team = snap(MGR, days=7, tz="UTC", whose="team")
    check("a team-scope snapshot answers over the same fixture",
          team.status_code == 200)
    check("the whole team-scope request stays in double digits of statements",
          team_req["n"] < 40)
    check("scope resolution and personal attention share one resolution",
          # Both need "who is this waiting on?"; the request-scoped cache
          # means the candidate scan is not repeated for the desk.
          sum(1 for q in team_req["sql"] if "EXISTS (SELECT 1 FROM loop_events" in q)
          == 1)

    # =====================================================================
    print("\n=== 4. scope metadata matches the filter that ran ===")
    # =====================================================================
    # Company breadth, no reports: `team` is not a choice a control would
    # offer, but the server applies it and it resolves to this person alone.
    # A LEAF role carrying company breadth: this person sees everything and
    # has nobody under them, which is exactly the shape that produced the
    # contradiction.
    solo_role = c.post("/org/roles", headers=auth(CEO), json={
        "title": "Chief of staff", "parent_role_id": ceo_role["id"],
        "scope_level_id": lv["exec"]["id"]}).json()
    SOLO, solo_id = join("solo@acme.test", "Sol Ito", solo_role["id"])
    solo_seat = database.resolve_seat(ACCT, solo_id)
    check("fixture: company breadth, and genuinely no reports",
          solo_seat["breadth"] == "company"
          and solo_seat["subtree_user_ids"] == []
          and solo_seat["visible_user_ids"] is None)
    solo_team = snap(SOLO, days=7, whose="team").json()["scope"]
    check("company breadth + no reports + whose=team reports the filter that ran",
          solo_team["requested"] == "team"
          and solo_team["effective"] == "team"
          and solo_team["filtered"] is True
          and solo_team["people_in_scope"] == 1)
    check("a selector a control would not offer is flagged, not relabelled",
          solo_team["selector_offered"] is False
          and "team" not in solo_team["choices"])
    solo_nav = snap(SOLO, days=7, whose="team").json()["navigation"]
    check("navigation carries the actual selection, not a wider one",
          solo_nav["carry_query"]["whose"] == "team")

    solo_self = snap(SOLO, days=7, whose="person", person_id=solo_id).json()
    check("the same person asking for themselves is reported as person",
          solo_self["scope"]["effective"] == "person"
          and solo_self["scope"]["person_id"] == solo_id
          and solo_self["scope"]["people_in_scope"] == 1
          and solo_self["navigation"]["carry_query"]["person_id"] == solo_id)

    # Personal breadth asking for everyone: honored, and clamped to self.
    rep_all = snap(REP, days=7, whose="everyone").json()["scope"]
    check("personal breadth + whose=everyone stays personal and says so",
          rep_all["effective"] == "everyone"
          and rep_all["breadth"] == "self"
          and rep_all["filtered"] is True
          and rep_all["people_in_scope"] == 1)

    # Reporting branch: the manager's seat.
    mgr_scope = snap(MGR, days=7, whose="team").json()["scope"]
    check("reporting-branch scope reports subtree breadth and its membership",
          mgr_scope["effective"] == "team"
          and mgr_scope["breadth"] == "subtree"
          and mgr_scope["selector_offered"] is True
          and mgr_scope["people_in_scope"] == 2)

    # Custom scope level, composed from the same atoms under a name that is
    # not a preset.
    custom = c.post("/org/scope-levels", headers=auth(CEO), json={
        "name": "Pod lead", "breadth": "self", "depth": "glance",
        "surfaces": ["Home", "Work", "Fleet", "Ask", "Connect"]}).json()
    pod_role = c.post("/org/roles", headers=auth(CEO), json={
        "title": "Pod", "parent_role_id": ceo_role["id"],
        "scope_level_id": custom["id"]}).json()
    pod_child = c.post("/org/roles", headers=auth(CEO), json={
        "title": "Pod IC", "parent_role_id": pod_role["id"],
        "scope_level_id": lv["ic"]["id"]}).json()
    POD, pod_id = join("pod@acme.test", "Pia Novak", pod_role["id"])
    # Someone actually sits below them, so `team` asks for two people while
    # the self-breadth seat allows one — the clamp is real, not vacuous.
    join("podic@acme.test", "Paz Rivera", pod_child["id"])
    pod_scope = snap(POD, days=7, whose="everyone").json()["scope"]
    check("a custom level is described by its atoms, never by its name",
          pod_scope["breadth"] == "self"
          and pod_scope["scope_level_id"] == custom["id"]
          and pod_scope["people_in_scope"] == 1
          and "Pod lead" not in str(pod_scope))

    # A self-breadth seat that DOES have reports asking for team: the seat
    # clamps it, and the clamp is reported rather than hidden.
    pod_team = snap(POD, days=7, whose="team").json()["scope"]
    check("a selector the seat must clamp reports clamped_by_seat",
          pod_team["effective"] == "team"
          and pod_team["clamped_by_seat"] is True
          and pod_team["people_in_scope"] == 1)

    # Machine session.
    key = c.get("/org/api-keys", headers=auth(CEO)).json()
    api_key = (key.get("keys") or [{}])[0].get("key") or acme["api_key"]
    machine = c.get("/home/snapshot?days=7&whose=team",
                    headers={"X-Trovis-Api-Key": api_key}).json()
    check("a machine session has no seat, one choice, and no person filter",
          machine["scope"]["effective"] == "everyone"
          and machine["scope"]["choices"] == ["everyone"]
          and machine["scope"]["filtered"] is False
          and machine["scope"]["breadth"] is None
          and machine["scope"]["viewer_user_id"] is None)

    # Malformed selector: established behavior is to narrow nothing.
    junk = snap(MGR, days=7, whose="team-ish").json()
    check("an unreadable selector narrows nothing and is flagged as unreadable",
          junk["scope"]["effective"] == "everyone"
          and junk["scope"]["request_unreadable"] is True)
    check("navigation does not echo an unreadable selector back",
          junk["navigation"]["carry_query"]["whose"] == "everyone")
    good = snap(MGR, days=7, whose="team").json()
    check("a readable selector is not flagged unreadable",
          good["scope"]["request_unreadable"] is False)

print("\n" + ("FAILED: " + "; ".join(failures) if failures else "ALL PASS"))
raise SystemExit(1 if failures else 0)
