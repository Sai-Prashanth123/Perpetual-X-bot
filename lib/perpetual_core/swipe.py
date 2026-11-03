"""Swipe file: the posts in a client's niche that spread this week, ranked fairly.

Big accounts always win on raw likes, so each post is scored against its own author's median:
a small account's 6x post beats a big account's ordinary one. Only original posts count (no
replies, no retweets). One account failing to fetch never stops the others.
"""

import json
import re
import statistics

from . import console, ledger, radar
from .settings import client_handle

_THREAD = re.compile(r"🧵|(?<![\d/])1\s?/\s?(?:\d{1,2}|n)?(?![\d/])|\bthread\b\s*[:👇⬇↓]", re.IGNORECASE)


def _fetch(handle, days, limit):
    payload = {"username": handle, "since": console._days_ago(days), "limit": limit,
               "include_replies": False, "max_pages": 2}
    _, items = console.run_actor("user-tweets", payload)
    return [console.compact_tweet(t) for t in items if not t.get("is_retweet")]


def score(t):
    n = lambda k: t.get(k) or 0  # noqa: E731
    return n("bookmarks") * 2 + n("likes") + n("replies") * 1.5 + n("reposts") * 2 + n("quotes") * 2


def format_guess(t):
    text = t.get("text") or ""
    if t.get("article_title"):
        return "article"
    if t.get("long_form"):
        return "long"
    if _THREAD.search(text):
        return "thread"
    if t.get("is_quote"):
        return "quote"
    return "short"


def hook(text):
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()[:200]
    return ""


def _original(t):
    text = t.get("text") or ""
    return not (t.get("is_reply") or t.get("is_retweet") or text.startswith("RT @"))


def swipe_candidates(accounts, days=7, per_account=20, top=12, fetch=None):
    """Top original posts across `accounts`, ranked by score as a multiple of the author's median."""
    fetch = fetch or _fetch
    names = list(dict.fromkeys(radar.handles(",".join(accounts)) if isinstance(accounts, (list, tuple))
                               else radar.handles(accounts)))
    posts, failed = [], []
    for handle in names:
        try:
            tweets = [t for t in (fetch(handle, days, per_account) or []) if _original(t)]
        except Exception as e:  # noqa: BLE001 — one bad account must not sink the rest
            failed.append({"account": handle, "error": str(e)[:200]})
            continue
        if not tweets:
            continue
        scores = [score(t) for t in tweets]
        median = statistics.median(scores) or 1
        for t, s in zip(tweets, scores):
            tid = t.get("id")
            author = t.get("by") or handle
            posts.append({
                "author": author,
                "id": tid,
                "url": t.get("url") or (f"https://x.com/{author}/status/{tid}" if tid else None),
                "hook": hook(t.get("text")),
                "format": format_guess(t),
                "text": t.get("text"),
                "article_title": t.get("article_title"),
                "metrics": {k: t.get(k) or 0 for k in ("likes", "replies", "reposts", "quotes", "bookmarks", "views")},
                "age_h": t.get("age_h"),
                "score": round(s, 1),
                "multiple_of_median": round(s / median, 2),
            })
    posts.sort(key=lambda p: (p["multiple_of_median"], p["score"]), reverse=True)
    out = [{k: v for k, v in p.items() if v is not None} for p in posts[:max(1, int(top))]]
    return {"accounts": names, "days": days, "count": len(out), "posts": out, "failed": failed}


def default_accounts(conn, handle):
    row = conn.execute("SELECT radar_accounts, watch_accounts FROM clients WHERE handle = %s",
                       (handle,)).fetchone()
    if not row:
        return []
    accts = radar.handles(row["radar_accounts"]) or radar.handles(row["watch_accounts"])
    return [a for a in accts if a != handle.lower()]


# ---------------------------------------------------------------- tool

def swipe_candidates_tool(params, fetch=None, **kwargs):
    del kwargs
    p = params or {}
    try:
        accounts = p.get("accounts")
        if isinstance(accounts, str):
            accounts = radar.handles(accounts)
        if not accounts:
            handle = client_handle()
            if not handle:
                raise RuntimeError("no accounts given and CLIENT_HANDLE is not set")
            with ledger.connect() as conn:
                accounts = default_accounts(conn, handle)
            if not accounts:
                raise RuntimeError(f"@{handle} has no radar_accounts or watch_accounts; pass accounts")
        res = swipe_candidates(accounts, days=int(p.get("days") or 7), top=int(p.get("top") or 12),
                               fetch=fetch)
        return json.dumps({"success": True, **res}, ensure_ascii=False, default=str)
    except Exception as e:  # noqa: BLE001 — surface every failure to the model
        return json.dumps({"success": False, "error": str(e)})


TOOLS = [
    ("swipe_candidates", swipe_candidates_tool,
     "This week's best ORIGINAL posts from the client's radar accounts (else watch list), ranked by "
     "engagement as a multiple of each author's own median (so small accounts count). Each has author, url, "
     "hook, format guess (article/long/thread/quote/short), metrics, score, multiple_of_median.",
     {"days": {"type": "integer", "description": "Look-back window, default 7."},
      "top": {"type": "integer", "description": "How many posts to return, default 12."},
      "accounts": {"type": "array", "items": {"type": "string"},
                   "description": "Handles to scan instead of the client's radar/watch list."}},
     []),
]


def register(ctx, toolset="swipe"):
    for name, fn, description, props, required in TOOLS:
        ctx.register_tool(name=name, toolset=toolset, handler=fn,
                          schema={"name": name, "description": description,
                                  "parameters": {"type": "object", "properties": props, "required": required}})
