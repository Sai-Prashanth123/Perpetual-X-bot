"""Offline tests for the trend radar (trends.scan) with a fake search.

    DATABASE_URL=postgresql://... uv run --no-project --with "psycopg[binary]" tests/test_trends.py
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from perpetual_core import agency, ledger, trends  # noqa: E402

A, B, C, D = "trdalpha", "trdbeta", "trdoff", "trdnochan"
ALL = [A, B, C, D]


def check(cond, msg):
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    print(f"ok   {msg}")


def reset(conn):
    for t in ("alerts", "agency_requests", "seen_targets"):
        conn.execute(f"DELETE FROM {t} WHERE client = ANY(%s)", (ALL,))
    conn.execute("DELETE FROM clients WHERE handle = ANY(%s)", (ALL,))
    conn.execute("""INSERT INTO clients(handle, display_name, slack_channel, niche_topics) VALUES
                    (%s, 'Alpha', 'CA', 'Clay GTM, cold email'), (%s, 'Beta', 'CB', 'AI agents'),
                    (%s, 'Off', 'CC', 'AI agents'), (%s, 'NoChan', NULL, 'AI agents')""", (A, B, C, D))
    conn.execute("UPDATE clients SET trend_enabled = false WHERE handle = %s", (C,))
    conn.commit()


def tweet(tid, by, age_h, likes_per_h, **kw):
    return {"id": tid, "by": by, "age_h": age_h, "likes_per_h": likes_per_h, "likes": round(age_h * likes_per_h),
            "url": f"https://x.com/{by}/status/{tid}", "text": "post", **kw}


def test_scan(conn):
    topics = trends.client_topics(conn)
    check(topics.get(A) == ["Clay GTM", "cold email"] and topics.get(B) == ["AI agents"],
          "niche phrases per live client")
    check(C not in topics and D not in topics, "trend_enabled off / no channel are skipped")

    pools = {A: [tweet("t1", "bigvoice", 1.0, 400),                 # rising: kept
                 tweet("t2", "bigvoice", 5.0, 400),                 # too old
                 tweet("t3", "bigvoice", 0.5, 80),                  # too slow
                 tweet("t4", A.upper(), 0.5, 900),                  # the client's own post
                 tweet("t5", "other", 0.5, 900, is_reply=True)],    # replies skipped
             B: [tweet("t1", "bigvoice", 1.0, 400)]}
    calls = []

    def fake_search(ts):
        calls.append(ts)
        if ts == ["AI agents"]:
            return pools[B]
        if ts == ["Clay GTM", "cold email"]:
            return pools[A]
        return []

    routed = trends.scan(conn, search=fake_search)
    mine = sorted(r for r in routed if r[0] in ALL)
    check(mine == [(A, "t1"), (B, "t1")], "only young, fast-rising, not-own, non-reply posts routed")
    check(["Clay GTM", "cold email"] in calls and calls.count(["Clay GTM", "cold email"]) == 1,
          "one batched search per client")
    reqs = agency.pending_requests(conn, A)
    check(len(reqs) == 1 and reqs[0]["request"].startswith("TREND") and "trend-jack" in reqs[0]["request"]
          and "Tweet id: t1" in reqs[0]["request"], "trend hit becomes a TREND request for the agent")
    check(not [r for r in trends.scan(conn, search=fake_search) if r[0] in ALL], "same tweet never routed twice")

    pools[A] = [tweet("n1", "x1", 0.5, 300), tweet("n2", "x2", 0.5, 500), tweet("n3", "x3", 0.5, 200)]
    got = [r for r in trends.scan(conn, search=fake_search) if r[0] == A]
    check(got == [(A, "n2")], "fastest first, stops at the daily cap")
    check(trends.alerts_today(conn, A) == trends.DAILY_CAP, "daily cap of 2 trend alerts per client")
    calls.clear()
    trends.scan(conn, search=fake_search)
    check(["Clay GTM", "cold email"] not in calls, "capped client is not searched (saves credits)")

    def flaky(ts):
        if ts == ["AI agents"]:
            raise RuntimeError("console down")
        return fake_search(ts)

    conn.execute("DELETE FROM alerts WHERE client = %s", (A,))
    conn.commit()
    pools[A] = [tweet("f1", "x4", 0.5, 300)]
    check([r for r in trends.scan(conn, search=flaky) if r[0] in ALL] == [(A, "f1")],
          "one failing search does not stop the others")


if __name__ == "__main__":
    if not os.environ.get("DATABASE_URL"):
        raise SystemExit("set DATABASE_URL")
    conn = ledger.connect()
    ledger.ensure_schema(conn)
    conn.execute(trends.SCHEMA_SQL)
    conn.commit()
    reset(conn)
    try:
        test_scan(conn)
    finally:
        conn.rollback()
        for t in ("alerts", "agency_requests", "seen_targets"):
            conn.execute(f"DELETE FROM {t} WHERE client = ANY(%s)", (ALL,))
        conn.execute("DELETE FROM clients WHERE handle = ANY(%s)", (ALL,))
        conn.commit()
        conn.close()
    print("all good")
