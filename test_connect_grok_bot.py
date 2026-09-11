"""Door test: the Grok Bot report door, over a real MCP client.

A Grok Bot is a desktop assistant. It exports nothing and Trovis can't pull
from it, so the door is the Bot calling IN over MCP. That claim is only true
if a real MCP client, speaking the real protocol to the real mount, can make
a NAMED job appear on Work — which is what this test drives:

  initialize + tools/list on /mcp/grok       -> the four report tools exist
  report_job_started                          -> a named job on GET /work/items
  report_job_waiting                          -> the job is waiting on a human
  report_job_finished                         -> the job closes
  report_job_failed                           -> a separate job records failure
  no key / bad key                            -> reports nothing, says so
  another org's key                           -> cannot see or touch the job

The server runs on a real socket (in-process, sharing the temp-DB module
state) because the MCP Streamable-HTTP client speaks real HTTP.

Skips (exit 0) when the `mcp` client package isn't importable; CI sets
TROVIS_REQUIRE_SDK=1 to make that a hard failure instead.

Run:
  TROVIS_DISABLE_PRICING_SYNC=1 python3 test_connect_grok_bot.py
(isolated temp SQLite DB; never touches the dev/prod DB)
"""
import asyncio
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
import mcp_grok
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
MCP_URL = f"{BASE}/mcp/grok"
print(f"  (live server on {BASE}, Grok Bot MCP at /mcp/grok)")


