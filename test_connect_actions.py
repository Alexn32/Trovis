"""Door test: ChatGPT Actions, over the real OAuth flow.

This is the first test the OAuth server has ever had. The full path, no
shortcuts — no hand-minted session tokens, no calling database.create_session
directly. Every step goes through the endpoint ChatGPT actually calls:

  GET  /oauth/authorize          -> consent page (HTML form)
  POST /oauth/authorize/submit   -> validates credentials, 302 with ?code=
  POST /oauth/token              -> code + client_secret -> access_token
  POST /actions/connect          -> registers the ChatGPT agent
  POST /actions/log              -> logs an activity span
  GET  /agents                   -> the agent is visible with the spans

Also pins the parts of the flow that are security-load-bearing: the
redirect_uri allowlist (on both the GET and the directly-POSTable submit
endpoint), the confidential-client secret check, and single-use codes.

Run:
  TROVIS_DISABLE_PRICING_SYNC=1 python3 test_connect_actions.py
(isolated temp SQLite DB; never touches the dev/prod DB)
"""
import os
import tempfile
import urllib.parse

os.environ["OVERSEE_DISABLE_PRICING_SYNC"] = "1"
os.environ["TROVIS_DISABLE_PRICING_SYNC"] = "1"
os.environ["TROVIS_DISABLE_ALERTS"] = "1"
os.environ["TROVIS_DISABLE_LOOP_SWEEP"] = "1"
os.environ.pop("DATABASE_URL", None)
os.environ.pop("ANTHROPIC_API_KEY", None)

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()

import database
database.SQLITE_PATH = _tmp.name

import describer
import main
from fastapi.testclient import TestClient

main._auto_describe = lambda *a, **k: False

# Stub the Claude boundary. GET /agents/{name}/summary regenerates a missing
# description on read, so a test that merely READS an agent will otherwise
# make a live Anthropic call — not hermetic, and it would burn a real key.
describer.describe_agent = lambda service_name, account_id=None, agent_id=None: {
    "service_name": service_name,
    "description": "Stubbed description.",
    "description_long": "Stubbed long description for a test agent.",
    "span_count_analyzed": 1,
    "source": "telemetry_only",
}
describer.record_summary = lambda user, agent: "Stubbed record summary"

failures = []


def check(label, cond, detail=""):
    print(("  PASS " if cond else "  FAIL ") + label)
    if detail:
        print(f"        {detail}")
    if not cond:
        failures.append(label)


EMAIL = "actions@test.com"
PASSWORD = "supersecret123"
# The real ChatGPT callback host — the allowlist default in main.py.
REDIRECT = "https://chatgpt.com/aip/g-abc123/oauth/callback"
CLIENT_ID = main._OAUTH_CLIENT_ID
CLIENT_SECRET = main._OAUTH_CLIENT_SECRET
STATE = "opaque-state-value"


