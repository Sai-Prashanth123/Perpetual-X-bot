"""Client goals and results vs goals.

Goals come from each client's strategy doc ("+2,000 followers by Dec 31", "500k impressions a month",
"40 replies per post", "2 posts a day"). Progress is computed from metrics_daily (one row per client per
day, written for yesterday by agency.snapshot_metrics), so the last complete day is today - 1.

    total      window start..as_of vs a straight-line share of the target (plus projection)
    per_day    trailing 7 days vs target * 7
    per_week   trailing 7 days vs target
    per_month  trailing 30 days vs target
    per_post   metric / posts over the trailing 30 days vs target
    engagement_rate (any period): 100 * (likes+replies+reposts+bookmarks) / views, vs target (%)

Status: ahead >= 110% of pace, on_track >= 90%, behind below, no_data when nothing to measure.
"""

import json
from datetime import date, timedelta

from . import ledger

METRICS = ("followers_gain", "impressions", "likes", "replies", "reposts", "bookmarks", "posts", "engagement_rate")
PERIODS = ("total", "per_month", "per_week", "per_post", "per_day")
COLUMN = {"impressions": "views", "likes": "likes", "replies": "replies", "reposts": "reposts",
          "bookmarks": "bookmarks", "posts": "posts"}
WINDOW = {"per_day": (7, 1), "per_week": (7, 7), "per_month": (30, 30), "per_post": (30, None)}
LABEL = {"followers_gain": "followers gained", "impressions": "impressions", "engagement_rate": "engagement rate %"}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS client_goals (
    id          SERIAL PRIMARY KEY,
    client      TEXT NOT NULL,
    metric      TEXT NOT NULL CHECK (metric IN ('followers_gain','impressions','likes','replies','reposts',
                                                'bookmarks','posts','engagement_rate')),
    target      NUMERIC NOT NULL,
    period      TEXT NOT NULL CHECK (period IN ('total','per_month','per_week','per_post','per_day')),
    start_date  DATE NOT NULL DEFAULT current_date,
    end_date    DATE,
    note        TEXT,
    created_by  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    active      BOOLEAN NOT NULL DEFAULT true
);
CREATE INDEX IF NOT EXISTS client_goals_client ON client_goals(client, active);
"""


def _date(v, today=None):
    if v is None or v == "":
        return None
    if isinstance(v, date):
        return v
    from .tasks import parse_due
    return parse_due(v, today)


def _client(c):
    return str(c or "").strip().lstrip("@").lower()


# ---------------------------------------------------------------- writes / reads

def set_goal(conn, client, metric, target, period, end_date=None, start_date=None, note=None, created_by=None,
             today=None):
    client, metric, period = _client(client), str(metric).strip().lower(), str(period).strip().lower()
    if not client:
        raise ValueError("client is required")
    if metric not in METRICS:
        raise ValueError(f"metric must be one of {', '.join(METRICS)}")
    if period not in PERIODS:
        raise ValueError(f"period must be one of {', '.join(PERIODS)}")
    if period == "per_post" and metric in ("followers_gain", "posts", "engagement_rate"):
        raise ValueError(f"{metric} can't be measured per post")
    today = today or date.today()
    start, end = _date(start_date, today) or today, _date(end_date, today)
    if period == "total" and not end:
        raise ValueError("a total goal needs an end_date")
    if end and end < start:
        raise ValueError("end_date is before start_date")
    return conn.execute(
        """INSERT INTO client_goals(client, metric, target, period, start_date, end_date, note, created_by)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
        (client, metric, float(target), period, start, end, note, created_by)).fetchone()


def list_goals(conn, client=None, active=True):
    q, vals = ["1=1"], []
    if client:
        q.append("client = %s")
        vals.append(_client(client))
    if active:
        q.append("active")
    return conn.execute(f"SELECT * FROM client_goals WHERE {' AND '.join(q)} ORDER BY client, id", vals).fetchall()


def remove_goal(conn, goal_id):
    row = conn.execute("UPDATE client_goals SET active = false WHERE id = %s RETURNING *", (int(goal_id),)).fetchone()
    if not row:
        raise ValueError(f"no goal {goal_id}")
    return row


# ---------------------------------------------------------------- measuring

