"""Offline tests for client goals (pace math, off-pace, tools) and the weekly team scorecard.

    DATABASE_URL=postgresql://... uv run --no-project --python 3.12 --with "psycopg[binary]" \
        --with cryptography --with tzdata tests/test_goals_scorecard.py
"""

import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
os.environ.update(HQ_SLACK_CHANNEL="CHQ", OWNER_SLACK_IDS="UOWNER")

from perpetual_core import connections, goals, ledger, notify, scorecard, slackui  # noqa: E402

C, EMPTY = "zz_goaltest", "zz_goaltest_empty"
UA, UB = "UZZTESTA", "UZZTESTB"
PEOPLE = [{"id": UA, "name": "alice", "real_name": "Alice Test", "display_name": "alice"},
          {"id": UB, "name": "bob", "real_name": "Bob Test", "display_name": "bob"}]
slackui.people = lambda: PEOPLE
DMS, POSTS, DOCS = [], [], []
notify.dm = lambda ids, text: DMS.append((list(ids), text)) or len(ids)
slackui.slack = lambda method, **kw: POSTS.append((method, kw)) or {"ok": True}
slackui.publish_doc = lambda conn, client, title, md, summary, kind="answer", asked_by=None: (
    DOCS.append((client, title, kind)) or {"mode": "canvas", "link": "https://x/canvas", "channel": "CX"})
connections.current_user = lambda: UA
IST = ZoneInfo("Asia/Kolkata")
TODAY = date(2026, 11, 4)


def check(cond, msg):
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    print(f"ok   {msg}")


def cleanup(conn):
    conn.execute("DELETE FROM metrics_daily WHERE client IN (%s, %s)", (C, EMPTY))
    conn.execute("DELETE FROM client_goals WHERE client IN (%s, %s)", (C, EMPTY))
    conn.execute("DELETE FROM team_tasks WHERE owner_slack IN (%s, %s)", (UA, UB))
    conn.execute("DELETE FROM standup_answers WHERE slack_user IN (%s, %s)", (UA, UB))
    conn.execute("DELETE FROM standup_runs WHERE day BETWEEN '2030-12-28' AND '2031-01-03'")
    conn.execute("DELETE FROM scorecard_runs WHERE week_end = '2031-01-03'")
    conn.commit()


def seed_metrics(conn):
    # 2026-09-01 .. 2026-11-03: +10 followers/day, 10k views, 2 posts, 100 likes, 60 replies, 10 reposts, 30 bookmarks
    d, i = date(2026, 9, 1), 0
    while d <= date(2026, 11, 3):
        conn.execute("""INSERT INTO metrics_daily(client, day, followers, posts, likes, replies, reposts, bookmarks, views)
                        VALUES (%s,%s,%s,2,100,60,10,30,10000)""", (C, d, 1000 + 10 * i))
        d, i = d + timedelta(days=1), i + 1
    conn.commit()


