"""Door test: the trovis-agents Python SDK, end to end over real HTTP.

trovis-agents/test_smoke.py proves the SDK doesn't crash — it exports to a dead
port on purpose. That is a different claim from "telemetry arrives", which is
the claim the Connect page actually makes. This test closes that gap: it boots
the real FastAPI app on a real socket, points a real `trovis.init()` at it, and
asserts the spans come out the other end in GET /agents.

The SDK's exporter uses `requests`, so TestClient's in-process transport can't
exercise it — hence uvicorn on an ephemeral port, in-process so it shares the
temp-DB module state.

Skips (exit 0) when the SDK isn't importable, so the backend CI job doesn't
need the SDK installed; the `sdk` CI job runs it with the package present.

Run:
  TROVIS_DISABLE_PRICING_SYNC=1 python3 test_connect_sdk.py
(isolated temp SQLite DB; never touches the dev/prod DB)
"""
import os
import socket
import sys
import tempfile
import threading
import time

os.environ["OVERSEE_DISABLE_PRICING_SYNC"] = "1"
os.environ["TROVIS_DISABLE_PRICING_SYNC"] = "1"
os.environ["TROVIS_DISABLE_ALERTS"] = "1"
os.environ["TROVIS_DISABLE_LOOP_SWEEP"] = "1"
os.environ.pop("DATABASE_URL", None)
os.environ.pop("ANTHROPIC_API_KEY", None)
# A configured shell must not leak into the assertions below.
for _k in ("TROVIS_ENDPOINT", "OVERSEE_ENDPOINT", "TROVIS_AGENT_NAME",
           "OVERSEE_AGENT_NAME", "TROVIS_API_KEY", "OVERSEE_API_KEY"):
    os.environ.pop(_k, None)

# The SDK lives beside the backend in this repo but isn't installed into the
# backend's environment. Import it from source when it isn't on the path.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "trovis-agents"))
try:
    import trovis
except ImportError as e:  # pragma: no cover - environment-dependent
    # A skip is fine on a dev box without the SDK's OTEL deps. It is NOT fine
    # in CI: a silently-skipped door test is a green build for an unverified
    # door, which is the exact failure this suite exists to prevent. CI sets
    # TROVIS_REQUIRE_SDK=1 so a missing SDK fails the build instead.
    msg = f"trovis-agents not importable ({e})"
    if os.environ.get("TROVIS_REQUIRE_SDK") == "1":
        print(f"FAILED — {msg}. TROVIS_REQUIRE_SDK=1 means this door must be "
              f"verified, not skipped. Install it: pip install -e ./trovis-agents")
        raise SystemExit(1)
    print(f"SKIP — {msg}. Set TROVIS_REQUIRE_SDK=1 to make this a hard failure.")
    raise SystemExit(0)

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()

import database
database.SQLITE_PATH = _tmp.name

import describer
import main
import requests
import uvicorn

main._auto_describe = lambda *a, **k: False

# Stub the Claude boundary. GET /agents/{name}/summary regenerates a missing
# description on read, so a test that merely READS an agent will otherwise
# make a live Anthropic call — not hermetic, and it would burn a real key.
# The server runs in-process, so patching here covers the request handlers.
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


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# --- Boot the real app on a real socket -----------------------------------
PORT = free_port()
config = uvicorn.Config(main.app, host="127.0.0.1", port=PORT, log_level="error")
server = uvicorn.Server(config)
thread = threading.Thread(target=server.run, daemon=True)
thread.start()

deadline = time.time() + 30
while not server.started and time.time() < deadline:
    time.sleep(0.05)
if not server.started:
    print("FAILED: uvicorn did not start within 30s")
    raise SystemExit(1)

BASE = f"http://127.0.0.1:{PORT}"
print(f"  (live server on {BASE})")

class Http:
    """Minimal client for the live server. TestClient is deliberately NOT
    used here: it drives the app's lifespan, and the MCP session manager
    mounted in that lifespan refuses to run twice in one process — so
    TestClient and uvicorn cannot coexist. Everything below therefore goes
    over the same real socket the SDK uses."""

    @staticmethod
    def post(path, **kw):
        return requests.post(BASE + path, timeout=30, **kw)

    @staticmethod
    def get(path, **kw):
        return requests.get(BASE + path, timeout=30, **kw)


