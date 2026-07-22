"""Agentic Ask: read-only tools + the retrieval loop.

Verifies (isolated temp DB, mocked Anthropic client — no network):
  1. get_agent_outputs surfaces captured message/response content (data path).
  2. _run_tool('get_recent_exchanges') returns that real content to the model.
  3. Tools are account-scoped — a different account sees nothing.
  4. The loop executes a tool call, feeds the result back, and returns the
     final answer — and the tool_result it fed the model carried the real
     captured content (so Ask can now quote what an agent actually said).
"""

import json
import os
import tempfile
import time
import uuid
import types

os.environ["OVERSEE_DISABLE_PRICING_SYNC"] = "1"
os.environ.pop("DATABASE_URL", None)
os.environ["ANTHROPIC_API_KEY"] = "sk-test-dummy"  # _require_api_key only checks presence

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()

import database as db  # noqa: E402
db.SQLITE_PATH = _tmp.name
import asker  # noqa: E402

_fail = []
def check(label, cond):
    print(f"  {'PASS' if cond else 'FAIL'} {label}")
    if not cond:
        _fail.append(label)


def _span(svc, attrs, i, now, name="op"):
    return {
        "trace_id": uuid.uuid4().hex, "span_id": uuid.uuid4().hex[:16], "parent_span_id": None,
        "service_name": svc, "span_name": name, "kind": 0,
        "start_time_unix": now - i * 1_000_000_000, "end_time_unix": now - i * 1_000_000_000 + 5_000_000,
        "status_code": 0, "status_message": "",
        "attributes": attrs, "resource_attributes": {"service.name": svc},
    }


def main():
    db.init_db()
    acct = db.create_account("owner@test.com", account_type="business", name="Co")
    aid = acct["id"]
    now = int(time.time() * 1e9)

    USER_MSG = "Please summarize the Q3 revenue report."
    AGENT_MSG = "Q3 revenue was $4.2M, up 12% QoQ; EMEA led the growth."
    db.insert_spans([
        _span("demo", {"trovis.event.type": "message_received", "trovis.message.content": USER_MSG}, 2, now, "message_received"),
        _span("demo", {"trovis.event.type": "llm_output", "trovis.response.content": AGENT_MSG}, 1, now, "llm_output"),
    ], account_id=aid)

    # 1. data path
    outs = db.get_agent_outputs("demo", account_id=aid, limit=10)
    contents = " ".join(o.get("content", "") for o in outs)
    check("get_agent_outputs surfaces captured message + response", USER_MSG in contents and AGENT_MSG in contents)

    # 2. tool returns real content
    tool_out = asker._run_tool("get_recent_exchanges", {"service_name": "demo"}, aid)
    check("get_recent_exchanges tool returns the agent response text", AGENT_MSG in tool_out)

    # 3. account scoping — a different account sees nothing
    other = asker._run_tool("get_recent_exchanges", {"service_name": "demo"}, aid + 999)
    check("tool is account-scoped (other account gets no content)", AGENT_MSG not in other and '"count": 0' in other)

    # 4. loop: script the Anthropic client — turn 1 asks for the tool, turn 2 answers.
    captured = {"tools_sent": None, "tool_result_seen": None}

    def blk(**kw):
        return types.SimpleNamespace(**kw)

    class FakeMessages:
        def __init__(self):
            self.calls = 0
        def create(self, **kwargs):
            self.calls += 1
            captured["tools_sent"] = kwargs.get("tools")
            if self.calls == 1:
                return types.SimpleNamespace(
                    stop_reason="tool_use",
                    content=[blk(type="tool_use", name="get_recent_exchanges",
                                 input={"service_name": "demo"}, id="tu_1")],
                )
            # turn 2 — inspect the tool_result the loop fed back
            for m in kwargs.get("messages", []):
                if m.get("role") == "user" and isinstance(m.get("content"), list):
                    for c in m["content"]:
                        if isinstance(c, dict) and c.get("type") == "tool_result":
                            captured["tool_result_seen"] = c.get("content")
            return types.SimpleNamespace(
                stop_reason="end_turn",
                content=[blk(type="text", text=f'The last thing demo said: "{AGENT_MSG}"')],
            )

    class FakeClient:
        def __init__(self, *a, **k):
            self.messages = FakeMessages()

    asker.anthropic.Anthropic = FakeClient  # monkeypatch

    answer = asker.ask_about_agent("demo", aid, [{"role": "user", "content": "what was the last message demo sent?"}])
    check("loop returned a final answer quoting the real content", AGENT_MSG in answer)
    check("loop actually passed tools to the model", bool(captured["tools_sent"]))
    check("tool_result fed back to the model carried the captured content",
          captured["tool_result_seen"] and AGENT_MSG in captured["tool_result_seen"])

    print("\n" + ("ALL CHECKS PASSED" if not _fail else f"{len(_fail)} FAILED"))
    try:
        os.remove(_tmp.name)
    except OSError:
        pass
    raise SystemExit(1 if _fail else 0)


if __name__ == "__main__":
    main()