with TestClient(main.app) as c:
    r = c.post("/auth/signup", json={
        "email": EMAIL, "password": PASSWORD,
        "name": "Actions Tester", "account_type": "individual", "org_name": "Actions Co",
    })
    assert r.status_code == 201, r.text
    api_key = r.json()["api_key"]
    KH = {"X-Trovis-Api-Key": api_key}
    account_id = database.validate_api_key(api_key)["account_id"]

    print("\n[1] GET /oauth/authorize renders the consent form")
    q = urllib.parse.urlencode({
        "client_id": CLIENT_ID, "redirect_uri": REDIRECT,
        "response_type": "code", "scope": "", "state": STATE,
    })
    r = c.get(f"/oauth/authorize?{q}")
    check("consent page returns 200", r.status_code == 200, f"got {r.status_code}")
    html = r.text
    check("form posts to /oauth/authorize/submit",
          'action="/oauth/authorize/submit"' in html)
    check("redirect_uri is carried through as a hidden field",
          f'name="redirect_uri" value="{REDIRECT}"' in html)
    check("state is carried through as a hidden field",
          f'name="state" value="{STATE}"' in html)

    print("\n[2] The redirect_uri allowlist is enforced before the password form")
    evil = "https://attacker.example.com/steal"
    r = c.get(f"/oauth/authorize?{urllib.parse.urlencode({'client_id': CLIENT_ID, 'redirect_uri': evil})}")
    check("off-allowlist redirect_uri is refused a consent page",
          r.status_code == 400, f"got {r.status_code}")
    check("no password field is rendered for an off-allowlist client",
          'type="password"' not in r.text)
    # /submit is directly POSTable, so it must re-check rather than trusting
    # that the GET gate ran.
    r = c.post("/oauth/authorize/submit", data={
        "email": EMAIL, "password": PASSWORD,
        "client_id": CLIENT_ID, "redirect_uri": evil, "state": STATE,
    }, follow_redirects=False)
    check("POSTing /submit directly with an off-allowlist redirect_uri is refused",
          r.status_code == 400, f"got {r.status_code}")

    print("\n[3] Bad credentials don't produce a code")
    r = c.post("/oauth/authorize/submit", data={
        "email": EMAIL, "password": "wrong-password",
        "client_id": CLIENT_ID, "redirect_uri": REDIRECT, "state": STATE,
    }, follow_redirects=False)
    check("wrong password is rejected", r.status_code == 401, f"got {r.status_code}")
    check("no redirect is issued on bad credentials", "location" not in r.headers)

    print("\n[4] The consent form issues an auth code")
    r = c.post("/oauth/authorize/submit", data={
        "email": EMAIL, "password": PASSWORD,
        "client_id": CLIENT_ID, "redirect_uri": REDIRECT, "scope": "", "state": STATE,
    }, follow_redirects=False)
    check("submit redirects (302)", r.status_code == 302, f"got {r.status_code}")
    location = r.headers.get("location", "")
    check("redirect goes to the requested callback", location.startswith(REDIRECT),
          f"location={location[:90]}")
    parsed = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
    code = (parsed.get("code") or [""])[0]
    check("a code is present in the callback URL", bool(code), f"code={code[:12]}…")
    check("state is echoed back unchanged", (parsed.get("state") or [""])[0] == STATE)

    print("\n[5] /oauth/token rejects a caller without the client secret")
    r = c.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code,
        "client_id": CLIENT_ID, "client_secret": "wrong-secret",
        "redirect_uri": REDIRECT,
    })
    check("wrong client_secret is rejected 401", r.status_code == 401, f"got {r.status_code}")
    r = c.post("/oauth/token", data={
        "grant_type": "client_credentials", "code": code,
        "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, "redirect_uri": REDIRECT,
    })
    check("unsupported grant_type is rejected 400", r.status_code == 400, f"got {r.status_code}")

    print("\n[6] /oauth/token exchanges the code for an access token")
    r = c.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code,
        "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
        "redirect_uri": REDIRECT,
    })
    check("token exchange returns 200", r.status_code == 200, f"got {r.status_code}: {r.text[:160]}")
    tok = r.json() if r.status_code == 200 else {}
    access_token = tok.get("access_token", "")
    check("access_token present", bool(access_token))
    check("token_type is bearer", tok.get("token_type") == "bearer", f"{tok.get('token_type')!r}")
    check("expires_in is a positive int", isinstance(tok.get("expires_in"), int)
          and tok["expires_in"] > 0, f"expires_in={tok.get('expires_in')}")

    print("\n[7] The auth code is single-use")
    r2 = c.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code,
        "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
        "redirect_uri": REDIRECT,
    })
    check("replaying the same code is rejected 400", r2.status_code == 400, f"got {r2.status_code}")

    AH = {"Authorization": f"Bearer {access_token}"}

    print("\n[8] /actions/* rejects a bad bearer token")
    r = c.post("/actions/connect", json={"agent_name": "X"},
               headers={"Authorization": "Bearer not-a-real-token"})
    check("bogus bearer is rejected 401", r.status_code == 401, f"got {r.status_code}")
    r = c.post("/actions/connect", json={"agent_name": "X"})
    check("missing bearer is rejected 401", r.status_code == 401, f"got {r.status_code}")

    print("\n[9] /actions/connect registers the ChatGPT agent")
    r = c.post("/actions/connect", json={
        "agent_name": "Support Copilot",
        "agent_role": "Customer support triage",
        "agent_instructions": "Answer billing questions and escalate refunds.",
    }, headers=AH)
    check("connect returns 200", r.status_code == 200, f"got {r.status_code}: {r.text[:160]}")
    body = r.json() if r.status_code == 200 else {}
    check("connect confirms the agent name",
          body.get("agent_name") == "Support Copilot", f"body={body}")

    print("\n[10] /actions/log records an activity span")
    r = c.post("/actions/log", json={
        "step_name": "resolve_ticket",
        "description": "Looked up invoice 4417 and issued a partial refund.",
        "tools_used": "billing_lookup,refund",
        "output_summary": "Refund issued",
        "duration_seconds": 12,
    }, headers=AH)
    check("log returns 200", r.status_code == 200, f"got {r.status_code}: {r.text[:160]}")
    check("log echoes the step name", (r.json() or {}).get("step_name") == "resolve_ticket")

    print("\n[11] /actions/status reflects the connected agent")
    r = c.get("/actions/status", headers=AH)
    check("status returns 200", r.status_code == 200, f"got {r.status_code}")
    check("status names the connected agent",
          (r.json() or {}).get("agent_name") == "Support Copilot", f"body={r.json()}")

    print("\n[12] THE POINT — the agent and its spans are visible in the fleet")
    agents = c.get("/agents", headers=KH).json()
    names = [a["service_name"] for a in agents]
    check("'Support Copilot' is in GET /agents", "Support Copilot" in names, f"agents={names}")

    rows = c.get("/agents/Support Copilot/spans", headers=KH).json()
    span_names = {s["span_name"] for s in rows}
    check("both the registration and the activity span landed",
          {"agent_registration", "resolve_ticket"} <= span_names, f"spans={sorted(span_names)}")

    print("\n[13] The spans are stamped trovis.platform=chatgpt")
    platforms = {s.get("resource_attributes", {}).get("trovis.platform") for s in rows}
    check("every Actions span carries trovis.platform=chatgpt",
          platforms == {"chatgpt"}, f"platforms={platforms}")

    activity = next((s for s in rows if s["span_name"] == "resolve_ticket"), {})
    attrs = activity.get("attributes", {})
    check("the activity span kept its description",
          "invoice 4417" in (attrs.get("trovis.step.description") or ""),
          f"description={attrs.get('trovis.step.description')!r}")
    check("the activity span kept its tools list",
          attrs.get("trovis.tools.used") == "billing_lookup,refund",
          f"tools={attrs.get('trovis.tools.used')!r}")

    print("\n[14] The registration reached the registration table too")
    reg = database.get_latest_registration("Support Copilot", account_id=account_id,
                                           agent_id="main")
    check("a registration row exists", reg is not None)
    if reg:
        check("instructions were stored as the agent's soul",
              "billing questions" in (reg.get("soul") or ""),
              f"soul={(reg.get('soul') or '')[:60]!r}")

    print("\n[15] KNOWN DEFECT — the agent name does not survive a restart")
    # main._action_agents is a module-level in-memory dict. /actions/connect
    # writes the agent name into it; /actions/log reads it back, falling back
    # to the literal "ChatGPT Agent" on a miss. The registration itself IS
    # durable (agent_registrations, asserted above) — only this lookup is not.
    #
    # So after a process restart, or on any second instance behind the load
    # balancer, the SAME ChatGPT agent starts logging under a different
    # service_name and splits into two agents in the fleet. This is a
    # structural fix (persist the mapping, or resolve it from the
    # registration table), deliberately NOT made in this test-only commit.
    #
    # This block pins the CURRENT behavior so the defect is visible on every
    # push. When it is fixed, this check flips to failing — at which point
    # replace it with an assertion that the name survives.
    main._action_agents.clear()   # stand-in for a restart / a second instance
    r = c.post("/actions/log", json={"step_name": "after_restart"}, headers=AH)
    check("post-restart log still returns 200", r.status_code == 200, f"got {r.status_code}")
    names_after = [a["service_name"] for a in c.get("/agents", headers=KH).json()]
    check("DEFECT PINNED: a second 'ChatGPT Agent' appears after a restart",
          "ChatGPT Agent" in names_after,
          f"agents={names_after} — if this FAILS, the in-memory _action_agents "
          f"map was fixed; tighten this to assert the name survives instead")

print()
if failures:
    print(f"FAILED ({len(failures)}): " + "; ".join(failures))
    raise SystemExit(1)
print("CHATGPT ACTIONS DOOR VERIFIED (full OAuth flow)")
