"""perpetual — the Perpetual X Bot plugin (one client per Hermes profile).

Toolsets:
  xconsole  read-only X data from the Thought Pilot scraping console (console.py)
  ledger    the shared suggestion ledger: save, feedback, outcomes, stats (ledger.py)

Slash command:
  /post <code>  human-only one-tap posting through the agency web app. The model has no
                posting tool at all; only a person typing /post can publish.
"""

import json
import os
import sys
import urllib.error
import urllib.request

# Shared code and psycopg live beside the image (/opt/perpetual/pylib), not in Hermes' venv.
for _p in ("/opt/perpetual/pylib", os.environ.get("PERPETUAL_PYLIB")):
    if _p and os.path.isdir(_p) and _p not in sys.path:
        sys.path.append(_p)

from perpetual_core import console, ledger  # noqa: E402
from perpetual_core.settings import client_handle, env, hermes_home  # noqa: E402


def _ok(**data):
    return json.dumps({"success": True, **data}, ensure_ascii=False, default=str)


def _with_db(fn):
    def handler(params, **kwargs):
        del kwargs
        try:
            conn = ledger.connect()
            try:
                return fn(conn, client_handle(), params or {})
            finally:
                conn.close()
        except Exception as e:  # noqa: BLE001 — surface every failure to the model
            return json.dumps({"success": False, "error": str(e)})
    return handler


def suggest_save(conn, client, p):
    return _ok(saved=ledger.save(conn, client, p.get("items") or [], batch=p.get("batch")))


def suggest_feedback(conn, client, p):
    return _ok(**ledger.feedback(conn, client, p["code"], p["action"], text=p.get("text"),
                                 note=p.get("note"), by=p.get("by")))


def suggest_list(conn, client, p):
    rows = ledger.list_suggestions(conn, client, status=p.get("status"), kind=p.get("kind"),
                                   days=p.get("days", 7), limit=min(int(p.get("limit", 30)), 100))
    return _ok(count=len(rows), suggestions=rows)


def targets_filter_new(conn, client, p):
    return _ok(new=ledger.filter_new_targets(conn, client, p.get("tweet_ids") or []))


def outcomes_sync(conn, client, p):
    days = p.get("days", 7)
    data = json.loads(console.x_client_content({"days": days, "refresh": p.get("refresh", True), "limit": 100}))
    if not data.get("success"):
        return json.dumps(data)
    return _ok(window_days=days, **ledger.record_outcomes(conn, client, data["posts"]))


def stats_query(conn, client, p):
    return _ok(**ledger.stats(conn, client, days=p.get("days", 30)))


def learning_log(conn, client, p):
    ledger.log_learning(conn, client, p["summary"], p.get("changes"))
    # Publish the current learned skills so the dashboard (another service) can show them.
    published = []
    for skill in ("playbook", "client-voice"):
        path = hermes_home() / "skills" / "perpetual-x" / skill / "SKILL.md"
        if path.exists():
            ledger.save_document(conn, f"{client}:{skill}", path.read_text())
            published.append(skill)
    return _ok(logged=True, published=published)


def agency_requests_take(conn, client, p):
    from perpetual_core import agency
    return _ok(requests=agency.take_requests(conn, client))


_STR = {"type": "string"}
_INT = {"type": "integer"}
_KIND = {"type": "string", "enum": sorted(ledger.PREFIX)}

