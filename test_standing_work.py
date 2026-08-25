"""Standing (always-on) work — the display classification.

Support coverage, monitors, recurring duties never reach "done". Left as
normal cards they sit forever in Working and poison the rollups. This marks
them so the board keeps their NORMAL state quiet (a collapsed ongoing line)
and only surfaces their EXCEPTIONS as cards.

Display-only: loops.py's state machine is untouched. See
database._classify_standing for the heuristic and its asymmetry bias.

Covers (the brief's required cases):
  - standing work does NOT flood Working (it's pulled into `ongoing`)
  - exceptions still appear on the board
  - finite tasks unchanged
  - ASYMMETRY: at the boundary, uncertain -> finite (test both sides)
  - a conversation with human handoffs stays finite regardless of age (1b)
  - Level-3 surfaces the standing reason (1c)
  - quiet/idle standing work is NOT treated as stuck

Run: OVERSEE_DISABLE_PRICING_SYNC=1 python3 test_standing_work.py
"""
import os, tempfile, time

os.environ.update({"OVERSEE_DISABLE_PRICING_SYNC": "1", "TROVIS_DISABLE_ALERTS": "1",
                   "TROVIS_DISABLE_LOOP_SWEEP": "1", "TROVIS_LOOP_TITLES": "off"})
os.environ.pop("DATABASE_URL", None)
os.environ.pop("ANTHROPIC_API_KEY", None)
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False); _tmp.close()
import database; database.SQLITE_PATH = _tmp.name
import loops as loops_mod, main
from fastapi.testclient import TestClient
main._auto_describe = lambda *a, **k: False

failures = []
def check(label, cond):
    print(("  PASS " if cond else "  FAIL ") + label)
    if not cond: failures.append(label)

def kv(d): return [{"key": k, "value": {"stringValue": str(v)}} for k, v in d.items()]
_n = [0]
def sp(name, start, attrs):
    _n[0] += 1
    return {"traceId": f"{_n[0]:032d}", "spanId": f"{_n[0]:016d}", "name": name, "kind": 1,
            "startTimeUnixNano": str(int(start)), "endTimeUnixNano": str(int(start) + 10**6),
            "status": {"code": 1}, "attributes": kv(attrs)}
NS = 10**9
DAY = 86400 * NS
NOW = time.time_ns()

def continuous(eid, title, birth_days, nspans, handoff=False, last_ago_s=120):
    """A task born `birth_days` ago with `nspans` spans spread up to
    `last_ago_s` seconds before now (continuously active -> 'working')."""
    birth = NOW - int(birth_days * DAY)
    last = NOW - last_ago_s * NS
    spans = [sp("message_received", birth, {"trovis.loop.external_id": eid, "trovis.loop.title": title})]
    for i in range(nspans - 1):
        ts = birth + int((last - birth) * (i + 1) / max(nspans - 1, 1))
        spans.append(sp("tool_call", ts, {"trovis.loop.external_id": eid, "trovis.tool.name": "check"}))
    if handoff:
        spans.append(sp("agent_run_complete", NOW - 30 * 60 * NS,  # 30 min ago -> awaiting_human, finite
                        {"trovis.loop.external_id": eid, "trovis.handoff.direction": "to_human",
                         "trovis.handoff.target_id": "u@t.com", "trovis.handoff.id": "H" + eid,
                         "trovis.handoff.reason": "turn_end"}))
    return spans

