"""Document reader + library: any file someone sends Agency Bot in Slack (PDF, Word, PowerPoint, Excel,
HTML, CSV, text…) becomes readable text via Microsoft's MarkItDown, and is kept in a searchable library
("what did the Acme contract say about payment terms?").

Safe for everyone: it only opens files people sent to the bot (Hermes' document cache) or Slack files by
id/link, never arbitrary paths on the server. Conversion runs MarkItDown in its own process (separate
venv), so its heavy libraries never sit in the bot's memory.

    library_docs(id, sha256, name, source, slack_user, chat_id, chars, text)
    library_chunks(doc_id, n, text)      ~1500-char pieces: full-text index + semantic search (semantic.py)
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

from . import ledger
from .settings import env, hermes_home

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS library_docs (
    id          SERIAL PRIMARY KEY,
    sha256      TEXT UNIQUE NOT NULL,
    name        TEXT NOT NULL,
    source      TEXT,                     -- slack upload / slack file id
    slack_user  TEXT,
    chat_id     TEXT,
    chars       INT NOT NULL DEFAULT 0,
    text        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS library_chunks (
    doc_id  INT NOT NULL REFERENCES library_docs(id) ON DELETE CASCADE,
    n       INT NOT NULL,
    text    TEXT NOT NULL,
    PRIMARY KEY (doc_id, n)
);
CREATE INDEX IF NOT EXISTS library_chunks_fts ON library_chunks USING GIN (to_tsvector('english', text));
"""

TEXT_EXT = {".txt", ".md", ".csv", ".json", ".log", ".yaml", ".yml", ".xml", ".ini", ".cfg", ".toml", ".tsv"}
MAX_BYTES = 30 * 1024 * 1024
CHUNK = 1500


class DocError(Exception):
    pass


# ---------------------------------------------------------------- where files may come from

def _roots():
    """Folders the bot may read from: Hermes' document caches (HQ + every client profile)."""
    data = Path(env("PX_DATA_ROOT") or "/opt/data")
    roots = [hermes_home() / "cache" / "documents", data / "cache" / "documents"]
    roots += list((data / "profiles").glob("*/cache/documents")) if (data / "profiles").is_dir() else []
    return [r.resolve() for r in roots if r.exists()]


def allowed(path):
    """The real path, if it's a file inside a document cache; else None (no server files, no symlink tricks)."""
    try:
        real = Path(path).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not real.is_file():
        return None
    for root in _roots():
        if real == root or root in real.parents:
            return real
    return None


def fetch_slack_file(ref):
    """A Slack file id (F0…) or link → downloaded into the document cache. Returns (path, name)."""
    from . import slackui
    m = re.search(r"\b(F[A-Z0-9]{8,})\b", ref or "")
    if not m:
        raise DocError("give a Slack file link or id (F…)")
    info = slackui.slack("files.info", file=m.group(1)).get("file") or {}
    url = info.get("url_private_download") or info.get("url_private")
    if not url:
        raise DocError("that Slack file can't be downloaded (is it shared where the bot can see it?)")
    if (info.get("size") or 0) > MAX_BYTES:
        raise DocError("file is larger than 30 MB")
    token = env("SLACK_BOT_TOKEN") or env("PX_SLACK_TOKEN")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise DocError("file is larger than 30 MB")
    name = re.sub(r"[^\w.\- ]", "_", info.get("name") or f"{m.group(1)}.bin")[:120]
    folder = hermes_home() / "cache" / "documents"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"slack-{m.group(1)}-{name}"
    path.write_bytes(data)
    return path, info.get("name") or name


# ---------------------------------------------------------------- conversion

def _markitdown_cmd():
    exe = env("MARKITDOWN_BIN") or ("/opt/markitdown/bin/markitdown" if Path("/opt/markitdown/bin/markitdown").exists()
                                    else shutil.which("markitdown"))
    return [exe] if exe else [sys.executable, "-m", "markitdown"]