def _followers(conn, client, start, end):
    """Followers gained between the level on/before `start` (else first in window) and the last <= end."""
    q = "SELECT followers FROM metrics_daily WHERE client = %s AND followers IS NOT NULL AND day {} %s ORDER BY day {} LIMIT 1"
    base = conn.execute(q.format("<=", "DESC"), (client, start)).fetchone() \
        or conn.execute(q.format(">=", "ASC"), (client, start)).fetchone()
    last = conn.execute(q.format("<=", "DESC"), (client, end)).fetchone()
    if not base or not last:
        return None
    return last["followers"] - base["followers"]


def _sums(conn, client, start, end):
    r = conn.execute(
        """SELECT count(*) n, count(views) nv, SUM(posts) posts, SUM(likes) likes, SUM(replies) replies,
                  SUM(reposts) reposts, SUM(bookmarks) bookmarks, SUM(views) views
           FROM metrics_daily WHERE client = %s AND day BETWEEN %s AND %s""", (client, start, end)).fetchone()
    return r if r["n"] else None


def _actual(conn, g, start, end, per_post=False):
    """The measured value for goal g over [start, end]; None = no data."""
    m = g["metric"]
    if m == "followers_gain":
        return _followers(conn, g["client"], start, end)
    s = _sums(conn, g["client"], start, end)
    if not s:
        return None
    if m == "engagement_rate":
        if not s["views"]:
            return None
        eng = sum(int(s[k] or 0) for k in ("likes", "replies", "reposts", "bookmarks"))
        return round(100 * eng / int(s["views"]), 2)
    v = s[COLUMN[m]]
    if v is None:
        return None
    if per_post:
        return round(int(v) / int(s["posts"]), 1) if s["posts"] else None
    return int(v)


def _status(pct):
    return "ahead" if pct >= 110 else "on_track" if pct >= 90 else "behind"


def evaluate(conn, g, today=None):
    today = today or date.today()
    target = float(g["target"])
    start, end = g["start_date"], g["end_date"]
    as_of = min(today - timedelta(days=1), end) if end else today - timedelta(days=1)
    out = {"id": g["id"], "client": g["client"], "metric": g["metric"], "period": g["period"], "target": target,
           "start_date": start, "end_date": end, "as_of": as_of, "note": g["note"],
           "ended": bool(end and today > end), "actual": None, "expected": None, "pct": None, "status": "no_data"}
    out["label"] = label(g)
    if as_of < start:
        return {**out, "reason": f"starts {start:%d %b}" if start > today - timedelta(days=1) else "no complete day yet"}
    elapsed = (as_of - start).days + 1
    ratio = g["metric"] == "engagement_rate" or g["period"] == "per_post"
    if g["period"] == "total":
        w_start = start
        if ratio:
            expected = target
        else:
            total_days = (end - start).days + 1
            expected = target * elapsed / total_days
    else:
        days, period_days = WINDOW[g["period"]]
        n = min(days, elapsed)
        w_start = as_of - timedelta(days=n - 1)
        expected = target if ratio else target * n / period_days
    actual = _actual(conn, g, w_start, as_of, per_post=g["period"] == "per_post")
    out.update(window=[w_start, as_of])
    if actual is None:
        return {**out, "reason": "no metrics in the window"}
    pct = round(100 * actual / expected, 1) if expected else (100.0 if actual >= 0 else 0.0)
    out.update(actual=actual, expected=round(expected, 2), pct=pct, status=_status(pct))
    if g["period"] == "total" and not ratio:
        total_days = (end - start).days + 1
        left = (end - as_of).days
        out["projected"] = round(actual / elapsed * total_days)
        out["needed_per_day"] = round(max(target - actual, 0) / left, 1) if left > 0 else None
    return out


def progress(conn, client=None, today=None):
    return [evaluate(conn, g, today) for g in list_goals(conn, client)]


def off_pace(conn, today=None):
    """{client: [behind results]} across every client with active goals."""
    out = {}
    for r in progress(conn, None, today):
        if r["status"] == "behind":
            out.setdefault(r["client"], []).append(r)
    return out


# ---------------------------------------------------------------- text

def label(g):
    t = float(g["target"])
    tgt = f"{t:,.0f}" if t == int(t) else f"{t:,.2f}"
    what = LABEL.get(g["metric"], g["metric"])
    if g["period"] == "total":
        return f"+{tgt} {what} by {g['end_date']:%d %b %Y}" if g["metric"] != "engagement_rate" \
            else f"{tgt}% engagement rate by {g['end_date']:%d %b %Y}"
    return f"{tgt} {what} {g['period'].replace('_', ' ')}"


ICON = {"ahead": "🚀", "on_track": "✅", "behind": "⚠️", "no_data": "❔"}


