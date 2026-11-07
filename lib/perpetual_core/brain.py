"""Learning from experience: how Agency Bot gets better the way a brain does.

    Brain                          Here
    hippocampus (episodes)         conversation_log rows + situation, tools used, tool errors, reward
    dopamine (prediction error)    the person's next message scores the last reply: corrected / redo / praised
    sleep (consolidation)          nightly `sleep`: replays the most surprising episodes -> lessons
    neocortex (know-how)           lessons: one rule each, scoped to the agency / a person / a client
    strengthening + forgetting     lesson strength goes up when confirmed, down when contradicted, fades unused
    metacognition                  learning style per person + situation: careful where it keeps being
                                   corrected, direct where it's reliably right
    childhood                      bootstrap: score the whole conversation history once to seed it

Everything here is cheap: regex + SQL per turn, one Haiku run a night (the `sleep` skill).
"""

import json
import re

from . import ledger

SCHEMA_SQL = """
ALTER TABLE conversation_log ADD COLUMN IF NOT EXISTS situation TEXT;
ALTER TABLE conversation_log ADD COLUMN IF NOT EXISTS tools JSONB;
ALTER TABLE conversation_log ADD COLUMN IF NOT EXISTS tool_errors INT NOT NULL DEFAULT 0;
ALTER TABLE conversation_log ADD COLUMN IF NOT EXISTS reward REAL NOT NULL DEFAULT 0;
ALTER TABLE conversation_log ADD COLUMN IF NOT EXISTS signals TEXT[];
ALTER TABLE conversation_log ADD COLUMN IF NOT EXISTS lessons_used INT[];
ALTER TABLE conversation_log ADD COLUMN IF NOT EXISTS consolidated_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS conversation_log_pending ON conversation_log(at) WHERE consolidated_at IS NULL;
CREATE TABLE IF NOT EXISTS lessons (
    id           SERIAL PRIMARY KEY,
    scope        TEXT NOT NULL,                 -- agency | person:<slack id> | client:<handle>
    situation    TEXT NOT NULL DEFAULT 'any',
    rule         TEXT NOT NULL,
    kind         TEXT NOT NULL DEFAULT 'do',    -- do | avoid | prefer
    strength     REAL NOT NULL DEFAULT 1.0,
    evidence     INT NOT NULL DEFAULT 1,
    episode_ids  BIGINT[],
    created_by   TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used    TIMESTAMPTZ,
    uses         INT NOT NULL DEFAULT 0,
    active       BOOLEAN NOT NULL DEFAULT true
);
CREATE INDEX IF NOT EXISTS lessons_scope ON lessons(scope, active);
CREATE INDEX IF NOT EXISTS lessons_fts ON lessons USING GIN (to_tsvector('english', rule));
"""

FOLLOWUP_WINDOW_MIN = 30
DECAY_AFTER_DAYS = 14
DECAY = 0.95
PRUNE_BELOW = 0.3
SIMILAR_DISTANCE = 0.15            # cosine distance: ≥ 0.85 similar = the same lesson
SITUATIONS = ("tasks", "research", "content", "numbers", "automation", "client", "files", "chat")

# ---------------------------------------------------------------- situation (what kind of ask)

_KEYS = {
    "tasks": r"task|todo|to-do|remind|deadline|assign|standup|meeting|schedule|calendar|follow ?up|pending|slack list",
    "research": r"research|find|search|look ?up|competitor|compare|crawl|website|repo|github|tool",
    "content": r"post|tweet|thread|draft|write|caption|linkedin|hook|content|article|voice|newsletter|copy",
    "numbers": r"revenue|income|invoice|money|dollar|rupee|usd|\$|metric|stats|analytic|how many|how much|growth|"
               r"numbers|report|kpi|budget|price|pricing",
    "automation": r"build|code|bot|script|automat|api|deploy|integrat|feature|bug|error|fix|agent|server|workflow",
    "client": r"client|onboard|account|@[a-z0-9_]{2,}",
    "files": r"pdf|file|document|attachment|docx|xlsx|upload|slides|sheet|spreadsheet",
}
_KEYS = {k: re.compile(rf"(?:{v})", re.I) for k, v in _KEYS.items()}


def situation(text):
    """The kind of ask, by keyword hits (no model). Ties go to the earlier, more specific kind."""
    t = text or ""
    best, hits = "chat", 0
    for name, rx in _KEYS.items():
        n = len(rx.findall(t))
        if n > hits:
            best, hits = name, n
    return best


