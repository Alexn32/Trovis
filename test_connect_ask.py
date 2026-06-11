"""Tests for the guided add-agent chat (POST /connect/ask + asker.ask_connect).

Run:
  OVERSEE_DISABLE_PRICING_SYNC=1 python3 test_connect_ask.py
(uses an isolated temp SQLite DB; never touches the dev/prod DB)
"""
import json
import os
import tempfile

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

    # Stub the Claude call; capture the messages it would have sent.
    captured = {}
    def fake_call(api_key, system_prompt, context, messages):
        captured["system"] = system_prompt
        captured["messages"] = messages
        return json.dumps({
            "answer": "Great — install the SDK first.",
            "options": ["Done", "I got an error"],
            "code": [{"title": "Install", "language": "bash",
                      "content": "pip install trovis-agents[openai]"}],
        })
    real_call = asker._call_claude
    asker._call_claude = fake_call
    try:
        r = c.post("/connect/ask", json=body, headers=headers)
        check("stubbed happy path → 200", r.status_code == 200)
        data = r.json() if r.status_code == 200 else {}
        check("response carries answer/options/code",
              data.get("answer") == "Great — install the SDK first."
              and data.get("options") == ["Done", "I got an error"]
              and data.get("code", [{}])[0].get("language") == "bash")
        check("assistant-first history got a synthetic user primer",
              captured["messages"][0]["role"] == "user"
              and "connect" in captured["messages"][0]["content"]
              and captured["messages"][-1]["role"] == "user")
        check("connect system prompt used (placeholders + chips rules)",
              "TROVIS_API_KEY" in captured["system"]
              and "options" in captured["system"])
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

print()
if failures:
    print(f"FAILED: {len(failures)} check(s):")
    for f in failures:
        print("  - " + f)
    raise SystemExit(1)
print("ALL CHECKS PASSED")
