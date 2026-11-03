"""Team tasks: who owns what, by when. Created from chat ("add a task for Shravya: …"), from
meetings (Read AI action items), and later from standups. Codes are TASK-<n>.

Owners are Slack members, matched by email (meeting participants) or name. Each owner is DMed
once per batch of new tasks, and once a morning about what's due or overdue (model-free job).
"""

import json
import re
from datetime import date, datetime, timedelta

from . import ledger, notify

STATUSES = ("open", "doing", "blocked", "done", "dropped")
OPEN = ("open", "doing", "blocked")


def code(row_or_id):
    return f"TASK-{row_or_id['id'] if isinstance(row_or_id, dict) else row_or_id}"


def parse_code(c):
    m = re.search(r"(\d+)", str(c or ""))
    if not m:
        raise ValueError(f"not a task code: {c!r} (looks like TASK-12)")
    return int(m.group(1))


def parse_due(v, today=None):
    """'2026-11-05', 'today', 'tomorrow', 'friday', 'in 3 days' -> date (None if empty)."""
    if not v:
        return None
    today = today or date.today()
    s = str(v).strip().lower()
    if s == "today":
        return today
    if s == "tomorrow":
        return today + timedelta(days=1)
    m = re.fullmatch(r"in (\d+) days?", s)
    if m:
        return today + timedelta(days=int(m.group(1)))
    days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    if s in days:
        return today + timedelta(days=(days.index(s) - today.weekday()) % 7 or 7)
    return datetime.fromisoformat(s[:10]).date()


# ---------------------------------------------------------------- owners

def resolve_owner(who, people=None):
    """(slack_id, display_name, problem). Matches an email, a member id, or a unique name."""
    from . import slackui
    if not who:
        return None, None, None
    w = str(who).strip()
    everyone = people if people is not None else slackui.people()
    if "@" in w and "." in w.split("@")[-1]:
        for p in everyone:
            if (p.get("email") or "").lower() == w.lower():
                return p["id"], p["real_name"] or p["display_name"] or p["name"], None
        return None, w, f"no Slack member with email {w}"
    if re.fullmatch(r"U[A-Z0-9]{6,}", w.lstrip("<@").rstrip(">")):
        uid = w.lstrip("<@").rstrip(">")
        p = next((p for p in everyone if p["id"] == uid), None)
        return uid, (p and (p["real_name"] or p["name"])) or uid, None
    q = w.lstrip("@").lower()
    exact = [p for p in everyone if q in ((p.get("name") or "").lower(), p["real_name"].lower(),
                                          p["display_name"].lower(), p["real_name"].lower().split(" ")[0])]
    if len(exact) == 1:
        p = exact[0]
        return p["id"], p["real_name"] or p["display_name"] or p["name"], None
    partial = exact or [p for p in everyone if q in p["real_name"].lower() or q in (p.get("name") or "").lower()]
    if len(partial) == 1:
        p = partial[0]
        return p["id"], p["real_name"] or p["display_name"] or p["name"], None
    if partial:
        return None, w, "several people match: " + ", ".join(p["real_name"] or p["name"] for p in partial[:5])
    return None, w, f"nobody in Slack matches {w!r}"


# ---------------------------------------------------------------- writes

