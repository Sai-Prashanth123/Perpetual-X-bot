"""The team's daily Slack List ("4-11-2026"): one to-do list per day with Name, Assignee, Due Date and a
done checkbox, shared with the team. Tasks added anywhere (chat, standup, meetings) appear in it;
ticking a box in Slack (or adding a row by hand) flows back into team_tasks every few minutes.

Slack Lists API (paid plans): slackLists.create (todo_mode), slackLists.items.create / update / list,
slackLists.access.set. Bot scopes lists:read + lists:write.
"""

import json
from datetime import date

from . import slackui


def list_name(day):
    return f"{day.day}-{day.month}-{day.year}"          # same style as the team's own lists: 3-11-2026


def _rt(text):
    return [{"type": "rich_text", "elements": [{"type": "rich_text_section",
                                                 "elements": [{"type": "text", "text": text[:300]}]}]}]


def _cols(schema):
    """{'name'|'done'|'assignee'|'due': column_id} from a todo-mode List schema."""
    out = {}
    for c in schema or []:
        t, k = c.get("type"), c.get("key")
        if c.get("is_primary_column"):
            out["name"] = c["id"]
        elif t == "todo_completed" or k == "todo_completed":
            out["done"] = c["id"]
        elif t == "todo_assignee" or k == "todo_assignee":
            out["assignee"] = c["id"]
        elif t == "todo_due_date" or k == "todo_due_date":
            out["due"] = c["id"]
    return out


def get_daily(conn, day):
    return conn.execute("SELECT * FROM daily_lists WHERE day = %s", (day,)).fetchone()


def share(conn, lst, member_ids, slack=None):
    """Give the team write access (each person + the team channel). Records shared_at / share_error."""
    from .settings import env
    slack = slack or slackui.slack
    errors, ok = [], False
    ids = [u for u in dict.fromkeys(member_ids or []) if u]
    # one person per call: Slack rejects several user_ids at once (invalid_arguments), and one bad id
    # must not block the rest
    for u in ids:
        try:
            slack("slackLists.access.set", list_id=lst["list_id"], access_level="write", user_ids=[u])
            ok = True
        except Exception as e1:  # noqa: BLE001
            errors.append(f"{u}: {e1}")
    ch = env("HQ_SLACK_CHANNEL")
    if ch:
        try:
            slack("slackLists.access.set", list_id=lst["list_id"], access_level="write", channel_ids=[ch])
            ok = True
        except Exception as e:  # noqa: BLE001
            errors.append(f"{ch}: {e}")
    if errors:
        print(f"[agency-bot] daily List sharing: {'; '.join(errors)}", flush=True)
    conn.execute("UPDATE daily_lists SET shared_at = CASE WHEN %s THEN now() END, share_error = %s WHERE day = %s",
                 (ok and not errors, "; ".join(errors)[:1000] or None, lst["day"]))
    conn.commit()
    return {"ok": ok, "errors": errors}


def ensure_daily(conn, day, member_ids, slack=None):
    """Today's List, created once and shared (write access) with the team. Sharing is retried on every
    call until it fully succeeds."""
    slack = slack or slackui.slack
    row = get_daily(conn, day)
    if not row:
        r = slack("slackLists.create", name=list_name(day), todo_mode=True,
                  schema=[{"key": "name", "name": "Name", "type": "text", "is_primary_column": True}])
        list_id = r["list_id"]
        cols = _cols((r.get("list_metadata") or {}).get("schema"))
        url = None
        try:
            url = (slack("files.info", file=list_id).get("file") or {}).get("permalink")
        except Exception:  # noqa: BLE001
            pass
        conn.execute("INSERT INTO daily_lists(day, list_id, url, columns) VALUES (%s,%s,%s,%s) "
                     "ON CONFLICT (day) DO NOTHING", (day, list_id, url, json.dumps(cols)))
        conn.commit()
        row = get_daily(conn, day)
    if not row.get("shared_at"):
        try:
            share(conn, row, member_ids, slack)
        except Exception:  # noqa: BLE001 — sharing failure must not lose the list
            pass
        row = get_daily(conn, day)
    return row


def _rt_links(text, links):
    """Title plus a clickable link per attached doc (Google Doc, Canva, ...)."""
    rt = _rt(text)
    if links:
        from . import sharing
        els = rt[0]["elements"][0]["elements"]
        for u in links[:5]:
            els += [{"type": "text", "text": " · "}, {"type": "link", "url": u, "text": sharing.label(u)}]
    return rt


def _item_fields(cols, task, title):
    fields = [{"column_id": cols["name"], "rich_text": _rt_links(title, task.get("links"))}] if cols.get("name") else []
    if task.get("owner_slack") and cols.get("assignee"):
        fields.append({"column_id": cols["assignee"], "user": [task["owner_slack"]]})
    if task.get("due") and cols.get("due"):
        fields.append({"column_id": cols["due"], "date": [task["due"].isoformat()]})
    return fields


def _put(conn, lst, task, title, slack):
    r = slack("slackLists.items.create", list_id=lst["list_id"], initial_fields=_item_fields(lst["columns"] or {}, task, title))
    item_id = (r.get("item") or {}).get("id")
    conn.execute("UPDATE team_tasks SET list_id = %s, list_item_id = %s WHERE id = %s",
                 (lst["list_id"], item_id, task["id"]))
    conn.commit()
    return item_id


