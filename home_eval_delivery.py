"""What the model was actually shown, and whether it was enough to look at all.

Three things were being treated as one, and each substitution let a false
"delivered" through:

  RETRIEVED   A tool ran and the server returned rows. Says nothing about what
              reached the model — a response can be trimmed or dropped whole.
  DELIVERED   The rows that survived fitting and were sent. `InvestigationSession`
              already tracks this for EVIDENCE (`_settle_delivery` promotes into
              `delivered`); it does NOT track it for CALCULATIONS, which are
              registered during retrieval and would otherwise look delivered the
              moment a tool ran.
  SUFFICIENT  The delivered contents establish the thing the scenario turns on.
              Run ids arriving is not the same as the failing step arriving.

Two reviewer reproductions, both of which returned
`value: true, reason: "every requirement was delivered"`:

  * Scenario B with `list_comparable_runs` + `compare_outcome_mix` and NO
    `inspect_run`. The run ids were delivered; no `failed_span` was, and the
    word `approval_service` appears nowhere in the delivered payloads. The
    model had not been shown the shared failing step it was meant to find.
  * Scenario F with `session.run(...)` and no `fit`/`retrieve`. Nothing was
    delivered at all — `delivered_keys: 0` — and twelve calculation ids in the
    registry were read as twelve delivered calculations.

So delivery is captured from the settlement path, calculations are counted only
when both the id AND its supporting value survived into the sent payload, and
requirements are structured per scenario rather than "did an id show up".

**This is not semantic grading.** "The evidence needed to assess the pattern
arrived" and "the model correctly discovered the pattern" stay separate
questions; only the first is answered here, and it is answered from delivered
contents, never from wording.
"""
from __future__ import annotations

import copy
from typing import Any

# ---------------------------------------------------------------------------
# Test-only instrumentation of the real settlement path
# ---------------------------------------------------------------------------


def recording_session_class(base):
    """A subclass of `InvestigationSession` that remembers what it delivered.

    It overrides exactly one method, calls `super()` first, and changes no
    retrieval behaviour: `_settle_delivery` is the point where the product
    itself decides what reached the model, so hooking it is the only way to
    record delivery without inventing a second notion of it.
    """

    class RecordingSession(base):  # type: ignore[valid-type,misc]
        def __init__(self, **kw):
            super().__init__(**kw)
            # One entry per response that actually reached the model.
            self.eval_delivered_payloads: list[dict[str, Any]] = []
            self.eval_dropped_responses: list[dict[str, Any]] = []

        def _settle_delivery(self, resp, sent, *, whole_result_dropped):
            super()._settle_delivery(resp, sent,
                                     whole_result_dropped=whole_result_dropped)
            if resp is None:
                return
            tool = resp.get("tool")
            if whole_result_dropped:
                # Nothing reached the model. Recording it as delivered would be
                # the exact error this file exists to prevent.
                self.eval_dropped_responses.append({"tool": tool})
                return
            self.eval_delivered_payloads.append({
                "tool": tool,
                # A deep copy, so a later retrieval mutating a shared dict
                # cannot rewrite what we recorded as delivered.
                "payload": copy.deepcopy(sent) if isinstance(sent, dict) else sent,
            })

    RecordingSession.__name__ = f"Recording{getattr(base, '__name__', 'Session')}"
    return RecordingSession


# ---------------------------------------------------------------------------
# Calculations: delivered only with their supporting values
# ---------------------------------------------------------------------------

def _mix_support(payload: dict) -> dict[str, Any]:
    """`compare_outcome_mix` calc ids whose number is present in what was sent."""
    ids = payload.get("calculation_ids") or {}
    out: dict[str, Any] = {}
    cur, prev = payload.get("current") or {}, payload.get("previous") or {}
    for name, calc_id in ids.items():
        window, _, field = name.partition("_")
        if window == "current" and field in cur:
            out[calc_id] = cur[field]
        elif window == "previous" and field in prev:
            out[calc_id] = prev[field]
        elif window == "delta" and field in cur and field in prev:
            out[calc_id] = cur[field] - prev[field]
    return out


def _wait_support(payload: dict) -> dict[str, Any]:
    ids = payload.get("calculation_ids") or {}
    holders = payload.get("by_holder") or {}
    return {calc_id: holders[holder] for holder, calc_id in ids.items()
            if holder in holders}


def _cost_support(payload: dict) -> dict[str, Any]:
    ids = payload.get("calculation_ids") or {}
    return {calc_id: payload[field] for field, calc_id in ids.items()
            if payload.get(field) is not None}