# ---------------------------------------------------------------- reward (the next message scores the last reply)

_CORRECTION = re.compile(
    r"^\s*(no+|nope|nah|wrong)\b|"
    r"\bnot (this|that|what i|like this|correct|right|working|at all)\b|"
    r"\b(that'?s|this is|it'?s|its) (not|wrong)\b|"
    r"\b(wrong|incorrect|mistake)\b|"
    r"\bi (said|meant|asked|told you)\b|"
    r"\b(didn'?t|did not|never) (ask|say|want)\b|"
    r"\bstill (not|wrong|broken|the same|same|failing|giving)\b|"
    r"\bwhy (did|are|do) you\b|"
    r"\b(you|u) (missed|forgot|ignored|skipped)\b|"
    r"\b(doesn'?t|does not|isn'?t|is not|not) work(ing)?\b|"
    r"\b(try|do it|check) again\b|\bsame (issue|problem|error|mistake)\b|"
    r"\b(redo|re-do)\b", re.I)
_PRAISE_STRONG = re.compile(r"\b(perfect|great|awesome|excellent|amazing|exactly|brilliant|love it|good job|"
                            r"well done|nice work|spot on|works now|working now|it works|that works)\b", re.I)
_PRAISE = re.compile(r"\b(thanks|thank you|thx|ty|nice|good|cool|works|worked)\b", re.I)


def _words(text):
    from .agency_memory import _words as w
    return set(w(text))


def score_followup(prev_user_text, new_text):
    """(reward, signals) that the person's next message gives the previous reply."""
    new = (new_text or "").strip()
    if not new:
        return 0.0, []
    if _CORRECTION.search(new):
        return -1.0, ["corrected"]
    a, b = _words(prev_user_text), _words(new)
    if len(a) >= 3 and len(b) >= 3 and len(a & b) / len(a | b) >= 0.6:
        return -0.5, ["repeated"]
    if _PRAISE_STRONG.search(new):
        return 1.0, ["praised"]
    if _PRAISE.search(new) and len(new) < 80:
        return 0.5, ["thanked"]
    return 0.0, []


def _adjust(conn, ids, delta):
    if ids:
        conn.execute("""UPDATE lessons SET strength = greatest(0, strength + %s), last_seen = now()
                        WHERE id = ANY(%s)""", (delta, list(ids)))


def score_previous(conn, user, new_text):
    """Called before each reply: the person's last unscored exchange (≤ 30 min ago) gets its reward, and the
    lessons used in it learn from the surprise (corrected: weaker; praised: stronger)."""
    prev = conn.execute(
        """SELECT id, user_text, reward, lessons_used FROM conversation_log
           WHERE slack_user = %s AND signals IS NULL AND at > now() - make_interval(mins => %s)
           ORDER BY at DESC LIMIT 1""", (user, FOLLOWUP_WINDOW_MIN)).fetchone()
    if not prev:
        return None
    reward, signals = score_followup(prev["user_text"], new_text)
    conn.execute("UPDATE conversation_log SET reward = reward + %s, signals = %s::text[] WHERE id = %s",
                 (reward, signals, prev["id"]))
    if reward < 0:
        _adjust(conn, prev["lessons_used"], -0.5)
    elif reward > 0:
        _adjust(conn, prev["lessons_used"], 0.3 * reward)
    conn.commit()
    return reward, signals


# ---------------------------------------------------------------- tools seen in a turn (post_tool_call)

_TURN_TOOLS = {}      # session_id -> [[name, status], ...]
_INJECTED = {}        # session_id -> lesson ids injected this turn


def post_tool_call(tool_name="", session_id=None, status=None, **_):
    key = session_id or "-"
    calls = _TURN_TOOLS.setdefault(key, [])
    if len(calls) < 40:
        calls.append([tool_name, status or "ok"])
    if len(_TURN_TOOLS) > 200:                      # never grow without bound
        _TURN_TOOLS.pop(next(iter(_TURN_TOOLS)))
    return None


def take_turn(session_id):
    """Tools used + lessons injected during this turn (cleared)."""
    key = session_id or "-"
    calls = _TURN_TOOLS.pop(key, [])
    errors = sum(1 for _, s in calls if str(s).lower() not in ("ok", "success", "succeeded", "none", ""))
    return calls, errors, _INJECTED.pop(key, [])


