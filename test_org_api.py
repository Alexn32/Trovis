"""The Org API: who may redraw the chart, who may invite, and graduating
a one-seat workspace into a company.

PR 1 proved the seat *resolves* correctly. This proves nobody can reach past
their rung to change it — which is the half that matters, because the client
is going to be told what it may do (RolePublic.can_edit) and a client is not
an authority.

Four things have to hold:

  1. The two rungs are different shapes and both are enforced server-side.
     A manager edits BELOW their own box (never their own — that would let
     them re-parent themselves under the CEO) but may ADD under their own
     box, because growing your own team is the ordinary case.

  2. An invite is a chart edit in disguise: accepting one seats a person, and
     an UNPLACED person falls back to the full seat. So a subtree manager
     minting a role-less invite would hand out more access than they hold —
     that path stays owner/builder-only, and the placement happens in the
     same transaction that creates the user.

  3. Deletes don't quietly widen anyone. Removing an occupied box would
     unplace its occupant and hand them the fallback seat, so it refuses;
     removing an empty one moves its reports UP rather than orphaning them.

  4. Graduation is additive. Agents, jobs and API keys hang off account_id,
     which graduation never touches, and running it twice changes nothing.

Run:
  OVERSEE_DISABLE_PRICING_SYNC=1 python3 test_org_api.py
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


def signup(email, org_name, account_type="business"):
    r = client.post(
        "/auth/signup",
        json={
            "email": email,
            "password": "correct horse battery",
            "name": email.split("@")[0],
            "account_type": account_type,
            "org_name": org_name,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def invite_accept(owner_token, email, role_id=None, expect=201):
    body = {"email": email, "role": "member"}
    if role_id is not None:
        body["role_id"] = role_id
    r = client.post("/org/invites", json=body, headers=auth(owner_token))
    if r.status_code != expect:
        return r
    if r.status_code != 201:
        return r
    token = r.json()["invite_url"].split("token=")[1]
    a = client.post(
        "/auth/accept-invite",
        json={
            "token": token,
            "name": email.split("@")[0],
            "password": "correct horse battery",
        },
    )
    assert a.status_code == 201, a.text
    return a.json()


# ---------------------------------------------------------------------------
# Setup: Acme, chart drawn through the API by its builder
# ---------------------------------------------------------------------------
print("\nChart CRUD")

acme = signup("founder@acme.test", "Acme")
acme_id = acme["org"]["id"]
FOUNDER = acme["token"]

levels = client.get("/org/scope-levels", headers=auth(FOUNDER)).json()
by_key = {l["key"]: l for l in levels}
check("scope levels are readable over the API", len(levels) == 5)

r = client.post(
    "/org/roles",
    json={"title": "CEO", "scope_level_id": by_key["exec"]["id"]},
    headers=auth(FOUNDER),
)
check("builder creates a root role", r.status_code == 201)
ceo = r.json()

r = client.post(
    "/org/roles",
    json={
        "title": "Support Manager",
        "parent_role_id": ceo["id"],
        "scope_level_id": by_key["manager"]["id"],
    },
    headers=auth(FOUNDER),
)
check("builder creates a child role", r.status_code == 201)
mgr_role = r.json()

r = client.post(
    "/org/roles",
    json={
        "title": "Support IC",
        "parent_role_id": mgr_role["id"],
        "scope_level_id": by_key["ic"]["id"],
    },
    headers=auth(FOUNDER),
)
ic_role = r.json()

# A second branch, so "sibling" is a real thing to be refused.
sales_role = client.post(
    "/org/roles",
    json={
        "title": "Sales Manager",
        "parent_role_id": ceo["id"],
        "scope_level_id": by_key["manager"]["id"],
    },
    headers=auth(FOUNDER),
).json()

mgr = invite_accept(FOUNDER, "manager@acme.test", role_id=mgr_role["id"])
ic = invite_accept(FOUNDER, "ic@acme.test", role_id=ic_role["id"])
client.post(
    "/org/roles/%d/members" % ceo["id"],
    json={"user_id": acme["user"]["id"]},
    headers=auth(FOUNDER),
)
MGR, IC = mgr["token"], ic["token"]

check(
    "accepting a role invite seats the person",
    database.get_role_id_for_user(acme_id, mgr["user"]["id"]) == mgr_role["id"],
)
seat = client.get("/auth/me", headers=auth(MGR)).json()["seat"]
check("...and their seat resolves from that role", seat["breadth"] == "subtree")
check("...naming the role they landed in", seat["role_title"] == "Support Manager")

ic_seat = client.get("/auth/me", headers=auth(IC)).json()["seat"]
check("the IC's invite gave them the IC seat", ic_seat["breadth"] == "self")

r = client.patch(
    "/org/roles/%d" % ic_role["id"], json={"title": "Support Specialist"},
    headers=auth(FOUNDER),
)
check("builder renames a role", r.status_code == 200 and r.json()["title"] == "Support Specialist")


# ---------------------------------------------------------------------------
# The two rungs, over HTTP
# ---------------------------------------------------------------------------
print("\nAdmin ladder over HTTP")

r = client.patch(
    "/org/roles/%d" % ic_role["id"], json={"title": "Renamed by manager"},
    headers=auth(MGR),
)
check("manager edits a role below them", r.status_code == 200)

r = client.patch(
    "/org/roles/%d" % mgr_role["id"], json={"title": "Chief Everything"},
    headers=auth(MGR),
)
check("manager cannot edit their OWN box", r.status_code == 403)

r = client.patch(
    "/org/roles/%d" % ceo["id"], json={"title": "Nope"}, headers=auth(MGR)
)
check("manager cannot edit their manager's box", r.status_code == 403)

r = client.patch(
    "/org/roles/%d" % sales_role["id"], json={"title": "Nope"}, headers=auth(MGR)
)
check("manager cannot edit a sibling's box", r.status_code == 403)

r = client.post(
    "/org/roles", json={"title": "New report", "parent_role_id": mgr_role["id"]},
    headers=auth(MGR),
)
check("manager CAN add a box under their own (grow their team)", r.status_code == 201)
new_report = r.json()

r = client.post(
    "/org/roles", json={"title": "Land grab", "parent_role_id": sales_role["id"]},
    headers=auth(MGR),
)
check("manager cannot add a box under a sibling", r.status_code == 403)

r = client.post("/org/roles", json={"title": "Co-CEO"}, headers=auth(MGR))
check("manager cannot add a top-level role", r.status_code == 403)

r = client.post("/org/roles", json={"title": "Anything"}, headers=auth(IC))
check("IC with no reports adds nothing", r.status_code == 403)

# Reparenting needs rights on both ends.
r = client.patch(
    "/org/roles/%d" % ic_role["id"], json={"parent_role_id": sales_role["id"]},
    headers=auth(MGR),
)
check(
    "manager cannot push their report into someone else's team",
    r.status_code == 403,
)
r = client.patch(
    "/org/roles/%d" % ic_role["id"], json={"parent_role_id": new_report["id"]},
    headers=auth(MGR),
)
check("manager reparents inside their own subtree", r.status_code == 200)
r = client.patch(
    "/org/roles/%d" % ic_role["id"], json={"parent_role_id": mgr_role["id"]},
    headers=auth(MGR),
)
check("...and back", r.status_code == 200)

r = client.patch(
    "/org/roles/%d" % ic_role["id"], json={"parent_role_id": None}, headers=auth(MGR)
)
check("manager cannot detach a role to the root", r.status_code == 403)

# Cycles.
r = client.patch(
    "/org/roles/%d" % ceo["id"], json={"parent_role_id": ic_role["id"]},
    headers=auth(FOUNDER),
)
check("a role cannot report to its own descendant", r.status_code == 400)
r = client.patch(
    "/org/roles/%d" % ceo["id"], json={"parent_role_id": ceo["id"]},
    headers=auth(FOUNDER),
)
check("a role cannot report to itself", r.status_code == 400)

# A PATCH that only renames must not silently strip the role's seat.
before = database.get_role(acme_id, ic_role["id"])["scope_level_id"]
client.patch(
    "/org/roles/%d" % ic_role["id"], json={"title": "Support IC"}, headers=auth(FOUNDER)
)
check(
    "renaming a role leaves its scope level alone",
    database.get_role(acme_id, ic_role["id"])["scope_level_id"] == before,
)
client.patch(
    "/org/roles/%d" % ic_role["id"], json={"scope_level_id": None}, headers=auth(FOUNDER)
)
check(
    "an explicit null detaches the scope level",
    database.get_role(acme_id, ic_role["id"])["scope_level_id"] is None,
)
client.patch(
    "/org/roles/%d" % ic_role["id"],
    json={"scope_level_id": by_key["ic"]["id"]},
    headers=auth(FOUNDER),
)


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------
print("\nDelete")

r = client.delete("/org/roles/%d" % ic_role["id"], headers=auth(MGR))
check("cannot delete a role someone is standing in", r.status_code == 409)
check(
    "...and the person is still seated",
    database.get_role_id_for_user(acme_id, ic["user"]["id"]) == ic_role["id"],
)

# Empty middle box: its reports move UP, they are not orphaned.
client.patch(
    "/org/roles/%d" % ic_role["id"], json={"parent_role_id": new_report["id"]},
    headers=auth(MGR),
)
r = client.delete("/org/roles/%d" % new_report["id"], headers=auth(MGR))
check("deleting an empty box below you works", r.status_code == 204)
check(
    "...and its reports move up to its parent",
    database.get_role(acme_id, ic_role["id"])["parent_role_id"] == mgr_role["id"],
)
r = client.delete("/org/roles/%d" % mgr_role["id"], headers=auth(MGR))
check("cannot delete your own box", r.status_code == 403)


# ---------------------------------------------------------------------------
# Chart visibility
# ---------------------------------------------------------------------------
print("\nChart visibility")

full_chart = client.get("/org/chart", headers=auth(FOUNDER)).json()
check("builder sees the whole chart", len(full_chart["roles"]) == 4)
check("builder is flagged as one", full_chart["org_builder"] is True)

mgr_chart = client.get("/org/chart", headers=auth(MGR)).json()
mgr_titles = {r["title"] for r in mgr_chart["roles"]}
check("manager sees their own box", "Support Manager" in mgr_titles)
check("manager sees the chain above them (manager context)", "CEO" in mgr_titles)
check("manager sees their subtree", "Support IC" in mgr_titles)
check("manager does NOT see a sibling's team", "Sales Manager" not in mgr_titles)
check(
    "the chart marks what the manager may edit",
    {r["title"]: r["can_edit"] for r in mgr_chart["roles"]}
    == {"CEO": False, "Support Manager": False, "Support IC": True},
)
check(
    "...and where they may add",
    {r["title"]: r["can_add_child"] for r in mgr_chart["roles"]}
    == {"CEO": False, "Support Manager": True, "Support IC": True},
)
check(
    "the chart is not a back door onto the member directory",
    {m["email"] for m in mgr_chart["members"]}
    == {"founder@acme.test", "manager@acme.test", "ic@acme.test"},
)

ic_chart = client.get("/org/chart", headers=auth(IC)).json()
ic_titles = {r["title"] for r in ic_chart["roles"]}
check("IC sees their place plus manager context", ic_titles == {"CEO", "Support Manager", "Support IC"})
check("IC may edit nothing on it", all(not r["can_edit"] for r in ic_chart["roles"]))

key_chart = client.get("/org/chart", headers={"X-Trovis-Api-Key": acme["api_key"]})
check("an API key has no rung on the ladder", key_chart.status_code == 401)


# ---------------------------------------------------------------------------
# Invites follow the same ladder
# ---------------------------------------------------------------------------
print("\nInvites")

r = client.post(
    "/org/invites",
    json={"email": "poach@acme.test", "role_id": sales_role["id"]},
    headers=auth(MGR),
)
check("manager cannot invite into a role outside their subtree", r.status_code == 403)

r = client.post(
    "/org/invites",
    json={"email": "poach@acme.test", "role_id": mgr_role["id"]},
    headers=auth(MGR),
)
check("manager cannot invite into their OWN box", r.status_code == 403)

r = client.post("/org/invites", json={"email": "loose@acme.test"}, headers=auth(MGR))
check(
    "manager cannot mint a role-less invite (it would grant the full seat)",
    r.status_code == 403,
)

r = client.post(
    "/org/invites",
    json={"email": "report@acme.test", "role_id": ic_role["id"]},
    headers=auth(MGR),
)
check("manager CAN invite into a role below them", r.status_code == 201)
check("...and the invite records the role", r.json()["role_id"] == ic_role["id"])

token = r.json()["invite_url"].split("token=")[1]
a = client.post(
    "/auth/accept-invite",
    json={"token": token, "name": "report", "password": "correct horse battery"},
)
check("accepting it creates the login", a.status_code == 201)
new_uid = a.json()["user"]["id"]
check(
    "...seated in the invited role",
    database.get_role_id_for_user(acme_id, new_uid) == ic_role["id"],
)
new_seat = client.get("/auth/me", headers=auth(a.json()["token"])).json()["seat"]
check("...inheriting that role's seat, not the fallback", new_seat["breadth"] == "self")
check("...and not an org builder", a.json()["user"]["org_builder"] is False)

r = client.post(
    "/org/invites",
    json={"email": "anyone@acme.test", "role_id": sales_role["id"]},
    headers=auth(FOUNDER),
)
check("builder invites anywhere", r.status_code == 201)
sales_invite_id = None
for i in client.get("/org/invites", headers=auth(FOUNDER)).json():
    if i["email"] == "anyone@acme.test":
        sales_invite_id = i["id"]
check("builder sees every pending invite", sales_invite_id is not None)
mgr_invites = client.get("/org/invites", headers=auth(MGR)).json()
check(
    "manager sees only invites into roles they own",
    all(i["role_id"] == ic_role["id"] for i in mgr_invites),
)
r = client.delete("/org/invites/%d" % sales_invite_id, headers=auth(MGR))
check("manager cannot revoke an invite they can't see", r.status_code == 404)
check(
    "...and it is still pending",
    any(i["id"] == sales_invite_id for i in client.get("/org/invites", headers=auth(FOUNDER)).json()),
)
r = client.delete("/org/invites/%d" % sales_invite_id, headers=auth(FOUNDER))
check("builder revokes it", r.status_code == 204)


# ---------------------------------------------------------------------------
# Org builder grant
# ---------------------------------------------------------------------------
print("\nOrg builder grant")

mgr_uid, ic_uid = mgr["user"]["id"], ic["user"]["id"]

r = client.put(
    "/org/members/%d/org-builder" % ic_uid, json={"org_builder": True},
    headers=auth(MGR),
)
check("a non-builder cannot grant org builder", r.status_code == 403)

r = client.put(
    "/org/members/%d/org-builder" % mgr_uid, json={"org_builder": True},
    headers=auth(FOUNDER),
)
check("a builder grants org builder", r.status_code == 200)
check("...and it shows on the user", r.json()["org_builder"] is True)
r = client.patch(
    "/org/roles/%d" % ceo["id"], json={"title": "Chief Exec"}, headers=auth(MGR)
)
check("the new builder can now edit the whole chart", r.status_code == 200)

r = client.put(
    "/org/members/%d/org-builder" % mgr_uid, json={"org_builder": False},
    headers=auth(FOUNDER),
)
check("a builder revokes org builder", r.status_code == 200)
r = client.patch(
    "/org/roles/%d" % ceo["id"], json={"title": "CEO"}, headers=auth(MGR)
)
check("...and the rung is gone again", r.status_code == 403)

r = client.put(
    "/org/members/%d/org-builder" % acme["user"]["id"], json={"org_builder": False},
    headers=auth(FOUNDER),
)
check("refuses to remove the LAST org builder", r.status_code == 409)
check(
    "...so the org can still edit its own chart",
    database.count_org_builders(acme_id) == 1,
)


# ---------------------------------------------------------------------------
# Custom scope levels compose atoms only
# ---------------------------------------------------------------------------
print("\nCustom scope levels")

r = client.post(
    "/org/scope-levels",
    json={
        "name": "Support Lead",
        "breadth": "subtree",
        "depth": "glance",
        "surfaces": ["Home", "Work", "Telepathy"],
    },
    headers=auth(FOUNDER),
)
check("builder creates a custom level", r.status_code == 201)
check("...dropping a surface the product doesn't have", r.json()["surfaces"] == ["Home", "Work"])
check("...and it is not a preset", r.json()["is_preset"] is False)

for bad in ({"breadth": "everything"}, {"depth": "xray"}):
    body = {"name": "Bad " + list(bad)[0], "breadth": "self", "depth": "glance",
            "surfaces": ["Home"], **bad}
    r = client.post("/org/scope-levels", json=body, headers=auth(FOUNDER))
    check(f"rejects an invented {list(bad)[0]}", r.status_code == 400)

r = client.post(
    "/org/scope-levels",
    json={"name": "Sneaky", "breadth": "self", "depth": "glance", "surfaces": ["Home"]},
    headers=auth(MGR),
)
check("a non-builder cannot define an org-wide level", r.status_code == 403)


# ---------------------------------------------------------------------------
# Path A → B graduation
# ---------------------------------------------------------------------------
print("\nGraduation")

solo = signup("solo@indie.test", None, account_type="individual")
solo_id = solo["org"]["id"]
SOLO = solo["token"]

# Give the workspace something to lose: an agent, its spans, and its key.
NS = 1_000_000_000
now = time.time_ns()
database.insert_spans(
    [
        {
            "trace_id": "t1",
            "span_id": "s1",
            "parent_span_id": None,
            "service_name": "indie-agent",
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
    account_id=solo_id,
)
agents_before = client.get("/agents", headers=auth(SOLO)).json()
keys_before = client.get("/org/api-keys", headers=auth(SOLO)).json()["keys"]
check("the solo workspace has an agent", len(agents_before) == 1)
check("...and an API key", len(keys_before) == 1)

r = client.post(
    "/org/graduate", json={"org_name": "Indie Co", "root_role_title": "Founder"},
    headers=auth(SOLO),
)
check("graduation succeeds", r.status_code == 200)
g = r.json()
check("the account is now a business", g["org"]["account_type"] == "business")
check("...named as asked", g["org"]["name"] == "Indie Co")
check("a root role was created", g["created_root_role"] is True)
check("...and the founder placed in it", g["placed_founder"] is True)

check(
    "the agent survived",
    client.get("/agents", headers=auth(SOLO)).json() == agents_before,
)
check(
    "the API key survived",
    client.get("/org/api-keys", headers=auth(SOLO)).json()["keys"] == keys_before,
)
check(
    "OTLP ingest still works after graduating",
    client.post(
        "/v1/traces",
        json={"resourceSpans": []},
        headers={"X-Trovis-Api-Key": solo["api_key"]},
    ).status_code
    in (200, 201, 202),
)

solo_seat = client.get("/auth/me", headers=auth(SOLO)).json()["seat"]
check("the founder now has a role", solo_seat["role_title"] == "Founder")
check("...and is an org builder", solo_seat["org_builder"] is True)
check("...and can edit the chart", solo_seat["can_edit_chart"] is True)

r2 = client.post("/org/graduate", json={}, headers=auth(SOLO))
check("graduating twice is idempotent", r2.status_code == 200)
check("...no second root role", r2.json()["created_root_role"] is False)
check("...the founder is not re-seated", r2.json()["placed_founder"] is False)
check(
    "...and there is still exactly one root",
    len([r for r in database.get_roles(solo_id) if r["parent_role_id"] is None]) == 1,
)

# Graduation must not re-seat someone who has since moved down the chart.
deep = client.post(
    "/org/roles", json={"title": "Deep", "parent_role_id": g["root_role"]["id"]},
    headers=auth(SOLO),
).json()
client.post(
    "/org/roles/%d/members" % deep["id"], json={"user_id": solo["user"]["id"]},
    headers=auth(SOLO),
)
client.post("/org/graduate", json={}, headers=auth(SOLO))
check(
    "graduation never silently promotes a moved founder",
    database.get_role_id_for_user(solo_id, solo["user"]["id"]) == deep["id"],
)

r = client.post("/org/graduate", json={}, headers=auth(IC))
check("a plain member cannot graduate the org", r.status_code == 403)


# ---------------------------------------------------------------------------
# Tenant isolation (IDOR)
# ---------------------------------------------------------------------------
print("\nTenant isolation")

other = signup("founder@other.test", "Other Co")
OTHER = other["token"]
other_role = client.post(
    "/org/roles", json={"title": "Their CEO"}, headers=auth(OTHER)
).json()
other_level = client.get("/org/scope-levels", headers=auth(OTHER)).json()[0]

for label, resp in (
    ("read", client.patch("/org/roles/%d" % other_role["id"], json={"title": "Mine now"},
                          headers=auth(FOUNDER))),
    ("delete", client.delete("/org/roles/%d" % other_role["id"], headers=auth(FOUNDER))),
    ("seat someone in", client.post("/org/roles/%d/members" % other_role["id"],
                                    json={"user_id": mgr_uid}, headers=auth(FOUNDER))),
    ("invite into", client.post("/org/invites",
                                json={"email": "x@acme.test", "role_id": other_role["id"]},
                                headers=auth(FOUNDER))),
):
    check(f"another org's role is a 404 on {label}", resp.status_code == 404)

r = client.post(
    "/org/roles", json={"title": "Graft", "parent_role_id": other_role["id"]},
    headers=auth(FOUNDER),
)
check("cannot parent a role under another org's role", r.status_code == 404)

r = client.post(
    "/org/roles/%d/members" % ceo["id"], json={"user_id": other["user"]["id"]},
    headers=auth(FOUNDER),
)
check("cannot seat another org's user in our chart", r.status_code == 404)

r = client.put(
    "/org/members/%d/org-builder" % other["user"]["id"], json={"org_builder": True},
    headers=auth(FOUNDER),
)
check("cannot grant org builder in another org", r.status_code == 404)

acme_titles = {r["title"] for r in client.get("/org/chart", headers=auth(FOUNDER)).json()["roles"]}
check("another org's roles never appear on our chart", "Their CEO" not in acme_titles)
check(
    "another org's scope levels are not ours",
    other_level["id"] not in {l["id"] for l in client.get("/org/scope-levels", headers=auth(FOUNDER)).json()},
)


print()
if failures:
    print(f"{len(failures)} FAILED:")
    for f in failures:
        print("  - " + f)
    raise SystemExit(1)
print("all passed")
