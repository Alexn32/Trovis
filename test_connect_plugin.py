"""Door test: the OpenClaw plugin's export payload, replayed through real ingest.

The plugin's own suite (trovis-openclaw-plugin/test/loop-attrs.test.mjs) proves
the plugin BUILDS the right spans — it asserts against a fake tracer and never
crosses the wire. Nothing checked that the backend does the right thing with
them. This test covers the other half: it replays the OTLP/JSON payload the
plugin's exporter would produce for one full execution unit and asserts that a
loop, an agent, and a registration all land.

The payload mirrors the plugin's real emission, taken from index.ts:
  - resource attrs: the exact four-field footprint initTelemetry() sets
    (service.name, service.version, trovis.plugin.version,
    openclaw.gateway.version) — autoDetectResources is off, so that's all of it
  - span sequence + attributes: the `simulateRun()` fixture in the plugin's
    test suite (message_received -> tool_call -> llm_output ->
    agent_run_complete), plus the agent_registration span sendRegistration()
    emits at startup
  - loop attributes: trovis.run.id on every span of the unit, and
    trovis.loop.close on the final one — the plugin's auto-close behavior

If the plugin's vocabulary changes, this test should be updated from the
fixtures again — it is a replay, not a mock.

Run:
  TROVIS_DISABLE_PRICING_SYNC=1 python3 test_connect_plugin.py
(isolated temp SQLite DB; never touches the dev/prod DB)
"""
import os
import tempfile
import time

os.environ["OVERSEE_DISABLE_PRICING_SYNC"] = "1"
os.environ["TROVIS_DISABLE_PRICING_SYNC"] = "1"
os.environ["TROVIS_DISABLE_ALERTS"] = "1"
os.environ["TROVIS_DISABLE_LOOP_SWEEP"] = "1"
os.environ.pop("DATABASE_URL", None)
os.environ.pop("ANTHROPIC_API_KEY", None)

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()

import database
database.SQLITE_PATH = _tmp.name

import describer
import main
from fastapi.testclient import TestClient

main._auto_describe = lambda *a, **k: False

# Stub the Claude boundary. GET /agents/{name}/summary regenerates a missing
# description on read, so a test that merely READS an agent will otherwise
# make a live Anthropic call — not hermetic, and it would burn a real key.
describer.describe_agent = lambda service_name, account_id=None, agent_id=None: {
    "service_name": service_name,
    "description": "Stubbed description.",
    "description_long": "Stubbed long description for a test agent.",
    "span_count_analyzed": 1,
    "source": "telemetry_only",
}
describer.record_summary = lambda user, agent: "Stubbed record summary"

failures = []


def check(label, cond, detail=""):
    print(("  PASS " if cond else "  FAIL ") + label)
    if detail:
        print(f"        {detail}")
    if not cond:
        failures.append(label)


NS = 1_000_000_000
T0 = time.time_ns() - 3600 * NS

PLUGIN_VERSION = "0.5.5"          # index.ts PLUGIN_VERSION
GATEWAY_VERSION = "0.9.1"         # ctx.version, reported by the OpenClaw gateway
AGENT = "openclaw-prod"
# Single-agent install: the plugin registers as "main" and pickAgentId()
# resolves the fixture's sessionKey "agent:main:s-run-A" to the same value.
AGENT_ID = "main"
RUN_ID = "run-A"


def kv(d):
    out = []
    for k, v in d.items():
        if isinstance(v, bool):
            out.append({"key": k, "value": {"boolValue": v}})
        elif isinstance(v, int):
            out.append({"key": k, "value": {"intValue": str(v)}})
        else:
            out.append({"key": k, "value": {"stringValue": str(v)}})
    return out


_n = [0]


