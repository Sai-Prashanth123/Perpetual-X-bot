"""perpetual — the Perpetual X Bot plugin (one client per Hermes profile).

Toolsets:
  xconsole  read-only X data from the Thought Pilot scraping console (console.py)
  ledger    the shared suggestion ledger: save, feedback, outcomes, stats (ledger.py)

Answers are published as documents (a Slack canvas, or a tidy thread when canvases aren't
available) with one short summary message in the client's channel. A single quick draft can still
be posted as an interactive card (Approve / Edit / Reject / Post now / Open in X); the buttons are
handled by the perpetual-hq plugin, which owns the Slack connection.

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
    if p.get("cards") is False:
        return _ok(saved=saved, cards_posted=0,
                   note="Saved without cards. Put each code (and its Open in X link) in the document you publish.")
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
    from perpetual_core import agency
    agency.console_pick(conn, client, res["code"], p["action"])
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
               note="Teammates' agents pick these up within a few minutes and publish the promo QRTs in their channels.")


def notify_team(conn, client, p):
    """DM a milestone to whoever added this client (or the owners)."""
    from perpetual_core import notify
    sent = notify.dm(notify.client_watchers(conn, client), f"@{client}: {p['text']}")
    return _ok(sent_to=sent)


def publish_document(conn, client, p):
    """One answer = one document in the client's channel (canvas, or thread fallback) + a summary line."""
    from perpetual_core import notify
    res = slackui.publish_doc(conn, client, p["title"], p["markdown"], p["summary"],
                              kind=p.get("kind") or "answer", asked_by=p.get("asked_by_slack"))
    dm_to = [u for u in (p.get("dm_to") or []) if u]
    if dm_to:
        where = res.get("link") or f"<#{res['channel']}>"
        notify.dm(dm_to, f"✅ @{client}: *{p['title']}* is ready → {where}\n{p['summary'][:300]}")
    note = ("Published. Do NOT repeat the document in your reply: reply with one short line at most "
            "(or nothing, in a scheduled job, the summary message is already in the channel).")
    return _ok(**res, dm_sent_to=dm_to, instructions=note)


def research_memory(conn, client, p):
    from perpetual_core import research
    if p.get("action") == "add":
        row = research.add(conn, client, p.get("kind") or "note", p.get("title"), p.get("body"), p.get("sources"))
        return _ok(saved=row)
    return _ok(findings=research.recent(conn, client, days=p.get("days") or 30, kind=p.get("kind"),
                                        limit=p.get("limit") or 20),
               recent_documents=research.recent_docs(conn, client))


def console_reply_drafts(conn, client, p):
    """Reply options the scraping console's own reply agent prepared for this client (read only)."""
    roster = {r["username"].lower(): r["username"] for r in (console._request("GET", "/reply/clients") or {}).get("clients", [])}
    if client not in roster:
        return _ok(drafts=[], note="This client isn't on the console's reply-agent roster.")
    out = []
    for d in console.reply_drafts(roster[client], limit=p.get("limit") or 15):
        src = d.get("source") or {}
        out.append({"draft_id": d["id"], "target_url": src.get("url"), "target_by": src.get("author"),
                    "target_text": (src.get("text") or "")[:400],
                    "options": [o["text"] for o in d.get("options") or [] if o.get("safe", True) and o.get("text")]})
    return _ok(drafts=out, note="Treat these as raw material: rewrite in the client's voice and run content-pipeline.")


def agency_requests_take(conn, client, p):
    from perpetual_core import agency
    return _ok(requests=agency.take_requests(conn, client))


# ---------------------------------------------------------------- v6: radar, post-mortems, leads, calendar

def send_radar_alert(conn, client, p):
    res = slackui.post_alert(conn, client, p["headline"], p.get("tweet_url"), p.get("tweet_text"), p["codes"])
    return _ok(**res, instructions="Alert sent. Reply with exactly [SILENT] if this was a scheduled hand-off.")


