"""Work suggestions: list, approve, edit+approve, decline; item detail spine.

Approve creates a named work item (GET /work/items). Decline removes the
suggestion and does not create a row. Garbage titles 400 on approve.
"""
import os, tempfile, uuid

os.environ.update({"OVERSEE_DISABLE_PRICING_SYNC": "1", "TROVIS_DISABLE_ALERTS": "1",
                   "TROVIS_DISABLE_LOOP_SWEEP": "1", "TROVIS_LOOP_TITLES": "off"})
os.environ.pop("DATABASE_URL", None)
os.environ.pop("ANTHROPIC_API_KEY", None)
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False); _tmp.close()
import database; database.SQLITE_PATH = _tmp.name
import loops as loops_mod, main
from fastapi.testclient import TestClient
main._auto_describe = lambda *a, **k: False

failures = []
def check(label, cond):
    print(("  PASS " if cond else "  FAIL ") + label)
    if not cond: failures.append(label)

ITEM_KEYS = {
    "id", "title", "status", "holder", "whats_next", "updated_at",
    # The open decision's event id, so Home's desk can offer Done / I've got
    # this / Not mine on the row without a detail fetch per row.
    "awaiting_handoff_event_id",
    # The kind of work, read off the page's own rows.
    "workflow_id", "workflow_name",
}
SUG_KEYS = {"id", "title", "why", "source", "draft_holder"}
DETAIL_KEYS = ITEM_KEYS | {"whats_happening", "process", "timeline", "provenance"}

