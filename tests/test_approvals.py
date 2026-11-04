"""Offline tests: the approvals queue (fake Slack, fake Google).

    DATABASE_URL=postgresql://... uv run --no-project --python 3.12 --with "psycopg[binary]" --with cryptography \
        --with tzdata tests/test_approvals.py
"""

import asyncio
import base64
import json
import os
import sys
import uuid
from email import message_from_bytes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
os.environ.update(OWNER_SLACK_IDS="UOWNER,UOWN2")

from perpetual_core import approvals, connections, ledger, notify, slackui, workspace  # noqa: E402

NAMES = {"UOWNER": "Sai", "UOWN2": "Gil", "UTEAM": "Shravya"}
slackui.person_name = lambda uid: NAMES.get(uid, uid)
DMS = []
notify.dm = lambda ids, text: DMS.append((list(ids), text)) or len(ids)
connections.token = lambda conn, user, provider: f"TOK-{user}"
GOOGLE_ROWS = {"UTEAM": False}
connections.get_row = lambda conn, user, provider: {"x": 1} if GOOGLE_ROWS.get(user) else None
WHO = {"user": "UTEAM"}
connections.current_user = lambda: WHO["user"]


class FakeSlack:
    def __init__(self):
        self.calls, self.n = [], 0

    def __call__(self, method, **kw):
        self.calls.append((method, kw))
        self.n += 1
        if method == "chat.postMessage":
            ch = kw["channel"]
            return {"ok": True, "channel": "D" + ch[1:] if ch.startswith("U") else ch, "ts": f"{self.n}.0"}
        return {"ok": True, "view": {"id": "V1"}}

    def of(self, method):
        return [kw for m, kw in self.calls if m == method]


class FakeGoogle:
    def __init__(self):
        self.calls, self.fail = [], None

    def __call__(self, method, url, token, body=None, params=None, **_):
        self.calls.append((method, url, token, body, params))
        if self.fail:
            raise RuntimeError(self.fail)
        return {"id": "G1", "threadId": "T1", "htmlLink": "https://cal/1"}

    def sent(self):
        return [c for c in self.calls if c[1].endswith("/messages/send")]


def raw_body(call):
    msg = message_from_bytes(base64.urlsafe_b64decode(call[3]["raw"]))
    return msg, msg.get_payload(decode=True).decode()


def check(cond, msg):
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    print(f"ok   {msg}")


def texts(blocks):
    return json.dumps(blocks, ensure_ascii=False)


class Ack:
    def __init__(self):
        self.kw = None

    async def __call__(self, **kw):
        self.kw = kw


def click(user, aid, channel="DOWNER"):
    return {"user": {"id": user}, "channel": {"id": channel}, "trigger_id": "TRIG"}, {"value": json.dumps({"id": aid})}


