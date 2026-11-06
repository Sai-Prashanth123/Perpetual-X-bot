"""Daily team rhythm, run from Slack (only the chosen team members).

    T - lead (default 45 min)   DM each member: yesterday / today / blockers + their open tasks;
                                today's Slack List ("4-11-2026") is created and shared with the team
    members reply in that DM    Agency Bot records it (standup_answer), adds today's items as tasks
                                (they appear in the List with assignee + due date), ticks finished ones
    T - 5 min                   summary in the standup channel + the List link
    check-in (15:00)            DM each member with open tasks: progress? blockers? (blocked → owners told)
    EOD (20:15, after the call) DM each member: what got done, what moves to tomorrow
    every tick                  List → tasks sync: ticked boxes = done, hand-added rows = new tasks

All timing is model-free (`jobs.py standup-tick`, every 5 minutes). Settings are changed in chat:
"standup is at 11:30 IST in #standup", "add Naga to standup", "pause standup".
"""

import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import ledger, notify, tasks
from .settings import env

DEFAULTS = {"time": "11:30", "timezone": "Asia/Kolkata", "days": "1,2,3,4,5", "lead_minutes": 45,
            "summary_minutes": 5, "enabled": True}


# ---------------------------------------------------------------- settings

def config(conn):
    row = conn.execute("SELECT * FROM standup_config WHERE id = 1").fetchone()
    if not row:
        conn.execute("""INSERT INTO standup_config(id, time, timezone, days, lead_minutes, summary_minutes, enabled)
                        VALUES (1,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING""",
                     (DEFAULTS["time"], DEFAULTS["timezone"], DEFAULTS["days"], DEFAULTS["lead_minutes"],
                      DEFAULTS["summary_minutes"], DEFAULTS["enabled"]))
        conn.commit()
        row = conn.execute("SELECT * FROM standup_config WHERE id = 1").fetchone()
    return row


def channel(cfg):
    return cfg["channel"] or env("HQ_SLACK_CHANNEL")


def members(cfg, people=None):
    from . import slackui
    everyone = people if people is not None else slackui.people()
    ids = cfg["members"]
    excluded = set(cfg["excluded"] or [])
    chosen = [p for p in everyone if p["id"] in ids] if ids else everyone
    return [p for p in chosen if p["id"] not in excluded]


def update_config(conn, **changes):
    cfg = config(conn)
    sets, vals = [], []
    for k in ("time", "timezone", "days", "lead_minutes", "summary_minutes", "enabled", "channel", "members", "excluded",
              "checkin_time", "eod_time"):
        if k in changes and changes[k] is not None:
            v = changes[k]
            if k in ("time", "checkin_time", "eod_time"):
                h, m = (int(x) for x in str(v).replace(".", ":").split(":")[:2])
                v = f"{h:02d}:{m:02d}"
            if k == "timezone":
                ZoneInfo(v)                                # raises on a bad zone
            if k in ("members", "excluded"):
                v = json.dumps(v)
            sets.append(f"{k} = %s")
            vals.append(v)
    if sets:
        conn.execute(f"UPDATE standup_config SET {', '.join(sets)} WHERE id = 1", vals)
        conn.commit()
    return config(conn) if sets else cfg


# ---------------------------------------------------------------- timing

def moments(cfg, today):
    """(ask_at, summary_at, call_at) as aware datetimes for `today` in the standup timezone."""
    tz = ZoneInfo(cfg["timezone"])
    h, m = (int(x) for x in cfg["time"].split(":"))
    call = datetime(today.year, today.month, today.day, h, m, tzinfo=tz)
    return call - timedelta(minutes=cfg["lead_minutes"]), call - timedelta(minutes=cfg["summary_minutes"]), call


def is_day(cfg, today):
    return str(today.isoweekday()) in {d.strip() for d in (cfg["days"] or "").split(",")}


def run_row(conn, day):
    conn.execute("INSERT INTO standup_runs(day) VALUES (%s) ON CONFLICT (day) DO NOTHING", (day,))
    return conn.execute("SELECT * FROM standup_runs WHERE day = %s", (day,)).fetchone()