def add(conn, items, created_by=None, people=None, notify_owners=True, source="manual", source_ref=None):
    """Add tasks: items = [{title, owner, due, client, notes}]. DMs each owner once with their new tasks."""
    added, problems, per_owner = [], [], {}
    for it in items:
        title = (it.get("title") or "").strip()
        if not title:
            continue
        oid, oname, problem = resolve_owner(it.get("owner"), people) if it.get("owner") else (None, None, None)
        if problem:
            problems.append({"title": title, "owner": it.get("owner"), "problem": problem})
        try:
            due = parse_due(it.get("due"))
        except ValueError:
            due, _ = None, problems.append({"title": title, "problem": f"couldn't read due date {it.get('due')!r}"})
        row = conn.execute(
            """INSERT INTO team_tasks(title, owner_slack, owner_name, client, due, source, source_ref, notes, created_by)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            (title[:500], oid, oname, (it.get("client") or None) and it["client"].lstrip("@").lower(), due,
             it.get("source") or source, it.get("source_ref") or source_ref, it.get("notes"), created_by)).fetchone()
        added.append(row)
        if oid and oid != created_by:
            per_owner.setdefault(oid, []).append(row)
    conn.commit()
    to_list(conn, added)
    if notify_owners:
        for oid, rows in per_owner.items():
            notify.dm([oid], "📝 New task" + ("s" if len(rows) > 1 else "") + " for you:\n"
                      + "\n".join(line(r) for r in rows)
                      + "\nReply `done TASK-n` here when finished, or tell me if a date is wrong.")
    return [view(r) for r in added], problems


def update(conn, task_code, by=None, status=None, due=None, owner=None, title=None, note=None, people=None):
    tid = parse_code(task_code)
    row = conn.execute("SELECT * FROM team_tasks WHERE id = %s", (tid,)).fetchone()
    if not row:
        raise ValueError(f"no task {code(tid)}")
    sets, vals = [], []
    if status:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {', '.join(STATUSES)}")
        sets.append("status = %s")
        vals.append(status)
        sets.append("done_at = " + ("now()" if status == "done" else "NULL"))
    if due is not None:
        sets.append("due = %s")
        vals.append(parse_due(due) if due else None)
    if owner:
        oid, oname, problem = resolve_owner(owner, people)
        if problem:
            raise ValueError(problem)
        sets += ["owner_slack = %s", "owner_name = %s"]
        vals += [oid, oname]
    if title:
        sets.append("title = %s")
        vals.append(title[:500])
    if note:
        sets.append("notes = coalesce(notes || E'\\n', '') || %s")
        vals.append(f"{date.today().isoformat()} {note}")
    if not sets:
        return view(row)
    sets.append("updated_at = now()")
    row = conn.execute(f"UPDATE team_tasks SET {', '.join(sets)} WHERE id = %s RETURNING *", (*vals, tid)).fetchone()
    conn.commit()
    if status in ("done", "open"):
        try:
            from . import lists
            lists.set_done(conn, row, status == "done")
        except Exception:  # noqa: BLE001 — the List is a mirror; the task itself is updated
            pass
    if status == "blocked":
        notify.dm([o for o in notify.owners() if o != by],
                  f"🚧 {row['owner_name'] or 'Someone'} is blocked on {code(row)} {row['title']}"
                  + (f": {note}" if note else "") + ". Can you help unblock it?")
    if owner and row["owner_slack"] and row["owner_slack"] != by:
        notify.dm([row["owner_slack"]], f"📝 {code(row)} is now yours: {row['title']}"
                  + (f" (due {row['due']:%a %d %b})" if row["due"] else ""))
    return view(row)


def to_list(conn, rows, day=None):
    """Mirror new tasks into today's Slack List (best effort: never blocks adding a task)."""
    from .settings import env
    if (env("PX_DAILY_LIST") or "on").lower() in ("off", "0", "false") or not rows:
        return
    try:
        from . import lists, standup
        team = [p["id"] for p in standup.members(standup.config(conn))]
        for r in rows:
            if r["status"] in OPEN:
                lists.add_task(conn, r, day=day or _local_today(conn), member_ids=team)
    except Exception:  # noqa: BLE001
        pass


def _local_today(conn):
    from zoneinfo import ZoneInfo
    from . import standup
    return datetime.now(ZoneInfo(standup.config(conn)["timezone"])).date()


# ---------------------------------------------------------------- reads

def view(r):
    return {"code": code(r), "title": r["title"], "owner": r["owner_name"], "owner_id": r["owner_slack"],
            "client": r["client"], "due": r["due"].isoformat() if r["due"] else None, "status": r["status"],
            "source": r["source"], "notes": r["notes"]}


def line(r, today=None):
    today = today or date.today()
    due = ""
    if r["due"]:
        late = (today - r["due"]).days
        due = f" · due {r['due']:%a %d %b}" + (f" (*{late}d overdue*)" if late > 0 else " (today)" if late == 0 else "")
    client = f" · @{r['client']}" if r.get("client") else ""
    status = "" if r["status"] == "open" else f" · _{r['status']}_"
    return f"• *{code(r)}* {r['title']}{client}{due}{status}"


def listing(conn, owner_id=None, status="open", client=None, overdue=False, limit=50):
    q, vals = ["1=1"], []
    if owner_id:
        q.append("owner_slack = %s")
        vals.append(owner_id)
    if status == "open":
        q.append("status IN ('open','doing','blocked')")
    elif status and status != "all":
        q.append("status = %s")
        vals.append(status)
    if client:
        q.append("client = %s")
        vals.append(client.lstrip("@").lower())
    if overdue:
        q.append("due < current_date")
    rows = conn.execute(f"SELECT * FROM team_tasks WHERE {' AND '.join(q)} "
                        "ORDER BY due NULLS LAST, id LIMIT %s", (*vals, limit)).fetchall()
    return rows


def reminders(conn, today=None):
    """{owner_id: [rows]} of open tasks due today or overdue, not yet reminded today; marks them."""
    today = today or date.today()
    rows = conn.execute("""SELECT * FROM team_tasks WHERE status IN ('open','doing','blocked')
                           AND owner_slack IS NOT NULL AND due <= %s
                           AND (reminded_on IS NULL OR reminded_on < %s) ORDER BY due, id""",
                        (today, today)).fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["owner_slack"], []).append(r)
    if rows:
        conn.execute("UPDATE team_tasks SET reminded_on = %s WHERE id = ANY(%s)", (today, [r["id"] for r in rows]))
        conn.commit()
    return out