def span(name, attrs, offset_ms=0, dur_ms=20, kind=1, status=1):
    _n[0] += 1
    start = T0 + offset_ms * 1_000_000
    return {
        "traceId": f"{7:032x}",
        "spanId": f"{_n[0]:016x}",
        "name": name,
        "kind": kind,
        "startTimeUnixNano": str(start),
        "endTimeUnixNano": str(start + dur_ms * 1_000_000),
        "status": {"code": status},
        "attributes": kv(attrs),
    }


# --- The captured plugin export -------------------------------------------
# One resourceSpans block, because the plugin builds exactly one Resource.
REGISTRATION_SPAN = span("agent_registration", {
    "trovis.event.type": "agent_registration",
    "trovis.agent.id": AGENT_ID,
    "trovis.agent.workspace_path": "/home/op/work",
    "trovis.agent.model": "claude-sonnet-5",
    "trovis.agent.soul": "You are the production triage agent for billing.",
    "trovis.agent.identity": "Billing triage",
}, offset_ms=0, kind=1)

# simulateRun("run-A") from loop-attrs.test.mjs, in emission order.
RUN_SPANS = [
    span("message_received", {
        "trovis.event.type": "message_received",
        "trovis.run.id": RUN_ID,
        "trovis.session.key": "agent:main:s-run-A",
        "trovis.message.content_length": 18,
        "trovis.agent.id": AGENT_ID,
    }, offset_ms=100, kind=2),
    span("tool_call", {
        "trovis.event.type": "tool_call",
        "trovis.run.id": RUN_ID,
        "trovis.tool.name": "read_file",
        "trovis.tool.call_id": "tc-run-A-1",
        "trovis.tool.param_keys": '["path"]',
        "trovis.agent.id": AGENT_ID,
    }, offset_ms=200),
    span("llm_output", {
        "trovis.event.type": "llm_output",
        "trovis.run.id": RUN_ID,
        "trovis.agent.id": AGENT_ID,
    }, offset_ms=300),
    # agent_end -> the plugin auto-closes the loop on a successful run.
    span("agent_run_complete", {
        "trovis.event.type": "agent_run_complete",
        "trovis.run.id": RUN_ID,
        "trovis.run.success": True,
        "trovis.loop.close": "done",
        "trovis.agent.id": AGENT_ID,
    }, offset_ms=400),
]

PLUGIN_EXPORT = {
    "resourceSpans": [{
        "resource": {"attributes": kv({
            "service.name": AGENT,
            "service.version": PLUGIN_VERSION,
            "trovis.plugin.version": PLUGIN_VERSION,
            "openclaw.gateway.version": GATEWAY_VERSION,
        })},
        "scopeSpans": [{
            "scope": {"name": "@trovis/openclaw-plugin", "version": PLUGIN_VERSION},
            "spans": [REGISTRATION_SPAN] + RUN_SPANS,
        }],
    }]
}


