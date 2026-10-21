"""Post-mortems: once a client's post is 48 hours old, compare it with the client's own 30-day
median for that format, so the agent can say why it won or flopped and learn from it.

`prepare` is model-free (it runs as a cron monitor script): it refreshes the client's posts, records
outcomes, and opens a post-mortem row with the numbers for every post that just crossed 48 hours.
The agent then judges each open row (`judge`).
"""

import json
from statistics import median

METRICS = ("views", "likes", "bookmarks", "replies", "reposts")
MIN_AGE_H, MAX_AGE_H = 48, 24 * 7


def post_format(post, suggestion_kind=None):
    if post.get("article_title"):
        return "article"
    if suggestion_kind in ("thread", "long_tweet", "video_qrt", "qrt_promo"):
        return suggestion_kind
    if post.get("long_form"):
        return "long_tweet"
    if post.get("is_quote"):
        return "quote"
    return "tweet"


def baselines(posts, kinds):
    """Median of each metric per format, over settled posts (≥ 48h old)."""
    by_fmt = {}
    for p in posts:
        if p.get("is_reply") or (p.get("age_h") or 0) < MIN_AGE_H:
            continue
        by_fmt.setdefault(post_format(p, kinds.get(p.get("id"))), []).append(p)
    out = {}
    for fmt, items in by_fmt.items():
        out[fmt] = {"n": len(items)}
        for m in METRICS:
            vals = [p[m] for p in items if isinstance(p.get(m), (int, float))]
            if vals:
                out[fmt][m] = median(vals)
    return out


def ratios(post, base):
    return {m: round(post[m] / base[m], 2) for m in METRICS
            if isinstance(post.get(m), (int, float)) and base.get(m)}


def _fetch_posts(client, days=30):
    from . import console
    data = json.loads(console.x_client_content({"username": client, "days": days, "refresh": True, "limit": 100}))
    if not data.get("success"):
        raise RuntimeError(data.get("error") or "client-content failed")
    return data["posts"]


def prepare(conn, client, fetch=_fetch_posts):
    """Open post-mortems for posts that just settled. Returns the rows still waiting for a verdict."""
    from . import ledger
    posts = fetch(client)
    ledger.record_outcomes(conn, client, posts)
    codes = {r["tweet_id"]: r for r in conn.execute(
        """SELECT o.tweet_id, o.code, s.kind FROM outcomes o
           LEFT JOIN suggestions s ON s.client = o.client AND s.code = o.code WHERE o.client = %s""", (client,))}
    kinds = {tid: r["kind"] for tid, r in codes.items()}
    base = baselines(posts, kinds)
    for p in posts:
        age = p.get("age_h") or 0
        if p.get("is_reply") or not p.get("id") or not (MIN_AGE_H <= age <= MAX_AGE_H):
            continue
        fmt = post_format(p, kinds.get(p["id"]))
        b = base.get(fmt) or {}
        metrics = {m: p[m] for m in METRICS if p.get(m) is not None}
        conn.execute(
            """INSERT INTO postmortems(client, tweet_id, code, format, url, metrics, baseline, vs_median)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (client, tweet_id) DO NOTHING""",
            (client, p["id"], (codes.get(p["id"]) or {}).get("code"), fmt, p.get("url"),
             json.dumps(metrics), json.dumps(b), json.dumps(ratios(p, b))))
    conn.commit()
    return open_rows(conn, client)


def open_rows(conn, client):
    return [dict(r) for r in conn.execute(
        """SELECT tweet_id, code, format, url, metrics, baseline, vs_median FROM postmortems
           WHERE client = %s AND verdict IS NULL ORDER BY created_at""", (client,))]


def judge(conn, client, tweet_id, verdict, why, lesson=None):
    if verdict not in ("win", "par", "flop"):
        raise ValueError("verdict must be win, par or flop")
    row = conn.execute(
        """UPDATE postmortems SET verdict = %s, why = %s, lesson = %s, judged_at = now()
           WHERE client = %s AND tweet_id = %s RETURNING tweet_id""",
        (verdict, why, lesson, client, str(tweet_id))).fetchone()
    conn.commit()
    if not row:
        raise ValueError(f"no post-mortem open for tweet {tweet_id}")
    return True


def recent(conn, client=None, days=14, verdict=None):
    return [dict(r) for r in conn.execute(
        """SELECT client, tweet_id, code, format, url, verdict, vs_median, why, lesson, judged_at FROM postmortems
           WHERE judged_at > now() - make_interval(days => %s) AND (%s::text IS NULL OR client = %s)
             AND (%s::text IS NULL OR verdict = %s)
           ORDER BY judged_at DESC LIMIT 60""", (days, client, client, verdict, verdict))]
