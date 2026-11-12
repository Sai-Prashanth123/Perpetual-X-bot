"""Offline tests: Audience Seeding sync (fake GitHub), tools over a tiny DuckDB snapshot, daily Slack post,
and slack_send (people + channels, with docs and files).

    DATABASE_URL=postgresql://... uv run --no-project --with "psycopg[binary]" --with duckdb --with pytz tests/test_audience.py
"""

import json
import os
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
DATA = Path(tempfile.mkdtemp())
(DATA / "cache" / "documents").mkdir(parents=True)
os.environ.update(OWNER_SLACK_IDS="UOWNER", PX_DATA_ROOT=str(DATA), HQ_SLACK_CHANNEL="CHQ0001",
                  SEEDING_GITHUB_TOKEN="test")
os.environ.pop("VOYAGE_API_KEY", None)

import duckdb  # noqa: E402

from perpetual_core import audience, ledger, sharing  # noqa: E402

TODAY = date.today()


def check(cond, msg):
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    print(f"ok   {msg}")


def make_snapshot(path):
    con = duckdb.connect(str(path))
    now = datetime.now(timezone.utc)
    con.execute("CREATE TABLE kpis AS SELECT 1000 posts, 200 authors, 300 profiles, 250 bios, ?::TIMESTAMPTZ newest", [now])
    con.execute("""CREATE TABLE trust_checks("check" VARCHAR, status VARCHAR, detail VARCHAR, checked_at TIMESTAMPTZ)""")
    con.execute("INSERT INTO trust_checks VALUES ('Data is fresh','pass','',now()), ('Enough data','warn','x',now())")
    con.execute("""CREATE TABLE engage_list(list_date DATE, client VARCHAR, list_rank INT, tweet_id VARCHAR, url VARCHAR,
                   author VARCHAR, seg VARCHAR, text VARCHAR, created_at TIMESTAMPTZ, score DOUBLE, reason VARCHAR,
                   built_at TIMESTAMPTZ)""")
    for i in range(1, 4):
        con.execute("INSERT INTO engage_list VALUES (?, 'Kenny_GTM', ?, ?, ?, ?, 'b2b', ?, now(), 1.0, 'fresh, on topic', now())",
                    [TODAY, i, f"t{i}", f"https://x.com/a{i}/status/{i}", f"a{i}", f"cold email post {i}"])
    con.execute("CREATE TABLE engage_coverage(list_date DATE, client VARCHAR, listed BIGINT, replied BIGINT)")
    con.execute("INSERT INTO engage_coverage VALUES (?, 'Kenny_GTM', 3, 1)", [TODAY])
    con.execute("""CREATE TABLE pattern_memory(client VARCHAR, dim VARCHAR, value VARCHAR, status VARCHAR, direction VARCHAR,
                   since VARCHAR, weeks_cleared BIGINT, effect DOUBLE, lo DOUBLE, hi DOUBLE, n_own BIGINT, source VARCHAR,
                   weeks_seen BIGINT, reach_only BOOLEAN, group_clients BIGINT)""")
    con.execute("INSERT INTO pattern_memory VALUES ('Kenny_GTM','format','thread','habit','better','w1',3,0.4,0.1,0.7,12,'own',3,false,5)")
    con.execute("""CREATE TABLE content_ci(client VARCHAR, dim VARCHAR, value VARCHAR, posts BIGINT, rel DOUBLE, rel_lo DOUBLE,
                   rel_hi DOUBLE, status VARCHAR, niche DOUBLE, niche_lo DOUBLE, niche_hi DOUBLE, niche_posts BIGINT)""")
    con.execute("INSERT INTO content_ci VALUES ('Kenny_GTM','hook','question',9,1.6,1.1,2.2,'proven better',0.4,0.3,0.5,9)")
    con.execute("CREATE TABLE learning_mode(client VARCHAR, mode VARCHAR, signals VARCHAR)")
    con.execute("INSERT INTO learning_mode VALUES ('Kenny_GTM','normal','')")
    con.execute("""CREATE TABLE post_perf(client VARCHAR, tweet_id VARCHAR, url VARCHAR, created_at TIMESTAMPTZ, text VARCHAR,
                   format VARCHAR, hook VARCHAR, topic VARCHAR, view_count BIGINT, like_count BIGINT, reply_count BIGINT,
                   xscore DOUBLE)""")
    con.execute("INSERT INTO post_perf VALUES ('Kenny_GTM','p1','https://x.com/k/1',now(),'hi','thread','question','gtm',1000,10,2,5.0)")
    con.execute("""CREATE TABLE live_preds(tweet_id VARCHAR, client VARCHAR, created_at TIMESTAMPTZ, format VARCHAR, hook VARCHAR,
                   expected_vs_usual DOUBLE)""")
    con.execute("""CREATE TABLE track_weekly(client VARCHAR, "group" VARCHAR, week TIMESTAMP, engagers BIGINT,
                   niche_share DOUBLE, period VARCHAR)""")
    con.execute("CREATE TABLE account_segments(username VARCHAR, segment VARCHAR, also VARCHAR, confidence FLOAT, n_posts BIGINT)")
    con.execute("INSERT INTO account_segments VALUES ('a1','b2b','ai',0.9,40)")
    con.execute("CREATE TABLE seed_map(seg VARCHAR, rank BIGINT, username VARCHAR, seed_score DOUBLE, reason VARCHAR)")
    con.execute("INSERT INTO seed_map VALUES ('b2b',7,'a1',0.8,'replies to core')")
    con.execute("CREATE TABLE core_accounts(core_segment VARCHAR, core_rank BIGINT, username VARCHAR, core_score DOUBLE)")
    con.execute("CREATE TABLE profiles(handle VARCHAR, description VARCHAR, followers_count BIGINT)")
    con.close()
    return path.read_bytes()


