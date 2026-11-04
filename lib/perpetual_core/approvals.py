"""Approvals queue: anything client-facing, money, contracts, post scheduling or deletions waits for an
owner's Approve / Edit / Reject click in Slack, then runs by itself.

The agent calls request_approval instead of acting. Every owner gets a DM card with the full text
(the exact email, message or invite). The first owner to click decides: the approval is claimed
atomically, so a double click or two owners clicking at once runs the action exactly once. Every
owner's card then shows the outcome and who decided, and the requester gets a DM.

New kinds (Typefully, Stripe, DocuSign…) are one register_executor(kind, fn) call.
"""

import asyncio
import base64
import json
import logging
import uuid
from email.message import EmailMessage

from psycopg.types.json import Json

from . import connections, guard, ledger, notify, slackui, workspace

log = logging.getLogger("perpetual.approvals")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS approvals (
    id            serial PRIMARY KEY,
    kind          text NOT NULL,
    summary       text,
    payload       jsonb NOT NULL DEFAULT '{}',
    preview       text,
    requested_by  text,
    requested_for text,                       -- whose account runs it: a Slack id or 'agency'
    client        text,
    status        text NOT NULL DEFAULT 'pending',  -- pending|approved|rejected|executed|failed|cancelled
    decided_by    text,
    decided_at    timestamptz,
    result        jsonb,
    error         text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    approver_msg  jsonb NOT NULL DEFAULT '[]'  -- [{"channel", "ts"}] of each owner's card
);
CREATE INDEX IF NOT EXISTS approvals_status ON approvals (status, created_at);
"""

G = "https://www.googleapis.com"

# ---------------------------------------------------------------- executors

EXECUTORS, TEXT_FIELD, VERB, REQUIRED, EMOJI = {}, {}, {}, {}, {}


def register_executor(kind, fn, text_field=None, verb="Done", required=(), emoji="📌"):
    """fn(conn, approval_row) -> result dict; raise to mark the approval failed.
    text_field: the payload key the Edit button rewrites (None = no Edit button)."""
    EXECUTORS[kind], TEXT_FIELD[kind], VERB[kind], REQUIRED[kind], EMOJI[kind] = fn, text_field, verb, required, emoji


def _google(conn, row):
    return connections.token(conn, row["requested_for"] or connections.AGENCY, "google")


def _addr(v):
    return ", ".join(v) if isinstance(v, list) else v


def exec_email(conn, row):
    p = row["payload"]
    m = EmailMessage()
    m["To"] = _addr(p["to"])
    if p.get("cc"):
        m["Cc"] = _addr(p["cc"])
    m["Subject"] = p.get("subject") or ""
    m.set_content(p["body"])
    body = {"raw": base64.urlsafe_b64encode(m.as_bytes()).decode()}
    if p.get("thread_id"):
        body["threadId"] = p["thread_id"]
    r = workspace._call("POST", f"{G}/gmail/v1/users/me/messages/send", _google(conn, row), body)
    return {"id": r.get("id"), "thread_id": r.get("threadId")}


def exec_slack_message(conn, row):
    p = row["payload"]
    r = slackui.slack("chat.postMessage", channel=p["channel"], text=p["text"], unfurl_links=False,
                      **({"thread_ts": p["thread_ts"]} if p.get("thread_ts") else {}))
    return {"channel": r.get("channel"), "ts": r.get("ts")}


def exec_calendar_invite(conn, row):
    p = row["payload"]
    tz = {"timeZone": p["timezone"]} if p.get("timezone") else {}
    body = {"summary": p["title"], "description": p.get("description"),
            "start": {"dateTime": p["start"], **tz}, "end": {"dateTime": p["end"], **tz},
            "attendees": [{"email": a} for a in p.get("attendees") or []]}
    params = {"sendUpdates": "all"}
    if p.get("with_meet"):
        body["conferenceData"] = {"createRequest": {"requestId": uuid.uuid4().hex,
                                                    "conferenceSolutionKey": {"type": "hangoutsMeet"}}}
        params["conferenceDataVersion"] = 1
    e = workspace._call("POST", f"{G}/calendar/v3/calendars/primary/events", _google(conn, row), body, params=params)
    return {"id": e.get("id"), "link": e.get("htmlLink"), "meet": e.get("hangoutLink")}


def exec_calendar_delete(conn, row):
    eid = row["payload"]["event_id"]
    workspace._call("DELETE", f"{G}/calendar/v3/calendars/primary/events/{eid}", _google(conn, row),
                    params={"sendUpdates": "all"})
    return {"deleted": eid}


def exec_note(conn, row):
    return {"noted": True}


register_executor("email", exec_email, "body", "Sent", ("to", "body"), "✉️")
register_executor("slack_message", exec_slack_message, "text", "Posted", ("channel", "text"), "💬")
register_executor("calendar_invite", exec_calendar_invite, "description", "Invite sent", ("title", "start", "end"), "📅")
register_executor("calendar_delete", exec_calendar_delete, None, "Deleted", ("event_id",), "🗑️")
register_executor("note", exec_note, None, "Approved", (), "📝")


def render_preview(kind, p):
    """The exact thing that will go out, for the card (used when the agent gives no preview)."""
    if kind == "email":
        head = [f"To: {_addr(p.get('to'))}"] + ([f"Cc: {_addr(p['cc'])}"] if p.get("cc") else [])
        return "\n".join(head + [f"Subject: {p.get('subject') or ''}", "", p.get("body") or ""])
    if kind == "slack_message":
        return f"To <#{p.get('channel')}>:\n\n{p.get('text') or ''}"
    if kind == "calendar_invite":
        return "\n".join([f"{p.get('title')}", f"{p.get('start')} → {p.get('end')} {p.get('timezone') or ''}".strip(),
                          f"Guests: {', '.join(p.get('attendees') or []) or 'none'}"
                          + (" · with Google Meet" if p.get("with_meet") else ""), "", p.get("description") or ""])
    if kind == "calendar_delete":
        return f"Delete calendar event {p.get('event_id')} (guests are notified)." + (
            f"\n{p['title']}" if p.get("title") else "")
    return json.dumps(p, ensure_ascii=False, indent=1) if p else ""


# ---------------------------------------------------------------- cards

def _name(uid):
    if not uid:
        return "someone"
    try:
        return slackui.person_name(uid)
    except Exception:  # noqa: BLE001
        return uid


def outcome(row):
    who = _name(row["decided_by"])
    s = row["status"]
    if s == "executed":
        return f"✅ {VERB.get(row['kind'], 'Done')} by {who}"
    if s == "failed":
        return f"⚠️ Approved by {who}, but it failed: {row['error']}"
    if s == "rejected":
        return f"🗑️ Rejected by {who}" + (f": {row['error']}" if row["error"] else "")
    if s == "cancelled":
        return f"↩️ Cancelled by {who}"
    if s == "approved":
        return f"⏳ Approved by {who}, running…"
    return "⏳ Waiting for an owner"


def _chunks(text, size=2900):
    text = text or "(empty)"
    return [text[i:i + size] for i in range(0, len(text), size)][:20]


def card_blocks(row):
    kind = row["kind"]
    head = f"{EMOJI.get(kind, '📌')} {kind.replace('_', ' ').title()} approval #{row['id']}" + (
        f" · @{row['client']}" if row["client"] else "")
    blocks = [{"type": "header", "text": {"type": "plain_text", "text": head[:150]}},
              {"type": "section", "text": {"type": "mrkdwn", "text": f"*{row['summary'] or kind}*"}},
              {"type": "context", "elements": [{"type": "mrkdwn", "text":
                  f"Asked by {_name(row['requested_by'])} · runs as "
                  + ("the agency account" if row["requested_for"] in (None, connections.AGENCY)
                     else _name(row["requested_for"]))}]}]
    blocks += [{"type": "section", "text": {"type": "plain_text", "text": c}} for c in _chunks(row["preview"])]
    if row["status"] == "pending":
        val = json.dumps({"id": row["id"]})
        btns = [{"type": "button", "action_id": "px_apr_ok", "style": "primary", "value": val,
                 "text": {"type": "plain_text", "text": "Approve"}}]
        if TEXT_FIELD.get(kind):
            btns.append({"type": "button", "action_id": "px_apr_edit", "value": val,
                         "text": {"type": "plain_text", "text": "Edit"}})
        btns.append({"type": "button", "action_id": "px_apr_no", "style": "danger", "value": val,
                     "text": {"type": "plain_text", "text": "Reject"}})
        blocks.append({"type": "actions", "elements": btns})
    else:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": outcome(row)[:2900]}]})
    return blocks


def _fallback(row):
    return f"Approval #{row['id']} ({row['kind']}): {row['summary'] or ''} · {outcome(row)}"


def _refresh_cards(row):
    for m in row["approver_msg"] or []:
        try:
            slackui.slack("chat.update", channel=m["channel"], ts=m["ts"], text=_fallback(row), blocks=card_blocks(row))
        except Exception as e:  # noqa: BLE001 — a stale card must not undo the decision
            log.warning("approval card update failed: %s", e)


# ---------------------------------------------------------------- queue

def get(conn, approval_id):
    return conn.execute("SELECT * FROM approvals WHERE id = %s", (int(approval_id),)).fetchone()


def request(conn, kind, summary, payload, preview, requested_by, requested_for, client=None):
    if kind not in EXECUTORS:
        raise ValueError(f"unknown approval kind {kind!r}; choose {', '.join(EXECUTORS)}")
    payload = dict(payload or {})
    missing = [k for k in REQUIRED[kind] if not payload.get(k)]
    if missing:
        raise ValueError(f"{kind} needs {', '.join(missing)} in payload")
    client = (client or "").lstrip("@").lower() or None
    row = conn.execute(
        """INSERT INTO approvals(kind, summary, payload, preview, requested_by, requested_for, client)
           VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
        (kind, summary, Json(payload), preview or render_preview(kind, payload), requested_by,
         requested_for or connections.AGENCY, client)).fetchone()
    conn.commit()
    msgs = []
    for owner in notify.owners():
        try:
            r = slackui.slack("chat.postMessage", channel=owner, text=f"Needs your approval: {_fallback(row)}",
                              blocks=card_blocks(row), unfurl_links=False)
            msgs.append({"channel": r.get("channel") or owner, "ts": r.get("ts")})
        except Exception as e:  # noqa: BLE001
            log.warning("approval card to %s failed: %s", owner, e)
    row = conn.execute("UPDATE approvals SET approver_msg = %s WHERE id = %s RETURNING *",
                       (Json(msgs), row["id"])).fetchone()
    conn.commit()
    return row


