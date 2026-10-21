"""Client management by chat: add, update, list and remove the agency's clients.

Adding a client: validate the X handle, put it on the scraping console roster, resolve the
employee, create or reuse its public Slack channel, save it in Postgres, run the reconciler
(profile, skills, schedule, channel route), then restart the gateway so the new channel route
takes effect (routes are read once at gateway start).
"""

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


def _channel_name(handle):
    return ("x-" + re.sub(r"[^a-z0-9_-]", "", handle.lower()))[:80]


def _resolve_channel(given, handle):
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
                raise ValueError(f"I can't see a channel called #{ref}. Invite me to it first (/invite @Perpetual X).")
        if not info.get("is_member"):
            if info.get("is_private"):
                raise ValueError(f"#{info['name']} is private and I'm not in it. Type /invite @Perpetual X there first.")
            slackui.slack("conversations.join", channel=info["id"])
        return info["id"], info["name"], False
    name = _channel_name(handle)
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


def _invite(channel_id, user_ids):
    ids = [u for u in dict.fromkeys(user_ids) if u]
    if not ids:
        return []
    try:
        slackui.slack("conversations.invite", channel=channel_id, users=",".join(ids))
    except slackui.SlackError as e:
        if "already_in_channel" not in str(e) and "cant_invite_self" not in str(e):
            return [f"couldn't invite everyone ({e})"]
    return []


def apply_changes():
    """Run the reconciler now and restart the gateway shortly after, so the reply goes out first."""
    r = subprocess.run(RECONCILE, capture_output=True, text=True, timeout=900)
    if r.returncode != 0:
        raise RuntimeError("setup failed: " + (r.stderr or r.stdout)[-600:])
    # Channel routes are read once when the gateway starts. Restart it in the background,
    # after this turn has had time to reply.
    subprocess.Popen(["sh", "-c", "sleep 20; hermes gateway restart >/tmp/px-restart.log 2>&1"],
                     start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""


def client_add(conn, p):
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
    setup = apply_changes()
    return {
        "client": handle, "x_name": x_name, "channel": f"<#{channel_id}>", "channel_created": created,
        "employee": (employee["real_name"] or employee["name"]) if employee else "the whole team", "console": console_status, "setup": setup,
        "connect_x_link": slackui.connect_link(handle),
        "next": ("I'm restarting in ~20 seconds to switch the channel on. Within a few minutes the client's "
                 "agent studies their posts, learns the voice, and says hello in the channel."),
        "notes": notes,
    }


def client_update(conn, p):
    handle = agency.norm_handle(p.get("handle"))
    current = agency.get_client(conn, handle)
    if not current:
        raise ValueError(f"@{handle} isn't a client.")
    fields = {k: p.get(k) for k in ("niche_topics", "watch_accounts", "avoid_accounts", "timezone")}
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
    setup = apply_changes()
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
            channel_id, name, created = _resolve_channel(p.get("channel") if p.get("handle") else None, c["handle"])
            _invite(channel_id, [c["employee_slack"], _requester()])
            agency.upsert_client(conn, c["handle"], slack_channel=channel_id)
            done.append({"client": c["handle"], "channel": f"<#{channel_id}>", "created": created})
        except Exception as e:  # noqa: BLE001 — report per client, keep going
            failed.append({"client": c["handle"], "error": str(e)})
    setup = apply_changes() if done else ""
    return {"done": done, "failed": failed, "setup": setup,
            "next": "Restarting in ~20 seconds. Each client's agent then starts its schedule in its channel."}


def client_remove(conn, p):
    handle = agency.norm_handle(p.get("handle"))
    if not p.get("confirmed"):
        raise ValueError(f"Ask the user to confirm removing @{handle} first, then call again with confirmed=true.")
    if not agency.set_client_active(conn, handle, False):
        raise ValueError(f"@{handle} isn't a client.")
    setup = apply_changes()
    return {"client": handle, "removed": True, "kept": "history, learned voice and playbook (re-adding restores them)",
            "channel": "left as is", "setup": setup}


def client_list(conn, p):
    out = []
    for c in agency.list_clients(conn, include_inactive=bool(p.get("include_inactive"))):
        conn_x = xpost.connection(conn, c["handle"])
        out.append({"client": c["handle"], "name": c["display_name"], "active": c["active"],
                    "channel": f"<#{c['slack_channel']}>" if c["slack_channel"] else None,
                    "run_by": f"<@{c['employee_slack']}>" if c["employee_slack"] else None,
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
    ("client_add", client_add,
     "Add a new agency client and set everything up: checks the X handle, adds it to the scraping "
     "console, creates (or uses) its public Slack channel #x-<handle>, invites the employee and the "
     "requester, and starts the client's own agent (it learns their voice and says hello in the "
     "channel). Required: handle and niche_topics (specific phrases). "
     "Ask for anything missing before calling. Optional: employee (who runs it; default the whole team), channel (an existing channel the user made "
     "and invited the bot to), watch_accounts, avoid_accounts, timezone (default Asia/Kolkata), name.",
     {"handle": _S, "employee": _S, "niche_topics": _S, "watch_accounts": _S, "avoid_accounts": _S,
      "timezone": {**_S, "description": "IANA, e.g. Asia/Kolkata, Europe/London"}, "name": _S,
      "channel": {**_S, "description": "Existing channel (#name, name or ID). Omit to create #x-<handle>."}},
     ["handle", "niche_topics"]),
    ("client_update", client_update,
     "Change a client's settings: niche_topics, watch_accounts, avoid_accounts, timezone, name, "
     "employee (who runs it) or channel. Only pass what changes.",
     {"handle": _S, "employee": _S, "niche_topics": _S, "watch_accounts": _S, "avoid_accounts": _S,
      "timezone": _S, "name": _S, "channel": _S},
     ["handle"]),
    ("client_channels", client_channels,
     "Give clients their Slack channel. With no handle: create public #x-<handle> for EVERY active client "
     "that has no channel yet (e.g. imported clients). With a handle: that client only, optionally "
     "using an existing `channel` the user names. A client's scheduled work starts once it has a channel.",
     {"handle": _S, "channel": _S}, []),
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
