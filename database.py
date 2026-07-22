"""Storage layer for Oversee — Postgres in production, SQLite for local dev.

The backend is chosen at module load by the DATABASE_URL env var:
  DATABASE_URL set    → Postgres via psycopg2 (production / Railway)
  DATABASE_URL unset  → SQLite at ./trovis.db (local development)

Callers don't need to know which backend is active: schema, query semantics,
and return shapes are identical. SQL placeholders are `?` for SQLite and
`%s` for psycopg2; we substitute the right one via the module-level `PH`
constant (a literal — no SQL-injection surface).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
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

    SQLITE_PATH = "trovis.db"
    # Back-compat: if a pre-rename local DB exists and the new one doesn't,
    # keep using it so a developer's existing SQLite data isn't orphaned.
    # (Prod uses Postgres via DATABASE_URL, so this branch never runs there.)
    if not os.path.exists(SQLITE_PATH) and os.path.exists("oversee.db"):
        SQLITE_PATH = "oversee.db"

# Bind-parameter placeholder for the active backend. Used in SQL strings via
# f-string substitution. PH is a module constant, not user input.
PH = "%s" if USE_POSTGRES else "?"


# ---------------------------------------------------------------------------
# Trovis rename back-compat (PERMANENT — never remove these fallbacks)
# ---------------------------------------------------------------------------
# The product/SDK was renamed oversee → trovis, but live agents deployed before
# the rename still emit `oversee.*` span attributes + set `OVERSEE_*` env vars,
# and historical rows in the spans table carry `oversee.*` JSON forever. Every
# attribute read and server env lookup therefore prefers the new `trovis`/
# `TROVIS_` name and falls back to the legacy one. Removing the fallback would
# break still-deployed agents and orphan historical data — keep it permanently.


def attr(attrs: dict[str, Any] | None, suffix: str, default: Any = None) -> Any:
    """Read a span attribute by suffix, preferring `trovis.<suffix>` and
    falling back to the legacy `oversee.<suffix>` (e.g. suffix='agent.id')."""
    if not attrs:
        return default
    val = attrs.get(f"trovis.{suffix}")
    if val is None:
        val = attrs.get(f"oversee.{suffix}", default)
    return val


def env(name: str, default: str | None = None) -> str | None:
    """Read an env var, preferring `TROVIS_<name>` and falling back to the
    legacy `OVERSEE_<name>` (e.g. name='APP_URL')."""
    return os.environ.get(f"TROVIS_{name}", os.environ.get(f"OVERSEE_{name}", default))


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
    return datetime.fromtimestamp(int(ns) / 1_000_000_000, tz=timezone.utc).isoformat()


# "First seen" floor: 2025-01-01 UTC in ns. Trovis didn't exist before this, so
# any span timestamped earlier is a bad-clock / wrong-epoch artifact (e.g. an
# agent host with a skewed clock). MIN(start_time_unix) would otherwise surface
# that as a bogus "first seen" date (the 11/14/2023 bug). We exclude pre-floor
# timestamps from first-seen computations at the source.
_FIRST_SEEN_FLOOR_NS = 1_735_689_600_000_000_000  # 2025-01-01T00:00:00Z


# ---------------------------------------------------------------------------
# Password hashing + opaque tokens (stdlib only — no bcrypt/passlib dep)
# ---------------------------------------------------------------------------

# PBKDF2-HMAC-SHA256 work factor. 600k matches the OWASP 2023 floor for this
# algorithm. `needs_rehash` lets us raise this later without a migration.
_PBKDF2_ITERS = 600_000
# Session + invite lifetimes.
_SESSION_TTL_SECONDS = 30 * 24 * 60 * 60  # 30 days
_INVITE_TTL_SECONDS = 7 * 24 * 60 * 60  # 7 days


def hash_password(password: str) -> str:
    """Return a self-describing PBKDF2 hash: pbkdf2_sha256$iters$salt$hash
    (salt + hash base64url, no padding)."""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERS)
    b = lambda raw: base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")  # noqa: E731
    return f"pbkdf2_sha256${_PBKDF2_ITERS}${b(salt)}${b(dk)}"


def verify_password(password: str, stored: str | None) -> bool:
    """Constant-time verify against a stored hash. False on any parse error or
    when no password is set."""
    if not stored:
        return False
    try:
        algo, iters_s, salt_b64, hash_b64 = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        iters = int(iters_s)

        def _unb(s: str) -> bytes:
            return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

        salt = _unb(salt_b64)
        expected = _unb(hash_b64)
    except (ValueError, TypeError):
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iters)
    return hmac.compare_digest(dk, expected)


def needs_rehash(stored: str | None) -> bool:
    """True when a stored hash uses fewer iterations than the current target,
    so we can transparently upgrade it on the next successful login."""
    if not stored:
        return False
    try:
        _, iters_s, _, _ = stored.split("$")
        return int(iters_s) < _PBKDF2_ITERS
    except (ValueError, TypeError):
        return True


def _new_token() -> tuple[str, str]:
    """Mint an opaque token: (raw, sha256_hex). The raw value is shown to the
    client once; only the hash is persisted. High entropy → plain sha256 at
    rest is correct (and keeps the per-request lookup fast)."""
    raw = secrets.token_urlsafe(32)
    return raw, hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _hash_token(raw: str) -> str:
    return hashlib.sha256((raw or "").encode("utf-8")).hexdigest()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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
    if "trovis.plugin.version" in attrs or "oversee.plugin.version" in attrs:
        return "Trovis-instrumented Agent"

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
    input_tokens        INTEGER   DEFAULT NULL,
    output_tokens       INTEGER   DEFAULT NULL,
    total_tokens        INTEGER   DEFAULT NULL,
    cache_creation_input_tokens INTEGER DEFAULT NULL,
    cache_read_input_tokens     INTEGER DEFAULT NULL,
    estimated_cost_usd  REAL      DEFAULT NULL,
    cost_source         TEXT      DEFAULT NULL,
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
    input_tokens        INTEGER DEFAULT NULL,
    output_tokens       INTEGER DEFAULT NULL,
    total_tokens        INTEGER DEFAULT NULL,
    cache_creation_input_tokens INTEGER DEFAULT NULL,
    cache_read_input_tokens     INTEGER DEFAULT NULL,
    estimated_cost_usd  REAL    DEFAULT NULL,
    cost_source         TEXT    DEFAULT NULL,
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

# Directed agent→agent connections. Auto-detected from shared traces
# (parent_span_id crossing an agent boundary) and/or operator-curated.
# `status` tracks the operator's decision so re-detection refreshes the
# metrics without clobbering a confirm/dismiss/manual choice.
_CONNECTIONS_DDL_PG = """
CREATE TABLE IF NOT EXISTS agent_connections (
    id              SERIAL    PRIMARY KEY,
    account_id      INTEGER   NOT NULL,
    source_service  TEXT      NOT NULL,
    source_agent_id TEXT      NOT NULL DEFAULT 'main',
    target_service  TEXT      NOT NULL,
    target_agent_id TEXT      NOT NULL DEFAULT 'main',
    status          TEXT      NOT NULL DEFAULT 'detected',
    call_count      INTEGER   DEFAULT 0,
    trace_count     INTEGER   DEFAULT 0,
    first_seen      TIMESTAMP,
    last_seen       TIMESTAMP,
    created_at      TIMESTAMP DEFAULT NOW(),
    updated_at      TIMESTAMP DEFAULT NOW(),
    UNIQUE (account_id, source_service, source_agent_id, target_service, target_agent_id)
)
"""

_CONNECTIONS_DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS agent_connections (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id      INTEGER NOT NULL,
    source_service  TEXT    NOT NULL,
    source_agent_id TEXT    NOT NULL DEFAULT 'main',
    target_service  TEXT    NOT NULL,
    target_agent_id TEXT    NOT NULL DEFAULT 'main',
    status          TEXT    NOT NULL DEFAULT 'detected',
    call_count      INTEGER DEFAULT 0,
    trace_count     INTEGER DEFAULT 0,
    first_seen      TIMESTAMP,
    last_seen       TIMESTAMP,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (account_id, source_service, source_agent_id, target_service, target_agent_id)
)
"""

# OAuth 2.0 authorization codes (short-lived, single-use) for the ChatGPT
# Actions integration. ChatGPT redirects the user to /oauth/authorize; after
# consent we issue a code that ChatGPT exchanges at /oauth/token for an
# access token. The access token is a session token (same sessions table).
_OAUTH_CODES_DDL_PG = """
CREATE TABLE IF NOT EXISTS oauth_codes (
    id           SERIAL    PRIMARY KEY,
    code         TEXT      NOT NULL UNIQUE,
    account_id   INTEGER   NOT NULL,
    user_id      INTEGER,
    client_id    TEXT      NOT NULL,
    redirect_uri TEXT      NOT NULL,
    scope        TEXT      DEFAULT '',
    state        TEXT,
    created_at   TIMESTAMP DEFAULT NOW(),
    expires_at   TIMESTAMP NOT NULL,
    used         BOOLEAN   DEFAULT FALSE
)
"""

_OAUTH_CODES_DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS oauth_codes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    code         TEXT    NOT NULL UNIQUE,
    account_id   INTEGER NOT NULL,
    user_id      INTEGER,
    client_id    TEXT    NOT NULL,
    redirect_uri TEXT    NOT NULL,
    scope        TEXT    DEFAULT '',
    state        TEXT,
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at   TIMESTAMP NOT NULL,
    used         BOOLEAN DEFAULT 0
)
"""

# Real human logins. An account is the ORG/tenant; a user belongs to one org.
# email is globally unique (one person = one org in v1). password_hash is
# nullable until set (claimed legacy accounts / pending). role is 'owner' or
# 'member'. Agents never appear here — they authenticate with api_keys.
_USERS_DDL_PG = """
CREATE TABLE IF NOT EXISTS users (
    id            SERIAL    PRIMARY KEY,
    account_id    INTEGER   NOT NULL REFERENCES accounts(id),
    email         TEXT      NOT NULL UNIQUE,
    password_hash TEXT,
    name          TEXT,
    role          TEXT      NOT NULL DEFAULT 'member',
    created_at    TIMESTAMP DEFAULT NOW(),
    last_login_at TIMESTAMP
)
"""

_USERS_DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id    INTEGER NOT NULL REFERENCES accounts(id),
    email         TEXT    NOT NULL UNIQUE,
    password_hash TEXT,
    name          TEXT,
    role          TEXT    NOT NULL DEFAULT 'member',
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_login_at TIMESTAMP
)
"""

# Opaque session tokens for dashboard users. Only the sha256 of the token is
# stored; the raw token is returned to the client once. account_id is
# denormalized so the middleware resolves the tenant in one indexed lookup.
_SESSIONS_DDL_PG = """
CREATE TABLE IF NOT EXISTS sessions (
    id           SERIAL    PRIMARY KEY,
    token_hash   TEXT      NOT NULL UNIQUE,
    user_id      INTEGER   NOT NULL REFERENCES users(id),
    account_id   INTEGER   NOT NULL REFERENCES accounts(id),
    created_at   TIMESTAMP DEFAULT NOW(),
    expires_at   TIMESTAMP NOT NULL,
    last_seen_at TIMESTAMP DEFAULT NOW()
)
"""

_SESSIONS_DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS sessions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash   TEXT    NOT NULL UNIQUE,
    user_id      INTEGER NOT NULL REFERENCES users(id),
    account_id   INTEGER NOT NULL REFERENCES accounts(id),
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at   TIMESTAMP NOT NULL,
    last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

# One-time invite links for adding members to a Business org. Only the token
# hash is stored. Single-use (accepted_at) + expiry enforced on read.
_INVITES_DDL_PG = """
CREATE TABLE IF NOT EXISTS invites (
    id                 SERIAL    PRIMARY KEY,
    token_hash         TEXT      NOT NULL UNIQUE,
    account_id         INTEGER   NOT NULL REFERENCES accounts(id),
    email              TEXT      NOT NULL,
    role               TEXT      NOT NULL DEFAULT 'member',
    invited_by_user_id INTEGER   REFERENCES users(id),
    created_at         TIMESTAMP DEFAULT NOW(),
    expires_at         TIMESTAMP NOT NULL,
    accepted_at        TIMESTAMP
)
"""

_INVITES_DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS invites (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash         TEXT    NOT NULL UNIQUE,
    account_id         INTEGER NOT NULL REFERENCES accounts(id),
    email              TEXT    NOT NULL,
    role               TEXT    NOT NULL DEFAULT 'member',
    invited_by_user_id INTEGER REFERENCES users(id),
    created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at         TIMESTAMP NOT NULL,
    accepted_at        TIMESTAMP
)
"""

# One-time password-reset tokens. Only the token hash is stored; single-use
# (used_at) + short expiry enforced on read. Mirrors the invites table.
_PW_RESET_DDL_PG = """
CREATE TABLE IF NOT EXISTS password_reset_tokens (
    id         SERIAL    PRIMARY KEY,
    token_hash TEXT      NOT NULL UNIQUE,
    user_id    INTEGER   NOT NULL REFERENCES users(id),
    created_at TIMESTAMP DEFAULT NOW(),
    expires_at TIMESTAMP NOT NULL,
    used_at    TIMESTAMP
)
"""

_PW_RESET_DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS password_reset_tokens (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash TEXT    NOT NULL UNIQUE,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP NOT NULL,
    used_at    TIMESTAMP
)
"""

# Per-model token pricing. Global (not account-scoped) — these are
# published list prices, the same for everyone. Costs are per 1,000
# tokens. Seeded at init from _PRICING_SEED below; re-seeding is an
# UPSERT so price corrections on restart are picked up.
_PRICING_DDL_PG = """
CREATE TABLE IF NOT EXISTS model_pricing (
    model_name       TEXT PRIMARY KEY,
    input_cost_per_1k  REAL NOT NULL,
    output_cost_per_1k REAL NOT NULL
)
"""

_PRICING_DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS model_pricing (
    model_name       TEXT PRIMARY KEY,
    input_cost_per_1k  REAL NOT NULL,
    output_cost_per_1k REAL NOT NULL
)
"""

# Bootstrap prices, normalized to cost-per-1,000-tokens. Only used on a
# fresh DB before the first LiteLLM sync (seconds) and as a fallback if the
# sync is ever permanently unreachable — the daily sync overwrites these
# with the published rates. Kept in step with LiteLLM so the fallback isn't
# wrong: e.g. claude-opus-4-6 is $5/$25 per 1M, NOT the older $15/$75 (which
# is claude-opus-4-1) — confirmed against the live price list.
_PRICING_SEED = [
    # model_name,         input/1k,  output/1k   — Anthropic published list
    # prices (platform.claude.com/docs/.../pricing). Opus 4.5–4.8 share
    # $5/$25 per 1M; Opus 4/4.1 are the older $15/$75. Cache tokens are
    # priced as multiples of the base input rate (see _CACHE_* below).
    ("claude-opus-4-8",   0.005,     0.025),    # $5 / $25 per 1M
    ("claude-opus-4-7",   0.005,     0.025),    # $5 / $25 per 1M
    ("claude-opus-4-6",   0.005,     0.025),    # $5 / $25 per 1M
    ("claude-opus-4-5",   0.005,     0.025),    # $5 / $25 per 1M
    ("claude-opus-4-1",   0.015,     0.075),    # $15 / $75 per 1M
    ("claude-opus-4",     0.015,     0.075),    # $15 / $75 per 1M (deprecated)
    ("claude-sonnet-4-6", 0.003,     0.015),    # $3 / $15 per 1M
    ("claude-sonnet-4-5", 0.003,     0.015),    # $3 / $15 per 1M
    ("claude-sonnet-4",   0.003,     0.015),    # $3 / $15 per 1M (deprecated)
    ("claude-haiku-4-5",  0.001,     0.005),    # $1 / $5 per 1M
    ("claude-haiku-3-5",  0.0008,    0.004),    # $0.80 / $4 per 1M
    ("gpt-4o",            0.0025,    0.010),     # $2.50 / $10 per 1M
    ("gpt-4o-mini",       0.00015,   0.0006),    # $0.15 / $0.60 per 1M
]

# Prompt-caching cost multipliers, relative to a model's base input rate
# (Anthropic): 5-minute cache write = 1.25x, 1-hour write = 2x, cache read
# (hit) = 0.1x. We price cache-creation tokens at the 5-minute rate (the
# default TTL) — a 1-hour cache is billed slightly higher than we estimate.
_CACHE_WRITE_MULT = 1.25
_CACHE_READ_MULT = 0.10

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

# Public marketing-site waitlist. No account scoping — this is the pre-signup
# funnel, captured before a tenant exists. Email is the natural key (UNIQUE,
# stored lowercased + trimmed by the writer) so re-submits are idempotent.
_WAITLIST_DDL_PG = """
CREATE TABLE IF NOT EXISTS waitlist_signups (
    id               SERIAL    PRIMARY KEY,
    email            TEXT      NOT NULL UNIQUE,
    source           TEXT,
    runtime_interest TEXT,
    created_at       TIMESTAMP DEFAULT NOW()
)
"""

_WAITLIST_DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS waitlist_signups (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    email            TEXT    NOT NULL UNIQUE,
    source           TEXT,
    runtime_interest TEXT,
    created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

# Cache table for Claude-generated insights (weekly summaries,
# capability maps, …). Polymorphic on `kind` so both insight types
# share one table; `data` is a JSON blob whose shape is determined
# by the caller. UPSERT on the unique 4-tuple keeps it a single row
# per (account, service, agent, kind). TTL enforcement happens on the
# read side (see get_insight's max_age_seconds parameter).
_INSIGHTS_DDL_PG = """
CREATE TABLE IF NOT EXISTS agent_insights (
    id           SERIAL    PRIMARY KEY,
    account_id   INTEGER,
    service_name TEXT      NOT NULL,
    agent_id     TEXT      NOT NULL DEFAULT 'main',
    kind         TEXT      NOT NULL,
    data         TEXT      NOT NULL,
    generated_at TIMESTAMP DEFAULT NOW(),
    UNIQUE (account_id, service_name, agent_id, kind)
)
"""

_INSIGHTS_DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS agent_insights (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id   INTEGER,
    service_name TEXT    NOT NULL,
    agent_id     TEXT    NOT NULL DEFAULT 'main',
    kind         TEXT    NOT NULL,
    data         TEXT    NOT NULL,
    generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (account_id, service_name, agent_id, kind)
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

# Per-agent monthly spend caps (the cost page's per-agent "limits"). One row
# per (account, service_name, agent_id); absence = no cap.
_AGENT_BUDGETS_DDL_PG = """
CREATE TABLE IF NOT EXISTS agent_budgets (
    id               SERIAL    PRIMARY KEY,
    account_id       INTEGER,
    service_name     TEXT      NOT NULL,
    agent_id         TEXT      NOT NULL DEFAULT 'main',
    monthly_cap_usd  REAL      NOT NULL,
    updated_at       TIMESTAMP DEFAULT NOW(),
    UNIQUE (account_id, service_name, agent_id)
)
"""

_AGENT_BUDGETS_DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS agent_budgets (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id       INTEGER,
    service_name     TEXT    NOT NULL,
    agent_id         TEXT    NOT NULL DEFAULT 'main',
    monthly_cap_usd  REAL    NOT NULL,
    updated_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (account_id, service_name, agent_id)
)
"""

# Per-account proactive-alert configuration. One row per account; absence means
# "defaults" (email on to the owner, all rules on). Channels: email (to the
# account owner via Resend), plus an optional Slack incoming-webhook URL and/or
# a generic webhook URL. Rules are individually toggleable; thresholds tune the
# budget-warn percentage and the runaway-loop trip count.
_ALERT_SETTINGS_DDL_PG = """
CREATE TABLE IF NOT EXISTS alert_settings (
    account_id        INTEGER PRIMARY KEY,
    email_enabled     INTEGER   NOT NULL DEFAULT 1,
    slack_webhook_url TEXT,
    webhook_url       TEXT,
    rule_drift        INTEGER   NOT NULL DEFAULT 1,
    rule_budget       INTEGER   NOT NULL DEFAULT 1,
    rule_loop         INTEGER   NOT NULL DEFAULT 1,
    rule_error        INTEGER   NOT NULL DEFAULT 1,
    budget_warn_pct   INTEGER   NOT NULL DEFAULT 80,
    loop_threshold    INTEGER   NOT NULL DEFAULT 50,
    updated_at        TIMESTAMP DEFAULT NOW()
)
"""

