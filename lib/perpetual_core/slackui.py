"""Everything the team sees in Slack: suggestion cards with buttons, the Perpetual X Home tab
(the agency dashboard), and the pop-up modals. Pure Block Kit builders plus a tiny Web API client;
the Hermes plugins wire them to Slack events.
"""

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlencode

from .settings import env

KIND_LABEL = {"article": "X Article", "long_tweet": "Long tweet", "qrt_promo": "Article promo QRT",
              "video_qrt": "Video QRT", "tweet": "Tweet", "thread": "Thread", "reply": "Reply", "quote": "Quote tweet",
              "repost": "Repost", "engage": "Reply to an engager"}
STATUS_LABEL = {"pending": "Waiting for review", "approved": "Approved", "edited": "Edited",
                "rejected": "Rejected", "posted": "Posted", "expired": "Expired (not used in 4 days)"}
SPARK = "▁▂▃▄▅▆▇█"


# ---------------------------------------------------------------- Web API

class SlackError(RuntimeError):
    pass


FORM_METHODS = {"chat.getPermalink", "conversations.replies", "conversations.history", "conversations.info",
                "users.info", "files.info"}


def slack(method, **payload):
    # Client profiles get the token as PX_SLACK_TOKEN: a SLACK_BOT_TOKEN in their .env would make Hermes
    # start a second Slack connection for the same app (a duplicate-credential error).
    token = env("SLACK_BOT_TOKEN") or env("PX_SLACK_TOKEN")
    if not token:
        raise SlackError("no Slack bot token configured")
    payload = {k: v for k, v in payload.items() if v is not None}
    if method in FORM_METHODS:   # read methods that reject a JSON body ("invalid_arguments")
        data, ctype = urlencode(payload).encode(), "application/x-www-form-urlencoded"
    else:
        data, ctype = json.dumps(payload).encode(), "application/json; charset=utf-8"
    req = urllib.request.Request(f"https://slack.com/api/{method}", method="POST", data=data,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": ctype})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())
    if not data.get("ok"):
        raise SlackError(f"{method}: {data.get('error')} {data.get('needed') or ''}".strip())
    return data


_people_cache = {"at": 0, "people": []}


def people():
    """Workspace humans: [{id, name, real_name, display_name}] (cached 10 minutes)."""
    if time.time() - _people_cache["at"] > 600:
        members, cursor = [], None
        while True:
            data = slack("users.list", limit=200, cursor=cursor)
            members += data["members"]
            cursor = (data.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
        _people_cache["people"] = [
            {"id": m["id"], "name": m.get("name"), "real_name": m.get("real_name") or "",
             "display_name": (m.get("profile") or {}).get("display_name") or ""}
            for m in members if not m.get("is_bot") and not m.get("deleted") and m["id"] != "USLACKBOT"]
        _people_cache["at"] = time.time()
    return _people_cache["people"]


def person_name(user_id):
    for p in people():
        if p["id"] == user_id:
            return p["real_name"] or p["display_name"] or p["name"]
    return user_id


def find_person(query):
    """Match 'Shravya', '@shravya' or a member ID. Returns (person, candidates)."""
    q = (query or "").strip().lstrip("@").lower()
    if not q:
        return None, []
    everyone = people()
    exact = [p for p in everyone if q in (p["id"].lower(), (p["name"] or "").lower(),
                                         p["real_name"].lower(), p["display_name"].lower())]
    if len(exact) == 1:
        return exact[0], exact
    partial = exact or [p for p in everyone if q in p["real_name"].lower() or q in (p["name"] or "").lower()
                        or q in p["display_name"].lower()]
    return (partial[0] if len(partial) == 1 else None), partial


# ---------------------------------------------------------------- helpers

def _num(n):
    if n is None:
        return "–"
    n = float(n)
    for unit, size in (("M", 1e6), ("k", 1e3)):
        if abs(n) >= size:
            return f"{n / size:.1f}".rstrip("0").rstrip(".") + unit
    return f"{n:.0f}"


def _delta(n, pct=False):
    if n is None:
        return "–"
    sign = "+" if n > 0 else ""
    return f"{sign}{n:.0f}%" if pct else f"{sign}{_num(n)}"


def sparkline(values):
    vals = [v or 0 for v in values]
    if len(vals) < 2:
        return "no data yet"
    hi, lo = max(vals), min(vals)
    span = (hi - lo) or 1
    return "".join(SPARK[int((v - lo) / span * (len(SPARK) - 1))] for v in vals)


def _ago(ts):
    if not ts:
        return ""
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts)
    s = (datetime.now(timezone.utc) - ts).total_seconds()
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= size:
            return f"{int(s // size)}{unit} ago"
    return "just now"


def _quote(text, limit=2800):
    text = (text or "").strip()
    if len(text) > limit:
        text = text[:limit] + "…"
    return "\n".join("> " + line if line.strip() else ">" for line in text.splitlines())


def _esc(text):
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _val(**kw):
    return json.dumps(kw, separators=(",", ":"))


# ---------------------------------------------------------------- suggestion cards

def suggestion_blocks(s, x_connected=False):
    """One suggestion as a Slack card. `s` is a suggestions row (dict)."""
    code, kind, client = s["code"], s["kind"], s["client"]
    label = KIND_LABEL.get(kind, kind)
    tags = ", ".join(t for t in (s.get("hook"), s.get("format")) if t)
    blocks = [{"type": "section", "text": {"type": "mrkdwn",
               "text": f"*{code}*  {label}" + (f"  _({_esc(tags)})_" if tags else "")}}]
    if s.get("target_url"):
        who = f"*@{_esc(s['target_by'])}*: " if s.get("target_by") else ""
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text":
                       f"↪ {who}{_esc((s.get('target_text') or '')[:200])}  <{s['target_url']}|original>"}]})
    copy = s.get("final_text") or s.get("text")
    if copy:
        if kind == "thread":
            from .ledger import thread_parts
            copy = "\n\n".join(f"{i}/ {p}" for i, p in enumerate(thread_parts(copy), 1))
        clip = None
        if kind == "video_qrt" and "\n---" in copy:
            copy, clip = [x.strip() for x in copy.split("\n---", 1)]
        if kind == "article":
            lines = copy.strip().splitlines()
            title = lines[0].lstrip("# ").strip() if lines else ""
            body = "\n".join(lines[1:]).strip()
            words = len(body.split())
            more = "…" if len(body) > 600 else ""
            blocks.append({"type": "section", "text": {"type": "mrkdwn",
                           "text": f"*{_esc(title)}*\n{_quote(_esc(body[:600]))}{more}"}})
            blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
                           "text": f"{words} words · full article in this card's thread"}]})
        else:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": _quote(_esc(copy))}})
        if clip:
            blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": "🎬 " + _esc(clip)[:900]}]})
    blocks += quality_blocks(s)
    notes = []
    if s.get("rationale"):
        notes.append(_esc(s["rationale"]))
    if s.get("final_text") and s.get("text") and s["final_text"] != s["text"]:
        notes.append("Edited by the team.")
    if s.get("note"):
        notes.append(f"Note: {_esc(s['note'])}")
    if kind == "qrt_promo":
        promo = f"Promotes @{s.get('parent_client')} {s.get('parent_code')}" if s.get("parent_code") else "Article promo"
        if not s.get("intent_url"):
            promo += " (Open in X appears once the article is posted)"
        notes.append(promo)
    status = STATUS_LABEL.get(s.get("status"), s.get("status"))
    if s.get("decided_by"):
        status += f" by {_esc(s['decided_by'])}"
    if s.get("status") == "posted" and s.get("posted_id"):
        status += f"  <https://x.com/{client}/status/{s['posted_id']}|View on X>"
    notes.append(status)
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": "  ·  ".join(notes)[:2900]}]})

    v = _val(c=client, k=code)
    buttons = []
    open_status = s.get("status") in ("pending", "approved", "edited")
    if open_status and s.get("api_postable") and x_connected:
        buttons.append({"type": "button", "action_id": "px_post", "value": v, "style": "primary",
                        "text": {"type": "plain_text", "text": "Post now"},
                        "confirm": {"title": {"type": "plain_text", "text": f"Post {code}?"},
                                    "text": {"type": "mrkdwn", "text": f"This publishes {code} to @{client} on X right now."},
                                    "confirm": {"type": "plain_text", "text": "Post"},
                                    "deny": {"type": "plain_text", "text": "Cancel"}}})
    if s.get("intent_url") and s.get("status") not in ("posted", "rejected", "expired"):
        buttons.append({"type": "button", "action_id": "px_open", "url": s["intent_url"], "value": v,
                        "text": {"type": "plain_text", "text": "Open in X"}})
    if kind == "article" and s.get("status") not in ("posted", "rejected", "expired"):
        buttons.append({"type": "button", "action_id": "px_article_posted", "value": v,
                        "text": {"type": "plain_text", "text": "Mark as posted"}})
    if s.get("status") == "pending":
        buttons.append({"type": "button", "action_id": "px_approve", "value": v,
                        "text": {"type": "plain_text", "text": "Approve"},
                        **({} if buttons and buttons[0].get("style") else {"style": "primary"})})
    if open_status:
        if kind != "repost":
            buttons.append({"type": "button", "action_id": "px_edit", "value": v,
                            "text": {"type": "plain_text", "text": "Read & edit" if kind == "article" else "Edit"}})
        buttons.append({"type": "button", "action_id": "px_reject", "value": v, "style": "danger",
                        "text": {"type": "plain_text", "text": "Reject"}})
    if buttons:
        blocks.append({"type": "actions", "block_id": f"px_{code}", "elements": buttons[:5]})
    return blocks


