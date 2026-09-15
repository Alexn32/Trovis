"""What Trovis's own Home investigations cost to run.

Not the customers' agents. `spans` records what THEIR agents spend, which is
what the Cost page and Home's cost card report; this records what WE spend
running Home's analysis for them. The two never meet in one query, and no
customer-facing surface reads this ledger — adding our cost of goods to a
customer's bill would be the most expensive kind of wrong number.

**One row per provider request.** Not per investigation and not per stage: a
retry is a second potentially billable request and gets its own row, because
the provider charges for the attempt whatever we later decide about the answer.

**The row is opened before the request goes out.** Local persistence and the
provider's billing are not atomic and nothing here pretends they are. A worker
killed between the open and the close leaves an explicit `in_flight` row, which
the spending report counts as unresolved rather than quietly omitting. The
alternative — writing only on success — loses exactly the requests most likely
to have cost something and produced nothing.

**Unknown is never zero.** A provider that reports no usage, and a model with
no stored price, both leave NULL. A report that coalesces either to 0 is
stating a number nobody measured.

**Nothing sensitive is stored.** No API key, no authorization header, no prompt
or response text. Token counts, timings and outcomes only.

Attribution comes from a context the worker establishes around one job
execution (`attributed()`), read through a `ContextVar` so the investigator's
call sites need a stage label and nothing else. Outside that context — a
script, a test, a future caller — requests are recorded with a null job and
still counted, rather than being silently dropped.
"""
from __future__ import annotations

import contextlib
import contextvars
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

import database

logger = logging.getLogger("trovis.home_llm")

# The investigation stages that can reach a provider. Kept as constants so the
# spending report and the tests name the same things the code writes.
STAGE_PROPOSING = "proposing"
STAGE_INVESTIGATING = "investigating"
STAGE_ASSESSING = "assessing"
STAGE_RANKING = "ranking"
STAGE_COMPOSING = "composing"
STAGE_REVISING = "revising"

STAGES = (
    STAGE_PROPOSING, STAGE_INVESTIGATING, STAGE_ASSESSING,
    STAGE_RANKING, STAGE_COMPOSING, STAGE_REVISING,
)

OUTCOME_SUCCEEDED = "succeeded"
OUTCOME_FAILED = "failed"
OUTCOME_CANCELLED = "cancelled"
OUTCOME_UNKNOWN = "unknown"
OUTCOME_IN_FLIGHT = "in_flight"

_CTX: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "home_llm_ctx", default=None,
)


@contextlib.contextmanager
def attributed(
    *,
    account_id: int | None,
    analysis_job_id: int | None = None,
    job_attempt: int | None = None,
    analysis_id: str | None = None,
    scope_key: str | None = None,
):
    """Attribute every provider request inside this block to one job execution.

    A ContextVar rather than a threaded-through argument: the investigator
    reaches the provider from five call sites and a tool loop, and widening all
    of their signatures to carry accounting would be a behaviour change for the
    sake of bookkeeping. `run_one` owns the block; the call sites name a stage.

    `request_seq` is per-context, so the ordering of requests inside one
    execution survives even when two executions interleave.
    """
    token = _CTX.set({
        "account_id": account_id,
        "analysis_job_id": analysis_job_id,
        "job_attempt": job_attempt,
        "analysis_id": analysis_id,
        "scope_key": scope_key,
        "seq": 0,
    })
    try:
        yield
    finally:
        _CTX.reset(token)


def current_context() -> dict[str, Any] | None:
    ctx = _CTX.get()
    return dict(ctx) if ctx else None


def set_analysis_id(analysis_id: str | None) -> None:
    """Stamp the analysis id once the investigation has minted one.

    The id does not exist when the context opens — it is created inside the
    run — so requests made before it is known carry a null and the rest carry
    it. Both still carry the job and attempt, which is what attribution turns
    on.
    """
    ctx = _CTX.get()
    if ctx is not None and analysis_id:
        ctx["analysis_id"] = analysis_id


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _int_or_none(value: Any) -> int | None:
    """A token count, or None when the provider did not report one.

    `or 0` would be the bug this whole module exists to avoid: a missing count
    is unknown, and turning it into zero prices the request at less than it
    cost.
    """
    if value is None:
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def read_usage(resp: Any) -> dict[str, Any]:
    """Provider-reported usage, keeping "absent" and "zero" apart.

    Anthropic reports `input_tokens` EXCLUDING cached tokens and reports the
    cache counts separately, so the three add without overlapping. Reading them
    into one bucket, or defaulting the cache fields to zero when the block is
    missing entirely, would either double-count or under-count.
    """
    usage = getattr(resp, "usage", None)
    if usage is None:
        return {"input_tokens": None, "output_tokens": None,
                "cache_creation_input_tokens": None,
                "cache_read_input_tokens": None, "usage_reported": 0}
    fields = {
        "input_tokens": _int_or_none(getattr(usage, "input_tokens", None)),
        "output_tokens": _int_or_none(getattr(usage, "output_tokens", None)),
        "cache_creation_input_tokens": _int_or_none(
            getattr(usage, "cache_creation_input_tokens", None)),
        "cache_read_input_tokens": _int_or_none(
            getattr(usage, "cache_read_input_tokens", None)),
    }
    fields["usage_reported"] = int(any(v is not None for v in fields.values()))
    return fields


