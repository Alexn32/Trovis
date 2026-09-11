"""Public founding waitlist: HTML landing, signup persist, anti-spam.

Run:
  TROVIS_DISABLE_PRICING_SYNC=1 python3 test_waitlist.py
(isolated temp SQLite; no network)
"""
import os
import sqlite3
import tempfile

os.environ["TROVIS_DISABLE_PRICING_SYNC"] = "1"
os.environ["OVERSEE_DISABLE_PRICING_SYNC"] = "1"
os.environ["TROVIS_DISABLE_ALERTS"] = "1"
os.environ["TROVIS_DISABLE_LOOP_SWEEP"] = "1"
os.environ.pop("DATABASE_URL", None)
os.environ.pop("ANTHROPIC_API_KEY", None)

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["TROVIS_DB_PATH"] = _tmp.name

import database
database.SQLITE_PATH = _tmp.name

import main
from fastapi.testclient import TestClient

main._auto_describe = lambda *a, **k: False

# Same boot path as production (lifespan → init_db). Creates waitlist_signups
# plus the company/role/name columns via _try_add_column. A bare TestClient()
# does not run lifespan, which is why CI saw "no such table: waitlist_signups".
database.init_db()

failures = []


def check(label, cond):
    print(("  PASS " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


def _table_exists(name: str) -> bool:
    with database._connect() as conn, database._cursor(conn) as cur:
        if database.USE_POSTGRES:
            cur.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_name = %s",
                (name,),
            )
        else:
            cur.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
                (name,),
            )
        return cur.fetchone() is not None


def _columns(table: str) -> set[str]:
    with database._connect() as conn, database._cursor(conn) as cur:
        if database.USE_POSTGRES:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = %s",
                (table,),
            )
            return {row["column_name"] for row in cur.fetchall()}
        cur.execute(f"PRAGMA table_info({table})")
        return {row["name"] for row in cur.fetchall()}


print("\nboot / migrate:")
check("init_db created waitlist_signups", _table_exists("waitlist_signups"))
check("init_db has name column", "name" in _columns("waitlist_signups"))


def _signup_owner(c):
    r = c.post("/auth/signup", json={
        "email": "owner@example.com",
        "password": "password12",
        "name": "Owner",
        "account_type": "individual",
    })
    check("seed owner → 201", r.status_code == 201)


