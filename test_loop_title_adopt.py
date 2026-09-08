"""Ingest adopt-title: untitled open loops take a later human title.

Lead-SWE follow-on to Connect emit (#138). Title is no longer INSERT-only:
when an open loop is NULL/empty and a later creating/related span carries
`trovis.loop.title` (legacy `oversee.loop.title`), ingest adopts it as
`title_source=provided`. Shells ("Task from …", `{agent} · {tool} · N
actions`) are rejected. Existing provided titles are never overwritten.
Lean named-work (`_NAMED_TITLE_SQL`) stays `title_source=provided` only.

Run:
  TROVIS_DISABLE_PRICING_SYNC=1 python3 test_loop_title_adopt.py
"""
import os
import tempfile
import time

os.environ["OVERSEE_DISABLE_PRICING_SYNC"] = "1"
os.environ["TROVIS_DISABLE_PRICING_SYNC"] = "1"
os.environ["TROVIS_DISABLE_ALERTS"] = "1"
os.environ["TROVIS_DISABLE_LOOP_SWEEP"] = "1"
os.environ["TROVIS_LOOP_TITLES"] = "off"
os.environ.pop("DATABASE_URL", None)
os.environ.pop("ANTHROPIC_API_KEY", None)

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()

import database
database.SQLITE_PATH = _tmp.name

import main
from fastapi.testclient import TestClient

main._auto_describe = lambda *a, **k: False

failures = []


def check(label, cond, detail=""):
    print(("  PASS " if cond else "  FAIL ") + label)
    if detail:
        print(f"        {detail}")
    if not cond:
        failures.append(label)


def kv(d):
    return [{"key": k, "value": {"stringValue": str(v)}} for k, v in d.items()]


_n = [0]


def span(name, off, attrs):
    _n[0] += 1
    return {
        "traceId": f"{_n[0]:032d}",
        "spanId": f"{_n[0]:016d}",
        "name": name,
        "kind": 1,
        "startTimeUnixNano": str(NOW - off * NS),
        "endTimeUnixNano": str(NOW - off * NS + 10**6),
        "status": {"code": 1},
        "attributes": kv(attrs),
    }


NS = 10**9
NOW = time.time_ns()


def row_for(external_id):
    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute(
            "SELECT id, title, title_source, closed_at FROM loops "
            "WHERE external_id = ?",
            (external_id,),
        )
        got = cur.fetchone()
        return dict(got) if got else None


