"""Notion "Agency OS": the agency's source of truth, kept in sync with Agency Bot.

One top-level page "Agency OS" (created once with the agency's Notion connection) holding databases:

    Tasks      ⇄ team_tasks   (two-way: Status / Due / Owner / Title edits in Notion flow back; rows
                               added in Notion become tasks)
    Meetings   ← meetings     (summary, action items, follow-up state)
    Clients    ← clients      (handle, name, niche, channel, active)
    Goals      ← client_goals (when that table exists)

`sync(conn)` runs every 10 minutes (model-free job `notion-sync`). A human edit in Notion wins.
"""

import json
from datetime import datetime, timezone

from . import connections, ledger

NOTION = "https://api.notion.com/v1"
VERSION = "2022-06-28"
MAX_WRITES = 60                  # per run: Notion allows ~3 requests/second; stay gentle
STATUSES = ["open", "doing", "blocked", "done", "dropped"]

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS notion_os (
    id           INT PRIMARY KEY DEFAULT 1,
    page_id      TEXT,
    url          TEXT,
    dbs          JSONB,                      -- {"tasks": id, "meetings": id, "clients": id, "goals": id}
    last_pull    TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE team_tasks ADD COLUMN IF NOT EXISTS notion_page_id TEXT;
ALTER TABLE team_tasks ADD COLUMN IF NOT EXISTS notion_synced_at TIMESTAMPTZ;
ALTER TABLE meetings ADD COLUMN IF NOT EXISTS notion_page_id TEXT;
ALTER TABLE meetings ADD COLUMN IF NOT EXISTS notion_synced_at TIMESTAMPTZ;
ALTER TABLE clients ADD COLUMN IF NOT EXISTS notion_page_id TEXT;
ALTER TABLE clients ADD COLUMN IF NOT EXISTS notion_synced_at TIMESTAMPTZ;
"""


# ---------------------------------------------------------------- tiny Notion client

def _api(tok, method, path, body=None, call=None):
    if call:
        return call(method, path, body)
    from . import workspace
    return workspace._call(method, f"{NOTION}{path}", tok, body, headers={"Notion-Version": VERSION})


def _t(s, n=1900):
    s = (s or "")[:n]
    return [{"type": "text", "text": {"content": s}}] if s else []


def _title(s):
    return {"title": _t(s, 200) or _t("(untitled)")}


def _sel(v):
    return {"select": {"name": v}} if v else {"select": None}


def _date(d):
    return {"date": {"start": d.isoformat() if hasattr(d, "isoformat") else str(d)}} if d else {"date": None}


DATABASES = {
    "tasks": ("Tasks", {"Name": {"title": {}}, "Code": {"rich_text": {}},
                        "Status": {"select": {"options": [{"name": s} for s in STATUSES]}},
                        "Owner": {"rich_text": {}}, "Due": {"date": {}}, "Client": {"rich_text": {}},
                        "Source": {"select": {"options": [{"name": s} for s in
                                                          ("manual", "standup", "meeting", "list", "notion")]}},
                        "Notes": {"rich_text": {}}}),
    "meetings": ("Meetings", {"Name": {"title": {}}, "Date": {"date": {}}, "Client": {"rich_text": {}},
                              "Summary": {"rich_text": {}}, "Action items": {"rich_text": {}},
                              "Followed up": {"checkbox": {}}, "Tasks created": {"number": {}},
                              "Link": {"url": {}}}),
    "clients": ("Clients", {"Name": {"title": {}}, "Handle": {"rich_text": {}}, "Company": {"rich_text": {}},
                            "Niche": {"rich_text": {}}, "Active": {"checkbox": {}}, "X profile": {"url": {}}}),
    "goals": ("Goals", {"Name": {"title": {}}, "Client": {"rich_text": {}}, "Metric": {"rich_text": {}},
                        "Target": {"number": {}}, "Period": {"rich_text": {}}, "End": {"date": {}},
                        "Active": {"checkbox": {}}}),
}


# ---------------------------------------------------------------- provisioning

def state(conn):
    return conn.execute("SELECT * FROM notion_os WHERE id = 1").fetchone()


def ensure(conn, tok, call=None):
    """Create the Agency OS page and its databases once (and any database added later)."""
    st = state(conn)
    if not st or not st["page_id"]:
        page = _api(tok, "POST", "/pages", {
            "parent": {"type": "workspace", "workspace": True},
            "icon": {"type": "emoji", "emoji": "🧭"},
            "properties": {"title": {"title": _t("Agency OS")}},
            "children": [{"object": "block", "type": "paragraph", "paragraph": {"rich_text": _t(
                "The agency's source of truth, kept in sync with Agency Bot in Slack every 10 minutes. "
                "Edit tasks here (status, due date, owner) or in Slack — both stay the same.")}}]}, call)
        conn.execute("""INSERT INTO notion_os(id, page_id, url, dbs) VALUES (1,%s,%s,'{}')
                        ON CONFLICT (id) DO UPDATE SET page_id = EXCLUDED.page_id, url = EXCLUDED.url""",
                     (page["id"], page.get("url")))
        conn.commit()
        st = state(conn)
    dbs = dict(st["dbs"] or {})
    for key, (title, props) in DATABASES.items():
        if key == "goals" and not _has_table(conn, "client_goals"):
            continue
        if dbs.get(key):
            continue
        db = _api(tok, "POST", "/databases", {"parent": {"type": "page_id", "page_id": st["page_id"]},
                                              "title": _t(title), "properties": props}, call)
        dbs[key] = db["id"]
        conn.execute("UPDATE notion_os SET dbs = %s WHERE id = 1", (json.dumps(dbs),))
        conn.commit()
    return state(conn)


def _has_table(conn, name):
    return bool(conn.execute("SELECT to_regclass(%s) r", (name,)).fetchone()["r"])


# ---------------------------------------------------------------- push (Postgres → Notion)

def _task_props(r):
    from . import tasks
    return {"Name": _title(r["title"]), "Code": {"rich_text": _t(tasks.code(r))}, "Status": _sel(r["status"]),
            "Owner": {"rich_text": _t(r["owner_name"])}, "Due": _date(r["due"]),
            "Client": {"rich_text": _t(f"@{r['client']}" if r["client"] else "")},
            "Source": _sel(r["source"]), "Notes": {"rich_text": _t(r["notes"])}}


def _meeting_props(r):
    items = "\n".join(f"• {a.get('text')}" + (f" ({a['owner']})" if a.get("owner") else "")
                      for a in (r["action_items"] or []))
    return {"Name": _title(r["title"]), "Date": _date(r["started_at"].date() if r["started_at"] else None),
            "Client": {"rich_text": _t(f"@{r['client']}" if r["client"] else "")},
            "Summary": {"rich_text": _t(r["summary"])}, "Action items": {"rich_text": _t(items)},
            "Followed up": {"checkbox": bool(r["processed_at"])}, "Tasks created": {"number": r["tasks_created"]},
            "Link": {"url": r["url"] or None}}


def _client_props(r):
    return {"Name": _title(r["display_name"]), "Handle": {"rich_text": _t(f"@{r['handle']}")},
            "Company": {"rich_text": _t(r.get("company"))}, "Niche": {"rich_text": _t(r["niche_topics"])},
            "Active": {"checkbox": bool(r["active"])}, "X profile": {"url": f"https://x.com/{r['handle']}"}}


def _goal_props(r):
    return {"Name": _title(f"@{r['client']} · {r['metric']} {r['target']:g} {r['period']}"),
            "Client": {"rich_text": _t(f"@{r['client']}")}, "Metric": {"rich_text": _t(r["metric"])},
            "Target": {"number": float(r["target"])}, "Period": {"rich_text": _t(r["period"])},
            "End": _date(r.get("end_date")), "Active": {"checkbox": bool(r.get("active", True))}}


PUSH = [  # (db key, table, key column, changed-since column or None, props)
    ("tasks", "team_tasks", "id", "updated_at", _task_props),
    ("meetings", "meetings", "id", "processed_at", _meeting_props),
    ("clients", "clients", "handle", "updated_at", _client_props),
]


def push(conn, tok, st, budget, call=None):
    done = {}
    dbs = st["dbs"] or {}
    for key, table, pk, changed, props in PUSH:
        if not dbs.get(key) or budget[0] <= 0:
            continue
        rows = conn.execute(
            f"""SELECT * FROM {table} WHERE notion_page_id IS NULL
                   OR (notion_synced_at IS NOT NULL AND {changed} IS NOT NULL AND {changed} > notion_synced_at)
                ORDER BY {pk} LIMIT %s""", (budget[0],)).fetchall()
        n = 0
        for r in rows:
            body = {"properties": props(r)}
            if r["notion_page_id"]:
                _api(tok, "PATCH", f"/pages/{r['notion_page_id']}", body, call)
                page_id = r["notion_page_id"]
            else:
                page_id = _api(tok, "POST", "/pages", {"parent": {"database_id": dbs[key]}, **body}, call)["id"]
            conn.execute(f"UPDATE {table} SET notion_page_id = %s, notion_synced_at = now() WHERE {pk} = %s",
                         (page_id, r[pk]))
            conn.commit()
            n += 1
            budget[0] -= 1
        done[key] = n
    if dbs.get("goals") and _has_table(conn, "client_goals") and budget[0] > 0:
        conn.execute("ALTER TABLE client_goals ADD COLUMN IF NOT EXISTS notion_page_id TEXT")
        n = 0
        for r in conn.execute("SELECT * FROM client_goals WHERE notion_page_id IS NULL LIMIT %s", (budget[0],)).fetchall():
            pid = _api(tok, "POST", "/pages", {"parent": {"database_id": dbs["goals"]}, "properties": _goal_props(r)},
                       call)["id"]
            conn.execute("UPDATE client_goals SET notion_page_id = %s WHERE id = %s", (pid, r["id"]))
            conn.commit()
            n += 1
            budget[0] -= 1
        done["goals"] = n
    return done


# ---------------------------------------------------------------- pull (Notion → Postgres): tasks only

def _prop_text(p):
    if not p:
        return ""
    for k in ("title", "rich_text"):
        if k in p:
            return "".join(x.get("plain_text") or (x.get("text") or {}).get("content", "") for x in p[k]).strip()
    return ""


def _ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def pull(conn, tok, st, call=None, people=None):
    """Apply human edits made in Notion since the last pull. Returns {'updated': n, 'added': n}."""
    from . import tasks
    db = (st["dbs"] or {}).get("tasks")
    if not db:
        return {"updated": 0, "added": 0}
    since = st["last_pull"]
    started = datetime.now(timezone.utc)
    body = {"page_size": 100, "sorts": [{"timestamp": "last_edited_time", "direction": "descending"}]}
    if since:
        body["filter"] = {"timestamp": "last_edited_time", "last_edited_time": {"on_or_after": since.isoformat()}}
    res = _api(tok, "POST", f"/databases/{db}/query", body, call)
    updated = added = 0
    for page in res.get("results", []):
        props = page.get("properties", {})
        edited = _ts(page.get("last_edited_time"))
        row = conn.execute("SELECT * FROM team_tasks WHERE notion_page_id = %s", (page["id"],)).fetchone()
        title = _prop_text(props.get("Name"))
        status = ((props.get("Status") or {}).get("select") or {}).get("name")
        due = ((props.get("Due") or {}).get("date") or {}).get("start")
        owner = _prop_text(props.get("Owner"))
        if row:
            # Our own push also bumps last_edited_time; only treat edits made after our last write as human.
            if row["notion_synced_at"] and edited and edited <= row["notion_synced_at"]:
                continue
            changes = {}
            if status in STATUSES and status != row["status"]:
                changes["status"] = status
            if (due or None) != (row["due"].isoformat() if row["due"] else None):
                changes["due"] = due[:10] if due else ""
            if title and title != row["title"]:
                changes["title"] = title
            if owner and owner != (row["owner_name"] or ""):
                changes["owner"] = owner
            if changes:
                try:
                    tasks.update(conn, tasks.code(row), by="notion", people=people, **changes)
                    updated += 1
                except ValueError:
                    pass
            conn.execute("UPDATE team_tasks SET notion_synced_at = now() WHERE id = %s", (row["id"],))
            conn.commit()
        elif title and not _prop_text(props.get("Code")):
            new, _ = tasks.add(conn, [{"title": title, "owner": owner or None, "due": due[:10] if due else None}],
                               created_by="notion", people=people, source="notion")
            if new:
                tid = tasks.parse_code(new[0]["code"])
                conn.execute("UPDATE team_tasks SET notion_page_id = %s, notion_synced_at = now() WHERE id = %s",
                             (page["id"], tid))
                _api(tok, "PATCH", f"/pages/{page['id']}", {"properties": {"Code": {"rich_text": _t(new[0]["code"])}}}, call)
                conn.commit()
                added += 1
    conn.execute("UPDATE notion_os SET last_pull = %s WHERE id = 1", (started,))
    conn.commit()
    return {"updated": updated, "added": added}


# ---------------------------------------------------------------- entry points

def sync(conn, tok=None, call=None, people=None):
    if tok is None:
        tok = connections.token(conn, connections.AGENCY, "notion")
    st = ensure(conn, tok, call)
    pulled = pull(conn, tok, st, call, people)          # human edits first, so a push never overwrites them
    pushed = push(conn, tok, state(conn), [MAX_WRITES], call)
    return {"url": st["url"], "pulled": pulled, "pushed": pushed}


def notion_os_status(params, **_):
    conn = ledger.connect()
    try:
        st = state(conn)
        if not st:
            return json.dumps({"success": True, "set_up": False,
                               "note": "Agency OS appears in Notion within 10 minutes of the agency Notion being connected."})
        counts = {t: conn.execute(f"SELECT count(*) n FROM {t} WHERE notion_page_id IS NOT NULL").fetchone()["n"]
                  for t in ("team_tasks", "meetings", "clients")}
        return json.dumps({"success": True, "set_up": True, "url": st["url"], "databases": list((st["dbs"] or {})),
                           "synced_rows": counts, "last_pull": str(st["last_pull"])})
    finally:
        conn.close()


TOOLS = [("notion_os_status", notion_os_status,
          "Link to the agency's Notion 'Agency OS' (Tasks, Meetings, Clients, Goals databases kept in sync with "
          "Slack every 10 minutes) and how much is synced.", {}, [])]


def register(ctx, toolset="notion_os"):
    for name, fn, description, props, required in TOOLS:
        ctx.register_tool(name=name, toolset=toolset, handler=fn,
                          schema={"name": name, "description": description,
                                  "parameters": {"type": "object", "properties": props, "required": required}})
