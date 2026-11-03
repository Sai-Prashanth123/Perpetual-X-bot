"""Offline tests for the swipe file (swipe_candidates) with a fake fetch.

    DATABASE_URL=postgresql://... uv run --no-project --with "psycopg[binary]" tests/test_swipe.py
"""

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from perpetual_core import ledger, swipe  # noqa: E402

A = "swpalpha"


def check(cond, msg):
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    print(f"ok   {msg}")


def tw(tid, by, likes, text="a post\nsecond line", **kw):
    return {"id": tid, "by": by, "url": f"https://x.com/{by}/status/{tid}", "text": text, "likes": likes,
            "replies": 0, "reposts": 0, "quotes": 0, "bookmarks": 0, "age_h": 20, **kw}


FEED = {
    # big account: ordinary posts ~1000 likes, one at 3000 (3x its median)
    "bignick": [tw("b1", "bignick", 1000), tw("b2", "bignick", 1000), tw("b3", "bignick", 3000),
                tw("b4", "bignick", 900), tw("b5", "bignick", 99999, is_reply=True),
                tw("b6", "bignick", 50000, text="RT @someone: viral")],
    # small account: ordinary ~10 likes, one at 80 (8x) and a thread
    "smallsam": [tw("s1", "smallsam", 10), tw("s2", "smallsam", 10), tw("s3", "smallsam", 80, bookmarks=5),
                 tw("s4", "smallsam", 12, text="How I grew to 10k 🧵\n1/ start here")],
    "articleann": [tw("a1", "articleann", 50, article_title="The playbook", text="The playbook"),
                   tw("a2", "articleann", 50, long_form=True, text="\n\n  Long one " + "x" * 300),
                   tw("a3", "articleann", 50, is_quote=True, text="quoting this"),
                   tw("a4", "articleann", 50, text="Lessons from 5 years. 1/7")],
}


def fake_fetch(handle, days, limit):
    calls.append((handle, days, limit))
    if handle == "broken":
        raise RuntimeError("console 502")
    return FEED.get(handle, [])


calls = []


def test_scoring():
    t = tw("x", "u", 10, replies=2, reposts=3, quotes=1, bookmarks=4)
    check(swipe.score(t) == 4 * 2 + 10 + 2 * 1.5 + 3 * 2 + 1 * 2, "score = bookmarks*2 + likes + replies*1.5 + reposts*2 + quotes*2")
    check(swipe.hook("\n\n  First line here  \nsecond") == "First line here", "hook = first non-empty line")
    check(len(swipe.hook("y" * 500)) == 200, "hook capped at 200 chars")
    fmts = {t["id"]: swipe.format_guess(t) for t in FEED["articleann"] + FEED["smallsam"]}
    check(fmts["a1"] == "article" and fmts["a2"] == "long" and fmts["a3"] == "quote" and fmts["a4"] == "thread"
          and fmts["s4"] == "thread" and fmts["s1"] == "short", "format guess: article/long/quote/thread/short")
    check(swipe.format_guess(tw("d", "u", 1, text="Meeting on 10/1/2025 went well")) == "short",
          "dates are not mistaken for threads")


def test_candidates():
    calls.clear()
    res = swipe.swipe_candidates(["@BigNick", "smallsam", "broken", "articleann", "bignick"], days=7,
                                 per_account=20, top=5, fetch=fake_fetch)
    check([c[0] for c in calls] == ["bignick", "smallsam", "broken", "articleann"],
          "handles normalised and de-duplicated, every account fetched")
    check(calls[0][1:] == (7, 20), "fetch gets days and per_account")
    check(res["failed"] and res["failed"][0]["account"] == "broken", "a failing account is reported, not fatal")
    ids = [p["id"] for p in res["posts"]]
    check("b5" not in ids and "b6" not in ids, "replies and retweets are dropped")
    check(ids[0] == "s3", "small account's 8x post outranks the big account's raw likes")
    check(len(res["posts"]) == 5, "top N respected")
    top = res["posts"][0]
    check(top["multiple_of_median"] > 7 and top["author"] == "smallsam" and top["url"].endswith("/s3")
          and top["hook"] == "a post" and top["format"] == "short" and top["metrics"]["bookmarks"] == 5,
          "post carries author, url, hook, format, metrics, multiple_of_median")
    b3 = next(p for p in res["posts"] if p["id"] == "b3")
    check(abs(b3["multiple_of_median"] - 3.0) < 0.01, "multiple is vs the author's own median")
    allres = swipe.swipe_candidates("bignick, smallsam", top=50, fetch=fake_fetch)
    check(allres["count"] == 4 + 4, "comma-list accounts accepted")


def test_tool():
    conn = ledger.connect()
    conn.execute("DELETE FROM clients WHERE handle = %s", (A,))
    conn.execute("""INSERT INTO clients(handle, display_name, watch_accounts, radar_accounts)
                    VALUES (%s, 'Alpha', 'peer1', 'smallsam, @ArticleAnn, swpalpha')""", (A,))
    conn.commit()
    check(swipe.default_accounts(conn, A) == ["smallsam", "articleann"], "default = radar_accounts minus the client")
    conn.execute("UPDATE clients SET radar_accounts = NULL WHERE handle = %s", (A,))
    conn.commit()
    check(swipe.default_accounts(conn, A) == ["peer1"], "falls back to watch_accounts")
    conn.execute("UPDATE clients SET radar_accounts = 'smallsam' WHERE handle = %s", (A,))
    conn.commit()

    os.environ["CLIENT_HANDLE"] = A
    calls.clear()
    out = json.loads(swipe.swipe_candidates_tool({"top": 2}, fetch=fake_fetch))
    check(out["success"] and out["accounts"] == ["smallsam"] and out["count"] == 2, "tool uses the client's radar list")
    out = json.loads(swipe.swipe_candidates_tool({"accounts": ["articleann"], "days": 3}, fetch=fake_fetch))
    check(out["success"] and out["accounts"] == ["articleann"] and calls[-1][1] == 3, "tool accepts explicit accounts + days")
    os.environ["CLIENT_HANDLE"] = "swpnobody"
    out = json.loads(swipe.swipe_candidates_tool({}, fetch=fake_fetch))
    check(out["success"] is False and "pass accounts" in out["error"], "no list -> readable JSON error")

    registered = []

    class Ctx:
        def register_tool(self, **kw):
            registered.append(kw)

    swipe.register(Ctx())
    check(len(registered) == 1 and registered[0]["name"] == "swipe_candidates" and registered[0]["toolset"] == "swipe",
          "register() adds swipe_candidates in the swipe toolset")
    conn.execute("DELETE FROM clients WHERE handle = %s", (A,))
    conn.commit()
    conn.close()


if __name__ == "__main__":
    test_scoring()
    test_candidates()
    test_tool()
    print("all good")
