"""Offline tests for learning from experience (episodes, reward, lessons, sleep, learning style, bootstrap).

    DATABASE_URL=postgresql://... uv run --no-project --with "psycopg[binary]" tests/test_brain.py
"""

import json
import os
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
os.environ.update(OWNER_SLACK_IDS="UOWNER0001")
os.environ.pop("VOYAGE_API_KEY", None)           # word matching path (semantic is covered by test_doclib)

from perpetual_core import agency_memory as m, brain, connections, ledger  # noqa: E402


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


def row(conn, rid):
    return conn.execute("SELECT * FROM conversation_log WHERE id = %s", (rid,)).fetchone()


def last(conn, user):
    return conn.execute("SELECT * FROM conversation_log WHERE slack_user = %s ORDER BY id DESC LIMIT 1",
                        (user,)).fetchone()


def main():
    conn = ledger.connect()
    ledger.ensure_schema(conn)
    tag = uuid.uuid4().hex[:6].upper()
    me = f"UBRN{tag}"
    connections.current_user = lambda: me
    import perpetual_core.slackui as slackui
    slackui.person_name = lambda uid: "Test Person"
    ctx = Ctx()
    m.register(ctx)
    pre, post, tool_hook = ctx.hooks["pre_llm_call"], ctx.hooks["post_llm_call"], ctx.hooks["post_tool_call"]

    # --- situation + reward scoring (pure)
    check(brain.situation("what is our monthly revenue in dollars") == "numbers", "situation: numbers")
    check(brain.situation("draft a thread for @alex about hooks") == "content", "situation: content")
    check(brain.situation("hey") == "chat", "situation: chat by default")
    check(brain.score_followup("give me revenue", "not this i want the monthly number")[1] == ["corrected"],
          "correction detected (team's own phrasing)")
    check(brain.score_followup("x", "no, I meant the rupee amount")[0] == -1.0, "'no, I meant' = -1")
    check(brain.score_followup("list all clients we have now", "list all the clients we have now please")[1]
          == ["repeated"], "same ask again = redo")
    check(brain.score_followup("x", "perfect, exactly this")[1] == ["praised"], "praise")
    check(brain.score_followup("x", "thanks")[1] == ["thanked"], "thanks = mild positive")
    check(brain.score_followup("x", "now add a task for Shravya")[1] == [], "a new ask is neutral")

    # --- an episode: tools seen during the turn are stored on the row
    tool_hook(tool_name="web_search", session_id="s1", status="ok")
    tool_hook(tool_name="gsheet_create", session_id="s1", status="error")
    pre(session_id="s1", user_message=f"what is our monthly revenue {tag}", is_first_turn=True)
    post(session_id="s1", user_message=f"what is our monthly revenue {tag}", assistant_response="About $21k.",
         platform="slack")
    ep1 = last(conn, me)
    check(ep1["situation"] == "numbers" and ep1["tool_errors"] == 1 and len(ep1["tools"]) == 2
          and abs(ep1["reward"] + 0.2) < 1e-6, "episode stores situation, tools, tool errors (-0.2)")

    # --- the next message corrects it -> reward on the previous episode
    pre(session_id="s1", user_message="not this, give it in rupees too", is_first_turn=False)
    ep1 = row(conn, ep1["id"])
    check(ep1["signals"] == ["corrected"] and abs(ep1["reward"] + 1.2) < 1e-6, "correction scored on the last reply")
    post(session_id="s1", user_message="not this, give it in rupees too", assistant_response="$21k ≈ ₹18.5 lakh.",
         platform="slack")
    ep2 = last(conn, me)
    pre(session_id="s1", user_message="perfect", is_first_turn=False)
    check(row(conn, ep2["id"])["signals"] == ["praised"], "praise scored")
    pre(session_id="s1", user_message="and again for july", is_first_turn=False)
    check(row(conn, ep2["id"])["signals"] == ["praised"], "an episode is scored only once")

    # --- lessons: learn, reinforce (no duplicate), tool permissions
    out = json.loads(ctx.tools["lesson_learn"]({"rule": f"Give money in USD and INR side by side {tag}",
                                                 "about": "me", "situation": "numbers", "episodes": [ep1["id"]]}))
    check(out["success"] and out["result"] == "new" and out["scope"] == f"person:{me}", "lesson saved for the person")
    lid = out["lesson"]
    again = json.loads(ctx.tools["lesson_learn"]({"rule": f"give money in USD and INR side by side {tag}",
                                                   "about": "me", "situation": "numbers"}))
    les = conn.execute("SELECT * FROM lessons WHERE id = %s", (lid,)).fetchone()
    check(again["lesson"] == lid and again["result"] == "reinforced" and les["strength"] == 2.0 and les["evidence"] == 2,
          "same lesson again = reinforced, not duplicated")
    team = json.loads(ctx.tools["lesson_learn"]({"rule": f"Always cc finance on invoices {tag}", "about": "agency"}))
    check(team["scope"] == f"person:{me}", "a teammate can't set agency-wide lessons (kept personal)")
    other = conn.execute("INSERT INTO lessons(scope, rule) VALUES ('person:USOMEONE', %s) RETURNING id",
                         (f"someone else's habit {tag}",)).fetchone()["id"]
    conn.commit()
    check(not json.loads(ctx.tools["lesson_weaken"]({"id": other}))["success"], "can't change another person's lesson")
    listed = json.loads(ctx.tools["lessons_list"]({}))["lessons"]
    check(any(x["id"] == lid for x in listed) and all(x["id"] != other for x in listed),
          "lessons_list shows mine, hides other people's")

    # --- recall into working memory + prediction error
    res = pre(session_id="s2", user_message=f"how much revenue did we make in august {tag}", is_first_turn=True)
    check(res and "What experience taught you" in res["context"] and "USD and INR" in res["context"],
          "relevant lesson injected into the turn")
    post(session_id="s2", user_message=f"how much revenue did we make in august {tag}", assistant_response="$31k",
         platform="slack")
    ep3 = last(conn, me)
    check(ep3["lessons_used"] and lid in ep3["lessons_used"], "the row records which lessons were used")
    before = conn.execute("SELECT strength FROM lessons WHERE id = %s", (lid,)).fetchone()["strength"]
    pre(session_id="s2", user_message="wrong, that's july", is_first_turn=False)
    after = conn.execute("SELECT strength FROM lessons WHERE id = %s", (lid,)).fetchone()["strength"]
    check(abs(before - after - 0.5) < 1e-6, "used lesson + correction = weaker (prediction error)")

    # --- learning style: careful mode after repeated corrections on this kind of ask
    style = brain.style_for(conn, me, "numbers")
    check(any("Careful mode" in s for s in style), "careful mode where they keep correcting you")
    check(not any("Careful" in s for s in brain.style_for(conn, me, "content")), "other situations unaffected")
    happy = f"UHAP{tag}"
    for i in range(7):
        conn.execute("""INSERT INTO conversation_log(slack_user, user_text, reply_text, situation, signals, at)
                        VALUES (%s, %s, 'ok', 'content', '{}', now() - make_interval(hours => %s))""",
                     (happy, f"write a post {i}", i + 1))
    conn.commit()
    check(any("Direct mode" in s for s in brain.style_for(conn, happy, "content")), "direct mode where it's reliable")

    # --- sleep: salient first, lessons listed, marked consolidated, then nothing (no agent run)
    lines = brain.sleep_lines(conn, limit=500)
    eps = [x for x in lines if x.startswith("[ep ")]
    mine = [x for x in eps if f"({me})" in x]
    check(mine and "corrected" in mine[0], "sleep replays the corrected episode first")
    check(any(x.startswith(f"L{lid} ") for x in lines), "sleep shows the lessons so far")
    check(any("next they said: not this, give it in rupees" in x for x in lines), "sleep shows what they said next")
    check(brain.sleep_lines(conn) == [], "replayed episodes are put to bed (next run empty)")

    # --- forgetting curve
    conn.execute("UPDATE lessons SET strength = 0.31, last_seen = now() - interval '30 days', last_used = NULL "
                 "WHERE id = %s", (lid,))
    conn.commit()
    brain.decay(conn)
    les = conn.execute("SELECT * FROM lessons WHERE id = %s", (lid,)).fetchone()
    check(not les["active"], "an unused weak lesson fades out")

    # --- childhood: bootstrap scores history
    kid = f"UKID{tag}"
    conn.execute("""INSERT INTO conversation_log(slack_user, user_text, reply_text, at) VALUES
                    (%s, 'make me the client report', 'here', now() - interval '50 minutes'),
                    (%s, 'no, I meant the weekly one', 'here', now() - interval '45 minutes')""", (kid, kid))
    conn.commit()
    brain.bootstrap(conn)
    first = conn.execute("SELECT * FROM conversation_log WHERE slack_user = %s ORDER BY at", (kid,)).fetchall()
    check(first[0]["signals"] == ["corrected"] and first[0]["situation"], "bootstrap scores past exchanges")

    for u in (me, happy, kid):
        conn.execute("DELETE FROM conversation_log WHERE slack_user = %s", (u,))
    conn.execute("DELETE FROM lessons WHERE rule LIKE %s", (f"%{tag}",))
    conn.commit()
    conn.close()
    print("all good")


if __name__ == "__main__":
    main()
