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


def ensure_daily(conn, day, member_ids, slack=None):
    """Today's List, created once and shared (write access) with the team."""
    slack = slack or slackui.slack
    row = get_daily(conn, day)
    if row:
        return row
    r = slack("slackLists.create", name=list_name(day), todo_mode=True,
              schema=[{"key": "name", "name": "Name", "type": "text", "is_primary_column": True}])
    list_id = r["list_id"]
    cols = _cols((r.get("list_metadata") or {}).get("schema"))
    if member_ids:
        try:
            slack("slackLists.access.set", list_id=list_id, access_level="write", user_ids=list(member_ids))
        except Exception:  # noqa: BLE001 — sharing failure must not lose the list
            pass
    url = None
    try:
        url = (slack("files.info", file=list_id).get("file") or {}).get("permalink")
    except Exception:  # noqa: BLE001
        pass
    conn.execute("INSERT INTO daily_lists(day, list_id, url, columns) VALUES (%s,%s,%s,%s) ON CONFLICT (day) DO NOTHING",
                 (day, list_id, url, json.dumps(cols)))
    conn.commit()
    return get_daily(conn, day)


def add_task(conn, task, day=None, member_ids=None, slack=None):
    """Add one team_tasks row to the day's List (idempotent). Returns the List item id or None."""
    slack = slack or slackui.slack
    if task.get("list_item_id"):
        return task["list_item_id"]
    day = day or date.today()
    lst = ensure_daily(conn, day, member_ids, slack)
    cols = lst["columns"] or {}
    fields = [{"column_id": cols["name"], "rich_text": _rt(task["title"])}] if cols.get("name") else []
    if task.get("owner_slack") and cols.get("assignee"):
        fields.append({"column_id": cols["assignee"], "user": [task["owner_slack"]]})
    if task.get("due") and cols.get("due"):
        fields.append({"column_id": cols["due"], "date": [task["due"].isoformat()]})
    r = slack("slackLists.items.create", list_id=lst["list_id"], initial_fields=fields)
    item_id = (r.get("item") or {}).get("id")
    conn.execute("UPDATE team_tasks SET list_id = %s, list_item_id = %s WHERE id = %s",
                 (lst["list_id"], item_id, task["id"]))
    conn.commit()
    return item_id


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
