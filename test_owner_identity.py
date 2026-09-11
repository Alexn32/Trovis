"""Human identity on an agent: who owns it, and what their name resolves to.

`team_members` was a directory of people with no login, from before Trovis
had real users. The org chart replaced it, and two tables that both mean "the
humans here" is a silent second truth: the same agent could show an owner
resolved one way on Fleet and another on its detail page.

This ship points ownership at `users` and leaves exactly one resolver
(_OWNER_JOIN_SQL) behind every read. Four things have to hold:

  1. New ownership is a `users` assignment, and the role shown next to a name
     is their role ON THE CHART — not users.role, which is an account
     permission ('owner'/'member') and would silently change what that label
     means.

  2. Existing rows keep resolving. A legacy row is backfilled by email where
     a matching login exists, and dual-read covers the rest — an agent that
     had an owner yesterday must not show "unassigned" today.

  3. account_id holds on both ids. A cross-tenant user_id or team_member_id
     would leak that person's name and email straight back out through the
     owner join.

  4. A departed person owns nothing. Deleting a user clears their
     assignments rather than leaving an owner whose login no longer exists.

Run:
  OVERSEE_DISABLE_PRICING_SYNC=1 python3 test_owner_identity.py
(isolated temp SQLite DB; never touches the dev/prod DB)
"""
import os
import tempfile
import time

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


def invite_accept(owner_token, email, name, role_id=None):
    body = {"email": email, "role": "member"}
    if role_id is not None:
        body["role_id"] = role_id
    r = client.post("/org/invites", json=body, headers=auth(owner_token))
    assert r.status_code == 201, r.text
    token = r.json()["invite_url"].split("token=")[1]
    a = client.post(
        "/auth/accept-invite",
        json={"token": token, "name": name, "password": "correct horse battery"},
    )
    assert a.status_code == 201, a.text
    return a.json()


NS = 1_000_000_000
_seq = [0]


def legacy_directory_row(account_id, name, email, role):
    """A team_members row, written straight to the table.

    POST /team is closed (410) — people are org members or named invites
    now. But rows written before that closure still exist in real
    databases, and their names still have to resolve, so the tests that
    prove the legacy read path keeps working have to seed it the way a
    legacy database already looks.
    """
    return database.create_team_member(
        account_id=account_id, name=name, email=email, role=role
    )


def seed_agent(account_id, service, agent_id="main"):
    _seq[0] += 1
    now = time.time_ns()
    database.insert_spans(
        [
            {
                "trace_id": f"t{_seq[0]}",
                "span_id": f"s{_seq[0]}",
                "parent_span_id": None,
                "service_name": service,
                "agent_id": agent_id,
                "span_name": "run",
                "kind": 1,
                "start_time_unix": now - 60 * NS,
                "end_time_unix": now,
                "status_code": 0,
                "status_message": "",
                "attributes": {},
                "resource_attributes": {},
            }
        ],
        account_id=account_id,
    )


acme = signup("founder@acme.test", "Acme")
acme_id = acme["org"]["id"]
FOUNDER = acme["token"]

levels = {l["key"]: l for l in client.get("/org/scope-levels", headers=auth(FOUNDER)).json()}
ceo = client.post(
    "/org/roles", json={"title": "CEO", "scope_level_id": levels["exec"]["id"]},
    headers=auth(FOUNDER),
).json()
support = client.post(
    "/org/roles",
    json={
        "title": "Support Lead",
        "parent_role_id": ceo["id"],
        "scope_level_id": levels["manager"]["id"],
    },
    headers=auth(FOUNDER),
).json()

sarah = invite_accept(FOUNDER, "sarah@acme.test", "Sarah Chen", role_id=support["id"])
sarah_id = sarah["user"]["id"]

seed_agent(acme_id, "billing-agent")
seed_agent(acme_id, "triage-agent")


# ---------------------------------------------------------------------------
# 1. Ownership is a users assignment
# ---------------------------------------------------------------------------
print("\nOwning an agent")

r = client.put(
    "/agents/billing-agent/owner",
    json={"agent_id": "main", "user_id": sarah_id},
    headers=auth(FOUNDER),
)
check("assign an org member as owner", r.status_code == 204)

