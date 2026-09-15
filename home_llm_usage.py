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
import email.utils
import logging
import time
import uuid
from datetime import datetime, timezone
from random import random
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


# The fields Anthropic bills a Messages request on. Both are present on every
# well-formed usage block; the cache counts appear only when prompt caching is
# in play. So these two are REQUIRED to price a request and the cache fields
# are optional — which is the provider's actual contract, not a blanket rule in
# either direction.
BILLABLE_REQUIRED = ("input_tokens", "output_tokens")


def read_usage(resp: Any) -> dict[str, Any]:
    """Provider-reported usage, keeping three states apart.

    * NOTHING reported — `usage_reported: 0`. We do not know what it cost.
    * SOMETHING reported but not everything billable — `usage_reported: 1`,
      `usage_complete: 0`. We know part of it. Presenting that part as the
      request's cost is a number that is always low and always looks finished,
      which is the bug this split exists to prevent.
    * EVERYTHING billable reported — `usage_complete: 1`. Priceable.

    Anthropic reports `input_tokens` EXCLUDING cached tokens and reports the
    cache counts separately, so the three add without overlapping. An absent
    cache field alongside present required fields means no cached tokens — the
    block simply omits them when caching was not used — so it is zero, not
    unknown. An absent REQUIRED field is unknown and is never zeroed.
    """
    usage = getattr(resp, "usage", None)
    if usage is None:
        return {"input_tokens": None, "output_tokens": None,
                "cache_creation_input_tokens": None,
                "cache_read_input_tokens": None,
                "usage_reported": 0, "usage_complete": 0,
                "missing_billable": list(BILLABLE_REQUIRED)}
    fields = {
        "input_tokens": _int_or_none(getattr(usage, "input_tokens", None)),
        "output_tokens": _int_or_none(getattr(usage, "output_tokens", None)),
        "cache_creation_input_tokens": _int_or_none(
            getattr(usage, "cache_creation_input_tokens", None)),
        "cache_read_input_tokens": _int_or_none(
            getattr(usage, "cache_read_input_tokens", None)),
    }
    missing = [f for f in BILLABLE_REQUIRED if fields[f] is None]
    fields["usage_reported"] = int(any(v is not None for v in fields.values()))
    fields["usage_complete"] = int(not missing and bool(fields["usage_reported"]))
    fields["missing_billable"] = missing
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


# --------------------------------------------------------------------------
# Retry, moved to the recorded boundary
# --------------------------------------------------------------------------
#
# The Anthropic SDK retries inside `messages.create()` — 2 retries by default,
# with backoff. Wrapping that call recorded ONE row for what could be three
# HTTP requests, so a 500 / 500 / success looked like a single clean call and
# two potentially billable attempts vanished from the ledger.
#
# Disabling retries would have fixed the accounting by making Home less
# reliable, which is a bad trade. Instead the client is built with
# `max_retries=0` and the SAME policy is re-run here, one ledger row per
# attempt. The timeout is still the SDK's per-request timeout, so a retry gets
# a fresh one exactly as before.
#
# "The same policy" includes the parts that come from the PROVIDER, not from a
# status code. `anthropic._base_client` consults the response headers before
# anything else, and so does this:
#
#   `x-should-retry: true|false`  overrides the status decision in BOTH
#                                 directions. The provider knows whether a 400
#                                 is worth repeating and whether a 500 is not;
#                                 ignoring it means retrying requests it told
#                                 us not to and giving up on ones it told us to
#                                 repeat.
#   `retry-after-ms`              milliseconds, preferred — more precise.
#   `Retry-After`                 seconds, or an HTTP-date.
#
# A supplied delay is honoured only within the SDK's own bound (0 < d <= 60s),
# so a malformed or absurd header falls back to backoff instead of parking a
# worker. With no usable delay the SDK's exponential backoff WITH JITTER runs:
# jitter exists so that a fleet retrying a rate limit does not re-converge on
# the same instant, which is exactly the case `Retry-After` shows up in.
#
# Two counters, and they mean different things:
#   `request_seq`  — the LOGICAL call within a job execution (turn 3 of the
#                    tool loop). Shared by every attempt of that call.
#   `http_attempt` — which attempt this row is, 1-based. One row per attempt,
#                    so counting rows counts HTTP requests and grouping by
#                    `(request_seq)` counts logical calls. Neither double
#                    counts the other.

MAX_RETRIES = 2            # matches anthropic._constants.DEFAULT_MAX_RETRIES
_RETRY_BASE_DELAY_S = 0.5  # anthropic._constants.INITIAL_RETRY_DELAY
_RETRY_MAX_DELAY_S = 8.0   # anthropic._constants.MAX_RETRY_DELAY
# The statuses the SDK itself retries. 408 request-timeout, 409 conflict,
# 429 rate-limit and any 5xx; everything else is the provider's final answer.
_RETRY_STATUSES = frozenset({408, 409, 429})
# The SDK honours a provider-supplied delay only inside this bound. A header of
# 0, a negative one, or one asking for an hour falls through to backoff.
RETRY_AFTER_MAX_S = 60.0


