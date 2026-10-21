"""Agency-wide views: the client roster (managed by chat, stored in Postgres), daily metric and
viral snapshots, the overview the Main X agent and the Slack Home tab read, and requests to a
client's bot."""

import json
from datetime import date, datetime, timedelta, timezone

from . import console

VIRAL_MIN_LIKES = 150
CLIENT_FIELDS = ("display_name", "niche_topics", "watch_accounts", "avoid_accounts", "timezone",
                 "slack_channel", "employee_slack")


def _today():
    return datetime.now(timezone.utc).date()


def norm_handle(handle):
    return (handle or "").strip().lstrip("@").lower()


# ---------------------------------------------------------------- roster (Postgres is the source of truth)

def upsert_client(conn, handle, **fields):
    """Create or update a client. Only the fields passed (and not None) change; a new client is active."""
    handle = norm_handle(handle)
    if not handle:
        raise ValueError("handle is required")
    values = {k: _join(v) if k in ("niche_topics", "watch_accounts", "avoid_accounts") else v
              for k, v in fields.items() if k in CLIENT_FIELDS and v is not None}
    insert = {"display_name": handle, **values}            # a new client needs a display name
    conn.execute(
        f"""INSERT INTO clients(handle, active, updated_at, {", ".join(insert)})
            VALUES (%s, TRUE, now(){", %s" * len(insert)})
            ON CONFLICT (handle) DO UPDATE SET active = TRUE, updated_at = now()
            {"".join(f", {k} = EXCLUDED.{k}" for k in values)}""",
        [handle, *insert.values()])
    conn.commit()
    return get_client(conn, handle)


def get_client(conn, handle):
    return conn.execute("SELECT * FROM clients WHERE handle = %s", (norm_handle(handle),)).fetchone()


def set_client_active(conn, handle, active):
    row = conn.execute("UPDATE clients SET active = %s, updated_at = now() WHERE handle = %s RETURNING handle",
                       (active, norm_handle(handle))).fetchone()
    conn.commit()
    return bool(row)


def list_clients(conn, include_inactive=False):
    q = "SELECT * FROM clients" + ("" if include_inactive else " WHERE active") + " ORDER BY handle"
    return conn.execute(q).fetchall()


def active_clients(conn):
    return list_clients(conn)


def sync_clients(conn, clients):
    """Seed from clients.yaml: upsert each entry. Clients added by chat are never touched here."""
    for c in clients:
        upsert_client(conn, c["handle"], display_name=c.get("name"), niche_topics=c.get("niche_topics"),
                      watch_accounts=c.get("watch_accounts"), avoid_accounts=c.get("avoid_accounts"),
                      timezone=c.get("timezone"), slack_channel=c.get("slack_channel"),
                      employee_slack=c.get("employee"))


def _join(v):
    if v is None:
        return None
    return ", ".join(v) if isinstance(v, (list, tuple)) else str(v)


def console_onboard(handle, display_name):
    """Put the client on the scraping console roster (daily tracking) and start a history harvest."""
    handle = norm_handle(handle)
    roster = console._request("GET", "/roster") or []
    if not any((r.get("username") or "").lower() == handle for r in roster):
        console._request("POST", "/roster", {"username": handle, "display_name": display_name or handle,
                                             "group": "Agency clients"})
        added = True
    else:
        added = False
    console._request("POST", "/jobs", {"actor": "client-content", "input": {
        "username": handle, "tabs": ["posts", "replies"], "limit": 300, "max_pages": 10}})
    return {"added_to_roster": added, "harvest_started": True}


def competitors(handle, watch_accounts, days=14):
    """The client vs its watch accounts over the last `days` (live scrape; cache the result)."""
    names = [norm_handle(handle)] + [norm_handle(w) for w in (watch_accounts or "").split(",") if w.strip()][:6]
    rows = []
    for name in names:
        data = json.loads(console.x_user_tweets({"username": name, "since_days": days, "limit": 40}))
        tweets = data.get("tweets") or []
        n = len(tweets)
        rows.append({"handle": name, "is_client": name == names[0], "posts": n,
                     "avg_likes": round(sum(t.get("likes") or 0 for t in tweets) / n, 1) if n else None,
                     "avg_views": round(sum(t.get("views") or 0 for t in tweets) / n) if n else None,
                     "best": max(tweets, key=lambda t: t.get("likes") or 0) if tweets else None,
                     "error": None if data.get("success") else data.get("error")})
    return rows


# ---------------------------------------------------------------- snapshots (no LLM)