# ---------------------------------------------------------------- lessons (know-how)

def _norm_situation(s):
    s = (s or "any").strip().lower()
    return s if s in SITUATIONS else "any"


def _similar(conn, rule, scope):
    """An active lesson in this scope that says the same thing (by meaning when on, else by words)."""
    from . import semantic
    hits = semantic.search(conn, rule, ["lesson"], [scope], limit=1, max_distance=SIMILAR_DISTANCE)
    if hits:
        row = conn.execute("SELECT * FROM lessons WHERE id = %s AND active", (hits[0]["ref_id"],)).fetchone()
        if row:
            return row
    words = _words(rule)
    if not words:
        return None
    for row in conn.execute("SELECT * FROM lessons WHERE scope = %s AND active", (scope,)):
        other = _words(row["rule"])
        if other and len(words & other) / len(words | other) >= 0.7:
            return row
    return None


def learn(conn, rule, scope="agency", situation_="any", kind="do", episode_ids=None, by=None):
    """Save a lesson, or strengthen the one that already says this. Returns (id, 'new'|'reinforced')."""
    rule = " ".join((rule or "").split())[:400]
    if len(rule) < 8:
        raise ValueError("a lesson needs one clear sentence")
    kind = kind if kind in ("do", "avoid", "prefer") else "do"
    eps = [int(e) for e in (episode_ids or []) if str(e).isdigit()]
    same = _similar(conn, rule, scope)
    if same:
        conn.execute("""UPDATE lessons SET strength = strength + 1, evidence = evidence + 1, last_seen = now(),
                        episode_ids = (SELECT array_agg(DISTINCT e) FROM unnest(coalesce(episode_ids, '{}') || %s::bigint[]) e)
                        WHERE id = %s""", (eps, same["id"]))
        conn.commit()
        return same["id"], "reinforced"
    row = conn.execute("""INSERT INTO lessons(scope, situation, rule, kind, episode_ids, created_by)
                          VALUES (%s,%s,%s,%s,%s::bigint[],%s) RETURNING id""",
                       (scope, _norm_situation(situation_), rule, kind, eps, by)).fetchone()
    conn.commit()
    from . import semantic
    if semantic.ready(conn):
        try:                                   # embed now so tonight's next lesson can find it
            semantic.store(conn, [("lesson", row["id"], 0, scope, rule)])
        except Exception:  # noqa: BLE001 — px-index catches up
            conn.rollback()
    return row["id"], "new"


def weaken(conn, lesson_id, amount=1.0, remove=False):
    if remove:
        n = conn.execute("UPDATE lessons SET active = false WHERE id = %s", (lesson_id,)).rowcount
    else:
        n = conn.execute("""UPDATE lessons SET strength = greatest(0, strength - %s),
                            active = (strength - %s) >= %s WHERE id = %s""",
                         (amount, amount, PRUNE_BELOW, lesson_id)).rowcount
    conn.commit()
    return bool(n)


def relevant(conn, user, message, clients=(), limit=5):
    """Lessons for this turn: matched by meaning (or words), plus this person's strongest habits for this
    kind of ask. Ranked by match x strength, current situation first."""
    from . import semantic
    sit = situation(message)
    scopes = ["agency", f"person:{user}"] + [f"client:{c}" for c in clients]
    scored = {}
    for h in semantic.search(conn, message, ["lesson"], scopes, limit=10, max_distance=0.6):
        scored[h["ref_id"]] = 1.0 - h["distance"]
    words = [re.sub(r"[^a-z0-9]", "", w) for w in _words(message)]
    tsq = " | ".join(w for w in words if len(w) >= 3)
    if tsq:
        for r in conn.execute("""SELECT id FROM lessons WHERE active AND scope = ANY(%s)
                                 AND to_tsvector('english', rule) @@ to_tsquery('english', %s) LIMIT 10""",
                              (scopes, tsq)):
            scored.setdefault(r["id"], 0.5)
    for r in conn.execute("""SELECT id FROM lessons WHERE active AND scope = %s AND situation IN (%s, 'any')
                             ORDER BY strength DESC LIMIT 3""", (f"person:{user}", sit)):
        scored.setdefault(r["id"], 0.45)
    if not scored:
        return []
    rows = conn.execute("SELECT * FROM lessons WHERE id = ANY(%s) AND active", (list(scored),)).fetchall()
    rank = lambda r: scored[r["id"]] * (1 + r["strength"]) * (1.2 if r["situation"] in (sit, "any") else 0.8)
    return sorted(rows, key=rank, reverse=True)[:limit]


