"""Hook intelligence: what opening lines are working in a niche right now, which of the client's own
hooks carried their best and worst posts, and a library of proven hooks the studio can reuse.

Model-free. Research is one console search (injectable for tests); the library is a Postgres table
(SCHEMA_SQL below) with a per-client copy of every hook a client used, so outcomes are tracked per
client and never leak into another client's ranking.
"""

import json
import re
from statistics import median

from . import console

LABELS = ("number", "question", "contrarian", "story", "how-to", "list", "bold-claim", "curiosity", "other")
VERDICTS = ("win", "par", "flop")
HOOK_MAX = 200

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS hook_library (
    id              BIGSERIAL PRIMARY KEY,
    client          TEXT,                            -- NULL = niche-wide
    niche           TEXT,
    hook            TEXT NOT NULL,
    label           TEXT,
    source_url      TEXT,
    source_author   TEXT,
    likes           INT,
    views           INT,
    bookmarks       INT,
    er              REAL,
    found_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    used_code       TEXT,
    outcome         TEXT,                            -- win|par|flop
    UNIQUE (client, source_url)
);
CREATE INDEX IF NOT EXISTS hook_library_client_niche ON hook_library (client, niche);
"""


def ensure_schema(conn):
    conn.execute(SCHEMA_SQL)
    conn.commit()


# ---------------------------------------------------------------- reading a hook

_URL = re.compile(r"https?://\S+")


def hook_of(text):
    """The hook: the first line of a post, cut to its first sentence, trimmed to ~200 chars."""
    for line in (text or "").splitlines():
        line = _URL.sub("", line).strip()
        if line:
            break
    else:
        return ""
    first = re.split(r"(?<=[.!?])\s+(?=\S)", line, maxsplit=1)[0].strip()
    if len(first) < 12 and first != line:     # "Wow." is not a hook on its own; keep the full line
        first = line
    if len(first) > HOOK_MAX:
        first = first[:HOOK_MAX - 1].rsplit(" ", 1)[0].rstrip(",;:-") + "…"
    return first


_HOWTO = re.compile(r"^(how (to|i|we|you)\b|here'?s how\b|the (easiest |simple |fastest )?way to\b|step[- ]by[- ]step\b"
                    r"|a (simple |quick )?guide\b)")
_LIST = re.compile(r"\b\d+\s+(\w+\s+){0,2}(ways|things|lessons|tips|tools|mistakes|reasons|steps|rules|habits|ideas"
                   r"|signs|books|prompts|questions|frameworks|truths|principles|strategies|hacks|examples)\b"
                   r"|^here (are|is a list)\b|🧵|\b(a )?thread\b|:\s*$")
_CONTRARIAN = re.compile(r"\b(unpopular opinion|hot take|controversial|stop (doing|using|posting|trying|writing)|"
                         r"is dead|are dead|overrated|is a myth|myth|is wrong|are wrong|you don'?t need|"
                         r"(is|are) (a )?lie|nobody needs|forget (about )?|don'?t (start|use|do|post|hire)|"
                         r"most people (are )?wrong|everyone is wrong|is killing)\b")
_STORY = re.compile(r"^(i |i'm |i've |i was |my |we |when i\b|last (week|month|year)|yesterday|today i|"
                    r"(\w+ )?years? ago|in 20\d\d|back in)")
_CURIOSITY = re.compile(r"\b(secret|nobody talks about|no one talks about|here'?s what|this is why|the truth about|"
                        r"what happened|you won'?t believe|the real reason|almost nobody|few people know|"
                        r"most people don'?t know|this changed)\b|\.\.\.|…")
_BOLD = re.compile(r"\b(the best|the only|the most|the biggest|always|never|every single|guaranteed|#1|"
                   r"will (replace|change|kill|be)|is the future|is everything|matters more than)\b")
_QWORD = re.compile(r"^(what|why|how|who|when|where|which|do|does|did|is|are|can|could|should|would|will|have|has)\b")


def classify_hook(text):
    """Heuristic label for a hook (or a whole post: only its hook is read)."""
    h = hook_of(text).lower()
    if not h:
        return "other"
    if _HOWTO.search(h):
        return "how-to"
    if _LIST.search(h):
        return "list"
    if h.rstrip().endswith("?") or (_QWORD.match(h) and "?" in h):
        return "question"
    if _CONTRARIAN.search(h):
        return "contrarian"
    if _STORY.search(h):
        return "story"
    if re.search(r"\d|\$|%", h):
        return "number"
    if _CURIOSITY.search(h):
        return "curiosity"
    if _BOLD.search(h):
        return "bold-claim"
    return "other"


# ---------------------------------------------------------------- niche research

def _search(payload):
    _, items = console.run_actor("search", payload)
    return items


def _n(v):
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _er(likes, replies, reposts, bookmarks, views):
    return round((likes + replies + reposts + bookmarks) / views, 4) if views else None


def research_query(topic, days=7, min_likes=200):
    topic = (topic or "").replace('"', "").strip()
    return f'"{topic}" lang:en -filter:replies min_faves:{int(min_likes)} since:{console._days_ago(days)}'


def research_hooks(topic, days=7, min_likes=200, limit=40, search=None):
    """Top-performing hooks on a topic over the last `days`. `search(payload) -> raw console items`."""
    search = search or _search
    payload = {"raw_query": research_query(topic, days, min_likes), "product": "Top", "exclude_retweets": True,
               "include_replies": False, "limit": max(limit * 2, 40), "max_pages": 2}
    seen, posts = set(), []
    for raw in search(payload) or []:
        if raw.get("is_retweet"):
            continue
        t = console.compact_tweet(raw)
        if t.get("is_reply"):
            continue
        hook = hook_of(t.get("article_title") or t.get("text"))
        key = t.get("url") or t.get("id") or hook
        if not hook or key in seen:
            continue
        seen.add(key)
        likes, replies, reposts = _n(t.get("likes")), _n(t.get("replies")), _n(t.get("reposts"))
        bookmarks, views = _n(t.get("bookmarks")), _n(t.get("views"))
        posts.append({"hook": hook, "label": classify_hook(hook), "likes": likes, "replies": replies,
                      "reposts": reposts, "bookmarks": bookmarks, "views": views or None,
                      "er": _er(likes, replies, reposts, bookmarks, views), "url": t.get("url"), "author": t.get("by")})
    # Rank = sum of two ranks: raw save+like pull, and engagement rate (posts without views rank last on ER).
    by_pull = sorted(posts, key=lambda p: -(p["bookmarks"] + p["likes"]))
    by_er = sorted(posts, key=lambda p: -(p["er"] or 0))
    rank = {id(p): i for i, p in enumerate(by_pull)}
    for i, p in enumerate(by_er):
        rank[id(p)] += i
    posts.sort(key=lambda p: (rank[id(p)], -(p["bookmarks"] + p["likes"])))
    return {"topic": topic, "days": days, "min_likes": min_likes, "found": len(posts),
            "hooks": posts[:limit], "by_label": summarize(posts)}


def summarize(posts):
    """{label: {count, median_likes, median_bookmarks}}, most common label first."""
    groups = {}
    for p in posts:
        groups.setdefault(p["label"], []).append(p)
    out = {}
    for label, ps in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        out[label] = {"count": len(ps), "median_likes": median(p["likes"] for p in ps),
                      "median_bookmarks": median(p["bookmarks"] for p in ps)}
    return out


# ---------------------------------------------------------------- the client's own hooks

def _row_hook(r):
    m = r["latest"] or {}
    if isinstance(m, str):
        m = json.loads(m)
    likes, replies, reposts = _n(m.get("likes")), _n(m.get("replies")), _n(m.get("reposts"))
    bookmarks, views = _n(m.get("bookmarks")), _n(m.get("views"))
    hook = hook_of(r["text"])
    return {"hook": hook, "label": classify_hook(hook), "code": r["code"], "views": views, "likes": likes,
            "bookmarks": bookmarks, "replies": replies, "reposts": reposts,
            "er": _er(likes, replies, reposts, bookmarks, views), "url": r["url"],
            "posted_at": r["posted_at"].isoformat() if r["posted_at"] else None}


def client_top_hooks(conn, client, days=60, n=10):
    """The client's best original posts (by views, then bookmarks) → their hooks, plus the worst 5."""
    rows = conn.execute("""SELECT code, text, url, posted_at, latest FROM outcomes
                           WHERE client = %s AND COALESCE(kind, 'tweet') <> 'reply'
                             AND posted_at >= now() - make_interval(days => %s) AND COALESCE(text, '') <> ''""",
                        (client, int(days))).fetchall()
    posts = [p for p in map(_row_hook, rows) if p["hook"]]
    key = lambda p: (p["views"], p["bookmarks"], p["likes"])  # noqa: E731
    best = sorted(posts, key=key, reverse=True)[:n]
    best_urls = {p["url"] for p in best}
    worst = [p for p in sorted(posts, key=key) if p["url"] not in best_urls][:5]
    return {"client": client, "days": days, "posts": len(posts), "best": best, "worst": worst,
            "by_label": summarize(posts)}


