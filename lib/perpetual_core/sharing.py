"""Hand a task's materials to the person who has to do it.

"Add a task for Shravya: review this <Google Doc> and the <Canva> deck, here's the brief.pdf" should leave
Shravya able to open everything, not just with a task title:

    Google Docs / Sheets / Slides / Drive   -> Drive permission for their email (edit by default)
    Slack files, files sent to the bot      -> re-uploaded into their DM
    Canva / Figma / Notion / anything else  -> no API to grant access: link in their DM, and the
                                               creator is told to check the link's sharing setting
"""

import json
import re

URL_RX = re.compile(r"https?://[^\s<>|\]\)\"']+")
GOOGLE_RX = re.compile(r"https?://(?:docs|drive|sheets|slides)\.google\.com/", re.I)
ROLES = {"edit": "writer", "comment": "commenter", "view": "reader"}
LABELS = {"edit": "edit", "comment": "comment", "view": "view"}
KINDS = (("canva", r"canva\.(com|link)"), ("figma", r"figma\.com"), ("notion", r"notion\.(so|site)"),
         ("loom", r"loom\.com"), ("slack", r"slack\.com/(files|archives)"), ("dropbox", r"dropbox\.com"),
         ("onedrive", r"(1drv\.ms|sharepoint\.com|onedrive)"), ("airtable", r"airtable\.com"),
         ("miro", r"miro\.com"))


def links_in(*texts):
    """Every URL in these texts (Slack's <url|label> form included), in order, no duplicates."""
    out = []
    for t in texts:
        for u in URL_RX.findall(t or ""):
            u = u.rstrip(".,;:!?")
            if u not in out:
                out.append(u)
    return out


def kind(url):
    if GOOGLE_RX.match(url or ""):
        return "google"
    for name, rx in KINDS:
        if re.search(rx, url or "", re.I):
            return name
    return "link"


def google_id(url):
    for rx in (r"/d/([A-Za-z0-9_-]{15,})", r"/folders/([A-Za-z0-9_-]{15,})", r"[?&]id=([A-Za-z0-9_-]{15,})"):
        m = re.search(rx, url or "")
        if m:
            return m.group(1)
    return None


def google_label(url):
    for word, label in (("document", "Google Doc"), ("spreadsheets", "Google Sheet"), ("presentation", "Google Slides"),
                        ("forms", "Google Form"), ("folders", "Drive folder")):
        if f"/{word}/" in url:
            return label
    return "Drive file"


def email_of(user_id, people=None):
    from . import slackui
    for p in (people if people is not None else slackui.people()):
        if p["id"] == user_id:
            return p.get("email")
    return None


def _google_token(conn, created_by):
    """The creator's own Google if they connected it, else the agency's (None if neither)."""
    from . import connections
    for who in (created_by, connections.AGENCY):
        if who and connections.get_row(conn, who, "google"):
            try:
                return connections.token(conn, who, "google")
            except Exception:  # noqa: BLE001
                continue
    return None


def share_link(url, email, access, token, call=None):
    """{url, kind, label, status: shared|link_only|failed, note}."""
    k = kind(url)
    access = access if access in ROLES else "edit"
    if k != "google":
        name = {"link": "link"}.get(k, k.capitalize())
        return {"url": url, "kind": k, "label": name, "status": "link_only",
                "note": (f"{name}: link sent. I can't grant access in {name}, so make sure its sharing is set to "
                         f"'anyone with the link can {LABELS[access]}' (or invite them in {name}).")
                if k not in ("link", "slack") else "link sent"}
    label = google_label(url)
    fid = google_id(url)
    if not fid:
        return {"url": url, "kind": k, "label": label, "status": "link_only", "note": "couldn't read the file id; link sent"}
    if not email:
        return {"url": url, "kind": k, "label": label, "status": "link_only",
                "note": "their Slack profile has no email, so I couldn't share it in Drive; link sent"}
    if not token:
        return {"url": url, "kind": k, "label": label, "status": "link_only",
                "note": "no Google account is connected (say 'connect my google'), so I couldn't share it; link sent"}
    if call is None:
        from .workspace import _call as call
    try:
        call("POST", f"https://www.googleapis.com/drive/v3/files/{fid}/permissions", token,
             body={"type": "user", "role": ROLES[access], "emailAddress": email},
             params={"sendNotificationEmail": "false", "supportsAllDrives": "true"})
        return {"url": url, "kind": k, "label": label, "status": "shared", "access": LABELS[access],
                "note": f"{label}: {email} can now {LABELS[access]} it"}
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        why = ("the connected Google account can't share this file (it isn't its owner or an editor)"
               if msg.startswith(("403", "404")) else "Drive refused the share")
        return {"url": url, "kind": k, "label": label, "status": "failed", "note": f"{label}: {why}; link sent ({msg[:120]})"}


