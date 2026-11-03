"""Offline tests for the main agent's run_studio tool (queues content-studio requests).

    DATABASE_URL=postgresql://... uv run --no-project --with "psycopg[binary]" tests/test_studio_hq.py
"""

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "hermes-template" / "plugins" / "perpetual-hq"))

from perpetual_core import ledger  # noqa: E402
import studio_hq  # noqa: E402

A, B = "studioalpha", "studiobeta"


def check(cond, msg):
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    print(f"ok   {msg}")


def reset(conn):
    conn.execute("DELETE FROM agency_requests WHERE client = ANY(%s)", ([A, B],))
    conn.execute("DELETE FROM clients WHERE handle = ANY(%s)", ([A, B],))
    conn.execute("""INSERT INTO clients(handle, display_name, slack_channel)
                    VALUES (%s, 'Alpha', 'CSTUDIOA'), (%s, 'Beta', NULL)""", (A, B))
    conn.commit()


def rows(conn, client):
    return conn.execute("SELECT * FROM agency_requests WHERE client = %s ORDER BY id", (client,)).fetchall()


def test_named(conn):
    out = json.loads(studio_hq.run_studio({"clients": ["@StudioAlpha", B], "brief": "cold email reply rates",
                                           "kind": "thread", "count": 2}))
    check(out["success"] is True, "handler returns success JSON")
    check(out["queued"] == [A], "client with a channel queued")
    check(out["skipped_no_channel"] == [B], "client without a channel skipped")
    r = rows(conn, A)
    check(len(r) == 1 and r[0]["kind"] == "studio", "one agency_request with kind 'studio'")
    check(r[0]["request"] == "Follow content-studio: write 2 thread about cold email reply rates. Publish one document.",
          "request text tells the agent to follow content-studio")
    check(r[0]["by_name"] == "Perpetual" and r[0]["requester_slack"] is None, "no session: by Perpetual, no requester")
    check(rows(conn, B) == [], "nothing queued for the channel-less client")


def test_all(conn):
    before = conn.execute("SELECT COALESCE(max(id), 0) AS m FROM agency_requests").fetchone()["m"]
    try:
        out = json.loads(studio_hq.run_studio({"brief": "pricing"}))
        check(out["success"] and A in out["queued"] and B not in out["queued"], "empty clients = every client with a channel")
        check(B in out["skipped_no_channel"], "channel-less clients listed as skipped")
        r = rows(conn, A)[-1]
        check(r["request"] == "Follow content-studio: write 3 post about pricing. Publish one document.",
              "defaults: kind post, count 3")
    finally:
        conn.execute("DELETE FROM agency_requests WHERE id > %s", (before,))
        conn.commit()


def test_errors():
    out = json.loads(studio_hq.run_studio({"clients": [A]}))
    check(out["success"] is False and "brief" in out["error"], "missing brief is an error, not a crash")


def test_register():
    seen = []

    class Ctx:
        def register_tool(self, **kw):
            seen.append(kw)

    studio_hq.register(Ctx())
    check([s["name"] for s in seen] == ["run_studio"] and seen[0]["toolset"] == "studio", "registers run_studio")
    check(seen[0]["schema"]["parameters"]["required"] == ["brief"], "schema requires brief")


if __name__ == "__main__":
    if not os.environ.get("DATABASE_URL"):
        raise SystemExit("set DATABASE_URL")
    conn = ledger.connect()
    ledger.ensure_schema(conn)
    reset(conn)
    try:
        test_named(conn)
        test_all(conn)
        test_errors()
        test_register()
    finally:
        conn.execute("DELETE FROM agency_requests WHERE client = ANY(%s)", ([A, B],))
        conn.execute("DELETE FROM clients WHERE handle = ANY(%s)", ([A, B],))
        conn.commit()
        conn.close()
    print("all good")
