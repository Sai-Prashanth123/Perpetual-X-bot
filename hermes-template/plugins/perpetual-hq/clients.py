"""Client management by chat: add, update, list and remove the agency's clients.

Adding a client: validate the X handle, put it on the scraping console roster, resolve the
employee, create or reuse its public Slack channel, save it in Postgres, run the reconciler
(profile, skills, schedule, channel route), then restart the gateway so the new channel route
takes effect (routes are read once at gateway start).
"""

import json
import os
import re
import subprocess

from perpetual_core import agency, console, slackui, xpost
from perpetual_core.settings import env

RECONCILE = ["/opt/hermes/.venv/bin/python", "/opt/perpetual/reconcile.py", "--from-runtime"]
DEFAULT_TZ = "Asia/Kolkata"


def _requester():
    try:
        from gateway.session_context import get_session_env
        return get_session_env("HERMES_SESSION_USER_ID") or None
    except Exception:  # noqa: BLE001 — not in a gateway turn (tests, CLI)
        return None


def _channel_name(handle, platform="x"):
    return (("li-" if platform == "linkedin" else "x-") + re.sub(r"[^a-z0-9_-]", "", handle.lower()))[:80]


def _resolve_channel(given, handle, platform="x"):
    """Use the channel the user named (ID, #name or name), or create/reuse public #x-<handle>."""
    if given:
        ref = given.strip().lstrip("#")
        m = re.match(r"<#(C[A-Z0-9]+)(?:\|[^>]*)?>", given.strip())
        if m:
            ref = m.group(1)
        if re.fullmatch(r"[CG][A-Z0-9]{6,}", ref):
            info = slackui.slack("conversations.info", channel=ref)["channel"]
        else:
            info = _find_channel_by_name(ref)
            if not info:
                raise ValueError(f"I can't see a channel called #{ref}. Invite me to it first (/invite @Agency Bot).")
        if not info.get("is_member"):
            if info.get("is_private"):
                raise ValueError(f"#{info['name']} is private and I'm not in it. Type /invite @Agency Bot there first.")
            slackui.slack("conversations.join", channel=info["id"])
        return info["id"], info["name"], False
    name = _channel_name(handle, platform)
    existing = _find_channel_by_name(name)
    if existing:
        if not existing.get("is_member"):
            slackui.slack("conversations.join", channel=existing["id"])
        return existing["id"], existing["name"], False
    info = slackui.slack("conversations.create", name=name, is_private=False)["channel"]
    return info["id"], info["name"], True


