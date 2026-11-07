"""Offline tests: crawl_site (Crawl4AI light + Firecrawl rescue, public-only) and Graphiti nightly ingest.

    DATABASE_URL=postgresql://... uv run --no-project --with "psycopg[binary]" --with markitdown tests/test_power.py
"""

import json
import os
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
TMP = Path(tempfile.mkdtemp())
(TMP / "cache" / "documents").mkdir(parents=True)
os.environ.update(OWNER_SLACK_IDS="UOWNER", PX_DATA_ROOT=str(TMP), HERMES_HOME=str(TMP))

from perpetual_core import agency_memory, connections, doclib, graph_memory, ledger, webcrawl  # noqa: E402


def check(cond, msg):
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    print(f"ok   {msg}")


def main():
    conn = ledger.connect()
    ledger.ensure_schema(conn)
    tag = uuid.uuid4().hex[:6]

    # ---- crawl: public addresses only
    for bad in ("http://localhost:8080", "http://127.0.0.1/admin", "http://10.0.0.5", "http://169.254.169.254/latest",
                "file:///etc/passwd"):
        try:
            webcrawl.public_url(bad)
            check(False, f"{bad} should be refused")
        except webcrawl.CrawlError:
            pass
    check(True, "internal / private / file addresses refused")

    calls = []

    def fake_crawler(url, max_pages, max_depth):
        calls.append((url, max_pages, max_depth))
        return [{"url": url, "ok": True, "title": "Home", "markdown": f"Acme helps agencies grow. Pricing from $99 {tag}. " * 20},
                {"url": url + "/app", "ok": True, "title": "App", "markdown": "Loading..."},   # JavaScript page
                {"url": url + "/down", "ok": False, "markdown": "", "error": "500"}]

    def fake_rescue(url):
        return {"url": url, "ok": True, "title": "App (rendered)", "markdown": f"Dashboard features list {tag} " * 30,
                "via": "firecrawl"} if url.endswith("/app") else None

    webcrawl.public_url = lambda u: u if u.startswith("http") else "https://" + u
    pages = webcrawl.crawl("acme.example", 5, 1, crawler=fake_crawler, rescue=fake_rescue)
    check(calls == [("https://acme.example", 5, 1)], "Crawl4AI called with page/depth caps")
    check(len(pages) == 2 and pages[1].get("via") == "firecrawl", "JavaScript page rescued by Firecrawl, dead page dropped")

    connections.current_user = lambda: "UOWNER"
    row = webcrawl.save(conn, "https://acme.example", pages, "UOWNER", "D123")
    check(row and "Website: acme.example" in row["name"], "crawl saved to the document library")
    hits = doclib.search(conn, f"acme pricing {tag}", "UOWNER")
    check(hits and hits[0]["doc_id"] == row["id"], "crawled site searchable later")

    webcrawl.crawl4ai = fake_crawler
    webcrawl.firecrawl = fake_rescue
    out = json.loads(webcrawl.tool_crawl_site({"url": "https://acme.example", "max_pages": 99, "depth": 9}))
    check(out["success"] and calls[-1][1:] == (30, 3), "tool caps pages at 30, depth at 3")

    # ---- graph: nightly ingest of new facts, progress remembered
    conn.execute("DELETE FROM graph_state")
    conn.commit()
    agency_memory.remember(conn, f"Shravya now owns the Alex Vacca account {tag}", "client:itsalexvacca")
    agency_memory.remember(conn, f"Invoices go out on the 1st {tag}", "agency")
    seen = []

    def fake_worker(cmd, payload, timeout=0):
        seen.append((cmd, payload))
        return {"added": len(payload), "failed": []}

    msg = graph_memory.ingest(conn, worker=fake_worker)
    sent = seen[-1][1]
    check(seen[-1][0] == "ingest" and any(f"owns the Alex Vacca account {tag}" in e["body"] for e in sent),
          "new facts sent to the graph")
    check(all(e["at"] and e["name"].startswith("fact-") for e in sent), "episodes carry their time")
    check("added" in msg, "nightly summary")
    seen.clear()
    check(graph_memory.ingest(conn, worker=fake_worker) == "" and not seen, "nothing new → no graph run")
    os.environ.pop("VOYAGE_API_KEY", None)
    check(json.loads(graph_memory.tool_graph_recall({"query": "x"}))["success"] is False,
          "graph_recall explains when connected memory is off")

    conn.execute("DELETE FROM agency_memory WHERE text LIKE %s", (f"%{tag}",))
    conn.execute("DELETE FROM library_docs WHERE id = %s", (row["id"],))
    conn.commit()
    conn.close()
    print("all good")


if __name__ == "__main__":
    main()
