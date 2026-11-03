"""Offline tests for media: quote cards, carousels, Slack's external upload flow and text-to-speech.

    uv run --no-project --python 3.12 --with "psycopg[binary]" --with cryptography --with Pillow --with edge-tts tests/test_media.py
"""

import asyncio
import io
import json
import os
import sys
from pathlib import Path
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
os.environ["SLACK_BOT_TOKEN"] = "xoxb-test"

from perpetual_core import media  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n"


def check(cond, msg):
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    print(f"ok   {msg}")


def size(png):
    from PIL import Image
    return Image.open(io.BytesIO(png)).size


def test_quote_card():
    for theme in ("dark", "light"):
        png = media.make_quote_card("Distribution is a product feature. Treat it like one, or watch a worse "
                                    "product with better reach win your market. 🚀", "AlexVacca", "Alex Vacca", theme)
        check(png.startswith(PNG) and size(png) == (1080, 1080), f"quote card is a 1080x1080 PNG ({theme})")
    long = " ".join(["words"] * 400) + " https://example.com/" + "x" * 300
    check(size(media.make_quote_card(long, "@x")) == (1080, 1080), "very long quote still fits (shrinks/truncates)")
    check(media.clean_text("Ship it 🚀✅ now‍") == "Ship it now", "emoji stripped")
    try:
        media.make_quote_card("🔥🔥", "x")
        check(False, "emoji-only quote refused")
    except media.MediaError:
        check(True, "emoji-only quote refused")


def test_carousel():
    slides = [{"title": "5 things we learned growing to 100k on X", "body": "A thread-sized playbook"},
              {"title": "Post daily", "body": "Consistency beats intensity. " * 6},
              {"title": "Reply early", "body": "The first hour decides reach."},
              {"title": "Steal formats, not ideas", "body": "Structure is reusable; your story isn't."}]
    pngs, pdf = media.make_carousel(slides, "perpetual", "light")
    check(len(pngs) == 4 and all(p.startswith(PNG) for p in pngs), "4 PNG slides")
    check(all(size(p) == (1080, 1350) for p in pngs), "slides are 1080x1350")
    check(pdf.startswith(b"%PDF") and pdf.count(b"/Type /Page\n") + pdf.count(b"/Type /Page ") + pdf.count(b"/Type/Page") >= 1,
          "PDF of the slides")
    try:
        media.make_carousel([{"title": "", "body": ""}], "x")
        check(False, "empty carousel refused")
    except media.MediaError:
        check(True, "empty carousel refused")


class FakeHttp:
    def __init__(self, permalink=True):
        self.calls, self.permalink = [], permalink

    def __call__(self, url, data, headers):
        self.calls.append((url, data, headers))
        if url.endswith("files.getUploadURLExternal"):
            n = len(self.calls)
            return json.dumps({"ok": True, "upload_url": f"https://files.slack.com/upload/v1/abc{n}",
                               "file_id": f"F{n}"}).encode()
        if url.startswith("https://files.slack.com/upload/"):
            return b"OK - 123"
        if url.endswith("files.completeUploadExternal"):
            body = json.loads(data)
            files = [{"id": f["id"], "title": f["title"],
                      **({"permalink": f"https://team.slack.com/files/U/{f['id']}/x"} if self.permalink else {})}
                     for f in body["files"]]
            return json.dumps({"ok": True, "files": files}).encode()
        if url.endswith("files.info"):
            fid = parse_qs(data.decode())["file"][0]
            return json.dumps({"ok": True, "file": {"id": fid, "permalink": f"https://info/{fid}"}}).encode()
        raise AssertionError(url)


