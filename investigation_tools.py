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
        return {
            "tool_calls": self.calls,
            "tool_call_limit": self.max_calls,
            "rows_retrieved": self.rows,
            "row_limit": self.max_rows,
            "events_retrieved": self.events,
            "event_limit": self.max_events,
            "exhausted": sorted(set(self.exhausted)),
            "complete": not self.exhausted,
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
        # calculation id -> {value, ...}
        self.calculations: dict[str, dict[str, Any]] = {}
        self.transcript: list[dict[str, Any]] = []

    # -- ledger ---------------------------------------------------------
    def _record(self, kind: str, ref: Any, payload: dict[str, Any]) -> None:
        self.evidence[f"{kind}:{ref}"] = payload

    def _calc(self, calc_id: str, value: Any, detail: dict[str, Any]) -> str:
        self.calculations[calc_id] = {"value": value, **detail}
        return calc_id

    # -- dispatch -------------------------------------------------------
    def run(self, name: str, raw_input: dict[str, Any]) -> dict[str, Any]:
        """Execute one allowlisted tool. Never raises into the model loop."""
        if not self.budget.can_call():
            return {
                "error": "budget_exhausted",
                "exhausted": sorted(set(self.budget.exhausted)),
                "note": "No further retrieval is available for this analysis.",
            }
        self.budget.spend_call()
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            return {"error": f"unknown tool {name!r}"}
        try:
            result = handler(raw_input or {})
        except Exception as exc:  # noqa: BLE001 — a tool failure is data, not a crash
            logger.warning("[investigation] tool %s failed: %s", name, exc)
            result = {"error": f"tool {name} failed"}
        result = _clip_deep(result)
        self.transcript.append({"tool": name, "input": raw_input, "ok": "error" not in result})
        return result

    def result_text(self, result: dict[str, Any]) -> str:
        """The tool result as the model sees it, hard-capped."""
        blob = json.dumps(result, default=str)
        if len(blob) > MAX_TOOL_RESULT_CHARS:
            blob = blob[:MAX_TOOL_RESULT_CHARS] + '…","truncated_for_size":true}'
        return blob

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
            self._record("run", run["run_id"], run)
            if run.get("job_id") is not None:
                self._record("job", run["job_id"], {"job_id": run["job_id"]})
            if run.get("agent"):
                self._record("agent", run["agent"], {"agent": run["agent"]})
        self.budget.spend_rows(res["returned"])
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
            self._record("run_event", ev["event_id"], ev)
        for i, span in enumerate(detail["failed_spans"]):
            self._record("failed_span", f"{run_id}.{i}", span)
        self.budget.spend_events(len(detail["events"]))
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
            self._record("run", w["run_id"], w)
            holder = w.get("waiting_on") or w.get("state") or "unknown"
            by_holder[holder] = by_holder.get(holder, 0) + 1
        for holder, n in by_holder.items():
            self._calc(
                f"wait.count.{holder}", n,
                {"holder": holder, "measured": "open work waiting now"},
            )
        self.budget.spend_rows(res["returned"])
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
