"""Every run belongs to a job.

When no declared job claims a run, ingest files it under a job DERIVED from
its agent's service.name — named after the agent, flagged `derived`, with one
recognition rule (service_name equals) and no expectation. A person promotes
it by posting a version with a name and a description; the matcher prefers a
declaration over the derived default at equal specificity; closed runs keep
the job they had when they closed.
"""
import os, tempfile, time

os.environ.update({"OVERSEE_DISABLE_PRICING_SYNC": "1", "TROVIS_DISABLE_PRICING_SYNC": "1",
                   "TROVIS_DISABLE_ALERTS": "1", "TROVIS_DISABLE_LOOP_SWEEP": "1",
                   "TROVIS_LOOP_TITLES": "off"})
os.environ.pop("DATABASE_URL", None)
os.environ.pop("ANTHROPIC_API_KEY", None)
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False); _tmp.close()
import database; database.SQLITE_PATH = _tmp.name
import loops as loops_mod, main
from fastapi.testclient import TestClient
main._auto_describe = lambda *a, **k: False

failures = []
def check(label, cond):
    print(("  PASS " if cond else "  FAIL ") + label)
    if not cond: failures.append(label)

def kv(d): return [{"key": k, "value": {"stringValue": str(v)}} for k, v in d.items()]
_n = [0]
NS = 10**9
NOW = time.time_ns()
def sp(name, off, attrs):
    _n[0] += 1
    return {"traceId": f"{_n[0]:032d}", "spanId": f"{_n[0]:016d}", "name": name, "kind": 1,
            "startTimeUnixNano": str(NOW - off * NS), "endTimeUnixNano": str(NOW - off * NS + 10**6),
            "status": {"code": 1}, "attributes": kv(attrs)}

