"""Trend radar: catch a post that is taking off in a client's niche while it is still rising.

One model-free scan per client: the client's niche phrases go into one console search (any_of,
Top, last day, min likes), and posts younger than a few hours that are gaining likes fast become a
request for the client's agent, which drafts an original take and a quote-tweet (trend-jack) and
sends one compact alert. Capped per client per day; a tweet is never routed to the same client twice.
"""

from datetime import datetime, timezone

from . import agency, console

SCHEMA_SQL = "ALTER TABLE clients ADD COLUMN IF NOT EXISTS trend_enabled BOOLEAN NOT NULL DEFAULT true;"

MIN_LIKES = 300           # search floor: only posts that already have traction
MIN_LIKES_PER_H = 150     # still accelerating
MAX_AGE_H = 3             # young enough to ride
DAILY_CAP = 2             # trend alerts per client per day
MAX_PHRASES = 12          # niche phrases per search (keeps the query short)


def phrases(text):
    return [p.strip() for p in (text or "").replace("\n", ",").split(",") if p.strip()]


def client_topics(conn):
    """{client: [niche phrases]} for live clients (channel set) with the trend radar on."""
    rows = conn.execute("""SELECT handle, niche_topics FROM clients
                           WHERE active AND slack_channel IS NOT NULL AND trend_enabled AND platform = 'x'""").fetchall()
    out = {}
    for r in rows:
        topics = phrases(r["niche_topics"])[:MAX_PHRASES]
        if topics:
            out[r["handle"]] = topics
    return out


def _search(topics):
    payload = {"any_of": topics, "since": console._days_ago(1), "product": "Top", "min_likes": MIN_LIKES,
               "lang": "en", "exclude_retweets": True, "include_replies": False, "limit": 40, "max_pages": 1}
    _, items = console.run_actor("search", payload)
    return [console.compact_tweet(t) for t in items if not t.get("is_retweet")]


def alerts_today(conn, client):
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return conn.execute("SELECT count(*) AS n FROM alerts WHERE client = %s AND kind = 'trend' AND at >= %s",
                        (client, start)).fetchone()["n"]


def rising(tweet, client=None):
    if tweet.get("is_reply") or not tweet.get("id") or tweet.get("age_h") is None:
        return False
    if client and (tweet.get("by") or "").lstrip("@").lower() == client.lower():
        return False
    return tweet["age_h"] <= MAX_AGE_H and (tweet.get("likes_per_h") or 0) >= MIN_LIKES_PER_H


def _age(age_h):
    minutes = max(1, round(age_h * 60))
    return f"{minutes} min" if minutes < 90 else f"{round(age_h, 1):g} h"


def scan(conn, search=_search):
    """Route rising niche posts to their clients. Returns [(client, tweet_id)]."""
    from . import ledger
    routed = []
    for client, topics in client_topics(conn).items():
        if alerts_today(conn, client) >= DAILY_CAP:
            continue
        try:
            tweets = search(topics)
        except Exception as e:  # noqa: BLE001 — one failed search shouldn't stop the others
            print(f"⚠️ trend search failed for @{client}: {e}")
            continue
        hits = sorted((t for t in tweets if rising(t, client)), key=lambda t: -(t.get("likes_per_h") or 0))
        for t in hits:
            if alerts_today(conn, client) >= DAILY_CAP:
                break
            if not ledger.filter_new_targets(conn, client, [t["id"]]):
                continue
            agency.add_request(conn, client, (
                f"TREND rising in your niche: @{t.get('by')} posted {_age(t['age_h'])} ago, "
                f"{t.get('likes', 0)} likes ({round(t.get('likes_per_h') or 0)} likes/h). Follow trend-jack.\n"
                f"Tweet: {t.get('url')}\nTweet id: {t['id']}\nText: {(t.get('text') or '')[:600]}"),
                by="Trend radar", kind="trend")
            conn.execute("INSERT INTO alerts(client, kind, tweet_id) VALUES (%s, 'trend', %s)", (client, t["id"]))
            conn.execute("""INSERT INTO seen_targets(client, tweet_id, used_for) VALUES (%s, %s, 'trend')
                            ON CONFLICT DO NOTHING""", (client, t["id"]))
            conn.commit()
            routed.append((client, t["id"]))
    return routed
