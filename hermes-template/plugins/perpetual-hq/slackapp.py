"""Slack interactivity for the whole agency: suggestion-card buttons, edit/reject pop-ups, the
Perpetual X Home tab (the dashboard) and the client details pop-up.

These live in the HQ plugin because the default profile owns the one Slack connection; card
buttons carry the client handle in their value, so one set of handlers serves every client.
"""

import asyncio
import json
import logging
import time

from perpetual_core import agency, ledger, slackui, xpost

log = logging.getLogger("perpetual.slack")
_home_window = {}       # user id -> days shown on their Home tab
_comp_cache = {}        # client -> (time, rows)


def _db(fn, *args, **kwargs):
    conn = ledger.connect()
    try:
        return fn(conn, *args, **kwargs)
    finally:
        conn.close()


def _who(body):
    user = body.get("user") or {}
    uid = user.get("id")
    try:
        return slackui.person_name(uid)
    except Exception:  # noqa: BLE001
        return user.get("name") or user.get("username") or uid


def _value(action):
    try:
        return json.loads(action.get("value") or action.get("selected_option", {}).get("value") or "{}")
    except ValueError:
        return {}


async def _run(fn, *args, **kwargs):
    return await asyncio.to_thread(fn, *args, **kwargs)


# ---------------------------------------------------------------- card buttons

def _feedback(conn, client, code, action, by, text=None, note=None):
    ledger.feedback(conn, client, code, action, text=text, note=note, by=by)
    slackui.refresh_card(conn, client, code)
    agency.console_pick(conn, client, code, action)   # drafts from the console's reply agent learn from this


async def on_approve(ack, body, action):
    await ack()
    v = _value(action)
    await _run(_db, _feedback, v["c"], v["k"], "approve", _who(body))


async def on_open(ack, body, action):
    await ack()          # URL buttons still send an action; nothing else to do


async def on_post(ack, body, action):
    await ack()
    v = _value(action)
    msg = await _run(xpost.post_and_report, v["c"], v["k"], _who(body), "slack")
    channel = (body.get("channel") or {}).get("id")
    if channel and not msg.startswith(("✅", "Already")):
        await _run(slackui.slack, "chat.postEphemeral", channel=channel, user=body["user"]["id"], text=msg)


async def on_edit(ack, body, action):
    await ack()
    v = _value(action)
    s = await _run(_db, lambda conn: dict(ledger.get(conn, v["c"], v["k"]) or {}))
    if s:
        await _run(slackui.slack, "views.open", trigger_id=body["trigger_id"], view=slackui.edit_modal(s))


async def on_reject(ack, body, action):
    await ack()
    v = _value(action)
    s = await _run(_db, lambda conn: dict(ledger.get(conn, v["c"], v["k"]) or {}))
    if s:
        await _run(slackui.slack, "views.open", trigger_id=body["trigger_id"], view=slackui.reject_modal(s))


async def on_edit_submit(ack, body, view):
    values = view["state"]["values"]
    parts = [values[k]["v"]["value"] or "" for k in sorted(values, key=lambda k: int(k.split("_")[-1]))
             if k.startswith("text_")]
    text = "\n\n".join(x.strip() for x in parts if x.strip())
    if not text.strip():
        await ack(response_action="errors", errors={"text_0": "Write the version you want, or press Cancel."})
        return
    await ack()
    meta = json.loads(view["private_metadata"])
    await _run(_db, _feedback, meta["c"], meta["k"], "edit", _who(body), text=text.strip())


async def on_reject_submit(ack, body, view):
    await ack()
    meta = json.loads(view["private_metadata"])
    note = (view["state"]["values"]["note"]["v"]["value"] or "").strip() or None
    await _run(_db, _feedback, meta["c"], meta["k"], "reject", _who(body), note=note)


async def on_article_posted(ack, body, action):
    await ack()
    v = _value(action)
    s = await _run(_db, lambda conn: dict(ledger.get(conn, v["c"], v["k"]) or {}))
    if s:
        await _run(slackui.slack, "views.open", trigger_id=body["trigger_id"], view=slackui.article_posted_modal(s))


def _link_article(conn, client, code, url, who):
    changed = ledger.link_article_posted(conn, client, code, url)
    conn.execute("UPDATE suggestions SET decided_by = COALESCE(decided_by, %s) WHERE client = %s AND code = %s",
                 (who, client, code))
    conn.commit()
    for c, k in [(client, code)] + changed:
        try:
            slackui.refresh_card(conn, c, k)
        except Exception:  # noqa: BLE001
            pass
    return changed


async def on_article_posted_submit(ack, body, view):
    url = (view["state"]["values"]["url"]["v"]["value"] or "").strip()
    if "/status/" not in url and "/i/article" not in url:
        await ack(response_action="errors", errors={"url": "Paste the x.com link of the published article."})
        return
    await ack()
    meta = json.loads(view["private_metadata"])
    await _run(_db, _link_article, meta["c"], meta["k"], url, _who(body))


# ---------------------------------------------------------------- Home tab (dashboard)