def fake_github(snapshot, sha):
    def get(path, raw=False, timeout=0):
        if path == "commits/main":
            return {"sha": sha}
        if path.startswith("contents/reports?"):
            return [{"type": "file", "name": "proof.md"}, {"type": "file", "name": "2_weekly.md"}]
        if path.startswith("contents/reports/"):
            return f"# {path.split('/')[-1].split('?')[0]}\nreport body".encode()
        if path.startswith("contents/data/dashboard.duckdb"):
            return snapshot
        raise audience.AudienceError("GitHub 404")
    return get


def main():
    conn = ledger.connect()
    ledger.ensure_schema(conn)
    conn.execute("DELETE FROM audience_state")
    conn.commit()
    snap = make_snapshot(Path(tempfile.mkdtemp()) / "dash.duckdb")

    # --- sync: downloads, records state, skips when unchanged
    msg = audience.sync(conn, get=fake_github(snap, "a" * 40))
    check(msg.startswith("synced aaaaaaa") and audience.current_dir(), "sync downloads the snapshot")
    st = conn.execute("SELECT * FROM audience_state").fetchone()
    check(st["trust_ok"] is True, "warn is not a failure; trust ok")
    check(audience.sync(conn, get=fake_github(snap, "a" * 40)).startswith("up to date"), "unchanged repo: no download")
    audience.sync(conn, get=fake_github(snap, "b" * 40))
    audience.sync(conn, get=fake_github(snap, "c" * 40))
    kept = [d.name for d in audience.root().iterdir() if d.is_dir()]
    check(len(kept) == 2 and "cccccccccccc" in kept, "keeps the newest two versions")

    # --- tools
    e = audience.engage("kenny_gtm")
    check(e["client"] == "Kenny_GTM" and len(e["posts"]) == 3 and e["posts"][0]["list_rank"] == 1,
          "engage list (client spelling matched)")
    v = audience.client_view("@Kenny_GTM")
    check(v["proven_patterns"][0]["status"] == "habit" and v["content_ranges"][0]["status"] == "proven better",
          "client view: habits + proven ranges")
    check(audience.account("@a1")["seed_map"][0]["rank"] == 7, "account lookup")
    check(audience.query("select count(*) n from engage_list")["rows"] == [[3]], "read-only SQL works")
    for bad in ("drop table kpis", "select 1; drop table kpis", "select * from read_csv('/etc/passwd')",
                "attach '/tmp/x.db'", "copy kpis to '/tmp/x.csv'"):
        try:
            audience.query(bad)
            check(False, f"blocked: {bad}")
        except audience.AudienceError:
            pass
    check(True, "writes, file reads and multiple statements are refused")
    check("proof" in audience.report()["reports"] and "report body" in audience.report("weekly")["text"], "reports")
    check(audience.freshness(conn)["warning"] is None, "fresh data: no warning")

    # --- daily post: HQ summary + bundle, Engage list into the client's channel, once a day
    posts, uploads = [], []
    conn.execute("""INSERT INTO clients(handle, display_name, active, slack_channel, platform)
                    VALUES ('kenny_gtm','Kenny Damian',true,'CKENNY01','x')
                    ON CONFLICT (handle) DO UPDATE SET active = true, slack_channel = 'CKENNY01', platform = 'x'""")
    conn.commit()
    done = audience.post_daily(conn, slack=lambda m, **kw: posts.append((m, kw)) or {"ok": True},
                               upload=lambda ch, files, initial_comment=None: uploads.append((ch, files, initial_comment)))
    check(uploads and uploads[0][0] == "CHQ0001" and b"report body" in uploads[0][1][0][1]
          and "Audience Seeding" in uploads[0][2], "HQ gets the summary + all reports")
    kp = [kw for m, kw in posts if kw.get("channel") == "CKENNY01"]
    check(kp and "Engage today" in kp[0]["text"] and "x.com/a1" in kp[0]["text"], "client channel gets its Engage list")
    check(audience.post_daily(conn, slack=lambda *a, **k: {}, upload=lambda *a, **k: []) == [], "only once a day")
    check(any("@kenny_gtm" in d for d in done), "post_daily reports what it posted")

    # --- LinkedIn clients are on the roster but not in X jobs
    from perpetual_core import agency
    conn.execute("""INSERT INTO clients(handle, display_name, active, platform) VALUES ('gilallouche_t','Gil',true,'linkedin')
                    ON CONFLICT (handle) DO UPDATE SET active = true, platform = 'linkedin'""")
    conn.commit()
    check("gilallouche_t" in [c["handle"] for c in agency.list_clients(conn)]
          and "gilallouche_t" not in [c["handle"] for c in agency.active_clients(conn)], "LinkedIn client: roster yes, X jobs no")

    # --- slack_send: a person (Drive share + DM + file) and a channel
    people = [{"id": "UOWNER", "name": "sai", "real_name": "Sai", "display_name": "sai", "email": "sai@tp.com"},
              {"id": "UNAGA", "name": "naga", "real_name": "Naga Pramod", "display_name": "naga", "email": "naga@tp.com"}]
    calls, msgs, ups = [], [], []
    sharing._google_token = lambda conn, by: "gtok"

    def slack(method, **kw):
        msgs.append((method, kw))
        if method == "conversations.list":
            return {"channels": [{"id": "CGEN0001", "name": "general", "is_member": False, "is_private": False}]}
        if method == "conversations.open":
            return {"channel": {"id": "D" + kw["users"]}}
        return {"ok": True}

    f = DATA / "cache" / "documents" / "deck.pdf"
    f.write_bytes(b"%PDF")
    doc = "https://docs.google.com/presentation/d/1AbCdEfGhIjKlMnOpQrStUvWxYz012345/edit"
    out = sharing.send(conn, ["Naga", "#general", "Nobody Here"], f"Please review {doc}", files=[str(f)],
                       by="UOWNER", people=people, call=lambda *a, **k: calls.append((a, k)) or {"id": "p"},
                       slack=slack, upload=lambda ch, files, initial_comment=None: ups.append((ch, [x[0] for x in files])))
    check([s["to"] for s in out["sent"]] == ["Naga Pramod", "#general"] and out["problems"][0]["to"] == "Nobody Here",
          "sent to the person and the channel; unknown person reported")
    check(calls and calls[0][1]["body"]["emailAddress"] == "naga@tp.com", "Google file shared with the person in Drive")
    dm = next(kw for m, kw in msgs if m == "chat.postMessage" and kw["channel"] == "UNAGA")
    check("edit access" in dm["text"] and "sent by Agency Bot for Sai" in dm["text"], "DM says access + who sent it")
    check(("conversations.join", {"channel": "CGEN0001"}) in msgs, "joins a public channel it isn't in")
    check(("DUNAGA", ["deck.pdf"]) in ups and ("CGEN0001", ["deck.pdf"]) in ups, "file uploaded to both")

    # --- slack_api is for owners only, and destructive methods need confirmation
    from perpetual_core import connections
    connections.current_user = lambda: "UNAGA"
    check("owners only" in sharing.tool_slack_api({"method": "conversations.history"}), "teammates can't use slack_api")
    connections.current_user = lambda: "UOWNER"
    check(json.loads(sharing.tool_slack_api({"method": "conversations.archive", "args": {"channel": "C1"}}))
          .get("needs_confirmation"), "archive needs confirmation")

    conn.execute("DELETE FROM clients WHERE handle = 'gilallouche_t'")
    conn.execute("DELETE FROM library_docs WHERE source = 'audience-seeding'")
    conn.commit()
    conn.close()
    print("all good")


if __name__ == "__main__":
    main()
