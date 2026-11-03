"""Media for client posts: quote cards and carousels drawn with Pillow (no AI image API), voice briefs
through edge-tts, and Slack's external upload flow to drop the files into the client's own channel.

Pillow and edge_tts are imported inside the functions that need them, so this module always imports;
a missing package comes back as a clear error from the tool instead of a crash at plugin load.
"""

import asyncio
import io
import json
import re
import urllib.error
import urllib.request
from urllib.parse import urlencode

from .settings import client_handle, env

QUOTE_SIZE = (1080, 1080)
SLIDE_SIZE = (1080, 1350)
MAX_SLIDES = 12
MAX_TTS_CHARS = 6000

# Two colours plus one accent per theme. "muted" is the foreground blended into the background, not a new hue.
THEMES = {
    "dark": {"bg": (17, 19, 24), "fg": (244, 243, 238), "accent": (56, 189, 248)},
    "light": {"bg": (250, 249, 245), "fg": (22, 24, 29), "accent": (37, 99, 235)},
}

FONT_DIRS = ["/usr/share/fonts/truetype/dejavu/", "/usr/share/fonts/dejavu/", "/usr/share/fonts/TTF/",
             "/usr/local/share/fonts/", "/Library/Fonts/", "/System/Library/Fonts/Supplemental/",
             "C:/Windows/Fonts/"]
FONT_FILES = {False: ["DejaVuSans.ttf", "arial.ttf", "Arial.ttf"],
              True: ["DejaVuSans-Bold.ttf", "arialbd.ttf", "Arial Bold.ttf"]}


class MediaError(RuntimeError):
    pass


# ---------------------------------------------------------------- drawing helpers

def _pil():
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        raise MediaError("image rendering needs Pillow, which isn't installed on the server (pip install Pillow)") from None
    return Image, ImageDraw, ImageFont


_font_cache = {}


def _font(size, bold=False):
    key = (size, bold)
    if key in _font_cache:
        return _font_cache[key]
    _, _, ImageFont = _pil()
    font = None
    for d in FONT_DIRS:
        for name in FONT_FILES[bold]:
            try:
                font = ImageFont.truetype(d + name, size)
                break
            except OSError:
                continue
        if font:
            break
    if font is None:
        try:
            font = ImageFont.load_default(size=size)       # Pillow >= 10.1
        except TypeError:
            font = ImageFont.load_default()
    _font_cache[key] = font
    return font


_EMOJI = re.compile("[\U00010000-\U0010FFFF\u2600-\u27BF\u2B00-\u2BFF\uFE00-\uFE0F\u200D\u20E3]")


