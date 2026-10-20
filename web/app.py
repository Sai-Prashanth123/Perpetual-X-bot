"""Agency dashboard: every client's growth, the suggestion queues, the viral feed, competitors,
the team and what the bots are learning. Also the X-connect flow and the post endpoint the
Slack `/post` command calls.

Run:  uvicorn app:app --app-dir web   (PYTHONPATH must include lib/)
"""

import hmac
import json
import os
import secrets
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from perpetual_core import agency, console, ledger

import xapi

HERE = Path(__file__).parent
app = FastAPI(title="Perpetual X — agency dashboard", docs_url=None, redoc_url=None)
app.add_middleware(SessionMiddleware, secret_key=os.environ.get("SESSION_SECRET") or secrets.token_hex(32),
                   https_only=os.environ.get("PUBLIC_URL", "").startswith("https"), same_site="lax",
                   max_age=14 * 24 * 3600)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")

OWNERS = {s.strip() for s in os.environ.get("OWNER_SLACK_IDS", "").split(",") if s.strip()}
DEV_LOGIN = os.environ.get("DASHBOARD_DEV_LOGIN") == "1"


@app.on_event("startup")
def _schema():
    with db() as conn:
        ledger.ensure_schema(conn)


@contextmanager
def db():
    conn = ledger.connect()
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------- helpers for templates

def fmt_num(n):
    if n is None:
        return "–"
    n = float(n)
    for unit, size in (("M", 1e6), ("k", 1e3)):
        if abs(n) >= size:
            return f"{n / size:.1f}".rstrip("0").rstrip(".") + unit
    return f"{n:.0f}"


def fmt_delta(n, pct=False):
    if n is None:
        return "–"
    sign = "+" if n > 0 else ""
    return f"{sign}{n:.0f}%" if pct else f"{sign}{fmt_num(n)}"


def ago(ts):
    if not ts:
        return ""
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts)
    s = (datetime.now(timezone.utc) - ts).total_seconds()
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= size:
            return f"{int(s // size)}{unit} ago"
    return "just now"


templates.env.filters.update(num=fmt_num, delta=fmt_delta, ago=ago)


def page(request, name, **ctx):
    with db() as conn:
        clients = agency.active_clients(conn)
    return templates.TemplateResponse(request, name, {"user": request.session.get("user"),
                                                      "nav_clients": clients, **ctx})


def require_user(request):
    user = request.session.get("user")
    if not user and DEV_LOGIN and os.environ.get("DASHBOARD_DEV_AUTOLOGIN") == "1":  # local screenshots only
        user = request.session["user"] = {"id": "UDEV", "name": "Local tester", "owner": True}
    if not user:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return user


@app.exception_handler(HTTPException)
async def _redirects(request, exc):
    if exc.status_code == 303:
        return RedirectResponse(exc.headers["Location"], status_code=303)
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


# ---------------------------------------------------------------- sign in with Slack

@app.get("/login", response_class=HTMLResponse)
def login(request: Request):
    return templates.TemplateResponse(request, "login.html", {
        "dev": DEV_LOGIN, "error": request.query_params.get("e"),
        "slack": bool(os.environ.get("SLACK_CLIENT_ID")), "password": bool(os.environ.get("DASHBOARD_PASSWORD"))})


@app.get("/login/password")
def back_to_login(request: Request):
    """Refreshing or going Back to the sign-in form address lands here instead of a 405."""
    return RedirectResponse("/" if request.session.get("user") else "/login", status_code=303)


