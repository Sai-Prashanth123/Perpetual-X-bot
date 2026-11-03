"""Offline tests: Read AI connection (PKCE, rotating refresh), Read AI parsing, team tasks,
meeting follow-up sync, agency-wide account guard.

    DATABASE_URL=postgresql://... uv run --no-project --with "psycopg[binary]" --with cryptography tests/test_meetings_tasks.py
"""

import json
import os
import sys
import time
import uuid
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
os.environ.update(INTERNAL_TOKEN="test-key", AGENCY_WEB_URL="https://connector.example",
                  READAI_CLIENT_ID="rid", READAI_CLIENT_SECRET="rsecret", OWNER_SLACK_IDS="UOWNER")
from cryptography.fernet import Fernet  # noqa: E402
os.environ.setdefault("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())

from perpetual_core import connections, ledger, meetings, notify, readai, tasks, workspace  # noqa: E402

SENT = []
notify.dm = lambda ids, text: SENT.append((list(ids), text)) or len(ids)
PEOPLE = [
    {"id": "UOWNER", "name": "sai", "real_name": "Sai Prashanth", "display_name": "sai", "email": "sai@tp.com"},
    {"id": "USHR", "name": "shravya", "real_name": "Shravya K", "display_name": "shravya", "email": "shravya@tp.com"},
    {"id": "UNAGA", "name": "naga", "real_name": "Naga R", "display_name": "naga", "email": "naga@tp.com"},
    {"id": "UNAG2", "name": "nagendra", "real_name": "Nagendra P", "display_name": "", "email": "nag@tp.com"},
]
from perpetual_core import slackui  # noqa: E402
slackui.people = lambda: PEOPLE


def check(cond, msg):
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    print(f"ok   {msg}")


def test_readai_oauth():
    a = connections.authorize_url("readai", "state123")
    q = {k: v[0] for k, v in parse_qs(urlparse(a).query).items()}
    check(a.startswith("https://authn.read.ai/oauth2/auth?"), "Read AI sign-in goes to authn.read.ai")
    check("meeting:read" in q["scope"] and "offline_access" in q["scope"], "asks for meetings + refresh")
    v = connections.pkce_verifier("state123")
    check(q["code_challenge"] == connections.pkce_challenge(v) and q["code_challenge_method"] == "S256",
          "PKCE challenge derived from the state")
    check(connections.pkce_verifier("other") != v and 43 <= len(v) <= 128, "verifier is per-sign-in and valid length")
    calls = []
    connections._post = lambda url, data=None, headers=None, body=None: calls.append((url, data)) or {
        "access_token": "AT1", "refresh_token": "RT1", "expires_in": 600, "scope": "meeting:read"}
    connections._account = lambda provider, tok: "sai@tp.com"
    conn = ledger.connect()
    ledger.ensure_schema(conn)
    user = f"U{uuid.uuid4().hex[:8].upper()}"
    connections.finish(conn, "readai", user, "CODE", "state123")
    check(calls[0][1]["code_verifier"] == v and calls[0][1]["client_secret"] == "rsecret", "code exchange sends verifier + secret")
    check(connections.token(conn, user, "readai") == "AT1", "fresh token used as-is")
    conn.execute("UPDATE user_connections SET expires_at = now() - interval '1 minute' WHERE slack_user = %s", (user,))
    conn.commit()
    connections._post = lambda url, data=None, headers=None, body=None: calls.append((url, data)) or {
        "access_token": "AT2", "refresh_token": "RT2", "expires_in": 600}
    check(connections.token(conn, user, "readai") == "AT2", "expired token refreshed")
    check(calls[-1][1]["refresh_token"] == "RT1", "refresh used the stored refresh token")
    row = connections.get_row(conn, user, "readai")
    check(connections._fernet().decrypt(row["refresh_enc"].encode()).decode() == "RT2", "rotated refresh token saved")
    conn.execute("UPDATE user_connections SET expires_at = now() - interval '1 minute' WHERE slack_user = %s", (user,))
    conn.commit()

    def revoked(*a, **k):
        raise RuntimeError('400: {"error":"invalid_grant"}')
    connections._post = revoked
    try:
        connections.token(conn, user, "readai")
        check(False, "revoked refresh should raise")
    except connections.NotConnected as e:
        check("connect my readai" in str(e), "revoked connection asks to reconnect")
    try:
        connections.token(conn, connections.AGENCY + "-none", "readai")
    except connections.NotConnected:
        pass
    conn.execute("DELETE FROM user_connections WHERE slack_user = %s", (user,))
    conn.commit()
    conn.close()


def test_readai_normalize():
    m = readai.normalize({
        "id": "01HX", "title": "Alex weekly", "start_time_ms": 1762160400000, "end_time_ms": 1762162200000,
        "participants": [{"name": "Shravya K", "email": "shravya@tp.com"}, "Alex"],
        "summary": {"text": "Agreed on Q4 plan."},
        "action_items": [{"text": "Send Alex the Q4 plan", "assignee": {"name": "Shravya K"}}, "Book next call", {"text": ""}],
        "key_questions": [{"question": "Budget?"}], "topics": [{"title": "Q4"}],
        "transcript": {"speaker_blocks": [{"speaker": {"name": "Alex"}, "words": "hi", "text": "hi there"}]}},
        transcript=True)
    check(m["start"].startswith("2025-11-03") and m["end"], "epoch ms times become ISO")
    check(m["participants"][0]["email"] == "shravya@tp.com" and m["participants"][1]["name"] == "Alex", "participants read")
    check(m["summary"] == "Agreed on Q4 plan." and m["key_questions"] == ["Budget?"] and m["topics"] == ["Q4"], "summary/questions/topics")
    check([a["text"] for a in m["action_items"]] == ["Send Alex the Q4 plan", "Book next call"], "action items, empty dropped")
    check(m["action_items"][0]["owner"] == "Shravya K", "action item owner read")
    check(m["transcript"] == "Alex: hi there", "transcript speaker blocks joined")
    check(readai.normalize({})["title"] == "Untitled meeting", "missing fields never fail")


def test_tasks():
    conn = ledger.connect()
    ledger.ensure_schema(conn)
    SENT.clear()
    tag = uuid.uuid4().hex[:6]
    added, problems = tasks.add(conn, [
        {"title": f"Send Alex the plan {tag}", "owner": "shravya@tp.com", "due": "tomorrow", "client": "@ItsAlexVacca"},
        {"title": f"Book call {tag}", "owner": "Shravya", "due": "2020-01-01"},
        {"title": f"Fix deck {tag}", "owner": "nag"},
        {"title": f"Nobody {tag}", "owner": "zed"},
        {"title": f"Mine {tag}", "owner": "UOWNER"},
    ], created_by="UOWNER", people=PEOPLE)
    check(len(added) == 5 and added[0]["owner_id"] == "USHR" and added[0]["client"] == "itsalexvacca", "email owner + client")
    check(added[1]["owner_id"] == "USHR", "first-name owner match")
    check(added[2]["owner_id"] is None and any("several" in p["problem"] for p in problems), "ambiguous name reported")
    check(any("nobody" in p["problem"] for p in problems), "unknown owner reported")
    check(len(SENT) == 1 and SENT[0][0] == ["USHR"] and SENT[0][1].count("• *TASK-") == 2,
          "one grouped DM per owner, not to the creator")
    c = added[1]["code"]
    t = tasks.update(conn, c, by="UOWNER", status="done")
    check(t["status"] == "done", "done")
    t = tasks.update(conn, c, by="UOWNER", status="open", due="friday", owner="naga", note="moved")
    check(t["owner_id"] == "UNAGA" and SENT[-1][0] == ["UNAGA"] and "moved" in t["notes"], "reassign DMs new owner")
    check(tasks.parse_due("in 3 days", date(2026, 11, 3)) == date(2026, 11, 6)
          and tasks.parse_due("friday", date(2026, 11, 3)) == date(2026, 11, 6)
          and tasks.parse_due("tuesday", date(2026, 11, 3)) == date(2026, 11, 10), "due words")
    # reminders: due today/overdue, once per day
    conn.execute("UPDATE team_tasks SET due = current_date - 2 WHERE id = %s", (tasks.parse_code(added[0]["code"]),))
    conn.commit()
    SENT.clear()
    tasks.send_reminders(conn)
    first = [s for s in SENT if s[0] == ["USHR"]]
    check(first and added[0]["code"] in first[0][1] and "overdue" in first[0][1], "overdue reminder DMed")
    SENT.clear()
    tasks.send_reminders(conn)
    check(not [s for s in SENT if added[0]["code"] in s[1]], "reminded only once a day")
    rows = tasks.listing(conn, "USHR")
    check(any(r["title"].endswith(tag) for r in rows), "listing by owner")
    conn.execute("DELETE FROM team_tasks WHERE title LIKE %s", (f"%{tag}",))
    conn.commit()
    conn.close()


def test_meeting_sync():
    conn = ledger.connect()
    ledger.ensure_schema(conn)
    now = time.time()
    a, b, c = (f"T{uuid.uuid4().hex[:8]}" for _ in range(3))
    iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))  # noqa: E731
    listed = [
        {"id": a, "title": "Alex weekly", "start": iso(now - 7200), "end": iso(now - 5400)},
        {"id": b, "title": "Live now", "start": iso(now - 600), "end": None},
        {"id": c, "title": "Just ended", "start": iso(now - 3000), "end": iso(now - 600)},
    ]
    full = {a: {"title": "Alex weekly", "summary": "Plan agreed", "action_items": [{"text": "Send plan", "owner": "Shravya"}],
                "participants": [{"name": "Shravya K", "email": "shravya@tp.com"}]},
            c: {"title": "Just ended", "summary": None, "action_items": []}}
    stored = meetings.sync(conn, tok="x", fetch_list=lambda: listed, fetch_one=lambda mid: full[mid], now=now)
    check(stored == [f"readai:{a}"], "only finished meetings with notes are stored")
    check(meetings.sync(conn, tok="x", fetch_list=lambda: listed, fetch_one=lambda mid: full[mid], now=now) == [],
          "no duplicates on the next run")
    p = [r for r in meetings.pending(conn, 10) if r["id"] == f"readai:{a}"]
    check(p and p[0]["action_items"][0]["text"] == "Send plan", "pending meeting with action items")
    out = json.loads(meetings.meetings_pending({"limit": 10}))
    check(any(m["id"] == f"readai:{a}" for m in out["meetings"]), "meetings_pending tool")
    check(meetings.done(conn, f"readai:{a}", 1, "itsalexvacca"), "closed")
    check(not [r for r in meetings.pending(conn, 10) if r["id"] == f"readai:{a}"], "closed meeting no longer pending")
    conn.execute("DELETE FROM meetings WHERE id = ANY(%s)", ([f"readai:{x}" for x in (a, b, c)],))
    conn.commit()
    conn.close()


