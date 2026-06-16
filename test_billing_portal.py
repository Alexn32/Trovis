"""Tests for billing portal + subscription lifecycle webhooks.

Run:
  OVERSEE_DISABLE_PRICING_SYNC=1 python3 test_billing_portal.py
(isolated temp SQLite; Stripe boundary stubbed — no network/keys needed.)
"""
import os
import tempfile

os.environ["OVERSEE_DISABLE_PRICING_SYNC"] = "1"
os.environ.pop("DATABASE_URL", None)
os.environ.pop("ANTHROPIC_API_KEY", None)
# Price ids so plan_for_price / is_configured resolve.
os.environ["STRIPE_PRICE_STARTER"] = "price_starter_m"
os.environ["STRIPE_PRICE_PRO"] = "price_pro_m"
os.environ["STRIPE_PRICE_PRO_ANNUAL"] = "price_pro_a"

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()

import database
database.SQLITE_PATH = _tmp.name

import main
import billing
from fastapi.testclient import TestClient

main._auto_describe = lambda *a, **k: False

failures = []
def check(label, cond):
    print(("  PASS " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


def stub_event(ev):
    billing.parse_webhook_event = lambda payload, sig: ev

def webhook(c, ev):
    stub_event(ev)
    return c.post("/billing/webhook", content=b"{}", headers={"Stripe-Signature": "t=1,v1=x"})


print("\nplan_for_price (reverse lookup):")
check("pro monthly", billing.plan_for_price("price_pro_m") == "pro")
check("pro annual", billing.plan_for_price("price_pro_a") == "pro")
check("starter monthly", billing.plan_for_price("price_starter_m") == "starter")
check("unknown price -> None", billing.plan_for_price("price_zzz") is None)

print("\nstripe_customer_id round-trip:")
print("\nportal endpoint + subscription lifecycle:")
with TestClient(main.app) as c:
    r = c.post("/auth/signup", json={
        "email": "portal@test.com", "password": "supersecret123",
        "name": "Portal Tester", "account_type": "individual", "org_name": "Portal Co",
    })
    assert r.status_code == 201, r.text
    key = r.json()["api_key"]
    acct = r.json()["org"]["id"]
    H = {"X-Trovis-Api-Key": key}

    check("DB customer starts empty", database.get_account_stripe_customer(acct) is None)
    database.set_account_stripe_customer(acct, "cus_RT")
    check("DB customer round-trip", database.get_account_stripe_customer(acct) == "cus_RT")

    # Portal with no customer (a second fresh account) -> 400
    r2 = c.post("/auth/signup", json={
        "email": "nocust@test.com", "password": "supersecret123",
        "name": "No Cust", "account_type": "individual", "org_name": "NC",
    })
    H2 = {"X-Trovis-Api-Key": r2.json()["api_key"]}
    r = c.post("/account/billing-portal", headers=H2)
    check("portal 400 when no Stripe customer", r.status_code == 400)

    # checkout.session.completed -> plan=pro + customer stored
    resp = webhook(c, {"type": "checkout.session.completed", "data": {"object": {
        "payment_status": "paid", "customer": "cus_123",
        "metadata": {"account_id": str(acct), "plan": "pro"},
    }}})
    check("checkout webhook 200", resp.status_code == 200)
    check("plan applied = pro", database.get_account(acct)["plan"] == "pro")
    check("customer stored", database.get_account_stripe_customer(acct) == "cus_123")

    # Portal now returns a URL (stub the Stripe call)
    billing.create_portal_session = lambda *, customer_id, return_url: f"https://billing.stripe.test/p/{customer_id}"
    r = c.post("/account/billing-portal", headers=H)
    check("portal 200 with url", r.status_code == 200 and "cus_123" in r.json()["portal_url"])

    # subscription.updated (portal plan switch) -> map price -> starter? use pro_a -> pro
    resp = webhook(c, {"type": "customer.subscription.updated", "data": {"object": {
        "status": "active", "customer": "cus_123",
        "metadata": {"account_id": str(acct)},
        "items": {"data": [{"price": {"id": "price_starter_m"}}]},
    }}})
    check("sub updated 200", resp.status_code == 200)
    check("plan mapped from price -> starter", database.get_account(acct)["plan"] == "starter")

    # subscription.deleted (cancellation took effect) -> free
    resp = webhook(c, {"type": "customer.subscription.deleted", "data": {"object": {
        "metadata": {"account_id": str(acct)},
    }}})
    check("sub deleted 200", resp.status_code == 200)
    check("plan back to free after cancel", database.get_account(acct)["plan"] == "free")

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    raise SystemExit(1)
print("ALL PASS")
os.unlink(_tmp.name)
