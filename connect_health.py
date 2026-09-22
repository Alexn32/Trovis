"""Connection health — a normalized read model over facts Trovis already records.

Connections are the systems Trovis connects to in order to observe work
(connectors.py is the canonical connector registry; frontend/src/connectors.js
mirrors it through a committed snapshot). This module
answers ONE question per connector: "what does Trovis actually know about
this connection right now?" It does not answer how much of the work Trovis
can see — that is Work Coverage, a later, separate model.

Four ideas are kept apart:

  configuration  a durable fact that a connection path was authorized or set up
  activity       Trovis actually received data attributable to the connector
  health         whether the data path is behaving, from facts we HAVE
  coverage       how much of the relevant work is visible (NOT here)

States (the whole enum — nothing else is ever emitted):

  not_connected     no configuration fact and no attributable observation
  waiting_for_data  a durable configuration exists (an OAuth connection) but
                    Trovis has not observed attributable activity from it
  connected         Trovis has observed attributable activity; for an OAuth
                    connector the authorization is also present

There is deliberately no `degraded`: nothing in the current data model
records a concrete failure (token refresh failures and webhook signature
failures are logged, not stored; see saas_hubspot.py / saas_stripe.py), and
silence is not a fault — some agents legitimately run rarely. Adding the
state without a recorded fact behind it would turn absence of evidence into
a verdict, which is the one thing this module must never do.

Truth rules this module enforces:
  * OAuth authorization alone is `configured`, never `observed`.
  * A stored span or a verified, mapped SaaS webhook is an observation of
    that source at that timestamp. `last_observed_at` is always the newest
    timestamp among observations ATTRIBUTABLE TO THAT CONNECTOR, never an
    authorization time and never "the newest span of a service whose
    latest stamp happens to be this connector".
  * Attribution belongs to the observation, not to the service name. Spans
    are grouped by service AND by their exact resource-attribute blob (the
    stamp), so a service that changed how it exports contributes to each
    connector it was ever attributable to, each with its own newest time.
  * Connector identity for telemetry comes only from stamps a Trovis-owned
    door writes on the wire (below). A bare `service.name` proves nothing
    about the vendor, so unstamped telemetry is the custom OpenTelemetry
    connector — by definition of that connector, not by guess.
  * For a work system, `connected` means verified events ARRIVE. Whether
    any of them reached a run is a separate, recorded fact
    (`events_linked` from saas_events.outcome) — an event with no
    trovis_loop_external_id / trovis_run_id on the provider object is
    counted, not inferred into a link.
  * Fields Trovis cannot establish are None, not a default.

Identity on the wire, in precedence order (`identify_connector`):

  1. `trovis.connector.id` — the explicit, canonical resource attribute,
     introduced with this model. Emitted today by the Trovis-owned doors
     that build spans server-side (GPT Actions and the ChatGPT MCP server
     stamp `chatgpt`; the Grok Bot MCP server stamps `grok-bot`) and by the
     Cursor recipe in Add Agent (`cursor`). Anything may set it, but an
     unknown value is ignored rather than trusted.
  2. `trovis.platform` — the legacy stamp those same doors already wrote
     (`chatgpt`, `cursor-grok-bot` / `grok-bot`). Old telemetry keeps
     resolving.
  3. `openclaw.gateway.version` — set by the OpenClaw plugin on every span.
  4. `trovis.sdk.platform` — set by the trovis-agents Python SDK:
     `openai` → openai-agents, `anthropic` / `claude-agent-sdk` → claude,
     `xai` → grok. The SDK's generic modes (`all`, `agent`) name no vendor
     and resolve to custom-otel with method `sdk`.
  5. Nothing recognised → custom-otel over `otel`.

Grok (the xAI SDK) and Grok Bot (a desktop assistant reporting over MCP)
are different connectors with different stamps and never merge.

Instances. A connection set up THROUGH Trovis has a row in
connection_instances and a key it stamps on the wire as
`trovis.connection.id` (`identify_connection`). Health rows carry those
instances with a derived per-instance state (INSTANCE_STATES); a stamped
span attributes to its instance IN ADDITION to the connector its other
stamps name. Unstamped telemetry attributes at connector level only —
today's behaviour, not a fallback that invents an instance.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import connectors
import database

CONNECTOR_ID_ATTR = "trovis.connector.id"
# The per-INSTANCE stamp: a connection_instances.connection_key, written on the
# resource by a snippet / SDK / door that was set up through Trovis. Absent on
# most traffic today; absence means connector-level attribution, exactly as
# before instances existed.
CONNECTION_ID_ATTR = "trovis.connection.id"

# Telemetry connectors this model can attribute — every available connector
# whose data path is telemetry rather than OAuth. Derived from the canonical
# registry (connectors.py), which the frontend mirrors through a committed
# snapshot; nothing here is a second list.
TELEMETRY_CONNECTOR_IDS: tuple[str, ...] = connectors.telemetry_ids()

# Work systems with a durable OAuth row in saas_connections.
SAAS_CONNECTOR_IDS: tuple[str, ...] = connectors.saas_ids()

STATE_NOT_CONNECTED = "not_connected"
STATE_WAITING_FOR_DATA = "waiting_for_data"
STATE_CONNECTED = "connected"
STATES: tuple[str, ...] = (STATE_NOT_CONNECTED, STATE_WAITING_FOR_DATA, STATE_CONNECTED)

# Per-INSTANCE states (connection_instances), a separate enum from the
# connector-level STATES above, which stay three-valued. Derived, never
# stored: the row records an action (started / completed / disconnected);
# whether data arrived is read from spans or saas_events.
INSTANCE_SETUP_STARTED = "setup_started"
INSTANCE_WAITING_FOR_DATA = "waiting_for_data"
INSTANCE_CONNECTED = "connected"
INSTANCE_DISCONNECTED = "disconnected"
INSTANCE_STATES: tuple[str, ...] = (
    INSTANCE_SETUP_STARTED, INSTANCE_WAITING_FOR_DATA, INSTANCE_CONNECTED, INSTANCE_DISCONNECTED,
)

# Legacy explicit stamp → (connector, method). Both GPT doors (Actions and
# the MCP server) stamp the same value, so the method is not knowable.
_PLATFORM_STAMPS: dict[str, tuple[str, str | None]] = {
    "chatgpt": ("chatgpt", None),
    "cursor-grok-bot": ("grok-bot", "mcp"),
    "grok-bot": ("grok-bot", "mcp"),
}

# trovis-agents SDK `platform` → connector. Generic modes name no vendor.
_SDK_STAMPS: dict[str, str] = {
    "openai": "openai-agents",
    "anthropic": "claude",
    "claude-agent-sdk": "claude",
    "xai": "grok",
    "all": "custom-otel",
    "agent": "custom-otel",
}

# Method for an explicit connector id when it is deterministic for that id
# (registry `explicit_method`; None where the id alone does not settle it).
_EXPLICIT_METHODS: dict[str, str | None] = connectors.explicit_methods()


def _load_attrs(resource_attrs: str | dict[str, Any] | None) -> dict[str, Any]:
    if isinstance(resource_attrs, dict):
        return resource_attrs
    if not resource_attrs:
        return {}
    try:
        parsed = json.loads(resource_attrs)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def identify_connector(resource_attrs: str | dict[str, Any] | None) -> tuple[str, str | None]:
    """(connector_id, connection_method) for one span's resource attributes.

    Deterministic and stamp-based — see the module docstring for the order.
    Never returns a branded connector for telemetry that carries no
    Trovis-owned stamp.
    """
    attrs = _load_attrs(resource_attrs)

    explicit = attrs.get(CONNECTOR_ID_ATTR)
    if isinstance(explicit, str):
        cid = explicit.strip().lower()
        if cid in TELEMETRY_CONNECTOR_IDS:
            return cid, _EXPLICIT_METHODS.get(cid)

    stamp = attrs.get("trovis.platform") or attrs.get("oversee.platform")
    if isinstance(stamp, str) and stamp.strip().lower() in _PLATFORM_STAMPS:
        return _PLATFORM_STAMPS[stamp.strip().lower()]

    if "openclaw.gateway.version" in attrs:
        return "openclaw", "plugin"

    sdk = attrs.get("trovis.sdk.platform")
    if isinstance(sdk, str) and sdk.strip().lower() in _SDK_STAMPS:
        return _SDK_STAMPS[sdk.strip().lower()], "sdk"

    return "custom-otel", "otel"


def identify_connection(resource_attrs: str | dict[str, Any] | None) -> str | None:
    """The instance key a span's resource carries (`trovis.connection.id`),
    or None. Identity only — the caller resolves it against the account's
    own connection_instances; a key alone attributes nothing."""
    attrs = _load_attrs(resource_attrs)
    key = attrs.get(CONNECTION_ID_ATTR)
    if isinstance(key, str) and key.strip():
        return key.strip()
    return None


def _iso_max(a: str | None, b: str | None) -> str | None:
    if a is None:
        return b
    if b is None:
        return a
    return a if a >= b else b


def _telemetry_rows(
    account_id: int, instance_obs: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Fold observations into per-connector rows.

    Each observation is one distinct (service, resource-attribute blob)
    with the newest span time under it (database.get_connector_observations).
    The blob is the stamp, so attribution is decided per observation: a
    service that exported Claude-stamped spans and later bare OTEL
    contributes to BOTH connectors, each with its own newest time, and
    neither history is reassigned by the other.

      observed          at least one observation is attributable
      last_observed_at  the newest time among attributable observations
      source_count      distinct service names with an attributable
                        observation (a service in two connectors counts
                        once in each)
      connection_method one method only when every attributable
                        observation agrees; otherwise None

    `instance_obs`, when given, is filled per `trovis.connection.id` stamp
    with the newest time, the service names and the connector ids seen under
    that key — the same pass, no second scan. The connector-level row still
    counts every observation under its STAMPED connector; the instance is
    a second, finer attribution, never a reassignment.
    """
    rows: dict[str, dict[str, Any]] = {}
    sources: dict[str, set[str]] = {}
    for obs in database.get_connector_observations(account_id):
        cid, method = identify_connector(obs.get("resource_attributes"))
        last = obs.get("last_observed_at")
        key = identify_connection(obs.get("resource_attributes"))
        if key is not None and instance_obs is not None:
            io = instance_obs.setdefault(key, {"last_observed_at": None, "services": set(), "connector_ids": set()})
            io["last_observed_at"] = _iso_max(io["last_observed_at"], last)
            io["services"].add(obs.get("service_name"))
            io["connector_ids"].add(cid)
        row = rows.get(cid)
        if row is None:
            row = rows[cid] = {
                "connector_id": cid,
                "state": STATE_CONNECTED,
                # No door records that setup began or finished; the span
                # itself is the only fact, so configuration is not tracked
                # for telemetry connectors — None, not False.
                "configured": None,
                "observed": True,
                "last_observed_at": last,
                "connection_method": method,
                "label": None,
                "source_count": 0,
            }
            sources[cid] = set()
        else:
            row["last_observed_at"] = _iso_max(row["last_observed_at"], last)
            if row["connection_method"] != method:
                # Observations of one connector over different paths: the
                # method is no longer a single fact.
                row["connection_method"] = None
        sources[cid].add(obs.get("service_name"))
    for cid, row in rows.items():
        row["source_count"] = len(sources[cid])
    return rows


