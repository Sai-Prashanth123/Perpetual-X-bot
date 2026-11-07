"""Crawl4AI in light mode (plain HTTP, no browser): crawl a site breadth-first and print the pages as
Markdown JSON. Runs in its own venv (/opt/crawl4ai) as a separate process; never imports perpetual_core.

    python crawl_worker.py '{"url": "https://example.com", "max_pages": 10, "max_depth": 1}'
"""

import asyncio
import json
import os
import sys


async def crawl(url, max_pages, max_depth):
    from crawl4ai import AsyncWebCrawler, BFSDeepCrawlStrategy, CacheMode, CrawlerRunConfig
    from crawl4ai.async_crawler_strategy import AsyncHTTPCrawlerStrategy

    cfg = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS, page_timeout=20000, word_count_threshold=5,
        excluded_tags=["nav", "footer", "script", "style", "noscript"],
        deep_crawl_strategy=(BFSDeepCrawlStrategy(max_depth=max_depth, max_pages=max_pages, include_external=False)
                             if max_depth > 0 and max_pages > 1 else None),
    )
    out = []
    async with AsyncWebCrawler(crawler_strategy=AsyncHTTPCrawlerStrategy(), base_directory="/tmp") as crawler:
        res = await crawler.arun(url, config=cfg)
        results = list(res)          # a list (deep crawl) or a CrawlResultContainer (one page): both iterate
        for r in results[:max_pages]:
            md = getattr(r, "markdown", "") or ""
            md = getattr(md, "raw_markdown", None) or str(md)
            out.append({"url": r.url, "ok": bool(r.success), "status": getattr(r, "status_code", None),
                        "title": ((getattr(r, "metadata", None) or {}).get("title") or ""),
                        "markdown": md[:30000], "error": None if r.success else (r.error_message or "")[:300]})
    return out


def main():
    args = json.loads(sys.argv[1])
    os.environ.setdefault("CRAWL4_AI_BASE_DIRECTORY", "/tmp")
    pages = asyncio.run(crawl(args["url"], int(args.get("max_pages", 10)), int(args.get("max_depth", 1))))
    sys.stdout.write(json.dumps(pages, ensure_ascii=False))


if __name__ == "__main__":
    main()