def goals_part(conn):
    seed_metrics(conn)
    g = lambda *a, **k: goals.evaluate(conn, goals.set_goal(conn, *a, **k, today=TODAY), TODAY)  # noqa: E731
    # total followers: 30 of 88 days elapsed, +290 so far
    r = g(C, "followers_gain", 1000, "total", "2026-12-31", "2026-10-05")
    check(r["actual"] == 290 and round(r["expected"], 1) == 340.9 and r["status"] == "behind", "total followers behind (85%)")
    check(r["projected"] == 851 and r["needed_per_day"] == 12.2, "projection + needed per day")
    check(g(C, "followers_gain", 900, "total", "2026-12-31", "2026-10-05")["status"] == "on_track", "total followers on track")
    check(g(C, "followers_gain", 600, "total", "2026-12-31", "2026-10-05")["status"] == "ahead", "total followers ahead")
    r = g(C, "impressions", 300000, "per_month", start_date="2026-09-01")
    check(r["actual"] == 300000 and r["pct"] == 100 and r["status"] == "on_track", "impressions per month on track")
    check(g(C, "impressions", 500000, "per_month", start_date="2026-09-01")["status"] == "behind", "impressions per month behind")
    r = g(C, "replies", 40, "per_post", start_date="2026-09-01")
    check(r["actual"] == 30 and r["status"] == "behind", "replies per post behind (30 vs 40)")
    check(g(C, "replies", 25, "per_post", start_date="2026-09-01")["status"] == "ahead", "replies per post ahead")
    r = g(C, "posts", 2, "per_day", start_date="2026-09-01")
    check(r["actual"] == 14 and r["expected"] == 14 and r["status"] == "on_track", "2 posts/day on track")
    r = g(C, "likes", 1000, "per_week", start_date="2026-09-01")
    check(r["actual"] == 700 and r["status"] == "behind", "likes per week behind")
    r = g(C, "engagement_rate", 2, "per_month", start_date="2026-09-01")
    check(r["actual"] == 2.0 and r["status"] == "on_track", "engagement rate 2% on track")
    r = g(C, "followers_gain", 100, "per_week", start_date="2026-11-01")
    check(r["actual"] == 20 and r["expected"] == round(100 * 3 / 7, 2), "new goal: window clipped to start")
    r = g(C, "posts", 2, "per_day", start_date="2026-11-10")
    check(r["status"] == "no_data" and "starts" in r["reason"], "future goal: no_data")
    r = g(EMPTY, "impressions", 100000, "per_month", start_date="2026-09-01")
    check(r["status"] == "no_data" and r["actual"] is None, "no metrics: no_data")
    try:
        goals.set_goal(conn, C, "followers_gain", 10, "total")
        check(False, "total goal without end date refused")
    except ValueError:
        check(True, "total goal without end date refused")
    conn.commit()
    off = goals.off_pace(conn, TODAY)
    mine = off.get(C, [])
    check(len(mine) == 5 and all(x["status"] == "behind" for x in mine) and EMPTY not in off, "off_pace: only behind goals")
    txt = goals.text(goals.progress(conn, C, TODAY))
    check(txt.splitlines()[0].startswith("⚠️") and "no data" in txt and "🚀" in txt, "goals text: behind first")

    # tools
    out = json.loads(goals.TOOLS[0][1]({"client": "@ZZ_GoalTest", "metric": "bookmarks", "target": 50,
                                        "period": "per_week"}))
    check(out["success"] and out["goal"]["client"] == C and out["goal"]["created_by"] == UA, "goals_set tool")
    gid = out["goal"]["id"]
    out = json.loads(goals.TOOLS[1][1]({"client": C}))
    check(out["success"] and out["count"] == 13, "goals_list tool")
    out = json.loads(goals.TOOLS[2][1]({"client": C}))
    check(out["success"] and out["count"] == 13 and "text" in out, "goals_progress tool")
    check(not json.loads(goals.TOOLS[0][1]({"client": C, "metric": "vibes", "target": 1, "period": "total"}))["success"],
          "goals_set refuses unknown metric")
    out = json.loads(goals.TOOLS[3][1]({"id": gid}))
    check(out["success"] and json.loads(goals.TOOLS[1][1]({"client": C}))["count"] == 12, "goals_remove tool")
    out = json.loads(goals.TOOLS[4][1]({"client": C, "title": "Results: October", "markdown": "# x", "summary": "s"}))
    check(out["success"] and DOCS[-1] == (C, "Results: October", "report"), "results_publish tool → internal canvas")


