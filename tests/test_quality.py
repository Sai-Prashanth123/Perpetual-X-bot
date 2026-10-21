"""Offline tests for the content engine: linters, fact-check, lineup, cards for the new formats,
and the teammate fan-out for article promo QRTs.

    DATABASE_URL=postgresql://... uv run --no-project --with "psycopg[binary]" tests/test_quality.py
(The database part is skipped when DATABASE_URL is unset.)
"""

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from perpetual_core import quality, slackui  # noqa: E402


def check(cond, msg):
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    print(f"ok   {msg}")


def test_lint():
    ai = "In today's fast-paced world, it's not just about outbound — it's about synergy. Let's delve in."
    human = "we booked 110 calls last month.\n\nzero DMs. the list did the work."
    check(not quality.lint(ai)["pass"], "AI-sounding text fails the linters")
    check(any("dash" in h for h in quality.lint(ai)["hard"]), "em dash is a HARD hit")
    check(quality.lint(human)["pass"], "a real-sounding post passes")
    cfg = {"do_not_say_hard": [{"pattern": r"\bAcme\b", "label": "banned name"}]}
    check(not quality.lint("we beat Acme on price", cfg)["pass"], "client lint-config bans apply")


def test_fact_check():
    receipts = ("# Receipts\n| 500+ inbound leads last month | X post |\n| $153K in new MRR | giveaway |\n\n"
                "## DO-NOT-SAY\n| Pattern | Why |\n|---|---|\n| \\$10M | ARR cap is $7M+ |\n")
    bank = {"claims": [{"claim_id": "abc", "text": "24 people posting", "numbers": ["581"],
                        "provenance": {"url": "https://youtube.com/x", "quote": "581 posts in 87 days"}}]}
    fc = quality.fact_check("We added $153K in MRR, shipped 581 posts and 500+ leads. Next: $10M, up 40%.", receipts, bank)
    st = {c["claim"]: c["status"] for c in fc["claims"]}
    check(st.get("$153K") == "verified" and st.get("500+") == "verified", "receipt numbers verified")
    check(st.get("581") == "verified" and any("claim:abc" in r for r in fc["references"]), "claim-bank number verified with provenance")
    check(st.get("40%") == "unsourced", "invented number flagged unsourced")
    check(any(c["status"] == "do_not_say" for c in fc["claims"]), "DO-NOT-SAY item blocked")
    check(not fc["pass"], "draft with unsourced/banned claims fails")
    check(quality.fact_check("$153K in MRR.", receipts)["pass"], "fully sourced draft passes")
    check(quality.fact_check("In 2026 we ran 3 steps.", receipts)["pass"], "years and list counters aren't claims")


def test_lineup():
    ins, body, ans = quality.lineup_prompt("the draft", ["a", "b", "c", "d", "e"], seed=3)
    check("the draft" in body and 1 <= ans <= 6, "draft hidden among real posts")
    check(not quality.lineup_verdict({"suspect": ans, "confidence": "HIGH", "tells": ["x"]}, ans)["pass"], "caught = fail")
    check(quality.lineup_verdict({"suspect": ans, "confidence": "LOW", "tells": []}, ans)["pass"], "low confidence = pass")


