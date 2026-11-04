"""Offline tests for proactive alerts: triggers, dedupe/cooldown, limits, mutes, backoff, buttons, stats.

    DATABASE_URL=postgresql://... PYTHONIOENCODING=utf-8 uv run --no-project --python 3.12 --with "psycopg[binary]" \
        --with cryptography --with tzdata tests/test_proactive.py
"""

import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
TAG = uuid.uuid4().hex[:6].upper()
UO, UA, UB = f"UO{TAG}", f"UA{TAG}", f"UB{TAG}"
CLIENT = f"pt{TAG.lower()}"
os.environ.update(HQ_SLACK_CHANNEL="CHQ", OWNER_SLACK_IDS=UO)

from perpetual_core import ledger, notify, proactive, slackui, standup  # noqa: E402

PEOPLE = [{"id": UA, "name": "asha", "real_name": "Asha T", "display_name": "asha", "email": ""},
          {"id": UB, "name": "bala", "real_name": "Bala T", "display_name": "bala", "email": ""}]
slackui.people = lambda: PEOPLE
slackui.person_name = lambda uid: next((p["real_name"] for p in PEOPLE if p["id"] == uid), uid)
DMS, POSTS = [], []
notify.dm = lambda ids, text: DMS.append((list(ids), text)) or len(ids)
slackui.slack = lambda method, **kw: POSTS.append((method, kw)) or {"ok": True}
IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=IST)      # Wednesday, after the 11:25 standup summary


def check(cond, msg):
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    print(f"ok   {msg}")


def task(conn, title, owner, due=None, status="open", updated=NOW):
    return conn.execute("""INSERT INTO team_tasks(title, owner_slack, owner_name, due, status, updated_at)
                           VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""",
                        (f"{title} {TAG}", owner, slackui.person_name(owner), due, status, updated)).fetchone()["id"]


def mine(conn, start):
    return {(r["key"], r["target_slack"]): r for r in conn.execute(
        "SELECT * FROM proactive_alerts WHERE id > %s AND target_slack = ANY(%s)", (start, [UO, UA, UB]))}


def cleanup(conn, start):
    conn.rollback()
    conn.execute("DELETE FROM proactive_alerts WHERE id > %s OR target_slack = ANY(%s)", (start, [UO, UA, UB]))
    conn.execute("DELETE FROM proactive_mutes WHERE target_slack = ANY(%s)", ([UO, UA, UB],))
    conn.execute("DELETE FROM team_tasks WHERE title LIKE %s", (f"% {TAG}",))
    conn.execute("DELETE FROM meetings WHERE id LIKE %s", (f"readai:pt-{TAG}%",))
    for t in ("suggestions", "calendar_slots", "postmortems"):
        conn.execute(f"DELETE FROM {t} WHERE client = %s", (CLIENT,))
    conn.execute("DELETE FROM standup_answers WHERE slack_user = ANY(%s)", ([UA, UB],))
    conn.execute("DELETE FROM standup_runs WHERE day = %s", (NOW.date(),))
    conn.execute("DELETE FROM standup_config")
    conn.commit()


def main():
    conn = ledger.connect()
    ledger.ensure_schema(conn)
    conn.execute(proactive.SCHEMA_SQL)
    conn.commit()
    start = conn.execute("SELECT coalesce(max(id), 0) m FROM proactive_alerts").fetchone()["m"]
    try:
        run(conn, start)
    finally:
        cleanup(conn, start)
        conn.close()
    print("all good")


