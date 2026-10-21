"""Research memory: what a client's agent has found out about its client (own performance,
competitors, the niche on the web, growth levers). Written by the nightly research run and by
on-demand research; read before every answer so the agent knows its client without redoing work.
"""

from psycopg.types.json import Json

KINDS = ("performance", "competitors", "niche", "growth", "content", "note")


def add(conn, client, kind, title, body, sources=None):
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    if not (title or "").strip() or not (body or "").strip():
        raise ValueError("title and body are required")
    row = conn.execute(
        "INSERT INTO research(client, kind, title, body, sources) VALUES (%s,%s,%s,%s,%s) RETURNING id, at",
        (client, kind, title.strip(), body.strip(), Json(sources or []))).fetchone()
    conn.commit()
    return dict(row)


def recent(conn, client, days=30, kind=None, limit=20):
    """Newest first. Older findings stay in the table but drop out of the agent's working memory."""
    rows = conn.execute(
        """SELECT id, at, kind, title, body, sources FROM research
           WHERE client = %s AND at > now() - make_interval(days => %s) AND (%s::text IS NULL OR kind = %s)
           ORDER BY at DESC LIMIT %s""", (client, days, kind, kind, limit)).fetchall()
    return [dict(r) for r in rows]


def log_doc(conn, client, title, kind, mode, link, channel, ts):
    conn.execute("INSERT INTO published_docs(client, title, kind, mode, link, channel, ts) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                 (client, title, kind, mode, link, channel, ts))
    conn.commit()


def recent_docs(conn, client, limit=10):
    rows = conn.execute("SELECT at, title, kind, mode, link FROM published_docs WHERE client = %s ORDER BY at DESC LIMIT %s",
                        (client, limit)).fetchall()
    return [dict(r) for r in rows]