with TestClient(main.app) as c:
    _signup_owner(c)
    main._waitlist_hits.clear()

    print("\nseeded owner does not hide the public landing:")
    r = c.get("/")
    check("GET / after owner exists → 200", r.status_code == 200)
    check("GET / after owner is waitlist HTML, not 401", "Join the founding list" in r.text)

    print("\npublic HTML landing:")
    for path in ("/", "/waitlist", "/waitlist/"):
        r = c.get(path)
        check(f"GET {path} → 200", r.status_code == 200)
        check(f"GET {path} is HTML", "text/html" in r.headers.get("content-type", ""))
        body = r.text
        check(f"GET {path} has founding CTA", "Join the founding list" in body)
        check(f"GET {path} has name field", 'name="name"' in body)
        check(f"GET {path} name precedes email", body.find('name="name"') < body.find('name="email"'))
        check(f"GET {path} has email field", 'name="email"' in body)
        check(f"GET {path} has company field", 'name="company"' in body)
        check(f"GET {path} has role field", 'name="role"' in body)
        check(f"GET {path} has tools field", 'name="tools"' in body)
        check(f"GET {path} has honeypot", 'name="website"' in body)
        check(f"GET {path} title is waitlist", "Founding waitlist" in body)
        check(f"GET {path} has locked headline", "See the Work" in body)
        hero = body.split("Home desk: what needs you")[0]
        check(f"GET {path} visibility is not in the hero", "actually doing" not in hero)
        check(f"GET {path} has Home desk bullet", "Home desk: what needs you, not another dashboard" in body)
        check(
            f"GET {path} has Work-as-jobs bullet",
            "Work as jobs you can see and judge — people, agents, and the tools already in the loop — one place to see and judge the Work." in body,
        )
        check(
            f"GET {path} has agent-visibility bullet",
            "See what your agents are actually doing — and who’s waiting / what’s stuck — without drowning in traces" in body,
        )
        check(f"GET {path} has beachhead bullet", "eng/founder teams with agents already in production" in body)
        check(f"GET {path} has honesty line", "Desk and Work visibility first" in body)
        check(f"GET {path} honesty says not live yet", "we’re not pitching them as live yet" in body)
        low = body.lower()
        check(f"GET {path} does not claim Connect is live", "connect is live" not in low)
        check(f"GET {path} does not lead with Cost", "cost tracking" not in low)
        check(f"GET {path} does not lead with Fleet", "fleet" not in low)
        check(f"GET {path} is not create-account hero", "create your account" not in low)
        check(f"GET {path} mentions handoffs", "handoffs get stuck" in low)
        check(f"GET {path} mentions waiting", "waiting" in low)
        check(f"GET {path} does not imply Connect live", "saas in one loop" not in low)
        check(f"GET {path} is not recording pitch", "record themselves" not in low)

    print("\nsignup persist:")
    r = c.post("/waitlist", json={
        "name": "Ada Founder",
        "email": "founder@acme.dev",
        "company": "Acme",
        "role": "Founder",
        "tools": "OpenClaw, Cursor, Claude",
    })
    check("POST valid email → 200", r.status_code == 200)
    check("status joined", r.json().get("status") == "joined")
    row = database.get_waitlist_signup("founder@acme.dev")
    check("row stored", row is not None)
    check("email lowercased", row and row["email"] == "founder@acme.dev")
    check("name stored", row and row["name"] == "Ada Founder")
    check("company stored", row and row["company"] == "Acme")
    check("role stored", row and row["role"] == "Founder")
    check("tools stored on runtime_interest", row and row["runtime_interest"] == "OpenClaw, Cursor, Claude")
    check("source defaulted", row and row["source"] == "founding-waitlist")
    check("count is 1", database.get_waitlist_count() == 1)

    r = c.post("/waitlist", json={"email": "noname@acme.dev"})
    check("POST without name → 200", r.status_code == 200)
    nameless = database.get_waitlist_signup("noname@acme.dev")
    check("nameless row stored", nameless is not None)
    check("omitted name is empty", nameless and not nameless.get("name"))
    check("count is 2 after nameless", database.get_waitlist_count() == 2)

    r = c.post("/waitlist", json={"email": "founder@acme.dev"})
    check("repeat email → 200", r.status_code == 200)
    check("repeat is already_joined", r.json().get("status") == "already_joined")
    check("count still 2 after repeat", database.get_waitlist_count() == 2)

    r = c.post("/waitlist", json={"email": "  OTHER@Acme.dev  ", "runtime_interest": "legacy field"})
    check("legacy runtime_interest accepted", r.status_code == 200)
    legacy = database.get_waitlist_signup("other@acme.dev")
    check("legacy email trimmed/lowercased", legacy and legacy["email"] == "other@acme.dev")
    check("legacy tools from runtime_interest", legacy and legacy["runtime_interest"] == "legacy field")

    r = c.post("/waitlist", json={"email": "not-an-email"})
    check("invalid email → 422", r.status_code == 422)
    check("invalid email not stored", database.get_waitlist_signup("not-an-email") is None)

    print("\nhoneypot:")
    before = database.get_waitlist_count()
    r = c.post("/waitlist", json={"email": "bot@spam.test", "website": "https://spam.test"})
    check("honeypot → 200", r.status_code == 200)
    check("honeypot looks like joined", r.json().get("status") == "joined")
    check("honeypot not stored", database.get_waitlist_signup("bot@spam.test") is None)
    check("count unchanged after honeypot", database.get_waitlist_count() == before)

    print("\nrate limit:")
    main._waitlist_hits.clear()
    saved_max = main._WAITLIST_RATE_MAX
    main._WAITLIST_RATE_MAX = 2
    try:
        a = c.post("/waitlist", json={"email": "r1@acme.dev"})
        b = c.post("/waitlist", json={"email": "r2@acme.dev"})
        d = c.post("/waitlist", json={"email": "r3@acme.dev"})
        check("first two under cap", a.status_code == 200 and b.status_code == 200)
        check("third is 429", d.status_code == 429)
        check("429 has Retry-After", "retry-after" in {k.lower() for k in d.headers})
        check("rate-limited email not stored", database.get_waitlist_signup("r3@acme.dev") is None)
    finally:
        main._WAITLIST_RATE_MAX = saved_max
        main._waitlist_hits.clear()

    print("\ncount endpoint still public:")
    r = c.get("/waitlist/count")
    check("GET /waitlist/count → 200", r.status_code == 200)
    check("count is a number", isinstance(r.json().get("count"), int))

print("\nmigrate path (existing table gains name):")
_old = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_old.close()
with sqlite3.connect(_old.name) as conn:
    conn.execute(
        """
        CREATE TABLE waitlist_signups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL UNIQUE,
            source TEXT,
            runtime_interest TEXT,
            company TEXT,
            role TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute("INSERT INTO waitlist_signups (email) VALUES ('legacy@acme.dev')")
    conn.commit()

_prev_path = database.SQLITE_PATH
database.SQLITE_PATH = _old.name
try:
    check("pre-migrate has no name column", "name" not in _columns("waitlist_signups"))
    database.init_db()
    check("init_db adds name to existing waitlist_signups", "name" in _columns("waitlist_signups"))
    legacy_row = database.get_waitlist_signup("legacy@acme.dev")
    check("legacy row survives migrate", legacy_row is not None)
    status = database.add_waitlist_signup(email="after-migrate@acme.dev", name="Ada")
    check("signup after migrate → joined", status == "joined")
    migrated = database.get_waitlist_signup("after-migrate@acme.dev")
    check("name stored after migrate", migrated and migrated["name"] == "Ada")
finally:
    database.SQLITE_PATH = _prev_path

if failures:
    print(f"\n{len(failures)} FAILED: {failures}")
    raise SystemExit(1)
print("\nAll waitlist checks passed.")
