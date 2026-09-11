"""The allowlist: everything the investigation may look at, and nothing else.

The model chooses WHICH of these to call and with what arguments. It never
chooses what they do. Each one is a named, read-only retrieval over the same
tables and the same definitions the Home snapshot uses, executed with the
scope the SERVER resolved — the model cannot supply an account, widen a
visibility, reach another tenant, write SQL, or fetch a URL.

Three rules this module exists to keep:

1. **Scope is an argument the caller cannot set.** `account_id` and
   `only_user_ids` come from the request's resolved seat and are closed over
   before the model sees a tool. An id outside that scope returns "not found",
   not an error a model could read as confirmation it exists.

2. **Everything is bounded, and says so.** Rows, events and text are capped,
   and every result carries the truncation and coverage metadata it was given.
   A conclusion drawn from a fragment that looked whole is the failure mode
   this whole file is arranged against.

3. **Retrieved content is EVIDENCE, never instruction.** Titles, error
   messages and recorded text come from agents and the outside world. They are
   wrapped and labelled untrusted before they reach a prompt; anything inside
   them that reads like a directive is data about what an agent said.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import database

logger = logging.getLogger("trovis")

# Per-analysis budgets. These are ceilings on ONE investigation, not per call.
MAX_TOOL_CALLS = 14
MAX_RETRIEVED_ROWS = 400
MAX_RETRIEVED_EVENTS = 600
MAX_TOOL_RESULT_CHARS = 6000

# Text that came from outside. Truncated hard: an investigation does not need
# a whole transcript to establish that a step failed, and a long one is both a
# token bill and a bigger injection surface.
MAX_TEXT_CHARS = 400


def _clip(value: Any, limit: int = MAX_TEXT_CHARS) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + "…"
    return value


def _clip_deep(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _clip_deep(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_clip_deep(v) for v in node]
    return _clip(node)


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "list_comparable_runs",
        "description": (
            "Runs (work items) for one job or one agent, newest first, with "
            "their recorded outcome, span count and error-span count. Use it "
            "to see whether something happens repeatedly or once. `outcome` "
            "filters to completed / abandoned / open using the record's own "
            "definitions — there is no success taxonomy beyond these."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "integer", "description": "An existing job (workflow) id."},
                "agent": {"type": "string", "description": "An agent's service name."},
                "outcome": {"type": "string", "enum": ["completed", "abandoned", "open"]},
                "days_back": {"type": "integer", "description": "Window in days (max 90)."},
                "limit": {"type": "integer", "description": "Max runs (max 50)."},
            },
        },
    },
    {
        "name": "inspect_run",
        "description": (
            "One run's lifecycle events in order plus its failing operations. "
            "Use it to check what actually happened — including whether an "
            "error was followed by recovery and completion, which is friction "
            "rather than failed work."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "integer"},
                "event_limit": {"type": "integer", "description": "Max events (max 40)."},
            },
            "required": ["run_id"],
        },
    },
    {
        "name": "compare_outcome_mix",
        "description": (
            "SERVER-CALCULATED outcome counts (started / completed / abandoned "
            "/ still open) for two equal-length windows, optionally for one job "
            "or agent. This is how you compare periods: you may cite these "
            "numbers, and you may not compute your own."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "Length of each window (max 45)."},
                "job_id": {"type": "integer"},
                "agent": {"type": "string"},
            },
            "required": ["days"],
        },
    },
    {
        "name": "wait_concentration",
        "description": (
            "Open work that is waiting right now, with who or what holds it "
            "and for how long. Present tense only — the record keeps no "
            "history of waiting, so this cannot show a trend."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "description": "Max rows (max 50)."}},
        },
    },
    {
        "name": "agent_context",
        "description": (
            "What an agent is for, as the record has it: its description, "
            "when it was first and last seen, and its span/error counts. No "
            "cost fields — money is gated separately."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"agent": {"type": "string"}},
            "required": ["agent"],
        },
    },
    {
        "name": "cost_evidence",
        "description": (
            "Organization-wide stored spend for a window, with its pricing "
            "coverage. Only available when the reader's seat includes the "
            "financial surface. Spend is recorded per account and agent, not "
            "per work scope, so it can never be attributed to a narrowed set "
            "of work."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "Window in days (max 45)."},
            },
            "required": ["days"],
        },
    },
]


class ToolBudget:
    """One investigation's allowance, spent down as it goes.

    Exhausting a budget is a REPORTABLE OUTCOME, not an error: the assessment
    step is told what it could not see, and a conclusion that needed the
    unread rows has to be abstained from or qualified.
    """

    def __init__(
        self,
        max_calls: int = MAX_TOOL_CALLS,
        max_rows: int = MAX_RETRIEVED_ROWS,
        max_events: int = MAX_RETRIEVED_EVENTS,
    ):
        self.max_calls = max_calls
        self.max_rows = max_rows
        self.max_events = max_events
        self.calls = 0
        self.rows = 0
        self.events = 0
        self.exhausted: list[str] = []

    def can_call(self) -> bool:
        return self.calls < self.max_calls and not self.exhausted

    def spend_call(self) -> None:
        self.calls += 1
        if self.calls >= self.max_calls:
            self.exhausted.append("tool_calls")

    def spend_rows(self, n: int) -> None:
        self.rows += int(n or 0)
        if self.rows >= self.max_rows:
            self.exhausted.append("retrieved_rows")

    def spend_events(self, n: int) -> None:
        self.events += int(n or 0)
        if self.events >= self.max_events:
            self.exhausted.append("retrieved_events")

    def report(self) -> dict[str, Any]:
        """The BUDGET's own view. Not the whole coverage story.

        This says whether the allowance ran out. It cannot know that a query
        hit its own row cap, that assignment resolution was incomplete, that a
        result was trimmed to fit, or that a tool failed — those are facts
        about individual retrievals, and they live on the session (see
        `InvestigationSession.retrieval_report`). Passing THIS to validation
        was the bug: a tool returned 30 rows, size fitting delivered 15, and
        `complete: true` let "no other job is affected" through as supported.
        """
        return {
            "tool_calls": self.calls,
            "tool_call_limit": self.max_calls,
            "rows_retrieved": self.rows,
            "row_limit": self.max_rows,
            "events_retrieved": self.events,
            "event_limit": self.max_events,
            "exhausted": sorted(set(self.exhausted)),
            "budget_complete": not self.exhausted,
        }


class InvestigationSession:
    """Runs the allowlist for one analysis, under one resolved scope.

    Also the EVIDENCE LEDGER. Every row a tool returns is recorded here, keyed
    the way a finding must cite it (`run:41`, `run_event:900`, `calc:...`). A
    finding that names something absent from this ledger did not read it — it
    made it up — and `findings.validate_finding` rejects on exactly that.
    """

    def __init__(
        self,
        *,
        account_id: int | None,
        only_user_ids: list[int] | None,
        financial_visible: bool,
        budget: ToolBudget | None = None,
        now: datetime | None = None,
    ):
        self.account_id = account_id
        self.only_user_ids = only_user_ids
        self.financial_visible = financial_visible
        self.budget = budget or ToolBudget()
        self.now = now or datetime.now(timezone.utc)
        # kind:ref -> the payload as retrieved
        self.evidence: dict[str, dict[str, Any]] = {}
        # Keys the model was ACTUALLY SHOWN. A key in `evidence` but not here
        # was retrieved and then trimmed away before delivery — it is not
        # citable, and it is not evidence.
        self.delivered: set[str] = set()
        # calculation id -> {value, ...}
        self.calculations: dict[str, dict[str, Any]] = {}
        self.transcript: list[dict[str, Any]] = []
        # Every way this retrieval fell short of a complete search. Budget
        # exhaustion is only one of them; see `retrieval_report`.
        self.limitations: list[dict[str, Any]] = []
        # Provenance for the response currently being assembled: which ledger
        # keys THIS tool call introduced, and where each one sits in the
        # payload so delivery can be reconciled after trimming.
        self._response: dict[str, Any] | None = None
        # Provenance parked by payload identity, so `fit` reconciles the
        # response it was actually handed. Keying it on "the last `run`" would
        # make correctness depend on `run`/`fit` being perfectly interleaved,
        # and a caller that retrieves twice before sending either result would
        # silently attribute one response's rows to the other. The payload is
        # held alongside its provenance so the id cannot be recycled.
        self._responses: dict[int, tuple[Any, dict[str, Any]]] = {}

    # -- ledger ---------------------------------------------------------
    def _record(
        self, kind: str, ref: Any, payload: dict[str, Any],
        *, listed: tuple[str, Any] | None = None,
    ) -> None:
        """Record one retrieved row, with the provenance delivery needs.

        `listed` names the payload list this row travels in (`("runs", 41)`),
        so when `fit` shortens that list we can tell which ledger entries this
        response failed to deliver — WITHOUT touching entries an earlier
        response already delivered. Dropping every entry of a kind was the bug
        (`forget_beyond`): a run delivered by call 1 vanished when call 3's run
        list was trimmed, and a finding legitimately citing it was then
        rejected as a fabrication.
        """
        key = f"{kind}:{ref}"
        first_time = key not in self.evidence
        self.evidence[key] = payload
        if self._response is not None:
            if first_time and key not in self.delivered:
                self._response["introduced"].add(key)
            if listed is not None:
                self._response["listed"].setdefault(listed[0], []).append(
                    (str(listed[1]), key)
                )
            else:
                self._response["unlisted"].add(key)

    def _calc(self, calc_id: str, value: Any, detail: dict[str, Any]) -> str:
        self.calculations[calc_id] = {"value": value, **detail}
        return calc_id

    def note_limitation(self, kind: str, **detail: Any) -> None:
        """Record one concrete way this retrieval was less than the whole.

        These travel into `derive_coverage` and decide whether an exhaustive
        claim ("none", "only", "all", a percentage) may be published at all.
        """
        entry = {"kind": kind, **detail}
        if entry not in self.limitations:
            self.limitations.append(entry)

    # -- dispatch -------------------------------------------------------
    def run(self, name: str, raw_input: dict[str, Any]) -> dict[str, Any]:
        """Execute one allowlisted tool. Never raises into the model loop."""
        self._response = {"introduced": set(), "listed": {}, "unlisted": set(),
                          "tool": name}
        if not self.budget.can_call():
            self.note_limitation(
                "budget_exhausted", tool=name,
                exhausted=sorted(set(self.budget.exhausted)),
            )
            self._response = None
            return {
                "error": "budget_exhausted",
                "exhausted": sorted(set(self.budget.exhausted)),
                "note": "No further retrieval is available for this analysis.",
            }
        self.budget.spend_call()
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            self.note_limitation("unknown_tool", tool=name)
            self._response = None
            return {"error": f"unknown tool {name!r}"}
        try:
            result = handler(raw_input or {})
        except Exception as exc:  # noqa: BLE001 — a tool failure is data, not a crash
            logger.warning("[investigation] tool %s failed: %s", name, exc)
            # A question the investigation asked and did not get an answer to.
            # The search is incomplete whatever the budget says.
            self.note_limitation("retrieval_failed", tool=name)
            result = {"error": f"tool {name} failed"}
        result = _clip_deep(result)
        if isinstance(result, dict) and result.get("error"):
            self.note_limitation("retrieval_failed", tool=name,
                                 error=str(result.get("error"))[:80])
        self.transcript.append({"tool": name, "input": raw_input, "ok": "error" not in result})
        self._park_response(result)
        return result

    def _park_response(self, result: Any) -> None:
        """Hold this call's provenance until `fit` is handed its payload."""
        resp, self._response = self._response, None
        if resp is None:
            return
        if len(self._responses) > 32:  # a tool loop is bounded; this is belt
            self._responses.pop(next(iter(self._responses)))
        self._responses[id(result)] = (result, resp)

    # -- coverage -------------------------------------------------------
    def retrieval_report(self) -> dict[str, Any]:
        """What this retrieval session could and could not establish.

        The budget's own report is one input. The others are facts only the
        individual retrievals know:

          query truncation        a row or event cap was reached
          incomplete resolution   assignment/scope membership was not resolved
          size trimming           `fit` dropped entries to make a result send
          dropped results         a whole tool result was too large to send
          retrieval failure       a tool errored, leaving its question open

        `complete` is true only when NONE of these fired. It is what
        `findings.derive_coverage` reads, and it is what stops "no other job is
        affected" being published over a search that returned half its rows.
        """
        budget = self.budget.report()
        kinds = sorted({str(l.get("kind")) for l in self.limitations})
        return {
            **budget,
            "limitations": list(self.limitations),
            "limitation_kinds": kinds,
            "exhausted": sorted(set(self.budget.exhausted)),
            "evidence_delivered": len(self.delivered),
            "evidence_retrieved": len(self.evidence),
            "complete": bool(budget["budget_complete"]) and not self.limitations,
        }

    # Lists that may be shortened to make a result fit, in the order they are
    # sacrificed. Rows first: dropping the 20th comparable run costs less than
    # dropping the events that explain the one run we opened.
    _TRIMMABLE = ("runs", "waits", "events", "failed_spans")

    def fit(self, result: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """Shrink a result to the size budget WITHOUT breaking its JSON.

        The bug this replaces: `result_text` serialized the payload and then
        sliced the string at 6,000 characters, so a permitted 25-row result
        with long titles produced invalid JSON — and the investigation loop
        called `json.loads` on it and raised `JSONDecodeError`.

        So the STRUCTURE is trimmed before serialization, not the text after
        it. Whole list entries are dropped (never half an object), the first
        entries are kept so identifiers and the earliest records survive, and
        what was dropped is stated in `size_truncated` rather than implied.

        The payload is serialized at most a handful of times — once per trim
        round, halving the list each time — and the returned string is the one
        the caller sends, so nothing is re-serialized downstream.

        Returns (payload_as_sent, serialized) so the evidence ledger can be
        reconciled with what the model actually received.
        """
        parked = self._responses.pop(id(result), None)
        resp = parked[1] if parked else None
        tool = (resp or {}).get("tool")
        trimmed = dict(result)
        dropped: dict[str, int] = {}
        blob = json.dumps(trimmed, default=str)
        for key in self._TRIMMABLE:
            if len(blob) <= MAX_TOOL_RESULT_CHARS:
                break
            items = trimmed.get(key)
            if not isinstance(items, list) or not items:
                continue
            while len(blob) > MAX_TOOL_RESULT_CHARS and items:
                # Halve rather than peel one at a time: a 25-row result with
                # long titles would otherwise cost 20 serializations.
                keep = max(1, len(items) // 2) if len(items) > 1 else 0
                dropped[key] = dropped.get(key, 0) + (len(items) - keep)
                items = items[:keep]
                trimmed[key] = items
                blob = json.dumps(trimmed, default=str)
        if dropped:
            trimmed["size_truncated"] = {
                "dropped": dropped,
                "reason": "result exceeded the per-call size budget",
                "budget_chars": MAX_TOOL_RESULT_CHARS,
                "note": (
                    "Entries were dropped from the END of these lists. What "
                    "remains is a subset; do not read it as the whole set."
                ),
            }
            blob = json.dumps(trimmed, default=str)
        whole_result_dropped = False
        if len(blob) > MAX_TOOL_RESULT_CHARS:
            # Nothing left to drop — the scalar fields alone are over budget.
            # Replace rather than slice: a valid small object beats a large
            # broken one.
            trimmed = {
                "error": "result_too_large_to_send",
                "tool_result_dropped": True,
                "budget_chars": MAX_TOOL_RESULT_CHARS,
            }
            blob = json.dumps(trimmed)
            whole_result_dropped = True
            self.note_limitation("result_dropped", tool=tool)
        if dropped:
            self.note_limitation("size_trimmed", tool=tool, dropped=dict(dropped))
        self._settle_delivery(resp, trimmed,
                              whole_result_dropped=whole_result_dropped)
        return trimmed, blob

    def _settle_delivery(
        self, resp: dict[str, Any] | None, sent: dict[str, Any], *,
        whole_result_dropped: bool,
    ) -> None:
        """Reconcile the ledger with what this response actually delivered.

        Two rules, and the second is the one the old `forget_beyond` broke:

          * A record this response INTRODUCED and did not deliver is removed.
            It was retrieved but never shown, so citing it would be citing
            something the investigation never received. If the whole result was
            dropped, that is every record it introduced.
          * A record delivered by an EARLIER response is kept, even when this
            response also mentioned it and then trimmed it away. Overlapping
            results are normal — the same run comes back from a run list and
            from an inspection — and losing evidence already put in front of
            the model would invalidate findings that legitimately rest on it.

        So removal is scoped to what this one response newly introduced, never
        to a kind.
        """
        if resp is None:
            return
        introduced: set[str] = set(resp["introduced"])
        if whole_result_dropped:
            # Nothing reached the model. Anything this response was the first
            # to retrieve is unseen content and must not become citable.
            for key in introduced:
                self.evidence.pop(key, None)
            return

        delivered_now: set[str] = set(resp["unlisted"])
        for list_name, entries in resp["listed"].items():
            items = sent.get(list_name)
            kept_refs = set()
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        for field in ("run_id", "event_id", "ref", "id"):
                            if item.get(field) is not None:
                                kept_refs.add(str(item[field]))
            for ref, key in entries:
                if ref in kept_refs:
                    delivered_now.add(key)
        self.delivered |= delivered_now
        for key in introduced - delivered_now:
            # Retrieved by this response, trimmed before it was sent, and not
            # delivered by any earlier response either.
            if key not in self.delivered:
                self.evidence.pop(key, None)

    def result_text(self, result: dict[str, Any]) -> str:
        """The serialized, size-bounded result. Always valid JSON."""
        return self.fit(result)[1]

    # -- tools ----------------------------------------------------------
    def _tool_list_comparable_runs(self, inp: dict[str, Any]) -> dict[str, Any]:
        days = max(1, min(90, int(inp.get("days_back") or 30)))
        res = database.investigation_runs(
            self.account_id,
            only_user_ids=self.only_user_ids,
            workflow_id=_as_int(inp.get("job_id")),
            service_name=_as_str(inp.get("agent")),
            outcome=_as_outcome(inp.get("outcome")),
            since_utc=self.now - timedelta(days=days),
            limit=int(inp.get("limit") or 25),
        )
        for run in res["runs"]:
            self._record("run", run["run_id"], run, listed=("runs", run["run_id"]))
            if run.get("job_id") is not None:
                self._record("job", run["job_id"], {"job_id": run["job_id"]},
                             listed=("runs", run["run_id"]))
            if run.get("agent"):
                self._record("agent", run["agent"], {"agent": run["agent"]},
                             listed=("runs", run["run_id"]))
        self.budget.spend_rows(res["returned"])
        if res.get("truncated"):
            # The QUERY hit its own row cap. The budget knows nothing about
            # this, and a scope the search did not finish reading cannot
            # support a claim about what is not in it.
            self.note_limitation(
                "query_row_cap", tool="list_comparable_runs",
                returned=res["returned"], row_cap=res.get("row_cap"),
            )
        res["window_days"] = days
        return res

    def _tool_inspect_run(self, inp: dict[str, Any]) -> dict[str, Any]:
        run_id = _as_int(inp.get("run_id"))
        if run_id is None:
            return {"error": "run_id is required"}
        detail = database.investigation_run_detail(
            self.account_id, run_id,
            only_user_ids=self.only_user_ids,
            event_cap=int(inp.get("event_limit") or 25),
        )
        if detail is None:
            # Out of scope and nonexistent are the same answer on purpose.
            return {"error": "run not found in this scope"}
        self._record("run", run_id, {
            k: detail[k] for k in
            ("run_id", "title", "agent", "job_id", "outcome", "state",
             "span_count", "error_span_count")
        })
        for ev in detail["events"]:
            self._record("run_event", ev["event_id"], ev,
                         listed=("events", ev["event_id"]))
        for i, span in enumerate(detail["failed_spans"]):
            # Stamp the citable ref onto the row the model is shown, so the
            # reference it may quote and the entry delivery reconciles against
            # are the same string.
            span["ref"] = f"{run_id}.{i}"
            self._record("failed_span", span["ref"], span,
                         listed=("failed_spans", span["ref"]))
        self.budget.spend_events(len(detail["events"]))
        if detail.get("events_truncated"):
            self.note_limitation(
                "query_event_cap", tool="inspect_run", run_id=run_id,
                event_cap=detail.get("event_cap"),
            )
        return detail

    def _tool_compare_outcome_mix(self, inp: dict[str, Any]) -> dict[str, Any]:
        days = max(1, min(45, int(inp.get("days") or 7)))
        job_id = _as_int(inp.get("job_id"))
        agent = _as_str(inp.get("agent"))
        end = self.now
        mid = end - timedelta(days=days)
        start = mid - timedelta(days=days)
        current = database.investigation_outcome_mix(
            self.account_id, only_user_ids=self.only_user_ids,
            start_utc=mid, end_utc=end, workflow_id=job_id, service_name=agent,
        )
        previous = database.investigation_outcome_mix(
            self.account_id, only_user_ids=self.only_user_ids,
            start_utc=start, end_utc=mid, workflow_id=job_id, service_name=agent,
        )
        suffix = f"{days}d:{job_id or ''}:{agent or ''}"
        calc_ids = {}
        for field in ("started", "completed", "abandoned", "still_open"):
            calc_ids[f"current_{field}"] = self._calc(
                f"mix.current.{field}.{suffix}", current[field],
                {"window": "current", "field": field, "days": days,
                 "job_id": job_id, "agent": agent},
            )
            calc_ids[f"previous_{field}"] = self._calc(
                f"mix.previous.{field}.{suffix}", previous[field],
                {"window": "previous", "field": field, "days": days,
                 "job_id": job_id, "agent": agent},
            )
            calc_ids[f"delta_{field}"] = self._calc(
                f"mix.delta.{field}.{suffix}", current[field] - previous[field],
                {"window": "delta", "field": field, "days": days,
                 "job_id": job_id, "agent": agent},
            )
        return {
            "current": current,
            "previous": previous,
            "windows_equal_length": True,
            "window_days": days,
            # The ids a claim must cite to use any of these numbers.
            "calculation_ids": calc_ids,
            "note": (
                "Both windows are the same length and do not overlap. A change "
                "in volume is not by itself a change in how the work is going."
            ),
        }

    def _tool_wait_concentration(self, inp: dict[str, Any]) -> dict[str, Any]:
        res = database.investigation_wait_profile(
            self.account_id, only_user_ids=self.only_user_ids,
            limit=int(inp.get("limit") or 25),
        )
        by_holder: dict[str, int] = {}
        for w in res["waits"]:
            self._record("run", w["run_id"], w, listed=("waits", w["run_id"]))
            holder = w.get("waiting_on") or w.get("state") or "unknown"
            by_holder[holder] = by_holder.get(holder, 0) + 1
        for holder, n in by_holder.items():
            self._calc(
                f"wait.count.{holder}", n,
                {"holder": holder, "measured": "open work waiting now"},
            )
        self.budget.spend_rows(res["returned"])
        if res.get("truncated"):
            self.note_limitation(
                "query_row_cap", tool="wait_concentration",
                returned=res["returned"], row_cap=res.get("row_cap"),
            )
        if not res.get("assignment_resolution_complete", True):
            # The snapshot's own bound, reaching the investigation: some of
            # these rows have no resolved holder, which is not the same as
            # having none. A concentration claim over that is a floor.
            self.note_limitation(
                "assignment_resolution_incomplete", tool="wait_concentration",
            )
        res["by_holder"] = by_holder
        res["calculation_ids"] = {
            h: f"wait.count.{h}" for h in by_holder
        }
        res["note"] = (
            "Current state only. The record does not retain what was waiting "
            "yesterday, so no trend can be drawn from this."
        )
        return res

    def _tool_agent_context(self, inp: dict[str, Any]) -> dict[str, Any]:
        agent = _as_str(inp.get("agent"))
        if not agent:
            return {"error": "agent is required"}
        ctx = database.investigation_agent_context(self.account_id, agent)
        if ctx is None:
            return {"error": "agent not found in this account"}
        self._record("agent", agent, ctx)
        self._record("agent_context", agent, ctx)
        ctx["note"] = (
            "An agent's roster entry is organization-wide. It does not widen "
            "which WORK this analysis covers."
        )
        return ctx

    def _tool_cost_evidence(self, inp: dict[str, Any]) -> dict[str, Any]:
        if not self.financial_visible:
            # The gate is enforced here, at retrieval, so unauthorized spend
            # never reaches the model's context — not merely its output.
            return {
                "error": "financial_surface_not_available",
                "note": "This reader's seat does not include Cost.",
            }
        days = max(1, min(45, int(inp.get("days") or 7)))
        end = self.now
        start = end - timedelta(days=days)
        with database._connect() as conn, database._cursor(conn) as cur:
            window = database._home_cost_window(cur, self.account_id, start, end)
        priced = window["priced_spans"]
        unpriced = window["unpriced_token_spans"]
        denom = priced + unpriced
        spend_id = self._calc(
            f"cost.spend_usd.{days}d", round(window["spend_usd"], 6),
            {"window_days": days, "scope": "organization_wide"},
        )
        coverage_id = self._calc(
            f"cost.coverage.{days}d", round(priced / denom, 4) if denom else None,
            {"measure": "priced_cost_bearing_spans", "denominator": denom},
        )
        self._record("financial_window", f"{days}d", window)
        return {
            "scope": "organization_wide",
            "attributable_to_shown_work": False,
            "window_days": days,
            "spend_usd": round(window["spend_usd"], 6),
            "priced_spans": priced,
            "unpriced_token_spans": unpriced,
            "coverage_ratio": round(priced / denom, 4) if denom else None,
            "calculation_ids": {"spend_usd": spend_id, "coverage_ratio": coverage_id},
            "note": (
                "Organization-wide. Stored span cost hangs off the account and "
                "the agent, not off a work scope, so it cannot be divided by a "
                "narrowed completion count. Unpriced spans are unknown cost, "
                "not zero cost."
            ),
        }


def _as_int(v: Any) -> int | None:
    try:
        return int(v) if v is not None and str(v).strip() != "" else None
    except (TypeError, ValueError):
        return None


def _as_str(v: Any) -> str | None:
    s = str(v).strip() if v is not None else ""
    return s[:200] or None


def _as_outcome(v: Any) -> str | None:
    s = str(v or "").strip().lower()
    return s if s in ("completed", "abandoned", "open") else None


def tools_for(financial_visible: bool) -> list[dict[str, Any]]:
    """The schemas this reader's seat allows.

    Cost is withheld from the LIST as well as from the handler. Offering a tool
    that always refuses would tell a reader without the financial surface that
    there is money here to ask about.
    """
    if financial_visible:
        return list(TOOL_SCHEMAS)
    return [t for t in TOOL_SCHEMAS if t["name"] != "cost_evidence"]
