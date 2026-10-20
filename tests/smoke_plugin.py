"""Smoke test: load the perpetual plugin with a fake Hermes ctx and exercise every tool.

    DATABASE_URL=postgresql://... XCONSOLE_API_KEY=... \
        uv run --no-project --with "psycopg[binary]" tests/smoke_plugin.py [--offline]

Uses throwaway client handles, so it never touches real clients' rows.
--offline skips the tools that call the live console.
"""

import importlib.util
import json
import os
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "hermes-template" / "plugins" / "perpetual"
os.environ.setdefault("PERPETUAL_PYLIB", str(ROOT / "lib"))


class FakeCtx:
    def __init__(self):
        self.tools, self.commands = {}, {}

    def register_tool(self, name, toolset, schema, handler, **_):
        assert schema["name"] == name and "description" in schema
        self.tools[name] = handler

    def register_command(self, name, handler, **_):
        self.commands[name] = handler


def load():
    spec = importlib.util.spec_from_file_location("perpetual", PLUGIN / "__init__.py",
                                                  submodule_search_locations=[str(PLUGIN)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["perpetual"] = mod
    spec.loader.exec_module(mod)
    ctx = FakeCtx()
    mod.register(ctx)
    return ctx


def call(tools, name, **params):
    out = json.loads(tools[name](params))
    status = "ok " if out.get("success") else "ERR"
    print(f"[{status}] {name}: {json.dumps(out, ensure_ascii=False)[:150]}")
    if not out.get("success"):
        raise SystemExit(1)
    return out


def main():
    offline = "--offline" in sys.argv
    ctx = load()
    tools = ctx.tools
    from perpetual_core import ledger
    print("registered:", ", ".join(sorted(tools)), "| commands:", ", ".join(ctx.commands))

    conn = ledger.connect()
    ledger.ensure_schema(conn)

    # Two throwaway clients prove rows never cross clients.
    a, b = f"smoke_a_{uuid.uuid4().hex[:6]}", f"smoke_b_{uuid.uuid4().hex[:6]}"
    os.environ["CLIENT_HANDLE"] = a
    saved = call(tools, "suggest_save", batch="smoke", items=[
        {"kind": "tweet", "text": "Most cold email advice is written by people who stopped sending cold email.",
         "hook": "contrarian", "format": "one-liner", "topic": "cold email"},
        {"kind": "reply", "text": "The bounce threshold is the setting nobody checks.", "target_id": "111",
         "hook": "insight", "format": "one-liner", "topic": "deliverability"},
        {"kind": "quote", "text": "This, but for LinkedIn too.", "target_id": "222",
         "target_url": "https://x.com/someone/status/222", "topic": "linkedin"},
        {"kind": "repost", "target_id": "333", "rationale": "peer milestone"},
        {"kind": "thread", "text": "Hook tweet\n---\nSecond beat\n---\nThird beat", "format": "thread"},
    ])["saved"]
    assert [s["code"] for s in saved] == ["T1", "R1", "Q1", "P1", "H1"], saved
    assert [s["one_tap_post"] for s in saved] == [True, False, False, False, True], saved
    assert "in_reply_to=111" in saved[1]["open_in_x"]
    assert "url=https%3A%2F%2Fx.com%2Fsomeone%2Fstatus%2F222" in saved[2]["open_in_x"]
    assert saved[3]["open_in_x"] == "https://x.com/intent/retweet?tweet_id=333"
    assert "Second" not in saved[4]["open_in_x"]          # thread link carries only the hook tweet
    assert ledger.thread_parts("a\n---\nb\n --- \nc") == ["a", "b", "c"]

    call(tools, "suggest_feedback", code="t1", action="edit", by="Priya",
         text="Most cold email advice comes from people who quit sending it.")
    call(tools, "suggest_feedback", code="R1", action="reject", note="too generic", by="Priya")
    assert call(tools, "targets_filter_new", tweet_ids=["111", "444"])["new"] == ["444"]
    call(tools, "suggest_list")

    res = ledger.record_outcomes(conn, a, [{"id": "999", "text": "Most cold email advice comes from people who quit sending it",
                                            "likes": 42, "age_h": 30, "at": "2026-10-19T10:00:00Z"}])
    assert res["newly_matched"] and res["newly_matched"][0]["code"] == "T1", res
    stats = call(tools, "stats_query")
    assert stats["by_hook"] and stats["recent_client_edits"], stats
    call(tools, "learning_log", summary="smoke", changes=["none"])

    # Client B starts from T1 again and cannot see A's rows.
    os.environ["CLIENT_HANDLE"] = b
    assert call(tools, "suggest_save", items=[{"kind": "tweet", "text": "b's first"}])["saved"][0]["code"] == "T1"
    assert call(tools, "suggest_list")["count"] == 1
    assert call(tools, "targets_filter_new", tweet_ids=["111"])["new"] == ["111"]
    hq = ledger.stats(conn, None, days=1)
    assert {r["client"] for r in hq["suggestions_by_kind"]} >= {a, b}, hq["suggestions_by_kind"]

    # /post fails closed without a real chat sender.
    assert "only works from Slack" in ctx.commands["post"]("T1", source=None)
    who = SimpleNamespace(user_id="U1", user_name="Priya")
    assert "Couldn't post" in ctx.commands["post"]("T1", source=who)   # no web app configured here

    for c in (a, b):
        for t in ("suggestions", "feedback_events", "seen_targets", "outcomes", "learning_log", "counters"):
            conn.execute(f"DELETE FROM {t} WHERE client = %s", (c,))
    conn.commit()

    if offline:
        print("offline mode: skipped live console tools")
        return
    os.environ["CLIENT_HANDLE"] = "itsalexvacca"
    call(tools, "x_find_tweets", query="cold email", min_likes=50, limit=5)
    call(tools, "x_trends", limit=3)
    call(tools, "x_client_analytics", kind="summary", days=7)
    call(tools, "x_client_content", days=3, limit=5)
    call(tools, "x_user_info", usernames=["nikolak47"])
    print("all good")


if __name__ == "__main__":
    main()