def _saas_rows(account_id: int) -> dict[str, dict[str, Any]]:
    connections = {
        c["provider"]: c for c in database.get_saas_connections(account_id)
    }
    activity = database.get_saas_activity(account_id)
    rows: dict[str, dict[str, Any]] = {}
    for cid in SAAS_CONNECTOR_IDS:
        conn = connections.get(cid)
        authorized = bool(conn and conn.get("status") == "connected")
        act = activity.get(cid)
        observed = bool(act and act.get("event_count"))
        if authorized and observed:
            state = STATE_CONNECTED
        elif authorized:
            state = STATE_WAITING_FOR_DATA
        else:
            state = STATE_NOT_CONNECTED
        rows[cid] = {
            "connector_id": cid,
            "state": state,
            "configured": authorized,
            # A verified, mapped webhook that reached this account is an
            # observation even if the row was later disconnected: the fact
            # that data arrived does not un-happen. The state above still
            # reads not_connected without the authorization.
            "observed": observed,
            "last_observed_at": act.get("last_event_at") if act else None,
            "connection_method": "oauth" if authorized else None,
            "label": (conn or {}).get("provider_account_id") if authorized else None,
            "source_count": None,
            # What the verified events DID. `connected` above means events
            # arrive; it does not mean any of them reached a run — that
            # needs a trovis_loop_external_id / trovis_run_id on the
            # provider object. These counts let the page say which.
            "events_received": int(act.get("event_count") or 0) if act else 0,
            "events_linked": int(act.get("linked_count") or 0) if act else 0,
            "events_without_link_key": int(act.get("no_link_key_count") or 0) if act else 0,
            "events_without_open_run": int(act.get("no_open_run_count") or 0) if act else 0,
            "last_linked_at": act.get("last_linked_at") if act else None,
        }
    return rows


