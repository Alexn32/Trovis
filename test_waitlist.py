"""Public founding waitlist: HTML landing, signup persist, anti-spam.

Run:
  TROVIS_DISABLE_PRICING_SYNC=1 python3 test_waitlist.py
(isolated temp SQLite; no network)
"""
import os
import tempfile

os.environ["TROVIS_DISABLE_PRICING_SYNC"] = "1"
os.environ["OVERSEE_DISABLE_PRICING_SYNC"] = "1"
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


def check(label, cond):
    print(("  PASS " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


def _signup_owner(c):
    r = c.post("/auth/signup", json={
        "email": "owner@example.com",
        "password": "password1",
        "name": "Owner",
        "account_type": "individual",
    })
    check("seed owner → 201 (so / is still public after users exist)", r.status_code == 201)


c = TestClient(main.app)
_signup_owner(c)
main._waitlist_hits.clear()

print("\npublic HTML landing:")
for path in ("/", "/waitlist", "/waitlist/"):
    r = c.get(path)
    check(f"GET {path} → 200", r.status_code == 200)
    check(f"GET {path} is HTML", "text/html" in r.headers.get("content-type", ""))
    body = r.text
    check(f"GET {path} has founding CTA", "Join the founding list" in body)
    check(f"GET {path} has email field", 'name="email"' in body)
    check(f"GET {path} has company field", 'name="company"' in body)
    check(f"GET {path} has role field", 'name="role"' in body)
    check(f"GET {path} has tools field", 'name="tools"' in body)
    check(f"GET {path} has honeypot", 'name="website"' in body)
    check(f"GET {path} title is waitlist", "Founding waitlist" in body)
    low = body.lower()
    check(f"GET {path} does not claim Connect is live", "connect is live" not in low)
    check(f"GET {path} does not lead with Cost", "cost tracking" not in low)
    check(f"GET {path} does not lead with Fleet", "fleet" not in low)
    check(f"GET {path} is not Monday-of-AI", "monday of" not in low)
    check(f"GET {path} is not create-account hero", "create your account" not in low)
    check(f"GET {path} mentions handoffs", "handoff" in low)
    check(f"GET {path} mentions waiting work", "waiting" in low)
    check(f"GET {path} mentions Home desk live", "home desk is live" in low)

print("\nsignup persist:")
r = c.post("/waitlist", json={
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
check("company stored", row and row["company"] == "Acme")
check("role stored", row and row["role"] == "Founder")
check("tools stored on runtime_interest", row and row["runtime_interest"] == "OpenClaw, Cursor, Claude")
check("source defaulted", row and row["source"] == "founding-waitlist")
check("count is 1", database.get_waitlist_count() == 1)

r = c.post("/waitlist", json={"email": "founder@acme.dev"})
check("repeat email → 200", r.status_code == 200)
check("repeat is already_joined", r.json().get("status") == "already_joined")
check("count still 1", database.get_waitlist_count() == 1)

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

if failures:
    print(f"\n{len(failures)} FAILED: {failures}")
    raise SystemExit(1)
print("\nAll waitlist checks passed.")
