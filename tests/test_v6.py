"""Offline tests for v6: early-reply radar, post-mortems, leads, content calendar.

    DATABASE_URL=postgresql://... uv run --no-project --with "psycopg[binary]" tests/test_v6.py
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from perpetual_core import agency, leads, ledger, planner, postmortem, radar  # noqa: E402

A, B = "v6alpha", "v6beta"


def check(cond, msg):
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    print(f"ok   {msg}")


def reset(conn):
    for t in ("alerts", "agency_requests", "seen_targets", "postmortems", "outcomes", "leads", "calendar_slots",
              "suggestions", "counters"):
        conn.execute(f"DELETE FROM {t} WHERE client = ANY(%s)", ([A, B],))
    conn.execute("DELETE FROM clients WHERE handle = ANY(%s)", ([A, B],))
    conn.execute("""INSERT INTO clients(handle, display_name, slack_channel, watch_accounts, radar_accounts)
                    VALUES (%s, 'Alpha', 'CA', 'peer1', 'bignick, @BigLeo'), (%s, 'Beta', 'CB', 'bignick', NULL)""", (A, B))
    conn.commit()


def tweet(tid, by, age_h, **kw):
    return {"id": tid, "by": by, "age_h": age_h, "url": f"https://x.com/{by}/status/{tid}", "text": "post", **kw}


def test_radar(conn):
    wanted = radar.client_accounts(conn)
    check(wanted[A] == {"bignick", "bigleo"} and wanted[B] == {"bignick"}, "radar lists (radar_accounts, else watch list)")
    batches = [[tweet("1001", "BigNick", 0.2, likes=40), tweet("1002", "bigleo", 2.0),
                tweet("1003", "bigleo", 0.1, is_reply=True), tweet("1004", "stranger", 0.1)]]
    searched = []

    def fake_search(accts):
        searched.append(accts)
        return batches[0]

    routed = radar.scan(conn, search=fake_search)
    check(sorted(routed) == [(A, "1001"), (B, "1001")], "fresh post routed to every client watching its author only")
    check(len(searched) >= 1 and {"bignick", "bigleo"} <= set(searched[0]) and searched[0].count("bignick") == 1,
          "one search over the union of accounts (shared ones once)")
    reqs = agency.pending_requests(conn, A)
    check(len(reqs) == 1 and reqs[0]["request"].startswith("RADAR"), "radar hit becomes a request for the agent")
    check(radar.scan(conn, search=fake_search) == [], "same tweet never routed twice")
    for i in range(5):
        batches[0] = [tweet(f"20{i}", "bigleo", 0.1)]
        radar.scan(conn, search=fake_search)
    check(radar.alerts_today(conn, A) == radar.DAILY_CAP, "daily cap of 4 alerts per client")
    check(radar.set_accounts(conn, B, add=["@New1"], remove=["bignick"]) == ["new1"], "add/remove radar accounts")


def test_postmortem(conn):
    posts = [tweet("p1", A, 60, views=10000, likes=100, bookmarks=50),
             tweet("p2", A, 70, views=2000, likes=10, bookmarks=5),
             tweet("p3", A, 200, views=4000, likes=40, bookmarks=20),
             tweet("p4", A, 10, views=500),                                  # too young
             tweet("p5", A, 80, views=900, is_reply=True),                   # replies are not judged
             tweet("p6", A, 90, views=30000, likes=300, bookmarks=600, article_title="Big piece")]
    rows = postmortem.prepare(conn, A, fetch=lambda c: posts)
    ids = sorted(r["tweet_id"] for r in rows)
    check(ids == ["p1", "p2", "p6"], "only original posts aged 48h-7d get a post-mortem")
    p1 = next(r for r in rows if r["tweet_id"] == "p1")
    check(p1["format"] == "tweet" and p1["baseline"]["views"] == 4000, "baseline = median of settled tweets")
    check(p1["vs_median"]["views"] == 2.5, "ratio vs median")
    check(next(r for r in rows if r["tweet_id"] == "p6")["format"] == "article", "articles judged against articles")
    postmortem.judge(conn, A, "p1", "win", "contrarian hook, posted 9am", "repeat the hook shape")
    check(len(postmortem.prepare(conn, A, fetch=lambda c: posts)) == 2, "judged posts drop out; nothing reopens")
    check(postmortem.recent(conn, A)[0]["verdict"] == "win", "recent post-mortems readable")


def test_leads(conn):
    saved = leads.save(conn, A, [{"handle": "@Founder1", "fit_score": 9}, {"handle": "ops2", "fit_score": 7},
                                 {"handle": A}])
    check([s["code"] for s in saved] == ["L1", "L2"], "leads get L codes; the client itself is skipped")
    check(leads.save(conn, A, [{"handle": "founder1"}, {"handle": "new3"}])[0]["code"] == "L3", "duplicates skipped")
    check(leads.known(conn, A, ["@FOUNDER1", "zzz"]) == ["founder1"], "known handles")
    check(leads.update(conn, A, "l2", "not_a_fit", "too small")["status"] == "not_a_fit", "status update")
    check(len(leads.listing(conn, A, status="new")) == 2, "list by status")


def test_calendar(conn):
    items = [{"kind": "tweet", "text": f"draft {i}"} for i in range(3)]
    codes = [s["code"] for s in ledger.save(conn, A, items)]
    now = datetime.now(timezone.utc)
    planner.save(conn, A, [{"code": codes[0], "slot_at": (now - timedelta(minutes=5)).isoformat()},
                           {"code": codes[1], "slot_at": (now - timedelta(minutes=5)).isoformat()},
                           {"code": codes[2], "slot_at": (now + timedelta(days=1)).isoformat()}])
    check(planner.due(conn, A) == [], "planned (unapproved) slots never remind")
    ledger.feedback(conn, A, codes[1], "approve", by="Sai")
    check([r["code"] for r in planner.due(conn, A)] == [codes[1]], "'ok T2' approves its slot")
    check(planner.due(conn, A) == [], "a reminder is sent once")
    check(planner.approve(conn, A) == [codes[2]], "'ok week' approves future planned slots")
    check([r["code"] for r in planner.due(conn, A, now=now + timedelta(days=1, minutes=1))] == [codes[2]],
          "approved slot reminds at its time")
    try:
        planner.save(conn, A, [{"code": codes[0], "slot_at": "2026-10-22T09:00:00"}])
        check(False, "time without zone rejected")
    except ValueError:
        check(True, "time without zone rejected")


if __name__ == "__main__":
    if not os.environ.get("DATABASE_URL"):
        raise SystemExit("set DATABASE_URL")
    conn = ledger.connect()
    ledger.ensure_schema(conn)
    reset(conn)
    test_radar(conn)
    test_postmortem(conn)
    test_leads(conn)
    test_calendar(conn)
    reset(conn)
    conn.close()
    print("all good")