def _instance_state(inst: dict[str, Any], observed: bool) -> str:
    if inst.get("setup_status") == "disconnected":
        return INSTANCE_DISCONNECTED
    if observed:
        return INSTANCE_CONNECTED
    if inst.get("setup_status") == "completed":
        return INSTANCE_WAITING_FOR_DATA
    return INSTANCE_SETUP_STARTED


def _instance_rows(
    account_id: int,
    instance_obs: dict[str, dict[str, Any]],
    saas: dict[str, dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Per connector, the account's configured instances with derived state.

    Telemetry instance: observed when a stored span carries its key (the
    key is looked up under THIS account only — a foreign key attributes
    nothing). OAuth instance: observed when the provider's row is observed,
    since saas_events carry no instance key while saas_connections is
    one-per-provider."""
    out: dict[str, list[dict[str, Any]]] = {}
    for inst in database.list_connection_instances(account_id):
        cid = inst["connector_id"]
        key = inst["connection_key"]
        if inst["setup_type"] == "oauth":
            row = saas.get(cid) or {}
            observed = bool(row.get("observed"))
            last = row.get("last_observed_at") if observed else None
            services: set[str] = set()
            stamped: set[str] = set()
        else:
            io = instance_obs.get(key)
            observed = io is not None
            last = io["last_observed_at"] if io else None
            services = io["services"] if io else set()
            stamped = io["connector_ids"] if io else set()
        out.setdefault(cid, []).append({
            "id": inst["id"],
            "connector_id": cid,
            "connection_key": key,
            "label": inst.get("label"),
            "setup_type": inst["setup_type"],
            "setup_source": inst["setup_source"],
            "setup_status": inst["setup_status"],
            "state": _instance_state(inst, observed),
            "setup_started_at": inst.get("setup_started_at"),
            "setup_completed_at": inst.get("setup_completed_at"),
            "disconnected_at": inst.get("disconnected_at"),
            "last_observed_at": last,
            "source_count": len([sname for sname in services if sname]),
            # Stamps seen under this key that name a DIFFERENT connector: the
            # instance wins the attribution, and the mismatch is surfaced.
            "observed_connector_ids": sorted(c for c in stamped if c != cid),
            "saas_connection_id": inst.get("saas_connection_id"),
        })
    return out


def _configured_from_instances(instances: list[dict[str, Any]]) -> bool | None:
    """None when nothing was ever set up through Trovis (not tracked, as
    before), True when a live instance exists, False when only disconnected
    ones remain."""
    if not instances:
        return None
    return any(i["setup_status"] != "disconnected" for i in instances)


def build_connection_health(account_id: int) -> dict[str, Any]:
    """The read model for one account. Deterministic; no model calls."""
    instance_obs: dict[str, dict[str, Any]] = {}
    telemetry = _telemetry_rows(account_id, instance_obs)
    saas = _saas_rows(account_id)
    instances = _instance_rows(account_id, instance_obs, saas)
    connectors: list[dict[str, Any]] = []
    for cid in TELEMETRY_CONNECTOR_IDS:
        row = telemetry.get(cid) or {
            "connector_id": cid,
            "state": STATE_NOT_CONNECTED,
            "configured": None,
            "observed": False,
            "last_observed_at": None,
            "connection_method": None,
            "label": None,
            "source_count": 0,
        }
        insts = instances.get(cid, [])
        row["instances"] = insts
        # Configuration is now a recorded fact for telemetry connectors too —
        # but only where a door or the UI recorded it. No rows → None, as
        # before: absence of a record is not False.
        row["configured"] = _configured_from_instances(insts)
        if not row["observed"] and any(i["state"] == INSTANCE_WAITING_FOR_DATA for i in insts):
            row["state"] = STATE_WAITING_FOR_DATA
        connectors.append(row)
    for cid in SAAS_CONNECTOR_IDS:
        row = saas[cid]
        row["instances"] = instances.get(cid, [])
        connectors.append(row)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "connectors": connectors,
    }