def _handled(row):
    return f"Approval #{row['id']} is already handled: {outcome(row)}."


def decide(conn, approval_id, action, by, edited_text=None, note=None):
    """action approve|edit|reject. Returns (row, message). Only owners; only pending approvals."""
    if action not in ("approve", "edit", "reject"):
        raise ValueError("action must be approve, edit or reject")
    if not by or by not in guard.owners():
        raise PermissionError("Only an agency owner can approve, edit or reject this.")
    row = get(conn, approval_id)
    if not row:
        raise ValueError(f"no approval #{approval_id}")
    if row["status"] != "pending":
        return row, _handled(row)
    sets, vals = ["decided_by = %s", "decided_at = now()"], [by]
    if action == "reject":
        sets += ["status = 'rejected'", "error = %s"]
        vals.append(note)
    else:
        sets.append("status = 'approved'")
        if action == "edit":
            field = TEXT_FIELD.get(row["kind"])
            if not field or not (edited_text or "").strip():
                raise ValueError(f"nothing to edit on a {row['kind']} approval")
            payload = {**row["payload"], field: edited_text.strip()}
            sets += ["payload = %s", "preview = %s"]
            vals += [Json(payload), render_preview(row["kind"], payload)]
    # The claim: only one click can move it out of pending.
    claimed = conn.execute(f"UPDATE approvals SET {', '.join(sets)} WHERE id = %s AND status = 'pending' RETURNING *",
                           (*vals, row["id"])).fetchone()
    conn.commit()
    if not claimed:
        return get(conn, row["id"]), _handled(get(conn, row["id"]))
    row = claimed
    if action != "reject":
        try:
            result = EXECUTORS[row["kind"]](conn, row)
            row = conn.execute("UPDATE approvals SET status = 'executed', result = %s WHERE id = %s RETURNING *",
                               (Json(result or {}), row["id"])).fetchone()
        except Exception as e:  # noqa: BLE001
            conn.rollback()
            log.error("approval #%s (%s) failed: %s", row["id"], row["kind"], e)
            row = conn.execute("UPDATE approvals SET status = 'failed', error = %s WHERE id = %s RETURNING *",
                               (str(e)[:1000], row["id"])).fetchone()
        conn.commit()
    _refresh_cards(row)
    msg = f"Approval #{row['id']} ({row['summary'] or row['kind']}): {outcome(row)}."
    if row["requested_by"] and row["requested_by"] != by:
        notify.dm([row["requested_by"]], msg)
    return row, msg


