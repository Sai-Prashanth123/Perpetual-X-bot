"""Meeting follow-up: finished meetings from the agency's Read AI become tasks and a short recap.

    jobs.py meetings-pending   (cron monitor, model-free) pulls new finished meetings into the
                               `meetings` table and prints the ones still waiting for follow-up;
                               unchanged output = no agent run, so quiet hours cost nothing.
    Agency Bot (skill meeting-followup) reads them with meetings_pending, adds tasks with task_add,
    and closes each with meeting_done.

Uses the agency-wide Read AI connection (an owner connected it on purpose for scheduled work).
"""

import json
import time
from datetime import datetime

from . import connections, ledger, readai

LOOKBACK_DAYS = 2
WAIT_FOR_NOTES_H = 3        # Read AI writes the summary after the call; give it this long


def _ts(iso):
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() if iso else None
    except ValueError:
        return None


def sync(conn, tok=None, fetch_list=None, fetch_one=None, now=None):
    """Store finished meetings we haven't seen. Returns the ids stored this run."""
    now = now or time.time()
    if tok is None:
        tok = connections.token(conn, connections.AGENCY, "readai")
    fetch_list = fetch_list or (lambda: readai.listing(tok, LOOKBACK_DAYS))
    fetch_one = fetch_one or (lambda mid: readai.get(tok, mid))
    seen = {r["id"] for r in conn.execute("SELECT id FROM meetings WHERE started_at > now() - interval '7 days' "
                                          "OR fetched_at > now() - interval '7 days'")}
    stored = []
    for m in fetch_list():
        mid = f"readai:{m['id']}"
        if not m.get("id") or mid in seen:
            continue
        started = _ts(m.get("start"))
        ended = _ts(m.get("end"))
        if ended is None and started and now - started < 4 * 3600:
            continue                                   # still running (or no end yet)
        full = fetch_one(m["id"])
        if not full.get("summary") and not full.get("action_items") and (now - (ended or started or now)) < WAIT_FOR_NOTES_H * 3600:
            continue                                   # notes not written yet; try next run
        conn.execute(
            """INSERT INTO meetings(id, source, title, started_at, ended_at, participants, summary, action_items,
                                    key_questions, url)
               VALUES (%s,'readai',%s,to_timestamp(%s),to_timestamp(%s),%s,%s,%s,%s,%s)
               ON CONFLICT (id) DO NOTHING""",
            (mid, full.get("title") or m.get("title"), started, ended, json.dumps(full.get("participants") or []),
             full.get("summary"), json.dumps(full.get("action_items") or []),
             json.dumps(full.get("key_questions") or []), full.get("url")))
        stored.append(mid)
    conn.commit()
    return stored


def pending(conn, limit=5):
    return conn.execute("""SELECT * FROM meetings WHERE processed_at IS NULL
                           AND fetched_at > now() - interval '3 days' ORDER BY started_at LIMIT %s""",
                        (limit,)).fetchall()


def view(r):
    return {"id": r["id"], "title": r["title"],
            "start": r["started_at"].isoformat() if r["started_at"] else None,
            "participants": r["participants"], "summary": r["summary"], "action_items": r["action_items"],
            "key_questions": r["key_questions"], "url": r["url"]}


def done(conn, mid, tasks_created=0, client=None, note=None):
    n = conn.execute("""UPDATE meetings SET processed_at = now(), tasks_created = %s, client = %s, note = %s
                        WHERE id = %s""", (tasks_created, client, note, mid)).rowcount
    conn.commit()
    return bool(n)


# ---------------------------------------------------------------- tools (Agency Bot)

def _ok(**d):
    return json.dumps({"success": True, **d}, ensure_ascii=False, default=str)


def meetings_pending(params, **_):
    conn = ledger.connect()
    try:
        rows = pending(conn, min(params.get("limit") or 5, 10))
        return _ok(count=len(rows), meetings=[view(r) for r in rows])
    finally:
        conn.close()


def meeting_done(params, **_):
    conn = ledger.connect()
    try:
        return _ok(closed=done(conn, params["id"], int(params.get("tasks_created") or 0),
                               (params.get("client") or "").lstrip("@").lower() or None, params.get("note")))
    finally:
        conn.close()


def meetings_recent(params, **_):
    conn = ledger.connect()
    try:
        rows = conn.execute("SELECT * FROM meetings WHERE started_at > now() - make_interval(days => %s) "
                            "ORDER BY started_at DESC LIMIT 20", (params.get("days") or 7,)).fetchall()
        return _ok(meetings=[{**view(r), "followed_up": bool(r["processed_at"]), "tasks_created": r["tasks_created"],
                              "client": r["client"]} for r in rows])
    finally:
        conn.close()


_S = {"type": "string"}
TOOLS = [
    ("meetings_pending", meetings_pending,
     "Finished meetings (from the agency's Read AI) still waiting for follow-up: summary, participants with emails, "
     "action items. Follow skill meeting-followup.", {"limit": {"type": "integer"}}, []),
    ("meeting_done", meeting_done,
     "Close a meeting's follow-up once its tasks are added (tasks_created, client handle if it was a client call).",
     {"id": _S, "tasks_created": {"type": "integer"}, "client": _S, "note": _S}, ["id"]),
    ("meetings_recent", meetings_recent,
     "Team meetings from the last N days (default 7) with summaries, action items and whether they were followed up.",
     {"days": {"type": "integer"}}, []),
]


def register(ctx, toolset="meetings"):
    for name, fn, description, props, required in TOOLS:
        ctx.register_tool(name=name, toolset=toolset, handler=fn,
                          schema={"name": name, "description": description,
                                  "parameters": {"type": "object", "properties": props, "required": required}})
