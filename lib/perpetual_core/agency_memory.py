"""Long-term memory for Agency Bot, so it never "forgets" between Slack conversations.

Two layers, both in Postgres (shared by every profile, survive restarts and redeploys):

    agency_memory      facts worth keeping: decisions, preferences, client details, who does what.
                       Saved by the agent (`remember`) whenever it learns something durable; scoped
                       to the agency, a person, or a client.
    conversation_log   every chat exchange (person, channel, what they asked, what Agency Bot answered).

Hooks (registered by both plugins):
    pre_llm_call   injects what's relevant into the turn: the asker's own memories, agency/client
                   memories that match the message, and, when a conversation is new, the last few
                   exchanges with this person (from any thread or channel), so a new thread continues
                   where the last one stopped.
    post_llm_call  logs the exchange.
"""

import json
import re

from . import ledger

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS agency_memory (
    id          SERIAL PRIMARY KEY,
    scope       TEXT NOT NULL,                    -- agency | person:<slack id> | client:<handle>
    text        TEXT NOT NULL,
    created_by  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used   TIMESTAMPTZ,
    uses        INT NOT NULL DEFAULT 0,
    active      BOOLEAN NOT NULL DEFAULT true
);
CREATE INDEX IF NOT EXISTS agency_memory_scope ON agency_memory(scope, active);
CREATE TABLE IF NOT EXISTS conversation_log (
    id          BIGSERIAL PRIMARY KEY,
    slack_user  TEXT,
    chat_id     TEXT,
    platform    TEXT,
    session_id  TEXT,
    user_text   TEXT,
    reply_text  TEXT,
    at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS conversation_log_user ON conversation_log(slack_user, at DESC);
ALTER TABLE conversation_log ADD COLUMN IF NOT EXISTS digested_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS agency_memory_fts ON agency_memory USING GIN (to_tsvector('english', text));
CREATE INDEX IF NOT EXISTS conversation_log_fts ON conversation_log
    USING GIN (to_tsvector('english', coalesce(user_text, '') || ' ' || coalesce(reply_text, '')));
"""

STOP = set("""a an the and or but if then so to of in on at for with from by is are was were be been it this that
these those i you we they he she me my our your their its what which who whom how when where why can could would
should will shall do does did done have has had not no yes please pls ok okay hi hello hey thanks thank also aslo
need want like just get give make show tell send all any some more most much very about into over after before""".split())
MAX_CONTEXT = 2800


def _words(text):
    return [w for w in re.findall(r"[a-z0-9@_]{3,}", (text or "").lower()) if w not in STOP][:25]


def _user():
    from . import connections
    return connections.current_user()


def _env(name):
    try:
        from gateway.session_context import get_session_env
        return get_session_env(name) or None
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- facts

def scope_for(about, user=None):
    a = (about or "agency").strip()
    if a.lower() in ("agency", "team", "everyone", "all"):
        return "agency"
    if a.lower() in ("me", "myself", "mine") and user:
        return f"person:{user}"
    if a.startswith("@") or a.lower().startswith("client:"):
        return "client:" + a.split(":")[-1].lstrip("@").lower()
    if a.startswith("person:") or a.startswith("client:"):
        return a
    from . import tasks
    oid, _, problem = tasks.resolve_owner(a)
    if oid and not problem:
        return f"person:{oid}"
    return "client:" + a.lstrip("@").lower()


def remember(conn, text, scope="agency", by=None):
    text = (text or "").strip()
    if not text:
        raise ValueError("nothing to remember")
    dup = conn.execute("SELECT id FROM agency_memory WHERE scope = %s AND active AND lower(text) = lower(%s)",
                       (scope, text)).fetchone()
    if dup:
        return dup["id"]
    row = conn.execute("INSERT INTO agency_memory(scope, text, created_by) VALUES (%s,%s,%s) RETURNING id",
                       (scope, text[:1000], by)).fetchone()
    conn.commit()
    return row["id"]


def _tsquery(words):
    """'invoice | pricing | alex' — any word matches, stemmed (invoices ~ invoice); ranking does the rest."""
    terms = [re.sub(r"[^a-z0-9]", "", w) for w in words]
    return " | ".join(t for t in terms if len(t) >= 3)


def search(conn, query, scopes=None, limit=8):
    """Facts by meaning first (semantic search, when on), topped up by full-text matches."""
    from . import semantic
    hits = semantic.search(conn, query, ["memory"], scopes, limit)
    if not hits:
        return _search_text(conn, query, scopes, limit)
    rows = conn.execute("SELECT *, 1.0 AS score FROM agency_memory WHERE id = ANY(%s) AND active",
                        ([h["ref_id"] for h in hits],)).fetchall()
    order = {h["ref_id"]: i for i, h in enumerate(hits)}
    rows = sorted(rows, key=lambda r: order[r["id"]])
    seen = {r["id"] for r in rows}
    rows += [r for r in _search_text(conn, query, scopes, limit) if r["id"] not in seen]
    return rows[:limit]


def _search_text(conn, query, scopes=None, limit=8):
    """Facts ranked by full-text relevance (stemmed, any word), then by plain word overlap, then recency."""
    words = _words(query)
    q, vals = ["active"], []
    if scopes:
        q.append("scope = ANY(%s)")
        vals.append(list(scopes))
    tsq = _tsquery(words)
    if tsq:
        rows = conn.execute(
            f"""SELECT *, ts_rank(to_tsvector('english', text), to_tsquery('english', %s)) AS score
                FROM agency_memory WHERE {' AND '.join(q)} AND to_tsvector('english', text) @@ to_tsquery('english', %s)
                ORDER BY score DESC, created_at DESC LIMIT %s""", (tsq, *vals, tsq, limit)).fetchall()
        if rows:
            return rows
    if words:
        score = " + ".join(["(CASE WHEN lower(text) LIKE %s THEN 1 ELSE 0 END)"] * len(words))
        vals_score = [f"%{w}%" for w in words]
        rows = conn.execute(f"SELECT *, ({score}) AS score FROM agency_memory WHERE {' AND '.join(q)} "
                            f"ORDER BY score DESC, created_at DESC LIMIT %s", (*vals_score, *vals, limit)).fetchall()
        return [r for r in rows if r["score"] > 0]
    return conn.execute(f"SELECT * FROM agency_memory WHERE {' AND '.join(q)} ORDER BY created_at DESC LIMIT %s",
                        (*vals, limit)).fetchall()


def recent_exchanges(conn, user, limit=5, exclude_session=None):
    return conn.execute("""SELECT * FROM conversation_log WHERE slack_user = %s
                           AND (%s::text IS NULL OR session_id IS DISTINCT FROM %s)
                           ORDER BY at DESC LIMIT %s""", (user, exclude_session, exclude_session, limit)).fetchall()[::-1]


def search_log(conn, query, user=None, limit=8):
    """Past exchanges by meaning first (when on), else full-text; `user` limits to one person's chats."""
    from . import semantic
    hits = semantic.search(conn, query, ["conversation"], [user] if user else None, limit)
    if hits:
        rows = conn.execute("SELECT *, 1.0 AS score FROM conversation_log WHERE id = ANY(%s)",
                            ([h["ref_id"] for h in hits],)).fetchall()
        order = {h["ref_id"]: i for i, h in enumerate(hits)}
        return sorted(rows, key=lambda r: order[r["id"]])
    return _search_log_text(conn, query, user, limit)


def _search_log_text(conn, query, user=None, limit=8):
    words = _words(query)
    if not words:
        return []
    tsq = _tsquery(words)
    if tsq:
        doc = "to_tsvector('english', coalesce(user_text, '') || ' ' || coalesce(reply_text, ''))"
        rows = conn.execute(f"""SELECT *, ts_rank({doc}, to_tsquery('english', %s)) AS score FROM conversation_log
                                WHERE {doc} @@ to_tsquery('english', %s) AND (%s::text IS NULL OR slack_user = %s)
                                ORDER BY score DESC, at DESC LIMIT %s""", (tsq, tsq, user, user, limit)).fetchall()
        if rows:
            return rows
    score = " + ".join(["(CASE WHEN lower(user_text || ' ' || coalesce(reply_text,'')) LIKE %s THEN 1 ELSE 0 END)"] * len(words))
    return conn.execute(f"""SELECT *, ({score}) AS score FROM conversation_log
                            WHERE (%s::text IS NULL OR slack_user = %s) ORDER BY score DESC, at DESC LIMIT %s""",
                        (*[f"%{w}%" for w in words], user, user, limit)).fetchall()


def digest_lines(conn, limit=80, mark=True):
    """Nightly memory digest (monitor script): the conversations not yet reviewed, one line each, for the
    agent to pull durable facts from. Nothing new → no lines → no agent run. Marks them reviewed."""
    rows = conn.execute("""SELECT * FROM conversation_log WHERE digested_at IS NULL
                           AND at > now() - interval '3 days' ORDER BY at LIMIT %s""", (limit,)).fetchall()
    if not rows:
        return []
    from . import slackui
    names = {}
    out = []
    for r in rows:
        who = r["slack_user"]
        if who not in names:
            try:
                names[who] = slackui.person_name(who) or who
            except Exception:  # noqa: BLE001
                names[who] = who
        out.append(f"[{r['at']:%d %b %H:%M}] {names[who]} ({who}) asked: {_clip(r['user_text'], 400)} "
                   f"|| you answered: {_clip(r['reply_text'], 300)}")
    if mark:
        conn.execute("UPDATE conversation_log SET digested_at = now() WHERE id = ANY(%s)", ([r["id"] for r in rows],))
        conn.commit()
    return out


# ---------------------------------------------------------------- hooks

def _clip(s, n):
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def context_for(conn, user, message, is_first_turn, session_id=None):
    """The text injected into a turn ('' when there's nothing useful)."""
    if not user:
        return ""
    clients = [m.lstrip("@").lower() for m in re.findall(r"@([A-Za-z0-9_]{2,})", message or "")]
    own = conn.execute("SELECT * FROM agency_memory WHERE scope = %s AND active ORDER BY created_at DESC LIMIT 8",
                       (f"person:{user}",)).fetchall()
    shared = search(conn, message, ["agency"] + [f"client:{c}" for c in clients], limit=8)
    if clients:
        shared += [r for r in conn.execute("SELECT * FROM agency_memory WHERE scope = ANY(%s) AND active "
                                           "ORDER BY created_at DESC LIMIT 6", ([f"client:{c}" for c in clients],))
                   if r["id"] not in {s["id"] for s in shared}]
    lines = []
    try:                                   # learning from experience: lessons + learning style come first
        from . import brain
        lines += brain.context_lines(conn, user, message, clients, session_id)
    except Exception:  # noqa: BLE001 — learning must never break a reply
        conn.rollback()
    facts = own + [r for r in shared if r["id"] not in {o["id"] for o in own}]
    if facts:
        lines.append("What you remember (use it; don't repeat it back unless asked):")
        lines += [f"- [{r['scope'].split(':')[0]}] {_clip(r['text'], 220)}" for r in facts[:14]]
        conn.execute("UPDATE agency_memory SET uses = uses + 1, last_used = now() WHERE id = ANY(%s)",
                     ([r["id"] for r in facts[:14]],))
        conn.commit()
    if is_first_turn:
        past = recent_exchanges(conn, user, 5, exclude_session=session_id)
        if past:
            lines.append("Your last exchanges with this person (earlier conversations, oldest first):")
            lines += [f"- {r['at']:%d %b %H:%M} they: {_clip(r['user_text'], 180)} | you: {_clip(r['reply_text'], 180)}"
                      for r in past]
    text = "\n".join(lines)
    return text[:MAX_CONTEXT]


def pre_llm_call(session_id=None, user_message=None, is_first_turn=False, **_):
    user = _user()
    if not user or not isinstance(user_message, str):
        return None
    try:
        conn = ledger.connect()
        try:
            try:                           # their message scores your last reply (corrected / redo / praised)
                from . import brain
                brain.score_previous(conn, user, user_message)
            except Exception:  # noqa: BLE001
                conn.rollback()
            text = context_for(conn, user, user_message, bool(is_first_turn), session_id)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — memory must never break a reply
        return None
    return {"context": text} if text else None


def post_llm_call(session_id=None, user_message=None, assistant_response=None, platform=None, **_):
    user = _user()
    if not user or not isinstance(user_message, str) or not user_message.strip() or not assistant_response:
        return None
    from . import brain
    tools, errors, lessons_used = brain.take_turn(session_id)
    try:
        conn = ledger.connect()
        try:
            conn.execute("""INSERT INTO conversation_log(slack_user, chat_id, platform, session_id, user_text, reply_text,
                                                         situation, tools, tool_errors, reward, lessons_used)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::int[])""",
                         (user, _env("HERMES_SESSION_CHAT_ID"), str(platform or ""), session_id,
                          user_message[:3000], str(assistant_response)[:3000], brain.situation(user_message),
                          json.dumps(tools) if tools else None, errors, -0.2 * errors, lessons_used))
            conn.execute("DELETE FROM conversation_log WHERE at < now() - interval '120 days'")
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass
    return None


# ---------------------------------------------------------------- tools

def _ok(**d):
    return json.dumps({"success": True, **d}, ensure_ascii=False, default=str)


def tool_remember(params, **_):
    conn = ledger.connect()
    try:
        user = _user()
        scope = scope_for(params.get("about"), user)
        mid = remember(conn, params.get("fact"), scope, by=user)
        return _ok(saved=mid, scope=scope)
    except Exception as e:  # noqa: BLE001
        return json.dumps({"success": False, "error": str(e)})
    finally:
        conn.close()


def tool_recall(params, **_):
    conn = ledger.connect()
    try:
        user = _user()
        q = params.get("query") or ""
        scopes = None
        if params.get("about"):
            scopes = [scope_for(params["about"], user)]
        facts = search(conn, q, scopes, limit=15)
        talks = search_log(conn, q, None if params.get("everyone") else user, limit=8)
        return _ok(facts=[{"id": r["id"], "scope": r["scope"], "text": r["text"], "saved": r["created_at"]} for r in facts],
                   conversations=[{"when": r["at"], "who": r["slack_user"], "asked": _clip(r["user_text"], 300),
                                   "answered": _clip(r["reply_text"], 400)} for r in talks])
    finally:
        conn.close()


def tool_forget(params, **_):
    conn = ledger.connect()
    try:
        n = conn.execute("UPDATE agency_memory SET active = false WHERE id = %s", (int(params["id"]),)).rowcount
        conn.commit()
        return _ok(forgotten=bool(n))
    finally:
        conn.close()


_S = {"type": "string"}
TOOLS = [
    ("remember", tool_remember,
     "Save something worth remembering for good: decisions, preferences, client details, who owns what, "
     "deadlines, how someone likes things done. about: 'agency' (default, everyone), 'me' (the asker), a "
     "teammate's name, or a client @handle. One clear sentence per fact. Use it whenever you learn something "
     "durable, without being asked.", {"fact": _S, "about": _S}, ["fact"]),
    ("recall", tool_recall,
     "Search long-term memory and past conversations (any thread/channel) for a topic: 'what did we decide about "
     "Alex's pricing?', 'what did Shravya ask yesterday?'. about narrows to agency / me / a person / @client; "
     "everyone=true searches all people's conversations.",
     {"query": _S, "about": _S, "everyone": {"type": "boolean"}}, ["query"]),
    ("forget", tool_forget, "Forget one remembered fact by id (from recall).", {"id": {"type": "integer"}}, ["id"]),
]


def register(ctx, toolset="memory_store"):
    for name, fn, description, props, required in TOOLS:
        ctx.register_tool(name=name, toolset=toolset, handler=fn,
                          schema={"name": name, "description": description,
                                  "parameters": {"type": "object", "properties": props, "required": required}})
    if hasattr(ctx, "register_hook"):
        ctx.register_hook("pre_llm_call", pre_llm_call)
        ctx.register_hook("post_llm_call", post_llm_call)
    from . import brain
    brain.register(ctx, toolset)       # learning from experience: lessons, reward, nightly sleep
