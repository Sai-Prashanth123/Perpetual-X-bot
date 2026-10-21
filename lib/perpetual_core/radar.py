"""Early-reply radar: be among the first replies when a big account in a client's niche posts.

One model-free scan covers every client: it searches the union of all radar accounts (a few
console jobs), keeps posts younger than ~45 minutes, and hands each new one to the matching
client's agent as a request. The agent drafts two replies and sends one compact alert. Capped per
client per day; a tweet is never routed to the same client twice.
"""

from datetime import datetime, timezone

from . import agency, console

MAX_AGE_H = 0.75          # the reply window that still lands near the top
DAILY_CAP = 4             # alerts per client per day
CHUNK = 15                # accounts per search query


def handles(text):
    return [h.strip().lstrip("@").lower() for h in (text or "").replace("\n", ",").split(",") if h.strip()]


def client_accounts(conn):
    """{client: set(radar handles)} for clients that are live (have a channel) and have the radar on."""
    rows = conn.execute("""SELECT handle, radar_accounts, watch_accounts FROM clients
                           WHERE active AND slack_channel IS NOT NULL AND radar_enabled""").fetchall()
    out = {}
    for r in rows:
        accts = set(handles(r["radar_accounts"]) or handles(r["watch_accounts"]))
        accts.discard(r["handle"])
        if accts:
            out[r["handle"]] = accts
    return out


def _search(accounts):
    payload = {"from_accounts": accounts, "since": console._days_ago(1), "product": "Latest",
               "exclude_retweets": True, "include_replies": False, "limit": 40, "max_pages": 1}
    _, items = console.run_actor("search", payload)
    return [console.compact_tweet(t) for t in items if not t.get("is_retweet")]


def alerts_today(conn, client):
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return conn.execute("SELECT count(*) AS n FROM alerts WHERE client = %s AND kind = 'radar' AND at >= %s",
                        (client, start)).fetchone()["n"]


def fresh(tweet, max_age_h=MAX_AGE_H):
    return (not tweet.get("is_reply") and tweet.get("id") and tweet.get("age_h") is not None
            and tweet["age_h"] <= max_age_h)


def scan(conn, search=_search):
    """Route fresh posts from radar accounts to their clients. Returns [(client, tweet_id)]."""
    from . import ledger
    wanted = client_accounts(conn)
    if not wanted:
        return []
    every = sorted(set().union(*wanted.values()))
    tweets = []
    for i in range(0, len(every), CHUNK):
        try:
            tweets += search(every[i:i + CHUNK])
        except Exception as e:  # noqa: BLE001 — one failed chunk shouldn't stop the others
            print(f"⚠️ radar search failed for {len(every[i:i + CHUNK])} accounts: {e}")
    routed = []
    for t in sorted(filter(fresh, tweets), key=lambda t: t.get("age_h", 1)):
        author = (t.get("by") or "").lower()
        for client, accts in wanted.items():
            if author not in accts or alerts_today(conn, client) >= DAILY_CAP:
                continue
            if not ledger.filter_new_targets(conn, client, [t["id"]]):
                continue
            minutes = max(1, round(t["age_h"] * 60))
            agency.add_request(conn, client, (
                f"RADAR early-reply window: @{t.get('by')} posted {minutes} min ago "
                f"({t.get('likes', 0)} likes, {t.get('replies', 0)} replies so far). Follow radar-reply.\n"
                f"Tweet: {t.get('url')}\nTweet id: {t['id']}\nText: {(t.get('text') or '')[:600]}"),
                by="Radar", kind="radar")
            conn.execute("INSERT INTO alerts(client, kind, tweet_id) VALUES (%s, 'radar', %s)", (client, t["id"]))
            conn.execute("""INSERT INTO seen_targets(client, tweet_id, used_for) VALUES (%s, %s, 'radar')
                            ON CONFLICT DO NOTHING""", (client, t["id"]))
            conn.commit()
            routed.append((client, t["id"]))
    return routed


def set_accounts(conn, client, add=(), remove=()):
    row = conn.execute("SELECT radar_accounts, watch_accounts FROM clients WHERE handle = %s", (client,)).fetchone()
    current = handles(row["radar_accounts"]) or handles(row["watch_accounts"])
    for h in handles(",".join(add)):
        if h not in current:
            current.append(h)
    drop = set(handles(",".join(remove)))
    current = [h for h in current if h not in drop]
    conn.execute("UPDATE clients SET radar_accounts = %s WHERE handle = %s", (",".join(current), client))
    conn.commit()
    return current
