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
`account + effective permissions + work scope + viewer + period slot + evidence
schedule bucket + prompt version`. Two things fall out of that:

  * Polling is free. Home asking again with nothing changed lands on the same
    key, finds the job already queued or its findings already published, and
    buys no model call.
  * Ingest is coalesced. The key uses the COARSE schedule bucket from
    `database.evidence_version`, not its exact version, so a burst of spans in
    one 15-minute window maps to one analysis rather than one per event. The
    exact version still travels on every finding, so staleness stays
    detectable — coalescing scheduling is not the same as ignoring change.

Freshness, and why a quiet account still gets looked at
------------------------------------------------------
Evidence is not the only thing that expires. The job key also carries a
`period_slot`, so a rolling window ("the last 7 days") becomes a different
question as it moves; and a completed analysis past `freshness_s()` is
reconsidered whatever the telemetry did. Underneath both sits `debounce_s()`,
a floor that stops an ingest burst plus a polled Home from buying an
investigation per event. The tradeoff is explicit: at most one investigation
per audience per debounce window, so a finding's worst-case staleness is that
window plus the analysis's own duration.

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
from datetime import datetime, timezone
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


# How old a published analysis may be before a read re-enqueues, even with no
# new telemetry at all. A quiet account still needs reconsidering: the selected
# period rolls forward, waits get longer, and "nothing has changed" is itself a
# thing that changes with time.
def freshness_s() -> int:
    return max(60, _i("TROVIS_ANALYSIS_FRESHNESS_S", 3600))


# The floor under re-analysis. Debounce is what stops an ingest burst plus a
# polled Home from buying an LLM call per event: once an analysis completes,
# the same audience will not start another inside this window however much
# evidence arrives. Evidence is never IGNORED — the next analysis after the
# window covers everything that landed meanwhile, and the read says so — it is
# only delayed. The tradeoff is explicit: at most one investigation per
# audience per debounce window, so the worst-case staleness of a finding is
# the debounce interval plus the analysis's own duration.
def debounce_s() -> int:
    return max(0, _i("TROVIS_ANALYSIS_DEBOUNCE_S", 300))


_worker_lock = threading.Lock()


def _now() -> datetime:
    """The one clock this module reads, so tests can pin it."""
    return datetime.now(timezone.utc)


