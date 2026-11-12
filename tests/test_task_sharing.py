"""Offline tests: a task's links and files reach the person who has to do it.

    DATABASE_URL=postgresql://... uv run --no-project --with "psycopg[binary]" tests/test_task_sharing.py
"""

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
DATA = Path(tempfile.mkdtemp())
(DATA / "cache" / "documents").mkdir(parents=True)
os.environ.update(OWNER_SLACK_IDS="UOWNER", PX_DATA_ROOT=str(DATA), PX_DAILY_LIST="off")

from perpetual_core import ledger, notify, sharing, tasks  # noqa: E402

SENT, CALLS, UPLOADS = [], [], []
notify.dm = lambda ids, text: SENT.append((list(ids), text)) or len(ids)
PEOPLE = [
    {"id": "UOWNER", "name": "sai", "real_name": "Sai Prashanth", "display_name": "sai", "email": "sai@tp.com"},
    {"id": "USHR", "name": "shravya", "real_name": "Shravya K", "display_name": "shravya", "email": "shravya@tp.com"},
    {"id": "UNOMAIL", "name": "ravi", "real_name": "Ravi T", "display_name": "ravi", "email": None},
]
DOC = "https://docs.google.com/document/d/1AbCdEfGhIjKlMnOpQrStUvWxYz012345/edit?usp=sharing"
SHEET = "https://docs.google.com/spreadsheets/d/1ZyXwVuTsRqPoNmLkJiHgFeDcBa987654/edit"
CANVA = "https://www.canva.com/design/DAGabc123/edit"


def check(cond, msg):
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    print(f"ok   {msg}")


def fake_call(method, url, token, body=None, params=None, **_):
    CALLS.append((method, url, token, body, params))
    if "1ZyXwVuTsRqPoNmLkJiHgFeDcBa987654" in url:
        raise RuntimeError("403 from https://www.googleapis.com/drive/v3/files/x/permissions: insufficientFilePermissions")
    return {"id": "perm1"}


def fake_slack(method, **kw):
    assert method == "conversations.open"
    return {"channel": {"id": "D" + kw["users"]}}


def fake_upload(channel, files, initial_comment=None):
    UPLOADS.append((channel, [f[0] for f in files], initial_comment))
    return []


def share(conn, row, oid, created_by, access, people):
    return sharing.share_task(conn, row, oid, created_by, access, people, call=fake_call, slack=fake_slack,
                              upload=fake_upload)


def main():
    # pure helpers
    check(sharing.links_in(f"see <{DOC}|the doc> and {CANVA}.") == [DOC, CANVA], "links found in Slack text")
    check(sharing.kind(DOC) == "google" and sharing.kind(CANVA) == "canva", "link kinds")
    check(sharing.google_id(DOC) == "1AbCdEfGhIjKlMnOpQrStUvWxYz012345", "Google file id")
    check(sharing.google_label(SHEET) == "Google Sheet", "Google label")

    conn = ledger.connect()
    ledger.ensure_schema(conn)
    sharing._google_token = lambda conn, created_by: "gtok"
    brief = DATA / "cache" / "documents" / "brief.pdf"
    brief.write_bytes(b"%PDF-1.4 test")

    added, problems = tasks.add(conn, [{
        "title": "Review the launch doc", "owner": "Shravya", "due": "tomorrow",
        "notes": f"Also check the deck {CANVA}", "links": [DOC], "files": [str(brief)]}],
        created_by="UOWNER", people=PEOPLE, share=share)
    t = added[0]
    check(not problems, "no problems")
    check(t["links"] == [DOC, CANVA] and t["files"] == 1, "links (given + found in notes) and files saved on the task")
    perm = [c for c in CALLS if "/permissions" in c[1]]
    check(len(perm) == 1 and perm[0][3] == {"type": "user", "role": "writer", "emailAddress": "shravya@tp.com"}
          and perm[0][4]["sendNotificationEmail"] == "false", "Google Doc shared in Drive with edit access")
    statuses = {s["kind"]: s["status"] for s in t["shared"]["links"]}
    check(statuses == {"google": "shared", "canva": "link_only"}, "doc shared, Canva link-only")
    canva = next(s for s in t["shared"]["links"] if s["kind"] == "canva")
    check("anyone with the link can edit" in canva["note"], "creator told to open up the Canva link")
    check(UPLOADS and UPLOADS[0][0] == "DUSHR" and UPLOADS[0][1] == ["brief.pdf"], "file uploaded into their DM")
    dm = [s for s in SENT if s[0] == ["USHR"]][-1][1]
    check(DOC in dm and CANVA in dm and "edit access" in dm and "brief.pdf" in dm and "check the deck" in dm,
          "their DM has the notes, both links and the file")

    # view access + a file the connected account can't share + someone without an email
    CALLS.clear()
    added, _ = tasks.add(conn, [{"title": "Look at numbers", "owner": "Shravya", "links": [SHEET], "access": "view"},
                                {"title": "Read this", "owner": "Ravi", "links": [DOC]}],
                         created_by="UOWNER", people=PEOPLE, share=share)
    s1 = added[0]["shared"]["links"][0]
    check(s1["status"] == "failed" and "isn't its owner" in s1["note"], "Drive refusal explained")
    s2 = added[1]["shared"]["links"][0]
    check(s2["status"] == "link_only" and "no email" in s2["note"], "no email: link sent instead")

    # tasks for yourself: nothing to share, nobody DMed
    before = len(SENT)
    added, _ = tasks.add(conn, [{"title": "My own thing", "owner": "Sai", "links": [DOC]}],
                         created_by="UOWNER", people=PEOPLE, share=share)
    check("shared" not in added[0] and len(SENT) == before, "own task: no share, no DM")

    # files outside the document cache are refused
    out = sharing.deliver_files("USHR", ["/etc/passwd"], slack=fake_slack, upload=fake_upload)
    check(out[0]["status"] == "failed", "server files can't be sent")

    # reassigning carries the links
    import perpetual_core.slackui as slackui
    slackui.people = lambda: PEOPLE
    CALLS.clear()
    orig = sharing.share_task
    sharing.share_task = lambda conn, row, oid, by, access, people: orig(conn, row, oid, by, access, people,
                                                                          call=fake_call, slack=fake_slack,
                                                                          upload=fake_upload)
    tasks.update(conn, t["code"], by="UOWNER", owner="Ravi", people=PEOPLE)
    dm = [s for s in SENT if s[0] == ["UNOMAIL"]][-1][1]
    check(t["code"] in dm and DOC in dm, "reassigned owner gets the links too")
    sharing.share_task = orig

    # tool schema carries links/files/access
    tool = next(x for x in tasks.TOOLS if x[0] == "task_add")
    check({"links", "files", "access"} <= set(tool[3]) and "ALWAYS pass" in tool[2], "task_add exposes links/files")

    conn.execute("DELETE FROM team_tasks WHERE created_by = 'UOWNER' AND title IN "
                 "('Review the launch doc','Look at numbers','Read this','My own thing')")
    conn.commit()
    conn.close()
    print(json.dumps({"ok": True}))
    print("all good")


if __name__ == "__main__":
    main()
