"""Offline tests for long-term memory (facts + conversation log + per-turn context).

    DATABASE_URL=postgresql://... uv run --no-project --with "psycopg[binary]" tests/test_memory.py
"""

import json
import os
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
os.environ.update(OWNER_SLACK_IDS="UOWNER0001")

from perpetual_core import agency_memory as m, connections, ledger  # noqa: E402


def check(cond, msg):
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    print(f"ok   {msg}")


class Ctx:
    def __init__(self):
        self.tools, self.hooks = {}, {}

    def register_tool(self, name, toolset, schema, handler, **_):
        self.tools[name] = handler

    def register_hook(self, name, cb):
        self.hooks[name] = cb


def main():
    conn = ledger.connect()
    ledger.ensure_schema(conn)
    tag = uuid.uuid4().hex[:6]
    me, other = f"UMEM{tag.upper()}", f"UOTH{tag.upper()}"
    connections.current_user = lambda: me
    ctx = Ctx()
    m.register(ctx)
    check(set(ctx.tools) == {"remember", "recall", "forget"} and set(ctx.hooks) == {"pre_llm_call", "post_llm_call"},
          "tools + hooks registered")

    out = json.loads(ctx.tools["remember"]({"fact": f"Alex prefers threads over single tweets {tag}", "about": "@ItsAlexVacca"}))
    check(out["success"] and out["scope"] == "client:itsalexvacca", "client fact saved under the client")
    json.loads(ctx.tools["remember"]({"fact": f"Invoices go out on the 1st {tag}"}))
    json.loads(ctx.tools["remember"]({"fact": f"I like short bullet answers {tag}", "about": "me"}))
    again = json.loads(ctx.tools["remember"]({"fact": f"invoices go out on the 1st {tag}"}))
    check(again["saved"] == conn.execute("SELECT id FROM agency_memory WHERE text = %s",
                                         (f"Invoices go out on the 1st {tag}",)).fetchone()["id"], "no duplicates")

    # a conversation, logged by the post hook
    ctx.hooks["post_llm_call"](session_id="s1", user_message=f"what's our invoice day {tag}?",
                               assistant_response="The 1st of each month.", platform="slack")
    ctx.hooks["post_llm_call"](session_id="s1", user_message="", assistant_response="x")    # ignored
    n = conn.execute("SELECT count(*) n FROM conversation_log WHERE slack_user = %s", (me,)).fetchone()["n"]
    check(n == 1, "exchange logged once (empty ones skipped)")

    # new conversation (different session): context brings facts + last exchange
    res = ctx.hooks["pre_llm_call"](session_id="s2", user_message=f"draft something for @ItsAlexVacca about invoices {tag}",
                                    is_first_turn=True)
    text = res["context"]
    check("prefers threads" in text and "Invoices go out" in text and "short bullet" in text,
          "client, agency and personal facts injected")
    check("invoice day" in text and "1st of each month" in text, "last exchange from the earlier conversation injected")
    res = ctx.hooks["pre_llm_call"](session_id="s1", user_message="thanks", is_first_turn=False)
    check(res is None or "last exchanges" not in res["context"], "ongoing conversation: no history replay")

    # another person doesn't get my personal facts or my chat history
    connections.current_user = lambda: other
    res = ctx.hooks["pre_llm_call"](session_id="s9", user_message=f"invoices {tag}", is_first_turn=True)
    check(res and "short bullet" not in res["context"] and "invoice day" not in res["context"]
          and "Invoices go out" in res["context"], "personal memory stays personal; agency memory is shared")

    connections.current_user = lambda: me
    out = json.loads(ctx.tools["recall"]({"query": f"invoice {tag}"}))
    check(out["facts"] and out["conversations"], "recall finds facts and past conversations")
    fid = out["facts"][0]["id"]
    check(json.loads(ctx.tools["forget"]({"id": fid}))["forgotten"], "forget")
    check(fid not in [f["id"] for f in json.loads(ctx.tools["recall"]({"query": f"invoice {tag}"}))["facts"]],
          "forgotten fact no longer recalled")
    connections.current_user = lambda: None
    check(ctx.hooks["pre_llm_call"](session_id="c", user_message="x", is_first_turn=True) is None,
          "scheduled jobs (no person) get nothing injected")

    # full-text search: stemmed ("invoicing" finds "invoice"), ranked, word order free
    m.remember(conn, f"Clients are invoiced monthly via Stripe {tag}", "agency")
    hits = m.search(conn, f"how do we invoice clients {tag}", ["agency"])
    check(hits and "invoiced monthly" in hits[0]["text"], "full-text recall finds stemmed matches")

    # nightly digest: unreviewed conversations once, then nothing (no agent run)
    import perpetual_core.slackui as slackui
    slackui.person_name = lambda uid: "Test Person"
    connections.current_user = lambda: me
    ctx.hooks["post_llm_call"](session_id="s3", user_message=f"we always send reports on Fridays {tag}",
                               assistant_response="Noted.", platform="slack")
    lines = m.digest_lines(conn)
    check(any("reports on Fridays" in x and "Test Person" in x for x in lines), "digest lists today's conversations")
    check(m.digest_lines(conn) == [], "digest marks them reviewed (next run empty)")

    conn.execute("DELETE FROM agency_memory WHERE text LIKE %s", (f"%{tag}",))
    conn.execute("DELETE FROM conversation_log WHERE slack_user IN (%s, %s)", (me, other))
    conn.commit()
    conn.close()
    print("all good")


if __name__ == "__main__":
    main()