def snapshot_metrics(conn, days_back=1):
    """Store each client's daily totals and follower count. days_back>1 backfills."""
    done = 0
    for c in active_clients(conn):
        handle = c["handle"]
        followers = _followers_by_day(handle, days_back + 1)
        for offset in range(1, days_back + 1):
            day = _today() - timedelta(days=offset)
            try:
                s = console._request("GET", f"/clients/{handle}/summary",
                                     params={"since": day.isoformat(), "until": day.isoformat(), "top": 1})
            except RuntimeError:
                continue
            t = (s or {}).get("totals") or {}
            conn.execute(
                """INSERT INTO metrics_daily(client, day, followers, posts, likes, replies, reposts, bookmarks, views, raw)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (client, day) DO UPDATE SET followers = COALESCE(EXCLUDED.followers, metrics_daily.followers),
                       posts = EXCLUDED.posts, likes = EXCLUDED.likes, replies = EXCLUDED.replies,
                       reposts = EXCLUDED.reposts, bookmarks = EXCLUDED.bookmarks, views = EXCLUDED.views,
                       raw = EXCLUDED.raw""",
                (handle, day, followers.get(day), t.get("posts"), t.get("likes"), t.get("replies"),
                 t.get("reposts"), t.get("bookmarks"), t.get("views"), json.dumps(t)))
            done += 1
        conn.commit()
    return done


def _followers_by_day(handle, days):
    try:
        data = console._request("GET", f"/clients/{handle}/followers", params={"days": days})
    except RuntimeError:
        return {}
    out = {}
    for p in (data or {}).get("points", []):
        try:
            day = datetime.fromisoformat(p["observed_at"]).date()
        except (KeyError, ValueError):
            continue
        out[day] = p.get("followers_count")  # last observation of the day wins
    return out


def snapshot_viral(conn):
    """Today's top tweets per client niche plus the console's niche-wide meta."""
    today = _today()
    rows = 0
    niches = {}
    for c in active_clients(conn):
        for topic in (c["niche_topics"] or "").split(","):
            topic = topic.strip()
            if topic:
                niches.setdefault(topic.lower(), topic)
    for key, topic in niches.items():
        try:
            # Exact phrase, English, no replies: keeps "Clay GTM" from matching pottery videos.
            data = json.loads(console.x_find_tweets({
                "raw_query": f'"{topic}" lang:en -filter:replies min_faves:{VIRAL_MIN_LIKES} '
                             f'since:{(_today() - timedelta(days=2)).isoformat()}',
                "limit": 20}))
        except Exception:  # noqa: BLE001 — one bad topic shouldn't stop the snapshot
            continue
        rows += _store_viral(conn, today, key, data.get("tweets") or [])
    try:
        meta = console._request("GET", "/trends/meta") or {}
        rows += _store_viral(conn, today, "_meta", [console.compact_tweet(e) for e in meta.get("exemplars") or []])
    except RuntimeError:
        pass
    conn.commit()
    return rows


def _store_viral(conn, day, niche, tweets):
    for t in tweets:
        conn.execute(
            """INSERT INTO viral_feed(day, niche, tweet_id, url, author, text, likes, views, bookmarks, likes_per_h, posted_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (day, niche, tweet_id) DO UPDATE SET likes = EXCLUDED.likes, views = EXCLUDED.views,
                   bookmarks = EXCLUDED.bookmarks, likes_per_h = EXCLUDED.likes_per_h""",
            (day, niche, t.get("id"), t.get("url"), t.get("by"), (t.get("text") or "")[:1200], t.get("likes"),
             t.get("views"), t.get("bookmarks"), t.get("likes_per_h"), t.get("at")))
    return len(tweets)


# ---------------------------------------------------------------- reads

def viral(conn, days=1, niche=None, limit=25):
    q = """SELECT DISTINCT ON (tweet_id) niche, tweet_id, url, author, text, likes, views, bookmarks,
                  likes_per_h, posted_at
           FROM viral_feed WHERE day >= %s"""
    args = [_today() - timedelta(days=days - 1)]
    if niche:
        q += " AND niche = %s"
        args.append(niche.lower())
    q = f"SELECT * FROM ({q} ORDER BY tweet_id, day DESC) v ORDER BY likes DESC NULLS LAST LIMIT %s"
    args.append(limit)
    return [_jsonable(r) for r in conn.execute(q, args)]


