"""Personal tool connections: each teammate connects their own Google (Calendar, Meet, Gmail, Drive,
Docs, Sheets), Notion and Read AI, and the bot uses an account only when that same person is asking.

One exception, chosen by an owner: an *agency* connection (stored under AGENCY) that scheduled jobs
may use, e.g. the agency's Read AI so finished meetings become tasks without anyone asking.

Flow: the person asks Agency Bot "connect my Google" → a signed link is DMed to them (never posted
in a channel, where someone else could click it) → they sign in on the connector site → tokens are
stored encrypted under their Slack user id. Scheduled jobs have no person behind them, so they
can't use anyone's account.
"""

import base64
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from .settings import env

GOOGLE_SCOPES = " ".join([
    "openid", "email",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/meetings.space.readonly",     # Meet: past calls, participants, transcripts
])
READAI_SCOPES = "openid email profile meeting:read offline_access"
AGENCY = "agency"          # pseudo-user for the agency-wide connection (owners only)

PROVIDERS = {
    "google": {"label": "Google (Calendar, Gmail, Drive, Docs, Sheets)",
               "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
               "token_url": "https://oauth2.googleapis.com/token",
               "id_env": "GOOGLE_CLIENT_ID", "secret_env": "GOOGLE_CLIENT_SECRET"},
    "notion": {"label": "Notion",
               "auth_url": "https://api.notion.com/v1/oauth/authorize",
               "token_url": "https://api.notion.com/v1/oauth/token",
               "id_env": "NOTION_CLIENT_ID", "secret_env": "NOTION_CLIENT_SECRET"},
    "readai": {"label": "Read AI (meeting notes, summaries, action items)",
               "auth_url": "https://authn.read.ai/oauth2/auth",
               "token_url": "https://authn.read.ai/oauth2/token",
               "id_env": "READAI_CLIENT_ID", "secret_env": "READAI_CLIENT_SECRET"},
}


class NotConnected(RuntimeError):
    pass


def configured(provider):
    p = PROVIDERS[provider]
    return bool(env(p["id_env"]) and env(p["secret_env"]))


def current_user():
    """The Slack user id of whoever is talking to the bot right now (None in scheduled jobs)."""
    try:
        from gateway.session_context import get_session_env
        return get_session_env("HERMES_SESSION_USER_ID") or None
    except Exception:  # noqa: BLE001 — not inside a gateway turn
        return None


# ---------------------------------------------------------------- signed links

def _sig(provider, user, exp):
    key = env("INTERNAL_TOKEN") or ""
    return hmac.new(key.encode(), f"conn|{provider}|{user}|{exp}".encode(), hashlib.sha256).hexdigest()[:32]


def link(provider, user, ttl=3600):
    base = (env("AGENCY_WEB_URL") or "").rstrip("/")
    if provider not in PROVIDERS:
        raise ValueError(f"unknown tool {provider}; choose {', '.join(PROVIDERS)}")
    if not base.startswith("https://") or not env("INTERNAL_TOKEN"):
        raise RuntimeError("the connector site isn't configured (AGENCY_WEB_URL / INTERNAL_TOKEN)")
    exp = int(time.time()) + ttl
    q = urllib.parse.urlencode({"u": user, "exp": exp, "sig": _sig(provider, user, exp)})
    return f"{base}/connect/{provider}?{q}"


def verify(provider, user, exp, sig):
    try:
        if int(exp) < time.time():
            return False
    except (TypeError, ValueError):
        return False
    return bool(env("INTERNAL_TOKEN")) and hmac.compare_digest(_sig(provider, user, exp), sig or "")


# ---------------------------------------------------------------- OAuth

def redirect_uri(provider):
    return f"{(env('PUBLIC_URL') or env('AGENCY_WEB_URL') or '').rstrip('/')}/connect/{provider}/callback"


def pkce_verifier(state):
    """PKCE verifier derived from the OAuth state, so nothing extra has to be stored between the
    redirect and the callback (the state itself is random and checked against the session)."""
    return hmac.new((env("INTERNAL_TOKEN") or "").encode(), f"pkce|{state}".encode(), hashlib.sha256).hexdigest()