def _load(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return None
    return v


def quality_blocks(s):
    """Scores, fact-check result and numbered references, so the team can trust (or challenge) a draft."""
    scores = _load(s.get("scores")) or {}
    fc = _load(s.get("factcheck")) or {}
    refs = _load(s.get("refs")) or []
    out, bits = [], []
    labels = {"voice": "Voice", "hook": "Hook", "substance": "Substance", "anti_ai": "Anti-AI",
              "algo": "Algo", "lineup": "Blind lineup"}
    for k, label in labels.items():
        if isinstance(scores, dict) and scores.get(k) not in (None, ""):
            bits.append(f"{label} {scores[k]}")
    if isinstance(fc, dict) and fc.get("summary"):
        bits.append("Fact-check: " + str(fc["summary"]))
    if bits:
        out.append({"type": "context", "elements": [{"type": "mrkdwn",
                    "text": "  ·  ".join(_esc(b) for b in bits)[:2900]}]})
    if refs:
        lines = "\n".join(f"[{i}] " + _esc(str(r))[:220] for i, r in enumerate(refs[:8], 1))
        more = f"\n…and {len(refs) - 8} more" if len(refs) > 8 else ""
        out.append({"type": "context", "elements": [{"type": "mrkdwn",
                    "text": ("*References*\n" + lines + more)[:2900]}]})
    return out


def article_sections(text, size=2800):
    """Split a long text at paragraph boundaries into chunks Slack can show and edit (3,000-char cap)."""
    chunks, cur = [], ""
    for para in (text or "").split("\n\n"):
        if cur and len(cur) + len(para) + 2 > size:
            chunks.append(cur)
            cur = para
        else:
            cur = f"{cur}\n\n{para}" if cur else para
        while len(cur) > size:
            chunks.append(cur[:size])
            cur = cur[size:]
    if cur:
        chunks.append(cur)
    return chunks or [""]


def suggestion_fallback_text(s):
    return f"{s['code']} {KIND_LABEL.get(s['kind'], s['kind'])}: {(s.get('final_text') or s.get('text') or '')[:150]}"


def edit_modal(s):
    return {"type": "modal", "callback_id": "px_edit_submit",
            "private_metadata": _val(c=s["client"], k=s["code"], ch=s.get("slack_channel"), ts=s.get("slack_ts")),
            "title": {"type": "plain_text", "text": f"Edit {s['code']}"[:24]},
            "submit": {"type": "plain_text", "text": "Save"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": edit_inputs(s.get("final_text") or s.get("text") or "")}


def edit_inputs(text):
    parts = article_sections(text)
    n = len(parts)
    blocks = []
    for i, part in enumerate(parts[:90]):
        block = {"type": "input", "block_id": f"text_{i}",
                 "label": {"type": "plain_text", "text": "Your version" if n == 1 else f"Part {i + 1} of {n}"},
                 "element": {"type": "plain_text_input", "action_id": "v", "multiline": True, "initial_value": part}}
        if i == 0:
            block["hint"] = {"type": "plain_text",
                             "text": "The bot learns from every edit. For threads, separate tweets with a line containing only ---"}
        blocks.append(block)
    return blocks


def article_posted_modal(s):
    return {"type": "modal", "callback_id": "px_article_posted_submit",
            "private_metadata": _val(c=s["client"], k=s["code"]),
            "title": {"type": "plain_text", "text": f"{s['code']} is live"[:24]},
            "submit": {"type": "plain_text", "text": "Save"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": [{"type": "input", "block_id": "url",
                        "label": {"type": "plain_text", "text": "Link to the published article on X"},
                        "hint": {"type": "plain_text", "text": "Teammates' promo QRTs for it get a working Open in X button."},
                        "element": {"type": "url_text_input", "action_id": "v"}}]}


def reject_modal(s):
    return {"type": "modal", "callback_id": "px_reject_submit",
            "private_metadata": _val(c=s["client"], k=s["code"], ch=s.get("slack_channel"), ts=s.get("slack_ts")),
            "title": {"type": "plain_text", "text": f"Reject {s['code']}"[:24]},
            "submit": {"type": "plain_text", "text": "Reject"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": [{"type": "input", "block_id": "note", "optional": True,
                        "label": {"type": "plain_text", "text": "What was off? The bot learns from this."},
                        "element": {"type": "plain_text_input", "action_id": "v",
                                    "placeholder": {"type": "plain_text", "text": "Too salesy, off topic, not their voice…"}}}]}


# ---------------------------------------------------------------- connect X (signed link to the connector page)

def connect_link(client, ttl=7 * 86400):
    base = (env("AGENCY_WEB_URL") or "").rstrip("/")
    key = env("INTERNAL_TOKEN")
    if not base.startswith("https://") or not key:
        return None
    exp = int(time.time()) + ttl
    sig = hmac.new(key.encode(), f"{client}|{exp}".encode(), hashlib.sha256).hexdigest()[:32]
    return f"{base}/x/connect?{urlencode({'client': client, 'exp': exp, 'sig': sig})}"


def verify_connect(client, exp, sig, key):
    try:
        if int(exp) < time.time():
            return False
    except (TypeError, ValueError):
        return False
    good = hmac.new(key.encode(), f"{client}|{exp}".encode(), hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(good, sig or "")


# ---------------------------------------------------------------- Home tab (the dashboard)

def home_view(data, days, spark, viral, learning, playbook_version, clients_for_select):
    """`data` = agency.overview(conn, days); `spark` = {client: [daily views]}; `viral` = agency.viral rows."""
    rows = data["clients"]
    tot = {k: sum((c[k] or 0) for c in rows) for k in ("views", "likes", "posts", "bookmarks")}
    foll = sum((c["followers_change"] or 0) for c in rows)
    b = [{"type": "header", "text": {"type": "plain_text", "text": "Perpetual X — agency dashboard"}},
         {"type": "actions", "block_id": "px_window", "elements": [
             {"type": "button", "action_id": f"px_home_{d}", "value": str(d),
              "text": {"type": "plain_text", "text": f"{'Today' if d == 1 else f'{d} days'}"},
              **({"style": "primary"} if d == days else {})} for d in (1, 7, 28)] + [
             {"type": "button", "action_id": "px_home_refresh", "value": str(days),
              "text": {"type": "plain_text", "text": "Refresh"}}]},
         {"type": "section", "fields": [
             {"type": "mrkdwn", "text": f"*Views*\n{_num(tot['views'])}"},
             {"type": "mrkdwn", "text": f"*Likes*\n{_num(tot['likes'])}"},
             {"type": "mrkdwn", "text": f"*New followers*\n{_delta(foll)}"},
             {"type": "mrkdwn", "text": f"*Posts*\n{tot['posts']}"}]},
         {"type": "context", "elements": [{"type": "mrkdwn", "text":
             f"Last {days} {'day' if days == 1 else 'days'} across {len(rows)} client(s), ranked by views. "
             "Ask Perpetual in a DM for anything else."}]},
         {"type": "divider"},
         {"type": "header", "text": {"type": "plain_text", "text": "Leaderboard"}}]
    if not rows:
        b.append({"type": "section", "text": {"type": "mrkdwn", "text":
                  "No clients yet. DM Perpetual: _add client @handle, run by <name>, niche <topics>_."}})
    for i, c in enumerate(rows[:20], 1):
        sug = c["suggestions"]
        flags = "  ".join(f"⚠️ {f}" for f in c["flags"]) or "✅ All good"
        b.append({"type": "section",
                  "text": {"type": "mrkdwn", "text":
                           f"*{i}. @{c['client']}*  {_esc(c['name'] or '')}\n"
                           f"`{sparkline(spark.get(c['client'], []))}`  28-day views\n"
                           f"Followers *{_num(c['followers'])}* ({_delta(c['followers_change'])})   "
                           f"Views *{_num(c['views'])}* ({_delta(c['views_change_pct'], pct=True)})   "
                           f"Engagement *{c['engagement_rate'] if c['engagement_rate'] is not None else '–'}%*\n"
                           f"Suggestions used {sug['used']}/{sug['suggested']}, posted {sug['posted']}"
                           + (f", {sug['pending']} waiting" if sug['pending'] else "") + f"\n{flags}"},
                  "accessory": {"type": "button", "action_id": "px_client_details", "value": c["client"],
                                "text": {"type": "plain_text", "text": "Details"}}})
    b.append({"type": "divider"})
    b.append({"type": "header", "text": {"type": "plain_text", "text": "Viral now in our niches"}})
    if not viral:
        b.append({"type": "context", "elements": [{"type": "mrkdwn", "text": "Collected at 06:30 and 14:30 each day."}]})
    for t in viral[:5]:
        sec = {"type": "section", "text": {"type": "mrkdwn", "text":
               f"*@{_esc(t.get('author') or '')}*  _{'cross-niche' if t.get('niche') == '_meta' else _esc(t.get('niche') or '')}_\n"
               f"{_esc((t.get('text') or '')[:180])}\n"
               f"{_num(t.get('likes'))} likes · {_num(t.get('views'))} views · <{t.get('url')}|open>"}}
        if clients_for_select:
            sec["accessory"] = {"type": "static_select", "action_id": "px_draft_for",
                                "placeholder": {"type": "plain_text", "text": "Draft for…"},
                                "options": [{"text": {"type": "plain_text", "text": f"@{h}"[:75]},
                                             "value": _val(c=h, u=t.get("url"))[:150]}
                                            for h in clients_for_select[:100]]}
        b.append(sec)
    b.append({"type": "divider"})
    b.append({"type": "header", "text": {"type": "plain_text", "text": "Team"}})
    team = data.get("team") or []
    if not team:
        b.append({"type": "context", "elements": [{"type": "mrkdwn", "text":
                  "No actions yet. People act with the buttons on suggestion cards or by replying in a client channel."}]})
    for t in team[:8]:
        mins = t.get("median_minutes_to_act")
        b.append({"type": "context", "elements": [{"type": "mrkdwn", "text":
                  f"*{_esc(t['employee'])}* on @{t['client']}: {t['actions']} actions, {t['approved']} approved, "
                  f"{t['edited']} edited, {t['rejected']} rejected, {t['posted']} posted"
                  + (f", typically acts in {int(mins)} min" if mins is not None else "")}]})
    b.append({"type": "divider"})
    b.append({"type": "header", "text": {"type": "plain_text", "text": "What we're learning"}})
    lines = [f"• *{'All clients' if l['client'] == '_agency' else '@' + l['client']}* ({_ago(l['at'])}): {_esc(l['summary'][:200])}"
             for l in learning[:3]] or ["• The first weekly reviews run on Sunday night."]
    b.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)},
              "accessory": {"type": "button", "action_id": "px_view_playbook", "value": "agency",
                            "text": {"type": "plain_text", "text": f"Agency playbook {playbook_version or ''}".strip()}}})
    return {"type": "home", "blocks": b[:100]}


