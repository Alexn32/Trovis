"""Seats: what a person sees and can change.

A seat is never stored on the person. It is composed from a fixed set of
atoms, bundled into a named scope level, attached to a ROLE, and inherited by
whoever sits in that role:

    scope_levels  →  org_roles.scope_level_id  →  org_role_members.user_id

Four things have to hold before anything can be filtered by a seat:

  1. Presets exist on every org, seeded once and only once, composed only of
     real atoms. A custom level cannot invent a new axis.

  2. Inheritance resolves through the whole chain, and an unplaced person
     falls back to the FULL seat. Seats narrow an account that drew a chart;
     they must never take away access an org already had.

  3. The admin ladder is not the view ladder. An Org builder edits the chart
     anywhere; anyone else edits strictly below their own role — not their own
     box, not a sibling's. A company-breadth Exec who was never granted
     builder edits nothing.

  4. account_id holds. A role, a scope level, or a builder grant from another
     org is a miss, not a 403-shaped hint.

Run:
  OVERSEE_DISABLE_PRICING_SYNC=1 python3 test_org_seats.py
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

# Schema only — no lifespan, so no pricing sync / alert / sweep threads.
database.init_db()
client = TestClient(main.app)

failures = []


def check(label, cond):
    print(("  PASS " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


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


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def invite_and_accept(owner_token, email):
    """Add a second person to an org through the real invite path."""
    r = client.post(
        "/org/invites", json={"email": email, "role": "member"},
        headers=auth(owner_token),
    )
    assert r.status_code == 201, r.text
    token = r.json()["invite_url"].split("token=")[1]
    r = client.post(
        "/auth/accept-invite",
        json={"token": token, "name": email.split("@")[0], "password": "correct horse battery"},
    )
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------
# 1. Presets seed from the fixed atoms, once
# ---------------------------------------------------------------------------
print("\nPresets")

acme = signup("founder@acme.test", "Acme")
acme_id = acme["org"]["id"]

levels = database.get_scope_levels(acme_id)
by_key = {l["key"]: l for l in levels}
check(
    "five presets seeded on a new org",
    sorted(by_key) == ["exec", "ic", "manager", "middle_manager", "vp"],
)
check("presets are flagged as presets", all(l["is_preset"] for l in levels))
check(
    "every preset composes only real atoms",
    all(
        l["breadth"] in database.SCOPE_BREADTHS
        and l["depth"] in database.SCOPE_DEPTHS
        and set(l["surfaces"]) <= set(database.SURFACES)
        for l in levels
    ),
)
check("IC preset is self-breadth", by_key["ic"]["breadth"] == "self")
# A seat narrows WHOSE work you see. It was never meant to take the product
# away from the person who bought it: an Exec preset without Agents left a
# founder unable to see their own agents, and without Connect unable to wire
# one up while an IC could.
check(
    "every preset can reach Agents",
    all("Fleet" in l["surfaces"] for l in levels),
)
check(
    "every preset can connect an agent",
    all("Connect" in l["surfaces"] for l in levels),
)
check(
    "depth is a person's preference, not a rank — every preset is technical",
    all(l["depth"] == "technical" for l in levels),
)
# Cost is the one surface that really does differ by role, and is unchanged.
check(
    "Cost stays with Exec, VP and Manager",
    all("Cost" in by_key[k]["surfaces"] for k in ("exec", "vp", "manager")),
)
check(
    "...and off Middle manager and IC",
    all("Cost" not in by_key[k]["surfaces"] for k in ("middle_manager", "ic")),
)
check("Manager preset is subtree-breadth", by_key["manager"]["breadth"] == "subtree")
check("Exec preset is company-breadth", by_key["exec"]["breadth"] == "company")

database.ensure_scope_level_presets(acme_id)
database.ensure_scope_level_presets(acme_id)
check(
    "re-seeding is idempotent (no duplicate presets)",
    len(database.get_scope_levels(acme_id)) == len(levels),
)

# A custom level composes atoms — it cannot introduce one.
custom = database.create_scope_level(
    acme_id, "support_lead", "Support lead", "subtree", "glance",
    ["Home", "Work", "Nonsense"],
)
check("custom level drops an unknown surface", custom["surfaces"] == ["Home", "Work"])
check("custom level is not a preset", custom["is_preset"] is False)

for bad in ({"breadth": "everything"}, {"depth": "xray"}):
    kwargs = {"breadth": "self", "depth": "glance", **bad}
    try:
        database.create_scope_level(
            acme_id, f"bad_{list(bad)[0]}", "Bad", surfaces=["Home"], **kwargs
        )
        check(f"invented {list(bad)[0]} rejected", False)
    except ValueError:
        check(f"invented {list(bad)[0]} rejected", True)



# ---------------------------------------------------------------------------
# 1b. Existing orgs move forward — and only the untouched rows
# ---------------------------------------------------------------------------
print("\nPreset migration")

# ensure_scope_level_presets only INSERTS missing keys, so changing
# SCOPE_LEVEL_PRESETS fixes nothing for an org that already has them. Without
# the boot migration every existing account would sit on an Exec seat with no
# Agents tab forever, no matter what the constant says.
migr = signup("founder@legacy.test", "Legacy Co")
legacy_id = migr["org"]["id"]


def _write_shape(account_id, key, depth, surfaces):
    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute(
            f"UPDATE scope_levels SET depth = {database.PH}, surfaces = {database.PH} "
            f"WHERE account_id = {database.PH} AND key = {database.PH}",
            (depth, __import__("json").dumps(surfaces), account_id, key),
        )


def _shape(account_id, key):
    lv = {l["key"]: l for l in database.get_scope_levels(account_id)}[key]
    return lv["depth"], set(lv["surfaces"])


# Roll three rows back to exactly the shapes that shipped before.
_write_shape(legacy_id, "exec", "glance", ["Home", "Work", "Ask", "Cost", "Org"])
_write_shape(legacy_id, "vp", "glance", ["Home", "Work", "Fleet", "Ask", "Cost", "Org"])
_write_shape(legacy_id, "middle_manager", "glance", ["Home", "Work", "Ask", "Org"])
# ...and edit one deliberately: a Manager preset this org trimmed on purpose.
_write_shape(legacy_id, "manager", "glance", ["Home", "Work"])

check(
    "before the migration, Exec cannot reach Agents",
    "Fleet" not in _shape(legacy_id, "exec")[1],
)

database.init_db()  # idempotent boot

for key in ("exec", "vp", "middle_manager"):
    depth, surfaces = _shape(legacy_id, key)
    check(f"{key} gains Agents", "Fleet" in surfaces)
    check(f"{key} gains Connect", "Connect" in surfaces)
    check(f"{key} is technical now", depth == "technical")
check(
    "...and Cost is neither granted nor taken by the migration",
    "Cost" in _shape(legacy_id, "exec")[1]
    and "Cost" in _shape(legacy_id, "vp")[1]
    and "Cost" not in _shape(legacy_id, "middle_manager")[1],
)
# The one that matters most: an org that trimmed a preset itself keeps its
# edit. Silently re-granting surfaces someone removed on purpose would be a
# worse bug than the one being fixed.
check(
    "a deliberately edited preset is left exactly alone",
    _shape(legacy_id, "manager") == ("glance", {"Home", "Work"}),
)

database.init_db()
check(
    "running the migration again changes nothing",
    _shape(legacy_id, "exec")[0] == "technical"
    and _shape(legacy_id, "manager") == ("glance", {"Home", "Work"}),
)
check(
    "a fresh org is untouched by it",
    _shape(acme_id, "exec")[0] == "technical",
)

# ---------------------------------------------------------------------------
# 2. Inheritance: scope level → role → person
# ---------------------------------------------------------------------------
print("\nInheritance")

# Acme's chart:  CEO ── VP Eng ── Manager ── IC
ceo_role = database.create_role(acme_id, "CEO", scope_level_id=by_key["exec"]["id"])
vp_role = database.create_role(
    acme_id, "VP Engineering", parent_role_id=ceo_role["id"],
    scope_level_id=by_key["vp"]["id"],
)
mgr_role = database.create_role(
    acme_id, "Support Manager", parent_role_id=vp_role["id"],
    scope_level_id=by_key["manager"]["id"],
)
ic_role = database.create_role(
    acme_id, "Support IC", parent_role_id=mgr_role["id"],
    scope_level_id=by_key["ic"]["id"],
)

founder_id = acme["user"]["id"]
mgr = invite_and_accept(acme["token"], "manager@acme.test")
ic = invite_and_accept(acme["token"], "ic@acme.test")
unplaced = invite_and_accept(acme["token"], "newhire@acme.test")

database.assign_user_to_role(acme_id, founder_id, ceo_role["id"])
database.assign_user_to_role(acme_id, mgr["user"]["id"], mgr_role["id"])
database.assign_user_to_role(acme_id, ic["user"]["id"], ic_role["id"])

ic_seat = database.resolve_seat(acme_id, ic["user"]["id"])
check("IC inherits the IC level's breadth", ic_seat["breadth"] == "self")
check("IC inherits the IC level's depth", ic_seat["depth"] == "technical")
check("IC seat names the role it came from", ic_seat["role_title"] == "Support IC")
check("IC has no reports", ic_seat["subtree_user_ids"] == [])
check("IC sees only their own work", ic_seat["visible_user_ids"] == [ic["user"]["id"]])
check("Fleet is in the IC's surfaces", "Fleet" in ic_seat["surfaces"])

mgr_seat = database.resolve_seat(acme_id, mgr["user"]["id"])
check("Manager inherits subtree breadth", mgr_seat["breadth"] == "subtree")
check(
    "Manager's subtree is the people below them",
    mgr_seat["subtree_user_ids"] == [ic["user"]["id"]],
)
check(
    "Manager sees themselves plus their subtree",
    mgr_seat["visible_user_ids"] == sorted([mgr["user"]["id"], ic["user"]["id"]]),
)

ceo_seat = database.resolve_seat(acme_id, founder_id)
check("CEO inherits company breadth", ceo_seat["breadth"] == "company")
check(
    "company breadth means no person filter at all",
    ceo_seat["visible_user_ids"] is None,
)
check(
    "CEO's subtree is everyone placed below them",
    ceo_seat["subtree_user_ids"] == sorted([mgr["user"]["id"], ic["user"]["id"]]),
)
check(
    "depth is not baked into a rank — every preset seat is technical",
    ceo_seat["depth"] == "technical",
)

# A role with no scope level attached, and a person with no role at all, both
# fall back to the full seat. Seats narrow; they never revoke.
naked_role = database.create_role(acme_id, "Contractor", parent_role_id=ceo_role["id"])
database.assign_user_to_role(acme_id, unplaced["user"]["id"], naked_role["id"])
fallback = database.resolve_seat(acme_id, unplaced["user"]["id"])
check("role without a scope level falls back to full breadth", fallback["breadth"] == "company")
check("...and full depth", fallback["depth"] == "technical")
check("...and every surface", fallback["surfaces"] == list(database.SURFACES))

solo = signup("solo@indie.test", None, account_type="individual")
solo_seat = database.resolve_seat(solo["org"]["id"], solo["user"]["id"])
check(
    "Path A workspace with no chart keeps the full seat",
    solo_seat["breadth"] == "company"
    and solo_seat["depth"] == "technical"
    and solo_seat["surfaces"] == list(database.SURFACES),
)

# Re-assigning moves a person; it never leaves them in two roles at once.
database.assign_user_to_role(acme_id, ic["user"]["id"], vp_role["id"])
check(
    "re-assigning moves the person",
    database.get_role_id_for_user(acme_id, ic["user"]["id"]) == vp_role["id"],
)
database.assign_user_to_role(acme_id, ic["user"]["id"], ic_role["id"])


# ---------------------------------------------------------------------------
# 3. /auth/me carries the resolved seat
# ---------------------------------------------------------------------------
print("\n/auth/me")

me = client.get("/auth/me", headers=auth(ic["token"])).json()
check("me returns a seat", me.get("seat") is not None)
check("me's seat is the IC's", me["seat"]["breadth"] == "self")
check("me's seat lists surfaces", "Work" in me["seat"]["surfaces"])
check("IC is not an org builder", me["user"]["org_builder"] is False)
check("IC cannot edit the chart", me["seat"]["can_edit_chart"] is False)

founder_me = client.get("/auth/me", headers=auth(acme["token"])).json()
check("the org creator is an Org builder", founder_me["user"]["org_builder"] is True)
check("...and can edit the chart", founder_me["seat"]["can_edit_chart"] is True)

# API-key auth is a machine credential — it has no seat to resolve.
key_me = client.get(
    "/auth/me", headers={"X-Trovis-Api-Key": acme["api_key"]}
).json()
check("API-key auth carries no seat", key_me.get("seat") is None)
check("API-key auth carries no user", key_me.get("user") is None)


# ---------------------------------------------------------------------------
# 4. Admin ladder: subtree edit vs Org builder
# ---------------------------------------------------------------------------
print("\nAdmin ladder")

mgr_uid = mgr["user"]["id"]
ic_uid = ic["user"]["id"]

check(
    "manager edits a role below them",
    database.can_edit_chart(acme_id, mgr_uid, ic_role["id"]) is True,
)
check(
    "manager cannot edit their OWN box",
    database.can_edit_chart(acme_id, mgr_uid, mgr_role["id"]) is False,
)
check(
    "manager cannot edit their manager's box",
    database.can_edit_chart(acme_id, mgr_uid, vp_role["id"]) is False,
)
check(
    "manager cannot edit a cousin box",
    database.can_edit_chart(acme_id, mgr_uid, naked_role["id"]) is False,
)
check(
    "IC with no reports edits nothing",
    database.can_edit_chart(acme_id, ic_uid, ic_role["id"]) is False
    and database.can_edit_chart(acme_id, ic_uid) is False,
)
check(
    "org builder edits the root of the chart",
    database.can_edit_chart(acme_id, founder_id, ceo_role["id"]) is True,
)

# A wide VIEW is not an admin grant. Give the IC an Exec seat (company
# breadth, sees everything) and they still cannot redraw a single box.
database.set_role_scope_level(acme_id, ic_role["id"], by_key["exec"]["id"])
wide_ic = database.resolve_seat(acme_id, ic_uid)
check("IC now sees the whole company", wide_ic["breadth"] == "company")
check(
    "...and still cannot edit the chart (view ≠ admin)",
    wide_ic["can_edit_chart"] is False,
)
database.set_role_scope_level(acme_id, ic_role["id"], by_key["ic"]["id"])

# Org builder is granted, not inherited from a seat.
check("granting org builder", database.set_org_builder(acme_id, ic_uid, True) is True)
check(
    "a granted builder edits anywhere",
    database.can_edit_chart(acme_id, ic_uid, ceo_role["id"]) is True,
)
check(
    "...and their seat says so",
    database.resolve_seat(acme_id, ic_uid)["org_builder"] is True,
)
database.set_org_builder(acme_id, ic_uid, False)
check(
    "revoking org builder puts the ladder back",
    database.can_edit_chart(acme_id, ic_uid, ceo_role["id"]) is False,
)


# ---------------------------------------------------------------------------
# 5. Tenant isolation (IDOR)
# ---------------------------------------------------------------------------
print("\nTenant isolation")

other = signup("founder@other.test", "Other Co")
other_id = other["org"]["id"]
other_role = database.create_role(other_id, "Their CEO")

check(
    "another org's role is a miss, not a read",
    database.get_role(acme_id, other_role["id"]) is None,
)
other_level = database.get_scope_levels(other_id)[0]
check(
    "another org's scope level is a miss",
    database.get_scope_level(acme_id, other_level["id"]) is None,
)
check(
    "acme's roles never include another org's",
    other_role["id"] not in [r["id"] for r in database.get_roles(acme_id)],
)
check(
    "an acme builder cannot edit another org's chart",
    database.can_edit_chart(other_id, founder_id, other_role["id"]) is False,
)
check(
    "an acme builder cannot promote another org's user",
    database.set_org_builder(acme_id, other["user"]["id"], True) is False,
)
check(
    "...and that user's own-org builder flag is untouched by the attempt",
    database.get_user_by_id(other["user"]["id"])["org_builder"] is True,  # from signup
)

for label, fn in (
    ("seat a foreign user in our role", lambda: database.assign_user_to_role(
        acme_id, other["user"]["id"], ceo_role["id"])),
    ("seat our user in a foreign role", lambda: database.assign_user_to_role(
        acme_id, ic_uid, other_role["id"])),
    ("parent a role under a foreign role", lambda: database.create_role(
        acme_id, "Sneaky", parent_role_id=other_role["id"])),
    ("attach a foreign scope level", lambda: database.create_role(
        acme_id, "Sneaky2", scope_level_id=other_level["id"])),
):
    try:
        fn()
        check(f"refuses to {label}", False)
    except ValueError:
        check(f"refuses to {label}", True)

check(
    "another org's people never land in our subtree",
    other["user"]["id"] not in database.resolve_seat(acme_id, founder_id)["subtree_user_ids"],
)


# ---------------------------------------------------------------------------
# 6. A cycle in the chart terminates instead of hanging
# ---------------------------------------------------------------------------
print("\nMalformed chart")

# create_role/get_role guard the tree, but a hand-edited DB could still hold a
# cycle. Walking one must stop, not spin — this runs inside a request.
with database._connect() as conn, database._cursor(conn) as cur:
    cur.execute(
        f"UPDATE org_roles SET parent_role_id = {database.PH} WHERE id = {database.PH}",
        (ic_role["id"], ceo_role["id"]),
    )
ids = database.role_subtree_ids(acme_id, mgr_role["id"])
check("subtree walk terminates on a cycle", len(ids) == len(set(ids)) and len(ids) < 100)
with database._connect() as conn, database._cursor(conn) as cur:
    cur.execute(
        f"UPDATE org_roles SET parent_role_id = NULL WHERE id = {database.PH}",
        (ceo_role["id"],),
    )


print()
if failures:
    print(f"{len(failures)} FAILED:")
    for f in failures:
        print("  - " + f)
    raise SystemExit(1)
print("all passed")