# A tool whose calculations we cannot verify support for is NOT counted. Failing
# closed is the only safe default: an uncounted calculation makes a requirement
# read unsatisfied, which is recoverable; a counted one that never arrived is
# the false positive.
_CALC_SUPPORT = {
    "compare_outcome_mix": _mix_support,
    "wait_concentration": _wait_support,
    "cost_evidence": _cost_support,
}


def delivery_report(sessions: list[Any], *, reason_if_none: str | None = None
                    ) -> dict[str, Any]:
    """What one investigation delivered, from its own sessions.

    `available: False` means we could not look. `observed_empty` means we
    looked and nothing had been delivered. They are different answers and the
    caller must be able to tell them apart.
    """
    if not sessions:
        return {"available": False,
                "reason": reason_if_none or "no investigation session was captured"}
    keys: set[str] = set()
    payloads: list[dict[str, Any]] = []
    calcs: dict[str, Any] = {}
    dropped: list[dict[str, Any]] = []
    instrumented = 0
    for sess in sessions:
        keys |= set(getattr(sess, "delivered", ()) or ())
        entries = getattr(sess, "eval_delivered_payloads", None)
        if entries is None:
            continue
        instrumented += 1
        dropped.extend(getattr(sess, "eval_dropped_responses", ()) or ())
        for entry in entries:
            payloads.append(entry)
            support = _CALC_SUPPORT.get(entry.get("tool"))
            if support and isinstance(entry.get("payload"), dict):
                calcs.update(support(entry["payload"]))

    if instrumented == 0:
        # Evidence keys are still trustworthy (the product tracks them), but
        # nothing can be said about calculations or payload contents.
        return {
            "available": True, "keys": sorted(keys), "payloads": [],
            "calculations": [], "calculation_values": {},
            "payload_capture": False,
            "observed_empty": not keys,
            "note": ("Evidence keys come from the product's own delivery "
                     "ledger. Payload capture was not installed, so "
                     "calculations and contents cannot be verified."),
        }
    return {
        "available": True,
        "keys": sorted(keys),
        "payloads": payloads,
        "calculations": sorted(calcs),
        "calculation_values": calcs,
        "dropped_responses": dropped,
        "payload_capture": True,
        "observed_empty": not keys and not calcs,
        "note": ("Captured at `_settle_delivery` — the product's own decision "
                 "about what reached the model. Calculations count only when "
                 "the id AND its supporting value survived into the sent "
                 "payload."),
    }


# ---------------------------------------------------------------------------
# Structured requirements
# ---------------------------------------------------------------------------

PASS, FAIL, UNKNOWN = "satisfied", "not_delivered", "unknown"


def _payloads(delivery: dict, tool: str) -> list[dict]:
    return [p["payload"] for p in (delivery.get("payloads") or [])
            if p.get("tool") == tool and isinstance(p.get("payload"), dict)]


def _failing_steps(delivery: dict) -> dict[int, set[str]]:
    """run id -> the failing step names actually delivered for it.

    Read from the `failed_spans` list of delivered `inspect_run` payloads, which
    is where the step name lives. A `run:` key proves a SUMMARY arrived; only
    this proves the step did.
    """
    out: dict[int, set[str]] = {}
    for payload in _payloads(delivery, "inspect_run"):
        run_id = payload.get("run_id")
        if run_id is None:
            continue
        names: set[str] = set()
        for span in payload.get("failed_spans") or []:
            if not isinstance(span, dict):
                continue
            for field in ("tool", "tool_name", "span_name", "name",
                          "status_message", "message"):
                value = span.get(field)
                if isinstance(value, str) and value:
                    names.add(value)
        if names:
            out.setdefault(int(run_id), set()).update(names)
    return out


def _completed_runs(delivery: dict) -> set[int]:
    """Runs whose DELIVERED payload shows them completed."""
    done: set[int] = set()
    for payload in _payloads(delivery, "inspect_run"):
        if payload.get("run_id") is None:
            continue
        if payload.get("outcome") == "completed" or payload.get("state") == "done":
            done.add(int(payload["run_id"]))
    for payload in _payloads(delivery, "list_comparable_runs"):
        for row in payload.get("runs") or []:
            if isinstance(row, dict) and row.get("outcome") == "completed" \
                    and row.get("run_id") is not None:
                done.add(int(row["run_id"]))
    return done