@app.get("/logout")
def logout_link(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/login/slack")
def login_slack(request: Request):
    if not os.environ.get("SLACK_CLIENT_ID"):
        return RedirectResponse("/login?e=Slack+sign-in+isn't+set+up+yet.+Use+the+dashboard+password.",
                                status_code=303)
    state = secrets.token_urlsafe(24)
    request.session["slack_state"] = state
    params = {"response_type": "code", "scope": "openid profile", "client_id": os.environ["SLACK_CLIENT_ID"],
              "redirect_uri": os.environ["PUBLIC_URL"].rstrip("/") + "/login/slack/callback", "state": state,
              "nonce": secrets.token_urlsafe(16)}
    if os.environ.get("SLACK_TEAM_ID"):
        params["team"] = os.environ["SLACK_TEAM_ID"]
    return RedirectResponse("https://slack.com/openid/connect/authorize?" + urlencode(params))


@app.get("/login/slack/callback")
def login_slack_callback(request: Request, code: str = "", state: str = ""):
    if not state or not hmac.compare_digest(state, request.session.pop("slack_state", "")):
        return RedirectResponse("/login?e=Sign-in+expired.+Try+again.", status_code=303)
    tok = httpx.post("https://slack.com/api/openid.connect.token", timeout=30, data={
        "client_id": os.environ["SLACK_CLIENT_ID"], "client_secret": os.environ["SLACK_CLIENT_SECRET"],
        "code": code, "redirect_uri": os.environ["PUBLIC_URL"].rstrip("/") + "/login/slack/callback"}).json()
    if not tok.get("ok"):
        return RedirectResponse(f"/login?e=Slack+said+{tok.get('error', 'no')}", status_code=303)
    info = httpx.get("https://slack.com/api/openid.connect.userInfo", timeout=30,
                     headers={"Authorization": f"Bearer {tok['access_token']}"}).json()
    team = info.get("https://slack.com/team_id")
    if os.environ.get("SLACK_TEAM_ID") and team != os.environ["SLACK_TEAM_ID"]:
        return RedirectResponse("/login?e=That+Slack+workspace+isn't+the+agency's.", status_code=303)
    user = {"id": info["sub"] if "sub" in info else info.get("https://slack.com/user_id"),
            "name": info.get("name") or info.get("given_name") or "teammate", "picture": info.get("picture")}
    user["owner"] = user["id"] in OWNERS
    request.session["user"] = user
    with db() as conn:
        conn.execute("""INSERT INTO employees(slack_id, name, is_owner, last_seen) VALUES (%s,%s,%s,now())
                        ON CONFLICT (slack_id) DO UPDATE SET name = EXCLUDED.name, is_owner = EXCLUDED.is_owner,
                        last_seen = now()""", (user["id"], user["name"], user["owner"]))
        conn.commit()
    return RedirectResponse(request.session.pop("next", "/"), status_code=303)


@app.post("/login/password")
def login_password(request: Request, password: str = Form(...), name: str = Form("Owner")):
    """Fallback sign-in (DASHBOARD_PASSWORD) for before Slack sign-in is set up."""
    expected = os.environ.get("DASHBOARD_PASSWORD", "")
    if not expected or not hmac.compare_digest(password.encode(), expected.encode()):
        time.sleep(1)  # slow down guessing
        return RedirectResponse("/login?e=Wrong+password.", status_code=303)
    request.session["user"] = {"id": "UPASSWORD", "name": name.strip()[:40] or "Owner", "owner": True}
    return RedirectResponse("/", status_code=303)


@app.post("/login/dev")
def login_dev(request: Request, name: str = Form("Local tester")):
    if not DEV_LOGIN:
        raise HTTPException(404)
    request.session["user"] = {"id": "UDEV", "name": name, "owner": True}
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ---------------------------------------------------------------- pages

@app.get("/", response_class=HTMLResponse)
def overview(request: Request, days: int = 7):
    require_user(request)
    with db() as conn:
        data = agency.overview(conn, days=days)
        spark = {c["client"]: [r["views"] or 0 for r in agency.client_series(conn, c["client"], days=28)]
                 for c in data["clients"]}
        totals = {k: sum((c[k] or 0) for c in data["clients"]) for k in ("views", "likes", "posts", "bookmarks")}
        foll = sum((c["followers_change"] or 0) for c in data["clients"])
    return page(request, "overview.html", data=data, spark=spark, totals=totals, foll_change=foll, days=days)


@app.get("/c/{handle}", response_class=HTMLResponse)
def client_page(request: Request, handle: str, status: str = ""):
    require_user(request)
    handle = handle.lower()
    with db() as conn:
        c = conn.execute("SELECT * FROM clients WHERE handle = %s", (handle,)).fetchone()
        if not c:
            raise HTTPException(404, "No such client")
        series = agency.client_series(conn, handle, days=60)
        queue = ledger.list_suggestions(conn, handle, status=status or None, days=7, limit=60)
        published = conn.execute("SELECT * FROM published WHERE client = %s ORDER BY at DESC LIMIT 10", (handle,)).fetchall()
        top = conn.execute(
            """SELECT tweet_id, url, text, code, (latest->>'likes')::int AS likes, (latest->>'views')::bigint AS views,
                      (latest->>'bookmarks')::int AS bookmarks, posted_at FROM outcomes
               WHERE client = %s AND posted_at >= now() - interval '14 days'
               ORDER BY (latest->>'likes')::int DESC NULLS LAST LIMIT 5""", (handle,)).fetchall()
        learning = conn.execute("SELECT at, summary, changes FROM learning_log WHERE client = %s ORDER BY id DESC LIMIT 5",
                                (handle,)).fetchall()
        docs = {r["key"].split(":", 1)[1]: r for r in conn.execute(
            "SELECT key, body, updated_at FROM documents WHERE key LIKE %s", (f"{handle}:%",))}
        x = xapi.connection(conn, handle)
        counts = {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, count(*) n FROM suggestions WHERE client = %s AND created_at >= now() - interval '7 days' GROUP BY 1",
            (handle,))}
    return page(request, "client.html", c=c, series=series, queue=queue, published=published, top=top,
                learning=learning, docs=docs, x=x, counts=counts, status=status,
                flash=request.session.pop("flash", None))