def test_cards():
    art = {"client": "acme", "code": "A1", "kind": "article", "status": "pending", "api_postable": False,
           "text": "How we booked 110 calls without one DM\n\n" + "\n\n".join(["word " * 120] * 10),
           "refs": json.dumps(["receipts.md: 110 calls", "claim:abc https://x.com/a/status/1"]),
           "factcheck": json.dumps({"summary": "2 verified, 0 unsourced"}),
           "scores": json.dumps({"voice": 9, "hook": 9, "algo": 78})}
    b = slackui.suggestion_blocks(art, x_connected=True)
    flat = json.dumps(b)
    ids = [e.get("action_id") for x in b if x["type"] == "actions" for e in x["elements"]]
    check("px_post" not in ids and "px_article_posted" in ids, "article: no Post now, has Mark as posted")
    check("References" in flat and "[2]" in flat and "Fact-check" in flat and "Algo 78" in flat, "card shows scores, fact-check, references")
    check(all(len(x["text"]["text"]) <= 3000 for x in b if x["type"] == "section"), "article card sections within 3,000 chars")
    parts = slackui.article_sections(art["text"])
    check(len(parts) >= 2 and all(len(p) <= 3000 for p in parts), "long article split into editable parts")
    modal = slackui.edit_modal({**art, "slack_channel": "C1", "slack_ts": "1"})
    check(len(modal["blocks"]) == len(parts) and modal["blocks"][1]["block_id"] == "text_1", "edit modal has one input per part")
    vq = {"client": "mich", "code": "V1", "kind": "video_qrt", "status": "pending", "api_postable": False,
          "text": "A: meanwhile me\n---\nClip: guy calmly sipping coffee as office burns · Search: \"this is fine\"",
          "intent_url": "https://x.com/intent/post?text=x"}
    vflat = json.dumps(slackui.suggestion_blocks(vq, True), ensure_ascii=False)
    check("🎬" in vflat and "this is fine" in vflat and "px_post" not in vflat, "video QRT: clip brief, no Post now")
    promo = {"client": "mich", "code": "B1", "kind": "qrt_promo", "status": "pending", "api_postable": False,
             "text": "read this", "parent_client": "alex", "parent_code": "A1"}
    pflat = json.dumps(slackui.suggestion_blocks(promo, True), ensure_ascii=False)
    check("Promotes @alex A1" in pflat and "once the article is posted" in pflat, "promo QRT waits for its article")


def test_db():
    if not os.environ.get("DATABASE_URL"):
        print("skip db (no DATABASE_URL)")
        return
    from perpetual_core import agency, ledger
    conn = ledger.connect()
    ledger.ensure_schema(conn)
    hs = ["zq_alex", "zq_mich", "zq_solo"]
    for h in hs:
        for t in ("clients", "suggestions", "counters", "content_history"):
            conn.execute(f"DELETE FROM {t} WHERE {'handle' if t == 'clients' else 'client'} = %s", (h,))
    conn.commit()
    agency.upsert_client(conn, "zq_alex"); agency.upsert_client(conn, "zq_mich"); agency.upsert_client(conn, "zq_solo")
    agency.apply_import_extras(conn, [{"handle": "zq_alex", "team": "zqteam", "package": {"article": 1},
                                       "history": [{"day": "2026-10-01", "format": "x_article", "title": "Old piece"}]},
                                      {"handle": "zq_mich", "team": "zqteam"}, {"handle": "zq_solo", "team": "solo1"}])
    check(agency.teammates(conn, "zq_alex") == ["zq_mich"], "teammates found by team key")
    check(agency.teammates(conn, "zq_solo") == [], "solo client has no teammates")
    check(any(r["title"] == "Old piece" for r in ledger.recent_content(conn, "zq_alex", days=400)), "imported history visible")
    saved = ledger.save(conn, "zq_alex", [{"kind": "article", "text": "Title\n\nbody", "references": ["r1"],
                                           "scores": {"algo": 70}}])
    check(saved[0]["code"] == "A1" and saved[0]["open_in_x"] is None, "article saved as A1, no compose link")
    pr = ledger.save(conn, "zq_mich", [{"kind": "qrt_promo", "text": "go read it", "parent_client": "zq_alex",
                                        "parent_code": "a1"}])
    check(pr[0]["code"] == "B1" and pr[0]["open_in_x"] is None, "promo saved, waits for article")
    changed = ledger.link_article_posted(conn, "zq_alex", "A1", "https://x.com/zq_alex/status/123")
    check(changed == [("zq_mich", "B1")], "posting the article updates its promo QRT")
    b1 = ledger.get(conn, "zq_mich", "B1")
    check("url=https" in (b1["intent_url"] or ""), "promo now has an Open in X quoting the article")
    for h in hs:
        for t in ("clients", "suggestions", "counters", "content_history"):
            conn.execute(f"DELETE FROM {t} WHERE {'handle' if t == 'clients' else 'client'} = %s", (h,))
    conn.commit()


if __name__ == "__main__":
    test_lint()
    test_fact_check()
    test_lineup()
    test_cards()
    test_db()
    print("all good")
