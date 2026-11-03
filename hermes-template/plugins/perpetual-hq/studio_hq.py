"""Content studio for the main agent: queue a content-studio run on each client's own agent.

Like clients.run_package: one agency_request per client (kind 'studio'). Each client agent picks it
up, runs the content-studio skill (research, hooks, three writers, critics, editor) and publishes
one document in its channel.
"""

import json

from perpetual_core import agency, ledger


def _requester():
    try:
        from gateway.session_context import get_session_env
        return get_session_env("HERMES_SESSION_USER_ID") or None
    except Exception:  # noqa: BLE001 — not in a gateway turn (tests, CLI)
        return None


def _person(user_id):
    if not user_id:
        return "Agency Bot"
    try:
        from perpetual_core import slackui
        return slackui.person_name(user_id) or "Agency Bot"
    except Exception:  # noqa: BLE001 — Slack unreachable or not configured
        return "Agency Bot"


def queue_studio(conn, p, who=None):
    brief = (p.get("brief") or "").strip()
    if not brief:
        raise ValueError("What should they write about? Pass a brief.")
    kind = (p.get("kind") or "post").strip() or "post"
    try:
        count = max(1, min(int(p.get("count") or 3), 10))
    except (TypeError, ValueError):
        count = 3
    wanted = [agency.norm_handle(h) for h in (p.get("clients") or []) if h]
    active = agency.list_clients(conn)
    targets = [c for c in active if c["slack_channel"] and (not wanted or c["handle"] in wanted)]
    queued_handles = {c["handle"] for c in targets}
    if wanted:
        skipped = [h for h in wanted if h not in queued_handles]
    else:
        skipped = [c["handle"] for c in active if not c["slack_channel"]][:20]
    text = f"Follow content-studio: write {count} {kind} about {brief}. Publish one document."
    by = _person(who)
    for c in targets:
        agency.add_request(conn, c["handle"], text, by=by, requester_slack=who, kind="studio")
    return {"queued": [c["handle"] for c in targets], "skipped_no_channel": skipped, "request": text,
            "next": ("Each client's agent starts within a few minutes, one at a time on this server. Each runs "
                     "its writing team and publishes one document in its channel, then DMs the requester.")}


def run_studio(params, **kwargs):
    del kwargs
    try:
        conn = ledger.connect()
        try:
            out = queue_studio(conn, params or {}, who=_requester())
        finally:
            conn.close()
        return json.dumps({"success": True, **out}, ensure_ascii=False, default=str)
    except Exception as e:  # noqa: BLE001
        return json.dumps({"success": False, "error": str(e)})


_S = {"type": "string"}
TOOLS = [
    ("run_studio", run_studio,
     "Have clients' agents write content NOW through the content studio (a research, hook, writer and critic "
     "team; the best piece plus 2 alternates). Pass `brief` (what to write about), optional `kind` (post, "
     "thread, article, long_tweet, qrt, reply; default post), `count` (default 3) and `clients` (handles; "
     "omit for every active client with a channel). Each agent publishes one document in its channel.",
     {"clients": {"type": "array", "items": _S}, "brief": _S,
      "kind": {**_S, "description": "post, thread, article, long_tweet, qrt or reply. Default post."},
      "count": {"type": "integer", "description": "Pieces per client. Default 3."}},
     ["brief"]),
]


def register(ctx, toolset="studio"):
    for name, fn, description, props, required in TOOLS:
        ctx.register_tool(name=name, toolset=toolset, handler=fn,
                          schema={"name": name, "description": description,
                                  "parameters": {"type": "object", "properties": props, "required": required}})