def run(conn, start):
    today = NOW.date()
    a = task(conn, "Send Q4 plan", UA, today - timedelta(days=1))
    b = task(conn, "Fix bio", UA, today - timedelta(days=4))
    c = task(conn, "Get analytics access", UB, None, "blocked", NOW - timedelta(hours=30))
    d = task(conn, "Fresh blocker", UB, None, "blocked", NOW - timedelta(hours=2))
    e = task(conn, "Due today", UA, today)
    meet = """INSERT INTO meetings(id, source, title, started_at, ended_at, action_items, fetched_at)
              VALUES (%s,'readai',%s,%s,%s,%s,%s)"""
    conn.execute(meet, (f"readai:pt-{TAG}-1", "Client sync", NOW - timedelta(hours=8), NOW - timedelta(hours=7),
                        json.dumps(["Send deck"]), NOW - timedelta(hours=7)))
    conn.execute(meet, (f"readai:pt-{TAG}-2", "Fresh call", NOW - timedelta(hours=3), NOW - timedelta(hours=2),
                        json.dumps(["x"]), NOW - timedelta(hours=2)))
    standup.update_config(conn, members=[UA, UB], excluded=[])
    standup.run_row(conn, today)
    conn.execute("UPDATE standup_runs SET asked_at = %s WHERE day = %s", (NOW - timedelta(hours=2), today))
    standup.save_answer(conn, UB, "Bala T", ["x"], ["y"], ["none"], day=today)
    sug = "INSERT INTO suggestions(client, code, kind, text, created_at, status) VALUES (%s,%s,'tweet','t',%s,%s)"
    conn.execute(sug, (CLIENT, "T1", NOW - timedelta(hours=50), "pending"))
    conn.execute(sug, (CLIENT, "T2", NOW - timedelta(hours=10), "pending"))
    conn.execute(sug, (CLIENT, "T4", NOW - timedelta(hours=5), "posted"))
    slot = "INSERT INTO calendar_slots(client, slot_at, code, status) VALUES (%s,%s,%s,%s)"
    conn.execute(slot, (CLIENT, NOW - timedelta(hours=3), "T3", "approved"))
    conn.execute(slot, (CLIENT, NOW - timedelta(hours=3), "T4", "reminded"))      # posted → no alert
    conn.execute(slot, (CLIENT, NOW + timedelta(hours=3), "T5", "approved"))      # not due yet
    conn.execute("""INSERT INTO postmortems(client, tweet_id, code, format, verdict, judged_at)
                    VALUES (%s,'991','T1','tweet','win',%s), (%s,'992','T2','tweet','flop',%s)""",
                 (CLIENT, NOW - timedelta(days=1), CLIENT, NOW - timedelta(days=1)))
    conn.commit()

    # ---- triggers
    proactive.scan(conn, NOW, PEOPLE)
    got = mine(conn, start)
    check((f"overdue:TASK-{a}", UA) in got and (f"overdue:TASK-{b}", UA) in got, "overdue ≥1 day → owner")
    check((f"overdue3:TASK-{b}", UO) in got and (f"overdue3:TASK-{a}", UO) not in got, "overdue ≥3 days → owners too")
    check((f"overdue:TASK-{e}", UA) not in got, "due today is not overdue")
    check(got[(f"overdue:TASK-{b}", UA)]["facts"]["days_overdue"] == 4, "facts carry days overdue")
    check((f"blocked:TASK-{c}", UO) in got and (f"blocked:TASK-{d}", UO) not in got, "blocked > 24h → owners only")
    check((f"meeting:readai:pt-{TAG}-1", UO) in got and (f"meeting:readai:pt-{TAG}-2", UO) not in got,
          "meeting not followed up 6h → owners")
    check((f"standup:{today}:{UA}", UA) in got and (f"standup:{today}:{UB}", UB) not in got,
          "standup: only the member who didn't answer")
    drafts = got.get((f"drafts:{CLIENT}", UO))
    check(drafts and drafts["facts"]["count"] == 1 and drafts["facts"]["codes"] == ["T1"], "drafts waiting > 48h → owners")
    check((f"slot:{CLIENT}:T3", UO) in got and (f"slot:{CLIENT}:T4", UO) not in got
          and (f"slot:{CLIENT}:T5", UO) not in got, "missed calendar slot (nothing posted) → watchers")
    check((f"win:{CLIENT}:991", UO) in got and (f"win:{CLIENT}:992", UO) not in got, "post-mortem win → watchers")
    check(all(r["status"] == "candidate" for r in got.values()), "stored as candidates")

    # ---- dedupe + cooldown
    check(not [r for r in proactive.scan(conn, NOW + timedelta(minutes=30), PEOPLE) if r["target_slack"] in (UO, UA, UB)],
          "second scan adds nothing (pending candidate dedupes)")
    ka, ks = (f"overdue:TASK-{a}", UA), (f"standup:{today}:{UA}", UA)
    proactive.skip(conn, got[ka]["id"], "test")
    proactive.skip(conn, got[ks]["id"], "test")
    again = {(r["key"], r["target_slack"]) for r in proactive.scan(conn, NOW + timedelta(hours=1), PEOPLE)}
    check(ka not in again, "skipped key stays quiet within 24h cooldown")
    conn.execute("UPDATE proactive_alerts SET created_at = created_at - interval '25 hours' WHERE id = ANY(%s)",
                 ([got[ka]["id"], got[ks]["id"]],))
    conn.commit()
    again = {(r["key"], r["target_slack"]) for r in proactive.scan(conn, NOW + timedelta(hours=1), PEOPLE)}
    check(ka in again and ks not in again, "after 24h the overdue key returns; standup is once a day")

    # ---- send: message rules, quiet hours, blocks
    got = mine(conn, start)
    rid = got[(f"overdue:TASK-{b}", UA)]["id"]
    try:
        proactive.send(conn, rid, "one\ntwo\nthree", NOW)
        check(False, "3-line message refused")
    except ValueError:
        check(True, "3-line message refused")
    res = proactive.send(conn, rid, "TASK is 4 days late.\nReply `done` or a new date.", NOW.replace(hour=22))
    check(not res["sent"] and "quiet" in res["held"], "quiet hours hold (22:00 IST)")
    check(proactive.send(conn, rid, "x", NOW.replace(hour=7))["held"], "quiet hours hold (07:00 IST)")
    check(proactive._get(conn, rid)["status"] == "candidate", "held alert stays pending")
    POSTS.clear()
    res = proactive.send(conn, rid, "TASK is 4 days late.\nReply `done` or a new date.", NOW)
    method, kw = POSTS[-1]
    ids = [el["action_id"] for el in kw["blocks"][1]["elements"]]
    check(res["sent"] and method == "chat.postMessage" and kw["channel"] == UA, "sent as a DM to the target")
    check(ids == ["px_pro_useful", "px_pro_later", "px_pro_never"]
          and json.loads(kw["blocks"][1]["elements"][0]["value"]) == {"id": rid}, "3 feedback buttons with {id}")
    try:
        proactive.send(conn, rid, "again", NOW)
        check(False, "can't send twice")
    except ValueError:
        check(True, "can't send twice")

    # ---- daily limit (owner has 6 candidates)
    owner_ids = [r["id"] for k, r in sorted(got.items(), key=lambda kv: kv[1]["id"]) if k[1] == UO]
    check(len(owner_ids) >= 5, "owner has several candidates")
    sent = [proactive.send(conn, i, f"alert {i}", NOW + timedelta(minutes=n))["sent"] for n, i in enumerate(owner_ids[:4])]
    check(sent == [True, True, True, False], "max 3 alerts per person per day")
    check(proactive.sent_today(conn, UO, NOW + timedelta(hours=1)) == 3, "sent_today counts IST day")
    check(proactive.send(conn, owner_ids[3], "next day", NOW + timedelta(days=1))["sent"], "limit resets next day")

    # ---- buttons: useful + never (mute forever)
    async def ack(*a, **k):
        return None

    body = {"user": {"id": UO}, "channel": {"id": "DOWNER"}, "message": {"ts": "1.2", "text": f"alert {owner_ids[0]}"}}
    POSTS.clear()
    asyncio.run(proactive.on_pro_useful(ack, body, {"value": json.dumps({"id": owner_ids[0]})}))
    check(proactive._get(conn, owner_ids[0])["status"] == "useful", "Useful click recorded")
    check(POSTS and POSTS[-1][0] == "chat.update" and "Thanks, noted" in json.dumps(POSTS[-1][1], ensure_ascii=False),
          "message updated: Thanks, noted")
    win = next(r for k, r in got.items() if k == (f"win:{CLIENT}:991", UO))
    if proactive._get(conn, win["id"])["status"] == "candidate":
        conn.execute("UPDATE proactive_alerts SET status = 'sent', sent_at = %s WHERE id = %s", (NOW, win["id"]))
        conn.commit()
    asyncio.run(proactive.on_pro_never(ack, body, {"value": json.dumps({"id": win["id"]})}))
    check(proactive._get(conn, win["id"])["status"] == "never" and proactive.muted(conn, UO, "win", NOW + timedelta(days=400)),
          "Never → that trigger muted forever")
    conn.execute("""INSERT INTO postmortems(client, tweet_id, code, format, verdict, judged_at)
                    VALUES (%s,'993','T9','tweet','win',%s)""", (CLIENT, NOW))
    conn.commit()
    check((f"win:{CLIENT}:993", UO) not in {(r["key"], r["target_slack"]) for r in proactive.scan(conn, NOW, PEOPLE)},
          "muted trigger produces no new candidates")
    POSTS.clear()
    asyncio.run(proactive.on_pro_later(ack, body, {"value": json.dumps({"id": win["id"]})}))
    check(proactive._get(conn, win["id"])["status"] == "not_now", "later click on a handled alert still recorded")

    # ---- backoff: 2 ignored in a row → muted 3 days
    ins = """INSERT INTO proactive_alerts(trigger, key, target_slack, subject, status, message, created_at, sent_at)
             VALUES ('overdue', %s, %s, 's', 'sent', 'm', %s, %s)"""
    conn.execute(ins, (f"overdue:bk1{TAG}", UB, NOW - timedelta(hours=60), NOW - timedelta(hours=60)))
    conn.execute(ins, (f"overdue:bk2{TAG}", UB, NOW - timedelta(hours=30), NOW - timedelta(hours=30)))
    conn.commit()
    proactive.scan(conn, NOW, PEOPLE)
    check(conn.execute("SELECT count(*) n FROM proactive_alerts WHERE target_slack = %s AND status = 'ignored'",
                       (UB,)).fetchone()["n"] == 2, "no click in 24h → ignored")
    check(proactive.muted(conn, UB, "overdue", NOW + timedelta(days=2))
          and not proactive.muted(conn, UB, "overdue", NOW + timedelta(days=4)), "2 ignored in a row → muted 3 days")
    check(not proactive.muted(conn, UA, "overdue", NOW), "one ignored is not enough to mute")

    # ---- skip, stats, examples, cron lines
    left = [r["id"] for r in proactive.pending(conn) if r["target_slack"] in (UO, UA, UB)]
    check(proactive.skip(conn, left[0], "already handled in standup"), "skip with reason")
    try:
        proactive.skip(conn, left[1], " ")
        check(False, "skip needs a reason")
    except ValueError:
        check(True, "skip needs a reason")
    s = proactive.stats(conn, 7)
    bad = sum(s["by_status"].get(k, 0) for k in proactive.NOT_USEFUL)
    check(s["total"] > 0 and s["false_alarm_rate"] == round(bad / s["total"], 2) and s["by_status"].get("useful", 0) >= 1,
          "stats: false-alarm rate = (skipped+not_now+never+ignored)/all")
    ex = proactive.examples(conn, UO)
    check(any(x["message"] == f"alert {owner_ids[0]}" for x in ex["useful"]) and
          any(x["status"] in proactive.NOT_USEFUL for x in ex["not_useful"]), "examples: useful vs not useful")
    check(proactive.pending_lines(conn, NOW.replace(hour=23), PEOPLE) == [], "cron lines empty in quiet hours")
    lines = proactive.pending_lines(conn, NOW, PEOPLE)
    check(any(f"proactive {left[1]} " in ln for ln in lines), "cron lines list pending candidates")
    out = json.loads(proactive.proactive_stats({"days": 7}))
    check(out["success"] and "false_alarm_rate" in out, "proactive_stats tool")
    out = json.loads(proactive.proactive_candidates({}))
    check(out["success"] and any(c["id"] == left[1] for c in out["candidates"]), "proactive_candidates tool")
    check(not json.loads(proactive.proactive_send({"id": left[1], "message": ""}))["success"], "empty message refused")


if __name__ == "__main__":
    main()
