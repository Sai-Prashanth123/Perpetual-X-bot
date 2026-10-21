"""Workspace tools: the asking teammate's own Google Calendar, Gmail (drafts only), Drive, Docs,
Sheets and Notion. Every call uses the account of whoever is talking to the bot right now
(connections.current_user); nobody can reach anyone else's account, and scheduled jobs can't
reach any. Registered by both plugins (Perpetual and the client agents).
"""

import base64
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

from . import connections, ledger, notify

NOTION_VERSION = "2022-06-28"


def _ok(**data):
    return json.dumps({"success": True, **data}, ensure_ascii=False, default=str)


def _call(method, url, token, body=None, params=None, headers=None, raw=None, content_type="application/json"):
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params, doseq=True)
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    h = {"Authorization": f"Bearer {token}", **(headers or {})}
    if data is not None:
        h["Content-Type"] = content_type
    req = urllib.request.Request(url, method=method, data=data, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            out = r.read()
            return json.loads(out) if out[:1] in (b"{", b"[") else out.decode(errors="replace")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{e.code} from {url.split('?')[0]}: {e.read().decode(errors='replace')[:400]}") from None


def _with_token(provider):
    def wrap(fn):
        def handler(params, **kwargs):
            del kwargs
            try:
                conn = ledger.connect()
                try:
                    tok = connections.token(conn, connections.current_user(), provider)
                finally:
                    conn.close()
                return fn(tok, params or {})
            except connections.NotConnected as e:
                return json.dumps({"success": False, "not_connected": True, "error": str(e)})
            except Exception as e:  # noqa: BLE001
                return json.dumps({"success": False, "error": str(e)})
        return handler
    return wrap


# ---------------------------------------------------------------- connecting

def connect_account(params, **_):
    provider = (params.get("tool") or "").lower()
    user = connections.current_user()
    if not user:
        return json.dumps({"success": False, "error": "Only a person can connect their own account."})
    if provider not in connections.PROVIDERS:
        return json.dumps({"success": False, "error": f"Choose one of: {', '.join(connections.PROVIDERS)}"})
    if not connections.configured(provider):
        return json.dumps({"success": False, "error": f"{provider} sign-in isn't set up on the server yet "
                                                       "(the agency owner needs to add its OAuth app)."})
    url = connections.link(provider, user)
    label = connections.PROVIDERS[provider]["label"]
    notify.dm([user], f"🔐 Connect your {label}: <{url}|sign in here> (link works for 1 hour, only for you).\n"
                      "I'll only use it when you ask me something.")
    return _ok(sent_to="your DMs", note="Tell them the sign-in link is in their DMs. Never post the link in a channel.")


def my_connections(params, **_):
    user = connections.current_user()
    conn = ledger.connect()
    try:
        return _ok(connected=connections.listing(conn, user) if user else [],
                   available={k: v["label"] for k, v in connections.PROVIDERS.items()})
    finally:
        conn.close()


def disconnect_account(params, **_):
    user = connections.current_user()
    conn = ledger.connect()
    try:
        return _ok(disconnected=connections.disconnect(conn, user, (params.get("tool") or "").lower()))
    finally:
        conn.close()


# ---------------------------------------------------------------- Google Calendar

G = "https://www.googleapis.com"


@_with_token("google")
def gcal_events(tok, p):
    start = datetime.now(timezone.utc) - timedelta(days=p.get("days_back") or 0)
    end = datetime.now(timezone.utc) + timedelta(days=p.get("days_ahead") or 7)
    data = _call("GET", f"{G}/calendar/v3/calendars/primary/events", tok, params={
        "timeMin": start.isoformat(), "timeMax": end.isoformat(), "singleEvents": "true", "orderBy": "startTime",
        "maxResults": min(p.get("limit") or 50, 100), **({"q": p["query"]} if p.get("query") else {})})
    return _ok(events=[{"id": e["id"], "title": e.get("summary"), "start": (e.get("start") or {}).get("dateTime") or (e.get("start") or {}).get("date"),
                        "end": (e.get("end") or {}).get("dateTime") or (e.get("end") or {}).get("date"),
                        "where": e.get("location"), "attendees": [a.get("email") for a in e.get("attendees", [])][:10],
                        "link": e.get("htmlLink")} for e in data.get("items", [])])


@_with_token("google")
def gcal_create_event(tok, p):
    body = {"summary": p["title"], "description": p.get("description"),
            "start": {"dateTime": p["start"]}, "end": {"dateTime": p["end"]}}
    if p.get("timezone"):
        body["start"]["timeZone"] = body["end"]["timeZone"] = p["timezone"]
    if p.get("attendees"):
        body["attendees"] = [{"email": a} for a in p["attendees"]]
    # sendUpdates=none: adding attendees never emails them unless the person sends invites themselves.
    e = _call("POST", f"{G}/calendar/v3/calendars/primary/events", tok, body, params={"sendUpdates": "none"})
    return _ok(created=e.get("summary"), start=(e.get("start") or {}).get("dateTime"), link=e.get("htmlLink"))


# ---------------------------------------------------------------- Gmail (read + drafts, never send)

def _header(msg, name):
    return next((h["value"] for h in msg.get("payload", {}).get("headers", []) if h["name"].lower() == name), None)


def _plain(part):
    if part.get("mimeType") == "text/plain" and (part.get("body") or {}).get("data"):
        return base64.urlsafe_b64decode(part["body"]["data"] + "==").decode(errors="replace")
    for sub in part.get("parts", []) or []:
        text = _plain(sub)
        if text:
            return text
    return ""


@_with_token("google")
def gmail_search(tok, p):
    ids = _call("GET", f"{G}/gmail/v1/users/me/messages", tok,
                params={"q": p.get("query") or "", "maxResults": min(p.get("limit") or 10, 25)}).get("messages", [])
    out = []
    for m in ids:
        msg = _call("GET", f"{G}/gmail/v1/users/me/messages/{m['id']}", tok,
                    params={"format": "metadata", "metadataHeaders": ["From", "To", "Subject", "Date"]})
        out.append({"id": m["id"], "from": _header(msg, "from"), "subject": _header(msg, "subject"),
                    "date": _header(msg, "date"), "snippet": msg.get("snippet")})
    return _ok(messages=out)


@_with_token("google")
def gmail_read(tok, p):
    msg = _call("GET", f"{G}/gmail/v1/users/me/messages/{p['id']}", tok, params={"format": "full"})
    return _ok(subject=_header(msg, "subject"), sender=_header(msg, "from"), to=_header(msg, "to"),
               date=_header(msg, "date"), body=_plain(msg.get("payload", {}))[:8000])


@_with_token("google")
def gmail_draft(tok, p):
    m = EmailMessage()
    m["To"] = ", ".join(p["to"]) if isinstance(p["to"], list) else p["to"]
    m["Subject"] = p["subject"]
    m.set_content(p["body"])
    raw = base64.urlsafe_b64encode(m.as_bytes()).decode()
    d = _call("POST", f"{G}/gmail/v1/users/me/drafts", tok, {"message": {"raw": raw}})
    return _ok(draft_id=d.get("id"), note="Saved as a draft in their Gmail. It is NOT sent; they review and send it.",
               open="https://mail.google.com/mail/u/0/#drafts")


# ---------------------------------------------------------------- Drive, Docs, Sheets

@_with_token("google")
def gdrive_search(tok, p):
    q = p.get("query") or ""
    query = f"fullText contains '{q.replace(chr(39), ' ')}' and trashed = false" if q else "trashed = false"
    params = {"q": query, "pageSize": min(p.get("limit") or 15, 50),
              "fields": "files(id,name,mimeType,modifiedTime,webViewLink)"}
    if not q:                    # Drive can't sort full-text results; recent files can be
        params["orderBy"] = "modifiedTime desc"
    data = _call("GET", f"{G}/drive/v3/files", tok, params=params)
    return _ok(files=data.get("files", []))


@_with_token("google")
def gdoc_create(tok, p):
    """A Google Doc from markdown (Drive converts headings, lists, links and bold)."""
    boundary = "pxboundary7"
    meta = {"name": p["title"], "mimeType": "application/vnd.google-apps.document"}
    if p.get("folder_id"):
        meta["parents"] = [p["folder_id"]]
    for media_type in ("text/markdown", "text/plain"):
        body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{json.dumps(meta)}\r\n"
                f"--{boundary}\r\nContent-Type: {media_type}; charset=UTF-8\r\n\r\n{p['markdown']}\r\n--{boundary}--").encode()
        try:
            f = _call("POST", f"{G}/upload/drive/v3/files", tok, raw=body,
                      params={"uploadType": "multipart", "fields": "id,name,webViewLink"},
                      content_type=f"multipart/related; boundary={boundary}")
            return _ok(doc=f.get("name"), link=f.get("webViewLink"), id=f.get("id"))
        except RuntimeError as e:
            if media_type == "text/plain" or "400" not in str(e):
                raise
    return json.dumps({"success": False, "error": "could not create the document"})


