"""Offline tests for the daily standup (timing, DMs, answers, summary, settings).

    DATABASE_URL=postgresql://... uv run --no-project --with "psycopg[binary]" tests/test_standup.py
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
os.environ.update(HQ_SLACK_CHANNEL="CHQ", OWNER_SLACK_IDS="UOWNER")

from perpetual_core import connections, ledger, notify, slackui, standup, tasks  # noqa: E402

PEOPLE = [
    {"id": "UOWNER", "name": "sai", "real_name": "Sai Prashanth", "display_name": "sai", "email": "s@tp.com"},
    {"id": "USHR", "name": "shravya", "real_name": "Shravya K", "display_name": "shravya", "email": "sh@tp.com"},
    {"id": "UNAGA", "name": "naga", "real_name": "Naga R", "display_name": "naga", "email": "n@tp.com"},
]
slackui.people = lambda: PEOPLE
slackui.person_name = lambda uid: next(p["real_name"] for p in PEOPLE if p["id"] == uid)
DMS, POSTS = [], []
notify.dm = lambda ids, text: DMS.append((list(ids), text)) or len(ids)
slackui.slack = lambda method, **kw: POSTS.append((method, kw)) or {"ok": True}
IST = ZoneInfo("Asia/Kolkata")


def check(cond, msg):
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    print(f"ok   {msg}")


def reset(conn):
    for t in ("standup_config", "standup_runs", "standup_answers"):
        conn.execute(f"DELETE FROM {t}")
    conn.commit()


def main():
    conn = ledger.connect()
    ledger.ensure_schema(conn)
    reset(conn)
    cfg = standup.config(conn)
    check(cfg["time"] == "11:30" and cfg["timezone"] == "Asia/Kolkata" and cfg["lead_minutes"] == 45, "defaults")
    wed = datetime(2026, 11, 4, 10, 40, tzinfo=IST)
    check(standup.tick(conn, wed, PEOPLE) is None and not DMS, "nothing before 10:45")
    check(standup.tick(conn, wed.replace(minute=45), PEOPLE) == "asked" and len(DMS) == 3, "10:45: everyone DMed")
    check("11:30" in DMS[0][1] and "Yesterday" in DMS[0][1], "question names the call time")
    DMS.clear()
    check(standup.tick(conn, wed.replace(minute=50), PEOPLE) is None and not DMS, "asked only once")

    connections.current_user = lambda: "USHR"
    out = json.loads(standup.standup_answer({"yesterday": ["Shipped Alex thread"], "today": ["Write Q4 plan"],
                                             "blockers": ["Need Alex's analytics access"]}))
    check(out["success"], "answer saved")
    conn.execute("UPDATE standup_answers SET day = %s WHERE slack_user = 'USHR'", (wed.date(),))
    conn.commit()
    standup.save_answer(conn, "UNAGA", "Naga R", ["x"], ["Calls"], ["none"], day=wed.date())
    check(standup.tick(conn, wed.replace(hour=11, minute=25), PEOPLE) == "summary", "11:25 summary")
    method, kw = [p for p in POSTS if p[0] == "chat.postMessage"][-1]
    text = kw["text"]
    check(method == "chat.postMessage" and kw["channel"] == "CHQ", "summary goes to the reports channel")
    check("2/3 answered" in text and "Write Q4 plan" in text, "summary lists plans")
    check("Need Alex's analytics access" in text and "Naga R:* none" not in text, "real blockers only")
    check("No answer yet:* Sai" in text, "who hasn't answered")
    POSTS.clear()
    check(standup.tick(conn, wed.replace(hour=11, minute=28), PEOPLE) is None
          and not [p for p in POSTS if p[0] == "chat.postMessage"], "summary once")
    sat = datetime(2026, 11, 7, 10, 50, tzinfo=IST)
    check(standup.tick(conn, sat, PEOPLE) is None, "no standup on Saturday")

    # settings by chat
    tasks.resolve_owner = lambda who, people=None: {"naga": ("UNAGA", "Naga R", None),
                                                    "shravya": ("USHR", "Shravya K", None)}[who.lower()]
    out = json.loads(standup.standup_settings({"time": "11.00", "remove_members": ["Naga"], "channel": "<#CSTAND|standup>"}))
    check(out["success"] and out["time"] == "11:00" and out["questions_at"] == "10:15" and out["summary_at"] == "10:55",
          "time change moves questions and summary")
    check(out["channel"] == "CSTAND" and "Naga R" not in out["members"] and len(out["members"]) == 2, "channel + remove")
    out = json.loads(standup.standup_settings({"only_members": ["Shravya"]}))
    check(out["members"] == ["Shravya K"], "only chosen members")
    out = json.loads(standup.standup_settings({"enabled": False}))
    check(out["enabled"] is False and standup.tick(conn, datetime(2026, 11, 5, 10, 20, tzinfo=IST), PEOPLE) is None,
          "paused standup does nothing")
    check(not json.loads(standup.standup_settings({"timezone": "Mars/Base"}))["success"], "bad timezone refused")
    reset(conn)
    conn.close()
    print("all good")


if __name__ == "__main__":
    main()
