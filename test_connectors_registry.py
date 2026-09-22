"""Connector registry — one canonical list, mirrored, never forked.

connectors.py is the source of truth for connector identity, setup shape
and capabilities. connect_health.py derives its id tuples from it, asker.py
builds its roster from it, and the frontend reads a committed snapshot
(frontend/src/connectors.registry.json). These checks fail the moment any
of those stops being true — in particular when the snapshot is stale.

Run:
  TROVIS_DISABLE_PRICING_SYNC=1 python3 test_connectors_registry.py
"""
from __future__ import annotations

import json
import os
import tempfile

os.environ.update({
    "OVERSEE_DISABLE_PRICING_SYNC": "1",
    "TROVIS_DISABLE_PRICING_SYNC": "1",
    "TROVIS_DISABLE_ALERTS": "1",
    "TROVIS_DISABLE_LOOP_SWEEP": "1",
    "TROVIS_LOOP_TITLES": "off",
})
os.environ.pop("DATABASE_URL", None)
os.environ.pop("ANTHROPIC_API_KEY", None)

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()

import database
database.SQLITE_PATH = _tmp.name

import asker
import connect_health
import connectors
import main
from fastapi.testclient import TestClient

main._auto_describe = lambda *a, **k: False

failures = []


def check(label, cond):
    print(("  PASS " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


HERE = os.path.dirname(os.path.abspath(__file__))
SNAPSHOT_PATH = os.path.join(HERE, "frontend", "src", "connectors.registry.json")

# --- the registry itself ------------------------------------------------------

print("registry shape")
ids = connectors.ids()
check("ids are unique", len(set(ids)) == len(ids))
check("14 connectors today", len(ids) == 14)
check("every id is kebab-case",
      all(i == i.lower() and " " not in i and "_" not in i for i in ids))
for c in connectors.CONNECTORS:
    check(f"{c.id}: to_public is JSON-serialisable and round-trips lists",
          json.loads(json.dumps(c.to_public()))["methods"] == list(c.methods))

check("coming-soon connectors have nothing to set up or manage",
      all(c.setup_type == "none" and c.management == "none"
          for c in connectors.CONNECTORS if c.availability == "coming_soon"))
check("available connectors all have a setup path",
      all(c.setup_type != "none" for c in connectors.available()))
check("work systems never discover agents — they enrich Work",
      all(not c.discovers_agents for c in connectors.CONNECTORS
          if c.category == "work_system"))
check("work systems contribute external_outcomes and nothing execution-shaped",
      all(c.observes == ("external_outcomes",) for c in connectors.CONNECTORS
          if c.category == "work_system" and c.availability == "available"))
check("get() is case-insensitive and never throws",
      connectors.get("Grok-Bot").id == "grok-bot"
      and connectors.get("zendesk") is None and connectors.get(None) is None)

# --- the committed frontend snapshot ------------------------------------------

print("frontend snapshot")
check("snapshot file exists", os.path.exists(SNAPSHOT_PATH))
if os.path.exists(SNAPSHOT_PATH):
    with open(SNAPSHOT_PATH, encoding="utf-8") as fh:
        snapshot = json.load(fh)
    check("snapshot equals connectors.to_public() — regenerate with "
          "`python3 connectors.py > frontend/src/connectors.registry.json`",
          snapshot == connectors.to_public())

# --- connect_health derives from it -------------------------------------------

print("connect_health")
check("TELEMETRY_CONNECTOR_IDS = every available non-OAuth connector",
      connect_health.TELEMETRY_CONNECTOR_IDS == connectors.telemetry_ids()
      == tuple(c.id for c in connectors.available() if c.setup_type != "oauth"))
check("SAAS_CONNECTOR_IDS = every available OAuth connector",
      connect_health.SAAS_CONNECTOR_IDS == connectors.saas_ids() == ("stripe", "hubspot", "shopify"))
check("every stamp target the identity ladder can produce is a registry telemetry id",
      all(cid in connectors.telemetry_ids()
          for cid, _m in connect_health._PLATFORM_STAMPS.values())
      and all(cid in connectors.telemetry_ids()
              for cid in connect_health._SDK_STAMPS.values()))
check("explicit-stamp methods come from the registry",
      connect_health._EXPLICIT_METHODS == connectors.explicit_methods()
      and connect_health._EXPLICIT_METHODS["chatgpt"] is None
      and connect_health._EXPLICIT_METHODS["grok"] == "sdk")
check("the identity ladder still resolves each explicit id to the registry method",
      all(connect_health.identify_connector({"trovis.connector.id": cid})
          == (cid, connectors.get(cid).explicit_method)
          for cid in connectors.telemetry_ids()))

# --- asker builds its roster from it ------------------------------------------

print("asker")
roster = asker._registry_roster()
check("every available connector is in the setup roster",
      all(c.name in roster and f"[{c.id}]" in roster for c in connectors.available()))
check("no coming-soon connector is offered as connectable",
      all(f"[{c.id}]" not in roster for c in connectors.CONNECTORS
          if c.availability == "coming_soon"))
check("the roster reaches the connect guide prompt", roster in asker.SYSTEM_CONNECT)
check("and the fleet assistant prompt", roster in asker.SYSTEM_FLEET_CONCISE)

# --- the endpoint -------------------------------------------------------------

print("GET /connect/connectors")
with TestClient(main.app) as c:
    r = c.get("/connect/connectors")
    check("unauthenticated is 401", r.status_code == 401)
    a = c.post("/auth/signup", json={
        "email": "reg-a@t.com", "password": "supersecret123",
        "name": "Ada", "account_type": "business", "org_name": "A Co",
    }).json()
    r = c.get("/connect/connectors", headers={"Authorization": f"Bearer {a['token']}"})
    check("authenticated is 200", r.status_code == 200)
    body = r.json()
    check("the endpoint serves exactly the registry",
          body.get("connectors") == connectors.to_public())
    check("and therefore exactly the snapshot the frontend committed",
          os.path.exists(SNAPSHOT_PATH) and body.get("connectors") == snapshot)

os.unlink(_tmp.name)
if failures:
    print(f"\n{len(failures)} check(s) failed:")
    for f in failures:
        print("  -", f)
    raise SystemExit(1)
print("\nall connector-registry checks passed")