def _find_channel_by_name(name):
    cursor = None
    while True:
        data = slackui.slack("conversations.list", types="public_channel,private_channel",
                             exclude_archived=True, limit=500, cursor=cursor)
        for ch in data["channels"]:
            if ch["name"] == name:
                return ch
        cursor = (data.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return None


def channel_team():
    """Who is in every client channel by default (CLIENT_CHANNEL_MEMBERS, else the owners)."""
    from perpetual_core.settings import env
    raw = env("CLIENT_CHANNEL_MEMBERS") or env("OWNER_SLACK_IDS") or ""
    return [u.strip() for u in raw.split(",") if u.strip()]


def _invite(channel_id, user_ids, team=True):
    """Invite the default channel team plus these people (one call each: one bad id must not block the rest)."""
    ids = [u for u in dict.fromkeys((channel_team() if team else []) + list(user_ids or [])) if u]
    notes = []
    for uid in ids:
        try:
            slackui.slack("conversations.invite", channel=channel_id, users=uid)
        except slackui.SlackError as e:
            if not any(x in str(e) for x in ("already_in_channel", "cant_invite_self")):
                notes.append(f"couldn't invite <@{uid}> ({e})")
    return notes


def set_channel_members(channel_id, kick_others=True):
    """Make the channel's people exactly the default team (+ the bot): invite who's missing, remove the rest."""
    me = slackui.slack("auth.test").get("user_id")
    team = set(channel_team())
    current, cursor = set(), None
    while True:
        data = slackui.slack("conversations.members", channel=channel_id, limit=500, cursor=cursor)
        current |= set(data.get("members") or [])
        cursor = (data.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break
    notes = _invite(channel_id, sorted(team - current), team=False)
    removed = []
    if kick_others:
        for uid in sorted(current - team - {me}):
            try:
                slackui.slack("conversations.kick", channel=channel_id, user=uid)
                removed.append(uid)
            except slackui.SlackError as e:
                notes.append(f"couldn't remove <@{uid}> ({e})")
    return {"added": sorted(team - current), "removed": removed, "notes": notes}


def client_channel_members(conn, p):
    """Owners: set every client channel (or one) to exactly the default team."""
    from perpetual_core import guard
    if _requester() and _requester() not in guard.owners():
        raise ValueError("Only an owner can change who is in the client channels.")
    rows = agency.list_clients(conn)
    if p.get("handle"):
        rows = [c for c in rows if c["handle"] == agency.norm_handle(p["handle"])]
    out = []
    for c in rows:
        if c["slack_channel"]:
            r = set_channel_members(c["slack_channel"], kick_others=p.get("kick_others", True) is not False)
            out.append({"client": c["handle"], "channel": f"<#{c['slack_channel']}>", **r})
    return {"team": [f"<@{u}>" for u in channel_team()], "channels": out}


def apply_changes(summary):
    """Apply client changes in the background and DM the requester the outcome.

    The reconciler can take minutes on a small server (it registers each client's schedule), so it
    never runs inside the chat turn: a detached job runs it, DMs "✅ set up", restarts the gateway
    (channel routes are read at start) and DMs "🟢 back" once Agency Bot is live again."""
    subprocess.Popen(
        ["/opt/hermes/.venv/bin/python", "-m", "perpetual_core.jobs", "apply-changes", _requester() or "", summary],
        env={**os.environ, "PYTHONPATH": "/opt/perpetual/pylib"},
        start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return "setting up in the background; I'll DM you when it's live"


def client_add_linkedin(conn, p):
    """A LinkedIn client: on the roster with a #li-<name> channel, run by HQ (no X agent or X jobs)."""
    handle = re.sub(r"[^a-z0-9_]", "", agency.norm_handle(p.get("handle") or p.get("name") or ""))[:30]
    if not handle:
        raise ValueError("Give me a short id for the client (e.g. gilallouche) and their name.")
    if not p.get("name"):
        raise ValueError("What's the client's full name?")
    channel_id, channel_name, created = _resolve_channel(p.get("channel"), handle, "linkedin")
    notes = _invite(channel_id, [_requester()])
    agency.upsert_client(conn, handle, display_name=p["name"], niche_topics=p.get("niche_topics") or "",
                         timezone=p.get("timezone") or DEFAULT_TZ, slack_channel=channel_id)
    conn.execute("""UPDATE clients SET platform = 'linkedin', linkedin_url = COALESCE(%s, linkedin_url),
                    company = COALESCE(%s, company), radar_enabled = false, trend_enabled = false,
                    added_by = COALESCE(added_by, %s) WHERE handle = %s""",
                 (p.get("linkedin_url"), p.get("company"), _requester(), handle))
    conn.commit()
    return {"client": handle, "platform": "linkedin", "channel": f"<#{channel_id}>", "channel_created": created,
            "notes": notes, "next": "LinkedIn client added: HQ handles tasks, memory and reports for them in that channel."}


def client_add(conn, p):
    if (p.get("platform") or "x").lower() == "linkedin":
        return client_add_linkedin(conn, p)
    handle = agency.norm_handle(p.get("handle"))
    if not re.fullmatch(r"[a-z0-9_]{1,15}", handle):
        raise ValueError("Give me the client's X handle, e.g. @alexvacca.")
    if not p.get("niche_topics"):
        raise ValueError("What niche phrases should I watch for them? Specific ones work best, e.g. 'cold email, Clay GTM'.")
    _, profiles = console.run_actor("user-info", {"usernames": [handle]}, items_path="profiles")
    if not profiles:
        raise ValueError(f"I couldn't find @{handle} on X. Check the spelling.")
    x_name = profiles[0].get("display_name") or handle
    employee = None
    if p.get("employee"):   # optional: the whole team can work on any client
        employee, candidates = slackui.find_person(p["employee"])
        if not employee:
            names = ", ".join(c["real_name"] or c["name"] for c in candidates) or "nobody"
            raise ValueError(f"I couldn't pin down '{p['employee']}' in Slack (matches: {names}). Who exactly?")
    channel_id, channel_name, created = _resolve_channel(p.get("channel"), handle)
    taken = conn.execute("SELECT handle FROM clients WHERE slack_channel = %s AND handle <> %s AND active",
                         (channel_id, handle)).fetchone()
    if taken:
        raise ValueError(f"#{channel_name} already belongs to @{taken['handle']}. Pick another channel.")
    notes = _invite(channel_id, [employee and employee["id"], _requester()])
    console_status = agency.console_onboard(handle, p.get("name") or x_name)
    agency.upsert_client(conn, handle, display_name=p.get("name") or x_name, niche_topics=p["niche_topics"],
                         watch_accounts=p.get("watch_accounts"), avoid_accounts=p.get("avoid_accounts"),
                         timezone=p.get("timezone") or DEFAULT_TZ, slack_channel=channel_id,
                         employee_slack=employee and employee["id"])
    if _requester():
        conn.execute("UPDATE clients SET added_by = COALESCE(added_by, %s) WHERE handle = %s", (_requester(), handle))
        conn.commit()
    setup = apply_changes(f"@{handle} in #{channel_name}")
    return {
        "client": handle, "x_name": x_name, "channel": f"<#{channel_id}>", "channel_created": created,
        "employee": (employee["real_name"] or employee["name"]) if employee else "the whole team", "console": console_status, "setup": setup,
        "connect_x_link": slackui.connect_link(handle),
        "next": ("Tell the user it's being set up in the background and that they'll get a DM when it's "
                 "live (about a minute, then a ~20 second restart). The client's agent then says hello in the channel."),
        "notes": notes,
    }


def client_update(conn, p):
    handle = agency.norm_handle(p.get("handle"))
    current = agency.get_client(conn, handle)
    if not current:
        raise ValueError(f"@{handle} isn't a client.")
    fields = {k: p.get(k) for k in ("niche_topics", "watch_accounts", "avoid_accounts", "timezone")}
    if p.get("package"):
        pkg = p["package"] if isinstance(p["package"], dict) else json.loads(p["package"])
        conn.execute("UPDATE clients SET package = %s WHERE handle = %s", (json.dumps(pkg), handle))
        conn.commit()
    if p.get("name"):
        fields["display_name"] = p["name"]
    if p.get("employee"):
        employee, candidates = slackui.find_person(p["employee"])
        if not employee:
            raise ValueError(f"I couldn't pin down '{p['employee']}' in Slack. Who exactly?")
        fields["employee_slack"] = employee["id"]
        if current["slack_channel"]:
            _invite(current["slack_channel"], [employee["id"]])
    if p.get("channel"):
        channel_id, _, _ = _resolve_channel(p["channel"], handle)
        fields["slack_channel"] = channel_id
    agency.upsert_client(conn, handle, **fields)
    setup = apply_changes(f"the update to @{handle}")
    return {"client": handle, "updated": [k for k, v in fields.items() if v is not None], "setup": setup}


def client_channels(conn, p):
    """Give clients a Slack channel (create #x-<handle>, or use one the user names). Their jobs start
    once they have a channel."""
    if p.get("handle"):
        targets = [agency.get_client(conn, p["handle"])]
        if not targets[0]:
            raise ValueError(f"@{agency.norm_handle(p['handle'])} isn't a client.")
    else:
        targets = [c for c in agency.list_clients(conn) if not c["slack_channel"]]
    if not targets:
        return {"done": [], "note": "Every active client already has a channel."}
    done, failed = [], []
    for c in targets:
        try:
            channel_id, name, created = _resolve_channel(p.get("channel") if p.get("handle") else None, c["handle"],
                                                         c.get("platform") or "x")
            _invite(channel_id, [c["employee_slack"], _requester()])
            agency.upsert_client(conn, c["handle"], slack_channel=channel_id)
            if _requester():
                conn.execute("UPDATE clients SET added_by = COALESCE(added_by, %s) WHERE handle = %s",
                             (_requester(), c["handle"]))
                conn.commit()
            done.append({"client": c["handle"], "channel": f"<#{channel_id}>", "created": created})
        except Exception as e:  # noqa: BLE001 — report per client, keep going
            failed.append({"client": c["handle"], "error": str(e)})
    setup = apply_changes(f"{len(done)} new channel(s)") if done else ""
    return {"done": done, "failed": failed, "setup": setup,
            "next": ("Tell the user the channels exist now and the agents are being set up in the background; "
                     "they'll get a DM when everything is live.")}


def run_package(conn, p):
    """Produce the weekly package now (article + promo QRTs + long tweet + extras) for some or all clients."""
    wanted = [agency.norm_handle(h) for h in (p.get("clients") or []) if h]
    targets = [c for c in agency.active_clients(conn) if c["slack_channel"] and (not wanted or c["handle"] in wanted)]
    skipped = [h for h in wanted if h not in {c["handle"] for c in targets}]
    who = _requester()
    for c in targets:
        agency.add_request(conn, c["handle"], "Produce your weekly content package now: follow weekly-package.",
                           by=slackui.person_name(who) if who else "Agency Bot", requester_slack=who)
        if who:
            conn.execute("UPDATE clients SET added_by = COALESCE(added_by, %s) WHERE handle = %s", (who, c["handle"]))
    conn.commit()
    return {"queued": [c["handle"] for c in targets],
            "skipped_no_channel": skipped or [c["handle"] for c in agency.list_clients(conn) if not c["slack_channel"]][:20],
            "next": ("Each client's agent starts within a few minutes, one at a time on this server. Each publishes "
                     "its package as one document in its channel and DMs the requester the link.")}


def content_report(conn, p):
    """Everything written: articles, long tweets, QRTs (with the article they promote), video QRTs."""
    days = int(p.get("days") or 7)
    kinds = p.get("kinds") or ["article", "long_tweet", "qrt_promo", "video_qrt", "tweet", "thread", "quote"]
    q = ("""SELECT client, code, kind, status, created_at, decided_by, parent_client, parent_code,
                   left(COALESCE(final_text, text), 120) AS preview,
                   jsonb_array_length(COALESCE(refs, '[]'::jsonb)) AS n_refs,
                   factcheck->>'summary' AS factcheck, scores
            FROM suggestions WHERE created_at >= now() - make_interval(days => %s) AND kind = ANY(%s)""")
    args = [days, kinds]
    if p.get("client"):
        q += " AND client = %s"
        args.append(agency.norm_handle(p["client"]))
    q += " ORDER BY client, created_at DESC LIMIT 200"
    rows = []
    for r in conn.execute(q, args):
        rows.append({"client": r["client"], "code": r["code"], "kind": r["kind"], "status": r["status"],
                     "when": r["created_at"].date().isoformat(), "by": r["decided_by"],
                     "promotes": f"@{r['parent_client']} {r['parent_code']}" if r["parent_code"] else None,
                     "preview": r["preview"], "references": r["n_refs"], "factcheck": r["factcheck"],
                     "scores": r["scores"]})
    return {"window_days": days, "count": len(rows), "items": rows}


def client_remove(conn, p):
    handle = agency.norm_handle(p.get("handle"))
    if not p.get("confirmed"):
        raise ValueError(f"Ask the user to confirm removing @{handle} first, then call again with confirmed=true.")
    if not agency.set_client_active(conn, handle, False):
        raise ValueError(f"@{handle} isn't a client.")
    setup = apply_changes(f"removing @{handle}")
    return {"client": handle, "removed": True, "kept": "history, learned voice and playbook (re-adding restores them)",
            "channel": "left as is", "setup": setup}


def client_list(conn, p):
    out = []
    for c in agency.list_clients(conn, include_inactive=bool(p.get("include_inactive"))):
        conn_x = xpost.connection(conn, c["handle"])
        out.append({"client": c["handle"], "name": c["display_name"], "active": c["active"],
                    "channel": f"<#{c['slack_channel']}>" if c["slack_channel"] else None,
                    "run_by": f"<@{c['employee_slack']}>" if c["employee_slack"] else None,
                    "platform": c.get("platform") or "x", "company": c.get("company"),
                    "linkedin": c.get("linkedin_url"),
                    "niche": c["niche_topics"], "watching": c["watch_accounts"], "timezone": c["timezone"],
                    "x_connected": bool(conn_x)})
    return {"count": len(out), "clients": out}


def slack_people(conn, p):
    q = (p.get("query") or "").lower()
    return {"people": [{"id": x["id"], "name": x["real_name"] or x["name"]} for x in slackui.people()
                       if not q or q in (x["real_name"] or "").lower() or q in (x["name"] or "").lower()]}


def connect_x_link(conn, p):
    handle = agency.norm_handle(p.get("handle"))
    if not agency.get_client(conn, handle):
        raise ValueError(f"@{handle} isn't a client.")
    link = slackui.connect_link(handle)
    if not link:
        raise ValueError("The X connector page isn't configured (AGENCY_WEB_URL / INTERNAL_TOKEN).")
    return {"client": handle, "link": link, "note": "Open it while logged in to X as the client's account. Valid 7 days."}


_S = {"type": "string"}
TOOLS = [
    ("client_channel_members", client_channel_members,
     "Owners: make client channels contain exactly the default team (CLIENT_CHANNEL_MEMBERS: Sai, Naga, Shravya) "
     "plus the bot: invites who's missing and removes everyone else (kick_others=false to only add). "
     "Optional handle for one client; default all client channels.",
     {"handle": _S, "kick_others": {"type": "boolean"}}, []),
    ("client_add", client_add,
     "Add a new agency client. platform 'linkedin' (LinkedIn-only clients): needs handle (short id), name, "
     "optional linkedin_url, company; creates #li-<id>, HQ runs it, no X jobs. Default platform 'x': "
     "checks the X handle, adds it to the scraping "
     "console, creates (or uses) its public Slack channel #x-<handle>, invites the employee and the "
     "requester, and starts the client's own agent (it learns their voice and says hello in the "
     "channel). Required: handle and niche_topics (specific phrases). "
     "Ask for anything missing before calling. Optional: employee (who runs it; default the whole team), channel (an existing channel the user made "
     "and invited the bot to), watch_accounts, avoid_accounts, timezone (default Asia/Kolkata), name.",
     {"handle": _S, "employee": _S, "niche_topics": _S, "watch_accounts": _S, "avoid_accounts": _S,
      "platform": {"type": "string", "enum": ["x", "linkedin"]}, "linkedin_url": _S, "company": _S,
      "timezone": {**_S, "description": "IANA, e.g. Asia/Kolkata, Europe/London"}, "name": _S,
      "channel": {**_S, "description": "Existing channel (#name, name or ID). Omit to create #x-<handle>."}},
     ["handle"]),
    ("client_update", client_update,
     "Change a client's settings: niche_topics, watch_accounts, avoid_accounts, timezone, name, "
     "employee (who runs it), channel, or package (the weekly content package, e.g. "
     '{"article":1,"long_tweet":1,"article_qrts":true,"video_qrt":2}). Only pass what changes.',
     {"handle": _S, "employee": _S, "niche_topics": _S, "watch_accounts": _S, "avoid_accounts": _S,
      "timezone": _S, "name": _S, "channel": _S, "package": {"type": "object"}},
     ["handle"]),
    ("client_channels", client_channels,
     "Give clients their Slack channel. With no handle: create public #x-<handle> for EVERY active client "
     "that has no channel yet (e.g. imported clients). With a handle: that client only, optionally "
     "using an existing `channel` the user names. A client's scheduled work starts once it has a channel.",
     {"handle": _S, "channel": _S}, []),
    ("run_package", run_package,
     "Produce the weekly content package NOW: for each client 1 X article (with promo QRTs from same-team "
     "teammates and a self-QRT), 1 long tweet, plus extras like Mich's 2 video QRTs. Pass `clients` to "
     "limit it (handles), or omit for every client with a channel. Each piece is fact-checked, "
     "de-AI'd and delivered as a card with references.",
     {"clients": {"type": "array", "items": _S}}, []),
    ("content_report", content_report,
     "List what's been written: per client, each article, long tweet, QRT (and which article it promotes), "
     "video QRT, with status, who acted, fact-check summary, number of references and scores.",
     {"client": _S, "days": {"type": "integer"}, "kinds": {"type": "array", "items": _S}}, []),
    ("client_remove", client_remove,
     "Stop working on a client: their scheduled jobs stop; history, learned voice and playbook are "
     "kept. ALWAYS ask the user to confirm first, then call with confirmed=true.",
     {"handle": _S, "confirmed": {"type": "boolean"}}, ["handle"]),
    ("client_list", client_list,
     "All clients with their channel, who runs them, niche, timezone and whether X is connected.",
     {"include_inactive": {"type": "boolean"}}, []),
    ("slack_people", slack_people,
     "People in the Slack workspace (name and member ID), optionally filtered by a name.",
     {"query": _S}, []),
    ("connect_x_link", connect_x_link,
     "A link that lets a client's X account be connected so 'Post now' works. Must be opened while "
     "logged in to X as that client.",
     {"handle": _S}, ["handle"]),
]