@_with_token("google")
def gdoc_read(tok, p):
    file_id = p["id"]
    if "/d/" in file_id:
        file_id = file_id.split("/d/")[1].split("/")[0]
    text = _call("GET", f"{G}/drive/v3/files/{file_id}/export", tok, params={"mimeType": "text/plain"})
    return _ok(text=(text if isinstance(text, str) else json.dumps(text))[:15000])


@_with_token("google")
def gsheet_create(tok, p):
    s = _call("POST", "https://sheets.googleapis.com/v4/spreadsheets", tok, {"properties": {"title": p["title"]}})
    sid = s["spreadsheetId"]
    if p.get("rows"):
        _append(tok, sid, p["rows"])
    return _ok(sheet=p["title"], id=sid, link=s.get("spreadsheetUrl"))


def _append(tok, sid, rows):
    return _call("POST", f"https://sheets.googleapis.com/v4/spreadsheets/{sid}/values/A1:append", tok,
                 {"values": rows}, params={"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"})


@_with_token("google")
def gsheet_append(tok, p):
    sid = p["id"]
    if "/d/" in sid:
        sid = sid.split("/d/")[1].split("/")[0]
    r = _append(tok, sid, p["rows"])
    return _ok(updated=(r.get("updates") or {}).get("updatedRows"))