def tick(conn, now=None, people=None):
    """Called every few minutes. Sends the questions and the summary once each, at the right time."""
    cfg = config(conn)
    if not cfg["enabled"]:
        return None
    now = now or datetime.now(ZoneInfo(cfg["timezone"]))
    now = now.astimezone(ZoneInfo(cfg["timezone"]))
    today = now.date()
    if not is_day(cfg, today):
        return None
    ask_at, summary_at, call_at = moments(cfg, today)
    did = None
    run = run_row(conn, today)
    if ask_at <= now < call_at:
        if not run["asked_at"]:
            ask(conn, cfg, today, people)
            did = "asked"
        if now >= summary_at and not run["summary_at"]:
            post_summary(conn, cfg, today, people)
            did = "summary"
    checkin_at, eod_at = _at(cfg, today, cfg["checkin_time"]), _at(cfg, today, cfg["eod_time"])
    if checkin_at <= now < checkin_at + timedelta(hours=2) and not run["checkin_at"]:
        checkin(conn, cfg, today, people)
        did = "checkin"
    if eod_at <= now < eod_at + timedelta(hours=3) and not run["eod_at"]:
        eod(conn, cfg, today, people)
        did = "eod"
    try:
        from . import lists
        # today's List holds everyone's open tasks, including ones still pending from earlier days
        lists.fill(conn, today, [p["id"] for p in members(cfg, people)])
        lists.sync(conn, today)
    except Exception:  # noqa: BLE001 — Lists not available (scope/plan): tasks still work in chat
        pass
    conn.commit()
    return did


def _at(cfg, day, hhmm):
    tz = ZoneInfo(cfg["timezone"])
    h, m = (int(x) for x in (hhmm or "00:00").split(":"))
    return datetime(day.year, day.month, day.day, h, m, tzinfo=tz)


def _first(p):
    return (p["real_name"] or p["display_name"] or p["name"] or "").split(" ")[0]


def _list_link(conn, today, crew):
    """Today's Slack List (created on first use); '' if Lists aren't available."""
    try:
        from . import lists
        row = lists.ensure_daily(conn, today, [p["id"] for p in crew])
        return f"<{row['url']}|{lists.list_name(today)}>" if row["url"] else lists.list_name(today)
    except Exception:  # noqa: BLE001
        return ""


def checkin(conn, cfg, today, people=None):
    sent = 0
    for p in members(cfg, people):
        rows = [r for r in tasks.listing(conn, p["id"], "open", limit=15) if not r["due"] or r["due"] <= today]
        if not rows:
            continue
        sent += notify.dm([p["id"]], (
            f"👋 Quick check-in, {_first(p)}: how are these going?\n" + "\n".join(tasks.line(r, today) for r in rows)
            + "\nReply with `done TASK-n` for finished ones, or tell me what's blocking you and I'll get help."))
    conn.execute("UPDATE standup_runs SET checkin_at = now() WHERE day = %s", (today,))
    conn.commit()
    return sent


def eod(conn, cfg, today, people=None):
    sent = 0
    for p in members(cfg, people):
        rows = [r for r in tasks.listing(conn, p["id"], "open", limit=20) if not r["due"] or r["due"] <= today]
        done_today = conn.execute("SELECT count(*) n FROM team_tasks WHERE owner_slack = %s AND status = 'done' "
                                  "AND done_at::date = %s", (p["id"], today)).fetchone()["n"]
        if not rows and not done_today:
            continue
        sent += notify.dm([p["id"]], (
            f"🌙 End of day, {_first(p)}: {done_today} done today. "
            + ("Still open:\n" + "\n".join(tasks.line(r, today) for r in rows)
               + "\nReply with what you finished (`done TASK-n`), and anything that moves to tomorrow "
                 "(\"TASK-n tomorrow\"). Anything from the EOD call I should add?" if rows
               else "Everything's done. Anything from the EOD call I should add for tomorrow?")))
    conn.execute("UPDATE standup_runs SET eod_at = now() WHERE day = %s", (today,))
    conn.commit()
    return sent


INTRO = (
    "👋 Hi {first}! I'm *Agency Bot*, the team's assistant. From now on I'll help run our day:\n"
    "• *{ask}* (before the {call} standup) I'll DM you: what you did yesterday, what you'll do today, any "
    "blockers. Just reply here.\n"
    "• I turn your answer into tasks in today's Slack List (like *{listname}*), with you as assignee and a due date.\n"
    "• *{checkin}* a quick check-in: progress or blockers. If you're stuck, I tell Sai right away.\n"
    "• *{eod}* after the EOD call: what got done, what moves to tomorrow.\n"
    "• Anytime: `my tasks`, `done TASK-4`, `move TASK-3 to Monday`, or ask me for research, drafts, your calendar, "
    "Google Docs or Notion.\n"
    "Tick tasks in the List or tell me here; both stay in sync. 🙌")


def send_intro(conn, people=None, force=False):
    cfg = config(conn)
    if cfg["intro_sent_at"] and not force:
        return 0
    from . import lists
    today = datetime.now(ZoneInfo(cfg["timezone"])).date()
    ask_at, _, call_at = moments(cfg, today)
    sent = 0
    for p in members(cfg, people):
        sent += notify.dm([p["id"]], INTRO.format(first=_first(p), ask=f"{ask_at:%H:%M}", call=f"{call_at:%H:%M}",
                                                  checkin=cfg["checkin_time"], eod=cfg["eod_time"],
                                                  listname=lists.list_name(today)))
    conn.execute("UPDATE standup_config SET intro_sent_at = now() WHERE id = 1")
    conn.commit()
    return sent