_ALERT_SETTINGS_DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS alert_settings (
    account_id        INTEGER PRIMARY KEY,
    email_enabled     INTEGER   NOT NULL DEFAULT 1,
    slack_webhook_url TEXT,
    webhook_url       TEXT,
    rule_drift        INTEGER   NOT NULL DEFAULT 1,
    rule_budget       INTEGER   NOT NULL DEFAULT 1,
    rule_loop         INTEGER   NOT NULL DEFAULT 1,
    rule_error        INTEGER   NOT NULL DEFAULT 1,
    budget_warn_pct   INTEGER   NOT NULL DEFAULT 80,
    loop_threshold    INTEGER   NOT NULL DEFAULT 50,
    updated_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

# Dedup + history for fired alerts. The sweep records one row per delivered
# alert keyed by (account, rule, subject, state); before firing it checks
# whether a matching row exists inside the cooldown window, so a standing
# condition (an agent still drifting) alerts at most once per cooldown instead
# of every sweep. `state_key` captures WHAT tripped (e.g. budget bucket "100",
# drift headline hash) so a genuinely new state re-alerts.
_ALERT_LOG_DDL_PG = """
CREATE TABLE IF NOT EXISTS alert_log (
    id          SERIAL    PRIMARY KEY,
    account_id  INTEGER   NOT NULL,
    rule        TEXT      NOT NULL,
    subject_key TEXT      NOT NULL DEFAULT '',
    state_key   TEXT      NOT NULL DEFAULT '',
    created_at  TIMESTAMP DEFAULT NOW()
)
"""

_ALERT_LOG_DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS alert_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id  INTEGER NOT NULL,
    rule        TEXT    NOT NULL,
    subject_key TEXT    NOT NULL DEFAULT '',
    state_key   TEXT    NOT NULL DEFAULT '',
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

# Process workflows: a named, ordered sequence of steps (agent + human)
# describing how work flows through an agent's process. Auto-generated from
# telemetry + identity by Claude, then operator-editable. workflow_steps
# cascades on workflow delete (enforced in code for SQLite — see
# delete_workflow — since we don't enable the foreign_keys pragma).
_WORKFLOWS_DDL_PG = """
CREATE TABLE IF NOT EXISTS workflows (
    id                 SERIAL    PRIMARY KEY,
    account_id         INTEGER   NOT NULL,
    name               TEXT      NOT NULL,
    description        TEXT,
    agent_service_name TEXT,
    agent_id           TEXT      DEFAULT 'main',
    created_at         TIMESTAMP DEFAULT NOW(),
    updated_at         TIMESTAMP DEFAULT NOW()
)
"""

_WORKFLOWS_DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS workflows (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id         INTEGER NOT NULL,
    name               TEXT    NOT NULL,
    description        TEXT,
    agent_service_name TEXT,
    agent_id           TEXT    DEFAULT 'main',
    created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

_WORKFLOW_STEPS_DDL_PG = """
CREATE TABLE IF NOT EXISTS workflow_steps (
    id                   SERIAL  PRIMARY KEY,
    workflow_id          INTEGER NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
    step_order           INTEGER NOT NULL,
    step_type            TEXT    NOT NULL,
    label                TEXT    NOT NULL,
    description          TEXT,
    agent_service_name   TEXT,
    agent_id             TEXT,
    team_member_id       INTEGER,
    operation            TEXT,
    duration_estimate_ms INTEGER,
    inferred_from        TEXT,
    config               TEXT
)
"""

_WORKFLOW_STEPS_DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS workflow_steps (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id          INTEGER NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
    step_order           INTEGER NOT NULL,
    step_type            TEXT    NOT NULL,
    label                TEXT    NOT NULL,
    description          TEXT,
    agent_service_name   TEXT,
    agent_id             TEXT,
    team_member_id       INTEGER,
    operation            TEXT,
    duration_estimate_ms INTEGER,
    inferred_from        TEXT,
    config               TEXT
)
"""

# Multi-agent / multi-human: who participates in a workflow. One row per
# agent (service_name + agent_id) or human role. Cascades on workflow delete
# (enforced in code for SQLite — see delete_workflow).
_WORKFLOW_PARTICIPANTS_DDL_PG = """
CREATE TABLE IF NOT EXISTS workflow_participants (
    id                 SERIAL  PRIMARY KEY,
    workflow_id        INTEGER NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
    participant_type   TEXT    NOT NULL,
    agent_service_name TEXT,
    agent_id           TEXT,
    role_name          TEXT,
    team_member_id     INTEGER
)
"""

_WORKFLOW_PARTICIPANTS_DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS workflow_participants (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id        INTEGER NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
    participant_type   TEXT    NOT NULL,
    agent_service_name TEXT,
    agent_id           TEXT,
    role_name          TEXT,
    team_member_id     INTEGER
)
"""

# Directed connections between steps (the workflow graph). is_branch flags a
# decision-path edge (drawn dashed). Cascades on workflow + step delete.
_WORKFLOW_EDGES_DDL_PG = """
CREATE TABLE IF NOT EXISTS workflow_edges (
    id           SERIAL  PRIMARY KEY,
    workflow_id  INTEGER NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
    from_step_id INTEGER NOT NULL REFERENCES workflow_steps(id) ON DELETE CASCADE,
    to_step_id   INTEGER NOT NULL REFERENCES workflow_steps(id) ON DELETE CASCADE,
    label        TEXT,
    is_branch    BOOLEAN DEFAULT FALSE,
    edge_order   INTEGER DEFAULT 0
)
"""

_WORKFLOW_EDGES_DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS workflow_edges (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id  INTEGER NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
    from_step_id INTEGER NOT NULL REFERENCES workflow_steps(id) ON DELETE CASCADE,
    to_step_id   INTEGER NOT NULL REFERENCES workflow_steps(id) ON DELETE CASCADE,
    label        TEXT,
    is_branch    INTEGER DEFAULT 0,
    edge_order   INTEGER DEFAULT 0
)
"""

# --- Workloops ------------------------------------------------------------
# A loop is a unit of work derived from the event stream (see loops.py).
# `loops` is the mutable READ MODEL: cached_state/closed_at/last_event_unix
# are recomputed caches, never sources of truth. The event record itself
# (spans + loop_events) is append-only. external_id/service_name/agent_id/
# last_event_unix are denormalized here purely so implicit grouping (keyed
# lookup + 30-min gap rule) is one indexed query, not a JSON scan.
_LOOPS_DDL_PG = """
CREATE TABLE IF NOT EXISTS loops (
    id                SERIAL  PRIMARY KEY,
    account_id        INTEGER REFERENCES accounts(id),
    external_id       TEXT,
    service_name      TEXT    NOT NULL,
    agent_id          TEXT    NOT NULL DEFAULT 'main',
    title             TEXT,
    initiated_by_type TEXT    NOT NULL DEFAULT 'agent'
                      CHECK (initiated_by_type IN ('agent', 'human')),
    initiated_by      TEXT    NOT NULL DEFAULT '',
    cached_state      TEXT    NOT NULL DEFAULT 'open'
                      CHECK (cached_state IN ('open', 'working', 'awaiting_human',
                             'awaiting_agent', 'stalled', 'done', 'abandoned')),
    last_event_unix   BIGINT,
    created_at        TIMESTAMP DEFAULT NOW(),
    closed_at         TIMESTAMP
)
"""

_LOOPS_DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS loops (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id        INTEGER REFERENCES accounts(id),
    external_id       TEXT,
    service_name      TEXT    NOT NULL,
    agent_id          TEXT    NOT NULL DEFAULT 'main',
    title             TEXT,
    initiated_by_type TEXT    NOT NULL DEFAULT 'agent'
                      CHECK (initiated_by_type IN ('agent', 'human')),
    initiated_by      TEXT    NOT NULL DEFAULT '',
    cached_state      TEXT    NOT NULL DEFAULT 'open'
                      CHECK (cached_state IN ('open', 'working', 'awaiting_human',
                             'awaiting_agent', 'stalled', 'done', 'abandoned')),
    last_event_unix   INTEGER,
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    closed_at         TIMESTAMP
)
"""

# Loop lifecycle events — APPEND-ONLY, like spans. Never UPDATE or DELETE
# rows here; corrections are new events. `type` vocabulary is enforced in
# code (append_loop_event / loops.EVENT_TYPES), not a CHECK, so it can grow
# without a table rebuild on SQLite. event_time_unix (unix ns) is the sole
# ordering key — created_at is display-only (dialect-divergent type).
# actor_type includes 'system' so sweep-authored events (auto-abandon) are
# attributed natively, with no reserved uuid or boolean flag needed.
_LOOP_EVENTS_DDL_PG = """
CREATE TABLE IF NOT EXISTS loop_events (
    id              SERIAL  PRIMARY KEY,
    account_id      INTEGER,
    loop_id         INTEGER NOT NULL REFERENCES loops(id),
    type            TEXT    NOT NULL,
    actor_type      TEXT    NOT NULL CHECK (actor_type IN ('agent', 'human', 'system')),
    actor           TEXT    NOT NULL DEFAULT '',
    payload         TEXT    DEFAULT '{}',
    event_time_unix BIGINT  NOT NULL,
    created_at      TIMESTAMP DEFAULT NOW()
)
"""

_LOOP_EVENTS_DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS loop_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id      INTEGER,
    loop_id         INTEGER NOT NULL REFERENCES loops(id),
    type            TEXT    NOT NULL,
    actor_type      TEXT    NOT NULL CHECK (actor_type IN ('agent', 'human', 'system')),
    actor           TEXT    NOT NULL DEFAULT '',
    payload         TEXT    DEFAULT '{}',
    event_time_unix INTEGER NOT NULL,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

# Who's involved in a loop. participant is the composite "service:agent_id"
# for agents, str(user_id) for humans (see loops.agent_actor).
_LOOP_PARTICIPANTS_DDL_PG = """
CREATE TABLE IF NOT EXISTS loop_participants (
    id               SERIAL  PRIMARY KEY,
    loop_id          INTEGER NOT NULL REFERENCES loops(id),
    participant_type TEXT    NOT NULL CHECK (participant_type IN ('agent', 'human')),
    participant      TEXT    NOT NULL,
    role             TEXT    NOT NULL DEFAULT 'executor'
                     CHECK (role IN ('initiator', 'executor', 'reviewer')),
    added_at         TIMESTAMP DEFAULT NOW(),
    UNIQUE (loop_id, participant_type, participant, role)
)
"""

_LOOP_PARTICIPANTS_DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS loop_participants (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    loop_id          INTEGER NOT NULL REFERENCES loops(id),
    participant_type TEXT    NOT NULL CHECK (participant_type IN ('agent', 'human')),
    participant      TEXT    NOT NULL,
    role             TEXT    NOT NULL DEFAULT 'executor'
                     CHECK (role IN ('initiator', 'executor', 'reviewer')),
    added_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (loop_id, participant_type, participant, role)
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
    "CREATE INDEX IF NOT EXISTS idx_insights_account_id ON agent_insights(account_id)",
    "CREATE INDEX IF NOT EXISTS idx_insights_service_agent_kind ON agent_insights(service_name, agent_id, kind)",
    # Speeds the cost aggregation, which filters to spans with a
    # non-null total_tokens within a service + time window.
    "CREATE INDEX IF NOT EXISTS idx_spans_tokens ON spans(service_name, total_tokens)",
    "CREATE INDEX IF NOT EXISTS idx_workflows_account_id ON workflows(account_id)",
    "CREATE INDEX IF NOT EXISTS idx_workflow_steps_workflow_id ON workflow_steps(workflow_id)",
    "CREATE INDEX IF NOT EXISTS idx_workflow_steps_order ON workflow_steps(workflow_id, step_order)",
    "CREATE INDEX IF NOT EXISTS idx_workflow_participants_workflow_id ON workflow_participants(workflow_id)",
    "CREATE INDEX IF NOT EXISTS idx_workflow_edges_workflow_id ON workflow_edges(workflow_id)",
    "CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)",
    "CREATE INDEX IF NOT EXISTS idx_users_account_id ON users(account_id)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_token_hash ON sessions(token_hash)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_invites_token_hash ON invites(token_hash)",
    "CREATE INDEX IF NOT EXISTS idx_invites_account_id ON invites(account_id)",
    "CREATE INDEX IF NOT EXISTS idx_pw_reset_token_hash ON password_reset_tokens(token_hash)",
    # Trace-grouping for agent-to-agent connection detection.
    "CREATE INDEX IF NOT EXISTS idx_spans_account_trace ON spans(account_id, trace_id)",
    "CREATE INDEX IF NOT EXISTS idx_connections_account_id ON agent_connections(account_id)",
    "CREATE INDEX IF NOT EXISTS idx_agent_budgets_account ON agent_budgets(account_id, service_name, agent_id)",
    "CREATE INDEX IF NOT EXISTS idx_oauth_codes_code ON oauth_codes(code)",
    "CREATE INDEX IF NOT EXISTS idx_alert_log_lookup ON alert_log(account_id, rule, subject_key, state_key)",
    # Workloops. The spans index is partial — most historical spans have
    # NULL loop_id (no backfill) and the syntax is identical on both
    # backends (SQLite supports partial indexes since 3.8).
    "CREATE INDEX IF NOT EXISTS idx_spans_loop_id ON spans(loop_id) WHERE loop_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_loops_account_state ON loops(account_id, cached_state)",
    "CREATE INDEX IF NOT EXISTS idx_loops_account_created ON loops(account_id, created_at DESC)",
    # Implicit grouping lookups: keyed (external_id/run.id, service-wide so
    # multi-agent runs share one loop) and per-agent gap-rule.
    "CREATE INDEX IF NOT EXISTS idx_loops_grouping ON loops(account_id, service_name, external_id)",
    "CREATE INDEX IF NOT EXISTS idx_loops_gap ON loops(account_id, service_name, agent_id, last_event_unix)",
    "CREATE INDEX IF NOT EXISTS idx_loop_events_loop_ts ON loop_events(loop_id, event_time_unix)",
    "CREATE INDEX IF NOT EXISTS idx_loop_participants_loop ON loop_participants(loop_id)",
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
            _INSIGHTS_DDL_PG,
            _PRICING_DDL_PG,
            # workflows before workflow_steps — the FK references it.
            _WORKFLOWS_DDL_PG,
            _WORKFLOW_STEPS_DDL_PG,
            # participants + edges reference workflows/workflow_steps.
            _WORKFLOW_PARTICIPANTS_DDL_PG,
            _WORKFLOW_EDGES_DDL_PG,
            # users before sessions/invites — the FKs reference it.
            _USERS_DDL_PG,
            _SESSIONS_DDL_PG,
            _INVITES_DDL_PG,
            _PW_RESET_DDL_PG,
            _CONNECTIONS_DDL_PG,
            _AGENT_BUDGETS_DDL_PG,
            _ALERT_SETTINGS_DDL_PG,
            _ALERT_LOG_DDL_PG,
            _OAUTH_CODES_DDL_PG,
            # Standalone (no FKs, no account scoping) — order doesn't matter.
            _WAITLIST_DDL_PG,
            # loops before loop_events/loop_participants — the FKs reference it.
            _LOOPS_DDL_PG,
            _LOOP_EVENTS_DDL_PG,
            _LOOP_PARTICIPANTS_DDL_PG,
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
            _INSIGHTS_DDL_SQLITE,
            _PRICING_DDL_SQLITE,
            _WORKFLOWS_DDL_SQLITE,
            _WORKFLOW_STEPS_DDL_SQLITE,
            _WORKFLOW_PARTICIPANTS_DDL_SQLITE,
            _WORKFLOW_EDGES_DDL_SQLITE,
            _USERS_DDL_SQLITE,
            _SESSIONS_DDL_SQLITE,
            _INVITES_DDL_SQLITE,
            _PW_RESET_DDL_SQLITE,
            _CONNECTIONS_DDL_SQLITE,
            _AGENT_BUDGETS_DDL_SQLITE,
            _ALERT_SETTINGS_DDL_SQLITE,
            _ALERT_LOG_DDL_SQLITE,
            _OAUTH_CODES_DDL_SQLITE,
            _WAITLIST_DDL_SQLITE,
            _LOOPS_DDL_SQLITE,
            _LOOP_EVENTS_DDL_SQLITE,
            _LOOP_PARTICIPANTS_DDL_SQLITE,
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
        # Two-field descriptions (redesigned Agent Detail header): `description`
        # holds the short declarative line; `description_long` the 2-3 sentence
        # context shown behind "More". NULL on pre-v2 rows → the detail endpoint
        # treats a NULL long as "regenerate this description on next read".
        _try_add_column(cur, "descriptions", "description_long", "TEXT DEFAULT NULL")
        # Token + cost columns on spans. NULL on existing rows (we never
        # captured tokens before), which the cost aggregates treat as
        # "no usage data" — they SUM only non-NULL rows.
        _try_add_column(cur, "spans", "input_tokens", "INTEGER DEFAULT NULL")
        _try_add_column(cur, "spans", "output_tokens", "INTEGER DEFAULT NULL")
        _try_add_column(cur, "spans", "total_tokens", "INTEGER DEFAULT NULL")
        _try_add_column(cur, "spans", "estimated_cost_usd", "REAL DEFAULT NULL")
        # Prompt-caching token counts (billed at cache multipliers of input).
        # NULL on pre-cache-era rows → treated as 0 (no cache cost recoverable).
        _try_add_column(cur, "spans", "cache_creation_input_tokens", "INTEGER DEFAULT NULL")
        _try_add_column(cur, "spans", "cache_read_input_tokens", "INTEGER DEFAULT NULL")
        # Cost provenance: 'reported' = the SDK supplied an authoritative run
        # cost (trovis.run.cost_usd) and estimated_cost_usd IS that value;
        # 'covered' = a per-turn span whose token cost is already included in
        # a reported run total (cost zeroed to avoid double-counting);
        # NULL/'estimate' = token-derived estimate (recompute may re-price).
        _try_add_column(cur, "spans", "cost_source", "TEXT DEFAULT NULL")
        # Pricing provenance: which source set each rate, and when it was
        # last refreshed. Lets the admin endpoint report freshness and lets
        # us tell a seeded fallback price apart from a live LiteLLM one.
        # (No CURRENT_TIMESTAMP default — SQLite rejects a non-constant
        # default on ALTER ADD COLUMN; upsert_pricing sets it explicitly.)
        _try_add_column(cur, "model_pricing", "source", "TEXT DEFAULT 'seed'")
        _try_add_column(cur, "model_pricing", "updated_at", "TIMESTAMP")
        # Account (org) gains a type + display name. Existing rows default to
        # 'individual'; name stays NULL until set.
        _try_add_column(cur, "accounts", "account_type", "TEXT DEFAULT 'individual'")
        _try_add_column(cur, "accounts", "name", "TEXT")
        # Editable org-wide monthly cost budget (the cost page's "limit"). NULL
        # until set → falls back to the OVERSEE_MONTHLY_BUDGET env default.
        _try_add_column(cur, "accounts", "monthly_budget_usd", "REAL")
        # Set when the owner finishes (or skips) the post-signup onboarding
        # wizard. NULL → wizard still shows for the owner.
        _try_add_column(cur, "accounts", "onboarded_at", "TIMESTAMP")
        # Plan tier gates how many agents are *viewable* (never how many are
        # recorded). Default 'free'. See _AGENT_LIMIT_BY_PLAN / agent_limit().
        _try_add_column(cur, "accounts", "plan", "TEXT DEFAULT 'free'")
        # Stripe customer id, captured from the first completed checkout. Needed
        # to open the Stripe Customer Portal (manage/cancel/invoices).
        _try_add_column(cur, "accounts", "stripe_customer_id", "TEXT")
        # Connection edges gained "what's transferred" metrics post-launch.
        _try_add_column(cur, "agent_connections", "via_operations", "TEXT")
        _try_add_column(cur, "agent_connections", "total_tokens", "INTEGER DEFAULT 0")
        _try_add_column(cur, "agent_connections", "sample", "TEXT")
        # Workflows redesign: creation method + the source description (for
        # the 'describe' method). Existing workflows default to 'generate'.
        _try_add_column(cur, "workflows", "method", "TEXT DEFAULT 'generate'")
        _try_add_column(cur, "workflows", "source_description", "TEXT")
        # Spatial canvas: each step gains a position + node size. Existing
        # steps default to (0,0) and are auto-laid-out client-side.
        _try_add_column(cur, "workflow_steps", "pos_x", "REAL DEFAULT 0")
        _try_add_column(cur, "workflow_steps", "pos_y", "REAL DEFAULT 0")
        _try_add_column(cur, "workflow_steps", "node_width", "REAL DEFAULT 170")
        _try_add_column(cur, "workflow_steps", "node_height", "REAL DEFAULT 72")
        # Workloops: link each span to the loop it belongs to. Nullable —
        # historical spans are never backfilled, and non-ingest writers
        # (MCP synthetic spans) stay loop-less. Set at INSERT time only;
        # spans are append-only, so this is never UPDATEd afterwards. No
        # FK: SQLite's ALTER ADD COLUMN can't enforce one, and the two
        # backends must stay schema-identical.
        _try_add_column(cur, "spans", "loop_id", "INTEGER DEFAULT NULL")
        for idx in _INDEXES:
            cur.execute(idx)
        # Bootstrap the pricing table with a handful of common models so
        # cost works on a fresh DB before the first LiteLLM refresh lands.
        # INSERT-only (DO NOTHING) — the daily sync is authoritative and
        # must never be clobbered back to these hardcoded values on restart.
        _seed_pricing(cur)


def _seed_pricing(cur) -> None:
    """Seed the authoritative Anthropic/OpenAI list prices on every startup.

    Inserts any missing model and corrects rows we previously seeded — but the
    `WHERE source = 'seed'` guard means a fresher rate written by the daily
    LiteLLM sync is never clobbered. This guarantees newly-released models
    (e.g. claude-opus-4-7) get a price after a deploy even if the sync hasn't
    run or doesn't list them yet, while keeping the sync authoritative."""
    ph = "%s" if USE_POSTGRES else "?"
    excluded = "EXCLUDED" if USE_POSTGRES else "excluded"
    sql = f"""
        INSERT INTO model_pricing (model_name, input_cost_per_1k, output_cost_per_1k, source)
        VALUES ({ph}, {ph}, {ph}, 'seed')
        ON CONFLICT (model_name) DO UPDATE SET
            input_cost_per_1k = {excluded}.input_cost_per_1k,
            output_cost_per_1k = {excluded}.output_cost_per_1k
        WHERE model_pricing.source = 'seed'
    """
    for model, in_cost, out_cost in _PRICING_SEED:
        cur.execute(sql, (model, in_cost, out_cost))


def upsert_pricing(
    rows: list[tuple[str, float, float]], source: str = "litellm"
) -> int:
    """Bulk INSERT-or-UPDATE model rates from the daily sync. Each row is
    (model_name, input_cost_per_1k, output_cost_per_1k). Stamps `source`
    and `updated_at` so the admin endpoint can report freshness. Returns the
    number of rows written."""
    if not rows:
        return 0
    params = [(m, i, o, source) for (m, i, o) in rows]
    with _connect() as conn, _cursor(conn) as cur:
        if USE_POSTGRES:
            execute_values(
                cur,
                "INSERT INTO model_pricing "
                "(model_name, input_cost_per_1k, output_cost_per_1k, source, updated_at) "
                "VALUES %s "
                "ON CONFLICT (model_name) DO UPDATE SET "
                "input_cost_per_1k = EXCLUDED.input_cost_per_1k, "
                "output_cost_per_1k = EXCLUDED.output_cost_per_1k, "
                "source = EXCLUDED.source, "
                "updated_at = EXCLUDED.updated_at",
                params,
                template="(%s, %s, %s, %s, NOW())",
            )
        else:
            cur.executemany(
                "INSERT INTO model_pricing "
                "(model_name, input_cost_per_1k, output_cost_per_1k, source, updated_at) "
                "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT (model_name) DO UPDATE SET "
                "input_cost_per_1k = excluded.input_cost_per_1k, "
                "output_cost_per_1k = excluded.output_cost_per_1k, "
                "source = excluded.source, "
                "updated_at = CURRENT_TIMESTAMP",
                params,
            )
    return len(params)


def recompute_span_costs(account_id: int | None = None) -> dict[str, Any]:
    """Re-price stored token-bearing spans using the CURRENT pricing table +
    cache multipliers, and write back any changed `estimated_cost_usd`.

    Use after pricing changes (new model added, rate corrected) so historical
    cost reflects today's prices instead of staying frozen at ingest. Cache
    cost is recovered only for spans that captured cache tokens; older spans
    (NULL cache columns) re-price on input/output alone. Account-scoped when
    `account_id` is given, else the whole table. Returns {scanned, updated}."""
    account_filter = f"AND account_id = {PH}" if account_id is not None else ""
    # Only token-derived estimates are re-priced. 'reported' rows carry the
    # SDK's authoritative run cost and 'covered' rows are deliberately zeroed
    # (their cost lives in a reported total) — re-pricing either would
    # corrupt exact billing.
    sql = (
        "SELECT id, attributes, input_tokens, output_tokens, "
        "cache_creation_input_tokens, cache_read_input_tokens, estimated_cost_usd "
        "FROM spans WHERE total_tokens IS NOT NULL "
        f"AND (cost_source IS NULL OR cost_source = 'estimate') {account_filter}"
    )
    args: tuple[Any, ...] = (account_id,) if account_id is not None else ()
    updated = 0
    with _connect() as conn, _cursor(conn) as cur:
        pricing = _load_pricing(cur)
        cur.execute(sql, args)
        rows = cur.fetchall()
        changes: list[tuple[Any, Any]] = []
        for r in rows:
            try:
                attrs = json.loads(r["attributes"] or "{}")
            except (TypeError, ValueError):
                attrs = {}
            model = model_from_attrs(attrs)
            new_cost = _compute_cost(
                model,
                r["input_tokens"],
                r["output_tokens"],
                pricing,
                cache_creation=_row_get(r, "cache_creation_input_tokens") or 0,
                cache_read=_row_get(r, "cache_read_input_tokens") or 0,
            )
            if new_cost != r["estimated_cost_usd"]:
                changes.append((new_cost, r["id"]))
        for new_cost, sid in changes:
            cur.execute(
                f"UPDATE spans SET estimated_cost_usd = {PH} WHERE id = {PH}",
                (new_cost, sid),
            )
        updated = len(changes)
    return {"scanned": len(rows), "updated": updated}


def get_pricing_summary(sample: int = 10) -> dict[str, Any]:
    """Total model count, per-source breakdown, last refresh time, and a
    sample of rows — backs the GET /admin/pricing endpoint."""
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute("SELECT COUNT(*) AS n FROM model_pricing")
        total = int(cur.fetchone()["n"])
        cur.execute("SELECT MAX(updated_at) AS last FROM model_pricing")
        last = cur.fetchone()["last"]
        cur.execute(
            "SELECT source, COUNT(*) AS n FROM model_pricing GROUP BY source"
        )
        by_source = {
            (r["source"] or "unknown"): int(r["n"]) for r in cur.fetchall()
        }
        cur.execute(
            f"SELECT model_name, input_cost_per_1k, output_cost_per_1k, source "
            f"FROM model_pricing ORDER BY model_name LIMIT {int(sample)}"
        )
        sample_rows = [dict(r) for r in cur.fetchall()]
    return {
        "total_models": total,
        "last_updated": _ts_to_str(last),
        "by_source": by_source,
        "sample": sample_rows,
    }


def get_pricing_coverage(
    account_id: int | None = None, days: int = 30
) -> dict[str, Any]:
    """Which models that actually showed up in telemetry resolve to a price.

    Looks at token-bearing spans (those carrying `gen_ai.usage.*`) over the
    last `days`, pulls each distinct `gen_ai.request.model`, and classifies
    it against the CURRENT pricing table + matcher — so it answers "with
    today's prices, is this model's cost tracked?" The `unmatched` list is
    the actionable bit: models burning tokens with no price (NULL cost),
    worst offenders by token volume first. Scoped to the caller's account.
    """
    window_ns = time.time_ns() - days * 24 * 60 * 60 * 1_000_000_000
    sql = (
        "SELECT attributes, total_tokens FROM spans "
        f"WHERE total_tokens IS NOT NULL AND start_time_unix >= {PH}"
    )
    params: list[Any] = [window_ns]
    if account_id is not None:
        sql += f" AND account_id = {PH}"
        params.append(account_id)

    seen: dict[str, dict[str, int]] = {}
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(sql, tuple(params))
        for row in cur.fetchall():
            try:
                attrs = json.loads(row["attributes"] or "{}")
            except (TypeError, ValueError):
                continue
            model = model_from_attrs(attrs)
            if not model:
                continue
            agg = seen.setdefault(model, {"spans": 0, "tokens": 0})
            agg["spans"] += 1
            agg["tokens"] += int(row["total_tokens"] or 0)
        pricing = _load_pricing(cur)

    matched, unmatched = [], []
    for model, agg in seen.items():
        rate = _match_pricing(model, pricing)
        if rate is None:
            unmatched.append(
                {"model": model, "spans": agg["spans"], "tokens": agg["tokens"]}
            )
        else:
            matched.append(
                {
                    "model": model,
                    "spans": agg["spans"],
                    "tokens": agg["tokens"],
                    "input_cost_per_1k": rate[0],
                    "output_cost_per_1k": rate[1],
                }
            )
    # Worst offenders first: most tokens burned with no price.
    unmatched.sort(key=lambda m: m["tokens"], reverse=True)
    matched.sort(key=lambda m: m["tokens"], reverse=True)
    return {
        "window_days": days,
        "models_seen": len(seen),
        "matched_count": len(matched),
        "unmatched_count": len(unmatched),
        "matched": matched,
        "unmatched": unmatched,
    }


def get_cost_audit(
    account_id: int | None = None,
    service_name: str | None = None,
    days: int = 30,
) -> dict[str, Any]:
    """Per-day, per-model cost breakdown for debugging undercounting.

    For each UTC day in the window it reports total spans, how many carried
    usage tokens, how many of those got a price, total tokens, total stored
    cost, and the tokens that were token-bearing but UNPRICED (cost NULL).
    This separates the two failure modes: a *match gap* (tokens present but
    cost NULL → model not in the price table) vs a *capture gap* (few/no
    token-bearing spans on a day the agent clearly ran → the SDK didn't emit
    `gen_ai.usage.*`). Account-scoped; optional single-agent filter.
    """
    window_ns = time.time_ns() - days * 24 * 60 * 60 * 1_000_000_000
    sql = (
        "SELECT start_time_unix, attributes, total_tokens, estimated_cost_usd "
        f"FROM spans WHERE start_time_unix >= {PH}"
    )
    params: list[Any] = [window_ns]
    if account_id is not None:
        sql += f" AND account_id = {PH}"
        params.append(account_id)
    if service_name is not None:
        sql += f" AND service_name = {PH}"
        params.append(service_name)

    days_map: dict[str, dict[str, Any]] = {}
    unpriced: dict[str, dict[str, int]] = {}
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(sql, tuple(params))
        for row in cur.fetchall():
            ts = row["start_time_unix"] or 0
            day = datetime.fromtimestamp(ts / 1_000_000_000, tz=timezone.utc).strftime(
                "%Y-%m-%d"
            )
            d = days_map.setdefault(
                day,
                {
                    "date": day, "spans": 0, "token_spans": 0, "priced_spans": 0,
                    "tokens": 0, "cost": 0.0, "unpriced_tokens": 0,
                    "no_usage_spans": 0, "reported_spans": 0,
                },
            )
            d["spans"] += 1
            tok = row["total_tokens"]
            cost = row["estimated_cost_usd"]
            if tok is None:
                # Token-less spans normally carry no cost — except SDK-reported
                # run totals (cost without usage), which must count.
                if cost is not None:
                    d["reported_spans"] += 1
                    d["cost"] += float(cost or 0.0)
                else:
                    d["no_usage_spans"] += 1
                continue
            d["token_spans"] += 1
            d["tokens"] += int(tok or 0)
            if cost is not None:
                d["priced_spans"] += 1
                d["cost"] += float(cost or 0.0)
            else:
                d["unpriced_tokens"] += int(tok or 0)
                try:
                    attrs = json.loads(row["attributes"] or "{}")
                except (TypeError, ValueError):
                    attrs = {}
                model = model_from_attrs(attrs) or "(no model on span)"
                agg = unpriced.setdefault(model, {"spans": 0, "tokens": 0})
                agg["spans"] += 1
                agg["tokens"] += int(tok or 0)

    day_rows = sorted(days_map.values(), key=lambda x: x["date"], reverse=True)
    for d in day_rows:
        d["cost"] = round(d["cost"], 6)
    unpriced_models = sorted(
        ({"model": m, **agg} for m, agg in unpriced.items()),
        key=lambda x: x["tokens"], reverse=True,
    )
    return {
        "window_days": days,
        "service_name": service_name,
        "days": day_rows,
        "unpriced_models": unpriced_models,
        "unpriced_token_total": sum(m["tokens"] for m in unpriced_models),
    }


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
    "attributes, resource_attributes, account_id, "
    "input_tokens, output_tokens, total_tokens, "
    "cache_creation_input_tokens, cache_read_input_tokens, estimated_cost_usd, "
    "cost_source, loop_id"
)
_INSERT_COLUMN_COUNT = 22


def _agent_id_from_attrs(attrs: dict[str, Any] | None) -> str:
    """Extract the per-event agent id from a parsed span's attributes.

    The plugin stamps this on every hook span as `trovis.agent.id` (legacy
    agents: `oversee.agent.id`). Other OTEL SDKs don't set it; those agents are
    single-instance, so we default to 'main' to keep them grouped per service.
    """
    if not attrs:
        return "main"
    val = attr(attrs, "agent.id")
    if isinstance(val, str) and val:
        return val
    return "main"


# ---------------------------------------------------------------------------
# Token usage + cost estimation
# ---------------------------------------------------------------------------


def _to_int(v: Any) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


# Run ids are SDK session/run uuids. Only ids matching this are used to build
# LIKE patterns for cross-batch cost covering (guards against pattern abuse).
_RUN_ID_SAFE_RE = re.compile(r"^[A-Za-z0-9_\-:.]{1,128}$")


# Token-usage attribute aliases, most-standard first. Trovis is built on the
# OTEL standard, and the ecosystem spans three generations of naming:
#   - current GenAI semconv:  gen_ai.usage.input_tokens / output_tokens
#   - legacy GenAI semconv:   gen_ai.usage.prompt_tokens / completion_tokens
#     (still emitted by OpenLLMetry/Traceloop and older instrumentations)
#   - OpenInference (Arize):  llm.token_count.prompt / completion / total
# Accepting all three means any OTEL-instrumented agent cost-tracks out of the
# box, not just ones using our SDK.
_INPUT_TOKEN_KEYS = (
    "gen_ai.usage.input_tokens",
    "gen_ai.usage.prompt_tokens",
    "llm.token_count.prompt",
)
_OUTPUT_TOKEN_KEYS = (
    "gen_ai.usage.output_tokens",
    "gen_ai.usage.completion_tokens",
    "llm.token_count.completion",
)
_TOTAL_TOKEN_KEYS = (
    "gen_ai.usage.total_tokens",
    "llm.token_count.total",
)

# Model-id attribute aliases. request.model first (what our SDKs set), then
# the semconv response model (often the only one set, and more precise — it
# carries the dated id), then OpenInference's name.
_MODEL_KEYS = (
    "gen_ai.request.model",
    "gen_ai.response.model",
    "llm.model_name",
)


def _first_int(attrs: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    for k in keys:
        v = _to_int(attrs.get(k))
        if v is not None:
            return v
    return None


def model_from_attrs(attrs: dict[str, Any] | None) -> str | None:
    """The span's model id, accepting the standard aliases (see _MODEL_KEYS)."""
    if not isinstance(attrs, dict):
        return None
    for k in _MODEL_KEYS:
        v = attrs.get(k)
        if v:
            return str(v)
    return None


def _extract_tokens(
    attrs: dict[str, Any] | None,
) -> tuple[int | None, int | None, int | None]:
    """Pull (input, output, total) token counts from a span's attributes,
    accepting current GenAI semconv, legacy semconv, and OpenInference names
    (see _INPUT_TOKEN_KEYS et al).

    Returns (None, None, None) when no usage data is present. Derives
    `total` from input+output when the SDK only reports the two parts.
    """
    if not attrs:
        return (None, None, None)
    inp = _first_int(attrs, _INPUT_TOKEN_KEYS)
    out = _first_int(attrs, _OUTPUT_TOKEN_KEYS)
    tot = _first_int(attrs, _TOTAL_TOKEN_KEYS)
    if tot is None and (inp is not None or out is not None):
        tot = (inp or 0) + (out or 0)
    return (inp, out, tot)


def _extract_cache_tokens(
    attrs: dict[str, Any] | None,
) -> tuple[int | None, int | None]:
    """Pull (cache_creation, cache_read) input-token counts from a span's
    attributes. Anthropic bills these separately from input_tokens
    (creation at 1.25x, read at 0.1x of the base input rate)."""
    if not attrs:
        return (None, None)
    cc = _to_int(attrs.get("gen_ai.usage.cache_creation_input_tokens"))
    cr = _to_int(attrs.get("gen_ai.usage.cache_read_input_tokens"))
    return (cc, cr)


def _normalize_model(model: Any) -> str:
    """Lowercase, strip a leading `provider/` segment. So
    'Anthropic/Claude-Opus-4-6' → 'claude-opus-4-6'."""
    if not model:
        return ""
    return str(model).strip().lower().split("/")[-1]


# Model families eligible for same-family price fallback. Providers price by
# family/tier, so the newest known same-family (and same-tier, when the name
# carries a tier token like opus/flash/mini) rate is a sound estimate for a
# brand-new id the table doesn't carry yet. In production the table holds the
# LiteLLM-synced live list (~2,700 models, refreshed daily), so the fallback
# anchors to real current rates — and the post-sync recompute retroactively
# corrects any span priced via fallback once the exact rate lands.
_FALLBACK_FAMILIES = (
    "claude", "gpt", "gemini", "grok", "deepseek",
    "mistral", "mixtral", "llama", "qwen", "command",
)


def _version_tuple(key: str) -> tuple[int, ...]:
    """Numeric tokens in a model key, for picking the newest in a tier:
    'claude-opus-4-7' → (4, 7), 'claude-opus-4' → (4,)."""
    return tuple(int(n) for n in re.findall(r"\d+", key))


def _family_fallback(
    norm: str, pricing: dict[str, tuple[float, float]]
) -> tuple[float, float] | None:
    """Estimate a rate for an unknown id from its model family — the known
    same-family rate sharing the most name tokens (tier words like opus /
    flash / mini), newest version winning ties. Returns None for unrecognized
    families (the id surfaces as unmatched in coverage rather than pricing as
    a wrong guess), but a known-family model never silently prices as $0."""
    family = norm.split("-", 1)[0].split(".")[0]
    if family not in _FALLBACK_FAMILIES:
        return None
    cands = [k for k in pricing if k == family or k.startswith(family + "-")]
    if not cands:
        return None
    # Tier tokens of the unknown id: its name parts minus the family name and
    # bare version numbers ('claude-opus-5' → {'opus'}).
    want = {
        t for t in norm.split("-")[1:] if t and not t.replace(".", "").isdigit()
    }

    def score(key: str) -> tuple[int, tuple[int, ...], int]:
        overlap = len(want & set(key.split("-")))
        # Prefer: most tier-token overlap, then newest version, then the
        # shorter (base/flagship) id on ties.
        return (overlap, _version_tuple(key), -len(key))

    return pricing[max(cands, key=score)]


# One trailing date/version token providers append to a base id, e.g.
# '-20250514', '-2024-08-06', '-v1:0', '-001', '-latest'.
_DATE_SUFFIX_RE = re.compile(
    r"[-@:](\d{8}|\d{6}|\d{4}-\d{2}-\d{2}|v\d+(?::\d+)?|\d{3}|latest|preview)$"
)


def _strip_date_suffix(name: str) -> str:
    """Drop one trailing date/version token: 'claude-opus-4-20250514' →
    'claude-opus-4'. Last-resort so a dated id an agent emits can still find
    a base price when the table only carries the undated form."""
    return _DATE_SUFFIX_RE.sub("", name)


def _match_pricing(
    model: Any, pricing: dict[str, tuple[float, float]]
) -> tuple[float, float] | None:
    """Resolve a model id to its (input/1k, output/1k) rate.

    Most-specific first:
      1. exact id as emitted
      2. normalized (lowercased, provider prefix stripped)
      3. longest boundary-aware prefix — a dated/variant id
         ('claude-opus-4-20250514') matches its base key
         ('claude-opus-4'); we pick the LONGEST matching base, and require a
         '-' boundary so 'gpt-4' can't masquerade as a match for 'gpt-45'
      4. date/version-suffix stripped, retried exact

    Returns None for genuinely unknown models — those spans store NULL cost
    rather than a wrong guess, and surface in /admin/pricing/coverage.
    """
    if not model:
        return None
    if model in pricing:
        return pricing[model]
    norm = _normalize_model(model)
    if norm in pricing:
        return pricing[norm]
    best: str | None = None
    for key in pricing:
        if norm == key or norm.startswith(key + "-"):
            if best is None or len(key) > len(best):
                best = key
    if best is not None:
        return pricing[best]
    base = _strip_date_suffix(norm)
    if base != norm and base in pricing:
        return pricing[base]
    # Same-family fallback: a brand-new/variant id we don't carry yet
    # (e.g. 'claude-opus-4-9', 'claude-sonnet-5') should price at the newest
    # KNOWN same-tier rate rather than store NULL and silently drop the cost.
    # Anthropic prices by tier, so the newest opus/sonnet/haiku rate is a sound
    # estimate. Surfaces in /admin/pricing/coverage either way.
    return _family_fallback(norm, pricing)


def _compute_cost(
    model: Any,
    input_tokens: int | None,
    output_tokens: int | None,
    pricing: dict[str, tuple[float, float]],
    cache_creation: int | None = 0,
    cache_read: int | None = 0,
) -> float | None:
    """Estimated USD cost for one model call. None when the model is
    unknown (no pricing) — we never guess a price. Cached input tokens are
    billed as multiples of the base input rate: cache-creation at 1.25x,
    cache-read at 0.1x (Anthropic prompt caching)."""
    rate = _match_pricing(model, pricing)
    if rate is None:
        return None
    in_per_1k, out_per_1k = rate
    cost = (
        (input_tokens or 0) / 1000.0 * in_per_1k
        + (output_tokens or 0) / 1000.0 * out_per_1k
        + (cache_creation or 0) / 1000.0 * in_per_1k * _CACHE_WRITE_MULT
        + (cache_read or 0) / 1000.0 * in_per_1k * _CACHE_READ_MULT
    )
    return round(cost, 6)


def _load_pricing(cur) -> dict[str, tuple[float, float]]:
    """Read the whole (small) pricing table into a dict for in-Python
    cost computation during a bulk insert."""
    cur.execute(
        "SELECT model_name, input_cost_per_1k, output_cost_per_1k FROM model_pricing"
    )
    return {
        r["model_name"]: (r["input_cost_per_1k"], r["output_cost_per_1k"])
        for r in cur.fetchall()
    }


def insert_spans(
    spans: list[dict[str, Any]], account_id: int | None = None,
) -> int:
    """Bulk-insert parsed spans with loop_id NULL. Returns the row count.

    Thin wrapper around _insert_span_rows for writers that don't participate
    in workloops (MCP synthetic spans, connect-ask). The OTLP ingest path
    uses ingest_spans_with_loops instead, which resolves loops and links
    spans in the same transaction. These callers staying loop-less is
    intentional — don't "fix" them to create loops.
    """
    if not spans:
        return 0
    with _connect() as conn, _cursor(conn) as cur:
        return _insert_span_rows(cur, spans, account_id)


def _insert_span_rows(
    cur,
    spans: list[dict[str, Any]],
    account_id: int | None = None,
    loop_ids: list[int | None] | None = None,
) -> int:
    """Insert parsed spans on an open cursor — the caller owns the
    transaction. Tags each row with account_id when provided (None preserves
    the pre-multi-tenant behavior). loop_ids is positionally parallel to
    spans (None → every row gets loop_id NULL).

    Token usage (`gen_ai.usage.*`) and the model (`gen_ai.request.model`)
    are read off each span's attributes; when both are present and the
    model is in the pricing table, an estimated USD cost is computed and
    stored. Spans without usage data store NULLs and are ignored by the
    cost aggregates.

    SDK-reported cost beats estimation: when a span carries an authoritative
    run cost (`trovis.run.cost_usd`, e.g. the Claude Agent SDK's own
    `total_cost_usd` — which includes internal model calls our token stream
    never sees), that value IS the span's cost (`cost_source='reported'`),
    and the same run's per-turn token estimates are zeroed
    (`cost_source='covered'`) so the run isn't double-counted. Works for any
    platform that reports a run cost; everything else keeps token estimates.
    """
    if not spans:
        return 0

    pricing = _load_pricing(cur)

    # Pass 1: which runs in this batch carry an SDK-reported total?
    parsed: list[tuple] = []
    reported_runs: set[str] = set()
    for s in spans:
        attrs = s.get("attributes") or {}
        inp, out, tot = _extract_tokens(attrs)
        cc, cr = _extract_cache_tokens(attrs)
        model = model_from_attrs(attrs)
        has_usage = tot is not None or cc is not None or cr is not None
        # total_tokens counts every billed token, cache included.
        if has_usage:
            tot = (tot or 0) + (cc or 0) + (cr or 0)
        run_id = attr(attrs, "run.id")
        run_id = str(run_id) if run_id else None
        reported: float | None = None
        try:
            rc = attr(attrs, "run.cost_usd")
            if rc is not None and float(rc) > 0:
                reported = round(float(rc), 6)
        except (TypeError, ValueError):
            pass
        if reported is not None and run_id:
            reported_runs.add(run_id)
        parsed.append(
            (s, attrs, inp, out, tot, cc, cr, model, has_usage, run_id, reported)
        )

    rows = []
    for i, (
        s, attrs, inp, out, tot, cc, cr, model, has_usage, run_id, reported,
    ) in enumerate(parsed):
        if reported is not None:
            cost: float | None = reported
            source: str | None = "reported"
        elif has_usage and run_id in reported_runs:
            cost = 0.0  # included in this run's reported total
            source = "covered"
        elif has_usage:
            cost = _compute_cost(
                model, inp, out, pricing, cache_creation=cc, cache_read=cr
            )
            source = None  # token-derived estimate
        else:
            cost = None
            source = None
        rows.append(
            (
                s["trace_id"],
                s["span_id"],
                s.get("parent_span_id") or None,
                s["service_name"],
                _agent_id_from_attrs(attrs),
                s["span_name"],
                s.get("kind", 0),
                s["start_time_unix"],
                s["end_time_unix"],
                s.get("status_code", 0),
                s.get("status_message", "") or "",
                json.dumps(s.get("attributes", {})),
                json.dumps(s.get("resource_attributes", {})),
                account_id,
                inp,
                out,
                tot,
                cc,
                cr,
                cost,
                source,
                loop_ids[i] if loop_ids else None,
            )
        )

    if USE_POSTGRES:
        execute_values(
            cur,
            f"INSERT INTO spans ({_INSERT_COLUMNS}) VALUES %s",
            rows,
        )
    else:
        placeholders = ", ".join(["?"] * _INSERT_COLUMN_COUNT)
        cur.executemany(
            f"INSERT INTO spans ({_INSERT_COLUMNS}) VALUES ({placeholders})",
            rows,
        )

    # Cross-batch covering: a run's per-turn spans often arrive in earlier
    # export batches than its run-complete span. Now that the run's
    # reported total has landed, zero those earlier token estimates so the
    # run isn't double-counted. Prior 'reported' rows are left alone —
    # per the Agent SDK docs each result reflects only its own query()
    # call, so multiple reported totals on one session legitimately sum.
    for rid in reported_runs:
        if not _RUN_ID_SAFE_RE.match(rid):
            continue  # don't build LIKE patterns from exotic ids
        sql = (
            "UPDATE spans SET estimated_cost_usd = 0, cost_source = 'covered' "
            f"WHERE (attributes LIKE {PH} OR attributes LIKE {PH}) "
            "AND total_tokens IS NOT NULL "
            "AND (cost_source IS NULL OR cost_source = 'estimate')"
        )
        args: list[Any] = [
            f'%"trovis.run.id": "{rid}"%',
            f'%"oversee.run.id": "{rid}"%',
        ]
        if account_id is not None:
            sql += f" AND account_id = {PH}"
            args.append(account_id)
        cur.execute(sql, tuple(args))
    return len(rows)


# ---------------------------------------------------------------------------
# Workloops
# ---------------------------------------------------------------------------
# The event record (spans + loop_events) is APPEND-ONLY: no function below
# ever UPDATEs or DELETEs a row in either table. The `loops` table is the
# derived read model — its cached_state / closed_at / last_event_unix are
# recomputed caches (from loops.compute_loop_state), never sources of truth,
# and are the only rows this feature mutates.

_NS_PER_S = 1_000_000_000


def _loops_mod():
    """loops.py imports database (for env config), so database imports loops
    lazily — same pattern as sentry_sdk in main.py."""
    import loops  # noqa: PLC0415

    return loops


def _insert_returning_id(cur, sql: str, params: tuple) -> int:
    """INSERT and return the new row id on either backend."""
    if USE_POSTGRES:
        cur.execute(sql + " RETURNING id", params)
        return cur.fetchone()["id"]
    cur.execute(sql, params)
    return cur.lastrowid


def _loop_account_clause(account_id: int | None) -> tuple[str, list]:
    """Strict account matching for loop grouping/writes. Unlike the read
    convention (omit the filter when None), grouping must match NULL rows
    exactly — otherwise open-mode spans could join another tenant's loop."""
    if account_id is None:
        return "AND account_id IS NULL", []
    return f"AND account_id = {PH}", [account_id]


def append_loop_event(
    cur,
    loop_id: int,
    event_type: str,
    actor_type: str,
    actor: str,
    payload: dict[str, Any] | None = None,
    account_id: int | None = None,
    event_time_unix: int | None = None,
) -> int:
    """Append one loop lifecycle event. The single write path into
    loop_events — the type/actor vocabularies are enforced here (in code,
    not a CHECK, so they can grow without a SQLite table rebuild)."""
    lp = _loops_mod()
    if event_type not in lp.EVENT_TYPES:
        raise ValueError(f"unknown loop event type: {event_type!r}")
    if actor_type not in lp.ACTOR_TYPES:
        raise ValueError(f"unknown loop actor type: {actor_type!r}")
    if event_time_unix is None:
        event_time_unix = time.time_ns()
    return _insert_returning_id(
        cur,
        "INSERT INTO loop_events "
        "(account_id, loop_id, type, actor_type, actor, payload, event_time_unix) "
        f"VALUES ({PH}, {PH}, {PH}, {PH}, {PH}, {PH}, {PH})",
        (
            account_id,
            loop_id,
            event_type,
            actor_type,
            actor or "",
            json.dumps(payload or {}),
            int(event_time_unix),
        ),
    )


def _upsert_loop_participant(
    cur, loop_id: int, participant_type: str, participant: str, role: str
) -> None:
    # Same-syntax upsert on both backends (SQLite >= 3.24, like _seed_pricing).
    cur.execute(
        "INSERT INTO loop_participants (loop_id, participant_type, participant, role) "
        f"VALUES ({PH}, {PH}, {PH}, {PH}) "
        "ON CONFLICT (loop_id, participant_type, participant, role) DO NOTHING",
        (loop_id, participant_type, participant, role),
    )


def _resolve_loop_for_span(
    cur,
    attrs: dict[str, Any],
    service_name: str,
    agent_id: str,
    ts_ns: int,
    account_id: int | None,
    cache: dict,
) -> int:
    """Find or create the loop a span belongs to.

    Grouping, in order:
      1. Keyed: trovis.loop.external_id, falling back to trovis.run.id
         (both dual-read with the legacy oversee.* prefix via attr()) →
         the service's open loop with that external_id. Deliberately NOT
         per-agent_id: a multi-agent run shares one run.id, and its
         sub-agents must land in ONE loop as co-participants.
      2. Gap rule: the agent's most recent open keyless loop whose last
         event is < GAP_THRESHOLD old, else a new loop. (Per-agent_id —
         with no shared key there's nothing tying sub-agents together.)

    Closed loops (closed_at set) never accept new spans — a recurring
    external_id/run.id after a close starts a fresh loop.
    """
    lp = _loops_mod()
    key = attr(attrs, "loop.external_id") or attr(attrs, "run.id")
    key = str(key) if key else None
    cache_key = (service_name, key) if key else (service_name, agent_id, None)
    if cache_key in cache:
        return cache[cache_key]

    acct_sql, acct_args = _loop_account_clause(account_id)
    row = None
    if key is not None:
        cur.execute(
            f"SELECT id FROM loops WHERE service_name = {PH} "
            f"AND external_id = {PH} AND closed_at IS NULL {acct_sql} "
            "ORDER BY id DESC LIMIT 1",
            tuple([service_name, key, *acct_args]),
        )
        row = cur.fetchone()
    else:
        cutoff = ts_ns - lp.GAP_THRESHOLD_S * _NS_PER_S
        cur.execute(
            f"SELECT id FROM loops WHERE service_name = {PH} AND agent_id = {PH} "
            f"AND external_id IS NULL AND closed_at IS NULL "
            f"AND last_event_unix >= {PH} {acct_sql} "
            "ORDER BY last_event_unix DESC LIMIT 1",
            tuple([service_name, agent_id, cutoff, *acct_args]),
        )
        row = cur.fetchone()
    if row:
        loop_id = row["id"]
        cache[cache_key] = loop_id
        return loop_id

    title = attr(attrs, "loop.title")
    actor = lp.agent_actor(service_name, agent_id)
    loop_id = _insert_returning_id(
        cur,
        "INSERT INTO loops (account_id, external_id, service_name, agent_id, "
        "title, initiated_by_type, initiated_by, cached_state, last_event_unix) "
        f"VALUES ({PH}, {PH}, {PH}, {PH}, {PH}, 'agent', {PH}, 'open', {PH})",
        (
            account_id,
            key,
            service_name,
            agent_id,
            str(title) if title else None,
            actor,
            ts_ns,
        ),
    )
    append_loop_event(
        cur, loop_id, "loop_opened", "agent", actor,
        account_id=account_id, event_time_unix=ts_ns,
    )
    _upsert_loop_participant(cur, loop_id, "agent", actor, "initiator")
    cache[cache_key] = loop_id
    return loop_id


# Values of trovis.loop.close that mean a bare "done" (no extra detail).
_LOOP_CLOSE_BARE = ("", "done", "true", "1", "yes")


def ingest_spans_with_loops(
    spans: list[dict[str, Any]], account_id: int | None = None,
) -> int:
    """The OTLP ingest write path: resolve a loop for every span, link spans
    at INSERT time (loop_id is never UPDATEd later), append any loop
    lifecycle events the batch carries (handoff / close attributes), and
    recompute each affected loop's cached_state — all in ONE transaction.

    Historical spans are never backfilled; only spans flowing through here
    get a loop_id.
    """
    if not spans:
        return 0
    lp = _loops_mod()
    now_ns = time.time_ns()

    with _connect() as conn, _cursor(conn) as cur:
        cache: dict = {}
        loop_ids: list[int | None] = []
        affected: dict[int, int] = {}  # loop_id -> max event ts in this batch
        executor_pairs: set[tuple[int, str]] = set()
        closes: dict[int, tuple[str, str, int]] = {}  # loop_id -> (actor, value, ts)

        for s in spans:
            attrs = s.get("attributes") or {}
            svc = s["service_name"]
            aid = _agent_id_from_attrs(attrs)
            # Loop bookkeeping uses the span's own clock so the gap rule and
            # idle thresholds survive batched/delayed exports — but clamped:
            # agent clocks are untrusted (the _FIRST_SEEN_FLOOR_NS lesson).
            ts = min(max(int(s.get("start_time_unix") or 0), _FIRST_SEEN_FLOOR_NS), now_ns)
            loop_id = _resolve_loop_for_span(cur, attrs, svc, aid, ts, account_id, cache)
            loop_ids.append(loop_id)
            affected[loop_id] = max(affected.get(loop_id, 0), ts)
            actor = lp.agent_actor(svc, aid)
            executor_pairs.add((loop_id, actor))

            # handoff block -> handoff_initiated event in the span's loop.
            direction = attr(attrs, "handoff.direction")
            if isinstance(direction, str) and direction in lp.HANDOFF_DIRECTIONS:
                payload: dict[str, Any] = {"direction": direction}
                for suffix, pkey in (
                    ("handoff.target_id", "target_id"),
                    ("handoff.reason", "reason"),
                    ("handoff.id", "handoff_id"),
                ):
                    v = attr(attrs, suffix)
                    if v is not None:
                        payload[pkey] = str(v)
                append_loop_event(
                    cur, loop_id, "handoff_initiated", "agent", actor,
                    payload=payload, account_id=account_id, event_time_unix=ts,
                )

            # trovis.loop.close -> agent completed the loop. Without this,
            # every agent-finished task would sit idle until the sweep
            # mislabels it abandoned 48h later.
            close_val = attr(attrs, "loop.close")
            if close_val is not None:
                closes[loop_id] = (actor, str(close_val), ts)

        _insert_span_rows(cur, spans, account_id, loop_ids)

        for loop_id, actor in executor_pairs:
            _upsert_loop_participant(cur, loop_id, "agent", actor, "executor")

        now_sql = "NOW()" if USE_POSTGRES else "CURRENT_TIMESTAMP"
        for loop_id, (actor, value, ts) in closes.items():
            cur.execute(
                f"SELECT closed_at FROM loops WHERE id = {PH}", (loop_id,)
            )
            row = cur.fetchone()
            if row is None or row["closed_at"] is not None:
                continue  # already closed — never a second close event
            payload = {"reason": "completed_by_agent"}
            if value.strip().lower() not in _LOOP_CLOSE_BARE:
                payload["detail"] = value
            append_loop_event(
                cur, loop_id, "loop_closed", "agent", actor,
                payload=payload, account_id=account_id, event_time_unix=ts,
            )
            cur.execute(
                f"UPDATE loops SET closed_at = {now_sql} WHERE id = {PH}",
                (loop_id,),
            )

        for loop_id, batch_max in affected.items():
            cur.execute(
                f"SELECT last_event_unix FROM loops WHERE id = {PH}", (loop_id,)
            )
            row = cur.fetchone()
            prev = int(row["last_event_unix"] or 0) if row else 0
            cur.execute(
                f"UPDATE loops SET last_event_unix = {PH} WHERE id = {PH}",
                (max(prev, batch_max), loop_id),
            )
            recompute_loop_state(cur, loop_id, now_ns=now_ns)

    return len(spans)


def _fetch_loop_stream(cur, loop_id: int, full: bool = False) -> list[dict[str, Any]]:
    """The loop's merged, ordered event stream (loop_events + span-derived
    'activity'), normalized for loops.compute_loop_state.

    full=False (state recompute): spans collapse to at most two activity
    events (MIN and MAX start time) — provably equivalent for the state
    rules, which only test "any non-open event" and the latest timestamp.
    full=True (detail endpoint): one activity event per span.

    Ties sort loop events before span activity (a loop_opened stamped at the
    same ns as its first span comes first), then by id — deterministic on
    both backends.
    """
    lp = _loops_mod()
    keyed: list[tuple[tuple, dict]] = []

    cur.execute(
        "SELECT id, type, actor_type, actor, payload, event_time_unix "
        f"FROM loop_events WHERE loop_id = {PH} ORDER BY event_time_unix, id",
        (loop_id,),
    )
    for r in cur.fetchall():
        ev = lp.normalize_loop_event(dict(r))
        keyed.append(((ev["ts"], 0, r["id"]), ev))

    if full:
        cur.execute(
            "SELECT id, trace_id, span_id, span_name, service_name, agent_id, "
            "start_time_unix, estimated_cost_usd "
            f"FROM spans WHERE loop_id = {PH} ORDER BY start_time_unix, id",
            (loop_id,),
        )
        for r in cur.fetchall():
            ev = lp.activity_event(
                r["start_time_unix"],
                actor=lp.agent_actor(r["service_name"], r["agent_id"] or "main"),
                payload={
                    "span_name": r["span_name"],
                    "trace_id": r["trace_id"],
                    "span_id": r["span_id"],
                    "cost_usd": r["estimated_cost_usd"],
                },
            )
            keyed.append(((ev["ts"], 1, r["id"]), ev))
    else:
        cur.execute(
            "SELECT COUNT(*) AS c, MIN(start_time_unix) AS mn, MAX(start_time_unix) AS mx "
            f"FROM spans WHERE loop_id = {PH}",
            (loop_id,),
        )
        r = cur.fetchone()
        if r and r["c"]:
            keyed.append(((int(r["mn"]), 1, 0), lp.activity_event(r["mn"])))
            if r["mx"] != r["mn"]:
                keyed.append(((int(r["mx"]), 1, 1), lp.activity_event(r["mx"])))

    keyed.sort(key=lambda kv: kv[0])
    return [ev for _, ev in keyed]


def recompute_loop_state(cur, loop_id: int, now_ns: int | None = None) -> str:
    """Recompute cached_state from the event stream and write it if changed.

    Called in-transaction on every ingest that touches the loop. The cache
    can still lag for pure time-based transitions (awaiting_* -> stalled,
    idle -> stalled) since nothing recomputes between events — the sweep job
    covers those within its interval.
    """
    lp = _loops_mod()
    events = _fetch_loop_stream(cur, loop_id)
    state = lp.compute_loop_state(events, now_ns=now_ns)
    cur.execute(
        f"UPDATE loops SET cached_state = {PH} "
        f"WHERE id = {PH} AND cached_state != {PH}",
        (state, loop_id, state),
    )
    return state


def recompute_loop_state_standalone(
    loop_id: int, account_id: int | None = None, now_ns: int | None = None,
) -> str | None:
    """Sweep entry point: recompute one loop's cached_state in its own
    transaction. Returns the computed state, or None if the loop doesn't
    exist under this account."""
    acct_sql, acct_args = _loop_account_clause(account_id)
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(
            f"SELECT id FROM loops WHERE id = {PH} {acct_sql}",
            tuple([loop_id, *acct_args]),
        )
        if cur.fetchone() is None:
            return None
        return recompute_loop_state(cur, loop_id, now_ns=now_ns)


def abandon_loop(loop_id: int, account_id: int | None = None) -> bool:
    """Sweep-driven terminal close for a loop idle past ABANDON_THRESHOLD:
    append a system-attributed loop_closed event (payload.reason='abandoned')
    and set closed_at + cached_state. Idempotent — an already-closed loop is
    left alone (never a second close event)."""
    lp = _loops_mod()
    acct_sql, acct_args = _loop_account_clause(account_id)
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(
            f"SELECT closed_at FROM loops WHERE id = {PH} {acct_sql}",
            tuple([loop_id, *acct_args]),
        )
        row = cur.fetchone()
        if row is None or row["closed_at"] is not None:
            return False
        append_loop_event(
            cur, loop_id, "loop_closed", "system", lp.SYSTEM_ACTOR,
            payload={"reason": "abandoned"}, account_id=account_id,
        )
        now_sql = "NOW()" if USE_POSTGRES else "CURRENT_TIMESTAMP"
        cur.execute(
            f"UPDATE loops SET cached_state = 'abandoned', closed_at = {now_sql} "
            f"WHERE id = {PH}",
            (loop_id,),
        )
        return True


def close_loop(
    loop_id: int, account_id: int | None, user_id: int,
) -> dict[str, Any] | None:
    """Operator close: append a loop_closed event attributed to the user and
    set closed_at. The ONLY user-facing write — it writes an EVENT; it never
    mutates existing events. Idempotent: closing an already-closed loop
    returns it unchanged (no duplicate close event)."""
    with _connect() as conn, _cursor(conn) as cur:
        # Reads scope by the read convention (filter only when account_id is
        # set) so open/dev mode behaves like every other endpoint.
        sql = f"SELECT id, closed_at FROM loops WHERE id = {PH}"
        args: list[Any] = [loop_id]
        if account_id is not None:
            sql += f" AND account_id = {PH}"
            args.append(account_id)
        cur.execute(sql, tuple(args))
        row = cur.fetchone()
        if row is None:
            return None
        if row["closed_at"] is None:
            append_loop_event(
                cur, loop_id, "loop_closed", "human", str(user_id),
                payload={"reason": "closed_by_user"}, account_id=account_id,
            )
            _upsert_loop_participant(cur, loop_id, "human", str(user_id), "reviewer")
            now_sql = "NOW()" if USE_POSTGRES else "CURRENT_TIMESTAMP"
            cur.execute(
                f"UPDATE loops SET cached_state = 'done', closed_at = {now_sql} "
                f"WHERE id = {PH}",
                (loop_id,),
            )
    return get_loop(loop_id, account_id)


# Shared SELECT for loop listings: the read model row + live aggregates.
# Total cost is SUM(spans.estimated_cost_usd) computed here, never stored.
_LOOP_SELECT = """
SELECT l.id, l.account_id, l.external_id, l.service_name, l.agent_id, l.title,
       l.initiated_by_type, l.initiated_by, l.cached_state, l.last_event_unix,
       l.created_at, l.closed_at,
       COALESCE(p.c, 0) AS participant_count,
       COALESCE(e.c, 0) AS loop_event_count,
       COALESCE(sp.c, 0) AS span_count,
       COALESCE(sp.cost, 0) AS total_cost_usd
FROM loops l
LEFT JOIN (SELECT loop_id, COUNT(*) AS c
           FROM loop_participants GROUP BY loop_id) p ON p.loop_id = l.id
LEFT JOIN (SELECT loop_id, COUNT(*) AS c
           FROM loop_events GROUP BY loop_id) e ON e.loop_id = l.id
LEFT JOIN (SELECT loop_id, COUNT(*) AS c,
                  COALESCE(SUM(estimated_cost_usd), 0) AS cost
           FROM spans WHERE loop_id IS NOT NULL GROUP BY loop_id) sp
       ON sp.loop_id = l.id
"""


def _loop_row(r: dict[str, Any]) -> dict[str, Any]:
    d = dict(r)
    loop_event_count = int(d.get("loop_event_count") or 0)
    span_count = int(d.get("span_count") or 0)
    return {
        "id": d["id"],
        "external_id": d.get("external_id"),
        "service_name": d.get("service_name"),
        "agent_id": d.get("agent_id") or "main",
        "title": d.get("title"),
        "initiated_by_type": d.get("initiated_by_type"),
        "initiated_by": d.get("initiated_by"),
        "cached_state": d.get("cached_state"),
        "last_event_unix": d.get("last_event_unix"),
        "created_at": _ts_to_str(d.get("created_at")),
        "closed_at": _ts_to_str(d.get("closed_at")),
        "participant_count": int(d.get("participant_count") or 0),
        "span_count": span_count,
        # "events" in the API = the merged stream (lifecycle + activity).
        "event_count": loop_event_count + span_count,
        "total_cost_usd": round(float(d.get("total_cost_usd") or 0.0), 6),
    }


def get_loops(
    account_id: int | None,
    state: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """List loops for the org, newest first."""
    sql = _LOOP_SELECT + " WHERE 1=1"
    args: list[Any] = []
    if account_id is not None:
        sql += f" AND l.account_id = {PH}"
        args.append(account_id)
    if state:
        sql += f" AND l.cached_state = {PH}"
        args.append(state)
    # id DESC tiebreak: SQLite created_at is second-granularity TEXT.
    sql += f" ORDER BY l.created_at DESC, l.id DESC LIMIT {PH} OFFSET {PH}"
    args.extend([int(limit), int(offset)])
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(sql, tuple(args))
        return [_loop_row(dict(r)) for r in cur.fetchall()]


def get_loop(loop_id: int, account_id: int | None) -> dict[str, Any] | None:
    sql = _LOOP_SELECT + f" WHERE l.id = {PH}"
    args: list[Any] = [loop_id]
    if account_id is not None:
        sql += f" AND l.account_id = {PH}"
        args.append(account_id)
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(sql, tuple(args))
        row = cur.fetchone()
        return _loop_row(dict(row)) if row else None


def get_loop_participants(
    loop_id: int, account_id: int | None,
) -> list[dict[str, Any]] | None:
    """Participants for one loop, or None when the loop isn't visible to
    this account (so the API can 404)."""
    if get_loop(loop_id, account_id) is None:
        return None
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(
            "SELECT participant_type, participant, role, added_at "
            f"FROM loop_participants WHERE loop_id = {PH} ORDER BY id",
            (loop_id,),
        )
        return [
            {
                "participant_type": r["participant_type"],
                "participant": r["participant"],
                "role": r["role"],
                "added_at": _ts_to_str(r["added_at"]),
            }
            for r in cur.fetchall()
        ]


def _resolve_human_name(cur, target_id: str, account_id: int | None) -> str | None:
    """Resolve a handoff target_id to a person's display name, org-scoped.

    For 'to_human' handoffs the ingest contract asks agents to pass the
    teammate's email (or their Trovis user id). Lookup order: numeric ->
    users.id; email -> users.email, then team_members.email. Strictly
    scoped to the account — another org's user never resolves. Returns
    None when nothing matches (the UI falls back to "a human").
    """
    tid = str(target_id or "").strip()
    if not tid:
        return None
    acct_sql, acct_args = _loop_account_clause(account_id)
    if tid.isdigit():
        cur.execute(
            f"SELECT name, email FROM users WHERE id = {PH} {acct_sql}",
            tuple([int(tid), *acct_args]),
        )
        row = cur.fetchone()
        return (row["name"] or row["email"]) if row else None
    if "@" in tid:
        cur.execute(
            f"SELECT name, email FROM users WHERE LOWER(email) = LOWER({PH}) {acct_sql}",
            tuple([tid, *acct_args]),
        )
        row = cur.fetchone()
        if row:
            return row["name"] or row["email"]
        cur.execute(
            f"SELECT name FROM team_members WHERE LOWER(email) = LOWER({PH}) {acct_sql}",
            tuple([tid, *acct_args]),
        )
        row = cur.fetchone()
        return row["name"] if row else None
    return None


def get_loop_stream(
    loop_id: int, account_id: int | None,
) -> list[dict[str, Any]] | None:
    """Full ordered event stream for the detail endpoint (every span as an
    activity event), or None when the loop isn't visible to this account.

    Read-time decoration: to_human handoff_initiated events whose
    payload.target_id resolves to a person in this org gain a
    payload.target_name. Decoration only — the stored loop_events rows are
    never modified (the event record stays append-only).
    """
    if get_loop(loop_id, account_id) is None:
        return None
    with _connect() as conn, _cursor(conn) as cur:
        events = _fetch_loop_stream(cur, loop_id, full=True)
        names: dict[str, str | None] = {}
        for ev in events:
            payload = ev.get("payload") or {}
            if (
                ev.get("type") == "handoff_initiated"
                and payload.get("direction") == "to_human"
                and payload.get("target_id")
            ):
                tid = str(payload["target_id"])
                if tid not in names:
                    names[tid] = _resolve_human_name(cur, tid, account_id)
                if names[tid]:
                    payload["target_name"] = names[tid]
        return events


def get_stalled_loops(
    account_id: int | None, limit: int = 50,
) -> list[dict[str, Any]]:
    """Loops needing attention (stalled, or waiting on a human), oldest
    stall first. stalled_for_s = now - last event timestamp."""
    sql = _LOOP_SELECT + " WHERE l.cached_state IN ('stalled', 'awaiting_human')"
    args: list[Any] = []
    if account_id is not None:
        sql += f" AND l.account_id = {PH}"
        args.append(account_id)
    # COALESCE keeps NULL ordering identical on both backends.
    sql += f" ORDER BY COALESCE(l.last_event_unix, 0) ASC, l.id ASC LIMIT {PH}"
    args.append(int(limit))
    now_ns = time.time_ns()
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(sql, tuple(args))
        out = []
        for r in cur.fetchall():
            row = _loop_row(dict(r))
            last = int(row.get("last_event_unix") or 0)
            row["stalled_for_s"] = max(0, (now_ns - last) // _NS_PER_S) if last else None
            out.append(row)
        return out


def get_open_loops_for_sweep(account_id: int | None) -> list[dict[str, Any]]:
    """Non-terminal loops for one account (strict NULL matching — the sweep
    visits every account plus one NULL pass, and each loop must be visited
    exactly once)."""
    acct_sql, acct_args = _loop_account_clause(account_id)
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(
            "SELECT id, cached_state, last_event_unix FROM loops "
            f"WHERE cached_state NOT IN ('done', 'abandoned') {acct_sql}",
            tuple(acct_args),
        )
        return [dict(r) for r in cur.fetchall()]


def save_description(
    service_name: str,
    description: str,
    span_count_analyzed: int,
    account_id: int | None = None,
    agent_id: str | None = None,
    description_long: str | None = None,
) -> None:
    """Persist a newly generated description (append-only — history kept).

    `description` is the short declarative line; `description_long` is the
    optional 2-3 sentence context. `agent_id` scopes the description to one
    sub-agent within an instance. Passing None defaults to 'main' — pre-
    multi-agent descriptions were always for the lone 'main' agent, so this
    keeps backwards-compat for callers that don't pass agent_id.
    """
    sql = f"""
        INSERT INTO descriptions (service_name, agent_id, description, description_long, span_count_analyzed, account_id)
        VALUES ({PH}, {PH}, {PH}, {PH}, {PH}, {PH})
    """
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(
            sql,
            (
                service_name,
                agent_id or "main",
                description,
                description_long,
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
    # Day/week thresholds for the windowed cost columns (nanoseconds).
    # "Today" is the UTC calendar day (since 00:00 UTC), matching the 30-day
    # chart + month-to-date (both UTC-bucketed) and the providers' billing day,
    # so "Today" equals the last point of the trend chart and lines up with the
    # console. "7d" stays a rolling 7-day window.
    from time import time as _time

    _now_ns = int(_time() * 1_000_000_000)
    _day_ns = 24 * 60 * 60 * 1_000_000_000
    _utc_midnight = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    today_ns = int(_utc_midnight.timestamp() * 1_000_000_000)
    week_ns = _now_ns - 7 * _day_ns

    agg_sql = f"""
        SELECT
            service_name,
            COALESCE(agent_id, 'main')                       AS agent_id,
            COUNT(*)                                         AS span_count,
            SUM(CASE WHEN status_code = 2 THEN 1 ELSE 0 END) AS error_count,
            AVG((end_time_unix - start_time_unix) / 1000000.0) AS avg_duration_ms,
            MIN(CASE WHEN start_time_unix >= {_FIRST_SEEN_FLOOR_NS} THEN start_time_unix END) AS first_seen_ns,
            MAX(start_time_unix)                             AS last_seen_ns,
            SUM(total_tokens)                                AS total_tokens,
            SUM(estimated_cost_usd)                          AS estimated_cost_usd,
            SUM(CASE WHEN start_time_unix >= {PH} THEN estimated_cost_usd ELSE 0 END) AS cost_today,
            SUM(CASE WHEN start_time_unix >= {PH} THEN estimated_cost_usd ELSE 0 END) AS cost_7d,
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
    # cost_today threshold (1) + cost_7d threshold (1) come first (they're
    # in the SELECT, always present), THEN the account-scope placeholders
    # when account_id is set: desc_filter (1) + reg_filter (1) +
    # dn_filter (1) + 3× own_filter + sample_acct_filter (1) +
    # span_filter (1) = 8.
    if account_id is not None:
        agg_args = (today_ns, week_ns) + (account_id,) * 8
    else:
        agg_args = (today_ns, week_ns)

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

    # View-lock state under the account's plan (telemetry is never gated).
    lock = get_locked_state(account_id)
    locked_keys = lock["locked"]

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
                # Token usage + cost. None/0 when this agent never
                # reported usage data.
                "total_tokens": int(row["total_tokens"] or 0),
                "estimated_cost_usd": round(float(row["estimated_cost_usd"] or 0.0), 6),
                "cost_today": round(float(row["cost_today"] or 0.0), 6),
                "cost_7d": round(float(row["cost_7d"] or 0.0), 6),
                # View-locked when this agent's INSTANCE is beyond the plan's
                # instance limit (sub-agents inherit their instance's lock).
                # Telemetry is still fully recorded regardless.
                "locked": sn in locked_keys,
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
                    # Cost rollups across all sub-agents in the instance.
                    "total_tokens": 0,
                    "estimated_cost_usd": 0.0,
                    "cost_today": 0.0,
                    "cost_7d": 0.0,
                    # Set in the finalize loop from the sub-agents' locked flags.
                    "locked": False,
                    "locked_count": 0,
                }
            g = groups[sn]
            g["agents"].append(agent_record)
            g["total_spans"] += agent_record["span_count"]
            g["total_errors"] += agent_record["error_count"]
            g["total_tokens"] += agent_record["total_tokens"]
            g["estimated_cost_usd"] += agent_record["estimated_cost_usd"]
            g["cost_today"] += agent_record["cost_today"]
            g["cost_7d"] += agent_record["cost_7d"]
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
            # Tidy float accumulation noise on the cost rollups.
            g["estimated_cost_usd"] = round(g["estimated_cost_usd"], 6)
            g["cost_today"] = round(g["cost_today"], 6)
            g["cost_7d"] = round(g["cost_7d"], 6)
            # The instance card is locked only when every sub-agent is locked;
            # locked_count drives the "N recording" hint.
            g["locked_count"] = sum(1 for a in g["agents"] if a["locked"])
            g["locked"] = bool(g["agents"]) and all(a["locked"] for a in g["agents"])

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


# Span names that mark a record as a "system" event (no real exchange).
_SYSTEM_SPAN_NAMES = {"agent_registration", "heartbeat"}


def _fmt_dur_ns(ns: int | None) -> str:
    """Human duration from a nanosecond delta: 38µs / 51ms / 8.85s."""
    if not ns or ns < 0:
        return "0ms"
    if ns < 1_000_000:  # < 1ms
        return f"{int(ns / 1000)}µs"
    if ns < 1_000_000_000:  # < 1s
        return f"{int(ns / 1_000_000)}ms"
    return f"{ns / 1_000_000_000:.2f}s"


def _clean_msg(v: Any) -> str | None:
    """Best-effort clean human text from a captured message/response value.

    Captured content can arrive wrapped: an OpenAI `Response(...)` repr, a JSON
    envelope (`{"role":...,"content":...}` or a list of message dicts), or plain
    text. Pull the readable text out; return None when there's nothing usable so
    the caller can fall back to a system record. Never raises."""
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    # Unwrap an OpenAI Responses/Generation repr: Response(... output_text='...')
    if "Response(" in s[:40] or "output_text=" in s or "ResponseOutput" in s:
        m = re.search(r"(?:output_text|text|content)=['\"](.+?)['\"](?:[,)\]]|$)", s, re.S)
        if m:
            s = m.group(1)
    # JSON envelope: dict with content/text, or a list of message dicts.
    if s[:1] in "{[":
        try:
            parsed = json.loads(s)
        except (ValueError, TypeError):
            parsed = None
        text = _text_from_json(parsed)
        if text:
            s = text
    # Decode escaped sequences left behind by a repr/JSON unwrap.
    s = s.replace("\\n", "\n").replace('\\"', '"').strip()
    return s or None


def _text_from_json(parsed: Any) -> str | None:
    """Pull readable text from a parsed JSON message / list of messages."""
    if isinstance(parsed, str):
        return parsed.strip() or None
    if isinstance(parsed, dict):
        for k in ("content", "text", "output_text", "message"):
            val = parsed.get(k)
            if isinstance(val, str) and val.strip():
                return val.strip()
            if isinstance(val, (list, dict)):
                inner = _text_from_json(val)
                if inner:
                    return inner
        return None
    if isinstance(parsed, list):
        parts = [_text_from_json(x) for x in parsed]
        parts = [p for p in parts if p]
        return "\n".join(parts) if parts else None
    return None


def _extract_exchange(span_attrs: list[dict[str, Any]]) -> dict[str, str] | None:
    """From a trace's spans (parsed attribute dicts, in time order), pull the
    user prompt and agent response as clean text. Returns {"user","agent"} or
    None when neither side is recoverable (→ a system record)."""
    user = None
    agent = None
    for attrs in span_attrs:
        if user is None:
            user = _clean_msg(attr(attrs, "message.content"))
        if agent is None:
            agent = _clean_msg(
                attr(attrs, "response.content") or attr(attrs, "tool.result")
            )
    if user is None and agent is None:
        return None
    return {"user": user or "", "agent": agent or ""}


def count_agent_records(
    service_name: str,
    account_id: int | None = None,
    agent_id: str | None = None,
) -> int:
    """Total record count for an agent = distinct trace_ids. Used to prove a
    locked agent's data exists ("N records recorded since …") without exposing
    any of it."""
    acct = f"AND account_id = {PH}" if account_id is not None else ""
    agent = f"AND COALESCE(agent_id, 'main') = {PH}" if agent_id is not None else ""
    sql = f"""
        SELECT COUNT(DISTINCT trace_id) AS n
        FROM spans
        WHERE service_name = {PH} {acct} {agent}
    """
    args: list[Any] = [service_name]
    if account_id is not None:
        args.append(account_id)
    if agent_id is not None:
        args.append(agent_id)
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(sql, tuple(args))
        row = cur.fetchone()
    return int(row["n"]) if row else 0


def get_agent_records(
    service_name: str,
    account_id: int | None = None,
    agent_id: str | None = None,
    limit: int = 20,
    before_ns: int | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """One "record" = one interaction = all spans sharing a trace_id, newest
    first. Returns (records, next_cursor). Cursor is the record-start ns of the
    last row; pass it back as `before_ns` to page. Pure DB — the plain-English
    `summary` is generated + cached by the caller (keyed by the immutable
    record id = trace_id)."""
    limit = max(1, min(100, int(limit)))
    acct = f"AND account_id = {PH}" if account_id is not None else ""
    agent = f"AND COALESCE(agent_id, 'main') = {PH}" if agent_id is not None else ""
    having = f"HAVING MIN(start_time_unix) < {PH}" if before_ns is not None else ""

    page_sql = f"""
        SELECT trace_id,
               MIN(start_time_unix)                            AS rec_start_ns,
               MAX(end_time_unix)                              AS rec_end_ns,
               SUM(total_tokens)                               AS tokens,
               SUM(estimated_cost_usd)                         AS cost,
               SUM(CASE WHEN status_code = 2 THEN 1 ELSE 0 END) AS error_spans,
               COUNT(*)                                        AS span_count
        FROM spans
        WHERE service_name = {PH} {acct} {agent}
        GROUP BY trace_id
        {having}
        ORDER BY rec_start_ns DESC
        LIMIT {PH}
    """
    page_args: list[Any] = [service_name]
    if account_id is not None:
        page_args.append(account_id)
    if agent_id is not None:
        page_args.append(agent_id)
    if before_ns is not None:
        page_args.append(int(before_ns))
    page_args.append(limit)

    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(page_sql, tuple(page_args))
        page = cur.fetchall()
        if not page:
            return [], None
        trace_ids = [r["trace_id"] for r in page]

        # Fetch every span for this page's traces in one round-trip.
        in_ph = ", ".join([PH] * len(trace_ids))
        span_sql = f"""
            SELECT trace_id, span_name, start_time_unix, end_time_unix,
                   status_code, attributes
            FROM spans
            WHERE service_name = {PH} {acct} {agent}
              AND trace_id IN ({in_ph})
            ORDER BY start_time_unix ASC
        """
        span_args: list[Any] = [service_name]
        if account_id is not None:
            span_args.append(account_id)
        if agent_id is not None:
            span_args.append(agent_id)
        span_args.extend(trace_ids)
        cur.execute(span_sql, tuple(span_args))
        span_rows = cur.fetchall()

    # Group spans by trace.
    by_trace: dict[str, list[Any]] = {}
    for sr in span_rows:
        by_trace.setdefault(sr["trace_id"], []).append(sr)

    records: list[dict[str, Any]] = []
    for prow in page:
        tid = prow["trace_id"]
        srows = by_trace.get(tid, [])
        span_list = []
        attrs_list = []
        only_system = True
        for sr in srows:
            try:
                a = json.loads(sr["attributes"] or "{}")
            except (ValueError, TypeError):
                a = {}
            attrs_list.append(a if isinstance(a, dict) else {})
            if sr["span_name"] not in _SYSTEM_SPAN_NAMES:
                only_system = False
            span_list.append(
                {
                    "operation": sr["span_name"],
                    "duration": _fmt_dur_ns(
                        (sr["end_time_unix"] or 0) - (sr["start_time_unix"] or 0)
                    ),
                    "status": "error" if sr["status_code"] == 2 else "ok",
                }
            )
        exchange = None if only_system else _extract_exchange(attrs_list)
        is_registration = only_system or exchange is None
        rec_start = prow["rec_start_ns"]
        records.append(
            {
                "id": tid,
                "time": _ns_to_iso(rec_start),
                "cost_usd": (
                    round(float(prow["cost"]), 6) if prow["cost"] is not None else None
                ),
                "duration_ms": max(
                    0.0, ((prow["rec_end_ns"] or 0) - (rec_start or 0)) / 1_000_000.0
                ),
                "tokens": int(prow["tokens"] or 0),
                "error": (prow["error_spans"] or 0) > 0,
                "is_registration": bool(is_registration),
                "exchange": exchange,
                "spans": span_list,
                "_start_ns": rec_start,  # internal: cursor source
            }
        )

    next_cursor = str(records[-1]["_start_ns"]) if len(records) == limit else None
    for r in records:
        r.pop("_start_ns", None)
    return records, next_cursor


def get_agent_record_stats(
    service_name: str,
    account_id: int | None = None,
    agent_id: str | None = None,
) -> dict[str, Any]:
    """Record-level stats for the detail page's status pill + This Week strip:
    total records, records in the last 7d, the latest record's time + whether
    it errored + its dominant operation, and the agent's typical run cadence
    (median gap between recent records; None when <5 records)."""
    acct = f"AND account_id = {PH}" if account_id is not None else ""
    agent = f"AND COALESCE(agent_id, 'main') = {PH}" if agent_id is not None else ""
    base_args: list[Any] = [service_name]
    if account_id is not None:
        base_args.append(account_id)
    if agent_id is not None:
        base_args.append(agent_id)

    recent_sql = f"""
        SELECT trace_id,
               MIN(start_time_unix)                            AS rec_start_ns,
               SUM(CASE WHEN status_code = 2 THEN 1 ELSE 0 END) AS error_spans
        FROM spans
        WHERE service_name = {PH} {acct} {agent}
          AND span_name NOT IN ('agent_registration', 'heartbeat')
        GROUP BY trace_id
        ORDER BY rec_start_ns DESC
        LIMIT 50
    """
    total_sql = f"""
        SELECT COUNT(DISTINCT trace_id) AS n
        FROM spans
        WHERE service_name = {PH} {acct} {agent}
          AND span_name NOT IN ('agent_registration', 'heartbeat')
    """
    from time import time as _time

    week_ns = int(_time() * 1_000_000_000) - 7 * 24 * 60 * 60 * 1_000_000_000
    week_sql = f"""
        SELECT COUNT(DISTINCT trace_id) AS n
        FROM spans
        WHERE service_name = {PH} {acct} {agent}
          AND span_name NOT IN ('agent_registration', 'heartbeat')
          AND start_time_unix >= {PH}
    """
    last_err_sql = f"""
        SELECT span_name
        FROM spans
        WHERE service_name = {PH} {acct} {agent}
          AND status_code = 2
        ORDER BY start_time_unix DESC
        LIMIT 1
    """
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(recent_sql, tuple(base_args))
        recent = cur.fetchall()
        cur.execute(total_sql, tuple(base_args))
        total = cur.fetchone()
        cur.execute(week_sql, tuple([*base_args, week_ns]))
        week = cur.fetchone()
        cur.execute(last_err_sql, tuple(base_args))
        last_err = cur.fetchone()

    starts = [r["rec_start_ns"] for r in recent if r["rec_start_ns"]]
    last_ns = starts[0] if starts else None
    last_errored = bool(recent[0]["error_spans"]) if recent else False
    # Median gap between consecutive recent records (newest-first → diffs).
    cadence_s = None
    if len(starts) >= 5:
        gaps = sorted(starts[i] - starts[i + 1] for i in range(len(starts) - 1))
        if gaps:
            mid = gaps[len(gaps) // 2] / 1_000_000_000.0
            cadence_s = mid if mid > 0 else None
    return {
        "total_records": int(total["n"]) if total else 0,
        "records_7d": int(week["n"]) if week else 0,
        "last_record_ns": last_ns,
        "last_record_errored": last_errored,
        "last_error_op": last_err["span_name"] if last_err else None,
        "cadence_seconds": cadence_s,
    }


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
            MIN(CASE WHEN start_time_unix >= {_FIRST_SEEN_FLOOR_NS} THEN start_time_unix END) AS first_seen_ns,
            MAX(start_time_unix)                           AS last_seen_ns,
            SUM(total_tokens)                              AS total_tokens,
            SUM(estimated_cost_usd)                        AS estimated_cost_usd
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
        SELECT description, description_long
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
        "description_long": (
            desc_row["description_long"] if desc_row else None
        ),
        "platform": _detect_platform(
            sample_row["resource_attributes"] if sample_row else None
        ),
        "display_name": dn_row["display_name"] if dn_row else None,
        "owner_id": owner_row["team_member_id"] if owner_row else None,
        "owner_name": owner_row["owner_name"] if owner_row else None,
        "owner_role": owner_row["owner_role"] if owner_row else None,
        "total_tokens": int(row["total_tokens"] or 0),
        "estimated_cost_usd": round(float(row["estimated_cost_usd"] or 0.0), 6),
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
        SELECT service_name, agent_id, description, description_long, span_count_analyzed, generated_at
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
        "description_long": row["description_long"],
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


# ---------------------------------------------------------------------------
# Waitlist (public marketing-site funnel — no auth, no account scoping)
# ---------------------------------------------------------------------------


def add_waitlist_signup(
    email: str,
    source: str | None = None,
    runtime_interest: str | None = None,
) -> str:
    """Record a waitlist signup. Email is lowercased + trimmed before insert.

    Idempotent: a repeat email is success, not an error — we swallow the
    UNIQUE violation and report it. Returns "joined" for a new signup or
    "already_joined" when the email is already on the list.
    """
    clean_email = (email or "").strip().lower()
    if not clean_email:
        raise ValueError("email is required")
    clean_source = (source or "").strip() or None
    clean_interest = (runtime_interest or "").strip() or None

    if USE_POSTGRES:
        sql = """
            INSERT INTO waitlist_signups (email, source, runtime_interest)
            VALUES (%s, %s, %s)
        """
        try:
            with _connect() as conn, _cursor(conn) as cur:
                cur.execute(sql, (clean_email, clean_source, clean_interest))
        except psycopg2.errors.UniqueViolation:
            return "already_joined"
    else:
        sql = """
            INSERT INTO waitlist_signups (email, source, runtime_interest)
            VALUES (?, ?, ?)
        """
        try:
            with _connect() as conn, _cursor(conn) as cur:
                cur.execute(sql, (clean_email, clean_source, clean_interest))
        except sqlite3.IntegrityError as e:
            if "UNIQUE" in str(e):
                return "already_joined"
            raise

    return "joined"


def get_waitlist_count() -> int:
    """Return the total number of waitlist signups."""
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute("SELECT COUNT(*) AS n FROM waitlist_signups")
        row = cur.fetchone()
    return int(row["n"])


def delete_waitlist_signup(email: str) -> int:
    """Remove a waitlist signup by email (lowercased + trimmed to match how
    add_waitlist_signup stores it). Returns rows deleted (0 or 1). Operator
    tool — e.g. to clear a test/bogus signup so it doesn't skew the count."""
    clean = (email or "").strip().lower()
    if not clean:
        return 0
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(f"DELETE FROM waitlist_signups WHERE email = {PH}", (clean,))
        return cur.rowcount or 0


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


def team_member_in_account(account_id: int | None, team_member_id: int | None) -> bool:
    """True if team_member_id belongs to this account. Used to reject a
    cross-tenant team_member_id before it's stored (owner/workflow), which would
    otherwise leak that member's name/email/role through the read-side joins."""
    if account_id is None or team_member_id is None:
        return False
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(
            f"SELECT 1 FROM team_members WHERE id = {PH} AND account_id = {PH}",
            (team_member_id, account_id),
        )
        return cur.fetchone() is not None


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
# Workflows — named, ordered step sequences (agent + human)
# ---------------------------------------------------------------------------

# Fields a workflow_steps row carries beyond id/workflow_id/step_order.
_STEP_FIELDS = (
    "step_type",
    "label",
    "description",
    "agent_service_name",
    "agent_id",
    "team_member_id",
    "operation",
    "duration_estimate_ms",
    "inferred_from",
    "config",
    "pos_x",
    "pos_y",
    "node_width",
    "node_height",
)

_VALID_STEP_TYPES = {"trigger", "agent", "human", "decision", "output"}


def _row_to_workflow(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "account_id": row["account_id"],
        "name": row["name"],
        "description": row["description"],
        "agent_service_name": row["agent_service_name"],
        "agent_id": row["agent_id"],
        "method": _row_get(row, "method", "generate") or "generate",
        "source_description": _row_get(row, "source_description"),
        "loop_count": int(_row_get(row, "loop_count", 0) or 0),
        "created_at": _ts_to_str(row["created_at"]),
        "updated_at": _ts_to_str(row["updated_at"]),
    }


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    """Read a column that may or may not be present in the row (sqlite3.Row
    raises IndexError, RealDictRow raises KeyError for absent keys)."""
    try:
        return row[key]
    except (KeyError, IndexError):
        return default


def _row_to_step(row: Any) -> dict[str, Any]:
    raw_config = row["config"]
    config: Any = None
    if raw_config:
        try:
            config = json.loads(raw_config)
        except (TypeError, ValueError):
            config = None
    return {
        "id": row["id"],
        "workflow_id": row["workflow_id"],
        "step_order": row["step_order"],
        "step_type": row["step_type"],
        "label": row["label"],
        "description": row["description"],
        "agent_service_name": row["agent_service_name"],
        "agent_id": row["agent_id"],
        "team_member_id": row["team_member_id"],
        # Only present when the query LEFT JOINed team_members (get_workflow).
        "team_member_name": _row_get(row, "team_member_name"),
        "operation": row["operation"],
        "duration_estimate_ms": row["duration_estimate_ms"],
        "inferred_from": row["inferred_from"],
        "config": config,
        # Spatial canvas position + size (default for pre-redesign rows).
        "pos_x": _row_get(row, "pos_x", 0.0) or 0.0,
        "pos_y": _row_get(row, "pos_y", 0.0) or 0.0,
        "node_width": _row_get(row, "node_width", 170.0) or 170.0,
        "node_height": _row_get(row, "node_height", 72.0) or 72.0,
    }


def _clean_step_values(step: dict[str, Any]) -> dict[str, Any]:
    """Pull the persistable fields out of an arbitrary step dict, applying
    light validation/normalization. `config` is JSON-encoded for storage."""
    step_type = str(step.get("step_type") or "agent").strip().lower()
    if step_type not in _VALID_STEP_TYPES:
        step_type = "agent"
    duration = step.get("duration_estimate_ms")
    try:
        duration = int(duration) if duration is not None else None
    except (TypeError, ValueError):
        duration = None
    config = step.get("config")
    config_json = (
        json.dumps(config) if isinstance(config, (dict, list)) else None
    )
    member_id = step.get("team_member_id")
    try:
        member_id = int(member_id) if member_id not in (None, "") else None
    except (TypeError, ValueError):
        member_id = None

    def _num(v: Any, default: float) -> float:
        try:
            return float(v) if v is not None else default
        except (TypeError, ValueError):
            return default

    return {
        "step_type": step_type,
        "label": str(step.get("label") or "").strip() or "Untitled step",
        "description": (step.get("description") or None),
        "agent_service_name": (step.get("agent_service_name") or None),
        "agent_id": (step.get("agent_id") or None),
        "team_member_id": member_id,
        "operation": (step.get("operation") or None),
        "duration_estimate_ms": duration,
        "inferred_from": (step.get("inferred_from") or "manual"),
        "config": config_json,
        "pos_x": _num(step.get("pos_x"), 0.0),
        "pos_y": _num(step.get("pos_y"), 0.0),
        "node_width": _num(step.get("node_width"), 170.0),
        "node_height": _num(step.get("node_height"), 72.0),
    }


def _touch_workflow(cur, workflow_id: int) -> None:
    """Bump a workflow's updated_at. Called after step mutations."""
    now_sql = "NOW()" if USE_POSTGRES else "CURRENT_TIMESTAMP"
    cur.execute(
        f"UPDATE workflows SET updated_at = {now_sql} WHERE id = {PH}",
        (workflow_id,),
    )


def create_workflow(
    account_id: int | None,
    name: str,
    description: str | None = None,
    agent_service_name: str | None = None,
    agent_id: str | None = "main",
    method: str = "generate",
    source_description: str | None = None,
) -> dict[str, Any]:
    """Insert a new (empty) workflow and return the row."""
    clean_name = (name or "").strip()
    if not clean_name:
        raise ValueError("name is required")
    cols = (
        "(account_id, name, description, agent_service_name, agent_id, "
        "method, source_description)"
    )
    vals = (
        account_id,
        clean_name,
        description or None,
        agent_service_name or None,
        agent_id or "main",
        method or "generate",
        source_description or None,
    )
    ph = ", ".join([PH] * len(vals))
    with _connect() as conn, _cursor(conn) as cur:
        if USE_POSTGRES:
            cur.execute(
                f"INSERT INTO workflows {cols} VALUES ({ph}) RETURNING *", vals
            )
            row = cur.fetchone()
        else:
            cur.execute(f"INSERT INTO workflows {cols} VALUES ({ph})", vals)
            new_id = cur.lastrowid
            cur.execute("SELECT * FROM workflows WHERE id = ?", (new_id,))
            row = cur.fetchone()
    return _row_to_workflow(row)


def get_workflows(account_id: int | None) -> list[dict[str, Any]]:
    """List workflows for an account (most recently updated first), each
    with a `step_count`. Steps themselves are not loaded here."""
    account_filter = (
        f"WHERE w.account_id = {PH}"
        if account_id is not None
        else "WHERE w.account_id IS NULL"
    )
    sql = f"""
        SELECT w.*,
               (SELECT COUNT(*) FROM workflow_steps s WHERE s.workflow_id = w.id) AS step_count,
               (SELECT COUNT(*) FROM workflow_participants p WHERE p.workflow_id = w.id) AS participant_count,
               (SELECT COUNT(*) FROM workflow_edges e
                  JOIN workflow_steps fs ON fs.id = e.from_step_id
                  JOIN workflow_steps ts ON ts.id = e.to_step_id
                  WHERE e.workflow_id = w.id AND ts.step_order < fs.step_order) AS loop_count
        FROM workflows w
        {account_filter}
        ORDER BY w.updated_at DESC, w.id DESC
    """
    args: tuple[Any, ...] = (account_id,) if account_id is not None else ()
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(sql, args)
        rows = cur.fetchall()
        out = []
        for r in rows:
            wf = _row_to_workflow(r)
            wf["step_count"] = int(r["step_count"] or 0)
            wf["participant_count"] = int(_row_get(r, "participant_count", 0) or 0)
            wf["steps"] = []
            # Participant pills on the list card (cheap — small N per workflow).
            cur.execute(
                "SELECT * FROM workflow_participants "
                f"WHERE workflow_id = {PH} ORDER BY id ASC",
                (wf["id"],),
            )
            wf["participants"] = [_row_to_participant(p) for p in cur.fetchall()]
            out.append(wf)
    return out


def get_workflow(
    workflow_id: int, account_id: int | None
) -> dict[str, Any] | None:
    """Fetch one workflow (account-scoped) with all steps ordered by
    step_order. Returns None when not found / not owned."""
    account_clause = (
        f"AND account_id = {PH}"
        if account_id is not None
        else "AND account_id IS NULL"
    )
    wf_args: tuple[Any, ...] = (workflow_id,)
    if account_id is not None:
        wf_args = (workflow_id, account_id)
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(
            f"SELECT * FROM workflows WHERE id = {PH} {account_clause}",
            wf_args,
        )
        row = cur.fetchone()
        if row is None:
            return None
        wf = _row_to_workflow(row)
        cur.execute(
            "SELECT s.*, m.name AS team_member_name "
            "FROM workflow_steps s "
            "LEFT JOIN team_members m ON m.id = s.team_member_id "
            f"WHERE s.workflow_id = {PH} ORDER BY s.step_order ASC, s.id ASC",
            (workflow_id,),
        )
        wf["steps"] = [_row_to_step(s) for s in cur.fetchall()]
        cur.execute(
            "SELECT * FROM workflow_participants "
            f"WHERE workflow_id = {PH} ORDER BY id ASC",
            (workflow_id,),
        )
        wf["participants"] = [_row_to_participant(p) for p in cur.fetchall()]
        cur.execute(
            "SELECT * FROM workflow_edges "
            f"WHERE workflow_id = {PH} ORDER BY edge_order ASC, id ASC",
            (workflow_id,),
        )
        wf["edges"] = [_row_to_edge(e) for e in cur.fetchall()]
    wf["step_count"] = len(wf["steps"])
    wf["participant_count"] = len(wf["participants"])
    return wf


def update_workflow(
    workflow_id: int,
    account_id: int | None,
    name: str | None = None,
    description: str | None = None,
) -> dict[str, Any] | None:
    """Update a workflow's name/description (only provided fields). Returns
    the updated workflow (with steps) or None when not owned."""
    sets = []
    args: list[Any] = []
    if name is not None:
        sets.append(f"name = {PH}")
        args.append(name.strip())
    if description is not None:
        sets.append(f"description = {PH}")
        args.append(description or None)
    now_sql = "NOW()" if USE_POSTGRES else "CURRENT_TIMESTAMP"
    sets.append(f"updated_at = {now_sql}")
    account_clause = (
        f"AND account_id = {PH}"
        if account_id is not None
        else "AND account_id IS NULL"
    )
    args.append(workflow_id)
    if account_id is not None:
        args.append(account_id)
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(
            f"UPDATE workflows SET {', '.join(sets)} WHERE id = {PH} {account_clause}",
            tuple(args),
        )
        if cur.rowcount == 0:
            return None
    return get_workflow(workflow_id, account_id)


def delete_workflow(workflow_id: int, account_id: int | None) -> bool:
    """Delete a workflow and its steps (steps first — SQLite doesn't
    enforce the cascade). Account-scoped. Returns True when removed."""
    account_clause = (
        f"AND account_id = {PH}"
        if account_id is not None
        else "AND account_id IS NULL"
    )
    with _connect() as conn, _cursor(conn) as cur:
        # Ownership check first so we don't drop another tenant's steps.
        cur.execute(
            f"SELECT 1 FROM workflows WHERE id = {PH} {account_clause}",
            (workflow_id,) if account_id is None else (workflow_id, account_id),
        )
        if cur.fetchone() is None:
            return False
        # Children first — SQLite doesn't enforce the FK cascade. Edges
        # reference steps, so drop edges + participants before steps.
        cur.execute(
            f"DELETE FROM workflow_edges WHERE workflow_id = {PH}", (workflow_id,)
        )
        cur.execute(
            f"DELETE FROM workflow_participants WHERE workflow_id = {PH}",
            (workflow_id,),
        )
        cur.execute(
            f"DELETE FROM workflow_steps WHERE workflow_id = {PH}", (workflow_id,)
        )
        cur.execute(
            f"DELETE FROM workflows WHERE id = {PH} {account_clause}",
            (workflow_id,) if account_id is None else (workflow_id, account_id),
        )
        return cur.rowcount > 0


def add_workflow_step(
    workflow_id: int, step_data: dict[str, Any]
) -> dict[str, Any]:
    """Append a step to a workflow (step_order = current max + 1 unless an
    explicit step_order is given). Ownership must be checked by the caller."""
    clean = _clean_step_values(step_data)
    with _connect() as conn, _cursor(conn) as cur:
        order = step_data.get("step_order")
        if order is None:
            cur.execute(
                f"SELECT COALESCE(MAX(step_order), -1) + 1 AS n FROM workflow_steps WHERE workflow_id = {PH}",
                (workflow_id,),
            )
            order = int(cur.fetchone()["n"])
        cols = "(workflow_id, step_order, " + ", ".join(_STEP_FIELDS) + ")"
        ph = ", ".join([PH] * (2 + len(_STEP_FIELDS)))
        vals = (workflow_id, order, *(clean[f] for f in _STEP_FIELDS))
        if USE_POSTGRES:
            cur.execute(
                f"INSERT INTO workflow_steps {cols} VALUES ({ph}) RETURNING *",
                vals,
            )
            row = cur.fetchone()
        else:
            cur.execute(f"INSERT INTO workflow_steps {cols} VALUES ({ph})", vals)
            new_id = cur.lastrowid
            cur.execute(
                "SELECT * FROM workflow_steps WHERE id = ?",
                (new_id,),
            )
            row = cur.fetchone()
        _touch_workflow(cur, workflow_id)
    return _row_to_step(row)


def update_workflow_step(
    step_id: int, workflow_id: int, step_data: dict[str, Any]
) -> dict[str, Any] | None:
    """Patch a step (only the fields present in step_data), scoped to its
    workflow. Returns the updated step or None when not found."""
    sets = []
    args: list[Any] = []
    cleaned = _clean_step_values({**step_data}) if step_data else {}
    for field in _STEP_FIELDS:
        if field in step_data:
            sets.append(f"{field} = {PH}")
            args.append(cleaned[field])
    if "step_order" in step_data and step_data["step_order"] is not None:
        sets.append(f"step_order = {PH}")
        args.append(int(step_data["step_order"]))
    if not sets:
        # Nothing to change — just return the current row.
        with _connect() as conn, _cursor(conn) as cur:
            cur.execute(
                f"SELECT * FROM workflow_steps WHERE id = {PH} AND workflow_id = {PH}",
                (step_id, workflow_id),
            )
            row = cur.fetchone()
        return _row_to_step(row) if row else None
    args.extend([step_id, workflow_id])
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(
            f"UPDATE workflow_steps SET {', '.join(sets)} "
            f"WHERE id = {PH} AND workflow_id = {PH}",
            tuple(args),
        )
        if cur.rowcount == 0:
            return None
        _touch_workflow(cur, workflow_id)
        cur.execute(
            f"SELECT * FROM workflow_steps WHERE id = {PH} AND workflow_id = {PH}",
            (step_id, workflow_id),
        )
        row = cur.fetchone()
    return _row_to_step(row) if row else None


def delete_workflow_step(step_id: int, workflow_id: int) -> bool:
    """Delete one step from a workflow, plus any edges that reference it.
    Returns True when removed. (FK cascade isn't enabled — PRAGMA
    foreign_keys is off — so we remove the dependent edges by hand.)"""
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(
            f"DELETE FROM workflow_steps WHERE id = {PH} AND workflow_id = {PH}",
            (step_id, workflow_id),
        )
        removed = cur.rowcount > 0
        if removed:
            cur.execute(
                f"DELETE FROM workflow_edges WHERE workflow_id = {PH} "
                f"AND (from_step_id = {PH} OR to_step_id = {PH})",
                (workflow_id, step_id, step_id),
            )
            _touch_workflow(cur, workflow_id)
    return removed


def reorder_workflow_steps(
    workflow_id: int, step_ids_in_order: list[int]
) -> None:
    """Set step_order to match the given id list. Only ids belonging to the
    workflow are touched; unknown ids are ignored."""
    with _connect() as conn, _cursor(conn) as cur:
        for order, sid in enumerate(step_ids_in_order):
            cur.execute(
                f"UPDATE workflow_steps SET step_order = {PH} "
                f"WHERE id = {PH} AND workflow_id = {PH}",
                (order, int(sid), workflow_id),
            )
        _touch_workflow(cur, workflow_id)


def _replace_workflow_steps(
    workflow_id: int, steps: list[dict[str, Any]]
) -> None:
    """Drop all steps for a workflow and insert the given list in order.
    Used by generate / regenerate."""
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(
            f"DELETE FROM workflow_steps WHERE workflow_id = {PH}", (workflow_id,)
        )
        cols = "(workflow_id, step_order, " + ", ".join(_STEP_FIELDS) + ")"
        ph = ", ".join([PH] * (2 + len(_STEP_FIELDS)))
        for order, step in enumerate(steps):
            clean = _clean_step_values(step)
            cur.execute(
                f"INSERT INTO workflow_steps {cols} VALUES ({ph})",
                (workflow_id, order, *(clean[f] for f in _STEP_FIELDS)),
            )
        _touch_workflow(cur, workflow_id)


# ---------------------------------------------------------------------------
# Workflow graph: participants, edges, positions
# ---------------------------------------------------------------------------


def _row_to_participant(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "workflow_id": row["workflow_id"],
        "type": row["participant_type"],
        "agent_service_name": _row_get(row, "agent_service_name"),
        "agent_id": _row_get(row, "agent_id"),
        "role_name": _row_get(row, "role_name"),
        "team_member_id": _row_get(row, "team_member_id"),
    }


def _row_to_edge(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "workflow_id": row["workflow_id"],
        "from_step_id": row["from_step_id"],
        "to_step_id": row["to_step_id"],
        "label": _row_get(row, "label"),
        "is_branch": bool(_row_get(row, "is_branch", False)),
        "edge_order": int(_row_get(row, "edge_order", 0) or 0),
    }


def add_workflow_participant(
    workflow_id: int,
    participant_type: str,
    agent_service_name: str | None = None,
    agent_id: str | None = None,
    role_name: str | None = None,
    team_member_id: int | None = None,
) -> dict[str, Any]:
    """Add one participant (agent or human role) to a workflow. Returns the
    created participant row."""
    ptype = "human" if str(participant_type).strip().lower() == "human" else "agent"
    vals = (
        workflow_id,
        ptype,
        agent_service_name or None,
        (agent_id or "main") if ptype == "agent" else None,
        role_name or None,
        team_member_id,
    )
    cols = (
        "(workflow_id, participant_type, agent_service_name, agent_id, "
        "role_name, team_member_id)"
    )
    with _connect() as conn, _cursor(conn) as cur:
        if USE_POSTGRES:
            cur.execute(
                f"INSERT INTO workflow_participants {cols} "
                f"VALUES ({PH}, {PH}, {PH}, {PH}, {PH}, {PH}) RETURNING *",
                vals,
            )
            row = cur.fetchone()
        else:
            cur.execute(
                f"INSERT INTO workflow_participants {cols} "
                f"VALUES ({PH}, {PH}, {PH}, {PH}, {PH}, {PH})",
                vals,
            )
            cur.execute(
                "SELECT * FROM workflow_participants WHERE id = ?", (cur.lastrowid,)
            )
            row = cur.fetchone()
        _touch_workflow(cur, workflow_id)
    return _row_to_participant(row)


def delete_workflow_participant(participant_id: int, workflow_id: int) -> bool:
    """Delete one participant from a workflow. Returns True when removed."""
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(
            f"DELETE FROM workflow_participants WHERE id = {PH} AND workflow_id = {PH}",
            (participant_id, workflow_id),
        )
        removed = cur.rowcount > 0
        if removed:
            _touch_workflow(cur, workflow_id)
    return removed


def add_workflow_edge(
    workflow_id: int,
    from_step_id: int,
    to_step_id: int,
    label: str | None = None,
    is_branch: bool = False,
    edge_order: int | None = None,
) -> dict[str, Any]:
    """Add one edge to a workflow's graph. `edge_order` is appended (max+1)
    when omitted. Returns the created edge row. Backward edges (loops) are
    allowed — the caller validates."""
    branch_val = bool(is_branch) if USE_POSTGRES else (1 if is_branch else 0)
    with _connect() as conn, _cursor(conn) as cur:
        order = edge_order
        if order is None:
            cur.execute(
                f"SELECT COALESCE(MAX(edge_order), -1) + 1 AS n FROM workflow_edges WHERE workflow_id = {PH}",
                (workflow_id,),
            )
            order = int(cur.fetchone()["n"])
        cols = "(workflow_id, from_step_id, to_step_id, label, is_branch, edge_order)"
        vals = (workflow_id, from_step_id, to_step_id, label or None, branch_val, int(order))
        if USE_POSTGRES:
            cur.execute(
                f"INSERT INTO workflow_edges {cols} "
                f"VALUES ({PH}, {PH}, {PH}, {PH}, {PH}, {PH}) RETURNING *",
                vals,
            )
            row = cur.fetchone()
        else:
            cur.execute(
                f"INSERT INTO workflow_edges {cols} "
                f"VALUES ({PH}, {PH}, {PH}, {PH}, {PH}, {PH})",
                vals,
            )
            cur.execute("SELECT * FROM workflow_edges WHERE id = ?", (cur.lastrowid,))
            row = cur.fetchone()
        _touch_workflow(cur, workflow_id)
    return _row_to_edge(row)


def update_workflow_edge(
    edge_id: int, workflow_id: int, fields: dict[str, Any]
) -> dict[str, Any] | None:
    """Patch an edge's label / is_branch, scoped to its workflow. Returns the
    updated edge or None when not found."""
    sets = []
    args: list[Any] = []
    if "label" in fields:
        sets.append(f"label = {PH}")
        args.append(fields["label"] or None)
    if "is_branch" in fields and fields["is_branch"] is not None:
        sets.append(f"is_branch = {PH}")
        args.append(bool(fields["is_branch"]) if USE_POSTGRES else (1 if fields["is_branch"] else 0))
    if not sets:
        with _connect() as conn, _cursor(conn) as cur:
            cur.execute(
                f"SELECT * FROM workflow_edges WHERE id = {PH} AND workflow_id = {PH}",
                (edge_id, workflow_id),
            )
            row = cur.fetchone()
        return _row_to_edge(row) if row else None
    args.extend([edge_id, workflow_id])
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(
            f"UPDATE workflow_edges SET {', '.join(sets)} "
            f"WHERE id = {PH} AND workflow_id = {PH}",
            tuple(args),
        )
        if cur.rowcount == 0:
            return None
        _touch_workflow(cur, workflow_id)
        cur.execute(
            f"SELECT * FROM workflow_edges WHERE id = {PH} AND workflow_id = {PH}",
            (edge_id, workflow_id),
        )
        row = cur.fetchone()
    return _row_to_edge(row) if row else None


def delete_workflow_edge(edge_id: int, workflow_id: int) -> bool:
    """Delete one edge from a workflow. Returns True when removed."""
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(
            f"DELETE FROM workflow_edges WHERE id = {PH} AND workflow_id = {PH}",
            (edge_id, workflow_id),
        )
        removed = cur.rowcount > 0
        if removed:
            _touch_workflow(cur, workflow_id)
    return removed


def update_step_position(
    step_id: int, workflow_id: int, pos_x: float, pos_y: float
) -> dict[str, Any] | None:
    """Persist a node's canvas position (drag-to-reposition). Returns the
    updated step or None when not found / not owned by the workflow."""
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(
            f"UPDATE workflow_steps SET pos_x = {PH}, pos_y = {PH} "
            f"WHERE id = {PH} AND workflow_id = {PH}",
            (float(pos_x), float(pos_y), step_id, workflow_id),
        )
        if cur.rowcount == 0:
            return None
        _touch_workflow(cur, workflow_id)
        cur.execute(
            f"SELECT * FROM workflow_steps WHERE id = {PH} AND workflow_id = {PH}",
            (step_id, workflow_id),
        )
        row = cur.fetchone()
    return _row_to_step(row) if row else None


def _replace_workflow_graph(
    workflow_id: int,
    participants: list[dict[str, Any]],
    steps: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> None:
    """Atomically replace a workflow's participants, steps, and edges in one
    transaction. `edges` reference steps by their index in the `steps` list
    (`from_index` / `to_index`); those map to the new DB ids after insert.
    Used by the AI describe / multi-agent generate flows."""
    branch_true = True if USE_POSTGRES else 1
    branch_false = False if USE_POSTGRES else 0
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(f"DELETE FROM workflow_edges WHERE workflow_id = {PH}", (workflow_id,))
        cur.execute(
            f"DELETE FROM workflow_participants WHERE workflow_id = {PH}", (workflow_id,)
        )
        cur.execute(f"DELETE FROM workflow_steps WHERE workflow_id = {PH}", (workflow_id,))

        cols = "(workflow_id, step_order, " + ", ".join(_STEP_FIELDS) + ")"
        ph = ", ".join([PH] * (2 + len(_STEP_FIELDS)))
        index_to_id: dict[int, int] = {}
        for order, step in enumerate(steps):
            clean = _clean_step_values(step)
            vals = (workflow_id, order, *(clean[f] for f in _STEP_FIELDS))
            if USE_POSTGRES:
                cur.execute(
                    f"INSERT INTO workflow_steps {cols} VALUES ({ph}) RETURNING id", vals
                )
                index_to_id[order] = cur.fetchone()["id"]
            else:
                cur.execute(f"INSERT INTO workflow_steps {cols} VALUES ({ph})", vals)
                index_to_id[order] = cur.lastrowid

        for eo, e in enumerate(edges or []):
            try:
                fi = int(e.get("from_index"))
                ti = int(e.get("to_index"))
            except (TypeError, ValueError):
                continue
            if fi not in index_to_id or ti not in index_to_id or fi == ti:
                continue
            cur.execute(
                "INSERT INTO workflow_edges (workflow_id, from_step_id, to_step_id, "
                f"label, is_branch, edge_order) VALUES ({PH}, {PH}, {PH}, {PH}, {PH}, {PH})",
                (
                    workflow_id,
                    index_to_id[fi],
                    index_to_id[ti],
                    (e.get("label") or None),
                    branch_true if e.get("is_branch") else branch_false,
                    eo,
                ),
            )

        for p in participants or []:
            ptype = (
                "human"
                if str(p.get("type") or p.get("participant_type") or "agent").strip().lower() == "human"
                else "agent"
            )
            cur.execute(
                "INSERT INTO workflow_participants (workflow_id, participant_type, "
                f"agent_service_name, agent_id, role_name, team_member_id) "
                f"VALUES ({PH}, {PH}, {PH}, {PH}, {PH}, {PH})",
                (
                    workflow_id,
                    ptype,
                    (p.get("agent_service_name") or p.get("service_name") or None),
                    ((p.get("agent_id") or "main") if ptype == "agent" else None),
                    (p.get("role_name") or None),
                    p.get("team_member_id"),
                ),
            )
        _touch_workflow(cur, workflow_id)


def apply_workflow_edit_operations(
    workflow_id: int, operations: list[dict[str, Any]]
) -> int:
    """Apply a list of AI-generated edit operations to a workflow in ONE
    transaction. Operations preserve existing step ids; new steps carry a
    string `tmp_id` that later edges reference. Invalid/unresolvable ops are
    skipped (these are best-effort canvas edits, not transactions). Returns the
    number of operations applied. Ownership must be checked by the caller.

    Op shapes (see describer.workflow_edit_operations):
      add_step{tmp_id,...}, update_step{step_id,...}, delete_step{step_id},
      add_edge{from,to,label,is_branch}, delete_edge{edge_id},
      add_participant{type,...}
    """
    branch_true = True if USE_POSTGRES else 1
    branch_false = False if USE_POSTGRES else 0
    ops = operations or []
    applied = 0

    def _insert_step(cur, clean, order):
        cols = "(workflow_id, step_order, " + ", ".join(_STEP_FIELDS) + ")"
        ph = ", ".join([PH] * (2 + len(_STEP_FIELDS)))
        vals = (workflow_id, order, *(clean[f] for f in _STEP_FIELDS))
        if USE_POSTGRES:
            cur.execute(
                f"INSERT INTO workflow_steps {cols} VALUES ({ph}) RETURNING id", vals
            )
            return cur.fetchone()["id"]
        cur.execute(f"INSERT INTO workflow_steps {cols} VALUES ({ph})", vals)
        return cur.lastrowid

    with _connect() as conn, _cursor(conn) as cur:
        # Current state inside the txn.
        cur.execute(
            f"SELECT id FROM workflow_steps WHERE workflow_id = {PH}", (workflow_id,)
        )
        live_ids: set[int] = {r["id"] for r in cur.fetchall()}
        cur.execute(
            f"SELECT COALESCE(MAX(step_order), -1) + 1 AS n FROM workflow_steps WHERE workflow_id = {PH}",
            (workflow_id,),
        )
        next_order = int(cur.fetchone()["n"])
        cur.execute(
            f"SELECT COALESCE(MAX(edge_order), -1) + 1 AS n FROM workflow_edges WHERE workflow_id = {PH}",
            (workflow_id,),
        )
        next_edge_order = int(cur.fetchone()["n"])

        tmp_to_id: dict[str, int] = {}
        new_agent_steps: list[tuple[str, str]] = []  # (service, agent_id) added

        def _resolve(ref) -> int | None:
            if isinstance(ref, str) and ref in tmp_to_id:
                return tmp_to_id[ref]
            try:
                rid = int(ref)
            except (TypeError, ValueError):
                return None
            return rid if rid in live_ids else None

        # 1) add_step (so tmp ids resolve for later edges)
        for op in ops:
            if not isinstance(op, dict) or op.get("op") != "add_step":
                continue
            clean = _clean_step_values(op)
            new_id = _insert_step(cur, clean, next_order)
            next_order += 1
            live_ids.add(new_id)
            tmp = op.get("tmp_id")
            if isinstance(tmp, str) and tmp:
                tmp_to_id[tmp] = new_id
            if clean["step_type"] == "agent" and clean["agent_service_name"]:
                new_agent_steps.append((clean["agent_service_name"], clean["agent_id"] or "main"))
            applied += 1

        # 2) update_step
        for op in ops:
            if not isinstance(op, dict) or op.get("op") != "update_step":
                continue
            sid = _resolve(op.get("step_id"))
            if sid is None:
                continue
            clean = _clean_step_values(op)
            sets, args = [], []
            for f in _STEP_FIELDS:
                if f in op:  # only fields the op actually provided
                    sets.append(f"{f} = {PH}")
                    args.append(clean[f])
            if not sets:
                continue
            args.extend([sid, workflow_id])
            cur.execute(
                f"UPDATE workflow_steps SET {', '.join(sets)} WHERE id = {PH} AND workflow_id = {PH}",
                tuple(args),
            )
            if clean["step_type"] == "agent" and clean["agent_service_name"] and "agent_service_name" in op:
                new_agent_steps.append((clean["agent_service_name"], clean["agent_id"] or "main"))
            applied += 1

        # 3) delete_step (+ its edges — SQLite FK cascade is off)
        for op in ops:
            if not isinstance(op, dict) or op.get("op") != "delete_step":
                continue
            sid = _resolve(op.get("step_id"))
            if sid is None:
                continue
            cur.execute(
                f"DELETE FROM workflow_edges WHERE workflow_id = {PH} AND (from_step_id = {PH} OR to_step_id = {PH})",
                (workflow_id, sid, sid),
            )
            cur.execute(
                f"DELETE FROM workflow_steps WHERE id = {PH} AND workflow_id = {PH}",
                (sid, workflow_id),
            )
            live_ids.discard(sid)
            applied += 1

        # 4) delete_edge
        for op in ops:
            if not isinstance(op, dict) or op.get("op") != "delete_edge":
                continue
            try:
                eid = int(op.get("edge_id"))
            except (TypeError, ValueError):
                continue
            cur.execute(
                f"DELETE FROM workflow_edges WHERE id = {PH} AND workflow_id = {PH}",
                (eid, workflow_id),
            )
            if cur.rowcount > 0:
                applied += 1

        # 5) add_edge (endpoints resolved against the now-current id set)
        for op in ops:
            if not isinstance(op, dict) or op.get("op") != "add_edge":
                continue
            fi = _resolve(op.get("from"))
            ti = _resolve(op.get("to"))
            if fi is None or ti is None or fi == ti:
                continue
            cur.execute(
                f"SELECT 1 FROM workflow_edges WHERE workflow_id = {PH} AND from_step_id = {PH} AND to_step_id = {PH}",
                (workflow_id, fi, ti),
            )
            if cur.fetchone():
                continue  # skip duplicates
            cur.execute(
                "INSERT INTO workflow_edges (workflow_id, from_step_id, to_step_id, "
                f"label, is_branch, edge_order) VALUES ({PH}, {PH}, {PH}, {PH}, {PH}, {PH})",
                (
                    workflow_id,
                    fi,
                    ti,
                    (op.get("label") or None),
                    branch_true if op.get("is_branch") else branch_false,
                    next_edge_order,
                ),
            )
            next_edge_order += 1
            applied += 1

        # Existing participants (for dedupe of explicit add + auto-add).
        cur.execute(
            f"SELECT participant_type, agent_service_name, agent_id, role_name "
            f"FROM workflow_participants WHERE workflow_id = {PH}",
            (workflow_id,),
        )
        have_agents: set[tuple[str, str]] = set()
        have_roles: set[str] = set()
        for r in cur.fetchall():
            if r["participant_type"] == "human" and r["role_name"]:
                have_roles.add(r["role_name"].strip().lower())
            elif r["agent_service_name"]:
                have_agents.add((r["agent_service_name"], r["agent_id"] or "main"))

        def _add_participant(ptype, svc, aid, role):
            cur.execute(
                "INSERT INTO workflow_participants (workflow_id, participant_type, "
                f"agent_service_name, agent_id, role_name, team_member_id) "
                f"VALUES ({PH}, {PH}, {PH}, {PH}, {PH}, NULL)",
                (workflow_id, ptype, svc, aid, role),
            )

        # 6) explicit add_participant
        for op in ops:
            if not isinstance(op, dict) or op.get("op") != "add_participant":
                continue
            ptype = "human" if str(op.get("type") or "").strip().lower() == "human" else "agent"
            if ptype == "human":
                role = (op.get("role_name") or "").strip()
                if not role or role.lower() in have_roles:
                    continue
                _add_participant("human", None, None, role)
                have_roles.add(role.lower())
            else:
                svc = (op.get("agent_service_name") or "").strip()
                aid = (op.get("agent_id") or "main")
                if not svc or (svc, aid) in have_agents:
                    continue
                _add_participant("agent", svc, aid, None)
                have_agents.add((svc, aid))
            applied += 1

        # Auto-add any agent introduced by a step but not yet on the roster.
        for svc, aid in new_agent_steps:
            if (svc, aid) not in have_agents:
                _add_participant("agent", svc, aid, None)
                have_agents.add((svc, aid))

        _touch_workflow(cur, workflow_id)
    return applied


def get_workflow_stats(
    workflow_id: int, account_id: int | None
) -> dict[str, Any] | None:
    """Live telemetry stats for a workflow's source agent — runs (distinct
    traces), errors, success rate, avg duration, last run, tokens, cost.
    All-time, scoped to the workflow's (service_name, agent_id) and account.
    Returns None when the workflow isn't owned; `has_agent=False` when the
    workflow has no source agent to pull stats from."""
    account_clause = (
        f"AND account_id = {PH}"
        if account_id is not None
        else "AND account_id IS NULL"
    )
    empty = {
        "has_agent": False,
        "runs": 0,
        "spans": 0,
        "errors": 0,
        "success_rate": 0.0,
        "avg_duration_ms": 0.0,
        "last_run": None,
        "total_tokens": 0,
        "estimated_cost_usd": 0.0,
    }
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(
            f"SELECT agent_service_name, agent_id FROM workflows WHERE id = {PH} {account_clause}",
            (workflow_id,) if account_id is None else (workflow_id, account_id),
        )
        wf = cur.fetchone()
        if wf is None:
            return None
        service_name = wf["agent_service_name"]
        agent_id = wf["agent_id"] or "main"
        if not service_name:
            return empty

        span_acct = (
            f"AND account_id = {PH}"
            if account_id is not None
            else "AND account_id IS NULL"
        )
        sql = f"""
            SELECT
                COUNT(DISTINCT trace_id)                         AS runs,
                COUNT(*)                                         AS spans,
                SUM(CASE WHEN status_code = 2 THEN 1 ELSE 0 END) AS errors,
                AVG((end_time_unix - start_time_unix) / 1000000.0) AS avg_duration_ms,
                MAX(start_time_unix)                             AS last_run_ns,
                SUM(total_tokens)                                AS total_tokens,
                SUM(estimated_cost_usd)                          AS est_cost
            FROM spans
            WHERE service_name = {PH}
              AND COALESCE(agent_id, 'main') = {PH}
              {span_acct}
        """
        args: tuple[Any, ...] = (service_name, agent_id)
        if account_id is not None:
            args = (*args, account_id)
        cur.execute(sql, args)
        row = cur.fetchone()

    spans = int(row["spans"] or 0)
    errors = int(row["errors"] or 0)
    return {
        "has_agent": True,
        "runs": int(row["runs"] or 0),
        "spans": spans,
        "errors": errors,
        "success_rate": round((spans - errors) / spans * 100, 1) if spans else 0.0,
        "avg_duration_ms": round(float(row["avg_duration_ms"] or 0.0), 1),
        "last_run": _ns_to_iso(row["last_run_ns"]),
        "total_tokens": int(row["total_tokens"] or 0),
        "estimated_cost_usd": round(float(row["est_cost"] or 0.0), 6),
    }


def get_workflow_step_stats(
    workflow_id: int, account_id: int | None
) -> dict[str, Any] | None:
    """Per-step + per-workflow telemetry over the last 24h for the multi-agent
    canvas. Per-step runs/avg_duration/success are real (spans matched by the
    step's agent + operation). Workflow rollup (total_runs, success_rate,
    avg_cycle_ms) is real; escalation_rate + avg_human_wait_ms are returned
    None (no per-run edge-path telemetry to derive them). Returns None when
    the workflow isn't owned."""
    wf = get_workflow(workflow_id, account_id)
    if wf is None:
        return None

    from time import time as _time

    now_ns = int(_time() * 1_000_000_000)
    start_ns = now_ns - 24 * 60 * 60 * 1_000_000_000

    steps = wf["steps"]
    services = sorted(
        {s["agent_service_name"] for s in steps if s["step_type"] == "agent" and s["agent_service_name"]}
        | {
            p["agent_service_name"]
            for p in wf.get("participants", [])
            if p["type"] == "agent" and p["agent_service_name"]
        }
    )

    result: dict[str, Any] = {
        "per_step": {},
        "total_runs": 0,
        "success_rate": 0.0,
        "avg_cycle_ms": 0.0,
        "escalation_rate": None,
        "avg_human_wait_ms": None,
        "loop_rate": None,
        "avg_rounds": None,
    }
    if not services:
        return result

    # Does the graph contain a backward edge (a loop)? Loop metrics are only
    # meaningful — and only "derivable" — for workflows that actually loop.
    order_by_id = {s["id"]: s["step_order"] for s in steps}
    has_loop_edge = any(
        order_by_id.get(e["to_step_id"], 0) < order_by_id.get(e["from_step_id"], 0)
        for e in wf.get("edges", [])
    )
    # Operations that correspond to real agent steps — a repeat of one of these
    # span_names within a single trace is the signal that the run looped back.
    loop_ops = sorted(
        {s["operation"] for s in steps if s["step_type"] == "agent" and s["operation"]}
    )

    span_acct = (
        f"AND account_id = {PH}" if account_id is not None else "AND account_id IS NULL"
    )
    placeholders = ", ".join([PH] * len(services))
    grouped: dict[tuple, dict[str, Any]] = {}
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(
            f"""
            SELECT service_name, COALESCE(agent_id, 'main') AS agent_id, span_name,
                   COUNT(DISTINCT trace_id) AS runs,
                   COUNT(*) AS spans,
                   SUM(CASE WHEN status_code = 2 THEN 1 ELSE 0 END) AS errors,
                   AVG((end_time_unix - start_time_unix) / 1000000.0) AS avg_dur
            FROM spans
            WHERE service_name IN ({placeholders})
              AND start_time_unix >= {PH}
              {span_acct}
            GROUP BY service_name, COALESCE(agent_id, 'main'), span_name
            """,
            tuple([*services, start_ns] + ([account_id] if account_id is not None else [])),
        )
        for r in cur.fetchall():
            grouped[(r["service_name"], r["agent_id"], r["span_name"])] = {
                "runs": int(r["runs"] or 0),
                "spans": int(r["spans"] or 0),
                "errors": int(r["errors"] or 0),
                "avg_dur": float(r["avg_dur"] or 0.0),
            }

        # Workflow rollup: distinct traces + average per-trace cycle time.
        cur.execute(
            f"""
            SELECT COUNT(DISTINCT trace_id) AS runs,
                   AVG(cycle) AS avg_cycle
            FROM (
                SELECT trace_id,
                       (MAX(end_time_unix) - MIN(start_time_unix)) / 1000000.0 AS cycle
                FROM spans
                WHERE service_name IN ({placeholders})
                  AND start_time_unix >= {PH}
                  {span_acct}
                GROUP BY trace_id
            ) t
            """,
            tuple([*services, start_ns] + ([account_id] if account_id is not None else [])),
        )
        roll = cur.fetchone()

        # Loop metrics: a run "looped" when the same step span_name appears 2+
        # times for the same agent within one trace. Only derivable when the
        # graph actually has a backward edge and there are step operations to
        # inspect — otherwise we leave loop_rate / avg_rounds null (honest).
        loop_rows = []
        if has_loop_edge and loop_ops:
            op_ph = ", ".join([PH] * len(loop_ops))
            cur.execute(
                f"""
                SELECT trace_id, service_name, COALESCE(agent_id, 'main') AS agent_id,
                       span_name, COUNT(*) AS c
                FROM spans
                WHERE service_name IN ({placeholders})
                  AND span_name IN ({op_ph})
                  AND start_time_unix >= {PH}
                  {span_acct}
                GROUP BY trace_id, service_name, COALESCE(agent_id, 'main'), span_name
                """,
                tuple(
                    [*services, *loop_ops, start_ns]
                    + ([account_id] if account_id is not None else [])
                ),
            )
            loop_rows = cur.fetchall()

    tot_spans = 0
    tot_errors = 0
    for s in steps:
        if s["step_type"] != "agent" or not s["agent_service_name"] or not s["operation"]:
            continue
        key = (s["agent_service_name"], s["agent_id"] or "main", s["operation"])
        g = grouped.get(key)
        if not g:
            continue
        sp = g["spans"]
        er = g["errors"]
        tot_spans += sp
        tot_errors += er
        result["per_step"][str(s["id"])] = {
            "runs": g["runs"],
            "avg_duration_ms": round(g["avg_dur"], 1),
            "success_rate": round((sp - er) / sp * 100, 1) if sp else 0.0,
        }

    result["total_runs"] = int(roll["runs"] or 0) if roll else 0
    result["avg_cycle_ms"] = round(float(roll["avg_cycle"] or 0.0), 1) if roll else 0.0
    result["success_rate"] = (
        round((tot_spans - tot_errors) / tot_spans * 100, 1) if tot_spans else 0.0
    )

    # Roll the loop rows up per trace: rounds = max repeat count of any looped
    # span in that trace; a trace looped when rounds >= 2.
    if has_loop_edge and loop_ops and result["total_runs"] > 0:
        rounds_by_trace: dict[Any, int] = {}
        for r in loop_rows:
            c = int(r["c"] or 0)
            if c > rounds_by_trace.get(r["trace_id"], 0):
                rounds_by_trace[r["trace_id"]] = c
        looped = [n for n in rounds_by_trace.values() if n >= 2]
        result["loop_rate"] = round(len(looped) / result["total_runs"] * 100, 1)
        result["avg_rounds"] = round(sum(looped) / len(looped), 1) if looped else 0.0

    return result


# ---------------------------------------------------------------------------
# Per-agent windowed aggregates (for weekly summaries)
# ---------------------------------------------------------------------------


def get_window_aggregate(
    service_name: str,
    agent_id: str,
    start_time_ns: int,
    end_time_ns: int,
    account_id: int | None = None,
) -> dict[str, Any]:
    """Aggregate one agent's spans over an arbitrary time window.

    Returns runs, errors, success_rate, avg_duration_ms, plus the
    distinct tool names and operations seen in-window. `runs` counts
    every span — message_received, tool_call, etc. — but the operations
    list collapses to distinct `span_name`. Tool names are extracted
    from spans that look like tool_calls (`oversee.tool.name` in the
    attributes blob — checked via LIKE).
    """
    account_filter = (
        f"AND account_id = {PH}" if account_id is not None else ""
    )
    base_args: list[Any] = [service_name, agent_id or "main", start_time_ns, end_time_ns]
    if account_id is not None:
        base_args.append(account_id)

    agg_sql = f"""
        SELECT
            COUNT(*)                                       AS runs,
            SUM(CASE WHEN status_code = 2 THEN 1 ELSE 0 END) AS errors,
            AVG((end_time_unix - start_time_unix) / 1000000.0) AS avg_duration_ms
        FROM spans
        WHERE service_name = {PH}
          AND COALESCE(agent_id, 'main') = {PH}
          AND start_time_unix >= {PH}
          AND start_time_unix <  {PH}
          {account_filter}
    """
    ops_sql = f"""
        SELECT DISTINCT span_name
        FROM spans
        WHERE service_name = {PH}
          AND COALESCE(agent_id, 'main') = {PH}
          AND start_time_unix >= {PH}
          AND start_time_unix <  {PH}
          {account_filter}
    """
    # Tool names live inside the attributes JSON blob. Cheap LIKE filter
    # to narrow candidate rows, then parse in Python to extract the
    # actual values.
    #
    # IMPORTANT: bind the LIKE pattern as a parameter. psycopg2 treats
    # every literal `%` in the SQL string as a format marker when
    # `params` is provided — an inline `LIKE '%foo%'` against Postgres
    # crashes with `IndexError: tuple index out of range` even though
    # SQLite (which uses `?` binding) is unaffected.
    tools_sql = f"""
        SELECT attributes
        FROM spans
        WHERE service_name = {PH}
          AND COALESCE(agent_id, 'main') = {PH}
          AND start_time_unix >= {PH}
          AND start_time_unix <  {PH}
          AND (attributes LIKE {PH} OR attributes LIKE {PH})
          {account_filter}
    """
    # The tools query needs the pattern args slotted BEFORE the
    # optional account_id (placeholder order: service, agent,
    # start, end, pattern(s), [account_id]). Two patterns: the new
    # `trovis.*` namespace + the legacy `oversee.*` (live/historical agents).
    tools_args: list[Any] = [
        service_name,
        agent_id or "main",
        start_time_ns,
        end_time_ns,
        '%"trovis.tool.name":%',
        '%"oversee.tool.name":%',
    ]
    if account_id is not None:
        tools_args.append(account_id)

    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(agg_sql, tuple(base_args))
        agg = cur.fetchone()
        cur.execute(ops_sql, tuple(base_args))
        ops_rows = cur.fetchall()
        cur.execute(tools_sql, tuple(tools_args))
        tools_rows = cur.fetchall()

    runs = (agg["runs"] if agg else 0) or 0
    errors = (agg["errors"] if agg else 0) or 0
    avg_ms = float(agg["avg_duration_ms"]) if agg and agg["avg_duration_ms"] else 0.0
    success_rate = ((runs - errors) / runs * 100.0) if runs else 0.0

    operations = sorted({r["span_name"] for r in ops_rows if r["span_name"]})

    tool_set: set[str] = set()
    for r in tools_rows:
        try:
            attrs = json.loads(r["attributes"] or "{}")
        except (TypeError, ValueError):
            continue
        name = attr(attrs, "tool.name")
        if isinstance(name, str) and name:
            tool_set.add(name)

    return {
        "runs": runs,
        "errors": errors,
        "success_rate": success_rate,
        "avg_duration_ms": avg_ms,
        "operations": operations,
        "tools_used": sorted(tool_set),
    }


# ---------------------------------------------------------------------------
# Cost + token aggregation
# ---------------------------------------------------------------------------


def get_agent_costs(
    service_name: str,
    account_id: int | None = None,
    agent_id: str | None = None,
    days: int = 7,
) -> dict[str, Any]:
    """Aggregate token usage + estimated cost for an agent over the
    last `days` days. Returns totals plus per-day and per-model
    breakdowns.

    Only spans with a non-NULL `total_tokens` contribute — spans that
    never carried usage data are ignored. The per-day bucketing and
    per-model grouping happen in Python (model lives in the attributes
    JSON), which keeps the SQL backend-portable and the row set small
    (one window of one agent's model calls).
    """
    days = max(1, min(365, int(days)))
    from time import time as _time

    now_ns = int(_time() * 1_000_000_000)
    start_ns = now_ns - days * 24 * 60 * 60 * 1_000_000_000

    account_filter = (
        f"AND account_id = {PH}" if account_id is not None else ""
    )
    agent_filter = (
        f"AND COALESCE(agent_id, 'main') = {PH}" if agent_id is not None else ""
    )
    sql = f"""
        SELECT start_time_unix, input_tokens, output_tokens, total_tokens,
               estimated_cost_usd, attributes, cost_source
        FROM spans
        WHERE service_name = {PH}
          AND (total_tokens IS NOT NULL OR estimated_cost_usd IS NOT NULL)
          AND start_time_unix >= {PH}
          {account_filter}
          {agent_filter}
        ORDER BY start_time_unix ASC
    """
    args_list: list[Any] = [service_name, start_ns]
    if account_id is not None:
        args_list.append(account_id)
    if agent_id is not None:
        args_list.append(agent_id)

    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(sql, tuple(args_list))
        rows = cur.fetchall()

    total_input = 0
    total_output = 0
    total_tokens = 0
    total_cost = 0.0
    by_day: dict[str, dict[str, float]] = {}
    by_model: dict[str, dict[str, float]] = {}

    from datetime import datetime, timezone

    for r in rows:
        inp = r["input_tokens"] or 0
        out = r["output_tokens"] or 0
        tot = r["total_tokens"] or 0
        cost = r["estimated_cost_usd"] or 0.0
        total_input += inp
        total_output += out
        total_tokens += tot
        total_cost += cost

        # Day bucket — ISO date (UTC) from the nanosecond start time.
        day = datetime.fromtimestamp(
            r["start_time_unix"] / 1_000_000_000, tz=timezone.utc
        ).strftime("%Y-%m-%d")
        d = by_day.setdefault(day, {"tokens": 0, "cost": 0.0})
        d["tokens"] += tot
        d["cost"] += cost

        # Model bucket — read gen_ai.request.model from attributes JSON.
        # SDK-reported run totals may not name a model (the run can span
        # several internally); label them honestly instead of "unknown".
        model = "unknown"
        try:
            attrs = json.loads(r["attributes"] or "{}")
            if isinstance(attrs, dict):
                model = model_from_attrs(attrs) or "unknown"
        except (TypeError, ValueError):
            pass
        if model == "unknown" and _row_get(r, "cost_source") == "reported":
            model = "(run total)"
        m = by_model.setdefault(model, {"tokens": 0, "cost": 0.0})
        m["tokens"] += tot
        m["cost"] += cost

    cost_by_day = [
        {"date": day, "tokens": v["tokens"], "cost": round(v["cost"], 6)}
        for day, v in sorted(by_day.items())
    ]
    cost_by_model = [
        {"model": model, "tokens": v["tokens"], "cost": round(v["cost"], 6)}
        for model, v in sorted(
            by_model.items(), key=lambda kv: kv[1]["cost"], reverse=True
        )
    ]

    return {
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "total_tokens": total_tokens,
        "estimated_cost_usd": round(total_cost, 6),
        "cost_by_day": cost_by_day,
        "cost_by_model": cost_by_model,
    }


# ---------------------------------------------------------------------------
# Fleet-wide dashboard aggregates
# ---------------------------------------------------------------------------


def count_fleet_spans(
    account_id: int | None,
    since_ns: int,
    until_ns: int | None = None,
    activity_only: bool = False,
) -> int:
    """Count spans (tasks) across the whole fleet within a time window.

    Filters `since_ns <= start_time_unix < until_ns` (the upper bound is
    optional). Account-scoped when account_id is provided; counts all rows
    otherwise (local-dev / pre-auth behavior). Powers the briefing's
    today / last-7d / prior-7d task counts.

    `activity_only=True` excludes `agent_registration` spans — a just-connected
    agent has only a registration span, which isn't a real "task", so the
    briefing counts (and the dashboard's "waiting for telemetry" signal) reflect
    actual activity.
    """
    account_filter = f"AND account_id = {PH}" if account_id is not None else ""
    until_filter = f"AND start_time_unix < {PH}" if until_ns is not None else ""
    activity_filter = "AND span_name != 'agent_registration'" if activity_only else ""
    sql = f"""
        SELECT COUNT(*) AS n
        FROM spans
        WHERE start_time_unix >= {PH}
          {until_filter}
          {activity_filter}
          {account_filter}
    """
    args: list[Any] = [since_ns]
    if until_ns is not None:
        args.append(until_ns)
    if account_id is not None:
        args.append(account_id)
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(sql, tuple(args))
        row = cur.fetchone()
    return int(row["n"]) if row else 0


def get_fleet_daily_cost(
    account_id: int | None,
    days: int = 30,
) -> list[dict[str, Any]]:
    """Org-wide daily rollup of estimated cost + tokens over the last
    `days` days. Buckets by UTC date in Python (keeps the SQL portable),
    returning [{date, tokens, cost}] sorted ascending. Days with no
    usage are omitted — the caller fills gaps for the sparkline.

    Counts spans carrying usage data OR a cost — SDK-reported run totals
    (`cost_source='reported'`) may carry no tokens but must count, matching
    `get_agent_costs`.
    """
    days = max(1, min(365, int(days)))
    from time import time as _time

    now_ns = int(_time() * 1_000_000_000)
    start_ns = now_ns - days * 24 * 60 * 60 * 1_000_000_000

    account_filter = f"AND account_id = {PH}" if account_id is not None else ""
    sql = f"""
        SELECT start_time_unix, total_tokens, estimated_cost_usd
        FROM spans
        WHERE (total_tokens IS NOT NULL OR estimated_cost_usd IS NOT NULL)
          AND start_time_unix >= {PH}
          {account_filter}
        ORDER BY start_time_unix ASC
    """
    args: list[Any] = [start_ns]
    if account_id is not None:
        args.append(account_id)
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(sql, tuple(args))
        rows = cur.fetchall()

    from datetime import datetime, timezone

    by_day: dict[str, dict[str, float]] = {}
    for r in rows:
        day = datetime.fromtimestamp(
            r["start_time_unix"] / 1_000_000_000, tz=timezone.utc
        ).strftime("%Y-%m-%d")
        d = by_day.setdefault(day, {"tokens": 0, "cost": 0.0})
        d["tokens"] += r["total_tokens"] or 0
        d["cost"] += r["estimated_cost_usd"] or 0.0
    return [
        {"date": day, "tokens": v["tokens"], "cost": round(v["cost"], 6)}
        for day, v in sorted(by_day.items())
    ]


def get_cost_breakdown(account_id: int | None) -> dict[str, Any]:
    """Month-to-date (calendar month, UTC) cost breakdown for the cost page:
    org month_total, per-service MTD (for per-agent cap comparison), and an
    org-wide by-model list. One pass over this month's token-bearing spans."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    start_ns = int(month_start.timestamp() * 1_000_000_000)

    account_filter = f"AND account_id = {PH}" if account_id is not None else ""
    sql = f"""
        SELECT service_name, total_tokens, estimated_cost_usd, attributes,
               cost_source
        FROM spans
        WHERE (total_tokens IS NOT NULL OR estimated_cost_usd IS NOT NULL)
          AND start_time_unix >= {PH}
          {account_filter}
    """
    args: list[Any] = [start_ns]
    if account_id is not None:
        args.append(account_id)
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(sql, tuple(args))
        rows = cur.fetchall()

    by_service: dict[str, float] = {}
    by_model: dict[str, dict[str, float]] = {}
    month_total = 0.0
    for r in rows:
        cost = r["estimated_cost_usd"] or 0.0
        tok = r["total_tokens"] or 0
        month_total += cost
        by_service[r["service_name"]] = by_service.get(r["service_name"], 0.0) + cost
        model = "unknown"
        try:
            attrs = json.loads(r["attributes"] or "{}")
            if isinstance(attrs, dict):
                model = model_from_attrs(attrs) or "unknown"
        except (TypeError, ValueError):
            pass
        if model == "unknown" and _row_get(r, "cost_source") == "reported":
            model = "(run total)"
        m = by_model.setdefault(model, {"tokens": 0, "cost": 0.0})
        m["tokens"] += tok
        m["cost"] += cost
    return {
        "month_total": round(month_total, 6),
        "by_service_mtd": {k: round(v, 6) for k, v in by_service.items()},
        "by_model": sorted(
            [
                {"model": k, "tokens": v["tokens"], "cost": round(v["cost"], 6)}
                for k, v in by_model.items()
            ],
            key=lambda x: x["cost"],
            reverse=True,
        ),
    }


# ---------------------------------------------------------------------------
# Cost budgets (org monthly budget + per-agent monthly caps)
# ---------------------------------------------------------------------------


def get_account_budget(account_id: int | None) -> float | None:
    """The org's editable monthly budget, or None when unset (use env default)."""
    if account_id is None:
        return None
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(
            f"SELECT monthly_budget_usd FROM accounts WHERE id = {PH}", (account_id,)
        )
        row = cur.fetchone()
    if not row:
        return None
    v = _row_get(row, "monthly_budget_usd")
    return float(v) if v is not None else None


def set_account_budget(account_id: int | None, usd: float | None) -> None:
    if account_id is None:
        return
    val = None if usd is None else max(0.0, float(usd))
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(
            f"UPDATE accounts SET monthly_budget_usd = {PH} WHERE id = {PH}",
            (val, account_id),
        )


def get_agent_budgets(account_id: int | None) -> list[dict[str, Any]]:
    """All per-agent monthly caps for the account."""
    clause = f"account_id = {PH}" if account_id is not None else "account_id IS NULL"
    args = (account_id,) if account_id is not None else ()
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(
            "SELECT service_name, COALESCE(agent_id, 'main') AS agent_id, "
            f"monthly_cap_usd FROM agent_budgets WHERE {clause}",
            args,
        )
        return [
            {
                "service_name": r["service_name"],
                "agent_id": r["agent_id"],
                "monthly_cap_usd": float(r["monthly_cap_usd"]),
            }
            for r in cur.fetchall()
        ]


def set_agent_budget(
    account_id: int | None,
    service_name: str,
    agent_id: str | None,
    monthly_cap_usd: float | None,
) -> None:
    """Upsert (or, when monthly_cap_usd is None, clear) a per-agent monthly cap."""
    aid = agent_id or "main"
    with _connect() as conn, _cursor(conn) as cur:
        if monthly_cap_usd is None:
            clause = (
                f"account_id = {PH}" if account_id is not None else "account_id IS NULL"
            )
            args = (service_name, aid) + (
                (account_id,) if account_id is not None else ()
            )
            cur.execute(
                f"DELETE FROM agent_budgets WHERE service_name = {PH} "
                f"AND agent_id = {PH} AND {clause}",
                args,
            )
            return
        cap = max(0.0, float(monthly_cap_usd))
        if USE_POSTGRES:
            cur.execute(
                "INSERT INTO agent_budgets (account_id, service_name, agent_id, monthly_cap_usd) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (account_id, service_name, agent_id) "
                "DO UPDATE SET monthly_cap_usd = EXCLUDED.monthly_cap_usd, updated_at = NOW()",
                (account_id, service_name, aid, cap),
            )
        else:
            cur.execute(
                "INSERT INTO agent_budgets (account_id, service_name, agent_id, monthly_cap_usd) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT (account_id, service_name, agent_id) "
                "DO UPDATE SET monthly_cap_usd = excluded.monthly_cap_usd, updated_at = CURRENT_TIMESTAMP",
                (account_id, service_name, aid, cap),
            )


# ---------------------------------------------------------------------------
# Insights cache (weekly summaries, capability maps)
# ---------------------------------------------------------------------------


def save_insight(
    account_id: int | None,
    service_name: str,
    agent_id: str,
    kind: str,
    data: dict[str, Any],
) -> None:
    """Upsert a JSON insight payload. `kind` distinguishes the row type
    (e.g. 'weekly_summary', 'capabilities'). `data` is JSON-serialized
    and the unique 4-tuple guarantees one row per kind per agent."""
    payload = json.dumps(data)
    if USE_POSTGRES:
        sql = """
            INSERT INTO agent_insights (account_id, service_name, agent_id, kind, data, generated_at)
            VALUES (%s, %s, %s, %s, %s, NOW())
            ON CONFLICT (account_id, service_name, agent_id, kind)
            DO UPDATE SET data = EXCLUDED.data, generated_at = NOW()
        """
    else:
        sql = """
            INSERT INTO agent_insights (account_id, service_name, agent_id, kind, data, generated_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT (account_id, service_name, agent_id, kind)
            DO UPDATE SET data = excluded.data, generated_at = CURRENT_TIMESTAMP
        """
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(sql, (account_id, service_name, agent_id or "main", kind, payload))


def get_insight(
    account_id: int | None,
    service_name: str,
    agent_id: str,
    kind: str,
    max_age_seconds: int | None = None,
) -> dict[str, Any] | None:
    """Read a cached insight. Returns the parsed payload + generated_at
    when fresh enough; returns None when missing OR when older than
    `max_age_seconds`. Stale rows aren't deleted — they get overwritten
    by the next save_insight() call.
    """
    account_clause = (
        f"AND account_id = {PH}"
        if account_id is not None
        else "AND account_id IS NULL"
    )
    sql = f"""
        SELECT data, generated_at
        FROM agent_insights
        WHERE service_name = {PH}
          AND COALESCE(agent_id, 'main') = {PH}
          AND kind = {PH}
          {account_clause}
        LIMIT 1
    """
    args: tuple[Any, ...] = (service_name, agent_id or "main", kind)
    if account_id is not None:
        args = (*args, account_id)
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(sql, args)
        row = cur.fetchone()
    if row is None:
        return None

    # TTL check. Postgres returns a datetime, SQLite a string — both
    # need normalizing. We compare in UTC seconds since epoch.
    if max_age_seconds is not None:
        from datetime import datetime, timezone

        raw = row["generated_at"]
        try:
            if isinstance(raw, str):
                # SQLite returns "YYYY-MM-DD HH:MM:SS" in UTC.
                gen = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if gen.tzinfo is None:
                    gen = gen.replace(tzinfo=timezone.utc)
            else:
                gen = raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - gen).total_seconds()
            if age > max_age_seconds:
                return None
        except (ValueError, AttributeError):
            # If we can't parse the timestamp, treat as fresh — better
            # to serve a slightly old summary than to retry indefinitely.
            pass

    try:
        data = json.loads(row["data"])
    except (TypeError, ValueError):
        return None
    return {
        "data": data,
        "generated_at": _ts_to_str(row["generated_at"]),
    }


# ---------------------------------------------------------------------------
# Proactive alerts — settings, dedup log, account enumeration
# ---------------------------------------------------------------------------

# Defaults returned when an account has no alert_settings row yet. Kept here so
# the sweep and the config endpoint agree on the out-of-the-box behavior:
# email on to the owner, every rule on, warn at 80% budget, loop trip at 50.
_ALERT_DEFAULTS: dict[str, Any] = {
    "email_enabled": True,
    "slack_webhook_url": None,
    "webhook_url": None,
    "rule_drift": True,
    "rule_budget": True,
    "rule_loop": True,
    "rule_error": True,
    "budget_warn_pct": 80,
    "loop_threshold": 50,
}

_ALERT_BOOL_FIELDS = (
    "email_enabled", "rule_drift", "rule_budget", "rule_loop", "rule_error",
)
_ALERT_INT_FIELDS = ("budget_warn_pct", "loop_threshold")
_ALERT_STR_FIELDS = ("slack_webhook_url", "webhook_url")


def list_account_ids() -> list[int]:
    """Every account id, for the fleet-wide alert sweep to iterate tenants."""
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute("SELECT id FROM accounts ORDER BY id")
        return [r["id"] for r in cur.fetchall()]


def get_alert_settings(account_id: int) -> dict[str, Any]:
    """Return an account's alert config, filling any unset fields with the
    defaults. Always returns a full dict (never None) so callers don't branch."""
    out = dict(_ALERT_DEFAULTS)
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(
            "SELECT email_enabled, slack_webhook_url, webhook_url, rule_drift, "
            "rule_budget, rule_loop, rule_error, budget_warn_pct, loop_threshold "
            f"FROM alert_settings WHERE account_id = {PH}",
            (account_id,),
        )
        row = cur.fetchone()
    if row is not None:
        for f in _ALERT_BOOL_FIELDS:
            out[f] = bool(_row_get(row, f))
        for f in _ALERT_INT_FIELDS:
            v = _row_get(row, f)
            if v is not None:
                out[f] = int(v)
        for f in _ALERT_STR_FIELDS:
            out[f] = _row_get(row, f) or None
    return out


def upsert_alert_settings(account_id: int, fields: dict[str, Any]) -> dict[str, Any]:
    """Create or update an account's alert config with the provided fields
    (partial updates supported). Unknown keys are ignored. Returns the full
    resolved settings after the write."""
    allowed = set(_ALERT_BOOL_FIELDS) | set(_ALERT_INT_FIELDS) | set(_ALERT_STR_FIELDS)
    clean: dict[str, Any] = {}
    for k, v in (fields or {}).items():
        if k not in allowed:
            continue
        if k in _ALERT_BOOL_FIELDS:
            clean[k] = 1 if v else 0
        elif k in _ALERT_INT_FIELDS:
            try:
                clean[k] = int(v)
            except (TypeError, ValueError):
                continue
        else:
            s = (str(v).strip() if v is not None else "")
            clean[k] = s or None
    # Clamp the budget percentage to a sane 1–100 range.
    if "budget_warn_pct" in clean:
        clean["budget_warn_pct"] = max(1, min(100, clean["budget_warn_pct"]))
    if "loop_threshold" in clean:
        clean["loop_threshold"] = max(2, clean["loop_threshold"])

    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(
            f"SELECT 1 FROM alert_settings WHERE account_id = {PH}", (account_id,)
        )
        exists = cur.fetchone() is not None
        if not exists:
            # Seed a row from defaults, then apply the update on top.
            base = dict(_ALERT_DEFAULTS)
            merged = {
                **{k: (1 if base[k] else 0) for k in _ALERT_BOOL_FIELDS},
                **{k: base[k] for k in _ALERT_INT_FIELDS},
                **{k: base[k] for k in _ALERT_STR_FIELDS},
            }
            merged.update(clean)
            cols = ["account_id", *merged.keys()]
            ph = ", ".join([PH] * len(cols))
            cur.execute(
                f"INSERT INTO alert_settings ({', '.join(cols)}) VALUES ({ph})",
                (account_id, *merged.values()),
            )
        elif clean:
            sets = ", ".join(f"{k} = {PH}" for k in clean)
            ts = "NOW()" if USE_POSTGRES else "CURRENT_TIMESTAMP"
            cur.execute(
                f"UPDATE alert_settings SET {sets}, updated_at = {ts} "
                f"WHERE account_id = {PH}",
                (*clean.values(), account_id),
            )
    return get_alert_settings(account_id)


def was_alerted(
    account_id: int,
    rule: str,
    subject_key: str,
    state_key: str,
    cooldown_seconds: int,
) -> bool:
    """True if a matching alert was already fired inside the cooldown window —
    so a standing condition alerts once per window, not every sweep. A new
    `state_key` (e.g. budget crossing 100% after 80%) is treated as fresh."""
    with _connect() as conn, _cursor(conn) as cur:
        if USE_POSTGRES:
            cur.execute(
                "SELECT 1 FROM alert_log WHERE account_id = %s AND rule = %s "
                "AND subject_key = %s AND state_key = %s "
                "AND created_at > NOW() - (%s || ' seconds')::interval LIMIT 1",
                (account_id, rule, subject_key, state_key, str(int(cooldown_seconds))),
            )
        else:
            cur.execute(
                "SELECT 1 FROM alert_log WHERE account_id = ? AND rule = ? "
                "AND subject_key = ? AND state_key = ? "
                "AND created_at > datetime('now', ?) LIMIT 1",
                (account_id, rule, subject_key, state_key, f"-{int(cooldown_seconds)} seconds"),
            )
        return cur.fetchone() is not None


def record_alert(account_id: int, rule: str, subject_key: str, state_key: str) -> None:
    """Record that an alert fired (for dedup + history)."""
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(
            "INSERT INTO alert_log (account_id, rule, subject_key, state_key) "
            f"VALUES ({PH}, {PH}, {PH}, {PH})",
            (account_id, rule, subject_key, state_key),
        )


# ---------------------------------------------------------------------------
# Agent deletion (full sweep across every per-agent table)
# ---------------------------------------------------------------------------


# Tables that hold per-agent rows keyed on `(service_name, agent_id)`.
# Ordered so referential constraints (the agent_owners → team_members
# foreign key) don't trip — agent_owners has the FK, but the join
# direction (owner row references team_member) means deleting owners
# is safe at any point. Order is otherwise irrelevant.
#
# All of these share the same `(service_name, account_id, agent_id)`
# column shape, so the simple loop in delete_agent handles them. Tables
# with a different shape — agent_connections (source/target pairs) and
# workflows (+ their step/participant/edge children) — are swept
# separately below.
_PER_AGENT_TABLES = (
    "spans",
    "descriptions",
    "agent_registrations",
    "agent_display_names",
    "agent_owners",
    "agent_insights",
    "agent_budgets",
)


def delete_agent(
    service_name: str,
    account_id: int | None = None,
    agent_id: str | None = None,
) -> dict[str, int]:
    """Delete every row Oversee knows about for an agent.

    When `agent_id` is provided, scopes to that sub-agent only
    (matches via `COALESCE(agent_id, 'main') = ?`). When omitted,
    drops the whole `service_name` — every sub-agent under it.

    The sweep covers the per-agent tables (spans, descriptions,
    registrations, display names, owners, cached insights, budgets),
    plus two tables with a different key shape:

      * `agent_connections` — edges where the agent is the *source* or
        the *target* of a detected agent→agent call.
      * `workflows` owned by the agent (`agent_service_name`/`agent_id`),
        cascading to their `workflow_steps`, `workflow_participants`,
        and `workflow_edges` (SQLite doesn't enforce the FK cascade, so
        children are dropped explicitly — mirrors `delete_workflow`).

    Workflows merely *referencing* the agent as a step/participant while
    owned by a different agent are left intact; only workflows the agent
    owns are removed.

    Account-scoped: NULL-account rows (pre-multi-tenant local dev) are
    only touched when `account_id` is None. Cross-tenant safety
    matters here more than anywhere else since this is irreversible.

    Returns a `{table: rows_deleted}` summary — useful for logging and
    for the API response so the caller can audit what just happened.
    All DELETEs run inside the same `_connect()` context, which
    auto-commits on success and rolls back on exception — partial
    deletion is impossible.
    """
    account_clause = (
        f"AND account_id = {PH}"
        if account_id is not None
        else "AND account_id IS NULL"
    )
    agent_clause = (
        f"AND COALESCE(agent_id, 'main') = {PH}"
        if agent_id is not None
        else ""
    )

    base_args: list[Any] = [service_name]
    if account_id is not None:
        base_args.append(account_id)
    if agent_id is not None:
        base_args.append(agent_id)
    args = tuple(base_args)

    summary: dict[str, int] = {}
    with _connect() as conn, _cursor(conn) as cur:
        for table in _PER_AGENT_TABLES:
            sql = (
                f"DELETE FROM {table} "
                f"WHERE service_name = {PH} {account_clause} {agent_clause}"
            )
            cur.execute(sql, args)
            summary[table] = cur.rowcount

        # agent_connections: the agent can sit on either end of an edge,
        # and the columns are source_*/target_* rather than service_name,
        # so it needs its own WHERE. Drop any edge touching the agent.
        conn_where = [
            "account_id = " + PH if account_id is not None else "account_id IS NULL"
        ]
        conn_args: list[Any] = [account_id] if account_id is not None else []
        if agent_id is not None:
            conn_where.append(
                f"((source_service = {PH} AND COALESCE(source_agent_id, 'main') = {PH})"
                f" OR (target_service = {PH} AND COALESCE(target_agent_id, 'main') = {PH}))"
            )
            conn_args += [service_name, agent_id, service_name, agent_id]
        else:
            conn_where.append(
                f"(source_service = {PH} OR target_service = {PH})"
            )
            conn_args += [service_name, service_name]
        cur.execute(
            "DELETE FROM agent_connections WHERE " + " AND ".join(conn_where),
            tuple(conn_args),
        )
        summary["agent_connections"] = cur.rowcount

        # workflows owned by the agent, plus their step/participant/edge
        # children. workflows key on agent_service_name (value is the same
        # service_name) + agent_id, so the existing account/agent clauses
        # and `args` apply unchanged.
        cur.execute(
            f"SELECT id FROM workflows "
            f"WHERE agent_service_name = {PH} {account_clause} {agent_clause}",
            args,
        )
        wf_ids = [row["id"] for row in cur.fetchall()]
        wf_children = {
            "workflow_edges": 0,
            "workflow_participants": 0,
            "workflow_steps": 0,
        }
        for wf_id in wf_ids:
            # Edges reference steps, so drop edges + participants first.
            for child in wf_children:
                cur.execute(
                    f"DELETE FROM {child} WHERE workflow_id = {PH}", (wf_id,)
                )
                wf_children[child] += cur.rowcount
        cur.execute(
            f"DELETE FROM workflows "
            f"WHERE agent_service_name = {PH} {account_clause} {agent_clause}",
            args,
        )
        summary["workflows"] = cur.rowcount
        summary.update(wf_children)
    return summary


# ---------------------------------------------------------------------------
# Accounts and API keys
# ---------------------------------------------------------------------------


class EmailAlreadyExistsError(Exception):
    """Raised when create_account hits the unique constraint on email."""


def create_account(
    email: str,
    account_type: str = "individual",
    name: str | None = None,
) -> dict[str, Any]:
    """Insert a new account (the org/tenant). Raises EmailAlreadyExistsError
    if the email is taken. Returns {id, email, account_type, name, created_at}.

    The RETURNING clause differs between backends: Postgres has it natively;
    SQLite has it since 3.35 (March 2021) but we use lastrowid + SELECT as a
    safer fallback that works on any 3.x.
    """
    email = (email or "").strip().lower()
    if not email:
        raise ValueError("email is required")
    account_type = account_type if account_type in ("individual", "business") else "individual"
    name = (name or "").strip() or None
    cols = "(email, account_type, name)"
    sel = "id, email, account_type, name, created_at"

    try:
        with _connect() as conn, _cursor(conn) as cur:
            if USE_POSTGRES:
                cur.execute(
                    f"INSERT INTO accounts {cols} VALUES ({PH}, {PH}, {PH}) RETURNING {sel}",
                    (email, account_type, name),
                )
                row = cur.fetchone()
            else:
                cur.execute(
                    f"INSERT INTO accounts {cols} VALUES ({PH}, {PH}, {PH})",
                    (email, account_type, name),
                )
                new_id = cur.lastrowid
                cur.execute(
                    f"SELECT {sel} FROM accounts WHERE id = {PH}", (new_id,)
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
        "account_type": row["account_type"],
        "name": row["name"],
        "created_at": _ts_to_str(row["created_at"]),
    }


# Plan → number of *viewable* agents. None = unlimited. The single
# server-side source of truth for the limit (the cardinal rule of this
# feature is that ingestion is NEVER gated — only the view is).
_AGENT_LIMIT_BY_PLAN: dict[str, int | None] = {
    "free": 5,
    "starter": 15,
    "pro": 50,
    "enterprise": None,  # unlimited
}


def agent_limit(plan: str | None) -> int | None:
    """Viewable-agent cap for a plan. None = unlimited. Unknown/None → free."""
    return _AGENT_LIMIT_BY_PLAN.get((plan or "free"), _AGENT_LIMIT_BY_PLAN["free"])


def get_account(account_id: int) -> dict[str, Any] | None:
    """Return an org by id: {id, email, name, account_type, created_at,
    onboarded_at, plan}."""
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(
            "SELECT id, email, account_type, name, created_at, onboarded_at, plan "
            f"FROM accounts WHERE id = {PH}",
            (account_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "email": row["email"],
        "account_type": row["account_type"],
        "name": row["name"],
        "created_at": _ts_to_str(row["created_at"]),
        "onboarded_at": _ts_to_str(_row_get(row, "onboarded_at")),
        "plan": _row_get(row, "plan") or "free",
    }


def set_account_plan(account_id: int, plan: str) -> None:
    """Set an account's plan tier. Paid tiers are written ONLY by the verified
    Stripe webhook (main.billing_webhook → see billing.py); a downgrade to
    'free' may also be applied directly by PUT /account/plan. Never call this
    from a client-facing path for a paid tier without a payment check, or you
    reopen the free self-upgrade hole. Raising the plan unlocks previously
    locked agents instantly because their telemetry was never gated."""
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(
            f"UPDATE accounts SET plan = {PH} WHERE id = {PH}",
            ((plan or "free"), account_id),
        )


def set_account_stripe_customer(account_id: int, customer_id: str | None) -> None:
    """Record the account's Stripe customer id (from a completed checkout) so we
    can later open the Stripe Customer Portal for them. No-op on a blank id."""
    if not customer_id:
        return
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(
            f"UPDATE accounts SET stripe_customer_id = {PH} WHERE id = {PH}",
            (customer_id, account_id),
        )


def get_account_stripe_customer(account_id: int) -> str | None:
    """Return the account's stored Stripe customer id, or None if it has never
    completed a checkout."""
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(
            f"SELECT stripe_customer_id FROM accounts WHERE id = {PH}",
            (account_id,),
        )
        row = cur.fetchone()
    return (_row_get(row, "stripe_customer_id") if row else None) or None


def get_locked_state(account_id: int | None) -> dict[str, Any]:
    """Which agents are *view-locked* for this account under its plan.

    The plan limit counts *instances* (distinct `service_name`), NOT sub-agents:
    a single instance with many sub-agents consumes one slot, and its sub-agents
    are free. Instances are ordered by first_seen ascending; the first `limit`
    stay unlocked, older-beyond-limit instances lock (with all their sub-agents).
    Unlimited plans (limit None) lock nothing. This informs the VIEW only —
    telemetry is never gated, so locked instances still have every span recorded.

    Returns {plan, limit, agent_count, locked_count,
             locked: set[service_name],
             first_seen: {service_name: iso}}.
    where agent_count / locked_count are INSTANCE counts.
    """
    plan = "free"
    if account_id is not None:
        acct = get_account(account_id)
        if acct:
            plan = acct.get("plan", "free")
    limit = agent_limit(plan)

    account_filter = f"AND account_id = {PH}" if account_id is not None else ""
    # One row per instance; first_seen is the earliest across its sub-agents.
    sql = f"""
        SELECT service_name,
               MIN(CASE WHEN start_time_unix >= {_FIRST_SEEN_FLOOR_NS}
                        THEN start_time_unix END) AS first_seen_ns
        FROM spans
        WHERE 1=1 {account_filter}
        GROUP BY service_name
    """
    args = (account_id,) if account_id is not None else ()
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(sql, args)
        rows = cur.fetchall()

    # Oldest first. Instances with no valid (post-floor) first_seen sort last, so
    # an undated/garbage-timestamp instance never preempts an established one.
    items = [(r["service_name"], r["first_seen_ns"]) for r in rows]
    items.sort(key=lambda t: (t[1] is None, t[1] or 0))

    first_seen = {s: _ns_to_iso(ns) for (s, ns) in items}
    locked: set[str] = set()
    if limit is not None:
        locked = {s for (s, _ns) in items[limit:]}
    return {
        "plan": plan,
        "limit": limit,
        "agent_count": len(items),
        "locked_count": len(locked),
        "locked": locked,
        "first_seen": first_seen,
    }


def update_account_profile(account_id: int | None, name: str | None) -> None:
    """Set the org's display name (used by the onboarding 'name workspace' step)."""
    if account_id is None:
        return
    clean = (name or "").strip() or None
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(
            f"UPDATE accounts SET name = {PH} WHERE id = {PH}", (clean, account_id)
        )


def mark_account_onboarded(account_id: int | None) -> None:
    """Stamp onboarded_at so the post-signup wizard never shows again.
    Idempotent — only sets it the first time."""
    if account_id is None:
        return
    now_sql = "NOW()" if USE_POSTGRES else "CURRENT_TIMESTAMP"
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(
            f"UPDATE accounts SET onboarded_at = COALESCE(onboarded_at, {now_sql}) "
            f"WHERE id = {PH}",
            (account_id,),
        )


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
# Users, sessions, invites (real human auth)
# ---------------------------------------------------------------------------


class UserEmailExistsError(Exception):
    """Raised when a user email collides with the global UNIQUE constraint."""


def has_any_users() -> bool:
    """True if any user exists. Pairs with has_any_keys() so the local-dev
    no-auth passthrough closes once either credential type exists."""
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute("SELECT 1 FROM users LIMIT 1")
        return cur.fetchone() is not None


def _user_public(row: Any) -> dict[str, Any]:
    """Shape a user row for the API — never includes password_hash."""
    return {
        "id": row["id"],
        "account_id": row["account_id"],
        "email": row["email"],
        "name": row["name"],
        "role": row["role"],
        "created_at": _ts_to_str(row["created_at"]),
        "last_login_at": _ts_to_str(row["last_login_at"]),
    }


_USER_COLS = "id, account_id, email, name, role, created_at, last_login_at"


def create_user(
    account_id: int,
    email: str,
    name: str | None,
    role: str = "member",
    password_hash: str | None = None,
) -> dict[str, Any]:
    """Insert a user (login) into an org. Raises UserEmailExistsError on a
    duplicate email. Returns the public user shape (no password_hash)."""
    email = (email or "").strip().lower()
    if not email:
        raise ValueError("email is required")
    role = role if role in ("owner", "member") else "member"
    name = (name or "").strip() or None
    cols = "(account_id, email, name, role, password_hash)"
    vals = (account_id, email, name, role, password_hash)
    try:
        with _connect() as conn, _cursor(conn) as cur:
            if USE_POSTGRES:
                cur.execute(
                    f"INSERT INTO users {cols} VALUES ({PH}, {PH}, {PH}, {PH}, {PH}) "
                    f"RETURNING {_USER_COLS}",
                    vals,
                )
                row = cur.fetchone()
            else:
                cur.execute(
                    f"INSERT INTO users {cols} VALUES ({PH}, {PH}, {PH}, {PH}, {PH})",
                    vals,
                )
                cur.execute(
                    f"SELECT {_USER_COLS} FROM users WHERE id = {PH}", (cur.lastrowid,)
                )
                row = cur.fetchone()
    except Exception as e:
        msg = str(e).lower()
        if "unique" in msg or "duplicate" in msg:
            raise UserEmailExistsError(email) from e
        raise
    return _user_public(row)


def get_user_by_email(email: str) -> dict[str, Any] | None:
    """Look up a user by email INCLUDING password_hash — internal use only
    (login / claim). Never return this shape from an endpoint."""
    email = (email or "").strip().lower()
    if not email:
        return None
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(
            f"SELECT {_USER_COLS}, password_hash FROM users WHERE email = {PH}",
            (email,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    out = _user_public(row)
    out["password_hash"] = row["password_hash"]
    return out


def get_user_by_id(user_id: int) -> dict[str, Any] | None:
    """Public user shape by id (no password_hash)."""
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(f"SELECT {_USER_COLS} FROM users WHERE id = {PH}", (user_id,))
        row = cur.fetchone()
    return _user_public(row) if row else None


def set_user_password(user_id: int, password_hash: str) -> None:
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(
            f"UPDATE users SET password_hash = {PH} WHERE id = {PH}",
            (password_hash, user_id),
        )


_PW_RESET_TTL_SECONDS = 3600  # 1 hour — short by design for a reset link


def create_password_reset(user_id: int, ttl_seconds: int = _PW_RESET_TTL_SECONDS) -> str:
    """Mint a one-time password-reset token for a user; returns the raw token
    (shown once, embedded in the emailed link). Only its hash is stored."""
    raw, token_hash = _new_token()
    expires_at = (_utcnow() + timedelta(seconds=ttl_seconds)).isoformat()
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(
            "INSERT INTO password_reset_tokens (token_hash, user_id, expires_at) "
            f"VALUES ({PH}, {PH}, {PH})",
            (token_hash, user_id, expires_at),
        )
    return raw


def consume_password_reset(raw_token: str | None) -> int | None:
    """Validate + single-use-consume a reset token. Returns the user_id when
    the token exists, is unexpired, and is unused (atomically marking it used);
    otherwise None. Reuse/expired/invalid all return None."""
    if not raw_token:
        return None
    token_hash = _hash_token(raw_token)
    now_iso = _utcnow().isoformat()
    now_sql = "NOW()" if USE_POSTGRES else "CURRENT_TIMESTAMP"
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(
            "SELECT id, user_id FROM password_reset_tokens "
            f"WHERE token_hash = {PH} AND used_at IS NULL AND expires_at > {PH}",
            (token_hash, now_iso),
        )
        row = cur.fetchone()
        if row is None:
            return None
        # Mark used in the same connection; the UNIQUE token_hash + used_at
        # guard makes a double-spend a no-op.
        cur.execute(
            f"UPDATE password_reset_tokens SET used_at = {now_sql} "
            f"WHERE id = {PH} AND used_at IS NULL",
            (row["id"],),
        )
        return row["user_id"]


def touch_user_login(user_id: int) -> None:
    now_sql = "NOW()" if USE_POSTGRES else "CURRENT_TIMESTAMP"
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(
            f"UPDATE users SET last_login_at = {now_sql} WHERE id = {PH}", (user_id,)
        )


def get_org_users(account_id: int) -> list[dict[str, Any]]:
    """All users in an org, owners first then by creation order."""
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(
            f"SELECT {_USER_COLS} FROM users WHERE account_id = {PH} "
            "ORDER BY CASE WHEN role = 'owner' THEN 0 ELSE 1 END, id ASC",
            (account_id,),
        )
        return [_user_public(r) for r in cur.fetchall()]


def count_owners(account_id: int) -> int:
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(
            f"SELECT COUNT(*) AS n FROM users WHERE account_id = {PH} AND role = 'owner'",
            (account_id,),
        )
        return int(cur.fetchone()["n"])


def delete_user(account_id: int, user_id: int) -> bool:
    """Remove a user from an org (account-scoped) plus their sessions."""
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(f"DELETE FROM sessions WHERE user_id = {PH}", (user_id,))
        cur.execute(
            f"DELETE FROM users WHERE id = {PH} AND account_id = {PH}",
            (user_id, account_id),
        )
        return cur.rowcount > 0


def create_session(
    user_id: int, account_id: int, ttl_seconds: int = _SESSION_TTL_SECONDS
) -> str:
    """Mint a session; store only the token hash. Returns the raw token."""
    raw, token_hash = _new_token()
    expires_at = (_utcnow() + timedelta(seconds=ttl_seconds)).isoformat()
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(
            "INSERT INTO sessions (token_hash, user_id, account_id, expires_at) "
            f"VALUES ({PH}, {PH}, {PH}, {PH})",
            (token_hash, user_id, account_id, expires_at),
        )
    return raw


def resolve_session(raw_token: str | None) -> dict[str, Any] | None:
    """Hot path: resolve a raw bearer token to its user + org. Returns None
    when unknown or expired. Slides the expiry forward on use."""
    if not raw_token:
        return None
    token_hash = _hash_token(raw_token)
    now_iso = _utcnow().isoformat()
    new_expiry = (_utcnow() + timedelta(seconds=_SESSION_TTL_SECONDS)).isoformat()
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(
            "SELECT s.user_id, s.account_id, u.role, u.email, u.name "
            "FROM sessions s JOIN users u ON u.id = s.user_id "
            f"WHERE s.token_hash = {PH} AND s.expires_at > {PH}",
            (token_hash, now_iso),
        )
        row = cur.fetchone()
        if row is None:
            # Opportunistically clear an expired/dead row.
            cur.execute(
                f"DELETE FROM sessions WHERE token_hash = {PH}", (token_hash,)
            )
            return None
        cur.execute(
            f"UPDATE sessions SET last_seen_at = {PH}, expires_at = {PH} "
            f"WHERE token_hash = {PH}",
            (now_iso, new_expiry, token_hash),
        )
        return {
            "account_id": row["account_id"],
            "user_id": row["user_id"],
            "role": row["role"],
            "email": row["email"],
            "name": row["name"],
        }


def delete_session(raw_token: str | None) -> None:
    if not raw_token:
        return
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(
            f"DELETE FROM sessions WHERE token_hash = {PH}", (_hash_token(raw_token),)
        )


def delete_sessions_for_user(
    user_id: int, except_raw_token: str | None = None
) -> None:
    """Invalidate all of a user's sessions (used on password change), keeping
    the caller's current session when provided."""
    with _connect() as conn, _cursor(conn) as cur:
        if except_raw_token:
            cur.execute(
                f"DELETE FROM sessions WHERE user_id = {PH} AND token_hash != {PH}",
                (user_id, _hash_token(except_raw_token)),
            )
        else:
            cur.execute(f"DELETE FROM sessions WHERE user_id = {PH}", (user_id,))


# ---------------------------------------------------------------------------
# OAuth 2.0 authorization codes (for ChatGPT Actions integration)
# ---------------------------------------------------------------------------

_OAUTH_CODE_TTL_SECONDS = 5 * 60  # 5 minutes


def create_oauth_code(
    account_id: int,
    user_id: int | None,
    client_id: str,
    redirect_uri: str,
    scope: str = "",
    state: str | None = None,
) -> str:
    """Issue a short-lived authorization code. Returns the raw code (shown
    once). ChatGPT exchanges it at /oauth/token for an access token."""
    raw = secrets.token_urlsafe(32)
    code_hash = hashlib.sha256(raw.encode()).hexdigest()
    if USE_POSTGRES:
        sql = (
            "INSERT INTO oauth_codes (code, account_id, user_id, client_id, "
            "redirect_uri, scope, state, expires_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, NOW() + INTERVAL '%s seconds')"
        )
        args = (code_hash, account_id, user_id, client_id, redirect_uri,
                scope, state, _OAUTH_CODE_TTL_SECONDS)
    else:
        sql = (
            "INSERT INTO oauth_codes (code, account_id, user_id, client_id, "
            "redirect_uri, scope, state, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now', '+' || ? || ' seconds'))"
        )
        args = (code_hash, account_id, user_id, client_id, redirect_uri,
                scope, state, str(_OAUTH_CODE_TTL_SECONDS))
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(sql, args)
    return raw


def exchange_oauth_code(raw_code: str, client_id: str, redirect_uri: str) -> dict[str, Any] | None:
    """Exchange an authorization code for an access token (session). Returns
    {access_token, token_type, expires_in, refresh_token} or None if the code
    is invalid/expired/already-used."""
    code_hash = hashlib.sha256(raw_code.encode()).hexdigest()
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(
            f"SELECT id, account_id, user_id, client_id, redirect_uri, used, expires_at "
            f"FROM oauth_codes WHERE code = {PH}",
            (code_hash,),
        )
        row = cur.fetchone()
        if not row:
            return None
        # Validate: not used, not expired, client/redirect match
        if row["used"]:
            return None
        if row["client_id"] != client_id:
            return None
        if row["redirect_uri"] != redirect_uri:
            return None
        # Expiry check
        from datetime import datetime, timezone
        raw_exp = row["expires_at"]
        try:
            if isinstance(raw_exp, str):
                exp = datetime.fromisoformat(raw_exp.replace("Z", "+00:00"))
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
            else:
                exp = raw_exp if raw_exp.tzinfo else raw_exp.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) > exp:
                return None
        except (ValueError, AttributeError):
            pass  # Can't parse → let it through, better than blocking
        # Mark as used
        cur.execute(
            f"UPDATE oauth_codes SET used = {PH} WHERE id = {PH}",
            (True if USE_POSTGRES else 1, row["id"]),
        )
    # Create a long-lived session token for the account
    account_id = row["account_id"]
    user_id = row["user_id"]
    ttl = 90 * 24 * 60 * 60  # 90 days
    raw_token = create_session(user_id or 0, account_id, ttl_seconds=ttl)
    return {
        "access_token": raw_token,
        "token_type": "bearer",
        "expires_in": ttl,
        "refresh_token": raw_token,  # Same token; ChatGPT expects this field
    }


def create_invite(
    account_id: int,
    email: str,
    role: str,
    invited_by_user_id: int | None,
    ttl_seconds: int = _INVITE_TTL_SECONDS,
) -> dict[str, Any]:
    """Create a one-time invite. Returns {token (raw), email, role,
    expires_at} — the raw token is shown once (in the invite link)."""
    email = (email or "").strip().lower()
    role = role if role in ("owner", "member") else "member"
    raw, token_hash = _new_token()
    expires_at = (_utcnow() + timedelta(seconds=ttl_seconds)).isoformat()
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(
            "INSERT INTO invites (token_hash, account_id, email, role, "
            "invited_by_user_id, expires_at) "
            f"VALUES ({PH}, {PH}, {PH}, {PH}, {PH}, {PH})",
            (token_hash, account_id, email, role, invited_by_user_id, expires_at),
        )
    return {"token": raw, "email": email, "role": role, "expires_at": expires_at}


def list_invites(account_id: int) -> list[dict[str, Any]]:
    """Pending (unaccepted, unexpired) invites for an org. Never leaks the
    token or its hash."""
    now_iso = _utcnow().isoformat()
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(
            "SELECT id, email, role, created_at, expires_at FROM invites "
            f"WHERE account_id = {PH} AND accepted_at IS NULL AND expires_at > {PH} "
            "ORDER BY created_at DESC, id DESC",
            (account_id, now_iso),
        )
        return [
            {
                "id": r["id"],
                "email": r["email"],
                "role": r["role"],
                "created_at": _ts_to_str(r["created_at"]),
                "expires_at": _ts_to_str(r["expires_at"]),
            }
            for r in cur.fetchall()
        ]


def revoke_invite(account_id: int, invite_id: int) -> bool:
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(
            f"DELETE FROM invites WHERE id = {PH} AND account_id = {PH}",
            (invite_id, account_id),
        )
        return cur.rowcount > 0


def accept_invite(
    raw_token: str, name: str | None, password_hash: str
) -> dict[str, Any]:
    """Redeem an invite: re-validate, create the member user, and stamp the
    invite accepted — all in one transaction so a token can't be reused via a
    race. Raises LookupError on an invalid/expired/used token and
    UserEmailExistsError when the invited email already has a user."""
    token_hash = _hash_token(raw_token)
    now_iso = _utcnow().isoformat()
    name = (name or "").strip() or None
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(
            "SELECT id, account_id, email, role FROM invites "
            f"WHERE token_hash = {PH} AND accepted_at IS NULL AND expires_at > {PH}",
            (token_hash, now_iso),
        )
        inv = cur.fetchone()
        if inv is None:
            raise LookupError("invite invalid, expired, or already used")
        account_id = inv["account_id"]
        email = inv["email"]
        role = inv["role"] if inv["role"] in ("owner", "member") else "member"
        cols = "(account_id, email, name, role, password_hash)"
        vals = (account_id, email, name, role, password_hash)
        try:
            if USE_POSTGRES:
                cur.execute(
                    f"INSERT INTO users {cols} VALUES ({PH}, {PH}, {PH}, {PH}, {PH}) "
                    f"RETURNING {_USER_COLS}",
                    vals,
                )
                urow = cur.fetchone()
            else:
                cur.execute(
                    f"INSERT INTO users {cols} VALUES ({PH}, {PH}, {PH}, {PH}, {PH})",
                    vals,
                )
                cur.execute(
                    f"SELECT {_USER_COLS} FROM users WHERE id = {PH}", (cur.lastrowid,)
                )
                urow = cur.fetchone()
        except Exception as e:
            msg = str(e).lower()
            if "unique" in msg or "duplicate" in msg:
                raise UserEmailExistsError(email) from e
            raise
        now_sql = "NOW()" if USE_POSTGRES else "CURRENT_TIMESTAMP"
        cur.execute(
            f"UPDATE invites SET accepted_at = {now_sql} WHERE id = {PH}",
            (inv["id"],),
        )
        return _user_public(urow)


# ---------------------------------------------------------------------------
# Agent-to-agent connections (auto-detected + operator-curated)
# ---------------------------------------------------------------------------

_CONN_WINDOW_DAYS = 30
_CONN_STATUSES = {"detected", "confirmed", "dismissed", "manual"}


def _connection_row(r: Any) -> dict[str, Any]:
    via_raw = _row_get(r, "via_operations")
    try:
        via = json.loads(via_raw) if via_raw else []
    except (TypeError, ValueError):
        via = []
    return {
        "id": r["id"],
        "source_service": r["source_service"],
        "source_agent_id": r["source_agent_id"],
        "target_service": r["target_service"],
        "target_agent_id": r["target_agent_id"],
        "status": r["status"],
        "call_count": int(r["call_count"] or 0),
        "trace_count": int(r["trace_count"] or 0),
        "total_tokens": int(_row_get(r, "total_tokens") or 0),
        "via_operations": via if isinstance(via, list) else [],
        "sample": _row_get(r, "sample"),
        "first_seen": _ts_to_str(r["first_seen"]),
        "last_seen": _ts_to_str(r["last_seen"]),
    }


def detect_agent_connections(
    account_id: int | None, days: int = _CONN_WINDOW_DAYS
) -> int:
    """Find directed agent→agent edges from shared traces and upsert them.

    An edge exists when a span's parent lives in a *different* agent within
    the same trace (parent_agent called child_agent). Metrics (call/trace
    counts, last_seen) are recomputed over the window each run; the operator
    `status` is preserved — re-detection never un-confirms or un-dismisses.
    Returns the number of edges upserted.
    """
    window_ns = time.time_ns() - days * 24 * 60 * 60 * 1_000_000_000
    acct = (
        f"AND account_id = {PH}" if account_id is not None else "AND account_id IS NULL"
    )
    base_args: tuple[Any, ...] = () if account_id is None else (account_id,)

    edges: dict[tuple, dict[str, Any]] = {}
    with _connect() as conn, _cursor(conn) as cur:
        # 1. Traces (within the window) that touch more than one agent.
        cur.execute(
            f"SELECT trace_id FROM spans WHERE start_time_unix >= {PH} {acct} "
            "GROUP BY trace_id "
            "HAVING COUNT(DISTINCT service_name || '/' || COALESCE(agent_id, 'main')) > 1",
            (window_ns, *base_args),
        )
        trace_ids = [r["trace_id"] for r in cur.fetchall() if r["trace_id"]]
        if not trace_ids:
            return 0

        # 2. Pull the spans for those traces and link parent→child across agents.
        chunk = 200
        for i in range(0, len(trace_ids), chunk):
            batch = trace_ids[i : i + chunk]
            ph = ", ".join([PH] * len(batch))
            cur.execute(
                "SELECT trace_id, span_id, parent_span_id, service_name, agent_id, "
                "span_name, total_tokens, attributes, "
                f"start_time_unix FROM spans WHERE trace_id IN ({ph}) {acct}",
                (*batch, *base_args),
            )
            rows = cur.fetchall()
            by_span = {
                r["span_id"]: {
                    "svc": r["service_name"],
                    "aid": r["agent_id"] or "main",
                    "parent": r["parent_span_id"],
                    "start": r["start_time_unix"],
                    "trace": r["trace_id"],
                    "op": r["span_name"],
                    "tokens": r["total_tokens"] or 0,
                    "attrs": r["attributes"],
                }
                for r in rows
            }
            for s in by_span.values():
                parent = s["parent"]
                if not parent or parent not in by_span:
                    continue
                p = by_span[parent]
                if (p["svc"], p["aid"]) == (s["svc"], s["aid"]):
                    continue  # same agent — not a crossing
                key = (p["svc"], p["aid"], s["svc"], s["aid"])
                e = edges.setdefault(
                    key,
                    {
                        "calls": 0,
                        "traces": set(),
                        "first": s["start"],
                        "last": s["start"],
                        "ops": {},
                        "tokens": 0,
                        "sample": None,
                    },
                )
                e["calls"] += 1
                e["traces"].add(s["trace"])
                e["first"] = min(e["first"], s["start"])
                e["last"] = max(e["last"], s["start"])
                # What's transferred: the child operation, token volume, and a
                # content sample if output-capture was on for that span.
                if s["op"]:
                    e["ops"][s["op"]] = e["ops"].get(s["op"], 0) + 1
                e["tokens"] += int(s["tokens"] or 0)
                if e["sample"] is None:
                    e["sample"] = _capture_sample(s["attrs"])

        if not edges:
            return 0

        now_sql = "NOW()" if USE_POSTGRES else "CURRENT_TIMESTAMP"
        touched = 0
        for (ssvc, said, tsvc, taid), e in edges.items():
            calls = e["calls"]
            traces = len(e["traces"])
            last_iso = _ns_to_iso(e["last"])
            first_iso = _ns_to_iso(e["first"])
            # Top bridging operations (what's transferred), most-frequent first.
            ops = sorted(e["ops"].items(), key=lambda kv: -kv[1])[:5]
            via_json = json.dumps([{"operation": o, "count": c} for o, c in ops])
            tokens = e["tokens"]
            sample = e["sample"]
            where = (
                f"account_id = {PH} AND source_service = {PH} AND source_agent_id = {PH} "
                f"AND target_service = {PH} AND target_agent_id = {PH}"
                if account_id is not None
                else f"account_id IS NULL AND source_service = {PH} AND source_agent_id = {PH} "
                f"AND target_service = {PH} AND target_agent_id = {PH}"
            )
            key_args = (
                (account_id, ssvc, said, tsvc, taid)
                if account_id is not None
                else (ssvc, said, tsvc, taid)
            )
            # Update metrics (preserve status); insert if absent.
            cur.execute(
                f"UPDATE agent_connections SET call_count = {PH}, trace_count = {PH}, "
                f"last_seen = {PH}, via_operations = {PH}, total_tokens = {PH}, "
                f"sample = {PH}, updated_at = {now_sql} WHERE {where}",
                (calls, traces, last_iso, via_json, tokens, sample, *key_args),
            )
            if cur.rowcount == 0:
                cur.execute(
                    "INSERT INTO agent_connections (account_id, source_service, "
                    "source_agent_id, target_service, target_agent_id, status, "
                    "call_count, trace_count, first_seen, last_seen, via_operations, "
                    "total_tokens, sample) "
                    f"VALUES ({PH}, {PH}, {PH}, {PH}, {PH}, 'detected', {PH}, {PH}, {PH}, {PH}, {PH}, {PH}, {PH})",
                    (account_id, ssvc, said, tsvc, taid, calls, traces, first_iso,
                     last_iso, via_json, tokens, sample),
                )
            touched += 1
    return touched


# Capture-content attribute keys (set by the plugin/SDK when capture is on).
# New `trovis.*` first, then legacy `oversee.*` (live agents + historical rows).
_CAPTURE_KEYS = (
    "trovis.message.content",
    "trovis.response.content",
    "trovis.tool.result",
    "oversee.message.content",
    "oversee.response.content",
    "oversee.tool.result",
)


def _capture_sample(attrs_json: str | None) -> str | None:
    """Pull a short content sample from a span's attributes — only present
    when output-capture was enabled for that span. Returns None otherwise."""
    if not attrs_json:
        return None
    try:
        attrs = json.loads(attrs_json)
    except (TypeError, ValueError):
        return None
    if not isinstance(attrs, dict):
        return None
    for k in _CAPTURE_KEYS:
        v = attrs.get(k)
        if isinstance(v, str) and v.strip():
            s = v.strip().replace("\n", " ")
            return s[:200] + ("…" if len(s) > 200 else "")
    return None


def get_connections(account_id: int | None) -> list[dict[str, Any]]:
    """All connection edges for an account (most-recently-active first)."""
    acct = (
        f"WHERE account_id = {PH}" if account_id is not None else "WHERE account_id IS NULL"
    )
    args: tuple[Any, ...] = (account_id,) if account_id is not None else ()
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(
            "SELECT id, source_service, source_agent_id, target_service, "
            "target_agent_id, status, call_count, trace_count, total_tokens, via_operations, sample, first_seen, last_seen "
            f"FROM agent_connections {acct} ORDER BY last_seen DESC NULLS LAST, id DESC"
            if USE_POSTGRES
            else "SELECT id, source_service, source_agent_id, target_service, "
            "target_agent_id, status, call_count, trace_count, total_tokens, via_operations, sample, first_seen, last_seen "
            f"FROM agent_connections {acct} ORDER BY last_seen DESC, id DESC",
            args,
        )
        return [_connection_row(r) for r in cur.fetchall()]


def add_manual_connection(
    account_id: int | None,
    source_service: str,
    source_agent_id: str,
    target_service: str,
    target_agent_id: str,
) -> dict[str, Any]:
    """Operator-drawn edge. Upserts to status='manual' (promotes an existing
    detected edge). Returns the row."""
    said = source_agent_id or "main"
    taid = target_agent_id or "main"
    where = (
        f"account_id = {PH} AND source_service = {PH} AND source_agent_id = {PH} "
        f"AND target_service = {PH} AND target_agent_id = {PH}"
        if account_id is not None
        else f"account_id IS NULL AND source_service = {PH} AND source_agent_id = {PH} "
        f"AND target_service = {PH} AND target_agent_id = {PH}"
    )
    key_args = (
        (account_id, source_service, said, target_service, taid)
        if account_id is not None
        else (source_service, said, target_service, taid)
    )
    now_sql = "NOW()" if USE_POSTGRES else "CURRENT_TIMESTAMP"
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(
            f"UPDATE agent_connections SET status = 'manual', updated_at = {now_sql} WHERE {where}",
            key_args,
        )
        if cur.rowcount == 0:
            cur.execute(
                "INSERT INTO agent_connections (account_id, source_service, "
                "source_agent_id, target_service, target_agent_id, status) "
                f"VALUES ({PH}, {PH}, {PH}, {PH}, {PH}, 'manual')",
                (account_id, source_service, said, target_service, taid),
            )
        cur.execute(
            "SELECT id, source_service, source_agent_id, target_service, "
            "target_agent_id, status, call_count, trace_count, total_tokens, via_operations, sample, first_seen, last_seen "
            f"FROM agent_connections WHERE {where}",
            key_args,
        )
        return _connection_row(cur.fetchone())


def set_connection_status(
    account_id: int | None, conn_id: int, status: str
) -> dict[str, Any] | None:
    """Confirm / dismiss / re-detect an edge. Returns the updated row or None."""
    if status not in _CONN_STATUSES:
        raise ValueError(f"invalid status: {status}")
    acct = (
        f"AND account_id = {PH}" if account_id is not None else "AND account_id IS NULL"
    )
    now_sql = "NOW()" if USE_POSTGRES else "CURRENT_TIMESTAMP"
    args = (status, conn_id, account_id) if account_id is not None else (status, conn_id)
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(
            f"UPDATE agent_connections SET status = {PH}, updated_at = {now_sql} "
            f"WHERE id = {PH} {acct}",
            args,
        )
        if cur.rowcount == 0:
            return None
        sel_args = (conn_id, account_id) if account_id is not None else (conn_id,)
        cur.execute(
            "SELECT id, source_service, source_agent_id, target_service, "
            "target_agent_id, status, call_count, trace_count, total_tokens, via_operations, sample, first_seen, last_seen "
            f"FROM agent_connections WHERE id = {PH} {acct}",
            sel_args,
        )
        row = cur.fetchone()
    return _connection_row(row) if row else None


def delete_connection(account_id: int | None, conn_id: int) -> bool:
    acct = (
        f"AND account_id = {PH}" if account_id is not None else "AND account_id IS NULL"
    )
    args = (conn_id, account_id) if account_id is not None else (conn_id,)
    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(
            f"DELETE FROM agent_connections WHERE id = {PH} {acct}", args
        )
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Captured outputs (gated by the plugin's captureOutputs flag)
# ---------------------------------------------------------------------------


# Attributes the plugin sets when captureOutputs is enabled. One span
# carries at most one of these (they're emitted on different span types:
# message_received / message_sent / tool_call).
# Both the new `trovis.*` namespace and the legacy `oversee.*` (live agents +
# historical rows) — keep both permanently.
_CAPTURE_ATTR_PATTERNS = (
    '%"trovis.message.content":%',
    '%"trovis.response.content":%',
    '%"trovis.tool.result":%',
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
            OR attributes LIKE {PH}
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
        if attr(attrs, "tool.result") is not None:
            content_type = "tool_result"
            content = attr(attrs, "tool.result") or ""
        elif attr(attrs, "response.content") is not None:
            content_type = "response"
            content = attr(attrs, "response.content") or ""
        elif attr(attrs, "message.content") is not None:
            content_type = "message"
            content = attr(attrs, "message.content") or ""
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


# Captured-content snippets in the activity feed are truncated to this many
# characters server-side — enough to convey the gist of a message/response/tool
# result without shipping a whole transcript to the dashboard.
_ACTIVITY_CONTENT_CHARS = 280


def get_fleet_activity(
    account_id: int | None,
    since_ns: int,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Chronological, fleet-wide work feed: real work events across ALL agents
    in the window, newest first. Excludes `agent_registration` (a just-connected
    agent isn't doing work). Surfaces captured message/response/tool content when
    the span carried it (dual-read `trovis.*` / `oversee.*` via attr()), else the
    row is just the operation. Pure DB — no Claude — so it always renders fast.
    account-scoped when account_id is provided.

    The one query mirrors get_agents' display-name correlated subquery so each
    row is labeled with the operator's name when set. The placeholder order
    matters: the dn subquery sits textually BEFORE the WHERE clause, so its
    account_id binds first.
    """
    limit = max(1, min(500, int(limit)))
    account_filter = f"AND s.account_id = {PH}" if account_id is not None else ""
    dn_filter = f"AND dn.account_id = {PH}" if account_id is not None else ""
    sql = f"""
        SELECT
            s.service_name,
            COALESCE(s.agent_id, 'main')                     AS agent_id,
            s.span_name,
            s.start_time_unix,
            s.end_time_unix,
            s.status_code,
            s.attributes,
            s.loop_id,
            (
                SELECT display_name
                FROM agent_display_names dn
                WHERE dn.service_name = s.service_name
                  AND COALESCE(dn.agent_id, 'main') = COALESCE(s.agent_id, 'main')
                  {dn_filter}
                LIMIT 1
            )                                                AS display_name
        FROM spans s
        WHERE s.start_time_unix >= {PH}
          AND s.span_name != 'agent_registration'
          {account_filter}
        ORDER BY s.start_time_unix DESC
        LIMIT {PH}
    """
    args: list[Any] = []
    if account_id is not None:
        args.append(account_id)   # dn_filter (SELECT subquery — binds first)
    args.append(since_ns)         # WHERE start_time_unix >= …
    if account_id is not None:
        args.append(account_id)   # account_filter (WHERE)
    args.append(limit)

    with _connect() as conn, _cursor(conn) as cur:
        cur.execute(sql, tuple(args))
        rows = cur.fetchall()

    items: list[dict[str, Any]] = []
    for r in rows:
        try:
            attrs = json.loads(r["attributes"] or "{}")
        except (TypeError, ValueError):
            attrs = {}
        if not isinstance(attrs, dict):
            attrs = {}

        # Surface the most specific captured content, if any.
        content: str | None = None
        content_type: str | None = None
        if attr(attrs, "tool.result") is not None:
            content_type, content = "tool_result", str(attr(attrs, "tool.result") or "")
        elif attr(attrs, "response.content") is not None:
            content_type, content = "response", str(attr(attrs, "response.content") or "")
        elif attr(attrs, "message.content") is not None:
            content_type, content = "message", str(attr(attrs, "message.content") or "")
        if content is not None:
            content = content.strip()[:_ACTIVITY_CONTENT_CHARS] or None
            if content is None:
                content_type = None

        tool = attr(attrs, "tool.name")
        duration_ms = (r["end_time_unix"] - r["start_time_unix"]) / 1_000_000.0
        items.append(
            {
                "time": _ns_to_iso(r["start_time_unix"]),
                "service_name": r["service_name"],
                "agent_id": r["agent_id"],
                "agent": r["display_name"] or r["service_name"],
                "operation": r["span_name"],
                "status": "error" if r["status_code"] == 2 else "ok",
                "duration_ms": duration_ms,
                "content": content,
                "content_type": content_type,
                "tool": str(tool) if tool is not None else None,
                # NULL for historical/loopless spans — lets the feed UI split
                # loop-grouped activity from "Ungrouped activity".
                "loop_id": r["loop_id"],
            }
        )
    return items