def _check_one(req: dict, delivery: dict) -> dict:
    """One requirement against delivered contents. Never a wording judgement."""
    kind = req.get("kind")
    keys = set(delivery.get("keys") or [])
    captured = bool(delivery.get("payload_capture"))

    def result(status, detail=""):
        return {"requirement": req, "status": status, "detail": detail}

    if kind == "runs_listed":
        missing = [r for r in req["runs"] if f"run:{r}" not in keys]
        return result(PASS if not missing else FAIL,
                      f"run summaries missing: {missing}" if missing else
                      f"{len(req['runs'])} run summaries delivered")

    if kind == "failing_step":
        if not captured:
            return result(UNKNOWN, "payload capture unavailable; the failing "
                                   "step cannot be verified from keys alone")
        steps = _failing_steps(delivery)
        want = req.get("step")
        if want is None:
            # "the failing-step details arrived", without naming which. Scenario
            # I needs exactly this: its point is that the three runs failed at
            # DIFFERENT steps, so demanding a shared one would be the opposite
            # of the pattern under assessment.
            missing = [r for r in req["runs"] if not steps.get(r)]
            return result(PASS if not missing else FAIL,
                          f"runs with no delivered failing step: {missing}"
                          if missing else
                          f"failing-step details delivered for all "
                          f"{len(req['runs'])} runs")
        missing = [r for r in req["runs"]
                   if not any(want in name for name in steps.get(r, ()))]
        return result(PASS if not missing else FAIL,
                      f"runs with no delivered {want!r} failing step: {missing}"
                      if missing else
                      f"{want!r} delivered as a failing step for all "
                      f"{len(req['runs'])} runs")

    if kind == "recovery":
        if not captured:
            return result(UNKNOWN, "payload capture unavailable")
        steps, done = _failing_steps(delivery), _completed_runs(delivery)
        want = req["step"]
        no_failure = [r for r in req["runs"]
                      if not any(want in n for n in steps.get(r, ()))]
        no_completion = [r for r in req["runs"] if r not in done]
        if no_failure or no_completion:
            return result(FAIL, f"missing failure evidence for {no_failure}, "
                                f"missing completion evidence for {no_completion}")
        return result(PASS, "both the failing step and the completion were "
                            "delivered for every run")

    if kind == "comparison":
        if not captured:
            return result(UNKNOWN, "payload capture unavailable; a calculation "
                                   "id alone does not establish delivery")
        for payload in _payloads(delivery, "compare_outcome_mix"):
            if req.get("job") is not None and payload.get("current", {}).get(
                    "job_id") != req["job"]:
                continue
            if req.get("days") is not None and payload.get("window_days") != req["days"]:
                continue
            cur, prev = payload.get("current") or {}, payload.get("previous") or {}
            if all(f in cur and f in prev for f in req["fields"]):
                return result(PASS, f"comparison for job {req.get('job')} over "
                                    f"{req.get('days')}d delivered with "
                                    f"{req['fields']}")
        return result(FAIL, f"no delivered comparison for job {req.get('job')} "
                            f"over {req.get('days')}d carrying {req['fields']}")

    if kind == "waits":
        if not captured:
            return result(UNKNOWN, "payload capture unavailable")
        seen: dict[int, Any] = {}
        for payload in _payloads(delivery, "wait_concentration"):
            for row in payload.get("waits") or []:
                if isinstance(row, dict) and row.get("run_id") is not None:
                    seen[int(row["run_id"])] = row
        missing = [r for r in req["runs"] if r not in seen]
        if missing:
            return result(FAIL, f"no delivered wait row for {missing}")
        if req.get("holder_required"):
            unheld = [r for r in req["runs"] if not seen[r].get("waiting_on")]
            if unheld:
                return result(FAIL, f"delivered wait rows name no holder for {unheld}")
        return result(PASS, f"{len(req['runs'])} wait rows delivered with holders")

    if kind == "agent_context":
        return result(PASS if f"agent_context:{req['agent']}" in keys else FAIL,
                      f"agent_context:{req['agent']}")

    if kind == "cost":
        if not captured:
            return result(UNKNOWN, "payload capture unavailable")
        for payload in _payloads(delivery, "cost_evidence"):
            if req.get("days") is not None and payload.get("window_days") != req["days"]:
                continue
            if all(payload.get(f) is not None for f in req["fields"]):
                return result(PASS, f"cost evidence delivered with {req['fields']}")
        return result(FAIL, f"no delivered cost evidence carrying {req['fields']}")

    if kind == "unretrievable":
        return result(FAIL, req.get("why") or "no tool exposes this")

    # An unrecognised requirement must never read as satisfied.
    return result(UNKNOWN, f"unsupported requirement kind {kind!r}")


