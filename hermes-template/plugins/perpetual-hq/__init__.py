"""perpetual-hq — the Main X agent (the default Hermes profile, Perpetual X in Slack DMs).

* Agency tools: scorecards, team activity, viral feed, learning, the shared agency playbook.
* Client management by chat: add / update / remove / list clients (clients.py).
* All Slack interactivity, because this profile owns the Slack connection: suggestion-card
  buttons, edit/reject pop-ups, the Home tab dashboard and client details (slackapp.py).
* /post from any client channel.
"""

import json
import os
import sys

for _p in ("/opt/perpetual/pylib", os.environ.get("PERPETUAL_PYLIB")):
    if _p and os.path.isdir(_p) and _p not in sys.path:
        sys.path.append(_p)

from perpetual_core import agency, console, ledger, xpost  # noqa: E402

from . import clients, slackapp  # noqa: E402
from perpetual_core.settings import hermes_home, profile_slug  # noqa: E402

AGENCY_PLAYBOOK_HEADER = """---
name: agency-playbook
description: "Cross-client patterns that work on X for this agency's clients — hooks, formats, timing, reply targets — with evidence. Shared read-only with every client bot. Written by the Main X agent; never contains any client's voice."
version: {version}
metadata:
  hermes:
    tags: [perpetual-x, agency, learned]
---

"""


def _ok(**data):
    return json.dumps({"success": True, **data}, ensure_ascii=False, default=str)


def _db(fn):
    def handler(params, **kwargs):
        del kwargs
        try:
            conn = ledger.connect()
            try:
                return fn(conn, params or {})
            finally:
                conn.close()
        except Exception as e:  # noqa: BLE001
            return json.dumps({"success": False, "error": str(e)})
    return handler


def _client_arg(conn, p):
    handle = (p.get("client") or "").lstrip("@").lower()
    if not handle:
        raise ValueError("client is required")
    if not conn.execute("SELECT 1 FROM clients WHERE handle = %s", (handle,)).fetchone():
        raise ValueError(f"@{handle} is not an agency client")
    return handle


def agency_overview(conn, p):
    return _ok(**agency.overview(conn, days=p.get("days", 7)))


def agency_stats(conn, p):
    return _ok(**ledger.stats(conn, None, days=p.get("days", 30)))


def viral_feed(conn, p):
    rows = agency.viral(conn, days=p.get("days", 1), niche=p.get("niche"), limit=min(p.get("limit", 25), 60))
    return _ok(count=len(rows), tweets=rows)


def client_detail(conn, p):
    handle = _client_arg(conn, p)
    which = p.get("include") or ["series", "stats", "learning"]
    out = {"client": handle}
    if "series" in which:
        out["daily"] = agency.client_series(conn, handle, days=p.get("days", 28))
    if "stats" in which:
        out["stats"] = ledger.stats(conn, handle, days=p.get("days", 28))
    if "learning" in which:
        out["learning_log"] = [agency._jsonable(r) for r in conn.execute(
            "SELECT at, summary, changes FROM learning_log WHERE client = %s ORDER BY id DESC LIMIT 5", (handle,))]
    for skill in ("playbook", "client-voice"):
        if skill in which:
            path = hermes_home() / "profiles" / profile_slug(handle) / "skills" / "perpetual-x" / skill / "SKILL.md"
            out[skill] = path.read_text() if path.exists() else None
    return _ok(**out)


def agency_playbook_write(conn, p):
    body = (p.get("body") or "").strip()
    if len(body) < 200:
        raise ValueError("body looks too short to be a full playbook; write the complete document")
    path = hermes_home() / "shared-skills" / "agency-playbook" / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    version = p.get("version") or "1.0.0"
    path.write_text(AGENCY_PLAYBOOK_HEADER.format(version=version) + body + "\n")
    ledger.save_document(conn, "agency-playbook", path.read_text())
    ledger.log_learning(conn, "_agency", p.get("summary") or f"agency playbook v{version}", p.get("changes"))
    return _ok(written=str(path), version=version)


def delegate_to_client(conn, p):
    """Hand client work to that client's own agent (its voice, research memory and history live there)."""
    handle = _client_arg(conn, p)
    row = conn.execute("SELECT slack_channel FROM clients WHERE handle = %s", (handle,)).fetchone()
    if not row or not row["slack_channel"]:
        raise ValueError(f"@{handle} has no channel yet, so its agent isn't running. "
                         "Create it first (client_channels), then ask again.")
    requester = clients._requester()
    rid = agency.add_request(conn, handle, p["request"], by=p.get("by") or "Perpetual", requester_slack=requester)
    return _ok(request_id=rid, channel=row["slack_channel"],
               note=(f"@{handle}'s agent picks this up within ~2 minutes and publishes the result as a document "
                     f"in <#{row['slack_channel']}>; the requester gets the link by DM. Tell them that in one line; "
                     "don't attempt the work yourself."))


def agency_postmortems(conn, p):
    from perpetual_core import postmortem
    client = _client_arg(conn, p) if p.get("client") else None
    return _ok(postmortems=postmortem.recent(conn, client, days=p.get("days") or 14, verdict=p.get("verdict")))


def agency_leads(conn, p):
    from perpetual_core import leads
    client = _client_arg(conn, p) if p.get("client") else None
    return _ok(leads=leads.listing(conn, client, status=p.get("status"), days=p.get("days") or 30))