def _file_bytes(ref):
    """(name, bytes) for a Slack file id/link or a file the person sent (document cache only)."""
    from pathlib import Path
    from . import doclib
    if re.search(r"\bF[A-Z0-9]{8,}\b", ref or ""):
        path, name = doclib.fetch_slack_file(ref)
        return name, Path(path).read_bytes()
    real = doclib.allowed(ref)
    if not real:
        raise ValueError("I can only send files that were shared with me in Slack")
    return real.name, real.read_bytes()


def deliver_files(user_id, refs, note=None, slack=None, upload=None):
    """Upload each file into the person's DM. Returns [{file, status, note}]."""
    from . import media, slackui
    slack = slack or slackui.slack
    upload = upload or media.upload_files
    out, files = [], []
    for ref in refs or []:
        try:
            name, data = _file_bytes(str(ref))
            files.append((name, data, name))
            out.append({"file": name, "status": "sent"})
        except Exception as e:  # noqa: BLE001
            out.append({"file": str(ref)[-60:], "status": "failed", "note": str(e)[:160]})
    if files:
        try:
            channel = user_id if re.fullmatch(r"[CGD][A-Z0-9]{6,}", user_id or "") else slack(
                "conversations.open", users=user_id)["channel"]["id"]
            upload(channel, files, initial_comment=note)
        except Exception as e:  # noqa: BLE001
            for o in out:
                if o["status"] == "sent":
                    o.update(status="failed", note=f"couldn't upload to their DM: {str(e)[:120]}")
    return out


def share_task(conn, row, owner_id, created_by=None, access="edit", people=None, call=None, slack=None, upload=None):
    """Share one task's links and files with its owner. Returns {links: [...], files: [...]}."""
    links = list(row.get("links") or [])
    files = list(row.get("files") or [])
    if not owner_id or (not links and not files):
        return {"links": [], "files": []}
    email = email_of(owner_id, people)
    token = _google_token(conn, created_by) if any(kind(u) == "google" for u in links) else None
    shared = [share_link(u, email, access, token, call) for u in links]
    sent = deliver_files(owner_id, files, note=f"📎 Files for TASK-{row['id']}: {row['title'][:120]}",
                         slack=slack, upload=upload) if files else []
    return {"links": shared, "files": sent}


def dm_lines(result):
    """Lines under a task in the owner's DM."""
    out = []
    for s in result.get("links") or []:
        icon = "✅" if s["status"] == "shared" else "🔗"
        extra = f" (you have {s['access']} access)" if s["status"] == "shared" else ""
        out.append(f"   {icon} <{s['url']}|{s['label']}>{extra}")
    for f in result.get("files") or []:
        if f["status"] == "sent":
            out.append(f"   📎 {f['file']} (sent here in this chat)")
    return out


# ---------------------------------------------------------------- send anything to anyone in Slack

