"""perpetual — the Perpetual X Bot plugin (one client per Hermes profile).

Toolsets:
  xconsole  read-only X data from the Thought Pilot scraping console (console.py)
  ledger    the shared suggestion ledger: save, feedback, outcomes, stats (ledger.py)

Every saved suggestion is posted to the client's Slack channel as an interactive card
(Approve / Edit / Reject / Post now / Open in X). The buttons are handled by the perpetual-hq
plugin, which owns the Slack connection.

Slash command:
  /post <code>  human-only posting to X. The model has no posting tool at all; only a person
                clicking Post now or typing /post can publish.
"""

import json
import os
import sys

# Shared code and psycopg live beside the image (/opt/perpetual/pylib), not in Hermes' venv.
for _p in ("/opt/perpetual/pylib", os.environ.get("PERPETUAL_PYLIB")):
    if _p and os.path.isdir(_p) and _p not in sys.path:
        sys.path.append(_p)

from perpetual_core import console, ledger, slackui, xpost  # noqa: E402
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
    saved = ledger.save(conn, client, p.get("items") or [], batch=p.get("batch"))
    try:
        cards = slackui.post_cards(conn, client, [s["code"] for s in saved])
    except Exception as e:  # noqa: BLE001 — cards are a convenience; the ledger row is what matters
        cards, note = 0, f"cards not posted: {e}"
    else:
        if cards:
            note = (f"{cards} card(s) posted to the client's channel with buttons. Do NOT repeat the drafts "
                    "in your reply; just write a 1-2 line header for this batch.")
        else:
            note = "No client channel set, so no cards were posted: show the drafts in your reply."
    return _ok(saved=saved, cards_posted=cards, note=note)


def suggest_feedback(conn, client, p):
    res = ledger.feedback(conn, client, p["code"], p["action"], text=p.get("text"),
                          note=p.get("note"), by=p.get("by"))
    try:
        slackui.refresh_card(conn, client, res["code"])
    except Exception:  # noqa: BLE001
        pass
    return _ok(**res)


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


def _voice_file(name):
    path = hermes_home() / "skills" / "perpetual-x" / "client-voice" / "references" / name
    return path if path.exists() else None


def _real_posts(days=90, limit=60):
    data = json.loads(console.x_client_content({"days": days, "limit": limit}))
    return [p for p in data.get("posts", []) if not p.get("is_reply")] if data.get("success") else []


def lint_draft(conn, client, p):
    from perpetual_core import quality
    cfg_path = _voice_file("lint-config.json")
    cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path else None
    return _ok(**quality.lint(p["text"], cfg))


def fact_check(conn, client, p):
    from perpetual_core import quality
    receipts = _voice_file("receipts.md")
    bank = _voice_file("claim-bank.json")
    res = quality.fact_check(p["text"], receipts.read_text(encoding="utf-8") if receipts else "",
                             json.loads(bank.read_text(encoding="utf-8")) if bank else None, _real_posts())
    res["rule"] = ("Remove or [placeholder] every unsourced claim, never ship a do_not_say item, and eyeball "
                   "'check' names. Pass `references` from this result to suggest_save.")
    return _ok(**res)


def blind_lineup(conn, client, p):
    from perpetual_core import quality
    posts = [x["text"] for x in sorted(_real_posts(), key=lambda x: -(x.get("likes") or 0))
             if len(x.get("text") or "") > 60][:5]
    if len(posts) < 3:
        return _ok(**{"pass": True, "skipped": "not enough real posts to compare against"})
    instructions, body, answer = quality.lineup_prompt(p["text"], posts)
    ctx = _CTX.get("ctx")
    if not ctx or not getattr(ctx, "llm", None):
        return _ok(**{"pass": True, "skipped": "no LLM access for the judge"})
    res = ctx.llm.complete_structured(instructions=instructions, input=[{"type": "text", "text": body}],
                                      json_schema=quality.LINEUP_SCHEMA, purpose="perpetual.blind-lineup",
                                      temperature=0.0, max_tokens=400)
    return _ok(**quality.lineup_verdict(res.parsed or {}, answer))


def content_history(conn, client, p):
    return _ok(recent=ledger.recent_content(conn, client, days=p.get("days", 45)))


def article_posted(conn, client, p):
    changed = ledger.link_article_posted(conn, client, p["code"], p["url"])
    for c, code in [(client, p["code"].upper())] + changed:
        try:
            slackui.refresh_card(conn, c, code)
        except Exception:  # noqa: BLE001
            pass
    return _ok(article=p["code"].upper(), promo_cards_updated=[f"@{c} {k}" for c, k in changed])


