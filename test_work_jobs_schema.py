"""A job declares an expectation; a run keeps the job it belongs to.

Two changes with one purpose: make the job a thing you can be RIGHT or WRONG
about. Until now a workflow declared how to recognise its runs and nothing
about what "working" means, so a job page could only ever report observed
numbers with nothing to compare them against — and a health verdict with no
declared number behind it is an adjective.

The other half is that a run must stay attached to its job. Matching was a
string-match cache that overwrote itself with NULL whenever a run stopped
matching, so editing a job's hints silently emptied its board.

The rule every assertion here defends: NULL means NOT DECLARED. Never zero,
never a default baseline, never a verdict nobody asked for.

Run:
  OVERSEE_DISABLE_PRICING_SYNC=1 python3 test_work_jobs_schema.py
"""
import os, tempfile, time

os.environ.update({"OVERSEE_DISABLE_PRICING_SYNC": "1", "TROVIS_DISABLE_ALERTS": "1",
                   "TROVIS_DISABLE_LOOP_SWEEP": "1", "TROVIS_LOOP_TITLES": "off"})
os.environ.pop("DATABASE_URL", None)
os.environ.pop("ANTHROPIC_API_KEY", None)
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False); _tmp.close()
import database; database.SQLITE_PATH = _tmp.name
import main
from fastapi.testclient import TestClient
main._auto_describe = lambda *a, **k: False

failures = []
def check(label, cond):
    print(("  PASS " if cond else "  FAIL ") + label)
    if not cond: failures.append(label)

def kv(d): return [{"key": k, "value": {"stringValue": str(v)}} for k, v in d.items()]
NS = 10**9
NOW = time.time_ns()
_n = [0]

def sp(name, off, attrs):
    _n[0] += 1
    st = NOW - off * NS
    return {"traceId": f"{_n[0]:032d}", "spanId": f"{_n[0]:016d}", "name": name,
            "kind": 1, "startTimeUnixNano": str(st), "endTimeUnixNano": str(st + 10**6),
            "status": {"code": 1}, "attributes": kv(attrs)}

HINT = [{"field": "service_name", "op": "equals", "value": "refunds-agent"}]