def main():
    slack, google = FakeSlack(), FakeGoogle()
    slackui.slack = slack
    workspace._call = google
    conn = ledger.connect()
    ledger.ensure_schema(conn)
    conn.execute(approvals.SCHEMA_SQL)
    conn.commit()
    tag = uuid.uuid4().hex[:6]

    def email(body="Hi Alex,\n\nHere is the October report.\n\nSai"):
        return approvals.request(conn, "email", f"October report to Alex {tag}",
                                 {"to": ["alex@client.com"], "cc": "gil@agency.com", "subject": "October report",
                                  "body": body}, None, "UTEAM", "agency", client="@AlexVacca")

    # ---- request card
    row = email()
    posts = slack.of("chat.postMessage")
    check(len(posts) == 2 and {p["channel"] for p in posts} == {"UOWNER", "UOWN2"}, "card DMed to both owners")
    blocks = posts[0]["blocks"]
    actions = [e["action_id"] for b in blocks if b["type"] == "actions" for e in b["elements"]]
    check(actions == ["px_apr_ok", "px_apr_edit", "px_apr_no"], "card has Approve / Edit / Reject")
    check(json.loads(blocks[-1]["elements"][0]["value"]) == {"id": row["id"]}, "button value carries the id")
    check("Here is the October report" in texts(blocks) and "alex@client.com" in texts(blocks)
          and "@alexvacca" in blocks[0]["text"]["text"], "card shows header with client and the full email")
    check(len(row["approver_msg"]) == 2 and row["approver_msg"][0]["ts"], "card channel/ts stored")
    check(row["status"] == "pending" and row["requested_for"] == "agency", "stored pending, runs as agency")
    try:
        approvals.request(conn, "wire_money", "x", {}, None, "UTEAM", "agency")
        check(False, "unknown kind rejected")
    except ValueError:
        check(True, "unknown kind rejected")
    note = approvals.request(conn, "note", f"Send the contract {tag}", {}, "Send Alex the MSA", "UTEAM", "agency")
    nb = slack.of("chat.postMessage")[-1]["blocks"]
    check([e["action_id"] for b in nb if b["type"] == "actions" for e in b["elements"]] == ["px_apr_ok", "px_apr_no"],
          "no Edit button for kinds without text")

    # ---- non-owner refused (function and button)
    try:
        approvals.decide(conn, row["id"], "approve", "UTEAM")
        check(False, "non-owner refused")
    except PermissionError:
        check(True, "non-owner refused by decide")
    slack.calls.clear()
    body, action = click("UTEAM", row["id"], "DTEAM")
    asyncio.run(approvals.on_apr_ok(Ack(), body, action))
    eph = slack.of("chat.postEphemeral")
    check(eph and "Only agency owners" in eph[0]["text"] and eph[0]["user"] == "UTEAM", "non-owner gets ephemeral")
    asyncio.run(approvals.on_apr_edit(Ack(), body, action))
    check(not slack.of("views.open"), "non-owner can't open the edit modal")
    check(approvals.get(conn, row["id"])["status"] == "pending" and not google.sent(), "still pending, nothing sent")

    # ---- approve executes exactly once (via the button)
    DMS.clear()
    slack.calls.clear()
    body, action = click("UOWNER", row["id"])
    asyncio.run(approvals.on_apr_ok(Ack(), body, action))
    check(len(google.sent()) == 1, "approve sends the email once")
    msg, text = raw_body(google.sent()[0])
    check(msg["To"] == "alex@client.com" and msg["Cc"] == "gil@agency.com" and "October report" in text
          and google.sent()[0][2] == "TOK-agency", "email sent from the agency Gmail with to/cc/body")
    r = approvals.get(conn, row["id"])
    check(r["status"] == "executed" and r["decided_by"] == "UOWNER" and r["result"]["id"] == "G1", "status executed")
    ups = slack.of("chat.update")
    check(len(ups) == 2 and all("✅ Sent by Sai" in texts(u["blocks"]) for u in ups)
          and not any(b["type"] == "actions" for b in ups[0]["blocks"]), "both owner cards show '✅ Sent by Sai'")
    check(DMS and DMS[0][0] == ["UTEAM"] and "Sent by Sai" in DMS[0][1], "requester DMed the outcome")
    check(any("Sent by Sai" in e["text"] for e in slack.of("chat.postEphemeral")), "clicker gets feedback")

    # ---- double click idempotent
    slack.calls.clear()
    body, action = click("UOWN2", row["id"])
    asyncio.run(approvals.on_apr_ok(Ack(), body, action))
    check(len(google.sent()) == 1, "second click doesn't send again")
    check("already handled" in slack.of("chat.postEphemeral")[0]["text"], "second click told it's already handled")
    _, m = approvals.decide(conn, row["id"], "reject", "UOWN2")
    check("already handled" in m and approvals.get(conn, row["id"])["status"] == "executed", "late reject ignored")

    # ---- edit changes body before sending (modal flow)
    row2 = email("Draft body")
    slack.calls.clear()
    body, action = click("UOWNER", row2["id"])
    asyncio.run(approvals.on_apr_edit(Ack(), body, action))
    v = slack.of("views.open")[0]["view"]
    check(v["callback_id"] == "px_apr_edit_submit" and v["blocks"][0]["element"]["initial_value"] == "Draft body",
          "edit modal prefilled with the body")
    ack = Ack()
    empty = {"private_metadata": v["private_metadata"], "state": {"values": {"text": {"v": {"value": "  "}}}}}
    asyncio.run(approvals.on_apr_edit_submit(ack, body, empty))
    check(ack.kw.get("response_action") == "errors" and len(google.sent()) == 1, "empty edit refused, nothing sent")
    filled = {"private_metadata": v["private_metadata"],
              "state": {"values": {"text": {"v": {"value": "Final body, edited by Sai"}}}}}
    asyncio.run(approvals.on_apr_edit_submit(Ack(), body, filled))
    check(len(google.sent()) == 2 and raw_body(google.sent()[-1])[1].strip() == "Final body, edited by Sai",
          "edit sends the edited body")
    r2 = approvals.get(conn, row2["id"])
    check(r2["status"] == "executed" and r2["payload"]["body"] == "Final body, edited by Sai"
          and "edited by Sai" in r2["preview"], "edited payload and preview saved")

    # ---- reject doesn't execute (modal flow)
    row3 = email("Should never go out")
    slack.calls.clear()
    DMS.clear()
    body, action = click("UOWN2", row3["id"])
    asyncio.run(approvals.on_apr_no(Ack(), body, action))
    v = slack.of("views.open")[0]["view"]
    check(v["callback_id"] == "px_apr_no_submit", "reject opens the reason modal")
    asyncio.run(approvals.on_apr_no_submit(Ack(), body, {"private_metadata": v["private_metadata"],
                                                         "state": {"values": {"note": {"v": {"value": "wrong tone"}}}}}))
    r3 = approvals.get(conn, row3["id"])
    check(r3["status"] == "rejected" and r3["error"] == "wrong tone" and len(google.sent()) == 2,
          "reject: nothing sent, reason kept")
    check(any("Rejected by Gil: wrong tone" in texts(u["blocks"]) for u in slack.of("chat.update"))
          and "wrong tone" in DMS[0][1], "cards + requester show the rejection")

    # ---- executor failure → failed + error shown
    google.fail = "403 from gmail: insufficient scope"
    row4 = email()
    slack.calls.clear()
    r4, m = approvals.decide(conn, row4["id"], "approve", "UOWNER")
    google.fail = None
    check(r4["status"] == "failed" and "insufficient scope" in r4["error"], "failure → status failed + error")
    check("failed" in m and all("insufficient scope" in texts(u["blocks"]) for u in slack.of("chat.update")),
          "error shown on the cards")
    _, m = approvals.decide(conn, row4["id"], "approve", "UOWNER")
    check("already handled" in m, "failed approval isn't retried by a second click")

    # ---- slack_message executor
    row5 = approvals.request(conn, "slack_message", f"Weekly update to client {tag}",
                             {"channel": "CCLIENT", "text": "Hi team, this week's numbers…"}, None, "UTEAM", "agency")
    slack.calls.clear()
    r5, _ = approvals.decide(conn, row5["id"], "approve", "UOWNER")
    out = [p for p in slack.of("chat.postMessage") if p["channel"] == "CCLIENT"]
    check(len(out) == 1 and out[0]["text"].startswith("Hi team") and r5["status"] == "executed"
          and r5["result"]["channel"] == "CCLIENT", "slack_message posts to the client channel")

    # ---- calendar invite: guests are emailed
    row6 = approvals.request(conn, "calendar_invite", f"Kickoff {tag}",
                             {"title": "Kickoff", "start": "2026-11-10T10:00:00", "end": "2026-11-10T10:30:00",
                              "timezone": "Asia/Kolkata", "attendees": ["alex@client.com"], "with_meet": True},
                             None, "UOWNER", "UOWNER")
    r6, _ = approvals.decide(conn, row6["id"], "approve", "UOWNER")
    cal = google.calls[-1]
    check(cal[4]["sendUpdates"] == "all" and cal[4]["conferenceDataVersion"] == 1 and cal[2] == "TOK-UOWNER"
          and cal[3]["attendees"] == [{"email": "alex@client.com"}] and r6["status"] == "executed",
          "calendar_invite sends updates to guests, with Meet, from the requester's calendar")
    r7, _ = approvals.decide(conn, note["id"], "approve", "UOWN2")
    check(r7["status"] == "executed" and r7["result"] == {"noted": True}, "note kind just records the decision")

    # ---- tools + cancel
    WHO["user"] = "UTEAM"
    res = json.loads(approvals.request_approval({"kind": "email", "summary": f"Invoice {tag}", "client": "alexvacca",
                                                 "payload": {"to": "billing@client.com", "subject": "Invoice",
                                                             "body": "Attached."}}))
    check(res["success"] and res["approval"]["runs_as"] == "agency" and res["owners_asked"] == 2,
          "request_approval: no own Google → agency account")
    GOOGLE_ROWS["UTEAM"] = True
    res2 = json.loads(approvals.request_approval({"kind": "slack_message", "summary": f"Ping {tag}",
                                                  "payload": {"channel": "C1", "text": "hi"}}))
    check(res2["approval"]["runs_as"] == "UTEAM", "request_approval: own Google → their account")
    bad = json.loads(approvals.request_approval({"kind": "email", "summary": "x", "payload": {"to": "a@b.c"}}))
    check(not bad["success"] and "body" in bad["error"], "missing payload field refused")
    lst = json.loads(approvals.approvals_list({}))
    check(lst["success"] and any(a["id"] == res["approval"]["id"] for a in lst["approvals"])
          and all(a["status"] == "pending" for a in lst["approvals"]), "approvals_list shows pending")
    WHO["user"] = "USTRANGER"
    c = json.loads(approvals.approval_cancel({"id": res["approval"]["id"]}))
    check(not c["success"] and "cancel" in c["error"], "stranger can't cancel")
    WHO["user"] = "UTEAM"
    slack.calls.clear()
    c = json.loads(approvals.approval_cancel({"id": res["approval"]["id"]}))
    check(c["success"] and c["approval"]["status"] == "cancelled"
          and all("Cancelled by Shravya" in texts(u["blocks"]) for u in slack.of("chat.update")),
          "requester cancels; cards updated")
    WHO["user"] = "UOWN2"
    c = json.loads(approvals.approval_cancel({"id": res2["approval"]["id"]}))
    check(c["success"] and c["approval"]["status"] == "cancelled", "owner can cancel")
    _, m = approvals.decide(conn, res["approval"]["id"], "approve", "UOWNER")
    check("already handled" in m and len(google.sent()) == 3, "cancelled approval can't be approved")

    conn.execute("DELETE FROM approvals WHERE summary LIKE %s", (f"%{tag}",))
    conn.commit()
    conn.close()
    print("all good")


if __name__ == "__main__":
    main()
