"""Unit tests for loops.compute_loop_state — the pure loop state machine.

No DB, no server: every rule, both threshold boundaries, and handoff
matching (by handoff_id and by most-recent-unresolved).

Run:
  OVERSEE_DISABLE_PRICING_SYNC=1 python3 test_loop_state.py
"""
import os

os.environ["OVERSEE_DISABLE_PRICING_SYNC"] = "1"
os.environ.pop("DATABASE_URL", None)

import loops
from loops import NS_PER_S, compute_loop_state

failures = []
def check(label, cond):
    print(("  PASS " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


T0 = 1_900_000_000_000_000_000  # base ts, well past the first-seen floor
NOW = T0 + 3600 * NS_PER_S      # "now" = one hour after T0


def ev(type, ts, actor_type="agent", actor="svc:main", **payload):
    return {"type": type, "ts": ts, "actor_type": actor_type,
            "actor": actor, "payload": payload}


def state(events, now_ns=NOW, **kw):
    return compute_loop_state(events, now_ns=now_ns, **kw)


# --- Rule 7: open ---
check("no events -> open", state([]) == "open")
check("loop_opened only -> open", state([ev("loop_opened", T0)]) == "open")

# --- Rule 6: working ---
check("opened + activity -> working",
      state([ev("loop_opened", T0), ev("activity", T0 + 1)]) == "working")
check("activity only (no loop_opened) -> working",
      state([ev("activity", T0)]) == "working")

# --- Rules 1-2: closed ---
check("loop_closed (no reason) -> done",
      state([ev("loop_opened", T0), ev("loop_closed", T0 + 2)]) == "done")
check("loop_closed reason=closed_by_user -> done",
      state([ev("loop_closed", T0, reason="closed_by_user")]) == "done")
check("loop_closed reason=abandoned -> abandoned",
      state([ev("loop_opened", T0),
             ev("loop_closed", T0 + 2, actor_type="system", actor="system",
                reason="abandoned")]) == "abandoned")
check("close beats a later-timestamped unresolved handoff",
      state([ev("loop_opened", T0),
             ev("loop_closed", T0 + 1),
             ev("handoff_initiated", T0 + 2, direction="to_human")]) == "done")

# --- Rules 3-4: unresolved handoffs + stall boundary ---
STALL_NS = loops.STALL_THRESHOLD_S * NS_PER_S
h_human = [ev("loop_opened", T0), ev("handoff_initiated", T0 + 5, direction="to_human")]
check("unresolved to_human handoff -> awaiting_human",
      state(h_human) == "awaiting_human")
check("to_human at exactly STALL_THRESHOLD -> still awaiting_human (boundary is >)",
      state(h_human, now_ns=T0 + 5 + STALL_NS) == "awaiting_human")
check("to_human one ns past STALL_THRESHOLD -> stalled",
      state(h_human, now_ns=T0 + 5 + STALL_NS + 1) == "stalled")

h_agent = [ev("loop_opened", T0), ev("handoff_initiated", T0 + 5, direction="to_agent")]
check("unresolved to_agent handoff -> awaiting_agent",
      state(h_agent) == "awaiting_agent")
check("to_agent past STALL_THRESHOLD -> stalled",
      state(h_agent, now_ns=T0 + 5 + STALL_NS + 1) == "stalled")

check("pending to_human wins over later pending to_agent (rule order)",
      state([ev("handoff_initiated", T0, direction="to_human"),
             ev("handoff_initiated", T0 + 10, direction="to_agent")])
      == "awaiting_human")

# --- to_system mirrors (symmetric with the human/agent rules) ---
h_sys = [ev("loop_opened", T0), ev("handoff_initiated", T0 + 5, direction="to_system",
             target_id="stripe")]
check("unresolved to_system handoff -> awaiting_system",
      state(h_sys) == "awaiting_system")
check("to_system at exactly STALL_THRESHOLD -> still awaiting_system",
      state(h_sys, now_ns=T0 + 5 + STALL_NS) == "awaiting_system")
check("to_system one ns past STALL_THRESHOLD -> stalled",
      state(h_sys, now_ns=T0 + 5 + STALL_NS + 1) == "stalled")
check("handoff_completed resolves to_system -> working",
      state(h_sys + [ev("handoff_completed", T0 + 10)]) == "working")
check("handoff_declined resolves to_system -> working",
      state(h_sys + [ev("handoff_declined", T0 + 10)]) == "working")
check("to_system resolution matches by handoff_id",
      state([ev("handoff_initiated", T0 + 1, direction="to_system", handoff_id="s1"),
             ev("handoff_initiated", T0 + 2, direction="to_agent", handoff_id="a1"),
             ev("handoff_completed", T0 + 3, handoff_id="a1")]) == "awaiting_system")
check("pending to_human wins over pending to_system (rule order)",
      state([ev("handoff_initiated", T0, direction="to_system"),
             ev("handoff_initiated", T0 + 10, direction="to_human")])
      == "awaiting_human")

# --- Handoff resolution ---
for res in ("handoff_accepted", "handoff_completed", "handoff_declined"):
    check(f"{res} resolves the handoff -> working",
          state([ev("loop_opened", T0),
                 ev("handoff_initiated", T0 + 1, direction="to_human"),
                 ev(res, T0 + 2)]) == "working")

check("resolution matches by handoff_id, older unmatched handoff still drives state",
      state([ev("handoff_initiated", T0 + 1, direction="to_human", handoff_id="h1"),
             ev("handoff_initiated", T0 + 2, direction="to_agent", handoff_id="h2"),
             ev("handoff_completed", T0 + 3, handoff_id="h2")])
      == "awaiting_human")
check("resolution without handoff_id resolves most-recent-unresolved",
      state([ev("handoff_initiated", T0 + 1, direction="to_human"),
             ev("handoff_initiated", T0 + 2, direction="to_agent"),
             ev("handoff_completed", T0 + 3)])  # resolves the to_agent one
      == "awaiting_human")
check("resolution with unmatched handoff_id resolves nothing",
      state([ev("handoff_initiated", T0 + 1, direction="to_human", handoff_id="h1"),
             ev("handoff_completed", T0 + 2, handoff_id="nope")])
      == "awaiting_human")
check("two handoffs, two anonymous resolutions -> all resolved -> working",
      state([ev("handoff_initiated", T0 + 1, direction="to_human"),
             ev("handoff_initiated", T0 + 2, direction="to_agent"),
             ev("handoff_completed", T0 + 3),
             ev("handoff_accepted", T0 + 4)]) == "working")

# --- Rule 5: idle past ABANDON_THRESHOLD ---
ABANDON_NS = loops.ABANDON_THRESHOLD_S * NS_PER_S
idle = [ev("loop_opened", T0), ev("activity", T0 + 10)]
check("idle at exactly ABANDON_THRESHOLD -> still working (boundary is >)",
      state(idle, now_ns=T0 + 10 + ABANDON_NS) == "working")
check("idle one ns past ABANDON_THRESHOLD -> stalled",
      state(idle, now_ns=T0 + 10 + ABANDON_NS + 1) == "stalled")
check("open loop (no activity) idle past ABANDON_THRESHOLD -> stalled",
      state([ev("loop_opened", T0)], now_ns=T0 + ABANDON_NS + 1) == "stalled")

# --- Threshold overrides ---
check("stall_threshold_s override honored",
      state(h_human, now_ns=T0 + 5 + 11 * NS_PER_S, stall_threshold_s=10) == "stalled")
check("abandon_threshold_s override honored",
      state(idle, now_ns=T0 + 10 + 11 * NS_PER_S, abandon_threshold_s=10) == "stalled")

# --- Robustness ---
check("unsorted input handled (defensive re-sort)",
      state([ev("loop_closed", T0 + 5),
             ev("loop_opened", T0),
             ev("activity", T0 + 2)]) == "done")
check("string payload rows normalize fail-soft",
      loops.normalize_loop_event(
          {"type": "loop_closed", "event_time_unix": T0, "actor_type": "human",
           "actor": "7", "payload": '{"reason": "abandoned"}'}
      )["payload"] == {"reason": "abandoned"})
check("corrupt payload normalizes to {}",
      loops.normalize_loop_event(
          {"type": "activity", "event_time_unix": T0, "payload": "{not json"}
      )["payload"] == {})

print()
if failures:
    print(f"FAILED: {len(failures)} check(s):")
    for f in failures:
        print("  - " + f)
    raise SystemExit(1)
print("ALL CHECKS PASSED")