def client_modal(c, series, top_posts, queue_counts, x_connection, competitors_rows=None, connect_url=None):
    views = [r.get("views") or 0 for r in series]
    foll = [r.get("followers") for r in series if r.get("followers") is not None]
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text":
         f"*{_esc(c['display_name'])}* (@{c['handle']})\n"
         f"Channel: {'<#' + c['slack_channel'] + '>' if c.get('slack_channel') else 'none'}   "
         f"Run by: {('<@' + c['employee_slack'] + '>') if c.get('employee_slack') else 'unassigned'}\n"
         f"Niche: {_esc(c.get('niche_topics') or '–')}\nWatching: {_esc(c.get('watch_accounts') or '–')}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text":
         f"*Views, last {len(views)} days*\n`{sparkline(views)}`\n"
         f"*Followers*  {_num(foll[0]) if foll else '–'} → {_num(foll[-1]) if foll else '–'}"
         + (f" ({_delta(foll[-1] - foll[0])})" if len(foll) > 1 else "")}},
        {"type": "section", "text": {"type": "mrkdwn", "text":
         "*Suggestions, last 7 days*  " + ("  ".join(f"{k}: {v}" for k, v in sorted(queue_counts.items())) or "none yet")}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": "*Best posts, last 14 days*\n" + (
            "\n".join(f"• <{t['url']}|{_esc((t['text'] or '')[:80])}> — {_num(t['likes'])} likes, {_num(t['views'])} views"
                      for t in top_posts[:5]) or "The nightly recap fills this in.")}},
        {"type": "divider"},
    ]
    if competitors_rows is None:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": "Loading the comparison with watch accounts…"}]})
    else:
        lines = []
        for r in competitors_rows:
            if r.get("error"):
                lines.append(f"• @{r['handle']}: couldn't load")
            else:
                lines.append(f"• {'*' if r['is_client'] else ''}@{r['handle']}{'*' if r['is_client'] else ''}: "
                             f"{r['posts']} posts, {_num(r['avg_likes'])} avg likes, {_num(r['avg_views'])} avg views")
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
                       "text": "*Against the accounts we watch (14 days)*\n" + "\n".join(lines)}})
    blocks.append({"type": "divider"})
    if x_connection:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text":
                       f"✅ X connected as @{x_connection['x_username']}: *Post now* works on tweets, threads and engage-back replies."}]})
    elif connect_url:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text":
                       "X isn't connected, so drafts go out with *Open in X*. Connect to enable *Post now*."},
                       "accessory": {"type": "button", "action_id": "px_open", "url": connect_url,
                                     "text": {"type": "plain_text", "text": "Connect X account"}}})
    return {"type": "modal", "callback_id": "px_client_modal", "private_metadata": c["handle"],
            "title": {"type": "plain_text", "text": f"@{c['handle']}"[:24]},
            "close": {"type": "plain_text", "text": "Close"}, "blocks": blocks}


