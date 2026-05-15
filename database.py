"""Storage layer for Oversee — Postgres in production, SQLite for local dev.

The backend is chosen at module load by the DATABASE_URL env var:
  DATABASE_URL set    → Postgres via psycopg2 (production / Railway)
  DATABASE_URL unset  → SQLite at ./oversee.db (local development)

Callers don't need to know which backend is active: schema, query semantics,
and return shapes are identical. SQL placeholders are `?` for SQLite and
`%s` for psycopg2; we substitute the right one via the module-level `PH`
constant (a literal — no SQL-injection surface).
"""

from __future__ import annotations

import json
import os
import secrets
from contextlib import contextmanager
from typing import Any, Iterator

# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------

_DATABASE_URL = os.environ.get("DATABASE_URL")
USE_POSTGRES = bool(_DATABASE_URL)

if USE_POSTGRES:
    import psycopg2  # noqa: F401  (used indirectly via pool / cursor)
    from psycopg2.extras import RealDictCursor, execute_values
    from psycopg2.pool import ThreadedConnectionPool

    _pool: ThreadedConnectionPool | None = None
else:
    import sqlite3

    SQLITE_PATH = "oversee.db"

# Bind-parameter placeholder for the active backend. Used in SQL strings via
# f-string substitution. PH is a module constant, not user input.
PH = "%s" if USE_POSTGRES else "?"


# ---------------------------------------------------------------------------
# Connection management
# ---------------------------------------------------------------------------


@contextmanager
def _connect() -> Iterator[Any]:
    """Yield a connection. Commits on success, rolls back on exception."""
    if USE_POSTGRES:
        if _pool is None:
            raise RuntimeError(
                "Postgres pool not initialized — call init_db() at startup",
            )
        conn = _pool.getconn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            _pool.putconn(conn)
    else:
        conn = sqlite3.connect(SQLITE_PATH)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


@contextmanager
def _cursor(conn) -> Iterator[Any]:
    """Yield a cursor that returns dict-like rows on either backend."""
    if USE_POSTGRES:
        cur = conn.cursor(cursor_factory=RealDictCursor)
    else:
        # sqlite3.Row already supports both indexed and string access.
        cur = conn.cursor()
    try:
        yield cur
    finally:
        cur.close()


def _ts_to_str(v: Any) -> str | None:
    """Normalize a TIMESTAMP value to an ISO string.

    SQLite returns timestamps as text already; psycopg2 returns datetime
    objects. The API response model expects a string either way.
    """
    if v is None:
        return None
    if isinstance(v, str):
        return v
    return v.isoformat()


def _ns_to_iso(ns: int | None) -> str | None:
    """Convert a nanosecond unix timestamp to an ISO-8601 string (UTC)."""
    if not ns:
        return None
    from datetime import datetime, timezone

    return datetime.fromtimestamp(int(ns) / 1_000_000_000, tz=timezone.utc).isoformat()


def _detect_platform(resource_attrs_json: str | None) -> str | None:
    """Infer a human-readable platform label from a span's resource
    attributes. Returns None when no identifying signal is present — we'd
    rather show no label than invent one. Detection order matters:
    OpenClaw is a specific platform built ON the Oversee plugin, so it
    wins over the generic plugin signal.
    """
    if not resource_attrs_json:
        return None
    try:
        attrs = json.loads(resource_attrs_json)
    except (TypeError, ValueError):
        return None
    if not isinstance(attrs, dict):
        return None

    if "openclaw.gateway.version" in attrs:
        return "OpenClaw Agent"
    if "oversee.plugin.version" in attrs:
        return "Oversee-instrumented Agent"

    lang = attrs.get("telemetry.sdk.language")
    if isinstance(lang, str) and lang:
        # Title-case so "python" → "Python Agent", "nodejs" → "Nodejs
        # Agent". Special-case nodejs to its more recognizable form.
        if lang.lower() == "nodejs":
            return "Node.js Agent"
        return f"{lang.title()} Agent"

    return None


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
#
# Two dialect-specific DDLs. Differences:
#   - id:          INTEGER PRIMARY KEY AUTOINCREMENT  vs  SERIAL PRIMARY KEY
#   - timestamps:  start_time_unix / end_time_unix are nanosecond counts.
#                  SQLite's INTEGER is dynamically sized (up to 8 bytes);
#                  Postgres INTEGER is 4 bytes and overflows. Must use BIGINT.
#   - default ts:  CURRENT_TIMESTAMP  vs  NOW()


_SPANS_DDL_PG = """
CREATE TABLE IF NOT EXISTS spans (
    id                  SERIAL PRIMARY KEY,
    trace_id            TEXT      NOT NULL,
    span_id             TEXT      NOT NULL,
    parent_span_id      TEXT,
    service_name        TEXT      NOT NULL,
    agent_id            TEXT      DEFAULT 'main',
    span_name           TEXT      NOT NULL,
    kind                INTEGER   DEFAULT 0,
    start_time_unix     BIGINT    NOT NULL,
    end_time_unix       BIGINT    NOT NULL,
    status_code         INTEGER   DEFAULT 0,
    status_message      TEXT      DEFAULT '',
    attributes          TEXT      DEFAULT '{}',
    resource_attributes TEXT      DEFAULT '{}',
    created_at          TIMESTAMP DEFAULT NOW()
)
"""

_DESC_DDL_PG = """
CREATE TABLE IF NOT EXISTS descriptions (
    id                  SERIAL PRIMARY KEY,
    service_name        TEXT      NOT NULL,
    agent_id            TEXT      DEFAULT 'main',
    description         TEXT      NOT NULL,
    span_count_analyzed INTEGER,
    generated_at        TIMESTAMP DEFAULT NOW()
)
"""

