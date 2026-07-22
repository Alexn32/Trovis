"""Immutability guard for the workloop event record.

Static (grep-level) source check: spans and loop_events are APPEND-ONLY.
The workloop feature added ZERO update/delete paths against either table —
the only `UPDATE spans` statements are the two pre-existing cost writers
(pricing recompute + cross-batch reported-cost covering), pinned here by
exact count so new code can't silently add another. All loop_events INSERTs
flow through the single vocabulary-enforcing writer (append_loop_event).

Run:
  python3 test_loop_immutability.py
"""
import re

failures = []
def check(label, cond):
    print(("  PASS " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


SOURCES = ["database.py", "main.py", "loops.py", "models.py", "alerts.py", "mcp_server.py"]
text = {}
for path in SOURCES:
    with open(path, encoding="utf-8") as f:
        text[path] = f.read()


def count(path, needle):
    return len(re.findall(re.escape(needle), text[path], flags=re.IGNORECASE))


# loop_events: append-only, no exceptions.
for path in SOURCES:
    check(f"{path}: no UPDATE loop_events", count(path, "UPDATE loop_events") == 0)
    check(f"{path}: no DELETE FROM loop_events", count(path, "DELETE FROM loop_events") == 0)

# spans: never deleted anywhere.
for path in SOURCES:
    check(f"{path}: no DELETE FROM spans", count(path, "DELETE FROM spans") == 0)

# spans: exactly the two PRE-EXISTING cost writers in database.py
# (recompute_span_costs re-pricing + cross-batch 'covered' zeroing), and
# none anywhere else. If this fails at >2, someone added a spans UPDATE —
# that breaks the append-only contract; find another way.
check("database.py: UPDATE spans count pinned at 2 (both pre-existing cost writers)",
      count("database.py", "UPDATE spans") == 2)
for path in [p for p in SOURCES if p != "database.py"]:
    check(f"{path}: no UPDATE spans", count(path, "UPDATE spans") == 0)

# Single write path into loop_events: append_loop_event only.
check("database.py: exactly one INSERT INTO loop_events (append_loop_event)",
      count("database.py", "INSERT INTO loop_events") == 1)
for path in [p for p in SOURCES if p != "database.py"]:
    check(f"{path}: no direct INSERT INTO loop_events", count(path, "INSERT INTO loop_events") == 0)

# loops (the read model) is never deleted either — closes are events.
for path in SOURCES:
    check(f"{path}: no DELETE FROM loops", count(path, "DELETE FROM loops") == 0)

# --- Versioned workflows -----------------------------------------------------
# workflow_versions: APPEND-ONLY, no exceptions — every edit is a new row.
for path in SOURCES:
    check(f"{path}: no UPDATE workflow_versions", count(path, "UPDATE workflow_versions") == 0)
    check(f"{path}: no DELETE FROM workflow_versions", count(path, "DELETE FROM workflow_versions") == 0)

# workflows allows EXACTLY TWO mutations, both in database.py:
#   1. the current_version bump in create_workflow_version
#   2. the archived_at set in archive_workflow (archive, never delete)
check("database.py: UPDATE workflows pinned at 2 (version bump + archive)",
      count("database.py", "UPDATE workflows") == 2)
for path in [p for p in SOURCES if p != "database.py"]:
    check(f"{path}: no UPDATE workflows", count(path, "UPDATE workflows") == 0)

# Exactly ONE DELETE FROM workflows survives: the agent hard-delete sweep of
# LEGACY graph rows in delete_agent, guarded by
# `id NOT IN (SELECT workflow_id FROM workflow_versions)` — it can never
# touch a versioned declaration. Anything beyond that count is a new delete
# path and breaks the archive-never-delete contract.
check("database.py: DELETE FROM workflows pinned at 1 (legacy-scoped agent sweep)",
      count("database.py", "DELETE FROM workflows") == 1)
check("database.py: the legacy sweep is version-guarded",
      "NOT IN (SELECT workflow_id FROM workflow_versions)" in text["database.py"])
for path in [p for p in SOURCES if p != "database.py"]:
    check(f"{path}: no DELETE FROM workflows", count(path, "DELETE FROM workflows") == 0)

# Two writers only into workflow_versions: create_workflow (v1) and
# create_workflow_version (v2+).
check("database.py: exactly two INSERT INTO workflow_versions",
      count("database.py", "INSERT INTO workflow_versions") == 2)

print()
if failures:
    print(f"FAILED: {len(failures)} check(s):")
    for f in failures:
        print("  - " + f)
    raise SystemExit(1)
print("ALL CHECKS PASSED")
