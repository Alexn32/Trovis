"""The fleet-pulse insight is truth-locked: a sentence ships only if the
packet entails it.

This is the whole safety case for putting generated text on Home. A dashboard
that invents one number stops being evidence for any of them, so the validator
fails CLOSED — it rejects anything it cannot prove, and rejecting a good
sentence costs nothing while showing a bad one costs the page its credibility.

Covers the four ways a plausible-sounding sentence is actually wrong:
a number that is not in DATA, a name that is not in DATA, a banned claim about
health or money, and a graphic naming a series we do not have. Plus the
latency contract: the pulse renders without the model, and a slow model never
holds up a request.

Run:
  OVERSEE_DISABLE_PRICING_SYNC=1 python3 test_pulse_insight.py
"""
import os
import tempfile
import time

os.environ["OVERSEE_DISABLE_PRICING_SYNC"] = "1"
os.environ["TROVIS_DISABLE_ALERTS"] = "1"
os.environ["TROVIS_DISABLE_LOOP_SWEEP"] = "1"
os.environ.pop("DATABASE_URL", None)

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()

import database
database.SQLITE_PATH = _tmp.name

import pulse

failures = []
def check(label, cond):
    print(("  PASS " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


PACKET = {
    "agents_count": 5,
    "need_a_look": [{"name": "flaky-agent"}],
    "finished_this_week": 18,
    "finished_last_week": 12,
    "waiting_now": 3,
    "stuck_now": 2,
}


print("--- a sentence the packet entails is kept ---")
for good in [
    "18 finished this week, up from 12.",
    "flaky-agent is the only one needing a look.",
    "Finished work rose week over week, and 2 are stuck.",
    "5 agents reporting; 1 needs a look.",
]:
    ok, why = pulse.validate_insight(good, PACKET)
    check(f"kept: {good!r}" + ("" if ok else f"  <-- {why}"), ok)


print("\n--- an invented NUMBER is dropped ---")
for bad in [
    "23 finished this week.",                    # not in DATA
    "Finished work is up 50 from last week.",    # arithmetic the model did
    "Throughput rose by 6 items.",               # 18-12 is not a DATA value
]:
    ok, why = pulse.validate_insight(bad, PACKET)
    check(f"dropped: {bad!r}", not ok)

# The one that matters most: a DERIVED number looks right and is unverifiable.
ok, _ = pulse.validate_insight("Finished work rose 6 over last week.", PACKET)
check("a derived number (18-12=6) is not 'in DATA' and is dropped", not ok)


print("\n--- an invented NAME is dropped ---")
for bad in [
    "Billing Bot is falling behind.",     # no such agent in DATA
    "The Refunds queue is backing up.",   # invented kind of work
]:
    ok, why = pulse.validate_insight(bad, PACKET)
    check(f"dropped: {bad!r}", not ok)
check("a name that IS in DATA passes",
      pulse.validate_insight("flaky-agent needs a look.", PACKET)[0])


print("\n--- banned claims are dropped whatever DATA says ---")
for bad in [
    "Spend is up $40 this week.",
    "Throughput improved 30%.",
    "The fleet is healthy.",
    "Agent efficiency improved.",
    "Uptime held steady.",
    "One loop is stuck.",
    "A handoff is waiting.",
]:
    ok, why = pulse.validate_insight(bad, PACKET)
    check(f"dropped: {bad!r}", not ok)


print("\n--- empty / oversized ---")
check("empty is not an insight", not pulse.validate_insight("", PACKET)[0])
check("whitespace is not an insight", not pulse.validate_insight("   ", PACKET)[0])
check("an essay is dropped", not pulse.validate_insight("word " * 100, PACKET)[0])


print("\n--- the graphic chooser ---")
check("model choice is honoured when the series exists",
      pulse.choose_graphic(PACKET, "week_finished") == "week_finished")
check("a series we do NOT have is overruled",
      pulse.choose_graphic(PACKET, "week_stuck") == "week_finished")
check("garbage from the model is overruled",
      pulse.choose_graphic(PACKET, "pie_chart") == "week_finished")
check("default order: finished wins when both weeks exist",
      pulse.choose_graphic(PACKET, None) == "week_finished")
check("falls back to need_a_look when there is no week series",
      pulse.choose_graphic({"need_a_look": [{"name": "x"}]}, None) == "need_a_look")
check("none when there is nothing to draw",
      pulse.choose_graphic({"agents_count": 2, "need_a_look": []}, None) == "none")
check("'none' from the model is respected",
      pulse.choose_graphic({"agents_count": 2, "need_a_look": []}, "none") == "none")


print("\n--- a thin packet earns no insight ---")
thin = {"agents_count": 1, "need_a_look": []}
check("a sentence quoting a number not in a thin packet is dropped",
      not pulse.validate_insight("3 finished this week.", thin)[0])


print("\n--- the cache is keyed on the FACTS ---")
a = dict(PACKET)
b = {k: PACKET[k] for k in reversed(list(PACKET))}   # same facts, different order
check("key order does not change the packet key", pulse.packet_key(a) == pulse.packet_key(b))
c = {**PACKET, "finished_this_week": 19}
check("a changed fact changes the key", pulse.packet_key(a) != pulse.packet_key(c))


print("\n--- latency: a slow model never holds up the request ---")
pulse._cache.clear()
pulse._inflight.clear()
os.environ["ANTHROPIC_API_KEY"] = "test-key-not-used"
os.environ["TROVIS_PULSE_INSIGHT_WAIT_S"] = "0.3"

calls = {"n": 0}
def _slow(_packet):
    calls["n"] += 1
    time.sleep(1.0)
    return {"insight": "18 finished this week, up from 12.", "graphic": "week_finished"}

pulse._call_model = _slow
t0 = time.time()
first = pulse.insight_for(PACKET)
waited = time.time() - t0
check(f"returned inside the wait budget (took {waited:.2f}s)", waited < 0.9)
check("and returned no sentence rather than blocking", first["insight"] == "")
check("but the graphic is still usable", first["graphic"] == "week_finished")

# The worker keeps going and fills the cache — that is the point of not
# cancelling. Give it a moment, then the same packet is instant.
time.sleep(1.2)
t0 = time.time()
second = pulse.insight_for(PACKET)
check(f"the next load is a cache hit ({time.time()-t0:.3f}s)", second.get("cached") is True)
check("and now carries the sentence", second["insight"].startswith("18 finished"))
check("the model was called once, not once per load", calls["n"] == 1)


print("\n--- an invalid generation reaches nobody ---")
pulse._cache.clear()
pulse._inflight.clear()
os.environ["TROVIS_PULSE_INSIGHT_WAIT_S"] = "5"
pulse._call_model = lambda _p: {
    "insight": "Spend rose 30% and Billing Bot is unhealthy.", "graphic": "week_finished",
}
bad = pulse.insight_for(PACKET)
check("the sentence is dropped end to end", bad["insight"] == "")
check("the graphic still comes back", bad["graphic"] == "week_finished")

pulse._cache.clear()
pulse._inflight.clear()
pulse._call_model = lambda _p: {}
check("a non-JSON / empty reply is simply no insight",
      pulse.insight_for(PACKET)["insight"] == "")

pulse._cache.clear()
pulse._inflight.clear()
def _boom(_p):
    raise RuntimeError("model exploded")
pulse._call_model = _boom
try:
    blown = pulse.insight_for(PACKET)
    check("a raising model is not an error for the caller", blown["insight"] == "")
    check("...and the graphic survives it", blown["graphic"] == "week_finished")
except Exception as exc:  # noqa: BLE001
    check(f"a raising model must not propagate (got {exc!r})", False)

print("\n--- no key configured: the pulse still works ---")
pulse._cache.clear()
pulse._inflight.clear()
os.environ.pop("ANTHROPIC_API_KEY", None)
off = pulse.insight_for(PACKET)
check("no API key means no sentence and no error", off["insight"] == "")
check("and the graphic is still chosen from the record", off["graphic"] == "week_finished")

print("\n--- the packet is bounded, not trusted ---")
# It arrives from a browser and is spent on model tokens.
pulse._cache.clear(); pulse._inflight.clear()
os.environ["ANTHROPIC_API_KEY"] = "test-key-not-used"
called = {"n": 0}
def _count(_p):
    called["n"] += 1
    return {"insight": "", "graphic": "none"}
pulse._call_model = _count
huge = {"agents_count": 1, "blob": "x" * 8000}
check("an oversized packet is refused without calling the model",
      pulse.insight_for(huge)["insight"] == "" and called["n"] == 0)
wide = {f"k{i}": i for i in range(50)}
check("a packet with too many keys is refused too",
      pulse.insight_for(wide)["insight"] == "" and called["n"] == 0)
os.environ.pop("ANTHROPIC_API_KEY", None)

print("\n" + (f"FAILURES: {failures}" if failures else "All pulse-insight checks passed."))
raise SystemExit(1 if failures else 0)
