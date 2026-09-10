"""GET /work/items/:id?include=runs — the job pane's technical fold.

This is the door out of Work: someone who needs the machine rather than the
process reads these rows and then leaves for the agent's own page. So the
payload carries exactly what that hop needs — what ran, the ROUTE to its
agent, pass/fail with one line of why, duration, cost — and deliberately not
tokens, attributes, or a span waterfall.

The rule every field here is tested against is the same one: report what the
record holds, and report nothing else. An absent duration is absent. A zero
cost is not a measurement. A run that failed without saying why says only
that it failed — a reason we were never given would be worse than silence,
because the whole point of this pane is that you can trust what is on it.

Run:
  OVERSEE_DISABLE_PRICING_SYNC=1 python3 test_work_item_runs.py
"""
import os, tempfile, time

os.environ.update({"OVERSEE_DISABLE_PRICING_SYNC": "1", "TROVIS_DISABLE_ALERTS": "1",
                   "TROVIS_DISABLE_LOOP_SWEEP": "1", "TROVIS_LOOP_TITLES": "off"})
os.environ.pop("DATABASE_URL", None)
os.environ.pop("ANTHROPIC_API_KEY", None)
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False); _tmp.close()
import database; database.SQLITE_PATH = _tmp.name
import main
from fastapi.testclient import TestClient
main._auto_describe = lambda *a, **k: False

failures = []
def check(label, cond):
    print(("  PASS " if cond else "  FAIL ") + label)
    if not cond: failures.append(label)

def kv(d): return [{"key": k, "value": {"stringValue": str(v)}} for k, v in d.items()]
NS = 10**9
NOW = time.time_ns()
_n = [0]

def sp(name, off, attrs, *, dur_ns=None, status=1, msg=None):
    _n[0] += 1
    start = NOW - off * NS
    span = {
        "traceId": f"{_n[0]:032d}", "spanId": f"{_n[0]:016d}", "name": name, "kind": 1,
        "startTimeUnixNano": str(start),
        "endTimeUnixNano": str(start + (dur_ns if dur_ns is not None else 10**6)),
        "status": {"code": status}, "attributes": kv(attrs),
    }
    if msg is not None:
        span["status"]["message"] = msg
    return span