with TestClient(main.app) as c:
    r = c.post("/auth/signup", json={"email": "a@t.com", "password": "supersecret123",
        "name": "Alex", "account_type": "business", "org_name": "A"}).json()
    K, T = r["api_key"], r["token"]; H = {"Authorization": f"Bearer {T}"}
    r2 = c.post("/auth/signup", json={"email": "b@other.com", "password": "supersecret123",
        "name": "Bea", "account_type": "business", "org_name": "B"}).json()
    K2, T2 = r2["api_key"], r2["token"]; H2 = {"Authorization": f"Bearer {T2}"}

    def post(svc, spans, key=K):
        rr = c.post("/v1/traces", json={"resourceSpans": [{
            "resource": {"attributes": kv({"service.name": svc})},
            "scopeSpans": [{"spans": spans}]}]}, headers={"X-Trovis-Api-Key": key})
        assert rr.status_code < 300, rr.text
    def jobs(h=H, **q):
        qs = "&".join(f"{k}={v}" for k, v in q.items())
        return c.get(f"/workflows{'?' + qs if qs else ''}", headers=h).json()
    def items(h=H, **q):
        qs = "&".join(f"{k}={v}" for k, v in q.items())
        return c.get(f"/work/items?limit=100{'&' + qs if qs else ''}", headers=h).json()["items"]
    def t(x, ext): return {"trovis.loop.title": x, "trovis.loop.external_id": ext}

    print("\n--- a run with no declared job is filed, not left unmatched ---")
    post("refunds-agent", [sp("message_received", 600, t("Refund #1", "r1"))])
    post("refunds-agent", [sp("message_received", 500, t("Refund #2", "r2"))])
    post("cs-agent", [sp("message_received", 400, t("Reply to Acme", "c1"))])
    js = jobs()
    derived = {j["derived_from"]: j for j in js if j["derived"]}
    check("one derived job per agent, named after the agent",
          set(derived) == {"refunds-agent", "cs-agent"}
          and derived["refunds-agent"]["name"] == "refunds-agent")
    check("a derived job is observed, not graded: no expectation, no definition",
          all(j["has_expectation"] is False and j["definition"] is None for j in derived.values()))
    check("its one recognition rule is the agent's service.name",
          c.get(f"/workflows/{derived['refunds-agent']['id']}", headers=H).json()["match_hints"]
          == [{"field": "service_name", "op": "equals", "value": "refunds-agent"}])
    rows = items()
    check("every run carries a job — the undeclared slice is empty",
          all(i["workflow_id"] is not None for i in rows) and len(items(workflow_id="none")) == 0)
    check("the rows name the derived job so the board can group them",
          {i["workflow_name"] for i in rows if i["title"].startswith("Refund")} == {"refunds-agent"})
    check("a second batch from the same agent reuses the job (one per account+service)",
          derived["refunds-agent"]["loop_counts"].get("working") == 2
          and len([j for j in js if j["derived_from"] == "refunds-agent"]) == 1)

    print("\n--- derived jobs are account-scoped ---")
    post("refunds-agent", [sp("message_received", 300, t("Refund #9", "x1"))], key=K2)
    other = [j for j in jobs(H2) if j["derived"]]
    check("the other account gets its own derived job for the same service name",
          len(other) == 1 and other[0]["derived_from"] == "refunds-agent"
          and other[0]["id"] != derived["refunds-agent"]["id"])
    check("...and never sees this account's", len(jobs(H2)) == 1)

    print("\n--- a declaration beats the derived default ---")
    r = c.post("/workflows", headers=H, json={
        "name": "Refund requests",
        "match_hints": [{"field": "service_name", "op": "equals", "value": "refunds-agent"}],
    })
    declared_id = r.json()["id"]
    post("refunds-agent", [sp("message_received", 200, t("Refund #3", "r3"))])
    r3 = [i for i in items() if i["title"] == "Refund #3"][0]
    check("a new run from that agent lands on the declared job, same hint count and all",
          r3["workflow_id"] == declared_id)
    summary = loops_mod.run_sweep_for_account(c.get("/auth/me", headers=H).json()["org"]["id"])
    r1 = [i for i in items() if i["title"] == "Refund #1"][0]
    check("the sweep moves the open runs off the derived job onto the declaration",
          r1["workflow_id"] == declared_id and summary["rematched"] >= 2)

    print("\n--- promotion: a person describes the derived job ---")
    cs = derived["cs-agent"]
    r = c.post(f"/workflows/{cs['id']}/versions", headers=H, json={
        "name": "Customer replies",
        "match_hints": [{"field": "service_name", "op": "equals", "value": "cs-agent"}],
        "stations": [{"holder_type": "agent", "holder": "cs-agent"}],
        "expectation": {"definition": "Reply to inbound support email.", "expected_per_day_min": 10},
        "note": "Described by Alex",
    })
    check("a version on a derived job promotes it: named, declared, described",
          r.status_code == 200 and r.json()["name"] == "Customer replies"
          and r.json()["derived"] is False and r.json()["derived_from"] is None
          and r.json()["definition"] == "Reply to inbound support email."
          and r.json()["current_version"] == 2)
    check("the promoted job kept its runs", [i for i in items() if i["title"] == "Reply to Acme"][0]["workflow_id"] == cs["id"])
    r = c.post(f"/workflows/{declared_id}/versions", headers=H, json={
        "name": "Something else",
        "match_hints": [{"field": "service_name", "op": "equals", "value": "refunds-agent"}],
    })
    check("a declared job cannot be renamed through a version", r.status_code == 400)
    post("cs-agent", [sp("message_received", 100, t("Reply to Globex", "c2"))])
    check("no second derived job appears for an agent whose job was promoted",
          [j for j in jobs() if j["derived"] and j["derived_from"] == "cs-agent"] == []
          and [i for i in items() if i["title"] == "Reply to Globex"][0]["workflow_id"] == cs["id"])

    print("\n--- archiving a derived job is respected ---")
    post("ops-bot", [sp("message_received", 90, t("Rotate keys", "o1"))])
    ops = [j for j in jobs() if j["derived_from"] == "ops-bot"][0]
    assert c.post(f"/workflows/{ops['id']}/archive", headers=H).status_code == 200
    post("ops-bot", [sp("message_received", 80, t("Rotate keys again", "o2"))])
    o2 = [i for i in items() if i["title"] == "Rotate keys again"][0]
    check("after a person archives the derived job, that agent's new runs stay unfiled rather than re-creating it",
          o2["workflow_id"] is None and len([j for j in jobs(include_archived=1) if j["derived_from"] == "ops-bot"]) == 1)

    print("\n--- only NAMED work earns a job ---")
    # An agent that emits untitled traces is an agent, not a job: the same
    # line Work draws everywhere (title_source = provided). No derived job,
    # the run stays unmatched, and the agent is absent from the job list.
    post("quiet-scraper", [sp("http.request", 50, {"trovis.run.id": "q1"})])
    post("quiet-scraper", [sp("http.request", 40, {"trovis.run.id": "q2"})])
    check("untitled runs derive no job",
          not any(j["derived_from"] == "quiet-scraper" for j in jobs(include_archived=1)))
    # A single named run from that same agent is named work, and gets one.
    post("quiet-scraper", [sp("message_received", 30, t("Scrape the vendor catalog", "q3"))])
    q = [j for j in jobs() if j["derived_from"] == "quiet-scraper"]
    q3 = [i for i in items() if i["title"] == "Scrape the vendor catalog"]
    check("one named run from the same agent derives its job, and the run lands on it",
          len(q) == 1 and len(q3) == 1 and q3[0]["workflow_id"] == q[0]["id"])

    print("\n--- the boot sweep retires derived jobs holding no named run ---")
    # Simulate the pre-gate world: a derived job with only untitled runs.
    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute(
            f"INSERT INTO workflows (account_id, name, created_by, current_version, derived_from) "
            f"VALUES ({database.PH}, 'flood-bot', 'trovis', 1, 'flood-bot')",
            (c.get("/auth/me", headers=H).json()["org"]["id"],),
        )
        cur.execute("SELECT id FROM workflows WHERE derived_from = 'flood-bot'")
        flood_id = int(cur.fetchone()["id"])
        cur.execute(
            f"INSERT INTO workflow_versions (workflow_id, version, stations, match_hints, created_by) "
            f"VALUES ({database.PH}, 1, '[]', '[]', 'trovis')", (flood_id,))
        n = database._archive_unnamed_derived_jobs(cur)
    swept = [j for j in jobs(include_archived=1) if j["derived_from"] == "flood-bot"]
    kept = [j for j in jobs() if j["derived_from"] == "quiet-scraper"]
    check("a derived job with no named run is archived by the sweep; one with a named run is kept",
          n >= 1 and len(swept) == 1 and swept[0]["archived_at"]
          and len(kept) == 1 and kept[0]["archived_at"] is None)
    with database._connect() as conn, database._cursor(conn) as cur:
        again = database._archive_unnamed_derived_jobs(cur)
    check("the sweep is idempotent — a second pass archives nothing", again == 0)

    print("\n--- closed runs are frozen ---")
    post("billing-agent", [sp("message_received", 70, t("Invoice 1", "b1")),
        sp("agent_run_complete", 60, {"trovis.loop.external_id": "b1", "trovis.loop.close": "done"})])
    billing = [j for j in jobs() if j["derived_from"] == "billing-agent"][0]
    b1 = [i for i in items() if i["title"] == "Invoice 1"][0]
    check("a run that closes in its first batch still records the derived job it had",
          b1["status"] == "done" and b1["workflow_id"] == billing["id"])

print()
if failures:
    print(f"FAILED: {len(failures)} check(s):")
    for f in failures: print("  -", f)
    raise SystemExit(1)
print("All derived-job checks passed.")