try:
    c = Http()
    r = c.post("/auth/signup", json={
        "email": "sdk@test.com", "password": "supersecret123",
        "name": "SDK Tester", "account_type": "individual", "org_name": "SDK Co",
    })
    assert r.status_code == 201, r.text
    key = r.json()["api_key"]
    H = {"X-Trovis-Api-Key": key}

    print("\n[1] init() against the live server reports a real connection")
    from trovis.core import _probe_endpoint

    ok, reason = _probe_endpoint(f"{BASE}/v1/traces", key)
    check("endpoint probe succeeds with a valid key", ok, f"reason={reason}")
    bad_ok, bad_reason = _probe_endpoint(f"{BASE}/v1/traces", "ov_sk_not_a_real_key")
    check("endpoint probe reports a rejected key rather than claiming success",
          not bad_ok and "401" in (bad_reason or ""), f"reason={bad_reason}")

    print("\n[2] Real init() + real span emission")
    trovis.init(
        api_key=key,
        agent_name="sdk-door-agent",
        endpoint=f"{BASE}/v1/traces",
        capture_outputs=False,
    )
    check("init() completed", True)

    from opentelemetry import trace

    tracer = trace.get_tracer("sdk-door-test")
    with tracer.start_as_current_span("plan_work") as span:
        span.set_attribute("trovis.event.type", "tool_call")
        span.set_attribute("trovis.tool.name", "plan_work")
    with tracer.start_as_current_span("call_model") as span:
        span.set_attribute("trovis.event.type", "model_call")
        span.set_attribute("gen_ai.request.model", "claude-sonnet-5")
        span.set_attribute("gen_ai.usage.input_tokens", 800)
        span.set_attribute("gen_ai.usage.output_tokens", 210)

    print("\n[3] Flush — spans travel over a real socket to the real ingest path")
    provider = trace.get_tracer_provider()
    flushed = provider.force_flush(timeout_millis=15_000)
    check("force_flush reported success", bool(flushed), f"returned {flushed!r}")

    print("\n[4] The agent arrived (this is what test_smoke.py never proved)")
    # BatchSpanProcessor hands off asynchronously; poll briefly rather than
    # racing it with a bare sleep.
    agent_names = []
    deadline = time.time() + 15
    while time.time() < deadline:
        agent_names = [a["service_name"] for a in c.get("/agents", headers=H).json()]
        if "sdk-door-agent" in agent_names:
            break
        time.sleep(0.25)
    check("sdk-door-agent is visible in GET /agents", "sdk-door-agent" in agent_names,
          f"agents={agent_names}")

    print("\n[5] Both spans landed with their names intact")
    summary = c.get("/agents/sdk-door-agent/summary", headers=H)
    check("summary returns 200", summary.status_code == 200, f"got {summary.status_code}")
    body = summary.json() if summary.status_code == 200 else {}
    check("span_count == 2", body.get("span_count") == 2, f"span_count={body.get('span_count')}")
    ops = body.get("top_operations") or []
    check("both operation names present",
          "plan_work" in ops and "call_model" in ops, f"top_operations={ops}")

    print("\n[6] The SDK stamped its own resource identity on the spans")
    rows = c.get("/agents/sdk-door-agent/spans", headers=H).json()
    check("span rows retrievable", len(rows) == 2, f"got {len(rows)}")
    check("spans carry a trace id", all(r.get("trace_id") for r in rows))

    print("\n[7] set_loop_title / trovis.loop.title → named Work")
    # The SDK helper (and a manual span that carries the same attr) is the
    # documented way to get title_source=provided without an LLM title.
    trovis.set_loop_title("Plan the Q3 rollout")
    with tracer.start_as_current_span("message_received") as span:
        from trovis.loop_attrs import apply_loop_attrs
        span.set_attribute("trovis.event.type", "message_received")
        apply_loop_attrs(span, run_id="sdk-run-q3", title="Plan the Q3 rollout")
    flushed = provider.force_flush(timeout_millis=15_000)
    check("titled span flushed", bool(flushed), f"returned {flushed!r}")

    titled = None
    deadline = time.time() + 15
    while time.time() < deadline:
        loops = [l for l in database.get_loops(
            database.validate_api_key(key)["account_id"], limit=50)
                 if l.get("external_id") == "sdk-run-q3"]
        if loops:
            titled = loops[0]
            break
        time.sleep(0.25)
    check("titled loop landed", titled is not None)
    if titled:
        check("title is the helper value",
              titled.get("title") == "Plan the Q3 rollout",
              f"title={titled.get('title')!r}")
        with database._connect() as conn, database._cursor(conn) as cur:
            cur.execute("SELECT title_source FROM loops WHERE id = ?",
                        (titled["id"],))
            src = cur.fetchone()["title_source"]
        check("title_source=provided",
              src == "provided", f"title_source={src!r}")

    page = c.get("/work/items", headers=H).json()
    titles = [it.get("title") for it in page.get("items") or []]
    check("named work appears in lean GET /work/items",
          "Plan the Q3 rollout" in titles, f"titles={titles}")

finally:
    server.should_exit = True
    thread.join(timeout=10)

print()
if failures:
    print(f"FAILED ({len(failures)}): " + "; ".join(failures))
    raise SystemExit(1)
print("PYTHON SDK DOOR VERIFIED (live server, real export)")