def send_reminders(conn, today=None):
    sent = 0
    for oid, rows in reminders(conn, today).items():
        sent += notify.dm([oid], "☀️ Due today or overdue:\n" + "\n".join(line(r, today) for r in rows)
                          + "\nReply `done TASK-n`, or tell me a new date.")
    return sent


# ---------------------------------------------------------------- tools

def _ok(**d):
    return json.dumps({"success": True, **d}, ensure_ascii=False, default=str)


def _err(e):
    return json.dumps({"success": False, "error": str(e)})


def _asker():
    from . import connections
    return connections.current_user()


def task_add(params, **_):
    conn = ledger.connect()
    try:
        items = params.get("items") or [params]
        added, problems = add(conn, items, created_by=_asker(), source=params.get("source") or "manual",
                              source_ref=params.get("source_ref"))
        return _ok(added=added, problems=problems)
    except Exception as e:  # noqa: BLE001
        return _err(e)
    finally:
        conn.close()


def task_list(params, **_):
    conn = ledger.connect()
    try:
        who = (params.get("owner") or "me").strip()
        oid = None
        if who.lower() == "me":
            oid = _asker()
        elif who.lower() not in ("all", "everyone", "team"):
            oid, _, problem = resolve_owner(who)
            if problem:
                return _err(problem)
        rows = listing(conn, oid, params.get("status") or "open", params.get("client"), bool(params.get("overdue")),
                       min(params.get("limit") or 50, 200))
        return _ok(count=len(rows), tasks=[view(r) for r in rows], text="\n".join(line(r) for r in rows) or "No tasks.")
    except Exception as e:  # noqa: BLE001
        return _err(e)
    finally:
        conn.close()


def task_update(params, **_):
    conn = ledger.connect()
    try:
        return _ok(task=update(conn, params["code"], by=_asker(), status=params.get("status"), due=params.get("due"),
                               owner=params.get("owner"), title=params.get("title"), note=params.get("note")))
    except Exception as e:  # noqa: BLE001
        return _err(e)
    finally:
        conn.close()


_S = {"type": "string"}
_ITEM = {"type": "object", "properties": {"title": _S, "owner": _S, "due": _S, "client": _S, "notes": _S},
         "required": ["title"]}

TOOLS = [
    ("task_add", task_add,
     "Add team tasks (one, or several with items). owner = a teammate's name, @handle, Slack id or email; "
     "due = YYYY-MM-DD, 'today', 'tomorrow', 'friday', 'in 3 days'. Owners get a DM. Use for 'add a task for …', "
     "meeting action items (source 'meeting', source_ref = meeting id) and standup answers (source 'standup').",
     {"title": _S, "owner": _S, "due": _S, "client": _S, "notes": _S,
      "items": {"type": "array", "items": _ITEM}, "source": _S, "source_ref": _S}, []),
    ("task_list", task_list,
     "List team tasks. owner: 'me' (default), a name, or 'all'. status: open (default: open/doing/blocked), "
     "done, all… Optional client, overdue=true.",
     {"owner": _S, "status": _S, "client": _S, "overdue": {"type": "boolean"}, "limit": {"type": "integer"}}, []),
    ("task_update", task_update,
     "Update a task by code (TASK-12): status open|doing|blocked|done|dropped, new due date, new owner (DMed), "
     "title, or a note. Use for 'done TASK-12', 'move TASK-3 to Friday', 'give TASK-5 to Shravya'.",
     {"code": _S, "status": {"type": "string", "enum": list(STATUSES)}, "due": _S, "owner": _S, "title": _S,
      "note": _S}, ["code"]),
]


def register(ctx, toolset="tasks"):
    for name, fn, description, props, required in TOOLS:
        ctx.register_tool(name=name, toolset=toolset, handler=fn,
                          schema={"name": name, "description": description,
                                  "parameters": {"type": "object", "properties": props, "required": required}})
