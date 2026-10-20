"""Suggestion ledger — every suggestion, every piece of feedback, every real outcome.

Lives in the shared agency Postgres (DATABASE_URL) so the dashboard and the HQ agent see
every client. All writes are scoped to one client; reads take client=None for "all clients"
(HQ only).
"""

import json
import re
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urlencode

from .settings import env

PREFIX = {"tweet": "T", "reply": "R", "quote": "Q", "thread": "H", "engage": "E", "repost": "P"}
# What the X API lets us post on the client's behalf (Feb 2026 rules): original posts,
# threads, and replies to people who engaged with the client. Everything else gets an
# "Open in X" intent link the employee taps.
API_POSTABLE = {"tweet", "thread", "engage"}
MATCH_THRESHOLD = 0.62      # text similarity above which a real post counts as "used this suggestion"
THREAD_BREAK = re.compile(r"\n\s*---\s*\n")
SCHEMA_FILE = Path(__file__).with_name("schema.sql")


def _now():
    return datetime.now(timezone.utc)


def connect():
    import psycopg
    from psycopg.rows import dict_row
    url = env("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    return psycopg.connect(url, row_factory=dict_row, autocommit=False, **_ipv4_hostaddr(url))


def _ipv4_hostaddr(url):
    """Prefer IPv4 for hosted Postgres (Neon, Supabase): many Docker networks have no IPv6
    route, and libpq would otherwise fail on the AAAA record. `host` stays as given, so TLS
    SNI and certificate checks still use the hostname."""
    import socket
    from urllib.parse import urlparse
    parsed = urlparse(url)
    host = parsed.hostname
    if not host or host in ("localhost",) or host.replace(".", "").isdigit():
        return {}
    try:
        infos = socket.getaddrinfo(host, parsed.port or 5432, socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        return {}
    return {"hostaddr": infos[0][4][0]} if infos else {}


def ensure_schema(conn):
    conn.execute(SCHEMA_FILE.read_text())
    conn.commit()


# ---------------------------------------------------------------- helpers

def thread_parts(text):
    return [p.strip() for p in THREAD_BREAK.split(text or "") if p.strip()]


def intent_url(kind, text, target_id=None, target_url=None):
    """A prefilled x.com compose link: one tap opens X with the copy ready to post."""
    if kind == "repost":
        return f"https://x.com/intent/retweet?{urlencode({'tweet_id': target_id})}" if target_id else None
    first = thread_parts(text)[0] if kind == "thread" and thread_parts(text) else (text or "")
    params = {"text": first}
    if kind in ("reply", "engage") and target_id:
        params["in_reply_to"] = target_id
    if kind == "quote" and target_url:
        params["url"] = target_url
    return "https://x.com/intent/post?" + urlencode(params)


def _next_code(conn, client, kind):
    prefix = PREFIX.get(kind)
    if not prefix:
        raise ValueError(f"unknown kind {kind!r}; use one of {sorted(PREFIX)}")
    row = conn.execute(
        """INSERT INTO counters(client, prefix, n) VALUES (%s, %s, 1)
           ON CONFLICT (client, prefix) DO UPDATE SET n = counters.n + 1 RETURNING n""",
        (client, prefix)).fetchone()
    return f"{prefix}{row['n']}"


def _norm(text):
    text = re.sub(r"https?://\S+", "", text or "").lower()
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", text)).strip()


def similarity(a, b):
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _clean(row):
    out = {}
    for k, v in row.items():
        if v is None:
            continue
        out[k] = v.isoformat(timespec="seconds") if isinstance(v, datetime) else v
    return out


# ---------------------------------------------------------------- writes (one client)

def save(conn, client, items, batch=None):
    saved = []
    for it in items:
        text = (it.get("text") or "").strip()
        kind = it.get("kind", "tweet")
        if not text and kind != "repost":
            continue
        code = _next_code(conn, client, kind)
        target_id = str(it["target_id"]) if it.get("target_id") else None
        url = intent_url(kind, text, target_id, it.get("target_url"))
        conn.execute(
            """INSERT INTO suggestions(client, code, kind, text, target_id, target_url, target_text,
                   target_by, rationale, hook, format, topic, batch, intent_url, api_postable)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (client, code, kind, text, target_id, it.get("target_url"),
             (it.get("target_text") or "")[:1000] or None, it.get("target_by"), it.get("rationale"),
             it.get("hook"), it.get("format"), it.get("topic"), batch, url, kind in API_POSTABLE))
        if target_id:
            conn.execute(
                """INSERT INTO seen_targets(client, tweet_id, used_for) VALUES (%s,%s,%s)
                   ON CONFLICT (client, tweet_id) DO UPDATE
                   SET used_for = COALESCE(seen_targets.used_for || ',', '') || EXCLUDED.used_for""",
                (client, target_id, code))
        saved.append({"code": code, "kind": kind, "open_in_x": url,
                      "one_tap_post": kind in API_POSTABLE})
    conn.commit()
    return saved


STATUS = {"approve": "approved", "edit": "edited", "reject": "rejected", "posted": "posted"}


def feedback(conn, client, code, action, text=None, note=None, by=None):
    code = code.strip().upper()
    status = STATUS.get(action)
    if not status:
        raise ValueError("action must be approve, edit, reject or posted")
    row = conn.execute(
        """UPDATE suggestions SET status = %s, final_text = COALESCE(%s, final_text),
               note = COALESCE(%s, note), decided_at = now(), decided_by = COALESCE(%s, decided_by)
           WHERE client = %s AND code = %s RETURNING kind, final_text, text, target_id, target_url""",
        (status, text, note, by, client, code)).fetchone()
    if not row:
        conn.rollback()
        raise ValueError(f"no suggestion {code} for @{client}")
    if text:  # an edit changes what the intent link should carry
        conn.execute("UPDATE suggestions SET intent_url = %s WHERE client = %s AND code = %s",
                     (intent_url(row["kind"], text, row["target_id"], row["target_url"]), client, code))
    conn.execute(
        "INSERT INTO feedback_events(client, code, action, text, note, by_name) VALUES (%s,%s,%s,%s,%s,%s)",
        (client, code, action, text, note, by))
    conn.commit()
    return {"code": code, "status": status}


def get(conn, client, code):
    row = conn.execute("SELECT * FROM suggestions WHERE client = %s AND code = %s",
                       (client, code.strip().upper())).fetchone()
    return _clean(row) if row else None


def mark_published(conn, client, code, tweet_id, url, text, by, via):
    conn.execute(
        "INSERT INTO published(client, code, tweet_id, url, text, by_name, via) VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (client, code, tweet_id, url, text, by, via))
    if code:
        conn.execute(
            """UPDATE suggestions SET status = 'posted', posted_id = %s, decided_at = now(),
                   decided_by = COALESCE(%s, decided_by), match_score = 1.0
               WHERE client = %s AND code = %s""", (tweet_id, by, client, code))
        conn.execute(
            "INSERT INTO feedback_events(client, code, action, by_name) VALUES (%s,%s,'posted',%s)",
            (client, code, by))
    conn.commit()


def log_learning(conn, client, summary, changes=None):
    conn.execute("INSERT INTO learning_log(client, summary, changes) VALUES (%s,%s,%s)",
                 (client, summary, json.dumps(changes) if changes is not None else None))
    conn.commit()


# ---------------------------------------------------------------- reads

def _scope(client, alias=""):
    col = f"{alias}client" if alias else "client"
    return (f" AND {col} = %s", [client]) if client else ("", [])


def list_suggestions(conn, client, status=None, kind=None, days=7, limit=30):
    where, args = _scope(client)
    q = "SELECT * FROM suggestions WHERE created_at >= %s" + where
    args = [_now() - timedelta(days=days)] + args
    if status:
        q += " AND status = %s"
        args.append(status)
    if kind:
        q += " AND kind = %s"
        args.append(kind)
    q += " ORDER BY created_at DESC LIMIT %s"
    args.append(limit)
    return [_clean(r) for r in conn.execute(q, args)]


def filter_new_targets(conn, client, tweet_ids):
    ids = [str(t) for t in tweet_ids if t]
    if not ids:
        return []
    seen = {r["tweet_id"] for r in conn.execute(
        "SELECT tweet_id FROM seen_targets WHERE client = %s AND tweet_id = ANY(%s)", (client, ids))}
    return [t for t in ids if t not in seen]


def _metrics(post):
    return {k: post.get(k) for k in ("likes", "replies", "reposts", "quotes", "bookmarks", "views")
            if post.get(k) is not None}


def record_outcomes(conn, client, posts, lookback_days=10):
    """Upsert the client's real posts and link each to the suggestion it came from, if any."""
    candidates = conn.execute(
        """SELECT code, text, final_text FROM suggestions
           WHERE client = %s AND created_at >= %s AND posted_id IS NULL AND kind <> 'repost'""",
        (client, _now() - timedelta(days=lookback_days))).fetchall()
    matched, updated = [], 0
    for post in posts:
        tid = post.get("id")
        if not tid:
            continue
        age = post.get("age_h") or 0
        metrics = _metrics(post)
        existing = conn.execute("SELECT code, at_24h, at_72h FROM outcomes WHERE client = %s AND tweet_id = %s",
                                (client, tid)).fetchone()
        code = existing["code"] if existing else None
        if not code:  # posted through us?
            pub = conn.execute("SELECT code FROM published WHERE client = %s AND tweet_id = %s",
                               (client, tid)).fetchone()
            code = pub["code"] if pub else None
        kind = "reply" if post.get("is_reply") else "quote" if post.get("is_quote") else "tweet"
        if not code:
            best, best_score = None, 0.0
            for c in candidates:
                score = max(similarity(post.get("text"), c["final_text"] or ""), similarity(post.get("text"), c["text"]))
                if score > best_score:
                    best, best_score = c, score
            if best and best_score >= MATCH_THRESHOLD:
                code = best["code"]
                conn.execute(
                    """UPDATE suggestions SET status = 'posted', posted_id = %s, match_score = %s,
                           decided_at = COALESCE(decided_at, now()) WHERE client = %s AND code = %s""",
                    (tid, round(best_score, 3), client, code))
                candidates = [c for c in candidates if c["code"] != code]
                matched.append({"code": code, "tweet_id": tid, "score": round(best_score, 2)})
        at_24h = existing["at_24h"] if existing else None
        at_72h = existing["at_72h"] if existing else None
        if age >= 24 and not at_24h:
            at_24h = metrics
        if age >= 72 and not at_72h:
            at_72h = metrics
        conn.execute(
            """INSERT INTO outcomes(client, tweet_id, code, kind, text, url, posted_at, latest, at_24h, at_72h, checked_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
               ON CONFLICT (client, tweet_id) DO UPDATE SET code = EXCLUDED.code, latest = EXCLUDED.latest,
                   at_24h = EXCLUDED.at_24h, at_72h = EXCLUDED.at_72h, checked_at = now()""",
            (client, tid, code, kind, (post.get("text") or "")[:1200], post.get("url"), post.get("at"),
             json.dumps(metrics), json.dumps(at_24h) if at_24h else None,
             json.dumps(at_72h) if at_72h else None))
        updated += 1
    # Pending suggestions older than 4 days were never used; mark them so stats stay honest.
    conn.execute("UPDATE suggestions SET status = 'expired' WHERE client = %s AND status = 'pending' AND created_at < %s",
                 (client, _now() - timedelta(days=4)))
    conn.commit()
    return {"posts_checked": updated, "newly_matched": matched}


def stats(conn, client, days=30):
    """Learning data. client=None aggregates every client (HQ)."""
    since = _now() - timedelta(days=days)
    where, sargs = _scope(client)
    by_client = "client, " if client is None else ""
    by_kind = [dict(r) for r in conn.execute(
        f"""SELECT {by_client}kind, COUNT(*) AS suggested,
                   COUNT(*) FILTER (WHERE status IN ('approved','edited','posted')) AS liked,
                   COUNT(*) FILTER (WHERE final_text IS NOT NULL) AS edited,
                   COUNT(*) FILTER (WHERE status = 'rejected') AS rejected,
                   COUNT(*) FILTER (WHERE status = 'posted') AS posted
            FROM suggestions WHERE created_at >= %s{where} GROUP BY {by_client}kind ORDER BY 1""",
        [since] + sargs)]

    def dimension(col):
        where_s, args = _scope(client, "s.")
        rows = conn.execute(
            f"""SELECT s.{col} AS value, COUNT(*) AS suggested,
                       COUNT(*) FILTER (WHERE s.status IN ('approved','edited','posted')) AS liked,
                       COUNT(*) FILTER (WHERE s.status = 'rejected') AS rejected,
                       COUNT(*) FILTER (WHERE s.status = 'posted') AS posted,
                       ROUND(AVG((o.latest->>'likes')::numeric), 1) AS avg_likes_when_posted
                FROM suggestions s
                LEFT JOIN outcomes o ON o.client = s.client AND o.tweet_id = s.posted_id
                WHERE s.created_at >= %s AND s.{col} IS NOT NULL{where_s}
                GROUP BY s.{col} ORDER BY suggested DESC LIMIT 15""", [since] + args)
        return [{k: (float(v) if hasattr(v, "as_integer_ratio") and not isinstance(v, int) else v)
                 for k, v in r.items() if v is not None} for r in rows]

    where_o, oargs = _scope(client)
    own = [dict(r) for r in conn.execute(
        f"""SELECT {by_client}kind, COUNT(*) AS posts,
                   ROUND(AVG((latest->>'likes')::numeric), 1)::float AS avg_likes,
                   COUNT(code) AS from_suggestions
            FROM outcomes WHERE posted_at >= %s AND latest ? 'likes'{where_o}
            GROUP BY {by_client}kind ORDER BY 1""", [since] + oargs)]
    where_r, rargs = _scope(client)
    rejections = [_clean(r) for r in conn.execute(
        f"""SELECT {by_client}code, kind, hook, format, note, decided_by FROM suggestions
            WHERE status = 'rejected' AND note IS NOT NULL AND created_at >= %s{where_r}
            ORDER BY decided_at DESC LIMIT 15""", [since] + rargs)]
    edits = [_clean(r) for r in conn.execute(
        f"""SELECT {by_client}code, kind, text AS suggested, final_text AS client_version FROM suggestions
            WHERE final_text IS NOT NULL AND created_at >= %s{where_r} ORDER BY decided_at DESC LIMIT 10""",
        [since] + rargs)]
    last = conn.execute(
        "SELECT client, at, summary FROM learning_log WHERE client = %s ORDER BY id DESC LIMIT 1",
        (client or "_agency",)).fetchone()
    return {
        "scope": client or "all clients",
        "window_days": days,
        "suggestions_by_kind": by_kind,
        "by_hook": dimension("hook"),
        "by_format": dimension("format"),
        "by_topic": dimension("topic"),
        "client_posts_by_kind": own,
        "recent_rejection_reasons": rejections,
        "recent_client_edits": edits,
        "last_learning_review": _clean(last) if last else None,
    }


def save_document(conn, key, body):
    conn.execute(
        """INSERT INTO documents(key, body, updated_at) VALUES (%s, %s, now())
           ON CONFLICT (key) DO UPDATE SET body = EXCLUDED.body, updated_at = now()""", (key, body))
    conn.commit()