def _status_of(exc: BaseException) -> int | None:
    for attr in ("status_code", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    return code if isinstance(code, int) else None


def response_headers(exc: BaseException) -> Any | None:
    """The provider's response headers, when the failure carried a response.

    A connection or timeout failure never got one, and that is not an error —
    it just means the header rules below have nothing to say.
    """
    headers = getattr(getattr(exc, "response", None), "headers", None)
    return headers if headers is not None else None


def _header(headers: Any, name: str) -> Any:
    if headers is None:
        return None
    try:
        return headers.get(name, None)
    except (AttributeError, TypeError):
        return None


def should_retry_header(headers: Any) -> bool | None:
    """The provider's explicit verdict, or None when it did not give one.

    Mirrors `_base_client._should_retry`: the header wins over the status in
    both directions, so a 400 marked retryable is retried and a 500 marked
    non-retryable is not.
    """
    value = _header(headers, "x-should-retry")
    if value == "true":
        return True
    if value == "false":
        return False
    return None


def parse_retry_after(headers: Any) -> float | None:
    """Seconds to wait, per the provider. Mirrors the SDK's parser exactly.

    Order matters and is the SDK's: `retry-after-ms` first because it is more
    precise, then `Retry-After` as (possibly fractional) seconds, then
    `Retry-After` as an HTTP-date, which is converted to a delay from now.
    Anything unparseable is None, not zero.
    """
    if headers is None:
        return None
    try:
        return float(_header(headers, "retry-after-ms")) / 1000
    except (TypeError, ValueError):
        pass
    retry_header = _header(headers, "retry-after")
    try:
        # The spec says integer seconds; the SDK accepts a float, so do we.
        return float(retry_header)
    except (TypeError, ValueError):
        pass
    retry_date_tuple = email.utils.parsedate_tz(retry_header)
    if retry_date_tuple is None:
        return None
    return float(email.utils.mktime_tz(retry_date_tuple) - time.time())


def is_retryable(exc: BaseException) -> bool:
    """Would the SDK have retried this? Same rule, at our boundary."""
    if isinstance(exc, (KeyboardInterrupt, SystemExit)):
        return False
    # The provider's own verdict comes FIRST, ahead of the status.
    explicit = should_retry_header(response_headers(exc))
    if explicit is not None:
        return explicit
    name = type(exc).__name__
    # Connection and timeout failures never reached the provider's answer.
    if "APIConnection" in name or "APITimeout" in name or isinstance(
            exc, (ConnectionError, TimeoutError)):
        return True
    status = _status_of(exc)
    if status is None:
        return False
    return status in _RETRY_STATUSES or 500 <= status < 600


def _backoff(attempt: int, headers: Any = None) -> float:
    """How long before attempt N+1. The provider's delay, else backoff+jitter.

    `attempt` is 1-based, so the first failure gives `INITIAL_RETRY_DELAY` —
    the SDK's `nb_retries = 0` case.
    """
    retry_after = parse_retry_after(headers)
    if retry_after is not None and 0 < retry_after <= RETRY_AFTER_MAX_S:
        return retry_after
    sleep_seconds = min(_RETRY_BASE_DELAY_S * pow(2.0, attempt - 1),
                        _RETRY_MAX_DELAY_S)
    # Plus-or-minus, as the SDK does it: a multiplier in (0.75, 1.0]. Without
    # it every caller rate-limited at the same moment retries at the same
    # moment.
    timeout = sleep_seconds * (1 - 0.25 * random())
    return timeout if timeout >= 0 else 0.0


def call(stage: str, fn: Callable[[], Any], *, model: str,
         max_retries: int = MAX_RETRIES, sleep: Callable[[float], None] = time.sleep
         ) -> Any:
    """Run one logical provider request, recording EVERY HTTP attempt.

    `fn` must issue exactly one HTTP request — build the client with
    `max_retries=0` — because this function owns the retrying now and counts
    one ledger row per call of it.

    The request happens either way. Every failure inside the accounting is
    swallowed and logged: a ledger that can take Home's analysis down is worse
    than one with a gap in it, and the gap is visible in the report's coverage.
    """
    ctx = _CTX.get()
    seq = None
    if ctx is not None:
        ctx["seq"] = int(ctx.get("seq") or 0) + 1
        seq = ctx["seq"]

    attempt = 0
    while True:
        attempt += 1
        request_key = uuid.uuid4().hex
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
            "http_attempt": attempt,
            "model_requested": model,
            "started_at": _stamp(started),
        })
        monotonic = time.monotonic()
        try:
            resp = fn()
        except BaseException as exc:  # noqa: BLE001 — recorded, then decided
            if opened:
                # A failed attempt reached the provider or did not; we cannot
                # tell, and we do not claim it was charged. Usage stays unknown
                # and the cost stays NULL.
                _settle(request_key, model, None, started, monotonic,
                        outcome=_outcome_for(exc), error_kind=_error_kind(exc))
            if attempt <= max_retries and is_retryable(exc):
                # The same response the decision was made from also carries
                # how long the provider wants us to wait.
                sleep(_backoff(attempt, response_headers(exc)))
                continue
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
                  "cache_read_input_tokens": None,
                  "usage_reported": 0, "usage_complete": 0})
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
                "usage_reported", "usage_complete")},
            # The FULL cost, or NULL when something billable is unknown.
            "estimated_cost_usd": cost["total_usd"],
            # What we can price from what did arrive. A floor, in its own
            # column, never added to a total as though it were one.
            "known_subtotal_usd": cost["known_subtotal_usd"],
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
