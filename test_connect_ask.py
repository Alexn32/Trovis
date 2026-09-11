"""Tests for the guided add-agent chat (POST /connect/ask + asker.ask_connect).

Run:
  OVERSEE_DISABLE_PRICING_SYNC=1 python3 test_connect_ask.py
(uses an isolated temp SQLite DB; never touches the dev/prod DB)

Covers the shape of the reply, the structured-output request, and the rule
that a turn promising a snippet never reaches the user without one.
"""
import json
import os
import pathlib
import tempfile
from types import SimpleNamespace

os.environ["OVERSEE_DISABLE_PRICING_SYNC"] = "1"
os.environ.pop("DATABASE_URL", None)          # force the SQLite branch
os.environ.pop("ANTHROPIC_API_KEY", None)     # start on the 503 path

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()

import database
database.SQLITE_PATH = _tmp.name              # isolate before init_db runs

import asker
import main
from fastapi.testclient import TestClient

# main.py's load_dotenv(override=True) restores ANTHROPIC_API_KEY from .env at
# import time — drop it again so the 503 path is actually exercised (and no
# real Claude calls can ever fire from this test).
os.environ.pop("ANTHROPIC_API_KEY", None)

failures = []
def check(label, cond):
    print(("  PASS " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


print("-- _parse_connect_response units --")
P = asker._parse_connect_response

r = P(json.dumps({
    "answer": "Install the SDK.",
    "options": ["Done", "I got an error"],
    "code": [{"title": "Install", "language": "bash",
              "content": "pip install trovis-agents[openai]"}],
}))
check("valid JSON passes through",
      r["answer"] == "Install the SDK."
      and r["options"] == ["Done", "I got an error"]
      and r["code"][0]["content"].startswith("pip install"))

r = P('```json\n{"answer": "Fenced.", "options": [], "code": []}\n```')
check("fenced JSON stripped + parsed", r["answer"] == "Fenced.")

r = P("Just plain prose, no JSON at all.")
check("plain prose → fallback, empty options/code",
      r == {"answer": "Just plain prose, no JSON at all.",
            "options": [], "code": []})

r = P(json.dumps({
    "answer": "Mixed bag.",
    "options": ["ok", "", 42, "  "],
    "code": [{"content": ""}, "nope", {"content": "echo hi"},
             {"title": 7, "content": "echo bye"}],
}))
check("malformed options/code entries dropped",
      r["options"] == ["ok"] and len(r["code"]) == 2
      and r["code"][1]["title"] is None)

r = P(json.dumps({
    "answer": "Caps.",
    "options": [f"o{i}" for i in range(10)],
    "code": [{"content": f"c{i}"} for i in range(10)],
}))
check("options capped at 6, code at 4",
      len(r["options"]) == 6 and len(r["code"]) == 4)

r = P('{"answer": "Truncated mid-sentence about set')
check("truncated JSON salvages the answer",
      r["answer"] == "Truncated mid-sentence about set"
      and r["options"] == [] and r["code"] == [])

r = P('{"answer": "He said \\"run it\\" and then')
check("salvage handles escaped quotes", r["answer"] == 'He said "run it" and then')

r = P("")
check("empty reply → empty answer, no crash",
      r == {"answer": "", "options": [], "code": []})


print("\n-- code-promise detector --")
# Promises a snippet in THIS turn → an empty `code` array is a broken turn.
for promise in [
    "Add these two lines at the very top of your entry file.",
    "Here are the lines to paste into ~/.claude/settings.json.",
    "Here are the two lines to paste at the top.",
    "Here's the config block for your exporter.",
    "Here's the command for that.",
    "Paste this into your settings file, then restart.",
    "Run this command in the project directory.",
    "Copy the snippet below and run it.",
    "Use the following config for the exporter.",
]:
    check(f"promise detected: {promise[:34]!r}", asker._promises_code(promise))

# Backward-looking or code-free turns must NOT be treated as promises —
# otherwise a legitimate question would be retried and then failed.
for benign in [
    "Run that, then tell me how it went — or paste any error.",
    "What's your agent built with?",
    "Did that work? Paste the error you saw and I'll read it.",
    "Your agent is connected. Rename it on the dashboard whenever you like.",
    "Does your code call query() or client.beta.agents?",
    "That error means init() ran after the framework import.",
]:
    check(f"not a promise: {benign[:34]!r}", not asker._promises_code(benign))

print("\n-- incomplete-reply rule --")
GOOD = {"answer": "Add these two lines.", "options": [],
        "code": [{"title": "Init", "language": "python", "content": "x = 1"}]}
PROMISE_NO_CODE = {"answer": "Add these two lines.", "options": [], "code": []}
QUESTION = {"answer": "What's your agent built with?", "options": ["A", "B"],
            "code": []}
I = asker._connect_reply_incomplete
check("answer + code → complete", not I(GOOD))
check("promise with empty code → incomplete", I(PROMISE_NO_CODE))
check("plain question with empty code → complete", not I(QUESTION))
check("empty answer → incomplete", I({"answer": "", "options": [], "code": []}))
# Truncated at MAX_TOKENS: even a reply that looks whole is partial (its last
# snippet can end mid-line), so it is never shown as-is.
check("truncated → incomplete even with code", I(GOOD, truncated=True))
check("truncated → incomplete for a question too", I(QUESTION, truncated=True))


print("\n-- structured output request (_call_claude) --")
# Stub the Anthropic client itself so the request kwargs can be inspected.
sent = {}


class _FakeMessages:
    def __init__(self, reply, stop_reason="end_turn"):
        self._reply = reply
        self._stop_reason = stop_reason

    def create(self, **kwargs):
        sent.clear()
        sent.update(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self._reply)],
            stop_reason=self._stop_reason,
        )


