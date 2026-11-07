"""Connected memory (Graphiti): people, clients, decisions and how they relate and change over time
("who owns Alex's account now?", "how has Michel's posting plan changed?"). Built nightly from the facts
Agency Bot saved (agency_memory), in an embedded Kuzu graph on the bot's disk; Graphiti itself runs in
its own venv as a separate process (workers/graph_worker.py), so it never sits in the bot's memory.

Off until VOYAGE_API_KEY is set and the Graphiti venv exists.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

from . import ledger
from .settings import env

WORKER = Path(__file__).resolve().parent / "workers" / "graph_worker.py"
PER_NIGHT = 40      # episodes per night: several model calls each, keep the cost small

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS graph_state (
    id              INT PRIMARY KEY DEFAULT 1,
    last_memory_id  INT NOT NULL DEFAULT 0,
    last_run        TIMESTAMPTZ,
    last_result     TEXT
);
"""


class GraphError(Exception):
    pass


def _python():
    p = env("GRAPHITI_PYTHON") or "/opt/graphiti/bin/python"
    return p if Path(p).exists() else None


def enabled():
    return bool(env("VOYAGE_API_KEY") and env("ANTHROPIC_API_KEY") and _python())


def _db():
    path = Path(env("GRAPH_DB") or "/opt/data/graph/agency.kuzu")
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


def run_worker(cmd, payload, timeout=1500):
    if not enabled():
        raise GraphError("connected memory is off (needs the Voyage key and the Graphiti install)")
    child_env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "TZ", "SSL_CERT_FILE")}
    child_env.update(GRAPH_DB=_db(), ANTHROPIC_API_KEY=env("ANTHROPIC_API_KEY"), VOYAGE_API_KEY=env("VOYAGE_API_KEY"),
                     GRAPH_MODEL=env("PX_ROUTINE_MODEL") or "claude-haiku-5-5")
    try:
        out = subprocess.run([_python(), "-I", str(WORKER), cmd], input=json.dumps(payload).encode(),
                             capture_output=True, timeout=timeout, env=child_env)
    except subprocess.TimeoutExpired:
        raise GraphError("the graph took too long") from None
    if out.returncode != 0:
        err = out.stderr.decode(errors="replace").strip()
        if "lock" in err.lower():
            raise GraphError("the graph is being updated right now; try again in a few minutes")
        raise GraphError("graph worker failed: " + err[-300:])
    return json.loads(out.stdout.decode("utf-8", errors="replace") or "null")


def state(conn):
    conn.execute("INSERT INTO graph_state(id) VALUES (1) ON CONFLICT (id) DO NOTHING")
    return conn.execute("SELECT * FROM graph_state WHERE id = 1").fetchone()


def episodes_since(conn, last_id, limit=PER_NIGHT):
    rows = conn.execute("""SELECT id, scope, text, created_at FROM agency_memory
                           WHERE active AND id > %s ORDER BY id LIMIT %s""", (last_id, limit)).fetchall()
    return rows, [{"name": f"fact-{r['id']}", "body": f"({r['scope']}) {r['text']}", "source": f"agency memory, {r['scope']}",
                   "at": r["created_at"].isoformat()} for r in rows]


def ingest(conn, worker=run_worker):
    """Nightly: new facts → the graph. Returns a one-line summary ('' when nothing to do)."""
    if not enabled() and worker is run_worker:
        return ""
    st = state(conn)
    rows, eps = episodes_since(conn, st["last_memory_id"])
    if not eps:
        return ""
    res = worker("ingest", eps)
    conn.execute("UPDATE graph_state SET last_memory_id = %s, last_run = now(), last_result = %s WHERE id = 1",
                 (rows[-1]["id"], json.dumps(res)[:2000]))
    conn.commit()
    return f"graph: {res.get('added', 0)} added, {len(res.get('failed') or [])} failed"


def tool_graph_recall(params, **_):
    try:
        facts = run_worker("search", {"query": params.get("query") or "", "limit": int(params.get("limit") or 10)},
                           timeout=120)
        return json.dumps({"success": True, "facts": facts}, ensure_ascii=False)
    except GraphError as e:
        return json.dumps({"success": False, "error": str(e)})


def register(ctx, toolset="memory_graph"):
    ctx.register_tool(name="graph_recall", toolset=toolset, handler=tool_graph_recall, schema={
        "name": "graph_recall",
        "description": ("Connected memory: how people, clients, deals and decisions relate and how that changed "
                        "over time ('who handles Alex's account now?', 'what changed in Michel's plan since "
                        "September?'). Each fact has valid_at / invalid_at (still_true=false means it was replaced). "
                        "Built nightly from saved memories; use recall for today's facts."),
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
                       "required": ["query"]}})


if __name__ == "__main__":       # manual: python -m perpetual_core.graph_memory
    c = ledger.connect()
    print(ingest(c) or "nothing new", file=sys.stderr)
