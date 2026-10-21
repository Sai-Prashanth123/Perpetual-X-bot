"""X (Twitter) API: per-client OAuth 2.0 (PKCE) connection and posting approved suggestions.

What the API may post for a client (X rules since Feb 2026): original posts and threads, and
replies to people who engaged with the client (they mentioned or quoted the client). Replies,
quotes and reposts on anyone else's tweets are refused by X, so those return the suggestion's
"Open in X" intent link instead.
"""

import base64
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet

from . import ledger
from .settings import env

AUTHORIZE_URL = "https://x.com/i/oauth2/authorize"
TOKEN_URL = "https://api.x.com/2/oauth2/token"
API = "https://api.x.com/2"
SCOPES = "tweet.read tweet.write users.read offline.access"
POSTABLE = ledger.API_POSTABLE


class PostRefused(Exception):
    def __init__(self, message, intent_url=None):
        super().__init__(message)
        self.intent_url = intent_url


def _fernet():
    key = env("TOKEN_ENCRYPTION_KEY")
    if not key:
        raise RuntimeError("TOKEN_ENCRYPTION_KEY is not set")
    return Fernet(key.encode())


def _client_creds():
    cid, secret = env("X_CLIENT_ID"), env("X_CLIENT_SECRET")
    if not cid:
        raise RuntimeError("X_CLIENT_ID is not set — create an X developer app first")
    return cid, secret


def _redirect_uri():
    return (env("PUBLIC_URL") or env("AGENCY_WEB_URL") or "").rstrip("/") + "/x/callback"


# ---------------------------------------------------------------- OAuth

def start_connect():
    """Returns (authorize_url, state, verifier) — keep state + verifier in the session."""
    cid, _ = _client_creds()
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(24)
    url = AUTHORIZE_URL + "?" + urlencode({
        "response_type": "code", "client_id": cid, "redirect_uri": _redirect_uri(), "scope": SCOPES,
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})
    return url, state, verifier


def _token_request(data):
    cid, secret = _client_creds()
    auth = (cid, secret) if secret else None
    if not secret:
        data = {**data, "client_id": cid}
    r = httpx.post(TOKEN_URL, data=data, auth=auth, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"X token endpoint said {r.status_code}: {r.text[:300]}")
    return r.json()


def finish_connect(conn, client, code, verifier, by):
    tok = _token_request({"grant_type": "authorization_code", "code": code,
                          "redirect_uri": _redirect_uri(), "code_verifier": verifier})
    me = httpx.get(f"{API}/users/me", headers={"Authorization": f"Bearer {tok['access_token']}"}, timeout=30)
    me.raise_for_status()
    user = me.json()["data"]
    if user["username"].lower() != client.lower():
        raise RuntimeError(f"You signed in to X as @{user['username']}, but this is @{client}'s page. "
                           f"Sign in to X as @{client} and connect again.")
    _store(conn, client, tok, user, by)
    return user


def _store(conn, client, tok, user=None, by=None):
    f = _fernet()
    expires = datetime.now(timezone.utc) + timedelta(seconds=int(tok.get("expires_in", 7200)) - 60)
    conn.execute(
        """INSERT INTO x_tokens(client, x_user_id, x_username, access_enc, refresh_enc, expires_at, scope, connected_by, updated_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s, now())
           ON CONFLICT (client) DO UPDATE SET
               x_user_id = COALESCE(EXCLUDED.x_user_id, x_tokens.x_user_id),
               x_username = COALESCE(EXCLUDED.x_username, x_tokens.x_username),
               access_enc = EXCLUDED.access_enc,
               refresh_enc = COALESCE(EXCLUDED.refresh_enc, x_tokens.refresh_enc),
               expires_at = EXCLUDED.expires_at, scope = EXCLUDED.scope,
               connected_by = COALESCE(EXCLUDED.connected_by, x_tokens.connected_by), updated_at = now()""",
        (client, user and user["id"], user and user["username"], f.encrypt(tok["access_token"].encode()).decode(),
         f.encrypt(tok["refresh_token"].encode()).decode() if tok.get("refresh_token") else None,
         expires, tok.get("scope"), by))
    conn.commit()


