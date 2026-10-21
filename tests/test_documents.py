"""Offline tests for v5: documents (canvas with thread fallback), research memory, hand-offs.

    DATABASE_URL=postgresql://... uv run --no-project --with "psycopg[binary]" tests/test_documents.py
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from perpetual_core import slackui  # noqa: E402

DOC = """# Replies: The 70-Signal Timing System (Oct 21)
**Post:** [link](https://x.com/a/status/1)
**Start with:** R12, R15

## Questions
### R12: @joshua (1,200 followers)
> Has it flagged anyone you never would've thought to reach out to?

**Reply**
> linear stood out, 81 off an enterprise AE posting

- one
- two

---

## Sources
1. receipts.md
"""


def check(cond, msg):
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    print(f"ok   {msg}")


def test_markdown():
    m = slackui.md_to_mrkdwn(DOC)
    check("*Replies: The 70-Signal Timing System (Oct 21)*" in m, "heading → bold")
    check("<https://x.com/a/status/1|link>" in m, "markdown link → Slack link")
    check("*Post:*" in m and "**" not in m, "**bold** → *bold*")
    check("• one" in m, "bullets → •")
    check("> linear stood out" in m, "quotes kept")
    chunks = slackui.doc_chunks(DOC)
    check(len(chunks) == 3 and chunks[1].startswith("*Questions*"), "split at ## sections")
    long_doc = "# T\n\n## A\n" + "\n\n".join("para " + "x" * 400 for _ in range(20))
    check(all(len(c) <= 2900 for c in slackui.doc_chunks(long_doc)), "long sections split under Slack's limit")
    b = slackui.doc_summary_blocks("Title", "**3** replies ready", link="https://x.slack.com/docs/T/F", asked_by="U1")
    check(b[2]["elements"][0]["url"] == "https://x.slack.com/docs/T/F", "Open document button links the canvas")
    check("<@U1>" in b[-1]["elements"][0]["text"], "summary names who asked")
    b = slackui.doc_summary_blocks("Title", "x", mode="thread")
    check(not any(x["type"] == "actions" for x in b), "thread mode: no Open document button")


def test_publish_fallback(conn):
    calls = []

    def fake_slack(method, **payload):
        calls.append((method, payload))
        if method == "canvases.create":
            raise slackui.SlackError("canvases.create: missing_scope canvases:write")
        if method == "chat.getPermalink":
            return {"ok": True, "permalink": "https://slack/p1"}
        return {"ok": True, "ts": f"{len(calls)}.0", "channel": payload.get("channel")}

    real = slackui.slack
    slackui.slack = fake_slack
    try:
        res = slackui.publish_doc(conn, "acme5", "Replies", DOC, "3 replies ready", kind="reply-pack")
    finally:
        slackui.slack = real
    check(res["mode"] == "thread" and res["link"] == "https://slack/p1", "no canvas scope → thread, link returned")
    threaded = [p for m, p in calls if m == "chat.postMessage" and p.get("thread_ts")]
    check(len(threaded) == 3, "document laid out in the summary's thread")
    check("canvas unavailable" in res["note"], "fallback reason reported")
    row = conn.execute("SELECT mode, kind FROM published_docs WHERE client = 'acme5' ORDER BY id DESC LIMIT 1").fetchone()
    check(row["mode"] == "thread" and row["kind"] == "reply-pack", "document logged")

    calls.clear()

    def canvas_slack(method, **payload):
        calls.append((method, payload))
        if method == "canvases.create":
            return {"ok": True, "canvas_id": "F123"}
        if method == "auth.test":
            return {"ok": True, "url": "https://tp.slack.com/", "team_id": "T9"}
        return {"ok": True, "ts": "9.0", "channel": payload.get("channel")}

    slackui.slack, slackui._auth = canvas_slack, {}
    try:
        res = slackui.publish_doc(conn, "acme5", "Research", DOC, "summary")
    finally:
        slackui.slack = real
    check(res["mode"] == "canvas" and res["link"] == "https://tp.slack.com/docs/T9/F123", "canvas mode with link")
    check(not any(p.get("thread_ts") for m, p in calls if m == "chat.postMessage"), "canvas mode: no thread dump")


def test_research_and_handoff(conn):
    from perpetual_core import agency, research
    research.add(conn, "acme5", "performance", "Threads win", "Threads get 3x median views", ["https://x.com/a/1"])
    rows = research.recent(conn, "acme5")
    check(rows and rows[0]["title"] == "Threads win" and rows[0]["sources"] == ["https://x.com/a/1"], "research saved and read")
    check(research.recent(conn, "acme5", kind="niche") == [], "filter by kind")
    try:
        research.add(conn, "acme5", "bogus", "t", "b")
        check(False, "bad kind rejected")
    except ValueError:
        check(True, "bad kind rejected")
    agency.add_request(conn, "acme5", "replies for my latest article", by="Sai", requester_slack="U0SAI")
    taken = agency.take_requests(conn, "acme5")
    check(taken and taken[-1]["requester_slack"] == "U0SAI", "hand-off carries who asked")
    check(agency.take_requests(conn, "acme5") == [], "each request taken once")


if __name__ == "__main__":
    test_markdown()
    if os.environ.get("DATABASE_URL"):
        from perpetual_core import ledger
        conn = ledger.connect()
        ledger.ensure_schema(conn)
        for t in ("published_docs", "research", "agency_requests"):
            conn.execute(f"DELETE FROM {t} WHERE client = 'acme5'")
        conn.execute("DELETE FROM clients WHERE handle = 'acme5'")
        conn.execute("INSERT INTO clients(handle, display_name, slack_channel) VALUES ('acme5', 'Acme', 'CACME5')")
        conn.commit()
        test_publish_fallback(conn)
        test_research_and_handoff(conn)
        conn.close()
    print("all good")