def overview(conn, days=7):
    """Per-client scorecard: growth, output, reach, suggestion use, and attention flags."""
    since, prev = _today() - timedelta(days=days), _today() - timedelta(days=2 * days)
    out = []
    for c in active_clients(conn):
        h = c["handle"]
        cur = _period(conn, h, since, _today())
        before = _period(conn, h, prev, since)
        foll = conn.execute(
            """SELECT (SELECT followers FROM metrics_daily WHERE client=%s AND followers IS NOT NULL
                       ORDER BY day DESC LIMIT 1) AS now,
                      (SELECT followers FROM metrics_daily WHERE client=%s AND followers IS NOT NULL AND day <= %s
                       ORDER BY day DESC LIMIT 1) AS then""", (h, h, since)).fetchone()
        sug = conn.execute(
            """SELECT COUNT(*) AS suggested,
                      COUNT(*) FILTER (WHERE status IN ('approved','edited','posted')) AS used,
                      COUNT(*) FILTER (WHERE status = 'posted') AS posted,
                      COUNT(*) FILTER (WHERE status = 'rejected') AS rejected,
                      COUNT(*) FILTER (WHERE status = 'pending') AS pending
               FROM suggestions WHERE client = %s AND created_at >= %s""", (h, since)).fetchone()
        last_post = conn.execute("SELECT max(day) AS d FROM metrics_daily WHERE client=%s AND posts > 0",
                                 (h,)).fetchone()["d"]
        flags = []
        if last_post is None or (_today() - last_post).days > 3:
            flags.append("no posts in 3+ days")
        if before["views"] and cur["views"] < 0.7 * before["views"]:
            flags.append("reach down 30%+")
        if sug["suggested"] >= 10 and sug["used"] == 0:
            flags.append("suggestions unused")
        out.append({
            "client": h, "name": c["display_name"], "employee": c["employee_slack"],
            "followers": foll["now"],
            "followers_change": (foll["now"] - foll["then"]) if foll["now"] and foll["then"] else None,
            **{f"{k}": cur[k] for k in ("posts", "likes", "views", "bookmarks", "replies")},
            "views_change_pct": _pct(cur["views"], before["views"]),
            "likes_change_pct": _pct(cur["likes"], before["likes"]),
            "engagement_rate": round(100 * (cur["likes"] + cur["replies"] + cur["reposts"] + cur["bookmarks"])
                                     / cur["views"], 2) if cur["views"] else None,
            "suggestions": dict(sug), "flags": flags,
        })
    out.sort(key=lambda r: r["views"] or 0, reverse=True)
    return {"window_days": days, "clients": out, "team": team(conn, days)}


def _period(conn, client, start, end):
    r = conn.execute(
        """SELECT COALESCE(SUM(posts),0) posts, COALESCE(SUM(likes),0) likes, COALESCE(SUM(views),0) views,
                  COALESCE(SUM(bookmarks),0) bookmarks, COALESCE(SUM(replies),0) replies,
                  COALESCE(SUM(reposts),0) reposts
           FROM metrics_daily WHERE client = %s AND day >= %s AND day < %s""", (client, start, end)).fetchone()
    return {k: int(v) for k, v in r.items()}


def _pct(now, before):
    return round(100 * (now - before) / before, 1) if before else None


def team(conn, days=7):
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = conn.execute(
        """SELECT COALESCE(f.by_name, '(unknown)') AS employee, f.client,
                  COUNT(*) AS actions,
                  COUNT(*) FILTER (WHERE f.action IN ('approve','edit')) AS approved,
                  COUNT(*) FILTER (WHERE f.action = 'edit') AS edited,
                  COUNT(*) FILTER (WHERE f.action = 'reject') AS rejected,
                  COUNT(*) FILTER (WHERE f.action = 'posted') AS posted,
                  ROUND(EXTRACT(EPOCH FROM percentile_cont(0.5) WITHIN GROUP (ORDER BY f.at - s.created_at)) / 60)
                      AS median_minutes_to_act
           FROM feedback_events f JOIN suggestions s ON s.client = f.client AND s.code = f.code
           WHERE f.at >= %s GROUP BY 1, 2 ORDER BY actions DESC""", (since,))
    return [_jsonable(r) for r in rows]


def client_series(conn, client, days=60):
    rows = conn.execute(
        """SELECT day, followers, posts, likes, views, bookmarks, replies, reposts FROM metrics_daily
           WHERE client = %s AND day >= %s ORDER BY day""", (client, _today() - timedelta(days=days)))
    return [_jsonable(r) for r in rows]


def _jsonable(row):
    out = {}
    for k, v in dict(row).items():
        if isinstance(v, (datetime, date)):
            v = v.isoformat()
        elif v is not None and not isinstance(v, (int, float, str, bool, list, dict)):
            v = float(v)
        out[k] = v
    return out


# ---------------------------------------------------------------- dashboard -> bot requests

def add_request(conn, client, request, by=None):
    row = conn.execute("INSERT INTO agency_requests(client, request, by_name) VALUES (%s,%s,%s) RETURNING id",
                       (client, request, by)).fetchone()
    conn.commit()
    return row["id"]


def pending_requests(conn, client):
    return conn.execute(
        "SELECT id, request, by_name, created_at FROM agency_requests WHERE client = %s AND picked_up_at IS NULL ORDER BY id",
        (client,)).fetchall()


def take_requests(conn, client):
    rows = conn.execute(
        """UPDATE agency_requests SET picked_up_at = now() WHERE client = %s AND picked_up_at IS NULL
           RETURNING id, request, by_name""", (client,)).fetchall()
    conn.commit()
    return [dict(r) for r in rows]