def request_article_qrts(conn, client, p):
    """Ask each same-team teammate (and this client, for a self-QRT) to write a promo QRT."""
    from perpetual_core import agency
    code = p["code"].strip().upper()
    art = ledger.get(conn, client, code)
    if not art or art["kind"] != "article":
        raise ValueError(f"{code} isn't an article for @{client}")
    title = ((art.get("final_text") or art["text"]).strip().splitlines() or [""])[0][:140]
    mates = agency.teammates(conn, client)
    asked = []
    for who in mates + [client]:
        relation = "your own article (self-QRT)" if who == client else f"your teammate @{client}'s article"
        agency.add_request(conn, who, (
            f"Write 1 article-promo QRT (kind qrt_promo) for {relation} {code}: \"{title}\". "
            f"Follow article-qrt. When saving, set parent_client='{client}' and parent_code='{code}'. "
            f"Article text: {(art.get('final_text') or art['text'])[:2500]}"), by=client)
        asked.append(who)
    return _ok(article=code, requested_from=asked,
               note="Teammates' agents pick these up within 15 minutes; promo cards land in their channels.")


def notify_team(conn, client, p):
    """DM a milestone to whoever added this client (or the owners)."""
    from perpetual_core import notify
    sent = notify.dm(notify.client_watchers(conn, client), f"@{client}: {p['text']}")
    return _ok(sent_to=sent)


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
         "topic": {**_STR, "description": "Short topic label, reused consistently."},
         "references": {"type": "array", "items": _STR, "description": "Sources for every claim (from fact_check)."},
         "factcheck": {"type": "object", "description": "fact_check result summary + claims."},
         "scores": {"type": "object", "description": "e.g. {anti_ai, hook, voice, algo, lineup}."},
         "parent_client": {**_STR, "description": "For qrt_promo: whose article it promotes."},
         "parent_code": {**_STR, "description": "For qrt_promo: the article's code, e.g. A3."}},
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
    ("lint_draft", lint_draft,
     "Run the agency's AI-pattern linters on a draft (em dashes, AI-tell words, not-X-but-Y, tricolons, "
     "rhythm, the client's banned terms). Fix every HARD hit before showing anything.",
     {"text": _STR, "format": _STR}, ["text"]),
    ("fact_check", fact_check,
     "Trace every number and named entity in a draft to the client's receipts, claim bank or real posts. "
     "Returns verified / unsourced / do_not_say per claim plus the references list for the card.",
     {"text": _STR}, ["text"]),
    ("blind_lineup", blind_lineup,
     "Judge 0: hide the draft among 5 of the client's real top posts and ask an independent judge to spot "
     "the AI one. pass=false means it reads as AI: rewrite before shipping.",
     {"text": _STR}, ["text"]),
    ("content_history", content_history,
     "What this client already published or was given recently (incl. imported history), to avoid repeats.",
     {"days": _INT}, []),
    ("article_posted", article_posted,
     "Mark an article as live with its x.com URL; every teammate's promo QRT for it gets a working Open in X.",
     {"code": _STR, "url": _STR}, ["code", "url"]),
    ("request_article_qrts", request_article_qrts,
     "After saving an article: ask same-team teammates' agents (and this client, self-QRT) to write promo QRTs.",
     {"code": _STR}, ["code"]),
    ("notify_team", notify_team,
     "DM a short milestone to the person who added this client (or the agency owners): onboarding done, "
     "this week's package delivered with codes, a problem that needs a human. One or two sentences.",
     {"text": _STR},
     ["text"]),
    ("agency_requests_take", agency_requests_take,
     "Take this client's pending requests from the agency dashboard (e.g. 'draft a quote for <url>'). "
     "Each request is returned once; handle every one.",
     {},
     []),
]


# ---------------------------------------------------------------- /post (human only)

def handle_post(raw_args, source=None, **_):
    if source is None or not getattr(source, "user_id", None):
        return "Posting only works from Slack, by a person."  # fail closed
    code = (raw_args or "").strip().split(" ")[0].upper()
    if not code:
        return "Usage: /post T3  — posts suggestion T3 to X (the edited version if there is one)."
    by = getattr(source, "user_name", None) or source.user_id
    return xpost.post_and_report(client_handle(), code, by, via="slack")


_CTX = {}


def register(ctx):
    _CTX["ctx"] = ctx
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