# ---------------------------------------------------------------- the library

def save_hooks(conn, client, niche, hooks):
    """Store researched hooks (client=None → niche-wide). Skips ones already saved. Returns ids saved."""
    saved = []
    for h in hooks or []:
        text = (h.get("hook") or "").strip()
        if not text:
            continue
        url = h.get("url") or h.get("source_url")
        dup = conn.execute("""SELECT id FROM hook_library WHERE client IS NOT DISTINCT FROM %s
                              AND (source_url = %s OR (%s::text IS NULL AND hook = %s))""",
                           (client, url, url, text)).fetchone()
        if dup:
            continue
        row = conn.execute(
            """INSERT INTO hook_library(client, niche, hook, label, source_url, source_author, likes, views,
                                        bookmarks, er)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING id""",
            (client, (niche or "").strip().lower() or None, text[:HOOK_MAX + 20], h.get("label") or classify_hook(text),
             url, (h.get("author") or h.get("source_author") or "").lstrip("@") or None,
             _n(h.get("likes")), _n(h.get("views")), _n(h.get("bookmarks")), h.get("er"))).fetchone()
        if row:
            saved.append(row["id"])
    conn.commit()
    return saved


_OUTCOME_RANK = "CASE outcome WHEN 'win' THEN 0 WHEN 'par' THEN 2 WHEN 'flop' THEN 3 ELSE 1 END"