owner = database.get_agent_owner(acme_id, "billing-agent", "main")
check("owner resolves to the user", owner["user_id"] == sarah_id)
check("...by name", owner["name"] == "Sarah Chen")
check("...with their email", owner["email"] == "sarah@acme.test")
check(
    "...and the role shown is their place on the CHART, not owner/member",
    owner["role"] == "Support Lead",
)
check("no legacy directory row is involved", owner["team_member_id"] is None)

summary = client.get("/agents/billing-agent/summary", headers=auth(FOUNDER)).json()
check("the agent summary agrees", summary["owner_name"] == "Sarah Chen")
check("...on the role", summary["owner_role"] == "Support Lead")
check("...and owner_id is the user id", summary["owner_id"] == sarah_id)

groups = {g["service_name"]: g for g in client.get("/agents", headers=auth(FOUNDER)).json()}
check(
    "the fleet list agrees — one resolver, every surface",
    groups["billing-agent"]["owner_name"] == "Sarah Chen"
    and groups["billing-agent"]["owner_role"] == "Support Lead",
)
check(
    "an unowned agent still shows no owner",
    groups["triage-agent"]["owner_name"] is None,
)

r = client.get("/org/members/%d/agents" % sarah_id, headers=auth(FOUNDER)).json()
check("their agents list back", [a["service_name"] for a in r] == ["billing-agent"])

# The roster marks which agents nobody owns, and it counts per SUB-AGENT
# because that is the unit an owner is assigned to. That only works if
# /agents carries owner_name down at the sub-agent level, not just on the
# instance — an instance-only field would collapse a gateway with five gaps
# into one, and the count would stop moving as they were filled.
# The sub-agent id rides on the span's attributes (`trovis.agent.id`), not on
# a top-level field — seed_agent's agent_id argument names the row, not the
# grouping key, so set the attribute the ingest path actually reads.
def seed_sub_agent(account_id, service, sub):
    _seq[0] += 1
    now = time.time_ns()
    database.insert_spans(
        [
            {
                "trace_id": f"t{_seq[0]}",
                "span_id": f"s{_seq[0]}",
                "parent_span_id": None,
                "service_name": service,
                "agent_id": sub,
                "span_name": "run",
                "kind": 1,
                "start_time_unix": now - 60 * NS,
                "end_time_unix": now,
                "status_code": 0,
                "status_message": "",
                "attributes": {"trovis.agent.id": sub},
                "resource_attributes": {},
            }
        ],
        account_id=account_id,
    )


seed_sub_agent(acme_id, "gateway", "router")
seed_sub_agent(acme_id, "gateway", "scorer")
client.put(
    "/agents/gateway/owner",
    json={"agent_id": "router", "user_id": sarah_id},
    headers=auth(FOUNDER),
)
gw = {
    g["service_name"]: g for g in client.get("/agents", headers=auth(FOUNDER)).json()
}["gateway"]
subs = {a["agent_id"]: a for a in gw["agents"]}
check("a sub-agent carries its own owner", subs["router"]["owner_name"] == "Sarah Chen")
check("...and its unowned sibling carries none", subs["scorer"]["owner_name"] is None)
check(
    "a sub-agent also reports whether it is locked",
    "locked" in subs["scorer"],
)

# Moving them on the chart moves the label with them.
client.post(
    "/org/roles/%d/members" % ceo["id"], json={"user_id": sarah_id}, headers=auth(FOUNDER)
)
check(
    "the role label follows the person around the chart",
    database.get_agent_owner(acme_id, "billing-agent", "main")["role"] == "CEO",
)
client.post(
    "/org/roles/%d/members" % support["id"], json={"user_id": sarah_id},
    headers=auth(FOUNDER),
)

r = client.put(
    "/agents/billing-agent/owner",
    json={"agent_id": "main", "user_id": acme["user"]["id"]},
    headers=auth(FOUNDER),
)
check("re-assigning overwrites", r.status_code == 204)
check(
    "...to the new person",
    database.get_agent_owner(acme_id, "billing-agent", "main")["user_id"]
    == acme["user"]["id"],
)
client.put(
    "/agents/billing-agent/owner",
    json={"agent_id": "main", "user_id": sarah_id},
    headers=auth(FOUNDER),
)

