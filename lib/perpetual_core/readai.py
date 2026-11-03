"""Read AI REST client (open beta): recent meetings with summary, action items, key questions,
topics and (optionally) the transcript.

    GET https://api.read.ai/v1/meetings            paginated (10 per page, cursor = id of last meeting)
    GET https://api.read.ai/v1/meetings/{id}       expand[]=summary&expand[]=action_items&...

Bearer token from connections.token(..., "readai"). The beta's field names aren't fully
documented, so `normalize` reads several likely spellings and never fails on a missing field.
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API = "https://api.read.ai/v1"
FULL = ["summary", "action_items", "key_questions", "topics", "chapter_summaries"]
UA = "PerpetualX/1.0"


class ReadAIError(RuntimeError):
    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.status = status


def _get(tok, path, params=None):
    url = f"{API}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}", "Accept": "application/json",
                                               "User-Agent": UA})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            if e.code in (429, 502, 503, 504) and attempt < 2:
                time.sleep(3 * (attempt + 1))
                continue
            raise ReadAIError(f"Read AI {path} -> HTTP {e.code}: {e.read().decode(errors='replace')[:300]}",
                              e.code) from None
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < 2:
                time.sleep(3)
                continue
            raise ReadAIError(f"Read AI unreachable: {e}") from None


# ---------------------------------------------------------------- normalizing

def _time(v):
    """ISO string from epoch ms / epoch s / ISO text."""
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit()):
        n = float(v)
        n = n / 1000 if n > 1e11 else n
        return datetime.fromtimestamp(n, timezone.utc).isoformat()
    return str(v)


def _text(v):
    if v is None:
        return None
    if isinstance(v, str):
        return v.strip() or None
    if isinstance(v, dict):
        for k in ("text", "content", "summary", "value", "title", "question", "name"):
            if isinstance(v.get(k), str) and v[k].strip():
                return v[k].strip()
        return None
    if isinstance(v, list):
        parts = [_text(x) for x in v]
        return "\n".join(p for p in parts if p) or None
    return str(v)


def _list(v):
    if v is None:
        return []
    if isinstance(v, dict):
        for k in ("items", "data", "list", "action_items", "questions", "topics"):
            if isinstance(v.get(k), list):
                return v[k]
        return [v]
    return v if isinstance(v, list) else [v]


def _person(p):
    if isinstance(p, str):
        return {"name": p, "email": None}
    p = p or {}
    return {"name": p.get("name") or p.get("display_name") or p.get("full_name"),
            "email": p.get("email") or p.get("email_address")}


def _action(a):
    if isinstance(a, str):
        return {"text": a, "owner": None}
    a = a or {}
    owner = a.get("assignee") or a.get("owner") or a.get("assigned_to") or a.get("speaker")
    if isinstance(owner, dict):
        owner = owner.get("name") or owner.get("email")
    return {"text": _text(a), "owner": owner}


def first(m, *keys):
    for k in keys:
        if m.get(k) not in (None, "", [], {}):
            return m[k]
    return None


def normalize(m, transcript=False):
    m = m or {}
    out = {
        "id": first(m, "id", "meeting_id", "ulid"),
        "title": first(m, "title", "name", "meeting_title") or "Untitled meeting",
        "start": _time(first(m, "start_time_ms", "start_time", "started_at", "start")),
        "end": _time(first(m, "end_time_ms", "end_time", "ended_at", "end")),
        "platform": first(m, "platform", "source"),
        "url": first(m, "report_url", "url", "web_url", "meeting_url"),
        "participants": [_person(p) for p in _list(first(m, "participants", "attendees"))][:30],
        "summary": _text(first(m, "summary", "overview")),
        "action_items": [a for a in (_action(x) for x in _list(first(m, "action_items", "actionItems", "next_steps")))
                         if a["text"]],
        "key_questions": [t for t in (_text(x) for x in _list(first(m, "key_questions", "questions"))) if t][:10],
        "topics": [t for t in (_text(x) for x in _list(first(m, "topics", "chapters", "chapter_summaries"))) if t][:12],
    }
    if transcript:
        t = first(m, "transcript")
        if isinstance(t, (list, dict)):
            rows = _list(t.get("speaker_blocks") if isinstance(t, dict) and t.get("speaker_blocks") else t)
            lines = []
            for r in rows:
                if isinstance(r, dict):
                    who = r.get("speaker") or r.get("speaker_name") or ""
                    if isinstance(who, dict):
                        who = who.get("name") or ""
                    lines.append(f"{who}: {_text(r) or ''}".strip(": "))
                else:
                    lines.append(str(r))
            t = "\n".join(lines)
        out["transcript"] = (t or "")[:30000] if t else None
    return out


# ---------------------------------------------------------------- calls

def _expanded(tok, mid, expand):
    """GET one meeting, retrying with fewer expands if the beta rejects one of them."""
    for ex in (expand, ["summary", "action_items"], []):
        try:
            return _get(tok, f"/meetings/{urllib.parse.quote(str(mid))}", {"expand[]": ex} if ex else None)
        except ReadAIError as e:
            if e.status not in (400, 422) or not ex:
                raise
    return {}


def get(tok, mid, transcript=False):
    raw = _expanded(tok, mid, FULL + (["transcript"] if transcript else []))
    return normalize(raw.get("data", raw) if isinstance(raw, dict) else raw, transcript=transcript)


def listing(tok, days=7, max_pages=5):
    """Raw meeting list items from the last `days`, newest first (stops paging once older)."""
    since = time.time() - days * 86400
    items, cursor = [], None
    for _ in range(max_pages):
        page = _get(tok, "/meetings", {"limit": 10, **({"cursor": cursor} if cursor else {})})
        data = page.get("data") if isinstance(page, dict) else page
        data = data or []
        items += data
        if not data or not (isinstance(page, dict) and page.get("has_more", True)):
            break
        last = normalize(data[-1])
        try:
            if last["start"] and datetime.fromisoformat(last["start"].replace("Z", "+00:00")).timestamp() < since:
                break
        except ValueError:
            pass
        cursor = last["id"]
    out = []
    for m in items:
        n = normalize(m)
        try:
            if n["start"] and datetime.fromisoformat(n["start"].replace("Z", "+00:00")).timestamp() < since:
                continue
        except ValueError:
            pass
        out.append(n)
    return out


def recent(tok, days=7, limit=10):
    """Recent meetings with summary + action items (one detail call each)."""
    out = []
    for n in listing(tok, days)[:limit]:
        try:
            out.append(get(tok, n["id"]))
        except ReadAIError:
            out.append(n)
    return out