with TestClient(main.app) as c:
    r = c.post("/auth/signup", json={"email": "r@t.com", "password": "supersecret123",
        "name": "Alex", "account_type": "business", "org_name": "Co"}).json()
    K, T = r["api_key"], r["token"]; H = {"Authorization": f"Bearer {T}"}

    def post(svc, spans, agent_id=None):
        res = {"service.name": svc}
        return c.post("/v1/traces", json={"resourceSpans": [{
            "resource": {"attributes": kv(res)},
            "scopeSpans": [{"spans": spans}]}]}, headers={"X-Trovis-Api-Key": K})

    # One named job whose runs cover every shape the fold has to render:
    # a plain success, a priced + slow success, a failure with a tool and a
    # message, and a failure that said nothing at all.
    post("refunds-agent", [
        sp("message_received", 900, {"trovis.loop.title": "Approve refund #4821",
                                     "trovis.loop.external_id": "j1"}),
        sp("tool_call", 880, {"trovis.loop.external_id": "j1", "trovis.tool.name": "Stripe",
                              "trovis.run.cost_usd": "0.0042"},
           dur_ns=1_400_000_000),
        sp("tool_call", 870, {"trovis.loop.external_id": "j1", "trovis.tool.name": "Stripe"},
           status=2, msg="Card declined: insufficient funds\nstack frame 1\nstack frame 2"),
        sp("agent_run_complete", 860, {"trovis.loop.external_id": "j1"}, status=2),
        # A run that SUCCEEDED but still carried a status message. Agents put
        # all sorts of things there; none of it is a failure reason, and the
        # fold must not present it as one.
        sp("model_call", 850, {"trovis.loop.external_id": "j1"},
           status=1, msg="retried once, then fine"),
    ])

    items = c.get("/work/items?limit=50", headers=H).json()["items"]
    job = next(i for i in items if i["title"] == "Approve refund #4821")

    print("\n--- the fold is not on the spine ---")
    spine = c.get(f"/work/items/{job['id']}", headers=H).json()
    check("the plain detail carries no runs — the spine must not pay for them",
          spine.get("runs") is None)

    body = c.get(f"/work/items/{job['id']}?include=runs", headers=H).json()
    runs = body.get("runs")
    check("?include=runs returns them", isinstance(runs, list) and len(runs) > 0)
    by_name = {}
    for r_ in runs:
        by_name.setdefault(r_["name"], []).append(r_)

    print("\n--- the route out to Fleet ---")
    one = runs[0]
    check("every run carries service_name, the ROUTE",
          all(r_.get("service_name") == "refunds-agent" for r_ in runs))
    check("...and `agent`, the LABEL, kept as its own field",
          all(r_.get("agent") == "refunds-agent" for r_ in runs))
    check("agent_id is present as a key even when the service has no sub-agent",
          all("agent_id" in r_ for r_ in runs))

    print("\n--- pass / fail ---")
    errored = [r_ for r_ in runs if r_["errored"]]
    ok = [r_ for r_ in runs if not r_["errored"]]
    check("the failures are marked", len(errored) == 2)
    check("the successes are not", len(ok) >= 1)

    print("\n--- one line of why, and only when there is one ---")
    declined = next((r_ for r_ in errored if r_.get("error")), None)
    check("a failure's message comes back", declined is not None)
    if declined:
        check("it is the FIRST line only — no stack frames in the pane",
              declined["error"] == "Card declined: insufficient funds")
        check("the tool that failed is named separately, for the one-line join",
              declined.get("tool") == "stripe")
    silent = next((r_ for r_ in errored if r_["name"] == "agent_run_complete"), None)
    check("a failure with no message reports no reason rather than inventing one",
          silent is not None and silent.get("error") is None)
    check("a run that SUCCEEDED never carries an error string",
          all(r_.get("error") is None for r_ in ok))

    print("\n--- duration ---")
    slow = next((r_ for r_ in runs if (r_.get("duration_ms") or 0) >= 1000), None)
    check("a real duration is reported in ms", slow is not None and slow["duration_ms"] == 1400)
    check("every run has the key, so the client never has to guess",
          all("duration_ms" in r_ for r_ in runs))

    print("\n--- cost: a real one, or none at all ---")
    priced = [r_ for r_ in runs if r_.get("cost_usd") is not None]
    check("the priced run reports its cost", len(priced) == 1 and priced[0]["cost_usd"] > 0)
    check("the unpriced runs report None, NOT 0.0 — 'not told' is not 'free'",
          all(r_.get("cost_usd") is None for r_ in runs if r_ is not priced[0])
          if priced else False)

    print("\n--- what the fold deliberately does NOT carry ---")
    # Someone who needs this depth is on the agent's page; this fold's job is
    # to hand them off, not to reproduce it.
    banned = {"input_tokens", "output_tokens", "total_tokens", "attributes",
              "resource_attributes", "span_id", "trace_id", "status_message"}
    leaked = sorted(banned & set().union(*(set(r_) for r_ in runs)))
    check(f"no tokens, attributes or raw span ids (leaked: {leaked})", not leaked)

    print("\n--- bounded ---")
    check("the page is short by construction", len(runs) <= 25)
    check("a run always has a name, never an empty cell",
          all(str(r_.get("name") or "").strip() for r_ in runs))

    print("\n--- scoping ---")
    r2 = c.post("/auth/signup", json={"email": "other@t.com", "password": "supersecret123",
        "name": "Sam", "account_type": "business", "org_name": "Other"}).json()
    H2 = {"Authorization": f"Bearer {r2['token']}"}
    other = c.get(f"/work/items/{job['id']}?include=runs", headers=H2)
    check("another account cannot read this job's runs", other.status_code == 404)

print()
if failures:
    print(f"FAILED ({len(failures)}):")
    for f in failures: print("  - " + f)
    raise SystemExit(1)
print("All work-item run checks passed.")
os.unlink(_tmp.name)