def text_modal(title, body):
    chunks = [body[i:i + 2900] for i in range(0, min(len(body), 2900 * 30), 2900)] or ["(empty)"]
    return {"type": "modal", "title": {"type": "plain_text", "text": title[:24]},
            "close": {"type": "plain_text", "text": "Close"},
            "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": _esc(ch)}} for ch in chunks]}


# ---------------------------------------------------------------- posting and refreshing cards

def _card_row(conn, client, code):
    return conn.execute("SELECT * FROM suggestions WHERE client = %s AND code = %s", (client, code)).fetchone()


def _x_connected(conn, client):
    return bool(conn.execute("SELECT 1 FROM x_tokens WHERE client = %s", (client,)).fetchone())


def post_cards(conn, client, codes):
    """Post each saved suggestion as an interactive card in the client's channel; remember where."""
    row = conn.execute("SELECT slack_channel FROM clients WHERE handle = %s", (client,)).fetchone()
    channel = row and row["slack_channel"]
    if not channel:
        return 0
    connected = _x_connected(conn, client)
    posted = 0
    for code in codes:
        s = _card_row(conn, client, code)
        if not s:
            continue
        res = slack("chat.postMessage", channel=channel, blocks=suggestion_blocks(dict(s), connected),
                    text=suggestion_fallback_text(s), unfurl_links=False, unfurl_media=False)
        conn.execute("UPDATE suggestions SET slack_channel = %s, slack_ts = %s WHERE client = %s AND code = %s",
                     (res["channel"], res["ts"], client, code))
        if s["kind"] == "article":        # the full text, copy-ready, in the card's thread
            parts = article_sections(s["final_text"] or s["text"])
            for i, part in enumerate(parts, 1):
                head = f"*{code}, part {i}/{len(parts)}*\n" if len(parts) > 1 else ""
                slack("chat.postMessage", channel=res["channel"], thread_ts=res["ts"], text=head + part)
        posted += 1
    conn.commit()
    return posted


