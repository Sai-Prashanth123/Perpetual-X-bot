"""Direct-message receipts: tell the person who asked (or the owners) what happened."""

from .settings import env


def owners():
    return [u.strip() for u in (env("OWNER_SLACK_IDS") or "").split(",") if u.strip()]


def dm(user_ids, text):
    """Send `text` as a DM to each user id; never raises (a missed receipt must not break work)."""
    from . import slackui
    sent = 0
    for uid in dict.fromkeys(u for u in (user_ids or []) if u):
        try:
            slackui.slack("chat.postMessage", channel=uid, text=text, unfurl_links=False)
            sent += 1
        except Exception:  # noqa: BLE001
            pass
    return sent


def client_watchers(conn, client):
    """Who hears about a client's milestones: whoever added it, else the owners."""
    row = conn.execute("SELECT added_by FROM clients WHERE handle = %s", (client,)).fetchone()
    return [row["added_by"]] if row and row["added_by"] else owners()