def pkce_challenge(verifier):
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


def authorize_url(provider, state):
    p = PROVIDERS[provider]
    q = {"client_id": env(p["id_env"]), "redirect_uri": redirect_uri(provider), "response_type": "code", "state": state}
    if provider == "google":
        q.update(scope=GOOGLE_SCOPES, access_type="offline", prompt="consent", include_granted_scopes="true")
    elif provider == "readai":
        q.update(scope=READAI_SCOPES, code_challenge=pkce_challenge(pkce_verifier(state)),
                 code_challenge_method="S256")
    else:
        q.update(owner="user")
    return f"{p['auth_url']}?{urllib.parse.urlencode(q)}"


def _post(url, data=None, headers=None, body=None):
    req = urllib.request.Request(url, method="POST", headers=headers or {},
                                 data=body if body is not None else urllib.parse.urlencode(data or {}).encode())
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{e.code}: {e.read().decode(errors='replace')[:300]}") from None


def _exchange(provider, code, state=None):
    p = PROVIDERS[provider]
    cid, secret = env(p["id_env"]), env(p["secret_env"])
    if provider == "readai":
        return _post(p["token_url"], {"grant_type": "authorization_code", "code": code, "client_id": cid,
                                      "client_secret": secret, "redirect_uri": redirect_uri(provider),
                                      "code_verifier": pkce_verifier(state or "")})
    if provider == "google":
        return _post(p["token_url"], {"code": code, "client_id": cid, "client_secret": secret,
                                      "redirect_uri": redirect_uri(provider), "grant_type": "authorization_code"})
    basic = __import__("base64").b64encode(f"{cid}:{secret}".encode()).decode()
    return _post(p["token_url"], headers={"Authorization": f"Basic {basic}", "Content-Type": "application/json"},
                 body=json.dumps({"grant_type": "authorization_code", "code": code,
                                  "redirect_uri": redirect_uri(provider)}).encode())


