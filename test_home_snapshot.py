"""GET /home/snapshot — the authoritative Home snapshot.

What this file defends, in order of how badly it would hurt to get wrong:

  1. SCOPE. A request can narrow what a person sees and can never widen it,
     across company / reporting-branch / personal / custom seats, and across
     accounts. Personal attention stays personal whatever scope is selected.
  2. TRUTH. Counts are SQL aggregates over the whole permitted dataset, not a
     fold over the first page. Buckets reconcile with their total. Spans, tool
     calls, nested child activity and registrations are not completions.
     Missing history is distinguishable from zero.
  3. MONEY. Financial values appear only for a seat carrying the Cost surface,
     are labeled organization-wide, and never claim coverage they can't prove.
  4. LATENCY. No model is called on this path.

Run:
  OVERSEE_DISABLE_PRICING_SYNC=1 python3 test_home_snapshot.py
(isolated temp SQLite DB; never touches the dev/prod DB)
"""
import os
import tempfile
import time
from datetime import datetime, timedelta, timezone

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

import home_snapshot
import main
import asker
import describer
from fastapi.testclient import TestClient

main._auto_describe = lambda *a, **k: False

failures = []


def check(label, cond):
    print(("  PASS " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


# Any model call on this path is a bug, so make one impossible rather than
# merely unlikely: the SDK entry points blow up if anything reaches for them.
class _Exploding:
    def __init__(self, *a, **k):
        raise AssertionError("the snapshot path must never call a model")


describer.anthropic.Anthropic = _Exploding
asker.anthropic.Anthropic = _Exploding

NS = 10**9
NOW = time.time_ns()
_n = [0]


def kv(d):
    return [{"key": k, "value": {"stringValue": str(v)}} for k, v in d.items()]


def sp(name, off_s, attrs):
    _n[0] += 1
    t = NOW - int(off_s) * NS
    return {
        "traceId": f"{_n[0]:032d}", "spanId": f"{_n[0]:016d}", "name": name,
        "kind": 1, "startTimeUnixNano": str(t), "endTimeUnixNano": str(t + 10**6),
        "status": {"code": 1}, "attributes": kv(attrs),
    }


def utc_str(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def set_closed(title, dt):
    """Move a named work item's COMPLETION timestamp. Period-boundary and
    bucketing assertions need control of it; ingest stamps 'now'."""
    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute(
            f"UPDATE loops SET closed_at = {database.PH} WHERE title = {database.PH}",
            (utc_str(dt), title),
        )


def set_created(title, dt):
    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute(
            f"UPDATE loops SET created_at = {database.PH} WHERE title = {database.PH}",
            (utc_str(dt), title),
        )


def auth(tok):
    return {"Authorization": f"Bearer {tok}"}


with TestClient(main.app) as c:
    def post(key, svc, spans):
        return c.post(
            "/v1/traces",
            json={"resourceSpans": [{
                "resource": {"attributes": kv({"service.name": svc})},
                "scopeSpans": [{"spans": spans}]}]},
            headers={"X-Trovis-Api-Key": key},
        )

    def snap(tok, **q):
        qs = "&".join(f"{k}={v}" for k, v in q.items() if v is not None)
        return c.get(f"/home/snapshot{'?' + qs if qs else ''}", headers=auth(tok))

    # -----------------------------------------------------------------
    # Org: CEO (Exec, company) -> Lead (Manager, subtree) -> Ira (IC, self)
    #      plus Pia on a CUSTOM scope level composed from the same atoms.
    # -----------------------------------------------------------------
    acme = c.post("/auth/signup", json={
        "email": "ceo@acme.test", "password": "correct horse battery",
        "name": "Cleo", "account_type": "business", "org_name": "Acme",
    }).json()
    ACCT, CEO, KEY = acme["org"]["id"], acme["token"], acme["api_key"]
    ceo_id = acme["user"]["id"]

    lv = {l["key"]: l for l in c.get("/org/scope-levels", headers=auth(CEO)).json()}
    # A custom level: subtree breadth WITHOUT the financial surface. Named
    # something that is not a preset name on purpose — nothing in the snapshot
    # may branch on what a level is called.
    custom = c.post("/org/scope-levels", headers=auth(CEO), json={
        "name": "Pod lead", "breadth": "subtree", "depth": "glance",
        "surfaces": ["Home", "Work", "Fleet", "Ask", "Connect"],
    }).json()

    ceo_role = c.post("/org/roles", headers=auth(CEO), json={
        "title": "CEO", "scope_level_id": lv["exec"]["id"]}).json()
    lead_role = c.post("/org/roles", headers=auth(CEO), json={
        "title": "Lead", "parent_role_id": ceo_role["id"],
        "scope_level_id": lv["manager"]["id"]}).json()
    ic_role = c.post("/org/roles", headers=auth(CEO), json={
        "title": "IC", "parent_role_id": lead_role["id"],
        "scope_level_id": lv["ic"]["id"]}).json()
    pod_role = c.post("/org/roles", headers=auth(CEO), json={
        "title": "Pod", "parent_role_id": ceo_role["id"],
        "scope_level_id": custom["id"]}).json()
    pod_child = c.post("/org/roles", headers=auth(CEO), json={
        "title": "Pod IC", "parent_role_id": pod_role["id"],
        "scope_level_id": lv["ic"]["id"]}).json()
    c.post(f"/org/roles/{ceo_role['id']}/members", headers=auth(CEO),
           json={"user_id": ceo_id})

    def join(email, name, role_id):
        r = c.post("/org/invites", headers=auth(CEO),
                   json={"email": email, "name": name, "role_id": role_id}).json()
        tok = r["invite_url"].split("token=")[1]
        out = c.post("/auth/accept-invite", json={
            "token": tok, "name": name, "password": "correct horse battery"}).json()
        return out["token"], out["user"]["id"]

    LEAD, lead_id = join("lead@acme.test", "Lena Ortiz", lead_role["id"])
    IRA, ira_id = join("ira@acme.test", "Ira Chen", ic_role["id"])
    POD, pod_id = join("pia@acme.test", "Pia Novak", pod_role["id"])
    PODIC, podic_id = join("paz@acme.test", "Paz Rivera", pod_child["id"])

    # A second account, to prove isolation.
    other = c.post("/auth/signup", json={
        "email": "boss@other.test", "password": "correct horse battery",
        "name": "Bo", "account_type": "business", "org_name": "Other",
    }).json()
    OTHER, OTHER_KEY = other["token"], other["api_key"]
    other_id = other["user"]["id"]

    # -----------------------------------------------------------------
    print("\n--- empty workspace ---")
    # -----------------------------------------------------------------
    r = snap(OTHER, days=7, tz="UTC")
    check("empty workspace answers 200", r.status_code == 200)
    e = r.json()
    check("empty: top-level contract", set(e.keys()) == {
        "generated_at", "scope", "period", "current_state", "completions_series",
        "by_job", "attention", "financial", "freshness", "completeness",
        "navigation"})
    check("empty: workspace_state says empty, not 'zero work done'",
          e["completeness"]["workspace_state"] == "empty"
          and e["completeness"]["has_any_recorded_work"] is False)
    check("empty: period total is 0 and the series has one bucket per day",
          e["period"]["completed"] == 0 and len(e["completions_series"]["points"]) == 7)
    check("empty: comparison is UNAVAILABLE, not a 0-vs-0 flat line",
          e["period"]["comparison"]["available"] is False
          and e["period"]["comparison"]["previous_completed"] is None)
    check("empty: freshness is null everywhere, not epoch 0",
          all(v is None for v in e["freshness"].values()))
    check("empty: no raw traces or work records in the payload",
          "items" not in e and "spans" not in e and "loops" not in e)

    # -----------------------------------------------------------------
    print("\n--- malformed input ---")
    # -----------------------------------------------------------------
    check("unknown IANA timezone is a 400",
          snap(CEO, days=7, tz="Mars/Olympus").status_code == 400)
    check("days below the bound is refused", snap(CEO, days=0).status_code == 422)
    check("days above the bound is refused", snap(CEO, days=900).status_code == 422)
    check("non-numeric days is refused", snap(CEO, days="lots").status_code == 422)
    junk = snap(CEO, days=7, whose="everyone--or-not")
    check("an unreadable whose narrows to everyone rather than 400-ing",
          junk.status_code == 200 and junk.json()["scope"]["effective"] == "everyone")
    check("whose=person with no person_id is a 400",
          snap(CEO, days=7, whose="person").status_code == 400)

    # -----------------------------------------------------------------
    print("\n--- seed: named work, owners, jobs ---")
    # -----------------------------------------------------------------
    # Two declared jobs, so the breakdown has real identities to use.
    job_a = c.post("/workflows", headers=auth(CEO), json={
        "name": "Refund requests", "description": "d",
        "steps": [{"step_type": "agent", "label": "run"}]}).json()
    job_b = c.post("/workflows", headers=auth(CEO), json={
        "name": "Invoice reconcile", "description": "d",
        "steps": [{"step_type": "agent", "label": "run"}]}).json()

    def done_item(svc, key_, title, ext, off=3600, n_actions=0):
        """One named work item that finished. `n_actions` adds NESTED activity
        (tool calls) inside the same item — which must never count as extra
        completed work."""
        spans = [sp("message_received", off + 60, {
            "trovis.loop.title": title, "trovis.loop.external_id": ext})]
        for i in range(n_actions):
            spans.append(sp("tool_call", off + 50 - i, {
                "trovis.loop.external_id": ext, "trovis.tool.name": "exec"}))
        spans.append(sp("agent_run_complete", off, {
            "trovis.loop.external_id": ext, "trovis.loop.close": "done"}))
        post(key_, svc, spans)

    # Agents, one owned by each person, so ownership drives the seat filter.
    for svc in ("ceo-agent", "lead-agent", "ira-agent", "pod-agent", "podic-agent"):
        post(KEY, svc, [sp("message_received", 9000, {
            "trovis.loop.external_id": f"boot-{svc}"})])
    for svc, uid in (("ceo-agent", ceo_id), ("lead-agent", lead_id),
                     ("ira-agent", ira_id), ("pod-agent", pod_id),
                     ("podic-agent", podic_id)):
        c.put(f"/agents/{svc}/owner", headers=auth(CEO),
              json={"agent_id": "main", "user_id": uid})

    done_item("ceo-agent", KEY, "CEO closed one", "c1")
    done_item("lead-agent", KEY, "Lead closed one", "l1")
    done_item("lead-agent", KEY, "Lead closed two", "l2")
    done_item("ira-agent", KEY, "Ira closed one", "i1", n_actions=4)
    done_item("pod-agent", KEY, "Pod closed one", "p1")
    done_item("podic-agent", KEY, "Pod IC closed one", "p2")
    # Raw untitled OTel + a registration: neither is a work completion.
    post(KEY, "flood-agent", [
        sp("message_received", 500, {"trovis.loop.external_id": "raw1"}),
        sp("tool_call", 400, {"trovis.loop.external_id": "raw1",
                              "trovis.tool.name": "exec"}),
    ])
    post(KEY, "reg-agent", [sp("agent_registration", 450, {
        "trovis.agent.name": "Reggie", "agent.registration.files": "x"})])
    # Open work in three engine states, for current_state.
    post(KEY, "ceo-agent", [
        sp("message_received", 300, {"trovis.loop.title": "Still moving",
                                     "trovis.loop.external_id": "open1"}),
        sp("tool_call", 200, {"trovis.loop.external_id": "open1",
                              "trovis.tool.name": "search"})])
    post(KEY, "ceo-agent", [
        sp("message_received", 200000, {"trovis.loop.title": "Waiting on Cleo",
                                        "trovis.loop.external_id": "open2"}),
        sp("agent_run_complete", 190000, {
            "trovis.loop.external_id": "open2",
            "trovis.handoff.direction": "to_human",
            "trovis.handoff.target_id": "ceo@acme.test",
            "trovis.handoff.id": "HY"})])
    post(KEY, "ira-agent", [
        sp("message_received", 900, {"trovis.loop.title": "Stuck on Stripe",
                                     "trovis.loop.external_id": "open3"}),
        sp("agent_run_complete", 800, {
            "trovis.loop.external_id": "open3",
            "trovis.handoff.direction": "to_system",
            "trovis.handoff.target_id": "Stripe",
            "trovis.handoff.id": "HS"})])

    # Attach the completed items to declared jobs (the matcher's column).
    with database._connect() as conn, database._cursor(conn) as cur:
        for title, wid in (("CEO closed one", job_a["id"]),
                           ("Lead closed one", job_a["id"]),
                           ("Lead closed two", job_b["id"])):
            cur.execute(
                f"UPDATE loops SET workflow_id = {database.PH} "
                f"WHERE title = {database.PH}", (wid, title))

    # -----------------------------------------------------------------
    print("\n--- scope: company, reporting branch, personal, custom ---")
    # -----------------------------------------------------------------
    ceo_s = snap(CEO, days=7, tz="UTC").json()
    lead_s = snap(LEAD, days=7, tz="UTC").json()
    ira_s = snap(IRA, days=7, tz="UTC").json()
    pod_s = snap(POD, days=7, tz="UTC").json()

    check("company seat counts every recorded completion (6)",
          ceo_s["period"]["completed"] == 6)
    check("company seat is unfiltered (people_in_scope is null, not 0)",
          ceo_s["scope"]["filtered"] is False
          and ceo_s["scope"]["people_in_scope"] is None)
    check("reporting-branch seat sees its own branch only (Lead + Ira = 3)",
          lead_s["period"]["completed"] == 3)
    check("personal seat sees only its own work (1)",
          ira_s["period"]["completed"] == 1)
    check("custom scope level narrows by ATOMS, not by its name (Pod + Pod IC = 2)",
          pod_s["period"]["completed"] == 2 and pod_s["scope"]["breadth"] == "subtree"
          and pod_s["scope"]["scope_level_id"] == custom["id"])
    check("scope choices follow reports, not titles",
          set(ceo_s["scope"]["choices"]) == {"everyone", "me", "team", "person"}
          and set(ira_s["scope"]["choices"]) == {"everyone", "me"})

    # -----------------------------------------------------------------
    print("\n--- attempts to broaden, and cross-account ---")
    # -----------------------------------------------------------------
    ira_wide = snap(IRA, days=7, tz="UTC", whose="everyone").json()
    check("a personal seat asking for 'everyone' still gets only its own work",
          ira_wide["period"]["completed"] == 1)
    check("asking for a person outside your reporting line is a 403",
          snap(IRA, days=7, person_id=ceo_id, whose="person").status_code == 403)
    check("a lead may narrow to a report",
          snap(LEAD, days=7, whose="person", person_id=ira_id
               ).json()["period"]["completed"] == 1)
    check("a lead may NOT widen to their own manager",
          snap(LEAD, days=7, whose="person", person_id=ceo_id).status_code == 403)
    other_s = snap(OTHER, days=7, tz="UTC").json()
    check("another account sees none of Acme's work",
          other_s["period"]["completed"] == 0
          and other_s["current_state"]["open"] == 0)
    check("a cross-account person_id is refused, not silently emptied",
          snap(CEO, days=7, whose="person", person_id=other_id).status_code == 403)

    # -----------------------------------------------------------------
    print("\n--- personal attention stays personal ---")
    # -----------------------------------------------------------------
    a_all = snap(CEO, days=7, whose="everyone").json()["attention"]
    a_me = snap(CEO, days=7, whose="me").json()["attention"]
    a_team = snap(CEO, days=7, whose="team").json()["attention"]
    a_person = snap(CEO, days=7, whose="person", person_id=ira_id).json()["attention"]
    check("needs_you is the same number whatever scope is selected",
          a_all["needs_you"] == a_me["needs_you"] == a_team["needs_you"]
          == a_person["needs_you"] == 1)
    check("attention names the identity it answers to",
          a_person["scoped_to"] == "session_identity"
          and a_person["viewer_user_id"] == ceo_id)
    check("someone else's desk is their own",
          snap(IRA, days=7).json()["attention"]["needs_you"] == 0)

    # A machine session has no person: attention is unavailable, not zero.
    key_r = c.get("/home/snapshot?days=7", headers={"X-Trovis-Api-Key": KEY})
    check("API-key session answers 200 with account-wide work",
          key_r.status_code == 200 and key_r.json()["period"]["completed"] == 6)
    ks = key_r.json()
    check("API-key attention is unavailable, never a confident 0",
          ks["attention"]["available"] is False
          and ks["attention"]["needs_you"] is None
          and ks["attention"]["unavailable_reason"]
          == "no_personal_identity_for_machine_session")
    check("API-key session gets no money (no seat to grant the surface)",
          ks["financial"]["visible"] is False
          and ks["financial"]["unavailable_reason"] == "no_seat_for_machine_session")

    # -----------------------------------------------------------------
    print("\n--- truthful counts: current state vs the period ---")
    # -----------------------------------------------------------------
    cs = ceo_s["current_state"]
    check("current state splits open work by engine state and adds up",
          cs["open"] == cs["moving"] + cs["waiting_on_person"] + cs["blocked"]
          and cs["open"] == 3)
    check("current state is labeled 'now' and offers no invented trend",
          cs["as_of"] == "now" and cs["trend_available"] is False)
    check("'moving now' is not in the period object, and vice versa",
          "completed" not in cs and "moving" not in ceo_s["period"])

    check("nested tool calls inside one item do not add completions",
          snap(IRA, days=7).json()["period"]["completed"] == 1)
    check("untitled OTel + a registration are not completions",
          ceo_s["period"]["completed"] == 6)
    check("telemetry freshness is recorded without touching cost",
          ceo_s["freshness"]["latest_telemetry_at"] is not None)

    # -----------------------------------------------------------------
    print("\n--- more than 50 items: totals are not preview-derived ---")
    # -----------------------------------------------------------------
    bulk = []
    for i in range(60):
        ext = f"bulk{i}"
        bulk.append(sp("message_received", 2000 + i, {
            "trovis.loop.title": f"Bulk item {i}", "trovis.loop.external_id": ext}))
        bulk.append(sp("agent_run_complete", 1900 + i, {
            "trovis.loop.external_id": ext, "trovis.loop.close": "done"}))
    post(KEY, "ceo-agent", bulk)

    big = snap(CEO, days=7, tz="UTC").json()
    page = c.get("/work/items?limit=50", headers=auth(CEO)).json()
    check("60 more items land",
          big["period"]["completed"] == 66)
    check("the total exceeds one page of /work/items",
          len(page["items"]) == 50 and big["period"]["completed"] > 50)
    check("the series still reconciles above one page",
          big["completions_series"]["reconciles"] is True
          and big["completions_series"]["total"] == 66)
    check("the job breakdown reconciles above one page",
          big["by_job"]["reconciles"] is True)

    # -----------------------------------------------------------------
    print("\n--- job identity, and unclassified work ---")
    # -----------------------------------------------------------------
    bj = big["by_job"]
    by_id = {r["workflow_id"]: r for r in bj["rows"]}
    check("existing job identity is used verbatim",
          by_id.get(job_a["id"], {}).get("completed") == 2
          and by_id[job_a["id"]]["name"] == "Refund requests"
          and by_id.get(job_b["id"], {}).get("completed") == 1)
    check("unmatched work is reported as unclassified, not dropped",
          bj["unclassified_completed"] == 63)
    check("rows + other + unclassified equal the period total",
          sum(r["completed"] for r in bj["rows"]) + bj["other_completed"]
          + bj["unclassified_completed"] == big["period"]["completed"])
    check("no outcome/success classification is invented",
          all(set(r.keys()) == {"workflow_id", "name", "completed"}
              for r in bj["rows"]))

    # -----------------------------------------------------------------
    print("\n--- period boundaries ---")
    # -----------------------------------------------------------------
    # Chicago is UTC-5 in September, so 03:00 UTC is still "yesterday" locally.
    tz_name = "America/Chicago"
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(tz_name)
    today_local = datetime.now(tz).date()
    start_local = datetime(today_local.year, today_local.month, today_local.day,
                           tzinfo=tz) - timedelta(days=2)
    set_closed("Bulk item 0", start_local - timedelta(minutes=1))  # just outside
    set_closed("Bulk item 1", start_local + timedelta(minutes=1))  # just inside

    three = snap(CEO, days=3, tz=tz_name).json()
    seven = snap(CEO, days=7, tz=tz_name).json()
    check("a completion one minute before the local start is excluded",
          seven["period"]["completed"] - three["period"]["completed"] == 1)
    check("boundaries are returned explicitly in local time AND UTC",
          three["period"]["start"].endswith(("-05:00", "-06:00"))
          and three["period"]["start_utc"].endswith("+00:00")
          and three["period"]["timezone"] == tz_name)
    check("the previous window is equal-duration and does not overlap",
          three["period"]["comparison"]["previous_end_utc"]
          == three["period"]["start_utc"])
    check("buckets are local days and reconcile with the total",
          len(three["completions_series"]["points"]) == 3
          and three["completions_series"]["reconciles"] is True
          and three["completions_series"]["total"] == three["period"]["completed"])

    # -----------------------------------------------------------------
    print("\n--- daylight saving ---")
    # -----------------------------------------------------------------
    # Chile springs forward on the first Sunday of September: 2026-09-06 is a
    # 23-hour local day. It must still be exactly ONE bucket, and completions
    # inside it must still reconcile.
    dst_now = datetime(2026, 9, 8, 15, 0, tzinfo=timezone.utc)
    p = home_snapshot.resolve_period(5, "America/Santiago", now=dst_now)
    starts = p["bucket_starts_local"]
    gaps = [
        (starts[i + 1].astimezone(timezone.utc) - starts[i].astimezone(timezone.utc))
        for i in range(len(starts) - 1)
    ]
    check("a DST day is one bucket, 23 hours wide",
          len(starts) == 5 and timedelta(hours=23) in gaps
          and all(g in (timedelta(hours=23), timedelta(hours=24),
                        timedelta(hours=25)) for g in gaps))
    check("every bucket is a distinct local calendar date",
          len({s.date() for s in starts}) == 5)
    # Fold synthetic completions across the transition and check reconciliation.
    epochs = []
    for s in starts:
        epochs.append(s.astimezone(timezone.utc).timestamp() + 60)
        epochs.append(s.astimezone(timezone.utc).timestamp() + 3600)
    rows = {"completed_epochs": epochs}
    series = home_snapshot._completion_series(rows, p, len(epochs), True)
    check("completions across a DST boundary reconcile with the total",
          series["reconciles"] is True
          and [pt["completed"] for pt in series["points"]] == [2, 2, 2, 2, 2])

    # -----------------------------------------------------------------
    print("\n--- missing history vs an actual zero ---")
    # -----------------------------------------------------------------
    fresh = snap(OTHER, days=7, tz="UTC").json()
    check("a workspace with no history returns comparison UNAVAILABLE",
          fresh["period"]["comparison"]["available"] is False
          and fresh["period"]["comparison"]["unavailable_reason"]
          == "no_recorded_work_before_previous_period")
    # Give Other real, old history: one item created and completed long ago.
    done_item("other-agent", OTHER_KEY, "Ancient other work", "o1")
    old = datetime.now(timezone.utc) - timedelta(days=60)
    set_created("Ancient other work", old)
    set_closed("Ancient other work", old)
    aged = snap(OTHER, days=7, tz="UTC").json()
    check("with history behind it, a quiet week is a real zero",
          aged["period"]["comparison"]["available"] is True
          and aged["period"]["comparison"]["previous_completed"] == 0
          and aged["period"]["completed"] == 0
          and aged["period"]["comparison"]["delta"] == 0)
    check("completeness lists what could not be established",
          any(u["field"] == "period.comparison"
              for u in fresh["completeness"]["unavailable"])
          and not any(u["field"] == "period.comparison"
                      for u in aged["completeness"]["unavailable"]))
    check("no stuck/waiting history is manufactured",
          aged["current_state"]["trend_unavailable_reason"]
          == "current_state_is_not_retained_historically")

    # -----------------------------------------------------------------
    print("\n--- financial surface present vs absent ---")
    # -----------------------------------------------------------------
    # Priced and unpriced spend, inside the window.
    post(KEY, "ceo-agent", [sp("llm_call", 120, {
        "gen_ai.request.model": "claude-sonnet-4-5",
        "gen_ai.usage.input_tokens": 1000, "gen_ai.usage.output_tokens": 500})])
    post(KEY, "ceo-agent", [sp("llm_call", 110, {
        "gen_ai.request.model": "not-a-real-model-nobody-prices",
        "gen_ai.usage.input_tokens": 800, "gen_ai.usage.output_tokens": 200})])

    ceo_fin = snap(CEO, days=7, tz="UTC").json()["financial"]
    ira_fin = snap(IRA, days=7, tz="UTC").json()["financial"]
    pod_fin = snap(POD, days=7, tz="UTC").json()["financial"]
    check("a seat with the Cost surface sees money",
          ceo_fin["visible"] is True and ceo_fin["spend_usd"] > 0)
    check("a seat without the Cost surface sees none, with a reason",
          ira_fin["visible"] is False
          and ira_fin["spend_usd"] is None
          and ira_fin["unavailable_reason"] == "seat_excludes_financial_surface")
    check("a CUSTOM level without the Cost atom is refused money too",
          pod_fin["visible"] is False)
    check("money is labeled organization-wide and not attributed to the work shown",
          ceo_fin["scope"] == "organization_wide"
          and ceo_fin["attributable_to_shown_work"] is False
          and "organization" in ceo_fin["scope_note"])
    check("no cost-per-completion is offered anywhere",
          not any("per_completion" in k for k in ceo_fin)
          and "cost_per_completion" not in str(ceo_fin))

    # Org-wide cost alongside a narrower work scope: the money must not move.
    narrow = snap(CEO, days=7, tz="UTC", whose="person", person_id=ira_id).json()
    check("narrowing the work scope does not narrow (or re-scale) the spend",
          narrow["financial"]["spend_usd"] == ceo_fin["spend_usd"]
          and narrow["financial"]["scope"] == "organization_wide"
          and narrow["period"]["completed"] == 1)

    cov = ceo_fin["coverage"]
    check("unpriced usage is reported as unknown, not folded in as free",
          cov["unpriced_token_spans"] >= 1 and cov["priced_spans"] >= 1
          and cov["denominator"] == cov["priced_spans"] + cov["unpriced_token_spans"])
    check("coverage is a span share, and says so",
          0 < cov["ratio"] < 1 and cov["measure"] == "priced_cost_bearing_spans"
          and "Not a share of dollars" in cov["definition"])
    empty_cov = snap(OTHER, days=7, tz="UTC").json()["financial"]["coverage"]
    check("with no cost-bearing spans there is no coverage percentage",
          empty_cov["ratio"] is None
          and empty_cov["unavailable_reason"] == "no_cost_bearing_spans_in_period")
    check("connected-agent counts are not derived from financial access",
          "agents" not in ceo_fin and "agent_count" not in str(ceo_fin))

    # -----------------------------------------------------------------
    print("\n--- navigation identifiers ---")
    # -----------------------------------------------------------------
    nav = narrow["navigation"]
    check("navigation carries the scope and period forward",
          nav["carry_query"]["whose"] == "person"
          and nav["carry_query"]["person_id"] == ira_id
          and nav["carry_query"]["days"] == 7
          and nav["carry_query"]["tz"] == "UTC")
    check("navigation names the account and the drill-in path",
          nav["account_id"] == ACCT and nav["work_items_path"] == "/work/items")

    # -----------------------------------------------------------------
    print("\n--- no model call, and no board fetch ---")
    # -----------------------------------------------------------------
    calls = []
    real_board, real_summary = database.get_work_board, main.work_summary
    database.get_work_board = lambda *a, **k: calls.append("board")
    main.work_summary = lambda *a, **k: calls.append("summary")
    boom = snap(CEO, days=30, tz="UTC")
    database.get_work_board, main.work_summary = real_board, real_summary
    check("the snapshot answers without touching a model (SDK is booby-trapped)",
          boom.status_code == 200)
    check("the snapshot does not reach for the fat board or summary", calls == [])

print("\n" + ("FAILED: " + "; ".join(failures) if failures else "ALL PASS"))
raise SystemExit(1 if failures else 0)
