"""Proactive alert sweep for Trovis.

This is the background job that makes the product's core promise real —
"you find out from Trovis, not from the damage." Nobody has to be looking at
the dashboard: every ~15 minutes the sweep evaluates each account's fleet
against a handful of rules and pushes an alert the moment something trips.

Rules (each individually toggleable per account):
  - drift   : an agent's observed behavior left its declared job (Claude verdict)
  - budget  : month-to-date spend crossed the warn % or 100% of the budget
  - loop    : a single operation repeated past a threshold in a short window
              (runaway / non-terminating loop)
  - error   : an agent produced a burst of failures in a short window

Delivery channels: email (Resend, to the account owner) + optional Slack
incoming-webhook and/or a generic webhook URL.

Everything is FAIL-SOFT: one account's failure, one rule's failure, or one
channel's failure is logged and swallowed so the sweep always finishes and can
never take the API down. Dedup via database.alert_log means a standing
condition (an agent still drifting) alerts once per cooldown, not every sweep.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from datetime import datetime, timezone
from time import time as _now
from typing import Any

import database
import describer
import email_send

logger = logging.getLogger("trovis.alerts")

# How long a standing condition stays quiet after firing once (per distinct
# state_key). A genuinely new state (budget 80→100, a different drift headline)
# re-alerts immediately because its state_key differs.
_COOLDOWN_S = 24 * 60 * 60
# Look-back window for the loop + error rules.
_WINDOW_S = 30 * 60
# Reuse a cached drift verdict within this age; otherwise compute a fresh one.
_DRIFT_TTL_S = 6 * 60 * 60
# Cap on fresh (Claude-backed) drift computations per sweep per account, so a
# large fleet can't trigger a huge model bill in one pass. Agents beyond the cap
# fall back to their cached verdict and get re-evaluated on a later sweep.
_MAX_FRESH_DRIFT = 25
# Recent spans pulled per agent for the loop/error rules.
_SPAN_SCAN_LIMIT = 300


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


def _post_json(url: str, payload: dict[str, Any], timeout: float = 6.0) -> bool:
    """Fail-soft JSON POST (Slack / generic webhook). Never raises."""
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}, method="POST"
        )
        urllib.request.urlopen(req, timeout=timeout).close()
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("[alerts] webhook POST failed (%s): %s", url[:40], e)
        return False


def _deliver(settings: dict[str, Any], account: dict[str, Any], alert: dict[str, Any]) -> None:
    """Send one alert over every enabled channel. Fail-soft per channel."""
    title = alert["title"]
    body = alert["body"]
    # Email → the account owner.
    if settings.get("email_enabled") and account.get("email"):
        try:
            email_send.send_email(account["email"], f"Trovis alert — {title}", _email_html(account, alert))
        except Exception as e:  # noqa: BLE001
            logger.warning("[alerts] email send failed: %s", e)
    # Slack incoming webhook expects {"text": ...}.
    slack = settings.get("slack_webhook_url")
    if slack:
        _post_json(slack, {"text": f":rotating_light: *Trovis alert — {title}*\n{body}"})
    # Generic webhook gets the structured payload.
    hook = settings.get("webhook_url")
    if hook:
        _post_json(hook, {
            "source": "trovis",
            "rule": alert["rule"],
            "title": title,
            "body": body,
            "subject": alert.get("subject_key", ""),
            "account_id": account.get("id"),
        })


def _email_html(account: dict[str, Any], alert: dict[str, Any]) -> str:
    """Minimal branded HTML for the alert email (inline styles — email clients
    strip <style>)."""
    org = account.get("name") or "your fleet"
    return (
        f'<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;'
        f'background:#F5F1EB;padding:24px;color:#2C2418">'
        f'<div style="max-width:520px;margin:0 auto;background:#FBF8F3;'
        f'border:1px solid #DDD7CE;border-radius:12px;padding:24px">'
        f'<div style="font-weight:700;color:#5A7B7B;font-size:15px;margin-bottom:4px">Trovis</div>'
        f'<h2 style="font-size:18px;margin:8px 0 4px;color:#2C2418">{_esc(alert["title"])}</h2>'
        f'<p style="font-size:14px;line-height:1.6;color:#4A4137;margin:8px 0 16px">{_esc(alert["body"])}</p>'
        f'<p style="font-size:12px;color:#8C8378;margin:0">Detected across {_esc(org)}. '
        f'Manage alerts in Trovis → Settings.</p>'
        f'</div></div>'
    )


def _esc(s: Any) -> str:
    import html as _html
    return _html.escape(str(s or ""), quote=True)


# ---------------------------------------------------------------------------
# Rule evaluators — each returns a list of alert dicts:
#   {rule, subject_key, state_key, title, body}
# ---------------------------------------------------------------------------


def _agent_label(group: dict[str, Any]) -> str:
    return group.get("display_name") or group.get("service_name") or "agent"


def _eval_drift(account_id: int, groups: list[dict[str, Any]], fresh_budget: int) -> tuple[list[dict], int]:
    """Drift verdicts for each unlocked agent with a declared identity. Uses the
    6h-cached verdict when fresh; otherwise computes a bounded number of fresh
    ones. Emits an alert only for a 'drift' status."""
    out: list[dict[str, Any]] = []
    for g in groups:
        if g.get("locked"):
            continue
        svc = g.get("service_name")
        if not svc:
            continue
        for agent in (g.get("agents") or [{"agent_id": "main"}]):
            agent_id = agent.get("agent_id", "main")
            cached = database.get_insight(
                account_id=account_id, service_name=svc, agent_id=agent_id,
                kind="drift", max_age_seconds=_DRIFT_TTL_S,
            )
            report = cached.get("data") if cached else None
            if report is None and fresh_budget > 0:
                reg = database.get_latest_registration(svc, account_id=account_id, agent_id=agent_id)
                if not reg:
                    continue  # no declared identity → nothing to compare against
                fresh_budget -= 1
                try:
                    spans = database.get_agent_spans(svc, limit=60, account_id=account_id, agent_id=agent_id)
                    outputs = database.get_agent_outputs(svc, account_id=account_id, limit=15, agent_id=agent_id)
                    report = describer.detect_drift(reg, spans, outputs)
                    if report.get("status") != "unknown":
                        database.save_insight(
                            account_id=account_id, service_name=svc,
                            agent_id=agent_id, kind="drift", data=report,
                        )
                except Exception as e:  # noqa: BLE001
                    logger.warning("[alerts] drift compute failed for %s: %s", svc, e)
                    report = None
            if not report or report.get("status") != "drift":
                continue
            headline = report.get("headline", "Stepped outside its declared job.")
            label = _agent_label(g)
            out.append({
                "rule": "drift",
                "subject_key": f"{svc}:{agent_id}",
                # New headline → new state → re-alert; same standing drift → deduped.
                "state_key": _short(headline),
                "title": f"{label} drifted from its job",
                "body": headline,
            })
    return out, fresh_budget


def _eval_budget(account_id: int, warn_pct: int) -> list[dict[str, Any]]:
    """Month-to-date spend vs the account's monthly budget. Fires once at the
    warn threshold and once at 100%."""
    budget = database.get_account_budget(account_id)
    if not budget or budget <= 0:
        return []
    prefix = datetime.now(timezone.utc).strftime("%Y-%m-")
    try:
        days = database.get_fleet_daily_cost(account_id, days=31)
    except Exception:  # noqa: BLE001
        return []
    mtd = round(sum(r["cost"] for r in days if str(r.get("date", "")).startswith(prefix)), 2)
    pct = (mtd / budget) * 100 if budget else 0
    if pct >= 100:
        state, title = "100", "Monthly budget exceeded"
    elif pct >= warn_pct:
        state, title = f"warn{warn_pct}", f"Monthly budget {int(pct)}% used"
    else:
        return []
    return [{
        "rule": "budget",
        "subject_key": "account",
        "state_key": state,
        "title": title,
        "body": f"Month-to-date spend is ${mtd:,.2f} of your ${budget:,.2f} budget ({int(pct)}%).",
    }]


def _recent_ops(account_id: int, svc: str, agent_id: str, since_ns: int) -> tuple[dict[str, int], int]:
    """Count operations and errors in the recent window for one agent from its
    latest spans. Returns ({op_name: count}, error_count)."""
    counts: dict[str, int] = {}
    errors = 0
    try:
        spans = database.get_agent_spans(svc, limit=_SPAN_SCAN_LIMIT, account_id=account_id, agent_id=agent_id)
    except Exception:  # noqa: BLE001
        return counts, 0
    for s in spans:
        if (s.get("start_time_unix") or 0) < since_ns:
            continue
        name = s.get("span_name") or "operation"
        counts[name] = counts.get(name, 0) + 1
        if s.get("status_code") == 2:  # OTEL ERROR
            errors += 1
    return counts, errors


def _eval_loop_and_errors(
    account_id: int, groups: list[dict[str, Any]], loop_threshold: int,
    do_loop: bool, do_error: bool,
) -> list[dict[str, Any]]:
    """Runaway-loop + error-burst rules, sharing one recent-span scan per agent."""
    if not (do_loop or do_error):
        return []
    since_ns = int((_now() - _WINDOW_S) * 1_000_000_000)
    out: list[dict[str, Any]] = []
    for g in groups:
        if g.get("locked"):
            continue
        svc = g.get("service_name")
        if not svc:
            continue
        label = _agent_label(g)
        for agent in (g.get("agents") or [{"agent_id": "main"}]):
            agent_id = agent.get("agent_id", "main")
            counts, errors = _recent_ops(account_id, svc, agent_id, since_ns)
            if do_loop and counts:
                op, n = max(counts.items(), key=lambda kv: kv[1])
                if n >= loop_threshold:
                    out.append({
                        "rule": "loop",
                        "subject_key": f"{svc}:{agent_id}",
                        "state_key": op,
                        "title": f"{label} may be stuck in a loop",
                        "body": (
                            f"Ran '{op}' {n} times in the last "
                            f"{_WINDOW_S // 60} minutes — a possible runaway or "
                            f"non-terminating loop."
                        ),
                    })
            if do_error and errors >= 3:
                out.append({
                    "rule": "error",
                    "subject_key": f"{svc}:{agent_id}",
                    "state_key": f"errs{_WINDOW_S // 60}m",
                    "title": f"{label} is failing repeatedly",
                    "body": (
                        f"{errors} failed operations in the last "
                        f"{_WINDOW_S // 60} minutes."
                    ),
                })
    return out


def _short(s: str) -> str:
    """Short stable key for a headline (dedup by content, not exact string)."""
    import hashlib
    return hashlib.sha256((s or "").strip().lower().encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Sweep
# ---------------------------------------------------------------------------


def run_sweep_for_account(account_id: int) -> int:
    """Evaluate one account, deliver + dedup any tripped alerts. Returns the
    number of alerts sent. Fail-soft — never raises."""
    try:
        settings = database.get_alert_settings(account_id)
        account = database.get_account(account_id)
        if not account:
            return 0
        # Nothing to deliver over → skip the (potentially Claude-backed) work.
        if not (settings.get("email_enabled") and account.get("email")) \
                and not settings.get("slack_webhook_url") \
                and not settings.get("webhook_url"):
            return 0

        groups = database.get_agents(account_id=account_id)

        candidates: list[dict[str, Any]] = []
        if settings.get("rule_drift"):
            drift_alerts, _ = _eval_drift(account_id, groups, _MAX_FRESH_DRIFT)
            candidates += drift_alerts
        if settings.get("rule_budget"):
            candidates += _eval_budget(account_id, int(settings.get("budget_warn_pct", 80)))
        candidates += _eval_loop_and_errors(
            account_id, groups, int(settings.get("loop_threshold", 50)),
            do_loop=bool(settings.get("rule_loop")),
            do_error=bool(settings.get("rule_error")),
        )

        sent = 0
        for a in candidates:
            if database.was_alerted(
                account_id, a["rule"], a["subject_key"], a["state_key"], _COOLDOWN_S
            ):
                continue
            _deliver(settings, account, a)
            database.record_alert(account_id, a["rule"], a["subject_key"], a["state_key"])
            sent += 1
        return sent
    except Exception as e:  # noqa: BLE001
        logger.warning("[alerts] sweep failed for account %s: %s", account_id, e)
        return 0


def run_sweep() -> dict[str, Any]:
    """Sweep every account. Returns a summary for the log line."""
    accounts = 0
    sent = 0
    for account_id in database.list_account_ids():
        accounts += 1
        sent += run_sweep_for_account(account_id)
    return {"accounts": accounts, "alerts_sent": sent}


def send_test_alert(account_id: int) -> dict[str, Any]:
    """Deliver a sample alert over the account's configured channels (no dedup).
    Powers the Settings "send test" button so an operator can confirm delivery."""
    settings = database.get_alert_settings(account_id)
    account = database.get_account(account_id)
    if not account:
        return {"delivered": False, "reason": "account not found"}
    channels = []
    if settings.get("email_enabled") and account.get("email"):
        channels.append(f"email ({account['email']})")
    if settings.get("slack_webhook_url"):
        channels.append("Slack")
    if settings.get("webhook_url"):
        channels.append("webhook")
    if not channels:
        return {"delivered": False, "reason": "no channels enabled"}
    _deliver(settings, account, {
        "rule": "test",
        "subject_key": "test",
        "state_key": "test",
        "title": "Test alert",
        "body": "This is a test alert from Trovis. If you're seeing this, your alert channels are working.",
    })
    return {"delivered": True, "channels": channels}
