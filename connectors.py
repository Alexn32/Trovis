"""Connector registry — the canonical identity, taxonomy and capability layer
for everything Trovis can connect to.

This is the backend twin of ``frontend/src/connectors.js`` and the single
source of truth for connector IDENTITY (ids, categories, methods,
availability) and SETUP SHAPE (how a connector is configured, how the
Connect surfaces label it). The frontend keeps its own module for the
presentation-only fields (brand marks, one-line descriptions) and reads the
committed snapshot ``frontend/src/connectors.registry.json`` for everything
here; ``test_connectors_registry.py`` fails when the snapshot is stale, and
``frontend/test/connectors.test.mjs`` fails when the two modules disagree.

Regenerate the snapshot after editing this file:

    python3 connectors.py > frontend/src/connectors.registry.json

Vocabulary (product decision, Connections ↔ Work initiative):

  Connection  how Trovis RECEIVES information — a system it is connected to
              in order to observe work. This registry names the KINDS of
              connection (connectors); connect_health.py reports their state.
  Agent       a worker Trovis DISCOVERED through a connection. One OpenClaw
              connection may expose ten agents. Connections ≠ Fleet.
  Work        operational truth derived from what those connections let
              Trovis observe. Nothing here touches the Work model.

Truth rules:

  * ``available`` means the CURRENT codebase has a real, viable connection
    path — a live Connect door, a shipped SDK/plugin, an OAuth + webhook
    adapter, or the OTLP receiver. Anything planned is ``coming_soon``,
    whatever its logo says on a landing page.
  * ``observes`` lists the Work Coverage dimensions a connection of this
    kind CAN contribute (execution / actions / external_outcomes / handoffs
    / cost). They are capabilities, never a promise that a given run has
    them observed — Coverage decides that per run, from evidence.
  * ``stamps`` documents how connect_health.identify_connector recognises
    telemetry from this connector on the wire. It is documentation of the
    identity ladder, not a second implementation of it.
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field
from typing import Any

# --- vocabularies -----------------------------------------------------------

CATEGORIES: tuple[str, ...] = (
    "ai_worker",       # a single AI assistant/worker that reports in (a GPT, a Bot)
    "agent_platform",  # a framework/runtime that runs agents (SDKs, OpenClaw)
    "work_system",     # a business system where work lives (payments, CRM, orders)
    "communication",   # where people and agents talk (chat, support inbox)
    "custom",          # anything else that speaks OpenTelemetry
)

METHODS: tuple[str, ...] = (
    "otel",     # OTLP/HTTP spans to POST /v1/traces
    "sdk",      # trovis-agents pip package (wraps OTEL)
    "plugin",   # a first-party plugin inside the platform (trovis-openclaw-plugin)
    "mcp",      # the worker reports in over an MCP server Trovis mounts
    "oauth",    # Trovis is authorised into the system's account
    "webhook",  # the system pushes events to Trovis
    "actions",  # GPT Actions (OpenAPI) — a custom GPT calls Trovis
)

AVAILABILITY: tuple[str, ...] = ("available", "coming_soon")

# How a connection of this kind is SET UP. Decides which Connect surface a
# click lands on and what the guided flow can promise.
#   sdk      pip install + init() lines (a manual-wizard tile)
#   plugin   install a first-party plugin inside the platform (a tile)
#   actions  configure a custom GPT's Action + OAuth (a tile)
#   mcp      the worker adds the Trovis MCP server (a tile)
#   recipe   copy-paste OTEL exporter config (a tile in the "recipe" row)
#   guide    no tile of its own — the AI guide handles it (custom OTEL)
#   oauth    Trovis authorises into the system (a work-system Connect button)
#   none     nothing to set up yet (coming soon)
SETUP_TYPES: tuple[str, ...] = (
    "sdk", "plugin", "actions", "mcp", "recipe", "guide", "oauth", "none",
)

# Setup types that are picker tiles in the manual wizard. `recipe` renders in
# the "Or send traces yourself" row; the rest in the main grid.
TILE_SETUP_TYPES: tuple[str, ...] = ("sdk", "plugin", "actions", "mcp", "recipe")

# The Work Coverage dimensions (work_coverage.DIMENSIONS) a connection of a
# kind can contribute. Kept as a tuple here rather than imported so this
# module stays dependency-free (the frontend snapshot is generated from it).
OBSERVES: tuple[str, ...] = (
    "execution", "actions", "external_outcomes", "handoffs", "cost",
)

# What "manage this connection" can mean, per setup path. The product explains
# the difference instead of offering a Disconnect that does nothing.
#   stop_exporter     Trovis cannot remotely disable a raw exporter: stop
#                     exporting from the app or rotate the API key
#   revoke_key        rotate/revoke the org API key the SDK was given
#   remove_plugin     disable or remove the Trovis plugin in the platform
#   remove_action     remove the Trovis Action from the GPT / revoke OAuth
#   remove_mcp        remove the Trovis MCP server from the worker's config
#   oauth_disconnect  DELETE the provider connection (tokens revoked)
#   none              nothing to manage yet
MANAGEMENT: tuple[str, ...] = (
    "stop_exporter", "revoke_key", "remove_plugin", "remove_action",
    "remove_mcp", "oauth_disconnect", "none",
)


@dataclass(frozen=True)
class Connector:
    id: str
    name: str
    category: str
    availability: str
    methods: tuple[str, ...]
    setup_type: str
    # Coverage dimensions a connection of this kind can contribute.
    observes: tuple[str, ...]
    # Does one connection of this kind expose agents Trovis discovers
    # (Fleet rows)? False for work systems: they enrich Work, never invent
    # workers.
    discovers_agents: bool
    # Can one account hold several connections of this kind at once
    # (several OpenAI agents, several Shopify shops)? A capability of the
    # kind; the SaaS tables enforce one-per-provider until phase 9.
    supports_multiple_instances: bool
    management: str
    # The method connect_health reports when telemetry carries this
    # connector's explicit `trovis.connector.id` stamp, or None when the id
    # alone does not settle it (chatgpt: Actions and MCP write the same id).
    explicit_method: str | None = None
    # How identify_connector recognises this connector's telemetry (docs).
    stamps: tuple[str, ...] = ()
    # Labels the Connect surfaces show for this connector. `tile_*` are the
    # manual-wizard picker tile; `guide_label` is the AI guide's opening chip.
    tile_label: str | None = None
    tile_subtitle: str | None = None
    guide_label: str | None = None
    # Implementation variants shown on a sub-step under ONE tile (Claude Agent
    # SDK vs Managed Agents). Each {id, label, subtitle}; ids are the
    # instructions-page ids the wizard already dispatches on.
    variants: tuple[dict[str, str], ...] = ()
    # Per-connector facts the setup assistant needs that are not commands:
    # disambiguation, what the door can and cannot do.
    setup_notes: str | None = None

    def to_public(self) -> dict[str, Any]:
        d = asdict(self)
        d["methods"] = list(self.methods)
        d["observes"] = list(self.observes)
        d["stamps"] = list(self.stamps)
        d["variants"] = [dict(v) for v in self.variants]
        return d


_TELEMETRY_OBSERVES = ("execution", "actions", "handoffs", "cost")

CONNECTORS: tuple[Connector, ...] = (
    Connector(
        id="openclaw",
        name="OpenClaw",
        category="agent_platform",
        availability="available",
        methods=("plugin",),
        setup_type="plugin",
        observes=_TELEMETRY_OBSERVES,
        discovers_agents=True,
        supports_multiple_instances=True,
        management="remove_plugin",
        explicit_method="plugin",
        stamps=("trovis.connector.id=openclaw", "openclaw.gateway.version present"),
        tile_label="OpenClaw",
        tile_subtitle="AI agent platform — agents connect themselves",
        guide_label="OpenClaw",
        setup_notes=(
            "The trovis plugin runs inside the OpenClaw gateway; one plugin "
            "install can expose every agent the gateway runs. /trovis capture "
            "on is what makes runs land as named Work."
        ),
    ),
    Connector(
        id="openai-agents",
        name="OpenAI Agents SDK",
        category="agent_platform",
        availability="available",
        methods=("sdk",),
        setup_type="sdk",
        observes=_TELEMETRY_OBSERVES,
        discovers_agents=True,
        supports_multiple_instances=True,
        management="revoke_key",
        explicit_method="sdk",
        stamps=("trovis.connector.id=openai-agents", "trovis.sdk.platform=openai"),
        tile_label="OpenAI Agents SDK",
        tile_subtitle="OpenAI native agent framework",
        guide_label="OpenAI Agents SDK",
        setup_notes="pip install trovis-agents[openai]; init() before the framework import.",
    ),
    Connector(
        id="claude",
        name="Claude Agents",
        category="agent_platform",
        availability="available",
        methods=("sdk",),
        setup_type="sdk",
        observes=_TELEMETRY_OBSERVES,
        discovers_agents=True,
        supports_multiple_instances=True,
        management="revoke_key",
        explicit_method="sdk",
        stamps=(
            "trovis.connector.id=claude",
            "trovis.sdk.platform=anthropic",
            "trovis.sdk.platform=claude-agent-sdk",
        ),
        tile_label="Claude Agents",
        tile_subtitle="Claude Agent SDK or Managed Agents",
        guide_label="Claude Agent SDK / Claude Code",
        variants=(
            {
                "id": "claude-agent-sdk",
                "label": "Claude Agent SDK",
                "subtitle": "query() + ClaudeSDKClient — the Claude Code engine",
            },
            {
                "id": "claude-agents",
                "label": "Claude Managed Agents",
                "subtitle": "client.beta.agents + beta.sessions API",
            },
        ),
        setup_notes=(
            "Two variants under one connector: the Claude Agent SDK "
            "(query()/ClaudeSDKClient — pip install trovis-agents[claude-agent-sdk]) "
            "and Anthropic Managed Agents (client.beta.agents — pip install "
            "trovis-agents[anthropic]). Claude Code the CLI exports OTEL itself."
        ),
    ),
    Connector(
        id="chatgpt",
        name="ChatGPT custom GPT",
        category="ai_worker",
        availability="available",
        methods=("actions", "oauth"),
        setup_type="actions",
        observes=("execution", "actions"),
        discovers_agents=True,
        supports_multiple_instances=True,
        management="remove_action",
        explicit_method=None,
        stamps=("trovis.connector.id=chatgpt", "trovis.platform=chatgpt (legacy)"),
        tile_label="ChatGPT (custom GPT)",
        tile_subtitle="Monitor + query a GPT via Actions — no code",
        guide_label="ChatGPT (custom GPT)",
        setup_notes=(
            "No code, no pip, no API key: the GPT gets Trovis as an Action over "
            "OAuth and reports its own activity (connectAgent / logActivity / "
            "reportComplete) — it can also ask about the fleet (askFleet). No "
            "token-level cost is visible; the GPT runs on OpenAI's side."
        ),
    ),
    Connector(
        id="grok",
        name="Grok (xAI SDK)",
        category="agent_platform",
        availability="available",
        methods=("sdk", "otel"),
        setup_type="sdk",
        observes=_TELEMETRY_OBSERVES,
        discovers_agents=True,
        supports_multiple_instances=True,
        management="revoke_key",
        explicit_method="sdk",
        stamps=("trovis.connector.id=grok", "trovis.sdk.platform=xai"),
        tile_label="Grok (xAI SDK)",
        tile_subtitle="Already OpenTelemetry-instrumented — two lines",
        guide_label="Grok (xAI SDK)",
        setup_notes=(
            "For apps built on the xai-sdk package, which already emits "
            "OpenTelemetry: pip install trovis-agents[xai] and init() before "
            "the xAI Client is created. NOT the Grok Bot door."
        ),
    ),
    Connector(
        id="grok-bot",
        name="Grok Bot",
        category="ai_worker",
        availability="available",
        methods=("mcp",),
        setup_type="mcp",
        observes=("execution", "actions", "handoffs"),
        discovers_agents=True,
        supports_multiple_instances=True,
        management="remove_mcp",
        explicit_method="mcp",
        stamps=(
            "trovis.connector.id=grok-bot",
            "trovis.platform=grok-bot / cursor-grok-bot (legacy)",
        ),
        tile_label="Grok Bot",
        tile_subtitle="Desktop assistant — it reports in over MCP",
        guide_label="Grok Bot",
        setup_notes=(
            "A desktop assistant (the kind someone runs in Cursor) that exports "
            "nothing, so it REPORTS to Trovis over MCP; nothing is recorded "
            "unless the Bot calls the report_job_* tools. NOT the xAI SDK door."
        ),
    ),
    Connector(
        id="cursor",
        name="Cursor",
        category="ai_worker",
        availability="available",
        methods=("otel",),
        setup_type="recipe",
        observes=_TELEMETRY_OBSERVES,
        discovers_agents=True,
        supports_multiple_instances=True,
        management="stop_exporter",
        explicit_method="otel",
        stamps=("trovis.connector.id=cursor (set by the recipe)",),
        tile_label="Cursor",
        tile_subtitle="Send traces over OpenTelemetry — no plugin",
        guide_label="Cursor (OpenTelemetry)",
        setup_notes=(
            "A recipe, not a plugin: anything run from Cursor that emits "
            "OpenTelemetry points its exporter at Trovis."
        ),
    ),
    Connector(
        id="custom-otel",
        name="Custom (OpenTelemetry)",
        category="custom",
        availability="available",
        methods=("otel",),
        setup_type="guide",
        observes=_TELEMETRY_OBSERVES,
        discovers_agents=True,
        supports_multiple_instances=True,
        management="stop_exporter",
        explicit_method="otel",
        stamps=("any telemetry with no Trovis-owned stamp",),
        guide_label="Custom Python / other",
        setup_notes=(
            "Anything that emits OpenTelemetry spans: Python via pip install "
            "trovis-agents + init(platform=\"auto\"), or any OTLP/HTTP exporter "
            "pointed at POST /v1/traces with the X-Trovis-Api-Key header."
        ),
    ),
    Connector(
        id="stripe",
        name="Stripe",
        category="work_system",
        availability="available",
        methods=("oauth", "webhook"),
        setup_type="oauth",
        observes=("external_outcomes",),
        discovers_agents=False,
        supports_multiple_instances=True,
        management="oauth_disconnect",
        setup_notes=(
            "Independent evidence of payment and refund outcomes, linked to a "
            "run only through an explicit trovis_loop_external_id / "
            "trovis_run_id in the Stripe object's metadata. Not Trovis billing."
        ),
    ),
    Connector(
        id="hubspot",
        name="HubSpot",
        category="work_system",
        availability="available",
        methods=("oauth", "webhook"),
        setup_type="oauth",
        observes=("external_outcomes",),
        discovers_agents=False,
        supports_multiple_instances=True,
        management="oauth_disconnect",
        setup_notes=(
            "Independent evidence of deal and ticket state, linked to a run "
            "only through an explicit link key on the HubSpot object."
        ),
    ),
    Connector(
        id="shopify",
        name="Shopify",
        category="work_system",
        availability="available",
        methods=("oauth", "webhook"),
        setup_type="oauth",
        observes=("external_outcomes",),
        discovers_agents=False,
        supports_multiple_instances=True,
        management="oauth_disconnect",
        setup_notes=(
            "Independent evidence of order, payment and fulfillment state, "
            "linked to a run only through an explicit link key on the order."
        ),
    ),
    # Recognised in Work today (brand marks), no direct door yet.
    Connector(
        id="slack",
        name="Slack",
        category="communication",
        availability="coming_soon",
        methods=("oauth",),
        setup_type="none",
        observes=("handoffs",),
        discovers_agents=False,
        supports_multiple_instances=True,
        management="none",
    ),
    Connector(
        id="github",
        name="GitHub",
        category="work_system",
        availability="coming_soon",
        methods=("oauth",),
        setup_type="none",
        observes=("external_outcomes",),
        discovers_agents=False,
        supports_multiple_instances=True,
        management="none",
    ),
    Connector(
        id="intercom",
        name="Intercom",
        category="communication",
        availability="coming_soon",
        methods=("oauth",),
        setup_type="none",
        observes=("handoffs", "external_outcomes"),
        discovers_agents=False,
        supports_multiple_instances=True,
        management="none",
    ),
)

_BY_ID: dict[str, Connector] = {c.id: c for c in CONNECTORS}


def get(connector_id: str | None) -> Connector | None:
    """Connector by id. Unknown → None, never a throw."""
    if not isinstance(connector_id, str):
        return None
    return _BY_ID.get(connector_id.strip().lower())


def ids() -> tuple[str, ...]:
    return tuple(c.id for c in CONNECTORS)


def available() -> tuple[Connector, ...]:
    return tuple(c for c in CONNECTORS if c.availability == "available")


def telemetry_ids() -> tuple[str, ...]:
    """Connectors connect_health attributes from stored spans: every
    available connector whose data path is telemetry (not OAuth)."""
    return tuple(
        c.id for c in CONNECTORS
        if c.availability == "available" and c.setup_type != "oauth"
    )


def saas_ids() -> tuple[str, ...]:
    """Work systems with a durable OAuth row in saas_connections."""
    return tuple(
        c.id for c in CONNECTORS
        if c.availability == "available" and c.setup_type == "oauth"
    )


def tile_ids() -> tuple[str, ...]:
    """The manual wizard's picker tiles, in registry order."""
    return tuple(
        c.id for c in CONNECTORS
        if c.availability == "available" and c.setup_type in TILE_SETUP_TYPES
    )