def _fake_anthropic(reply, stop_reason="end_turn"):
    class _FakeClient:
        def __init__(self, api_key=None):
            self.messages = _FakeMessages(reply, stop_reason)
    return SimpleNamespace(Anthropic=_FakeClient)


real_anthropic = asker.anthropic
reply_json = json.dumps({"answer": "Hi.", "options": [], "code": []})
try:
    asker.anthropic = _fake_anthropic(reply_json)
    text, truncated = asker._call_claude(
        "k", "SYS", "CTX", [{"role": "user", "content": "hello"}],
        output_format=asker.CONNECT_OUTPUT_FORMAT,
    )
    check("returns the reply text", text == reply_json)
    check("end_turn → not truncated", truncated is False)
    check("output_config carries effort AND the json_schema format",
          sent["output_config"].get("effort") == asker.OUTPUT_CONFIG["effort"]
          and sent["output_config"].get("format") is asker.CONNECT_OUTPUT_FORMAT)
    check("format is a json_schema with the connect schema",
          asker.CONNECT_OUTPUT_FORMAT["type"] == "json_schema"
          and asker.CONNECT_OUTPUT_FORMAT["schema"] is asker.CONNECT_RESPONSE_SCHEMA)
    # Mutating the shared OUTPUT_CONFIG would put the connect schema on every
    # Ask call in the process.
    check("module OUTPUT_CONFIG not mutated", "format" not in asker.OUTPUT_CONFIG)
    check("model/thinking/max_tokens unchanged",
          sent["model"] == asker.MODEL and sent["thinking"] == asker.THINKING
          and sent["max_tokens"] == asker.MAX_TOKENS)

    # No schema requested → no format key (the fleet/agent paths).
    asker._call_claude("k", "SYS", "CTX", [{"role": "user", "content": "hi"}])
    check("no output_format → plain output_config",
          "format" not in sent["output_config"])

    asker.anthropic = _fake_anthropic(reply_json, stop_reason="max_tokens")
    _, truncated = asker._call_claude(
        "k", "SYS", "CTX", [{"role": "user", "content": "hello"}],
    )
    check("stop_reason max_tokens → truncated", truncated is True)
finally:
    asker.anthropic = real_anthropic

schema = asker.CONNECT_RESPONSE_SCHEMA
check("schema requires answer/options/code",
      sorted(schema["required"]) == ["answer", "code", "options"]
      and schema["additionalProperties"] is False)
code_item = schema["properties"]["code"]["items"]
check("schema requires all three snippet keys",
      sorted(code_item["required"]) == ["content", "language", "title"]
      and code_item["additionalProperties"] is False)