def _home_data(conn, days):
    data = agency.overview(conn, days=days)
    spark = {c["client"]: [r["views"] or 0 for r in agency.client_series(conn, c["client"], days=28)]
             for c in data["clients"]}
    viral = agency.viral(conn, days=1, limit=5)
    learning = conn.execute("SELECT client, at, summary FROM learning_log ORDER BY id DESC LIMIT 3").fetchall()
    doc = conn.execute("SELECT body FROM documents WHERE key = 'agency-playbook'").fetchone()
    version = None
    if doc:
        for line in doc["body"].splitlines():
            if line.startswith("version:"):
                version = "v" + line.split(":", 1)[1].strip()
                break
    handles = [c["handle"] for c in agency.active_clients(conn)]
    return slackui.home_view(data, days, spark, viral, learning, version, handles)


def publish_home(user_id, days=None):
    days = days or _home_window.get(user_id, 7)
    _home_window[user_id] = days
    view = _db(_home_data, days)
    slackui.slack("views.publish", user_id=user_id, view=view)


async def on_home_opened(event, body=None, **_):
    try:
        await _run(publish_home, event["user"])
    except Exception as e:  # noqa: BLE001
        log.error("home tab: %s", e, exc_info=True)


def _window_handler(days):
    async def handler(ack, body, action):
        await ack()
        await _run(publish_home, body["user"]["id"], int(action.get("value") or days))
    return handler


async def on_draft_for(ack, body, action):
    await ack()
    v = _value(action)
    who = _who(body)
    await _run(_db, agency.add_request, v["c"], f"Draft 3 replies and 2 quote tweets for {v['u']}", who)
    await _run(slackui.slack, "chat.postMessage", channel=body["user"]["id"],
               text=f"Sent to @{v['c']}'s agent. Drafts for {v['u']} will land in its channel within 15 minutes.")


async def on_view_playbook(ack, body, action):
    await ack()
    doc = await _run(_db, lambda conn: conn.execute(
        "SELECT body FROM documents WHERE key = 'agency-playbook'").fetchone())
    text = doc["body"].split("---", 2)[-1].strip() if doc else "Empty until the first Sunday review across clients."
    await _run(slackui.slack, "views.open", trigger_id=body["trigger_id"],
               view=slackui.text_modal("Agency playbook", text))


# ---------------------------------------------------------------- client details pop-up

def _client_parts(conn, handle):
    c = dict(agency.get_client(conn, handle))
    series = agency.client_series(conn, handle, days=28)
    top = conn.execute(
        """SELECT url, text, (latest->>'likes')::int AS likes, (latest->>'views')::bigint AS views FROM outcomes
           WHERE client = %s AND posted_at >= now() - interval '14 days'
           ORDER BY (latest->>'likes')::int DESC NULLS LAST LIMIT 5""", (handle,)).fetchall()
    counts = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, count(*) n FROM suggestions WHERE client = %s AND created_at >= now() - interval '7 days' GROUP BY 1",
        (handle,))}
    return c, series, top, counts, xpost.connection(conn, handle)


async def on_client_details(ack, body, action):
    await ack()
    handle = action["value"]
    c, series, top, counts, xconn = await _run(_db, _client_parts, handle)
    link = slackui.connect_link(handle)
    hit = _comp_cache.get(handle)
    cached = hit[1] if hit and time.time() - hit[0] < 6 * 3600 else None
    opened = await _run(slackui.slack, "views.open", trigger_id=body["trigger_id"],
                        view=slackui.client_modal(c, series, top, counts, xconn, cached, link))
    if cached is None:
        rows = await _run(agency.competitors, handle, c.get("watch_accounts"))
        _comp_cache[handle] = (time.time(), rows)
        await _run(slackui.slack, "views.update", view_id=opened["view"]["id"],
                   view=slackui.client_modal(c, series, top, counts, xconn, rows, link))


# ---------------------------------------------------------------- wiring

ACTIONS = [
    ("px_approve", on_approve), ("px_edit", on_edit), ("px_reject", on_reject), ("px_post", on_post),
    ("px_open", on_open), ("px_home_1", _window_handler(1)), ("px_home_7", _window_handler(7)),
    ("px_home_28", _window_handler(28)), ("px_home_refresh", _window_handler(7)),
    ("px_draft_for", on_draft_for), ("px_view_playbook", on_view_playbook),
    ("px_client_details", on_client_details), ("px_article_posted", on_article_posted),
]


def slack_factory(app, adapter):
    """Wire the Home tab and modal submissions onto Hermes' Slack app.

    Bolt runs only the FIRST listener that matches, and Hermes registers its own
    app_home_opened listener (it handles the Messages tab) before plugin factories run, so the
    Home-tab listener is moved to the front of the list. Its matcher only accepts tab == "home",
    so Hermes' Messages-tab flow is untouched."""
    async def _is_home(event):
        return event.get("tab") == "home"

    async def _home_listener(event, body):
        await on_home_opened(event, body)

    app.event("app_home_opened", matchers=[_is_home])(_home_listener)
    listeners = getattr(app, "_async_listeners", None)
    if isinstance(listeners, list) and listeners:
        listeners.insert(0, listeners.pop())
    app.view("px_edit_submit")(on_edit_submit)
    app.view("px_reject_submit")(on_reject_submit)
    app.view("px_article_posted_submit")(on_article_posted_submit)
    log.info("Perpetual X: Slack Home tab, modals and %d card actions wired", len(ACTIONS))