def best_hooks(conn, client, niche=None, label=None, limit=15):
    """The client's own hooks plus niche-wide ones: proven wins first, flops last, then by saves/likes."""
    where, args = ["(client = %s OR client IS NULL)"], [client]
    if niche:
        where.append("niche = %s")
        args.append(niche.strip().lower())
    if label:
        where.append("label = %s")
        args.append(label)
    rows = conn.execute(
        f"""SELECT id, client, niche, hook, label, source_url, source_author, likes, views, bookmarks, er,
                   found_at, used_code, outcome FROM hook_library WHERE {' AND '.join(where)}
            ORDER BY {_OUTCOME_RANK}, COALESCE(bookmarks, 0) DESC, COALESCE(likes, 0) DESC, id DESC
            LIMIT %s""", (*args, int(limit))).fetchall()
    return [{**r, "scope": "client" if r["client"] else "niche", "found_at": r["found_at"].isoformat()}
            for r in rows]


def record_use(conn, client, hook_id, code):
    """Mark a hook as used by draft `code`. A niche-wide hook is copied into the client's own library first,
    so its outcome is recorded for this client only. Returns the client's hook row id."""
    row = conn.execute("SELECT * FROM hook_library WHERE id = %s AND (client = %s OR client IS NULL)",
                       (int(hook_id), client)).fetchone()
    if not row:
        raise ValueError(f"no hook {hook_id} in @{client}'s library")
    code = (code or "").strip().upper()
    if row["client"]:
        conn.execute("UPDATE hook_library SET used_code = %s, outcome = NULL WHERE id = %s", (code, row["id"]))
        hid = row["id"]
    else:
        mine = conn.execute("""SELECT id FROM hook_library WHERE client = %s
                               AND (source_url = %s OR (%s::text IS NULL AND hook = %s))""",
                            (client, row["source_url"], row["source_url"], row["hook"])).fetchone()
        if mine:
            conn.execute("UPDATE hook_library SET used_code = %s, outcome = NULL WHERE id = %s", (code, mine["id"]))
            hid = mine["id"]
        else:
            hid = conn.execute(
                """INSERT INTO hook_library(client, niche, hook, label, source_url, source_author, likes, views,
                                            bookmarks, er, used_code)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (client, row["niche"], row["hook"], row["label"], row["source_url"], row["source_author"],
                 row["likes"], row["views"], row["bookmarks"], row["er"], code)).fetchone()["id"]
    conn.commit()
    return hid


def record_outcome(conn, client, code, verdict):
    """Verdict for the post that used a hook (by draft code). Returns how many hooks were updated."""
    verdict = (verdict or "").strip().lower()
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {', '.join(VERDICTS)}")
    n = conn.execute("UPDATE hook_library SET outcome = %s WHERE client = %s AND used_code = %s",
                     (verdict, client, (code or "").strip().upper())).rowcount
    conn.commit()
    return n


# ---------------------------------------------------------------- agent tools

def _ok(**data):
    return json.dumps({"success": True, **data}, ensure_ascii=False, default=str)


def _fail(e):
    return json.dumps({"success": False, "error": str(e)})


def _client():
    from . import settings
    return settings.client_handle()


def hook_research(params, **kwargs):
    del kwargs
    p = params or {}
    try:
        topic = (p.get("topic") or "").strip()
        if not topic:
            raise ValueError("topic is required")
        client = _client()
        found = research_hooks(topic, days=int(p.get("days") or 7), min_likes=int(p.get("min_likes") or 200),
                               limit=int(p.get("limit") or 25))
        from . import ledger
        conn = ledger.connect()
        try:
            ensure_schema(conn)
            saved = save_hooks(conn, client, topic, found["hooks"])
            mine = client_top_hooks(conn, client)
        finally:
            conn.close()
        return _ok(**found, saved_to_library=len(saved), client_hooks=mine,
                   note="Compare the niche's winning hook shapes (by_label) with the client's own best and worst "
                        "hooks. Reuse a shape, never copy a line.")
    except Exception as e:  # noqa: BLE001
        return _fail(e)


def hooks_library(params, **kwargs):
    del kwargs
    p = params or {}
    try:
        client = _client()
        action = (p.get("action") or "best").lower()
        from . import ledger
        conn = ledger.connect()
        try:
            ensure_schema(conn)
            if action == "best":
                return _ok(hooks=best_hooks(conn, client, niche=p.get("niche"), label=p.get("label"),
                                            limit=int(p.get("limit") or 15)))
            if action == "use":
                if not p.get("hook_id") or not p.get("code"):
                    raise ValueError("use needs hook_id and code")
                return _ok(hook_id=record_use(conn, client, p["hook_id"], p["code"]), code=p["code"].upper())
            if action == "outcome":
                if not p.get("code") or not p.get("verdict"):
                    raise ValueError("outcome needs code and verdict (win|par|flop)")
                return _ok(updated=record_outcome(conn, client, p["code"], p["verdict"]))
            raise ValueError("action must be best, use or outcome")
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        return _fail(e)


_S = {"type": "string"}
_I = {"type": "integer"}

TOOLS = [
    ("hook_research", hook_research,
     "Research which opening hooks are winning on a topic right now (top English posts of the last `days` with at "
     "least `min_likes`): each hook with its label (number, question, contrarian, story, how-to, list, bold-claim, "
     "curiosity, other), likes, bookmarks, views and engagement rate, plus a per-label summary. Saves them to the "
     "client's hook library and returns the client's own best and worst hooks for comparison.",
     {"topic": _S, "days": _I, "min_likes": _I, "limit": _I}, ["topic"]),
    ("hooks_library", hooks_library,
     "The client's hook library. action 'best' (optional niche, label, limit): proven hooks, wins first. "
     "action 'use' (hook_id, code): record that draft `code` uses this hook. action 'outcome' (code, verdict "
     "win|par|flop): record how the post with that draft code did.",
     {"action": {"type": "string", "enum": ["best", "use", "outcome"]}, "niche": _S,
      "label": {"type": "string", "enum": list(LABELS)}, "limit": _I, "hook_id": _I, "code": _S,
      "verdict": {"type": "string", "enum": list(VERDICTS)}}, ["action"]),
]


def register(ctx, toolset="hooks"):
    for name, fn, description, props, required in TOOLS:
        ctx.register_tool(name=name, toolset=toolset, handler=fn,
                          schema={"name": name, "description": description,
                                  "parameters": {"type": "object", "properties": props, "required": required}})