# ---------------------------------------------------------------- Notion

N = "https://api.notion.com/v1"


def _nh():
    return {"Notion-Version": NOTION_VERSION}


def _rich(text):
    """Plain text with [label](url) links and **bold** → Notion rich text (≤ 2000 chars per piece)."""
    out, pos = [], 0
    for m in re.finditer(r"\[([^\]]+)\]\((https?://[^)\s]+)\)|\*\*(.+?)\*\*", text):
        if m.start() > pos:
            out.append({"type": "text", "text": {"content": text[pos:m.start()]}})
        if m.group(1):
            out.append({"type": "text", "text": {"content": m.group(1), "link": {"url": m.group(2)}}})
        else:
            out.append({"type": "text", "text": {"content": m.group(3)}, "annotations": {"bold": True}})
        pos = m.end()
    if pos < len(text):
        out.append({"type": "text", "text": {"content": text[pos:]}})
    for piece in out:
        piece["text"]["content"] = piece["text"]["content"][:2000]
    return out or [{"type": "text", "text": {"content": ""}}]


def md_to_blocks(md):
    blocks = []
    for line in (md or "").splitlines():
        s = line.rstrip()
        if not s.strip():
            continue
        if re.match(r"^\s*([-*_])\1{2,}\s*$", s):
            blocks.append({"object": "block", "type": "divider", "divider": {}})
            continue
        h = re.match(r"^(#{1,3})\s+(.*)$", s)
        if h:
            kind = f"heading_{len(h.group(1))}"
            blocks.append({"object": "block", "type": kind, kind: {"rich_text": _rich(h.group(2))}})
            continue
        for pattern, kind in ((r"^\s*[-*]\s+(.*)$", "bulleted_list_item"), (r"^\s*\d+[.)]\s+(.*)$", "numbered_list_item"),
                              (r"^\s*>\s?(.*)$", "quote")):
            m = re.match(pattern, s)
            if m:
                blocks.append({"object": "block", "type": kind, kind: {"rich_text": _rich(m.group(1))}})
                break
        else:
            blocks.append({"object": "block", "type": "paragraph", "paragraph": {"rich_text": _rich(s.strip())}})
    return blocks


@_with_token("notion")
def notion_search(tok, p):
    body = {"query": p.get("query") or "", "page_size": min(p.get("limit") or 15, 50)}
    if p.get("only") in ("page", "database"):
        body["filter"] = {"property": "object", "value": p["only"]}
    data = _call("POST", f"{N}/search", tok, body, headers=_nh())
    out = []
    for r in data.get("results", []):
        title = ""
        if r["object"] == "database":
            title = "".join(t.get("plain_text", "") for t in r.get("title", []))
        else:
            for prop in (r.get("properties") or {}).values():
                if prop.get("type") == "title":
                    title = "".join(t.get("plain_text", "") for t in prop.get("title", []))
        out.append({"id": r["id"], "type": r["object"], "title": title, "url": r.get("url")})
    return _ok(results=out, note="Only pages the person shared with the integration when connecting are visible.")


@_with_token("notion")
def notion_page_create(tok, p):
    blocks = md_to_blocks(p.get("markdown") or "")
    parent_id = p["parent_id"]
    if p.get("parent_type") == "database":
        parent = {"database_id": parent_id}
        props = {p.get("title_property") or "Name": {"title": _rich(p["title"])}}
    else:
        parent = {"page_id": parent_id}
        props = {"title": {"title": _rich(p["title"])}}
    page = _call("POST", f"{N}/pages", tok, {"parent": parent, "properties": props, "children": blocks[:100]},
                 headers=_nh())
    for i in range(100, len(blocks), 100):
        _call("PATCH", f"{N}/blocks/{page['id']}/children", tok, {"children": blocks[i:i + 100]}, headers=_nh())
    return _ok(page=p["title"], url=page.get("url"), id=page["id"])


def _block_text(b):
    data = b.get(b["type"]) or {}
    return "".join(t.get("plain_text", "") for t in data.get("rich_text", []))


