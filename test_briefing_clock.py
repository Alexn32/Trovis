"""The briefing runs on the READER's clock, not the server's.

Two failures this pins:

  1. `generated_at` came off a TIMESTAMP column as UTC with nothing saying so
     ("2026-09-09 21:47:00"), and Home fed that to Date.parse — which reads an
     offset-less date-time as LOCAL. A 21:47 UTC stamp printed as "9:47 PM" to
     a reader in Chicago who should have seen 4:47 PM. It was also absent
     entirely on a fresh generation, so the footer silently vanished.
  2. Nothing told the model what time it was where the reader sits, so an
     opener written at 5pm local could call it a quiet morning. The browser
     now sends its hour and zone, and a summary cached in one part of their
     day is not served into the next.

Run:
  OVERSEE_DISABLE_PRICING_SYNC=1 python3 test_briefing_clock.py
"""
import os
import re
import tempfile
from datetime import datetime, timezone

os.environ["OVERSEE_DISABLE_PRICING_SYNC"] = "1"
os.environ["TROVIS_DISABLE_ALERTS"] = "1"
os.environ["TROVIS_DISABLE_LOOP_SWEEP"] = "1"
os.environ["TROVIS_LOOP_TITLES"] = "off"
os.environ.pop("DATABASE_URL", None)
os.environ.pop("ANTHROPIC_API_KEY", None)

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()

import database
database.SQLITE_PATH = _tmp.name

import describer
import main
from fastapi.testclient import TestClient

main._auto_describe = lambda *a, **k: False

failures = []
def check(label, cond):
    print(("  PASS " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


ZONED = re.compile(r"(?:Z|[+-]\d{2}:?\d{2})$")


print("-- part of day, on the reader's hour --")
# The same buckets and cutoffs as Home's greeting (Dashboard.jsx Greeting:
# <12 morning, <18 afternoon, else evening). A briefing that opens "this
# evening" under a "Good afternoon" heading is the page arguing with itself.
for hour, part in [
    (0, "morning"), (11, "morning"),
    (12, "afternoon"), (17, "afternoon"),
    (18, "evening"), (23, "evening"),
]:
    check(f"{hour:02d}:00 → {part}", main._part_of_day(hour) == part)

print("\n-- the clock the prompt receives --")
check("no hour sent → no clock at all (say nothing about the time)",
      main._viewer_clock(None, "America/Chicago") is None)
clock = main._viewer_clock(16, "America/Chicago")
check("hour + zone ride together",
      clock == {"hour_24": 16, "part_of_day": "afternoon",
                "timezone": "America/Chicago"})
check("midnight is a real hour, not a falsy one",
      main._viewer_clock(0, None) == {"hour_24": 0, "part_of_day": "morning"})
check("an out-of-range hour is dropped rather than trusted",
      main._viewer_clock(99, None) is None)

print("\n-- generated_at leaves the server saying which zone it is in --")
check("SQLite's naive UTC string gets a Z",
      main._utc_iso("2026-09-09 21:47:00") == "2026-09-09T21:47:00Z")
check("a naive ISO string gets a Z",
      main._utc_iso("2026-09-09T21:47:00") == "2026-09-09T21:47:00Z")
check("an already-zoned string is left alone",
      main._utc_iso("2026-09-09T21:47:00Z") == "2026-09-09T21:47:00Z"
      and main._utc_iso("2026-09-09T16:47:00-05:00") == "2026-09-09T16:47:00-05:00")
check("a naive datetime is read as UTC",
      main._utc_iso(datetime(2026, 9, 9, 21, 47)) == "2026-09-09T21:47:00+00:00")
check("an aware datetime keeps its instant",
      main._utc_iso(
          datetime(2026, 9, 9, 21, 47, tzinfo=timezone.utc)
      ) == "2026-09-09T21:47:00+00:00")
check("nothing in, nothing out", main._utc_iso(None) is None)

print("\n-- the prompt is told whose clock it is --")
check("viewer_clock is named in the briefing system prompt",
      "viewer_clock" in describer.DASHBOARD_BRIEFING_SYSTEM_PROMPT)
check("and it is told not to contradict it",
      "part_of_day" in describer.DASHBOARD_BRIEFING_SYSTEM_PROMPT)
check("the prompt no longer assumes the start of the day",
      "at the start of the day" not in describer.DASHBOARD_BRIEFING_SYSTEM_PROMPT)

seen = []


def fake_briefing(stats):
    seen.append(stats)
    part = (stats.get("viewer_clock") or {}).get("part_of_day", "unknown")
    return {"summary": f"Written for the {part}."}


describer.fleet_briefing = fake_briefing

with TestClient(main.app) as c:
    r = c.post("/auth/signup", json={
        "email": "clock@test.com", "password": "supersecret123",
        "name": "C", "account_type": "individual", "org_name": "C Co",
    }).json()
    H = {"Authorization": f"Bearer {r['token']}"}

    print("\n-- the browser's clock reaches the model --")
    seen.clear()
    first = c.get(
        "/dashboard/briefing?local_hour=16&tz=America/Chicago", headers=H
    ).json()
    check("the request generated a summary", first["summary"] == "Written for the afternoon.")
    check("the model saw the reader's hour, part of day and zone",
          seen and seen[-1].get("viewer_clock") == {
              "hour_24": 16, "part_of_day": "afternoon",
              "timezone": "America/Chicago",
          })

    print("\n-- every response carries a zoned generated_at --")
    check("fresh generation stamps a time (the footer used to disappear)",
          bool(first.get("generated_at")))
    check("and it says which zone it is in",
          bool(ZONED.search(first["generated_at"] or "")))

    print("\n-- a cached summary is reused within the same part of day --")
    seen.clear()
    same = c.get(
        "/dashboard/briefing?local_hour=15&tz=America/Chicago", headers=H
    ).json()
    check("no second model call inside the afternoon", seen == [])
    check("the cached prose is served", same["summary"] == first["summary"])
    check("the cache-hit branch stamps a zoned time too",
          bool(same.get("generated_at")) and bool(ZONED.search(same["generated_at"])))

    print("\n-- but never served into a different part of their day --")
    seen.clear()
    evening = c.get(
        "/dashboard/briefing?local_hour=19&tz=America/Chicago", headers=H
    ).json()
    check("the model is asked again once the reader has moved on", len(seen) == 1)
    check("and the prose matches the hour they are actually reading it",
          evening["summary"] == "Written for the evening.")

    print("\n-- a caller with no clock behaves exactly as before --")
    seen.clear()
    plain = c.get("/dashboard/briefing", headers=H).json()
    check("the cached summary is served rather than regenerated", seen == [])
    check("still a full response", bool(plain["summary"]))
    # And when it does generate without a clock, the prompt gets no clock key
    # rather than a guessed one.
    database.save_insight(
        account_id=database.get_user_by_email("clock@test.com")["account_id"],
        service_name=main._DASHBOARD_SENTINEL, agent_id="main",
        kind="briefing", data={"summary": ""},
    )
    seen.clear()
    noclock = c.get("/dashboard/briefing", headers=H).json()
    check("generated with no viewer_clock in the stats",
          len(seen) == 1 and "viewer_clock" not in seen[-1])
    check("the model was told nothing about the time of day",
          noclock["summary"] == "Written for the unknown.")

print()
if failures:
    print(f"FAILED ({len(failures)}):")
    for f in failures:
        print("  - " + f)
    raise SystemExit(1)
print("All briefing-clock checks passed.")
