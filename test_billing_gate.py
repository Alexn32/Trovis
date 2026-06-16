"""Tests for the Stripe payment gate on plan changes.

Security invariant: a PAID tier (starter/pro/enterprise) can only be reached
through a verified Stripe `checkout.session.completed` webhook. The direct
PUT /account/plan endpoint must NEVER apply a paid tier — when billing isn't
configured it fails closed (503); when it is configured it only returns a
checkout URL. Downgrading to 'free' needs no payment and applies directly.

Run:
  OVERSEE_DISABLE_PRICING_SYNC=1 python3 test_billing_gate.py
(isolated temp SQLite DB; never touches the dev/prod DB)
"""
import os
import tempfile

os.environ["OVERSEE_DISABLE_PRICING_SYNC"] = "1"
os.environ.pop("DATABASE_URL", None)
os.environ.pop("ANTHROPIC_API_KEY", None)
# Ensure billing reads as UNCONFIGURED regardless of the host environment, so
# the fail-closed assertions are deterministic.
for _k in (
    "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET",
    "STRIPE_PRICE_STARTER", "STRIPE_PRICE_PRO", "STRIPE_PRICE_ENTERPRISE",
):
    os.environ.pop(_k, None)

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()

import database
database.SQLITE_PATH = _tmp.name

import billing
import main
from fastapi.testclient import TestClient

main._auto_describe = lambda *a, **k: False

