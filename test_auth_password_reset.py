"""Password reset must survive logout.

Repro this suite locks:
  signup → forgot → reset (auto-session) → logout → login with the NEW
  password succeeds; the old / a wrong password still 401s.

Reset signs the caller in by user_id without going through /auth/login.
If login resolved a different row than reset updated (accounts.email vs
users.email, or a case-mismatched users.email), the reset session worked
and the next login failed with "invalid email or password".

Run:
  TROVIS_DISABLE_PRICING_SYNC=1 python3 test_auth_password_reset.py
(isolated temp SQLite; email send is captured, never hits the network)
"""
import os
import re
import tempfile

os.environ["TROVIS_DISABLE_PRICING_SYNC"] = "1"
os.environ["OVERSEE_DISABLE_PRICING_SYNC"] = "1"
os.environ.pop("DATABASE_URL", None)
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ.pop("RESEND_API_KEY", None)

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()

import database
database.SQLITE_PATH = _tmp.name

import email_send
import main
from fastapi.testclient import TestClient

main._auto_describe = lambda *a, **k: False

failures = []


def check(label, cond):
    print(("  PASS " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


_captured = {}


def _capture_email(to, subject, html):
    _captured["to"] = to
    _captured["subject"] = subject
    _captured["html"] = html
    return True


email_send.send_email = _capture_email


def _reset_token_from_email():
    html = _captured.get("html") or ""
    m = re.search(r"reset=([^\"&]+)", html)
    return m.group(1) if m else None


def _signup(c, email, password, name="Reset Tester"):
    r = c.post("/auth/signup", json={
        "email": email, "password": password, "name": name,
        "account_type": "individual",
    })
    check(f"signup {email} → 201", r.status_code == 201)
    return r.json()


def _login(c, email, password):
    return c.post("/auth/login", json={"email": email, "password": password})


print("\nhash / verify round-trip:")
h = database.hash_password("newpassword1")
check("verify matches", database.verify_password("newpassword1", h))
check("verify rejects other", not database.verify_password("oldpassword1", h))
check("verify rejects empty", not database.verify_password("newpassword1", None))
check("verify rejects garbage", not database.verify_password("newpassword1", "not-a-hash"))


with TestClient(main.app) as c:
    print("\nreset → logout → login with new password:")
    created = _signup(c, "Reset.User@Example.com", "oldpassword1")
    check("signup stored lowercased email", created["user"]["email"] == "reset.user@example.com")

    r = _login(c, "Reset.User@Example.com", "oldpassword1")
    check("login with original password → 200", r.status_code == 200)

    _captured.clear()
    r = c.post("/auth/forgot-password", json={"email": "Reset.User@Example.com"})
    check("forgot → 204", r.status_code == 204)
    tok = _reset_token_from_email()
    check("forgot emailed a reset token", bool(tok))

    r = c.post("/auth/reset-password", json={"token": tok, "new_password": "newpassword1"})
    check("reset → 200", r.status_code == 200)
    body = r.json() if r.status_code == 200 else {}
    check("reset returns a session token", bool(body.get("token")))
    check("reset user email is the signup email", body.get("user", {}).get("email") == "reset.user@example.com")
    sess = body.get("token")

    r = c.get("/auth/me", headers={"Authorization": f"Bearer {sess}"})
    check("reset session works (/auth/me)", r.status_code == 200)

    stored = database.get_user_by_email("Reset.User@Example.com")
    check("hash persisted for that email", bool(stored and stored.get("password_hash")))
    check("stored hash verifies the new password",
          stored is not None and database.verify_password("newpassword1", stored.get("password_hash")))
    check("stored hash rejects the old password",
          stored is not None and not database.verify_password("oldpassword1", stored.get("password_hash")))

    r = c.post("/auth/logout", headers={"Authorization": f"Bearer {sess}"})
    check("logout → 204", r.status_code == 204)

    r = _login(c, "Reset.User@Example.com", "newpassword1")
    check("login with new password after logout → 200", r.status_code == 200)
    check("login returns a new session", bool(r.json().get("token")) if r.status_code == 200 else False)

    r = _login(c, "reset.user@example.com", "newpassword1")
    check("login with lowercased email + new password → 200", r.status_code == 200)

    r = _login(c, "Reset.User@Example.com", "oldpassword1")
    check("old password after reset → 401", r.status_code == 401)
    check("old password uses generic error",
          r.status_code == 401 and r.json().get("detail") == "invalid email or password")

    r = _login(c, "Reset.User@Example.com", "wrongpassword")
    check("wrong password → 401", r.status_code == 401)

    r = c.post("/auth/reset-password", json={"token": tok, "new_password": "anotherpass1"})
    check("reused reset token → 400", r.status_code == 400)

    print("\nunknown email is not enumerated:")
    _captured.clear()
    r = c.post("/auth/forgot-password", json={"email": "nobody@example.com"})
    check("forgot unknown email → 204", r.status_code == 204)
    check("forgot unknown email sends nothing", _captured.get("html") is None)

    print("\naccounts.email ≠ users.email (claim / legacy):")
    acct = database.create_account("org@hammocks.test", account_type="individual", name="Hammocks")
    member = database.create_user(
        acct["id"], "human@hammocks.test", "Hammocks Owner", "owner",
        database.hash_password("legacy-pass1"),
    )
    check("diverged emails created",
          member["email"] == "human@hammocks.test" and acct["email"] == "org@hammocks.test")

    r = _login(c, "human@hammocks.test", "legacy-pass1")
    check("login via users.email still works", r.status_code == 200)
    r = _login(c, "org@hammocks.test", "legacy-pass1")
    check("login via accounts.email reaches the same owner", r.status_code == 200)

    _captured.clear()
    r = c.post("/auth/forgot-password", json={"email": "org@hammocks.test"})
    check("forgot via accounts.email → 204", r.status_code == 204)
    diverged_tok = _reset_token_from_email()
    check("forgot via accounts.email emailed a token", bool(diverged_tok))

    r = c.post("/auth/reset-password", json={"token": diverged_tok, "new_password": "hammocks-new1"})
    check("reset (token from accounts.email) → 200", r.status_code == 200)
    diverged_sess = r.json().get("token") if r.status_code == 200 else None
    c.post("/auth/logout", headers={"Authorization": f"Bearer {diverged_sess}"})

    r = _login(c, "org@hammocks.test", "hammocks-new1")
    check("after reset, login via accounts.email → 200", r.status_code == 200)
    r = _login(c, "human@hammocks.test", "hammocks-new1")
    check("after reset, login via users.email → 200", r.status_code == 200)
    r = _login(c, "org@hammocks.test", "legacy-pass1")
    check("after reset, old password via accounts.email → 401", r.status_code == 401)

    print("\nmixed-case users.email still logs in after reset:")
    mixed = database.create_user(
        acct["id"], "cased@hammocks.test", "Cased", "member",
        database.hash_password("cased-old-1"),
    )
    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute(
            f"UPDATE users SET email = {database.PH} WHERE id = {database.PH}",
            ("Cased@Hammocks.Test", mixed["id"]),
        )
    raw = database.create_password_reset(mixed["id"])
    r = c.post("/auth/reset-password", json={"token": raw, "new_password": "cased-new-1"})
    check("reset mixed-case user → 200", r.status_code == 200)
    mixed_sess = r.json().get("token") if r.status_code == 200 else None
    c.post("/auth/logout", headers={"Authorization": f"Bearer {mixed_sess}"})
    r = _login(c, "cased@hammocks.test", "cased-new-1")
    check("login lowercased email against mixed-case row → 200", r.status_code == 200)
    r = _login(c, "Cased@Hammocks.Test", "cased-new-1")
    check("login mixed-case email against mixed-case row → 200", r.status_code == 200)
    r = _login(c, "cased@hammocks.test", "cased-old-1")
    check("mixed-case row rejects old password → 401", r.status_code == 401)

    print("\nset_user_password refuses a missed UPDATE:")
    try:
        database.set_user_password(10_000_000, database.hash_password("no-such-user-1"))
        missed = False
    except LookupError:
        missed = True
    check("missing user_id raises LookupError", missed)


if failures:
    print(f"\n{len(failures)} FAILED: {failures}")
    raise SystemExit(1)
print("\nAll password-reset login checks passed.")
