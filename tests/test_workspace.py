"""Offline tests for personal tool connections (Google / Notion) and the workspace tools.

    DATABASE_URL=postgresql://... uv run --no-project --with "psycopg[binary]" --with cryptography tests/test_workspace.py
"""

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
os.environ.update(INTERNAL_TOKEN="test-key", AGENCY_WEB_URL="https://connector.example",
                  GOOGLE_CLIENT_ID="gid", GOOGLE_CLIENT_SECRET="gsecret")
from cryptography.fernet import Fernet  # noqa: E402
os.environ.setdefault("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())

from perpetual_core import connections, workspace  # noqa: E402


def check(cond, msg):
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    print(f"ok   {msg}")


def test_links():
    url = connections.link("google", "U1")
    check(url.startswith("https://connector.example/connect/google?"), "sign-in link on the connector site")
    from urllib.parse import parse_qs, urlparse
    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    check(connections.verify("google", "U1", q["exp"], q["sig"]), "link verifies for its owner")
    check(not connections.verify("google", "U2", q["exp"], q["sig"]), "link can't be reused for someone else")
    check(not connections.verify("notion", "U1", q["exp"], q["sig"]), "link is tied to its tool")
    check(not connections.verify("google", "U1", str(int(time.time()) - 5), q["sig"]), "expired link rejected")
    a = connections.authorize_url("google", "st")
    check("access_type=offline" in a and "prompt=consent" in a and "gmail.compose" in a, "Google asks for offline access")
    check(connections.configured("google") and not connections.configured("notion"), "configured only with client id+secret")


def test_blocks():
    b = workspace.md_to_blocks("# Title\n\n- one [link](https://x.com)\n1. first\n> quote **bold**\n---\nplain")
    kinds = [x["type"] for x in b]
    check(kinds == ["heading_1", "bulleted_list_item", "numbered_list_item", "quote", "divider", "paragraph"],
          "markdown → Notion blocks")
    link = b[1]["bulleted_list_item"]["rich_text"][1]
    check(link["text"]["link"]["url"] == "https://x.com", "links kept")
    check(b[3]["quote"]["rich_text"][1]["annotations"]["bold"], "bold kept")


def test_tokens(conn):
    conn.execute("DELETE FROM user_connections WHERE slack_user LIKE 'UTEST%'")
    conn.commit()
    try:
        connections.token(conn, None, "google")
        check(False, "no person → refused")
    except connections.NotConnected:
        check(True, "no person (scheduled job) → refused")
    try:
        connections.token(conn, "UTEST1", "google")
        check(False, "not connected → refused")
    except connections.NotConnected as e:
        check("connect my google" in str(e), "not connected → tells them how to connect")
    f = connections._fernet()
    conn.execute("""INSERT INTO user_connections(slack_user, provider, account, access_enc, refresh_enc, expires_at)
                    VALUES ('UTEST1','google','a@b.com',%s,%s, now() - interval '1 minute')""",
                 (f.encrypt(b"old").decode(), f.encrypt(b"refresh-1").decode()))
    conn.commit()
    sent = {}

    def fake_post(url, data=None, headers=None, body=None):
        sent.update(data or {})
        return {"access_token": "fresh", "expires_in": 3600}

    real = connections._post
    connections._post = fake_post
    try:
        check(connections.token(conn, "UTEST1", "google") == "fresh", "expired Google token refreshed")
    finally:
        connections._post = real
    check(sent.get("refresh_token") == "refresh-1" and sent.get("grant_type") == "refresh_token", "refresh uses stored token")
    check(connections.token(conn, "UTEST1", "google") == "fresh", "fresh token reused until expiry")
    try:
        connections.token(conn, "UTEST2", "google")
        check(False, "another person can't use it")
    except connections.NotConnected:
        check(True, "another person can't use someone else's account")
    check(connections.listing(conn, "UTEST1")[0]["account"] == "a@b.com", "my connections lists the account")
    check(connections.disconnect(conn, "UTEST1", "google") and not connections.listing(conn, "UTEST1"), "disconnect")


def test_tool_guard():
    out = json.loads(workspace.gcal_events({}))
    check(out["success"] is False and out.get("not_connected"), "tools refuse without a connected person")


if __name__ == "__main__":
    test_links()
    test_blocks()
    if os.environ.get("DATABASE_URL"):
        from perpetual_core import ledger
        conn = ledger.connect()
        ledger.ensure_schema(conn)
        test_tokens(conn)
        conn.close()
        test_tool_guard()
    print("all good")