def refresh_card(conn, client, code):
    """Re-render a card after feedback, an edit or a post (from a button or from typed feedback)."""
    s = _card_row(conn, client, code)
    if not s or not s["slack_ts"]:
        return False
    slack("chat.update", channel=s["slack_channel"], ts=s["slack_ts"],
          blocks=suggestion_blocks(dict(s), _x_connected(conn, client)), text=suggestion_fallback_text(s))
    return True


# ---------------------------------------------------------------- documents (canvas, or a tidy thread)

_auth = {}


def _workspace():
    if not _auth:
        _auth.update(slack("auth.test"))
    return _auth


def md_to_mrkdwn(md):
    """Markdown → Slack mrkdwn for the thread fallback (headings, bold, links, bullets)."""
    import re
    out = []
    for line in (md or "").splitlines():
        h = re.match(r"^\s*#{1,6}\s+(.*)$", line)
        if h:
            line = f"*{h.group(1).strip().strip('*')}*"
        elif re.match(r"^\s*[-*]\s+", line) and not re.match(r"^\s*[-*]{3,}\s*$", line):
            line = re.sub(r"^(\s*)[-*]\s+", lambda m: m.group(1) + "• ", line)
        elif re.match(r"^\s*([-*_])\1{2,}\s*$", line):
            line = "──────────"
        line = re.sub(r"\*\*(.+?)\*\*", r"*\1*", line)
        line = re.sub(r"(?<![\w*])__(.+?)__(?![\w*])", r"_\1_", line)
        line = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r"<\2|\1>", line)
        out.append(line)
    return "\n".join(out)


