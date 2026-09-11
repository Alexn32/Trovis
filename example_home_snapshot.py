"""Generate the example GET /home/snapshot response in HOME_SNAPSHOT.md.

FIXTURE DATA. Every number this prints comes from a throwaway SQLite database
seeded by this script — it is not production data and not a screenshot of a
real customer. It exists so the documented example can be regenerated and
checked rather than trusted.

Wall-clock fields (generated_at, the period end, freshness) are pinned to
fixed strings below so the committed example does not churn on every run.

Run:
  OVERSEE_DISABLE_PRICING_SYNC=1 python3 example_home_snapshot.py
"""
import os, tempfile, time, json
os.environ.update({"OVERSEE_DISABLE_PRICING_SYNC":"1","TROVIS_DISABLE_ALERTS":"1",
                   "TROVIS_DISABLE_LOOP_SWEEP":"1","TROVIS_LOOP_TITLES":"off"})
os.environ.pop("DATABASE_URL",None); os.environ.pop("ANTHROPIC_API_KEY",None)
t=tempfile.NamedTemporaryFile(suffix=".db",delete=False); t.close()
import database; database.SQLITE_PATH=t.name
import main
from fastapi.testclient import TestClient
main._auto_describe=lambda *a,**k: False
NS=10**9; NOW=time.time_ns(); n=[0]
def kv(d): return [{"key":k,"value":{"stringValue":str(v)}} for k,v in d.items()]
def sp(name,off,attrs):
    n[0]+=1; tt=NOW-int(off)*NS
    return {"traceId":f"{n[0]:032d}","spanId":f"{n[0]:016d}","name":name,"kind":1,
      "startTimeUnixNano":str(tt),"endTimeUnixNano":str(tt+10**6),"status":{"code":1},"attributes":kv(attrs)}
with TestClient(main.app) as c:
    r=c.post("/auth/signup",json={"email":"ceo@acme.test","password":"correct horse battery",
        "name":"Cleo","account_type":"business","org_name":"Acme"}).json()
    K,T=r["api_key"],r["token"]; H={"Authorization":"Bearer "+T}
    def post(svc,spans):
        return c.post("/v1/traces",json={"resourceSpans":[{"resource":{"attributes":kv({"service.name":svc})},
            "scopeSpans":[{"spans":spans}]}]},headers={"X-Trovis-Api-Key":K})
    job=c.post("/workflows",headers=H,json={"name":"Refund requests","description":"d",
        "steps":[{"step_type":"agent","label":"run"}]}).json()
    for i in range(5):
        e=f"d{i}"
        post("refunds-agent",[sp("message_received",3600+i,{"trovis.loop.title":f"Refund {i}","trovis.loop.external_id":e}),
                              sp("agent_run_complete",3500+i,{"trovis.loop.external_id":e,"trovis.loop.close":"done"})])
    post("refunds-agent",[sp("message_received",300,{"trovis.loop.title":"Refund in flight","trovis.loop.external_id":"o1"}),
                          sp("tool_call",200,{"trovis.loop.external_id":"o1","trovis.tool.name":"search"})])
    post("refunds-agent",[sp("message_received",200000,{"trovis.loop.title":"Approve exception","trovis.loop.external_id":"o2"}),
        sp("agent_run_complete",190000,{"trovis.loop.external_id":"o2","trovis.handoff.direction":"to_human",
            "trovis.handoff.target_id":"ceo@acme.test","trovis.handoff.id":"H1"})])
    post("refunds-agent",[sp("llm_call",120,{"gen_ai.request.model":"claude-sonnet-4-5",
        "gen_ai.usage.input_tokens":12000,"gen_ai.usage.output_tokens":3000})])
    post("refunds-agent",[sp("llm_call",110,{"gen_ai.request.model":"unpriced-model",
        "gen_ai.usage.input_tokens":800,"gen_ai.usage.output_tokens":200})])
    with database._connect() as conn, database._cursor(conn) as cur:
        cur.execute(f"UPDATE loops SET workflow_id = {database.PH} WHERE title LIKE 'Refund %'", (job["id"],))
        cur.execute("UPDATE loops SET created_at = '2026-06-01 00:00:00' WHERE title = 'Refund 0'")
        for i in range(5):
            cur.execute(f"UPDATE loops SET closed_at = {database.PH} WHERE title = {database.PH}",
                        (f"2026-09-0{5+i} 14:0{i}:00", f"Refund {i}"))
    out=c.get("/home/snapshot?days=7&tz=America/Chicago",headers=H).json()
    out["generated_at"]="2026-09-11T18:00:00+00:00"
    out["period"]["end"]="2026-09-11T13:00:00-05:00"
    out["period"]["end_utc"]="2026-09-11T18:00:00+00:00"
    out["period"]["start"]="2026-09-05T00:00:00-05:00"
    out["period"]["start_utc"]="2026-09-05T05:00:00+00:00"
    out["period"]["comparison"]["previous_start_utc"]="2026-08-29T18:00:00+00:00"
    out["financial"]["period_end_utc"]="2026-09-11T18:00:00+00:00"
    for k, v in out["freshness"].items():
        if v:
            out["freshness"][k] = v[:19] + "+00:00"
    print(json.dumps(out,indent=2))