def cancel(conn, approval_id, by):
    row = get(conn, approval_id)
    if not row:
        raise ValueError(f"no approval #{approval_id}")
    if by != row["requested_by"] and by not in guard.owners():
        raise PermissionError("Only whoever asked for it, or an owner, can cancel this approval.")
    claimed = conn.execute("""UPDATE approvals SET status = 'cancelled', decided_by = %s, decided_at = now()
                              WHERE id = %s AND status = 'pending' RETURNING *""", (by, row["id"])).fetchone()
    conn.commit()
    if not claimed:
        return row, _handled(row)
    _refresh_cards(claimed)
    return claimed, f"Approval #{row['id']} cancelled."


def listing(conn, status="pending", limit=50):
    if status in (None, "", "all"):
        return conn.execute("SELECT * FROM approvals ORDER BY id DESC LIMIT %s", (limit,)).fetchall()
    return conn.execute("SELECT * FROM approvals WHERE status = %s ORDER BY id DESC LIMIT %s",
                        (status, limit)).fetchall()


def view(r):
    return {"id": r["id"], "kind": r["kind"], "summary": r["summary"], "client": r["client"], "status": r["status"],
            "requested_by": r["requested_by"], "runs_as": r["requested_for"], "decided_by": r["decided_by"],
            "outcome": outcome(r), "result": r["result"], "error": r["error"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None}


# ---------------------------------------------------------------- Slack handlers

def _db(fn, *args, **kwargs):
    conn = ledger.connect()
    try:
        return fn(conn, *args, **kwargs)
    finally:
        conn.close()


async def _run(fn, *args, **kwargs):
    return await asyncio.to_thread(fn, *args, **kwargs)


def _id(action):
    try:
        return int(json.loads(action.get("value") or "{}")["id"])
    except (ValueError, KeyError, TypeError):
        return None


def _uid(body):
    return (body.get("user") or {}).get("id")


async def _tell(body, text, channel=None):
    """Only the clicker sees it."""
    user = _uid(body)
    channel = channel or (body.get("channel") or {}).get("id") or (body.get("container") or {}).get("channel_id")
    if not user:
        return
    try:
        if channel:
            await _run(slackui.slack, "chat.postEphemeral", channel=channel, user=user, text=text)
        else:
            await _run(slackui.slack, "chat.postMessage", channel=user, text=text)
    except Exception as e:  # noqa: BLE001
        log.warning("approval feedback failed: %s", e)


NOT_OWNER = "Only agency owners can approve, edit or reject this."


async def _decide_and_tell(body, aid, action, channel=None, **kw):
    try:
        _, msg = await _run(_db, decide, aid, action, _uid(body), **kw)
        await _tell(body, msg, channel)
    except PermissionError:
        await _tell(body, NOT_OWNER, channel)
    except Exception as e:  # noqa: BLE001
        log.error("approval %s %s failed: %s", action, aid, e, exc_info=True)
        await _tell(body, f"⚠️ Couldn't {action} approval #{aid}: {e}", channel)


async def on_apr_ok(ack, body, action):
    await ack()
    await _decide_and_tell(body, _id(action), "approve")


async def _open_modal(body, action, build):
    """Check owner + pending first, so nobody types into a modal that can't be used."""
    aid = _id(action)
    if _uid(body) not in guard.owners():
        return await _tell(body, NOT_OWNER)
    row = await _run(_db, get, aid)
    if not row:
        return await _tell(body, f"Approval #{aid} no longer exists.")
    if row["status"] != "pending":
        return await _tell(body, _handled(row))
    ch = (body.get("channel") or {}).get("id") or (body.get("container") or {}).get("channel_id")
    await _run(slackui.slack, "views.open", trigger_id=body["trigger_id"], view=build(row, ch))


def edit_modal(row, channel=None):
    field = TEXT_FIELD[row["kind"]]
    return {"type": "modal", "callback_id": "px_apr_edit_submit",
            "private_metadata": json.dumps({"id": row["id"], "ch": channel}),
            "title": {"type": "plain_text", "text": f"Edit #{row['id']}"[:24]},
            "submit": {"type": "plain_text", "text": "Approve edit"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": [{"type": "input", "block_id": "text",
                        "label": {"type": "plain_text", "text": f"Final {field} (runs as soon as you save)"},
                        "element": {"type": "plain_text_input", "action_id": "v", "multiline": True,
                                    "initial_value": (row["payload"].get(field) or "")[:3000]}}]}