def radar_control(conn, p):
    from perpetual_core import radar
    handle = _client_arg(conn, p)
    if p.get("enabled") is not None:
        conn.execute("UPDATE clients SET radar_enabled = %s WHERE handle = %s", (bool(p["enabled"]), handle))
        conn.commit()
    accts = radar.set_accounts(conn, handle, p.get("add") or [], p.get("remove") or [])
    on = conn.execute("SELECT radar_enabled FROM clients WHERE handle = %s", (handle,)).fetchone()["radar_enabled"]
    return _ok(client=handle, enabled=on, accounts=accts, alerts_today=radar.alerts_today(conn, handle))


_STR = {"type": "string"}
_INT = {"type": "integer"}

HQ_TOOLS = [
    ("agency_postmortems", agency_postmortems,
     "Post-mortems of posted content (48h after posting, vs each client's own median): verdict win/par/flop, "
     "why, lesson. Filter by client and verdict. Use for 'what flopped this week and why?'.",
     {"client": _STR, "verdict": {"type": "string", "enum": ["win", "par", "flop"]}, "days": _INT}, []),
    ("agency_leads", agency_leads,
     "Leads found in clients' comments and quotes (people who look like buyers), with fit score, why and "
     "status. Filter by client and status.",
     {"client": _STR, "status": _STR, "days": _INT}, []),
    ("radar_control", radar_control,
     "Early-reply radar for a client: list, add or remove the big accounts it watches, or turn it on/off.",
     {"client": _STR, "add": {"type": "array", "items": _STR}, "remove": {"type": "array", "items": _STR},
      "enabled": {"type": "boolean"}}, ["client"]),
    ("agency_overview", agency_overview,
     "Scorecard for every client over a window: followers and change, posts, likes, views, bookmarks, "
     "change vs the previous window, engagement rate, suggestion use, attention flags — plus per-employee "
     "activity (actions, approvals, edits, rejections, posts, median minutes to act).",
     {"days": {**_INT, "description": "Window in days (default 7)."}}, []),
    ("agency_stats", agency_stats,
     "Learning data across ALL clients: suggestion acceptance by kind/hook/format/topic, average likes "
     "when posted, rejection reasons and edits, per client.",
     {"days": _INT}, []),
    ("viral_feed", viral_feed,
     "What's going viral in the agency's niches (stored daily): top tweets by likes with likes/hour. "
     "niche '_meta' is the console's cross-niche meta.",
     {"days": _INT, "niche": _STR, "limit": _INT}, []),
    ("client_detail", client_detail,
     "Deep view of one client: daily series, learning stats, recent learning log, and optionally their "
     "current playbook / client-voice skill text.",
     {"client": _STR, "days": _INT,
      "include": {"type": "array", "items": {"type": "string",
                  "enum": ["series", "stats", "learning", "playbook", "client-voice"]}}},
     ["client"]),
    ("agency_playbook_write", agency_playbook_write,
     "Replace the shared agency-playbook skill (read by every client bot). Pass the full markdown body "
     "(no frontmatter), a semver version, a one-line summary and the list of changes. Patterns only — "
     "never copy any client's voice or private details into it.",
     {"body": _STR, "version": _STR, "summary": _STR, "changes": {"type": "array", "items": _STR}},
     ["body", "version"]),
    ("delegate_to_client", delegate_to_client,
     "Hand any client-specific work to that client's own agent: replies for their latest article, research "
     "on what's working, this week's package, ideas, a review of their profile. Write the request exactly as "
     "the person asked, plus any detail they gave (URLs, angles, deadlines). Result: a document in the "
     "client's channel and a DM with the link to whoever asked.",
     {"client": _STR, "request": _STR, "by": {**_STR, "description": "Name of the person asking."}},
     ["client", "request"]),
]


def handle_post(raw_args, source=None, **_):
    """/post typed in a client channel: the channel tells us which client."""
    if source is None or not getattr(source, "user_id", None):
        return "Posting only works from Slack, by a person."
    code = (raw_args or "").strip().split(" ")[0].upper()
    if not code:
        return "Usage: /post T3 in the client's channel."
    conn = ledger.connect()
    try:
        row = conn.execute("SELECT handle FROM clients WHERE slack_channel = %s AND active",
                           (getattr(source, "chat_id", None),)).fetchone()
    finally:
        conn.close()
    if not row:
        return "Use /post in a client's channel, e.g. /post T3 in #x-<client>."
    by = getattr(source, "user_name", None) or source.user_id
    return xpost.post_and_report(row["handle"], code, by, via="slack")


def register(ctx):
    console.register(ctx, toolset="xconsole")
    for name, fn, description, props, required in HQ_TOOLS:
        ctx.register_tool(
            name=name,
            toolset="agency",
            schema={"name": name, "description": description,
                    "parameters": {"type": "object", "properties": props, "required": required}},
            handler=_db(fn),
        )
    for name, fn, description, props, required in clients.TOOLS:
        ctx.register_tool(
            name=name,
            toolset="clients",
            schema={"name": name, "description": description,
                    "parameters": {"type": "object", "properties": props, "required": required}},
            handler=_db(lambda conn, p, _fn=fn: _ok(**_fn(conn, p))),
        )
    for action_id, callback in slackapp.ACTIONS:
        ctx.register_slack_action_handler(action_id, callback)
    ctx.register_platform_handler("slack", slackapp.slack_factory)
    ctx.register_command("post", handler=handle_post,
                         description="Post a suggestion to X now, e.g. /post T3", args_hint="<code>")
