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

_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_spans_service_name ON spans(service_name)",
    "CREATE INDEX IF NOT EXISTS idx_spans_start_time ON spans(start_time_unix)",
    "CREATE INDEX IF NOT EXISTS idx_descriptions_service_name ON descriptions(service_name)",
    "CREATE INDEX IF NOT EXISTS idx_registrations_service_name ON agent_registrations(service_name)",
]


def init_db() -> None:
    """Create tables and indexes. Initializes the pg pool when applicable."""
    global _pool
    if USE_POSTGRES:
        _pool = ThreadedConnectionPool(
            minconn=2, maxconn=10, dsn=_DATABASE_URL,
        )
        ddls = [_SPANS_DDL_PG, _DESC_DDL_PG, _REG_DDL_PG]
    else:
        ddls = [_SPANS_DDL_SQLITE, _DESC_DDL_SQLITE, _REG_DDL_SQLITE]

    with _connect() as conn, _cursor(conn) as cur:
        for ddl in ddls:
            cur.execute(ddl)
        for idx in _INDEXES:
            cur.execute(idx)


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
    "trace_id, span_id, parent_span_id, service_name, span_name, kind, "
    "start_time_unix, end_time_unix, status_code, status_message, "
    "attributes, resource_attributes"
)


def insert_spans(spans: list[dict[str, Any]]) -> int:
    """Bulk-insert parsed spans. Returns the row count."""
    if not spans:
        return 0

    rows = [
        (
            s["trace_id"],
            s["span_id"],
            s.get("parent_span_id") or None,
            s["service_name"],
            s["span_name"],
            s.get("kind", 0),
            s["start_time_unix"],
            s["end_time_unix"],
            s.get("status_code", 0),
            s.get("status_message", "") or "",
            json.dumps(s.get("attributes", {})),
            json.dumps(s.get("resource_attributes", {})),
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
            placeholders = ", ".join(["?"] * 12)
            cur.executemany(
                f"INSERT INTO spans ({_INSERT_COLUMNS}) VALUES ({placeholders})",
                rows,
            )
    return len(rows)


def save_description(
    service_name: str, description: str, span_count_analyzed: int,
) -> None:
    """Persist a newly generated description (append-only — history kept)."""
    sql = f"""
        INSERT INTO descriptions (service_name, description, span_count_analyzed)
        VALUES ({PH}, {PH}, {PH})
    """
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(sql, (service_name, description, span_count_analyzed))


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def get_agents() -> list[dict[str, Any]]:
    """List every agent that has reported telemetry, with latest description."""
    # No bound parameters → SQL is portable between SQLite and Postgres as-is.
    # The correlated subquery for `description` is the standard way to get
    # "latest row per group" in both dialects (avoids the SQLite-only
    # bare-column-on-MAX trick that doesn't port to Postgres).
    agg_sql = """
        SELECT
            service_name,
            COUNT(*)                                       AS span_count,
            SUM(CASE WHEN status_code = 2 THEN 1 ELSE 0 END) AS error_count,
            AVG((end_time_unix - start_time_unix) / 1000000.0) AS avg_duration_ms,
            MIN(start_time_unix)                           AS first_seen_ns,
            MAX(start_time_unix)                           AS last_seen_ns,
            (
                SELECT description
                FROM descriptions d
                WHERE d.service_name = spans.service_name
                ORDER BY d.generated_at DESC, d.id DESC
                LIMIT 1
            )                                              AS description,
            EXISTS (
                SELECT 1
                FROM agent_registrations r
                WHERE r.service_name = spans.service_name
            )                                              AS has_registration
        FROM spans
        GROUP BY service_name
        ORDER BY last_seen_ns DESC
    """
    top_ops_sql = f"""
        SELECT span_name, COUNT(*) AS c
        FROM spans
        WHERE service_name = {PH}
        GROUP BY span_name
        ORDER BY c DESC
        LIMIT 5
    """

    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(agg_sql)
        agg_rows = cur.fetchall()

        agents: list[dict[str, Any]] = []
        for row in agg_rows:
            cur.execute(top_ops_sql, (row["service_name"],))
            top_ops_rows = cur.fetchall()
            agents.append(
                {
                    "service_name": row["service_name"],
                    "span_count": row["span_count"],
                    "error_count": row["error_count"] or 0,
                    "avg_duration_ms": float(row["avg_duration_ms"] or 0.0),
                    "first_seen": _ns_to_iso(row["first_seen_ns"]),
                    "last_seen": _ns_to_iso(row["last_seen_ns"]),
                    "top_operations": [r["span_name"] for r in top_ops_rows],
                    "description": row["description"],
                    # Postgres returns bool, SQLite returns 0/1 — coerce.
                    "has_registration": bool(row["has_registration"]),
                }
            )
    return agents


def get_agent_spans(service_name: str, limit: int = 50) -> list[dict[str, Any]]:
    """Return the most recent spans for an agent, newest first."""
    sql = f"""
        SELECT id, trace_id, span_id, parent_span_id, service_name, span_name,
               kind, start_time_unix, end_time_unix, status_code, status_message,
               attributes, resource_attributes, created_at
        FROM spans
        WHERE service_name = {PH}
        ORDER BY start_time_unix DESC
        LIMIT {PH}
    """
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(sql, (service_name, limit))
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


def get_agent_summary(service_name: str) -> dict[str, Any] | None:
    """Single-agent summary. Returns None if the agent has no spans."""
    agg_sql = f"""
        SELECT
            COUNT(*)                                       AS span_count,
            SUM(CASE WHEN status_code = 2 THEN 1 ELSE 0 END) AS error_count,
            AVG((end_time_unix - start_time_unix) / 1000000.0) AS avg_duration_ms,
            MIN(start_time_unix)                           AS first_seen_ns,
            MAX(start_time_unix)                           AS last_seen_ns
        FROM spans
        WHERE service_name = {PH}
    """
    top_ops_sql = f"""
        SELECT span_name, COUNT(*) AS c
        FROM spans
        WHERE service_name = {PH}
        GROUP BY span_name
        ORDER BY c DESC
        LIMIT 5
    """
    desc_sql = f"""
        SELECT description
        FROM descriptions
        WHERE service_name = {PH}
        ORDER BY generated_at DESC, id DESC
        LIMIT 1
    """

    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(agg_sql, (service_name,))
        row = cur.fetchone()
        if not row or not row["span_count"]:
            return None

        cur.execute(top_ops_sql, (service_name,))
        top_ops_rows = cur.fetchall()

        cur.execute(desc_sql, (service_name,))
        desc_row = cur.fetchone()

    return {
        "service_name": service_name,
        "span_count": row["span_count"],
        "error_count": row["error_count"] or 0,
        "avg_duration_ms": float(row["avg_duration_ms"] or 0.0),
        "first_seen": _ns_to_iso(row["first_seen_ns"]),
        "last_seen": _ns_to_iso(row["last_seen_ns"]),
        "top_operations": [r["span_name"] for r in top_ops_rows],
        "description": desc_row["description"] if desc_row else None,
    }


def get_latest_description(service_name: str) -> dict[str, Any] | None:
    """Return the most recent description for an agent, or None."""
    sql = f"""
        SELECT service_name, description, span_count_analyzed, generated_at
        FROM descriptions
        WHERE service_name = {PH}
        ORDER BY generated_at DESC, id DESC
        LIMIT 1
    """
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(sql, (service_name,))
        row = cur.fetchone()

    if row is None:
        return None
    return {
        "service_name": row["service_name"],
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
) -> None:
    """Persist an agent registration. Append-only so we keep history of how
    an agent's identity changed over time."""
    sql = f"""
        INSERT INTO agent_registrations (
            service_name, agent_id, soul, identity, operating_manual,
            user_context, memory, workspace_path, model
        ) VALUES ({PH}, {PH}, {PH}, {PH}, {PH}, {PH}, {PH}, {PH}, {PH})
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
            ),
        )


def get_latest_registration(service_name: str) -> dict[str, Any] | None:
    """Return the most recent registration for an agent, or None."""
    sql = f"""
        SELECT service_name, agent_id, soul, identity, operating_manual,
               user_context, memory, workspace_path, model, created_at
        FROM agent_registrations
        WHERE service_name = {PH}
        ORDER BY created_at DESC, id DESC
        LIMIT 1
    """
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(sql, (service_name,))
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
