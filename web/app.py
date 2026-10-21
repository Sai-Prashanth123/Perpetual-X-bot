"""Perpetual X — the X account connector.

The agency runs entirely in Slack. This tiny service exists only because X's OAuth needs a public
web callback: Perpetual sends a signed "Connect X" link, the client logs in to X here, and the
encrypted token lands in the shared database so "Post now" works in Slack.

Run:  uvicorn app:app --app-dir web   (PYTHONPATH must include lib/)
"""

import hmac
import os
import secrets
from contextlib import contextmanager
from html import escape

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

from perpetual_core import agency, ledger, slackui, xpost

app = FastAPI(title="Perpetual X connector", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SessionMiddleware, secret_key=os.environ.get("SESSION_SECRET") or secrets.token_hex(32),
                   https_only=os.environ.get("PUBLIC_URL", "").startswith("https"), same_site="lax", max_age=1800)


@contextmanager
def db():
    conn = ledger.connect()
    try:
        yield conn
    finally:
        conn.close()


@app.on_event("startup")
def _schema():
    with db() as conn:
        ledger.ensure_schema(conn)


def page(title, message, ok=True, status=200):
    color = "#17804f" if ok else "#b83a26"
    return HTMLResponse(status_code=status, content=f"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)} — Perpetual X</title>
<style>
  :root {{ color-scheme: light dark; --bg:#f2f4f6; --card:#fff; --ink:#172433; --muted:#5e6b7a; }}
  @media (prefers-color-scheme: dark) {{ :root {{ --bg:#10161d; --card:#18212b; --ink:#e6ebf0; --muted:#93a0ae; }} }}
  body {{ margin:0; min-height:100vh; display:grid; place-items:center; background:var(--bg); color:var(--ink);
         font: 16px/1.5 system-ui, sans-serif; padding:16px; }}
  main {{ background:var(--card); border-radius:12px; padding:28px 32px; max-width:440px; }}
  h1 {{ margin:0 0 8px; font-size:1.4rem; color:{color}; }}
  p {{ margin:0; color:var(--muted); }}
</style></head><body><main><h1>{escape(title)}</h1><p>{escape(message)}</p></main></body></html>""")


@app.get("/x/connect")
def x_connect(request: Request, client: str = "", exp: str = "", sig: str = ""):
    key = os.environ.get("INTERNAL_TOKEN", "")
    client = agency.norm_handle(client)
    if not key or not slackui.verify_connect(client, exp, sig, key):
        return page("Link expired", "Ask Perpetual in Slack for a new Connect X link.", ok=False, status=403)
    with db() as conn:
        if not agency.get_client(conn, client):
            return page("Unknown client", f"@{client} isn't an agency client.", ok=False, status=404)
    url, state, verifier = xpost.start_connect()
    request.session["x_oauth"] = {"state": state, "verifier": verifier, "client": client}
    return RedirectResponse(url, status_code=303)


@app.get("/x/callback")
def x_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    pending = request.session.pop("x_oauth", None) or {}
    if error or not pending or not hmac.compare_digest(state, pending.get("state", "")):
        return page("Not connected", f"The X sign-in was cancelled or expired ({error or 'try the link again'}).", ok=False)
    client = pending["client"]
    with db() as conn:
        try:
            user = xpost.finish_connect(conn, client, code, pending["verifier"], by="connector")
        except Exception as e:  # noqa: BLE001
            return page("Not connected", str(e), ok=False)
        # Re-render open cards so their "Post now" buttons appear.
        for row in conn.execute("""SELECT code FROM suggestions WHERE client = %s AND slack_ts IS NOT NULL
                                   AND status IN ('pending','approved','edited') AND api_postable""", (client,)):
            try:
                slackui.refresh_card(conn, client, row["code"])
            except Exception:  # noqa: BLE001
                pass
    return page("X connected", f"@{user['username']} is connected. Go back to Slack — 'Post now' works on its drafts.")


@app.get("/")
def home():
    return page("Perpetual X", "Everything happens in Slack. Message Perpetual X there.")


@app.get("/healthz")
def healthz():
    with db() as conn:
        conn.execute("SELECT 1")
    return {"ok": True}
