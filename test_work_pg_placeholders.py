"""Postgres %s binding for lean Work home SQL (overview + items).

Railway #122: `_SHELL_TITLE_SQL` inlined LIKE `%` wildcards. SQLite `?`
tests missed it; psycopg2 treats every `%` as a placeholder whenever
params are passed → IndexError: tuple index out of range at
get_work_overview / get_work_items `cur.execute`.

This file binds the same SQL with `%s` + an account_id (no live Postgres).
"""
from __future__ import annotations

import os
import tempfile

os.environ.update({"TROVIS_DISABLE_PRICING_SYNC": "1", "TROVIS_DISABLE_ALERTS": "1",
                   "TROVIS_DISABLE_LOOP_SWEEP": "1", "TROVIS_LOOP_TITLES": "off"})
os.environ.pop("DATABASE_URL", None)
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["TROVIS_DB_PATH"] = _tmp.name

import database

failures = []


def check(label, cond):
    print(("  PASS " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


def _pg_quote(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "NULL"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def _pg_mogrify(query: str, args: tuple) -> str:
    """Bind `%s` the way psycopg2 does. Bare LIKE `%` → IndexError."""
    quoted = [_pg_quote(a) for a in args]

    tmp = query.replace("%%", "\x00")
    out: list[str] = []
    i = 0
    ai = 0
    while i < len(tmp):
        if tmp[i] == "%":
            if i + 1 < len(tmp) and tmp[i + 1] == "s":
                if ai >= len(quoted):
                    raise IndexError("tuple index out of range")
                out.append(quoted[ai])
                ai += 1
                i += 2
                continue
            # bare `%` — psycopg2 still consumes a bind slot (Railway crash)
            if ai >= len(quoted):
                raise IndexError("tuple index out of range")
            out.append(quoted[ai])
            ai += 1
            i += 1
            continue
        out.append(tmp[i])
        i += 1
    if ai != len(quoted):
        raise IndexError("tuple index out of range")
    return "".join(out).replace("\x00", "%")


def _named_title_sql_pg() -> str:
    """`_NAMED_TITLE_SQL` as Railway emits it (`USE_POSTGRES`, PH=`%s`)."""
    if database.USE_POSTGRES:
        return database._NAMED_TITLE_SQL
    return database._NAMED_TITLE_SQL.replace("%", "%%")


def _overview_open_sql(named: str) -> str:
    return (
        "SELECT COUNT(*) AS c FROM loops l "
        f"WHERE l.closed_at IS NULL AND {named} AND l.account_id = %s"
    )


def _overview_completed_sql(named: str) -> str:
    return (
        "SELECT COUNT(*) AS c FROM loops l "
        f"WHERE l.closed_at IS NOT NULL AND l.closed_at >= %s "
        f"AND {named} AND l.account_id = %s"
    )


def _items_sql(named: str) -> str:
    return (
        "SELECT l.id, l.title, l.title_source, l.cached_state, l.last_event_unix, "
        "       l.closed_at, l.created_at, l.service_name, l.agent_id "
        "FROM loops l "
        f"WHERE {named} AND l.account_id = %s "
        "AND (l.closed_at IS NULL OR l.closed_at >= %s) "
        "ORDER BY COALESCE(l.last_event_unix, 0) DESC, l.id DESC "
        "LIMIT %s"
    )


print("\n--- _SHELL_TITLE_SQL construction ---")
_pct = "%%" if database.USE_POSTGRES else "%"
expected_shell = (
    "("
    f"LOWER(l.title) LIKE 'task from {_pct}' "
    f"OR l.title LIKE '{_pct} · {_pct} · {_pct} actions'"
    ")"
)
check("shell SQL matches backend wildcard escape",
      database._SHELL_TITLE_SQL == expected_shell)
check("SQLite tests still use a single LIKE %",
      not database.USE_POSTGRES and "%" in database._SHELL_TITLE_SQL
      and "%%" not in database._SHELL_TITLE_SQL)

print("\n--- unescaped LIKE % (the Railway crash) ---")
bare = (
    "l.title IS NOT NULL AND TRIM(l.title) != '' "
    "AND l.title_source = 'provided' "
    "AND NOT ("
    "LOWER(l.title) LIKE 'task from %' "
    "OR l.title LIKE '% · % · % actions'"
    ")"
)
crashed = False
try:
    _pg_mogrify(_overview_open_sql(bare), (1,))
except IndexError:
    crashed = True
check("overview open SQL with bare % raises IndexError", crashed)

crashed = False
try:
    _pg_mogrify(_items_sql(bare), (1, "2026-01-01 00:00:00", 51))
except IndexError:
    crashed = True
check("items SQL with bare % raises IndexError", crashed)

print("\n--- escaped %% + %s + account_id (prod shape) ---")
named = _named_title_sql_pg()
check("PG named SQL escapes LIKE wildcards as %%",
      "%%" in named and "LIKE 'task from %'" not in named.replace("%%", ""))

ov_open = _pg_mogrify(_overview_open_sql(named), (1,))
ov_done = _pg_mogrify(_overview_completed_sql(named), ("2026-01-01 00:00:00", 1))
items = _pg_mogrify(_items_sql(named), (1, "2026-01-01 00:00:00", 51))
check("overview open SQL binds account_id",
      "account_id = 1" in ov_open and "task from %" in ov_open)
check("overview completed_week SQL binds week + account_id",
      "account_id = 1" in ov_done)
check("items SQL binds account_id + week + limit",
      "account_id = 1" in items and "LIMIT 51" in items)
check("bound SQL keeps a LIKE wildcard, not a leftover %s",
      "LIKE 'task from %'" in ov_open and "%s" not in ov_open)

print("\n--- home snapshot totals (same named-work predicate) ---")
# GET /home/snapshot reuses _work_named_scope_sql, so it inherits the same
# LIKE wildcards and the same way to get them wrong. Bind it the way psycopg2
# would: five timestamp/cutoff binds plus account_id, and not one slot more.
snap_sql, snap_args = database.home_snapshot_totals_sql(1)
snap_pg = snap_sql if database.USE_POSTGRES else snap_sql.replace("%", "%%")
snap_pg = snap_pg.replace("?", "%s")
bound = _pg_mogrify(
    snap_pg,
    (
        "2026-01-01 00:00:00", "2026-01-08 00:00:00",
        "2025-12-25 00:00:00", "2026-01-01 00:00:00",
        "2025-12-25 00:00:00",
        *snap_args,
    ),
)
check("snapshot totals SQL binds every slot and no more",
      "account_id = 1" in bound and "%s" not in bound)
check("snapshot totals SQL keeps a real LIKE wildcard after binding",
      "LIKE 'task from %'" in bound)

crashed = False
try:
    _pg_mogrify(snap_pg.replace("%%", "%"), (
        "2026-01-01 00:00:00", "2026-01-08 00:00:00",
        "2025-12-25 00:00:00", "2026-01-01 00:00:00",
        "2025-12-25 00:00:00", *snap_args))
except IndexError:
    crashed = True
check("snapshot totals SQL with bare % raises IndexError", crashed)

print()
if failures:
    print(f"FAILED ({len(failures)}):")
    for f in failures:
        print("  - " + f)
    raise SystemExit(1)
print("All Postgres placeholder checks passed.")
os.unlink(_tmp.name)