def explicit_methods() -> dict[str, str | None]:
    return {c.id: c.explicit_method for c in CONNECTORS if c.id in telemetry_ids()}


def to_public() -> list[dict[str, Any]]:
    return [c.to_public() for c in CONNECTORS]


def validate() -> None:
    """Internal consistency — raised at import so a bad entry never ships."""
    seen: set[str] = set()
    for c in CONNECTORS:
        assert c.id not in seen, f"duplicate connector id {c.id}"
        seen.add(c.id)
        assert c.category in CATEGORIES, c.id
        assert c.availability in AVAILABILITY, c.id
        assert c.methods and all(m in METHODS for m in c.methods), c.id
        assert len(set(c.methods)) == len(c.methods), c.id
        assert c.setup_type in SETUP_TYPES, c.id
        assert all(o in OBSERVES for o in c.observes), c.id
        assert c.management in MANAGEMENT, c.id
        assert c.explicit_method is None or c.explicit_method in METHODS, c.id
        if c.availability == "coming_soon":
            assert c.setup_type == "none" and c.management == "none", c.id
        else:
            assert c.setup_type != "none", c.id
        if c.setup_type in TILE_SETUP_TYPES:
            assert c.tile_label and c.tile_subtitle, f"{c.id} tile needs labels"
        if c.setup_type in TILE_SETUP_TYPES or c.setup_type == "guide":
            assert c.guide_label, f"{c.id} needs a guide chip label"
        for v in c.variants:
            assert set(v) == {"id", "label", "subtitle"}, c.id


validate()


def main(argv: list[str] | None = None) -> int:
    """Print the public snapshot the frontend commits."""
    sys.stdout.write(json.dumps(to_public(), indent=2, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