def doc_chunks(md, limit=2900):
    """Split a document at its ## sections, then at paragraphs, so each Slack message stays readable."""
    import re
    sections, cur = [], []
    for line in (md or "").splitlines():
        if re.match(r"^\s*#{1,2}\s+", line) and cur:
            sections.append("\n".join(cur).strip())
            cur = []
        cur.append(line)
    if cur:
        sections.append("\n".join(cur).strip())
    chunks = []
    for sec in filter(None, sections):
        text = md_to_mrkdwn(sec)
        while len(text) > limit:
            cut = text.rfind("\n\n", 0, limit)
            cut = cut if cut > limit // 3 else text.rfind("\n", 0, limit)
            cut = cut if cut > 0 else limit
            chunks.append(text[:cut].strip())
            text = text[cut:].strip()
        if text:
            chunks.append(text)
    return chunks


def doc_summary_blocks(title, summary, link=None, mode="canvas", asked_by=None):
    blocks = [{"type": "header", "text": {"type": "plain_text", "text": title[:150], "emoji": True}},
              {"type": "section", "text": {"type": "mrkdwn", "text": md_to_mrkdwn(summary)[:2900] or " "}}]
    if link and mode == "canvas":
        blocks.append({"type": "actions", "elements": [
            {"type": "button", "action_id": "px_open_doc", "style": "primary", "url": link,
             "text": {"type": "plain_text", "text": "Open document"}}]})
    where = "Full document: open it above." if mode == "canvas" else "Full document: in this thread 🧵"
    who = f"Asked by <@{asked_by}>. " if asked_by else ""
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text":
                   f"{who}{where} Reply here to ask for changes; react with codes like `ok R3` to approve."}]})
    return blocks