def _period_slot(period: dict[str, Any]) -> str:
    """Which freshness window the selected period currently sits in.

    In the job key so a ROLLING period re-analyses on its own. "The last 7
    days" is a different question at 09:00 than at 17:00 — the window moved,
    runs left it, waits grew — and an analysis keyed only on evidence would
    call yesterday's answer current forever on an account that has gone quiet.
    """
    end = period.get("end_utc")
    if isinstance(end, datetime):
        stamp = end
    else:
        stamp = _now()
    return str(int(stamp.timestamp()) // max(60, freshness_s()))


def _age_seconds(iso_or_sql: str | None) -> float | None:
    """Age of a stored timestamp in seconds, or None if unreadable."""
    if not iso_or_sql:
        return None
    raw = str(iso_or_sql).strip().replace("T", " ")[:19]
    try:
        stamp = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None
    return (_now() - stamp).total_seconds()


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
    # The job key uses the COARSE bucket plus the period window, never the
    # exact version. That is what makes an ingest burst and a polled Home
    # collapse onto one investigation; the exact version still travels on the
    # request and onto every finding, so staleness stays detectable.
    period_slot = _period_slot(period)
    return {
        "account_id": account_id,
        "viewer_user_id": viewer_user_id,
        "scope_key": scope,
        "job_key": findings_mod.job_key(
            scope,
            f"{evidence['schedule_bucket']}:{period_slot}",
            investigator.PROMPT_VERSION,
        ),
        "period_slot": period_slot,
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

    Freshness is not only about new telemetry. An earlier version reported any
    completed matching job as `current` forever — a job finished in 2000 still
    read as current with no refresh queued — because the only thing that could
    change the job key was new evidence. Three things expire an analysis now:

      * AGE. Past `freshness_s()` it is reconsidered even on a silent account.
      * THE PERIOD. `period_slot` is in the job key, so a rolling window
        ("the last 7 days") becomes a different question as it moves, which is
        also what makes a growing wait get looked at again.
      * EVIDENCE and PROMPTS, as before.

    And a debounce floor underneath all of it, so an ingest burst plus a polled
    Home cannot buy an investigation per event.
    """
    if not analysis_available():
        return {
            "state": "unavailable",
            "reason": "no_model_configured",
            "enqueued": False,
            "evidence_version": request["evidence"]["version"],
            "prompt_version": request["prompt_version"],
        }

    base = {
        "evidence_version": request["evidence"]["version"],
        "prompt_version": request["prompt_version"],
        "findings_from_previous_analysis": False,
    }
    existing = database.analysis_job_status(request["account_id"], request["job_key"])
    if existing and existing["status"] in ("queued", "running"):
        # A refresh is in flight. Whatever findings the reader is being shown
        # alongside this came from an EARLIER analysis, and saying so is the
        # difference between "here is the latest" and "here is the last
        # answer while we look again".
        return {
            **base,
            "state": existing["status"],
            "reason": None,
            "enqueued": False,
            "job_id": existing["id"],
            "findings_from_previous_analysis": findings_count > 0,
            "previous_analysis_at": newest_analyzed_at,
        }
    if existing and existing["status"] == "done":
        age = _age_seconds(existing.get("finished_at"))
        if age is not None and age < freshness_s():
            return {
                **base,
                "state": "current",
                "reason": None,
                "enqueued": False,
                "completed_at": existing["finished_at"],
            }
        # Expired. Fall through and queue a refresh — but a job row already
        # exists for this key, so the enqueue below is what re-opens it.
    if existing and existing["status"] == "failed":
        # Retries are exhausted for this key. A newer evidence bucket, a moved
        # period or a prompt change is a different key and queues on its own.
        # This is NOT reported as "analysed and found nothing": a failed
        # refresh masquerading as a successful empty investigation is exactly
        # the lie this layer exists to avoid.
        return {
            **base,
            "state": "failed",
            "reason": existing.get("error") or "analysis failed",
            "enqueued": False,
            # The previously published findings stay valid and stay visible.
            # A failed replacement attempt is not evidence that what it was
            # replacing was wrong.
            "findings_from_previous_analysis": findings_count > 0,
            "previous_analysis_at": newest_analyzed_at,
        }

    # Debounce: however much evidence has arrived, one AUDIENCE does not start
    # a second investigation inside this window. Keyed on the scope rather than
    # the job, because the job key changes every time evidence moves buckets
    # and holding a floor across exactly those changes is the point.
    #
    # What arrived meanwhile is not discarded. The next analysis after the
    # window reads the record as it is then, and the reader is told so.
    last = database.last_completed_analysis(
        request["account_id"], request["scope_key"]
    )
    since_last = _age_seconds((last or {}).get("finished_at"))
    if since_last is not None and since_last < debounce_s():
        return {
            **base,
            "state": "debounced",
            "reason": (
                "analysed recently; evidence that arrived since will be covered "
                "by the next run"
            ),
            "enqueued": False,
            "previous_analysis_at": (last or {}).get("finished_at")
                                    or newest_analyzed_at,
            "findings_from_previous_analysis": findings_count > 0,
            "debounce_seconds": debounce_s(),
        }

    queued = database.enqueue_analysis_job(
        request["account_id"], request["job_key"],
        {k: request[k] for k in
         ("scope_key", "viewer_user_id", "whose", "person_id", "days",
          "timezone", "evidence")},
        scope_key=request["scope_key"],
    )
    return {
        **base,
        "state": "queued",
        "reason": None,
        "enqueued": bool(queued.get("created")),
        "job_id": queued["id"],
        "stale_findings": findings_count > 0,
        "findings_from_previous_analysis": findings_count > 0,
        "previous_analysis_at": newest_analyzed_at,
    }


def run_one() -> dict[str, Any] | None:
    """Claim and run a single queued analysis.

    Returns a summary, or None when the queue is empty or the concurrency
    ceiling is genuinely occupied.

    The ordering here matters and used to be wrong. `count_running_analysis_jobs`
    was checked first and counted crashed rows, so with the default ceiling of
    one a single abandoned job held the slot forever: `run_one` returned None
    without ever reaching the recovery path that would have reclaimed it. The
    count now excludes rows whose heartbeat has gone stale — those are not
    occupying a worker, they are waiting to be taken over — and
    `claim_analysis_job` reclaims them by the same conditional UPDATE that
    makes ordinary claiming exclusive.
    """
    # LIVE runners only. A stale row is recoverable capacity, not used capacity.
    if database.count_running_analysis_jobs() >= max_concurrency():
        return None
    if not _worker_lock.acquire(blocking=False):
        return None
    try:
        job = database.claim_analysis_job()
        if job is None:
            return None
        token = job.get("claim_token")
        started = time.monotonic()
        try:
            report = _execute(job)
        except investigator.NoModelKey as exc:
            # Not retryable — waiting does not configure a key.
            database.finish_analysis_job(
                job["id"], "failed", error=str(exc), claim_token=token)
            return {"job_id": job["id"], "status": "failed", "error": "no_model_key"}
        except Exception as exc:  # noqa: BLE001 — one bad account must not stop the queue
            logger.warning("[analysis] job %s failed: %s", job["id"], exc)
            if job["attempts"] >= database.ANALYSIS_JOB_MAX_ATTEMPTS:
                database.finish_analysis_job(
                    job["id"], "failed", error=str(exc)[:400], claim_token=token)
                return {"job_id": job["id"], "status": "failed", "error": str(exc)[:200]}
            database.requeue_analysis_job(job["id"], str(exc)[:400])
            return {"job_id": job["id"], "status": "requeued", "error": str(exc)[:200]}
        report["seconds"] = round(time.monotonic() - started, 2)
        # Fenced: if this job was reclaimed as stale while we were working, a
        # newer worker owns it and our result is discarded rather than
        # overwriting theirs.
        if not database.finish_analysis_job(
            job["id"], "done", result=report, claim_token=token
        ):
            logger.info(
                "[analysis] job %s was superseded; discarding this worker's result",
                job["id"],
            )
            return {"job_id": job["id"], "status": "superseded"}
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

    # One more fence before anything is published: a worker whose claim was
    # superseded must not write findings over the worker that replaced it.
    token = job.get("claim_token")
    if token is not None and not database.analysis_claim_is_current(job["id"], token):
        return {"published": 0, "abstained": ["claim superseded before publication"],
                "superseded": True}

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
