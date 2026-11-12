"""Audience Seeding data for Agency Bot (github.com/Sai-Prashanth123/Audience-Seeding, private).

That repo's GitHub Actions rebuild everything daily at 06:00 IST (live test every 4h, weekly on Sunday)
and publish a results-only DuckDB (data/dashboard.duckdb) + reports/*.md. Here we:

    sync(conn)        pull the newest snapshot when the repo changed (read-only token, SEEDING_GITHUB_TOKEN)
    post_daily(conn)  once a day: the summary + full reports to the HQ channel, each client's Engage list
                      to their channel (model-free)
    tools             audience_engage / audience_client / audience_account / audience_report /
                      audience_query (read-only SQL) / audience_status
"""

import json
import re
import shutil
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

from . import ledger
from .settings import env

REPO = "Sai-Prashanth123/Audience-Seeding"
API = "https://api.github.com"
FILES = ("data/dashboard.duckdb", "data/memory/patterns.csv", "config/segments.yaml")
STALE_HOURS = 36
MAX_ROWS = 200


class AudienceError(RuntimeError):
    pass


def repo():
    return env("SEEDING_REPO") or REPO


def root():
    return Path(env("PX_DATA_ROOT") or "/opt/data") / "audience"


def current_dir():
    p = root() / "CURRENT"
    if p.exists():
        d = root() / p.read_text().strip()
        if (d / "data" / "dashboard.duckdb").exists():
            return d
    return None


# ---------------------------------------------------------------- GitHub