def reject_modal(row, channel=None):
    return {"type": "modal", "callback_id": "px_apr_no_submit",
            "private_metadata": json.dumps({"id": row["id"], "ch": channel}),
            "title": {"type": "plain_text", "text": f"Reject #{row['id']}"[:24]},
            "submit": {"type": "plain_text", "text": "Reject"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": [{"type": "input", "block_id": "note", "optional": True,
                        "label": {"type": "plain_text", "text": "Why? (the requester sees this)"},
                        "element": {"type": "plain_text_input", "action_id": "v"}}]}


async def on_apr_edit(ack, body, action):
    await ack()
    await _open_modal(body, action, edit_modal)


async def on_apr_no(ack, body, action):
    await ack()
    await _open_modal(body, action, reject_modal)


async def on_apr_edit_submit(ack, body, view):
    text = (view["state"]["values"]["text"]["v"]["value"] or "").strip()
    if not text:
        await ack(response_action="errors", errors={"text": "Write the final version, or press Cancel."})
        return
    await ack()
    meta = json.loads(view["private_metadata"])
    await _decide_and_tell(body, meta["id"], "edit", meta.get("ch"), edited_text=text)


async def on_apr_no_submit(ack, body, view):
    await ack()
    meta = json.loads(view["private_metadata"])
    note = (view["state"]["values"]["note"]["v"]["value"] or "").strip() or None
    await _decide_and_tell(body, meta["id"], "reject", meta.get("ch"), note=note)