_comp_cache = {}


@app.get("/c/{handle}/competitors", response_class=HTMLResponse)
def competitors(request: Request, handle: str):
    """Watch accounts vs the client, from the scraping console (cached 6h — it's a live scrape)."""
    require_user(request)
    handle = handle.lower()
    hit = _comp_cache.get(handle)
    if hit and time.time() - hit[0] < 6 * 3600:
        rows = hit[1]
    else:
        with db() as conn:
            c = conn.execute("SELECT watch_accounts FROM clients WHERE handle = %s", (handle,)).fetchone()
        names = [handle] + [w.strip().lstrip("@") for w in (c and c["watch_accounts"] or "").split(",") if w.strip()][:6]
        rows = []
        for name in names:
            data = json.loads(console.x_user_tweets({"username": name, "since_days": 14, "limit": 40}))
            tweets = data.get("tweets") or []
            n = len(tweets)
            rows.append({"handle": name, "is_client": name == handle, "posts": n,
                         "avg_likes": round(sum(t.get("likes") or 0 for t in tweets) / n, 1) if n else None,
                         "avg_views": round(sum(t.get("views") or 0 for t in tweets) / n) if n else None,
                         "best": max(tweets, key=lambda t: t.get("likes") or 0) if tweets else None,
                         "error": None if data.get("success") else data.get("error")})
        _comp_cache[handle] = (time.time(), rows)
    return templates.TemplateResponse(request, "_competitors.html", {"rows": rows})


@app.get("/viral", response_class=HTMLResponse)
def viral(request: Request, days: int = 1, niche: str = ""):
    require_user(request)
    with db() as conn:
        tweets = agency.viral(conn, days=days, niche=niche or None, limit=60)
        niches = [r["niche"] for r in conn.execute(
            "SELECT DISTINCT niche FROM viral_feed WHERE day >= current_date - 7 ORDER BY 1")]
    return page(request, "viral.html", tweets=tweets, niches=niches, niche=niche, days=days,
                flash=request.session.pop("flash", None))


@app.get("/team", response_class=HTMLResponse)
def team(request: Request, days: int = 7):
    require_user(request)
    with db() as conn:
        rows = agency.team(conn, days=days)
        data = agency.overview(conn, days=days)
        names = {r["slack_id"]: r["name"] for r in conn.execute("SELECT slack_id, name FROM employees")}
    return page(request, "team.html", rows=rows, clients=data["clients"], names=names, days=days)


@app.get("/learning", response_class=HTMLResponse)
def learning(request: Request):
    require_user(request)
    with db() as conn:
        doc = conn.execute("SELECT body, updated_at FROM documents WHERE key = 'agency-playbook'").fetchone()
        log = conn.execute("SELECT client, at, summary, changes FROM learning_log ORDER BY id DESC LIMIT 40").fetchall()
    return page(request, "learning.html", doc=doc, log=log)