_SPANS_DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS spans (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id            TEXT    NOT NULL,
    span_id             TEXT    NOT NULL,
    parent_span_id      TEXT,
    service_name        TEXT    NOT NULL,
    agent_id            TEXT    DEFAULT 'main',
    span_name           TEXT    NOT NULL,
    kind                INTEGER DEFAULT 0,
    start_time_unix     INTEGER NOT NULL,
    end_time_unix       INTEGER NOT NULL,
    status_code         INTEGER DEFAULT 0,
    status_message      TEXT    DEFAULT '',
    attributes          TEXT    DEFAULT '{}',
    resource_attributes TEXT    DEFAULT '{}',
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

_DESC_DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS descriptions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    service_name        TEXT    NOT NULL,
    agent_id            TEXT    DEFAULT 'main',
    description         TEXT    NOT NULL,
    span_count_analyzed INTEGER,
    generated_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

_REG_DDL_PG = """
CREATE TABLE IF NOT EXISTS agent_registrations (
    id               SERIAL PRIMARY KEY,
    service_name     TEXT      NOT NULL,
    agent_id         TEXT      DEFAULT 'main',
    soul             TEXT      DEFAULT '',
    identity         TEXT      DEFAULT '',
    operating_manual TEXT      DEFAULT '',
    user_context     TEXT      DEFAULT '',
    memory           TEXT      DEFAULT '',
    workspace_path   TEXT      DEFAULT '',
    model            TEXT      DEFAULT '',
    created_at       TIMESTAMP DEFAULT NOW()
)
"""

_REG_DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS agent_registrations (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    service_name     TEXT    NOT NULL,
    agent_id         TEXT    DEFAULT 'main',
    soul             TEXT    DEFAULT '',
    identity         TEXT    DEFAULT '',
    operating_manual TEXT    DEFAULT '',
    user_context     TEXT    DEFAULT '',
    memory           TEXT    DEFAULT '',
    workspace_path   TEXT    DEFAULT '',
    model            TEXT    DEFAULT '',
    created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

_ACCOUNTS_DDL_PG = """
CREATE TABLE IF NOT EXISTS accounts (
    id         SERIAL    PRIMARY KEY,
    email      TEXT      NOT NULL UNIQUE,
    created_at TIMESTAMP DEFAULT NOW()
)
"""

_ACCOUNTS_DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS accounts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    email      TEXT    NOT NULL UNIQUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

_API_KEYS_DDL_PG = """
CREATE TABLE IF NOT EXISTS api_keys (
    id         SERIAL    PRIMARY KEY,
    account_id INTEGER   NOT NULL REFERENCES accounts(id),
    key        TEXT      NOT NULL UNIQUE,
    name       TEXT      DEFAULT 'default',
    active     BOOLEAN   DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT NOW()
)
"""

_API_KEYS_DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS api_keys (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    key        TEXT    NOT NULL UNIQUE,
    name       TEXT    DEFAULT 'default',
    active     INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

# Human team members an operator manages. Per-account scoping; email
# is optional but UNIQUE within an account when set (NULLs allowed as
# duplicates per standard SQL semantics on both PG and SQLite).
_TEAM_DDL_PG = """
CREATE TABLE IF NOT EXISTS team_members (
    id         SERIAL    PRIMARY KEY,
    account_id INTEGER   NOT NULL,
    name       TEXT      NOT NULL,
    email      TEXT,
    role       TEXT,
    created_at TIMESTAMP DEFAULT NOW(),
    UNIQUE (account_id, email)
)
"""

_TEAM_DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS team_members (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    name       TEXT    NOT NULL,
    email      TEXT,
    role       TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (account_id, email)
)
"""

# Assignment of a sub-agent to a human owner. UNIQUE on the triple so
# each (account, service, agent) has at most one owner. Re-assigning
# is an UPSERT.
_OWNERS_DDL_PG = """
CREATE TABLE IF NOT EXISTS agent_owners (
    id              SERIAL  PRIMARY KEY,
    account_id      INTEGER NOT NULL,
    service_name    TEXT    NOT NULL,
    agent_id        TEXT    DEFAULT 'main',
    team_member_id  INTEGER REFERENCES team_members(id),
    UNIQUE (account_id, service_name, agent_id)
)
"""

_OWNERS_DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS agent_owners (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id      INTEGER NOT NULL,
    service_name    TEXT    NOT NULL,
    agent_id        TEXT    DEFAULT 'main',
    team_member_id  INTEGER REFERENCES team_members(id),
    UNIQUE (account_id, service_name, agent_id)
)
"""

# Operator-editable display name for an agent — keyed by the triple
# (account, service_name, agent_id) so each sub-agent gets its own.
# UPSERT on the unique triple to keep it a single row per agent.
_DISPLAY_DDL_PG = """
CREATE TABLE IF NOT EXISTS agent_display_names (
    id           SERIAL    PRIMARY KEY,
    account_id   INTEGER,
    service_name TEXT      NOT NULL,
    agent_id     TEXT      NOT NULL DEFAULT 'main',
    display_name TEXT      NOT NULL,
    updated_at   TIMESTAMP DEFAULT NOW(),
    UNIQUE (account_id, service_name, agent_id)
)
"""

_DISPLAY_DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS agent_display_names (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id   INTEGER,
    service_name TEXT    NOT NULL,
    agent_id     TEXT    NOT NULL DEFAULT 'main',
    display_name TEXT    NOT NULL,
    updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (account_id, service_name, agent_id)
)
"""

# Tables that gained account_id post-launch. The column is nullable so
# pre-multi-tenant rows (with NULL account_id) survive — but they're
# strictly filtered out for authenticated requests, since they have no
# owner.
_ACCOUNT_ID_TABLES = ("spans", "descriptions", "agent_registrations")

_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_spans_service_name ON spans(service_name)",
    "CREATE INDEX IF NOT EXISTS idx_spans_start_time ON spans(start_time_unix)",
    "CREATE INDEX IF NOT EXISTS idx_spans_service_agent ON spans(service_name, agent_id)",
    "CREATE INDEX IF NOT EXISTS idx_descriptions_service_name ON descriptions(service_name)",
    "CREATE INDEX IF NOT EXISTS idx_descriptions_service_agent ON descriptions(service_name, agent_id)",
    "CREATE INDEX IF NOT EXISTS idx_registrations_service_name ON agent_registrations(service_name)",
    "CREATE INDEX IF NOT EXISTS idx_api_keys_key ON api_keys(key)",
    "CREATE INDEX IF NOT EXISTS idx_api_keys_account_id ON api_keys(account_id)",
    "CREATE INDEX IF NOT EXISTS idx_spans_account_id ON spans(account_id)",
    "CREATE INDEX IF NOT EXISTS idx_descriptions_account_id ON descriptions(account_id)",
    "CREATE INDEX IF NOT EXISTS idx_registrations_account_id ON agent_registrations(account_id)",
    "CREATE INDEX IF NOT EXISTS idx_display_names_service_agent ON agent_display_names(service_name, agent_id)",
    "CREATE INDEX IF NOT EXISTS idx_display_names_account_id ON agent_display_names(account_id)",
    "CREATE INDEX IF NOT EXISTS idx_team_members_account_id ON team_members(account_id)",
    "CREATE INDEX IF NOT EXISTS idx_agent_owners_service_agent ON agent_owners(service_name, agent_id)",
    "CREATE INDEX IF NOT EXISTS idx_agent_owners_account_id ON agent_owners(account_id)",
    "CREATE INDEX IF NOT EXISTS idx_agent_owners_team_member ON agent_owners(team_member_id)",
]


def init_db() -> None:
    """Create tables, run column migrations, and create indexes. Also
    initializes the Postgres connection pool when applicable."""
    global _pool
    if USE_POSTGRES:
        _pool = ThreadedConnectionPool(
            minconn=2, maxconn=10, dsn=_DATABASE_URL,
        )
        ddls = [
            _SPANS_DDL_PG,
            _DESC_DDL_PG,
            _REG_DDL_PG,
            _ACCOUNTS_DDL_PG,
            _API_KEYS_DDL_PG,
            _DISPLAY_DDL_PG,
            # team_members must come before agent_owners — the FK
            # references it. Order matters on first init only.
            _TEAM_DDL_PG,
            _OWNERS_DDL_PG,
        ]
    else:
        ddls = [
            _SPANS_DDL_SQLITE,
            _DESC_DDL_SQLITE,
            _REG_DDL_SQLITE,
            _ACCOUNTS_DDL_SQLITE,
            _API_KEYS_DDL_SQLITE,
            _DISPLAY_DDL_SQLITE,
            _TEAM_DDL_SQLITE,
            _OWNERS_DDL_SQLITE,
        ]

    with _connect() as conn, _cursor(conn) as cur:
        for ddl in ddls:
            cur.execute(ddl)
        # Backfill the account_id column on existing tables — idempotent.
        for table in _ACCOUNT_ID_TABLES:
            _try_add_column(cur, table, "account_id", "INTEGER")
        # Multi-agent column. Older spans rows pre-date per-agent telemetry;
        # the DEFAULT applies to new rows, and on both backends a SELECT of
        # the now-NULL backfill returns 'main' (PG rewrites with the default,
        # SQLite returns the column default for missing rows). We also
        # COALESCE on reads to be defensive.
        _try_add_column(cur, "spans", "agent_id", "TEXT DEFAULT 'main'")
        # Same backfill for descriptions — pre-multi-agent rows were
        # generated from main's SOUL.md, so 'main' is the correct tag.
        _try_add_column(cur, "descriptions", "agent_id", "TEXT DEFAULT 'main'")
        for idx in _INDEXES:
            cur.execute(idx)


def _try_add_column(cur, table: str, column: str, type_decl: str) -> None:
    """Add a column if it doesn't already exist. Idempotent on both backends.

    Postgres has native `ADD COLUMN IF NOT EXISTS`; SQLite doesn't, so we
    catch the duplicate-column error there.
    """
    if USE_POSTGRES:
        cur.execute(
            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {type_decl}"
        )
    else:
        try:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {type_decl}")
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise


def shutdown_db() -> None:
    """Close all pooled connections. No-op for SQLite."""
    global _pool
    if USE_POSTGRES and _pool is not None:
        _pool.closeall()
        _pool = None


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


_INSERT_COLUMNS = (
    "trace_id, span_id, parent_span_id, service_name, agent_id, span_name, kind, "
    "start_time_unix, end_time_unix, status_code, status_message, "
    "attributes, resource_attributes, account_id"
)


def _agent_id_from_attrs(attrs: dict[str, Any] | None) -> str:
    """Extract the per-event agent id from a parsed span's attributes.

    The OpenClaw plugin stamps this on every hook span as `oversee.agent.id`.
    Other OTEL SDKs don't set it; those agents are single-instance, so we
    default to 'main' to keep them grouped as one entry per service.
    """
    if not attrs:
        return "main"
    val = attrs.get("oversee.agent.id")
    if isinstance(val, str) and val:
        return val
    return "main"


def insert_spans(
    spans: list[dict[str, Any]], account_id: int | None = None,
) -> int:
    """Bulk-insert parsed spans. Returns the row count. Tags each row with
    account_id when provided (None preserves the pre-multi-tenant behavior)."""
    if not spans:
        return 0

    rows = [
        (
            s["trace_id"],
            s["span_id"],
            s.get("parent_span_id") or None,
            s["service_name"],
            _agent_id_from_attrs(s.get("attributes")),
            s["span_name"],
            s.get("kind", 0),
            s["start_time_unix"],
            s["end_time_unix"],
            s.get("status_code", 0),
            s.get("status_message", "") or "",
            json.dumps(s.get("attributes", {})),
            json.dumps(s.get("resource_attributes", {})),
            account_id,
        )
        for s in spans
    ]

    with _connect() as conn, _cursor(conn) as cur:
        if USE_POSTGRES:
            execute_values(
                cur,
                f"INSERT INTO spans ({_INSERT_COLUMNS}) VALUES %s",
                rows,
            )
        else:
            placeholders = ", ".join(["?"] * 14)
            cur.executemany(
                f"INSERT INTO spans ({_INSERT_COLUMNS}) VALUES ({placeholders})",
                rows,
            )
    return len(rows)


def save_description(
    service_name: str,
    description: str,
    span_count_analyzed: int,
    account_id: int | None = None,
    agent_id: str | None = None,
) -> None:
    """Persist a newly generated description (append-only — history kept).

    `agent_id` scopes the description to one sub-agent within an
    instance. Passing None defaults to 'main' — pre-multi-agent
    descriptions were always for the lone 'main' agent, so this keeps
    backwards-compat for callers that don't pass agent_id.
    """
    sql = f"""
        INSERT INTO descriptions (service_name, agent_id, description, span_count_analyzed, account_id)
        VALUES ({PH}, {PH}, {PH}, {PH}, {PH})
    """
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(
            sql,
            (
                service_name,
                agent_id or "main",
                description,
                span_count_analyzed,
                account_id,
            ),
        )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def get_agents(account_id: int | None = None) -> list[dict[str, Any]]:
    """Return the fleet as instance groups, each with a nested list of agents.

    Spans are grouped by `(service_name, agent_id)` first to compute per-agent
    stats, then folded into one record per `service_name`. A single-agent
    instance still gets a one-element `agents` list — the frontend collapses
    those visually.

    When account_id is provided, results are strictly scoped to that account
    (pre-multi-tenant rows with NULL account_id are excluded). When None,
    returns ALL rows (local-dev / pre-auth behavior).
    """
    span_filter = f"WHERE account_id = {PH}" if account_id is not None else ""
    desc_filter = (
        f"AND d.account_id = {PH}" if account_id is not None else ""
    )
    reg_filter = (
        f"AND r.account_id = {PH}" if account_id is not None else ""
    )
    dn_filter = (
        f"AND dn.account_id = {PH}" if account_id is not None else ""
    )
    own_filter = (
        f"AND o.account_id = {PH}" if account_id is not None else ""
    )
    sample_acct_filter = (
        f"AND s2.account_id = {PH}" if account_id is not None else ""
    )

    # Per (service_name, agent_id) aggregation — every row is one bubble in
    # the nested `agents[]` list. The description and sample_resource lookup
    # only need to fire once per service_name, but it's cheaper to repeat the
    # subquery than to issue a separate round-trip per group; both are 1-row
    # lookups with the right indexes.
    #
    # Note on GROUP BY: we group by the bare `agent_id` column (not
    # `COALESCE(agent_id, 'main')`). Postgres is strict about subqueries
    # referencing outer-query columns that aren't either in GROUP BY or
    # aggregated, and the `has_registration` EXISTS subquery below
    # correlates on `spans.agent_id`. SQLite tolerates the COALESCE-only
    # grouping, but Postgres returns "column must appear in GROUP BY".
    # In practice every row has `agent_id = 'main'` or an explicit value
    # (the ADD COLUMN default backfills, and inserts always tag a value),
    # so the two forms produce the same groups — but only the bare-column
    # form is portable. COALESCE is moved into the SELECT projection.
    # Both the description and the display_name subqueries correlate on
    # `spans.agent_id` (so each sub-agent gets its own value). Postgres
    # requires every column referenced in a subquery to be in the outer
    # GROUP BY or aggregated — `agent_id` IS in the GROUP BY, so the
    # `COALESCE(spans.agent_id, 'main')` reads are legal there.
    agg_sql = f"""
        SELECT
            service_name,
            COALESCE(agent_id, 'main')                       AS agent_id,
            COUNT(*)                                         AS span_count,
            SUM(CASE WHEN status_code = 2 THEN 1 ELSE 0 END) AS error_count,
            AVG((end_time_unix - start_time_unix) / 1000000.0) AS avg_duration_ms,
            MIN(start_time_unix)                             AS first_seen_ns,
            MAX(start_time_unix)                             AS last_seen_ns,
            (
                SELECT description
                FROM descriptions d
                WHERE d.service_name = spans.service_name
                  AND COALESCE(d.agent_id, 'main') = COALESCE(spans.agent_id, 'main')
                  {desc_filter}
                ORDER BY d.generated_at DESC, d.id DESC
                LIMIT 1
            )                                                AS description,
            EXISTS (
                SELECT 1
                FROM agent_registrations r
                WHERE r.service_name = spans.service_name
                  AND COALESCE(r.agent_id, 'main') = COALESCE(spans.agent_id, 'main')
                  {reg_filter}
            )                                                AS has_registration,
            (
                SELECT display_name
                FROM agent_display_names dn
                WHERE dn.service_name = spans.service_name
                  AND COALESCE(dn.agent_id, 'main') = COALESCE(spans.agent_id, 'main')
                  {dn_filter}
                LIMIT 1
            )                                                AS display_name,
            (
                SELECT m.name
                FROM agent_owners o
                JOIN team_members m ON m.id = o.team_member_id
                WHERE o.service_name = spans.service_name
                  AND COALESCE(o.agent_id, 'main') = COALESCE(spans.agent_id, 'main')
                  {own_filter}
                LIMIT 1
            )                                                AS owner_name,
            (
                SELECT m.role
                FROM agent_owners o
                JOIN team_members m ON m.id = o.team_member_id
                WHERE o.service_name = spans.service_name
                  AND COALESCE(o.agent_id, 'main') = COALESCE(spans.agent_id, 'main')
                  {own_filter}
                LIMIT 1
            )                                                AS owner_role,
            (
                SELECT o.team_member_id
                FROM agent_owners o
                WHERE o.service_name = spans.service_name
                  AND COALESCE(o.agent_id, 'main') = COALESCE(spans.agent_id, 'main')
                  {own_filter}
                LIMIT 1
            )                                                AS owner_id,
            (
                SELECT resource_attributes
                FROM spans s2
                WHERE s2.service_name = spans.service_name
                  {sample_acct_filter}
                ORDER BY s2.start_time_unix DESC
                LIMIT 1
            )                                                AS sample_resource_attributes
        FROM spans
        {span_filter}
        GROUP BY service_name, agent_id
        ORDER BY last_seen_ns DESC
    """
    # Argument order matches the {PH} occurrences left-to-right in the SQL:
    # desc_filter (1) + reg_filter (1) + dn_filter (1) + 3× own_filter
    # (the three owner subqueries each carry their own account scope) +
    # sample_acct_filter (1) + span_filter (1) = 8 placeholders.
    agg_args = (
        (account_id,) * 8
        if account_id is not None
        else ()
    )

    # Top operations are computed per instance (service_name), not per
    # agent — useful at the group level. Per-agent top-ops would inflate
    # the payload without buying much.
    top_ops_args_extra = (account_id,) if account_id is not None else ()
    top_ops_sql = f"""
        SELECT span_name, COUNT(*) AS c
        FROM spans
        WHERE service_name = {PH}
          {f"AND account_id = {PH}" if account_id is not None else ""}
        GROUP BY span_name
        ORDER BY c DESC
        LIMIT 5
    """

    # Fold the per-(service, agent) rows into groups keyed by service_name.
    # Group-level totals are summed from the per-agent rows so a single SQL
    # round-trip is enough.
    groups: dict[str, dict[str, Any]] = {}

    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(agg_sql, agg_args)
        agg_rows = cur.fetchall()

        for row in agg_rows:
            sn = row["service_name"]
            agent_record = {
                "agent_id": row["agent_id"] or "main",
                "span_count": row["span_count"],
                "error_count": row["error_count"] or 0,
                "avg_duration_ms": float(row["avg_duration_ms"] or 0.0),
                "first_seen": _ns_to_iso(row["first_seen_ns"]),
                "last_seen": _ns_to_iso(row["last_seen_ns"]),
                "has_registration": bool(row["has_registration"]),
                # Per-agent description and display name. Each sub-agent
                # gets its own values — the group-level fields below
                # surface the 'main' sub-agent's values as a default for
                # the Fleet card.
                "description": row["description"],
                "display_name": row["display_name"],
                # Owner — the human team member assigned. None when
                # the sub-agent has no owner.
                "owner_id": row["owner_id"],
                "owner_name": row["owner_name"],
                "owner_role": row["owner_role"],
            }
            if sn not in groups:
                groups[sn] = {
                    "service_name": sn,
                    "agents": [],
                    "total_spans": 0,
                    "total_errors": 0,
                    # Weighted-duration accumulator + total span count for
                    # the post-loop weighted average. Kept here so we don't
                    # need a second SQL pass.
                    "_weighted_sum_ms": 0.0,
                    "first_seen": agent_record["first_seen"],
                    "last_seen": agent_record["last_seen"],
                    "top_operations": [],
                    # Group-level description/display_name/owner start
                    # with whatever the first sub-agent in this group
                    # has; we'll prefer 'main' below if we see it.
                    "description": agent_record["description"],
                    "display_name": agent_record["display_name"],
                    "owner_name": agent_record["owner_name"],
                    "owner_role": agent_record["owner_role"],
                    "has_registration": False,
                    "platform": _detect_platform(
                        row["sample_resource_attributes"]
                    ),
                }
            g = groups[sn]
            g["agents"].append(agent_record)
            g["total_spans"] += agent_record["span_count"]
            g["total_errors"] += agent_record["error_count"]
            # Prefer 'main' for the group-level description/display_name/
            # owner when it exists; otherwise leave whatever was seen first.
            if agent_record["agent_id"] == "main":
                if agent_record["description"]:
                    g["description"] = agent_record["description"]
                if agent_record["display_name"]:
                    g["display_name"] = agent_record["display_name"]
                if agent_record["owner_name"]:
                    g["owner_name"] = agent_record["owner_name"]
                    g["owner_role"] = agent_record["owner_role"]
            g["_weighted_sum_ms"] += (
                agent_record["avg_duration_ms"] * agent_record["span_count"]
            )
            # Earliest/latest seen across all agents in the instance.
            if (
                agent_record["first_seen"]
                and (not g["first_seen"] or agent_record["first_seen"] < g["first_seen"])
            ):
                g["first_seen"] = agent_record["first_seen"]
            if (
                agent_record["last_seen"]
                and (not g["last_seen"] or agent_record["last_seen"] > g["last_seen"])
            ):
                g["last_seen"] = agent_record["last_seen"]
            g["has_registration"] = g["has_registration"] or agent_record["has_registration"]

        # Resolve per-instance derived fields. top_operations needs one
        # extra round-trip per group (small N — number of distinct services).
        for sn, g in groups.items():
            cur.execute(top_ops_sql, (sn, *top_ops_args_extra))
            g["top_operations"] = [r["span_name"] for r in cur.fetchall()]
            g["avg_duration_ms"] = (
                g["_weighted_sum_ms"] / g["total_spans"]
                if g["total_spans"]
                else 0.0
            )
            del g["_weighted_sum_ms"]

    # Sort instances by their most recent span across any agent.
    return sorted(
        groups.values(),
        key=lambda g: g["last_seen"] or "",
        reverse=True,
    )


def get_agent_spans(
    service_name: str,
    limit: int = 50,
    account_id: int | None = None,
    agent_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return the most recent spans for an agent, newest first.

    When `agent_id` is provided, the result is scoped to that sub-agent
    within the instance. NULL agent_id rows (pre-multi-agent data) match
    against the literal string 'main' so they show up under the default
    sub-agent.
    """
    account_filter = (
        f"AND account_id = {PH}" if account_id is not None else ""
    )
    agent_filter = (
        f"AND COALESCE(agent_id, 'main') = {PH}" if agent_id is not None else ""
    )
    sql = f"""
        SELECT id, trace_id, span_id, parent_span_id, service_name,
               COALESCE(agent_id, 'main') AS agent_id, span_name,
               kind, start_time_unix, end_time_unix, status_code, status_message,
               attributes, resource_attributes, created_at
        FROM spans
        WHERE service_name = {PH}
          {account_filter}
          {agent_filter}
        ORDER BY start_time_unix DESC
        LIMIT {PH}
    """
    # Args follow placeholder order: service_name, [account_id], [agent_id], limit.
    args_list: list[Any] = [service_name]
    if account_id is not None:
        args_list.append(account_id)
    if agent_id is not None:
        args_list.append(agent_id)
    args_list.append(limit)
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(sql, tuple(args_list))
        rows = cur.fetchall()

    spans: list[dict[str, Any]] = []
    for r in rows:
        spans.append(
            {
                "id": r["id"],
                "trace_id": r["trace_id"],
                "span_id": r["span_id"],
                "parent_span_id": r["parent_span_id"],
                "service_name": r["service_name"],
                "agent_id": r["agent_id"] or "main",
                "span_name": r["span_name"],
                "kind": r["kind"],
                "start_time_unix": r["start_time_unix"],
                "end_time_unix": r["end_time_unix"],
                "status_code": r["status_code"],
                "status_message": r["status_message"],
                "attributes": json.loads(r["attributes"] or "{}"),
                "resource_attributes": json.loads(r["resource_attributes"] or "{}"),
                "created_at": _ts_to_str(r["created_at"]),
            }
        )
    return spans


def get_agent_summary(
    service_name: str,
    account_id: int | None = None,
    agent_id: str | None = None,
) -> dict[str, Any] | None:
    """Per-instance (or per-agent) summary. Returns None if no spans match.

    When `agent_id` is provided, all SUM/AVG aggregates are scoped to that
    sub-agent. The instance description is still returned unfiltered — there's
    one description per service_name, regardless of how many sub-agents an
    instance has.
    """
    span_account_filter = (
        f"AND account_id = {PH}" if account_id is not None else ""
    )
    desc_account_filter = (
        f"AND account_id = {PH}" if account_id is not None else ""
    )
    span_agent_filter = (
        f"AND COALESCE(agent_id, 'main') = {PH}" if agent_id is not None else ""
    )

    agg_sql = f"""
        SELECT
            COUNT(*)                                       AS span_count,
            SUM(CASE WHEN status_code = 2 THEN 1 ELSE 0 END) AS error_count,
            AVG((end_time_unix - start_time_unix) / 1000000.0) AS avg_duration_ms,
            MIN(start_time_unix)                           AS first_seen_ns,
            MAX(start_time_unix)                           AS last_seen_ns
        FROM spans
        WHERE service_name = {PH}
          {span_account_filter}
          {span_agent_filter}
    """
    top_ops_sql = f"""
        SELECT span_name, COUNT(*) AS c
        FROM spans
        WHERE service_name = {PH}
          {span_account_filter}
          {span_agent_filter}
        GROUP BY span_name
        ORDER BY c DESC
        LIMIT 5
    """
    # Description scoping mirrors span scoping: when agent_id is set,
    # return that sub-agent's description; otherwise return the most
    # recent across any sub-agent of the service (acts as a sensible
    # "instance description" for the group view).
    desc_agent_filter = (
        f"AND COALESCE(agent_id, 'main') = {PH}" if agent_id is not None else ""
    )
    desc_sql = f"""
        SELECT description
        FROM descriptions
        WHERE service_name = {PH}
          {desc_account_filter}
          {desc_agent_filter}
        ORDER BY generated_at DESC, id DESC
        LIMIT 1
    """
    sample_sql = f"""
        SELECT resource_attributes
        FROM spans
        WHERE service_name = {PH}
          {span_account_filter}
          {span_agent_filter}
        ORDER BY start_time_unix DESC
        LIMIT 1
    """

    # Build the args tuple aligned with the {PH} order: service_name first,
    # then optional account_id, then optional agent_id.
    span_args_list: list[Any] = [service_name]
    if account_id is not None:
        span_args_list.append(account_id)
    if agent_id is not None:
        span_args_list.append(agent_id)
    span_args = tuple(span_args_list)
    # desc_sql now optionally takes the same agent_id filter.
    desc_args_list: list[Any] = [service_name]
    if account_id is not None:
        desc_args_list.append(account_id)
    if agent_id is not None:
        desc_args_list.append(agent_id)
    desc_args = tuple(desc_args_list)

    # Display name is its own tiny lookup — at most one row per
    # (service, agent). Cheap enough to fire unconditionally.
    dn_sql = f"""
        SELECT display_name
        FROM agent_display_names
        WHERE service_name = {PH}
          AND COALESCE(agent_id, 'main') = {PH}
          {f"AND account_id = {PH}" if account_id is not None else ""}
        LIMIT 1
    """
    dn_args_list: list[Any] = [service_name, agent_id or "main"]
    if account_id is not None:
        dn_args_list.append(account_id)
    dn_args = tuple(dn_args_list)

    # Owner lookup — joins agent_owners → team_members. Same shape as
    # get_agent_owner but inlined so we keep this in one round-trip.
    owner_sql = f"""
        SELECT m.id AS team_member_id, m.name AS owner_name, m.role AS owner_role
        FROM agent_owners o
        JOIN team_members m ON m.id = o.team_member_id
        WHERE o.service_name = {PH}
          AND COALESCE(o.agent_id, 'main') = {PH}
          {f"AND o.account_id = {PH}" if account_id is not None else ""}
        LIMIT 1
    """
    owner_args = dn_args  # same triple

    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(agg_sql, span_args)
        row = cur.fetchone()
        if not row or not row["span_count"]:
            return None

        cur.execute(top_ops_sql, span_args)
        top_ops_rows = cur.fetchall()

        cur.execute(desc_sql, desc_args)
        desc_row = cur.fetchone()

        cur.execute(sample_sql, span_args)
        sample_row = cur.fetchone()

        cur.execute(dn_sql, dn_args)
        dn_row = cur.fetchone()

        cur.execute(owner_sql, owner_args)
        owner_row = cur.fetchone()

    return {
        "service_name": service_name,
        "agent_id": agent_id or "main" if agent_id is not None else None,
        "span_count": row["span_count"],
        "error_count": row["error_count"] or 0,
        "avg_duration_ms": float(row["avg_duration_ms"] or 0.0),
        "first_seen": _ns_to_iso(row["first_seen_ns"]),
        "last_seen": _ns_to_iso(row["last_seen_ns"]),
        "top_operations": [r["span_name"] for r in top_ops_rows],
        "description": desc_row["description"] if desc_row else None,
        "platform": _detect_platform(
            sample_row["resource_attributes"] if sample_row else None
        ),
        "display_name": dn_row["display_name"] if dn_row else None,
        "owner_id": owner_row["team_member_id"] if owner_row else None,
        "owner_name": owner_row["owner_name"] if owner_row else None,
        "owner_role": owner_row["owner_role"] if owner_row else None,
    }


def get_latest_description(
    service_name: str,
    account_id: int | None = None,
    agent_id: str | None = None,
) -> dict[str, Any] | None:
    """Return the most recent description for an agent, or None.

    When `agent_id` is provided, scopes to that sub-agent. When omitted,
    returns the latest description across any sub-agent — useful as the
    "headline" description for an instance group.
    """
    account_filter = (
        f"AND account_id = {PH}" if account_id is not None else ""
    )
    agent_filter = (
        f"AND COALESCE(agent_id, 'main') = {PH}" if agent_id is not None else ""
    )
    sql = f"""
        SELECT service_name, agent_id, description, span_count_analyzed, generated_at
        FROM descriptions
        WHERE service_name = {PH}
          {account_filter}
          {agent_filter}
        ORDER BY generated_at DESC, id DESC
        LIMIT 1
    """
    args_list: list[Any] = [service_name]
    if account_id is not None:
        args_list.append(account_id)
    if agent_id is not None:
        args_list.append(agent_id)
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(sql, tuple(args_list))
        row = cur.fetchone()

    if row is None:
        return None
    return {
        "service_name": row["service_name"],
        "agent_id": row["agent_id"] or "main",
        "description": row["description"],
        "span_count_analyzed": row["span_count_analyzed"],
        "generated_at": _ts_to_str(row["generated_at"]),
    }


# ---------------------------------------------------------------------------
# Registrations  (append-only — see save_description for the same pattern)
# ---------------------------------------------------------------------------


def save_registration(
    service_name: str,
    agent_id: str,
    soul: str,
    identity: str,
    operating_manual: str,
    user_context: str,
    memory: str,
    workspace_path: str,
    model: str,
    account_id: int | None = None,
) -> None:
    """Persist an agent registration. Append-only so we keep history of how
    an agent's identity changed over time."""
    sql = f"""
        INSERT INTO agent_registrations (
            service_name, agent_id, soul, identity, operating_manual,
            user_context, memory, workspace_path, model, account_id
        ) VALUES ({PH}, {PH}, {PH}, {PH}, {PH}, {PH}, {PH}, {PH}, {PH}, {PH})
    """
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(
            sql,
            (
                service_name,
                agent_id or "main",
                soul or "",
                identity or "",
                operating_manual or "",
                user_context or "",
                memory or "",
                workspace_path or "",
                model or "",
                account_id,
            ),
        )


def get_latest_registration(
    service_name: str,
    account_id: int | None = None,
    agent_id: str | None = None,
) -> dict[str, Any] | None:
    """Return the most recent registration for an agent, or None.

    When `agent_id` is provided, the result is scoped to that sub-agent.
    Multi-agent instances each have their own registration row.
    """
    account_filter = (
        f"AND account_id = {PH}" if account_id is not None else ""
    )
    agent_filter = (
        f"AND COALESCE(agent_id, 'main') = {PH}" if agent_id is not None else ""
    )
    sql = f"""
        SELECT service_name, agent_id, soul, identity, operating_manual,
               user_context, memory, workspace_path, model, created_at
        FROM agent_registrations
        WHERE service_name = {PH}
          {account_filter}
          {agent_filter}
        ORDER BY created_at DESC, id DESC
        LIMIT 1
    """
    args_list: list[Any] = [service_name]
    if account_id is not None:
        args_list.append(account_id)
    if agent_id is not None:
        args_list.append(agent_id)
    args = tuple(args_list)
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(sql, args)
        row = cur.fetchone()

    if row is None:
        return None
    return {
        "service_name": row["service_name"],
        "agent_id": row["agent_id"] or "main",
        "soul": row["soul"] or "",
        "identity": row["identity"] or "",
        "operating_manual": row["operating_manual"] or "",
        "user_context": row["user_context"] or "",
        "memory": row["memory"] or "",
        "workspace_path": row["workspace_path"] or "",
        "model": row["model"] or "",
        "created_at": _ts_to_str(row["created_at"]),
    }


# ---------------------------------------------------------------------------
# Agent display names (operator-editable per-agent labels)
# ---------------------------------------------------------------------------


def set_display_name(
    service_name: str,
    agent_id: str,
    display_name: str,
    account_id: int | None = None,
) -> None:
    """Upsert the operator-set display name for an agent. UNIQUE on
    (account_id, service_name, agent_id) — a second call for the same
    triple overwrites the previous value.

    Trims whitespace; an empty string after trimming clears the row
    (deletes it), so the agent falls back to its raw `service_name` /
    `agent_id` labels in the UI.
    """
    clean = (display_name or "").strip()
    if clean == "":
        # Clear the row entirely so the UI falls back to defaults.
        account_clause = (
            f"AND account_id = {PH}"
            if account_id is not None
            else "AND account_id IS NULL"
        )
        sql = f"""
            DELETE FROM agent_display_names
            WHERE service_name = {PH}
              AND COALESCE(agent_id, 'main') = {PH}
              {account_clause}
        """
        args: tuple[Any, ...] = (service_name, agent_id or "main")
        if account_id is not None:
            args = (*args, account_id)
        with _connect() as conn, _cursor(conn) as cur:
            cur.execute(sql, args)
        return

    # Both backends support `INSERT ... ON CONFLICT ... DO UPDATE` for
    # upsert semantics on the UNIQUE triple. SQLite has supported it
    # since 3.24 (2018); Postgres since 9.5.
    if USE_POSTGRES:
        sql = """
            INSERT INTO agent_display_names (
                account_id, service_name, agent_id, display_name
            ) VALUES (%s, %s, %s, %s)
            ON CONFLICT (account_id, service_name, agent_id)
            DO UPDATE SET display_name = EXCLUDED.display_name,
                          updated_at = NOW()
        """
    else:
        sql = """
            INSERT INTO agent_display_names (
                account_id, service_name, agent_id, display_name
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT (account_id, service_name, agent_id)
            DO UPDATE SET display_name = excluded.display_name,
                          updated_at = CURRENT_TIMESTAMP
        """
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(sql, (account_id, service_name, agent_id or "main", clean))


def get_display_name(
    service_name: str,
    agent_id: str,
    account_id: int | None = None,
) -> str | None:
    """Return the operator-set display name for an agent, or None when
    no override exists."""
    account_filter = (
        f"AND account_id = {PH}"
        if account_id is not None
        else "AND account_id IS NULL"
    )
    sql = f"""
        SELECT display_name
        FROM agent_display_names
        WHERE service_name = {PH}
          AND COALESCE(agent_id, 'main') = {PH}
          {account_filter}
        LIMIT 1
    """
    args: tuple[Any, ...] = (service_name, agent_id or "main")
    if account_id is not None:
        args = (*args, account_id)
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(sql, args)
        row = cur.fetchone()
    return row["display_name"] if row else None


# ---------------------------------------------------------------------------
# Team members + agent ownership
# ---------------------------------------------------------------------------


class TeamMemberEmailExistsError(Exception):
    """Raised when create_team_member hits UNIQUE(account_id, email)."""


def create_team_member(
    account_id: int | None,
    name: str,
    email: str | None = None,
    role: str | None = None,
) -> dict[str, Any]:
    """Insert a new team member and return the resulting row.

    Name is required; email and role are optional. Email is stored as
    NULL when not provided (so multiple "no email" entries coexist —
    the UNIQUE constraint treats NULLs as distinct).
    """
    clean_name = (name or "").strip()
    if not clean_name:
        raise ValueError("name is required")
    clean_email = (email or "").strip() or None
    clean_role = (role or "").strip() or None

    if USE_POSTGRES:
        sql = """
            INSERT INTO team_members (account_id, name, email, role)
            VALUES (%s, %s, %s, %s)
            RETURNING id, account_id, name, email, role, created_at
        """
        args = (account_id, clean_name, clean_email, clean_role)
        try:
            with _connect() as conn, _cursor(conn) as cur:
                cur.execute(sql, args)
                row = cur.fetchone()
        except psycopg2.errors.UniqueViolation as e:
            raise TeamMemberEmailExistsError(
                f"a team member with email '{clean_email}' already exists"
            ) from e
    else:
        sql = """
            INSERT INTO team_members (account_id, name, email, role)
            VALUES (?, ?, ?, ?)
        """
        try:
            with _connect() as conn, _cursor(conn) as cur:
                cur.execute(sql, (account_id, clean_name, clean_email, clean_role))
                new_id = cur.lastrowid
                cur.execute(
                    "SELECT id, account_id, name, email, role, created_at "
                    "FROM team_members WHERE id = ?",
                    (new_id,),
                )
                row = cur.fetchone()
        except sqlite3.IntegrityError as e:
            if "UNIQUE" in str(e):
                raise TeamMemberEmailExistsError(
                    f"a team member with email '{clean_email}' already exists"
                ) from e
            raise

    return {
        "id": row["id"],
        "account_id": row["account_id"],
        "name": row["name"],
        "email": row["email"],
        "role": row["role"],
        "created_at": _ts_to_str(row["created_at"]),
    }


def get_team_members(account_id: int | None) -> list[dict[str, Any]]:
    """Return the team members for an account, oldest first (insertion
    order is the natural sort for a team roster)."""
    account_filter = (
        f"WHERE account_id = {PH}"
        if account_id is not None
        else "WHERE account_id IS NULL"
    )
    sql = f"""
        SELECT id, account_id, name, email, role, created_at
        FROM team_members
        {account_filter}
        ORDER BY id ASC
    """
    args: tuple[Any, ...] = (account_id,) if account_id is not None else ()
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(sql, args)
        rows = cur.fetchall()
    return [
        {
            "id": r["id"],
            "account_id": r["account_id"],
            "name": r["name"],
            "email": r["email"],
            "role": r["role"],
            "created_at": _ts_to_str(r["created_at"]),
        }
        for r in rows
    ]


def delete_team_member(account_id: int | None, member_id: int) -> bool:
    """Delete a team member and clear any agent assignments that
    pointed to them. SQLite's FK enforcement requires PRAGMA
    foreign_keys=ON (we don't rely on it), so we handle the cascade in
    code: clean agent_owners first, then drop the team_member row.
    Returns True when a row was deleted.
    """
    account_clause = (
        f"AND account_id = {PH}"
        if account_id is not None
        else "AND account_id IS NULL"
    )

    with _connect() as conn, _cursor(conn) as cur:
        # Clear any agent assignments before deleting the team member.
        # Scope by account to avoid clearing rows owned by another tenant.
        cur.execute(
            f"DELETE FROM agent_owners WHERE team_member_id = {PH} {account_clause}",
            (member_id,) if account_id is None else (member_id, account_id),
        )
        # Then delete the member itself.
        cur.execute(
            f"DELETE FROM team_members WHERE id = {PH} {account_clause}",
            (member_id,) if account_id is None else (member_id, account_id),
        )
        return cur.rowcount > 0


def set_agent_owner(
    account_id: int | None,
    service_name: str,
    agent_id: str,
    team_member_id: int,
) -> None:
    """Upsert the owner assignment for one sub-agent. UNIQUE on
    (account_id, service_name, agent_id) — re-assigning overwrites."""
    if USE_POSTGRES:
        sql = """
            INSERT INTO agent_owners (account_id, service_name, agent_id, team_member_id)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (account_id, service_name, agent_id)
            DO UPDATE SET team_member_id = EXCLUDED.team_member_id
        """
    else:
        sql = """
            INSERT INTO agent_owners (account_id, service_name, agent_id, team_member_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (account_id, service_name, agent_id)
            DO UPDATE SET team_member_id = excluded.team_member_id
        """
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(
            sql,
            (account_id, service_name, agent_id or "main", team_member_id),
        )


def remove_agent_owner(
    account_id: int | None, service_name: str, agent_id: str
) -> bool:
    """Delete the owner assignment for one sub-agent. Returns True
    when a row was removed."""
    account_clause = (
        f"AND account_id = {PH}"
        if account_id is not None
        else "AND account_id IS NULL"
    )
    sql = f"""
        DELETE FROM agent_owners
        WHERE service_name = {PH}
          AND COALESCE(agent_id, 'main') = {PH}
          {account_clause}
    """
    args: tuple[Any, ...] = (service_name, agent_id or "main")
    if account_id is not None:
        args = (*args, account_id)
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(sql, args)
        return cur.rowcount > 0


def get_agents_for_team_member(
    account_id: int | None, member_id: int
) -> list[dict[str, Any]]:
    """Return the list of (service_name, agent_id) assignments for one
    team member, with the agent's display_name override (if any) and
    a couple of basic stats — span count and last-seen — folded in
    via correlated subqueries. Ordering is service_name then agent_id
    so the list reads predictably across re-renders.
    """
    account_clause = (
        f"AND o.account_id = {PH}"
        if account_id is not None
        else "AND o.account_id IS NULL"
    )
    # Correlated subqueries handle the per-row stats; with the
    # idx_spans_service_agent index each lookup is one indexed seek.
    # Same applies to the display_names LEFT JOIN.
    sql = f"""
        SELECT
            o.service_name,
            COALESCE(o.agent_id, 'main')                 AS agent_id,
            dn.display_name                              AS display_name,
            (SELECT MAX(start_time_unix)
               FROM spans s
              WHERE s.service_name = o.service_name
                AND COALESCE(s.agent_id, 'main') = COALESCE(o.agent_id, 'main')
                {("AND s.account_id = " + PH) if account_id is not None else ""}
            )                                            AS last_seen_ns,
            (SELECT COUNT(*)
               FROM spans s
              WHERE s.service_name = o.service_name
                AND COALESCE(s.agent_id, 'main') = COALESCE(o.agent_id, 'main')
                {("AND s.account_id = " + PH) if account_id is not None else ""}
            )                                            AS span_count
        FROM agent_owners o
        LEFT JOIN agent_display_names dn
          ON dn.service_name = o.service_name
         AND COALESCE(dn.agent_id, 'main') = COALESCE(o.agent_id, 'main')
         {("AND dn.account_id = " + PH) if account_id is not None else "AND dn.account_id IS NULL"}
        WHERE o.team_member_id = {PH}
          {account_clause}
        ORDER BY o.service_name ASC, COALESCE(o.agent_id, 'main') ASC
    """
    # Args order: last_seen acct, span_count acct, dn acct, member_id, owners acct.
    args_list: list[Any] = []
    if account_id is not None:
        args_list.extend([account_id, account_id, account_id])
    args_list.append(member_id)
    if account_id is not None:
        args_list.append(account_id)
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(sql, tuple(args_list))
        rows = cur.fetchall()
    return [
        {
            "service_name": r["service_name"],
            "agent_id": r["agent_id"] or "main",
            "display_name": r["display_name"],
            "last_seen": _ns_to_iso(r["last_seen_ns"]) if r["last_seen_ns"] else None,
            "span_count": r["span_count"] or 0,
        }
        for r in rows
    ]


def get_agent_owner(
    account_id: int | None, service_name: str, agent_id: str
) -> dict[str, Any] | None:
    """Return the team member assigned to a sub-agent, or None when
    unassigned. Joins through agent_owners so we get the full member
    record in one round-trip."""
    account_clause = (
        f"AND o.account_id = {PH}"
        if account_id is not None
        else "AND o.account_id IS NULL"
    )
    sql = f"""
        SELECT m.id, m.name, m.email, m.role
        FROM agent_owners o
        JOIN team_members m ON m.id = o.team_member_id
        WHERE o.service_name = {PH}
          AND COALESCE(o.agent_id, 'main') = {PH}
          {account_clause}
        LIMIT 1
    """
    args: tuple[Any, ...] = (service_name, agent_id or "main")
    if account_id is not None:
        args = (*args, account_id)
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(sql, args)
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "team_member_id": row["id"],
        "name": row["name"],
        "email": row["email"],
        "role": row["role"],
    }


# ---------------------------------------------------------------------------
# Accounts and API keys
# ---------------------------------------------------------------------------


class EmailAlreadyExistsError(Exception):
    """Raised when create_account hits the unique constraint on email."""


def create_account(email: str) -> dict[str, Any]:
    """Insert a new account. Raises EmailAlreadyExistsError if the email is
    taken. Returns {id, email, created_at} for the new row.

    The RETURNING clause differs between backends: Postgres has it natively;
    SQLite has it since 3.35 (March 2021) but we use lastrowid + SELECT as a
    safer fallback that works on any 3.x.
    """
    # Normalize email so comparison is case-insensitive on the application
    # side. We don't add a lowercase index to the table because that's a
    # one-way decision; keeping the original casing in storage preserves
    # the option to change normalization later.
    email = (email or "").strip().lower()
    if not email:
        raise ValueError("email is required")

    try:
        with _connect() as conn, _cursor(conn) as cur:
            if USE_POSTGRES:
                cur.execute(
                    f"INSERT INTO accounts (email) VALUES ({PH}) RETURNING id, email, created_at",
                    (email,),
                )
                row = cur.fetchone()
            else:
                cur.execute(
                    f"INSERT INTO accounts (email) VALUES ({PH})", (email,)
                )
                new_id = cur.lastrowid
                cur.execute(
                    f"SELECT id, email, created_at FROM accounts WHERE id = {PH}",
                    (new_id,),
                )
                row = cur.fetchone()
    except Exception as e:
        msg = str(e).lower()
        if "unique" in msg or "duplicate" in msg:
            raise EmailAlreadyExistsError(email) from e
        raise

    return {
        "id": row["id"],
        "email": row["email"],
        "created_at": _ts_to_str(row["created_at"]),
    }


def get_account_by_email(email: str) -> dict[str, Any] | None:
    """Look up an account by email. Returns None if not found."""
    email = (email or "").strip().lower()
    if not email:
        return None
    sql = f"SELECT id, email, created_at FROM accounts WHERE email = {PH}"
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(sql, (email,))
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "email": row["email"],
        "created_at": _ts_to_str(row["created_at"]),
    }


def generate_api_key(account_id: int, name: str = "default") -> str:
    """Mint a new API key for an account and persist it. Returns the key
    string. The key is shown to the user exactly once — we don't have a
    'retrieve key' flow because we don't store anything that lets us
    distinguish a real key from a forgery without the bytes themselves."""
    # 32 random hex chars = 128 bits of entropy. Plenty for a v1 scheme.
    key = "ov_sk_" + secrets.token_hex(16)
    sql = f"""
        INSERT INTO api_keys (account_id, key, name)
        VALUES ({PH}, {PH}, {PH})
    """
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(sql, (account_id, key, name or "default"))
    return key


def validate_api_key(key: str | None) -> dict[str, Any] | None:
    """Look up a key. Returns {account_id, email, key_name} if active and
    valid, else None. This is the hot path for every authenticated request —
    keep it a single indexed lookup."""
    if not key:
        return None
    sql = f"""
        SELECT k.account_id, a.email, k.name AS key_name
        FROM api_keys k
        JOIN accounts a ON a.id = k.account_id
        WHERE k.key = {PH} AND k.active = TRUE
        LIMIT 1
    """
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(sql, (key,))
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "account_id": row["account_id"],
        "email": row["email"],
        "key_name": row["key_name"],
    }