def _account(provider, tok):
    if provider == "google":
        req = urllib.request.Request("https://openidconnect.googleapis.com/v1/userinfo",
                                     headers={"Authorization": f"Bearer {tok['access_token']}"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read()).get("email") or "Google account"
    if provider == "readai":
        try:
            req = urllib.request.Request("https://authn.read.ai/userinfo",
                                         headers={"Authorization": f"Bearer {tok['access_token']}"})
            with urllib.request.urlopen(req, timeout=30) as r:
                info = json.loads(r.read())
            return info.get("email") or info.get("username") or "Read AI account"
        except Exception:  # noqa: BLE001 — the label is cosmetic
            return "Read AI account"
    owner = ((tok.get("owner") or {}).get("user") or {})
    who = (owner.get("person") or {}).get("email") or owner.get("name") or "Notion user"
    return f"{who} · {tok.get('workspace_name') or 'workspace'}"


def _fernet():
    from cryptography.fernet import Fernet
    key = env("TOKEN_ENCRYPTION_KEY")
    if not key:
        raise RuntimeError("TOKEN_ENCRYPTION_KEY is not set")
    return Fernet(key.encode())


def finish(conn, provider, user, code, state=None):
    """Exchange the code, identify the account, store the tokens encrypted. Returns the account label."""
    tok = _exchange(provider, code, state)
    if not tok.get("access_token"):
        raise RuntimeError(f"{provider} returned no access token")
    account = _account(provider, tok)
    old = get_row(conn, user, provider)
    refresh = tok.get("refresh_token") or (old and old["refresh_enc"] and _fernet().decrypt(old["refresh_enc"].encode()).decode())
    f = _fernet()
    expires = int(time.time()) + int(tok["expires_in"]) - 60 if tok.get("expires_in") else None
    conn.execute(
        """INSERT INTO user_connections(slack_user, provider, account, access_enc, refresh_enc, expires_at, scopes)
           VALUES (%s,%s,%s,%s,%s,to_timestamp(%s),%s)
           ON CONFLICT (slack_user, provider) DO UPDATE SET account = EXCLUDED.account,
               access_enc = EXCLUDED.access_enc, refresh_enc = EXCLUDED.refresh_enc,
               expires_at = EXCLUDED.expires_at, scopes = EXCLUDED.scopes, connected_at = now()""",
        (user, provider, account, f.encrypt(tok["access_token"].encode()).decode(),
         f.encrypt(refresh.encode()).decode() if refresh else None, expires, tok.get("scope")))
    conn.commit()
    return account


def get_row(conn, user, provider):
    return conn.execute("SELECT * FROM user_connections WHERE slack_user = %s AND provider = %s",
                        (user, provider)).fetchone()


REFRESHABLE = {"google", "readai"}


def token(conn, user, provider):
    """A valid access token for this account, refreshing it when it has expired.

    Read AI access tokens last 10 minutes and its refresh tokens rotate on every use, so the
    refresh runs under a row lock and the new refresh token is saved in the same transaction."""
    if not user:
        raise NotConnected("I can only use a connected account when its owner is the one asking.")
    row = get_row(conn, user, provider)
    if not row:
        if user == AGENCY:
            raise NotConnected(f"The agency's {PROVIDERS[provider]['label']} isn't connected yet. An owner can "
                               f"ask Agency Bot to 'connect {provider} for the agency'.")
        raise NotConnected(f"You haven't connected {PROVIDERS[provider]['label']} yet. "
                           f"Ask me to 'connect my {provider}' and I'll DM you a sign-in link.")
    f = _fernet()
    expired = row["expires_at"] and row["expires_at"].timestamp() < time.time()
    if provider not in REFRESHABLE or not expired:
        return f.decrypt(row["access_enc"].encode()).decode()
    conn.commit()                                  # start a clean transaction for the lock
    row = conn.execute("SELECT * FROM user_connections WHERE slack_user = %s AND provider = %s FOR UPDATE",
                       (user, provider)).fetchone()
    if row["expires_at"] and row["expires_at"].timestamp() >= time.time():
        conn.commit()                              # someone else refreshed while we waited
        return f.decrypt(row["access_enc"].encode()).decode()
    if not row["refresh_enc"]:
        conn.rollback()
        raise NotConnected(f"The {provider} connection expired. Ask me to 'connect my {provider}' again.")
    p = PROVIDERS[provider]
    try:
        tok = _post(p["token_url"], {"client_id": env(p["id_env"]), "client_secret": env(p["secret_env"]),
                                     "refresh_token": f.decrypt(row["refresh_enc"].encode()).decode(),
                                     "grant_type": "refresh_token"})
    except RuntimeError as e:
        conn.rollback()
        if "invalid_grant" in str(e):
            raise NotConnected(f"The {provider} connection was revoked or expired. "
                               f"Ask me to 'connect my {provider}' again.") from None
        raise
    new_refresh = tok.get("refresh_token")
    conn.execute("UPDATE user_connections SET access_enc = %s, expires_at = to_timestamp(%s)"
                 + (", refresh_enc = %s" if new_refresh else "")
                 + " WHERE slack_user = %s AND provider = %s",
                 (f.encrypt(tok["access_token"].encode()).decode(),
                  int(time.time()) + int(tok.get("expires_in", 3600)) - 60,
                  *([f.encrypt(new_refresh.encode()).decode()] if new_refresh else []), user, provider))
    conn.commit()
    return tok["access_token"]


def listing(conn, user):
    return [{"tool": r["provider"], "account": r["account"], "since": r["connected_at"].date().isoformat()}
            for r in conn.execute("SELECT provider, account, connected_at FROM user_connections WHERE slack_user = %s",
                                  (user,))]


def disconnect(conn, user, provider):
    n = conn.execute("DELETE FROM user_connections WHERE slack_user = %s AND provider = %s",
                     (user, provider)).rowcount
    conn.commit()
    return bool(n)
