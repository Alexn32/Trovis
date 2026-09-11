"""Analysis runs beside the request, never inside it.

A Home read must return whatever is already known, promptly, and say whether
more is coming. It must never wait on a model. So the investigation lives in a
durable queue: the read path enqueues (or joins) a job and returns; a worker
claims jobs one at a time and publishes findings when it is done.

Why a table and not a thread pool
---------------------------------
The work has to survive a restart, must not run twice concurrently for the
same audience, and has to be inspectable when it fails. A queue in memory
gives none of that on a platform that restarts a container whenever it likes.

What the job key buys
---------------------
`account + effective permissions + work scope + viewer + period + evidence
version + prompt version`. Two things fall out of that:

  * Polling is free. Home asking again with nothing changed lands on the same
    key, finds the job already queued or its findings already published, and
    buys no model call.
  * Ingest is coalesced. The evidence version buckets the newest timestamp
    (see `database.evidence_version`), so a burst of spans maps to one version
    and one analysis rather than one per event.

And the failure posture: no API key, or an outage, leaves the snapshot fully
usable and analysis explicitly unavailable. There is no canned fallback text —
a deterministic sentence presented as an AI finding would be exactly the lie
this whole subsystem is built to avoid.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

import database
import findings as findings_mod
import home_snapshot
import investigator

logger = logging.getLogger("trovis")


def _i(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default


# One investigation at a time per process by default. The work is model-bound,
# a single Uvicorn replica is the deployment, and two concurrent analyses would
# compete for the same connection pool the API needs to stay responsive.
def max_concurrency() -> int:
    return max(1, _i("TROVIS_ANALYSIS_CONCURRENCY", 1))


def poll_interval_s() -> int:
    return max(5, _i("TROVIS_ANALYSIS_POLL_S", 30))


# How old a published analysis may be before a read re-enqueues. Under this,
# a read serves what exists and enqueues nothing.
def freshness_s() -> int:
    return max(60, _i("TROVIS_ANALYSIS_FRESHNESS_S", 3600))


_worker_lock = threading.Lock()


def analysis_request(
    *,
    account_id: int,
    viewer_user_id: int | None,
    seat: dict[str, Any] | None,
    selection: dict[str, Any],
    period: dict[str, Any],
) -> dict[str, Any]:
    """Everything one analysis needs, plus the keys that identify it.

    Built from the SERVER's resolved seat and selection — the same objects the
    snapshot endpoint uses — so the analysis can never cover work the reader
    could not already see.
    """
    surfaces = list((seat or {}).get("surfaces") or [])
    scope = findings_mod.scope_key(
        account_id=account_id,
        surfaces=surfaces,
        breadth=(seat or {}).get("breadth"),
        visible_user_ids=(seat or {}).get("visible_user_ids"),
        whose=selection.get("choice") or "everyone",
        person_id=selection.get("person_id"),
        viewer_user_id=viewer_user_id,
        days=period["days"],
        timezone_name=period["timezone"],
    )
    evidence = database.evidence_version(account_id)
    return {
        "account_id": account_id,
        "viewer_user_id": viewer_user_id,
        "scope_key": scope,
        "job_key": findings_mod.job_key(
            scope, evidence["version"], investigator.PROMPT_VERSION
        ),
        "evidence": evidence,
        "whose": selection.get("choice") or "everyone",
        "person_id": selection.get("person_id"),
        "days": period["days"],
        "timezone": period["timezone"],
        "prompt_version": investigator.PROMPT_VERSION,
    }


def analysis_available() -> bool:
    """Can analysis run at all? A missing key is a product state, not a crash."""
    return bool(database.env("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_API_KEY"))


def ensure_analysis(request: dict[str, Any], *, findings_count: int,
                    newest_analyzed_at: str | None) -> dict[str, Any]:
    """Decide whether to queue, and report the status either way.

    Returns the `analysis` block a read serves. It never waits: the caller
    hands back whatever findings already exist alongside this.
    """
    if not analysis_available():
        return {
            "state": "unavailable",
            "reason": "no_model_configured",
            "enqueued": False,
            "evidence_version": request["evidence"]["version"],
            "prompt_version": request["prompt_version"],
        }

    existing = database.analysis_job_status(request["account_id"], request["job_key"])
    if existing and existing["status"] in ("queued", "running"):
        return {
            "state": existing["status"],
            "reason": None,
            "enqueued": False,
            "job_id": existing["id"],
            "evidence_version": request["evidence"]["version"],
            "prompt_version": request["prompt_version"],
        }
    if existing and existing["status"] == "done":
        return {
            "state": "current",
            "reason": None,
            "enqueued": False,
            "completed_at": existing["finished_at"],
            "evidence_version": request["evidence"]["version"],
            "prompt_version": request["prompt_version"],
        }
    if existing and existing["status"] == "failed":
        # Retries are exhausted for THIS evidence version; a newer version is
        # a different key and will queue on its own.
        return {
            "state": "failed",
            "reason": existing.get("error") or "analysis failed",
            "enqueued": False,
            "evidence_version": request["evidence"]["version"],
            "prompt_version": request["prompt_version"],
        }

    queued = database.enqueue_analysis_job(
        request["account_id"], request["job_key"],
        {k: request[k] for k in
         ("scope_key", "viewer_user_id", "whose", "person_id", "days",
          "timezone", "evidence")},
    )
    return {
        "state": "queued",
        "reason": None,
        "enqueued": bool(queued.get("created")),
        "job_id": queued["id"],
        "stale_findings": findings_count > 0,
        "previous_analysis_at": newest_analyzed_at,
        "evidence_version": request["evidence"]["version"],
        "prompt_version": request["prompt_version"],
    }


def run_one() -> dict[str, Any] | None:
    """Claim and run a single queued analysis. Returns a summary, or None when
    the queue is empty or the concurrency ceiling is already reached."""
    if database.count_running_analysis_jobs() >= max_concurrency():
        return None
    if not _worker_lock.acquire(blocking=False):
        return None
    try:
        job = database.claim_analysis_job()
        if job is None:
            return None
        started = time.monotonic()
        try:
            report = _execute(job)
        except investigator.NoModelKey as exc:
            # Not a retryable failure — nothing about waiting fixes it.
            database.finish_analysis_job(job["id"], "failed", error=str(exc))
            return {"job_id": job["id"], "status": "failed", "error": "no_model_key"}
        except Exception as exc:  # noqa: BLE001 — one bad account must not stop the queue
            logger.warning("[analysis] job %s failed: %s", job["id"], exc)
            if job["attempts"] >= database.ANALYSIS_JOB_MAX_ATTEMPTS:
                database.finish_analysis_job(job["id"], "failed", error=str(exc)[:400])
                return {"job_id": job["id"], "status": "failed", "error": str(exc)[:200]}
            database.requeue_analysis_job(job["id"], str(exc)[:400])
            return {"job_id": job["id"], "status": "requeued", "error": str(exc)[:200]}
        report["seconds"] = round(time.monotonic() - started, 2)
        database.finish_analysis_job(job["id"], "done", result=report)
        return {"job_id": job["id"], "status": "done", **report}
    finally:
        _worker_lock.release()


def _execute(job: dict[str, Any]) -> dict[str, Any]:
    """Rebuild the reader's authorized view, then investigate it.

    The seat and the scope are RE-RESOLVED here rather than trusted from the
    queued row. A job may sit for minutes; permissions can change in that time,
    and the analysis must run against what is true when it runs.
    """
    request = job["request"]
    account_id = job["account_id"]
    viewer_user_id = request.get("viewer_user_id")
    seat = (
        database.resolve_seat(account_id, viewer_user_id)
        if viewer_user_id is not None else None
    )
    period = home_snapshot.resolve_period(request.get("days"), request.get("timezone"))
    selection = _replay_selection(account_id, viewer_user_id, seat, request)

    # If the re-resolved scope no longer matches the queued one, the reader's
    # permissions changed. Publishing under the OLD key would hand findings to
    # a slice that no longer describes anybody.
    live_scope = findings_mod.scope_key(
        account_id=account_id,
        surfaces=list((seat or {}).get("surfaces") or []),
        breadth=(seat or {}).get("breadth"),
        visible_user_ids=(seat or {}).get("visible_user_ids"),
        whose=selection["choice"],
        person_id=selection["person_id"],
        viewer_user_id=viewer_user_id,
        days=period["days"],
        timezone_name=period["timezone"],
    )
    if live_scope != request.get("scope_key"):
        return {
            "published": 0,
            "abstained": ["permissions changed since this analysis was queued"],
            "scope_changed": True,
        }

    snapshot = home_snapshot.build_snapshot(
        account_id=account_id,
        viewer_user_id=viewer_user_id,
        selection={**selection, "seat": seat},
        period=period,
    )
    return investigator.investigate(
        account_id=account_id,
        viewer_user_id=viewer_user_id,
        seat=seat,
        only_user_ids=selection["user_ids"],
        snapshot=snapshot,
        scope_key=live_scope,
        evidence=request.get("evidence") or database.evidence_version(account_id),
    )


def _replay_selection(
    account_id: int,
    viewer_user_id: int | None,
    seat: dict[str, Any] | None,
    request: dict[str, Any],
) -> dict[str, Any]:
    """Rebuild the whose-work selection without an HTTP request.

    Mirrors `main._resolve_whose_selection`'s intersection exactly: the stored
    choice can narrow and can never widen, and a person who has since left the
    reader's reporting line falls back rather than staying visible.
    """
    choice = request.get("whose") or "everyone"
    person_id = request.get("person_id")
    if seat is None or viewer_user_id is None:
        return {"choice": "everyone", "person_id": None, "user_ids": None,
                "clamped": False, "unreadable": False, "requested": "everyone"}
    allowed = seat["visible_user_ids"]
    subtree = set(seat.get("subtree_user_ids") or [])
    if choice == "me":
        wanted = [viewer_user_id]
    elif choice == "team":
        wanted = sorted({viewer_user_id, *subtree})
    elif choice == "person" and person_id is not None:
        if person_id != viewer_user_id and person_id not in subtree:
            # They are no longer in this reader's line. Fall back rather than
            # quietly keeping the old visibility.
            choice, person_id, wanted = "everyone", None, None
        else:
            wanted = [person_id]
    else:
        choice, person_id, wanted = "everyone", None, None

    if wanted is None:
        user_ids, clamped = allowed, False
    elif allowed is None:
        user_ids, clamped = wanted, False
    else:
        user_ids = sorted(set(wanted) & set(allowed))
        clamped = len(user_ids) < len(wanted)
    return {
        "choice": choice, "requested": choice, "person_id": person_id,
        "user_ids": user_ids, "clamped": clamped, "unreadable": False,
    }


def drain(max_jobs: int = 5) -> list[dict[str, Any]]:
    """Run up to `max_jobs` queued analyses. The worker loop's body, and the
    hook tests use to run the queue deterministically."""
    out = []
    for _ in range(max(1, int(max_jobs))):
        result = run_one()
        if result is None:
            break
        out.append(result)
    return out