r = client.put(
    "/agents/billing-agent/owner", json={"agent_id": "main"}, headers=auth(FOUNDER)
)
check("an assignment with no person is refused", r.status_code == 400)
r = client.put(
    "/agents/billing-agent/owner",
    json={"agent_id": "main", "user_id": sarah_id, "team_member_id": 1},
    headers=auth(FOUNDER),
)
check("...and so is one naming both", r.status_code == 400)


# ---------------------------------------------------------------------------
# 2. Legacy rows keep resolving
# ---------------------------------------------------------------------------
print("\nLegacy rows")

# A directory person with no login. This is still how a handoff target who
# never signs in gets a name — see _resolve_human_name — so the read path
# must keep working even though nothing in the product writes here now.
legacy = legacy_directory_row(acme_id, "Pat Nolan", "pat@contractor.test", "Contractor")
r = client.put(
    "/agents/triage-agent/owner",
    json={"agent_id": "main", "team_member_id": legacy["id"]},
    headers=auth(FOUNDER),
)
check("a legacy directory person can still own an agent", r.status_code == 204)
owner = database.get_agent_owner(acme_id, "triage-agent", "main")
check("...and resolves by name", owner["name"] == "Pat Nolan")
check("...keeping their directory job title", owner["role"] == "Contractor")
check("...with no user id, since they have no login", owner["user_id"] is None)
summary = client.get("/agents/triage-agent/summary", headers=auth(FOUNDER)).json()
check("the same name on the summary", summary["owner_name"] == "Pat Nolan")

# The pre-migration shape: a row pointing only at team_member_id, for someone
# who DOES have a login. init_db backfills it by email.
noah = invite_accept(FOUNDER, "noah@acme.test", "Noah Reed", role_id=support["id"])
noah_tm = legacy_directory_row(acme_id, "Noah R", "noah@acme.test", "Support")
seed_agent(acme_id, "legacy-agent")
with database._connect() as conn, database._cursor(conn) as cur:
    cur.execute(
        "INSERT INTO agent_owners (account_id, service_name, agent_id, team_member_id) "
        f"VALUES ({database.PH}, {database.PH}, {database.PH}, {database.PH})",
        (acme_id, "legacy-agent", "main", noah_tm["id"]),
    )
check(
    "before the migration it resolves through the directory",
    database.get_agent_owner(acme_id, "legacy-agent", "main")["name"] == "Noah R",
)
database.init_db()  # idempotent — runs the backfill again
with database._connect() as conn, database._cursor(conn) as cur:
    cur.execute(
        f"SELECT user_id FROM agent_owners WHERE service_name = {database.PH}",
        ("legacy-agent",),
    )
    backfilled = cur.fetchone()["user_id"]
check("the backfill matches the login by email", backfilled == noah["user"]["id"])
owner = database.get_agent_owner(acme_id, "legacy-agent", "main")
check("...and it now resolves through users", owner["user_id"] == noah["user"]["id"])
check("...to the login's name", owner["name"] == "Noah Reed")
check("...and the chart role", owner["role"] == "Support Lead")
check(
    "a directory person with no matching login is left alone",
    database.get_agent_owner(acme_id, "triage-agent", "main")["user_id"] is None,
)


# ---------------------------------------------------------------------------
# 3. Handoff names still resolve (the Work/Home surface)
# ---------------------------------------------------------------------------
print("\nHandoff names")

with database._connect() as conn, database._cursor(conn) as cur:
    check(
        "a login resolves by email",
        database._resolve_human_name(cur, "sarah@acme.test", acme_id) == "Sarah Chen",
    )
    check(
        "a login resolves by user id",
        database._resolve_human_name(cur, str(sarah_id), acme_id) == "Sarah Chen",
    )
    check(
        "a directory person with no login still resolves",
        database._resolve_human_name(cur, "pat@contractor.test", acme_id) == "Pat Nolan",
    )
    # The contract that stops any email an agent emits from rendering as a
    # colleague. Without it, a cross-org address would show as a person.
    check(
        "an unknown email gains no name",
        database._resolve_human_name(cur, "nobody@else.test", acme_id) is None,
    )