def add_task(conn, task, day=None, member_ids=None, slack=None):
    """Put one team_tasks row on the day's List (idempotent). Returns the List item id or None."""
    slack = slack or slackui.slack
    day = day or date.today()
    lst = ensure_daily(conn, day, member_ids, slack)
    if task.get("list_item_id") and task.get("list_id") == lst["list_id"]:
        return task["list_item_id"]
    return _put(conn, lst, task, _carried_title(conn, task, day), slack)


def _carried_title(conn, task, day):
    """The task's title, plus '(from 5-11)' when it's pending from an earlier day."""
    since = None
    if task.get("list_id"):
        old = conn.execute("SELECT day FROM daily_lists WHERE list_id = %s", (task["list_id"],)).fetchone()
        since = old and old["day"]
    if not since and task.get("created_at"):
        since = task["created_at"].date()
    if since and since < day:
        return f"{task['title']} (from {since.day}-{since.month})"
    return task["title"]


def fill(conn, day, member_ids=None, slack=None):
    """Bring every open task (everyone's, any due date, including ones pending from earlier days) onto
    the day's List. Already-there tasks are skipped. Returns how many were added."""
    from . import tasks
    slack = slack or slackui.slack
    lst = ensure_daily(conn, day, member_ids, slack)
    rows = conn.execute("""SELECT * FROM team_tasks WHERE status = ANY(%s) AND list_id IS DISTINCT FROM %s
                           ORDER BY owner_name NULLS LAST, due NULLS LAST, id""",
                        (list(tasks.OPEN), lst["list_id"])).fetchall()
    n = 0
    for t in rows:
        try:
            _put(conn, lst, t, _carried_title(conn, t, day), slack)
            n += 1
        except Exception as e:  # noqa: BLE001 — one bad row must not stop the rest
            print(f"[agency-bot] daily List: couldn't add {tasks.code(t)}: {e}", flush=True)
    return n


def update_item(conn, task, slack=None):
    """Refresh a task's row on its List (title + links, owner, due). False when it isn't on a List."""
    slack = slack or slackui.slack
    if not task.get("list_item_id"):
        return False
    lst = conn.execute("SELECT * FROM daily_lists WHERE list_id = %s", (task["list_id"],)).fetchone()
    if not lst:
        return False
    fields = _item_fields(lst["columns"] or {}, task, _carried_title(conn, task, lst["day"]))
    if not fields:
        return False
    slack("slackLists.items.update", list_id=task["list_id"],
          cells=[{"row_id": task["list_item_id"], **f} for f in fields])
    return True


def set_done(conn, task, done=True, slack=None):
    slack = slack or slackui.slack
    if not task.get("list_item_id"):
        return False
    lst = conn.execute("SELECT * FROM daily_lists WHERE list_id = %s", (task["list_id"],)).fetchone()
    col = ((lst or {}).get("columns") or {}).get("done")
    if not col:
        return False
    slack("slackLists.items.update", list_id=task["list_id"],
          cells=[{"row_id": task["list_item_id"], "column_id": col, "checkbox": bool(done)}])
    return True


def _field(item, col_id):
    return next((f for f in item.get("fields") or [] if f.get("column_id") == col_id), None)


def sync(conn, day, slack=None, created_by=None):
    """Pull the List back: ticked boxes → task done; rows people added by hand → new tasks.
    Returns {'done': [...codes], 'added': [...codes]}."""
    from . import tasks
    slack = slack or slackui.slack
    lst = get_daily(conn, day)
    if not lst:
        return {"done": [], "added": []}
    cols = lst["columns"] or {}
    items, cursor = [], None
    for _ in range(10):
        r = slack("slackLists.items.list", list_id=lst["list_id"], limit=100, **({"cursor": cursor} if cursor else {}))
        items += r.get("items") or []
        cursor = (r.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break
    known = {t["list_item_id"]: t for t in conn.execute(
        "SELECT * FROM team_tasks WHERE list_id = %s", (lst["list_id"],))}
    done, added = [], []
    for it in items:
        name = _field(it, cols.get("name"))
        title = ((name or {}).get("text") or "").strip()
        ticked = bool((_field(it, cols.get("done")) or {}).get("checkbox"))
        task = known.get(it["id"])
        if task:
            if ticked and task["status"] != "done":
                conn.execute("UPDATE team_tasks SET status = 'done', done_at = now(), updated_at = now() WHERE id = %s",
                             (task["id"],))
                done.append(tasks.code(task))
            elif not ticked and task["status"] == "done":
                conn.execute("UPDATE team_tasks SET status = 'open', done_at = NULL, updated_at = now() WHERE id = %s",
                             (task["id"],))
            continue
        if not title:
            continue
        who = ((_field(it, cols.get("assignee")) or {}).get("user") or [None])[0]
        due = ((_field(it, cols.get("due")) or {}).get("date") or [None])[0]
        owner_name = slackui.person_name(who) if who else None
        row = conn.execute(
            """INSERT INTO team_tasks(title, owner_slack, owner_name, due, status, source, list_id, list_item_id,
                                      created_by, done_at)
               VALUES (%s,%s,%s,%s,%s,'list',%s,%s,%s,CASE WHEN %s THEN now() END) RETURNING *""",
            (title[:500], who, owner_name, due, "done" if ticked else "open", lst["list_id"], it["id"],
             it.get("created_by") or created_by, ticked)).fetchone()
        added.append(tasks.code(row))
    conn.commit()
    return {"done": done, "added": added}