# ---------------------------------------------------------------- asking + answers

def ask(conn, cfg, today, people=None):
    _, _, call_at = moments(cfg, today)
    crew = members(cfg, people)
    link = _list_link(conn, today, crew)
    sent = 0
    for p in crew:
        open_rows = tasks.listing(conn, p["id"], "open", limit=15)
        mine = "\n".join(tasks.line(r, today) for r in open_rows) or "_No open tasks._"
        first = (p["real_name"] or p["display_name"] or p["name"] or "").split(" ")[0]
        sent += notify.dm([p["id"]], (
            f"☀️ Morning {first}! Standup is at *{call_at:%H:%M}* ({cfg['timezone']}). Reply here in one message:\n"
            "*Yesterday:* what you finished\n*Today:* what you'll do\n*Blockers:* anything in your way (or none)\n\n"
            f"Your open tasks:\n{mine}" + (f"\n\nToday's list: {link}" if link else "")))
    conn.execute("UPDATE standup_runs SET asked_at = now(), asked_count = %s WHERE day = %s", (sent, today))
    conn.commit()
    return sent


def save_answer(conn, user, name, yesterday=None, today_items=None, blockers=None, day=None):
    day = day or datetime.now(ZoneInfo(config(conn)["timezone"])).date()
    run_row(conn, day)
    conn.execute("""INSERT INTO standup_answers(day, slack_user, name, yesterday, today, blockers)
                    VALUES (%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (day, slack_user) DO UPDATE SET yesterday = EXCLUDED.yesterday,
                        today = EXCLUDED.today, blockers = EXCLUDED.blockers, answered_at = now()""",
                 (day, user, name, json.dumps(yesterday or []), json.dumps(today_items or []),
                  json.dumps(blockers or [])))
    conn.commit()


def answers(conn, day):
    return conn.execute("SELECT * FROM standup_answers WHERE day = %s ORDER BY answered_at", (day,)).fetchall()


# ---------------------------------------------------------------- summary

def summary_text(conn, cfg, day, people=None):
    from . import slackui  # noqa: F401  (people() lives there)
    got = {a["slack_user"]: a for a in answers(conn, day)}
    crew = members(cfg, people)
    link = _list_link(conn, day, crew)
    lines = [f"*Standup · {day:%a %d %b}* · {len(got)}/{len(crew)} answered" + (f" · 📋 {link}" if link else "")]
    blockers = []
    for p in crew:
        a = got.get(p["id"])
        name = p["real_name"] or p["display_name"] or p["name"]
        if not a:
            continue
        today_items = a["today"] or []
        lines.append(f"\n*{name}*")
        lines += [f"• {t}" for t in today_items[:6]] or ["• (no plan given)"]
        for b in a["blockers"] or []:
            if b and str(b).strip().lower() not in ("none", "no", "nothing", "-"):
                blockers.append(f"• *{name}:* {b}")
    if blockers:
        lines.append("\n🚧 *Blockers*")
        lines += blockers
    overdue = tasks.listing(conn, None, "open", overdue=True, limit=10)
    if overdue:
        lines.append("\n⏰ *Overdue tasks*")
        lines += [tasks.line(r, day) + (f" · {r['owner_name']}" if r["owner_name"] else "") for r in overdue]
    missing = [p for p in crew if p["id"] not in got]
    if missing:
        lines.append("\n🙋 *No answer yet:* " + ", ".join((p["real_name"] or p["name"]).split(" ")[0] for p in missing))
    return "\n".join(lines)


def post_summary(conn, cfg, day, people=None):
    from . import slackui
    ch = channel(cfg)
    text = summary_text(conn, cfg, day, people)
    if ch:
        try:
            slackui.slack("chat.postMessage", channel=ch, text=text, unfurl_links=False)
        except Exception:  # noqa: BLE001
            notify.dm(notify.owners(), text)
    conn.execute("UPDATE standup_runs SET summary_at = now() WHERE day = %s", (day,))
    conn.commit()
    return text


# ---------------------------------------------------------------- tools

def _ok(**d):
    return json.dumps({"success": True, **d}, ensure_ascii=False, default=str)


def _err(e):
    return json.dumps({"success": False, "error": str(e)})


def _asker():
    from . import connections
    return connections.current_user()


def standup_answer(params, **_):
    """The asker's standup reply, already split by the agent into lists."""
    from . import slackui
    user = _asker()
    if not user:
        return _err("Only a person can answer the standup.")
    conn = ledger.connect()
    try:
        save_answer(conn, user, slackui.person_name(user), params.get("yesterday"), params.get("today"),
                    params.get("blockers"))
        return _ok(saved=True, note="Now add today's items with task_add (source 'standup', owner = them) unless "
                                    "an open task already covers it, and mark finished tasks done with task_update.")
    except Exception as e:  # noqa: BLE001
        return _err(e)
    finally:
        conn.close()