def _channel(ref, slack):
    """'#name', '<#C123|name>' or a C/G id → (id, name); joins public channels the bot isn't in."""
    ref = ref.strip()
    m = re.match(r"<#([CG][A-Z0-9]+)(?:\|[^>]*)?>", ref)
    cid = m.group(1) if m else (ref if re.fullmatch(r"[CG][A-Z0-9]{6,}", ref) else None)
    if cid:
        info = slack("conversations.info", channel=cid)["channel"]
    else:
        name, info, cursor = ref.lstrip("#").lower(), None, None
        while info is None:
            data = slack("conversations.list", types="public_channel,private_channel", exclude_archived=True,
                         limit=500, cursor=cursor)
            info = next((c for c in data["channels"] if c["name"] == name), None)
            cursor = (data.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
        if not info:
            raise ValueError(f"I can't see a channel called #{name}. Invite me there first (/invite @Agency Bot).")
    if not info.get("is_member"):
        if info.get("is_private"):
            raise ValueError(f"#{info['name']} is private and I'm not in it. Invite me there first.")
        slack("conversations.join", channel=info["id"])
    return info["id"], info["name"]


def _is_channel(target):
    t = target.strip()
    return t.startswith(("#", "<#")) or bool(re.fullmatch(r"[CG][A-Z0-9]{6,}", t))


def send(conn, to, text, links=None, files=None, access="edit", by=None, people=None, call=None, slack=None,
         upload=None):
    """Message people (DM) and/or channels, with docs and files. People get Google files shared with them in
    Drive and files uploaded into their DM; channels get the links and files posted there.
    Returns {sent: [...], problems: [...]}."""
    from . import slackui, tasks
    slack = slack or slackui.slack
    everyone = people if people is not None else slackui.people()
    links = links_in(" ".join(links or []), text)
    files = [str(f) for f in (files or []) if str(f).strip()]
    sender = next((p["real_name"] or p["name"] for p in everyone if p["id"] == by), None) if by else None
    footer = f"\n_sent by Agency Bot for {sender}_" if sender else ""
    token = _google_token(conn, by) if any(kind(u) == "google" for u in links) else None
    targets = to if isinstance(to, list) else re.split(r"\s*,\s*", to or "")
    sent, problems = [], []
    for target in [t for t in targets if t and t.strip()]:
        try:
            if _is_channel(target):
                cid, cname = _channel(target, slack)
                extra = "".join(f"\n🔗 <{u}|{google_label(u) if kind(u) == 'google' else kind(u).capitalize()}>"
                                for u in links if u not in text)
                slack("chat.postMessage", channel=cid, text=text + extra + footer, unfurl_links=True)
                got = deliver_files(cid, files, slack=slack, upload=upload) if files else []
                sent.append({"to": f"#{cname}", "files": got,
                             "links": [{"url": u, "status": "posted",
                                        "note": "posted in the channel; people need access to open it"} for u in links]})
                continue
            uid, name, problem = tasks.resolve_owner(target, everyone)
            if problem or not uid:
                problems.append({"to": target, "problem": problem or "not found"})
                continue
            shared = [share_link(u, email_of(uid, everyone), access, token, call) for u in links]
            lines = dm_lines({"links": shared})
            body = text + ("\n" + "\n".join(lines) if lines else "")
            slack("chat.postMessage", channel=uid, text=body + footer, unfurl_links=False)
            got = deliver_files(uid, files, slack=slack, upload=upload) if files else []
            sent.append({"to": name, "links": shared, "files": got})
        except Exception as e:  # noqa: BLE001 — report per target, keep going
            problems.append({"to": target, "problem": str(e)[:200]})
    return {"sent": sent, "problems": problems}


def tool_slack_send(params, **_):
    from . import connections, ledger
    conn = ledger.connect()
    try:
        out = send(conn, params.get("to") or [], params.get("text") or "", params.get("links"), params.get("files"),
                   params.get("access") or "edit", by=connections.current_user())
        return json.dumps({"success": bool(out["sent"]), **out}, ensure_ascii=False, default=str)
    except Exception as e:  # noqa: BLE001
        return json.dumps({"success": False, "error": str(e)})
    finally:
        conn.close()


# Slack Web API methods that remove or hide things: the owner confirms first.
_DESTRUCTIVE = re.compile(r"(delete|remove|archive|kick|leave|revoke|disable|deactivate|reset|close)", re.I)


def tool_slack_api(params, **_):
    """Owners only: any Slack Web API method the bot's scopes allow."""
    from . import connections, guard, slackui
    user = connections.current_user()
    if not user or user not in guard.owners():
        return json.dumps({"success": False, "error": "slack_api is for the agency owners only."})
    method = (params.get("method") or "").strip()
    if not re.fullmatch(r"[a-zA-Z]+(\.[a-zA-Z]+)+", method) or method.startswith("admin."):
        return json.dumps({"success": False, "error": "give a Slack Web API method like 'conversations.history'"})
    if _DESTRUCTIVE.search(method) and not params.get("confirmed"):
        return json.dumps({"success": False, "needs_confirmation": True,
                           "error": f"{method} removes or hides something. Ask the owner to confirm, then call "
                                    "again with confirmed=true."})
    try:
        args = params.get("args") or {}
        if isinstance(args, str):
            args = json.loads(args)
        text = json.dumps(slackui.slack(method, **args), ensure_ascii=False, default=str)
        return text if len(text) < 30000 else json.dumps({"success": True, "truncated": True, "data": text[:30000]})
    except Exception as e:  # noqa: BLE001
        return json.dumps({"success": False, "error": str(e)[:400]})


_S = {"type": "string"}
TOOLS = [
    ("slack_send", tool_slack_send,
     "Send a message to anyone in Slack: teammates by name/@/email (as a DM) and/or channels (#name), with any "
     "docs, links and files. Google Docs/Sheets/Slides/Drive links are shared with each person in Drive "
     "(access: edit default | comment | view); Canva/Figma/other links are included as links; files (Slack file "
     "ids/links or the saved path of a file sent to you) are uploaded to them. Use for 'send this to Naga', "
     "'share this doc with Shravya and Siri', 'post this in #general'. Report back exactly what was shared.",
     {"to": {"type": "array", "items": _S}, "text": _S, "links": {"type": "array", "items": _S},
      "files": {"type": "array", "items": _S}, "access": {"type": "string", "enum": ["edit", "comment", "view"]}},
     ["to", "text"]),
    ("slack_api", tool_slack_api,
     "Owners only: call any Slack Web API method with args (e.g. conversations.history, conversations.create, "
     "conversations.invite, pins.add, reactions.add, users.info, chat.update, canvases.create, bookmarks.add). "
     "Methods that delete/archive/remove need confirmed=true after the owner says yes.",
     {"method": _S, "args": {"type": "object"}, "confirmed": {"type": "boolean"}}, ["method"]),
]


def register(ctx, toolset="slack_power"):
    for name, fn, description, props, required in TOOLS:
        ctx.register_tool(name=name, toolset=toolset, handler=fn,
                          schema={"name": name, "description": description,
                                  "parameters": {"type": "object", "properties": props, "required": required}})