with TestClient(main.app) as c:
    r = c.post("/auth/signup", json={
        "email": "adopt@test.com", "password": "supersecret123",
        "name": "Adopt Tester", "account_type": "individual",
        "org_name": "Adopt Co",
    })
    assert r.status_code == 201, r.text
    key = r.json()["api_key"]
    tok = c.post("/auth/login", json={
        "email": "adopt@test.com", "password": "supersecret123",
    }).json()["token"]
    H_key = {"X-Trovis-Api-Key": key}
    H_user = {"Authorization": f"Bearer {tok}"}

    def post(svc, spans):
        return c.post("/v1/traces", json={"resourceSpans": [{
            "resource": {"attributes": kv({"service.name": svc})},
            "scopeSpans": [{"spans": spans}],
        }]}, headers=H_key)

    def overview():
        return c.get("/work/overview", headers=H_user).json()

    def items():
        return c.get("/work/items", headers=H_user).json()

    print("\n--- (a) NULL→provided adopt ---")
    post("billing", [span("start", 400, {"trovis.loop.external_id": "a1"})])
    a1 = row_for("a1")
    check("creating span without title leaves the loop untitled",
          a1 is not None and not (a1.get("title") or "").strip()
          and a1.get("title_source") is None, f"row={a1}")
    ov0 = overview()
    check("untitled loop is not named work",
          ov0["open"] == 0 and items()["items"] == [])

    post("billing", [span("named", 300, {
        "trovis.loop.external_id": "a1",
        "trovis.loop.title": "Reconcile vendor 19",
    })])
    a1 = row_for("a1")
    check("later title is adopted onto the same open loop",
          a1["title"] == "Reconcile vendor 19"
          and a1["title_source"] == "provided", f"row={a1}")
    check("still one loop (adopt, not a new insert)",
          len([l for l in database.get_loops(
              database.validate_api_key(key)["account_id"], limit=50)
               if l.get("external_id") == "a1"]) == 1)

    print("\n--- same-batch adopt (cache hit) + run.id key ---")
    post("runner", [
        span("start", 280, {"trovis.run.id": "run-a"}),
        span("named", 270, {
            "trovis.run.id": "run-a",
            "trovis.loop.title": "Ship the Friday digest",
        }),
    ])
    run_a = row_for("run-a")
    check("same-batch later span adopts via trovis.run.id",
          run_a["title"] == "Ship the Friday digest"
          and run_a["title_source"] == "provided", f"row={run_a}")

    print("\n--- legacy oversee.loop.title adopt ---")
    post("legacy", [span("start", 260, {"oversee.loop.external_id": "leg1"})])
    post("legacy", [span("named", 250, {
        "oversee.loop.external_id": "leg1",
        "oversee.loop.title": "Legacy titled follow-up",
    })])
    leg = row_for("leg1")
    check("oversee.loop.title adopts onto the oversee-keyed loop",
          leg["title"] == "Legacy titled follow-up"
          and leg["title_source"] == "provided", f"row={leg}")

    print("\n--- (b) shell titles rejected at INSERT and adopt ---")
    post("shells", [span("start", 240, {
        "trovis.loop.external_id": "sh1",
        "trovis.loop.title": "Task from shells",
    })])
    sh1 = row_for("sh1")
    check("Task-from shell rejected at INSERT (not provided)",
          sh1["title_source"] != "provided"
          and not (sh1.get("title") or "").strip(), f"row={sh1}")

    post("shells", [span("tmpl", 230, {
        "trovis.loop.external_id": "sh1",
        "trovis.loop.title": "shells · exec · 4 actions",
    })])
    sh1 = row_for("sh1")
    check("template shell rejected at adopt (still untitled)",
          sh1["title_source"] != "provided"
          and not (sh1.get("title") or "").strip(), f"row={sh1}")

    # After shells, a real title must still be able to adopt.
    post("shells", [span("real", 220, {
        "trovis.loop.external_id": "sh1",
        "trovis.loop.title": "Really file the claim",
    })])
    sh1 = row_for("sh1")
    check("real title adopts after rejected shells",
          sh1["title"] == "Really file the claim"
          and sh1["title_source"] == "provided", f"row={sh1}")

    print("\n--- (c) existing provided is not overwritten ---")
    post("locked", [span("start", 200, {
        "trovis.loop.external_id": "lk1",
        "trovis.loop.title": "Original provided title",
    })])
    post("locked", [span("later", 190, {
        "trovis.loop.external_id": "lk1",
        "trovis.loop.title": "Attempted overwrite",
    })])
    post("locked", [span("shell", 180, {
        "trovis.loop.external_id": "lk1",
        "trovis.loop.title": "Task from locked",
    })])
    lk1 = row_for("lk1")
    check("first provided title wins forever",
          lk1["title"] == "Original provided title"
          and lk1["title_source"] == "provided", f"row={lk1}")

    # Generated title is also not overwritten (NULL/empty only).
    post("gentitled", [span("start", 170, {"trovis.loop.external_id": "g1"})])
    g1 = row_for("g1")
    account_id = database.validate_api_key(key)["account_id"]
    database.set_loop_title_if_missing(
        g1["id"], "gentitled · run · 1 actions", account_id)
    post("gentitled", [span("later", 160, {
        "trovis.loop.external_id": "g1",
        "trovis.loop.title": "Human arrived after sweep",
    })])
    g1 = row_for("g1")
    check("generated title is not overwritten by a later provided title",
          g1["title"] == "gentitled · run · 1 actions"
          and g1["title_source"] == "generated", f"row={g1}")

    print("\n--- closed loop does not adopt ---")
    # Close, then a same-key title inside the grace window. The span
    # attaches to the closed loop; adopt is open-only so the title
    # must not land.
    post("done-loop", [
        span("start", 20, {"trovis.loop.external_id": "cl1"}),
        span("fin", 15, {
            "trovis.loop.external_id": "cl1",
            "trovis.loop.close": "done",
        }),
    ])
    post("done-loop", [span("late", 5, {
        "trovis.loop.external_id": "cl1",
        "trovis.loop.title": "Too late to name",
    })])
    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute(
            "SELECT id, title, title_source, closed_at FROM loops "
            "WHERE external_id = ? ORDER BY id",
            ("cl1",),
        )
        cl_rows = [dict(r) for r in cur.fetchall()]
    check("grace-window title attaches to the one closed loop",
          len(cl_rows) == 1 and cl_rows[0]["closed_at"] is not None,
          f"rows={cl_rows}")
    check("closed loop stays untitled (adopt is open-only)",
          not (cl_rows[0].get("title") or "").strip()
          and cl_rows[0].get("title_source") is None, f"row={cl_rows[0]}")

    print("\n--- (d) lean overview still only counts provided ---")
    ov = overview()
    page = items()
    titles = [it["title"] for it in page["items"]]
    # provided open: a1, run-a, leg1, sh1 (after real adopt), lk1.
    # generated g1, closed untitled cl1, rejected-until-adopt shells: excluded.
    check("overview.open counts provided titles only",
          ov["open"] == 5, f"open={ov['open']} titles={titles}")
    check("lean items are exactly the provided titles",
          set(titles) == {
              "Reconcile vendor 19",
              "Ship the Friday digest",
              "Legacy titled follow-up",
              "Really file the claim",
              "Original provided title",
          }, f"titles={titles}")
    check("generated / shell / closed-untitled never appear on lean items",
          "gentitled · run · 1 actions" not in titles
          and "Task from shells" not in titles
          and "Too late to name" not in titles
          and "Attempted overwrite" not in titles)
    check("_NAMED_TITLE_SQL still requires title_source=provided",
          "title_source = 'provided'" in database._NAMED_TITLE_SQL)
    check("_NAMED_TITLE_SQL still excludes shells",
          "task from" in database._NAMED_TITLE_SQL.lower()
          and "actions" in database._NAMED_TITLE_SQL)

print()
if failures:
    print(f"FAILED ({len(failures)}): " + "; ".join(failures))
    raise SystemExit(1)
print("ADOPT-TITLE INGEST VERIFIED")
os.unlink(_tmp.name)
