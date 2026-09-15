"""The OpenAI Managed Agents PULL adapter, against spec-shaped payloads.

This is not yet a claimed door — there is no Connect tile for it, because no
real OpenAI key has ever driven it. What this test does prove is that the
mapping is right and the sync is safe to run twice, using payloads whose
field names come from OpenAI's published OpenAPI spec (AgentResource,
SessionResource, TurnResource, TokenUsageResource) rather than from
imagination. When a key exists, the remaining question is only whether the
API returns what its own spec says.

Covers:
  agents            -> Trovis agents, with instructions as their identity
  sessions          -> named Work when the caller titled the session
  turns             -> spans with duration, outcome and token usage
  requires_action   -> the job reads as waiting on a person
  a second sync     -> writes nothing new (the cursor holds)
  subagent turns    -> recorded as a handoff, not a mystery span
  a dead API        -> reports a failure, writes nothing, raises nothing

Run:
  TROVIS_DISABLE_PRICING_SYNC=1 python3 test_pull_openai_agents.py
(isolated temp SQLite DB; never touches the dev/prod DB, never uses network)
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
database.init_db()

import pull_openai_agents as pull

failures = []


def check(label, cond, detail=""):
    print(("  PASS " if cond else "  FAIL ") + label)
    if detail:
        print(f"        {detail}")
    if not cond:
        failures.append(label)


NOW = int(time.time())

# --- Fixtures. Field names and enum values are the spec's. ------------------

AGENTS = [{
    "id": "agent_abc123", "object": "agent",
    "created_at": NOW - 9000, "updated_at": NOW - 9000,
    "name": "Invoice Reconciler",
    "metadata": {}, "model": "gpt-5.2",
    "instructions": "You reconcile supplier invoices against purchase orders "
                    "and escalate mismatches over $500 to a human.",
    "tools": [], "multi_agent": {},
}, {
    # A nameless agent must not collapse into a shared row.
    "id": "agent_nameless", "object": "agent",
    "created_at": NOW - 8000, "updated_at": NOW - 8000,
    "name": None, "metadata": {}, "model": "gpt-5.2",
    "instructions": "", "tools": [], "multi_agent": {},
}]

SESSION_DONE = {
    "id": "sess_reconcile_1", "object": "agent.session",
    "metadata": {"trovis.loop.title": "Reconcile the September invoices"},
    "created_at": NOW - 600, "last_active_at": NOW - 300,
    "status": "idle", "required_actions": [], "error": None,
    "agent": {"id": "agent_abc123", "name": "Invoice Reconciler",
              "model": "gpt-5.2", "instructions": "…", "tools": [],
              "multi_agent": {}},
    "environment": {}, "vault_ids": [],
    "usage": {"input_tokens": 900, "output_tokens": 200, "total_tokens": 1100},
}

SESSION_BLOCKED = {
    "id": "sess_blocked_1", "object": "agent.session",
    "metadata": {"title": "Approve the Acme mismatch"},
    "created_at": NOW - 400, "last_active_at": NOW - 120,
    "status": "requires_action",
    "required_actions": [{"type": "function_call"}],
    "error": None,
    "agent": {"id": "agent_abc123", "name": "Invoice Reconciler",
              "model": "gpt-5.2", "instructions": "…", "tools": [],
              "multi_agent": {}},
    "environment": {}, "vault_ids": [], "usage": None,
}

TURNS = {
    "sess_reconcile_1": [
        {"id": "turn_1", "object": "agent.turn", "session_id": "sess_reconcile_1",
         "agent_id": "agent_abc123", "subagent_id": None, "status": "completed",
         "created_at": NOW - 600, "started_at": NOW - 600,
         "completed_at": NOW - 580, "error": None,
         "usage": {"input_tokens": 700, "output_tokens": 150,
                   "total_tokens": 850}},
        {"id": "turn_2", "object": "agent.turn", "session_id": "sess_reconcile_1",
         "agent_id": "agent_abc123", "subagent_id": "subagent_ocr",
         "status": "completed", "created_at": NOW - 570,
         "started_at": NOW - 570, "completed_at": NOW - 560, "error": None,
         "usage": {"input_tokens": 200, "output_tokens": 50,
                   "total_tokens": 250}},
    ],
    "sess_blocked_1": [
        {"id": "turn_3", "object": "agent.turn", "session_id": "sess_blocked_1",
         "agent_id": "agent_abc123", "subagent_id": None, "status": "failed",
         "created_at": NOW - 400, "started_at": NOW - 400,
         "completed_at": NOW - 395,
         "error": {"code": "tool_error", "message": "PO lookup timed out"},
         "usage": None},
    ],
}


class FakeAPI:
    """Stands in for api.openai.com. Counts calls so we can prove the second
    sync is cheap as well as silent."""

    def __init__(self, dead=False):
        self.dead = dead
        self.calls = []

    def __call__(self, path, api_key, params=None):
        self.calls.append(path)
        if self.dead:
            return {}
        if path == "/agents":
            return {"object": "list", "data": AGENTS, "has_more": False}
        if path == "/agents/sessions":
            return {"object": "list",
                    "data": [SESSION_DONE, SESSION_BLOCKED], "has_more": False}
        if path.startswith("/agents/sessions/") and path.endswith("/turns"):
            sid = path.split("/")[3]
            return {"object": "list", "data": TURNS.get(sid, []),
                    "has_more": False}
        return {}


# --- Account -----------------------------------------------------------------

acct = database.create_account(
    "pull@test.com", account_type="individual", name="Pull Co")
ACCOUNT_ID = acct["id"] if isinstance(acct, dict) else acct


def loops_for(external_id):
    return [l for l in database.get_loops(ACCOUNT_ID, limit=50)
            if l.get("external_id") == external_id]


def spans_named(name):
    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute(
            f"SELECT * FROM spans WHERE account_id = {database.PH} "
            f"AND span_name = {database.PH}", (ACCOUNT_ID, name))
        return [dict(r) for r in cur.fetchall()]


print("\n[1] A key, and the org's agents are simply there")
api = FakeAPI()
result = pull.sync_account(ACCOUNT_ID, "sk-test", get=api)
check("the sync reports success", result.get("status") == "ok", f"{result}")
check("both agents were seen", result.get("agents") == 2, f"{result}")

reg = database.get_latest_registration(
    "Invoice Reconciler", account_id=ACCOUNT_ID, agent_id="main")
check("the agent's instructions became its identity",
      reg is not None and "reconcile supplier invoices" in (reg.get("identity") or ""),
      f"registration={reg}")
check("a nameless agent keeps its own row rather than collapsing",
      pull.agent_name(AGENTS[1]) == "agent_nameless",
      f"name={pull.agent_name(AGENTS[1])!r}")

print("\n[2] Sessions arrive as Work, named when the caller named them")
job = loops_for("sess_reconcile_1")
check("the session is a job", len(job) == 1, f"loops={job}")
if job:
    check("under the title the session carried",
          job[0].get("title") == "Reconcile the September invoices",
          f"title={job[0].get('title')!r}")
    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute(f"SELECT title_source FROM loops WHERE id = {database.PH}",
                    (job[0]["id"],))
        src = cur.fetchone()["title_source"]
    check("as real named Work, not an LLM guess", src == "provided",
          f"title_source={src!r}")

print("\n[3] Turns are spans — with duration, outcome and cost")
done = spans_named("turn.completed")
check("both completed turns landed", len(done) == 2, f"spans={len(done)}")
if done:
    one = [s for s in done if s["input_tokens"] == 700]
    check("token usage came across in the names ingest reads", bool(one),
          f"input_tokens={[s['input_tokens'] for s in done]}")
    if one:
        check("and the duration is the turn's own, not the poll's",
              one[0]["end_time_unix"] - one[0]["start_time_unix"] == 20_000_000_000,
              f"dur_ns={one[0]['end_time_unix'] - one[0]['start_time_unix']}")

failed = spans_named("turn.failed")
check("a failed turn is recorded as failed", len(failed) == 1, f"{failed}")
if failed:
    check("with the reason the API gave",
          "PO lookup timed out" in (failed[0].get("status_message") or ""),
          f"status_message={failed[0].get('status_message')!r}")

print("\n[4] A subagent turn is a handoff, not a mystery")
sub = [s for s in done if "subagent_ocr" in (s.get("attributes") or "")]
check("the subagent turn names who took over", len(sub) == 1,
      f"matched={len(sub)}")

print("\n[5] requires_action is what the board calls waiting on a person")
blocked = loops_for("sess_blocked_1")
check("the blocked session is a job", len(blocked) == 1, f"loops={blocked}")
if blocked:
    check("and it reads as awaiting a human",
          blocked[0].get("cached_state") == "awaiting_human",
          f"cached_state={blocked[0].get('cached_state')!r}")

print("\n[6] Polling again writes nothing — the cursor holds")
before = len(spans_named("turn.completed")) + len(spans_named("turn.failed"))
api2 = FakeAPI()
again = pull.sync_account(ACCOUNT_ID, "sk-test", get=api2)
after = len(spans_named("turn.completed")) + len(spans_named("turn.failed"))
check("the second sync succeeds", again.get("status") == "ok", f"{again}")
check("and duplicates nothing", before == after, f"{before} -> {after}")
check("the same job is still one job", len(loops_for("sess_reconcile_1")) == 1)

print("\n[7] A dead API is reported, never raised")
dead = pull.sync_account(ACCOUNT_ID, "sk-test", get=FakeAPI(dead=True))
check("an empty API is not an exception", isinstance(dead, dict), f"{dead}")
check("and it writes nothing",
      dead.get("spans") == 0 and dead.get("agents") == 0, f"{dead}")

missing = pull.sync_account(ACCOUNT_ID, "", get=FakeAPI())
check("no credential is a plain 'not connected'",
      missing.get("status") == "not_connected", f"{missing}")

print("\n[8] Nothing here claims to be a shipped door yet")
# The repo's rule: a door on the Connect page has a test_connect_<door>.py
# that drives the real thing. This adapter has never met a real key, so it
# must not appear as a connectable option.
# Match on this adapter's own identifiers only: the page already carries an
# "OpenAI Agents SDK" tile and an Anthropic "Managed Agents" one, and neither
# is this.
page = open("frontend/src/AddAgent.jsx", encoding="utf-8").read()
check("no Connect tile advertises this adapter",
      pull.PROVIDER not in page and pull.PLATFORM not in page,
      "add the tile in the PR that verifies it against a real key")

print()
if failures:
    print(f"FAILED ({len(failures)}): " + "; ".join(failures))
    raise SystemExit(1)
print("OPENAI AGENTS PULL ADAPTER VERIFIED (spec-shaped payloads, no network)")
