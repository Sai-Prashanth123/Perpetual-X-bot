"""Proactive alerts: tell a person something before they ask, only when they'd thank us for it.

    triggers (model-free SQL)  → candidates {trigger, key, target_slack, subject, facts}
    scan()                      drops muted / already-alerted keys (cooldown), stores the rest
    Agency Bot (skill proactive-review) is the judge: proactive_send (≤2 lines + 3 buttons) or
                                proactive_skip with a reason (kept as a negative example)
    send()                      enforces quiet hours (09–21 IST), max 3 a day per person, mutes
    buttons                     Useful / Not now / Never → feedback; "never" mutes that trigger forever;
                                2 ignored in a row (no click in 24h) → that trigger muted 3 days

LLM judges over-alert (~50% false alarms in ProactiveAgent), so the skill defaults to skip and
`proactive_stats` tracks the false-alarm rate.

    jobs.py proactive-pending   (cron monitor) scan + one line per pending candidate; unchanged
                                output = no agent run; quiet hours = no output
"""

import asyncio
import json
import logging
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from . import ledger, notify

log = logging.getLogger("perpetual.proactive")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS proactive_alerts (
    id            SERIAL PRIMARY KEY,
    trigger       TEXT NOT NULL,
    key           TEXT NOT NULL,                    -- dedupe key, e.g. 'overdue:TASK-12'
    target_slack  TEXT NOT NULL,                    -- who gets it
    subject       TEXT,
    facts         JSONB,
    status        TEXT NOT NULL DEFAULT 'candidate', -- candidate|sent|skipped|useful|not_now|never|ignored|expired
    reason        TEXT,                             -- why the judge skipped it
    message       TEXT,                             -- what was sent (few-shot examples)
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at       TIMESTAMPTZ,
    feedback_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS proactive_alerts_key ON proactive_alerts (key, target_slack, created_at DESC);
CREATE INDEX IF NOT EXISTS proactive_alerts_target ON proactive_alerts (target_slack, sent_at DESC);
CREATE TABLE IF NOT EXISTS proactive_mutes (
    target_slack  TEXT NOT NULL,
    trigger       TEXT NOT NULL,
    until         TIMESTAMPTZ,                      -- NULL = forever
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (target_slack, trigger)
);
"""

TZ = ZoneInfo("Asia/Kolkata")
QUIET = (9, 21)                 # send only between 09:00 and 21:00 local
DAILY_MAX = 3                   # alerts per person per local day
IGNORE_AFTER_H = 24             # no click within this long = ignored
BACKOFF_DAYS = 3                # 2 ignored in a row → trigger muted this long
STALE_H = 24                    # candidates never judged expire
COOLDOWN_H = {"overdue": 24, "overdue_escalate": 24, "blocked": 24, "meeting": 24, "drafts": 24,
              "standup": None, "slot": None, "win": None}       # None = once per key, ever
SENT = ("sent", "useful", "not_now", "never", "ignored")
NOT_USEFUL = ("skipped", "not_now", "never", "ignored")
OPEN = ("open", "doing", "blocked")


def _now(now=None):
    return now or datetime.now(timezone.utc)


def _cand(trigger, key, target, subject, **facts):
    return {"trigger": trigger, "key": key, "target_slack": target, "subject": subject, "facts": facts}


def _iso(v):
    return v.isoformat() if v else None


# ---------------------------------------------------------------- triggers (model-free)

def t_overdue(conn, now, people=None):
    """Open task ≥1 day overdue → its owner; ≥3 days → the owners too."""
    today = now.astimezone(TZ).date()
    out = []
    for r in conn.execute("""SELECT id, title, owner_slack, owner_name, client, due, status FROM team_tasks
                             WHERE status IN ('open','doing','blocked') AND owner_slack IS NOT NULL AND due <= %s
                             ORDER BY due, id""", (today - timedelta(days=1),)):
        code, days = f"TASK-{r['id']}", (today - r["due"]).days
        facts = {"task": code, "title": r["title"], "owner": r["owner_name"], "client": r["client"],
                 "due": _iso(r["due"]), "days_overdue": days, "status": r["status"]}
        out.append(_cand("overdue", f"overdue:{code}", r["owner_slack"], f"{code} {r['title']} is {days}d overdue",
                         **facts))
        if days >= 3:
            out += [_cand("overdue_escalate", f"overdue3:{code}", o,
                          f"{r['owner_name'] or 'Someone'}'s {code} {r['title']} is {days}d overdue", **facts)
                    for o in notify.owners() if o != r["owner_slack"]]
    return out


def t_blocked(conn, now, people=None):
    """Task blocked for more than 24h (last change while blocked) → owners."""
    out = []
    for r in conn.execute("""SELECT id, title, owner_name, client, notes, updated_at FROM team_tasks
                             WHERE status = 'blocked' AND updated_at < %s ORDER BY id""", (now - timedelta(hours=24),)):
        code, hours = f"TASK-{r['id']}", int((now - r["updated_at"]).total_seconds() // 3600)
        note = (r["notes"] or "").strip().splitlines()[-1:] or [None]
        out += [_cand("blocked", f"blocked:{code}", o, f"{code} {r['title']} blocked for {hours}h",
                      task=code, title=r["title"], owner=r["owner_name"], client=r["client"], hours_blocked=hours,
                      last_note=note[0]) for o in notify.owners()]
    return out


def t_meeting(conn, now, people=None):
    """Meeting with action items not followed up 6h after it ended → owners."""
    out = []
    for r in conn.execute("""SELECT id, title, started_at, ended_at, fetched_at, action_items, url FROM meetings
                             WHERE processed_at IS NULL AND jsonb_array_length(coalesce(action_items, '[]'::jsonb)) > 0
                               AND coalesce(ended_at, fetched_at) < %s AND coalesce(ended_at, fetched_at) > %s
                             ORDER BY started_at""", (now - timedelta(hours=6), now - timedelta(days=7))):
        hours = int((now - (r["ended_at"] or r["fetched_at"])).total_seconds() // 3600)
        out += [_cand("meeting", f"meeting:{r['id']}", o,
                      f"'{r['title']}' ended {hours}h ago, {len(r['action_items'])} action items not followed up",
                      meeting=r["id"], title=r["title"], started=_iso(r["started_at"]), hours_since=hours,
                      action_items=r["action_items"][:8], url=r["url"]) for o in notify.owners()]
    return out


def t_standup(conn, now, people=None):
    """Standup member with no answer by summary time → that member (once a day)."""
    from . import standup
    cfg = standup.config(conn)
    if not cfg["enabled"]:
        return []
    local = now.astimezone(ZoneInfo(cfg["timezone"]))
    day = local.date()
    if not standup.is_day(cfg, day):
        return []
    _, summary_at, call_at = standup.moments(cfg, day)
    run = conn.execute("SELECT asked_at FROM standup_runs WHERE day = %s", (day,)).fetchone()
    if local < summary_at or not run or not run["asked_at"]:
        return []
    answered = {r["slack_user"] for r in conn.execute("SELECT slack_user FROM standup_answers WHERE day = %s", (day,))}
    return [_cand("standup", f"standup:{day}:{p['id']}", p["id"],
                  f"{p['real_name'] or p['name']} hasn't answered today's standup",
                  day=day.isoformat(), call_at=f"{call_at:%H:%M}", name=p["real_name"] or p["name"])
            for p in standup.members(cfg, people) if p["id"] not in answered]


def t_drafts(conn, now, people=None):
    """Client drafts pending review for more than 48h (they expire at 4 days) → owners."""
    out = []
    for r in conn.execute("""SELECT client, count(*) n, min(created_at) oldest,
                                    (array_agg(code ORDER BY created_at))[1:5] codes
                             FROM suggestions WHERE status = 'pending' AND created_at < %s AND created_at > %s
                             GROUP BY client ORDER BY client""", (now - timedelta(hours=48), now - timedelta(days=4))):
        hours = int((now - r["oldest"]).total_seconds() // 3600)
        out += [_cand("drafts", f"drafts:{r['client']}", o,
                      f"{r['n']} draft(s) for @{r['client']} waiting for review, oldest {hours}h",
                      client=r["client"], count=r["n"], oldest_hours=hours, codes=r["codes"],
                      expires_in_hours=max(0, 96 - hours)) for o in notify.owners()]
    return out


def t_slot(conn, now, people=None):
    """Approved calendar slot whose time passed 2h+ ago with nothing posted → whoever watches the client."""
    out = []
    for r in conn.execute("""SELECT c.client, c.code, c.slot_at, c.status, c.note FROM calendar_slots c
                             LEFT JOIN suggestions s ON s.client = c.client AND s.code = c.code
                             WHERE c.status IN ('approved','reminded') AND c.slot_at <= %s AND c.slot_at > %s
                               AND coalesce(s.status, '') <> 'posted' ORDER BY c.slot_at""",
                          (now - timedelta(hours=2), now - timedelta(hours=48))):
        hours = int((now - r["slot_at"]).total_seconds() // 3600)
        out += [_cand("slot", f"slot:{r['client']}:{r['code']}", o,
                      f"@{r['client']} {r['code']} was due {hours}h ago and isn't posted",
                      client=r["client"], code=r["code"], slot_at=_iso(r["slot_at"]), hours_late=hours, note=r["note"])
                for o in notify.client_watchers(conn, r["client"])]
    return out


def t_win(conn, now, people=None):
    """Post-mortem judged a win in the last 3 days, never shared → whoever watches the client."""
    out = []
    for r in conn.execute("""SELECT client, tweet_id, code, format, url, vs_median, why FROM postmortems
                             WHERE verdict = 'win' AND judged_at > %s ORDER BY judged_at""", (now - timedelta(days=3),)):
        out += [_cand("win", f"win:{r['client']}:{r['tweet_id']}", o, f"@{r['client']} {r['format'] or 'post'} was a win",
                      client=r["client"], code=r["code"], url=r["url"], vs_median=r["vs_median"], why=r["why"])
                for o in notify.client_watchers(conn, r["client"])]
    return out


TRIGGERS = [t_overdue, t_blocked, t_meeting, t_standup, t_drafts, t_slot, t_win]


# ---------------------------------------------------------------- scan, mutes, limits

def quiet(now=None):
    h = _now(now).astimezone(TZ).hour
    return h < QUIET[0] or h >= QUIET[1]


def mute(conn, target, trigger, until=None):
    """Mute a trigger for a person until `until` (None = forever). Never shortens an existing mute."""
    conn.execute("""INSERT INTO proactive_mutes(target_slack, trigger, until) VALUES (%s,%s,%s)
                    ON CONFLICT (target_slack, trigger) DO UPDATE SET until = CASE
                        WHEN proactive_mutes.until IS NULL OR EXCLUDED.until IS NULL THEN NULL
                        ELSE GREATEST(proactive_mutes.until, EXCLUDED.until) END""", (target, trigger, until))


def muted(conn, target, trigger, now=None):
    return bool(conn.execute("""SELECT 1 FROM proactive_mutes WHERE target_slack = %s AND trigger = %s
                                AND (until IS NULL OR until > %s)""", (target, trigger, _now(now))).fetchone())


def sent_today(conn, target, now=None):
    local = _now(now).astimezone(TZ)
    midnight = datetime.combine(local.date(), time(0), TZ)
    return conn.execute("SELECT count(*) n FROM proactive_alerts WHERE target_slack = %s AND status = ANY(%s) "
                        "AND sent_at >= %s", (target, list(SENT), midnight)).fetchone()["n"]


def _mark_ignored(conn, now):
    """Sent with no click for 24h → ignored; 2 ignored in a row for a trigger → mute it 3 days."""
    rows = conn.execute("""UPDATE proactive_alerts SET status = 'ignored' WHERE status = 'sent' AND sent_at < %s
                           RETURNING target_slack, trigger""", (now - timedelta(hours=IGNORE_AFTER_H),)).fetchall()
    for target, trigger in {(r["target_slack"], r["trigger"]) for r in rows}:
        last = conn.execute("""SELECT status FROM proactive_alerts WHERE target_slack = %s AND trigger = %s
                               AND status = ANY(%s) ORDER BY sent_at DESC LIMIT 2""",
                            (target, trigger, [s for s in SENT if s != "sent"])).fetchall()
        if len(last) == 2 and all(r["status"] == "ignored" for r in last):
            mute(conn, target, trigger, now + timedelta(days=BACKOFF_DAYS))


def _recent(conn, c, now):
    hours = COOLDOWN_H.get(c["trigger"], 24)
    cutoff = None if hours is None else now - timedelta(hours=hours)
    return bool(conn.execute("""SELECT 1 FROM proactive_alerts WHERE key = %s AND target_slack = %s
                                AND (status = 'candidate' OR %s::timestamptz IS NULL OR created_at > %s) LIMIT 1""",
                             (c["key"], c["target_slack"], cutoff, cutoff)).fetchone())


def scan(conn, now=None, people=None):
    """Run every trigger; store and return the candidates that are new (not muted, not in cooldown)."""
    now = _now(now)
    conn.execute("""UPDATE proactive_alerts SET status = 'expired', reason = 'not judged in time'
                    WHERE status = 'candidate' AND created_at < %s""", (now - timedelta(hours=STALE_H),))
    _mark_ignored(conn, now)
    conn.commit()
    found = []
    for fn in TRIGGERS:
        try:
            found += fn(conn, now, people)
        except Exception as e:  # noqa: BLE001 — one broken trigger (or Slack down) never blocks the rest
            conn.rollback()
            log.warning("trigger %s failed: %s", fn.__name__, e)
    new, seen = [], set()
    for c in found:
        k = (c["key"], c["target_slack"])
        if not c["target_slack"] or k in seen or muted(conn, c["target_slack"], c["trigger"], now) or _recent(conn, c, now):
            continue
        seen.add(k)
        new.append(conn.execute("""INSERT INTO proactive_alerts(trigger, key, target_slack, subject, facts, created_at)
                                   VALUES (%s,%s,%s,%s,%s,%s) RETURNING *""",
                                (c["trigger"], c["key"], c["target_slack"], c["subject"][:300],
                                 json.dumps(c["facts"], default=str), now)).fetchone())
    conn.commit()
    return new


def pending(conn):
    return conn.execute("SELECT * FROM proactive_alerts WHERE status = 'candidate' ORDER BY id").fetchall()


def pending_lines(conn, now=None, people=None):
    """For the cron monitor: nothing in quiet hours; else one stable line per pending candidate."""
    if quiet(now):
        return []
    scan(conn, now, people)
    return [f"proactive {r['id']} {r['trigger']} -> {r['target_slack']}: {r['subject']}" for r in pending(conn)]


# ---------------------------------------------------------------- judge actions

def blocks(aid, message):
    val = json.dumps({"id": aid})
    btn = lambda label, action_id: {"type": "button", "text": {"type": "plain_text", "text": label},  # noqa: E731
                                    "action_id": action_id, "value": val}
    return [{"type": "section", "text": {"type": "mrkdwn", "text": message}},
            {"type": "actions", "block_id": f"px_pro_{aid}",
             "elements": [btn("👍 Useful", "px_pro_useful"), btn("Not now", "px_pro_later"),
                          btn("Never for this", "px_pro_never")]}]


def _get(conn, aid):
    row = conn.execute("SELECT * FROM proactive_alerts WHERE id = %s", (int(aid),)).fetchone()
    if not row:
        raise ValueError(f"no alert {aid}")
    return row


def send(conn, aid, message, now=None):
    """DM the target with the message + 3 feedback buttons, if limits allow. Held = stays pending."""
    from . import slackui
    now = _now(now)
    row = _get(conn, aid)
    if row["status"] != "candidate":
        raise ValueError(f"alert {aid} is already {row['status']}")
    msg = (message or "").strip()
    if not msg:
        raise ValueError("write the message")
    if msg.count("\n") > 1 or len(msg) > 400:
        raise ValueError("keep it to 2 short lines (max 400 characters)")
    if muted(conn, row["target_slack"], row["trigger"], now):
        skip(conn, aid, "muted by the person")
        return {"sent": False, "skipped": "they muted this kind of alert"}
    if quiet(now):
        return {"sent": False, "held": f"quiet hours (send only {QUIET[0]:02d}:00-{QUIET[1]:02d}:00 IST); stays pending"}
    if sent_today(conn, row["target_slack"], now) >= DAILY_MAX:
        return {"sent": False, "held": f"daily limit ({DAILY_MAX}) reached for this person; stays pending"}
    slackui.slack("chat.postMessage", channel=row["target_slack"], text=msg, blocks=blocks(row["id"], msg),
                  unfurl_links=False)
    conn.execute("UPDATE proactive_alerts SET status = 'sent', message = %s, sent_at = %s WHERE id = %s",
                 (msg, now, row["id"]))
    conn.commit()
    return {"sent": True, "id": row["id"], "to": row["target_slack"]}


def skip(conn, aid, reason):
    if not (reason or "").strip():
        raise ValueError("give the reason (it teaches the next run)")
    n = conn.execute("UPDATE proactive_alerts SET status = 'skipped', reason = %s WHERE id = %s AND status = 'candidate'",
                     (reason.strip()[:500], int(aid))).rowcount
    conn.commit()
    return bool(n)


def feedback(conn, aid, status, now=None):
    """A button click: useful | not_now | never (never also mutes that trigger for them, forever)."""
    now = _now(now)
    row = conn.execute("""UPDATE proactive_alerts SET status = %s, feedback_at = %s
                          WHERE id = %s AND status = ANY(%s) RETURNING *""",
                       (status, now, int(aid), list(SENT))).fetchone()
    if row and status == "never":
        mute(conn, row["target_slack"], row["trigger"], None)
    conn.commit()
    return row


def stats(conn, days=7, now=None):
    rows = conn.execute("""SELECT trigger, status, count(*) n FROM proactive_alerts
                           WHERE created_at > %s AND status NOT IN ('candidate','expired') GROUP BY 1, 2""",
                        (_now(now) - timedelta(days=days),)).fetchall()
    by_status, by_trigger = {}, {}
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + r["n"]
        by_trigger.setdefault(r["trigger"], {})[r["status"]] = r["n"]
    total = sum(by_status.values())
    bad = sum(by_status.get(s, 0) for s in NOT_USEFUL)
    return {"days": days, "total": total, "by_status": by_status, "by_trigger": by_trigger,
            "false_alarm_rate": round(bad / total, 2) if total else None,
            "useful_rate": round(by_status.get("useful", 0) / total, 2) if total else None}


def examples(conn, target, n=6):
    """Recent alerts this person found useful vs not (incl. the judge's own skips), for few-shot judging."""
    k = max(1, n // 2)
    good = conn.execute("""SELECT trigger, subject, message FROM proactive_alerts WHERE target_slack = %s
                           AND status = 'useful' ORDER BY coalesce(feedback_at, created_at) DESC LIMIT %s""",
                        (target, k)).fetchall()
    bad = conn.execute("""SELECT trigger, subject, message, status, reason FROM proactive_alerts WHERE target_slack = %s
                          AND status = ANY(%s) ORDER BY coalesce(feedback_at, sent_at, created_at) DESC LIMIT %s""",
                       (target, list(NOT_USEFUL), k)).fetchall()
    return {"useful": [dict(r) for r in good], "not_useful": [dict(r) for r in bad]}


# ---------------------------------------------------------------- tools (Agency Bot)

def _ok(**d):
    return json.dumps({"success": True, **d}, ensure_ascii=False, default=str)


def _err(e):
    return json.dumps({"success": False, "error": str(e)})


def _tool(fn):
    def handler(params, **_):
        conn = ledger.connect()
        try:
            return _ok(**fn(conn, params or {}))
        except Exception as e:  # noqa: BLE001
            return _err(e)
        finally:
            conn.close()
    return handler


def _name(uid):
    from . import slackui
    try:
        return slackui.person_name(uid)
    except Exception:  # noqa: BLE001
        return uid


def _candidates(conn, params):
    rows = pending(conn)
    people = {t: {"name": _name(t), "sent_today": (s := sent_today(conn, t)), "left_today": max(0, DAILY_MAX - s),
                  "examples": examples(conn, t)} for t in dict.fromkeys(r["target_slack"] for r in rows)}
    return {"quiet_hours": quiet(), "count": len(rows), "people": people,
            "candidates": [{"id": r["id"], "trigger": r["trigger"], "to": r["target_slack"],
                            "to_name": people[r["target_slack"]]["name"], "subject": r["subject"], "facts": r["facts"],
                            "since": r["created_at"]} for r in rows]}


proactive_candidates = _tool(_candidates)
proactive_send = _tool(lambda conn, p: send(conn, p["id"], p.get("message")))
proactive_skip = _tool(lambda conn, p: {"skipped": skip(conn, p["id"], p.get("reason"))})
proactive_stats = _tool(lambda conn, p: stats(conn, int(p.get("days") or 7)))

_S, _I = {"type": "string"}, {"type": "integer"}
TOOLS = [
    ("proactive_candidates", proactive_candidates,
     "Pending proactive alerts (model-free triggers: overdue/blocked tasks, meetings not followed up, missing standup "
     "answers, drafts waiting, missed calendar slots, wins) with facts, and per person: alerts left today and examples "
     "of what they found useful or not. Follow skill proactive-review.", {}, []),
    ("proactive_send", proactive_send,
     "Send one alert: DM the person a message of at most 2 lines (the fact + the one action) with Useful / Not now / "
     "Never buttons. Quiet hours, the 3-a-day limit and mutes are enforced (held alerts stay pending).",
     {"id": _I, "message": _S}, ["id", "message"]),
    ("proactive_skip", proactive_skip,
     "Don't send an alert. reason = why a busy person wouldn't want it (kept as a negative example).",
     {"id": _I, "reason": _S}, ["id", "reason"]),
    ("proactive_stats", proactive_stats,
     "Proactive alert results over N days (default 7): counts by status and trigger, false-alarm rate.",
     {"days": _I}, []),
]


def register(ctx, toolset="proactive"):
    for name, fn, description, props, required in TOOLS:
        ctx.register_tool(name=name, toolset=toolset, handler=fn,
                          schema={"name": name, "description": description,
                                  "parameters": {"type": "object", "properties": props, "required": required}})


# ---------------------------------------------------------------- Slack buttons

THANKS = {"useful": "👍 Thanks, noted. I'll keep sending alerts like this.",
          "not_now": "👌 Thanks, noted. I'll hold off on this one.",
          "never": "🔕 Thanks, noted. I won't send this kind of alert again."}


def _click_db(aid, status):
    conn = ledger.connect()
    try:
        return feedback(conn, aid, status)
    finally:
        conn.close()


async def _click(ack, body, action, status):
    from . import slackui
    await ack()
    try:
        aid = int(json.loads(action.get("value") or "{}")["id"])
    except (ValueError, KeyError, TypeError):
        return
    try:
        row = await asyncio.to_thread(_click_db, aid, status)
    except Exception as e:  # noqa: BLE001
        log.error("proactive feedback failed: %s", e, exc_info=True)
        row = None
    note = THANKS[status] if row else "This alert was already handled."
    channel = (body.get("channel") or {}).get("id") or (body.get("container") or {}).get("channel_id")
    msg = body.get("message") or {}
    ts = msg.get("ts") or (body.get("container") or {}).get("message_ts")
    original = (row and row["message"]) or msg.get("text") or ""
    try:
        if channel and ts:
            await asyncio.to_thread(slackui.slack, "chat.update", channel=channel, ts=ts, text=original, blocks=[
                {"type": "section", "text": {"type": "mrkdwn", "text": original or " "}},
                {"type": "context", "elements": [{"type": "mrkdwn", "text": note}]}])
        else:
            await asyncio.to_thread(slackui.slack, "chat.postMessage", channel=(body.get("user") or {}).get("id"),
                                    text=note)
    except Exception as e:  # noqa: BLE001
        log.warning("proactive click reply failed: %s", e)


async def on_pro_useful(ack, body, action):
    await _click(ack, body, action, "useful")


async def on_pro_later(ack, body, action):
    await _click(ack, body, action, "not_now")


async def on_pro_never(ack, body, action):
    await _click(ack, body, action, "never")


ACTIONS = [("px_pro_useful", on_pro_useful), ("px_pro_later", on_pro_later), ("px_pro_never", on_pro_never)]