# ---------------------------------------------------------------- learning style (metacognition)

def style_for(conn, user, sit):
    """How to work with this person on this kind of ask, from their record (no model)."""
    r = conn.execute(
        """SELECT count(*) n,
                  count(*) FILTER (WHERE signals && ARRAY['corrected','repeated']) bad
           FROM conversation_log WHERE slack_user = %s AND situation = %s AND signals IS NOT NULL
           AND at > now() - interval '30 days'""", (user, sit)).fetchone()
    lines = []
    n, bad = r["n"], r["bad"]
    if n >= 3 and bad / n >= 0.3:
        lines.append(f"Careful mode ({bad} of your last {n} '{sit}' replies to this person were corrected): restate "
                     "in one line exactly what you'll deliver before doing it; for big work show the plan and wait "
                     "for 'go'; ask one sharp question if anything is ambiguous.")
    elif n >= 6 and bad / n <= 0.05:
        lines.append(f"Direct mode (they've been happy with your '{sit}' work): act without asking and deliver.")
    p = conn.execute(
        """SELECT avg(length(user_text)) avg_len,
                  count(*) FILTER (WHERE user_text ~* '\\m(short|brief|concise|tl;?dr|one line)\\M') short,
                  count(*) FILTER (WHERE user_text ~* '\\m(detail|detailed|explain|in depth|step by step)\\M') deep,
                  count(*) n
           FROM (SELECT user_text FROM conversation_log WHERE slack_user = %s ORDER BY at DESC LIMIT 50) x""",
        (user,)).fetchone()
    if p["n"] >= 5:
        if p["short"] >= 2 and p["short"] > p["deep"]:
            lines.append("They ask for short answers: lead with the answer, a few bullets at most.")
        elif p["deep"] >= 2 and p["deep"] > p["short"]:
            lines.append("They like detail: explain the why and the steps.")
        elif (p["avg_len"] or 0) < 90:
            lines.append("They write short, informal messages: reply plainly and briefly, skip formal openers.")
    return lines


def context_lines(conn, user, message, clients=(), session_id=None):
    """Lines for this turn's context (lessons + learning style); records which lessons were used."""
    lessons = relevant(conn, user, message, clients)
    lines = []
    if lessons:
        lines.append("What experience taught you (follow these):")
        lines += [f"- {'Avoid: ' if r['kind'] == 'avoid' else ''}{r['rule']}" for r in lessons]
        conn.execute("UPDATE lessons SET uses = uses + 1, last_used = now() WHERE id = ANY(%s)",
                     ([r["id"] for r in lessons],))
        conn.commit()
    _INJECTED[session_id or "-"] = [r["id"] for r in lessons]
    style = style_for(conn, user, situation(message))
    if style:
        lines.append("How to work with this person right now:")
        lines += [f"- {s}" for s in style]
    return lines


# ---------------------------------------------------------------- sleep (nightly consolidation)

def _settle(conn, rows):
    """Episodes being put to bed: lessons used in replies nobody complained about get a little stronger."""
    ok = [i for r in rows if (r["reward"] or 0) >= 0 for i in (r["lessons_used"] or [])]
    if ok:
        conn.execute("UPDATE lessons SET strength = strength + 0.1, last_seen = now() WHERE id = ANY(%s)", (ok,))
    conn.execute("UPDATE conversation_log SET consolidated_at = now(), digested_at = coalesce(digested_at, now()) "
                 "WHERE id = ANY(%s)", ([r["id"] for r in rows],))


def decay(conn):
    """Forgetting curve: unused lessons fade; very weak ones are dropped."""
    faded = conn.execute(
        """UPDATE lessons SET strength = strength * %s WHERE active
           AND greatest(last_seen, coalesce(last_used, last_seen)) < now() - make_interval(days => %s)""",
        (DECAY, DECAY_AFTER_DAYS)).rowcount
    pruned = conn.execute("UPDATE lessons SET active = false WHERE active AND strength < %s",
                          (PRUNE_BELOW,)).rowcount
    conn.commit()
    return faded, pruned


