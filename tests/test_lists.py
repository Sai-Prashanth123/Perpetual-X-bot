"""Offline tests: the team's daily Slack List, check-in, EOD and the intro (fake Slack API).

    DATABASE_URL=postgresql://... uv run --no-project --with "psycopg[binary]" --with tzdata tests/test_lists.py
"""

import os
import sys
import uuid
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
os.environ.update(HQ_SLACK_CHANNEL="CHQ", OWNER_SLACK_IDS="UOWNER")

from perpetual_core import ledger, lists, notify, slackui, standup, tasks  # noqa: E402

PEOPLE = [
    {"id": "UOWNER", "name": "prashanth", "real_name": "Sai Prashanth", "display_name": "Prashanth", "email": ""},
    {"id": "USHR", "name": "shravya", "real_name": "Shravya", "display_name": "Shravya", "email": ""},
    {"id": "UTOAN", "name": "info", "real_name": "Toan Truong", "display_name": "", "email": ""},
]
slackui.people = lambda: PEOPLE
slackui.person_name = lambda uid: next((p["real_name"] for p in PEOPLE if p["id"] == uid), uid)
DMS = []
notify.dm = lambda ids, text: DMS.append((list(ids), text)) or len(ids)


class FakeSlack:
    def __init__(self):
        self.calls, self.items, self.n = [], {}, 0

    def __call__(self, method, **kw):
        self.calls.append((method, kw))
        if method == "slackLists.create":
            assert kw["todo_mode"] is True and kw["name"]
            return {"ok": True, "list_id": "FLIST1", "list_metadata": {"schema": [
                {"key": "name", "name": "Name", "is_primary_column": True, "type": "text", "id": "ColName"},
                {"key": "todo_completed", "type": "todo_completed", "id": "Col00"},
                {"key": "todo_assignee", "type": "todo_assignee", "id": "Col01"},
                {"key": "todo_due_date", "type": "todo_due_date", "id": "Col02"}]}}
        if method == "files.info":
            return {"ok": True, "file": {"permalink": "https://tp.slack.com/lists/T1/FLIST1"}}
        if method == "slackLists.items.create":
            self.n += 1
            rid = f"Rec{self.n}"
            f = {x["column_id"]: x for x in kw["initial_fields"]}
            self.items[rid] = {"id": rid, "fields": [
                {"column_id": "ColName", "text": f["ColName"]["rich_text"][0]["elements"][0]["elements"][0]["text"]},
                {"column_id": "Col00", "checkbox": False},
                {"column_id": "Col01", "user": (f.get("Col01") or {}).get("user", [])},
                {"column_id": "Col02", "date": (f.get("Col02") or {}).get("date", [])}]}
            return {"ok": True, "item": {"id": rid}}
        if method == "slackLists.items.update":
            for c in kw["cells"]:
                for fld in self.items[c["row_id"]]["fields"]:
                    if fld["column_id"] == c["column_id"]:
                        fld["checkbox"] = c["checkbox"]
            return {"ok": True}
        if method == "slackLists.items.list":
            return {"ok": True, "items": list(self.items.values()), "response_metadata": {"next_cursor": ""}}
        return {"ok": True}


def check(cond, msg):
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    print(f"ok   {msg}")