print("\n-- /connect/ask endpoint --")
with TestClient(main.app) as c:
    body = {"messages": [
        {"role": "assistant", "content": "What's your agent built with?"},
        {"role": "user", "content": "OpenAI Agents SDK"},
    ]}

    r = c.post("/auth/signup", json={
        "email": "guide@test.com", "password": "supersecret123",
        "name": "Guide Tester", "account_type": "individual",
        "org_name": "Guide Co",
    })
    assert r.status_code == 201, r.text
    api_key = r.json()["api_key"]
    headers = {"X-Trovis-Api-Key": api_key}

    # Once users exist, credential-less requests must be rejected. (Before
    # signup the middleware's pre-signup/local-dev mode lets them through.)
    r = c.post("/connect/ask", json=body)
    check("unauthenticated → 401", r.status_code == 401)

    r = c.post("/connect/ask", json=body, headers=headers)
    check("no ANTHROPIC_API_KEY → 503", r.status_code == 503)

    os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-dummy"

    # Stub the Claude call; capture the messages it would have sent. Replies
    # are (text, truncated) — same contract as the real _call_claude.
    captured = {}
    calls = []

    def stub_replies(*replies):
        """Install a stub that returns `replies` in order (last one repeats)."""
        queue = list(replies)

        def fake_call(api_key, system_prompt, context, messages,
                      output_format=None):
            captured["system"] = system_prompt
            captured["messages"] = messages
            captured["output_format"] = output_format
            calls.append(messages)
            return queue.pop(0) if len(queue) > 1 else queue[0]

        asker._call_claude = fake_call

    HAPPY = (json.dumps({
        "answer": "Great — install the SDK first.",
        "options": ["Done", "I got an error"],
        "code": [{"title": "Install", "language": "bash",
                  "content": "pip install trovis-agents[openai]"}],
    }), False)
    # What a reply truncated at MAX_TOKENS looks like after salvage: the prose
    # survives, the snippet it promises does not.
    TRUNCATED = ('{"answer": "Add these two lines at the top of your entry '
                 'file.", "options": [], "code": [{"title": "Init", '
                 '"language": "python", "content": "from trovis import ini',
                 True)
    PROMISE_NO_CODE = (json.dumps({
        "answer": "Here are the two lines to paste at the top.",
        "options": [], "code": [],
    }), False)
    QUESTION = (json.dumps({
        "answer": "Which framework does your code import?",
        "options": ["OpenAI Agents SDK", "Claude Agent SDK"], "code": [],
    }), False)

    real_call = asker._call_claude
    try:
        calls.clear()
        stub_replies(HAPPY)
        r = c.post("/connect/ask", json=body, headers=headers)
        check("stubbed happy path → 200", r.status_code == 200)
        data = r.json() if r.status_code == 200 else {}
        check("response carries answer/options/code",
              data.get("answer") == "Great — install the SDK first."
              and data.get("options") == ["Done", "I got an error"]
              and data.get("code", [{}])[0].get("language") == "bash")
        check("a complete turn is not re-asked", len(calls) == 1)
        check("assistant-first history got a synthetic user primer",
              captured["messages"][0]["role"] == "user"
              and "connect" in captured["messages"][0]["content"]
              and captured["messages"][-1]["role"] == "user")
        check("connect system prompt used (placeholders + chips rules)",
              "TROVIS_API_KEY" in captured["system"]
              and "options" in captured["system"])
        check("the turn is constrained to the connect schema",
              captured["output_format"] is asker.CONNECT_OUTPUT_FORMAT)

        # Truncated first reply, good second → the user gets the snippet, and
        # never the promise with an empty code array.
        calls.clear()
        stub_replies(TRUNCATED, HAPPY)
        r = c.post("/connect/ask", json=body, headers=headers)
        data = r.json() if r.status_code == 200 else {}
        check("truncated reply → re-asked once, 200 with code",
              r.status_code == 200 and len(calls) == 2
              and len(data.get("code") or []) == 1)
        check("the re-ask asks for the missing snippet",
              calls[1][-1]["role"] == "user"
              and "code" in calls[1][-1]["content"]
              and calls[1][-2]["role"] == "assistant")

        # Promise with no snippet (not truncated) → same rule.
        calls.clear()
        stub_replies(PROMISE_NO_CODE, HAPPY)
        r = c.post("/connect/ask", json=body, headers=headers)
        data = r.json() if r.status_code == 200 else {}
        check("promise without code → re-asked once, 200 with code",
              r.status_code == 200 and len(calls) == 2
              and len(data.get("code") or []) == 1)

        # Both attempts promise code without attaching it: fail soft (the
        # guide shows "try again" next to its manual path) rather than 200
        # with prose the user cannot act on.
        calls.clear()
        stub_replies(PROMISE_NO_CODE)
        r = c.post("/connect/ask", json=body, headers=headers)
        check("promise twice → 502, never a 200 with empty code",
              r.status_code == 502)
        check("exactly one re-ask, no retry storm", len(calls) == 2)

        calls.clear()
        stub_replies(TRUNCATED)
        r = c.post("/connect/ask", json=body, headers=headers)
        check("truncated twice → 502", r.status_code == 502)

        # A genuine question has nothing to attach — it must sail through.
        calls.clear()
        stub_replies(QUESTION)
        r = c.post("/connect/ask", json=body, headers=headers)
        data = r.json() if r.status_code == 200 else {}
        check("code-free question → 200, no re-ask",
              r.status_code == 200 and len(calls) == 1
              and data.get("code") == [] and len(data.get("options") or []) == 2)

        # If the API ever rejects the schema, the guide degrades to the
        # prompt-only request instead of going dark.
        import anthropic as _anthropic
        import httpx as _httpx

        formats = []

        def reject_schema(api_key, system_prompt, context, messages,
                          output_format=None):
            formats.append(output_format)
            if output_format is not None:
                raise _anthropic.BadRequestError(
                    "output_config.format is not supported",
                    response=_httpx.Response(
                        400, request=_httpx.Request("POST", "http://x")
                    ),
                    body=None,
                )
            return HAPPY

        asker._call_claude = reject_schema
        r = c.post("/connect/ask", json=body, headers=headers)
        data = r.json() if r.status_code == 200 else {}
        check("schema rejected → falls back unconstrained, still 200",
              r.status_code == 200 and len(data.get("code") or []) == 1
              and formats == [asker.CONNECT_OUTPUT_FORMAT, None])
    finally:
        asker._call_claude = real_call

    # Real _call_claude validates before any network call → 400.
    r = c.post("/connect/ask", json={"messages": [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]}, headers=headers)
    check("assistant-last history → 400", r.status_code == 400)

    r = c.post("/connect/ask", json={"messages": []}, headers=headers)
    check("empty history → 400", r.status_code == 400)