def standup_settings(params, **_):
    """Read or change the standup settings."""
    from . import slackui
    conn = ledger.connect()
    try:
        changes = {k: params.get(k) for k in ("time", "timezone", "days", "lead_minutes", "summary_minutes",
                                              "enabled", "channel", "checkin_time", "eod_time")}
        cfg = config(conn)
        if params.get("channel"):
            changes["channel"] = params["channel"].strip("<>#").split("|")[0]
        ids, excluded = list(cfg["members"] or []), list(cfg["excluded"] or [])
        for who in params.get("add_members") or []:
            oid, _, problem = tasks.resolve_owner(who)
            if problem:
                return _err(problem)
            if oid in excluded:
                excluded.remove(oid)
            if ids and oid not in ids:
                ids.append(oid)
        for who in params.get("remove_members") or []:
            oid, _, problem = tasks.resolve_owner(who)
            if problem:
                return _err(problem)
            if ids and oid in ids:
                ids.remove(oid)
            elif oid not in excluded:
                excluded.append(oid)
        if params.get("only_members"):
            ids = []
            for who in params["only_members"]:
                oid, _, problem = tasks.resolve_owner(who)
                if problem:
                    return _err(problem)
                ids.append(oid)
            excluded = []
        changes["members"], changes["excluded"] = ids, excluded
        cfg = update_config(conn, **changes)
        ask_at, summary_at, call_at = moments(cfg, date.today())
        crew = members(cfg)
        return _ok(time=cfg["time"], timezone=cfg["timezone"], days=cfg["days"], enabled=cfg["enabled"],
                   questions_at=f"{ask_at:%H:%M}", summary_at=f"{summary_at:%H:%M}",
                   checkin_at=cfg["checkin_time"], eod_at=cfg["eod_time"],
                   channel=channel(cfg), members=[p["real_name"] or p["name"] for p in crew],
                   who_gets_it="everyone in Slack" if not cfg["members"] else "chosen members")
    except Exception as e:  # noqa: BLE001
        return _err(e)
    finally:
        conn.close()


def standup_status(params, **_):
    conn = ledger.connect()
    try:
        cfg = config(conn)
        day = datetime.now(ZoneInfo(cfg["timezone"])).date()
        if params.get("action") == "ask_now":
            return _ok(sent=ask(conn, cfg, day))
        if params.get("action") == "summary_now":
            return _ok(posted=post_summary(conn, cfg, day))
        if params.get("action") == "intro":
            return _ok(sent=send_intro(conn, force=True))
        if params.get("action") == "checkin_now":
            return _ok(sent=checkin(conn, cfg, day))
        if params.get("action") == "eod_now":
            return _ok(sent=eod(conn, cfg, day))
        return _ok(summary=summary_text(conn, cfg, day))
    except Exception as e:  # noqa: BLE001
        return _err(e)
    finally:
        conn.close()


_S = {"type": "string"}
_L = {"type": "array", "items": _S}
TOOLS = [
    ("standup_answer", standup_answer,
     "Record the asking person's standup reply (they answer the morning DM). Split it into yesterday / today / "
     "blockers lists of short items. Then add today's items as tasks and mark finished ones done.",
     {"yesterday": _L, "today": _L, "blockers": _L}, []),
    ("standup_settings", standup_settings,
     "Show or change the daily standup: time 'HH:MM', timezone (e.g. Asia/Kolkata), days '1,2,3,4,5' (Mon=1), "
     "lead_minutes (questions before the call, default 45), channel for the summary, add_members / "
     "remove_members / only_members (names), enabled false to pause. No arguments = show settings.",
     {"time": _S, "timezone": _S, "days": _S, "lead_minutes": {"type": "integer"}, "checkin_time": _S, "eod_time": _S,
      "summary_minutes": {"type": "integer"}, "channel": _S, "enabled": {"type": "boolean"},
      "add_members": _L, "remove_members": _L, "only_members": _L}, []),
    ("standup_status", standup_status,
     "Today's standup so far (who answered, plans, blockers). action 'ask_now' sends the questions now; "
     "'summary_now' posts the summary now.",
     {"action": {"type": "string", "enum": ["show", "ask_now", "summary_now", "checkin_now", "eod_now", "intro"]}}, []),
]


def register(ctx, toolset="standup"):
    for name, fn, description, props, required in TOOLS:
        ctx.register_tool(name=name, toolset=toolset, handler=fn,
                          schema={"name": name, "description": description,
                                  "parameters": {"type": "object", "properties": props, "required": required}})