ACTIONS = [("px_apr_ok", on_apr_ok), ("px_apr_edit", on_apr_edit), ("px_apr_no", on_apr_no)]
VIEWS = [("px_apr_edit_submit", on_apr_edit_submit), ("px_apr_no_submit", on_apr_no_submit)]


# ---------------------------------------------------------------- tools

def _ok(**d):
    return json.dumps({"success": True, **d}, ensure_ascii=False, default=str)


def _err(e):
    return json.dumps({"success": False, "error": str(e)})


def request_approval(params, **_):
    asker = connections.current_user()
    conn = ledger.connect()
    try:
        runs_as = asker if asker and connections.get_row(conn, asker, "google") else connections.AGENCY
        payload = params.get("payload") or {}
        if isinstance(payload, str):
            payload = json.loads(payload)
        row = request(conn, params["kind"], params.get("summary"), payload, params.get("preview"),
                      asker, runs_as, params.get("client"))
        return _ok(approval=view(row), owners_asked=len(row["approver_msg"]),
                   note=f"Approval #{row['id']} is waiting for an owner. It runs by itself when approved; "
                        "tell the person, and don't do it another way meanwhile.")
    except Exception as e:  # noqa: BLE001
        return _err(e)
    finally:
        conn.close()


def approvals_list(params, **_):
    conn = ledger.connect()
    try:
        rows = listing(conn, params.get("status") or "pending", min(params.get("limit") or 50, 200))
        return _ok(count=len(rows), approvals=[view(r) for r in rows])
    except Exception as e:  # noqa: BLE001
        return _err(e)
    finally:
        conn.close()


def approval_cancel(params, **_):
    conn = ledger.connect()
    try:
        row, msg = cancel(conn, params["id"], connections.current_user())
        return _ok(approval=view(row), message=msg)
    except Exception as e:  # noqa: BLE001
        return _err(e)
    finally:
        conn.close()


_S = {"type": "string"}

TOOLS = [
    ("request_approval", request_approval,
     "Queue something for an owner's Approve / Edit / Reject in Slack; it runs by itself once approved. REQUIRED "
     "for anything client-facing, money, contracts, post scheduling or deletions. kind + payload: "
     "email {to, cc?, subject, body, thread_id?}; slack_message {channel, text, thread_ts?}; calendar_invite "
     "{title, start, end, timezone?, attendees[], description?, with_meet?}; calendar_delete {event_id, title?}; "
     "note {} (a decision a human carries out, e.g. 'send the contract'). preview = the exact text that will go "
     "out (built from payload if omitted). summary = one line. client = handle if it's for a client.",
     {"kind": {"type": "string", "enum": list(EXECUTORS)}, "summary": _S, "payload": {"type": "object"},
      "preview": _S, "client": _S}, ["kind", "summary", "payload"]),
    ("approvals_list", approvals_list,
     "List approvals: status pending (default), executed, failed, rejected, cancelled or all.",
     {"status": _S, "limit": {"type": "integer"}}, []),
    ("approval_cancel", approval_cancel,
     "Cancel a pending approval by id (whoever asked for it, or an owner).", {"id": {"type": "integer"}}, ["id"]),
]


def register(ctx, toolset="approvals"):
    for name, fn, description, props, required in TOOLS:
        ctx.register_tool(name=name, toolset=toolset, handler=fn,
                          schema={"name": name, "description": description,
                                  "parameters": {"type": "object", "properties": props, "required": required}})
