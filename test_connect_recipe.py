"""Door test: the OTEL recipes Trovis hands out actually land.

test_connect_otlp.py posts hand-built JSON. That proves the ingest CONTRACT
and nothing about the DOOR — which is how three separate breakages sat in the
Connect page unnoticed: the copy-paste recipe sent protobuf to a JSON-only
endpoint, pointed the base-url variable at a full URL (so every batch went to
/v1/traces/v1/traces), and ran plain `python`, which never bootstraps the SDK
at all. Each one silently dropped 100% of spans.

So this test runs the recipe itself. The env-var command is READ OUT OF
AddAgent.jsx rather than copied here: a recipe that regresses on the page
fails here, which a copy could never catch.

Covers:
  the explicit setup   — the SDK's own exporter (protobuf) against real ingest
  the quick setup      — the shipped env block, in a subprocess, end to end
  gzip                 — exporters compress when configured to
  hex + base64 ids     — protobuf carries ids as bytes; JSON carries hex

Skips (exit 0) when the OTEL SDK isn't importable; CI sets TROVIS_REQUIRE_SDK=1
to make that a hard failure.

Run:
  TROVIS_DISABLE_PRICING_SYNC=1 python3 test_connect_recipe.py
(isolated temp SQLite DB; never touches the dev/prod DB)
"""
import gzip
import json
import os
import re
import shutil
import socket
import subprocess
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
# A configured shell must not leak into a test about environment variables.
for _k in list(os.environ):
    if _k.startswith("OTEL_"):
        os.environ.pop(_k, None)

try:
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
except ImportError as e:  # pragma: no cover — environment-dependent
    msg = f"opentelemetry SDK not importable ({e})"
    if os.environ.get("TROVIS_REQUIRE_SDK") == "1":
        print(f"FAILED — {msg}. TROVIS_REQUIRE_SDK=1 means the recipes must be "
              f"verified, not skipped.")
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
describer.describe_agent = lambda service_name, account_id=None, agent_id=None: {
    "service_name": service_name,
    "description": "Stubbed description.",
    "description_long": "Stubbed long description.",
    "span_count_analyzed": 1,
    "source": "telemetry_only",
}

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


PORT = free_port()
server = uvicorn.Server(
    uvicorn.Config(main.app, host="127.0.0.1", port=PORT, log_level="error")
)
thread = threading.Thread(target=server.run, daemon=True)
thread.start()
deadline = time.time() + 30
while not server.started and time.time() < deadline:
    time.sleep(0.05)
if not server.started:
    print("FAILED: uvicorn did not start within 30s")
    raise SystemExit(1)

BASE = f"http://127.0.0.1:{PORT}"
ENDPOINT = f"{BASE}/v1/traces"
print(f"  (live server on {BASE})")


def agents(key):
    return [
        a["service_name"]
        for a in requests.get(
            f"{BASE}/agents", headers={"X-Trovis-Api-Key": key}, timeout=30
        ).json()
    ]


def wait_for_agent(key, name, seconds=15):
    end = time.time() + seconds
    while time.time() < end:
        if name in agents(key):
            return True
        time.sleep(0.25)
    return False