def sleep_lines(conn, limit=80, mark=True):
    """Nightly monitor script. Prints the episodes to replay (most surprising first) and the lessons so far;
    nothing to replay -> no lines -> no agent run. Always runs the forgetting curve first."""
    decay(conn)
    # quiet old episodes (no signal) are filed without a replay; the surprising ones wait for one
    stale = conn.execute("""SELECT id, reward, lessons_used FROM conversation_log WHERE consolidated_at IS NULL
                            AND at < now() - make_interval(days => %s) AND reward = 0""",
                         (DECAY_AFTER_DAYS,)).fetchall()
    if stale and mark:
        _settle(conn, stale)
        conn.commit()
    rows = conn.execute(
        """SELECT * FROM conversation_log WHERE consolidated_at IS NULL
           ORDER BY CASE WHEN reward < 0 THEN 0 WHEN reward > 0 THEN 1 ELSE 2 END, abs(reward) DESC, at
           LIMIT %s""", (limit,)).fetchall()
    if not rows:
        return []
    from . import slackui
    from .agency_memory import _clip
    names = {}

    def name(uid):
        if uid not in names:
            try:
                names[uid] = slackui.person_name(uid) or uid
            except Exception:  # noqa: BLE001
                names[uid] = uid
        return names[uid]

    out = ["EPISODES (most surprising first; 'next they said' is their reply to your answer):"]
    for r in rows:
        nxt = conn.execute("""SELECT user_text FROM conversation_log WHERE slack_user = %s AND at > %s
                              AND at < %s + make_interval(mins => %s) ORDER BY at LIMIT 1""",
                           (r["slack_user"], r["at"], r["at"], FOLLOWUP_WINDOW_MIN)).fetchone()
        tools = ", ".join(sorted({t[0] for t in (r["tools"] or [])}))[:160]
        sig = ",".join(r["signals"] or []) or "none"
        out.append(f"[ep {r['id']} · {r['at']:%d %b %H:%M}] {name(r['slack_user'])} ({r['slack_user']}) · "
                   f"{r['situation'] or situation(r['user_text'])} · signal: {sig} ({r['reward']:+.1f})"
                   + (f" · tools: {tools}" if tools else "") + (f" ({r['tool_errors']} failed)" if r["tool_errors"] else ""))
        out.append(f"   asked: {_clip(r['user_text'], 350)}")
        out.append(f"   you answered: {_clip(r['reply_text'], 280)}")
        if nxt:
            out.append(f"   next they said: {_clip(nxt['user_text'], 220)}")
    known = conn.execute("SELECT * FROM lessons WHERE active ORDER BY strength DESC LIMIT 30").fetchall()
    if known:
        out.append("LESSONS YOU ALREADY HAVE (id · scope · situation · strength):")
        out += [f"L{k['id']} · {k['scope']} · {k['situation']} · {k['strength']:.1f} · {k['kind']}: {k['rule']}"
                for k in known]
    if mark:
        _settle(conn, rows)
        conn.commit()
    return out


# ---------------------------------------------------------------- childhood (one-time bootstrap)

def bootstrap(conn):
    """Score the whole conversation history once: situation for every exchange, reward from the next
    message. Leaves them for sleep to replay (surprising ones first). Safe to re-run."""
    rows = conn.execute("""SELECT id, slack_user, user_text, at, signals FROM conversation_log
                           ORDER BY slack_user, at""").fetchall()
    tagged = scored = 0
    for i, r in enumerate(rows):
        conn.execute("UPDATE conversation_log SET situation = %s WHERE id = %s AND situation IS NULL",
                     (situation(r["user_text"]), r["id"]))
        tagged += 1
        if r["signals"] is not None:
            continue
        nxt = rows[i + 1] if i + 1 < len(rows) else None
        if nxt and nxt["slack_user"] == r["slack_user"] and \
                (nxt["at"] - r["at"]).total_seconds() <= FOLLOWUP_WINDOW_MIN * 60:
            reward, signals = score_followup(r["user_text"], nxt["user_text"])
            conn.execute("UPDATE conversation_log SET reward = reward + %s, signals = %s::text[] WHERE id = %s",
                         (reward, signals, r["id"]))
            scored += 1 if signals else 0
    conn.execute("UPDATE conversation_log SET consolidated_at = NULL WHERE consolidated_at IS NOT NULL "
                 "AND NOT EXISTS (SELECT 1 FROM lessons)")       # first childhood only: replay everything
    conn.commit()
    return {"episodes": tagged, "with_signal": scored}


# ---------------------------------------------------------------- tools