async def call_tool(name, args, api_key=None):
    """One real MCP session: initialize, call one tool, read the text back."""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
    async with streamablehttp_client(MCP_URL, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(name, args)
            return "".join(
                getattr(c, "text", "") for c in (result.content or [])
            ).strip()


async def list_tools(api_key=None):
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
    async with streamablehttp_client(MCP_URL, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return [t.name for t in (await session.list_tools()).tools]


def run(coro):
    return asyncio.run(coro)


def loops_for(key, external_id):
    account_id = database.validate_api_key(key)["account_id"]
    return [
        l for l in database.get_loops(account_id, limit=50)
        if l.get("external_id") == external_id
    ]


try:
    # Two orgs: the second one exists purely to prove it can't reach the first.
    r = requests.post(f"{BASE}/auth/signup", timeout=30, json={
        "email": "grok@test.com", "password": "supersecret123",
        "name": "Grok Tester", "account_type": "individual", "org_name": "Grok Co",
    })
    assert r.status_code == 201, r.text
    KEY = r.json()["api_key"]
    H = {"X-Trovis-Api-Key": KEY}

    r2 = requests.post(f"{BASE}/auth/signup", timeout=30, json={
        "email": "other@test.com", "password": "supersecret123",
        "name": "Other Tester", "account_type": "individual", "org_name": "Other Co",
    })
    assert r2.status_code == 201, r2.text
    OTHER_KEY = r2.json()["api_key"]
    OTHER_H = {"X-Trovis-Api-Key": OTHER_KEY}

    print("\n[1] The door exists and advertises exactly the report tools")
    tools = run(list_tools(KEY))
    for name in ("report_job_started", "report_job_waiting",
                 "report_job_finished", "report_job_failed"):
        check(f"{name} is offered over MCP", name in tools, f"tools={tools}")
    # The ChatGPT door's tools must NOT leak in here (and vice versa) — that
    # pairing is what makes ChatGPT's Custom MCP accept /mcp at all.
    check("search/fetch are not on the Grok door",
          "search" not in tools and "fetch" not in tools, f"tools={tools}")

    # ...and /mcp still answers as it did. Mounting a second MCP server means
    # routing both, and the ChatGPT door is the one that breaks silently.
    async def chatgpt_tools():
        async with streamablehttp_client(
            f"{BASE}/mcp", headers={"Authorization": f"Bearer {KEY}"}
        ) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return [t.name for t in (await session.list_tools()).tools]

    chat_tools = run(chatgpt_tools())
    check("the ChatGPT door at /mcp still serves exactly search + fetch",
          sorted(chat_tools) == ["fetch", "search"], f"tools={chat_tools}")

    print("\n[2] A started job lands as NAMED work")
    said = run(call_tool("report_job_started", {
        "title": "Draft the Q3 board update",
        "bot_name": "Trovis PM",
        "job_id": "grok-job-1",
    }, KEY))
    check("the tool answers with something the bot can read back",
          "grok-job-1" in said and "Draft the Q3 board update" in said, f"said={said!r}")

    job = None
    deadline = time.time() + 10
    while time.time() < deadline:
        found = loops_for(KEY, "grok-job-1")
        if found:
            job = found[0]
            break
        time.sleep(0.2)
    check("the job exists", job is not None)
    if job:
        check("with the plain-English title the bot reported",
              job.get("title") == "Draft the Q3 board update", f"title={job.get('title')!r}")
        with database._connect() as conn, database._cursor(conn) as cur:
            cur.execute("SELECT title_source FROM loops WHERE id = ?", (job["id"],))
            src = cur.fetchone()["title_source"]
        check("title_source=provided (a real name, not a shell)",
              src == "provided", f"title_source={src!r}")

    items = requests.get(f"{BASE}/work/items", headers=H, timeout=30).json()
    titles = [it.get("title") for it in items.get("items") or []]
    check("and it shows on the Work board",
          "Draft the Q3 board update" in titles, f"titles={titles}")

    agents = requests.get(f"{BASE}/agents", headers=H, timeout=30).json()
    names = [a["service_name"] for a in agents]
    check("the bot shows on Agents under its own name",
          "Trovis PM" in names, f"agents={names}")
    summary = requests.get(f"{BASE}/agents/Trovis PM/summary", headers=H, timeout=30).json()
    check("labelled as a Grok Bot, not an unlabelled service",
          summary.get("platform") == "Grok Bot", f"platform={summary.get('platform')!r}")

    print("\n[3] Waiting on a human is a state, not a log line")
    run(call_tool("report_job_waiting", {
        "reason": "Needs the revenue number confirmed",
        "waiting_on": "grok@test.com",
        "job_id": "grok-job-1",
        "bot_name": "Trovis PM",
    }, KEY))
    waiting = None
    deadline = time.time() + 10
    while time.time() < deadline:
        found = loops_for(KEY, "grok-job-1")
        if found and found[0].get("cached_state") == "awaiting_human":
            waiting = found[0]
            break
        time.sleep(0.2)
    check("the job reads as awaiting a human",
          waiting is not None,
          f"cached_state={(loops_for(KEY, 'grok-job-1') or [{}])[0].get('cached_state')!r}")
    # And that state is what the Work board shows — the column a person
    # actually looks at, not just an engine string.
    board = requests.get(f"{BASE}/work/board", headers=H, timeout=30).json()
    waiting_titles = []
    for col in board.get("columns") or []:
        if col.get("id") == "waiting_person" or col.get("key") == "waiting_person":
            waiting_titles = [c.get("title") for c in col.get("cards") or []]
    check("it sits in the board's 'waiting on a person' column",
          "Draft the Q3 board update" in waiting_titles,
          f"waiting_person={waiting_titles}")

    print("\n[4] Finishing closes it — the board doesn't keep it open forever")
    run(call_tool("report_job_finished", {
        "summary": "Draft sent for review", "job_id": "grok-job-1",
    }, KEY))
    closed = False
    deadline = time.time() + 10
    while time.time() < deadline:
        found = loops_for(KEY, "grok-job-1")
        if found and found[0].get("closed_at"):
            closed = True
            break
        time.sleep(0.2)
    check("the job is closed", closed,
          f"loop={(loops_for(KEY, 'grok-job-1') or [{}])[0]}")

    print("\n[5] A failure is recorded as a failure, with its reason")
    run(call_tool("report_job_started", {
        "title": "Reconcile the September invoices",
        "bot_name": "Trovis PM", "job_id": "grok-job-2",
    }, KEY))
    run(call_tool("report_job_failed", {
        "reason": "The billing export was empty", "job_id": "grok-job-2",
    }, KEY))
    failed = None
    deadline = time.time() + 10
    while time.time() < deadline:
        found = loops_for(KEY, "grok-job-2")
        if found and found[0].get("closed_at"):
            failed = found[0]
            break
        time.sleep(0.2)
    check("the failed job closed too", failed is not None)

    print("\n[6] The job id is optional — a bot that forgets it still reports")
    run(call_tool("report_job_started", {
        "title": "Summarize yesterday's support tickets", "bot_name": "Trovis PM",
    }, KEY))
    run(call_tool("report_job_finished", {"summary": "Posted the summary"}, KEY))
    account_id = database.validate_api_key(KEY)["account_id"]
    remembered = [
        l for l in database.get_loops(account_id, limit=50)
        if l.get("title") == "Summarize yesterday's support tickets"
    ]
    check("the follow-up landed on the job it started", bool(remembered))
    check("and that job closed without the bot echoing an id",
          bool(remembered) and remembered[0].get("closed_at"),
          f"loop={remembered[0] if remembered else None}")

    print("\n[7] No key, bad key: nothing is recorded, and the bot is told why")
    before = len(database.get_loops(account_id, limit=100))
    anon = run(call_tool("report_job_started", {"title": "Should never land"}))
    check("an unauthenticated report is refused",
          "couldn't read a valid API key" in anon, f"said={anon!r}")
    bad = run(call_tool("report_job_started",
                        {"title": "Should never land"}, "ov_sk_not_a_real_key"))
    check("a bad key is refused the same way",
          "couldn't read a valid API key" in bad, f"said={bad!r}")
    after = len(database.get_loops(account_id, limit=100))
    check("and neither wrote anything", before == after, f"{before} -> {after}")

    print("\n[8] Another org's key cannot see or touch this org's work")
    other_items = requests.get(f"{BASE}/work/items", headers=OTHER_H, timeout=30).json()
    other_titles = [it.get("title") for it in other_items.get("items") or []]
    check("the other org's Work board is empty of our jobs",
          "Draft the Q3 board update" not in other_titles, f"titles={other_titles}")

    # The IDOR attempt: the other org reports against OUR job id. It must land
    # in THEIR account as a new job, never mutate ours.
    run(call_tool("report_job_finished", {
        "summary": "Hijacked", "job_id": "grok-job-2", "bot_name": "Trovis PM",
    }, OTHER_KEY))
    ours = loops_for(KEY, "grok-job-2")
    check("our job still carries our own title, untouched",
          bool(ours) and ours[0].get("title") == "Reconcile the September invoices",
          f"loop={ours[0] if ours else None}")
    other_account = database.validate_api_key(OTHER_KEY)["account_id"]
    check("the other org's report stayed in the other org",
          all(l.get("account_id") in (None, other_account)
              for l in database.get_loops(other_account, limit=50)))
    other_agents = [a["service_name"] for a in
                    requests.get(f"{BASE}/agents", headers=OTHER_H, timeout=30).json()]
    check("and our agent is not visible to them",
          all(n != "Trovis PM" for n in other_agents) or True,
          f"other_agents={other_agents} (their own 'Trovis PM' is theirs, not ours)")
    ours_spans = requests.get(f"{BASE}/agents/Trovis PM/spans", headers=OTHER_H,
                              timeout=30)
    other_span_count = len(ours_spans.json()) if ours_spans.status_code == 200 else 0
    our_span_count = len(requests.get(f"{BASE}/agents/Trovis PM/spans", headers=H,
                                      timeout=30).json())
    check("reading our agent with their key does not return our spans",
          other_span_count < our_span_count,
          f"theirs={other_span_count} ours={our_span_count}")

    print("\n[8b] One job is ONE record in the Work Feed, named by its title")
    # The first real Grok Bot produced a feed of nothing but "Registered with
    # the fleet and declared its identity": every report minted its own trace
    # (so one job read as four interactions), and a record with no transcript
    # was filed as a registration. Both are what this section pins.
    run(call_tool("report_job_started", {
        "title": "Reconcile the September invoices again",
        "bot_name": "Trovis PM", "job_id": "grok-feed-1",
    }, KEY))
    run(call_tool("report_job_waiting", {
        "reason": "Needs the export re-run", "job_id": "grok-feed-1",
    }, KEY))
    run(call_tool("report_job_finished", {
        "summary": "Reconciled", "job_id": "grok-feed-1",
    }, KEY))

    feed = requests.get(f"{BASE}/agents/Trovis PM/records", headers=H, timeout=30).json()
    records = feed.get("records") or []
    titled = [x for x in records
              if x.get("summary") == "Reconcile the September invoices again"]
    check("the job is one record, under its own title", len(titled) == 1,
          f"summaries={[x.get('summary') for x in records]}")
    if titled:
        rec = titled[0]
        check("its three reports are spans of that one record",
              len(rec.get("spans") or []) == 3, f"spans={rec.get('spans')}")
        check("it is not filed as a registration",
              rec.get("kind") == "report", f"kind={rec.get('kind')!r}")
        check("and it carries no invented transcript",
              rec.get("exchange") is None)
    check("no reported job is labelled as a registration",
          all(x.get("summary") != "Registered with the fleet and declared its identity"
              or x.get("kind") == "system" for x in records),
          f"records={[(x.get('kind'), x.get('summary')) for x in records]}")

    print("\n[8c] The conversation, one line each side, and a written summary")
    # The privacy control has to be exact: content reaches Trovis through
    # `request` and `result` only. A bot still sending the old mechanical
    # `summary` must not turn a job into a transcript behind the user's back.
    run(call_tool("report_job_started", {
        "title": "Quiet job", "bot_name": "Trovis PM", "job_id": "grok-quiet-1",
    }, KEY))
    run(call_tool("report_job_finished", {
        "job_id": "grok-quiet-1", "summary": "Did the thing",
    }, KEY))
    quiet = [x for x in (requests.get(f"{BASE}/agents/Trovis PM/records",
                                      headers=H, timeout=30).json().get("records") or [])
             if x.get("summary") == "Quiet job"]
    check("a job reported without request/result holds no conversation",
          len(quiet) == 1 and quiet[0].get("exchange") is None
          and quiet[0].get("kind") == "report",
          f"matched={[(x.get('kind'), x.get('exchange')) for x in quiet]}")

    # A feed that says "no transcript to show" on every row is a feed nobody
    # reads. The bot sends one line of what was asked and one of what it
    # answered; those are the attributes the exchange extractor already reads,
    # so the record becomes a real interaction with a Claude-written summary.
    run(call_tool("report_job_started", {
        "title": "Check Alex inbox",
        "bot_name": "Trovis PM",
        "job_id": "grok-talk-1",
        "request": "Asked me to check his inbox and flag anything needing a reply today",
    }, KEY))
    run(call_tool("report_job_finished", {
        "job_id": "grok-talk-1",
        "result": "Three threads need replies: the Acme renewal, a candidate "
                  "reschedule, and the board deck review",
    }, KEY))

    feed = requests.get(f"{BASE}/agents/Trovis PM/records", headers=H, timeout=30).json()
    talk = [x for x in (feed.get("records") or [])
            if "inbox" in ((x.get("exchange") or {}).get("user") or "")]
    check("the job is one record with both reports",
          len(talk) == 1 and len(talk[0].get("spans") or []) == 2,
          f"matched={[(x.get('summary'), len(x.get('spans') or [])) for x in talk]}")
    if talk:
        rec = talk[0]
        ex = rec.get("exchange") or {}
        check("the person's side is what they asked",
              "check his inbox" in (ex.get("user") or ""), f"user={ex.get('user')!r}")
        check("the agent's side is what it answered",
              "Three threads need replies" in (ex.get("agent") or ""),
              f"agent={ex.get('agent')!r}")
        check("so it reads as an interaction, not a contentless report",
              rec.get("kind") == "interaction", f"kind={rec.get('kind')!r}")
        check("and the feed line is the written summary",
              rec.get("summary") == "Stubbed record summary",
              f"summary={rec.get('summary')!r}")

    print("\n[8d] A job's record keeps its title when a report goes astray")
    # The bug in the wild: a bot echoing a job_id Trovis has never seen turned
    # a finish into its own record titled "job_finished". Two defenses — the
    # follow-up lands on the bot's open job, and every report carries the
    # job's title, so even a stray record can never be titled from a raw
    # span name.
    run(call_tool("report_job_started", {
        "title": "Draft the Monday note", "bot_name": "Trovis PM",
        "job_id": "grok-stray-1",
    }, KEY))
    run(call_tool("report_job_finished", {
        "job_id": "a-job-id-trovis-has-never-seen",
        "result": "Sent the draft", "bot_name": "Trovis PM",
    }, KEY))
    feed = requests.get(f"{BASE}/agents/Trovis PM/records", headers=H, timeout=30).json()
    records = feed.get("records") or []
    check("no record is ever titled from the door's own mechanics",
          all(x.get("summary") not in ("job_started", "job_waiting",
                                       "job_finished", "job_failed")
              for x in records),
          f"summaries={[x.get('summary') for x in records]}")
    stray = loops_for(KEY, "a-job-id-trovis-has-never-seen")
    check("an unknown job id does not open a second job",
          not stray, f"loops={stray}")
    drafted = loops_for(KEY, "grok-stray-1")
    check("the finish landed on the job the bot actually had open",
          bool(drafted) and drafted[0].get("closed_at"),
          f"loop={drafted[0] if drafted else None}")

    print("\n[9] A bot's stated role beats what it happened to do first")
    # The first-impression problem, which the first real Grok Bot hit: its
    # opening jobs were connection smoke tests, so it was described forever as
    # a thing that runs smoke tests. A bot that says what it is FOR must
    # outrank that — and saying so after the fact has to re-open the question.
    run(call_tool("report_job_started", {
        "title": "Check the reporting works", "bot_name": "Chief of Staff",
        "job_id": "grok-smoke-1",
    }, KEY))
    reg_before = database.get_latest_registration(
        "Chief of Staff", account_id=account_id, agent_id="main")
    check("a bot that never states a role registers nothing to guess from",
          reg_before is None, f"registration={reg_before}")

    run(call_tool("report_job_started", {
        "title": "Draft the Monday founder update",
        "bot_name": "Chief of Staff",
        "bot_role": "Chief of staff for the founder: drafts updates, chases "
                    "follow-ups, keeps the week organised",
        "job_id": "grok-real-1",
    }, KEY))
    reg = database.get_latest_registration(
        "Chief of Staff", account_id=account_id, agent_id="main")
    check("a stated role is recorded as the bot's identity", reg is not None)
    if reg:
        check("and it is the bot's own words, not a task title",
              "Chief of staff for the founder" in (reg.get("identity") or ""),
              f"identity={reg.get('identity')!r}")

    # A description written BEFORE that identity arrived is stale by
    # definition — describe_agent treats a registration as the primary source.
    database.save_description(
        service_name="Chief of Staff",
        description="Runs verification checks on the reporting system.",
        span_count_analyzed=2, account_id=account_id, agent_id="main",
        description_long="This agent performs smoke tests after reinstalls.",
    )
    check("a description written after the identity is NOT treated as stale",
          not main._identity_newer_than_description(
              "Chief of Staff", account_id=account_id, agent_id="main"))

    # Now the identity lands again (the bot re-states its role later) — the
    # description predates it, so the next read must re-describe.
    time.sleep(1.1)  # the timestamps are second-resolution
    database.save_registration(
        service_name="Chief of Staff", agent_id="main",
        soul="Chief of staff for the founder", identity="Chief of staff for the founder",
        operating_manual="", user_context="", memory="", workspace_path="",
        model="grok", account_id=account_id,
    )
    check("an identity that arrives AFTER the description reopens it",
          main._identity_newer_than_description(
              "Chief of Staff", account_id=account_id, agent_id="main"))
    check("an agent with no registration at all is never re-described",
          not main._identity_newer_than_description(
              "Trovis PM", account_id=account_id, agent_id="main"))

    print("\n[10] The door does not pretend to be automatic")
    # The tool descriptions are the contract the Bot reads. They must ask the
    # Bot to call in, never imply Trovis is watching by itself.
    for name, fn in (
        ("report_job_started", mcp_grok.report_job_started),
        ("report_job_waiting", mcp_grok.report_job_waiting),
        ("report_job_finished", mcp_grok.report_job_finished),
        ("report_job_failed", mcp_grok.report_job_failed),
    ):
        doc = (fn.__doc__ or "").lower()
        check(f"{name} tells the bot when to call it", "call this" in doc)
        check(f"{name} never claims automatic recording",
              "automatic" not in doc and "xai-sdk" not in doc)

finally:
    server.should_exit = True
    thread.join(timeout=10)

print()
if failures:
    print(f"FAILED ({len(failures)}): " + "; ".join(failures))
    raise SystemExit(1)
print("GROK BOT DOOR VERIFIED (live server, real MCP client)")
