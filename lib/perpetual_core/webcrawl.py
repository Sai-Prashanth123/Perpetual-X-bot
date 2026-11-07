"""crawl_site: read a whole website (or one page) for free with Crawl4AI in light mode (plain HTTP, no
browser, its own process), Firecrawl only as backup for pages that need JavaScript. Pages are saved to the
document library, so "what does Acme say about pricing?" works later via search_documents.

Safety: public http(s) sites only (no internal / private addresses), same-site links only, page and
size caps.
"""

import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import urllib.request
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

from . import ledger
from .settings import env, hermes_home

WORKER = Path(__file__).resolve().parent / "workers" / "crawl_worker.py"
THIN = 300          # fewer chars than this → probably a JavaScript page → try Firecrawl


class CrawlError(Exception):
    pass


def public_url(url):
    """The normalized URL if it's a public http(s) address; raises CrawlError otherwise."""
    url = (url or "").strip()
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    u = urlparse(url)
    host = u.hostname or ""
    if u.scheme not in ("http", "https") or not host or host == "localhost" or host.endswith((".local", ".internal")):
        raise CrawlError("only public http(s) websites")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise CrawlError(f"can't find {host}") from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise CrawlError("only public websites (that address is internal)")
    return url


def _python():
    p = env("CRAWL4AI_PYTHON") or "/opt/crawl4ai/bin/python"
    return p if Path(p).exists() else sys.executable


def crawl4ai(url, max_pages, max_depth):
    keep = {k: v for k, v in os.environ.items() if not re.search(r"TOKEN|SECRET|KEY|PASSWORD|DATABASE_URL", k)}
    try:
        out = subprocess.run([_python(), "-I", str(WORKER),
                              json.dumps({"url": url, "max_pages": max_pages, "max_depth": max_depth})],
                             capture_output=True, timeout=240, env=keep)
    except subprocess.TimeoutExpired:
        raise CrawlError("the site took too long (over 4 minutes)") from None
    if out.returncode != 0:
        raise CrawlError("crawler failed: " + out.stderr.decode(errors="replace").strip()[-300:])
    return json.loads(out.stdout.decode("utf-8", errors="replace") or "[]")


def firecrawl(url):
    """One page through Firecrawl (renders JavaScript). None if no key / it fails."""
    key = env("FIRECRAWL_API_KEY")
    if not key:
        return None
    body = json.dumps({"url": url, "formats": ["markdown"], "onlyMainContent": True}).encode()
    req = urllib.request.Request("https://api.firecrawl.dev/v1/scrape", data=body, method="POST",
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.loads(r.read()).get("data") or {}
        return {"url": url, "ok": True, "title": (d.get("metadata") or {}).get("title") or "",
                "markdown": (d.get("markdown") or "")[:30000], "via": "firecrawl"}
    except Exception:  # noqa: BLE001
        return None


def crawl(url, max_pages=10, max_depth=1, crawler=None, rescue=None):
    crawler, rescue = crawler or crawl4ai, rescue or firecrawl
    url = public_url(url)
    pages = crawler(url, max_pages, max_depth)
    rescued = 0
    for i, p in enumerate(pages):
        if (not p.get("ok") or len(p.get("markdown") or "") < THIN) and rescued < 3:
            better = rescue(p["url"])
            if better and len(better["markdown"]) > len(p.get("markdown") or ""):
                pages[i] = better
                rescued += 1
    if not pages:
        better = rescue(url)
        pages = [better] if better else []
    return [p for p in pages if p.get("ok") and (p.get("markdown") or "").strip()]


def save(conn, url, pages, user=None, chat=None):
    """The crawl as one library document (searchable later). Returns the library row."""
    from . import doclib
    host = urlparse(url).hostname or "site"
    text = "\n\n".join(f"# {p.get('title') or p['url']}\nSource: {p['url']}\n\n{p['markdown']}" for p in pages)
    folder = hermes_home() / "cache" / "documents"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"site-{re.sub(r'[^a-z0-9.-]', '_', host)}-{date.today().isoformat()}-{abs(hash(text)) % 10**8}.md"
    path.write_text(text, encoding="utf-8")
    return doclib.ingest(conn, path, f"Website: {host} ({date.today():%d %b %Y}, {len(pages)} pages)",
                         f"crawl:{url}", user, chat)


def tool_crawl_site(params, **_):
    from . import doclib
    try:
        max_pages = max(1, min(int(params.get("max_pages") or 10), 30))
        depth = max(0, min(int(params.get("depth") if params.get("depth") is not None else 1), 3))
        pages = crawl(params.get("url"), max_pages, depth)
        if not pages:
            return json.dumps({"success": False, "error": "no readable pages (site blocks bots or needs a login)"})
        conn = ledger.connect()
        try:
            row = save(conn, public_url(params["url"]), pages, doclib._user(), doclib._chat())
        finally:
            conn.close()
        budget, out = 60000, []
        for p in pages:
            md = p["markdown"][: max(0, min(len(p["markdown"]), budget, 12000))]
            budget -= len(md)
            out.append({"url": p["url"], "title": p.get("title"), "via": p.get("via", "crawl4ai"), "text": md})
        return json.dumps({"success": True, "pages": out, "doc_id": row["id"],
                           "note": "Saved to the document library (search_documents / read_document(doc_id) for the "
                                   "full text)." + (" Some pages were cut to fit." if budget <= 0 else "")},
                          ensure_ascii=False)
    except CrawlError as e:
        return json.dumps({"success": False, "error": str(e)})
    except Exception as e:  # noqa: BLE001
        return json.dumps({"success": False, "error": f"crawl failed: {e}"})


def register(ctx, toolset="crawl"):
    ctx.register_tool(name="crawl_site", toolset=toolset, handler=tool_crawl_site, schema={
        "name": "crawl_site",
        "description": ("Read a whole website for free: the page plus the same-site pages it links to (depth 1 "
                        "by default, up to 30 pages), as clean text. Use it for client/competitor/prospect sites, "
                        "docs, blogs. Pages needing JavaScript are fetched through Firecrawl automatically. The "
                        "result is saved to the document library. depth=0 reads just that one page."),
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string"}, "max_pages": {"type": "integer"}, "depth": {"type": "integer"}},
            "required": ["url"]}})