def test_agency_guard():
    connections.current_user = lambda: "USHR"
    out = json.loads(workspace.gmail_search({"agency": True, "query": "x"}))
    check(not out["success"] and "owner" in out["error"], "non-owner can't use the shared Gmail inbox")
    out = json.loads(workspace.readai_meetings({}))
    check(not out["success"] and out.get("not_connected"), "non-owner falls back to the shared account (not connected here)")
    out = json.loads(workspace.connect_account({"tool": "readai", "for_agency": True}))
    check(not out["success"] and "owner" in out["error"], "non-owner can't connect an agency account")
    connections.current_user = lambda: "UOWNER"
    SENT.clear()
    out = json.loads(workspace.connect_account({"tool": "readai", "for_agency": True}))
    check(out["success"] and SENT and SENT[0][0] == ["UOWNER"] and "/connect/readai?u=agency" in SENT[0][1],
          "owner gets the agency sign-in link by DM")
    out = json.loads(workspace.readai_meetings({"agency": True}))
    check(not out["success"] and out.get("not_connected"), "agency account not connected yet → clear message")


def test_meeting_quota():
    conn = ledger.connect()
    now = time.time()
    a, b, c = (f"Q{uuid.uuid4().hex[:8]}" for _ in range(3))
    iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))  # noqa: E731
    listed = [{"id": x, "title": f"m {x}", "start": iso(now - 20000), "end": iso(now - 18000)} for x in (a, b, c)]

    def one(mid):
        if mid == a:
            raise readai.ReadAIError('Read AI -> HTTP 403: {"error_reason":"out_of_quota"}', 403)
        if mid == b:
            return {"title": "standup", "summary": None, "action_items": []}
        return {"title": "client call", "summary": "s", "action_items": [{"text": "Do it", "owner": None}]}
    SENT.clear()
    stored = meetings.sync(conn, tok="x", fetch_list=lambda: listed, fetch_one=one, now=now)
    check(stored == [f"readai:{c}"], "locked and empty meetings don't wake the agent; others continue")
    check(SENT and "quota" in SENT[0][1] and SENT[0][0] == ["UOWNER"], "owners told about the Read AI quota")
    SENT.clear()
    meetings.sync(conn, tok="x", fetch_list=lambda: listed, fetch_one=one, now=now)
    check(not SENT, "quota warning only once per meeting")
    ids = [f"readai:{x}" for x in (a, b, c)]
    pend = {r["id"] for r in meetings.pending(conn, 50)}
    check(f"readai:{c}" in pend and f"readai:{a}" not in pend and f"readai:{b}" not in pend, "only actionable pending")
    conn.execute("DELETE FROM meetings WHERE id = ANY(%s)", (ids,))
    conn.commit()
    conn.close()