def _error_kind(exc: BaseException) -> str:
    """The exception's type name. Never its message — a provider error can
    quote the request, and this ledger holds no prompt text."""
    return type(exc).__name__[:80]


def _outcome_for(exc: BaseException) -> str:
    if isinstance(exc, (KeyboardInterrupt, SystemExit)):
        return OUTCOME_CANCELLED
    name = type(exc).__name__.lower()
    if "cancel" in name or "abort" in name:
        return OUTCOME_CANCELLED
    return OUTCOME_FAILED


def call(stage: str, fn: Callable[[], Any], *, model: str) -> Any:
    """Run one provider request and record what it cost.

    The request happens either way. Every failure inside the accounting is
    swallowed and logged: a ledger that can take Home's analysis down is worse
    than one with a gap in it, and the gap is visible in the report's coverage.
    """
    ctx = _CTX.get()
    request_key = uuid.uuid4().hex
    seq = None
    if ctx is not None:
        ctx["seq"] = int(ctx.get("seq") or 0) + 1
        seq = ctx["seq"]
    started = _now()
    opened = database.open_home_llm_request({
        "request_key": request_key,
        "account_id": (ctx or {}).get("account_id"),
        "analysis_job_id": (ctx or {}).get("analysis_job_id"),
        "job_attempt": (ctx or {}).get("job_attempt"),
        "analysis_id": (ctx or {}).get("analysis_id"),
        "scope_key": (ctx or {}).get("scope_key"),
        "stage": stage,
        "request_seq": seq,
        "model_requested": model,
        "started_at": _stamp(started),
    })

    monotonic = time.monotonic()
    try:
        resp = fn()
    except BaseException as exc:  # noqa: BLE001 — recorded, then re-raised
        if opened:
            _settle(request_key, model, None, started, monotonic,
                    outcome=_outcome_for(exc), error_kind=_error_kind(exc))
        raise
    if opened:
        _settle(request_key, model, resp, started, monotonic,
                outcome=OUTCOME_SUCCEEDED, error_kind=None)
    return resp


def _settle(request_key, model_requested, resp, started, monotonic, *,
            outcome, error_kind) -> None:
    try:
        finished = _now()
        usage = (read_usage(resp) if resp is not None else
                 {"input_tokens": None, "output_tokens": None,
                  "cache_creation_input_tokens": None,
                  "cache_read_input_tokens": None, "usage_reported": 0})
        # The model the provider ACTUALLY served, which can differ from the one
        # asked for. Pricing follows what was served; a fallback priced at the
        # requested model's rate would be the wrong bill.
        served = getattr(resp, "model", None) or (
            model_requested if resp is not None else None)
        price = database.resolve_home_llm_price(served or model_requested)
        rates = price["rates"]
        cost = database.home_llm_cost(
            rates,
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
            cache_creation=usage["cache_creation_input_tokens"],
            cache_read=usage["cache_read_input_tokens"],
        )
        database.close_home_llm_request(request_key, {
            "model_served": served,
            "outcome": outcome,
            "error_kind": error_kind,
            "finished_at": _stamp(finished),
            "latency_ms": int((time.monotonic() - monotonic) * 1000),
            **{k: usage[k] for k in (
                "input_tokens", "output_tokens",
                "cache_creation_input_tokens", "cache_read_input_tokens",
                "usage_reported")},
            "estimated_cost_usd": cost,
            "pricing_source": price["source"],
            "pricing_model_key": price["matched_key"],
            # The rate AS IT WAS when the request ran. A later price update
            # must not silently rewrite what we thought this cost.
            "pricing_captured_at": price["captured_at"],
            "price_input_per_1k": rates[0] if rates else None,
            "price_output_per_1k": rates[1] if rates else None,
        })
    except Exception:  # noqa: BLE001
        logger.warning("[home-llm] could not settle usage row", exc_info=True)