def convert(path):
    """File → Markdown text. Plain-text formats are read directly; everything else goes through MarkItDown."""
    path = Path(path)
    if path.stat().st_size > MAX_BYTES:
        raise DocError("file is larger than 30 MB")
    if path.suffix.lower() in TEXT_EXT:
        return path.read_text(encoding="utf-8", errors="replace")
    try:
        out = subprocess.run(_markitdown_cmd() + [str(path)], capture_output=True, timeout=180,
                             env={k: v for k, v in os.environ.items()
                                  if not re.search(r"TOKEN|SECRET|KEY|PASSWORD|DATABASE_URL", k)})
    except subprocess.TimeoutExpired:
        raise DocError("conversion took too long (over 3 minutes)") from None
    except FileNotFoundError:
        raise DocError("document converter (MarkItDown) is not installed") from None
    if out.returncode != 0:
        raise DocError(f"couldn't read this file: {out.stderr.decode(errors='replace').strip()[-300:]}")
    text = out.stdout.decode("utf-8", errors="replace").strip()
    if not text:
        raise DocError("no text found (a scanned image PDF? send a text PDF or the original file)")
    return text


def chunks(text, size=CHUNK):
    """~size-char pieces, split on paragraph boundaries where possible."""
    out, cur = [], ""
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        while len(para) > size:
            if cur:
                out.append(cur)
                cur = ""
            out.append(para[:size])
            para = para[size:]
        if len(cur) + len(para) + 2 > size and cur:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n\n{para}" if cur else para
    if cur:
        out.append(cur)
    return out