def publish_doc(conn, client, title, markdown, summary, kind="answer", asked_by=None):
    """Publish a document for a client: a Slack canvas in its channel plus one short summary message,
    or (when canvases aren't available) the summary with the document laid out in its thread.
    Returns {mode, link, channel, ts, note}."""
    from . import research
    row = conn.execute("SELECT slack_channel FROM clients WHERE handle = %s", (client,)).fetchone()
    channel = row and row["slack_channel"]
    if not channel:
        raise SlackError("this client has no Slack channel yet")
    note, mode, link = None, "thread", None
    try:
        res = slack("canvases.create", title=title[:255], channel_id=channel,
                    document_content={"type": "markdown", "markdown": markdown})
        canvas_id = res["canvas_id"]
        try:
            slack("canvases.access.set", canvas_id=canvas_id, access_level="write", channel_ids=[channel])
        except SlackError:
            pass
        ws = _workspace()
        link = f"{ws['url'].rstrip('/')}/docs/{ws['team_id']}/{canvas_id}"
        mode = "canvas"
    except SlackError as e:
        note = f"canvas unavailable ({e}); used a thread instead"
    msg = slack("chat.postMessage", channel=channel, text=f"{title}: {summary[:200]}", unfurl_links=False,
                blocks=doc_summary_blocks(title, summary, link, mode, asked_by))
    if mode == "thread":
        for part in doc_chunks(markdown):
            slack("chat.postMessage", channel=channel, thread_ts=msg["ts"], text=part,
                  unfurl_links=False, unfurl_media=False)
        try:
            link = slack("chat.getPermalink", channel=channel, message_ts=msg["ts"])["permalink"]
        except SlackError:
            link = None
    research.log_doc(conn, client, title, kind, mode, link, channel, msg["ts"])
    return {"mode": mode, "link": link, "channel": channel, "ts": msg["ts"], "note": note}
