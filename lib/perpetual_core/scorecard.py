"""Weekly team scorecard (model-free): every Friday evening, per standup member:

    tasks done this week · on-time rate (done date <= due) · overdue open · blocked · standup answer rate

plus a few highlights, posted to the reports channel (HQ_SLACK_CHANNEL). The week runs Saturday..Friday
in the standup timezone. `tick` posts once per week (scorecard_runs), so the cron can run every
15 minutes on Fridays whatever the server timezone is.
"""

import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import ledger, notify, standup
from .settings import env

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS scorecard_runs (
    week_end   DATE PRIMARY KEY,
    posted_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""
POST_AT = (4, 18)          # Friday (Mon=0), 18:00 local
MAX_LINES = 25


def week_end_for(today, weeks_back=0):
    """Latest Friday on or before `today`, minus whole weeks."""
    return today - timedelta(days=(today.weekday() - 4) % 7 + 7 * int(weeks_back or 0))


def _name(p):
    return p["real_name"] or p["display_name"] or p["name"] or p["id"]


def compute(conn, week_end, people=None):
    cfg = standup.config(conn)
    tz, start = cfg["timezone"], week_end - timedelta(days=6)
    asked = [r["day"] for r in conn.execute(
        "SELECT day FROM standup_runs WHERE day BETWEEN %s AND %s AND asked_at IS NOT NULL", (start, week_end))]
    rows = []
    for p in standup.members(cfg, people):
        t = conn.execute(
            """WITH d AS (SELECT *, (done_at AT TIME ZONE %s)::date AS done_day FROM team_tasks WHERE owner_slack = %s)
               SELECT count(*) FILTER (WHERE status = 'done' AND done_day BETWEEN %s AND %s) AS done,
                      count(*) FILTER (WHERE status = 'done' AND done_day BETWEEN %s AND %s AND due IS NOT NULL) AS with_due,
                      count(*) FILTER (WHERE status = 'done' AND done_day BETWEEN %s AND %s AND done_day <= due) AS on_time,
                      count(*) FILTER (WHERE status IN ('open','doing','blocked') AND due < %s) AS overdue,
                      count(*) FILTER (WHERE status = 'blocked') AS blocked
               FROM d""", (tz, p["id"], start, week_end, start, week_end, start, week_end, week_end)).fetchone()
        answered = conn.execute("SELECT count(*) n FROM standup_answers WHERE slack_user = %s AND day = ANY(%s)",
                                (p["id"], asked)).fetchone()["n"] if asked else 0
        rows.append({
            "id": p["id"], "name": _name(p), "done": t["done"], "with_due": t["with_due"], "on_time": t["on_time"],
            "on_time_rate": round(100 * t["on_time"] / t["with_due"]) if t["with_due"] else None,
            "overdue": t["overdue"], "blocked": t["blocked"], "standups_asked": len(asked),
            "standups_answered": answered,
            "standup_rate": round(100 * answered / len(asked)) if asked else None,
        })
    rows.sort(key=lambda r: (-r["done"], r["name"]))
    return rows


def highlights(rows):
    out = []
    top = rows[0] if rows and rows[0]["done"] else None
    if top:
        out.append(f"🏆 Most done: *{top['name']}* ({top['done']})")
    perfect = [r["name"].split(" ")[0] for r in rows if r["on_time_rate"] == 100 and r["with_due"] >= 2]
    if perfect:
        out.append("⏱ Every dated task on time: " + ", ".join(perfect))
    blocked = [f"{r['name'].split(' ')[0]} ({r['blocked']})" for r in rows if r["blocked"]]
    if blocked:
        out.append("🚧 Blocked, needs help: " + ", ".join(blocked))
    late = [f"{r['name'].split(' ')[0]} ({r['overdue']})" for r in rows if r["overdue"] >= 3]
    if late:
        out.append("⏰ 3+ overdue: " + ", ".join(late))
    quiet = [r["name"].split(" ")[0] for r in rows if r["standup_rate"] is not None and r["standup_rate"] < 50]
    if quiet:
        out.append("🙋 Missed most standups: " + ", ".join(quiet))
    return out[:4]


def _pct(v):
    return "–" if v is None else f"{v}%"


def text(rows, week_end):
    start = week_end - timedelta(days=6)
    head = [f"*📊 Team scorecard · {start:%d %b} – {week_end:%a %d %b}*",
            "_done · on time · overdue · blocked · standups_"]
    hl = highlights(rows)
    room = MAX_LINES - len(head) - (len(hl) + 1 if hl else 0)
    body = [f"• *{r['name']}* — ✅ {r['done']} · ⏱ {_pct(r['on_time_rate'])} · ⏰ {r['overdue']} · 🚧 {r['blocked']}"
            f" · 🗣 {r['standups_answered']}/{r['standups_asked']}" for r in rows] or ["_No team members found._"]
    if len(body) > room:
        body = body[:room - 1] + [f"…and {len(body) - room + 1} more"]
    return "\n".join(head + body + (["*Highlights*"] + hl if hl else []))


def post(conn, week_end=None, people=None):
    cfg = standup.config(conn)
    week_end = week_end or week_end_for(datetime.now(ZoneInfo(cfg["timezone"])).date())
    msg = text(compute(conn, week_end, people), week_end)
    ch = env("HQ_SLACK_CHANNEL")
    try:
        if not ch:
            raise RuntimeError("no HQ_SLACK_CHANNEL")
        from . import slackui
        slackui.slack("chat.postMessage", channel=ch, text=msg, unfurl_links=False)
    except Exception:  # noqa: BLE001
        notify.dm(notify.owners(), msg)
    conn.execute("INSERT INTO scorecard_runs(week_end) VALUES (%s) ON CONFLICT (week_end) DO UPDATE SET posted_at = now()",
                 (week_end,))
    conn.commit()
    return msg


def tick(conn, now=None, people=None):
    """Post once, on Friday at/after 18:00 in the standup timezone. Returns the text when posted."""
    tz = ZoneInfo(standup.config(conn)["timezone"])
    now = (now or datetime.now(tz)).astimezone(tz)
    if now.weekday() != POST_AT[0] or now.hour < POST_AT[1]:
        return None
    if conn.execute("SELECT 1 FROM scorecard_runs WHERE week_end = %s", (now.date(),)).fetchone():
        return None
    return post(conn, now.date(), people)


# ---------------------------------------------------------------- tools

def team_scorecard(params, **_):
    conn = ledger.connect()
    try:
        params = params or {}
        cfg = standup.config(conn)
        week_end = week_end_for(datetime.now(ZoneInfo(cfg["timezone"])).date(), params.get("weeks_back") or 0)
        if params.get("post"):
            return json.dumps({"success": True, "posted": True, "text": post(conn, week_end)}, ensure_ascii=False)
        rows = compute(conn, week_end)
        return json.dumps({"success": True, "week": [str(week_end - timedelta(days=6)), str(week_end)],
                           "members": rows, "highlights": highlights(rows), "text": text(rows, week_end)},
                          ensure_ascii=False, default=str)
    except Exception as e:  # noqa: BLE001
        return json.dumps({"success": False, "error": str(e)})
    finally:
        conn.close()


TOOLS = [
    ("team_scorecard", team_scorecard,
     "Weekly team scorecard (week ending Friday): per member tasks done, on-time rate, overdue open, blocked, "
     "standup answer rate, highlights. weeks_back 1 = last week. post=true posts it to the reports channel.",
     {"weeks_back": {"type": "integer"}, "post": {"type": "boolean"}}, []),
]


def register(ctx, toolset="scorecard"):
    for name, fn, description, props, required in TOOLS:
        ctx.register_tool(name=name, toolset=toolset, handler=fn,
                          schema={"name": name, "description": description,
                                  "parameters": {"type": "object", "properties": props, "required": required}})
