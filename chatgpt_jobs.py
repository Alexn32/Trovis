"""Job bookkeeping for the two ChatGPT doors.

A custom GPT can reach Trovis two ways — GPT Actions over OAuth
(`/actions/*` in main.py) and Custom MCP (`/mcp` in mcp_server.py) — and
they are the same product to the person using them. They were not the same
code: Actions learned to produce named Work and the MCP twin kept writing
bare spans, so the same GPT reported differently depending on which door its
builder happened to pick. One implementation, two callers, no drift.

The model is the one the Grok Bot door settled on:

  - one job id per task, remembered server-side per (account, agent) so the
    GPT never has to echo it back — it has enough to keep track of
  - a plain-English title, which is what names the job on Work
  - every report for a job on one trace, so the Work Feed shows one record
    with its steps rather than a row per step

Nothing here raises: a reporting problem must never break the agent that is
reporting.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from typing import Any

import database

# Sentinel service_name for the "which job is open" cache. Reuses
# agent_insights — no new table, same trick as the other report doors.
_JOB_SENTINEL = "__chatgpt_job__"


def resolve_job(
    account_id: int,
    service: str,
    title: str | None = None,
    job_id: str | None = None,
) -> tuple[str, str | None]:
    """(job_id, title) for this GPT's current job.

    A GPT that sends a title starts — or moves on to — a job; one that sends
    nothing keeps reporting into the job it already had.
    """
    clean_title = " ".join(str(title or "").split())[:200] or None
    explicit = " ".join(str(job_id or "").split())[:120]
    row = database.get_insight(account_id, _JOB_SENTINEL, "main", service)
    remembered = (row or {}).get("data") if row else None
    remembered = remembered if isinstance(remembered, dict) else {}

    resolved_id = explicit or remembered.get("job_id") or ""
    # A new title with no id means a new job: the GPT moved on to something
    # else and said so.
    if clean_title and clean_title != remembered.get("title"):
        resolved_id = explicit or f"gpt-{uuid.uuid4().hex[:12]}"
    if not resolved_id:
        resolved_id = f"gpt-{uuid.uuid4().hex[:12]}"
    resolved_title = clean_title or remembered.get("title") or None
    database.save_insight(
        account_id, _JOB_SENTINEL, "main", service,
        {"job_id": resolved_id, "title": resolved_title or ""},
    )
    return resolved_id, resolved_title


def forget_job(account_id: int, service: str) -> None:
    """A completed job stops being the one later steps attach to."""
    database.save_insight(
        account_id, _JOB_SENTINEL, "main", service, {"job_id": "", "title": ""}
    )


def write_span(
    account_id: int,
    service: str,
    job_id: str,
    span_name: str,
    attributes: dict[str, Any],
    start_ns: int | None = None,
    end_ns: int | None = None,
    status_code: int = 0,
    status_message: str = "",
) -> None:
    """One report -> one span on the job's trace, through the loop-aware
    ingest path so it lands as Work rather than loose activity.

    The trace id is derived from the job id, so every step of a job groups
    into one record instead of one row each. Derived, not stored: same job,
    same trace, no bookkeeping — and account-scoped so two orgs reusing a job
    id never share a record.
    """
    now = time.time_ns()
    start = start_ns if start_ns is not None else now
    end = end_ns if end_ns is not None else now
    trace_id = hashlib.sha256(f"{account_id}:{job_id}".encode()).hexdigest()[:32]
    attrs: dict[str, Any] = {
        "trovis.agent.id": "main",
        "trovis.loop.external_id": job_id,
        "trovis.run.id": job_id,
    }
    attrs.update({k: v for k, v in attributes.items() if v not in (None, "")})
    database.ingest_spans_with_loops([{
        "trace_id": trace_id,
        "span_id": uuid.uuid4().hex[:16],
        "parent_span_id": None,
        "service_name": service,
        "span_name": span_name,
        "kind": 0,
        "start_time_unix": start,
        "end_time_unix": end,
        "status_code": status_code,
        "status_message": status_message,
        "attributes": attrs,
        "resource_attributes": {
            "service.name": service,
            "trovis.platform": "chatgpt",
        },
    }], account_id=account_id)