def get_api_keys_for_account(account_id: int) -> list[dict[str, Any]]:
    """Return all keys for an account (most-recent first). Returns the full
    key strings — only safe because this function is only reachable from
    /auth/login (a public endpoint protected by email knowledge) and from
    the user's own authenticated session."""
    sql = f"""
        SELECT key, name, active, created_at
        FROM api_keys
        WHERE account_id = {PH}
        ORDER BY created_at DESC, id DESC
    """
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(sql, (account_id,))
        rows = cur.fetchall()
    return [
        {
            "key": r["key"],
            "name": r["name"],
            "active": bool(r["active"]),
            "created_at": _ts_to_str(r["created_at"]),
        }
        for r in rows
    ]


def has_any_keys() -> bool:
    """True if any active API key exists. The middleware uses this to
    decide whether to enforce auth — once any key exists, auth is required
    on every protected endpoint. One-way transition: no auth → has auth."""
    sql = "SELECT 1 FROM api_keys WHERE active = TRUE LIMIT 1"
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(sql)
        return cur.fetchone() is not None


# ---------------------------------------------------------------------------
# Captured outputs (gated by the plugin's captureOutputs flag)
# ---------------------------------------------------------------------------


# Attributes the plugin sets when captureOutputs is enabled. One span
# carries at most one of these (they're emitted on different span types:
# message_received / message_sent / tool_call).
_CAPTURE_ATTR_PATTERNS = (
    '%"oversee.message.content":%',
    '%"oversee.response.content":%',
    '%"oversee.tool.result":%',
)


