"""Unit tests for the loop story derivations: possession segments
(compute_loop_segments), logical stream ordering, and template narration
(narrate_events). Pure functions, no DB — same idiom as test_loop_state.

Run:
  OVERSEE_DISABLE_PRICING_SYNC=1 python3 test_loop_story.py
"""
import os

os.environ["OVERSEE_DISABLE_PRICING_SYNC"] = "1"
os.environ.pop("DATABASE_URL", None)
os.environ.pop("TROVIS_LOOP_NARRATION", None)

import loops
from loops import compute_loop_segments, narrate_events, sort_stream

failures = []
def check(label, cond):
    print(("  PASS " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


T = 1_900_000_000_000_000_000


def ev(type, ts, actor_type="agent", actor="svc:main", **payload):
    return {"type": type, "ts": T + ts, "actor_type": actor_type,
            "actor": actor, "payload": payload}


def act(ts, span_name="tool_call", tool=None, actor="svc:main"):
    p = {"span_name": span_name}
    if tool:
        p["tool"] = tool
    return {"type": "activity", "ts": T + ts, "actor_type": "agent",
            "actor": actor, "payload": p}


# --- Single possession -------------------------------------------------------

segs = compute_loop_segments([ev("loop_opened", 0),
                              act(10, tool="exec"), act(20, "model_call")])
check("single possession: one segment, ongoing (end None)",
      len(segs) == 1 and segs[0]["end_ns"] is None
      and segs[0]["holder"] == "svc:main" and not segs[0]["waiting"])
check("touches: tool attaches, llm counts only",
      segs[0]["touches"] == [{"name": "exec", "count": 1}]
      and segs[0]["event_count"] == 2)

segs = compute_loop_segments([ev("loop_opened", 0), act(10, tool="exec"),
                              ev("loop_closed", 20)])
check("close ends the final segment at the close ts",
      len(segs) == 1 and segs[0]["end_ns"] == T + 20)

# --- Handoffs ----------------------------------------------------------------

three_handoffs = [
    ev("loop_opened", 0),
    act(10, tool="exec"),
    ev("handoff_initiated", 20, direction="to_human", target_name="Sarah"),
    ev("handoff_accepted", 30, actor_type="human", actor="7"),
    act(40, tool="read"),
    ev("handoff_initiated", 50, direction="to_agent", target_id="helper-bot"),
    act(60, actor="svc:helper"),           # agent activity resumes possession
    ev("handoff_initiated", 70, direction="to_human"),
    ev("handoff_declined", 80),
    ev("loop_closed", 90),
]
segs = compute_loop_segments(three_handoffs)
check("3-handoff loop yields 7 segments",
      len(segs) == 7,)
check("handoff target possession: named human, waiting",
      segs[1]["holder_type"] == "human" and segs[1]["holder"] == "Sarah"
      and segs[1]["waiting"] is True
      and segs[1]["start_ns"] == T + 20 and segs[1]["end_ns"] == T + 30)
check("accepted returns possession to the last agent",
      segs[2]["holder_type"] == "agent" and segs[2]["holder"] == "svc:main"
      and segs[2]["touches"] == [{"name": "read", "count": 1}])
check("to_agent handoff: agent target holds, waiting",
      segs[3]["holder_type"] == "agent" and segs[3]["holder"] == "helper-bot"
      and segs[3]["waiting"] is True)
check("agent activity resumes from waiting",
      segs[4]["holder"] == "svc:helper" and segs[4]["waiting"] is False
      and segs[4]["event_count"] == 1)
check("generic human target when unnamed",
      segs[5]["holder_type"] == "human" and segs[5]["holder"] == "human")
check("declined returns possession to the prior holder",
      segs[6]["holder"] == "svc:helper" and segs[6]["end_ns"] == T + 90)

# --- Straggler folding -------------------------------------------------------

closed = [ev("loop_opened", 0), act(10, tool="exec"),
          ev("handoff_initiated", 20, direction="to_human"),
          ev("handoff_completed", 30),
          ev("loop_closed", 40),
          act(15, tool="exec"),   # straggler timestamped in segment 0
          act(35, "model_call")]  # straggler timestamped in segment 2
segs = compute_loop_segments(closed)
check("stragglers never extend the chain past close",
      len(segs) == 3 and segs[-1]["end_ns"] == T + 40)
check("straggler folds into the segment covering its timestamp",
      segs[0]["touches"] == [{"name": "exec", "count": 2}]
      and segs[0]["event_count"] == 2 and segs[2]["event_count"] == 1)

# to_system wait: the one case where a tool/system HOLDS the work.
segs = compute_loop_segments([
    ev("loop_opened", 0),
    act(5, tool="exec"),
    ev("handoff_initiated", 10, direction="to_system", target_id="stripe"),
    act(20),  # agent activity resumes possession from the system
    ev("loop_closed", 30),
])
check("to_system opens a waiting segment held by the named system",
      len(segs) == 3 and segs[1]["holder_type"] == "system"
      and segs[1]["holder"] == "stripe" and segs[1]["waiting"] is True)
check("agent activity resumes from a system wait",
      segs[1]["end_ns"] == T + 20 and segs[2]["holder_type"] == "agent")

# The handoff-carrying span lands at the SAME timestamp as handoff_initiated
# (attrs ride the span). It is the act of handing off — it must never count
# as agent activity resuming the wait it just started.
segs = compute_loop_segments([
    ev("loop_opened", 0),
    ev("handoff_initiated", 10, direction="to_human", target_name="Sarah"),
    act(10),  # the span that carried the handoff attributes
])
check("handoff-carrying span does not resume its own wait",
      len(segs) == 2 and segs[1]["waiting"] is True and segs[1]["end_ns"] is None)

# --- Ongoing waiting possession -----------------------------------------------

segs = compute_loop_segments([ev("loop_opened", 0),
                              ev("handoff_initiated", 10, direction="to_human")])
check("unclosed loop: waiting segment ongoing",
      len(segs) == 2 and segs[1]["end_ns"] is None and segs[1]["waiting"])

# --- Logical stream ordering ----------------------------------------------------

shuffled = [act(5, "model_call"),            # deferred span, ts BEFORE opened
            ev("loop_closed", 30),
            ev("loop_opened", 8),
            act(40, "llm_output")]           # post-close ts
ordered = sort_stream(shuffled)
check("loop_opened always sorts first, loop_closed always last",
      ordered[0]["type"] == "loop_opened" and ordered[-1]["type"] == "loop_closed")
segs = compute_loop_segments(shuffled)
check("segments consume the corrected order (one possession, folds intact)",
      len(segs) == 1 and segs[0]["end_ns"] == T + 30
      and segs[0]["event_count"] == 2)

# --- Narration ------------------------------------------------------------------

stream = [
    ev("loop_opened", 0),
    act(10, tool="exec"), act(11, tool="exec"), act(12, tool="exec"),
    act(20, "model_call"), act(21, "model_call"),
    act(30, tool="weird_tool"),
    act(35, "model_call"),
    act(40, "agent_run_complete"),
    ev("loop_closed", 50, reason="completed_by_agent"),
]
n = narrate_events(stream)
sentences = [e["sentence"] for e in n]
check("collapse: consecutive same-tool calls", "Ran a command · 3×" in sentences)
check("collapse: consecutive LLM calls", "Worked through it (2 steps)" in sentences)
check("single LLM call reads as one thought", "Thought it through" in sentences)
check("unmapped tool falls back to Used {name}", "Used weird_tool" in sentences)
check("MCP tool names render as Used {Server}",
      loops.tool_sentence("mcp__shopify__lookup_orders_by_email") == "Used Shopify"
      and loops.tool_sentence("mcp__creative-toolkit__generate_image", 2)
      == "Used Creative Toolkit · 2×")
check("malformed MCP name falls back safely",
      loops.tool_sentence("mcp__broken") == "Used mcp__broken")
check("agent_run_complete omitted (loop_closed covers it)",
      not any((e.get("payload") or {}).get("span_name") == "agent_run_complete" for e in n))
check("lifecycle sentences present",
      sentences[0] == "Started" and sentences[-1] == "Closed by agent — completed")
check("collapsed entry keeps raw span_name + count in payload",
      any((e.get("payload") or {}).get("span_name") == "tool_call"
          and (e.get("payload") or {}).get("count") == 3 for e in n))

# Jargon extension: no raw identifiers reachable as a sentence, for any input.
jargon_stream = stream + [
    act(60, "llm_output"),
    ev("handoff_initiated", 65, direction="to_human", reason="check"),
    ev("stall_detected", 70),
    ev("loop_closed", 80, reason="ingestion_artifact"),
]
for e in narrate_events(jargon_stream):
    s = e["sentence"]
    if any(bad in s for bad in ("model_call", "tool_call", "llm_output", "span")):
        check(f"jargon leaked: {s}", False)
        break
else:
    check("no model_call/tool_call/llm_output/span reachable as a sentence", True)

check("ingestion_artifact close narrates honestly",
      any(e["sentence"] == "Closed — stray telemetry from a completed run"
          for e in narrate_events(jargon_stream)))

# LLM narration is stubbed, not silently wrong.
os.environ["TROVIS_LOOP_NARRATION"] = "llm"
try:
    narrate_events(stream)
    check("TROVIS_LOOP_NARRATION=llm raises NotImplementedError", False)
except NotImplementedError:
    check("TROVIS_LOOP_NARRATION=llm raises NotImplementedError", True)
finally:
    os.environ.pop("TROVIS_LOOP_NARRATION", None)

# Template title shape (pure part of the title pipeline).
check("template title shape",
      loops.template_title({"agent": "billing-bot", "tools": ["exec", "read"],
                            "action_count": 7}) == "billing-bot · exec · 7 actions")
check("template title with no tools",
      loops.template_title({"agent": "a", "tools": [], "action_count": 1})
      == "a · run · 1 actions")

print()
if failures:
    print(f"FAILED: {len(failures)} check(s):")
    for f in failures:
        print("  - " + f)
    raise SystemExit(1)
print("ALL CHECKS PASSED")