# --- the guide knows every door the manual wizard does ----------------------
#
# The AI guide and the manual wizard are two routes to the same setup. When a
# door ships on one and not the other, the guide confidently gives a recipe
# that doesn't exist — which is worse than admitting it doesn't know.

PROMPT = asker.SYSTEM_CONNECT

check("the guide knows the Grok Bot door",
      "Grok Bot" in PROMPT)
check("with the MCP URL as a substitutable placeholder, not a hardcoded host",
      "TROVIS_MCP_URL" in PROMPT
      and "https://api.trovisai.com/mcp/grok" not in PROMPT)
check("and the auth header it actually needs",
      "Authorization: Bearer TROVIS_API_KEY" in PROMPT)
for tool in ("report_job_started", "report_job_waiting",
             "report_job_finished", "report_job_failed"):
    check(f"the guide can name {tool}", tool in PROMPT)
check("the guide is told the title IS the job name",
      "title IS the job name" in PROMPT)
check("the guide tells the truth: nothing lands unless the Bot calls in",
      "nothing is recorded unless the Bot calls these tools" in PROMPT)
# The path that worked on the first real connection: hand the whole setup to
# the Bot, which adds its own MCP server. Hand-editing MCP settings is the
# fallback — a guide that leads with it teaches the slower route.
check("the guide leads with giving the setup to the Bot itself",
      "Give the setup TO THE BOT" in PROMPT
      and "fallback, not the headline" in PROMPT)
check("and warns about the placeholder-in-the-header failure",
      "PLACEHOLDER in the auth header" in PROMPT
      and "remove and re-add" in PROMPT)
check("the guide asks the bot for its role, not just its jobs",
      "bot_role" in PROMPT and "what the bot is FOR" in PROMPT)

# The ambiguity that makes this door dangerous to guess at: "Grok" alone.
check("the guide must ask WHICH Grok before answering",
      '"Grok" is ambiguous' in PROMPT and "Never guess" in PROMPT)
check("and it has both chip labels to offer",
      "Grok (xAI SDK)" in PROMPT and "Grok Bot" in PROMPT)
check("the xai-sdk door is still there, unconfused with the Bot one",
      "trovis-agents[xai]" in PROMPT and 'platform="xai"' in PROMPT)

# Every door the picker offers should be reachable through the guide too.
_wizard = pathlib.Path("frontend/src/AddAgent.jsx").read_text()
_chips = pathlib.Path("frontend/src/ConnectGuide.jsx").read_text()
for label in ("Grok Bot", "Grok (xAI SDK)", "ChatGPT", "OpenClaw"):
    check(f"the guide's opening chips offer {label}", label in _chips)
    check(f"and the manual picker offers {label}", label in _wizard)

print()
if failures:
    print(f"FAILED: {len(failures)} check(s):")
    for f in failures:
        print("  - " + f)
    raise SystemExit(1)
print("ALL CHECKS PASSED")
