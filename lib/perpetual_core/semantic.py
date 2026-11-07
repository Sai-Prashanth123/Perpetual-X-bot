"""Search by meaning (pgvector + Voyage AI embeddings) over what Agency Bot knows: long-term memories,
past conversations and the document library. "What did we decide about Alex's pricing?" finds "Alex's
retainer goes to $6k in January" even though no word matches.

Off until VOYAGE_API_KEY is set (and the database has the `vector` extension, which Neon does); every
caller falls back to full-text search, so nothing breaks without it.

    embeddings(kind, ref_id, chunk)  kind = memory | conversation | document | lesson
    index_pending(conn)              embeds anything new (cron px-index, model-free)
    search(conn, query, kinds, ...)  nearest by cosine distance
"""

import json
import urllib.request

from .settings import env

MODEL = "voyage-3.5-lite"           # 1024 dims, cheap, generous free tier
DIMS = 1024
URL = "https://api.voyageai.com/v1/embeddings"

SCHEMA_SQL = f"""
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS embeddings (
    kind       TEXT NOT NULL,
    ref_id     BIGINT NOT NULL,
    chunk      INT NOT NULL DEFAULT 0,
    scope      TEXT,                       -- memory scope / conversation user / document owner
    text       TEXT NOT NULL,
    embedding  vector({DIMS}) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (kind, ref_id, chunk)
);
CREATE INDEX IF NOT EXISTS embeddings_hnsw ON embeddings USING hnsw (embedding vector_cosine_ops);
"""

_ready = {}
_qcache = {}          # query text -> vector (one chat turn searches memories, chats and lessons with one call)


def enabled():
    return bool(env("VOYAGE_API_KEY"))


def ready(conn):
    """True when embeddings can be used here (key + vector extension + table). Cached per database."""
    if not enabled():
        return False
    if "ok" in _ready:
        return _ready["ok"]
    try:
        conn.execute(SCHEMA_SQL)
        conn.commit()
        _ready["ok"] = True
    except Exception:  # noqa: BLE001 — no vector extension: stay on full-text search
        conn.rollback()
        _ready["ok"] = False
    return _ready["ok"]


def embed(texts, input_type="document", timeout=30):
    """Voyage embeddings for a list of texts (batched by the caller; <= 128 per call)."""
    body = json.dumps({"input": [t[:8000] for t in texts], "model": MODEL, "input_type": input_type}).encode()
    req = urllib.request.Request(URL, data=body, method="POST", headers={
        "Authorization": f"Bearer {env('VOYAGE_API_KEY')}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())
    return [d["embedding"] for d in sorted(data["data"], key=lambda d: d["index"])]


def _vec(v):
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def store(conn, rows):
    """rows: [(kind, ref_id, chunk, scope, text)] → embedded and saved (replaces same key)."""
    rows = [r for r in rows if (r[4] or "").strip()]
    for i in range(0, len(rows), 64):
        batch = rows[i:i + 64]
        vecs = embed([r[4] for r in batch])
        for (kind, ref_id, chunk, scope, text), v in zip(batch, vecs):
            conn.execute("""INSERT INTO embeddings(kind, ref_id, chunk, scope, text, embedding)
                            VALUES (%s,%s,%s,%s,%s,%s::vector)
                            ON CONFLICT (kind, ref_id, chunk) DO UPDATE
                            SET scope = EXCLUDED.scope, text = EXCLUDED.text, embedding = EXCLUDED.embedding""",
                         (kind, ref_id, chunk, scope, text[:4000], _vec(v)))
        conn.commit()
    return len(rows)


def index_pending(conn, limit=200):
    """Embed whatever isn't embedded yet: memories, conversations, document chunks. Returns a count."""
    if not ready(conn):
        return 0
    todo = []
    todo += [("memory", r["id"], 0, r["scope"], r["text"]) for r in conn.execute(
        """SELECT m.id, m.scope, m.text FROM agency_memory m
           WHERE m.active AND NOT EXISTS (SELECT 1 FROM embeddings e WHERE e.kind = 'memory' AND e.ref_id = m.id)
           ORDER BY m.id LIMIT %s""", (limit,))]
    todo += [("conversation", r["id"], 0, r["slack_user"],
              f"They asked: {r['user_text'] or ''}\nAnswer: {(r['reply_text'] or '')[:1500]}") for r in conn.execute(
        """SELECT c.id, c.slack_user, c.user_text, c.reply_text FROM conversation_log c
           WHERE NOT EXISTS (SELECT 1 FROM embeddings e WHERE e.kind = 'conversation' AND e.ref_id = c.id)
           ORDER BY c.id DESC LIMIT %s""", (limit,))]
    try:
        todo += [("document", r["doc_id"], r["n"], r["slack_user"], f"{r['name']}: {r['text']}") for r in conn.execute(
            """SELECT k.doc_id, k.n, k.text, d.name, d.slack_user FROM library_chunks k JOIN library_docs d ON d.id = k.doc_id
               WHERE NOT EXISTS (SELECT 1 FROM embeddings e WHERE e.kind = 'document' AND e.ref_id = k.doc_id
                                 AND e.chunk = k.n)
               ORDER BY k.doc_id DESC, k.n LIMIT %s""", (limit,))]
    except Exception:  # noqa: BLE001 — document library not created yet
        conn.rollback()
    try:
        todo += [("lesson", r["id"], 0, r["scope"], r["rule"]) for r in conn.execute(
            """SELECT l.id, l.scope, l.rule FROM lessons l
               WHERE l.active AND NOT EXISTS (SELECT 1 FROM embeddings e WHERE e.kind = 'lesson' AND e.ref_id = l.id)
               ORDER BY l.id LIMIT %s""", (limit,))]
        conn.execute("""DELETE FROM embeddings e USING lessons l
                        WHERE e.kind = 'lesson' AND e.ref_id = l.id AND NOT l.active""")
    except Exception:  # noqa: BLE001 — lessons table not created yet
        conn.rollback()
    # forgotten memories leave the index
    conn.execute("""DELETE FROM embeddings e USING agency_memory m
                    WHERE e.kind = 'memory' AND e.ref_id = m.id AND NOT m.active""")
    conn.commit()
    return store(conn, todo) if todo else 0


def search(conn, query, kinds, scopes=None, limit=8, max_distance=0.65):
    """Nearest items by meaning: [{kind, ref_id, chunk, scope, text, distance}] ([] when off)."""
    if not (query or "").strip() or not ready(conn):
        return []
    qv = _qcache.get(query)
    if qv is None:
        try:
            qv = _vec(embed([query], "query", timeout=6)[0])   # per chat turn: never stall a reply
        except Exception:  # noqa: BLE001 — Voyage down / quota: callers use full-text search
            return []
        if len(_qcache) > 64:
            _qcache.clear()
        _qcache[query] = qv
    where, vals = ["kind = ANY(%s)"], [list(kinds)]
    if scopes is not None:
        where.append("scope = ANY(%s)")
        vals.append(list(scopes))
    rows = conn.execute(f"""SELECT kind, ref_id, chunk, scope, text, embedding <=> %s::vector AS distance
                            FROM embeddings WHERE {' AND '.join(where)}
                            ORDER BY embedding <=> %s::vector LIMIT %s""", (qv, *vals, qv, limit)).fetchall()
    return [dict(r) for r in rows if r["distance"] <= max_distance]