def _get(path, raw=False, timeout=120):
    token = env("SEEDING_GITHUB_TOKEN")
    if not token:
        raise AudienceError("SEEDING_GITHUB_TOKEN isn't set on the server (read-only token for the Audience-Seeding repo)")
    req = urllib.request.Request(f"{API}/repos/{repo()}/{path}", headers={
        "Authorization": f"Bearer {token}", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "agency-bot",
        "Accept": "application/vnd.github.raw" if raw else "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
    except urllib.error.HTTPError as e:
        raise AudienceError(f"GitHub {e.code} for {path.split('?')[0]}") from None
    return data if raw else json.loads(data)


def _state(conn):
    return conn.execute("SELECT * FROM audience_state WHERE id = 1").fetchone() or {}


def sync(conn, get=None, force=False):
    """Download the newest snapshot if the repo moved. Returns a short status string."""
    get = get or _get
    sha = get("commits/main")["sha"]
    st = _state(conn)
    if not force and st.get("sha") == sha and current_dir():
        return f"up to date ({sha[:7]})"
    tmp = root() / f".{sha[:12]}"
    if tmp.exists():
        shutil.rmtree(tmp)
    for f in FILES:
        try:
            data = get(f"contents/{f}?ref={sha}", raw=True)
        except AudienceError:
            if f.endswith(".duckdb"):
                raise
            continue
        (tmp / f).parent.mkdir(parents=True, exist_ok=True)
        (tmp / f).write_bytes(data)
    for item in get(f"contents/reports?ref={sha}"):
        if item.get("type") == "file" and item["name"].endswith(".md"):
            (tmp / "reports").mkdir(parents=True, exist_ok=True)
            (tmp / "reports" / item["name"]).write_bytes(get(f"contents/reports/{item['name']}?ref={sha}", raw=True))
    final = root() / sha[:12]
    if final.exists():
        shutil.rmtree(final)
    tmp.rename(final)
    (root() / "CURRENT").write_text(sha[:12])
    for old in sorted((d for d in root().iterdir() if d.is_dir() and not d.name.startswith(".") and d != final),
                      key=lambda d: d.stat().st_mtime)[:-1]:
        shutil.rmtree(old, ignore_errors=True)                 # keep this version and the one before
    newest, trust_ok, detail = None, None, ""
    try:
        con = _db()
        newest = con.execute("SELECT newest FROM kpis").fetchone()[0]
        bad = con.execute("SELECT \"check\", detail FROM trust_checks WHERE lower(status) = 'fail'").fetchall()
        trust_ok = not bad
        detail = "; ".join(f"{c}: {d}" for c, d in bad)[:500]
        con.close()
    except Exception as e:  # noqa: BLE001 — the files are there; the summary is best effort
        detail = f"couldn't read summary: {e}"[:300]
    conn.execute("""INSERT INTO audience_state(id, sha, synced_at, newest_post, trust_ok, detail)
                    VALUES (1,%s,now(),%s,%s,%s) ON CONFLICT (id) DO UPDATE SET sha = EXCLUDED.sha,
                    synced_at = now(), newest_post = EXCLUDED.newest_post, trust_ok = EXCLUDED.trust_ok,
                    detail = EXCLUDED.detail""", (sha, newest, trust_ok, detail))
    conn.commit()
    _index_reports(conn, final)
    return f"synced {sha[:7]} (newest post {newest}, trust {'ok' if trust_ok else 'CHECK: ' + detail})"


def _index_reports(conn, folder):
    """Reports go into the document library (search_documents / search by meaning); old versions out."""
    try:
        from . import doclib
        for f in sorted((folder / "reports").glob("*.md")):
            name = f"Audience Seeding: {f.stem}"
            row = doclib.ingest(conn, f, name=name, source="audience-seeding", chat="C-audience-seeding")
            conn.execute("DELETE FROM library_docs WHERE source = 'audience-seeding' AND name = %s AND id <> %s",
                         (name, row["id"]))
        conn.commit()
    except Exception:  # noqa: BLE001 — search is a bonus; the tools read the files directly
        conn.rollback()


# ---------------------------------------------------------------- reading

def _db():
    d = current_dir()
    if not d:
        raise AudienceError("no Audience Seeding data synced yet (job px-audience; needs SEEDING_GITHUB_TOKEN)")
    import duckdb
    return duckdb.connect(str(d / "data" / "dashboard.duckdb"), read_only=True)


def _rows(con, sql, params=()):
    cur = con.execute(sql, list(params))
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _client_key(con, client):
    """The repo's spelling of a client (e.g. Kenny_GTM vs kenny_gtm)."""
    c = (client or "").lstrip("@").lower()
    for t, col in (("post_perf", "client"), ("engage_list", "client"), ("pattern_memory", "client")):
        try:
            hit = con.execute(f"SELECT DISTINCT {col} FROM {t} WHERE lower({col}) = ?", [c]).fetchone()
        except Exception:  # noqa: BLE001
            continue
        if hit:
            return hit[0]
    return client


def freshness(conn=None):
    """{synced, newest_post, hours_old, trust_ok, warning}."""
    own = conn is None
    conn = conn or ledger.connect()
    try:
        st = _state(conn)
    finally:
        if own:
            conn.close()
    newest = st.get("newest_post")
    hours = round((datetime.now(timezone.utc) - newest).total_seconds() / 3600, 1) if newest else None
    warn = []
    if hours is None or hours > STALE_HOURS:
        warn.append(f"data is {hours}h old (> {STALE_HOURS}h): the GitHub run may have failed")
    if st.get("trust_ok") is False:
        warn.append(f"a trust check failed: {st.get('detail')}")
    return {"synced": st.get("synced_at"), "commit": (st.get("sha") or "")[:7], "newest_post": newest,
            "hours_old": hours, "trust_ok": st.get("trust_ok"), "warning": "; ".join(warn) or None}


def engage(client, day=None, limit=20):
    con = _db()
    try:
        key = _client_key(con, client)
        day = day or con.execute("SELECT max(list_date) FROM engage_list WHERE client = ?", [key]).fetchone()[0]
        rows = _rows(con, """SELECT list_rank, url, author, seg, text, score, reason FROM engage_list
                             WHERE client = ? AND list_date = ? ORDER BY list_rank LIMIT ?""", (key, day, limit))
        cov = _rows(con, "SELECT list_date, listed, replied FROM engage_coverage WHERE client = ? "
                         "ORDER BY list_date DESC LIMIT 7", (key,))
        return {"client": key, "date": day, "posts": rows, "coverage_last_7_days": cov}
    finally:
        con.close()


def client_view(client):
    con = _db()
    try:
        key = _client_key(con, client)
        out = {"client": key}
        out["proven_patterns"] = _rows(con, """SELECT dim, value, status, direction, effect, lo, hi, n_own, weeks_cleared,
                                               reach_only FROM pattern_memory WHERE lower(client) = lower(?)
                                               ORDER BY status, abs(effect) DESC LIMIT 25""", (key,))
        out["content_ranges"] = _rows(con, """SELECT dim, value, posts, rel, rel_lo, rel_hi, status FROM content_ci
                                              WHERE lower(client) = lower(?) AND status IS NOT NULL
                                              ORDER BY CASE WHEN status ILIKE 'proven%' THEN 0 ELSE 1 END, abs(rel - 1) DESC
                                              LIMIT 20""", (key,))
        out["learning_mode"] = _rows(con, "SELECT mode, signals FROM learning_mode WHERE lower(client) = lower(?)", (key,))
        recent = _rows(con, """SELECT created_at, url, format, hook, topic, view_count, like_count, reply_count, xscore
                               FROM post_perf WHERE lower(client) = lower(?) AND created_at > now() - INTERVAL 14 DAY
                               ORDER BY xscore DESC""", (key,))
        out["last_14_days"] = {"posts": len(recent), "best": recent[:3], "worst": recent[-3:] if len(recent) > 3 else []}
        out["live_predictions"] = _rows(con, """SELECT created_at, format, hook, expected_vs_usual FROM live_preds
                                                WHERE lower(client) = lower(?) ORDER BY created_at DESC LIMIT 5""", (key,))
        out["tracking"] = _rows(con, """SELECT "group", week, engagers, niche_share FROM track_weekly
                                        WHERE lower(client) = lower(?) ORDER BY week DESC LIMIT 6""", (key,))
        return out
    finally:
        con.close()


def account(username):
    con = _db()
    try:
        u = (username or "").lstrip("@")
        return {"segment": _rows(con, "SELECT segment, also, confidence, n_posts FROM account_segments "
                                      "WHERE lower(username) = lower(?)", (u,)),
                "seed_map": _rows(con, "SELECT seg, rank, seed_score, reason FROM seed_map WHERE lower(username) = lower(?)", (u,)),
                "core": _rows(con, "SELECT core_segment, core_rank, core_score FROM core_accounts "
                                   "WHERE lower(username) = lower(?)", (u,)),
                "profile": _rows(con, "SELECT description, followers_count FROM profiles WHERE lower(handle) = lower(?)", (u,))}
    finally:
        con.close()


_SAFE = re.compile(r"^\s*(with|select)\b", re.I)
_BAD = re.compile(r"\b(insert|update|delete|drop|create|alter|attach|detach|copy|install|load|pragma|export|import|"
                  r"call|set|checkpoint|read_csv|read_parquet|read_json|read_text|read_blob|glob|httpfs)\b", re.I)


def query(sql):
    """Read-only SELECT over the snapshot (≤ 200 rows)."""
    if not _SAFE.match(sql or "") or _BAD.search(sql) or ";" in sql.strip().rstrip(";"):
        raise AudienceError("only one read-only SELECT is allowed")
    con = _db()
    try:
        cur = con.execute(f"SELECT * FROM ({sql.strip().rstrip(';')}) q LIMIT {MAX_ROWS}")
        cols = [c[0] for c in cur.description]
        return {"columns": cols, "rows": [list(r) for r in cur.fetchall()]}
    finally:
        con.close()


def tables():
    con = _db()
    try:
        return {t: [c for (c,) in con.execute("SELECT column_name FROM information_schema.columns WHERE table_name = ?",
                                              [t]).fetchall()]
                for (t,) in con.execute("SELECT table_name FROM information_schema.tables ORDER BY 1").fetchall()}
    finally:
        con.close()


def report(name=None):
    d = current_dir()
    if not d:
        raise AudienceError("no Audience Seeding data synced yet")
    files = sorted((d / "reports").glob("*.md"))
    if not name:
        return {"reports": [f.stem for f in files]}
    want = name.lower().replace(".md", "")
    hit = next((f for f in files if f.stem.lower() == want), None) or \
        next((f for f in files if want in f.stem.lower()), None)
    if not hit:
        raise AudienceError(f"no report like {name!r}; have: {', '.join(f.stem for f in files)}")
    return {"report": hit.stem, "text": hit.read_text(encoding="utf-8")[:20000]}


# ---------------------------------------------------------------- daily post to Slack (model-free)

def _fmt_engage(e):
    lines = [f"*🎯 Engage today, @{e['client']}* ({e['date']}): reply by hand, in the client's voice"]
    for p in e["posts"]:
        text = " ".join((p["text"] or "").split())[:140]
        lines.append(f"{p['list_rank']}. <{p['url']}|@{p['author']}> · {p['seg'] or ''}: {text}"
                     + (f"\n      _why: {p['reason'][:120]}_" if p.get("reason") else ""))
    return "\n".join(lines)


def summary_text(conn):
    f = freshness(conn)
    con = _db()
    try:
        k = _rows(con, "SELECT * FROM kpis")[0]
        lists = _rows(con, """SELECT client, count(*) n FROM engage_list
                              WHERE list_date = (SELECT max(list_date) FROM engage_list) GROUP BY 1 ORDER BY 1""")
        trust = _rows(con, "SELECT \"check\", status FROM trust_checks ORDER BY 1")
        modes = _rows(con, "SELECT client, mode FROM learning_mode WHERE mode IS NOT NULL AND lower(mode) <> 'normal'")
    finally:
        con.close()
    lines = [f"*📡 Audience Seeding: daily update* (data to {f['newest_post']:%d %b %H:%M} UTC, commit {f['commit']})"
             if f["newest_post"] else "*📡 Audience Seeding: daily update*"]
    if f["warning"]:
        lines.append(f"⚠️ {f['warning']}")
    lines.append(f"• Corpus: {k['posts']:,} posts from {k['authors']:,} authors, {k['profiles']:,} profiles")
    if lists:
        lines.append("• Engage lists today: " + ", ".join(f"@{r['client']} {r['n']}" for r in lists))
    if trust:
        bad = [t["check"] for t in trust if str(t["status"]).lower() == "fail"]
        warn = [t["check"] for t in trust if str(t["status"]).lower() == "warn"]
        lines.append("• Trust checks: " + ("❌ failing: " + "; ".join(bad) if bad else "all passing ✅")
                     + (f" (⚠️ {len(warn)} warning{'s' if len(warn) > 1 else ''})" if warn else ""))
    if modes:
        lines.append("• Fast-learning mode: " + ", ".join(f"@{m['client']} ({m['mode']})" for m in modes))
    lines.append("Full reports attached. Ask me anything about it: “who should Alex reply to today?”, "
                 "“what's proven for Michel?”, “show the 2B tracking”.")
    return "\n".join(lines)


def post_daily(conn, slack=None, upload=None, today=None):
    """Once per new daily snapshot: HQ summary + all reports as one file; each client's Engage list in their
    channel. Returns what was posted ([] when already done today)."""
    from . import agency, media, slackui
    slack = slack or slackui.slack
    upload = upload or media.upload_files
    today = today or date.today()
    conn.execute("ALTER TABLE audience_state ADD COLUMN IF NOT EXISTS posted_day DATE")
    st = _state(conn)
    if not st.get("sha") or st.get("posted_day") == today:
        return []
    d = current_dir()
    con = _db()
    try:
        latest = con.execute("SELECT max(list_date) FROM engage_list").fetchone()[0]
    finally:
        con.close()
    if latest and latest < today:
        return []                                   # today's 06:00 run isn't in yet; try again next sync
    posted = []
    hq = env("HQ_SLACK_CHANNEL")
    if hq:
        reports = sorted((d / "reports").glob("*.md"))
        bundle = "\n\n---\n\n".join(f.read_text(encoding="utf-8") for f in reports)
        upload(hq, [(f"audience-seeding-{today.isoformat()}.md", bundle.encode("utf-8"),
                     f"Audience Seeding reports {today:%d %b %Y}")], initial_comment=summary_text(conn))
        posted.append(f"HQ <#{hq}>: summary + {len(reports)} reports")
    for c in agency.active_clients(conn):
        if not c.get("slack_channel"):
            continue
        try:
            e = engage(c["handle"], day=latest)
        except AudienceError:
            continue
        if e["posts"]:
            slack("chat.postMessage", channel=c["slack_channel"], text=_fmt_engage(e), unfurl_links=False,
                  unfurl_media=False)
            posted.append(f"@{c['handle']}: {len(e['posts'])} posts to engage")
    conn.execute("UPDATE audience_state SET posted_day = %s WHERE id = 1", (today,))
    conn.commit()
    return posted


# ---------------------------------------------------------------- tools

def _ok(**d):
    return json.dumps({"success": True, **d}, ensure_ascii=False, default=str)


def _tool(fn):
    def handler(params, **_):
        try:
            out = fn(params or {})
            try:
                out["freshness"] = freshness()
            except Exception:  # noqa: BLE001
                pass
            return _ok(**out)
        except Exception as e:  # noqa: BLE001
            return json.dumps({"success": False, "error": str(e)})
    return handler


_S = {"type": "string"}
TOOLS = [
    ("audience_engage", _tool(lambda p: engage(p["client"], p.get("date"), int(p.get("limit") or 20))),
     "Audience Seeding: today's Engage list for an X client: fresh, on-topic posts from their seed map worth "
     "replying to (rank, link, author, segment, why), plus listed vs replied for the last 7 days. "
     "'Who should Alex reply to today?'", {"client": _S, "date": _S, "limit": {"type": "integer"}}, ["client"]),
    ("audience_client", _tool(lambda p: client_view(p["client"])),
     "Audience Seeding: what works for one client: proven / emerging / fading patterns (format, hook, topic, "
     "timing) with 95–99% ranges, learning mode, last 14 days best/worst posts, live predictions and treatment-vs-"
     "control tracking. Use before planning or drafting a client's content: recommend ONLY proven patterns "
     "(status habit/proven), never reach-only ones.", {"client": _S}, ["client"]),
    ("audience_account", _tool(lambda p: account(p["username"])),
     "Audience Seeding: one X account: niche segment (Tech/B2B/AI) and confidence, seed-map rank, core account, "
     "bio, followers. 'Is @x in our niche?'", {"username": _S}, ["username"]),
    ("audience_report", _tool(lambda p: report(p.get("name"))),
     "Audience Seeding reports (no name = list): proof, 2_weekly (tracking), 1D_live, 1D_backtest, 1B_holdout, "
     "1C_<client> content performance.", {"name": _S}, []),
    ("audience_query", _tool(lambda p: query(p["sql"]) if p.get("sql") else {"tables": tables()}),
     "Audience Seeding: read-only SQL (DuckDB, one SELECT, ≤ 200 rows) over every table: engage_list, "
     "engage_coverage, post_perf, content_ci, pattern_memory, pattern_history, learning_mode, live_preds, "
     "seed_map, core_accounts, account_segments, profiles, track_weekly, track_reply_back, proof, trust_checks, "
     "kpis. No sql = list tables and columns.", {"sql": _S}, []),
    ("audience_status", _tool(lambda p: {"tables": list(tables())}),
     "Audience Seeding: how fresh the data is, last sync, trust checks.", {}, []),
]


def register(ctx, toolset="audience"):
    for name, fn, description, props, required in TOOLS:
        ctx.register_tool(name=name, toolset=toolset, handler=fn,
                          schema={"name": name, "description": description,
                                  "parameters": {"type": "object", "properties": props, "required": required}})