def _user():
    from . import connections
    return connections.current_user()


def _owner(user):
    from .guard import owners
    return user is None or user in owners()          # None = scheduled job (the nightly sleep)


def _ok(**d):
    return json.dumps({"success": True, **d}, ensure_ascii=False, default=str)


def _err(msg):
    return json.dumps({"success": False, "error": msg})


def _may_change(user, scope):
    return _owner(user) or scope == f"person:{user}" or scope.startswith("client:")


def tool_lesson_learn(params, **_):
    from .agency_memory import scope_for
    conn = ledger.connect()
    try:
        user = _user()
        scope = scope_for(params.get("about"), user)
        if scope == "agency" and not _owner(user):
            scope = f"person:{user}"                  # a teammate's habit is theirs unless an owner makes it agency-wide
        if not _may_change(user, scope):
            return _err("you can only teach lessons about yourself, the agency (owners) or a client")
        lid, how = learn(conn, params.get("rule"), scope, params.get("situation"), params.get("kind") or "do",
                         params.get("episodes"), by=user)
        return _ok(lesson=lid, result=how, scope=scope)
    except ValueError as e:
        return _err(str(e))
    finally:
        conn.close()


def tool_lesson_weaken(params, **_):
    conn = ledger.connect()
    try:
        row = conn.execute("SELECT * FROM lessons WHERE id = %s", (int(params["id"]),)).fetchone()
        if not row:
            return _err("no such lesson")
        if not _may_change(_user(), row["scope"]):
            return _err("only an owner can change that lesson")
        return _ok(changed=weaken(conn, row["id"], float(params.get("amount") or 1.0), bool(params.get("forget"))))
    finally:
        conn.close()


def tool_lessons_list(params, **_):
    from .agency_memory import scope_for
    conn = ledger.connect()
    try:
        user = _user()
        q, vals = ["active"], []
        if params.get("about"):
            q.append("scope = %s")
            vals.append(scope_for(params["about"], user))
        elif not _owner(user):
            q.append("(scope NOT LIKE 'person:%%' OR scope = %s)")
            vals.append(f"person:{user}")
        if params.get("situation"):
            q.append("situation IN (%s, 'any')")
            vals.append(_norm_situation(params["situation"]))
        rows = conn.execute(f"SELECT * FROM lessons WHERE {' AND '.join(q)} ORDER BY strength DESC LIMIT %s",
                            (*vals, int(params.get("limit") or 25))).fetchall()
        return _ok(lessons=[{"id": r["id"], "scope": r["scope"], "situation": r["situation"], "kind": r["kind"],
                             "rule": r["rule"], "strength": round(r["strength"], 2), "evidence": r["evidence"],
                             "used": r["uses"]} for r in rows])
    finally:
        conn.close()


_S = {"type": "string"}
TOOLS = [
    ("lesson_learn", tool_lesson_learn,
     "Save how to work better next time (a lesson from experience), or strengthen it if you already know it. "
     "One concrete rule: 'For Sai's research asks, give the answer first, no list of options'. about: agency "
     "(owners) / me / a teammate / @client. situation: tasks|research|content|numbers|automation|client|files|chat|any. "
     "kind: do|avoid|prefer. episodes: the episode ids it came from (nightly sleep).",
     {"rule": _S, "about": _S, "situation": _S, "kind": _S, "episodes": {"type": "array", "items": {"type": "integer"}}},
     ["rule"]),
    ("lesson_weaken", tool_lesson_weaken,
     "A lesson turned out wrong or outdated: weaken it (amount, default 1) or forget=true to drop it.",
     {"id": {"type": "integer"}, "amount": {"type": "number"}, "forget": {"type": "boolean"}}, ["id"]),
    ("lessons_list", tool_lessons_list,
     "What you've learned from experience (strongest first): 'what have you learned about how I like things?'. "
     "Filter by about (me / a teammate / @client / agency) and situation.",
     {"about": _S, "situation": _S, "limit": {"type": "integer"}}, []),
]


def register(ctx, toolset="memory_store"):
    for name, fn, description, props, required in TOOLS:
        ctx.register_tool(name=name, toolset=toolset, handler=fn,
                          schema={"name": name, "description": description,
                                  "parameters": {"type": "object", "properties": props, "required": required}})
    if hasattr(ctx, "register_hook"):
        ctx.register_hook("post_tool_call", post_tool_call)