failures = []
def check(label, cond):
    print(("  PASS " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


def otlp_attrs(d):
    return [{"key": k, "value": {"stringValue": str(v)}} for k, v in d.items()]


def span(trace, sid, name, start, attrs):
    return {
        "traceId": trace, "spanId": sid, "name": name, "kind": 1,
        "startTimeUnixNano": str(start), "endTimeUnixNano": str(start + 5_000_000),
        "status": {"code": 1}, "attributes": otlp_attrs(attrs),
    }


def post_spans(client, key, service, spans):
    payload = {"resourceSpans": [{
        "resource": {"attributes": otlp_attrs({
            "service.name": service, "trovis.plugin.version": "1.0.0",
        })},
        "scopeSpans": [{"spans": spans}],
    }]}
    return client.post("/v1/traces", json=payload, headers={"X-Trovis-Api-Key": key})


B = 1_900_000_000_000_000_000  # well past the first-seen floor (2025-01-01)
AGENTS = [f"agent-{i:02d}" for i in range(1, 7)]  # 6 agents → 1 locked on free


def current_plan(account_id):
    return database.get_account(account_id)["plan"]


with TestClient(main.app) as c:
    r = c.post("/auth/signup", json={
        "email": "billing@test.com", "password": "supersecret123",
        "name": "Billing Tester", "account_type": "individual", "org_name": "Bill Co",
    })
    assert r.status_code == 201, r.text
    key = r.json()["api_key"]
    H = {"X-Trovis-Api-Key": key}
    account_id = c.get("/auth/me", headers=H).json()["org"]["id"]

    # Six agents so the free plan (limit 5) leaves agent-06 view-locked. Lets us
    # prove the webhook still unlocks instantly once a paid plan lands.
    for i, svc in enumerate(AGENTS, start=1):
        t = B + i * 10_000_000_000
        trace = (f"{i:02d}".encode().hex() * 16)[:32]
        resp = post_spans(c, key, svc, [
            span(trace, "b" * 16, "agent_registration", t, {
                "trovis.event.type": "agent_registration", "trovis.agent.id": "main",
                "trovis.agent.identity": f"{svc} — worker",
            }),
        ])
        assert resp.status_code == 200, resp.text

    check("baseline: account starts on free", current_plan(account_id) == "free")
    u = c.get("/account/usage", headers=H).json()
    check("baseline: 6 agents, 1 locked on free", u["locked_count"] == 1)

    # 1. THE HOLE IS CLOSED — self-upgrade to enterprise is refused (billing
    #    unconfigured → fail closed) and the plan does NOT change.
    r = c.put("/account/plan", json={"plan": "enterprise"}, headers=H)
    check("upgrade to enterprise refused when billing unconfigured (503)",
          r.status_code == 503)
    check("plan unchanged after refused enterprise upgrade",
          current_plan(account_id) == "free")

    # 2. Same for every other paid tier.
    for tier in ("starter", "pro"):
        rr = c.put("/account/plan", json={"plan": tier}, headers=H)
        check(f"upgrade to {tier} refused when billing unconfigured (503)",
              rr.status_code == 503 and current_plan(account_id) == "free")

    # 3. Unknown plan → 400 (validated before any billing logic).
    r = c.put("/account/plan", json={"plan": "platinum"}, headers=H)
    check("unknown plan rejected (400)", r.status_code == 400)

    # 4. No-op (already free) applies directly, returns usage.
    r = c.put("/account/plan", json={"plan": "free"}, headers=H)
    body = r.json()
    check("re-requesting free → applied",
          r.status_code == 200 and body["status"] == "applied"
          and body["plan"] == "free" and body["usage"]["plan"] == "free")

    # 5. Downgrade to free needs no payment. Put the account on 'pro' (as the
    #    webhook would) then downgrade via the endpoint directly.
    database.set_account_plan(account_id, "pro")
    r = c.put("/account/plan", json={"plan": "free"}, headers=H)
    check("downgrade pro → free applied directly",
          r.status_code == 200 and r.json()["status"] == "applied"
          and current_plan(account_id) == "free")

    # 6. With billing CONFIGURED, the endpoint returns a checkout URL and STILL
    #    does not change the plan (monkeypatch the Stripe boundary).
    billing_calls = {}
    def _fake_checkout(*, account_id, plan, success_url, cancel_url):
        billing_calls["args"] = (account_id, plan, success_url, cancel_url)
        return "https://checkout.stripe.test/c/session_abc123"
    _real_is_configured = billing.is_configured
    _real_create = billing.create_checkout_session
    billing.is_configured = lambda plan=None: True
    billing.create_checkout_session = _fake_checkout
    try:
        r = c.put("/account/plan", json={"plan": "pro"}, headers=H)
        body = r.json()
        check("configured upgrade → checkout_required + URL",
              r.status_code == 200 and body["status"] == "checkout_required"
              and body["checkout_url"] == "https://checkout.stripe.test/c/session_abc123")
        check("checkout carries the right account + plan",
              billing_calls.get("args", (None, None))[:2] == (account_id, "pro"))
        check("endpoint did NOT apply the paid plan (still free)",
              current_plan(account_id) == "free")
    finally:
        billing.is_configured = _real_is_configured
        billing.create_checkout_session = _real_create

    # 7. The webhook is the sole writer of paid tiers. Verify it (a) is reachable
    #    without a Trovis credential and (b) applies the plan + unlocks instantly.
    #    Monkeypatch signature verification to return a synthetic paid event.
    _real_parse = billing.parse_webhook_event
    def _fake_event(payload, sig):
        return {
            "type": "checkout.session.completed",
            "data": {"object": {
                "payment_status": "paid",
                "client_reference_id": str(account_id),
                "metadata": {"account_id": str(account_id), "plan": "pro"},
            }},
        }
    billing.parse_webhook_event = _fake_event
    try:
        # No X-Trovis-Api-Key header — proves /billing/webhook is an open path.
        wr = c.post("/billing/webhook", content=b"{}",
                    headers={"Stripe-Signature": "t=1,v1=deadbeef"})
        check("webhook accepted without a Trovis credential (200)",
              wr.status_code == 200 and wr.json().get("received") is True)
        check("webhook applied paid plan (free → pro)",
              current_plan(account_id) == "pro")
        u2 = c.get("/account/usage", headers=H).json()
        check("paid plan unlocked the view-locked agent instantly",
              u2["plan"] == "pro" and u2["locked_count"] == 0)
    finally:
        billing.parse_webhook_event = _real_parse

    # 8. A webhook with a bad signature is rejected and changes nothing.
    database.set_account_plan(account_id, "free")
    def _raise_bad(payload, sig):
        raise billing.BillingError("invalid Stripe webhook signature")
    billing.parse_webhook_event = _raise_bad
    try:
        wr = c.post("/billing/webhook", content=b"{}",
                    headers={"Stripe-Signature": "bogus"})
        check("bad-signature webhook rejected (400)", wr.status_code == 400)
        check("bad-signature webhook changed nothing (still free)",
              current_plan(account_id) == "free")
    finally:
        billing.parse_webhook_event = _real_parse

print()
if failures:
    print(f"FAILED: {len(failures)} check(s):")
    for f in failures:
        print("  - " + f)
    raise SystemExit(1)
print("ALL CHECKS PASSED")