def _num(v):
    return "–" if v is None else f"{v:,.0f}" if abs(v) >= 100 or v == int(v) else f"{v:,.2f}"


def line(r):
    if r["status"] == "no_data":
        return f"{ICON['no_data']} @{r['client']} · {r['label']}: no data ({r.get('reason', '')})"
    s = f"{ICON[r['status']]} @{r['client']} · {r['label']}: {_num(r['actual'])} vs {_num(r['expected'])} expected ({r['pct']:.0f}%)"
    if r.get("needed_per_day"):
        s += f" · needs {_num(r['needed_per_day'])}/day"
    return s


def text(results):
    if not results:
        return "No client goals set."
    order = {"behind": 0, "no_data": 1, "on_track": 2, "ahead": 3}
    return "\n".join(line(r) for r in sorted(results, key=lambda r: (order[r["status"]], r["client"], r["id"])))


# ---------------------------------------------------------------- tools

def _ok(**d):
    return json.dumps({"success": True, **d}, ensure_ascii=False, default=str)


def _err(e):
    return json.dumps({"success": False, "error": str(e)})


def _asker():
    from . import connections
    return connections.current_user()


def _tool(fn):
    def handler(params, **_):
        conn = ledger.connect()
        try:
            return fn(conn, params or {})
        except Exception as e:  # noqa: BLE001
            conn.rollback()
            return _err(e)
        finally:
            conn.close()
    return handler


def _set(conn, p):
    row = set_goal(conn, p["client"], p["metric"], p["target"], p["period"], p.get("end_date"), p.get("start_date"),
                   p.get("note"), _asker())
    conn.commit()
    return _ok(goal=dict(row), label=label(row), progress=evaluate(conn, row))


def _list(conn, p):
    rows = list_goals(conn, p.get("client"))
    return _ok(count=len(rows), goals=[{**dict(r), "label": label(r)} for r in rows])


def _progress(conn, p):
    res = progress(conn, p.get("client"))
    behind = sorted({r["client"] for r in res if r["status"] == "behind"})
    return _ok(count=len(res), results=res, off_pace_clients=behind, text=text(res))


def _remove(conn, p):
    row = remove_goal(conn, p["id"])
    conn.commit()
    return _ok(removed=row["id"], label=label(row))


def _publish(conn, p):
    from . import slackui
    res = slackui.publish_doc(conn, _client(p["client"]), p["title"], p["markdown"], p["summary"], kind="report",
                              asked_by=_asker())
    return _ok(**res, note="Published for the team only. To send it to the client, use request_approval.")


_S = {"type": "string"}
TOOLS = [
    ("goals_set", _tool(_set),
     "Set a client goal from the strategy doc. metric: " + "|".join(METRICS) + "; period: " + "|".join(PERIODS)
     + ". Examples: +2000 followers by Dec 31 = followers_gain 2000 total end_date 2026-12-31; 500k impressions a "
       "month = impressions 500000 per_month; 40 replies per post = replies 40 per_post; 2 posts a day = posts 2 "
       "per_day; engagement_rate target is a percent. start_date defaults to today.",
     {"client": _S, "metric": {"type": "string", "enum": list(METRICS)}, "target": {"type": "number"},
      "period": {"type": "string", "enum": list(PERIODS)}, "end_date": _S, "start_date": _S, "note": _S},
     ["client", "metric", "target", "period"]),
    ("goals_list", _tool(_list), "List active client goals (one client, or all).", {"client": _S}, []),
    ("goals_progress", _tool(_progress),
     "Results vs goals from real daily metrics: actual, expected by now (linear pace), pct, status "
     "on_track|behind|ahead|no_data. Omit client for every client (off_pace_clients lists who is behind).",
     {"client": _S}, []),
    ("goals_remove", _tool(_remove), "Retire a goal by id (from goals_list).", {"id": {"type": "integer"}}, ["id"]),
    ("results_publish", _tool(_publish),
     "Publish a results report as ONE document (canvas) in the client's Slack channel, for the team only. "
     "Never reaches the client: client-facing sends go through request_approval.",
     {"client": _S, "title": _S, "markdown": _S, "summary": _S}, ["client", "title", "markdown", "summary"]),
]


def register(ctx, toolset="goals"):
    for name, fn, description, props, required in TOOLS:
        ctx.register_tool(name=name, toolset=toolset, handler=fn,
                          schema={"name": name, "description": description,
                                  "parameters": {"type": "object", "properties": props, "required": required}})