with TestClient(main.app) as c:
    r = c.post("/auth/signup", json={"email": "s@t.com", "password": "supersecret123",
        "name": "Alex", "account_type": "business", "org_name": "Co"}).json()
    K, T = r["api_key"], r["token"]
    H = {"Authorization": f"Bearer {T}"}
    me = c.get("/auth/me", headers=H).json()
    account_id = me["org"]["id"]

    print("\n--- empty list (no invented titles) ---")
    sug = c.get("/work/suggestions", headers=H).json()
    check("empty account: suggestions []", sug == {"suggestions": []})

    print("\n--- title gate helper ---")
    check("human title ok", database.is_human_work_title("Reply to customer"))
    check("too short rejected", not database.is_human_work_title("ab"))
    check("numeric id rejected", not database.is_human_work_title("1842"))
    check("snake_case rejected", not database.is_human_work_title("loop_42"))
    check("uuid rejected", not database.is_human_work_title(str(uuid.uuid4())))
    check("jargon rejected", not database.is_human_work_title("awaiting_handoff"))
    check("hex blob rejected", not database.is_human_work_title("a" * 16))
    check("Task-from shell rejected", not database.is_human_work_title("Task from main"))
    check("template-title shell rejected",
          not database.is_human_work_title("cs-agent · search · 3 actions"))

    print("\n--- approve creates a named work item ---")
    s1 = database.insert_work_suggestion(
        account_id, title="Reply to customer", why="It's been waiting on you",
        source="telemetry",
        draft_holder={"kind": "human", "name": "Alex"},
    )
    listed = c.get("/work/suggestions", headers=H).json()["suggestions"]
    check("list includes the pending suggestion",
          any(s["id"] == s1["id"] for s in listed))
    check("suggestion shape is the locked contract",
          all(set(s.keys()) == SUG_KEYS for s in listed))
    check("draft_holder kind/name present",
          listed[0]["draft_holder"]["kind"] == "human"
          and listed[0]["draft_holder"]["name"] == "Alex")

    ap = c.post(f"/work/suggestions/{s1['id']}/approve", headers=H)
    check("approve 200", ap.status_code == 200)
    item = ap.json()["item"]
    check("approve returns a work item with locked keys",
          set(item.keys()) == ITEM_KEYS)
    check("approved title is the suggestion title",
          item["title"] == "Reply to customer")
    page = c.get("/work/items", headers=H).json()
    by_title = {it["title"]: it for it in page["items"]}
    check("approved item appears in /work/items",
          "Reply to customer" in by_title)
    check("approved item is gone from the strip",
          c.get("/work/suggestions", headers=H).json()["suggestions"] == [])
    check("holder is human (approver)",
          by_title["Reply to customer"]["holder"]["kind"] == "human")

    print("\n--- approve is idempotent ---")
    ap2 = c.post(f"/work/suggestions/{s1['id']}/approve", headers=H)
    check("re-approve 200 with the same item id",
          ap2.status_code == 200 and ap2.json()["item"]["id"] == item["id"])

    print("\n--- edit then approve ---")
    s2 = database.insert_work_suggestion(
        account_id, title="Draft note", why="Needs a clearer name",
    )
    ed = c.patch(f"/work/suggestions/{s2['id']}", headers=H,
                 json={"title": "Send the weekly note", "why": "Friday digest"})
    check("edit 200", ed.status_code == 200)
    check("edit stays on the strip with the new title",
          ed.json()["title"] == "Send the weekly note"
          and any(s["id"] == s2["id"] for s in
                  c.get("/work/suggestions", headers=H).json()["suggestions"]))
    ap3 = c.post(f"/work/suggestions/{s2['id']}/approve", headers=H)
    check("approve-after-edit uses the edited title",
          ap3.status_code == 200
          and ap3.json()["item"]["title"] == "Send the weekly note")
    check("edited title is in /work/items",
          any(it["title"] == "Send the weekly note"
              for it in c.get("/work/items", headers=H).json()["items"]))

    print("\n--- edit+approve in one call ---")
    s3 = database.insert_work_suggestion(
        account_id, title="Placeholder name", why="rename me",
    )
    ap4 = c.post(
        f"/work/suggestions/{s3['id']}/approve", headers=H,
        json={"title": "Book the vendor call"},
    )
    check("one-shot edit+approve 200", ap4.status_code == 200)
    check("one-shot uses the body title",
          ap4.json()["item"]["title"] == "Book the vendor call")

    print("\n--- garbage title rejected on approve ---")
    s_bad = database.insert_work_suggestion(
        account_id, title="loop_42", why="internal id leaked",
    )
    bad = c.post(f"/work/suggestions/{s_bad['id']}/approve", headers=H)
    check("garbage title 400", bad.status_code == 400)
    check("400 copy is human, not a stack",
          "person can read" in (bad.json().get("detail") or "").lower())
    check("garbage approve did not create a work item",
          all(it["title"] != "loop_42"
              for it in c.get("/work/items", headers=H).json()["items"]))
    check("garbage suggestion still on the strip (not silently dropped)",
          any(s["id"] == s_bad["id"] for s in
              c.get("/work/suggestions", headers=H).json()["suggestions"]))
    # Fix the title via the approve body.
    fixed = c.post(
        f"/work/suggestions/{s_bad['id']}/approve", headers=H,
        json={"title": "Fix the billing export"},
    )
    check("approve with a human title after a 400 succeeds",
          fixed.status_code == 200
          and fixed.json()["item"]["title"] == "Fix the billing export")

    print("\n--- decline removes from strip, no ghost row ---")
    s4 = database.insert_work_suggestion(
        account_id, title="Noise we should ignore", why="false alarm",
    )
    dec = c.post(f"/work/suggestions/{s4['id']}/decline", headers=H)
    check("decline 204", dec.status_code == 204)
    check("declined is gone from the strip",
          all(s["id"] != s4["id"] for s in
              c.get("/work/suggestions", headers=H).json()["suggestions"]))
    check("decline created no work item",
          all(it["title"] != "Noise we should ignore"
              for it in c.get("/work/items", headers=H).json()["items"]))
    dec2 = c.post(f"/work/suggestions/{s4['id']}/decline", headers=H)
    check("re-decline is idempotent 204", dec2.status_code == 204)
    # Cannot approve after decline (gone; no resurrection / ghost).
    ap_dead = c.post(f"/work/suggestions/{s4['id']}/approve", headers=H)
    check("approve after decline is 404", ap_dead.status_code == 404)
    check("still no ghost row after a declined approve attempt",
          all(it["title"] != "Noise we should ignore"
              for it in c.get("/work/items", headers=H).json()["items"]))

    print("\n--- decline of an approved suggestion 409 ---")
    dec_ap = c.post(f"/work/suggestions/{s1['id']}/decline", headers=H)
    check("decline already-approved is 409", dec_ap.status_code == 409)
    check("approved item still in /work/items after decline-409",
          any(it["title"] == "Reply to customer"
              for it in c.get("/work/items", headers=H).json()["items"]))

    print("\n--- GET /work/items/:id spine ---")
    detail = c.get(f"/work/items/{item['id']}", headers=H)
    check("detail 200", detail.status_code == 200)
    d = detail.json()
    check("detail has spine keys", DETAIL_KEYS <= set(d.keys()))
    check("whats_happening is populated", bool(d["whats_happening"]))
    check("timeline is a list", isinstance(d["timeline"], list))
    check("provenance source is suggestion",
          (d.get("provenance") or {}).get("source") == "suggestion")
    check("provenance points at the suggestion id",
          str((d.get("provenance") or {}).get("suggestion_id")) == str(s1["id"]))
    missing = c.get("/work/items/999999", headers=H)
    check("unknown item 404", missing.status_code == 404)

    print("\n--- session required for mutations; api key cannot approve ---")
    s5 = database.insert_work_suggestion(
        account_id, title="Needs a person to approve", why="session only",
    )
    keyed = c.post(
        f"/work/suggestions/{s5['id']}/approve",
        headers={"X-Trovis-Api-Key": K},
    )
    check("api-key approve is 403", keyed.status_code == 403)
    check("api-key decline is 403",
          c.post(f"/work/suggestions/{s5['id']}/decline",
                 headers={"X-Trovis-Api-Key": K}).status_code == 403)

    print("\n--- cross-account ---")
    r2 = c.post("/auth/signup", json={"email": "o@t.com", "password": "supersecret123",
        "name": "O", "account_type": "individual", "org_name": "O"}).json()
    other_h = {"Authorization": f"Bearer {r2['token']}"}
    check("other account cannot see the suggestion",
          c.get("/work/suggestions", headers=other_h).json()["suggestions"] == [])
    check("other account cannot approve it",
          c.post(f"/work/suggestions/{s5['id']}/approve",
                 headers=other_h).status_code == 404)
    check("other account cannot read the item",
          c.get(f"/work/items/{item['id']}", headers=other_h).status_code == 404)

    print("\n--- engine states untouched ---")
    check("engine states unchanged",
          loops_mod.STATES == ("open", "working", "awaiting_human", "awaiting_agent",
                               "awaiting_system", "stalled", "done", "abandoned"))

print()
if failures:
    print(f"FAILED ({len(failures)}):")
    for f in failures: print("  - " + f)
    raise SystemExit(1)
print("All work-suggestion checks passed.")
os.unlink(_tmp.name)