# ---------------------------------------------------------------------------
# 4. Tenant isolation
# ---------------------------------------------------------------------------
print("\nTenant isolation")

other = signup("founder@other.test", "Other Co")
other_id = other["org"]["id"]
OTHER = other["token"]
other_tm = legacy_directory_row(other_id, "Their Person", "them@other.test", "Ops")

r = client.put(
    "/agents/billing-agent/owner",
    json={"agent_id": "main", "user_id": other["user"]["id"]},
    headers=auth(FOUNDER),
)
check("cannot own our agent with another org's user", r.status_code == 400)
r = client.put(
    "/agents/billing-agent/owner",
    json={"agent_id": "main", "team_member_id": other_tm["id"]},
    headers=auth(FOUNDER),
)
check("cannot own our agent with another org's directory person", r.status_code == 400)
check(
    "...and the real owner is untouched",
    database.get_agent_owner(acme_id, "billing-agent", "main")["user_id"] == sarah_id,
)

r = client.get("/org/members/%d/agents" % other["user"]["id"], headers=auth(FOUNDER))
check("another org's member is a 404, not a listing", r.status_code == 404)
check(
    "another org never sees our owner",
    database.get_agent_owner(other_id, "billing-agent", "main") is None,
)
with database._connect() as conn, database._cursor(conn) as cur:
    check(
        "a cross-org email resolves to no name",
        database._resolve_human_name(cur, "sarah@acme.test", other_id) is None,
    )


# ---------------------------------------------------------------------------
# 5. A departed person owns nothing
# ---------------------------------------------------------------------------
print("\nLeaving")

seed_agent(acme_id, "noah-agent")
client.put(
    "/agents/noah-agent/owner",
    json={"agent_id": "main", "user_id": noah["user"]["id"]},
    headers=auth(FOUNDER),
)
check(
    "Noah owns an agent",
    database.get_agent_owner(acme_id, "noah-agent", "main")["user_id"]
    == noah["user"]["id"],
)
r = client.delete("/org/members/%d" % noah["user"]["id"], headers=auth(FOUNDER))
check("removing them from the org succeeds", r.status_code == 204)
check(
    "the agent is unowned, not owned by a login that no longer exists",
    database.get_agent_owner(acme_id, "noah-agent", "main") is None,
)
summary = client.get("/agents/noah-agent/summary", headers=auth(FOUNDER)).json()
check("and the summary shows no owner", summary["owner_name"] is None)
check(
    "their chart seat is vacated too",
    database.get_role_id_for_user(acme_id, noah["user"]["id"]) is None,
)


# ---------------------------------------------------------------------------
# 6. One writer left, and it is marked as legacy
# ---------------------------------------------------------------------------
print("\nOne truth")

# Straight from the app: /openapi.json sits behind the auth middleware.
schema = main.app.openapi()
check(
    "POST /team is closed, not merely discouraged",
    schema["paths"]["/team"]["post"].get("deprecated") is True
    and client.post(
        "/team", json={"name": "Nope", "email": "nope@acme.test"},
        headers=auth(FOUNDER),
    ).status_code == 410,
)
check(
    "so is the old per-directory-person agent list",
    schema["paths"]["/team/{member_id}/agents"]["get"].get("deprecated") is True,
)
check(
    "and the org route that replaces it is not",
    schema["paths"]["/org/members/{user_id}/agents"]["get"].get("deprecated") is not True,
)
# One resolver behind every owner read. Three call sites used to hand-roll
# this join, which is how the same agent could show an owner on one page and
# none on another.
src = open("database.py").read()
check(
    "every owner read goes through the shared join",
    src.count("JOIN team_members m ON m.id = o.team_member_id") == 1
    and src.count("_OWNER_JOIN_SQL") >= 4,
)


print()
if failures:
    print(f"{len(failures)} FAILED:")
    for f in failures:
        print("  - " + f)
    raise SystemExit(1)
print("all passed")