def clean_text(text):
    """Drop emoji and other glyphs the fonts can't draw (they'd render as boxes); tidy whitespace."""
    text = _EMOJI.sub("", str(text or ""))
    text = text.replace("\r\n", "\n").replace("\t", " ")
    text = "\n".join(re.sub(r" {2,}", " ", line).strip() for line in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _theme(theme):
    t = dict(THEMES.get((theme or "dark").lower(), THEMES["dark"]))
    t["muted"] = tuple(int(f * 0.6 + b * 0.4) for f, b in zip(t["fg"], t["bg"]))
    return t


def _wrap(text, font, width):
    """Greedy word wrap by measured pixel width; paragraphs kept; over-long words split by character."""
    lines = []
    for para in text.split("\n"):
        if not para.strip():
            lines.append("")
            continue
        cur = ""
        for word in para.split(" "):
            while font.getlength(word) > width:          # a URL or a very long word
                cut = len(word)
                while cut > 1 and font.getlength(word[:cut]) > width:
                    cut -= 1
                if cur:
                    lines.append(cur)
                    cur = ""
                lines.append(word[:cut])
                word = word[cut:]
            trial = f"{cur} {word}" if cur else word
            if font.getlength(trial) <= width:
                cur = trial
            else:
                lines.append(cur)
                cur = word
        lines.append(cur)
    return lines


def _fit(text, box_w, box_h, max_size, min_size, bold=False, spacing=1.35):
    """Largest font size whose wrapped text fits the box. Returns (font, lines, line_height)."""
    size = max_size
    while True:
        font = _font(size, bold)
        lines = _wrap(text, font, box_w)
        lh = int(size * spacing)
        if len(lines) * lh <= box_h or size <= min_size:
            if len(lines) * lh > box_h:              # still too long at the floor: truncate with an ellipsis
                keep = max(1, box_h // lh)
                lines = lines[:keep]
                lines[-1] = lines[-1].rstrip(" .,;:") + "…"
            return font, lines, lh
        size -= 2


def _draw_lines(draw, lines, font, lh, x, y, fill):
    for line in lines:
        draw.text((x, y), line, font=font, fill=fill)
        y += lh
    return y


def _png(img):
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def _handle(handle):
    h = (handle or "").strip().lstrip("@")
    return f"@{h}" if h else ""


# ---------------------------------------------------------------- quote card

def make_quote_card(text, handle, name=None, theme="dark"):
    """A 1080x1080 PNG: the quote, auto-sized to fit, with name and @handle at the bottom."""
    Image, ImageDraw, _ = _pil()
    text = clean_text(text)
    if not text:
        raise MediaError("the quote is empty")
    t = _theme(theme)
    W, H = QUOTE_SIZE
    margin = 110
    img = Image.new("RGB", QUOTE_SIZE, t["bg"])
    d = ImageDraw.Draw(img)
    d.rectangle([margin, margin, margin + 90, margin + 10], fill=t["accent"])          # accent bar
    top, footer_h = margin + 70, 150
    box_h = H - margin - footer_h - top
    font, lines, lh = _fit(text, W - 2 * margin, box_h, max_size=84, min_size=30, bold=True, spacing=1.3)
    block_h = len(lines) * lh
    y = top + max(0, (box_h - block_h) // 2)                                            # centred vertically
    _draw_lines(d, lines, font, lh, margin, y, t["fg"])
    fy = H - margin - 80
    d.line([margin, fy - 30, W - margin, fy - 30], fill=t["muted"], width=2)
    name = clean_text(name)
    h = _handle(handle)
    if name:
        d.text((margin, fy), name, font=_font(38, True), fill=t["fg"])
        d.text((margin, fy + 50), h, font=_font(30), fill=t["muted"])
    else:
        d.text((margin, fy + 18), h, font=_font(36, True), fill=t["fg"])
    return _png(img)


# ---------------------------------------------------------------- carousel

def _slide(i, n, slide, handle, t):
    Image, ImageDraw, _ = _pil()
    W, H = SLIDE_SIZE
    m = 100
    img = Image.new("RGB", SLIDE_SIZE, t["bg"])
    d = ImageDraw.Draw(img)
    title = clean_text(slide.get("title"))
    body = clean_text(slide.get("body"))
    foot_y = H - m - 30
    small = _font(30)
    d.text((m, foot_y), _handle(handle), font=small, fill=t["muted"])
    num = f"{i}/{n}"
    d.text((W - m - small.getlength(num), foot_y), num, font=small, fill=t["muted"])
    if i == 1:                                                                     # cover
        d.rectangle([m, m, m + 120, m + 14], fill=t["accent"])
        avail_top, avail_bottom = m + 120, foot_y - 160
        tf, tl, tlh = _fit(title or body, W - 2 * m, (avail_bottom - avail_top) * 2 // 3,
                           max_size=104, min_size=44, bold=True, spacing=1.18)
        sub = body if title else ""
        sf, sl, slh = (_fit(sub, W - 2 * m, (avail_bottom - avail_top) // 3, 44, 28, spacing=1.4)
                       if sub else (None, [], 0))
        total = len(tl) * tlh + (40 + len(sl) * slh if sl else 0)
        y = avail_top + max(0, (avail_bottom - avail_top - total) // 2)
        y = _draw_lines(d, tl, tf, tlh, m, y, t["fg"])
        if sl:
            _draw_lines(d, sl, sf, slh, m, y + 40, t["muted"])
        hint = "Swipe  →"
        hf = _font(34, True)
        d.text((W - m - hf.getlength(hint), foot_y - 100), hint, font=hf, fill=t["accent"])
        return img
    d.text((m, m), f"{i - 1:02d}", font=_font(56, True), fill=t["accent"])
    y = m + 110
    if title:
        tf, tl, tlh = _fit(title, W - 2 * m, 330, max_size=72, min_size=40, bold=True, spacing=1.2)
        y = _draw_lines(d, tl, tf, tlh, m, y, t["fg"]) + 40
    if body:
        bf, bl, blh = _fit(body, W - 2 * m, foot_y - 70 - y, max_size=50, min_size=26, spacing=1.45)
        _draw_lines(d, bl, bf, blh, m, y, t["muted"] if title else t["fg"])
    return img


def make_carousel(slides, handle, theme="dark"):
    """slides: [{title, body}] → (list of 1080x1350 PNG bytes, PDF bytes of all slides). Slide 1 is the cover."""
    _pil()
    slides = [s if isinstance(s, dict) else {"body": str(s)} for s in (slides or [])]
    slides = [s for s in slides if clean_text(s.get("title")) or clean_text(s.get("body"))][:MAX_SLIDES]
    if not slides:
        raise MediaError("the carousel needs at least one slide with a title or body")
    t = _theme(theme)
    images = [_slide(i, len(slides), s, handle, t) for i, s in enumerate(slides, 1)]
    buf = io.BytesIO()
    images[0].save(buf, "PDF", save_all=True, append_images=images[1:], resolution=144.0)
    return [_png(im) for im in images], buf.getvalue()


# ---------------------------------------------------------------- voice

def text_to_speech(text, voice="en-US-AndrewNeural", communicate=None):
    """MP3 bytes for `text`. `communicate(text, voice)` must return an object with an async .stream()
    yielding {"type": "audio", "data": bytes} chunks (edge_tts.Communicate by default)."""
    text = clean_text(text)[:MAX_TTS_CHARS]
    if not text:
        raise MediaError("nothing to read out")
    if communicate is None:
        try:
            import edge_tts
        except ImportError:
            raise MediaError("voice briefs need edge-tts, which isn't installed on the server (pip install edge-tts)") from None
        communicate = edge_tts.Communicate

    async def collect():
        out = bytearray()
        async for chunk in communicate(text, voice).stream():
            if chunk.get("type") == "audio" and chunk.get("data"):
                out += chunk["data"]
        return bytes(out)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        audio = asyncio.run(collect())
    else:                                          # called from inside an event loop: use a fresh one in a thread
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=1) as pool:
            audio = pool.submit(asyncio.run, collect()).result()
    if not audio:
        raise MediaError("the voice service returned no audio")
    return audio


# ---------------------------------------------------------------- Slack upload (external upload flow)

def _default_http(url, data, headers):
    req = urllib.request.Request(url, method="POST", data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        raise MediaError(f"{e.code} from {url.split('?')[0]}: {e.read().decode(errors='replace')[:300]}") from None


def _token():
    token = env("SLACK_BOT_TOKEN") or env("PX_SLACK_TOKEN")
    if not token:
        raise MediaError("no Slack bot token configured")
    return token


def _api(http, method, token, payload, form=False):
    payload = {k: v for k, v in payload.items() if v is not None}
    if form:
        data, ctype = urlencode(payload).encode(), "application/x-www-form-urlencoded"
    else:
        data, ctype = json.dumps(payload).encode(), "application/json; charset=utf-8"
    raw = http(f"https://slack.com/api/{method}", data, {"Authorization": f"Bearer {token}", "Content-Type": ctype})
    res = json.loads(raw)
    if not res.get("ok"):
        raise MediaError(f"{method}: {res.get('error')} {res.get('needed') or ''}".strip())
    return res


def upload_files(channel, files, initial_comment=None, thread_ts=None, http=None):
    """files: [(filename, data, title)] → shared in one message. Returns [{id, title, permalink}]."""
    http = http or _default_http
    token = _token()
    ids = []
    for filename, data, title in files:
        got = _api(http, "files.getUploadURLExternal", token, {"filename": filename, "length": len(data)}, form=True)
        http(got["upload_url"], data, {"Content-Type": "application/octet-stream"})
        ids.append({"id": got["file_id"], "title": title or filename})
    done = _api(http, "files.completeUploadExternal", token,
                {"files": ids, "channel_id": channel, "initial_comment": initial_comment, "thread_ts": thread_ts})
    by_id = {f.get("id"): f for f in done.get("files") or []}
    out = []
    for f in ids:
        link = (by_id.get(f["id"]) or {}).get("permalink")
        if not link:
            try:
                link = _api(http, "files.info", token, {"file": f["id"]}, form=True)["file"].get("permalink")
            except Exception:  # noqa: BLE001 - the upload itself worked
                link = None
        out.append({**f, "permalink": link})
    return out


def upload_file(channel, filename, data, title, initial_comment=None, thread_ts=None, http=None):
    """Upload one file to a channel; returns its permalink (None if Slack didn't give one)."""
    return upload_files(channel, [(filename, data, title)], initial_comment, thread_ts, http)[0]["permalink"]


# ---------------------------------------------------------------- client-agent tools

def _client():
    """(handle, display_name, slack_channel) of the client this profile serves."""
    from . import ledger
    handle = client_handle()
    conn = ledger.connect()
    try:
        row = conn.execute("SELECT display_name, slack_channel FROM clients WHERE handle = %s", (handle,)).fetchone()
    finally:
        conn.close()
    if not row or not row["slack_channel"]:
        raise MediaError(f"@{handle} has no Slack channel yet")
    return handle, row["display_name"], row["slack_channel"]


def _slug(text, fallback):
    s = re.sub(r"[^a-z0-9]+", "-", clean_text(text).lower()).strip("-")[:40]
    return s or fallback


def _tool(fn):
    def handler(params, **kwargs):
        del kwargs
        try:
            return json.dumps({"success": True, **fn(params or {})}, ensure_ascii=False, default=str)
        except Exception as e:  # noqa: BLE001
            return json.dumps({"success": False, "error": str(e)})
    handler.__name__ = fn.__name__
    return handler


@_tool
def tool_quote_card(p):
    handle, name, channel = _client()
    png = make_quote_card(p.get("text"), handle, name, p.get("theme") or "dark")
    link = upload_file(channel, f"quote-{_slug(p.get('text'), 'card')}.png", png, "Quote card",
                       initial_comment="Quote card, ready to attach to a post.")
    return {"permalink": link, "note": "Posted in the client's channel as a 1080x1080 PNG."}


@_tool
def tool_carousel(p):
    handle, _, channel = _client()
    slides = p.get("slides")
    if isinstance(slides, str):
        slides = json.loads(slides)
    pngs, pdf = make_carousel(slides, handle, p.get("theme") or "dark")
    title = clean_text(p.get("title")) or clean_text((slides[0] or {}).get("title")) or "Carousel"
    slug = _slug(title, "carousel")
    files = [(f"{slug}.pdf", pdf, f"{title} (PDF)")]
    if len(pngs) <= 9:                         # Slack shares at most 10 files per message
        files += [(f"{slug}-{i}.png", png, f"{title} {i}/{len(pngs)}") for i, png in enumerate(pngs, 1)]
    up = upload_files(channel, files, initial_comment=f"Carousel: {title} ({len(pngs)} slides, 1080x1350).")
    return {"permalink": up[0]["permalink"], "slides": len(pngs),
            "images": [f["permalink"] for f in up[1:]], "note": "PDF for LinkedIn documents; PNGs for X."}


@_tool
def tool_voice_brief(p):
    _, _, channel = _client()
    title = clean_text(p.get("title")) or "Voice brief"
    mp3 = text_to_speech(p.get("text"), p.get("voice") or "en-US-AndrewNeural")
    link = upload_file(channel, f"{_slug(title, 'voice-brief')}.mp3", mp3, title,
                       initial_comment=f"Voice brief: {title}")
    return {"permalink": link}


_S = {"type": "string"}
_THEME = {"type": "string", "enum": ["dark", "light"]}

TOOLS = [
    ("make_quote_card", tool_quote_card,
     "Render a 1080x1080 quote-card image (the client's name and @handle at the bottom) from a short line or "
     "post excerpt, and post it in the client's Slack channel. Emoji are dropped. Returns the file link.",
     {"text": _S, "theme": _THEME}, ["text"]),
    ("make_carousel", tool_carousel,
     "Render a carousel (1080x1350 slides, slide 1 is the cover) and post it in the client's channel as a PDF "
     "plus PNGs. slides = [{title, body}], 3-10 slides work best; keep each body under ~60 words.",
     {"slides": {"type": "array", "items": {"type": "object",
                                            "properties": {"title": _S, "body": _S}}},
      "theme": _THEME, "title": _S}, ["slides"]),
    ("voice_brief", tool_voice_brief,
     "Read a short brief aloud (text-to-speech MP3) and post it in the client's channel, e.g. a morning "
     "summary someone can listen to. Keep it under ~600 words.",
     {"text": _S, "title": _S}, ["text"]),
]


def register(ctx, toolset="media"):
    for name, fn, description, props, required in TOOLS:
        ctx.register_tool(name=name, toolset=toolset, handler=fn,
                          schema={"name": name, "description": description,
                                  "parameters": {"type": "object", "properties": props, "required": required}})