def main():
    fake = FakeSlack()
    slackui.slack = fake
    conn = ledger.connect()
    ledger.ensure_schema(conn)
    conn.execute("DELETE FROM team_tasks WHERE list_id = 'FLIST1'")
    for t in ("standup_config", "standup_runs", "standup_answers", "daily_lists"):
        conn.execute(f"DELETE FROM {t}")
    conn.commit()
    standup.update_config(conn, members=["UOWNER", "USHR"], excluded=[])
    tag = uuid.uuid4().hex[:6]
    day = date.today()
    check(lists.list_name(date(2026, 11, 3)) == "3-11-2026", "list named like the team's (3-11-2026)")

    added, _ = tasks.add(conn, [{"title": f"Write Q4 plan {tag}", "owner": "Shravya", "due": "today"}],
                         created_by="UOWNER", people=PEOPLE)
    create = [c for c in fake.calls if c[0] == "slackLists.create"]
    check(len(create) == 1, "today's List created on first task")
    share = [c for c in fake.calls if c[0] == "slackLists.access.set"]
    check(share and share[0][1]["user_ids"] == ["UOWNER", "USHR"] and share[0][1]["access_level"] == "write",
          "List shared with the team only (not Toan)")
    item = fake.items["Rec1"]
    check(item["fields"][0]["text"].startswith("Write Q4 plan") and item["fields"][2]["user"] == ["USHR"]
          and item["fields"][3]["date"] == [day.isoformat()], "task row has name, assignee, due date")
    tasks.add(conn, [{"title": f"Second {tag}", "owner": "Sai"}], created_by="UOWNER", people=PEOPLE)
    check(len([c for c in fake.calls if c[0] == "slackLists.create"]) == 1, "same List reused all day")

    tasks.update(conn, added[0]["code"], by="USHR", status="done")
    check(fake.items["Rec1"]["fields"][1]["checkbox"] is True, "done in chat ticks the List box")

    # someone ticks the second task in Slack, and adds a row by hand
    fake.items["Rec2"]["fields"][1]["checkbox"] = True
    fake.items["RecHand"] = {"id": "RecHand", "created_by": "USHR", "fields": [
        {"column_id": "ColName", "text": f"Hand-added {tag}"}, {"column_id": "Col00", "checkbox": False},
        {"column_id": "Col01", "user": ["USHR"]}, {"column_id": "Col02", "date": [day.isoformat()]}]}
    res = lists.sync(conn, day)
    check(len(res["done"]) == 1 and len(res["added"]) == 1, "List sync: ticked → done, hand row → task")
    hand = conn.execute("SELECT * FROM team_tasks WHERE title = %s", (f"Hand-added {tag}",)).fetchone()
    check(hand and hand["owner_slack"] == "USHR" and hand["source"] == "list", "hand-added row owned + sourced")
    check(lists.sync(conn, day) == {"done": [], "added": []}, "sync is idempotent")

    # blocked → owners told
    DMS.clear()
    tasks.update(conn, tasks.code(hand), by="USHR", status="blocked", note="need analytics access")
    check(DMS and DMS[0][0] == ["UOWNER"] and "blocked" in DMS[0][1] and "analytics" in DMS[0][1], "blocker DMs Sai")

    # check-in and EOD timing (Asia/Kolkata)
    IST = ZoneInfo("Asia/Kolkata")
    DMS.clear()
    t = datetime(day.year, day.month, day.day, 15, 2, tzinfo=IST)
    conn.execute("DELETE FROM standup_runs WHERE day = %s", (day,))
    conn.commit()
    standup.config(conn)
    did = standup.tick(conn, t, PEOPLE) if standup.is_day(standup.config(conn), day) else "checkin"
    if standup.is_day(standup.config(conn), day):
        check(did == "checkin" and any("check-in" in d[1] for d in DMS), "15:00 check-in DMs people with open tasks")
        DMS.clear()
        standup.tick(conn, t.replace(hour=20, minute=20), PEOPLE)
        check(any("End of day" in d[1] for d in DMS), "20:15 EOD DM")
    DMS.clear()
    check(standup.send_intro(conn, PEOPLE) == 2 and "Agency Bot" in DMS[0][1], "intro to the 2 team members")
    check(standup.send_intro(conn, PEOPLE) == 0, "intro only once")

    conn.execute("DELETE FROM team_tasks WHERE title LIKE %s", (f"%{tag}",))
    for t in ("standup_config", "standup_runs", "standup_answers", "daily_lists"):
        conn.execute(f"DELETE FROM {t}")
    conn.commit()
    conn.close()
    print("all good")


if __name__ == "__main__":
    main()