def scorecard_part(conn):
    week_end = scorecard.week_end_for(date(2031, 1, 6))
    check(week_end == date(2031, 1, 3) and scorecard.week_end_for(date(2031, 1, 3)) == week_end, "week ends Friday")
    check(scorecard.week_end_for(date(2031, 1, 6), 1) == date(2030, 12, 27), "weeks_back")
    ins = """INSERT INTO team_tasks(title, owner_slack, owner_name, due, status, done_at) VALUES (%s,%s,%s,%s,%s,%s)"""
    for title, owner, due, status, done_at in [
        ("on time", UA, "2031-01-02", "done", "2031-01-01 10:00+05:30"),
        ("late", UA, "2030-12-30", "done", "2031-01-02 10:00+05:30"),
        ("no due", UA, None, "done", "2031-01-03 01:00+05:30"),          # 2 Jan in UTC, 3 Jan in IST
        ("last week", UA, None, "done", "2030-12-20 10:00+05:30"),
        ("overdue", UA, "2030-12-31", "open", None),
        ("stuck", UA, None, "blocked", None),
        ("b1", UB, "2030-12-29", "open", None), ("b2", UB, "2030-12-30", "doing", None),
        ("b3", UB, "2031-01-01", "open", None), ("b4", UB, "2031-01-03", "open", None),  # due today: not overdue
    ]:
        conn.execute(ins, (title, owner, owner[-1], due, status, done_at))
    for d in ("2030-12-29", "2030-12-30", "2030-12-31"):
        conn.execute("INSERT INTO standup_runs(day, asked_at) VALUES (%s, now()) ON CONFLICT (day) DO UPDATE SET asked_at = now()", (d,))
    for d in ("2030-12-29", "2030-12-31"):
        conn.execute("INSERT INTO standup_answers(day, slack_user, name) VALUES (%s,%s,'Alice')", (d, UA))
    conn.commit()
    rows = {r["id"]: r for r in scorecard.compute(conn, week_end, PEOPLE)}
    a, b = rows[UA], rows[UB]
    check(a["done"] == 3 and a["with_due"] == 2 and a["on_time_rate"] == 50, "Alice: 3 done, 50% on time")
    check(a["overdue"] == 1 and a["blocked"] == 1, "Alice: 1 overdue, 1 blocked")
    check(a["standups_answered"] == 2 and a["standups_asked"] == 3 and a["standup_rate"] == 67, "Alice: standups 2/3")
    check(b["done"] == 0 and b["on_time_rate"] is None and b["overdue"] == 3 and b["standup_rate"] == 0, "Bob: 3 overdue, no standups")
    txt = scorecard.text(list(rows.values()), week_end)
    check(len(txt.splitlines()) <= 25 and "Alice Test* — ✅ 3 · ⏱ 50% · ⏰ 1 · 🚧 1 · 🗣 2/3" in txt, "scorecard line")
    check("Most done: *Alice Test*" in txt and "3+ overdue: Bob (3)" in txt and "Missed most standups: Bob" in txt
          and "Blocked, needs help: Alice (1)" in txt, "highlights")
    many = [{**a, "name": f"P{i}"} for i in range(40)]
    check(len(scorecard.text(many, week_end).splitlines()) <= 25 and "more" in scorecard.text(many, week_end), "≤ 25 lines")

    POSTS.clear()
    fri = datetime(2031, 1, 3, 17, 0, tzinfo=IST)
    check(scorecard.tick(conn, fri, PEOPLE) is None and not POSTS, "not before 18:00 Friday")
    check(scorecard.tick(conn, datetime(2031, 1, 2, 19, 0, tzinfo=IST), PEOPLE) is None, "not on Thursday")
    out = scorecard.tick(conn, fri.replace(hour=18, minute=5), PEOPLE)
    check(out and POSTS[-1][0] == "chat.postMessage" and POSTS[-1][1]["channel"] == "CHQ", "Friday 18:05 → reports channel")
    check(scorecard.tick(conn, fri.replace(hour=19), PEOPLE) is None and len(POSTS) == 1, "posted once per week")
    check(scorecard.tick(conn, datetime(2031, 1, 3, 12, 45, tzinfo=ZoneInfo("UTC")), PEOPLE) is None,
          "12:45 UTC Friday (= 18:15 IST) already posted")
    out = json.loads(scorecard.team_scorecard({"weeks_back": 0}))
    check(out["success"] and "Team scorecard" in out["text"] and isinstance(out["members"], list), "team_scorecard tool")


def main():
    conn = ledger.connect()
    ledger.ensure_schema(conn)
    conn.execute(goals.SCHEMA_SQL)
    conn.execute(scorecard.SCHEMA_SQL)
    conn.commit()
    from perpetual_core import standup
    cfg = standup.config(conn)
    saved = (cfg["members"], cfg["excluded"], cfg["timezone"])
    conn.execute("UPDATE standup_config SET members = NULL, excluded = NULL, timezone = 'Asia/Kolkata' WHERE id = 1")
    conn.commit()
    cleanup(conn)
    try:
        goals_part(conn)
        scorecard_part(conn)
    finally:
        conn.rollback()
        cleanup(conn)
        conn.execute("UPDATE standup_config SET members = %s, excluded = %s, timezone = %s WHERE id = 1",
                     (json.dumps(saved[0]) if saved[0] is not None else None,
                      json.dumps(saved[1]) if saved[1] is not None else None, saved[2]))
        conn.commit()
        conn.close()
    print("all good")


if __name__ == "__main__":
    main()
