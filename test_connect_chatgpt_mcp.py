"""Door test: the ChatGPT Custom MCP door at /mcp, over a real MCP client.

A custom GPT can reach Trovis two ways — GPT Actions (test_connect_actions.py)
and a Custom MCP connector — and they are one product to the person using
them. So they must behave the same, and this test drives the MCP half of that
claim through a real MCP client speaking the real protocol to the real mount:

  initialize + tools/list on /mcp   -> exactly search + fetch (ChatGPT's rule)
  connect:                           -> the GPT appears as an agent
  log: with a job title              -> a NAMED job on GET /work/items
  log: again                         -> the same job, not a second one
  complete:                          -> the job closes, one record with steps
  log: without a title               -> still lands, unnamed, as it always did
  another org's key                  -> cannot see or touch the job

Skips (exit 0) when the `mcp` client package isn't importable; CI sets
TROVIS_REQUIRE_SDK=1 to make that a hard failure instead.

Run:
  TROVIS_DISABLE_PRICING_SYNC=1 python3 test_connect_chatgpt_mcp.py
(isolated temp SQLite DB; never touches the dev/prod DB)
"""
import asyncio
import os
import socket
import tempfile
import threading
import time

os.environ["OVERSEE_DISABLE_PRICING_SYNC"] = "1"
os.environ["TROVIS_DISABLE_PRICING_SYNC"] = "1"
os.environ["TROVIS_DISABLE_ALERTS"] = "1"
os.environ["TROVIS_DISABLE_LOOP_SWEEP"] = "1"
os.environ.pop("DATABASE_URL", None)
os.environ.pop("ANTHROPIC_API_KEY", None)

try:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
except ImportError as e:  # pragma: no cover — environment-dependent
    msg = f"mcp client not importable ({e})"
    if os.environ.get("TROVIS_REQUIRE_SDK") == "1":
        print(f"FAILED — {msg}. TROVIS_REQUIRE_SDK=1 means this door must be "
              f"verified, not skipped. Install it: pip install -r requirements.txt")
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

# Stub the Claude boundary — reading an agent regenerates a missing
# description on read, which would otherwise be a live Anthropic call.
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
MCP_URL = f"{BASE}/mcp"
print(f"  (live server on {BASE}, ChatGPT MCP at /mcp)")


async def _session(api_key):
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
    return streamablehttp_client(MCP_URL, headers=headers)


