"""Console tools — Hermes tools over the Thought Pilot scraping console REST API.

Read-only by design: nothing here can post, like, follow or log in to X.
Every tool returns compact JSON so the model spends tokens on judgement, not on
raw payloads.
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from .settings import client_handle, env

DEFAULT_URL = "https://scraping.thought-pilot.co/api"
MAX_LIMIT = 100          # hard cap per call so one tool call can't burn the scraping pool
JOB_TIMEOUT_S = 240
POLL_EVERY_S = 3


# ---------------------------------------------------------------- HTTP layer

def _base():
    return (env("XCONSOLE_URL") or DEFAULT_URL).rstrip("/")


def _request(method, path, body=None, params=None, timeout=60):
    key = env("XCONSOLE_API_KEY")
    if not key:
        raise RuntimeError("XCONSOLE_API_KEY is not set")
    url = _base() + path
    if params:
        clean = {k: v for k, v in params.items() if v is not None}
        url += "?" + urllib.parse.urlencode(clean, doseq=True)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-API-Key", key)
    req.add_header("Accept", "application/json")
    # Cloudflare in front of the console rejects urllib's default User-Agent.
    req.add_header("User-Agent", "perpetual-x-bot/1.0")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:400]
        raise RuntimeError(f"console {method} {path} -> HTTP {e.code}: {detail}") from None
    return json.loads(raw) if raw else None


def run_actor(actor, payload, items_limit=MAX_LIMIT, items_path="items"):
    """Submit a console job, wait for it, and return (job, items)."""
    job = _request("POST", "/jobs", {"actor": actor, "input": payload})
    job_id = job["id"]
    deadline = time.time() + JOB_TIMEOUT_S
    while job["status"] in ("queued", "running"):
        if time.time() > deadline:
            raise RuntimeError(f"console job {job_id} ({actor}) still {job['status']} after {JOB_TIMEOUT_S}s")
        time.sleep(POLL_EVERY_S)
        job = _request("GET", f"/jobs/{job_id}")
    if job["status"] != "succeeded":
        raise RuntimeError(f"console job {job_id} ({actor}) {job['status']}: {job.get('error')}")
    items = _request("GET", f"/jobs/{job_id}/{items_path}", params={"limit": items_limit}) or []
    return job, items


# ---------------------------------------------------------------- shaping

def _days_ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()


def _age_hours(created_at):
    try:
        ts = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    return max((datetime.now(timezone.utc) - ts).total_seconds() / 3600, 0.1)


def compact_tweet(t):
    """Keep what the model needs to judge a tweet; drop the rest."""
    likes = t.get("like_count", t.get("likes")) or 0
    age = _age_hours(t.get("created_at"))
    out = {
        "id": t.get("tweet_id"),
        "url": t.get("url"),
        "by": t.get("username"),
        "at": t.get("created_at"),
        "text": (t.get("text") or "")[:1200],
        "likes": likes,
        "replies": t.get("reply_count", t.get("replies")),
        "reposts": t.get("retweet_count", t.get("reposts")),
        "quotes": t.get("quote_count", t.get("quotes")),
        "bookmarks": t.get("bookmark_count", t.get("bookmarks")),
        "views": t.get("view_count", t.get("views")),
    }
    if t.get("is_reply"):
        out["is_reply"] = True
    if t.get("is_quote"):
        out["is_quote"] = True
    if t.get("is_long_form"):
        out["long_form"] = True
    if t.get("article_title"):
        out["article_title"] = t["article_title"]
    if age is not None:
        out["age_h"] = round(age, 1)
        out["likes_per_h"] = round(likes / age, 1)
    return {k: v for k, v in out.items() if v is not None}


def _client(params):
    handle = (params.get("username") or "").lstrip("@") or client_handle()
    if not handle:
        raise RuntimeError("no username given and CLIENT_HANDLE is not set")
    return handle


def _cap(n, default):
    try:
        return max(1, min(int(n), MAX_LIMIT))
    except (TypeError, ValueError):
        return default


def _ok(**data):
    return json.dumps({"success": True, **data}, ensure_ascii=False, default=str)


def _tool(fn):
    """Wrap a handler so failures come back as JSON the model can read."""
    def handler(params, **kwargs):
        del kwargs
        try:
            return fn(params or {})
        except Exception as e:  # noqa: BLE001 — surface every failure to the model
            return json.dumps({"success": False, "error": str(e)})
    return handler


# ---------------------------------------------------------------- tools

def x_find_tweets(p):
    since_days = p.get("since_days", 2)
    payload = {
        "words": p.get("query"),
        "raw_query": p.get("raw_query"),
        "any_of": p.get("any_of") or [],
        "from_accounts": p.get("from_accounts") or [],
        "min_likes": p.get("min_likes"),
        "min_replies": p.get("min_replies"),
        "since": _days_ago(since_days) if since_days else None,
        "lang": p.get("lang", "en"),
        "product": p.get("product", "Top"),
        "include_replies": p.get("include_replies", False),
        "exclude_retweets": True,
        "limit": _cap(p.get("limit"), 30),
        "max_pages": 3,
    }
    payload = {k: v for k, v in payload.items() if v not in (None, [], "")}
    _, items = run_actor("search", payload)
    tweets = [compact_tweet(t) for t in items if not t.get("is_retweet")]
    tweets.sort(key=lambda t: t.get("likes_per_h", 0), reverse=True)
    return _ok(count=len(tweets), tweets=tweets)


def x_trends(p):
    if p.get("run_fresh"):
        payload = {
            "topics": p.get("topics") or None,
            "clients": [_client(p)],
            "window_days": p.get("window_days", 5),
        }
        run_actor("trend-hunter", {k: v for k, v in payload.items() if v is not None}, items_limit=1)
    meta = _request("GET", "/trends/meta") or {}
    exemplars = [compact_tweet(e) for e in (meta.get("exemplars") or [])[: _cap(p.get("limit"), 25)]]
    result = {
        "window": [meta.get("window_start"), meta.get("window_end")],
        "meta": meta.get("meta"),
        "exemplars": exemplars,
    }
    try:
        result["client_recommendations"] = _request("GET", f"/trends/{_client(p)}")
    except RuntimeError as e:
        result["client_recommendations"] = None
        result["client_recommendations_note"] = str(e)
    return _ok(**result)


def x_client_content(p):
    handle = _client(p)
    days = p.get("days", 7)
    if p.get("refresh"):
        # Re-scrape so like/view counts are current, not as of the last daily harvest.
        run_actor("client-content", {"username": handle, "tabs": ["posts", "replies"],
                                     "since": _days_ago(days), "limit": 200, "max_pages": 3},
                  items_limit=1)
    data = _request("GET", f"/clients/{handle}/content",
                    params={"days": days, "tab": p.get("tab"), "limit": _cap(p.get("limit"), 100)})
    items = [compact_tweet(t) for t in (data or {}).get("items", [])]
    return _ok(username=handle, since=data.get("since"), until=data.get("until"),
               total=data.get("total"), posts=items)


def x_client_analytics(p):
    handle = _client(p)
    kind = p.get("kind", "summary")
    days = p.get("days", 30)
    if kind == "summary":
        data = _request("GET", f"/clients/{handle}/summary", params={"days": days, "top": p.get("top", 5)})
        if data and data.get("top_posts"):
            data["top_posts"] = [compact_tweet(t) for t in data["top_posts"]]
    elif kind == "rhythm":
        data = _request("GET", f"/clients/{handle}/rhythm", params={"days": days})
        if data:
            for key in ("by_hour", "by_weekday"):
                if isinstance(data.get(key), list):
                    data[key] = [row for row in data[key] if row.get("posts")]
    elif kind == "followers":
        data = _request("GET", f"/clients/{handle}/followers", params={"days": days})
    elif kind == "insights":
        data = _request("GET", f"/insights/{handle}", params={"limit": p.get("limit", 5)})
    else:
        raise RuntimeError(f"unknown kind {kind!r}; use summary, rhythm, followers or insights")
    return _ok(username=handle, kind=kind, data=data)


def x_engagers(p):
    handle = _client(p)
    payload = {"usernames": [handle], "since": _days_ago(p.get("since_days", 7)),
               "max_pages": 3, "include_quotes": True}
    _, items = run_actor("client-engagers", payload, items_limit=_cap(p.get("limit"), 60))
    return _ok(username=handle, count=len(items), engagements=[compact_tweet(t) for t in items])


def _tweet_id(value):
    value = str(value or "").strip()
    if "/status/" in value:
        value = value.split("/status/")[1].split("?")[0].split("/")[0]
    if not value.isdigit():
        raise RuntimeError(f"not a tweet id or status URL: {value!r}")
    return value


def x_tweet_detail(p):
    tid = _tweet_id(p.get("tweet") or p.get("tweet_id") or p.get("url"))
    _, items = run_actor("tweet-detail", {"tweet_id": tid, "limit": _cap(p.get("limit"), 40), "max_pages": 2})
    tweets = [compact_tweet(t) for t in items]
    root = next((t for t in tweets if t.get("id") == tid), None)
    replies = [t for t in tweets if t.get("id") != tid]
    replies.sort(key=lambda t: t.get("likes", 0), reverse=True)
    return _ok(tweet=root, top_replies=replies)


def x_user_tweets(p):
    handle = (p.get("username") or "").lstrip("@")
    if not handle:
        raise RuntimeError("username is required")
    payload = {"username": handle, "since": _days_ago(p.get("since_days", 14)),
               "include_replies": p.get("include_replies", False),
               "limit": _cap(p.get("limit"), 40), "max_pages": 3}
    _, items = run_actor("user-tweets", payload)
    tweets = [compact_tweet(t) for t in items if not t.get("is_retweet")]
    tweets.sort(key=lambda t: t.get("likes", 0), reverse=True)
    return _ok(username=handle, count=len(tweets), tweets=tweets)


def x_user_info(p):
    names = [n.lstrip("@") for n in (p.get("usernames") or []) if n]
    if not names:
        raise RuntimeError("usernames is required")
    _, profiles = run_actor("user-info", {"usernames": names[:50]}, items_path="profiles")
    return _ok(profiles=profiles)


# ---------------------------------------------------------------- registration

_STR = {"type": "string"}
_INT = {"type": "integer"}
_BOOL = {"type": "boolean"}
_STRS = {"type": "array", "items": {"type": "string"}}

TOOLS = [
    ("x_find_tweets", x_find_tweets,
     "Search X for recent high-engagement tweets (via the scraping console). Results are sorted by "
     "likes per hour so the fastest-rising tweets come first. Use for finding reply/quote targets, "
     "breakouts and niche conversations.",
     {"query": {**_STR, "description": "Keywords (all must match)."},
      "any_of": {**_STRS, "description": "Match any of these terms."},
      "raw_query": {**_STR, "description": "Full X advanced-search string, used verbatim. Overrides the friendly fields."},
      "from_accounts": {**_STRS, "description": "Only tweets by these handles."},
      "min_likes": {**_INT, "description": "Engagement floor, e.g. 50 for replies, 150 for trends."},
      "min_replies": _INT,
      "since_days": {**_INT, "description": "Look back this many days (default 2)."},
      "product": {"type": "string", "enum": ["Top", "Latest"], "description": "Default Top."},
      "include_replies": {**_BOOL, "description": "Default false."},
      "lang": {**_STR, "description": "Default en."},
      "limit": {**_INT, "description": "Max tweets (default 30, cap 100)."}},
     []),
    ("x_trends", x_trends,
     "The console's current viral meta: exemplar tweets across the niche plus this client's latest "
     "trend-hunter recommendations. Set run_fresh=true to re-run the trend hunter first (slow, use at most daily).",
     {"run_fresh": _BOOL, "topics": {**_STRS, "description": "Override topics for a fresh run."},
      "window_days": _INT, "limit": _INT, "username": _STR},
     []),
    ("x_client_content", x_client_content,
     "The client's own published tweets with engagement metrics. refresh=true re-scrapes first so "
     "numbers are current (use for outcome matching and reports).",
     {"username": {**_STR, "description": "Defaults to the client this bot serves."},
      "days": {**_INT, "description": "Window in days (default 7)."},
      "tab": {**_STR, "description": "posts, replies, articles or omit for all."},
      "refresh": _BOOL, "limit": _INT},
     []),
    ("x_client_analytics", x_client_analytics,
     "Aggregated analytics for the client: kind=summary (totals, per-post averages, top posts), "
     "rhythm (performance by posting hour/weekday), followers (growth), insights (console-written reviews).",
     {"kind": {"type": "string", "enum": ["summary", "rhythm", "followers", "insights"]},
      "days": _INT, "top": _INT, "limit": _INT, "username": _STR},
     ["kind"]),
    ("x_engagers", x_engagers,
     "Who replied to or quoted the client's posts recently — people worth engaging back.",
     {"since_days": {**_INT, "description": "Default 7."}, "limit": _INT, "username": _STR},
     []),
    ("x_tweet_detail", x_tweet_detail,
     "One tweet plus its thread and top replies. Accepts a tweet id or x.com status URL. Use before "
     "drafting a reply so you know what has already been said.",
     {"tweet": {**_STR, "description": "Tweet id or x.com/.../status/<id> URL."}, "limit": _INT},
     ["tweet"]),
    ("x_user_tweets", x_user_tweets,
     "Any account's recent original tweets, sorted by likes. Use for peer/competitor watch.",
     {"username": _STR, "since_days": _INT, "include_replies": _BOOL, "limit": _INT},
     ["username"]),
    ("x_user_info", x_user_info,
     "Profile details (bio, follower counts) for up to 50 handles.",
     {"usernames": _STRS},
     ["usernames"]),
]


def register(ctx, toolset="xconsole"):
    for name, fn, description, props, required in TOOLS:
        ctx.register_tool(
            name=name,
            toolset=toolset,
            schema={"name": name, "description": description,
                    "parameters": {"type": "object", "properties": props, "required": required}},
            handler=_tool(fn),
        )