def ingest(conn, path, name=None, source=None, user=None, chat=None):
    """Convert + store (deduplicated by content). Returns the documents row."""
    path = Path(path)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    row = conn.execute("SELECT * FROM library_docs WHERE sha256 = %s", (sha,)).fetchone()
    if row:
        return row
    text = convert(path)
    row = conn.execute("""INSERT INTO library_docs(sha256, name, source, slack_user, chat_id, chars, text)
                          VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                       (sha, (name or path.name)[:300], source, user, chat, len(text), text)).fetchone()
    for i, c in enumerate(chunks(text)):
        conn.execute("INSERT INTO library_chunks(doc_id, n, text) VALUES (%s,%s,%s)", (row["id"], i, c))
    conn.commit()
    return row


# ---------------------------------------------------------------- search

def visible_filter(user):
    """SQL + params: owners see every document; others their own + ones shared in channels."""
    from . import guard
    if user and user in guard.owners():
        return "TRUE", []
    return "(d.slack_user = %s OR d.chat_id LIKE 'C%%' OR d.chat_id LIKE 'G%%')", [user]


def search(conn, query, user, limit=6):
    from . import semantic
    where, vals = visible_filter(user)
    hits = semantic.search(conn, query, ["document"], None, limit * 3)
    if hits:
        ids = [h["ref_id"] for h in hits]
        ok = {r["id"]: r for r in conn.execute(f"SELECT d.* FROM library_docs d WHERE d.id = ANY(%s) AND {where}",
                                               (ids, *vals))}
        out = []
        for h in hits:
            d = ok.get(h["ref_id"])
            if d:
                k = conn.execute("SELECT text FROM library_chunks WHERE doc_id = %s AND n = %s", (d["id"], h["chunk"])).fetchone()
                out.append({"doc_id": d["id"], "name": d["name"], "part": h["chunk"], "text": (k or h)["text"]})
        if out:
            return out[:limit]
    from .agency_memory import _tsquery, _words
    tsq = _tsquery(_words(query))
    if not tsq:
        return []
    rows = conn.execute(
        f"""SELECT d.id, d.name, k.n, k.text, ts_rank(to_tsvector('english', k.text), to_tsquery('english', %s)) AS score
            FROM library_chunks k JOIN library_docs d ON d.id = k.doc_id
            WHERE to_tsvector('english', k.text) @@ to_tsquery('english', %s) AND {where}
            ORDER BY score DESC LIMIT %s""", (tsq, tsq, *vals, limit)).fetchall()
    return [{"doc_id": r["id"], "name": r["name"], "part": r["n"], "text": r["text"]} for r in rows]


# ---------------------------------------------------------------- tools

def _user():
    from . import connections
    return connections.current_user()


def _chat():
    try:
        from gateway.session_context import get_session_env
        return get_session_env("HERMES_SESSION_CHAT_ID") or None
    except Exception:  # noqa: BLE001
        return None


def _err(msg):
    return json.dumps({"success": False, "error": msg})


def tool_read_document(params, **_):
    conn = ledger.connect()
    try:
        user, chat = _user(), _chat()
        if params.get("doc_id"):
            where, vals = visible_filter(user)
            row = conn.execute(f"SELECT d.* FROM library_docs d WHERE d.id = %s AND {where}",
                               (int(params["doc_id"]), *vals)).fetchone()
            if not row:
                return _err("no such document (or it isn't shared with you)")
        elif params.get("slack_file"):
            path, name = fetch_slack_file(params["slack_file"])
            row = ingest(conn, path, name, "slack:" + params["slack_file"], user, chat)
        elif params.get("path"):
            real = allowed(params["path"])
            if not real:
                return _err("I can only open files people sent me in Slack (the path from the "
                            "'user sent a document' note), or a Slack file link.")
            row = ingest(conn, real, params.get("name") or re.sub(r"^slack-F\w+-", "", real.name), "slack upload",
                         user, chat)
        else:
            return _err("give path (from the document note), slack_file (link or id), or doc_id")
        start = max(0, int(params.get("start") or 0))
        size = min(max(int(params.get("max_chars") or 40000), 2000), 60000)
        part = row["text"][start:start + size]
        nxt = start + size if start + size < row["chars"] else None
        return json.dumps({"success": True, "doc_id": row["id"], "name": row["name"], "chars": row["chars"],
                           "start": start, "next_start": nxt, "text": part,
                           "note": ("More text follows: call again with start=next_start." if nxt else "")},
                          ensure_ascii=False)
    except DocError as e:
        return _err(str(e))
    except Exception as e:  # noqa: BLE001
        return _err(f"couldn't read the document: {e}")
    finally:
        conn.close()


def tool_search_documents(params, **_):
    conn = ledger.connect()
    try:
        hits = search(conn, params.get("query") or "", _user(), int(params.get("limit") or 6))
        return json.dumps({"success": True, "results": hits}, ensure_ascii=False)
    finally:
        conn.close()


def tool_list_documents(params, **_):
    conn = ledger.connect()
    try:
        where, vals = visible_filter(_user())
        rows = conn.execute(f"""SELECT d.id, d.name, d.chars, d.slack_user, d.created_at FROM library_docs d
                                WHERE {where} ORDER BY d.created_at DESC LIMIT %s""",
                            (*vals, int(params.get("limit") or 20))).fetchall()
        return json.dumps({"success": True, "documents": [dict(r) for r in rows]}, ensure_ascii=False, default=str)
    finally:
        conn.close()


_S = {"type": "string"}
TOOLS = [
    ("read_document", tool_read_document,
     "Read a file someone sent in Slack (PDF, Word, PowerPoint, Excel, HTML, CSV, text…) as text, and keep it "
     "in the document library. Use it whenever a message says 'The user sent a document … saved at: <path>' "
     "(pass that path), or with a Slack file link/id, or doc_id for one already in the library. Long files "
     "come in parts: call again with start=next_start.",
     {"path": _S, "slack_file": _S, "doc_id": {"type": "integer"}, "start": {"type": "integer"},
      "max_chars": {"type": "integer"}}, []),
    ("search_documents", tool_search_documents,
     "Search every document people have shared with Agency Bot (contracts, briefs, decks, reports) by meaning "
     "or keywords: 'Acme payment terms', 'what did the Q3 deck say about churn'. Returns the matching passages "
     "with doc_id; read_document(doc_id=…) for more.",
     {"query": _S, "limit": {"type": "integer"}}, ["query"]),
    ("list_documents", tool_list_documents, "Recent documents in the library (id, name, size, who shared it).",
     {"limit": {"type": "integer"}}, []),
]


def register(ctx, toolset="documents"):
    for name, fn, description, props, required in TOOLS:
        ctx.register_tool(name=name, toolset=toolset, handler=fn,
                          schema={"name": name, "description": description,
                                  "parameters": {"type": "object", "properties": props, "required": required}})