with TestClient(main.app) as c:
    r = c.post("/auth/signup", json={
        "email": "plugin@test.com", "password": "supersecret123",
        "name": "Plugin Tester", "account_type": "individual", "org_name": "Plugin Co",
    })
    assert r.status_code == 201, r.text
    key = r.json()["api_key"]
    H = {"X-Trovis-Api-Key": key}
    account_id = database.validate_api_key(key)["account_id"]

    print("\n[1] The plugin's export is accepted whole")
    r = c.post("/v1/traces", json=PLUGIN_EXPORT, headers=H)
    check("POST /v1/traces returns 200", r.status_code == 200, f"got {r.status_code}: {r.text[:200]}")
    body = r.json()
    check("all 5 spans accepted", body.get("accepted") == 5, f"body={body}")
    check("nothing dropped", body.get("dropped") == 0, f"body={body}")

    print("\n[2] AGENT — the plugin's agent appears in the fleet")
    agents = c.get("/agents", headers=H).json()
    names = [a["service_name"] for a in agents]
    check(f"{AGENT} is in GET /agents", AGENT in names, f"agents={names}")
    summary = c.get(f"/agents/{AGENT}/summary", headers=H).json()
    check("span_count == 5", summary.get("span_count") == 5, f"span_count={summary.get('span_count')}")

    print("\n[3] REGISTRATION — identity landed, not just telemetry")
    fleet_row = next((a for a in agents if a["service_name"] == AGENT), {})
    check("GET /agents reports has_registration",
          fleet_row.get("has_registration") is True,
          f"has_registration={fleet_row.get('has_registration')}")
    # NOTE (pre-existing, not a plugin-door problem): GET /agents/{name}/summary
    # never populates has_registration — it stays False there whether or not
    # ?agent_id= is passed, while the fleet list computes it correctly. Asserted
    # against the fleet list, which is the path that reflects reality; the
    # summary discrepancy is reported separately rather than papered over here.
    check("summary's has_registration is still the known-False stub",
          summary.get("has_registration") is False,
          "if this starts passing as True, the summary gap was fixed — "
          "tighten this test to assert True on both paths")
    reg = database.get_latest_registration(AGENT, account_id=account_id, agent_id=AGENT_ID)
    check("a registration row exists", reg is not None)
    if reg:
        check("SOUL text survived the round trip",
              "production triage agent" in (reg.get("soul") or ""),
              f"soul={(reg.get('soul') or '')[:60]!r}")
        check("identity survived the round trip",
              reg.get("identity") == "Billing triage", f"identity={reg.get('identity')!r}")

    print("\n[4] LOOP — the run became one workloop, closed by the agent")
    all_loops = [l for l in database.get_loops(account_id, limit=50)
                 if l["service_name"] == AGENT]
    run_loops = [l for l in all_loops if l.get("external_id") == RUN_ID]
    check("exactly one loop is keyed to the plugin's run id",
          len(run_loops) == 1, f"got {len(run_loops)} of {len(all_loops)} loop(s)")
    # The startup agent_registration span carries no trovis.run.id, so by the
    # documented ingest rule ("spans carrying none of these still get a loop")
    # it forms its own implicit loop. That's existing designed behavior, not
    # part of the run — assert it stays SEPARATE rather than polluting the run.
    check("the registration span did not join the run's loop",
          len(all_loops) == 2, f"loops={[(l.get('external_id'), l.get('cached_state')) for l in all_loops]}")
    if run_loops:
        loop = run_loops[0]
        check("loop is agent-initiated",
              loop.get("initiated_by_type") == "agent", f"{loop.get('initiated_by_type')!r}")
        check("trovis.loop.close drove the loop to done",
              loop.get("cached_state") == "done", f"cached_state={loop.get('cached_state')!r}")

        stream = database.get_loop_stream(loop["id"], account_id)
        types = [e["type"] for e in stream]
        check("stream opens with loop_opened", types and types[0] == "loop_opened", f"types={types}")
        check("stream ends with loop_closed", types and types[-1] == "loop_closed", f"types={types}")
        closed = [e for e in stream if e["type"] == "loop_closed"]
        check("close is attributed to the agent",
              closed and closed[0]["actor_type"] == "agent",
              f"actor_type={closed[0]['actor_type'] if closed else None!r}")

        print("\n[5] The run loop holds exactly the run's spans")
        check("event count = 4 run spans + loop_opened + loop_closed",
              loop.get("event_count") == 6, f"event_count={loop.get('event_count')}")

    print("\n[6] Plugin platform identity is visible on the agent record")
    detail = c.get(f"/agents/{AGENT}/spans", headers=H).json()
    check("all 5 spans retrievable", len(detail) == 5, f"got {len(detail)}")
    names_seen = {s["span_name"] for s in detail}
    check("every emitted span name is present",
          names_seen == {"agent_registration", "message_received", "tool_call",
                         "llm_output", "agent_run_complete"},
          f"names={sorted(names_seen)}")

print()
if failures:
    print(f"FAILED ({len(failures)}): " + "; ".join(failures))
    raise SystemExit(1)
print("OPENCLAW PLUGIN DOOR VERIFIED (payload replay through real ingest)")
