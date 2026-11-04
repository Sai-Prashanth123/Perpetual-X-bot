"""Offline tests for the Notion Agency OS sync (fake Notion API).

    DATABASE_URL=postgresql://... uv run --no-project --with "psycopg[binary]" --with tzdata tests/test_notion_os.py
"""

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
os.environ.update(OWNER_SLACK_IDS="UOWNER", PX_DAILY_LIST="off")

from perpetual_core import ledger, notify, notion_os, tasks  # noqa: E402

PEOPLE = [{"id": "UOWNER0001", "name": "prashanth", "real_name": "Sai Prashanth", "display_name": "Prashanth", "email": ""},
          {"id": "USHRAVYA01", "name": "shravya", "real_name": "Shravya", "display_name": "Shravya", "email": ""}]
notify.dm = lambda ids, text: len(ids)


class FakeNotion:
    def __init__(self):
        self.pages, self.calls, self.n = {}, [], 0

    def __call__(self, method, path, body):
        self.calls.append((method, path))
        if method == "POST" and path == "/pages":
            self.n += 1
            pid = f"page{self.n}"
            self.pages[pid] = {"id": pid, "url": f"https://notion.so/{pid}", "parent": body["parent"],
                               "properties": body["properties"], "last_edited_time": _now()}
            return self.pages[pid]
        if method == "POST" and path == "/databases":
            self.n += 1
            return {"id": f"db{self.n}"}
        if method == "PATCH" and path.startswith("/pages/"):
            p = self.pages[path.split("/")[2]]
            p["properties"].update(body["properties"])
            p["last_edited_time"] = _now()
            return p
        if method == "POST" and path.endswith("/query"):
            db = path.split("/")[2]
            return {"results": [p for p in self.pages.values() if p["parent"].get("database_id") == db]}
        raise AssertionError((method, path))

    def human_edit(self, pid, **props):
        self.pages[pid]["properties"].update(props)
        self.pages[pid]["last_edited_time"] = _now()


def _now():
    return datetime.now(timezone.utc).isoformat()


def check(cond, msg):
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    print(f"ok   {msg}")


def main():
    conn = ledger.connect()
    ledger.ensure_schema(conn)
    conn.execute(notion_os.SCHEMA_SQL)
    conn.execute("DELETE FROM notion_os")
    for t in ("team_tasks", "meetings", "clients"):
        conn.execute(f"UPDATE {t} SET notion_page_id = 'old', notion_synced_at = now() + interval '1 day'")
    conn.commit()
    fake = FakeNotion()
    tag = uuid.uuid4().hex[:6]
    added, _ = tasks.add(conn, [{"title": f"Write plan {tag}", "owner": "Shravya", "due": "2026-11-05"}],
                         created_by="UOWNER0001", people=PEOPLE, notify_owners=False)

    out = notion_os.sync(conn, tok="t", call=fake, people=PEOPLE)
    st = notion_os.state(conn)
    top = [c for c in fake.calls if c == ("POST", "/pages")][0]
    check(st["page_id"] == "page1" and fake.pages["page1"]["parent"] == {"type": "workspace", "workspace": True},
          "Agency OS page created at the top of the workspace")
    check(set(st["dbs"]) >= {"tasks", "meetings", "clients"}, "Tasks, Meetings, Clients databases created")
    check(out["pushed"]["tasks"] == 1, "new task pushed")
    row = conn.execute("SELECT * FROM team_tasks WHERE title = %s", (f"Write plan {tag}",)).fetchone()
    page = fake.pages[row["notion_page_id"]]
    check(page["properties"]["Owner"]["rich_text"][0]["text"]["content"] == "Shravya"
          and page["properties"]["Status"]["select"]["name"] == "open", "task page has owner + status")
    check(notion_os.ensure(conn, "t", fake)["page_id"] == "page1" and
          len([c for c in fake.calls if c == ("POST", "/databases")]) == len(st["dbs"]), "setup runs once")

    out = notion_os.sync(conn, tok="t", call=fake, people=PEOPLE)
    check(out["pushed"].get("tasks", 0) == 0 and out["pulled"] == {"updated": 0, "added": 0},
          "no changes → nothing written, own writes not mistaken for human edits")

    fake.human_edit(row["notion_page_id"], Status={"select": {"name": "done"}}, Due={"date": {"start": "2026-11-07"}})
    out = notion_os.sync(conn, tok="t", call=fake, people=PEOPLE)
    row = conn.execute("SELECT * FROM team_tasks WHERE id = %s", (row["id"],)).fetchone()
    check(out["pulled"]["updated"] == 1 and row["status"] == "done" and row["due"].isoformat() == "2026-11-07",
          "status + due edited in Notion flow back")
    check(out["pushed"].get("tasks", 0) == 0, "pulled edit isn't pushed back")

    tasks.update(conn, tasks.code(row), status="open", note="reopened")
    out = notion_os.sync(conn, tok="t", call=fake, people=PEOPLE)
    check(out["pushed"]["tasks"] == 1 and fake.pages[row["notion_page_id"]]["properties"]["Status"]["select"]["name"] == "open",
          "Slack change pushed to Notion")

    # a row added by hand in Notion becomes a task
    db = notion_os.state(conn)["dbs"]["tasks"]
    fake.n += 1
    fake.pages["hand"] = {"id": "hand", "parent": {"database_id": db}, "last_edited_time": _now(), "properties": {
        "Name": {"title": [{"plain_text": f"Hand task {tag}"}]}, "Owner": {"rich_text": [{"plain_text": "Sai"}]},
        "Code": {"rich_text": []}, "Status": {"select": None}, "Due": {"date": {"start": "2026-11-06"}}}}
    out = notion_os.sync(conn, tok="t", call=fake, people=PEOPLE)
    hand = conn.execute("SELECT * FROM team_tasks WHERE title = %s", (f"Hand task {tag}",)).fetchone()
    check(out["pulled"]["added"] == 1 and hand and hand["owner_slack"] == "UOWNER0001" and hand["source"] == "notion",
          "row added in Notion becomes an owned task")
    check(fake.pages["hand"]["properties"]["Code"]["rich_text"][0]["text"]["content"] == tasks.code(hand),
          "Notion row gets its TASK code")

    conn.execute("DELETE FROM team_tasks WHERE title LIKE %s", (f"%{tag}",))
    conn.execute("DELETE FROM notion_os")
    for t in ("team_tasks", "meetings", "clients"):
        conn.execute(f"UPDATE {t} SET notion_page_id = NULL, notion_synced_at = NULL WHERE notion_page_id = 'old'")
    conn.commit()
    conn.close()
    print("all good")


if __name__ == "__main__":
    main()