def radar_accounts(conn, client, p):
    from perpetual_core import radar
    if p.get("enabled") is not None:
        conn.execute("UPDATE clients SET radar_enabled = %s WHERE handle = %s", (bool(p["enabled"]), client))
        conn.commit()
    if p.get("add") or p.get("remove"):
        accts = radar.set_accounts(conn, client, p.get("add") or [], p.get("remove") or [])
    else:
        row = conn.execute("SELECT radar_accounts, watch_accounts FROM clients WHERE handle = %s", (client,)).fetchone()
        accts = radar.handles(row["radar_accounts"]) or radar.handles(row["watch_accounts"])
    on = conn.execute("SELECT radar_enabled FROM clients WHERE handle = %s", (client,)).fetchone()["radar_enabled"]
    return _ok(enabled=on, accounts=accts, alerts_today=radar.alerts_today(conn, client), daily_cap=radar.DAILY_CAP)


def postmortems(conn, client, p):
    from perpetual_core import postmortem
    if p.get("action") == "judge":
        postmortem.judge(conn, client, p["tweet_id"], p["verdict"], p.get("why"), p.get("lesson"))
        return _ok(judged=p["tweet_id"])
    if p.get("action") == "recent":
        return _ok(postmortems=postmortem.recent(conn, client, days=p.get("days") or 14))
    return _ok(open=postmortem.open_rows(conn, client))


def leads_tool(conn, client, p):
    from perpetual_core import leads
    action = p.get("action") or "list"
    if action == "known":
        return _ok(already_listed=leads.known(conn, client, p.get("handles") or []))
    if action == "save":
        return _ok(saved=leads.save(conn, client, p.get("items") or []))
    if action == "update":
        return _ok(updated=leads.update(conn, client, p["code"], p["status"], p.get("note")))
    return _ok(leads=leads.listing(conn, client, status=p.get("status"), days=p.get("days") or 60))


def client_icp(conn, client, p):
    if p.get("icp"):
        conn.execute("UPDATE clients SET icp = %s WHERE handle = %s", (p["icp"].strip(), client))
        conn.commit()
    row = conn.execute("SELECT icp FROM clients WHERE handle = %s", (client,)).fetchone()
    return _ok(icp=row["icp"], note=None if row["icp"] else "No ICP yet: write one from receipts and save it.")


