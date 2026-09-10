"""Whose work: seat breadth actually filtering the Work list.

Everyone in an org queries the same Work truth. A seat decides which slice
of it reaches the screen — and this is the ship where that stops being a
field on /auth/me and starts removing rows.

Five things have to hold:

  1. The server enforces it. The Whose-work control is a courtesy; the query
     string is the easiest thing in the product to edit, so a request is
     INTERSECTED with the seat, never trusted over it.

  2. "Belongs to" is two things. Work RUN BY an agent you own, and work
     WAITING ON you. Ownership alone would leave a manager's team view
     nearly empty — most work is not waiting on a human at any moment — and
     waiting-on alone would drop everything your own agents are quietly
     doing.

  3. The filter runs BEFORE the cursor. Filtering a fetched page would
     return short pages while matching rows sat behind it.

  4. THE DESK IS UNTOUCHED. Home's desk is `waiting_on_you`, resolved
     against the session identity. It is not a view of other people's work
     and no Whose-work choice may take a row off it.

  5. None and [] are different answers. None is a company seat (no filter,
     which also keeps work held by people with no login); [] is nobody.

Run:
  OVERSEE_DISABLE_PRICING_SYNC=1 python3 test_whose_work.py
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


def invite_accept(owner_token, email, name, role_id):
    r = client.post(
        "/org/invites",
        json={"email": email, "role": "member", "role_id": role_id},
        headers=auth(owner_token),
    )
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


def attrs(d):
    return [{"key": k, "value": {"stringValue": str(v)}} for k, v in d.items()]


def emit(api_key, service, title, extra=None, agent_id="main"):
    """One named work item, via the real OTLP door."""
    _seq[0] += 1
    now = time.time_ns()
    a = {"trovis.run.id": f"L{_seq[0]}", "trovis.loop.title": title}
    a.update(extra or {})
    body = {
        "resourceSpans": [
            {
                "resource": {"attributes": attrs({"service.name": service})},
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": f"{_seq[0]:032x}",
                                "spanId": f"{_seq[0]:016x}",
                                "name": "run",
                                "kind": 1,
                                "startTimeUnixNano": str(now - 60 * NS),
                                "endTimeUnixNano": str(now),
                                "attributes": attrs(
                                    {"trovis.agent.id": agent_id, **a}
                                ),
                            }
                        ]
                    }
                ],
            }
        ]
    }
    r = client.post("/v1/traces", json=body, headers={"X-Trovis-Api-Key": api_key})
    assert r.status_code in (200, 201, 202), r.text


def titles(token, **params):
    r = client.get("/work/items", params=params, headers=auth(token))
    assert r.status_code == 200, r.text
    return sorted(i["title"] for i in r.json()["items"])


# ---------------------------------------------------------------------------
# One org, one chart, three seats, four agents with four owners
# ---------------------------------------------------------------------------
acme = signup("founder@acme.test", "Acme")
acct = acme["org"]["id"]
KEY = acme["api_key"]
FOUNDER = acme["token"]
founder_id = acme["user"]["id"]

lv = {l["key"]: l for l in client.get("/org/scope-levels", headers=auth(FOUNDER)).json()}
ceo = client.post(
    "/org/roles", json={"title": "CEO", "scope_level_id": lv["exec"]["id"]},
    headers=auth(FOUNDER),
).json()
mgr_role = client.post(
    "/org/roles",
    json={"title": "Support Lead", "parent_role_id": ceo["id"],
          "scope_level_id": lv["manager"]["id"]},
    headers=auth(FOUNDER),
).json()
ic_role = client.post(
    "/org/roles",
    json={"title": "Support IC", "parent_role_id": mgr_role["id"],
          "scope_level_id": lv["ic"]["id"]},
    headers=auth(FOUNDER),
).json()
# A second branch, so "someone else's team" is a real thing to be excluded.
sales_role = client.post(
    "/org/roles",
    json={"title": "Sales Lead", "parent_role_id": ceo["id"],
          "scope_level_id": lv["manager"]["id"]},
    headers=auth(FOUNDER),
).json()

mgr = invite_accept(FOUNDER, "mgr@acme.test", "Mel Ortiz", mgr_role["id"])
ic = invite_accept(FOUNDER, "ic@acme.test", "Ira Chen", ic_role["id"])
sales = invite_accept(FOUNDER, "sales@acme.test", "Sam Reyes", sales_role["id"])
client.post(
    "/org/roles/%d/members" % ceo["id"], json={"user_id": founder_id},
    headers=auth(FOUNDER),
)
MGR, IC, SALES = mgr["token"], ic["token"], sales["token"]

emit(KEY, "mgr-agent", "Refund policy review")
emit(KEY, "ic-agent", "Ticket triage")
emit(KEY, "sales-agent", "Lead follow-up")
emit(KEY, "orphan-agent", "Nobody owns this")

for service, uid in (
    ("mgr-agent", mgr["user"]["id"]),
    ("ic-agent", ic["user"]["id"]),
    ("sales-agent", sales["user"]["id"]),
):
    r = client.put(
        f"/agents/{service}/owner",
        json={"agent_id": "main", "user_id": uid},
        headers=auth(FOUNDER),
    )
    assert r.status_code == 204, r.text


# ---------------------------------------------------------------------------
# 1. Default = the seat's own breadth
# ---------------------------------------------------------------------------
print("\nDefault is the seat")

check(
    "a company seat sees everything, including work nobody owns",
    titles(FOUNDER)
    == ["Lead follow-up", "Nobody owns this", "Refund policy review", "Ticket triage"],
)
check(
    "a subtree manager sees their own team's work",
    titles(MGR) == ["Refund policy review", "Ticket triage"],
)
check("...and not the other branch's", "Lead follow-up" not in titles(MGR))
check("...nor unowned work", "Nobody owns this" not in titles(MGR))
check("a self-breadth IC sees only their own", titles(IC) == ["Ticket triage"])
check(
    "the other branch's manager sees only theirs",
    titles(SALES) == ["Lead follow-up"],
)

# The company seat's answer is "no filter", not "every user id we know" —
# work owned by nobody would silently vanish under an id list.
founder_seat = database.resolve_seat(acct, founder_id)
check("company breadth means no filter at all", founder_seat["visible_user_ids"] is None)
mgr_seat = database.resolve_seat(acct, mgr["user"]["id"])
check(
    "subtree breadth names the manager and their reports",
    set(mgr_seat["visible_user_ids"]) == {mgr["user"]["id"], ic["user"]["id"]},
)


# ---------------------------------------------------------------------------
# 2. The control, and the seat as its ceiling
# ---------------------------------------------------------------------------
print("\nWhose work")

check("Me narrows to your own", titles(MGR, whose="me") == ["Refund policy review"])
check(
    "My team is you plus your reports",
    titles(MGR, whose="team") == ["Refund policy review", "Ticket triage"],
)
check(
    "one person under you",
    titles(MGR, whose="person", person_id=ic["user"]["id"]) == ["Ticket triage"],
)
check(
    "Everyone I can see is the seat's own breadth",
    titles(MGR, whose="everyone") == titles(MGR),
)
check("an IC asking for Me gets the same list", titles(IC, whose="me") == ["Ticket triage"])

# The enforcement. Every one of these is a hand-edited query string.
r = client.get(
    "/work/items", params={"whose": "person", "person_id": sales["user"]["id"]},
    headers=auth(MGR),
)
check("asking for someone outside your line is refused", r.status_code == 403)
r = client.get(
    "/work/items", params={"whose": "person", "person_id": founder_id},
    headers=auth(MGR),
)
check("...including your own manager", r.status_code == 403)
r = client.get(
    "/work/items", params={"whose": "person", "person_id": 999999}, headers=auth(MGR)
)
check(
    "an id that does not exist answers the same way — nothing is revealed",
    r.status_code == 403,
)
check(
    "an IC asking for 'team' gets themselves, not the company",
    titles(IC, whose="team") == ["Ticket triage"],
)
check(
    "an unreadable choice narrows nothing rather than erroring",
    titles(MGR, whose="everything") == titles(MGR),
)
r = client.get("/work/items", params={"whose": "person"}, headers=auth(MGR))
check("asking for a person without naming one is a 400", r.status_code == 400)

# A person with no login can still hold work, so a company seat must not be
# reduced to an id list. Belt and braces: the sales manager cannot reach the
# founder's unowned row by any choice.
for params in ({"whose": "everyone"}, {"whose": "team"}, {"whose": "me"}):
    check(
        f"unowned work stays out of a narrowed seat ({params['whose']})",
        "Nobody owns this" not in titles(SALES, **params),
    )


# ---------------------------------------------------------------------------
# 3. Waiting on you is the other half of "belongs to"
# ---------------------------------------------------------------------------
print("\nWaiting on a person")

# Work run by an agent the SALES manager owns, handed to the IC. It is not
# the IC's agent, but it is the IC's work until they pass it on.
emit(
    KEY,
    "sales-agent",
    "Contract needs a human",
    {
        "trovis.handoff.direction": "to_human",
        "trovis.handoff.reason": "needs approval",
        "trovis.handoff.target_id": "ic@acme.test",
    },
)
ic_titles = titles(IC)
check(
    "work handed to you is yours, even on someone else's agent",
    "Contract needs a human" in ic_titles,
)
check(
    "...and still shows for their own manager, who owns the agent",
    "Contract needs a human" in titles(SALES),
)
check(
    "...and for the IC's manager, through their report",
    "Contract needs a human" in titles(MGR, whose="team"),
)


# ---------------------------------------------------------------------------
# 4. The desk contract
# ---------------------------------------------------------------------------
print("\nThe desk is untouched")

# Home reads /work/items with no `whose` and derives the desk from
# status == waiting_on_you. That row must survive every seat.
def desk(token):
    r = client.get("/work/items", headers=auth(token))
    return sorted(
        i["title"] for i in r.json()["items"] if i["status"] == "waiting_on_you"
    )


check("the IC's desk has the row handed to them", desk(IC) == ["Contract needs a human"])
check(
    "a narrow seat does not empty the desk",
    database.resolve_seat(acct, ic["user"]["id"])["breadth"] == "self"
    and desk(IC) == ["Contract needs a human"],
)
check(
    "nobody else's desk picks it up",
    desk(MGR) == [] and desk(SALES) == [] and desk(FOUNDER) == [],
)
# needs_you is the desk's count and is never narrowed by a Whose-work choice
# — it answers "what is on MY desk", which does not depend on which slice of
# other people's work is on screen.
ov_default = client.get("/work/overview", headers=auth(IC)).json()
ov_me = client.get("/work/overview", params={"whose": "me"}, headers=auth(IC)).json()
check("needs_you is the same whatever is selected", ov_default["needs_you"] == ov_me["needs_you"])
check("...and it counts the desk row", ov_default["needs_you"] == 1)


# ---------------------------------------------------------------------------
# 5. Counts describe the rows
# ---------------------------------------------------------------------------
print("\nCounts match the table")


def open_count(token, **params):
    return client.get("/work/overview", params=params, headers=auth(token)).json()["open"]


check("the manager's count matches their table", open_count(MGR) == len(titles(MGR)))
check("the IC's count matches theirs", open_count(IC) == len(titles(IC)))
check("the founder's count matches theirs", open_count(FOUNDER) == len(titles(FOUNDER)))
check("Me narrows the count too", open_count(MGR, whose="me") == 1)


# ---------------------------------------------------------------------------
# 6. Filtering happens before the cursor
# ---------------------------------------------------------------------------
print("\nPagination")

# Enough of someone else's work to bury the manager's rows past a page
# boundary. If the filter ran after the fetch, page one would come back
# empty while matching rows sat behind the cursor.
for n in range(12):
    emit(KEY, "sales-agent", f"Sales chore {n:02d}")

page = client.get("/work/items", params={"limit": 5}, headers=auth(MGR)).json()
check("a filtered first page is full of the right rows", len(page["items"]) >= 2)
check(
    "...and none of them belong to the other branch",
    all("Sales chore" not in i["title"] for i in page["items"]),
)

seen, cursor, guard = [], None, 0
while guard < 10:
    guard += 1
    params = {"limit": 2}
    if cursor:
        params["cursor"] = cursor
    body = client.get("/work/items", params=params, headers=auth(MGR)).json()
    seen.extend(i["title"] for i in body["items"])
    cursor = body.get("next_cursor")
    if not cursor:
        break
check("paging a filtered list reaches every row once", sorted(seen) == titles(MGR))


# ---------------------------------------------------------------------------
# 7. Tenant isolation, and machine callers
# ---------------------------------------------------------------------------
print("\nIsolation")

other = signup("founder@other.test", "Other Co")
emit(other["api_key"], "their-agent", "Their private work")
check(
    "another org's work never appears, at any breadth",
    "Their private work" not in titles(FOUNDER)
    and "Their private work" not in titles(MGR),
)
check(
    "and ours never appears in theirs",
    "Refund policy review" not in titles(other["token"]),
)

# An API key is a machine credential: no person, so no seat to narrow by. It
# keeps the account-wide view it has always had, and the account still binds.
r = client.get("/work/items", headers={"X-Trovis-Api-Key": KEY})
check("an API-key caller still sees its account's work", r.status_code == 200)
check(
    "...scoped to that account",
    all("Their private work" != i["title"] for i in r.json()["items"]),
)


print()
if failures:
    print(f"{len(failures)} FAILED:")
    for f in failures:
        print("  - " + f)
    raise SystemExit(1)
print("all passed")