if __name__ == "__main__":
    test_meeting_quota()
    test_readai_oauth()
    test_readai_normalize()
    test_tasks()
    test_meeting_sync()
    test_agency_guard()
    print("all good")


def test_shared_fallback():
    """One rule: own account if connected, else the shared agency one; shared Gmail is owners only."""
    conn = ledger.connect()
    f = connections._fernet()
    for p in ("google", "readai"):
        conn.execute("""INSERT INTO user_connections(slack_user, provider, account, access_enc, expires_at)
                        VALUES ('agency', %s, 'hello@tp.com', %s, now() + interval '1 hour')
                        ON CONFLICT (slack_user, provider) DO UPDATE SET access_enc = EXCLUDED.access_enc,
                        expires_at = EXCLUDED.expires_at""", (p, f.encrypt(f"AGENCY-{p}".encode()).decode()))
    conn.commit()
    seen = []
    workspace.readai.recent = lambda tok, days, limit: seen.append(tok) or []
    connections.current_user = lambda: "USHR"
    out = json.loads(workspace.readai_meetings({}))
    check(out["success"] and seen[-1] == "AGENCY-readai", "teammate without own Read AI uses the shared one")
    out = json.loads(workspace.gmail_search({"query": "x"}))
    check(not out["success"] and "owners only" in out["error"], "teammate can't read the shared Gmail")
    connections.current_user = lambda: "UOWNER"
    tok = []
    workspace._call = lambda *a, **k: tok.append(a[2]) or {"messages": []}
    json.loads(workspace.gmail_search({"query": "x"}))
    check(tok and tok[-1] == "AGENCY-google", "owner without own Google uses the shared inbox")
    conn.execute("DELETE FROM user_connections WHERE slack_user = 'agency'")
    conn.commit()
    conn.close()


if __name__ == "__main__":
    test_shared_fallback()
    print("shared fallback good")