def check_requirements(requirements: list[dict], delivery: dict) -> dict:
    """Did this investigation receive what the scenario turns on?

    `True` only when every requirement is satisfied from delivered contents.
    `False` when one definitively was not. `None` when something could not be
    established — an unsupported requirement kind, or missing instrumentation.
    An unknown never passes.

    When the report carries `per_execution` (a reader with one or more job
    executions), every execution is assessed ON ITS OWN and the reader's answer
    is the strongest single-execution answer. Requirements are never pooled
    across executions: one execution's `inspect_run` cannot complete another
    execution's comparison.

    An execution whose delivery is UNAVAILABLE is assessed too, and it assesses
    as `None`. Leaving it out would let one observed failure stand as a
    definitive reader-wide "this never arrived" while another execution of the
    same reader was never looked at — an absence asserted from a gap in the
    instrumentation rather than from the record.
    """
    parts = (delivery or {}).get("per_execution")
    if parts is not None:
        return _check_across_executions(requirements, delivery, parts)
    return _check_one_execution(requirements, delivery)


# True beats "could not establish", which beats a definite "not delivered": a
# reader whose other execution left something unverifiable must not be reported
# as having definitively missed it.
_RANK = {True: 2, None: 1, False: 0}


def _check_across_executions(requirements: list[dict], delivery: dict,
                             parts: list[dict]) -> dict:
    present = [p for p in parts if p]
    if not present:
        return _check_one_execution(requirements, delivery)
    # EVERY execution, unavailable ones included. An unavailable one yields
    # `None` from `_check_one_execution`, which is exactly what it is worth.
    verdicts = [_check_one_execution(requirements, p) for p in present]
    observed = [v for p, v in zip(present, verdicts) if p.get("available")]
    best = max(verdicts, key=lambda v: _RANK[v["value"]])
    out = dict(best)
    out["executions_assessed"] = len(present)
    out["executions_observed"] = len(observed)
    out["executions_unavailable"] = len(present) - len(observed)
    out["per_execution_values"] = [v["value"] for v in verdicts]
    # The reader-wide answer and any single execution's verified answer are
    # different facts and are reported as both. `verified_by_execution` says an
    # execution of this reader definitely received everything; it does NOT say
    # the reader's capture was complete, which is `payload_capture` /
    # `payload_capture_partial` below.
    out["verified_by_execution"] = any(v["value"] is True for v in verdicts)
    out["payload_capture"] = bool(delivery.get("payload_capture"))
    if delivery.get("payload_capture_partial"):
        # Some execution of this reader was not instrumented, or could not be
        # read at all. Whatever the chosen verdict, the reader's capture is not
        # complete and the report must not read as though it were.
        out["payload_capture_partial"] = True
        out["reason"] = (out["reason"] + "; payload capture was unavailable for "
                         f"{delivery.get('executions_without_payload_capture', 0)} "
                         f"of this reader's {len(present)} job executions")
    if len(present) > 1:
        out["reason"] += (f" (strongest of {len(present)} job executions, each "
                          "assessed on its own delivery)")
    return out


def _check_one_execution(requirements: list[dict], delivery: dict) -> dict:
    if not delivery or not delivery.get("available"):
        return {
            "value": None,
            "requirements": requirements,
            "checked": [],
            "unretrievable": [r for r in requirements
                              if r.get("kind") == "unretrievable"],
            "reason": (delivery or {}).get("reason")
                      or "no delivery information for this investigation",
        }
    checked = [_check_one(r, delivery) for r in requirements]
    failed = [c for c in checked if c["status"] == FAIL
              and c["requirement"].get("kind") != "unretrievable"]
    unknown = [c for c in checked if c["status"] == UNKNOWN]
    unretrievable = [c for c in checked
                     if c["requirement"].get("kind") == "unretrievable"]

    if failed or unretrievable:
        value = False
    elif unknown:
        value = None
    else:
        value = True

    parts = []
    if failed:
        parts.append("this run did not retrieve: "
                     + "; ".join(c["detail"] for c in failed))
    if unretrievable:
        parts.append("no tool can satisfy: "
                     + "; ".join(c["detail"] for c in unretrievable))
    if unknown:
        parts.append("could not be established: "
                     + "; ".join(c["detail"] for c in unknown))
    if not parts:
        parts.append("every requirement was established from delivered contents")

    return {
        "value": value,
        "requirements": requirements,
        "checked": checked,
        "satisfied": [c for c in checked if c["status"] == PASS],
        "failed": failed,
        "unknown": unknown,
        "unretrievable": unretrievable,
        "delivered_keys": len(delivery.get("keys") or []),
        "delivered_calculations": len(delivery.get("calculations") or []),
        "payload_capture": bool(delivery.get("payload_capture")),
        "reason": "; ".join(parts),
    }