def connection(conn, client):
    return conn.execute("SELECT x_username, expires_at, connected_by, updated_at FROM x_tokens WHERE client = %s",
                        (client,)).fetchone()


def disconnect(conn, client):
    conn.execute("DELETE FROM x_tokens WHERE client = %s", (client,))
    conn.commit()


def _access_token(conn, client):
    row = conn.execute("SELECT access_enc, refresh_enc, expires_at FROM x_tokens WHERE client = %s",
                       (client,)).fetchone()
    if not row:
        raise PostRefused(f"@{client}'s X account isn't connected yet — connect it on the dashboard first.")
    f = _fernet()
    if row["expires_at"] > datetime.now(timezone.utc):
        return f.decrypt(row["access_enc"].encode()).decode()
    if not row["refresh_enc"]:
        raise PostRefused(f"@{client}'s X connection expired — reconnect it on the dashboard.")
    tok = _token_request({"grant_type": "refresh_token",
                          "refresh_token": f.decrypt(row["refresh_enc"].encode()).decode()})
    _store(conn, client, tok)
    return tok["access_token"]


# ---------------------------------------------------------------- posting

def _create(token, text, reply_to=None):
    body = {"text": text}
    if reply_to:
        body["reply"] = {"in_reply_to_tweet_id": reply_to}
    r = httpx.post(f"{API}/tweets", json=body, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    if r.status_code not in (200, 201):
        detail = r.json().get("detail") if r.headers.get("content-type", "").startswith("application/json") else r.text
        raise PostRefused(f"X refused the post ({r.status_code}): {str(detail)[:300]}")
    return r.json()["data"]["id"]


def post_suggestion(conn, client, code, by, via):
    """Publish one suggestion. Returns {'tweet_id', 'url'}; raises PostRefused with a reason."""
    s = ledger.get(conn, client, code)
    if not s:
        raise PostRefused(f"There's no suggestion {code.upper()} for @{client}.")
    if s["status"] == "posted" and s.get("posted_id"):
        return {"tweet_id": s["posted_id"], "url": f"https://x.com/{client}/status/{s['posted_id']}", "already": True}
    if s["status"] == "rejected":
        raise PostRefused(f"{s['code']} was rejected. Approve or edit it first if you still want it.")
    if s["kind"] not in POSTABLE:
        raise PostRefused("X only lets apps post replies to people who mentioned or quoted the client, and "
                          "doesn't allow app quote-posts or reposts", intent_url=s.get("intent_url"))
    text = (s.get("final_text") or s["text"]).strip()
    token = _access_token(conn, client)
    if s["kind"] == "thread":
        parts = ledger.thread_parts(text)
        first = _create(token, parts[0])
        prev = first
        for part in parts[1:]:
            prev = _create(token, part, reply_to=prev)
        tweet_id = first
    elif s["kind"] == "engage":
        if not s.get("target_id"):
            raise PostRefused(f"{s['code']} has no tweet to reply to.")
        try:
            tweet_id = _create(token, text, reply_to=s["target_id"])
        except PostRefused as e:
            raise PostRefused(str(e), intent_url=s.get("intent_url")) from None
    else:
        tweet_id = _create(token, text)
    url = f"https://x.com/{client}/status/{tweet_id}"
    ledger.mark_published(conn, client, s["code"], tweet_id, url, text, by, via)
    return {"tweet_id": tweet_id, "url": url}


def post_and_report(client, code, by, via):
    """Post one suggestion, refresh its Slack card, and return a one-line message for chat."""
    from . import slackui
    conn = ledger.connect()
    try:
        try:
            res = post_suggestion(conn, client, code, by=by, via=via)
        except PostRefused as e:
            if e.intent_url:
                return f"{code} can't go out through the X API ({e}). Post it yourself: {e.intent_url}"
            return f"Couldn't post {code}: {e}"
        try:
            slackui.refresh_card(conn, client, code.upper())
        except Exception:  # noqa: BLE001 — the post went out; a stale card is cosmetic
            pass
        prefix = "Already posted" if res.get("already") else "✅ Posted"
        return f"{prefix} {code.upper()} to @{client}: {res['url']}"
    finally:
        conn.close()