# ---------------------------------------------------------------- actions

@app.post("/c/{handle}/s/{code}/{action}")
def suggestion_action(request: Request, handle: str, code: str, action: str, text: str = Form(""), note: str = Form("")):
    user = require_user(request)
    handle = handle.lower()
    with db() as conn:
        if action == "post":
            try:
                res = xapi.post_suggestion(conn, handle, code, by=user["name"], via="dashboard")
                request.session["flash"] = {"ok": True, "msg": f"Posted {code.upper()}.", "url": res["url"]}
            except xapi.PostRefused as e:
                request.session["flash"] = {"ok": False, "msg": str(e), "url": e.intent_url}
        elif action in ("approve", "reject", "edit"):
            ledger.feedback(conn, handle, code, action, text=text or None, note=note or None, by=user["name"])
            request.session["flash"] = {"ok": True, "msg": f"{code.upper()}: {ledger.STATUS[action]}."}
        else:
            raise HTTPException(400, "unknown action")
    return RedirectResponse(f"/c/{handle}#{code.upper()}", status_code=303)


@app.post("/request")
def make_request(request: Request, client: str = Form(...), text: str = Form(...), back: str = Form("/viral")):
    user = require_user(request)
    with db() as conn:
        agency.add_request(conn, client.lower(), text, by=user["name"])
    request.session["flash"] = {"ok": True, "msg": f"Sent to @{client}'s bot — drafts land in its Slack channel within 15 minutes."}
    return RedirectResponse(back if back.startswith("/") else "/viral", status_code=303)


@app.get("/c/{handle}/x/connect")
def x_connect(request: Request, handle: str):
    require_user(request)
    url, state, verifier = xapi.start_connect()
    request.session["x_oauth"] = {"state": state, "verifier": verifier, "client": handle.lower()}
    return RedirectResponse(url, status_code=303)


@app.get("/x/callback")
def x_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    user = require_user(request)
    pending = request.session.pop("x_oauth", None) or {}
    client = pending.get("client", "")
    if error or not pending or not hmac.compare_digest(state, pending.get("state", "")):
        request.session["flash"] = {"ok": False, "msg": f"X connection cancelled ({error or 'state mismatch'})."}
        return RedirectResponse(f"/c/{client}" if client else "/", status_code=303)
    with db() as conn:
        try:
            xu = xapi.finish_connect(conn, client, code, pending["verifier"], by=user["name"])
            request.session["flash"] = {"ok": True, "msg": f"Connected @{xu['username']}. /post now works for this client."}
        except Exception as e:  # noqa: BLE001
            request.session["flash"] = {"ok": False, "msg": str(e)}
    return RedirectResponse(f"/c/{client}", status_code=303)


@app.post("/c/{handle}/x/disconnect")
def x_disconnect(request: Request, handle: str):
    require_user(request)
    with db() as conn:
        xapi.disconnect(conn, handle.lower())
    request.session["flash"] = {"ok": True, "msg": "Disconnected X. Posting is off for this client."}
    return RedirectResponse(f"/c/{handle.lower()}", status_code=303)


# ---------------------------------------------------------------- internal API (bot → web)

@app.post("/internal/post")
async def internal_post(request: Request):
    token = os.environ.get("INTERNAL_TOKEN", "")
    auth = request.headers.get("authorization", "")
    if not token or not hmac.compare_digest(auth, f"Bearer {token}"):
        raise HTTPException(401, "bad token")
    body = await request.json()
    with db() as conn:
        try:
            res = xapi.post_suggestion(conn, body["client"].lower(), body["code"], by=body.get("by"),
                                       via=body.get("via", "slack"))
            return {"ok": True, **res}
        except xapi.PostRefused as e:
            return JSONResponse({"ok": False, "error": str(e), "intent_url": e.intent_url}, status_code=200)
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(e)}, status_code=200)


@app.get("/healthz")
def healthz():
    with db() as conn:
        conn.execute("SELECT 1")
    return {"ok": True}
