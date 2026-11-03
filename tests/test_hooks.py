"""Offline tests for hook intelligence (lib/perpetual_core/hooks.py).

    DATABASE_URL=postgresql://... uv run --no-project --with "psycopg[binary]" tests/test_hooks.py
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from perpetual_core import hooks, ledger  # noqa: E402

A, B = "hkalpha", "hkbeta"
NICHE = "hk-test-niche"


def check(cond, msg):
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    print(f"ok   {msg}")


def reset(conn):
    conn.execute("DELETE FROM outcomes WHERE client = ANY(%s)", ([A, B],))
    conn.execute("DELETE FROM hook_library WHERE client = ANY(%s) OR niche = %s", ([A, B], NICHE))
    conn.commit()


def raw(tid, text, likes=0, bookmarks=0, views=0, replies=0, reposts=0, by="someone", **kw):
    return {"tweet_id": tid, "url": f"https://x.com/{by}/status/{tid}", "username": by, "text": text,
            "like_count": likes, "bookmark_count": bookmarks, "view_count": views, "reply_count": replies,
            "retweet_count": reposts, "created_at": "2026-11-01T10:00:00Z", **kw}


def test_hook_of():
    check(hooks.hook_of("I lost $40k in a year. Here's what I learned.\nmore") == "I lost $40k in a year.",
          "hook = first sentence of the first line")
    check(hooks.hook_of("\n\n  Stop writing threads\n\nbody") == "Stop writing threads", "blank lines skipped")
    check(hooks.hook_of("Wow. This changed how I write hooks forever") == "Wow. This changed how I write hooks forever",
          "a tiny first sentence keeps the whole line")
    long = hooks.hook_of("word " * 100)
    check(len(long) <= hooks.HOOK_MAX and long.endswith("…"), "long hooks trimmed to ~200 chars")
    check(hooks.hook_of("") == "" and hooks.classify_hook("") == "other", "empty text")
    check(hooks.hook_of("https://t.co/abc\nReal line here") == "Real line here", "bare links skipped")


def test_classify():
    cases = {
        "How to write a hook that stops the scroll": "how-to",
        "Here's how we grew to 100k followers": "how-to",
        "7 lessons from 10 years of cold email:": "list",
        "5 ways to double your reply rate": "list",
        "Why do most founders fail at content?": "question",
        "Unpopular opinion: cold email is not dead": "contrarian",
        "Stop posting threads every day": "contrarian",
        "I got fired from my first job at 23": "story",
        "3 years ago I had zero followers": "story",
        "$1.2M ARR with 4 people": "number",
        "Nobody talks about the real reason agencies die": "curiosity",
        "The best founders never pitch": "bold-claim",
        "Content is a long game": "other",
    }
    for text, want in cases.items():
        got = hooks.classify_hook(text)
        check(got == want, f"classify {text!r} -> {want} (got {got})")


def test_research():
    items = [raw("1", "7 lessons from scaling to $10M:\n1. ...", likes=900, bookmarks=600, views=200000),
             raw("2", "Why does nobody reply to cold email?", likes=300, bookmarks=20, views=100000),
             raw("3", "I quit my job last year. Best decision.", likes=1200, bookmarks=50, views=50000),
             raw("4", "Reply text", likes=5000, is_reply=True),
             raw("5", "Retweeted", likes=5000, is_retweet=True),
             raw("1", "7 lessons from scaling to $10M:\n1. ...", likes=900, bookmarks=600, views=200000),
             raw("6", "How to book 30 meetings a month", likes=250, bookmarks=300, views=0)]
    seen = []

    def fake(payload):
        seen.append(payload)
        return items

    out = hooks.research_hooks("cold email", days=7, min_likes=200, limit=10, search=fake)
    q = seen[0]["raw_query"]
    check(q.startswith('"cold email" lang:en -filter:replies min_faves:200 since:') and seen[0]["product"] == "Top",
          "console search query shape")
    check(out["found"] == 4, "replies, retweets and duplicates dropped")
    first = out["hooks"][0]
    check(first["hook"] == "7 lessons from scaling to $10M:" and first["label"] == "list", "best hook ranked first")
    check(first["er"] == round((900 + 600) / 200000, 4) and first["author"] == "someone", "engagement rate + author")
    no_views = next(h for h in out["hooks"] if h["label"] == "how-to")
    check(no_views["er"] is None and no_views["views"] is None, "no views -> no ER")
    check(out["by_label"]["list"] == {"count": 1, "median_likes": 900, "median_bookmarks": 600}, "per-label summary")
    check(len(hooks.research_hooks("x", limit=2, search=fake)["hooks"]) == 2, "limit respected")
    return out


def test_client_hooks(conn):
    now = datetime.now(timezone.utc)
    rows = [("t1", "tweet", "The best agencies never send proposals. Here's why.", 50000, 400),
            ("t2", "tweet", "How to write cold emails that get replies", 20000, 900),
            ("t3", "reply", "Great point, totally agree", 90000, 5),
            ("t4", "tweet", "Monday thoughts", 300, 1),
            ("t5", "quote", "3 mistakes I made hiring SDRs", 8000, 50)]
    for tid, kind, text, views, bm in rows:
        conn.execute("""INSERT INTO outcomes(client, tweet_id, code, kind, text, url, posted_at, latest)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                     (A, tid, None, kind, text, f"https://x.com/{A}/status/{tid}", now - timedelta(days=3),
                      json.dumps({"views": views, "likes": views // 100, "bookmarks": bm})))
    conn.execute("""INSERT INTO outcomes(client, tweet_id, kind, text, url, posted_at, latest)
                    VALUES (%s,'old','tweet','Ancient banger','u', %s, %s)""",
                 (A, now - timedelta(days=200), json.dumps({"views": 10 ** 7})))
    conn.commit()
    top = hooks.client_top_hooks(conn, A, days=60, n=2)
    check([p["hook"] for p in top["best"]] == ["The best agencies never send proposals.",
                                               "How to write cold emails that get replies"],
          "client's best original posts by views (replies and old posts excluded)")
    check(top["best"][0]["label"] == "bold-claim" and top["best"][1]["label"] == "how-to", "client hooks labelled")
    check([p["hook"] for p in top["worst"]] == ["Monday thoughts", "3 mistakes I made hiring SDRs"],
          "worst posts listed separately")
    check(top["posts"] == 4, "four original posts in window")


def test_library(conn, research):
    ids = hooks.save_hooks(conn, None, NICHE, research["hooks"])
    check(len(ids) == 4, "niche-wide hooks saved")
    check(hooks.save_hooks(conn, None, NICHE, research["hooks"]) == [], "niche-wide dedupe (NULL client)")
    mine = hooks.save_hooks(conn, A, NICHE, [{"hook": "My own proven opener", "url": "https://x.com/a/1",
                                               "likes": 10, "bookmarks": 5}])
    check(len(mine) == 1 and hooks.save_hooks(conn, A, NICHE, [{"hook": "dup", "url": "https://x.com/a/1"}]) == [],
          "client hooks dedupe on source_url")
    best = hooks.best_hooks(conn, A, niche=NICHE)
    check(len(best) == 5 and best[0]["hook"] == "7 lessons from scaling to $10M:", "ranked by bookmarks/likes")
    check(len(hooks.best_hooks(conn, B, niche=NICHE)) == 4, "other clients see niche-wide hooks, not A's own")
    check([h["label"] for h in hooks.best_hooks(conn, A, niche=NICHE, label="question")] == ["question"], "label filter")

    q = next(h for h in best if h["label"] == "question")
    hid = hooks.record_use(conn, A, q["id"], "t7")
    check(hid != q["id"], "using a niche-wide hook copies it into the client's library")
    check(hooks.record_use(conn, A, q["id"], "T8") == hid, "reusing it updates the same copy")
    check(hooks.record_outcome(conn, A, "t8", "win") == 1, "outcome recorded by draft code")
    best = hooks.best_hooks(conn, A, niche=NICHE)
    check(best[0]["id"] == hid and best[0]["outcome"] == "win" and best[0]["used_code"] == "T8", "wins rank first")
    check(all(h["outcome"] is None for h in hooks.best_hooks(conn, B, niche=NICHE)), "A's outcomes don't leak to B")
    flop = hooks.record_use(conn, A, mine[0], "T9")
    hooks.record_outcome(conn, A, "T9", "flop")
    check(hooks.best_hooks(conn, A, niche=NICHE)[-1]["id"] == flop, "flops rank last")
    for bad in (lambda: hooks.record_outcome(conn, A, "T9", "meh"), lambda: hooks.record_use(conn, B, mine[0], "T1")):
        try:
            bad()
            check(False, "bad input rejected")
        except ValueError:
            check(True, "bad input rejected (verdict / another client's hook)")


def test_tools(conn):
    os.environ["CLIENT_HANDLE"] = "@HKAlpha"
    orig = hooks._search
    hooks._search = lambda payload: [raw("90", "Cold email is dead. Long live cold email.", likes=800, bookmarks=90,
                                         views=60000, by="peer")]
    try:
        out = json.loads(hooks.hook_research({"topic": NICHE, "days": 3, "min_likes": 100}))
        check(out["success"] and out["found"] == 1 and out["saved_to_library"] == 1, "hook_research saves to library")
        check(out["client_hooks"]["client"] == A and out["client_hooks"]["best"], "hook_research compares client hooks")
        lib = json.loads(hooks.hooks_library({"action": "best", "niche": NICHE, "label": "contrarian"}))
        check(lib["success"] and lib["hooks"][0]["scope"] == "client", "hooks_library best")
        used = json.loads(hooks.hooks_library({"action": "use", "hook_id": lib["hooks"][0]["id"], "code": "t20"}))
        check(used["success"] and used["code"] == "T20", "hooks_library use")
        check(json.loads(hooks.hooks_library({"action": "outcome", "code": "T20", "verdict": "par"}))["updated"] == 1,
              "hooks_library outcome")
        check(json.loads(hooks.hooks_library({"action": "nope"}))["success"] is False, "bad action -> success false")
        check(json.loads(hooks.hook_research({}))["success"] is False, "missing topic -> success false")
        hooks._search = lambda payload: (_ for _ in ()).throw(RuntimeError("console down"))
        err = json.loads(hooks.hook_research({"topic": "x"}))
        check(err == {"success": False, "error": "console down"}, "console outage -> success false, no crash")
    finally:
        hooks._search = orig

    class Ctx:
        names = []

        def register_tool(self, name, toolset, handler, schema):
            self.names.append((name, toolset, schema["parameters"]["required"]))

    ctx = Ctx()
    hooks.register(ctx)
    check(ctx.names == [("hook_research", "hooks", ["topic"]), ("hooks_library", "hooks", ["action"])], "register")


if __name__ == "__main__":
    if not os.environ.get("DATABASE_URL"):
        raise SystemExit("set DATABASE_URL")
    conn = ledger.connect()
    ledger.ensure_schema(conn)
    conn.execute(hooks.SCHEMA_SQL)
    conn.commit()
    reset(conn)
    test_hook_of()
    test_classify()
    research = test_research()
    test_client_hooks(conn)
    test_library(conn, research)
    test_tools(conn)
    reset(conn)
    conn.close()
    print("all good")
