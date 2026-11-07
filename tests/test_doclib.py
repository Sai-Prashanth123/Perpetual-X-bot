"""Offline tests: document library (MarkItDown) + search by meaning (pgvector, fake embeddings).

    DATABASE_URL=postgresql://... uv run --no-project --with "psycopg[binary]" --with "markitdown" tests/test_doclib.py
    PX_VECTOR_DB=postgresql://... (a Postgres with the pgvector extension) enables the semantic part.
"""

import hashlib
import json
import math
import os
import re
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
TMP = Path(tempfile.mkdtemp())
(TMP / "cache" / "documents").mkdir(parents=True)
os.environ.update(OWNER_SLACK_IDS="UOWNER", PX_DATA_ROOT=str(TMP), HERMES_HOME=str(TMP))
os.environ.pop("VOYAGE_API_KEY", None)

from perpetual_core import agency_memory, connections, doclib, ledger, semantic  # noqa: E402


def check(cond, msg):
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    print(f"ok   {msg}")


class Ctx:
    def __init__(self):
        self.tools = {}

    def register_tool(self, name, toolset, schema, handler, **_):
        self.tools[name] = handler


def fake_embed(texts, input_type="document", timeout=30):
    """Deterministic bag-of-words vectors: shared words → close vectors (enough to test the pipeline)."""
    out = []
    for t in texts:
        v = [0.0] * semantic.DIMS
        for w in re.findall(r"[a-z]{3,}", t.lower()):
            v[int(hashlib.md5(w.encode()).hexdigest(), 16) % semantic.DIMS] += 1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        out.append([x / n for x in v])
    return out


def documents_part():
    conn = ledger.connect()
    ledger.ensure_schema(conn)
    tag = uuid.uuid4().hex[:6]
    ctx = Ctx()
    doclib.register(ctx)
    check(set(ctx.tools) == {"read_document", "search_documents", "list_documents"}, "tools registered")

    cache = TMP / "cache" / "documents"
    html = cache / f"brief-{tag}.html"
    html.write_text(f"<html><body><h1>Acme brief {tag}</h1><p>Payment terms: net 30, invoiced monthly.</p>"
                    f"<table><tr><td>Retainer</td><td>$6,000</td></tr></table></body></html>")
    connections.current_user = lambda: "UTEAM1"
    out = json.loads(ctx.tools["read_document"]({"path": str(html)}))
    check(out["success"] and "Payment terms" in out["text"] and "6,000" in out["text"],
          "HTML converted by MarkItDown")
    check("<p>" not in out["text"], "markup stripped to readable text")
    again = json.loads(ctx.tools["read_document"]({"path": str(html)}))
    check(again["doc_id"] == out["doc_id"], "same file → same library entry (no duplicate)")

    secret = TMP / "secret.env"
    secret.write_text("ANTHROPIC_API_KEY=sk-test")
    bad = json.loads(ctx.tools["read_document"]({"path": str(secret)}))
    check(not bad["success"] and "only open files" in bad["error"], "files outside the document cache refused")
    bad = json.loads(ctx.tools["read_document"]({"path": str(cache / ".." / "secret.env")}))
    check(not bad["success"], "path tricks (..) refused")

    big = cache / f"long-{tag}.txt"
    big.write_text(("lorem ipsum dolor " * 300 + "\n\n") * 20)
    part = json.loads(ctx.tools["read_document"]({"path": str(big), "max_chars": 2000}))
    check(part["next_start"] == 2000 and len(part["text"]) == 2000, "long files come in parts")
    check(conn.execute("SELECT count(*) n FROM library_chunks WHERE doc_id = %s", (part["doc_id"],)).fetchone()["n"] > 10,
          "chunked for search")

    res = json.loads(ctx.tools["search_documents"]({"query": f"acme payment terms {tag}"}))
    check(res["results"] and res["results"][0]["doc_id"] == out["doc_id"], "full-text document search")

    connections.current_user = lambda: "UOTHER"
    res = json.loads(ctx.tools["search_documents"]({"query": f"acme payment terms {tag}"}))
    check(not res["results"], "a DM'd document isn't visible to other teammates")
    connections.current_user = lambda: "UOWNER"
    check(json.loads(ctx.tools["search_documents"]({"query": f"acme payment terms {tag}"}))["results"],
          "owners can search every document")
    conn.execute("DELETE FROM library_docs WHERE name LIKE %s", (f"%{tag}%",))
    conn.commit()
    conn.close()


def semantic_part(url):
    os.environ["DATABASE_URL"] = url
    os.environ["VOYAGE_API_KEY"] = "test"
    semantic._ready.clear()
    semantic.embed = fake_embed
    conn = ledger.connect()
    ledger.ensure_schema(conn)
    check(semantic.ready(conn), "pgvector table ready")
    tag = uuid.uuid4().hex[:6]
    a = agency_memory.remember(conn, f"Alex retainer rises to six thousand dollars in January {tag}", "client:itsalexvacca")
    agency_memory.remember(conn, f"Standup moved to half past eleven {tag}", "agency")
    conn.execute("INSERT INTO conversation_log(slack_user, user_text, reply_text) VALUES (%s,%s,%s)",
                 ("UVEC1", f"what is the retainer for Alex {tag}", "Six thousand from January."))
    conn.commit()
    n = semantic.index_pending(conn)
    check(n >= 3, f"new memories + conversations embedded ({n})")
    check(semantic.index_pending(conn) == 0, "nothing re-embedded on the next run")
    hits = agency_memory.search(conn, f"alex retainer dollars {tag}", ["client:itsalexvacca"])
    check(hits and hits[0]["id"] == a, "memory found by meaning")
    log = agency_memory.search_log(conn, f"retainer alex {tag}", "UVEC1")
    check(log and "retainer" in log[0]["user_text"], "past conversation found by meaning")
    check(not agency_memory.search_log(conn, f"retainer alex {tag}", "USOMEONEELSE"),
          "someone else's conversations stay private")
    conn.execute("UPDATE agency_memory SET active = false WHERE id = %s", (a,))
    conn.commit()
    semantic.index_pending(conn)
    check(not conn.execute("SELECT 1 FROM embeddings WHERE kind='memory' AND ref_id=%s", (a,)).fetchone(),
          "forgotten memory leaves the index")
    os.environ["VOYAGE_API_KEY"] = ""
    check(semantic.search(conn, "anything", ["memory"]) == [], "no key → semantic off, full-text takes over")
    conn.close()


def main():
    documents_part()
    if os.environ.get("PX_VECTOR_DB"):
        semantic_part(os.environ["PX_VECTOR_DB"])
    else:
        print("skip semantic part (set PX_VECTOR_DB to a pgvector database)")
    print("all good")


if __name__ == "__main__":
    main()