def calendar_tool(conn, client, p):
    from perpetual_core import planner
    action = p.get("action") or "week"
    if action == "save":
        return _ok(planned=planner.save(conn, client, p.get("slots") or []))
    if action == "approve":
        return _ok(approved=planner.approve(conn, client, p.get("codes")))
    return _ok(slots=planner.week(conn, client, days=p.get("days") or 8))


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
      "batch": {**_STR, "description": "Which job produced these, e.g. reply-hunter."},
      "cards": {"type": "boolean", "description": "false = save to the ledger only (the drafts go in a document "
                                                  "you publish with publish_document). Default true = one card each."}},
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
    ("publish_document", publish_document,
     "Publish your answer as ONE readable document in the client's Slack channel: a canvas (or, if "
     "canvases are unavailable, a tidy thread) plus a short summary message with an Open document button. "
     "Use it for every substantial answer: reply packs, content packages, research, briefs, reports. "
     "Write `markdown` with # / ## headings, bullets, > quotes and [text](url) links; no tables.",
     {"title": {**_STR, "description": "e.g. 'Replies for: The 70-Signal Timing System (Oct 21)'."},
      "markdown": {**_STR, "description": "The full document (follow doc-templates)."},
      "summary": {**_STR, "description": "2-4 short lines for the channel: what's inside and what to do first."},
      "kind": {"type": "string", "enum": ["reply-pack", "package", "brief", "research", "report", "answer"]},
      "asked_by_slack": {**_STR, "description": "Slack user id of whoever asked (from the request), if known."},
      "dm_to": {"type": "array", "items": _STR, "description": "Slack user ids to DM the link to (the requester)."}},
     ["title", "markdown", "summary"]),
    ("research_memory", research_memory,
     "Your research notebook about this client. action=read (default): recent findings (performance, "
     "competitors, niche, growth, content) and recently published documents. Read it BEFORE answering "
     "anything strategic. action=add: save one finding with its sources (URLs, tweet links, numbers).",
     {"action": {"type": "string", "enum": ["read", "add"]},
      "kind": {"type": "string", "enum": ["performance", "competitors", "niche", "growth", "content", "note"]},
      "title": _STR, "body": {**_STR, "description": "The finding, specific, with numbers."},
      "sources": {"type": "array", "items": _STR}, "days": _INT, "limit": _INT},
     []),
    ("console_reply_drafts", console_reply_drafts,
     "Reply options the scraping console's own reply agent already prepared for this client (target tweet "
     "+ options). Raw material for reply work; nothing is posted or shown until you publish.",
     {"limit": _INT}, []),
    ("send_radar_alert", send_radar_alert,
     "Early-reply radar: after saving 2 replies (cards false) to a fresh post from a radar account, send ONE "
     "compact alert to the channel: headline like '⚡ Early-reply window: @nick posted 12 min ago', the tweet, "
     "and the reply codes with Open in X buttons.",
     {"headline": _STR, "tweet_url": _STR, "tweet_text": _STR, "codes": {"type": "array", "items": _STR}},
     ["headline", "codes"]),
    ("radar_accounts", radar_accounts,
     "The big accounts the early-reply radar watches for this client (checked every 10 minutes, max 4 alerts "
     "a day). No arguments = list. add / remove handles; enabled true/false turns the radar on or off.",
     {"add": {"type": "array", "items": _STR}, "remove": {"type": "array", "items": _STR},
      "enabled": {"type": "boolean"}}, []),
    ("postmortems", postmortems,
     "Post-mortems of the client's own posts at 48h, vs the client's 30-day median for that format. "
     "action=open (default): posts waiting for a verdict, with metrics, baseline and ratios. action=judge: "
     "tweet_id, verdict (win/par/flop), why (specific: hook, topic, timing, format, length), lesson (what to "
     "repeat or avoid). action=recent: judged ones (for the brief, report and learning review).",
     {"action": {"type": "string", "enum": ["open", "judge", "recent"]}, "tweet_id": _STR,
      "verdict": {"type": "string", "enum": ["win", "par", "flop"]}, "why": _STR, "lesson": _STR, "days": _INT},
     []),
    ("leads", leads_tool,
     "Lead finder storage. action=known: which handles are already on the list. action=save: items "
     "[{handle, name, bio, followers, role_company, said, said_url, fit_score 1-10, why, opener}] → codes L1… "
     "action=update: code + status (contacted/replied/won/not_a_fit) + note. action=list (default).",
     {"action": {"type": "string", "enum": ["list", "known", "save", "update"]},
      "handles": {"type": "array", "items": _STR}, "items": {"type": "array", "items": {"type": "object"}},
      "code": _STR, "status": _STR, "note": _STR, "days": _INT}, []),
    ("client_icp", client_icp,
     "The client's ideal customer profile (who buys from them: roles, company types/sizes, problems). "
     "Read it before scoring leads; pass icp to set or refine it.",
     {"icp": _STR}, []),
    ("content_calendar", calendar_tool,
     "The client's content calendar. action=save: slots [{code, slot_at ISO time WITH timezone, note}] for "
     "drafts already saved. action=approve: codes, or none to approve every planned slot ('ok week'). "
     "action=week (default): the next days' slots. Approved slots get a reminder in the channel at slot time.",
     {"action": {"type": "string", "enum": ["week", "save", "approve"]},
      "slots": {"type": "array", "items": {"type": "object"}}, "codes": {"type": "array", "items": _STR},
      "days": _INT}, []),
    ("agency_requests_take", agency_requests_take,
     "Take this client's pending requests (handed over by Perpetual or a teammate's agent). Each comes "
     "with by_name and requester_slack: pass requester_slack as asked_by_slack and in dm_to when you "
     "publish the result. Each request is returned once; handle every one.",
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
    from perpetual_core import workspace
    workspace.register(ctx)          # the asking person's own Google / Notion, if they connected it
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