def test_upload():
    http = FakeHttp()
    data = b"\x89PNGfake-bytes"
    link = media.upload_file("C123", "card.png", data, "Quote card", initial_comment="hi", thread_ts="1.2", http=http)
    check(len(http.calls) == 3, "upload makes exactly 3 calls")
    (u1, d1, h1), (u2, d2, h2), (u3, d3, h3) = http.calls
    q = parse_qs(d1.decode())
    check(u1 == "https://slack.com/api/files.getUploadURLExternal" and q == {"filename": ["card.png"], "length": [str(len(data))]}
          and h1["Content-Type"] == "application/x-www-form-urlencoded" and h1["Authorization"] == "Bearer xoxb-test",
          "1) getUploadURLExternal form-encoded with filename + length")
    check(u2 == "https://files.slack.com/upload/v1/abc1" and d2 == data, "2) raw bytes POSTed to the upload URL")
    body = json.loads(d3)
    check(u3.endswith("files.completeUploadExternal") and h3["Content-Type"].startswith("application/json")
          and body == {"files": [{"id": "F1", "title": "Quote card"}], "channel_id": "C123",
                       "initial_comment": "hi", "thread_ts": "1.2"}, "3) completeUploadExternal JSON payload")
    check(link == "https://team.slack.com/files/U/F1/x", "permalink from completeUploadExternal")

    http = FakeHttp(permalink=False)
    link = media.upload_file("C123", "a.mp3", b"id3", "Brief", http=http)
    body = json.loads(http.calls[2][1])
    check("thread_ts" not in body and "initial_comment" not in body, "unset fields left out")
    check(link == "https://info/F1" and len(http.calls) == 4, "falls back to files.info for the permalink")

    http = FakeHttp()
    up = media.upload_files("C9", [("a.pdf", b"%PDF", "A"), ("b.png", b"png", "B")], http=http)
    completes = [c for c in http.calls if c[0].endswith("completeUploadExternal")]
    check(len(http.calls) == 5 and len(completes) == 1 and [f["permalink"] is not None for f in up] == [True, True],
          "several files share one completeUploadExternal")

    def failing(url, data, headers):
        return json.dumps({"ok": False, "error": "not_in_channel"}).encode()
    try:
        media.upload_file("C1", "x.png", b"x", "x", http=failing)
        check(False, "Slack error raised")
    except media.MediaError as e:
        check("not_in_channel" in str(e), "Slack errors surface")


class FakeCommunicate:
    seen = []

    def __init__(self, text, voice):
        FakeCommunicate.seen.append((text, voice))

    async def stream(self):
        for chunk in ({"type": "audio", "data": b"ID3"}, {"type": "WordBoundary", "offset": 1},
                      {"type": "audio", "data": b"-mp3"}):
            yield chunk


def test_tts():
    audio = media.text_to_speech("Good morning 👋 team.", communicate=FakeCommunicate)
    check(audio == b"ID3-mp3", "audio chunks collected, other events ignored")
    check(FakeCommunicate.seen[-1] == ("Good morning team.", "en-US-AndrewNeural"), "default voice, emoji stripped")

    async def inside_loop():
        return media.text_to_speech("hi", voice="en-GB-SoniaNeural", communicate=FakeCommunicate)
    check(asyncio.run(inside_loop()) == b"ID3-mp3", "works when an event loop is already running")


def test_tools():
    calls = []
    real_client, real_upload, real_uploads, real_tts = media._client, media.upload_file, media.upload_files, media.text_to_speech
    media._client = lambda: ("alexvacca", "Alex Vacca", "C42")
    media.upload_file = lambda ch, fn, data, title, initial_comment=None, thread_ts=None, http=None: (
        calls.append((ch, fn, data[:8])) or f"https://slack/{fn}")
    media.upload_files = lambda ch, files, initial_comment=None, thread_ts=None, http=None: (
        calls.append((ch, [f[0] for f in files])) or [{"permalink": f"https://slack/{f[0]}"} for f in files])
    media.text_to_speech = lambda text, voice="en-US-AndrewNeural", communicate=None: b"ID3"
    try:
        out = json.loads(media.tool_quote_card({"text": "Reach is a skill.", "theme": "light"}))
        check(out["success"] and out["permalink"].endswith(".png") and calls[-1][0] == "C42"
              and calls[-1][2] == PNG, "make_quote_card tool uploads a PNG to the client's channel")
        out = json.loads(media.tool_carousel({"slides": [{"title": "Cover"}, {"title": "One", "body": "x"}],
                                              "title": "My deck"}))
        check(out["success"] and out["slides"] == 2 and calls[-1][1][0] == "my-deck.pdf" and len(calls[-1][1]) == 3,
              "make_carousel tool uploads the PDF plus PNGs")
        out = json.loads(media.tool_voice_brief({"text": "Morning brief", "title": "Monday brief"}))
        check(out["success"] and out["permalink"] == "https://slack/monday-brief.mp3", "voice_brief tool uploads an MP3")
        out = json.loads(media.tool_carousel({"slides": []}))
        check(out["success"] is False and "slide" in out["error"], "tool errors come back as success: false")
    finally:
        media._client, media.upload_file, media.upload_files, media.text_to_speech = real_client, real_upload, real_uploads, real_tts
    os.environ.pop("CLIENT_HANDLE", None)
    out = json.loads(media.tool_quote_card({"text": "x"}))
    check(out["success"] is False and "CLIENT_HANDLE" in out["error"], "no client profile → clear error")

    registered = []

    class Ctx:
        def register_tool(self, **kw):
            registered.append(kw)
    media.register(Ctx())
    check([r["name"] for r in registered] == ["make_quote_card", "make_carousel", "voice_brief"]
          and all(r["toolset"] == "media" for r in registered), "tools registered under the media toolset")


if __name__ == "__main__":
    test_quote_card()
    test_carousel()
    test_upload()
    test_tts()
    test_tools()
    print("all good")
