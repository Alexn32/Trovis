"""Naming a person who has not signed in yet.

Work gets handed to people before they have a Trovis login, and often before
they ever get one. Until now the only way that handoff read as "Sarah Chen"
instead of "a human" was the `team_members` directory — a second table of
people, written by an endpoint nothing in the product called any more. This
ship replaces it with the record of the person the org actually invited, and
closes the endpoint.

Four things have to hold:

  1. A NAMED INVITE names them. Invited as "Sarah Chen", and a handoff to her
     address reads as her name — before she signs in, and whether or not she
     ever does.

  2. AN UNRESOLVED EMAIL STAYS NAMELESS. This is the load-bearing one.
     Echoing a raw address back as a name would render ANY address an agent
     emits — including another org's — as a colleague. The seam is
     account_id, and it holds for invites exactly as it did for users.

  3. LEGACY NAMES SURVIVE. Rows already in team_members still resolve. That
     is why there is no backfill: minting invite tokens for old directory
     rows would create redeemable links nobody asked for, to fix names that
     already work.

  4. POST /team IS CLOSED. 410, not a deprecation notice, and nothing in the
     product writes there.

Run:
  OVERSEE_DISABLE_PRICING_SYNC=1 python3 test_pending_invite_names.py
(isolated temp SQLite DB; never touches the dev/prod DB)
"""
import os
import tempfile

os.environ["OVERSEE_DISABLE_PRICING_SYNC"] = "1"
os.environ["TROVIS_DISABLE_ALERTS"] = "1"
os.environ["TROVIS_DISABLE_LOOP_SWEEP"] = "1"
os.environ.pop("DATABASE_URL", None)
os.environ.pop("ANTHROPIC_API_KEY", None)

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()

import database
database.SQLITE_PATH = _tmp.name

import main
from fastapi.testclient import TestClient

main._auto_describe = lambda *a, **k: False

database.init_db()
client = TestClient(main.app)

failures = []