@_with_token("notion")
def notion_page_read(tok, p):
    pid = p["id"].rstrip("/").split("-")[-1] if p["id"].startswith("http") else p["id"]
    data = _call("GET", f"{N}/blocks/{pid}/children", tok, params={"page_size": 100}, headers=_nh())
    lines = []
    for b in data.get("results", []):
        text = _block_text(b)
        prefix = {"heading_1": "# ", "heading_2": "## ", "heading_3": "### ", "bulleted_list_item": "- ",
                  "numbered_list_item": "1. ", "quote": "> ", "to_do": "[ ] "}.get(b["type"], "")
        if text:
            lines.append(prefix + text)
    return _ok(text="\n".join(lines)[:15000])


# ---------------------------------------------------------------- registration

_S = {"type": "string"}
_I = {"type": "integer"}
_ROWS = {"type": "array", "items": {"type": "array", "items": {}}, "description": "Rows of cells, first row = headers."}

TOOLS = [
    ("connect_account", connect_account,
     "Send the person asking a private sign-in link (by DM) to connect THEIR own account: tool 'google' "
     "(Calendar, Gmail, Drive, Docs, Sheets) or 'notion'. Use when they say 'connect my google/notion/calendar'.",
     {"tool": {"type": "string", "enum": list(connections.PROVIDERS)}}, ["tool"]),
    ("my_connections", my_connections, "Which accounts the person asking has connected.", {}, []),
    ("disconnect_account", disconnect_account, "Disconnect one of the asking person's accounts.",
     {"tool": {"type": "string", "enum": list(connections.PROVIDERS)}}, ["tool"]),
    ("gcal_events", gcal_events,
     "The asking person's Google Calendar events (primary calendar). Optional query, days_ahead (default 7), "
     "days_back.", {"query": _S, "days_ahead": _I, "days_back": _I, "limit": _I}, []),
    ("gcal_create_event", gcal_create_event,
     "Create an event on the asking person's Google Calendar. start/end are ISO times with offset. Attendees are "
     "added without emailing them. Confirm title and time with the person first.",
     {"title": _S, "start": _S, "end": _S, "timezone": _S, "description": _S,
      "attendees": {"type": "array", "items": _S}}, ["title", "start", "end"]),
    ("gmail_search", gmail_search, "Search the asking person's Gmail (Gmail search syntax, e.g. 'from:alex newer_than:7d').",
     {"query": _S, "limit": _I}, ["query"]),
    ("gmail_read", gmail_read, "Read one email from the asking person's Gmail by id.", {"id": _S}, ["id"]),
    ("gmail_draft", gmail_draft,
     "Save an email as a DRAFT in the asking person's Gmail (it is never sent; they send it themselves).",
     {"to": _S, "subject": _S, "body": _S}, ["to", "subject", "body"]),
    ("gdrive_search", gdrive_search, "Search the asking person's Google Drive (full text). No query = recent files.",
     {"query": _S, "limit": _I}, []),
    ("gdoc_create", gdoc_create,
     "Create a Google Doc in the asking person's Drive from markdown (e.g. save a report, research or package).",
     {"title": _S, "markdown": _S, "folder_id": _S}, ["title", "markdown"]),
    ("gdoc_read", gdoc_read, "Read a Google Doc as text (id or docs.google.com link).", {"id": _S}, ["id"]),
    ("gsheet_create", gsheet_create, "Create a Google Sheet (optionally with rows), e.g. export leads or a scoreboard.",
     {"title": _S, "rows": _ROWS}, ["title"]),
    ("gsheet_append", gsheet_append, "Append rows to an existing Google Sheet (id or link).",
     {"id": _S, "rows": _ROWS}, ["id", "rows"]),
    ("notion_search", notion_search,
     "Search the asking person's Notion (pages/databases they shared when connecting).",
     {"query": _S, "only": {"type": "string", "enum": ["page", "database"]}, "limit": _I}, []),
    ("notion_page_create", notion_page_create,
     "Create a Notion page from markdown under a parent page, or as a row in a database (parent_type database; "
     "title_property = the database's title column, default 'Name'). Find the parent with notion_search.",
     {"parent_id": _S, "parent_type": {"type": "string", "enum": ["page", "database"]}, "title": _S,
      "markdown": _S, "title_property": _S}, ["parent_id", "title"]),
    ("notion_page_read", notion_page_read, "Read a Notion page's content as text (id or URL).", {"id": _S}, ["id"]),
]


def register(ctx, toolset="workspace"):
    for name, fn, description, props, required in TOOLS:
        ctx.register_tool(name=name, toolset=toolset, handler=fn,
                          schema={"name": name, "description": description,
                                  "parameters": {"type": "object", "properties": props, "required": required}})