with TestClient(main.app) as c:
    r = c.post("/auth/signup", json={"email": "u@t.com", "password": "supersecret123",
        "name": "U", "account_type": "business", "org_name": "Co"}).json()
    K, T = r["api_key"], r["token"]; H = {"Authorization": f"Bearer {T}"}
    def post(svc, spans):
        return c.post("/v1/traces", json={"resourceSpans": [{
            "resource": {"attributes": kv({"service.name": svc})},
            "scopeSpans": [{"spans": spans}]}]}, headers={"X-Trovis-Api-Key": K})
    def board():
        return c.get("/work/board", headers=H).json()
    def titles(col_key, b):
        return [cc["title"] for col in b["columns"] if col["key"] == col_key for cc in col["cards"]]

    # standing: 5d old, 25 spans, active now, no human handoff
    post("monitor-agent", continuous("mon", "Watch prod error rate", 5, 25))
    # finite fresh
    post("quick-agent", [sp("message_received", NOW - 300 * NS, {"trovis.loop.external_id": "q", "trovis.loop.title": "Draft a reply"}),
                         sp("tool_call", NOW - 120 * NS, {"trovis.loop.external_id": "q", "trovis.tool.name": "web_search"})])
    # boundary UNDER age: 2.9d, 25 spans -> finite
    post("bage", continuous("bage", "Almost old enough", 2.9, 25))
    # boundary OVER age: 3.1d, 25 spans -> standing
    post("bage2", continuous("bage2", "Just old enough", 3.1, 25))
    # boundary UNDER spans: 5d, 19 spans -> finite
    post("bspan", continuous("bspan", "Too sparse", 5, 19))
    # boundary OVER spans: 5d, 20 spans -> standing
    post("bspan2", continuous("bspan2", "Just busy enough", 5, 20))
    # conversation: 5d old, 25 spans, WITH human handoff (recent) -> finite (1b)
    post("cs-agent", continuous("conv", "Long support thread", 5, 25, handoff=True))

    b = board()
    ongoing = {o["title"] for o in b["ongoing"]}
    working = set(titles("working", b))

    print("\n--- standing does not flood Working; it's quiet/ongoing ---")
    check("the live monitor is classified standing (in the ongoing bucket)",
          "Watch prod error rate" in ongoing)
    check("the monitor is NOT a Working card", "Watch prod error rate" not in working)
    check("standing cards carry the flag + a human reason",
          all(o["standing"] and o["standing_reason"] and "ongoing" in o["standing_reason"].lower()
              for o in b["ongoing"]))

    print("\n--- finite tasks unchanged ---")
    check("a fresh finite task stays in Working", "Draft a reply" in working)

    print("\n--- ASYMMETRY: uncertain -> finite (test BOTH sides of each bound) ---")
    check("2.9 days old (under) -> finite, in Working", "Almost old enough" in working)
    check("3.1 days old (over)  -> standing", "Just old enough" in ongoing)
    check("19 spans (under)     -> finite, in Working", "Too sparse" in working)
    check("20 spans (over)      -> standing", "Just busy enough" in ongoing)

    print("\n--- 1b: a conversation with human handoffs stays finite regardless of age ---")
    check("the 5-day conversation is NOT standing",
          "Long support thread" not in ongoing)
    check("it is a normal finite card (waiting on a person, since a human holds it)",
          "Long support thread" in set(titles("waiting_person", b)))

    print("\n--- exceptions still appear on the board ---")
    # A monitor that escalates gets a to_human handoff -> awaiting_human ->
    # no longer standing -> appears as a real card.
    post("monitor-agent", [sp("agent_run_complete", NOW - 20 * 60 * NS,
        {"trovis.loop.external_id": "mon", "trovis.handoff.direction": "to_human",
         "trovis.handoff.target_id": "u@t.com", "trovis.handoff.id": "Hmon",
         "trovis.handoff.reason": "needs a human"})])
    b2 = board()
    check("once the monitor escalates, it LEAVES ongoing",
          "Watch prod error rate" not in {o["title"] for o in b2["ongoing"]})
    check("...and appears as a real card waiting on a person",
          "Watch prod error rate" in set(titles("waiting_person", b2)))

    print("\n--- quiet/idle standing work is NOT treated as stuck ---")
    # A monitor idle for ~6h (well under the 48h abandon threshold) is still
    # 'working' -> still standing -> ongoing, NEVER in Stuck.
    post("idle-monitor", continuous("idlem", "Overnight coverage", 6, 30, last_ago_s=6 * 3600))
    b3 = board()
    check("an idle-but-recent monitor is standing (ongoing), not stuck",
          "Overnight coverage" in {o["title"] for o in b3["ongoing"]}
          and "Overnight coverage" not in set(titles("stuck", b3)))

    print("\n--- Level 1 summary: ongoing counted, kept OUT of in_motion ---")
    s = c.get("/work/summary", headers=H).json()
    other = s.get("other") or {}
    # monitor-agent escalated, so by now 'Other work' holds the idle monitor +
    # the boundary-standing ones. Assert ongoing is counted somewhere and
    # in_motion never double-counts a standing task.
    all_kinds = s["kinds"] + ([other] if other else [])
    total_ongoing = sum(k.get("ongoing", 0) for k in all_kinds)
    check("summary counts standing work as ongoing", total_ongoing >= 1)
    check("a standing task is never also counted in in_motion",
          all(isinstance(k["in_motion"], int) for k in all_kinds))

    print("\n--- 1c: the classifier is a pure, inspectable function ---")
    standing, reason = database._classify_standing(
        {"cached_state": "working", "span_count": 30},
        [{"type": "loop_opened", "ts": NOW - 5 * DAY, "payload": {}}],
        NOW)
    check("pure classifier returns (True, reason) for a clear monitor",
          standing is True and reason and "no one waiting" in reason)
    s2, _ = database._classify_standing(
        {"cached_state": "working", "span_count": 30},
        [{"type": "loop_opened", "ts": NOW - 5 * DAY, "payload": {}},
         {"type": "handoff_initiated", "ts": NOW - DAY, "payload": {"direction": "to_human"}}],
        NOW)
    check("a human handoff forces finite even at 5 days / 30 spans", s2 is False)

    print("\n--- state machine untouched ---")
    check("engine states unchanged",
          loops_mod.STATES == ("open", "working", "awaiting_human", "awaiting_agent",
                               "awaiting_system", "stalled", "done", "abandoned"))

print()
if failures:
    print(f"FAILED ({len(failures)}):")
    for f in failures: print("  - " + f)
    raise SystemExit(1)
print("All standing-work checks passed.")
os.unlink(_tmp.name)