def check(label, cond):
    print(("  PASS " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def signup(email, org_name):
    r = client.post(
        "/auth/signup",
        json={
            "email": email,
            "password": "correct horse battery",
            "name": email.split("@")[0],
            "account_type": "business",
            "org_name": org_name,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def name_for(target, account_id):
    with database._connect() as conn, database._cursor(conn) as cur:
        return database._resolve_human_name(cur, target, account_id)


acme = signup("founder@acme.test", "Acme")
acct = acme["org"]["id"]
FOUNDER = acme["token"]

lv = {l["key"]: l for l in client.get("/org/scope-levels", headers=auth(FOUNDER)).json()}
lead = client.post(
    "/org/roles", json={"title": "Support Lead", "scope_level_id": lv["manager"]["id"]},
    headers=auth(FOUNDER),
).json()


# ---------------------------------------------------------------------------
# 1. A named invite names them, before they sign in
# ---------------------------------------------------------------------------
print("\nA named pending invite")

r = client.post(
    "/org/invites",
    json={"email": "sarah@acme.test", "name": "Sarah Chen", "role_id": lead["id"]},
    headers=auth(FOUNDER),
)
check("invite with a name is accepted", r.status_code == 201)
check("...and echoes the name back", r.json()["display_name"] == "Sarah Chen")
check("...alongside the role she will land in", r.json()["role_id"] == lead["id"])
sarah_token = r.json()["invite_url"].split("token=")[1]

check(
    "a handoff to her reads as her name, with no login anywhere",
    name_for("sarah@acme.test", acct) == "Sarah Chen",
)
check(
    "...case-insensitively, as agents will send it",
    name_for("SARAH@ACME.TEST", acct) == "Sarah Chen",
)
check(
    "she has no user row at all",
    database.get_user_by_email("sarah@acme.test") is None,
)
pending = client.get("/org/invites", headers=auth(FOUNDER)).json()
check(
    "the pending list shows the name, not a bare address",
    any(i["display_name"] == "Sarah Chen" for i in pending),
)

# An invite with no name is still a working invite — it just leaves the
# handoff nameless, which is the honest answer.
client.post(
    "/org/invites", json={"email": "anon@acme.test"}, headers=auth(FOUNDER)
)
check("an unnamed invite is still an invite", name_for("anon@acme.test", acct) is None)


# ---------------------------------------------------------------------------
# 2. The name outlives the token
# ---------------------------------------------------------------------------
print("\nThe token expires; the name does not")

# The token expiring is what stops someone joining late. The name is a
# record of who the org said this person is, and reading it grants nothing —
# so an expired invite must not turn a named colleague back into "a human".
with database._connect() as conn, database._cursor(conn) as cur:
    cur.execute(
        f"UPDATE invites SET expires_at = {database.PH} "
        f"WHERE LOWER(email) = {database.PH}",
        ("2000-01-01T00:00:00", "sarah@acme.test"),
    )
check(
    "an expired invite still names her",
    name_for("sarah@acme.test", acct) == "Sarah Chen",
)
check(
    "...while dropping off the pending list, which is about joining",
    not any(
        i["email"] == "sarah@acme.test"
        for i in client.get("/org/invites", headers=auth(FOUNDER)).json()
    ),
)
check(
    "...and the expired token cannot be redeemed",
    client.post(
        "/auth/accept-invite",
        json={"token": sarah_token, "name": "Sarah", "password": "correct horse battery"},
    ).status_code
    == 400,
)

# A re-invite is the org correcting itself; the newest name wins.
client.post(
    "/org/invites",
    json={"email": "sarah@acme.test", "name": "Sarah Chen-Okafor", "role_id": lead["id"]},
    headers=auth(FOUNDER),
)
check(
    "a re-invite corrects the name",
    name_for("sarah@acme.test", acct) == "Sarah Chen-Okafor",
)


# ---------------------------------------------------------------------------
# 3. Accepting still does everything it did
# ---------------------------------------------------------------------------
print("\nAccepting")

r = client.post(
    "/org/invites",
    json={"email": "ira@acme.test", "name": "Ira Chen", "role_id": lead["id"]},
    headers=auth(FOUNDER),
)
token = r.json()["invite_url"].split("token=")[1]
check("a named handoff works before acceptance", name_for("ira@acme.test", acct) == "Ira Chen")

a = client.post(
    "/auth/accept-invite",
    json={"token": token, "password": "correct horse battery"},
)
check("accepting creates the login", a.status_code == 201)
uid = a.json()["user"]["id"]
check(
    "...adopting the name the org gave them when they give none",
    a.json()["user"]["name"] == "Ira Chen",
)
check(
    "...seated in the invited role",
    database.get_role_id_for_user(acct, uid) == lead["id"],
)
check(
    "...and the handoff name now comes from the user row",
    name_for("ira@acme.test", acct) == "Ira Chen",
)
check("...as does their id", name_for(str(uid), acct) == "Ira Chen")

# A person who types their own name keeps it — the invite was a suggestion.
r = client.post(
    "/org/invites",
    json={"email": "bo@acme.test", "name": "Robert Ng"}, headers=auth(FOUNDER),
)
t2 = r.json()["invite_url"].split("token=")[1]
client.post(
    "/auth/accept-invite",
    json={"token": t2, "name": "Bo Ng", "password": "correct horse battery"},
)
check("their own name wins over the invited one", name_for("bo@acme.test", acct) == "Bo Ng")


# ---------------------------------------------------------------------------
# 4. An unresolved email stays nameless
# ---------------------------------------------------------------------------
print("\nNameless stays nameless")

check(
    "an address nobody has ever named resolves to nothing",
    name_for("stranger@nowhere.test", acct) is None,
)
other = signup("founder@other.test", "Other Co")
other_acct = other["org"]["id"]
client.post(
    "/org/invites",
    json={"email": "theirs@other.test", "name": "Their Person"},
    headers=auth(other["token"]),
)
check(
    "another org's named invite does not resolve in ours",
    name_for("theirs@other.test", acct) is None,
)
check(
    "...and ours does not resolve in theirs",
    name_for("sarah@acme.test", other_acct) is None,
)
check(
    "...though it does resolve in its own org",
    name_for("theirs@other.test", other_acct) == "Their Person",
)
check(
    "a user id from another org still resolves to nothing",
    name_for(str(other["user"]["id"]), acct) is None,
)


# ---------------------------------------------------------------------------
# 5. Legacy names survive; the writer is closed
# ---------------------------------------------------------------------------
print("\nThe old directory")

legacy = database.create_team_member(
    account_id=acct, name="Pat Nolan", email="pat@contractor.test", role="Contractor"
)
check(
    "a name already in the directory still resolves",
    name_for("pat@contractor.test", acct) == "Pat Nolan",
)
check(
    "...and is still account-scoped",
    name_for("pat@contractor.test", other_acct) is None,
)
check("...and can still own an agent", legacy["id"] > 0)

r = client.post(
    "/team", json={"name": "Nope", "email": "nope@acme.test", "role": "x"},
    headers=auth(FOUNDER),
)
check("POST /team is gone", r.status_code == 410)
check("...and says where people live now", "/org/invites" in (r.json().get("detail") or ""))
check(
    "...so nothing new lands in the directory",
    name_for("nope@acme.test", acct) is None,
)
r = client.post(
    "/team", json={"name": "Nope", "email": "nope2@acme.test"},
    headers={"X-Trovis-Api-Key": acme["api_key"]},
)
check("an API key cannot write there either", r.status_code == 410)

# Reading it is still allowed — an operator can see what is left.
check(
    "GET /team still reads the leftovers",
    any(
        m["email"] == "pat@contractor.test"
        for m in client.get("/team", headers=auth(FOUNDER)).json()
    ),
)

# An invite outranks a stale directory row for the same person: the invite
# is the newer statement of who they are.
database.create_team_member(
    account_id=acct, name="S. Chen (old)", email="sarah@acme.test", role="Lead"
)
check(
    "a named invite wins over a stale directory row",
    name_for("sarah@acme.test", acct) == "Sarah Chen-Okafor",
)


# ---------------------------------------------------------------------------
# 6. The invite ladder still applies to a named invite
# ---------------------------------------------------------------------------
print("\nAuthZ is unchanged")

sub = client.post(
    "/org/roles",
    json={"title": "Support IC", "parent_role_id": lead["id"],
          "scope_level_id": lv["ic"]["id"]},
    headers=auth(FOUNDER),
).json()
client.post(
    "/org/roles/%d/members" % lead["id"], json={"user_id": uid}, headers=auth(FOUNDER)
)
IRA = a.json()["token"]
r = client.post(
    "/org/invites",
    json={"email": "x@acme.test", "name": "X", "role_id": sub["id"]},
    headers=auth(IRA),
)
check("a manager can invite a named person below them", r.status_code == 201)
r = client.post(
    "/org/invites",
    json={"email": "y@acme.test", "name": "Y", "role_id": lead["id"]},
    headers=auth(IRA),
)
check("...but not into their own box", r.status_code == 403)
check(
    "...and the refused name was never recorded",
    name_for("y@acme.test", acct) is None,
)


print()
if failures:
    print(f"{len(failures)} FAILED:")
    for f in failures:
        print("  - " + f)
    raise SystemExit(1)
print("all passed")