with TestClient(main.app) as c:
    r = c.post("/auth/signup", json={"email": "j@t.com", "password": "supersecret123",
        "name": "Alex", "account_type": "business", "org_name": "Co"}).json()
    K, T = r["api_key"], r["token"]; H = {"Authorization": f"Bearer {T}"}

    def post(svc, spans):
        return c.post("/v1/traces", json={"resourceSpans": [{
            "resource": {"attributes": kv({"service.name": svc})},
            "scopeSpans": [{"spans": spans}]}]}, headers={"X-Trovis-Api-Key": K})

    print("--- a job with no expectation claims nothing ---")
    bare = c.post("/workflows", headers=H, json={"name": "Undeclared"}).json()
    check("has_expectation is False", bare["has_expectation"] is False)
    check("every ceiling reads None, not 0 — 'not declared' is not 'zero'",
          all(bare[k] is None for k in (
              "expected_per_day_min", "expected_per_day_max", "expected_close_s",
              "expected_intervention_pct", "expected_failure_pct", "definition")))
    only_words = c.post("/workflows", headers=H,
                        json={"name": "Words only",
                              "expectation": {"definition": "Two sentences here."}}).json()
    check("a description alone is NOT an expectation — no verdict is earned by prose",
          only_words["definition"] == "Two sentences here."
          and only_words["has_expectation"] is False)
    check("an empty string is not a declaration either",
          c.post("/workflows", headers=H, json={
              "name": "Blank", "expectation": {"definition": "   "},
          }).json()["definition"] is None)

    print("\n--- a declared expectation round-trips whole ---")
    wf = c.post("/workflows", headers=H, json={
        "name": "Refunds", "match_hints": HINT,
        "expectation": {
            "definition": "Handles refund requests end to end.",
            "expected_per_day_min": 8, "expected_per_day_max": 12,
            "expected_close_s": 360, "expected_intervention_pct": 10.0,
            "expected_failure_pct": 2.0, "owning_service_name": "refunds-agent",
            "owning_agent_id": "main", "approval_routing": "j@t.com",
            "stall_threshold_s": 7200,
        }}).json()
    check("has_expectation is True", wf["has_expectation"] is True)
    for k, v in (("expected_per_day_min", 8), ("expected_close_s", 360),
                 ("expected_intervention_pct", 10.0), ("owning_service_name", "refunds-agent"),
                 ("approval_routing", "j@t.com"), ("stall_threshold_s", 7200)):
        check(f"{k} == {v!r}", wf[k] == v)
    check("the list endpoint carries it too, so a board row needs no second fetch",
          next(w for w in c.get("/workflows", headers=H).json()
               if w["id"] == wf["id"])["expected_per_day_min"] == 8)

    print("\n--- an expectation belongs to a VERSION ---")
    v2 = c.post(f"/workflows/{wf['id']}/versions", headers=H, json={
        "match_hints": HINT, "note": "loosened",
        "expectation": {"definition": "v2 wording", "expected_per_day_min": 5}}).json()
    check("the new version's values are live", v2["expected_per_day_min"] == 5
          and v2["definition"] == "v2 wording")
    check("...and an omitted field CLEARS rather than inherits — a version is a "
          "full definition, same as stations and hints",
          v2["expected_close_s"] is None)
    check("history still shows both versions", len(v2["versions"]) == 2)

    print("\n--- the record's own numbers ---")
    # A: agent only, 20s. B: crossed a PERSON, 120s. C: handed to a SYSTEM, 40s.
    post("refunds-agent", [
        sp("message_received", 900, {"trovis.loop.title": "Refund A",
                                     "trovis.loop.external_id": "a1",
                                     "trovis.run.cost_usd": "0.02"}),
        sp("agent_run_complete", 880, {"trovis.loop.external_id": "a1",
                                       "trovis.loop.close": "done"})])
    post("refunds-agent", [
        sp("message_received", 800, {"trovis.loop.title": "Refund B",
                                     "trovis.loop.external_id": "a2",
                                     "trovis.run.cost_usd": "0.04"}),
        sp("agent_run_complete", 790, {"trovis.loop.external_id": "a2",
                                       "trovis.handoff.direction": "to_human",
                                       "trovis.handoff.target_id": "j@t.com",
                                       "trovis.handoff.id": "H1"}),
        sp("agent_run_complete", 680, {"trovis.loop.external_id": "a2",
                                       "trovis.loop.close": "done"})])
    post("refunds-agent", [
        sp("message_received", 600, {"trovis.loop.title": "Refund C",
                                     "trovis.loop.external_id": "a3"}),
        sp("agent_run_complete", 590, {"trovis.loop.external_id": "a3",
                                       "trovis.handoff.direction": "to_system",
                                       "trovis.handoff.target_id": "stripe",
                                       "trovis.handoff.id": "H2"}),
        sp("agent_run_complete", 560, {"trovis.loop.external_id": "a3",
                                       "trovis.loop.close": "done"})])
    post("refunds-agent", [sp("message_received", 100, {"trovis.loop.title": "Refund D",
                                                        "trovis.loop.external_id": "a4"})])
    time.sleep(1.2)
    d = c.get(f"/workflows/{wf['id']}", headers=H).json()

    check("closed runs counted, the open one excluded", d["closed_runs"] == 3)
    check("INTERVENTION is the run whose chain crossed a PERSON",
          d["intervention_runs"] == 1)
    check("...and an agent-to-agent handoff is NOT intervention — that is the "
          "whole distinction the metric exists for",
          d["intervention_pct"] == 33.3)
    check("close time is measured on the AGENT's clock, not the write clock "
          f"(median of 20/40/120 = 40, got {d['median_close_s']})",
          d["median_close_s"] == 40)
    check("cost is real and per-run", d["cost_usd"] == 0.06 and d["cost_per_run"] == 0.015)
    check("the cost denominator is published, because it is NOT the rate "
          "denominator — 4 runs cost, 3 runs closed",
          d["cost_runs"] == 4 and d["closed_runs"] == 3)
    check("CADENCE counts runs started, not runs finished — grading a declared "
          "per-day expectation against closed runs answers a different "
          f"question (4 started, 3 closed, got {d['started_runs']})",
          d["started_runs"] == 4 and d["started_runs"] != d["closed_runs"])
    check("the window is stated, so a computed number can say what it came from",
          d["window_days"] == database.WORKFLOW_WINDOW_DAYS)

    print("\n--- a rate with nothing to divide is None, not 0% ---")
    quiet = c.post("/workflows", headers=H, json={
        "name": "Never ran", "match_hints": [
            {"field": "service_name", "op": "equals", "value": "nobody"}]}).json()
    check("no closed runs means intervention is unknown, not zero",
          quiet["intervention_pct"] is None and quiet["failure_pct"] is None)
    check("...and cost per run is unknown, not $0.00",
          quiet["cost_per_run"] is None)
    # The harder case: a job that HAS runs, none of them closed yet. It has
    # real observed data, so it is not the empty default — the rate itself
    # has to refuse to divide.
    open_only = c.post("/workflows", headers=H, json={
        "name": "Open only", "match_hints": [
            {"field": "service_name", "op": "equals", "value": "legal-agent"}]}).json()
    post("legal-agent", [sp("message_received", 200, {"trovis.loop.title": "Review NDA",
                                                      "trovis.loop.external_id": "L1",
                                                      "trovis.run.cost_usd": "0.01"})])
    time.sleep(1.0)
    oo = c.get(f"/workflows/{open_only['id']}", headers=H).json()
    # Runs that produced spans but no PRICED spans.
    unpriced = c.post("/workflows", headers=H, json={
        "name": "Unpriced", "match_hints": [
            {"field": "service_name", "op": "equals", "value": "finance-agent"}]}).json()
    post("finance-agent", [sp("message_received", 250, {"trovis.loop.title": "Book entry",
                                                        "trovis.loop.external_id": "F1"})])
    time.sleep(1.0)
    up = c.get(f"/workflows/{unpriced['id']}", headers=H).json()
    check("runs whose spans carried no cost report NO cost per run, not $0.00 — "
          "'we were not told' is not 'it was free'",
          up["cost_runs"] == 1 and up["cost_usd"] == 0.0 and up["cost_per_run"] is None)

    check("a job with runs but none closed has real cost and NO rates",
          oo["cost_runs"] == 1 and oo["cost_usd"] > 0
          and oo["closed_runs"] == 0
          and oo["intervention_pct"] is None and oo["failure_pct"] is None)
    check("...but it DOES have a cadence — a job mid-flight is running, and "
          "reporting 0/day against a declared floor would call it stalled",
          oo["started_runs"] == 1)
    check("a job that never ran has no cadence to report", quiet["started_runs"] == 0)

    print("\n--- sticky matching: a run keeps the job it belongs to ---")
    before = {i["title"] for i in c.get(f"/workflows/{wf['id']}/loops", headers=H).json()}
    check("the runs are matched to the job", len(before) == 4)
    # Narrow the hints so nothing matches any more, then push new telemetry
    # (which is what triggers a re-match).
    c.post(f"/workflows/{wf['id']}/versions", headers=H, json={
        "match_hints": [{"field": "service_name", "op": "equals", "value": "nothing-like-this"}],
        "note": "narrowed"})
    post("refunds-agent", [sp("message_received", 50, {"trovis.loop.external_id": "a4"})])
    time.sleep(1.0)
    after = {i["title"] for i in c.get(f"/workflows/{wf['id']}/loops", headers=H).json()}
    check("editing the hints does NOT empty the job's board", after == before)

    print("\n--- ...but a BETTER match still wins ---")
    other = c.post("/workflows", headers=H, json={
        "name": "Everything refunds", "match_hints": [
            {"field": "service_name", "op": "equals", "value": "refunds-agent"},
            {"field": "title", "op": "contains", "value": "Refund D"}]}).json()
    post("refunds-agent", [sp("message_received", 40, {"trovis.loop.external_id": "a4"})])
    time.sleep(1.0)
    moved = {i["title"] for i in c.get(f"/workflows/{other['id']}/loops", headers=H).json()}
    check("a run re-matches when a real match appears — sticky is not frozen",
          "Refund D" in moved)

    print("\n--- the escape hatch ---")
    # Only an OPEN run can be stale: a closed run's job is frozen and its
    # link is permanent by design, not drift. So the case needs an open run
    # whose job stopped matching and which nothing else claims.
    solo = c.post("/workflows", headers=H, json={
        "name": "Onboarding", "match_hints": [
            {"field": "service_name", "op": "equals", "value": "sales-agent"}]}).json()
    post("sales-agent", [sp("message_received", 300, {"trovis.loop.title": "Onboard Acme",
                                                      "trovis.loop.external_id": "s1"})])
    time.sleep(1.0)
    check("the open run matched its job",
          any(i["title"] == "Onboard Acme"
              for i in c.get(f"/workflows/{solo['id']}/loops", headers=H).json()))
    c.post(f"/workflows/{solo['id']}/versions", headers=H, json={
        "match_hints": [{"field": "service_name", "op": "equals", "value": "gone"}],
        "note": "narrowed"})
    post("sales-agent", [sp("message_received", 20, {"trovis.loop.external_id": "s1"})])
    time.sleep(1.0)
    check("it KEPT the job rather than being detached",
          any(i["title"] == "Onboard Acme"
              for i in c.get(f"/workflows/{solo['id']}/loops", headers=H).json()))

    health = c.get("/workflows/match-health", headers=H).json()
    check("stale links are reported, not hidden", health["count"] >= 1)
    check("...each naming why it no longer matches",
          all(l["reason"] in ("hints no longer match", "workflow archived")
              for l in health["links"]))
    stale = next(l for l in health["links"] if l["title"] == "Onboard Acme")
    check("an open run can be explicitly unlinked",
          c.post(f"/loops/{stale['loop_id']}/unlink-workflow", headers=H).status_code == 204)
    check("...and it really detached",
          c.get(f"/loops/{stale['loop_id']}", headers=H).json()["workflow_id"] is None)

    print("\n--- a CLOSED run's job is frozen ---")
    closed_id = next(i["id"] for i in c.get("/loops", headers=H).json()
                     if i["cached_state"] == "done" and i["workflow_id"])
    check("unlinking a closed run is refused — it permanently records what it "
          "matched when it closed",
          c.post(f"/loops/{closed_id}/unlink-workflow", headers=H).status_code == 404)

    print("\n--- an archived job keeps its history, marked ---")
    c.post(f"/workflows/{wf['id']}/archive", headers=H)
    row = next(i for i in c.get("/loops", headers=H).json() if i["workflow_id"] == wf["id"])
    check("the run still names the job it ran under", row["workflow_name"] == "Refunds")
    check("...and says the job is archived, so no surface has to pretend it is current",
          row.get("workflow_archived_at") is not None)

    print("\n--- spend job cost structurally cannot see ---")
    check("unattributed spans are countable, so the gap can be stated rather "
          "than rounded into cost-per-run",
          isinstance(database.unattributed_span_count(None), int))

print()
if failures:
    print(f"FAILED ({len(failures)}):")
    for f in failures: print("  - " + f)
    raise SystemExit(1)
print("All job-schema checks passed.")
os.unlink(_tmp.name)