LEDGER_TOOLS = [
    ("suggest_save", suggest_save,
     "Save suggestions to the ledger BEFORE showing them. Returns per item: a code (T=tweet, R=reply, "
     "Q=quote, H=thread, E=engage-back reply, P=repost), an open_in_x link, and one_tap_post. Show the "
     "code with every suggestion, and the open_in_x link under it. Where one_tap_post is true, add "
     "'or /post <code>'. Threads: separate tweets with a line containing only ---. Saving a "
     "reply/quote/repost also marks its target tweet as handled.",
     {"items": {"type": "array", "items": {"type": "object", "properties": {
         "kind": _KIND,
         "text": {**_STR, "description": "The exact copy suggested (empty for a repost)."},
         "target_id": {**_STR, "description": "Tweet id being replied to / quoted / reposted."},
         "target_url": _STR, "target_text": _STR, "target_by": _STR,
         "rationale": {**_STR, "description": "One line: why this should work."},
         "hook": {**_STR, "description": "Hook type, e.g. contrarian, number, story, question, how-to."},
         "format": {**_STR, "description": "e.g. one-liner, list, thread, long-form, quote-take."},
         "topic": {**_STR, "description": "Short topic label, reused consistently."}},
         "required": ["kind"]}},
      "batch": {**_STR, "description": "Which job produced these, e.g. reply-hunter."}},
     ["items"]),
    ("suggest_feedback", suggest_feedback,
     "Record feedback on a suggestion. action: approve | edit (pass the new version as text) | "
     "reject (pass the reason as note) | posted. Always pass `by` = the name of the person who said it.",
     {"code": {**_STR, "description": "e.g. T12, R4."},
      "action": {"type": "string", "enum": ["approve", "edit", "reject", "posted"]},
      "text": _STR, "note": _STR, "by": {**_STR, "description": "Who gave the feedback."}},
     ["code", "action"]),
    ("suggest_list", suggest_list,
     "List recent suggestions for this client, optionally filtered by status or kind.",
     {"status": {"type": "string", "enum": ["pending", "approved", "edited", "rejected", "posted", "expired"]},
      "kind": _KIND, "days": _INT, "limit": _INT},
     []),
    ("targets_filter_new", targets_filter_new,
     "Given candidate tweet ids, return only those not already handled, so the same tweet is never suggested twice.",
     {"tweet_ids": {"type": "array", "items": _STR}},
     ["tweet_ids"]),
    ("outcomes_sync", outcomes_sync,
     "Re-scrape the client's recent posts, store their metrics, and link posts that came from our "
     "suggestions. Run daily before reporting or learning.",
     {"days": _INT, "refresh": {"type": "boolean"}},
     []),
    ("stats_query", stats_query,
     "Learning data for this client: acceptance and rejection by kind/hook/format/topic, average likes "
     "when posted, own post performance, recent rejection reasons and edits.",
     {"days": _INT},
     []),
    ("learning_log", learning_log,
     "Record what a learning review changed (summary plus list of changes).",
     {"summary": _STR, "changes": {"type": "array", "items": _STR}},
     ["summary"]),
    ("agency_requests_take", agency_requests_take,
     "Take this client's pending requests from the agency dashboard (e.g. 'draft a quote for <url>'). "
     "Each request is returned once; handle every one.",
     {},
     []),
]


# ---------------------------------------------------------------- /post (human only)

def _post_via_web(client, code, by):
    url = (env("AGENCY_WEB_URL") or "").rstrip("/")
    token = env("INTERNAL_TOKEN")
    if not url or not token:
        raise RuntimeError("AGENCY_WEB_URL / INTERNAL_TOKEN not configured")
    req = urllib.request.Request(
        url + "/internal/post", method="POST",
        data=json.dumps({"client": client, "code": code, "by": by, "via": "slack"}).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                 "User-Agent": "perpetual-x-bot/1.0"})
    try:
        # Generous timeout: a free-plan dashboard may need ~60 s to wake from sleep.
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except ValueError:
            return {"ok": False, "error": f"web app returned HTTP {e.code}"}


def handle_post(raw_args, source=None, **_):
    if source is None or not getattr(source, "user_id", None):
        return "Posting only works from Slack, by a person."  # fail closed
    code = (raw_args or "").strip().split(" ")[0].upper()
    if not code:
        return "Usage: /post T3  — posts suggestion T3 to X (use the edited version if there is one)."
    by = getattr(source, "user_name", None) or source.user_id
    client = client_handle()
    try:
        result = _post_via_web(client, code, by)
    except Exception as e:  # noqa: BLE001
        return f"Couldn't post {code}: {e}"
    if result.get("ok"):
        return f"✅ Posted {code} to @{client}: {result.get('url')}"
    if result.get("intent_url"):
        return f"{code} can't go out through the API ({result.get('error')}). Tap to post it yourself: {result['intent_url']}"
    return f"Couldn't post {code}: {result.get('error')}"


def register(ctx):
    console.register(ctx, toolset="xconsole")
    for name, fn, description, props, required in LEDGER_TOOLS:
        ctx.register_tool(
            name=name,
            toolset="ledger",
            schema={"name": name, "description": description,
                    "parameters": {"type": "object", "properties": props, "required": required}},
            handler=_with_db(fn),
        )
    ctx.register_command("post", handler=handle_post,
                         description="Post a suggestion to X now, e.g. /post T3", args_hint="<code>")