try:
    r = requests.post(f"{BASE}/auth/signup", timeout=30, json={
        "email": "recipe@test.com", "password": "supersecret123",
        "name": "Recipe Tester", "account_type": "individual",
        "org_name": "Recipe Co",
    })
    assert r.status_code == 201, r.text
    KEY = r.json()["api_key"]
    H = {"X-Trovis-Api-Key": KEY}

    print("\n[1] The explicit setup — the SDK's own exporter, as the page prints it")
    # opentelemetry-exporter-otlp-proto-http sends protobuf. This is the exact
    # shape of EXPLICIT_SETUP_TEMPLATE.
    provider = TracerProvider(
        resource=Resource.create({"service.name": "explicit-recipe-agent"})
    )
    provider.add_span_processor(
        SimpleSpanProcessor(OTLPSpanExporter(endpoint=ENDPOINT, headers=dict(H)))
    )
    trace.set_tracer_provider(provider)
    tracer = trace.get_tracer("explicit-recipe-agent")
    with tracer.start_as_current_span("handle_refund") as span:
        span.set_attribute("trovis.loop.title", "Approve refund for order #4821")
        span.set_attribute("trovis.loop.external_id", "recipe-explicit-1")
    provider.force_flush(15_000)
    check("the protobuf the Python exporter sends is accepted",
          wait_for_agent(KEY, "explicit-recipe-agent"), f"agents={agents(KEY)}")

    account_id = database.validate_api_key(KEY)["account_id"]
    titled = [l for l in database.get_loops(account_id, limit=50)
              if l.get("external_id") == "recipe-explicit-1"]
    check("and its attributes survive the protobuf decode (named Work)",
          bool(titled) and titled[0].get("title") == "Approve refund for order #4821",
          f"loop={titled[0] if titled else None}")
    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute("SELECT trace_id, span_id FROM spans WHERE service_name = ?",
                    ("explicit-recipe-agent",))
        row = cur.fetchone()
    check("ids are stored as hex, not base64 (protobuf carries raw bytes)",
          bool(row) and re.fullmatch(r"[0-9a-f]{32}", row["trace_id"] or "")
          and re.fullmatch(r"[0-9a-f]{16}", row["span_id"] or ""),
          f"trace_id={row['trace_id'] if row else None!r}")

    print("\n[2] The quick setup — the shipped env block, run for real")
    # Read the recipe out of the page. A copy here could drift; this cannot.
    page = open("frontend/src/AddAgent.jsx", encoding="utf-8").read()
    template = page[page.index("const QUICK_ENV_TEMPLATE ="):]
    template = template[template.index("`") + 1:template.index("`", template.index("`") + 1)]
    check("the recipe uses the signal-specific endpoint variable",
          "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=" in template
          and "\nOTEL_EXPORTER_OTLP_ENDPOINT=" not in template,
          "the generic one is a base url; the SDK appends /v1/traces to it")
    check("it does not ask Python for an exporter Python does not have",
          "http/json" not in template, "Python ships proto-http and grpc only")
    check("and it runs under opentelemetry-instrument",
          "opentelemetry-instrument python" in template,
          "env vars alone leave a ProxyTracerProvider — nothing records")

    env_lines = dict(
        re.findall(r"^(OTEL_[A-Z_]+)=(\S+)", template.replace(" \\\n", "\n"), re.M)
    )
    recipe_env = {
        k: v.replace("AGENT_NAME", "quick-recipe-agent")
             .replace("TROVIS_ENDPOINT", ENDPOINT)
             .replace("TROVIS_API_KEY", KEY)
        for k, v in env_lines.items()
    }
    check("the block carries every variable the run needs",
          {"OTEL_SERVICE_NAME", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
           "OTEL_TRACES_EXPORTER", "OTEL_EXPORTER_OTLP_HEADERS"}
          <= set(recipe_env), f"parsed={sorted(recipe_env)}")

    agent_py = os.path.join(tempfile.mkdtemp(), "your_agent.py")
    with open(agent_py, "w", encoding="utf-8") as fh:
        fh.write(
            "from opentelemetry import trace\n"
            "tracer = trace.get_tracer('quick-recipe-agent')\n"
            "with tracer.start_as_current_span('do_the_work') as s:\n"
            "    s.set_attribute('trovis.loop.title', 'Reconcile the invoices')\n"
            "    s.set_attribute('trovis.loop.external_id', 'recipe-quick-1')\n"
            "    assert s.is_recording(), 'span not recording — SDK never configured'\n"
        )
    runner = template.strip().splitlines()[-1].replace("{RUN_FILE}", agent_py)

    # The recipe's command and the recipe's install line are one recipe. A
    # missing `opentelemetry-instrument` used to surface as a FileNotFoundError
    # traceback from subprocess, which reads like a broken test rather than
    # what it is: an environment that never ran the install the page prints.
    quick_install = re.search(r"const quickInstall = `([^`]+)`", page)
    check("the page's quick install ships the command the recipe runs",
          bool(quick_install) and "opentelemetry-distro" in quick_install.group(1),
          "opentelemetry-distro is what puts `opentelemetry-instrument` on PATH")
    tool = runner.split()[0]
    if shutil.which(tool) is None:
        check(f"`{tool}` is installed here", False,
              f"run the page's own install first: "
              f"{quick_install.group(1) if quick_install else 'pip install opentelemetry-distro'}")
        raise SystemExit(
            f"FAILED — `{tool}` is not on PATH, so the shipped recipe cannot be "
            f"run at all. This is the test's whole job; do not skip it."
        )

    proc_env = {**os.environ, **recipe_env}
    completed = subprocess.run(
        runner.split(), env=proc_env, capture_output=True, text=True, timeout=180
    )
    if completed.returncode != 0:
        print("        runner stderr:", (completed.stderr or "")[-400:])
    check("the recipe's own command runs clean", completed.returncode == 0,
          f"cmd={runner.split()[0]} rc={completed.returncode}")
    check("and its spans arrive at Trovis",
          wait_for_agent(KEY, "quick-recipe-agent"), f"agents={agents(KEY)}")
    quick = [l for l in database.get_loops(account_id, limit=50)
             if l.get("external_id") == "recipe-quick-1"]
    check("as named Work, with the title the span carried",
          bool(quick) and quick[0].get("title") == "Reconcile the invoices",
          f"loop={quick[0] if quick else None}")

    print("\n[3] Compression — exporters gzip when told to")
    body = gzip.compress(json.dumps({
        "resourceSpans": [{
            "resource": {"attributes": [
                {"key": "service.name", "value": {"stringValue": "gzip-agent"}}]},
            "scopeSpans": [{"spans": [{
                "traceId": "b" * 32, "spanId": "c" * 16, "name": "gzipped_work",
                "startTimeUnixNano": str(time.time_ns()),
                "endTimeUnixNano": str(time.time_ns()),
            }]}],
        }],
    }).encode())
    r = requests.post(ENDPOINT, data=body, timeout=30, headers={
        "Content-Type": "application/json", "Content-Encoding": "gzip", **H})
    check("a gzipped batch is accepted", r.status_code == 200,
          f"HTTP {r.status_code}: {r.text[:120]}")
    check("and lands", wait_for_agent(KEY, "gzip-agent"), f"agents={agents(KEY)}")

    print("\n[4] A bad body still fails loudly, in the sender's terms")
    r = requests.post(ENDPOINT, data=b"not protobuf at all", timeout=30, headers={
        "Content-Type": "application/x-protobuf", **H})
    check("malformed protobuf is a 400 that names the format",
          r.status_code == 400 and "protobuf" in r.text.lower(),
          f"HTTP {r.status_code}: {r.text[:120]}")
    r = requests.post(ENDPOINT, data=b"\x1f\x8b garbage", timeout=30, headers={
        "Content-Type": "application/json", "Content-Encoding": "gzip", **H})
    check("malformed gzip is a 400 that names the encoding",
          r.status_code == 400 and "gzip" in r.text.lower(),
          f"HTTP {r.status_code}: {r.text[:120]}")
    r = requests.post(ENDPOINT, data=b"{}", timeout=30, headers={
        "Content-Type": "application/json", "Content-Encoding": "br", **H})
    check("an encoding we cannot read is a 415, not a confusing 400",
          r.status_code == 415, f"HTTP {r.status_code}: {r.text[:120]}")

finally:
    server.should_exit = True
    thread.join(timeout=10)

print()
if failures:
    print(f"FAILED ({len(failures)}): " + "; ".join(failures))
    raise SystemExit(1)
print("CONNECT RECIPES VERIFIED (live server, the shipped commands)")