async def _search(query, api_key):
    """One real MCP session: initialize, call search, read the title back.

    The title is what the GPT sees, so asserting on it is asserting on the
    thing the GPT is actually told.
    """
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
    async with streamablehttp_client(MCP_URL, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("search", {"query": query})
            data = getattr(result, "structuredContent", None) or {}
            titles = [r.get("title") for r in (data.get("results") or [])]
            return titles[0] if titles else ""


async def _tools(api_key):
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
    async with streamablehttp_client(MCP_URL, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return [t.name for t in (await session.list_tools()).tools]


def run(coro):
    return asyncio.run(coro)


def loops_titled(key, title):
    account_id = database.validate_api_key(key)["account_id"]
    return [l for l in database.get_loops(account_id, limit=50)
            if l.get("title") == title]


try:
    r = requests.post(f"{BASE}/auth/signup", timeout=30, json={
        "email": "gptmcp@test.com", "password": "supersecret123",
        "name": "GPT Tester", "account_type": "individual", "org_name": "GPT Co",
    })
    assert r.status_code == 201, r.text
    KEY = r.json()["api_key"]
    H = {"X-Trovis-Api-Key": KEY}

    r2 = requests.post(f"{BASE}/auth/signup", timeout=30, json={
        "email": "gptother@test.com", "password": "supersecret123",
        "name": "Other Tester", "account_type": "individual", "org_name": "Other Co",
    })
    assert r2.status_code == 201, r2.text
    OTHER_KEY = r2.json()["api_key"]
    OTHER_H = {"X-Trovis-Api-Key": OTHER_KEY}

    print("\n[1] The door speaks MCP and obeys ChatGPT's two-tool rule")
    tools = run(_tools(KEY))
    check("exactly search + fetch are offered", sorted(tools) == ["fetch", "search"],
          f"tools={tools}")

    print("\n[2] connect: puts the GPT in the fleet under its own name")
    said = run(_search(
        "connect:Ops GPT|Operations assistant|Handles vendor email and scheduling",
        KEY))
    check("the GPT is told it connected", "Ops GPT" in said, f"said={said!r}")
    agents = [a["service_name"] for a in
              requests.get(f"{BASE}/agents", headers=H, timeout=30).json()]
    check("and it shows on Agents", "Ops GPT" in agents, f"agents={agents}")
    summary = requests.get(f"{BASE}/agents/Ops GPT/summary", headers=H, timeout=30).json()
    check("labelled as a ChatGPT agent, not an unlabelled service",
          summary.get("platform") == "ChatGPT Agent",
          f"platform={summary.get('platform')!r}")

    print("\n[3] A job title on the first step makes NAMED work")
    # This is the gap: the Actions door learned to do it and its MCP twin
    # didn't, so the same GPT reported differently depending on which door
    # its builder happened to pick.
    run(_search("log:Read the thread|Pulled the Acme renewal thread|"
                "Chase the Acme renewal", KEY))
    job = None
    deadline = time.time() + 10
    while time.time() < deadline:
        found = loops_titled(KEY, "Chase the Acme renewal")
        if found:
            job = found[0]
            break
        time.sleep(0.2)
    check("the job exists, under the title the GPT gave it", job is not None)
    if job:
        with database._connect() as conn, database._cursor(conn) as cur:
            cur.execute("SELECT title_source FROM loops WHERE id = ?", (job["id"],))
            src = cur.fetchone()["title_source"]
        check("title_source=provided (a real name, not a shell)",
              src == "provided", f"title_source={src!r}")
    items = requests.get(f"{BASE}/work/items", headers=H, timeout=30).json()
    titles = [it.get("title") for it in items.get("items") or []]
    check("and it shows on the Work board",
          "Chase the Acme renewal" in titles, f"titles={titles}")

    print("\n[4] Later steps join that job — they don't each open their own")
    run(_search("log:Draft the reply|Wrote a reply for review", KEY))
    run(_search("complete:Sent the renewal reply and set a follow-up", KEY))

    closed = None
    deadline = time.time() + 10
    while time.time() < deadline:
        found = loops_titled(KEY, "Chase the Acme renewal")
        if found and found[0].get("closed_at"):
            closed = found[0]
            break
        time.sleep(0.2)
    check("complete: closes the job", closed is not None,
          f"loop={(loops_titled(KEY, 'Chase the Acme renewal') or [{}])[0]}")
    check("and only one job was ever opened for it",
          len(loops_titled(KEY, "Chase the Acme renewal")) == 1)

    feed = requests.get(f"{BASE}/agents/Ops GPT/records", headers=H, timeout=30).json()
    records = feed.get("records") or []
    acme = [x for x in records
            if "Sent the renewal reply" in ((x.get("exchange") or {}).get("agent") or "")]
    check("the whole job is ONE record in the Work Feed", len(acme) == 1,
          f"summaries={[x.get('summary') for x in records]}")
    if acme:
        rec = acme[0]
        check("with its three reports as that record's steps",
              len(rec.get("spans") or []) == 3, f"spans={len(rec.get('spans') or [])}")
        check("and the record points at its job", isinstance(rec.get("loop_id"), int),
              f"loop_id={rec.get('loop_id')!r}")
    check("no record is titled from the door's own mechanics",
          all(x.get("summary") != "agent_run_complete" for x in records),
          f"summaries={[x.get('summary') for x in records]}")

    print("\n[5] A GPT that never names anything still reports, as before")
    run(_search("log:Ran a check|Checked the inbox", KEY))
    account_id = database.validate_api_key(KEY)["account_id"]
    unnamed = [l for l in database.get_loops(account_id, limit=50)
               if not l.get("closed_at") and l.get("title") != "Chase the Acme renewal"]
    check("the step lands even with no title given", bool(unnamed),
          f"open loops={[l.get('title') for l in unnamed]}")
    spans = requests.get(f"{BASE}/agents/Ops GPT/spans", headers=H, timeout=30).json()
    check("and it is visible as activity on the agent",
          any(s.get("span_name") == "Ran a check" for s in spans),
          f"spans={[s.get('span_name') for s in spans]}")

    print("\n[6] Another org's key cannot see or touch this org's work")
    other_items = requests.get(f"{BASE}/work/items", headers=OTHER_H, timeout=30).json()
    other_titles = [it.get("title") for it in other_items.get("items") or []]
    check("the other org's Work board is empty of our jobs",
          "Chase the Acme renewal" not in other_titles, f"titles={other_titles}")

    # The other org drives the same door with its own key. Its work must land
    # in its own account and never touch ours — the job cache is keyed by
    # account, so a shared agent name must not be a shared job.
    run(_search("connect:Ops GPT|Operations assistant|Theirs, not ours", OTHER_KEY))
    run(_search("log:Step|Did a thing|Chase the Acme renewal", OTHER_KEY))
    ours = loops_titled(KEY, "Chase the Acme renewal")
    check("our job of the same name is still closed and ours",
          len(ours) == 1 and ours[0].get("closed_at"),
          f"loop={ours[0] if ours else None}")
    other_account = database.validate_api_key(OTHER_KEY)["account_id"]
    check("their report stayed in their account",
          all(l.get("account_id") in (None, other_account)
              for l in database.get_loops(other_account, limit=50)))

    print("\n[7] The two ChatGPT doors are one implementation, not two")
    # The whole point of the fix: if these ever diverge again, the same GPT
    # reports differently depending on which door its builder picked.
    import chatgpt_jobs
    import mcp_server
    check("the MCP door uses the shared job bookkeeping",
          mcp_server.chatgpt_jobs is chatgpt_jobs)
    check("and so does the Actions door", main.chatgpt_jobs is chatgpt_jobs)
    src = open("mcp_server.py").read()
    check("the MCP door no longer writes bare spans for log/complete",
          src.count("_create_span(") == 2,
          "only the registration span (its definition + one call) may remain")

finally:
    server.should_exit = True
    thread.join(timeout=10)

print()
if failures:
    print(f"FAILED ({len(failures)}): " + "; ".join(failures))
    raise SystemExit(1)
print("CHATGPT CUSTOM MCP DOOR VERIFIED (live server, real MCP client)")