def get_agent_outputs(
    service_name: str,
    account_id: int | None = None,
    limit: int = 20,
    agent_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return spans that carry captured content (message body, response
    body, or tool result) for an agent, newest first.

    Implementation note: the spans table stores `attributes` as a TEXT
    column holding JSON. Rather than per-row casting to JSONB on Postgres
    (and json_extract on SQLite), we use portable LIKE patterns on the
    serialized form — `"key":` matches the key boundary precisely without
    catching incidental occurrences in values. The Python pass then
    parses the JSON to pull out the actual content.
    """
    limit = max(1, min(100, int(limit)))
    account_filter = f"AND account_id = {PH}" if account_id is not None else ""
    agent_filter = (
        f"AND COALESCE(agent_id, 'main') = {PH}" if agent_id is not None else ""
    )
    sql = f"""
        SELECT span_name, start_time_unix, end_time_unix, attributes
        FROM spans
        WHERE service_name = {PH}
          {account_filter}
          {agent_filter}
          AND (
            attributes LIKE {PH}
            OR attributes LIKE {PH}
            OR attributes LIKE {PH}
          )
        ORDER BY start_time_unix DESC
        LIMIT {PH}
    """
    base_args: tuple[Any, ...] = (service_name,)
    if account_id is not None:
        base_args = (*base_args, account_id)
    if agent_id is not None:
        base_args = (*base_args, agent_id)
    args = (*base_args, *_CAPTURE_ATTR_PATTERNS, limit)

    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(sql, args)
        rows = cur.fetchall()

    outputs: list[dict[str, Any]] = []
    for r in rows:
        try:
            attrs = json.loads(r["attributes"] or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(attrs, dict):
            continue

        # Detect which capture attribute fired. Order matters only when
        # a span carries more than one — which shouldn't happen with the
        # current plugin, but we prefer the most specific signal.
        if "oversee.tool.result" in attrs:
            content_type = "tool_result"
            content = attrs.get("oversee.tool.result") or ""
        elif "oversee.response.content" in attrs:
            content_type = "response"
            content = attrs.get("oversee.response.content") or ""
        elif "oversee.message.content" in attrs:
            content_type = "message"
            content = attrs.get("oversee.message.content") or ""
        else:
            # WHERE clause matched but JSON parse showed no key — happens
            # if a value contained the literal pattern. Skip cleanly.
            continue

        duration_ms = (r["end_time_unix"] - r["start_time_unix"]) / 1_000_000.0
        outputs.append(
            {
                "operation": r["span_name"],
                "timestamp": _ns_to_iso(r["start_time_unix"]),
                "content_type": content_type,
                "content": str(content),
                "duration_ms": duration_ms,
            }
        )
    return outputs
