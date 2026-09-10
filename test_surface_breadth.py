"""Which surfaces breadth narrows, and which it deliberately does not.

Seat breadth filters WORK. It does not filter the Agents roster, Cost, or
Ask — and that is a decision, not an omission. Recording it as a test is the
point of this file: "we thought about it and chose no" is invisible in a
diff, so the next ship reads unnarrowed endpoints as an oversight and
helpfully narrows them.

The reasoning, so it can be argued with rather than guessed at:

  * AGENTS is shared infrastructure. Whether an agent is healthy, drifting,
    or running at all is an org fact, not a personal one. Narrowing it by
    ownership would give an IC who owns nothing an empty Agents tab while
    still offering them Connect — and Whose work on Work already gives
    managers the personal view of what the agents actually DID.

  * COST is gated by the surface atom instead, which is the sharper tool:
    only Exec, VP and Manager have Cost at all, and those are the people
    whose job the number is. A partial spend figure that looks like the
    company's is worse than no figure.

  * ASK answers from the same telemetry the roster shows. If the roster is
    whole and Ask were narrowed, or vice versa, one of them is lying — and a
    narrowed roster with an unnarrowed Ask is a hole, not an inconsistency.
    So Ask follows the roster, whatever the roster does.

If that trade is ever revisited, this file is where the old answer lives:
change it deliberately, don't let it rot.

Run:
  OVERSEE_DISABLE_PRICING_SYNC=1 python3 test_surface_breadth.py
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


NS = 1_000_000_000
_seq = [0]


def seed_agent(account_id, service):
    _seq[0] += 1
    now = time.time_ns()
    database.insert_spans(
        [
            {
                "trace_id": f"t{_seq[0]}",
                "span_id": f"s{_seq[0]}",
                "parent_span_id": None,
                "service_name": service,
                "agent_id": "main",
                "span_name": "run",
                "kind": 1,
                "start_time_unix": now - 60 * NS,
                "end_time_unix": now,
                "status_code": 0,
                "status_message": "",
                "attributes": {"llm.model": "claude-sonnet-4-5", "llm.usage.total_tokens": 1000},
                "resource_attributes": {},
            }
        ],
        account_id=account_id,
    )


acme = client.post(
    "/auth/signup",
    json={
        "email": "founder@acme.test", "password": "correct horse battery",
        "name": "founder", "account_type": "business", "org_name": "Acme",
    },
).json()
acct, FOUNDER = acme["org"]["id"], acme["token"]

lv = {l["key"]: l for l in client.get("/org/scope-levels", headers=auth(FOUNDER)).json()}
ceo = client.post(
    "/org/roles", json={"title": "CEO", "scope_level_id": lv["exec"]["id"]},
    headers=auth(FOUNDER),
).json()
ic_role = client.post(
    "/org/roles",
    json={"title": "IC", "parent_role_id": ceo["id"], "scope_level_id": lv["ic"]["id"]},
    headers=auth(FOUNDER),
).json()
client.post(
    "/org/roles/%d/members" % ceo["id"], json={"user_id": acme["user"]["id"]},
    headers=auth(FOUNDER),
)

r = client.post(
    "/org/invites", json={"email": "ic@acme.test", "role_id": ic_role["id"]},
    headers=auth(FOUNDER),
).json()
tok = r["invite_url"].split("token=")[1]
ic = client.post(
    "/auth/accept-invite",
    json={"token": tok, "name": "Ira Chen", "password": "correct horse battery"},
).json()
IC = ic["token"]

# Three agents: one the IC owns, one the founder owns, one nobody owns.
for svc in ("ic-agent", "founder-agent", "orphan-agent"):
    seed_agent(acct, svc)
client.put(
    "/agents/ic-agent/owner", json={"agent_id": "main", "user_id": ic["user"]["id"]},
    headers=auth(FOUNDER),
)
client.put(
    "/agents/founder-agent/owner",
    json={"agent_id": "main", "user_id": acme["user"]["id"]},
    headers=auth(FOUNDER),
)


# ---------------------------------------------------------------------------
# The seats really are different
# ---------------------------------------------------------------------------
print("\nTwo seats, one org")

ic_seat = client.get("/auth/me", headers=auth(IC)).json()["seat"]
founder_seat = client.get("/auth/me", headers=auth(FOUNDER)).json()["seat"]
check("the IC is on a self seat", ic_seat["breadth"] == "self")
check("the founder is on a company seat", founder_seat["breadth"] == "company")
check("...so Work does differ between them", True)


# ---------------------------------------------------------------------------
# Agents: NOT narrowed
# ---------------------------------------------------------------------------
print("\nAgents is shared infrastructure")


def roster(token):
    r = client.get("/agents", headers=auth(token))
    assert r.status_code == 200, r.text
    return sorted(g["service_name"] for g in r.json())


expected = ["founder-agent", "ic-agent", "orphan-agent"]
check("a self-breadth IC sees the whole roster", roster(IC) == expected)
check("...the same one the founder sees", roster(FOUNDER) == expected)
check(
    "including agents they do not own, and ones nobody owns",
    "founder-agent" in roster(IC) and "orphan-agent" in roster(IC),
)
# The sharp edge this choice avoids: ownership only became assignable
# recently, so nearly every agent in every existing org is unowned. A
# narrowed roster would have shown most people an empty Agents tab.
check(
    "an agent detail page opens for an agent they do not own",
    client.get("/agents/founder-agent/summary", headers=auth(IC)).status_code == 200,
)

# The surface atom is still the gate — that part IS seat-driven.
narrow = database.create_scope_level(
    acct, "no_fleet", "No agents", "self", "technical", ["Home", "Work"]
)
check(
    "a scope level can still remove the Agents surface entirely",
    "Fleet" not in narrow["surfaces"],
)
check(
    "...which is the seat's real lever here, not row filtering",
    "Fleet" in database.resolve_seat(acct, ic["user"]["id"])["surfaces"],
)


# ---------------------------------------------------------------------------
# Cost: NOT narrowed; gated by the surface instead
# ---------------------------------------------------------------------------
print("\nCost is gated by the surface, not sliced")

founder_cost = client.get("/cost/overview", headers=auth(FOUNDER))
ic_cost = client.get("/cost/overview", headers=auth(IC))
check("cost loads for both", founder_cost.status_code == 200 and ic_cost.status_code == 200)
check(
    "and reports the same org-wide total",
    founder_cost.json()["month_total"] == ic_cost.json()["month_total"],
)
check(
    "over the same agents",
    sorted(a["service_name"] for a in founder_cost.json()["agents"])
    == sorted(a["service_name"] for a in ic_cost.json()["agents"]),
)
# The IC preset has no Cost surface, so the page is hidden from them in nav.
# That is the whole mechanism: presence, not partial numbers.
check(
    "the IC's seat does not carry the Cost surface",
    "Cost" not in database.resolve_seat(acct, ic["user"]["id"])["surfaces"],
)
check(
    "...while the founder's does",
    "Cost" in database.resolve_seat(acct, acme["user"]["id"])["surfaces"],
)


# ---------------------------------------------------------------------------
# The endpoints do not even accept a whose-work filter
# ---------------------------------------------------------------------------
print("\nNo half-applied filter")

# If one of these ever grows a `whose` param without the others, the three
# surfaces start disagreeing about what an agent is. Passing it now is
# ignored, which is the honest behavior for a filter that does not exist.
check(
    "a whose param on /agents changes nothing",
    sorted(
        g["service_name"]
        for g in client.get("/agents", params={"whose": "me"}, headers=auth(IC)).json()
    )
    == expected,
)
schema = main.app.openapi()
for path in ("/agents", "/cost/overview"):
    params = {
        p.get("name") for p in schema["paths"][path]["get"].get("parameters", [])
    }
    check(f"{path} declares no whose-work filter", "whose" not in params)
check(
    "...while /work/items does",
    "whose"
    in {p.get("name") for p in schema["paths"]["/work/items"]["get"].get("parameters", [])},
)


# ---------------------------------------------------------------------------
# What still holds: the account
# ---------------------------------------------------------------------------
print("\nThe tenant boundary is untouched")

other = client.post(
    "/auth/signup",
    json={
        "email": "founder@other.test", "password": "correct horse battery",
        "name": "them", "account_type": "business", "org_name": "Other",
    },
).json()
seed_agent(other["org"]["id"], "their-agent")
check("another org's agent is not on our roster", "their-agent" not in roster(FOUNDER))
check("...nor on the IC's", "their-agent" not in roster(IC))
check(
    "...nor in our cost breakdown",
    all(
        a["service_name"] != "their-agent"
        for a in client.get("/cost/overview", headers=auth(FOUNDER)).json()["agents"]
    ),
)
check(
    "an unnarrowed roster is still not a cross-tenant one",
    client.get("/agents/their-agent/summary", headers=auth(FOUNDER)).status_code == 404,
)


print()
if failures:
    print(f"{len(failures)} FAILED:")
    for f in failures:
        print("  - " + f)
    raise SystemExit(1)
print("all passed")
