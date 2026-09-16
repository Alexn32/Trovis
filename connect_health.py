"""Connection health — a normalized read model over facts Trovis already records.

Connections are the systems Trovis connects to in order to observe work
(frontend/src/connectors.js is the canonical connector registry). This module
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
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import database

CONNECTOR_ID_ATTR = "trovis.connector.id"

# Telemetry connectors this model can attribute. Mirrors the AI / platform /
# custom ids in frontend/src/connectors.js — keep the two in step.
TELEMETRY_CONNECTOR_IDS: tuple[str, ...] = (
    "openclaw",
    "openai-agents",
    "claude",
    "chatgpt",
    "grok",
    "grok-bot",
    "cursor",
    "custom-otel",
)

# Work systems with a durable OAuth row in saas_connections.
SAAS_CONNECTOR_IDS: tuple[str, ...] = ("stripe", "hubspot", "shopify")

STATE_NOT_CONNECTED = "not_connected"
STATE_WAITING_FOR_DATA = "waiting_for_data"
STATE_CONNECTED = "connected"
STATES: tuple[str, ...] = (STATE_NOT_CONNECTED, STATE_WAITING_FOR_DATA, STATE_CONNECTED)

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

# Method for an explicit connector id when it is deterministic for that id.
_EXPLICIT_METHODS: dict[str, str | None] = {
    "openclaw": "plugin",
    "openai-agents": "sdk",
    "claude": "sdk",
    "chatgpt": None,
    "grok": "sdk",
    "grok-bot": "mcp",
    "cursor": "otel",
    "custom-otel": "otel",
}


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


def _iso_max(a: str | None, b: str | None) -> str | None:
    if a is None:
        return b
    if b is None:
        return a
    return a if a >= b else b


def _telemetry_rows(account_id: int) -> dict[str, dict[str, Any]]:
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
    """
    rows: dict[str, dict[str, Any]] = {}
    sources: dict[str, set[str]] = {}
    for obs in database.get_connector_observations(account_id):
        cid, method = identify_connector(obs.get("resource_attributes"))
        last = obs.get("last_observed_at")
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
        }
    return rows


def build_connection_health(account_id: int) -> dict[str, Any]:
    """The read model for one account. Deterministic; no model calls."""
    telemetry = _telemetry_rows(account_id)
    saas = _saas_rows(account_id)
    connectors: list[dict[str, Any]] = []
    for cid in TELEMETRY_CONNECTOR_IDS:
        connectors.append(telemetry.get(cid) or {
            "connector_id": cid,
            "state": STATE_NOT_CONNECTED,
            "configured": None,
            "observed": False,
            "last_observed_at": None,
            "connection_method": None,
            "label": None,
            "source_count": 0,
        })
    for cid in SAAS_CONNECTOR_IDS:
        connectors.append(saas[cid])
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "connectors": connectors,
    }
