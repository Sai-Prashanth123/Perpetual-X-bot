"""Offline tests for the Slack UI builders, signed connect links and the client roster.

    DATABASE_URL=postgresql://... uv run --no-project --with "psycopg[binary]" tests/test_slackui.py
(The roster part is skipped when DATABASE_URL is unset.)
"""

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
os.environ.setdefault("INTERNAL_TOKEN", "test-key")
os.environ.setdefault("AGENCY_WEB_URL", "https://connector.example")

from perpetual_core import slackui  # noqa: E402

BASE = {"client": "acme", "code": "T1", "kind": "tweet", "text": "Cold email is a list problem.",
        "status": "pending", "api_postable": True, "intent_url": "https://x.com/intent/post?text=x",
        "hook": "contrarian", "format": "one-liner", "rationale": "Wins for this client."}


def ids(blocks):
    return [e.get("action_id") for b in blocks if b["type"] == "actions" for e in b["elements"]]


def check(cond, msg):
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    print(f"ok   {msg}")


def test_cards():
    b = slackui.suggestion_blocks(BASE, x_connected=True)
    check(ids(b) == ["px_post", "px_open", "px_approve", "px_edit", "px_reject"], "pending tweet, X connected: all buttons")
    check(ids(slackui.suggestion_blocks(BASE, x_connected=False)) == ["px_open", "px_approve", "px_edit", "px_reject"],
          "no X connection: no Post now")
    reply = {**BASE, "code": "R1", "kind": "reply", "api_postable": False, "target_url": "https://x.com/a/status/1",
             "target_by": "a", "target_text": "hi"}
    check("px_post" not in ids(slackui.suggestion_blocks(reply, True)), "reply on someone else's tweet: no Post now")
    approved = {**BASE, "status": "approved", "decided_by": "Shravya"}
    b = slackui.suggestion_blocks(approved, True)
    check("px_approve" not in ids(b) and "Approved by Shravya" in json.dumps(b), "approved card shows who, no Approve")
    posted = {**BASE, "status": "posted", "posted_id": "99"}
    b = slackui.suggestion_blocks(posted, True)
    check(ids(b) == [] and "x.com/acme/status/99" in json.dumps(b), "posted card: link, no buttons")
    repost = {**BASE, "code": "P1", "kind": "repost", "text": "", "api_postable": False}
    check("px_edit" not in ids(slackui.suggestion_blocks(repost, True)), "repost card has no Edit")
    thread = {**BASE, "code": "H1", "kind": "thread", "text": "one\n---\ntwo"}
    check("1/ one" in json.dumps(slackui.suggestion_blocks(thread, True)), "thread rendered as numbered tweets")
    weird = {**BASE, "text": "<script> & stuff"}
    check("&lt;script&gt; &amp;" in json.dumps(slackui.suggestion_blocks(weird, True)), "copy is escaped")
    for blocks in (slackui.suggestion_blocks(BASE, True),):
        for blk in blocks:
            if blk["type"] == "actions":
                check(len(blk["elements"]) <= 5, "Slack limit: max 5 buttons per actions block")


def test_modals_and_home():
    m = slackui.edit_modal({**BASE, "slack_channel": "C1", "slack_ts": "1.2"})
    check(json.loads(m["private_metadata"]) == {"c": "acme", "k": "T1", "ch": "C1", "ts": "1.2"}, "edit modal metadata")
    check(len(m["title"]["text"]) <= 24, "modal title within 24 chars")
    overview = {"clients": [{"client": f"c{i}", "name": f"Client {i}", "employee": "U1", "followers": 1000 + i,
                             "followers_change": i, "posts": 3, "likes": 10, "views": 1000 * i, "bookmarks": 1,
                             "replies": 1, "views_change_pct": 12.0, "likes_change_pct": 5.0, "engagement_rate": 1.5,
                             "suggestions": {"suggested": 5, "used": 2, "posted": 1, "rejected": 1, "pending": 2},
                             "flags": []} for i in range(30)], "team": []}
    view = slackui.home_view(overview, 7, {"c1": [1, 5, 3, 8]}, [], [], "v1.2.0", ["c1", "c2"])
    check(view["type"] == "home" and len(view["blocks"]) <= 100, "home view within Slack's 100-block limit")
    check(json.dumps(view).count("px_client_details") == 20, "leaderboard capped at 20 clients")
    empty = slackui.home_view({"clients": [], "team": []}, 7, {}, [], [], None, [])
    check("add client" in json.dumps(empty), "empty home explains how to add a client")
    check(slackui.sparkline([0, 5, 10]) == "▁▄█", "sparkline")
    cm = slackui.client_modal({"handle": "acme", "display_name": "Acme", "slack_channel": "C1", "employee_slack": "U1",
                               "niche_topics": "cold email", "watch_accounts": None}, [{"views": 5, "followers": 10}],
                              [], {"pending": 2}, None, None, "https://connector.example/x/connect?client=acme")
    check("Connect X account" in json.dumps(cm) and "Loading" in json.dumps(cm), "client modal: connect button + loading")


def test_connect_links():
    link = slackui.connect_link("acme")
    check(link.startswith("https://connector.example/x/connect?"), "connect link uses the connector URL")
    from urllib.parse import parse_qs, urlparse
    q = {k: v[0] for k, v in parse_qs(urlparse(link).query).items()}
    check(slackui.verify_connect("acme", q["exp"], q["sig"], "test-key"), "valid signature verifies")
    check(not slackui.verify_connect("other", q["exp"], q["sig"], "test-key"), "signature bound to the client")
    check(not slackui.verify_connect("acme", str(int(time.time()) - 5), q["sig"], "test-key"), "expired link rejected")
    check(not slackui.verify_connect("acme", q["exp"], q["sig"], "wrong-key"), "wrong key rejected")


def test_roster():
    if not os.environ.get("DATABASE_URL"):
        print("skip roster (no DATABASE_URL)")
        return
    from perpetual_core import agency, ledger
    conn = ledger.connect()
    ledger.ensure_schema(conn)
    for h in ("zz_one", "zz_two"):
        conn.execute("DELETE FROM clients WHERE handle = %s", (h,))
    conn.commit()
    agency.upsert_client(conn, "@ZZ_One", niche_topics=["a", "b"], slack_channel="C1")
    row = agency.upsert_client(conn, "zz_one", timezone="Asia/Kolkata")
    check(row["niche_topics"] == "a, b" and row["slack_channel"] == "C1", "partial update keeps other fields")
    agency.sync_clients(conn, [{"handle": "zz_two"}])
    check({"zz_one", "zz_two"} <= {c["handle"] for c in agency.list_clients(conn)}, "seed never removes chat-added clients")
    agency.set_client_active(conn, "zz_one", False)
    check("zz_one" not in {c["handle"] for c in agency.list_clients(conn)} and agency.get_client(conn, "zz_one"),
          "removed client is inactive but kept")
    for h in ("zz_one", "zz_two"):
        conn.execute("DELETE FROM clients WHERE handle = %s", (h,))
    conn.commit()


if __name__ == "__main__":
    test_cards()
    test_modals_and_home()
    test_connect_links()
    test_roster()
    print("all good")
